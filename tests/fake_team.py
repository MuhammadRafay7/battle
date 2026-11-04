from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from battle.agents import AgentError, Job, UsageLimitReached
from battle.team.prompts import (
    DESIGN,
    DESIGN_CRITERIA,
    DEV_CRITERIA,
    MANIFEST,
    REQUIREMENTS,
    RESEARCH,
    RUN_NOTES,
)

STATIC_SERVER = 'python3 -m http.server "$PORT" --bind 127.0.0.1'


def _entrants(prompt: str) -> list[str]:
    listed = re.search(r"Entrants: (.+)", prompt).group(1)
    return [side.lower() for side in listed.split(", ")]


def _ids(text: str) -> list[str]:
    return re.findall(r'"id": "([\w-]+)"', text)


@dataclass
class FakeTeamAgent:
    name: str = "fake"
    design_winner: str | None = None
    dev_winner: str | None = None
    fixes: dict[str, list[str]] = field(default_factory=dict)
    gradient_sides: tuple[str, ...] = ()
    broken_build_sides: tuple[str, ...] = ()
    serve: bool = False
    vulnerable_rounds: int = 1
    fixer_fails: bool = False
    fail_roles: tuple[str, ...] = ()
    bad_json_once: tuple[str, ...] = ()
    invalid_winner: bool = False
    ask_research: tuple[str, ...] = ()
    limit_after: int | None = None
    already_ran: int = 0
    jobs: list[Job] = field(default_factory=list)
    designs_seen: dict[str, str] = field(default_factory=dict)
    attacker_rounds: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._guard = threading.Lock()
        self._failed_once: set[str] = set()

    def run(self, job: Job) -> str:
        with self._guard:
            self.jobs.append(job)
            count = self.already_ran + len(self.jobs)
        if self.limit_after is not None and count > self.limit_after:
            raise UsageLimitReached("Claude usage limit reached.")
        if job.role in self.fail_roles:
            raise AgentError("fake agent crashed")
        if job.role in self.bad_json_once and job.role not in self._failed_once:
            self._failed_once.add(job.role)
            return "this is not json"
        if "won its stage" in job.prompt:
            return self._polish(job)
        return getattr(self, "_" + job.role)(job)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _asks(self, job: Job) -> list[str]:
        if job.role in self.ask_research and "questions were answered" not in job.prompt:
            return ["Which payment provider works in this region?"]
        return []

    def _pm(self, job: Job) -> str:
        self._write(job.cwd / REQUIREMENTS, "# Requirements\n\nAC-1: has a home page\n")
        return json.dumps(
            {
                "summary": "A small site.",
                "ui_mode": "new",
                "existing_stack": None,
                "acceptance_criteria": ["AC-1: has a home page", "AC-2: no gradients"],
                "research": self._asks(job),
            }
        )

    def _researcher(self, job: Job) -> str:
        if "needs these answered" in job.prompt:
            answer = {"question": "payments", "answer": "Use Stripe.", "sources": ["https://stripe.com"]}
            return json.dumps({"answers": [answer]})
        self._write(job.cwd / RESEARCH, "# Research\n\nNiche notes.\n")
        return json.dumps(
            {
                "summary": "Researched.",
                "stacks": [
                    {"name": "Stack One", "brief": "first"},
                    {"name": "Stack Two", "brief": "second"},
                ],
            }
        )

    def _designer(self, job: Job) -> str:
        side = re.search(r"You are designer (\w)", job.prompt).group(1).lower()
        folder = job.cwd / DESIGN
        self._write(folder / "design.md", f"design by {side}\n")
        gradient = side in self.gradient_sides
        colour = "background: linear-gradient(red, blue);" if gradient else "background: red;"
        self._write(folder / "tokens.css", f":root {{ --bg: red; }}\n.hero {{ {colour} }}\n")
        self._write(folder / "prototype" / "index.html", f"<html><body>{side}</body></html>\n")
        return json.dumps({"summary": f"design {side}", "research": self._asks(job)})

    def _dev(self, job: Job) -> str:
        side = re.search(r"You are senior developer (\w)", job.prompt).group(1).lower()
        stack = re.search(r"Your stack: (.+?)\.", job.prompt).group(1)
        design = job.cwd / DESIGN / "design.md"
        with self._guard:
            self.designs_seen[side] = design.read_text() if design.exists() else ""
        page = f"<html><body>built by {side} on {stack}</body></html>\n"
        self._write(job.cwd / "site" / "index.html", page)
        gradient = side in self.gradient_sides
        style = "background: linear-gradient(red, blue);" if gradient else "background: red;"
        self._write(job.cwd / "site" / "style.css", f".hero {{ {style} }}\n")
        manifest = {
            "install": None,
            "build": "false" if side in self.broken_build_sides else "true",
            "test": None,
            "start": f"cd site && {STATIC_SERVER}" if self.serve else None,
            "url": "http://127.0.0.1:{port}/index.html",
        }
        self._write(job.cwd / MANIFEST, json.dumps(manifest))
        self._write(job.cwd / RUN_NOTES, "Open site/index.html\n")
        return json.dumps({"summary": f"built {side}", "research": self._asks(job)})

    def _judge(self, job: Job) -> str:
        sides = _entrants(job.prompt)
        design = "judge of the design stage" in job.prompt
        stage = "design" if design else "dev"
        criteria = DESIGN_CRITERIA if design else DEV_CRITERIA
        chosen = self.design_winner if design else self.dev_winner
        winner = "z" if self.invalid_winner else (chosen or sides[-1])
        return json.dumps(
            {
                "winner": winner,
                "scores": {side: {criterion: 7 for criterion in criteria} for side in sides},
                "acceptance": {side: {"AC-1": "met", "AC-2": "met"} for side in sides},
                "reason": "best evidence",
                "fixes": self.fixes.get(stage, []),
            }
        )

    def _polish(self, job: Job) -> str:
        target = job.cwd / "site" / "index.html"
        if target.exists():
            target.write_text(target.read_text() + "<!-- polished -->\n")
        else:
            design = job.cwd / DESIGN / "design.md"
            design.write_text(design.read_text() + "polished\n")
        return json.dumps({"summary": "polished", "research": []})

    def _attacker(self, job: Job) -> str:
        angle = re.search(r'"id": "(\w+)-1"', job.prompt).group(1)
        with self._guard:
            self.attacker_rounds[angle] = self.attacker_rounds.get(angle, 0) + 1
            round_number = self.attacker_rounds[angle]
        findings = []
        if angle == "auth" and round_number <= self.vulnerable_rounds:
            findings.append(
                {
                    "id": "1",
                    "severity": "high",
                    "title": "Session never expires",
                    "repro": "Log in, wait, still logged in",
                    "evidence": "cookie has no expiry",
                    "files": ["site/index.html"],
                    "fix": "set an expiry",
                }
            )
        return json.dumps({"summary": f"{angle} probed", "findings": findings})

    def _verifier(self, job: Job) -> str:
        listed = job.prompt.split("Findings:", 1)[1].split("For each finding", 1)[0]
        confirmed = [{"id": identifier, "severity": "high", "note": "reproduced"} for identifier in _ids(listed)]
        return json.dumps({"confirmed": confirmed, "rejected": []})

    def _fixer(self, job: Job) -> str:
        if self.fixer_fails:
            raise AgentError("fixer crashed")
        target = job.cwd / "site" / "index.html"
        target.write_text(target.read_text() + "<!-- fixed -->\n")
        return json.dumps({"summary": "fixed", "fixed": _ids(job.prompt), "skipped": [], "research": []})
