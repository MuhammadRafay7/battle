from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from battle.agents import (
    DEFAULT_TIMEOUT,
    Agent,
    AgentError,
    AgentTimedOut,
    Job,
    UsageLimitReached,
    extract_json,
)
from battle.engine import ATTEMPTS, BattleError, Engine, Stopped
from battle.store import RunStore
from battle.team import checks, prompts, report
from battle.team.prompts import (
    DESIGN,
    DESIGN_CRITERIA,
    DEV_CRITERIA,
    DOCS,
    MANIFEST,
    REQUIREMENTS,
    RESEARCH,
    RUN_NOTES,
    SEVERITIES,
    UI_MODES,
)
from battle.workspace import (
    ApplyConflict,
    ApplyRefused,
    apply_entry,
    capture,
    materialize,
    undo_apply,
)

STAGES = ("pm", "research", "design", "dev", "security")
SKIPPABLE = ("research", "design", "security")
FIGHTS = ("design", "dev")
SIDES = ("a", "b")
ANGLES = {
    "auth": "authentication and sessions: login, logout, password reset, session fixation, "
    "token handling",
    "injection": "injection: SQL, NoSQL, command and template injection in every input and query",
    "xss": "XSS and CSRF: reflected, stored and DOM XSS, unsafe HTML rendering, missing CSRF "
    "protection",
    "secrets": "secrets and configuration: leaked keys, debug modes, verbose errors, insecure "
    "headers and cookies",
    "deps": "dependencies: vulnerable or abandoned packages, lockfile integrity, supply-chain risks",
    "access": "access control and API abuse: IDOR, missing authorization, rate limits, mass "
    "assignment, open redirects",
}
DESIGN_KITS = {
    "a": ("frontend-design", "ui-ux-pro-max", "apple-design"),
    "b": ("impeccable", "design-taste-frontend", "3d-web-experience"),
}
DEV_KITS = {
    "a": ("frontend-design", "responsive-craft", "tdd"),
    "b": ("impeccable", "3d-web-experience", "ui-styling"),
}
DEFAULT_STACKS = [
    {
        "name": "Next.js + Tailwind CSS + React Three Fiber",
        "brief": "App Router, Tailwind for styling, React Three Fiber and Framer Motion for 3D "
        "and motion.",
    },
    {
        "name": "Astro + plain CSS + Three.js",
        "brief": "Astro islands, hand-written CSS with custom properties, Three.js and the Web "
        "Animations API.",
    },
]
SUBAGENTS = {
    "pm": "team-pm",
    "researcher": "team-researcher",
    "design-a": "team-designer-a",
    "design-b": "team-designer-b",
    "dev-a": "team-dev-a",
    "dev-b": "team-dev-b",
    "judge": "team-judge",
    "attacker": "team-attacker",
    "fixer": "team-fixer",
}
WRITER_ROLES = {"design": "designer", "dev": "dev"}
MAX_FIXES = 5
MAX_QUESTIONS = 3
MAX_SCORE = 10
DEFAULT_SECURITY_ROUNDS = 3
DEFAULT_CHECK_TIMEOUT = 600.0
LISTING_LIMIT = 40
HANDOFF = {"design": (DESIGN,), "dev": (MANIFEST, RUN_NOTES)}


def estimate_runs(settings: dict[str, Any]) -> dict[str, int]:
    skip = set(settings.get("skip", ()))
    solo = set(settings.get("solo", ()))
    angles = len(settings.get("angles") or ANGLES)
    rounds = settings.get("security_rounds", DEFAULT_SECURITY_ROUNDS)
    counts = {"pm": 1}
    if "research" not in skip:
        counts["research"] = 1
    for stage in FIGHTS:
        if stage not in skip:
            counts[stage] = 1 if stage in solo else 4
    if "security" not in skip:
        counts["security"] = (angles + 2) * rounds - 1
    return counts


def _score_ok(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 <= value <= MAX_SCORE


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class Pipeline(Engine):
    def __init__(
        self,
        store: RunStore,
        agents: list[Agent],
        limit: int,
        log: Callable[[str], None] = print,
    ) -> None:
        super().__init__(store, agents, limit, log)
        settings = store.settings
        self.timeout = settings.get("timeout", DEFAULT_TIMEOUT)
        self.check_timeout = settings.get("check_timeout", DEFAULT_CHECK_TIMEOUT)
        self.skip = set(settings.get("skip", ()))
        self.solo = set(settings.get("solo", ()))
        self.angles = list(settings.get("angles") or ANGLES)
        self.rounds = max(1, settings.get("security_rounds", DEFAULT_SECURITY_ROUNDS))
        self.keep = settings.get("keep", False)
        self._preamble = prompts.preamble(settings.get("web", True))
        self.docs = store.root / "docs"
        self.task = store.task

    def run(self) -> dict[str, Any]:
        self._say(f"Team {self.store.id}: up to {self.limit} agents at once.")
        self._prepare_baseline()
        self.docs.mkdir(exist_ok=True)
        brief = self._pm()
        research = self._research(brief)
        if "design" not in self.skip:
            self._fight("design", brief, research)
        entry = self._fight("dev", brief, research)
        entry = self._security(entry)
        return self._finish(entry)

    def _agent_for(self, index: int) -> Agent:
        return self.agents[index % len(self.agents)]

    def _entry(self, key: str) -> Path:
        return self.store.root / "entries" / key

    def _workspace(self, name: str) -> Path:
        return self.store.root / "workspace" / name

    def _fresh(self, name: str, entry: Path | None) -> Path:
        workspace = self._workspace(name)
        materialize(self.baseline, entry, workspace)
        self._copy_docs(workspace)
        return workspace

    def _copy_docs(self, workspace: Path) -> None:
        target = workspace / DOCS
        shutil.rmtree(target, ignore_errors=True)
        if self.docs.exists():
            shutil.copytree(self.docs, target)
        else:
            target.mkdir()

    def _ensure(self, name: str, entry: Path | None) -> Path:
        workspace = self._workspace(name)
        if not workspace.exists():
            self._fresh(name, entry)
        return workspace

    def _clean(self, names: list[str]) -> None:
        if self.keep:
            return
        for name in names:
            shutil.rmtree(self._workspace(name), ignore_errors=True)

    def _harvest(self, workspace: Path, relative: str) -> bool:
        source = workspace / relative
        target = self.docs / Path(relative).relative_to(DOCS)
        if source.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            return False
        return True

    def _turn(
        self,
        key: str,
        name: str,
        role: str,
        prompt: str,
        entry: Path | None,
        label: str,
        agent: Agent,
        bash: bool = False,
        fresh: bool = True,
        research_ok: bool = True,
    ) -> dict[str, Any] | None:
        if self.store.has(key):
            result = self.store.get(key)
            return None if result.get("forfeit") else result
        job = Job(
            role,
            self._preamble + prompt,
            self._workspace(name),
            writes=True,
            timeout=self.timeout,
            label=label,
            subagent=SUBAGENTS.get(name, SUBAGENTS.get(role, "")),
            bash=bash,
        )
        reply = self._attempt(agent, job, name, entry, fresh)
        if reply is None:
            self.store.put(key, {"forfeit": True, "reason": "It failed twice."})
            return None
        body = self._body(reply)
        questions = _texts(body.get("research"))[:MAX_QUESTIONS]
        if questions and research_ok and "research" not in self.skip:
            answered = self._answer(key, questions, label)
            if answered is not None:
                more = replace(job, prompt=job.prompt + prompts.followup(answered))
                again = self._attempt(agent, more, name, entry, fresh=False)
                if again is not None:
                    reply, body = again, self._body(again)
        changes = capture(self.baseline, job.cwd, self._entry(key))
        result = {
            "summary": str(body.get("summary") or reply[:500]),
            "body": {field: value for field, value in body.items() if field != "summary"},
            "changed": changes["changed"],
            "deleted": changes["deleted"],
            "agent": agent.name,
        }
        self._say(f"{label} finished: {len(changes['changed'])} files changed.")
        self.store.put(key, result)
        return result

    def _attempt(
        self, agent: Agent, job: Job, name: str, entry: Path | None, fresh: bool
    ) -> str | None:
        for attempt in range(1, ATTEMPTS + 1):
            if fresh or not job.cwd.exists():
                self._fresh(name, entry)
            try:
                return self._run_agent(agent, job, job.label)
            except UsageLimitReached:
                raise
            except AgentTimedOut as error:
                self._say(f"{job.label} failed: {error}")
                return None
            except AgentError as error:
                self._say(f"{job.label} failed on attempt {attempt}: {error}")
        return None

    @staticmethod
    def _body(reply: str) -> dict[str, Any]:
        try:
            return extract_json(reply)
        except ValueError:
            return {}

    def _answer(self, key: str, questions: list[str], context: str) -> dict[str, Any] | None:
        workspace = self._workspace("answers")
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True)
        self._copy_docs(workspace)
        job = Job(
            "researcher",
            self._preamble + prompts.answers(questions, context),
            workspace,
            writes=False,
            timeout=self.timeout,
            label=f"{context}: research questions",
            subagent=SUBAGENTS["researcher"],
        )

        def check(value: dict[str, Any]) -> None:
            if not isinstance(value.get("answers"), list):
                raise ValueError("answers must be a list.")

        self._say(f"{context} asked {len(questions)} research questions.")
        try:
            return self._ask_json(f"{key}/research", job, check)
        except BattleError as error:
            self._say(f"Research questions failed: {error}")
            return None

    def _listing(self) -> str:
        names = sorted(
            path.name + ("/" if path.is_dir() else "") for path in self.baseline.iterdir()
        )
        if not names:
            return "(empty folder)"
        shown = names[:LISTING_LIMIT]
        if len(names) > LISTING_LIMIT:
            shown.append(f"... and {len(names) - LISTING_LIMIT} more")
        return "\n".join(shown)

    def _pm(self) -> dict[str, Any]:
        if self.store.has("brief"):
            return self.store.get("brief")
        self._say("PM: writing the requirements.")
        result = self._turn(
            "pm", "pm", "pm", prompts.pm(self.task, self._listing()), None, "PM", self._agent_for(0)
        )
        if result is None:
            self._give_up("pm", "The PM failed, so there are no requirements.")
        workspace = self._ensure("pm", self._entry("pm"))
        if not self._harvest(workspace, REQUIREMENTS):
            self.store.forget(["pm"])
            raise BattleError(f"The PM did not write {REQUIREMENTS}.")
        body = result["body"]
        acceptance = _texts(body.get("acceptance_criteria"))
        if not acceptance:
            self.store.forget(["pm"])
            raise BattleError("The PM listed no acceptance criteria.")
        stack = body.get("existing_stack")
        brief = {
            "summary": result["summary"],
            "ui_mode": body.get("ui_mode") if body.get("ui_mode") in UI_MODES else "new",
            "existing_stack": stack.strip() if isinstance(stack, str) and stack.strip() else None,
            "acceptance_criteria": acceptance,
        }
        self.store.put("brief", brief)
        self._clean(["pm"])
        self._say(f"PM: {len(acceptance)} acceptance criteria, UI mode {brief['ui_mode']}.")
        return brief

    @staticmethod
    def _default_stacks(brief: dict[str, Any]) -> list[dict[str, str]]:
        existing = brief.get("existing_stack")
        if not existing:
            return DEFAULT_STACKS
        return [
            {"name": existing, "brief": "The project's own stack. Differ in libraries and approach."},
            {"name": existing, "brief": "The project's own stack. Take a different approach from A."},
        ]

    def _research(self, brief: dict[str, Any]) -> dict[str, Any]:
        fallback = {"stacks": self._default_stacks(brief), "summary": ""}
        if "research" in self.skip:
            return fallback
        self._say("Research: studying the niche, competitors, trends and stacks.")
        result = self._turn(
            "research",
            "research",
            "researcher",
            prompts.researcher(self.task),
            None,
            "Research",
            self._agent_for(0),
            research_ok=False,
        )
        if result is None:
            self._say("Research failed, so the team continues without it.")
            return fallback
        self._harvest(self._ensure("research", self._entry("research")), RESEARCH)
        self._clean(["research"])
        stacks = result["body"].get("stacks")
        valid = (
            isinstance(stacks, list)
            and len(stacks) >= 2
            and all(
                isinstance(stack, dict)
                and isinstance(stack.get("name"), str)
                and isinstance(stack.get("brief"), str)
                for stack in stacks[:2]
            )
        )
        if not valid:
            return {"stacks": fallback["stacks"], "summary": result["summary"]}
        chosen = [{"name": stack["name"], "brief": stack["brief"]} for stack in stacks[:2]]
        return {"stacks": chosen, "summary": result["summary"]}

    def _fight(self, stage: str, brief: dict[str, Any], research: dict[str, Any]) -> Path:
        sides = ["a"] if stage in self.solo else list(SIDES)
        role = WRITER_ROLES[stage]
        plural = "s" if len(sides) > 1 else ""
        self._say(f"{stage.capitalize()}: {len(sides)} entrant{plural}.")

        def prompt_for(side: str) -> str:
            if stage == "design":
                return prompts.designer(side, DESIGN_KITS[side], brief, self.task)
            stacks = research["stacks"]
            stack = stacks[SIDES.index(side) % len(stacks)]
            return prompts.dev(side, DEV_KITS[side], stack, brief, self.task)

        def turn(side: str) -> dict[str, Any] | None:
            return self._turn(
                f"{stage}/{side}",
                f"{stage}-{side}",
                role,
                prompt_for(side),
                None,
                f"{role.capitalize()} {side.upper()}",
                self._agent_for(SIDES.index(side)),
                bash=stage == "dev",
            )

        turns = self._each(sides, turn)
        alive = [side for side, result in zip(sides, turns) if result is not None]
        if not alive:
            self._give_up(f"{stage}/", f"No {stage} entrant finished.")
        for side in alive:
            self._ensure(f"{stage}-{side}", self._entry(f"{stage}/{side}"))
        reports = self._checks(stage, alive)
        verdict = self._verdict(stage, alive, brief, reports)
        winner = verdict["winner"]
        self._say(f"{stage.capitalize()}: {winner.upper()} wins. {verdict.get('reason', '')}".strip())
        entry = self._polish(stage, winner, verdict)
        workspace = self._workspace(f"{stage}-{winner}")
        for relative in HANDOFF[stage]:
            if not self._harvest(workspace, relative):
                self._say(f"{winner.upper()} left no {relative}; later stages work without it.")
        self._clean([f"{stage}-{side}" for side in sides])
        return entry

    def _checks(self, stage: str, sides: list[str]) -> dict[str, checks.CheckReport]:
        reports = {}
        for side in sides:
            workspace = self._workspace(f"{stage}-{side}")
            if stage == "design":
                reports[side] = checks.check_design(workspace, DESIGN)
            else:
                self._say(f"Checks: building {side.upper()}'s entry.")
                reports[side] = checks.check_build(workspace, MANIFEST, self.check_timeout)
        self.store.put(f"{stage}/checks", {side: r.as_dict() for side, r in reports.items()})
        return reports

    def _verdict(
        self,
        stage: str,
        sides: list[str],
        brief: dict[str, Any],
        reports: dict[str, checks.CheckReport],
    ) -> dict[str, Any]:
        key = f"{stage}/judge"
        if len(sides) == 1:
            if not self.store.has(key):
                self.store.put(
                    key,
                    {
                        "winner": sides[0],
                        "scores": {},
                        "acceptance": {},
                        "reason": "Only entrant.",
                        "fixes": [],
                    },
                )
            return self.store.get(key)
        criteria = DESIGN_CRITERIA if stage == "design" else DEV_CRITERIA

        def check(value: dict[str, Any]) -> None:
            if value["winner"] not in sides:
                raise ValueError(f"winner must be one of {', '.join(sides)}.")
            if set(value["scores"]) != set(sides):
                raise ValueError(f"Score exactly these entrants: {', '.join(sides)}.")
            for side in sides:
                for criterion in criteria:
                    if not _score_ok(value["scores"][side][criterion]):
                        raise ValueError(
                            f"{side} {criterion} must be a number from 0 to {MAX_SCORE}."
                        )
            if not isinstance(value.get("acceptance"), dict):
                raise ValueError("acceptance must be an object keyed by entrant.")
            fixes = value.get("fixes", [])
            if not isinstance(fixes, list) or not all(isinstance(fix, str) for fix in fixes):
                raise ValueError("fixes must be a list of strings.")

        folders = {side: str(self._workspace(f"{stage}-{side}")) for side in sides}
        rendered = {side: reports[side].render() for side in sides}
        prompt = prompts.judge(
            stage, sides, brief["acceptance_criteria"], rendered, folders, self.task
        )
        job = Job(
            "judge",
            self._preamble + prompt,
            self.store.root / "workspace",
            writes=False,
            read_dirs=(self.baseline,),
            timeout=self.timeout,
            label=f"{stage.capitalize()} judge",
            subagent=SUBAGENTS["judge"],
            bash=stage == "dev",
        )
        return self._ask_json(key, job, check)

    def _polish(self, stage: str, winner: str, verdict: dict[str, Any]) -> Path:
        entry = self._entry(f"{stage}/{winner}")
        fixes = _texts(verdict.get("fixes"))[:MAX_FIXES]
        if not fixes:
            return entry
        key = f"{stage}/polish"
        if not self.store.has(key):
            noun = "fix" if len(fixes) == 1 else "fixes"
            self._say(f"Polish: {winner.upper()} is making the judge's {len(fixes)} {noun}.")
        role = WRITER_ROLES[stage]
        result = self._turn(
            key,
            f"{stage}-{winner}",
            role,
            prompts.polish(f"{role} {winner.upper()}", fixes),
            entry,
            f"{role.capitalize()} {winner.upper()} (polish)",
            self._agent_for(SIDES.index(winner)),
            bash=stage == "dev",
            fresh=False,
            research_ok=False,
        )
        if result is None:
            self._say(f"Polish failed, so {winner.upper()}'s entry is kept as it was.")
            self._ensure(f"{stage}-{winner}", entry)
            return entry
        self._ensure(f"{stage}-{winner}", self._entry(key))
        return self._entry(key)

    def _security(self, entry: Path) -> Path:
        if "security" in self.skip:
            return entry
        if self.store.has("security"):
            return self._entry(self.store.get("security")["entry"])
        current = self._entry_key(entry)
        retest: dict[str, list[dict[str, Any]]] = {}
        open_findings: list[dict[str, Any]] = []
        number = 0
        for number in range(1, self.rounds + 1):
            key = f"security/r{number}"
            self._say(f"Security round {number} of {self.rounds}: {len(self.angles)} attackers.")
            if self.store.has(key):
                confirmed = self.store.get(key)["confirmed"]
            else:
                confirmed = self._attack_round(number, entry, retest)
            if not confirmed:
                self._say("Security: no confirmed findings remain.")
                open_findings = []
                break
            open_findings = confirmed
            if number == self.rounds:
                self._say(f"Security: round limit reached with {len(confirmed)} open findings.")
                break
            fix = self._turn(
                f"{key}/fix",
                "fixer",
                "fixer",
                prompts.fixer(confirmed, self.task),
                entry,
                f"Fixer round {number}",
                self._agent_for(0),
                bash=True,
            )
            if fix is None:
                self._say("Security: the fixer failed, so the findings stay open.")
                break
            current = f"{key}/fix"
            entry = self._entry(current)
            retest = {}
            for finding in confirmed:
                retest.setdefault(finding["angle"], []).append(finding)
        self.store.put("security", {"rounds": number, "open": open_findings, "entry": current})
        self._clean(["site", "fixer", *(f"attack-{angle}" for angle in self.angles)])
        return entry

    def _entry_key(self, entry: Path) -> str:
        return entry.relative_to(self.store.root / "entries").as_posix()

    def _attack_round(
        self, number: int, entry: Path, retest: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        site = self._fresh("site", entry)
        build = checks.check_build(site, MANIFEST, self.check_timeout)
        manifest = checks.read_manifest(site, MANIFEST) if build.manifest_error is None else {}
        if not build.built:
            self._say("Security: the site did not build, so attackers test the code statically.")
        log = self.store.root / "server.log"
        with checks.serve(site, manifest if build.built else {}, log) as url:
            if build.built and manifest.get("start") and url is None:
                self._say("Security: the site did not start listening, so attackers test it statically.")

            def attack(angle: str) -> list[dict[str, Any]]:
                result = self._turn(
                    f"security/r{number}/attack/{angle}",
                    f"attack-{angle}",
                    "attacker",
                    prompts.attacker(angle, ANGLES[angle], url, retest.get(angle, []), self.task),
                    entry,
                    f"Attacker {angle} (round {number})",
                    self._agent_for(self.angles.index(angle)),
                    bash=True,
                    research_ok=False,
                )
                return [] if result is None else self._findings(angle, result["body"])

            groups = self._each(self.angles, attack)
            findings = [finding for group in groups for finding in group]
            verdict = self._verify(number, findings, url, site)
        by_id = {finding["id"]: finding for finding in findings}
        confirmed = []
        for item in verdict["confirmed"]:
            finding = dict(by_id[item["id"]])
            if item.get("severity") in SEVERITIES:
                finding["severity"] = item["severity"]
            finding["note"] = str(item.get("note", ""))
            confirmed.append(finding)
        self.store.put(
            f"security/r{number}",
            {"reported": len(findings), "confirmed": confirmed, "served": url is not None},
        )
        self._say(f"Security round {number}: {len(findings)} reported, {len(confirmed)} confirmed.")
        return confirmed

    @staticmethod
    def _findings(angle: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        found = []
        seen: set[str] = set()
        for item in body.get("findings") or []:
            if not isinstance(item, dict):
                continue
            texts = {field: str(item.get(field) or "").strip() for field in ("id", "title", "repro")}
            if not all(texts.values()):
                continue
            prefix = f"{angle}-"
            identifier = texts["id"] if texts["id"].startswith(prefix) else prefix + texts["id"]
            if identifier in seen:
                continue
            seen.add(identifier)
            severity = item.get("severity")
            found.append(
                {
                    "id": identifier,
                    "angle": angle,
                    "severity": severity if severity in SEVERITIES else "medium",
                    "title": texts["title"],
                    "repro": texts["repro"],
                    "evidence": str(item.get("evidence") or ""),
                    "files": _texts(item.get("files")),
                    "fix": str(item.get("fix") or ""),
                }
            )
        return found

    def _verify(
        self, number: int, findings: list[dict[str, Any]], url: str | None, site: Path
    ) -> dict[str, Any]:
        key = f"security/r{number}/verify"
        if not findings:
            if not self.store.has(key):
                self.store.put(key, {"confirmed": [], "rejected": []})
            return self.store.get(key)
        ids = sorted(finding["id"] for finding in findings)

        def check(value: dict[str, Any]) -> None:
            confirmed, rejected = value["confirmed"], value["rejected"]
            if not isinstance(confirmed, list) or not isinstance(rejected, list):
                raise ValueError("confirmed and rejected must be lists.")
            listed = sorted(str(item["id"]) for item in [*confirmed, *rejected])
            if listed != ids:
                raise ValueError(f"List every id exactly once: {', '.join(ids)}.")

        self._say(f"Security round {number}: verifying {len(findings)} findings.")
        job = Job(
            "verifier",
            self._preamble + prompts.verifier(findings, url, str(site), self.task),
            self.store.root / "workspace",
            writes=False,
            read_dirs=(self.baseline,),
            timeout=self.timeout,
            label=f"Verifier (round {number})",
            subagent=SUBAGENTS["judge"],
            bash=True,
        )
        return self._ask_json(key, job, check)

    def _finish(self, entry: Path) -> dict[str, Any]:
        if self._stop.is_set():
            raise Stopped
        root = self.store.root
        if not (root / "champion").exists():
            partial = root / "champion.partial"
            shutil.rmtree(partial, ignore_errors=True)
            shutil.copytree(entry, partial)
            partial.rename(root / "champion")
        if not self.store.has("result"):
            if self.store.settings.get("apply", True):
                apply_site(self.store)
            else:
                self.store.put("result", {"applied": False, "reason": "Run with --no-apply."})
        report.write(self.store)
        self.store.set_status("done")
        if not self.keep:
            for folder in ("workspace", "entries"):
                shutil.rmtree(root / folder, ignore_errors=True)
        return {"champion": "site", **self.store.get("result")}


def roll_back_cut_off_apply(store: RunStore) -> bool:
    backup = store.root / "backup"
    if not backup.exists() or store.steps.get("result", {}).get("applied"):
        return False
    undo_apply(backup, store.project)
    return True


def apply_site(store: RunStore, force: bool = False) -> dict[str, Any]:
    root = store.root
    try:
        roll_back_cut_off_apply(store)
        changes = apply_entry(
            root / "champion", root / "baseline", store.project, root / "backup", force
        )
    except ApplyConflict as conflict:
        outcome: dict[str, Any] = {"applied": False, "conflicts": conflict.paths}
    except ApplyRefused as refusal:
        outcome = {"applied": False, "refused": str(refusal)}
    else:
        outcome = {"applied": True, **changes}
    store.put("result", outcome)
    report.write(store)
    return outcome


def undo_site(store: RunStore) -> None:
    if (store.root / "backup").exists():
        undo_apply(store.root / "backup", store.project)
    store.put("result", {"applied": False, "reason": "Undone."})
    report.write(store)
