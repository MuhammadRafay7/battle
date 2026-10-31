from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
from pathlib import Path, PurePosixPath

RIVALS_DIR = ".battle-rivals"
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
        RIVALS_DIR,
    }
)
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519")
SHAREABLE_ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})
MAX_SIMILARITY_LINES = 4000


class ApplyConflict(Exception):
    def __init__(self, paths: list[str]) -> None:
        super().__init__("Changed since the battle started: " + ", ".join(paths))
        self.paths = paths


def _is_secret(name: str) -> bool:
    if name in SHAREABLE_ENV_FILES:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        for name in names:
            path = Path(folder, name)
            if path.is_symlink() or _is_secret(name):
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def _same_bytes(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    return first.read_bytes() == second.read_bytes()


def _same_file(first: Path, second: Path) -> bool:
    if first.exists() != second.exists():
        return False
    return not first.exists() or _same_bytes(first, second)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _require_inside(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Refusing a path outside the project: {relative}")


def copy_project(source: Path, target: Path) -> None:
    def ignore(folder: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if _is_secret(name)
            or (name in EXCLUDED_DIRS and Path(folder, name).is_dir())
        ]

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
    (entry / "changes.diff").write_text(diff, encoding="utf-8")
    (entry / "deleted.json").write_text(json.dumps(deleted), encoding="utf-8")
    return {"changed": changed, "deleted": deleted}


def materialize(baseline: Path, entry: Path | None, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    copy_project(baseline, workspace)
    if entry is None:
        return
    for relative, path in _files(entry / "files").items():
        _copy(path, workspace / relative)
    for relative in json.loads((entry / "deleted.json").read_text(encoding="utf-8")):
        _require_inside(relative)
        (workspace / relative).unlink(missing_ok=True)


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


def apply_entry(
    entry: Path, baseline: Path, project: Path, backup: Path, force: bool = False
) -> dict[str, list[str]]:
    incoming = _files(entry / "files")
    changed = sorted(incoming)
    deleted = json.loads((entry / "deleted.json").read_text(encoding="utf-8"))
    for relative in [*changed, *deleted]:
        _require_inside(relative)
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

    created = [relative for relative in changed if not (project / relative).exists()]
    (backup / "files").mkdir(parents=True)
    for relative in [*changed, *deleted]:
        if (project / relative).exists():
            _copy(project / relative, backup / "files" / relative)
    (backup / "manifest.json").write_text(json.dumps({"created": created}), encoding="utf-8")

    for relative in changed:
        _copy(incoming[relative], project / relative)
    for relative in deleted:
        (project / relative).unlink(missing_ok=True)
    return {"changed": changed, "deleted": deleted}


def undo_apply(backup: Path, project: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for relative in manifest["created"]:
        _require_inside(relative)
        target = project / relative
        target.unlink(missing_ok=True)
        for parent in target.parents:
            if parent == project or any(parent.iterdir()):
                break
            parent.rmdir()
    for relative, path in _files(backup / "files").items():
        _copy(path, project / relative)
    shutil.rmtree(backup)
