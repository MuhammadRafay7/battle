import json
from pathlib import Path

import pytest

from battle.workspace import (
    RIVALS_DIR,
    ApplyConflict,
    add_rivals,
    apply_entry,
    capture,
    copy_project,
    materialize,
    similarity,
    undo_apply,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    write(root / "app.py", "print('hello')\n")
    write(root / "src" / "util.py", "VALUE = 1\n")
    write(root / "old.py", "gone = True\n")
    return root


@pytest.fixture
def baseline(tmp_path, project):
    target = tmp_path / "baseline"
    copy_project(project, target)
    return target


def changed_entry(tmp_path, baseline, name="entry"):
    workspace = tmp_path / f"{name}-workspace"
    materialize(baseline, None, workspace)
    write(workspace / "app.py", "print('better')\n")
    write(workspace / "src" / "new.py", "NEW = True\n")
    (workspace / "old.py").unlink()
    entry = tmp_path / name
    capture(baseline, workspace, entry)
    return entry


def test_copy_project_skips_heavy_folders_and_secrets(tmp_path, project):
    write(project / "node_modules" / "lib" / "index.js", "x")
    write(project / ".git" / "HEAD", "ref")
    write(project / ".env", "API_KEY=secret")
    write(project / ".env.local", "API_KEY=secret")
    write(project / "certs" / "server.pem", "secret")
    target = tmp_path / "copy"

    copy_project(project, target)

    assert (target / "app.py").exists()
    assert not (target / "node_modules").exists()
    assert not (target / ".git").exists()
    assert not (target / ".env").exists()
    assert not (target / ".env.local").exists()
    assert not (target / "certs" / "server.pem").exists()


def test_capture_records_changed_new_and_deleted_files(tmp_path, baseline):
    entry = changed_entry(tmp_path, baseline)

    assert (entry / "files" / "app.py").read_text() == "print('better')\n"
    assert (entry / "files" / "src" / "new.py").read_text() == "NEW = True\n"
    assert not (entry / "files" / "src" / "util.py").exists()
    assert json.loads((entry / "deleted.json").read_text()) == ["old.py"]


def test_capture_writes_a_readable_diff(tmp_path, baseline):
    diff = (changed_entry(tmp_path, baseline) / "changes.diff").read_text()

    assert "-print('hello')" in diff
    assert "+print('better')" in diff
    assert "+NEW = True" in diff
    assert "-gone = True" in diff


def test_capture_ignores_rival_files_and_symlinks(tmp_path, baseline):
    workspace = tmp_path / "workspace"
    materialize(baseline, None, workspace)
    write(workspace / RIVALS_DIR / "A2" / "changes.diff", "rival")
    (workspace / "escape").symlink_to("/etc/hostname")
    entry = tmp_path / "entry"

    summary = capture(baseline, workspace, entry)

    assert summary == {"changed": [], "deleted": []}
    assert not (entry / "files" / RIVALS_DIR).exists()


def test_materialize_rebuilds_a_fighter_from_its_entry(tmp_path, baseline):
    entry = changed_entry(tmp_path, baseline)
    workspace = tmp_path / "rebuilt"
    write(workspace / "leftover.txt", "from a crashed run")

    materialize(baseline, entry, workspace)

    assert (workspace / "app.py").read_text() == "print('better')\n"
    assert (workspace / "src" / "new.py").exists()
    assert not (workspace / "old.py").exists()
    assert not (workspace / "leftover.txt").exists()


def test_add_rivals_exposes_each_rivals_latest_work(tmp_path, baseline):
    entry = changed_entry(tmp_path, baseline)
    workspace = tmp_path / "workspace"
    materialize(baseline, None, workspace)

    add_rivals(workspace, {"A2": entry})

    rival = workspace / RIVALS_DIR / "A2"
    assert (rival / "changes.diff").exists()
    assert (rival / "files" / "app.py").read_text() == "print('better')\n"


def test_similarity_is_high_for_copies_and_low_for_different_work(tmp_path, baseline):
    first = changed_entry(tmp_path, baseline, "first")
    copy = changed_entry(tmp_path, baseline, "copy")
    workspace = tmp_path / "other-workspace"
    materialize(baseline, None, workspace)
    write(workspace / "src" / "util.py", "VALUE = 2\nOTHER = 3\nMORE = 4\n")
    different = tmp_path / "different"
    capture(baseline, workspace, different)

    assert similarity(first, copy) == 1.0
    assert similarity(first, different) < 0.5


def test_similarity_ignores_two_empty_entries(tmp_path, baseline):
    empty_a = tmp_path / "a-entry"
    empty_b = tmp_path / "b-entry"
    capture(baseline, baseline, empty_a)
    capture(baseline, baseline, empty_b)

    assert similarity(empty_a, empty_b) == 0.0


def test_apply_entry_changes_the_project_and_backs_it_up(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    backup = tmp_path / "backup"

    applied = apply_entry(entry, baseline, project, backup)

    assert (project / "app.py").read_text() == "print('better')\n"
    assert (project / "src" / "new.py").exists()
    assert not (project / "old.py").exists()
    assert (backup / "files" / "app.py").read_text() == "print('hello')\n"
    assert applied == {"changed": ["app.py", "src/new.py"], "deleted": ["old.py"]}


def test_apply_entry_refuses_files_you_changed_during_the_battle(
    tmp_path, project, baseline
):
    entry = changed_entry(tmp_path, baseline)
    write(project / "app.py", "print('my own edit')\n")

    with pytest.raises(ApplyConflict) as conflict:
        apply_entry(entry, baseline, project, tmp_path / "backup")

    assert conflict.value.paths == ["app.py"]
    assert (project / "app.py").read_text() == "print('my own edit')\n"
    assert (project / "old.py").exists()
    assert not (project / "src" / "new.py").exists()


def test_apply_entry_refuses_a_new_file_that_already_appeared(
    tmp_path, project, baseline
):
    entry = changed_entry(tmp_path, baseline)
    write(project / "src" / "new.py", "mine\n")

    with pytest.raises(ApplyConflict) as conflict:
        apply_entry(entry, baseline, project, tmp_path / "backup")

    assert conflict.value.paths == ["src/new.py"]


def test_apply_entry_force_overrides_conflicts(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    write(project / "app.py", "print('my own edit')\n")

    apply_entry(entry, baseline, project, tmp_path / "backup", force=True)

    assert (project / "app.py").read_text() == "print('better')\n"


def test_apply_entry_rejects_paths_outside_the_project(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    (entry / "deleted.json").write_text(json.dumps(["../outside.txt"]))
    outside = write(tmp_path / "outside.txt", "keep me")

    with pytest.raises(ValueError):
        apply_entry(entry, baseline, project, tmp_path / "backup")

    assert outside.exists()


def test_undo_restores_the_project_exactly(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    backup = tmp_path / "backup"
    apply_entry(entry, baseline, project, backup)

    undo_apply(backup, project)

    assert (project / "app.py").read_text() == "print('hello')\n"
    assert (project / "old.py").read_text() == "gone = True\n"
    assert not (project / "src" / "new.py").exists()
