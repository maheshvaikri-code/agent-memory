import json
import os

import pytest

from agent_memory.service import CODES, MemoryService, ServiceConfig, path_scrubber, resolve_config


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "secretrepo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "cache.py").write_text("x = 1\n")
    return root


def test_forbidden_is_a_service_error_code():
    assert "forbidden" in CODES


def test_status_shows_full_paths_by_default(repo):
    svc = MemoryService(resolve_config(repo=str(repo), agent="a", env={}))
    try:
        status = svc.status()
    finally:
        svc.close()
    assert status["repo_root"] == str(repo) and status["store"].endswith("project.memory")
    assert os.path.isabs(status["store"])


def test_status_with_redact_paths_names_files_and_directories_only(repo, tmp_path):
    config = resolve_config(repo=str(repo), agent="a", env={})
    svc = MemoryService(ServiceConfig(config.store_path, config.repo_root, "a", False, False,
                                      redact_paths=True))
    try:
        svc.remember("quirk", {"description": "a thing"}, files=["src/cache.py"])
        status = svc.status()
    finally:
        svc.close()
    assert status["store"] == "project.memory" and status["repo_root"] == "secretrepo"
    assert all(str(tmp_path) not in str(warning) for warning in status["warnings"])
    assert status["total_active"] == 1


def test_path_scrubber_replaces_every_form_of_the_server_paths(repo):
    config = resolve_config(repo=str(repo), agent="a", env={})
    scrub = path_scrubber(config)
    store, root = config.store_path, config.repo_root
    text = (f"cannot read {store}; the directory {os.path.dirname(store)} and "
            f"{root.replace(os.sep, '/')}/src/x.py are missing")
    out = scrub(text)
    assert root not in out and store not in out and root.replace(os.sep, "/") not in out
    assert "project.memory" in out and "secretrepo" in out


def test_path_scrubber_leaves_other_text_alone(repo):
    scrub = path_scrubber(resolve_config(repo=str(repo), agent="a", env={}))
    assert scrub("a note about src/cache.py") == "a note about src/cache.py"


def test_status_warnings_are_scrubbed_of_the_store_path_and_the_repo_root(repo, tmp_path):
    config = resolve_config(repo=str(repo), agent="a", env={})
    svc = MemoryService(ServiceConfig(config.store_path, config.repo_root, "a", False, False,
                                      redact_paths=True))
    try:
        store, root = config.store_path, config.repo_root
        svc.memory.load_warnings.append(
            f"snapshot {store} cannot be read; {root.replace(os.sep, '/')}/src/x.py is odd; "
            f"journal {store}.journal under {root}")
        status = svc.status()
    finally:
        svc.close()
    blob = " ".join(status["warnings"])
    assert "cannot be read" in blob and "project.memory" in blob and "secretrepo" in blob
    for form in (store, root, root.replace(os.sep, "/"), str(tmp_path), tmp_path.as_posix()):
        assert form not in blob
