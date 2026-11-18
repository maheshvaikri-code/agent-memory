"""The legacy read commands print stored strings through rich: they must neither interpret them
as markup (a title like `red[/]` crashed `amem list decisions` with MarkupError) nor pass
terminal control sequences through (a stored ESC[2J cleared the screen)."""
import pytest
from click.testing import CliRunner

from agent_memory.cli import main

ESC = "rule \x1b[2J\x1b]0;pwned\x07 end"
MARKUP = "red[/] [bold red]x[/bold red]"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    path = tmp_path / "legacy.memory"
    runner = CliRunner()

    def remember(*args):
        result = runner.invoke(main, ["remember", *args, "--repo", str(root),
                                      "--store", str(path)])
        assert result.exit_code == 0, result.output

    remember("convention", "-F", "category=naming", "-F", f"rule={ESC}")
    remember("convention", "-F", f"category={MARKUP}", "-F", "rule=keys use colons")
    remember("decision", "-F", f"title={MARKUP}", "-F", f"context=webhook {ESC}")
    remember("bug_pattern", "-F", f"name={MARKUP}", "-F", "signature=webhook retry storm")
    return str(path)


def invoke(*args):
    return CliRunner().invoke(main, list(args))


def clean(output):
    return "\x1b" not in output and "\x07" not in output


def test_list_conventions(store):
    out = invoke("list", "conventions", store)
    assert out.exit_code == 0, out.output
    assert clean(out.output) and "[2J" in out.output
    assert "red[/]" in out.output and "[bold red]x[/bold red]" in out.output


def test_list_decisions_with_a_markup_looking_title(store):
    out = invoke("list", "decisions", store)
    assert out.exit_code == 0, out.output
    assert clean(out.output) and "red[/]" in out.output


def test_context_prints_stored_text_literally(store):
    out = invoke("context", store, "webhook retry storm naming keys", "-f", "src/a.py")
    assert out.exit_code == 0, out.output           # list(files) used to call the click group
    assert clean(out.output) and "red[/]" in out.output and "[2J" in out.output


def test_context_json(store):
    out = invoke("context", store, "webhook retry storm", "--json", "-f", "src/a.py")
    assert out.exit_code == 0, out.output
    assert clean(out.output) and "red[/]" in out.output


def test_check_prints_stored_text_literally(store):
    out = invoke("check", store, "keys use colons webhook retry storm")
    assert out.exit_code == 0, out.output
    assert clean(out.output)


def test_a_markup_looking_task_and_error_are_printed_literally(store, tmp_path):
    out = invoke("context", store, "task [bold]x[/bold] red[/]")
    assert out.exit_code == 0, out.output
    assert "task [bold]x[/bold] red[/]" in out.output
    missing = invoke("stats", str(tmp_path / "no[bold]such.memory"))
    assert missing.exit_code == 1 and "no[bold]such.memory" in missing.output
