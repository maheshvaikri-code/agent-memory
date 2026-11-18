import json

import pytest

from agent_memory.installer import InstallError
from agent_memory.installer.changes import Change, apply_changes, read_text
from agent_memory.installer.gitignore import ensure_ignored
from agent_memory.installer.jsonmerge import has_server, merge_mcp_servers
from agent_memory.installer.markers import BEGIN, END, apply_block, has_block

BODY = "## Project memory\n\nUse the tools."


def test_a_block_is_appended_to_existing_content_untouched():
    out = apply_block("# My rules\n\nBe kind.\n", BODY)
    assert out.startswith("# My rules\n\nBe kind.\n")
    assert BEGIN in out and END in out and BODY in out and has_block(out)


def test_a_block_is_created_in_an_empty_file():
    assert apply_block("", BODY) == f"{BEGIN}\n{BODY}\n{END}\n"


def test_applying_twice_is_identical():
    once = apply_block("# rules\n", BODY)
    assert apply_block(once, BODY) == once


def test_only_the_marked_region_is_replaced():
    first = apply_block("before\n\nafter-less\n", BODY)
    edited = first.replace("Use the tools.", "Changed by hand.") + "\ntrailing user text\n"
    again = apply_block(edited, "## Project memory\n\nNEW BODY")
    assert "NEW BODY" in again and "Changed by hand." not in again
    assert again.startswith("before") and again.endswith("trailing user text\n")


def test_a_lone_marker_is_refused_not_guessed():
    with pytest.raises(InstallError):
        apply_block(f"text\n{BEGIN}\nno end marker\n", BODY)
    with pytest.raises(InstallError):
        apply_block(f"text\n{END}\n", BODY)


def test_file_without_trailing_newline_gets_a_separator():
    out = apply_block("no newline at end", BODY)
    assert out.startswith("no newline at end\n\n")


ENTRY = {"command": "amem", "args": ["serve"]}


def test_json_merge_creates_and_preserves_other_servers():
    created = json.loads(merge_mcp_servers(None, "agent-memory", ENTRY))
    assert created == {"mcpServers": {"agent-memory": ENTRY}}
    existing = json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1})
    merged = json.loads(merge_mcp_servers(existing, "agent-memory", ENTRY))
    assert merged["mcpServers"]["other"] == {"command": "x"} and merged["keep"] == 1
    assert merged["mcpServers"]["agent-memory"] == ENTRY
    assert has_server(json.dumps(merged), "agent-memory") and not has_server(existing, "agent-memory")


def test_json_merge_is_idempotent():
    once = merge_mcp_servers(None, "agent-memory", ENTRY)
    assert merge_mcp_servers(once, "agent-memory", ENTRY) == once


def test_invalid_or_oddly_shaped_json_is_refused():
    for bad in ("{not json", "[1, 2]", '{"mcpServers": []}'):
        with pytest.raises(InstallError):
            merge_mcp_servers(bad, "agent-memory", ENTRY)


def test_gitignore_entry():
    assert ensure_ignored("", ".agent-memory/") == ".agent-memory/\n"
    assert ensure_ignored("node_modules/\n", ".agent-memory/") == "node_modules/\n.agent-memory/\n"
    assert ensure_ignored("a\n.agent-memory/\n", ".agent-memory/") == "a\n.agent-memory/\n"
    assert ensure_ignored("a\n.agent-memory\n", ".agent-memory/") == "a\n.agent-memory\n"
    assert ensure_ignored("no-newline", ".agent-memory/") == "no-newline\n.agent-memory/\n"


def test_change_status_and_diff(tmp_path):
    path = tmp_path / "f.md"
    assert Change(path, None, "new\n", "x").status == "create"
    assert Change(path, "a\n", "a\n", "x").status == "unchanged"
    change = Change(path, "a\n", "b\n", "x")
    assert change.status == "update" and "-a" in change.diff() and "+b" in change.diff()


def test_crlf_files_keep_crlf_and_utf8_survives(tmp_path):
    path = tmp_path / "rules.md"
    path.write_bytes("# Règles\r\n\r\nBe kind.\r\n".encode("utf-8"))
    text, newline = read_text(path)
    assert newline == "\r\n" and "\r" not in text and "Règles" in text
    apply_changes([Change(path, text, apply_block(text, BODY), "x", newline=newline)])
    raw = path.read_bytes().decode("utf-8")
    assert "\r\n" in raw and raw.count("\n") == raw.count("\r\n")       # no bare LF
    assert "Règles" in raw and BEGIN in raw


def test_apply_creates_parent_directories_and_skips_unchanged(tmp_path):
    path = tmp_path / "deep" / "dir" / "f.md"
    apply_changes([Change(path, None, "hello\n", "x")])
    assert path.read_text(encoding="utf-8") == "hello\n"
    stamp = path.stat().st_mtime_ns
    apply_changes([Change(path, "hello\n", "hello\n", "x")])
    assert path.stat().st_mtime_ns == stamp


# ---------------------------------------------------------------------------
# Added for the review focus: idempotence on disk, preservation, newlines,
# refusal of unsafe inputs, atomicity, encoding edge cases.
# ---------------------------------------------------------------------------
import os

from agent_memory.installer import markers


def _install_file(path, body=BODY):
    """What `amem install --write` does for one instruction file."""
    text, newline = read_text(path)
    new = apply_block(text or "", body)
    return apply_changes([Change(path, text, new, "x", newline=newline)])


def test_write_twice_leaves_the_file_byte_identical(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"# mine\r\n\r\nkeep this\r\n")
    assert len(_install_file(path)) == 1
    first = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    assert _install_file(path) == []
    assert path.read_bytes() == first and path.stat().st_mtime_ns == stamp


def test_text_outside_the_markers_survives_byte_for_byte(tmp_path):
    path = tmp_path / "AGENTS.md"
    head = "# Intro  \n\n\ttabbed line   \n\nunicode: Règles ✓\n"
    tail = "\n\n## After\n  trailing spaces  \n\n\n"
    path.write_bytes((head + "\n" + markers.BEGIN + "\nold body\n" + markers.END + tail).encode("utf-8"))
    _install_file(path, "## New\n\nbody")
    raw = path.read_bytes().decode("utf-8")
    assert raw.startswith(head + "\n" + markers.BEGIN) and raw.endswith(markers.END + tail)
    assert "old body" not in raw and "## New" in raw


def test_lf_files_stay_lf(tmp_path):
    path = tmp_path / "rules.md"
    path.write_bytes(b"# rules\nline\n")
    _install_file(path)
    assert b"\r" not in path.read_bytes()


def test_new_files_are_written_with_lf(tmp_path):
    path = tmp_path / "new.md"
    _install_file(path)
    assert path.read_bytes() == f"{BEGIN}\n{BODY}\n{END}\n".encode("utf-8")


def test_mixed_line_endings_are_refused_not_normalised(tmp_path):
    path = tmp_path / "mixed.md"
    original = b"crlf line\r\nlf line\nanother\r\n"
    path.write_bytes(original)
    with pytest.raises(InstallError, match="line endings"):
        read_text(path)
    assert path.read_bytes() == original
    path.write_bytes(b"old mac\rline\n")
    with pytest.raises(InstallError, match="line endings"):
        read_text(path)


def test_utf8_bom_is_preserved(tmp_path):
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbf# rules\r\n")
    _install_file(path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf# rules\r\n") and raw.count(b"\xef\xbb\xbf") == 1
    assert _install_file(path) == []


def test_non_utf8_file_is_refused_untouched(tmp_path):
    path = tmp_path / "latin.md"
    path.write_bytes("caf\xe9\n".encode("latin-1"))
    with pytest.raises(InstallError, match="UTF-8"):
        read_text(path)
    assert path.read_bytes() == b"caf\xe9\n"


def test_a_directory_in_place_of_a_file_is_an_install_error(tmp_path):
    with pytest.raises(InstallError):
        read_text(tmp_path)


def test_empty_and_whitespace_only_files(tmp_path):
    path = tmp_path / "empty.md"
    path.write_bytes(b"")
    _install_file(path)
    assert path.read_bytes() == f"{BEGIN}\n{BODY}\n{END}\n".encode("utf-8")
    assert apply_block("\n\n", BODY) == f"{BEGIN}\n{BODY}\n{END}\n"


def test_no_trailing_newline_file_keeps_its_content_and_gets_one_blank_line(tmp_path):
    path = tmp_path / "x.md"
    path.write_bytes(b"no newline at end")
    _install_file(path)
    assert path.read_bytes().decode("utf-8").startswith("no newline at end\n\n" + BEGIN)


def test_marker_pairs_in_the_wrong_order_are_refused():
    for bad in (f"{END}\nmiddle\n{BEGIN}\n", f"a\n{END}\nuser text\n{BEGIN}\nb\n"):
        with pytest.raises(InstallError):
            apply_block(bad, BODY)


def test_duplicate_or_nested_markers_are_refused_so_user_text_between_them_is_kept():
    two_begins = f"{BEGIN}\nuser text between\n{BEGIN}\nbody\n{END}\n"
    two_blocks = f"{BEGIN}\nA\n{END}\nuser text\n{BEGIN}\nB\n{END}\n"
    for bad in (two_begins, two_blocks, f"{BEGIN}\n{END}\n{END}\n"):
        with pytest.raises(InstallError):
            apply_block(bad, BODY)


def test_a_body_containing_a_marker_is_refused():
    for body in (f"x {END} y", f"{BEGIN}\nz"):
        with pytest.raises(InstallError):
            apply_block("", body)


def test_refused_marker_file_is_left_untouched_on_disk(tmp_path):
    path = tmp_path / "CLAUDE.md"
    original = f"# mine\n{BEGIN}\nno end\n".encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(InstallError):
        _install_file(path)
    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["CLAUDE.md"]


def test_invalid_mcp_json_is_refused_with_a_clear_error_and_never_overwritten(tmp_path):
    path = tmp_path / ".mcp.json"
    original = b'{\n  // a comment\n  "mcpServers": {"x": {"command": "y"},}\n}\n'
    path.write_bytes(original)
    text, newline = read_text(path)
    with pytest.raises(InstallError, match="not valid JSON"):
        merge_mcp_servers(text, "agent-memory", ENTRY)
    assert path.read_bytes() == original


def test_json_merge_keeps_unicode_indent_and_key_order():
    existing = '{\n    "z": "é✓",\n    "mcpServers": {\n        "o": {"command": "x"}\n    },\n    "a": 1\n}\n'
    out = merge_mcp_servers(existing, "agent-memory", ENTRY)
    assert "é✓" in out and "\\u" not in out
    assert out.splitlines()[1].startswith('    "z"')          # 4-space indent kept
    assert list(json.loads(out)) == ["z", "mcpServers", "a"]


def test_json_merge_with_tab_indent_and_an_identical_entry_is_a_noop():
    existing = '{\n\t"mcpServers": {\n\t\t"agent-memory": {"command": "amem", "args": ["serve"]}\n\t}\n}\n'
    assert merge_mcp_servers(existing, "agent-memory", ENTRY) == existing


def test_json_merge_updates_a_stale_entry_in_place():
    existing = json.dumps({"mcpServers": {"agent-memory": {"command": "old"}, "o": {}}})
    merged = json.loads(merge_mcp_servers(existing, "agent-memory", ENTRY))
    assert merged["mcpServers"]["agent-memory"] == ENTRY and merged["mcpServers"]["o"] == {}


def test_json_merge_handles_bom_and_empty_files():
    out = merge_mcp_servers('﻿{"keep": 1}', "agent-memory", ENTRY)
    assert out.startswith("﻿{") and json.loads(out[1:])["keep"] == 1
    assert json.loads(merge_mcp_servers("", "agent-memory", ENTRY)) == {"mcpServers": {"agent-memory": ENTRY}}
    assert json.loads(merge_mcp_servers("  \n", "agent-memory", ENTRY)) == {"mcpServers": {"agent-memory": ENTRY}}


def test_has_server_tolerates_odd_shapes():
    assert not has_server('{"mcpServers": []}', "agent-memory")
    assert not has_server("{nope", "agent-memory")
    assert not has_server(None, "agent-memory")


def test_gitignore_accepts_anchored_forms_and_ignores_comments():
    assert ensure_ignored("/.agent-memory/\n") == "/.agent-memory/\n"
    assert ensure_ignored("/.agent-memory\n") == "/.agent-memory\n"
    assert ensure_ignored("# .agent-memory/\n") == "# .agent-memory/\n.agent-memory/\n"


def test_apply_is_atomic_a_failed_replace_leaves_the_original_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"original\n")
    import agent_memory.storage.atomic as atomic

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        apply_changes([Change(path, "original\n", "changed\n", "x")])
    monkeypatch.undo()
    assert path.read_bytes() == b"original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["CLAUDE.md"]


def test_a_file_changed_since_it_was_read_is_refused_and_nothing_is_written(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    changes = [Change(a, "a\n", "a2\n", "x"), Change(b, "b\n", "b2\n", "x")]
    b.write_bytes(b"edited meanwhile\n")
    with pytest.raises(InstallError, match="changed"):
        apply_changes(changes)
    assert a.read_bytes() == b"a\n" and b.read_bytes() == b"edited meanwhile\n"
    with pytest.raises(InstallError, match="changed"):
        apply_changes([Change(a, None, "new\n", "x")])        # appeared since planning
    assert a.read_bytes() == b"a\n"


def test_writing_through_a_symlink_keeps_the_link(tmp_path):
    target = tmp_path / "real.md"
    target.write_bytes(b"# real\n")
    link = tmp_path / "CLAUDE.md"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    _install_file(link)
    assert link.is_symlink() and BEGIN.encode() in target.read_bytes()
    assert target.read_bytes().startswith(b"# real\n")
