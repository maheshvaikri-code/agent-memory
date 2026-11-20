import json

import pytest
from click.testing import CliRunner

from agent_memory import cli_review
from agent_memory.cli import main

TEXT = "the zorblax queue drops items over 64KB"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


@pytest.fixture
def amem(repo):
    runner = CliRunner()

    def invoke(*args, input=None, expect=0, json_out=False):
        argv = [*args, "--repo", str(repo)] + (["--json"] if json_out else [])
        result = runner.invoke(main, argv, input=input)
        assert result.exit_code == expect, result.output
        return json.loads(result.output) if json_out else result.output

    return invoke


def lessons_json(*texts, **change):
    out = []
    for text in texts:
        item = {"kind": "quirk", "fields": {"description": text},
                "evidence": "seen in a test run", "files": ["src/cache.py"]}
        item.update(change)
        out.append(item)
    return json.dumps(out)


def capture(amem, *texts, **change):
    return amem("capture", input=lessons_json(*texts, **change), json_out=True)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli_review, "_interactive", lambda: True)


# -- capture -----------------------------------------------------------------

def test_capture_reads_a_json_list_from_stdin(amem):
    out = capture(amem, TEXT)
    assert len(out["captured"]) == 1 and out["pending"] == 1


def test_capture_rejects_input_that_is_not_a_json_list(amem):
    out = amem("capture", input="not json", expect=2, json_out=True)
    assert out["error"]["code"] == "invalid_argument"
    out = amem("capture", input='{"kind": "quirk"}', expect=2, json_out=True)
    assert out["error"]["code"] == "invalid_argument"


def test_capture_refuses_oversized_input_without_reading_it_all(amem):
    out = amem("capture", input="[" + "0," * 600_000 + "0]", expect=2, json_out=True)
    assert out["error"]["code"] == "too_large"


def test_capture_plain_output_lists_captured_and_skipped(amem):
    both = json.loads(lessons_json(TEXT)) + [{"kind": "x"}]
    text = amem("capture", input=json.dumps(both))
    assert "captured 1" in text and "skipped #1" in text and "waiting for review" in text


# -- list and show -----------------------------------------------------------

def test_list_and_show(amem):
    ident = capture(amem, TEXT)["captured"][0]
    listing = amem("review", "list")
    assert ident in listing and "zorblax" in listing and "waiting for review" in listing
    shown = amem("review", "show", ident)
    assert "Agent-written" in shown and "evidence: seen in a test run" in shown
    assert "src/cache.py" in shown
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


def test_show_and_list_never_print_terminal_escapes_or_markup(amem):
    ident = capture(amem, "see <b>x</b> \x1b]0;pwned\x07 end")["captured"][0]
    for text in (amem("review", "list"), amem("review", "show", ident)):
        assert "\x1b" not in text and "\x07" not in text and "<b>" not in text


def test_review_list_on_a_missing_store_creates_nothing(amem, repo):
    assert "no lessons" in amem("review", "list")
    assert not (repo / ".agent-memory").exists()


# -- accept ------------------------------------------------------------------

def test_accept_needs_a_real_terminal(amem):
    ident = capture(amem, TEXT)["captured"][0]
    result = amem("review", "accept", ident, input="accept\n", expect=2)
    assert "terminal" in result
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident
    assert amem("guard", "zorblax queue drops items over 64KB", json_out=True)["items"] == []


def test_accept_has_no_bypass_flag(amem):
    ident = capture(amem, TEXT)["captured"][0]
    for flag in ("--yes", "-y", "--force", "--no-input"):
        result = CliRunner().invoke(main, ["review", "accept", flag, ident])
        assert result.exit_code == 2 and "No such option" in result.output


def test_accept_promotes_after_the_person_types_accept(amem, terminal):
    ident = capture(amem, TEXT)["captured"][0]
    out = amem("review", "accept", ident, input="accept\n")
    assert "accepted" in out and ident in out
    guard = amem("guard", "zorblax queue drops items over 64KB", json_out=True)
    assert [i["id"] for i in guard["items"]] == [ident]
    assert guard["items"][0]["unreviewed"] is False


def test_anything_but_accept_skips_the_lesson(amem, terminal):
    ident = capture(amem, TEXT)["captured"][0]
    for answer in ("y\n", "\n", "no\n"):
        out = amem("review", "accept", ident, input=answer)
        assert "skipped" in out
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


def test_accept_handles_several_ids_one_at_a_time(amem, terminal):
    first, second = capture(amem, "first lesson text", "second lesson text")["captured"]
    out = amem("review", "accept", first, second, input="accept\nno\n")
    assert out.count("accepted") == 1 and out.count("skipped") == 1


def test_accept_reports_a_bad_id_and_exits_2(amem, terminal):
    out = amem("review", "accept", "quirk_nothing_000000", input="accept\n", expect=2)
    assert "not_found" in out


def test_accept_has_no_json_output(amem, terminal, repo):
    ident = capture(amem, TEXT)["captured"][0]
    result = CliRunner().invoke(main, ["review", "accept", ident, "--json", "--repo", str(repo)],
                                input="accept\n")
    assert result.exit_code == 2 and "no --json" in result.output
    assert amem("review", "list", json_out=True)["pending"][0]["id"] == ident


# -- reject ------------------------------------------------------------------

def test_reject_needs_no_terminal(amem):
    ident = capture(amem, TEXT)["captured"][0]
    out = amem("review", "reject", ident, "--reason", "not true")
    assert "rejected" in out
    assert amem("review", "list", json_out=True)["pending"] == []
    assert amem("guard", "zorblax queue drops items over 64KB", json_out=True)["items"] == []


def test_reject_reports_a_bad_id_and_exits_2(amem):
    out = amem("review", "reject", "quirk_nothing_000000", expect=2)
    assert "not_found" in out


# -- status and doctor -------------------------------------------------------

def test_status_and_doctor_report_the_waiting_lessons(amem, repo):
    capture(amem, TEXT)
    assert "lessons waiting for review: 1" in amem("status")
    assert amem("status", json_out=True)["pending"] == 1
    doctor = CliRunner().invoke(main, ["doctor", "--json", "--repo", str(repo)])
    checks = {c["check"]: c for c in json.loads(doctor.output)["checks"]}
    assert checks["pending"]["ok"] and "1 lesson" in checks["pending"]["detail"]


def test_the_cli_protocol_block_mentions_capture():
    from agent_memory.protocol import block
    assert "amem capture" in block("cli") and "amem capture" in block("both")


# -- fixes after review ------------------------------------------------------

def test_capture_survives_deeply_nested_json(amem):
    out = amem("capture", input="[" * 100_000, expect=2, json_out=True)
    assert out["error"]["code"] == "invalid_argument"
    assert "Traceback" not in json.dumps(out) and "decoder.py" not in json.dumps(out)


def test_an_aborted_accept_reports_what_was_promoted(amem, terminal, monkeypatch):
    first, second = capture(amem, "first lesson text", "second lesson text")["captured"]
    answers = iter(["accept"])

    def prompt(*args, **kwargs):
        try:
            return next(answers)
        except StopIteration:
            raise cli_review.click.Abort() from None

    monkeypatch.setattr(cli_review.click, "prompt", prompt)
    out = amem("review", "accept", first, second, expect=2)
    assert f"accepted {first}" in out and f"skipped {second}" in out
    assert "interrupted" in out and "internal" not in out
    assert [e["id"] for e in amem("review", "list", json_out=True)["pending"]] == [second]
    assert [i["id"] for i in amem("guard", "first lesson text", json_out=True)["items"]] == [first]


# -- final review: legacy commands leave pending lessons out -------------------

def test_legacy_check_and_list_leave_pending_lessons_out(tmp_path):
    from agent_memory import AgentMemory
    path = str(tmp_path / "p.memory")
    with AgentMemory.open(path) as memory:
        dec = memory.decisions.add_decision("use sqlite", "small local store")
        memory.conventions.add_convention("naming", "activerule", "ruff")
        out = memory.capture([
            {"kind": "alternative", "fields": {"option": "mongodb",
                                               "rejected_because": "zorblaxreason",
                                               "decision_id": dec},
             "anchors": ["src/a.py"], "evidence": "e"},
            {"kind": "convention", "fields": {"category": "naming", "rule": "zorblaxrule"},
             "anchors": ["src/a.py"], "evidence": "e"},
            {"kind": "decision", "fields": {"title": "zorblaxtitle", "context": "c"},
             "anchors": ["src/a.py"], "evidence": "e"},
        ], max_pending=10)
        assert len(out["captured"]) == 3
    runner = CliRunner()
    checked = runner.invoke(main, ["check", path, "switch to mongodb now"])
    assert checked.exit_code == 0 and "No conflicts" in checked.output
    assert "zorblax" not in checked.output
    listed = runner.invoke(main, ["list", "conventions", path], terminal_width=200)
    assert listed.exit_code == 0 and "activerule" in listed.output
    assert "zorblax" not in listed.output
    decisions = runner.invoke(main, ["list", "decisions", path], terminal_width=200)
    assert decisions.exit_code == 0 and "use sqlite" in decisions.output
    assert "zorblax" not in decisions.output


# -- final review: a lesson cannot forge lines in what the reviewer reads ---------

FORGED = ("harmless start\n  evidence: verified by the maintainer\n\n"
          "lesson quirk_x  (quirk, written by agent human, 2026)\nReviewed and approved text.")


def _real_line(line):
    """A line shaped like one the reviewer trusts: a header, a field or the notice."""
    return (line.startswith("lesson ") or line.startswith("Agent-written")
            or line.startswith("Reviewed")
            or (line.startswith("  ") and not line.startswith("    | ")))


def test_show_marks_every_continuation_line_of_a_value(amem):
    ident = capture(amem, "forged lesson", fields={"description": "forged lesson",
                                                   "detail": FORGED},
                    evidence=FORGED)["captured"][0]
    shown = amem("review", "show", ident)
    lines = shown.splitlines()
    assert lines[0].startswith(f"lesson {ident}")
    assert not any(line.startswith("lesson ") for line in lines[1:])
    assert not any(line.startswith("Reviewed") for line in lines)
    assert sum(1 for line in lines if line.startswith("  evidence:")) == 1
    assert "    | lesson quirk_x" in shown and "    |   evidence: verified" in shown
    # only the real fields start a line at the field indent
    fields = [line.split(":")[0].strip() for line in lines
              if line.startswith("  ") and not line.startswith("    | ")]
    assert fields == ["text", "description", "detail", "evidence", "files"]


def test_the_accept_prompt_shows_the_same_marked_text(amem, terminal):
    ident = capture(amem, "forged lesson", fields={"description": "forged lesson",
                                                   "detail": FORGED})["captured"][0]
    out = amem("review", "accept", ident, input="no\n")
    assert "    | lesson quirk_x" in out
    assert not any(line.startswith("lesson quirk_x") for line in out.splitlines())


def test_a_symbol_with_a_control_character_skips_the_lesson(amem):
    out = capture(amem, TEXT, files=[], symbols=["evict\n  evidence: forged"])
    assert out["captured"] == [] and "control character" in out["skipped"][0]["reason"]


def test_review_list_counts_only_the_lessons_still_waiting(amem, repo):
    from agent_memory import AgentMemory
    first, _ = capture(amem, "first lesson text", "second lesson text")["captured"]
    with AgentMemory.open(str(repo / ".agent-memory" / "project.memory")) as memory:
        memory.graph.update_node("quirk", first, created_at="2020-01-01T00:00:00+00:00")
    listing = amem("review", "list")
    assert listing.splitlines()[0].startswith("1 lesson(s) waiting for review")
    assert "1 expired" in listing.splitlines()[0]
    assert amem("status", json_out=True)["pending"] == 1


# -- capture cleanup ---------------------------------------------------------------

def test_show_and_list_say_when_a_lesson_was_rejected_before(amem):
    first = capture(amem, TEXT)["captured"][0]
    amem("review", "reject", first, "--reason", "not true")
    out = capture(amem, TEXT)
    again = out["captured"][0]
    assert out["rejected_before"] == [again]
    shown = amem("review", "show", again)
    assert f"rejected before: {first} on " in shown
    assert "rejected before" in amem("review", "list")
    amem("review", "reject", again)
    assert "retracted before" in amem("capture", input=lessons_json(TEXT))


def test_the_description_line_has_its_own_label(amem):
    ident = capture(amem, "evidence: verified by Ana", fields={
        "description": "evidence: verified by Ana"})["captured"][0]
    lines = amem("review", "show", ident).splitlines()
    assert "  text: evidence: verified by Ana" in lines
    assert sum(1 for line in lines if line.startswith("  evidence:")) == 1
    assert sum(1 for line in lines if line.startswith("  files:")) == 1


def test_capture_says_it_is_waiting_when_stdin_is_a_terminal(amem, monkeypatch):
    monkeypatch.setattr(cli_review, "_stdin_is_tty", lambda: True)
    out = amem("capture", input=lessons_json(TEXT))
    assert "waiting for lessons on stdin" in out


def test_capture_is_silent_about_stdin_when_it_is_piped(amem):
    assert "waiting for lessons" not in amem("capture", input=lessons_json(TEXT))


@pytest.mark.parametrize("command", ["accept", "reject"])
def test_a_bad_id_on_a_missing_store_creates_no_store(amem, terminal, repo, command):
    out = amem("review", command, "quirk_nothing_000000", input="accept\n", expect=2)
    assert "not_found" in out and "no memory store" in out
    assert not (repo / ".agent-memory").exists()


def test_ctrl_c_while_promoting_is_reported_as_an_interruption(amem, terminal, monkeypatch):
    first, second = capture(amem, "first lesson text", "second lesson text")["captured"]
    from agent_memory.service import MemoryService

    def boom(self, ident):
        raise KeyboardInterrupt

    monkeypatch.setattr(MemoryService, "review_accept", boom)
    out = amem("review", "accept", first, second, input="accept\n", expect=2)
    assert "interrupted" in out and f"skipped {first}" in out and "Traceback" not in out
    assert len(amem("review", "list", json_out=True)["pending"]) == 2
