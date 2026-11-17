"""The journal file: header, checksummed transaction lines, and an incremental tail reader."""
from __future__ import annotations

import json
import os
import zlib
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

_SEPARATORS = (",", ":")


def encode_header(generation: str) -> bytes:
    return (json.dumps({"journal": 1, "generation": generation}, separators=_SEPARATORS)
            + "\n").encode("utf-8")


def parse_header(line: bytes) -> Optional[str]:
    try:
        data = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if (isinstance(data, dict) and data.get("journal") == 1
            and isinstance(data.get("generation"), str)):
        return data["generation"]
    return None


def encode_op(op: list) -> str:
    """JSON text of one op. Raises TypeError/ValueError, naming the op, for a value that
    cannot be journaled - called *before* the graph is touched."""
    try:
        return json.dumps(op, separators=_SEPARATORS, allow_nan=False)
    except TypeError as exc:
        raise TypeError(f"cannot journal {op[0]!r} on {op[1:3]!r}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"cannot journal {op[0]!r} on {op[1:3]!r}: {exc}") from exc


def encode_tx(seq: int, actor: str, ts: str, op_texts: List[str]) -> bytes:
    head = json.dumps({"seq": seq, "actor": actor, "ts": ts}, separators=_SEPARATORS)
    body = (head[:-1] + ',"ops":[' + ",".join(op_texts) + "]}").encode("utf-8")
    return b"%08x " % (zlib.crc32(body) & 0xFFFFFFFF) + body + b"\n"


def decode_tx(line: bytes) -> Optional[dict]:
    """The transaction in one line (without its newline), or None when it is not valid."""
    if len(line) < 10 or line[8:9] != b" ":
        return None
    try:
        crc = int(line[:8], 16)
    except ValueError:
        return None
    body = line[9:]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    try:
        tx = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not (isinstance(tx, dict) and isinstance(tx.get("seq"), int)
            and isinstance(tx.get("ops"), list)):
        return None
    return tx


@dataclass
class Tail:
    generation: Optional[str]          # header generation; None when absent or unreadable
    txs: List[dict] = field(default_factory=list)
    valid_end: int = 0                 # byte offset just after the last valid line
    size: int = 0                      # file size seen
    torn: bool = False                 # an unfinished last line follows valid_end
    corrupt_at: Optional[int] = None   # byte offset of a damaged *complete* line
    shrunk: bool = False               # the file is shorter than the offset asked for
    signature: Optional[Tuple[int, int]] = None   # (size, mtime_ns) taken before reading
    # Set when the file could not be read (OSError: a share lock, a directory, an I/O error) or
    # its first line is complete but not a header (the lines after it cannot be trusted to
    # belong to any generation). generation is None then, but the caller must retry under the
    # lock and never treat it as an absent journal. A missing file, an empty one, or a single
    # unterminated line (a crash while the header was written) is absent, not an error.
    error: Optional[str] = None


def read_tail(path, offset: int) -> Tail:
    """Read the transactions after byte `offset` (0 = from the start). Takes no lock.

    Never raises: a missing or empty file, or one unterminated line, gives
    Tail(generation=None) with error None; any other failure to read, and a damaged header,
    gives Tail(generation=None, error=...)."""
    try:
        return _read_tail(path, offset)
    except FileNotFoundError:
        return Tail(generation=None)
    except (OSError, RecursionError) as exc:
        return Tail(generation=None, error=f"{type(exc).__name__}: {exc}")


def _read_tail(path, offset: int) -> Tail:
    with open(path, "rb") as handle:
        st = os.fstat(handle.fileno())
        signature = (st.st_size, st.st_mtime_ns)
        first = handle.readline()
        if not first.endswith(b"\n"):     # empty, or a header cut short by a crash: absent
            return Tail(None, size=st.st_size, signature=signature)
        generation = parse_header(first)
        if generation is None:            # a complete line that is not a header: damaged
            return Tail(None, size=st.st_size, signature=signature,
                        error="the header on line 1 (byte 0) is damaged")
        start = max(offset, len(first))
        handle.seek(start)
        rest = handle.read()
        actual = os.fstat(handle.fileno()).st_size
    if offset > actual:
        return Tail(generation, valid_end=len(first), size=actual, shrunk=True,
                    signature=signature)
    lines = rest.split(b"\n")
    fragment = lines.pop()               # b"" when the data ends with a newline
    txs: List[dict] = []
    pos, corrupt_at = start, None
    for raw in lines:
        tx = decode_tx(raw)
        if tx is None:
            corrupt_at = pos
            break
        txs.append(tx)
        pos += len(raw) + 1
    return Tail(generation, txs, pos, actual, torn=corrupt_at is None and bool(fragment),
                corrupt_at=corrupt_at, signature=signature)
