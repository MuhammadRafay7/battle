from __future__ import annotations

import json
import shutil
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from battle import prompts, report
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
from battle.prompts import CATEGORIES
from battle.sizing import Bracket
from battle.store import RunStore
from battle.workspace import (
    ApplyConflict,
    ApplyRefused,
    add_rivals,
    apply_entry,
    capture,
    materialize,
    similarity,
    undo_apply,
)

ANGLES = (
    "the simplest thing that fully works",
    "the hardest to attack or break",
    "the cleanest structure to maintain",
    "the best result for the end user",
    "the fastest and lightest",
)
SINGLE_DIRECTION = {
    "name": "Best fit",
    "brief": "Choose whatever approach suits this project best.",
}
INLINE_DIFF_CHARS = 60_000
MAX_FIXES = 5
COPY_LIMIT = 0.9
MAX_SCORE = 10
FINAL = "final"

__all__ = ["ATTEMPTS", "BattleError", "Fight", "Fighter", "Stopped"]


@dataclass(frozen=True)
class Fighter:
    id: str
    group: str
    direction: dict[str, str]
    angle: str
    agent: Agent


@dataclass
class GroupResult:
    group: str
    alive: list[Fighter]
    entries: dict[str, Path]
    verdict: dict[str, Any]


@dataclass
class Standing:
    alive: list[Fighter]
    entries: dict[str, Path]
    attacks: list[dict[str, Any]]


def _require_score(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    if not 0 <= value <= MAX_SCORE:
        raise ValueError(f"{label} must be between 0 and {MAX_SCORE}.")


class Fight(Engine):
    def __init__(
        self,
        store: RunStore,
        agents: list[Agent],
        limit: int,
        log: Callable[[str], None] = print,
    ) -> None:
        super().__init__(store, agents, limit, log)
        settings = store.settings
        self.bracket = Bracket(
            settings["groups"],
            settings["fighters"],
            settings["rounds"],
            settings["final_rounds"],
        )
        self.timeout = settings.get("timeout", DEFAULT_TIMEOUT)
        web_access = prompts.WEB_RESEARCH if settings.get("web") else prompts.NO_WEB
        self._preamble = web_access + prompts.RULEBOOK
        self._roster: dict[str, Fighter] = {}

    def run(self) -> dict[str, Any]:
        self._say(f"Battle {self.store.id}: up to {self.limit} agents at once.")
        self._prepare_baseline()
        directions = self._choose_directions()
        letters = string.ascii_uppercase[: self.bracket.groups]
        groups = list(enumerate(zip(letters, directions)))
        fought = self._each(groups, lambda item: self._group_fight(item[0], *item[1]))
        results = [result for result in fought if result is not None]
        if not results:
            self._give_up("group-", "No fighter finished the group stage.")

        if len(results) == 1:
            champion, entry = self._crown_group_winner(results[0])
        else:
            finalists, entries = self._check_verdicts(results)
            final = self._final(finalists, entries)
            champion = self._pick_champion(final)
            entry = final.entries[champion]
        return self._finish(champion, self._polish(champion, entry))

    def _host_group(self, group: str) -> str:
        return group if self.bracket.groups > 1 else ""

    def _judge_job(self, role: str, prompt: str, cwd: Path, group: str = "") -> Job:
        return Job(
            role,
            self._preamble + prompt,
            cwd,
            writes=False,
            read_dirs=(self.baseline,),
            timeout=self.timeout,
            group=self._host_group(group) if group else "",
        )

    def _choose_directions(self) -> list[dict[str, str]]:
        count = self.bracket.groups
        if count == 1:
            return [SINGLE_DIRECTION]

        def check(value: dict[str, Any]) -> None:
            names = [item["name"] for item in value["directions"]]
            if len(names) != count or len(set(names)) != count or not all(names):
                raise ValueError(f"Give exactly {count} directions with different names.")
            for item in value["directions"]:
                texts = (item["name"], item["brief"])
                if not all(isinstance(text, str) and text.strip() for text in texts):
                    raise ValueError("Every direction needs a text name and brief.")

        self._say("Setup: choosing a different direction for each group.")
        job = Job(
            "director",
            self._preamble + prompts.directions(self.store.task, count),
            self.baseline,
            writes=False,
            timeout=self.timeout,
        )
        return self._ask_json("directions", job, check)["directions"]

    def _group_fight(
        self, index: int, group: str, direction: dict[str, str]
    ) -> GroupResult | None:
        size = self.bracket.fighters
        fighters = [
            Fighter(
                id=f"{group}{number + 1}",
                group=group,
                direction=direction,
                angle=ANGLES[number % len(ANGLES)],
                agent=self.agents[(index * size + number) % len(self.agents)],
            )
            for number in range(size)
        ]
        self._roster.update({fighter.id: fighter for fighter in fighters})
        self._say(f"Group {group}: {direction['name']}")
        standing = self._rounds(f"group-{group}", fighters, {}, self.bracket.rounds, True)
        if not standing.alive:
            self._say(f"Group {group}: every fighter forfeited.")
            self._close_group(group)
            return None
        review = self._review(f"group-{group}", standing)
        verdict = self._group_verdict(group, standing, review)
        self._close_group(group)
        self._say(f"Group {group}: {verdict['winner']} wins.")
        return GroupResult(group, standing.alive, standing.entries, verdict)

    def _group_verdict(self, group: str, standing: Standing, review: Path) -> dict[str, Any]:
        ids = [fighter.id for fighter in standing.alive]
        key = f"group-{group}/judge"
        if len(ids) == 1:
            if not self.store.has(key):
                self.store.put(
                    key, {"winner": ids[0], "scores": {}, "reason": "Last fighter standing."}
                )
            return self.store.get(key)

        def check(value: dict[str, Any]) -> None:
            if value["winner"] not in ids:
                raise ValueError(f"{value['winner']} is not a fighter in group {group}.")
            if set(value["scores"]) != set(ids):
                raise ValueError(f"Score exactly these fighters: {', '.join(ids)}.")
            for fighter in ids:
                for category in CATEGORIES:
                    _require_score(value["scores"][fighter][category], f"{fighter} {category}")
            _require_fixes(value)

        prompt = prompts.group_judge(
            group, self.store.task, ids, str(self.baseline), _inline_diffs(review, ids)
        )
        return self._ask_json(key, self._judge_job("group-judge", prompt, review, group), check)

    def _rounds(
        self,
        scope: str,
        fighters: list[Fighter],
        entries: dict[str, Path],
        rounds: int,
        builds_first: bool,
    ) -> Standing:
        alive = list(fighters)
        entries = dict(entries)
        incoming: dict[str, list[dict[str, Any]]] = {}
        warnings: dict[str, str] = {}
        attacks: list[dict[str, Any]] = []

        for number in range(1, rounds + 1):
            builds = builds_first and number == 1
            self._say(f"{scope}, round {number} of {rounds}: {len(alive)} fighters.")
            turns = self._each(
                alive,
                lambda fighter: self._turn(
                    scope, number, fighter, alive, entries, incoming, warnings, builds
                ),
            )
            results = {fighter.id: turn for fighter, turn in zip(alive, turns)}
            for fighter in alive:
                if results[fighter.id].get("forfeit"):
                    self._say(f"{fighter.id} forfeited: {results[fighter.id]['reason']}")
                else:
                    entries[fighter.id] = self._entry_path(scope, number, fighter.id)
            alive = [fighter for fighter in alive if not results[fighter.id].get("forfeit")]

            attacks = self._valid_attacks(alive, results)
            incoming = {}
            for attack in attacks:
                incoming.setdefault(attack["target"], []).append(attack)
            warnings = self._copy_warnings(alive, entries)
            if len(alive) < 2:
                break
            if not builds and not attacks:
                self._say(f"{scope}: nobody landed a hit, so the rounds end early.")
                break
        return Standing(alive, entries, attacks)

    def _entry_path(self, scope: str, number: int, fighter: str) -> Path:
        return self.store.root / "entries" / scope / f"r{number}" / fighter

    def _turn(
        self,
        scope: str,
        number: int,
        fighter: Fighter,
        alive: list[Fighter],
        entries: dict[str, Path],
        incoming: dict[str, list[dict[str, Any]]],
        warnings: dict[str, str],
        builds: bool,
    ) -> dict[str, Any]:
        key = f"{scope}/r{number}/{fighter.id}"
        if self.store.has(key):
            return self.store.get(key)

        rival_ids = [other.id for other in alive if other.id != fighter.id]
        rivals = {} if builds else {rival: entries[rival] for rival in rival_ids}
        if builds:
            prompt = prompts.build(
                fighter.id, self.store.task, fighter.direction, fighter.angle
            )
        else:
            prompt = prompts.fight_round(
                fighter.id,
                self.store.task,
                fighter.direction,
                fighter.angle,
                number,
                rival_ids,
                incoming.get(fighter.id, []),
                warnings.get(fighter.id),
            )

        workspace = self.store.root / "workspace" / fighter.id
        label = f"{fighter.id} ({scope} round {number})"
        try:
            reply = self._attempt_turn(
                fighter,
                self._preamble + prompt,
                entries.get(fighter.id),
                rivals,
                workspace,
                label,
                self._host_group(fighter.group if scope.startswith("group-") else FINAL),
            )
            if reply is None:
                result = {"forfeit": True, "reason": "It failed twice."}
            else:
                changes = capture(
                    self.baseline, workspace, self._entry_path(scope, number, fighter.id)
                )
                result = self._read_turn(fighter, reply, changes)
                self._say(
                    f"{label} finished: {len(changes['changed'])} files changed, "
                    f"{len(result['attacks'])} attacks."
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        self.store.put(key, result)
        return result

    def _attempt_turn(
        self,
        fighter: Fighter,
        prompt: str,
        entry: Path | None,
        rivals: dict[str, Path],
        workspace: Path,
        label: str,
        group: str,
    ) -> str | None:
        for attempt in range(1, ATTEMPTS + 1):
            materialize(self.baseline, entry, workspace)
            add_rivals(workspace, rivals)
            job = Job(
                "fighter",
                prompt,
                workspace,
                writes=True,
                timeout=self.timeout,
                label=label,
                group=group,
            )
            try:
                return self._run_agent(fighter.agent, job, label)
            except UsageLimitReached:
                raise
            except AgentTimedOut as error:
                self._say(f"{fighter.id} failed: {error}")
                return None
            except AgentError as error:
                self._say(f"{fighter.id} failed on attempt {attempt}: {error}")
        return None

    @staticmethod
    def _read_turn(fighter: Fighter, reply: str, changes: dict[str, list[str]]) -> dict[str, Any]:
        try:
            body = extract_json(reply)
        except ValueError:
            body = {}
        attacks = body.get("attacks")
        return {
            "summary": str(body.get("summary") or reply[:500]),
            "attacks": attacks if isinstance(attacks, list) else [],
            "changed": changes["changed"],
            "deleted": changes["deleted"],
            "agent": fighter.agent.name,
        }

    @staticmethod
    def _valid_attacks(
        alive: list[Fighter], results: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ids = {fighter.id for fighter in alive}
        valid = []
        for fighter in alive:
            for attack in results[fighter.id]["attacks"]:
                if not isinstance(attack, dict):
                    continue
                target = attack.get("target")
                if not isinstance(target, str) or target not in ids or target == fighter.id:
                    continue
                if str(attack.get("problem", "")).strip() and str(attack.get("evidence", "")).strip():
                    valid.append({**attack, "from": fighter.id})
        return valid

    @staticmethod
    def _copy_warnings(alive: list[Fighter], entries: dict[str, Path]) -> dict[str, str]:
        warnings: dict[str, str] = {}
        for index, later in enumerate(alive):
            for earlier in alive[:index]:
                score = similarity(entries[earlier.id], entries[later.id])
                if score >= COPY_LIMIT:
                    warnings[later.id] = prompts.copy_warning(earlier.id, round(score * 100))
                    break
        return warnings

    def _review(self, scope: str, standing: Standing) -> Path:
        folder = self.store.root / "review" / scope
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        for fighter in standing.alive:
            shutil.copytree(standing.entries[fighter.id], folder / fighter.id)
        (folder / "attacks.json").write_text(
            json.dumps(standing.attacks, indent=2), encoding="utf-8"
        )
        return folder

    def _check_verdicts(
        self, results: list[GroupResult]
    ) -> tuple[list[Fighter], dict[str, Path]]:
        by_group = {result.group: result for result in results}
        by_id = {fighter.id: fighter for result in results for fighter in result.alive}

        def check(value: dict[str, Any]) -> None:
            if set(value["rulings"]) != set(by_group):
                raise ValueError(f"Rule on exactly these groups: {', '.join(by_group)}.")
            winners = set()
            for group, result in by_group.items():
                winner = value["rulings"][group]["winner"]
                if winner not in {fighter.id for fighter in result.alive}:
                    raise ValueError(f"{winner} is not a fighter in group {group}.")
                winners.add(winner)
            runner_up = value.get("runner_up")
            if runner_up is not None and (runner_up not in by_id or runner_up in winners):
                raise ValueError(f"{runner_up} cannot be the runner-up.")

        verdicts = [{"group": result.group, **result.verdict} for result in results]
        self._say("Judge check: the head judge is reviewing every group verdict.")
        prompt = prompts.head_check(self.store.task, verdicts, str(self.baseline))
        ruling = self._ask_json(
            "head-check",
            self._judge_job("head-check", prompt, self.store.root / "review"),
            check,
        )

        ids = [ruling["rulings"][result.group]["winner"] for result in results]
        for result, winner in zip(results, ids):
            if winner != result.verdict["winner"]:
                self._say(
                    f"Head judge overturned group {result.group}: "
                    f"{result.verdict['winner']} is out, {winner} is in."
                )
        if ruling.get("runner_up"):
            self._say(f"Head judge added {ruling['runner_up']} to the final.")
            ids.append(ruling["runner_up"])
        entries = {fighter: by_group[by_id[fighter].group].entries[fighter] for fighter in ids}
        return [by_id[fighter] for fighter in ids], entries

    def _final(self, finalists: list[Fighter], entries: dict[str, Path]) -> Standing:
        if len(finalists) < 2:
            return Standing(finalists, entries, [])
        self._say(f"Final: {', '.join(fighter.id for fighter in finalists)}")
        return self._rounds("final", finalists, entries, self.bracket.final_rounds, False)

    def _pick_champion(self, final: Standing) -> str:
        ids = [fighter.id for fighter in final.alive]
        if not ids:
            self._give_up("final/", "No fighter finished the final.")
        if len(ids) == 1:
            if not self.store.has("champion"):
                self.store.put("champion", {"champion": ids[0], "reason": "Last fighter standing."})
            self._close_group(FINAL)
            return ids[0]

        review = self._review("final", final)
        table = dict(
            zip(
                CATEGORIES,
                self._each(CATEGORIES, lambda category: self._specialist(category, ids, review)),
            )
        )
        self._close_group(FINAL)
        totals = {
            fighter: sum(table[category]["scores"][fighter] for category in CATEGORIES)
            for fighter in ids
        }
        scores = {category: table[category]["scores"] for category in CATEGORIES}

        def check(value: dict[str, Any]) -> None:
            if value["champion"] not in ids:
                raise ValueError(f"{value['champion']} is not a finalist.")
            _require_fixes(value)

        prompt = prompts.head_champion(self.store.task, ids, scores, totals)
        job = self._judge_job("head-champion", prompt, review)
        return self._ask_json("champion", job, check)["champion"]

    def _crown_group_winner(self, result: GroupResult) -> tuple[str, Path]:
        winner = result.verdict["winner"]
        if not self.store.has("champion"):
            self.store.put(
                "champion",
                {
                    "champion": winner,
                    "reason": result.verdict.get("reason", ""),
                    "fixes": result.verdict.get("fixes") or [],
                },
            )
        return winner, result.entries[winner]

    def _polish(self, champion: str, entry: Path) -> Path:
        fixes = [str(fix) for fix in self.store.get("champion").get("fixes") or []]
        fixes = [fix for fix in fixes if fix.strip()][:MAX_FIXES]
        fighter = self._roster.get(champion)
        if not fixes or fighter is None:
            return entry
        polished = self._entry_path("polish", 1, champion)
        if not self.store.has("polish"):
            noun = "fix" if len(fixes) == 1 else "fixes"
            self._say(f"Polish: {champion} is making the judge's {len(fixes)} {noun}.")
            workspace = self.store.root / "workspace" / champion
            label = f"{champion} (polish)"
            prompt = self._preamble + prompts.polish(champion, self.store.task, fixes)
            try:
                reply = self._attempt_turn(fighter, prompt, entry, {}, workspace, label, "")
                if reply is None:
                    outcome: dict[str, Any] = {"skipped": "The polish turn failed."}
                else:
                    changes = capture(self.baseline, workspace, polished)
                    outcome = self._read_turn(fighter, reply, changes)
                    self._say(f"{label} finished: {len(changes['changed'])} files changed.")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
            self.store.put("polish", outcome)
        if self.store.get("polish").get("skipped"):
            self._say(f"Polish skipped, so {champion}'s entry is kept as it was.")
            return entry
        return polished

    def _specialist(self, category: str, ids: list[str], review: Path) -> dict[str, Any]:
        def check(value: dict[str, Any]) -> None:
            if set(value["scores"]) != set(ids):
                raise ValueError(f"Score exactly these finalists: {', '.join(ids)}.")
            for fighter in ids:
                _require_score(value["scores"][fighter], f"{fighter} {category}")

        prompt = prompts.specialist(category, self.store.task, ids, str(self.baseline))
        job = self._judge_job("specialist", prompt, review, FINAL)
        return self._ask_json(f"verdict/{category}", job, check)

    def _finish(self, champion: str, entry: Path) -> dict[str, Any]:
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
                apply_champion(self.store)
            else:
                self.store.put("result", {"applied": False, "reason": "Run with --no-apply."})
        report.write(self.store)
        self.store.set_status("done")
        if not self.store.settings.get("keep", False):
            for folder in ("workspace", "review", "entries"):
                shutil.rmtree(root / folder, ignore_errors=True)
        return {"champion": champion, **self.store.get("result")}


def _require_fixes(value: dict[str, Any]) -> None:
    fixes = value.get("fixes", [])
    if not isinstance(fixes, list) or not all(isinstance(fix, str) for fix in fixes):
        raise ValueError("fixes must be a list of strings.")


def _inline_diffs(review: Path, ids: list[str]) -> str | None:
    parts = []
    for fighter in ids:
        diff = (review / fighter / "changes.diff").read_text(encoding="utf-8", errors="replace")
        parts.append(f"=== {fighter}/changes.diff ===\n{diff or '(no changes)'}")
    text = "\n".join(parts)
    return text if len(text) <= INLINE_DIFF_CHARS else None


def roll_back_cut_off_apply(store: RunStore) -> bool:
    backup = store.root / "backup"
    if not backup.exists() or store.steps.get("result", {}).get("applied"):
        return False
    undo_apply(backup, store.project)
    return True


def apply_champion(store: RunStore, force: bool = False) -> dict[str, Any]:
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


def undo_champion(store: RunStore) -> None:
    if (store.root / "backup").exists():
        undo_apply(store.root / "backup", store.project)
    store.put("result", {"applied": False, "reason": "Undone."})
    report.write(store)
