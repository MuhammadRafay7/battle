from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from battle.agents import AgentError, Job, UsageLimitReached  # noqa: E402
from battle.fight import CATEGORIES  # noqa: E402
from battle.workspace import RIVALS_DIR  # noqa: E402


def _ids(pattern: str, prompt: str) -> list[str]:
    return re.search(pattern, prompt).group(1).split(", ")


@dataclass
class FakeAgent:
    name: str = "fake"
    fail: tuple[str, ...] = ()
    limit_after: int | None = None
    already_ran: int = 0
    no_attacks: bool = False
    identical: bool = False
    bad_json_once: tuple[str, ...] = ()
    invalid_winner: bool = False
    scores_stranger: bool = False
    overturn: dict[str, str] = field(default_factory=dict)
    runner_up: str | None = None
    champion: str | None = None
    fixes: tuple[str, ...] = ("app.py: check the input",)
    jobs: list[Job] = field(default_factory=list)
    seen_rivals: dict[str, list[list[str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._guard = threading.Lock()
        self._failed_once: set[str] = set()

    def run(self, job: Job) -> str:
        with self._guard:
            self.jobs.append(job)
            count = self.already_ran + len(self.jobs)
        if self.limit_after is not None and count > self.limit_after:
            raise UsageLimitReached("Claude usage limit reached.")
        if job.role in self.bad_json_once and job.role not in self._failed_once:
            self._failed_once.add(job.role)
            return "this is not json"
        return getattr(self, "_" + job.role.replace("-", "_"))(job)

    def _director(self, job: Job) -> str:
        count = int(re.search(r"exactly (\d+)", job.prompt).group(1))
        return json.dumps(
            {"directions": [{"name": f"Plan {i}", "brief": f"Try {i}"} for i in range(count)]}
        )

    def _fighter(self, job: Job) -> str:
        fighter = re.search(r"You are fighter (\w+)", job.prompt).group(1)
        if fighter in self.fail:
            raise AgentError("fake agent crashed")
        rivals_dir = job.cwd / RIVALS_DIR
        rivals = sorted(path.name for path in rivals_dir.iterdir()) if rivals_dir.exists() else []
        with self._guard:
            self.seen_rivals.setdefault(fighter, []).append(rivals)
        name = "shared.txt" if self.identical else f"work_{fighter}.txt"
        line = "same line\n" if self.identical else f"work by {fighter}\n"
        with (job.cwd / name).open("a") as handle:
            handle.write(line)
        attacks = []
        if rivals and not self.no_attacks:
            attacks = [
                {
                    "target": rivals[0],
                    "category": "Security",
                    "file": "app.py",
                    "problem": f"{fighter} found a hole",
                    "evidence": "app.py line 1 trusts its input",
                },
                {
                    "target": rivals[0],
                    "category": "Design",
                    "file": "app.py",
                    "problem": "vague complaint",
                    "evidence": "",
                },
            ]
        return json.dumps({"summary": f"{fighter} worked", "attacks": attacks})

    def _group_judge(self, job: Job) -> str:
        fighters = _ids(r"Fighters: (.+)", job.prompt)
        winner = "Z9" if self.invalid_winner else fighters[-1]
        scored = [*fighters, "Z9"] if self.scores_stranger else fighters
        scores = {fighter: {category: 7 for category in CATEGORIES} for fighter in scored}
        return json.dumps(
            {"winner": winner, "scores": scores, "reason": "best evidence", "fixes": list(self.fixes)}
        )

    def _head_check(self, job: Job) -> str:
        picked = re.findall(r"Group (\w+): judge picked (\w+)", job.prompt)
        rulings = {
            group: {"winner": self.overturn.get(group, winner), "reason": "checked"}
            for group, winner in picked
        }
        return json.dumps({"rulings": rulings, "runner_up": self.runner_up})

    def _specialist(self, job: Job) -> str:
        finalists = _ids(r"Finalists: (.+)", job.prompt)
        return json.dumps(
            {
                "scores": {fighter: 7 for fighter in finalists},
                "notes": {fighter: "solid" for fighter in finalists},
            }
        )

    def _head_champion(self, job: Job) -> str:
        finalists = _ids(r"Finalists: (.+)", job.prompt)
        return json.dumps(
            {
                "champion": self.champion or finalists[0],
                "reason": "top score",
                "fixes": list(self.fixes),
            }
        )


def main() -> int:
    config = json.loads(os.environ.get("FAKE_CONFIG", "{}"))
    log = Path(config.pop("log"))
    ran = len(log.read_text().splitlines()) if log.exists() else 0
    with log.open("a") as handle:
        handle.write(os.environ["BATTLE_ROLE"] + "\n")
    agent = FakeAgent(**config, already_ran=ran)
    job = Job(os.environ["BATTLE_ROLE"], sys.stdin.read(), Path.cwd(), writes=True)
    try:
        print(agent.run(job))
    except AgentError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
