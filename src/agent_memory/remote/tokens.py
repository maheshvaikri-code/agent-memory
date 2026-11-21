"""Bearer tokens for the remote server: a JSON file of hashes, never the tokens themselves."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ..storage.atomic import atomic_write_text

ROLES = ("read-only", "read-write")
TOKEN_PREFIX = "amt_"
FILE_VERSION = 1
MAX_EXPIRY_DAYS = 3650
_NAME = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


_STALE = object()                                      # "re-read on the next use"


class TokenError(Exception):
    """A token file or token request that cannot be honoured. Never carries a token."""


class _ReadError(TokenError):
    """The file could not be read at all (as opposed to being read and found invalid)."""


@dataclass(frozen=True)
class TokenEntry:
    name: str
    role: str
    hash: str
    created_at: str
    expires_at: str = ""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    created_at: str


def new_token() -> str:
    return TOKEN_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIME)


def _parse(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, _TIME).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class TokenFile:
    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._signature = _STALE
        self._entries: List[TokenEntry] = []
        self._error = ""
        self._generation = 0

    def __repr__(self) -> str:
        return f"TokenFile({str(self._path)!r})"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def error(self) -> str:
        with self._lock:
            self._refresh()
            return self._error

    @property
    def generation(self) -> int:
        with self._lock:
            self._refresh()
            return self._generation

    # -- reading ---------------------------------------------------------------

    def _refresh(self) -> None:
        """Re-read the file when it changed. Any problem leaves no usable token (fail closed).

        A failure to stat or read the file (an OSError) is not remembered, so the next call
        retries; a file that reads but is invalid is remembered until it changes."""
        try:
            stat_result = os.stat(self._path)
            signature = (stat_result.st_mtime_ns, stat_result.st_size)
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            self._fail_transiently(exc)
            return
        if signature == self._signature:
            return
        self._generation += 1
        if signature is None:
            self._signature = signature
            self._entries, self._error = [], ""
            return
        try:
            self._entries, self._error = self._parse_file(), ""
        except _ReadError as exc:
            self._fail_transiently(exc.__cause__ or exc)
            return
        except TokenError as exc:
            self._entries, self._error = [], str(exc)
        self._signature = signature

    def _fail_transiently(self, exc: BaseException) -> None:
        self._signature = _STALE
        self._generation += 1
        self._entries = []
        self._error = f"the token file cannot be read: {type(exc).__name__}"

    def _parse_file(self) -> List[TokenEntry]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise _ReadError(f"the token file cannot be read: {type(exc).__name__}") from exc
        except ValueError:
            raise TokenError("the token file is not valid JSON") from None
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION \
                or not isinstance(data.get("tokens"), list):
            raise TokenError("the token file is not a version 1 token file")
        entries, names = [], set()
        for raw in data["tokens"]:
            try:
                entry = TokenEntry(raw["name"], raw["role"], raw["hash"], raw["created_at"],
                                   raw.get("expires_at", ""))
            except (KeyError, TypeError):
                raise TokenError("a token entry is missing a field") from None
            if not all(isinstance(value, str) for value in (
                    entry.name, entry.role, entry.hash, entry.created_at, entry.expires_at)):
                raise TokenError("a token entry has an invalid field type")
            if not _NAME.fullmatch(str(entry.name)) or entry.role not in ROLES \
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.hash)) \
                    or entry.name in names:
                raise TokenError("a token entry has an invalid name, role or hash")
            names.add(entry.name)
            entries.append(entry)
        return entries

    def entries(self) -> List[TokenEntry]:
        with self._lock:
            self._refresh()
            return list(self._entries)

    def usable(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return sum(1 for entry in self.entries() if not self._expired(entry, now))

    @staticmethod
    def _expired(entry: TokenEntry, now: datetime) -> bool:
        if not entry.expires_at:
            return False
        moment = _parse(entry.expires_at)
        return moment is None or moment <= now         # an unreadable expiry counts as expired

    def verify(self, token: str, now: Optional[datetime] = None) -> Optional[Principal]:
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return None
        now = now or datetime.now(timezone.utc)
        digest = hash_token(token)
        found = None
        for entry in self.entries():                   # every entry is compared: no early exit
            if hmac.compare_digest(entry.hash, digest):
                found = entry
        if found is None or self._expired(found, now):
            return None
        return Principal(found.name, found.role, found.created_at)

    # -- writing ---------------------------------------------------------------

    def _write(self, entries: List[TokenEntry]) -> None:
        body = {"version": FILE_VERSION, "tokens": [
            {"name": e.name, "role": e.role, "hash": e.hash, "created_at": e.created_at,
             "expires_at": e.expires_at} for e in entries]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            descriptor = os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
        atomic_write_text(self._path, json.dumps(body, indent=2) + "\n")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass                                       # best effort (Windows)
        self._signature = _STALE                       # force a re-read on the next use

    def create(self, name: str, role: str, expires_in_days: Optional[int] = None,
               now: Optional[datetime] = None) -> str:
        return self.create_entry(name, role, expires_in_days, now)[0]

    def create_entry(self, name: str, role: str, expires_in_days: Optional[int] = None,
                     now: Optional[datetime] = None) -> Tuple[str, TokenEntry]:
        """Like `create`, and also the stored entry, so a caller needs no re-read of the file."""
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise TokenError("a name is 1-64 letters, digits or _.@-")
        if role not in ROLES:
            raise TokenError(f"a role is one of: {', '.join(ROLES)}")
        if expires_in_days is not None and not (
                isinstance(expires_in_days, int) and 1 <= expires_in_days <= MAX_EXPIRY_DAYS):
            raise TokenError(f"expires-in is a whole number of days from 1 to {MAX_EXPIRY_DAYS}")
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._signature = _STALE
            self._refresh()
            if self._error:
                raise TokenError(f"{self._error}; fix or remove it first")
            if any(entry.name == name for entry in self._entries):
                raise TokenError(f"a token named {name!r} already exists")
            token = new_token()
            expires = _stamp(now + timedelta(days=expires_in_days)) if expires_in_days else ""
            entry = TokenEntry(name, role, hash_token(token), _stamp(now), expires)
            self._write(self._entries + [entry])
        return token, entry

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._signature = _STALE
            self._refresh()
            if self._error:
                raise TokenError(f"{self._error}; fix or remove it first")
            kept = [entry for entry in self._entries if entry.name != name]
            if len(kept) == len(self._entries):
                return False
            self._write(kept)
            return True
