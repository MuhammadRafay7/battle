import json
import os
from pathlib import Path

import pytest

from battle.workspace import (
    PARTIAL_FILE,
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


def entry_from(tmp_path, baseline, change):
    workspace = tmp_path / "change-workspace"
    materialize(baseline, None, workspace)
    change(workspace)
    entry = tmp_path / "change-entry"
    capture(baseline, workspace, entry)
    return entry


def file_becomes_folder(workspace):
    (workspace / "old.py").unlink()
    write(workspace / "old.py" / "inner.py", "inner\n")


def folder_becomes_file(workspace):
    (workspace / "src" / "util.py").unlink()
    (workspace / "src").rmdir()
    write(workspace / "src", "now a file\n")


def test_apply_and_undo_turn_a_file_into_a_folder(tmp_path, project, baseline):
    entry = entry_from(tmp_path, baseline, file_becomes_folder)
    backup = tmp_path / "backup"

    apply_entry(entry, baseline, project, backup)
    assert (project / "old.py" / "inner.py").read_text() == "inner\n"

    undo_apply(backup, project)
    assert (project / "old.py").read_text() == "gone = True\n"


def test_apply_and_undo_turn_a_folder_into_a_file(tmp_path, project, baseline):
    entry = entry_from(tmp_path, baseline, folder_becomes_file)
    backup = tmp_path / "backup"

    apply_entry(entry, baseline, project, backup)
    assert (project / "src").read_text() == "now a file\n"

    undo_apply(backup, project)
    assert (project / "src" / "util.py").read_text() == "VALUE = 1\n"


@pytest.mark.parametrize("change", [file_becomes_folder, folder_becomes_file])
def test_materialize_handles_a_file_and_folder_swap(tmp_path, baseline, change):
    entry = entry_from(tmp_path, baseline, change)
    rebuilt = tmp_path / "rebuilt"

    materialize(baseline, entry, rebuilt)

    assert capture(baseline, rebuilt, tmp_path / "again") == {
        "changed": json_changed(entry),
        "deleted": json.loads((entry / "deleted.json").read_text()),
    }


def json_changed(entry):
    root = entry / "files"
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def test_apply_refuses_to_write_through_a_symlinked_file(tmp_path, project, baseline):
    outside = write(tmp_path / "outside.txt", "keep me")
    (project / "app.py").unlink()
    (project / "app.py").symlink_to(outside)
    entry = changed_entry(tmp_path, baseline)

    with pytest.raises(ValueError, match="symlink"):
        apply_entry(entry, baseline, project, tmp_path / "backup", force=True)

    assert outside.read_text() == "keep me"
    assert not (tmp_path / "backup").exists()


def test_apply_refuses_to_write_through_a_symlinked_folder(tmp_path, project, baseline):
    outside = tmp_path / "outside"
    write(outside / "util.py", "VALUE = 1\n")
    (project / "src" / "util.py").unlink()
    (project / "src").rmdir()
    (project / "src").symlink_to(outside)
    entry = changed_entry(tmp_path, baseline)

    with pytest.raises(ValueError, match="symlink"):
        apply_entry(entry, baseline, project, tmp_path / "backup", force=True)

    assert not (outside / "new.py").exists()


def test_materialize_replaces_a_link_instead_of_writing_through_it(tmp_path, baseline):
    (baseline / "alias.py").symlink_to("app.py")

    def replace_link(workspace):
        (workspace / "alias.py").unlink()
        write(workspace / "alias.py", "mine\n")

    entry = entry_from(tmp_path, baseline, replace_link)
    rebuilt = tmp_path / "rebuilt"

    materialize(baseline, entry, rebuilt)

    assert not (rebuilt / "alias.py").is_symlink()
    assert (rebuilt / "alias.py").read_text() == "mine\n"
    assert (rebuilt / "app.py").read_text() == "print('hello')\n"


def test_copy_project_drops_links_to_outside_and_to_secrets(tmp_path, project):
    outside = write(tmp_path / "private.txt", "secret")
    write(project / ".env", "API_KEY=secret")
    (project / "away.txt").symlink_to(outside)
    (project / "harmless.env").symlink_to(project / ".env")
    (project / "alias.py").symlink_to("app.py")
    target = tmp_path / "copy"

    copy_project(project, target)

    assert not (target / "away.txt").is_symlink()
    assert not (target / "harmless.env").is_symlink()
    assert (target / "alias.py").is_symlink()


def test_special_files_are_skipped(tmp_path, project, baseline):
    os.mkfifo(project / "pipe")
    target = tmp_path / "copy"
    copy_project(project, target)
    assert not (target / "pipe").exists()

    workspace = tmp_path / "workspace"
    materialize(baseline, None, workspace)
    os.mkfifo(workspace / "pipe")
    assert capture(baseline, workspace, tmp_path / "entry") == {"changed": [], "deleted": []}


def test_a_failed_apply_puts_the_project_back(tmp_path, project, baseline):
    def add_blocked_file(workspace):
        write(workspace / "app.py", "print('better')\n")
        (workspace / "old.py").unlink()
        write(workspace / "zzz.py", "blocked\n")

    entry = entry_from(tmp_path, baseline, add_blocked_file)
    write(project / "zzz.py" / "keep.txt", "a folder in the way")
    backup = tmp_path / "backup"

    with pytest.raises(OSError):
        apply_entry(entry, baseline, project, backup)

    assert (project / "app.py").read_text() == "print('hello')\n"
    assert (project / "old.py").read_text() == "gone = True\n"
    assert (project / "zzz.py" / "keep.txt").exists()
    assert not backup.exists()


@pytest.mark.parametrize(
    "name", [".ENV", "SERVER.PEM", "prod.env", "id_ecdsa", ".npmrc", ".netrc", ".pgpass", "app.jks"]
)
def test_copy_project_skips_every_kind_of_secret(tmp_path, project, name):
    write(project / name, "secret")
    target = tmp_path / "copy"

    copy_project(project, target)

    assert not (target / name).exists()


def test_copy_project_drops_an_absolute_link_back_into_the_project(tmp_path, project):
    (project / "pinned.py").symlink_to(project / "app.py")
    target = tmp_path / "copy"

    copy_project(project, target)

    assert not os.path.lexists(target / "pinned.py")


def test_undo_refuses_to_write_through_a_symlink(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    backup = tmp_path / "backup"
    apply_entry(entry, baseline, project, backup)
    outside = write(tmp_path / "outside.txt", "keep me")
    (project / "app.py").unlink()
    (project / "app.py").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        undo_apply(backup, project)

    assert outside.read_text() == "keep me"
    assert backup.exists()


class Killed(BaseException):
    pass


def test_a_kill_mid_apply_tears_no_file_and_undo_clears_up(
    tmp_path, project, baseline, monkeypatch
):
    entry = changed_entry(tmp_path, baseline)
    backup = tmp_path / "backup"
    real_replace = os.replace

    def killed_at_the_project(source, target):
        if Path(target).parent == project:
            raise Killed
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", killed_at_the_project)
    with pytest.raises(Killed):
        apply_entry(entry, baseline, project, backup)
    monkeypatch.undo()

    assert (project / "app.py").read_text() == "print('hello')\n"
    assert (project / PARTIAL_FILE).exists()

    undo_apply(backup, project)

    assert (project / "old.py").read_text() == "gone = True\n"
    assert not (project / PARTIAL_FILE).exists()
    assert not backup.exists()


def test_undo_removes_a_new_folder_left_with_only_a_partial_file(tmp_path, project, baseline):
    entry = entry_from(tmp_path, baseline, lambda folder: write(folder / "fresh" / "new.py", "x"))
    backup = tmp_path / "backup"
    apply_entry(entry, baseline, project, backup)
    (project / "fresh" / "new.py").rename(project / "fresh" / PARTIAL_FILE)

    undo_apply(backup, project)

    assert not (project / "fresh").exists()


def test_a_kill_while_deleting_the_backup_cannot_block_the_next_apply(
    tmp_path, project, baseline
):
    entry = changed_entry(tmp_path, baseline)
    backup = tmp_path / "backup"
    retired = tmp_path / "backup.retired"
    write(retired / "files" / "junk.py", "half deleted")

    apply_entry(entry, baseline, project, backup)
    undo_apply(backup, project)

    assert not retired.exists()
    apply_entry(entry, baseline, project, backup)
    assert (project / "app.py").read_text() == "print('better')\n"


@pytest.mark.skipif(os.uname().sysname == "Darwin", reason="macOS refuses names that aren't UTF-8")
def test_a_file_name_that_is_not_utf8_cannot_stop_a_fighters_turn(tmp_path, baseline):
    workspace = tmp_path / "workspace"
    materialize(baseline, None, workspace)
    (workspace / os.fsdecode(b"bad\xff.txt")).write_text("x")

    capture(baseline, workspace, tmp_path / "entry")

    assert "bad" in (tmp_path / "entry" / "changes.diff").read_text()


def test_a_half_written_backup_does_not_block_the_next_apply(tmp_path, project, baseline):
    entry = changed_entry(tmp_path, baseline)
    write(tmp_path / "backup.partial" / "files" / "junk.py", "half")
    backup = tmp_path / "backup"

    apply_entry(entry, baseline, project, backup)

    assert not (backup / "files" / "junk.py").exists()
    assert not (tmp_path / "backup.partial").exists()
