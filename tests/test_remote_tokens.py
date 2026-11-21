import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_memory.remote.tokens import (ROLES, TOKEN_PREFIX, Principal, TokenError, TokenFile,
                                        hash_token, new_token)


@pytest.fixture
def tf(tmp_path):
    return TokenFile(tmp_path / "tokens.json")


def test_a_new_token_has_the_prefix_and_enough_entropy():
    token = new_token()
    assert token.startswith(TOKEN_PREFIX) and len(token) == len(TOKEN_PREFIX) + 43
    assert new_token() != token


def test_create_returns_the_token_once_and_the_file_holds_only_its_hash(tf):
    token = tf.create("base44-prod", "read-write")
    raw = tf.path.read_text(encoding="utf-8")
    assert token not in raw and hash_token(token) in raw
    entry = json.loads(raw)["tokens"][0]
    assert entry["name"] == "base44-prod" and entry["role"] == "read-write"
    assert set(entry) == {"name", "role", "hash", "created_at", "expires_at"}


def test_verify_returns_the_principal_for_a_good_token_and_none_otherwise(tf):
    token = tf.create("alice", "read-only")
    principal = tf.verify(token)
    assert principal == Principal("alice", "read-only", principal.created_at)
    assert tf.verify(token + "x") is None and tf.verify("") is None and tf.verify("amt_nope") is None


def test_names_are_validated_and_unique(tf):
    for bad in ["", " ", "a b", "x" * 65, "naïve", "a/b"]:
        with pytest.raises(TokenError):
            tf.create(bad, "read-only")
    tf.create("alice", "read-only")
    with pytest.raises(TokenError, match="already"):
        tf.create("alice", "read-write")


def test_roles_are_a_closed_set(tf):
    assert ROLES == ("read-only", "read-write")
    with pytest.raises(TokenError):
        tf.create("alice", "admin")


def test_expiry(tf):
    token = tf.create("alice", "read-write", expires_in_days=1)
    now = datetime.now(timezone.utc)
    assert tf.verify(token, now=now) is not None
    assert tf.verify(token, now=now + timedelta(days=2)) is None
    assert tf.usable(now=now + timedelta(days=2)) == 0 and tf.usable(now=now) == 1
    with pytest.raises(TokenError):
        tf.create("bob", "read-only", expires_in_days=0)


def test_revoke_applies_on_the_next_verify_without_a_restart(tf):
    token = tf.create("alice", "read-write")
    assert tf.verify(token) is not None
    assert tf.revoke("alice") is True and tf.revoke("alice") is False
    assert tf.verify(token) is None


def test_another_process_editing_the_file_is_noticed(tf, tmp_path):
    token = tf.create("alice", "read-write")
    other = TokenFile(tmp_path / "tokens.json")           # as the `amem token` command would
    other.revoke("alice")
    assert tf.verify(token) is None


@pytest.mark.parametrize("content", ["", "{", "[]", '{"version": 2, "tokens": []}',
                                     '{"version": 1, "tokens": [{"name": "a"}]}',
                                     '{"version": 1, "tokens": [{"name": "a", "role": "root", '
                                     '"hash": "x", "created_at": "", "expires_at": ""}]}'])
def test_a_damaged_file_fails_closed_and_recovers(tf, content):
    token = tf.create("alice", "read-write")
    good = tf.path.read_text(encoding="utf-8")
    tf.path.write_text(content, encoding="utf-8")
    assert tf.verify(token) is None and tf.error and tf.usable() == 0
    tf.path.write_text(good, encoding="utf-8")
    os.utime(tf.path, (1, 2 ** 31 - 1))                    # a new modification time
    assert tf.verify(token) is not None and tf.error == ""


def test_a_missing_file_means_no_tokens_not_an_error(tmp_path):
    tf = TokenFile(tmp_path / "none.json")
    assert tf.verify("amt_x") is None and tf.usable() == 0 and tf.error == ""


def test_generation_changes_when_the_file_is_reread(tf):
    tf.create("alice", "read-only")
    first = tf.generation
    tf.create("bob", "read-only")
    tf.entries()
    assert tf.generation != first


@pytest.mark.skipif(os.name == "nt", reason="file modes are best effort on Windows")
def test_the_file_is_private(tf):
    tf.create("alice", "read-only")
    assert stat.S_IMODE(os.stat(tf.path).st_mode) == 0o600


def test_a_token_is_not_in_the_repr_or_an_error(tf):
    token = tf.create("alice", "read-only")
    assert token not in repr(tf) and token not in repr(tf.verify(token))
    with pytest.raises(TokenError) as excinfo:
        tf.create("alice", "read-only")
    assert token not in str(excinfo.value)


def test_a_transient_read_error_fails_closed_then_is_retried(tf, tmp_path, monkeypatch):
    token = tf.create("alice", "read-write")
    fresh = TokenFile(tf.path)
    real, calls = Path.read_text, []

    def flaky(self, *args, **kwargs):
        if self == tf.path and len(calls) < 2:     # fails twice, each access retries
            calls.append(1)
            raise PermissionError("being replaced")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert fresh.verify(token) is None
    assert "PermissionError" in fresh.error                  # the second attempt, also failing
    assert calls == [1, 1]
    assert fresh.verify(token) is not None and fresh.error == ""      # retried, file untouched


def test_a_stat_error_fails_closed_without_raising_and_recovers(tf, monkeypatch):
    token = tf.create("alice", "read-write")
    fresh = TokenFile(tf.path)
    real, failing = os.stat, [True]

    def stat(path, *args, **kwargs):
        if failing[0] and str(path) == str(tf.path):
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    assert fresh.verify(token) is None and fresh.usable() == 0 and fresh.entries() == []
    assert "PermissionError" in fresh.error and fresh.generation >= 0
    assert token not in fresh.error
    failing[0] = False
    assert fresh.verify(token) is not None and fresh.error == ""


def test_create_and_revoke_refuse_a_damaged_file_and_leave_it_unchanged(tf):
    tf.create("alice", "read-write")
    tf.path.write_text("{ not json", encoding="utf-8")
    before = tf.path.read_bytes()
    with pytest.raises(TokenError, match="fix or remove it first"):
        tf.create("bob", "read-only")
    with pytest.raises(TokenError, match="fix or remove it first"):
        tf.revoke("alice")
    assert tf.path.read_bytes() == before
