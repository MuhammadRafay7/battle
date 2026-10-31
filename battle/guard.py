from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

ROLES = ("fighter", "judge")
FILE_TOOLS = ("Read", "Edit", "Write")
EXIT_BLOCKED = 2
RUN_FOLDER = re.compile(r"^.*/\.battle/runs/[^/]+(?:/|$)")
PROMPT_FILE = re.compile(r"^.*/\.battle/runs/[^/]+/jobs/\d+\.prompt\.md$")
WORKSPACE = re.compile(r"^(?P<run>.*/\.battle/runs/[^/]+)/workspace/(?P<fighter>[^/]+)(?:/|$)")


def _claim(claims: Path, agent: str, fighter: str) -> str:
    claims.mkdir(exist_ok=True)
    path = claims / re.sub(r"[^\w-]", "_", agent)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(fighter)
    except FileExistsError:
        return path.read_text(encoding="utf-8")
    return fighter


def _escapes(pattern: str | None) -> bool:
    return bool(pattern) and (pattern.startswith(("/", "~")) or ".." in pattern)


def check(payload: dict[str, Any], role: str) -> str | None:
    tool = payload["tool_name"]
    tool_input = payload["tool_input"]
    target = tool_input.get("file_path" if tool in FILE_TOOLS else "path")
    if not target:
        return "Pass an absolute path inside your folder."
    if _escapes(tool_input.get("glob")) or (tool == "Glob" and _escapes(tool_input.get("pattern"))):
        return "Search patterns must stay inside your folder."

    resolved = os.path.realpath(os.path.join(payload["cwd"], target))
    if tool == "Read" and PROMPT_FILE.match(resolved):
        return None
    if role == "judge":
        return None if RUN_FOLDER.match(resolved) else f"{resolved} is outside this battle's folder."

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
