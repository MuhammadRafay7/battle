from __future__ import annotations

import contextlib
import difflib
import fnmatch
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Iterator

from battle.store import sync_file, sync_folder, write_durable

RIVALS_DIR = ".battle-rivals"
PARTIAL_FILE = ".battle-partial"
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "target",
        ".gradle",
        ".battle",
        ".team",
        RIVALS_DIR,
    }
)
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
)
SHAREABLE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})
MAX_SIMILARITY_LINES = 4000


class ApplyConflict(Exception):
    def __init__(self, paths: list[str]) -> None:
        super().__init__("Changed since the battle started: " + ", ".join(paths))
        self.paths = paths


class ApplyRefused(ValueError):
    pass


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    if lowered in SHAREABLE_ENV_FILES:
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_PATTERNS)


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        for name in names:
            path = Path(folder, name)
            if path.is_symlink() or not path.is_file() or _is_secret(name):
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def _same_bytes(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    return first.read_bytes() == second.read_bytes()


def _same_file(first: Path, second: Path) -> bool:
    if first.is_file() != second.is_file():
        return False
    return not first.is_file() or _same_bytes(first, second)


def _copy(source: Path, target: Path) -> None:
    if target.is_dir():
        raise IsADirectoryError(f"A folder is in the way of {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _install(source: Path, target: Path) -> None:
    if target.is_dir():
        raise IsADirectoryError(f"A folder is in the way of {target}")
    partial = target.parent / PARTIAL_FILE
    if os.path.lexists(partial):
        partial.unlink()
    _copy(source, partial)
    sync_file(partial)
    os.replace(partial, target)


def _remove(root: Path, relative: str) -> None:
    target = root / relative
    if target.is_file():
        target.unlink()
    for parent in target.parents:
        if parent == root:
            break
        if not parent.is_dir():
            continue
        if any(parent.iterdir()):
            break
        parent.rmdir()


def _clear_partial(root: Path, relative: str) -> None:
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        ((root / relative).parent / PARTIAL_FILE).unlink()


def _links_on(root: Path, relative: str) -> Iterator[Path]:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            yield current


def _require_inside(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Refusing a path outside the project: {relative}")


def _require_safe(root: Path, relative: str) -> None:
    _require_inside(relative)
    if any(_links_on(root, relative)):
        raise ApplyRefused(f"Refusing to write through a symlink: {relative}")


def _unsafe_link(path: Path, root: Path) -> bool:
    target = Path(os.path.realpath(path))
    if os.path.isabs(os.readlink(path)) or not target.is_relative_to(root):
        return True
    return any(
        part in EXCLUDED_DIRS or _is_secret(part) for part in target.relative_to(root).parts
    )


def copy_project(source: Path, target: Path) -> None:
    root = Path(os.path.realpath(source))

    def skipped(path: Path, name: str) -> bool:
        if path.is_symlink():
            return _is_secret(name) or _unsafe_link(path, root)
        if path.is_dir():
            return _is_secret(name) or name in EXCLUDED_DIRS
        return _is_secret(name) or not path.is_file()

    def ignore(folder: str, names: list[str]) -> list[str]:
        return [name for name in names if skipped(Path(folder, name), name)]

    shutil.copytree(source, target, symlinks=True, ignore=ignore)


def _text_lines(path: Path | None) -> list[str] | None:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _file_diff(relative: str, old: Path | None, new: Path | None) -> str:
    old_lines, new_lines = _text_lines(old), _text_lines(new)
    if old_lines is None or new_lines is None:
        return f"Binary file changed: {relative}\n"
    lines = difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{relative}", tofile=f"b/{relative}"
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def capture(baseline: Path, workspace: Path, entry: Path) -> dict[str, list[str]]:
    before, after = _files(baseline), _files(workspace)
    changed = sorted(
        relative
        for relative, path in after.items()
        if relative not in before or not _same_bytes(path, before[relative])
    )
    deleted = sorted(set(before) - set(after))

    if entry.exists():
        shutil.rmtree(entry)
    (entry / "files").mkdir(parents=True)
    for relative in changed:
        _copy(after[relative], entry / "files" / relative)

    diff = "".join(
        _file_diff(relative, before.get(relative), after.get(relative))
        for relative in sorted([*changed, *deleted])
    )
    (entry / "changes.diff").write_text(diff, encoding="utf-8", errors="backslashreplace")
    (entry / "deleted.json").write_text(json.dumps(deleted), encoding="utf-8")
    return {"changed": changed, "deleted": deleted}


def materialize(baseline: Path, entry: Path | None, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    copy_project(baseline, workspace)
    if entry is None:
        return
    for relative in json.loads((entry / "deleted.json").read_text(encoding="utf-8")):
        _require_inside(relative)
        _remove(workspace, relative)
    for relative, path in _files(entry / "files").items():
        for link in _links_on(workspace, relative):
            link.unlink()
        _copy(path, workspace / relative)


def add_rivals(workspace: Path, rivals: dict[str, Path]) -> None:
    for fighter, entry in rivals.items():
        target = workspace / RIVALS_DIR / fighter
        target.mkdir(parents=True)
        shutil.copy2(entry / "changes.diff", target / "changes.diff")
        shutil.copytree(entry / "files", target / "files")


def _changed_lines(entry: Path) -> list[str]:
    text = (entry / "changes.diff").read_text(encoding="utf-8", errors="replace")
    lines = [
        line
        for line in text.splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    ]
    return lines[:MAX_SIMILARITY_LINES]


def similarity(first: Path, second: Path) -> float:
    first_lines, second_lines = _changed_lines(first), _changed_lines(second)
    if not first_lines or not second_lines:
        return 0.0
    return difflib.SequenceMatcher(None, first_lines, second_lines, autojunk=False).ratio()


def _partial_backup(backup: Path) -> Path:
    return backup.with_name(backup.name + ".partial")


def _retired_backup(backup: Path) -> Path:
    return backup.with_name(backup.name + ".retired")


def _clear_backup_leftovers(backup: Path) -> None:
    for leftover in (_partial_backup(backup), _retired_backup(backup)):
        if leftover.exists():
            shutil.rmtree(leftover)


def apply_entry(
    entry: Path, baseline: Path, project: Path, backup: Path, force: bool = False
) -> dict[str, list[str]]:
    incoming = _files(entry / "files")
    changed = sorted(incoming)
    deleted = json.loads((entry / "deleted.json").read_text(encoding="utf-8"))
    for relative in [*changed, *deleted]:
        _require_safe(project, relative)
    if backup.exists():
        raise ValueError("This champion has already been applied.")

    if not force:
        conflicts = [
            relative
            for relative in changed
            if not _same_file(project / relative, baseline / relative)
            and not _same_file(project / relative, incoming[relative])
        ]
        conflicts += [
            relative
            for relative in deleted
            if (project / relative).exists()
            and not _same_file(project / relative, baseline / relative)
        ]
        if conflicts:
            raise ApplyConflict(sorted(conflicts))

    created = [relative for relative in changed if not (project / relative).is_file()]
    _clear_backup_leftovers(backup)
    partial = _partial_backup(backup)
    (partial / "files").mkdir(parents=True)
    for relative in [*changed, *deleted]:
        if (project / relative).is_file():
            _install(project / relative, partial / "files" / relative)
    write_durable(partial / "manifest.json", json.dumps({"created": created}))
    partial.rename(backup)
    sync_folder(backup.parent)

    try:
        for relative in deleted:
            _remove(project, relative)
        for relative in changed:
            _install(incoming[relative], project / relative)
    except OSError:
        undo_apply(backup, project)
        raise
    return {"changed": changed, "deleted": deleted}


def undo_apply(backup: Path, project: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    restored = _files(backup / "files")
    for relative in [*manifest["created"], *restored]:
        _require_safe(project, relative)
    for relative in [*manifest["created"], *restored]:
        _clear_partial(project, relative)
    for relative in manifest["created"]:
        _remove(project, relative)
    for relative, path in restored.items():
        _install(path, project / relative)
    _clear_backup_leftovers(backup)
    backup.rename(_retired_backup(backup))
    sync_folder(backup.parent)
    shutil.rmtree(_retired_backup(backup))
