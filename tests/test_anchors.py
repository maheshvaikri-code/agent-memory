import os

from ison_graph import ISONGraph

from agent_memory.anchors import (
    HashCache, add_anchor, anchor_paths, content_hash, file_node_id, head_commit,
    norm_path, restamp, staleness,
)

REF = ("bug_pattern", "bp1")


def graph():
    g = ISONGraph(name="t")
    g.add_node("bug_pattern", "bp1", name="x", signature="y")
    return g


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def test_norm_path_handles_backslashes_and_absolute_paths(tmp_path):
    root = str(tmp_path)
    assert norm_path("services\\payments\\webhook.py", root) == "services/payments/webhook.py"
    assert norm_path("./a/b.py", root) == "a/b.py"
    absolute = os.path.join(root, "services", "payments", "webhook.py")
    assert norm_path(absolute, root) == "services/payments/webhook.py"
    assert norm_path("a\\b.py", None) == "a/b.py"


def test_file_node_id_is_a_valid_identifier():
    node_id = file_node_id("docs/my notes: v1|draft.md")
    assert not any(ch in node_id for ch in " :|")


def test_hash_ignores_line_endings_and_trailing_whitespace(tmp_path):
    a = write(tmp_path, "a.py", "x = 1\ny = 2\n")
    b = write(tmp_path, "b.py", "x = 1  \r\ny = 2\r\n")
    assert content_hash(a) == content_hash(b)
    assert content_hash(tmp_path / "missing.py") is None


def test_fresh_then_drifted_then_orphaned(tmp_path):
    g = graph()
    f = write(tmp_path, "src/a.py", "def handle():\n    pass\n")
    add_anchor(g, REF, "src/a.py", repo_root=str(tmp_path))
    assert staleness(g, REF, HashCache(str(tmp_path))) == "fresh"
    write(tmp_path, "src/a.py", "def handle():\n    return 1\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "drifted"
    f.unlink()
    assert staleness(g, REF, HashCache(str(tmp_path))) == "orphaned"


def test_broken_when_the_anchored_symbol_disappears(tmp_path):
    g = graph()
    write(tmp_path, "src/a.py", "def handle():\n    pass\n")
    add_anchor(g, REF, "src/a.py", symbol="handle", repo_root=str(tmp_path))
    write(tmp_path, "src/a.py", "def process():\n    pass\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "broken"


def test_worst_state_wins_and_restamp_clears_drift(tmp_path):
    g = graph()
    write(tmp_path, "a.py", "1\n")
    write(tmp_path, "b.py", "1\n")
    add_anchor(g, REF, "a.py", repo_root=str(tmp_path))
    add_anchor(g, REF, "b.py", repo_root=str(tmp_path))
    write(tmp_path, "b.py", "2\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "drifted"
    assert restamp(g, REF, str(tmp_path)) == 2
    assert staleness(g, REF, HashCache(str(tmp_path))) == "fresh"
    assert sorted(anchor_paths(g, REF)) == ["a.py", "b.py"]


def test_unknown_without_root_or_anchors(tmp_path):
    g = graph()
    assert staleness(g, REF, HashCache(str(tmp_path))) == "unknown"
    add_anchor(g, REF, "a.py", repo_root=None)
    assert staleness(g, REF, HashCache(None)) == "unknown"


def test_awkward_paths_are_accepted(tmp_path):
    g = graph()
    write(tmp_path, "docs/my notes.md", "hello\n")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    add_anchor(g, REF, "docs/my notes.md", repo_root=str(tmp_path))
    add_anchor(g, REF, "blob.bin", repo_root=str(tmp_path))
    add_anchor(g, REF, "../outside/elsewhere.py", repo_root=str(tmp_path))
    assert "docs/my notes.md" in anchor_paths(g, REF)
    assert staleness(g, REF, HashCache(str(tmp_path))) in ("fresh", "orphaned")


def test_anchor_survives_save_and_load(tmp_path):
    g = graph()
    write(tmp_path, "src/a b.py", "1\n")
    add_anchor(g, REF, "src/a b.py", repo_root=str(tmp_path))
    g.save(str(tmp_path / "m.memory"))
    h = ISONGraph.load(str(tmp_path / "m.memory"))
    assert anchor_paths(h, REF) == ["src/a b.py"]
    assert staleness(h, REF, HashCache(str(tmp_path))) == "fresh"


def test_head_commit_reads_git_without_a_subprocess(tmp_path):
    assert head_commit(str(tmp_path)) == ""
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
    assert head_commit(str(tmp_path)) == "abc123"


def test_restamp_keeps_hash_of_a_deleted_file_so_it_stays_orphaned(tmp_path):
    g = graph()
    write(tmp_path, "a.py", "x = 1\n")
    add_anchor(g, REF, "a.py", repo_root=str(tmp_path))
    (tmp_path / "a.py").unlink()
    assert restamp(g, REF, str(tmp_path)) == 0
    assert staleness(g, REF, HashCache(str(tmp_path))) == "orphaned"


def test_restamp_without_repo_root_changes_nothing():
    g = graph()
    add_anchor(g, REF, "a.py")
    assert restamp(g, REF, None) == 0


def test_nul_byte_path_does_not_raise(tmp_path):
    g = graph()
    add_anchor(g, REF, "bad\x00name.py", repo_root=str(tmp_path))
    staleness(g, REF, HashCache(str(tmp_path)))
    assert content_hash(str(tmp_path / "bad\x00name.py")) is None


# -- anchors outside the repo, unreadable and oversized files --------------------------

def _edge(g):
    (edge,) = list(g.edges("ANCHORED_TO", source=REF))
    return edge, g.get_node(*edge.target).properties["path"]


def test_absolute_path_outside_the_repo_is_not_hashed_or_persisted(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "elsewhere"
    repo.mkdir()
    secret = write(outside, "secret.py", "token = 1\n")
    g = graph()
    add_anchor(g, REF, str(secret), "token", repo_root=str(repo))
    edge, path = _edge(g)
    assert path == "outside/secret.py" and edge.properties["hash"] == ""
    assert str(tmp_path).replace("\\", "/").lower() not in g.to_ison().replace("\\", "/").lower()
    assert staleness(g, REF, HashCache(str(repo))) == "unknown"


def test_absolute_path_without_a_repo_root_is_not_persisted(tmp_path):
    g = graph()
    add_anchor(g, REF, str(write(tmp_path, "a.py", "x\n")))
    _, path = _edge(g)
    assert path == "outside/a.py"


def test_relative_dotdot_path_is_kept_relative_and_not_hashed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write(tmp_path, "outside/x.py", "x = 1\n")
    for rel in ("../outside/x.py", "src/../../outside/x.py"):
        g = graph()
        add_anchor(g, REF, rel, repo_root=str(repo))
        edge, path = _edge(g)
        assert path == rel and edge.properties["hash"] == ""
        assert staleness(g, REF, HashCache(str(repo))) == "unknown"


def test_dotdot_that_stays_inside_the_repo_is_hashed(tmp_path):
    write(tmp_path, "src/a.py", "x = 1\n")
    g = graph()
    add_anchor(g, REF, "lib/../src/a.py", repo_root=str(tmp_path))
    edge, _ = _edge(g)
    assert edge.properties["hash"]
    assert staleness(g, REF, HashCache(str(tmp_path))) == "fresh"


def test_nul_byte_path_is_recorded_without_a_hash(tmp_path):
    g = graph()
    add_anchor(g, REF, "src/bad\x00name.py", repo_root=str(tmp_path))
    edge, _ = _edge(g)
    assert edge.properties["hash"] == ""
    assert staleness(g, REF, HashCache(str(tmp_path))) == "unknown"


def test_files_over_the_size_cap_are_not_read(tmp_path):
    from agent_memory.anchors import MAX_HASH_BYTES
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (MAX_HASH_BYTES + 1))
    assert content_hash(big) is None
    assert HashCache(str(tmp_path)).get("big.bin") == ("", "")     # "" = not read
    g = graph()
    add_anchor(g, REF, "big.bin", repo_root=str(tmp_path))
    edge, _ = _edge(g)
    assert edge.properties["hash"] == ""
    assert staleness(g, REF, HashCache(str(tmp_path))) == "unknown"


def test_a_file_that_grows_past_the_cap_becomes_unknown_not_orphaned(tmp_path):
    from agent_memory.anchors import MAX_HASH_BYTES
    f = write(tmp_path, "log.txt", "small\n")
    g = graph()
    add_anchor(g, REF, "log.txt", repo_root=str(tmp_path))
    f.write_bytes(b"x" * (MAX_HASH_BYTES + 1))
    assert staleness(g, REF, HashCache(str(tmp_path))) == "unknown"


def test_memory_anchor_outside_repo_reports_unknown_through_recall(tmp_path):
    from agent_memory import AgentMemory
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = write(tmp_path, "outside/x.py", "secret = 1\n")
    m = AgentMemory("t", repo_root=str(repo))
    r = m.remember("quirk", {"description": "outside file quirk"})
    m.anchor(r.id, str(secret), "secret")
    m.anchor(r.id, "../outside/x.py")
    assert all(e.properties["hash"] == "" for e in m.graph.edges("ANCHORED_TO"))
    assert [i.staleness for i in m.recall("outside file quirk").items] == ["unknown"]


def test_location_field_outside_the_repo_is_not_auto_anchored(tmp_path):
    from agent_memory import AgentMemory
    repo = tmp_path / "repo"
    repo.mkdir()
    write(tmp_path, "out.py", "x\n")
    m = AgentMemory("t", repo_root=str(repo))
    r = m.remember("tech_debt", {"location": "../out.py", "description": "no lock"})
    assert not list(m.graph.edges("ANCHORED_TO", source=r.ref))
