"""A store is keyed by its real path, and an atomic write goes through a link to its target and
keeps the target's permission bits (final review I4).

Creating a symlink needs a privilege on Windows. Where it cannot be created the symlink tests
skip, and the `*_proxy` tests stand in: they map one name onto another file through a patched
os.path.realpath, which is exactly what a symlink does to the code under test.
"""
import os
import stat

import pytest

from agent_memory import AgentMemory, StoreInUse
from agent_memory.storage import JournalBackend
from agent_memory.storage.atomic import atomic_write_text


def _can_symlink(tmp_path):
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(tmp_path / "nowhere", probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


@pytest.fixture
def link_to(monkeypatch):
    """Make `link` resolve to `target` the way a symlink would, without creating one."""
    real_realpath = os.path.realpath
    links = {}

    def fake_realpath(path, *args, **kwargs):
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key in links:
            return links[key]
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", fake_realpath)

    def make(link, target):
        links[os.path.normcase(os.path.abspath(os.fspath(link)))] = real_realpath(target)
    return make


def _rules(memory):
    return sorted(n.properties.get("rule") for n in memory.graph.nodes("convention"))


# -- atomic_write_text -------------------------------------------------------

def test_atomic_write_goes_through_a_link_proxy(tmp_path, link_to):
    (tmp_path / "real").mkdir()
    (tmp_path / "wt").mkdir()
    target, link = tmp_path / "real" / "t.memory", tmp_path / "wt" / "t.memory"
    target.write_text("old", encoding="utf-8")
    link.write_text("stands in for the link", encoding="utf-8")
    link_to(link, target)
    atomic_write_text(link, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert link.read_text(encoding="utf-8") == "stands in for the link"   # not replaced
    assert sorted(os.listdir(tmp_path / "wt")) == ["t.memory"]          # no temp left there


def test_atomic_write_keeps_a_symlink_and_writes_its_target(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks cannot be created here")
    target, link = tmp_path / "t.memory", tmp_path / "l.memory"
    target.write_text("old", encoding="utf-8")
    os.symlink(target, link)
    atomic_write_text(link, "new")
    assert os.path.islink(link) and target.read_text(encoding="utf-8") == "new"
    plain = AgentMemory("p")
    plain.remember("convention", {"name": "n", "rule": "saved through a link"})
    plain.save(str(link))
    assert os.path.islink(link)
    assert _rules(AgentMemory.load(str(target))) == ["saved through a link"]


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod only sets the read-only flag")
def test_atomic_write_keeps_the_targets_permission_bits(tmp_path):
    target = tmp_path / "secret.memory"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o600)
    atomic_write_text(target, "new")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    plain = AgentMemory("p")
    plain.save(str(target))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


# -- the store's identity ----------------------------------------------------

def test_a_store_is_keyed_by_its_real_path_proxy(tmp_path, link_to):
    (tmp_path / "real").mkdir()
    (tmp_path / "wt").mkdir()
    real, link = tmp_path / "real" / "p.memory", tmp_path / "wt" / "p.memory"
    AgentMemory.open(str(real)).close()
    os.link(real, link)                                 # two names, one file
    link_to(link, real)
    assert JournalBackend(link).path == os.path.realpath(real)
    a, b = AgentMemory.open(str(real), agent="a"), AgentMemory.open(str(link), agent="b")
    a.remember("convention", {"name": "one", "rule": "via the real name"})
    b.remember("convention", {"name": "two", "rule": "via the link"})
    a.refresh()
    assert _rules(a) == _rules(b) == ["via the link", "via the real name"]
    assert sorted(os.listdir(tmp_path / "wt")) == ["p.memory"]   # no journal or lock there
    b.save(str(link))                                   # the store's own path: a checkpoint
    assert "via the link" in _rules(AgentMemory.load(str(real)))
    assert _rules(AgentMemory.load(str(link))) == ["via the link", "via the real name"]
    a.close()
    b.close()


def test_plain_save_through_a_link_proxy_to_a_live_store_is_refused(tmp_path, link_to):
    real, link = tmp_path / "p.memory", tmp_path / "alias.memory"
    shared = AgentMemory.open(str(real))
    shared.remember("convention", {"name": "n", "rule": "acknowledged"})
    link_to(link, real)
    with pytest.raises(StoreInUse):
        AgentMemory("plain").save(str(link))
    assert _rules(AgentMemory.load(str(link))) == ["acknowledged"]    # the journal replayed
    shared.close()


def test_two_symlinked_names_share_one_store(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks cannot be created here")
    real = tmp_path / "real.memory"
    (tmp_path / "wt1").mkdir()
    (tmp_path / "wt2").mkdir()
    AgentMemory.open(str(real)).close()
    l1, l2 = tmp_path / "wt1" / "project.memory", tmp_path / "wt2" / "project.memory"
    os.symlink(real, l1)
    os.symlink(real, l2)
    m1, m2 = AgentMemory.open(str(l1), agent="w1"), AgentMemory.open(str(l2), agent="w2")
    m1.remember("convention", {"name": "one", "rule": "from worktree 1"})
    m2.remember("convention", {"name": "two", "rule": "from worktree 2"})
    m1.refresh()
    assert _rules(m1) == _rules(m2) == ["from worktree 1", "from worktree 2"]
    m1.close()
    m2.close()
    assert os.path.islink(l1) and os.path.islink(l2)
    assert sorted(os.listdir(tmp_path / "wt1")) == ["project.memory"]
    assert _rules(AgentMemory.load(str(real))) == ["from worktree 1", "from worktree 2"]
