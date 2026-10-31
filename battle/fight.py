from __future__ import annotations

import json
import shutil
import signal
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, TypeVar

from battle import prompts, report
from battle.agents import (
    DEFAULT_TIMEOUT,
    Agent,
    AgentError,
    AgentStopped,
    Job,
    UsageLimitReached,
    extract_json,
)
from battle.prompts import CATEGORIES
from battle.sizing import Bracket
from battle.store import RunStore
from battle.workspace import (
    ApplyConflict,
    ApplyRefused,
    add_rivals,
    apply_entry,
    capture,
    copy_project,
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
COPY_LIMIT = 0.9
ATTEMPTS = 2
MAX_SCORE = 10

Item = TypeVar("Item")
Result = TypeVar("Result")


class BattleError(Exception):
    pass


class Stopped(Exception):
    pass


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


class Fight:
    def __init__(
        self,
        store: RunStore,
        agents: list[Agent],
        limit: int,
        log: Callable[[str], None] = print,
    ) -> None:
        settings = store.settings
        self.store = store
        self.agents = agents
        self.judge = agents[0]
        self.limit = max(1, limit)
        self._slots = threading.BoundedSemaphore(self.limit)
        self._stop = threading.Event()
        self.stop_signal: int | None = None
        self.bracket = Bracket(
            settings["groups"],
            settings["fighters"],
            settings["rounds"],
            settings["final_rounds"],
        )
        self.timeout = settings.get("timeout", DEFAULT_TIMEOUT)
        self.baseline = store.root / "baseline"
        web_access = prompts.WEB_RESEARCH if settings.get("web") else prompts.NO_WEB
        self._preamble = web_access + prompts.RULEBOOK
        self._log = log

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

        finalists, entries = self._check_verdicts(results)
        final = self._final(finalists, entries)
        champion = self._pick_champion(final)
        return self._finish(champion, final.entries[champion])

    def _say(self, message: str) -> None:
        self._log(message)
        self.store.note(message)

    def _give_up(self, stage: str, message: str) -> NoReturn:
        self.store.forget(
            [
                key
                for key, value in self.store.steps.items()
                if key.startswith(stage) and isinstance(value, dict) and value.get("forfeit")
            ]
        )
        raise BattleError(message)

    def _prepare_baseline(self) -> None:
        if self.baseline.exists():
            return
        partial = self.store.root / "baseline.partial"
        if partial.exists():
            shutil.rmtree(partial)
        copy_project(self.store.project, partial)
        partial.rename(self.baseline)

    def _judge_job(self, role: str, prompt: str, cwd: Path) -> Job:
        return Job(
            role,
            self._preamble + prompt,
            cwd,
            writes=False,
            read_dirs=(self.baseline,),
            timeout=self.timeout,
        )

    def _choose_directions(self) -> list[dict[str, str]]:
        count = self.bracket.groups

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
        self._say(f"Group {group}: {direction['name']}")
        standing = self._rounds(f"group-{group}", fighters, {}, self.bracket.rounds, True)
        if not standing.alive:
            self._say(f"Group {group}: every fighter forfeited.")
            return None
        review = self._review(f"group-{group}", standing)
        verdict = self._group_verdict(group, standing, review)
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

        prompt = prompts.group_judge(group, self.store.task, ids, str(self.baseline))
        return self._ask_json(key, self._judge_job("group-judge", prompt, review), check)

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
    ) -> str | None:
        for attempt in range(1, ATTEMPTS + 1):
            materialize(self.baseline, entry, workspace)
            add_rivals(workspace, rivals)
            job = Job(
                "fighter", prompt, workspace, writes=True, timeout=self.timeout, label=label
            )
            try:
                return self._run_agent(fighter.agent, job, label)
            except UsageLimitReached:
                raise
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
            return ids[0]

        review = self._review("final", final)
        table = dict(
            zip(
                CATEGORIES,
                self._each(CATEGORIES, lambda category: self._specialist(category, ids, review)),
            )
        )
        totals = {
            fighter: sum(table[category]["scores"][fighter] for category in CATEGORIES)
            for fighter in ids
        }
        scores = {category: table[category]["scores"] for category in CATEGORIES}

        def check(value: dict[str, Any]) -> None:
            if value["champion"] not in ids:
                raise ValueError(f"{value['champion']} is not a finalist.")

        prompt = prompts.head_champion(self.store.task, ids, scores, totals)
        job = self._judge_job("head-champion", prompt, review)
        return self._ask_json("champion", job, check)["champion"]

    def _specialist(self, category: str, ids: list[str], review: Path) -> dict[str, Any]:
        def check(value: dict[str, Any]) -> None:
            if set(value["scores"]) != set(ids):
                raise ValueError(f"Score exactly these finalists: {', '.join(ids)}.")
            for fighter in ids:
                _require_score(value["scores"][fighter], f"{fighter} {category}")

        prompt = prompts.specialist(category, self.store.task, ids, str(self.baseline))
        job = self._judge_job("specialist", prompt, review)
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

    def _ask_json(
        self, key: str, job: Job, check: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        if self.store.has(key):
            return self.store.get(key)
        problem = ""
        for _ in range(ATTEMPTS):
            attempt = replace(job, prompt=job.prompt + (prompts.retry(problem) if problem else ""))
            try:
                reply = self._run_agent(self.judge, attempt)
                value = extract_json(reply)
                check(value)
            except UsageLimitReached:
                raise
            except AgentError as error:
                problem = str(error)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                problem = f"{type(error).__name__}: {error}."
            else:
                self.store.put(key, value)
                return value
        raise BattleError(f"{key} failed twice. {problem}")

    def stop(self, signum: int) -> None:
        if self.stop_signal is not None:
            return
        self.stop_signal = signum
        self._halt()
        self._say(f"Stopping on {signal.Signals(signum).name}.")

    def _halt(self) -> None:
        self._stop.set()
        for agent in self.agents:
            cancel = getattr(agent, "cancel", None)
            if cancel:
                cancel()

    def _run_agent(self, agent: Agent, job: Job, label: str | None = None) -> str:
        with self._slots:
            if self._stop.is_set():
                raise Stopped
            if label:
                self._say(f"{label} started.")
            try:
                return agent.run(job)
            except AgentError as error:
                if isinstance(error, AgentStopped) or self._stop.is_set():
                    raise Stopped from error
                raise

    def _each(
        self, items: Iterable[Item], work: Callable[[Item], Result]
    ) -> list[Result]:
        listed = list(items)

        def guarded(item: Item) -> Result:
            if self._stop.is_set():
                raise Stopped
            try:
                return work(item)
            except Stopped:
                raise
            except BaseException:
                self._halt()
                raise

        with ThreadPoolExecutor(max_workers=max(1, len(listed))) as pool:
            futures = [pool.submit(guarded, item) for item in listed]
        errors = [error for future in futures if (error := future.exception()) is not None]
        for error in errors:
            if not isinstance(error, Stopped):
                raise error
        if errors:
            raise errors[0]
        return [future.result() for future in futures]


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
