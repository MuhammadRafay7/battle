from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from battle.agents import child_env
from battle.workspace import EXCLUDED_DIRS

GRADIENT = re.compile(r"gradient", re.IGNORECASE)
UI_SUFFIXES = frozenset(
    {
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".html",
        ".htm",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".svelte",
        ".astro",
        ".mjs",
        ".cjs",
    }
)
MAX_HITS = 40
TAIL_CHARS = 3000
MANIFEST_STEPS = ("install", "build", "test")
SERVER_WAIT_SECONDS = 90.0
STOP_WAIT_SECONDS = 5.0


@dataclass
class CheckReport:
    gradients: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    manifest_error: str | None = None

    @property
    def built(self) -> bool:
        return self.manifest_error is None and all(step["ok"] for step in self.steps)

    def render(self) -> str:
        lines = []
        if self.gradients:
            lines.append(f"Gradient rule broken in {len(self.gradients)} places:")
            lines += [f"  {hit}" for hit in self.gradients]
        else:
            lines.append("Gradient rule: no gradients found.")
        if self.manifest_error:
            lines.append(f"Run manifest: {self.manifest_error}")
        for step in self.steps:
            state = "passed" if step["ok"] else "FAILED"
            lines.append(f"{step['step']} ({step['command']}): {state}")
            if not step["ok"] and step["output"]:
                lines.append(step["output"])
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gradients": self.gradients,
            "built": self.built,
            "manifest_error": self.manifest_error,
            "steps": [{key: step[key] for key in ("step", "command", "ok")} for step in self.steps],
        }


def gradients(root: Path) -> list[str]:
    hits: list[str] = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        for name in sorted(names):
            path = Path(folder, name)
            if path.suffix.lower() not in UI_SUFFIXES or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if GRADIENT.search(line):
                    relative = path.relative_to(root).as_posix()
                    hits.append(f"{relative}:{number}: {line.strip()[:120]}")
                    if len(hits) >= MAX_HITS:
                        return hits
    return hits


def read_manifest(workspace: Path, relative: str) -> dict[str, Any]:
    path = workspace / relative
    if not path.is_file():
        raise ValueError(f"{relative} is missing.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"{relative} is not valid JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError(f"{relative} must be a JSON object.")
    for key, value in manifest.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{relative}: {key} must be a string or null.")
    return manifest


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:]


def _kill(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=STOP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_command(
    command: str, cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=child_env(os.environ) | (env or {}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return {"command": command, "ok": False, "output": str(error)}
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(process)
        output, _ = process.communicate()
        text = _tail(output.decode("utf-8", "replace"))
        return {"command": command, "ok": False, "output": f"{text}\n(timed out after {timeout:g}s)"}
    return {
        "command": command,
        "ok": process.returncode == 0,
        "output": _tail(output.decode("utf-8", "replace")),
    }


def run_manifest(workspace: Path, manifest: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    results = []
    for step in MANIFEST_STEPS:
        command = manifest.get(step)
        if not command:
            continue
        result = {"step": step, **run_command(command, workspace, timeout)}
        results.append(result)
        if not result["ok"]:
            break
    return results


def check_design(workspace: Path, design_dir: str) -> CheckReport:
    folder = workspace / design_dir
    report = CheckReport()
    if not folder.is_dir():
        report.manifest_error = f"{design_dir} is missing."
        return report
    report.gradients = gradients(folder)
    return report


def check_build(workspace: Path, manifest_path: str, timeout: float) -> CheckReport:
    report = CheckReport(gradients=gradients(workspace))
    try:
        manifest = read_manifest(workspace, manifest_path)
    except ValueError as error:
        report.manifest_error = str(error)
        return report
    report.steps = run_manifest(workspace, manifest, timeout)
    return report


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@contextlib.contextmanager
def serve(
    workspace: Path, manifest: dict[str, Any], log: Path, wait: float = SERVER_WAIT_SECONDS
) -> Iterator[str | None]:
    command = manifest.get("start")
    if not command:
        yield None
        return
    port = free_port()
    url = (manifest.get("url") or "http://127.0.0.1:{port}").replace("{port}", str(port))
    with log.open("ab") as handle:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=workspace,
            env=child_env(os.environ) | {"PORT": str(port)},
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + wait
            while not _listening(port):
                if process.poll() is not None or time.monotonic() >= deadline:
                    yield None
                    return
                time.sleep(0.25)
            yield url
        finally:
            _kill(process)
