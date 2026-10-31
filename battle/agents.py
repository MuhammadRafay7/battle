from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

DEFAULT_TIMEOUT = 1800.0
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


class AgentError(Exception):
    pass


class UsageLimitReached(AgentError):
    pass


@dataclass(frozen=True)
class Job:
    role: str
    prompt: str
    cwd: Path
    writes: bool
    read_dirs: tuple[Path, ...] = ()
    timeout: float = DEFAULT_TIMEOUT


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
        if isinstance(value, dict):
            found = value
    if found is None:
        raise ValueError("The reply has no JSON object.")
    return found


def child_env(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key not in SESSION_VARIABLES}


def _execute(
    argv: list[str], job: Job, extra_env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    try:
        process = subprocess.Popen(
            argv,
            cwd=job.cwd,
            env=child_env(os.environ) | dict(extra_env or {}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise AgentError(f"{argv[0]} is not installed or not on PATH.") from error
    try:
        stdout, stderr = process.communicate(job.prompt, timeout=job.timeout)
    except BaseException as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise AgentError(f"{argv[0]} timed out after {job.timeout:g} seconds.") from error
        raise
    return process.returncode, stdout, stderr


def _check(name: str, failed: bool, message: str) -> None:
    if failed:
        if FAILURE_LIMIT_PATTERN.search(message):
            raise UsageLimitReached(message.strip()[:500])
        raise AgentError(f"{name} failed: {message.strip()[:500]}")
    short_and_plain = len(message) <= SHORT_MESSAGE_CHARS and "{" not in message
    if short_and_plain and SUCCESS_LIMIT_PATTERN.search(message):
        raise UsageLimitReached(message.strip())


@dataclass(frozen=True)
class ClaudeAgent:
    model: str | None = None
    allow_bash: bool = False
    binary: str = "claude"
    name: str = "claude"

    def command(self, job: Job) -> list[str]:
        shell = ("Bash",) if self.allow_bash else ()
        tools = ",".join((*FIGHTER_TOOLS, *shell) if job.writes else JUDGE_TOOLS)
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
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
        code, stdout, stderr = _execute(self.command(job), job)
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
class CodexAgent:
    model: str | None = None
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
            code, stdout, stderr = _execute(self.command(job, reply_file), job)
            text = reply_file.read_text(encoding="utf-8") if reply_file.exists() else ""
        _check(self.name, code != 0, text if code == 0 else stderr or stdout or text)
        return text


@dataclass(frozen=True)
class CommandAgent:
    command_line: str
    name: str = "command"

    def run(self, job: Job) -> str:
        code, stdout, stderr = _execute(
            shlex.split(self.command_line), job, {"BATTLE_ROLE": job.role}
        )
        _check(self.name, code != 0, stdout if code == 0 else stderr or stdout)
        return stdout


def make_agent(spec: str, model: str | None = None, allow_bash: bool = False) -> Agent:
    if spec == "claude":
        return ClaudeAgent(model=model, allow_bash=allow_bash)
    if spec == "codex":
        return CodexAgent(model=model)
    if spec.startswith("command:"):
        return CommandAgent(spec.removeprefix("command:"))
    raise ValueError(f"Unknown agent: {spec}. Use claude, codex or command:<your command>.")
