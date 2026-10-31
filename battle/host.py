from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from battle.agents import AgentError, AgentStopped, Job, UsageLimitReached

SUBAGENT_MODELS = ("fable", "opus", "sonnet", "haiku")
REPLY = ".reply"
FAILED = ".failed"
LIMIT = ".limit"
JOB_ID = re.compile(r"^\d+$")


def subagent_model(model: str | None) -> str | None:
    for alias in SUBAGENT_MODELS:
        if model and alias in model:
            return alias
    return None


def _write(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def _post(jobs_dir: Path, job_id: str, suffix: str, text: str) -> None:
    if not JOB_ID.match(job_id) or not (jobs_dir / f"{job_id}.json").exists():
        raise ValueError(f"No job called {job_id}.")
    _write(jobs_dir / f"{job_id}{suffix}", text)


def post_reply(jobs_dir: Path, job_id: str, text: str) -> None:
    _post(jobs_dir, job_id, REPLY, text)


def post_failure(jobs_dir: Path, job_id: str, text: str) -> None:
    _post(jobs_dir, job_id, FAILED, text)


def post_usage_limit(jobs_dir: Path, job_id: str, text: str) -> None:
    _post(jobs_dir, job_id, LIMIT, text)


def claim_jobs(jobs_dir: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            path.with_suffix(".claimed").open("x").close()
        except FileExistsError:
            continue
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def reset_jobs(jobs_dir: Path) -> None:
    if jobs_dir.exists():
        for path in jobs_dir.iterdir():
            path.unlink()


@dataclass
class HostAgent:
    jobs_dir: Path
    model: str | None = None
    web: bool = True
    poll_seconds: float = 0.5
    name: str = "host"

    def __post_init__(self) -> None:
        self._guard = threading.Lock()
        self._count = time.time_ns()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self, job: Job) -> str:
        return self._wait(self._publish(job), job.timeout)

    def _publish(self, job: Job) -> str:
        with self._guard:
            self._count += 1
            job_id = str(self._count)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.jobs_dir / f"{job_id}.prompt.md"
        _write(
            prompt_file,
            f"Your working folder is {job.cwd}. Wherever this prompt says this folder or "
            f"here, it means that path. Always use absolute paths inside it.\n\n{job.prompt}",
        )
        record = {
            "id": job_id,
            "role": job.role,
            "label": job.label or job.role,
            "subagent_type": "battle-fighter" if job.writes else "battle-judge",
            "model": subagent_model(self.model),
            "cwd": str(job.cwd),
            "prompt_file": str(prompt_file),
            "prompt": f"Read {prompt_file} and follow it exactly.",
        }
        _write(self.jobs_dir / f"{job_id}.json", json.dumps(record, indent=2))
        return job_id

    def _wait(self, job_id: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            for suffix in (REPLY, FAILED, LIMIT):
                path = self.jobs_dir / f"{job_id}{suffix}"
                if path.exists():
                    return self._read(path, suffix)
            if self._cancelled.is_set():
                raise AgentStopped(f"Battle stopped job {job_id}.")
            if time.monotonic() >= deadline:
                raise AgentError(f"Job {job_id} timed out after {timeout:g} seconds.")
            time.sleep(self.poll_seconds)

    @staticmethod
    def _read(path: Path, suffix: str) -> str:
        text = path.read_text(encoding="utf-8")
        if suffix == LIMIT:
            raise UsageLimitReached(text.strip()[:500] or "Plan usage limit reached.")
        if suffix == FAILED:
            raise AgentError(text.strip()[:500] or "The subagent failed.")
        return text
