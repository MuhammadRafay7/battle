from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

ROLES = ("fighter", "judge", "lead")
AGENT_TOOLS = ("Agent", "Task")
LEAD_AGENTS = ("battle-fighter", "battle-judge")
REPLY_END = "BATTLE_REPLY"
BATTLE = Path(__file__).resolve().parent.parent / "bin" / "battle"
JOBS_COMMAND = re.compile(r'^"(?P<path>[^"]+)" jobs [\w-]+ --group \w+$')
REPLY_COMMAND = re.compile(
    rf"""^"(?P<path>[^"]+)" reply \d+ [\w-]+(?: --failed| --usage-limit)? <<'{REPLY_END}'$"""
)
FILE_TOOLS = ("Read", "Edit", "Write")
EXIT_BLOCKED = 2
BEFORE_FIRST_RUN = r"(?:(?!/\.battle/runs/).)*"
RUN_FOLDER = re.compile(rf"^(?P<run>(?P<home>{BEFORE_FIRST_RUN})/\.battle/runs/[^/]+)(?:/|$)")
ROOTED_PATTERN = re.compile(r"(?:^|[{,])\s*[/~]")
PROMPT_FILE = re.compile(rf"^{BEFORE_FIRST_RUN}/\.battle/runs/[^/]+/jobs/\d+\.prompt\.md$")
WORKSPACE = re.compile(
    rf"^(?P<run>{BEFORE_FIRST_RUN}/\.battle/runs/[^/]+)/workspace/(?P<fighter>[^/]+)(?:/|$)"
)


def _claim(claims: Path, agent: str, fighter: str) -> str:
    claims.mkdir(exist_ok=True)
    path = claims / re.sub(r"[^\w-]", "_", agent)
    handle, partial = tempfile.mkstemp(dir=claims)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(fighter)
    try:
        os.link(partial, path)
    except FileExistsError:
        return path.read_text(encoding="utf-8")
    finally:
        os.unlink(partial)
    return fighter


def _escapes(pattern: str | None) -> bool:
    return bool(pattern) and (ROOTED_PATTERN.search(pattern) is not None or ".." in pattern)


def _owns_run(run: str, home: str, cwd: str) -> bool:
    state = Path(run, "state.json")
    if state.exists():
        project = Path(os.path.realpath(json.loads(state.read_text(encoding="utf-8"))["project"]))
        return Path(cwd).is_relative_to(project) or project.is_relative_to(cwd)
    configured = os.environ.get("BATTLE_HOME")
    if configured and os.path.realpath(os.path.expanduser(configured)) == f"{home}/.battle":
        return True
    return Path(cwd).is_relative_to(home)


def _check_prompt(prompt: str, role: str, agent: str | None) -> str | None:
    record = Path(prompt.removesuffix(".prompt.md") + ".json")
    if role == "judge" or not agent or not record.exists():
        return None
    cwd = json.loads(record.read_text(encoding="utf-8"))["cwd"]
    match = WORKSPACE.match(os.path.realpath(cwd))
    if match is None:
        return f"{prompt} belongs to a judge."
    if _claim(Path(match["run"]) / "claims", agent, match["fighter"]) != match["fighter"]:
        return f"{prompt} belongs to another fighter."
    return None


def _approved_command(command: str) -> bool:
    lines = command.strip().split("\n")
    single = len(lines) == 1
    match = (JOBS_COMMAND if single else REPLY_COMMAND).match(lines[0])
    if match is None or os.path.realpath(match["path"]) != os.path.realpath(BATTLE):
        return False
    return single or (lines[-1] == REPLY_END and REPLY_END not in lines[1:-1])


def _check_lead(tool: str, tool_input: dict[str, Any]) -> str | None:
    if tool in AGENT_TOOLS:
        if tool_input.get("subagent_type") in LEAD_AGENTS:
            return None
        return "A lead can only start battle-fighter and battle-judge agents."
    if tool == "Bash" and _approved_command(tool_input["command"]):
        return None
    return "A lead can only run battle jobs and battle reply."


def check(payload: dict[str, Any], role: str) -> str | None:
    tool = payload["tool_name"]
    tool_input = payload["tool_input"]
    if role == "lead":
        return _check_lead(tool, tool_input)
    target = tool_input.get("file_path" if tool in FILE_TOOLS else "path")
    if not target or target.startswith("~"):
        return "Pass an absolute path inside your folder."
    if _escapes(tool_input.get("glob")) or (tool == "Glob" and _escapes(tool_input.get("pattern"))):
        return "Search patterns must stay inside your folder."

    cwd = os.path.realpath(payload["cwd"])
    resolved = os.path.realpath(os.path.join(cwd, target))
    run = RUN_FOLDER.match(resolved)
    if run and not _owns_run(run["run"], run["home"], cwd):
        return f"{resolved} belongs to another project's battle."
    if tool == "Read" and PROMPT_FILE.match(resolved):
        return _check_prompt(resolved, role, payload.get("agent_id"))
    if role == "judge":
        return None if run else f"{resolved} is outside this battle's folder."

    match = WORKSPACE.match(resolved)
    if match is None:
        return f"{resolved} is outside your workspace."
    agent = payload.get("agent_id")
    if not agent:
        return "This call has no agent id."
    owner = _claim(Path(match["run"]) / "claims", agent, match["fighter"])
    if owner != match["fighter"]:
        return f"{resolved} belongs to another fighter."
    return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        role = arguments[0]
        if role not in ROLES:
            raise ValueError(f"Unknown role {role}.")
        reason = check(json.loads(sys.stdin.read()), role)
    except Exception as error:
        reason = f"The guard could not check this call: {error}"
    if reason:
        print(f"Blocked: {reason}", file=sys.stderr)
        return EXIT_BLOCKED
    return 0
