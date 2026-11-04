import json
import re
import signal
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from battle import prompts, report
from battle.agents import AgentError, AgentStopped, AgentTimedOut, UsageLimitReached
from battle.fight import BattleError, Fight, Fighter, Stopped, apply_champion
from battle.sizing import Bracket
from battle.store import RunStore
from battle.workspace import apply_entry
from tests.fake_agent import FakeAgent

BRACKET = Bracket(groups=2, fighters=3, rounds=2, final_rounds=1)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTLE_HOME", str(tmp_path / "home"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    return root


def settings(**changes):
    base = {
        "groups": BRACKET.groups,
        "fighters": BRACKET.fighters,
        "rounds": BRACKET.rounds,
        "final_rounds": BRACKET.final_rounds,
        "apply": True,
        "keep": False,
        "timeout": 60,
    }
    return base | changes


def battle(project, agent, limit=3, **changes):
    agents = agent if isinstance(agent, list) else [agent]
    store = RunStore.create("add a feature", project, settings(**changes))
    return store, Fight(store, agents, limit=limit, log=lambda message: None)


def prompts_for(agent, role):
    return [job.prompt for job in agent.jobs if job.role == role]


def fighter_prompts(agent, fighter):
    return [p for p in prompts_for(agent, "fighter") if f"You are fighter {fighter}" in p]


def test_full_battle_applies_only_the_champion(project):
    agent = FakeAgent(champion="B3")
    store, fight = battle(project, agent)

    result = fight.run()

    assert result["champion"] == "B3"
    assert (project / "work_B3.txt").exists()
    assert not (project / "work_A3.txt").exists()
    assert not (project / "work_A1.txt").exists()
    assert store.status == "done"


def test_web_research_reaches_every_agent_and_judge_when_web_is_on(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, web=True)

    fight.run()

    assert {job.role for job in agent.jobs} == {
        "director",
        "fighter",
        "group-judge",
        "head-check",
        "specialist",
        "head-champion",
    }
    assert all(job.prompt.startswith(prompts.WEB_RESEARCH) for job in agent.jobs)


def test_no_agent_is_told_about_the_web_when_it_is_off(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, web=False)

    fight.run()

    assert not any(prompts.WEB_RESEARCH in job.prompt for job in agent.jobs)


def test_jobs_are_marked_with_who_leads_them(project):
    agent = FakeAgent(champion="A3")
    _, fight = battle(project, agent)

    fight.run()

    groups: dict[str, set[str]] = {}
    for job in agent.jobs:
        groups.setdefault(job.role, set()).add(job.group)
    assert groups["director"] == {""}
    assert groups["fighter"] == {"A", "B", "final", ""}
    assert groups["group-judge"] == {"A", "B"}
    assert groups["head-check"] == {""}
    assert groups["specialist"] == {"final"}
    assert groups["head-champion"] == {""}


def test_each_group_is_closed_once_its_work_is_done(project):
    closed = []

    class Closing(FakeAgent):
        def close_group(self, group):
            closed.append(group)

    _, fight = battle(project, Closing())

    fight.run()

    assert sorted(closed) == ["A", "B", "final"]


def test_a_group_with_no_fighters_left_is_closed_too(project):
    closed = []

    class Closing(FakeAgent):
        def close_group(self, group):
            closed.append(group)

    _, fight = battle(project, Closing(fail=("A1", "A2", "A3")))

    fight.run()

    assert "A" in closed


def test_rounds_ask_fighters_to_finish_quickly(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert "holds up the whole battle" in fighter_prompts(agent, "A1")[1]


def test_rulebook_has_ten_numbered_rules():
    assert len(re.findall(r"^\d+\. ", prompts.RULEBOOK, re.MULTILINE)) == 10


def test_every_agent_and_judge_gets_the_rulebook(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, web=True)

    fight.run()

    assert all(prompts.RULEBOOK in job.prompt for job in agent.jobs)


def test_rulebook_ships_with_the_package():
    config = tomllib.loads(Path("pyproject.toml").read_text())
    assert "rules.md" in config["tool"]["setuptools"]["package-data"]["battle"]


@pytest.mark.parametrize("role", ["group-judge", "specialist"])
def test_scoring_judges_penalize_broken_rules(project, role):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert all("breaks the rulebook" in prompt for prompt in prompts_for(agent, role))


def test_head_judge_overturns_verdicts_that_ignored_the_rulebook(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert "ignored the rulebook" in prompts_for(agent, "head-check")[0]


def test_head_judge_refuses_fake_work_as_champion(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert "fake work" in prompts_for(agent, "head-champion")[0]


def test_fighters_can_attack_a_broken_rule(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert "broken rule" in fighter_prompts(agent, "A1")[1]


def test_without_web_agents_say_what_they_could_not_check(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, web=False)

    fight.run()

    assert all(prompts.NO_WEB in job.prompt for job in agent.jobs)


def test_with_web_agents_are_not_told_they_have_none(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, web=True)

    fight.run()

    assert not any(prompts.NO_WEB in job.prompt for job in agent.jobs)


def test_web_prompt_treats_pages_as_data_and_protects_your_code():
    note = prompts.WEB_RESEARCH.lower()
    assert "never instructions" in note
    assert "never put this project's code" in note


def test_full_battle_makes_exactly_the_estimated_runs(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert len(agent.jobs) == BRACKET.estimated_runs()


def test_every_role_appears_in_the_right_number(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert Counter(job.role for job in agent.jobs) == {
        "director": 1,
        "fighter": 12 + 2 + 1,
        "group-judge": 2,
        "head-check": 1,
        "specialist": 5,
        "head-champion": 1,
    }


def test_judges_cannot_write_and_fighters_can(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert all(job.writes for job in agent.jobs if job.role == "fighter")
    assert not any(job.writes for job in agent.jobs if job.role != "fighter")


def test_each_group_gets_its_own_direction(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    assert "Plan 0" in fighter_prompts(agent, "A1")[0]
    assert "Plan 1" in fighter_prompts(agent, "B1")[0]


def test_fighters_in_a_group_get_different_angles(project):
    agent = FakeAgent()
    _, fight = battle(project, agent)

    fight.run()

    first_prompts = [fighter_prompts(agent, f"A{i}")[0] for i in (1, 2, 3)]
    assert len({prompt.split("Your angle:")[1].splitlines()[0] for prompt in first_prompts}) == 3


def test_groups_are_walled_off_until_the_final(project):
    agent = FakeAgent(champion="A3", fixes=())
    _, fight = battle(project, agent)

    fight.run()

    assert agent.seen_rivals["A1"] == [[], ["A2", "A3"]]
    assert agent.seen_rivals["A3"] == [[], ["A1", "A2"], ["B3"]]


def test_attacks_with_evidence_reach_the_target_in_the_next_round(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, rounds=3)

    fight.run()

    third_round = fighter_prompts(agent, "A1")[2]
    assert "found a hole" in third_round


def test_attacks_without_evidence_are_thrown_out(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, rounds=3)

    fight.run()

    assert not any("vague complaint" in prompt for prompt in prompts_for(agent, "fighter"))


def test_group_judge_sees_the_final_attacks(project):
    agent = FakeAgent()
    store, fight = battle(project, agent)
    original = agent._group_judge
    seen = []

    def spy(job):
        seen.append((job.cwd / "attacks.json").read_text())
        return original(job)

    agent._group_judge = spy
    fight.run()

    assert "found a hole" in seen[0]


def test_head_judge_can_overturn_a_group_verdict(project):
    agent = FakeAgent(overturn={"A": "A1"}, champion="A1", fixes=())
    _, fight = battle(project, agent)

    result = fight.run()

    assert result["champion"] == "A1"
    assert (project / "work_A1.txt").exists()
    assert agent.seen_rivals["A1"][-1] == ["B3"]


def test_runner_up_joins_the_final(project):
    agent = FakeAgent(runner_up="B1")
    _, fight = battle(project, agent)

    fight.run()

    assert agent.seen_rivals["B1"][-1] == ["A3", "B3"]
    assert len(agent.jobs) == BRACKET.estimated_runs() + BRACKET.final_rounds


def test_rounds_stop_early_when_nobody_lands_a_hit(project):
    agent = FakeAgent(no_attacks=True)
    _, fight = battle(project, agent, rounds=3, final_rounds=2)

    fight.run()

    assert len(fighter_prompts(agent, "A1")) == 2


def test_fighters_that_copy_each_other_are_told_to_change(project):
    agent = FakeAgent(identical=True)
    _, fight = battle(project, agent, rounds=3)

    fight.run()

    assert "too close to" in fighter_prompts(agent, "A2")[1]


def test_a_crashed_fighter_gets_one_retry_then_forfeits(project):
    agent = FakeAgent(fail=("A2",))
    store, fight = battle(project, agent)

    fight.run()

    assert len(fighter_prompts(agent, "A2")) == 2
    assert store.get("group-A/r1/A2")["forfeit"] is True
    assert agent.seen_rivals["A1"][-1] == ["A3"]


def test_report_names_forfeits(project):
    agent = FakeAgent(fail=("A2",))
    store, fight = battle(project, agent)

    fight.run()

    assert "A2" in (store.root / "report.md").read_text().split("Forfeits")[1]


def test_group_with_one_survivor_skips_its_judge(project):
    agent = FakeAgent(fail=("A1", "A2"))
    _, fight = battle(project, agent)

    fight.run()

    assert len(prompts_for(agent, "group-judge")) == 1


def test_battle_fails_when_every_fighter_is_out(project):
    agent = FakeAgent(fail=("A1", "A2", "A3", "B1", "B2", "B3"))
    _, fight = battle(project, agent)

    with pytest.raises(BattleError, match="No fighter"):
        fight.run()


def test_usage_limit_pauses_and_keeps_finished_work(project):
    agent = FakeAgent(limit_after=9)
    store, fight = battle(project, agent)

    with pytest.raises(UsageLimitReached):
        fight.run()

    saved = RunStore.open(store.id)
    assert saved.has("directions")
    assert len(saved.steps) >= 2
    assert not (project / "work_A3.txt").exists()


def test_resume_finishes_without_repeating_finished_work(project):
    first = FakeAgent(limit_after=9, champion="B3")
    store, fight = battle(project, first)
    with pytest.raises(UsageLimitReached):
        fight.run()
    finished = len(RunStore.open(store.id).steps)

    second = FakeAgent(champion="B3")
    result = Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert result["champion"] == "B3"
    assert (project / "work_B3.txt").exists()
    assert len(second.jobs) == BRACKET.estimated_runs() - finished


def test_resumed_fighter_starts_from_its_last_saved_round(project):
    store, fight = battle(project, FakeAgent(limit_after=5))
    with pytest.raises(UsageLimitReached):
        fight.run()

    second = FakeAgent(champion="A3", fixes=())
    Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert (project / "work_A3.txt").read_text().count("work by A3") == 3


def test_judge_gets_one_retry_on_a_bad_reply(project):
    agent = FakeAgent(bad_json_once=("group-judge",))
    _, fight = battle(project, agent)

    fight.run()

    assert len(prompts_for(agent, "group-judge")) == 3


def test_judge_that_invents_a_winner_stops_the_battle(project):
    agent = FakeAgent(invalid_winner=True)
    store, fight = battle(project, agent)

    with pytest.raises(BattleError, match="Z9"):
        fight.run()


def test_judge_that_scores_a_stranger_is_rejected(project):
    agent = FakeAgent(scores_stranger=True)
    _, fight = battle(project, agent)

    with pytest.raises(BattleError, match="Score exactly"):
        fight.run()


def test_failed_battle_can_resume_after_the_problem_is_gone(project):
    store, fight = battle(project, FakeAgent(invalid_winner=True))
    with pytest.raises(BattleError):
        fight.run()

    second = FakeAgent(champion="A3")
    result = Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert result["champion"] == "A3"


def test_losers_are_thrown_away_after_the_battle(project):
    store, fight = battle(project, FakeAgent())

    fight.run()

    assert not (store.root / "entries").exists()
    assert not (store.root / "workspace").exists()
    assert not (store.root / "review").exists()


def test_keep_saves_every_entry(project):
    store, fight = battle(project, FakeAgent(), keep=True)

    fight.run()

    assert (store.root / "entries").exists()


def test_no_apply_leaves_the_project_alone_until_asked(project):
    agent = FakeAgent(champion="B3")
    store, fight = battle(project, agent, apply=False)

    result = fight.run()

    assert result["applied"] is False
    assert not (project / "work_B3.txt").exists()

    assert apply_champion(store)["applied"] is True
    assert (project / "work_B3.txt").exists()


def test_your_own_edits_block_the_apply_instead_of_being_lost(project):
    agent = FakeAgent()
    store, fight = battle(project, agent, apply=False)
    fight.run()

    outcome_clean = store.get("result")
    assert outcome_clean["applied"] is False

    champion = store.get("champion")["champion"]
    (project / f"work_{champion}.txt").write_text("mine\n")

    outcome = apply_champion(store)

    assert outcome["applied"] is False
    assert outcome["conflicts"] == [f"work_{champion}.txt"]
    assert (project / f"work_{champion}.txt").read_text() == "mine\n"


def test_mixed_agents_share_the_fighters_and_the_first_one_judges(project):
    first, second = FakeAgent(name="first"), FakeAgent(name="second")
    _, fight = battle(project, [first, second])

    fight.run()

    assert any("You are fighter A1" in p for p in prompts_for(first, "fighter"))
    assert any("You are fighter A2" in p for p in prompts_for(second, "fighter"))
    assert not any(job.role != "fighter" for job in second.jobs)


@dataclass
class CountingAgent(FakeAgent):
    active: int = 0
    peak: int = 0
    gate: threading.Lock = field(default_factory=threading.Lock)

    def run(self, job):
        with self.gate:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.02)
        try:
            return super().run(job)
        finally:
            with self.gate:
                self.active -= 1


def test_fighters_at_once_is_respected(project):
    agent = CountingAgent()
    _, fight = battle(project, agent, limit=2)

    fight.run()

    assert agent.peak == 2


def test_the_limit_is_shared_across_all_groups(project):
    agent = CountingAgent()
    _, fight = battle(project, agent, limit=4, groups=3, fighters=3)

    fight.run()

    assert agent.peak == 4


class MeetingAgent(FakeAgent):
    def __init__(self, parties):
        super().__init__()
        self.barrier = threading.Barrier(parties, timeout=10)

    def _fighter(self, job):
        if "Do the task completely" in job.prompt:
            self.barrier.wait()
        return super()._fighter(job)


def test_every_fighter_in_every_group_starts_at_the_same_time(project):
    agent = MeetingAgent(parties=9)
    store, fight = battle(project, agent, limit=9, groups=3, fighters=3)

    fight.run()

    assert store.status == "done"
    assert not agent.barrier.broken


def test_a_usage_limit_in_one_group_stops_every_group(project):
    agent = FakeAgent(limit_after=8)
    _, fight = battle(project, agent, limit=9, groups=3, fighters=3)

    with pytest.raises(UsageLimitReached):
        fight.run()

    assert len(agent.jobs) <= 8 + 9


def test_a_battle_stopped_by_a_limit_in_parallel_groups_can_resume(project):
    store, fight = battle(project, FakeAgent(limit_after=8), limit=9, groups=3, fighters=3)
    with pytest.raises(UsageLimitReached):
        fight.run()

    second = FakeAgent(champion="C3", fixes=())
    Fight(RunStore.open(store.id), [second], limit=9, log=lambda message: None).run()

    assert (project / "work_C3.txt").read_text().count("work by C3") == 3


def test_the_log_shows_each_fighter_start_and_finish(project):
    store, fight = battle(project, FakeAgent())

    fight.run()

    log = (store.root / "log.txt").read_text()
    assert "A1 (group-A round 1) started." in log
    assert "A1 (group-A round 1) finished: 1 files changed, 0 attacks." in log


def test_secrets_never_reach_a_fighter(project):
    (project / ".env").write_text("API_KEY=secret")
    agent = FakeAgent()
    original = agent._fighter
    seen = []

    def spy(job):
        seen.append((job.cwd / ".env").exists())
        return original(job)

    agent._fighter = spy
    _, fight = battle(project, agent)

    fight.run()

    assert not any(seen)
    assert (project / ".env").read_text() == "API_KEY=secret"


class HangingAgent(FakeAgent):
    def __post_init__(self):
        super().__post_init__()
        self.crashed = set()
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def _fighter(self, job):
        fighter = re.search(r"You are fighter (\w+)", job.prompt).group(1)
        with self._guard:
            first_attempt = fighter not in self.crashed
            self.crashed.add(fighter)
        if first_attempt:
            raise AgentError("crashed once")
        self.started.set()
        self.cancelled.wait(10)
        raise AgentStopped("killed by the stop")


def stop_when_started(agent, fight):
    def stop():
        agent.started.wait(10)
        fight.stop(signal.SIGTERM)

    thread = threading.Thread(target=stop)
    thread.start()
    return thread


def test_a_stop_during_the_last_attempt_is_not_a_forfeit(project):
    agent = HangingAgent()
    store, fight = battle(project, agent)
    stopper = stop_when_started(agent, fight)

    with pytest.raises(Stopped):
        fight.run()
    stopper.join()

    assert agent.cancelled.is_set()
    assert fight.stop_signal == signal.SIGTERM
    assert not any(step.get("forfeit") for step in store.steps.values())


def test_a_stopped_battle_resumes_with_every_fighter(project):
    agent = HangingAgent()
    store, fight = battle(project, agent)
    stopper = stop_when_started(agent, fight)
    with pytest.raises(Stopped):
        fight.run()
    stopper.join()

    second = FakeAgent(champion="B3")
    result = Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert result["champion"] == "B3"
    assert len(fighter_prompts(second, "A1")) == 2


def test_a_stop_after_the_last_judge_leaves_the_project_alone(project):
    agent = FakeAgent()
    store, fight = battle(project, agent)
    answer = agent._head_champion

    def stop_then_answer(job):
        fight.stop(signal.SIGINT)
        return answer(job)

    agent._head_champion = stop_then_answer

    with pytest.raises(Stopped):
        fight.run()

    assert not list(project.glob("work_*.txt"))
    assert not store.has("result")


def test_an_attack_with_an_unhashable_target_is_thrown_out():
    fighters = [Fighter(name, "A", {}, "angle", FakeAgent()) for name in ("A1", "A2")]
    attack = {"target": ["A2"], "problem": "hole", "evidence": "line 1"}
    results = {"A1": {"attacks": [attack]}, "A2": {"attacks": []}}

    assert Fight._valid_attacks(fighters, results) == []


def test_the_first_signal_decides_how_a_battle_stopped(project):
    _, fight = battle(project, FakeAgent())

    fight.stop(signal.SIGTERM)
    fight.stop(signal.SIGINT)

    assert fight.stop_signal == signal.SIGTERM


def test_a_blank_direction_brief_gets_a_retry(project):
    agent = FakeAgent()
    answer = agent._director
    blank = {"directions": [{"name": "One", "brief": "  "}, {"name": "Two", "brief": "ok"}]}
    replies = [json.dumps(blank)]
    agent._director = lambda job: replies.pop() if replies else answer(job)
    store, fight = battle(project, agent)

    fight.run()

    assert len(prompts_for(agent, "director")) == 2
    assert store.status == "done"


def test_a_direction_without_a_brief_is_retried(project):
    agent = FakeAgent()
    store, fight = battle(project, agent)
    answer = agent._director
    calls = []

    def director(job):
        calls.append(job)
        if len(calls) == 1:
            return json.dumps({"directions": [{"name": "One"}, {"name": "Two"}]})
        return answer(job)

    agent._director = director
    fight.run()

    assert len(calls) == 2
    assert store.status == "done"


def test_a_ruling_on_an_unknown_group_is_retried(project):
    agent = FakeAgent()
    store, fight = battle(project, agent)
    answer = agent._head_check
    calls = []

    def head_check(job):
        calls.append(job)
        body = json.loads(answer(job))
        if len(calls) == 1:
            body["rulings"]["Z"] = {}
        return json.dumps(body)

    agent._head_check = head_check
    fight.run()

    assert len(calls) == 2
    assert store.status == "done"


def test_a_failed_report_leaves_entries_so_the_battle_can_resume(project, monkeypatch):
    store, fight = battle(project, FakeAgent(), apply=False)

    def broken(_store):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(report, "write", broken)
        with pytest.raises(OSError):
            fight.run()

    assert (store.root / "entries").exists()
    resumed = Fight(RunStore.open(store.id), [FakeAgent()], limit=3, log=lambda message: None)
    assert resumed.run()["applied"] is False


class ErrorOnStopAgent(FakeAgent):
    def __post_init__(self):
        super().__post_init__()
        self.fight = None

    def _fighter(self, job):
        self.fight.stop(signal.SIGTERM)
        raise AgentError("the chat reported a failure after the stop")


def test_an_ordinary_error_during_a_stop_is_not_a_forfeit(project):
    agent = ErrorOnStopAgent()
    store, fight = battle(project, agent)
    agent.fight = fight

    with pytest.raises(Stopped):
        fight.run()

    assert not any(step.get("forfeit") for step in store.steps.values())


ALL_FIGHTERS = ("A1", "A2", "A3", "B1", "B2", "B3")


def test_resume_retries_fighters_lost_to_a_total_wipeout(project):
    store, fight = battle(project, FakeAgent(fail=ALL_FIGHTERS))
    with pytest.raises(BattleError, match="group stage"):
        fight.run()

    second = FakeAgent(champion="B3")
    result = Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert result["champion"] == "B3"
    assert (project / "work_B3.txt").exists()


def test_resume_retries_a_final_every_finalist_lost(project):
    agent = FakeAgent()
    store, fight = battle(project, agent)
    work = agent._fighter

    def fail_in_the_final(job):
        if "group" not in job.label:
            raise AgentError("outage")
        return work(job)

    agent._fighter = fail_in_the_final
    with pytest.raises(BattleError, match="the final"):
        fight.run()

    second = FakeAgent(champion="B3")
    result = Fight(RunStore.open(store.id), [second], limit=3, log=lambda message: None).run()

    assert result["champion"] == "B3"


def test_an_apply_cut_off_by_a_crash_is_redone_cleanly(project):
    store, fight = battle(project, FakeAgent(champion="B3"), apply=False)
    fight.run()
    root = store.root
    apply_entry(root / "champion", root / "baseline", project, root / "backup")
    (project / "work_B3.txt").unlink()

    outcome = apply_champion(store)

    assert outcome["applied"] is True
    assert (project / "work_B3.txt").exists()


def test_a_leftover_partial_champion_is_never_used(project):
    store, fight = battle(project, FakeAgent())
    (store.root / "champion.partial").mkdir()
    (store.root / "champion.partial" / "junk").write_text("half a copy")

    fight.run()

    assert not (store.root / "champion" / "junk").exists()
    assert not (store.root / "champion.partial").exists()


QUICK = {"groups": 1, "fighters": 3, "rounds": 1}


def test_quick_battle_skips_directions_head_check_and_final(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, **QUICK)

    fight.run()

    assert Counter(job.role for job in agent.jobs) == {"fighter": 3 + 1, "group-judge": 1}
    assert len(agent.jobs) == Bracket(**QUICK).estimated_runs()


def test_quick_battle_hands_every_job_straight_to_the_chat(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, **QUICK)

    fight.run()

    assert {job.group for job in agent.jobs} == {""}


def test_quick_judge_reads_every_diff_in_its_prompt(project):
    agent = FakeAgent()
    _, fight = battle(project, agent, **QUICK)

    fight.run()

    judge = prompts_for(agent, "group-judge")[0]
    for fighter in ("A1", "A2", "A3"):
        assert f"=== {fighter}/changes.diff ===" in judge
        assert f"+work by {fighter}" in judge


def test_judge_reads_diffs_from_disk_when_they_are_too_big(project, monkeypatch):
    monkeypatch.setattr("battle.fight.INLINE_DIFF_CHARS", 10)
    agent = FakeAgent()
    _, fight = battle(project, agent, **QUICK)

    fight.run()

    judge = prompts_for(agent, "group-judge")[0]
    assert "too large to show here" in judge
    assert "=== A1/changes.diff ===" not in judge


def test_champion_polishes_the_judges_fixes_before_it_is_applied(project):
    agent = FakeAgent(fixes=("app.py: validate the input",))
    store, fight = battle(project, agent, **QUICK)

    result = fight.run()

    polish = fighter_prompts(agent, "A3")[-1]
    assert "app.py: validate the input" in polish
    assert "your entry won" in polish
    assert result["champion"] == "A3"
    assert (project / "work_A3.txt").read_text() == "work by A3\nwork by A3\n"
    assert store.get("champion")["fixes"] == ["app.py: validate the input"]


def test_no_fixes_means_no_polish_turn(project):
    agent = FakeAgent(fixes=())
    store, fight = battle(project, agent, **QUICK)

    fight.run()

    assert len(prompts_for(agent, "fighter")) == 3
    assert not store.has("polish")
    assert (project / "work_A3.txt").read_text() == "work by A3\n"


def test_failed_polish_keeps_the_winning_entry(project):
    @dataclass
    class PolishFails(FakeAgent):
        def _fighter(self, job):
            if "your entry won" in job.prompt:
                raise AgentError("polish crashed")
            return super()._fighter(job)

    store, fight = battle(project, PolishFails(), **QUICK)

    result = fight.run()

    assert result["applied"]
    assert store.get("polish") == {"skipped": "The polish turn failed."}
    assert (project / "work_A3.txt").read_text() == "work by A3\n"


def test_judge_must_give_fixes_as_a_list_of_text(project):
    @dataclass
    class BadFixesOnce(FakeAgent):
        sent_bad: bool = False

        def _group_judge(self, job):
            reply = json.loads(super()._group_judge(job))
            if not self.sent_bad:
                self.sent_bad = True
                reply["fixes"] = "not a list"
            return json.dumps(reply)

    agent = BadFixesOnce()
    _, fight = battle(project, agent, **QUICK)

    fight.run()

    judges = prompts_for(agent, "group-judge")
    assert len(judges) == 2
    assert "fixes must be a list of strings" in judges[1]


def test_a_fighter_that_times_out_is_not_retried(project):
    @dataclass
    class SlowA1(FakeAgent):
        def _fighter(self, job):
            if "You are fighter A1" in job.prompt:
                raise AgentTimedOut("fake timed out")
            return super()._fighter(job)

    agent = SlowA1()
    store, fight = battle(project, agent, **QUICK)

    fight.run()

    assert len(fighter_prompts(agent, "A1")) == 1
    assert store.get("group-A/r1/A1")["forfeit"]


def test_report_lists_the_fixes_and_what_the_polish_did(project):
    agent = FakeAgent(fixes=("app.py: validate the input",))
    store, fight = battle(project, agent, **QUICK)

    fight.run()

    text = report.render(store)
    assert "## Fixes after judging" in text
    assert "- app.py: validate the input" in text
    assert "A3 worked" in text
