from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol

from battle import tether
from battle.tether import EXIT_CANNOT_START, MARK_VARIABLE, START_FAILED, kill_marked

DEFAULT_TIMEOUT = 1200.0
TETHER = str(Path(tether.__file__).resolve())
POLL_SECONDS = 0.2
REAP_SECONDS = 2.0
SHORT_MESSAGE_CHARS = 300
FAILURE_LIMIT_PATTERN = re.compile(
    r"usage limit|limit reached|rate.?limit|quota|hit your .{0,30}limit|too many requests|\b429\b",
    re.IGNORECASE,
)
SUCCESS_LIMIT_PATTERN = re.compile(
    r"hit your .{0,30}limit|(usage|session|weekly) limit (reached|exceeded)|limit reached",
    re.IGNORECASE,
)
SESSION_VARIABLES = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_SESSION_ID",
    }
)
FIGHTER_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")
JUDGE_TOOLS = ("Read", "Grep", "Glob")
WEB_TOOLS = ("WebSearch", "WebFetch")


class AgentError(Exception):
    pass


class UsageLimitReached(AgentError):
    pass


class AgentStopped(AgentError):
    pass


class AgentTimedOut(AgentError):
    pass


@dataclass(frozen=True)
class Job:
    role: str
    prompt: str
    cwd: Path
    writes: bool
    read_dirs: tuple[Path, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    label: str = ""
    group: str = ""
    subagent: str = ""
    bash: bool = False


class Agent(Protocol):
    name: str

    def run(self, job: Job) -> str: ...


def extract_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    found = None
    position = 0
    while (position := text.find("{", position)) != -1:
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError:
            position += 1
            continue
        except RecursionError as error:
            raise ValueError("The reply's JSON is nested too deeply.") from error
        if isinstance(value, dict):
            found = value
    if found is None:
        raise ValueError("The reply has no JSON object.")
    return found


def child_env(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key not in SESSION_VARIABLES}


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class ProcessGroups:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._live: dict[subprocess.Popen, int] = {}
        self.cancelled = threading.Event()

    def start(self, argv: list[str], mark: str, **options: Any) -> subprocess.Popen:
        with self._guard:
            if self.cancelled.is_set():
                raise AgentStopped(f"{argv[0]} was not started because Battle is stopping.")
            read_end, write_end = os.pipe()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", TETHER, str(read_end), mark, *argv],
                    start_new_session=True,
                    pass_fds=(read_end,),
                    **options,
                )
            except BaseException:
                os.close(write_end)
                raise
            finally:
                os.close(read_end)
            self._live[process] = write_end
            return process

    def finish(self, process: subprocess.Popen, mark: str) -> None:
        with self._guard:
            write_end = self._live.pop(process)
            _kill_group(process)
            os.close(write_end)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=REAP_SECONDS)
        kill_marked(mark)

    def cancel(self) -> None:
        with self._guard:
            self.cancelled.set()
            for process in self._live:
                _kill_group(process)


@dataclass(frozen=True)
class _ProcessAgent:
    groups: ProcessGroups = field(
        default_factory=ProcessGroups, repr=False, compare=False, kw_only=True
    )

    def cancel(self) -> None:
        self.groups.cancel()


def _await_exit(process: subprocess.Popen, name: str, job: Job, groups: ProcessGroups) -> None:
    deadline = time.monotonic() + job.timeout
    while True:
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=max(0, min(POLL_SECONDS, remaining)))
        except subprocess.TimeoutExpired as error:
            if groups.cancelled.is_set():
                raise AgentStopped(f"{name} was stopped.") from error
            if time.monotonic() >= deadline:
                raise AgentTimedOut(f"{name} timed out after {job.timeout:g} seconds.") from error
            continue
        if groups.cancelled.is_set() and process.returncode != 0:
            raise AgentStopped(f"{name} was stopped.")
        return


def _decoded(output: BinaryIO) -> str:
    output.seek(0)
    return output.read().decode("utf-8", "replace")


def _execute(
    argv: list[str],
    job: Job,
    groups: ProcessGroups,
    extra_env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    mark = uuid.uuid4().hex
    with (
        tempfile.TemporaryFile() as prompt,
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        prompt.write(job.prompt.encode("utf-8", "replace"))
        prompt.seek(0)
        try:
            process = groups.start(
                argv,
                mark,
                cwd=job.cwd,
                env=child_env(os.environ) | dict(extra_env or {}) | {MARK_VARIABLE: mark},
                stdin=prompt,
                stdout=stdout,
                stderr=stderr,
            )
        except OSError as error:
            raise AgentError(f"{argv[0]} could not start: {error}") from error
        try:
            _await_exit(process, argv[0], job, groups)
        finally:
            groups.finish(process, mark)
        output, errors = _decoded(stdout), _decoded(stderr)
    if process.returncode == EXIT_CANNOT_START and errors.startswith(START_FAILED):
        raise AgentError(f"{argv[0]} {errors.removeprefix(START_FAILED)}")
    return process.returncode, output, errors


def _check(name: str, failed: bool, message: str) -> None:
    if failed:
        if FAILURE_LIMIT_PATTERN.search(message):
            raise UsageLimitReached(message.strip()[:500])
        raise AgentError(f"{name} failed: {message.strip()[:500]}")
    short_and_plain = len(message) <= SHORT_MESSAGE_CHARS and "{" not in message
    if short_and_plain and SUCCESS_LIMIT_PATTERN.search(message):
        raise UsageLimitReached(message.strip())


@dataclass(frozen=True)
class ClaudeAgent(_ProcessAgent):
    model: str | None = None
    allow_bash: bool = False
    web: bool = True
    binary: str = "claude"
    name: str = "claude"

    def command(self, job: Job) -> list[str]:
        shell = ("Bash",) if job.bash or (self.allow_bash and job.writes) else ()
        web = WEB_TOOLS if self.web else ()
        base = FIGHTER_TOOLS if job.writes else JUDGE_TOOLS
        tools = ",".join((*base, *shell, *web))
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "user",
            "--permission-mode",
            "acceptEdits" if job.writes else "dontAsk",
            "--tools",
            tools,
            "--allowedTools",
            tools,
        ]
        if self.model:
            argv += ["--model", self.model]
        for directory in job.read_dirs:
            argv += ["--add-dir", str(directory)]
        return argv

    def run(self, job: Job) -> str:
        code, stdout, stderr = _execute(self.command(job), job, self.groups)
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            text = str(payload.get("result") or "")
            failed = code != 0 or bool(payload.get("is_error"))
        else:
            text, failed = stdout, code != 0
        _check(self.name, failed, text or stderr or stdout)
        return text


@dataclass(frozen=True)
class CodexAgent(_ProcessAgent):
    model: str | None = None
    web: bool = True
    binary: str = "codex"
    name: str = "codex"

    def command(self, job: Job, reply_file: Path) -> list[str]:
        argv = [
            self.binary,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "workspace-write" if job.writes else "read-only",
            "-c",
            f'web_search="{"live" if self.web else "disabled"}"',
            "-C",
            str(job.cwd),
            "-o",
            str(reply_file),
        ]
        if self.model:
            argv += ["-m", self.model]
        return [*argv, "-"]

    def run(self, job: Job) -> str:
        with tempfile.TemporaryDirectory(prefix="battle-codex-") as folder:
            reply_file = Path(folder) / "reply.txt"
            code, stdout, stderr = _execute(self.command(job, reply_file), job, self.groups)
            text = (
                reply_file.read_text(encoding="utf-8", errors="replace")
                if reply_file.exists()
                else ""
            )
        _check(self.name, code != 0, text if code == 0 else stderr or stdout or text)
        return text


@dataclass(frozen=True)
class CommandAgent(_ProcessAgent):
    command_line: str
    web: bool = True
    name: str = "command"

    def run(self, job: Job) -> str:
        code, stdout, stderr = _execute(
            shlex.split(self.command_line),
            job,
            self.groups,
            {"BATTLE_ROLE": job.role, "BATTLE_WEB": "1" if self.web else "0"},
        )
        _check(self.name, code != 0, stdout if code == 0 else stderr or stdout)
        return stdout


def make_agent(
    spec: str, model: str | None = None, allow_bash: bool = False, web: bool = True
) -> Agent:
    if spec == "claude":
        return ClaudeAgent(model=model, allow_bash=allow_bash, web=web)
    if spec == "codex":
        return CodexAgent(model=model, web=web)
    if spec.startswith("command:"):
        command_line = spec.removeprefix("command:")
        if not shlex.split(command_line):
            raise ValueError("Give a command after command:.")
        return CommandAgent(command_line, web=web)
    raise ValueError(f"Unknown agent: {spec}. Use claude, codex or command:<your command>.")
