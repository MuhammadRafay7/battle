import threading
import time
from pathlib import Path

import pytest

from battle.agents import AgentError, AgentStopped, Job, UsageLimitReached
from battle.fight import Fight
from battle.host import (
    HostAgent,
    claim_jobs,
    post_failure,
    post_reply,
    post_usage_limit,
    reset_jobs,
    subagent_model,
)
from battle.store import RunStore
from tests.fake_agent import FakeAgent


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTLE_HOME", str(tmp_path / "home"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    return root


def job(tmp_path, **changes):
    values = {"role": "fighter", "prompt": "do it", "cwd": tmp_path, "writes": True, "label": "A1"}
    return Job(**(values | changes))


class Chat:
    def __init__(self, jobs_dir, fake):
        self.jobs_dir = jobs_dir
        self.fake = fake
        self.records = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join(timeout=5)

    def loop(self):
        while not self.done.is_set():
            for record in claim_jobs(self.jobs_dir):
                self.records.append(record)
                threading.Thread(target=self.answer, args=(record,), daemon=True).start()
            time.sleep(0.01)

    def answer(self, record):
        prompt = open(record["prompt_file"], encoding="utf-8").read()
        task = Job(record["role"], prompt, Path(record["cwd"]), writes=True)
        try:
            post_reply(self.jobs_dir, record["id"], self.fake.run(task))
        except UsageLimitReached as error:
            post_usage_limit(self.jobs_dir, record["id"], str(error))
        except AgentError as error:
            post_failure(self.jobs_dir, record["id"], str(error))


def battle(project, limit=4, **changes):
    settings = {
        "groups": 2,
        "fighters": 2,
        "rounds": 2,
        "final_rounds": 1,
        "apply": True,
        "keep": False,
        "timeout": 20,
        "web": True,
    } | changes
    store = RunStore.create("add a feature", project, settings)
    jobs_dir = store.root / "jobs"
    agent = HostAgent(jobs_dir, poll_seconds=0.01)
    return store, jobs_dir, Fight(store, [agent], limit=limit, log=lambda message: None)


def test_a_whole_battle_runs_through_the_chat(project):
    store, jobs_dir, fight = battle(project)

    with Chat(jobs_dir, FakeAgent(champion="B2")):
        result = fight.run()

    assert result["champion"] == "B2"
    assert (project / "work_B2.txt").exists()
    assert store.status == "done"


def test_chat_gets_every_role_with_the_right_subagent_type(project):
    _, jobs_dir, fight = battle(project)

    with Chat(jobs_dir, FakeAgent()) as chat:
        fight.run()

    types = {record["role"]: record["subagent_type"] for record in chat.records}
    assert types["fighter"] == "battle-fighter"
    for role in ("director", "group-judge", "head-check", "specialist", "head-champion"):
        assert types[role] == "battle-judge"


def test_limit_applies_across_the_chat(project):
    _, jobs_dir, fight = battle(project, limit=2)
    peak = {"active": 0, "peak": 0}
    gate = threading.Lock()

    class Slow(FakeAgent):
        def run(self, job):
            with gate:
                peak["active"] += 1
                peak["peak"] = max(peak["peak"], peak["active"])
            time.sleep(0.02)
            try:
                return super().run(job)
            finally:
                with gate:
                    peak["active"] -= 1

    with Chat(jobs_dir, Slow()):
        fight.run()

    assert peak["peak"] == 2


def test_usage_limit_from_the_chat_pauses_without_waiting_for_other_jobs(project):
    _, jobs_dir, fight = battle(project)
    started = time.monotonic()

    with Chat(jobs_dir, FakeAgent(limit_after=3)):
        with pytest.raises(UsageLimitReached):
            fight.run()

    assert time.monotonic() - started < 10


def test_a_new_chat_can_resume_a_paused_battle(project):
    store, jobs_dir, fight = battle(project)
    with Chat(jobs_dir, FakeAgent(limit_after=3)):
        with pytest.raises(UsageLimitReached):
            fight.run()

    reset_jobs(jobs_dir)
    agent = HostAgent(jobs_dir, poll_seconds=0.01)
    resumed = Fight(RunStore.open(store.id), [agent], limit=4, log=lambda message: None)
    with Chat(jobs_dir, FakeAgent(champion="A2")):
        result = resumed.run()

    assert result["champion"] == "A2"


def test_a_failed_fighter_forfeits_through_the_chat(project):
    store, jobs_dir, fight = battle(project, fighters=3)

    with Chat(jobs_dir, FakeAgent(fail=("A2",))) as chat:
        fight.run()

    assert store.get("group-A/r1/A2")["forfeit"] is True
    attempts = [r for r in chat.records if "A2 (group-A round 1)" in r["label"]]
    assert len(attempts) == 2


def test_each_job_can_only_be_claimed_once(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    waiter = threading.Thread(target=lambda: _ignore(agent, job(tmp_path, timeout=0.2)))
    waiter.start()
    while not list(tmp_path.glob("*.json")):
        time.sleep(0.01)

    assert len(claim_jobs(tmp_path)) == 1
    assert claim_jobs(tmp_path) == []
    waiter.join()


def _ignore(agent, task):
    try:
        agent.run(task)
    except AgentError:
        pass


def test_job_gives_the_chat_a_short_prompt_and_keeps_the_full_one_on_disk(tmp_path):
    agent = HostAgent(tmp_path, model="fable", poll_seconds=0.01)
    long_prompt = "x" * 50_000
    waiter = threading.Thread(target=lambda: _ignore(agent, job(tmp_path, prompt=long_prompt, timeout=0.3)))
    waiter.start()
    while not (records := claim_jobs(tmp_path)):
        time.sleep(0.01)
    waiter.join()

    record = records[0]
    assert len(record["prompt"]) < 500
    assert record["prompt_file"] in record["prompt"]
    assert record["model"] == "fable"
    text = open(record["prompt_file"], encoding="utf-8").read()
    assert long_prompt in text
    assert f"Your working folder is {tmp_path}" in text


def test_a_job_nobody_answers_times_out(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    with pytest.raises(AgentError, match="timed out"):
        agent.run(job(tmp_path, timeout=0.05))


def test_a_failure_reply_is_an_agent_error_not_a_usage_limit(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)

    def fail():
        while not (records := claim_jobs(tmp_path)):
            time.sleep(0.01)
        post_failure(tmp_path, records[0]["id"], "subagent crashed")

    threading.Thread(target=fail, daemon=True).start()
    with pytest.raises(AgentError, match="subagent crashed") as error:
        agent.run(job(tmp_path, timeout=5))
    assert not isinstance(error.value, UsageLimitReached)


def test_cancel_stops_a_waiting_job(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    threading.Timer(0.05, agent.cancel).start()
    with pytest.raises(AgentError, match="stopped"):
        agent.run(job(tmp_path, timeout=30))


def test_a_cancelled_job_counts_as_a_stop_not_a_failure(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    agent.cancel()
    with pytest.raises(AgentStopped):
        agent.run(job(tmp_path, timeout=30))


def test_a_late_reply_cannot_answer_a_job_from_a_newer_engine(tmp_path):
    _ignore(HostAgent(tmp_path, poll_seconds=0.01), job(tmp_path, timeout=0.02))
    stale = next(tmp_path.glob("*.json")).stem
    reset_jobs(tmp_path)
    _ignore(HostAgent(tmp_path, poll_seconds=0.01), job(tmp_path, timeout=0.02))

    with pytest.raises(ValueError, match=stale):
        post_reply(tmp_path, stale, "late")


@pytest.mark.parametrize("post", [post_reply, post_failure, post_usage_limit])
def test_replying_to_an_unknown_job_is_refused(tmp_path, post):
    with pytest.raises(ValueError, match="9999"):
        post(tmp_path, "9999", "text")


def test_job_ids_cannot_escape_the_jobs_folder(tmp_path):
    with pytest.raises(ValueError):
        post_reply(tmp_path, "../evil", "text")


def test_a_reply_may_contain_anything(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    text = "quotes ' \" `$(danger)` and\nlines\n"

    def answer():
        while not (records := claim_jobs(tmp_path)):
            time.sleep(0.01)
        post_reply(tmp_path, records[0]["id"], text)

    threading.Thread(target=answer, daemon=True).start()
    assert agent.run(job(tmp_path, timeout=5)) == text


def test_reset_jobs_clears_unanswered_work(tmp_path):
    agent = HostAgent(tmp_path, poll_seconds=0.01)
    _ignore(agent, job(tmp_path, timeout=0.02))

    reset_jobs(tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "model, alias",
    [
        (None, None),
        ("fable", "fable"),
        ("claude-fable-5-1", "fable"),
        ("claude-opus-5-5", "opus"),
        ("claude-sonnet-5-5", "sonnet"),
        ("haiku", "haiku"),
        ("gpt-9", None),
    ],
)
def test_subagent_model_maps_to_the_aliases_the_chat_accepts(model, alias):
    assert subagent_model(model) == alias
