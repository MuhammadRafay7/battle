from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

LOCK_WAIT_SECONDS = 0.25
LOCK_RETRY_SECONDS = 0.01


def sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_folder(path: Path) -> None:
    with contextlib.suppress(OSError):
        sync_file(path)


def write_durable(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8", errors="backslashreplace") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
    sync_folder(path.parent)


class RunNotFound(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def battle_home() -> Path:
    return Path(os.environ.get("BATTLE_HOME", "~/.battle")).expanduser()


def _runs_folder() -> Path:
    local = Path.cwd() / ".battle" / "runs"
    if "BATTLE_HOME" not in os.environ and local.is_dir():
        return local
    return battle_home() / "runs"


def latest_run() -> str | None:
    folder = _runs_folder()
    if not folder.exists():
        return None
    runs = sorted(path.name for path in folder.iterdir() if (path / "state.json").exists())
    return runs[-1] if runs else None


class RunStore:
    def __init__(self, root: Path, state: dict[str, Any]) -> None:
        self.root = root
        self._state = state
        self._guard = threading.Lock()

    @classmethod
    def create(
        cls, task: str, project: Path, settings: dict[str, Any], home: Path | None = None
    ) -> "RunStore":
        now = datetime.datetime.now(datetime.timezone.utc)
        run_id = now.strftime("%Y%m%d-%H%M%S-%f")
        root = (home / "runs" if home else _runs_folder()) / run_id
        root.mkdir(parents=True, mode=0o700)
        store = cls(
            root,
            {
                "id": run_id,
                "task": task,
                "project": str(Path(project).resolve()),
                "settings": settings,
                "status": "running",
                "created": now.isoformat(),
                "steps": {},
            },
        )
        store._save()
        return store

    @classmethod
    def open(cls, run_id: str) -> "RunStore":
        root = _runs_folder() / run_id
        state_file = root / "state.json"
        if Path(run_id).name != run_id or not state_file.exists():
            raise RunNotFound(f"No battle called {run_id}.")
        return cls(root, json.loads(state_file.read_text(encoding="utf-8")))

    @property
    def id(self) -> str:
        return self._state["id"]

    @property
    def task(self) -> str:
        return self._state["task"]

    @property
    def project(self) -> Path:
        return Path(self._state["project"])

    @property
    def settings(self) -> dict[str, Any]:
        return self._state["settings"]

    @property
    def status(self) -> str:
        return self._state["status"]

    @property
    def steps(self) -> dict[str, Any]:
        return self._state["steps"]

    def has(self, key: str) -> bool:
        return key in self.steps

    def get(self, key: str) -> Any:
        return self.steps[key]

    def put(self, key: str, value: Any) -> None:
        with self._guard:
            self.steps[key] = value
            self._save()

    def forget(self, keys: Iterable[str]) -> None:
        with self._guard:
            for key in keys:
                self.steps.pop(key, None)
            self._save()

    def set_status(self, status: str) -> None:
        with self._guard:
            self._state["status"] = status
            self._save()

    def note(self, message: str) -> None:
        with self._guard, (self.root / "log.txt").open("a", encoding="utf-8") as log:
            log.write(message + "\n")

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with (self.root / "lock").open("w") as handle:
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise AlreadyRunning(f"Battle {self.id} is already running.") from error
                    time.sleep(LOCK_RETRY_SECONDS)
            with self._guard:
                self._state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
            yield

    def engine_running(self) -> bool:
        path = self.root / "lock"
        if not path.exists():
            return False
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    def _save(self) -> None:
        write_durable(self.root / "state.json", json.dumps(self._state, indent=2))
