import re
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from battle import prompts
from battle.agents import UsageLimitReached
from battle.fight import BattleError, Fight, apply_champion
from battle.sizing import Bracket
from battle.store import RunStore
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
        "fighter": 12 + 2,
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
    agent = FakeAgent(champion="A3")
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
    agent = FakeAgent(overturn={"A": "A1"}, champion="A1")
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

    second = FakeAgent(champion="A3")
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
