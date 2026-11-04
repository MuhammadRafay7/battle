from __future__ import annotations

import shutil
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Callable, Iterable, NoReturn, TypeVar

from battle import prompts
from battle.agents import Agent, AgentError, AgentStopped, Job, UsageLimitReached, extract_json
from battle.store import RunStore
from battle.workspace import copy_project

ATTEMPTS = 2

Item = TypeVar("Item")
Result = TypeVar("Result")


class BattleError(Exception):
    pass


class Stopped(Exception):
    pass


class Engine:
    def __init__(
        self,
        store: RunStore,
        agents: list[Agent],
        limit: int,
        log: Callable[[str], None] = print,
    ) -> None:
        self.store = store
        self.agents = agents
        self.judge = agents[0]
        self.limit = max(1, limit)
        self._slots = threading.BoundedSemaphore(self.limit)
        self._stop = threading.Event()
        self.stop_signal: int | None = None
        self.baseline = store.root / "baseline"
        self._log = log

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

    def _close_group(self, group: str) -> None:
        for agent in self.agents:
            close = getattr(agent, "close_group", None)
            if close:
                close(group)

    def _ask_json(
        self, key: str, job: Job, check: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        if self.store.has(key):
            return self.store.get(key)
        problem = ""
        for _ in range(ATTEMPTS):
            attempt = replace(job, prompt=job.prompt + (prompts.retry(problem) if problem else ""))
            try:
                reply = self._run_agent(self.judge, attempt, job.label or None)
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

    def _each(self, items: Iterable[Item], work: Callable[[Item], Result]) -> list[Result]:
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
