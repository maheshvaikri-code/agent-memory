import datetime

import pytest

from agent_memory.storage import journal


def tx(seq, name="a"):
    ops = [journal.encode_op(["n", "note", f"{name}{seq}", {"text": "hé"}])]
    return journal.encode_tx(seq, "agent-1", "2026-11-17T00:00:00+00:00", ops)


def make(path, n=3, generation="g1"):
    data = journal.encode_header(generation) + b"".join(tx(i) for i in range(1, n + 1))
    path.write_bytes(data)
    return data


def test_header_round_trip_and_rejects_junk():
    assert journal.parse_header(journal.encode_header("abc")) == "abc"
    for junk in (b"", b"nonsense\n", b'{"journal":2,"generation":"x"}\n',
                 b'{"journal":1}\n', b"\xff\xfe\n"):
        assert journal.parse_header(junk) is None


def test_tx_round_trip_and_checksum():
    line = tx(7)
    decoded = journal.decode_tx(line[:-1])
    assert decoded["seq"] == 7 and decoded["actor"] == "agent-1"
    assert decoded["ops"] == [["n", "note", "a7", {"text": "hé"}]]
    flipped = bytearray(line[:-1])
    flipped[-3] ^= 0x01
    assert journal.decode_tx(bytes(flipped)) is None
    assert journal.decode_tx(line[:20]) is None
    assert journal.decode_tx(b"") is None


def test_encode_op_rejects_unjournalable_values():
    with pytest.raises(TypeError) as exc:
        journal.encode_op(["n", "note", "a", {"when": datetime.datetime.now()}])
    assert "note" in str(exc.value)
    with pytest.raises(ValueError):
        journal.encode_op(["n", "note", "a", {"x": float("nan")}])


def test_one_transaction_is_one_line_even_with_newlines_in_values():
    op = journal.encode_op(["n", "note", "a", {"text": "line1\nline2"}])
    assert b"\n" not in journal.encode_tx(1, "a", "t", [op])[:-1]


def test_read_tail_whole_file(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    tail = journal.read_tail(path, 0)
    assert tail.generation == "g1" and [t["seq"] for t in tail.txs] == [1, 2, 3]
    assert tail.valid_end == len(data) and not tail.torn and tail.corrupt_at is None
    assert tail.signature is not None


def test_read_tail_is_incremental(tmp_path):
    path = tmp_path / "j"
    make(path)
    first = journal.read_tail(path, 0)
    cut = len(journal.encode_header("g1")) + len(tx(1)) + len(tx(2))
    rest = journal.read_tail(path, cut)
    assert [t["seq"] for t in rest.txs] == [3] and rest.valid_end == first.valid_end


def test_torn_tail_is_ignored_and_reported(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    with open(path, "ab") as handle:
        handle.write(tx(4)[:25])
    tail = journal.read_tail(path, 0)
    assert [t["seq"] for t in tail.txs] == [1, 2, 3]
    assert tail.torn and tail.valid_end == len(data) and tail.corrupt_at is None


def test_nul_filled_tail_counts_as_torn(tmp_path):
    path = tmp_path / "j"
    data = make(path)
    with open(path, "ab") as handle:
        handle.write(b"\x00" * 64)
    tail = journal.read_tail(path, 0)
    assert tail.torn and tail.valid_end == len(data) and len(tail.txs) == 3


def test_damaged_complete_line_is_corruption_not_a_torn_tail(tmp_path):
    path = tmp_path / "j"
    data = bytearray(make(path))
    start = len(journal.encode_header("g1")) + len(tx(1))
    data[start + 14] ^= 0x01                     # inside the 2nd line's JSON
    path.write_bytes(bytes(data))
    tail = journal.read_tail(path, 0)
    assert tail.corrupt_at == start and [t["seq"] for t in tail.txs] == [1]
    assert not tail.torn


def test_missing_file_unfinished_header_and_shrunk(tmp_path):
    assert journal.read_tail(tmp_path / "nope", 0).generation is None
    path = tmp_path / "j"
    path.write_bytes(b'{"journal":1,"gene')
    assert journal.read_tail(path, 0).generation is None
    data = make(path)
    assert journal.read_tail(path, len(data) + 500).shrunk


def test_decoders_return_none_on_pathologically_nested_json():
    import zlib
    body = b"[" * 100000
    line = b"%08x " % (zlib.crc32(body) & 0xFFFFFFFF) + body
    assert journal.decode_tx(line) is None
    assert journal.parse_header(b"[" * 100000 + b"\n") is None


def test_read_tail_reports_unreadable_file_as_error_not_exception(tmp_path):
    tail = journal.read_tail(tmp_path, 0)           # a directory cannot be read as a journal
    assert tail.generation is None and tail.error


def test_read_tail_error_is_none_for_missing_and_normal_files(tmp_path):
    assert journal.read_tail(tmp_path / "nope", 0).error is None
    path = tmp_path / "j"
    make(path)
    assert journal.read_tail(path, 0).error is None


def test_read_tail_survives_nested_json_in_header_and_line(tmp_path):
    import zlib
    path = tmp_path / "j"
    path.write_bytes(b"[" * 100000 + b"\n")
    assert journal.read_tail(path, 0).generation is None
    body = b"[" * 100000
    path.write_bytes(journal.encode_header("g1") + tx(1)
                     + b"%08x " % (zlib.crc32(body) & 0xFFFFFFFF) + body + b"\n")
    tail = journal.read_tail(path, 0)
    assert [t["seq"] for t in tail.txs] == [1] and tail.corrupt_at is not None
