import threading
import time
import urllib.request
from pathlib import Path

import pytest

from battle.agents import ClaudeAgent, Job, UsageLimitReached
from battle.engine import BattleError
from battle.host import HostAgent, claim_jobs, post_reply
from battle.store import RunStore
from battle.team import checks, report
from battle.team.cli import main
from battle.team.pipeline import ANGLES, Pipeline, estimate_runs
from battle.team.prompts import MANIFEST
from tests.fake_team import STATIC_SERVER, FakeTeamAgent

ANGLE_PAIR = ["auth", "xss"]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTLE_HOME", str(tmp_path / "home"))


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# Old\n")
    return root


def settings(**changes):
    base = {
        "skip": [],
        "solo": [],
        "angles": ANGLE_PAIR,
        "security_rounds": 3,
        "apply": True,
        "keep": False,
        "timeout": 60,
        "check_timeout": 30,
        "web": True,
        "agents": ["fake"],
    }
    return base | changes


def team(project, agent, **changes):
    agents = agent if isinstance(agent, list) else [agent]
    store = RunStore.create("a site for a climbing gym", project, settings(**changes))
    return store, Pipeline(store, agents, limit=4, log=lambda message: None)


def roles(agent):
    return [job.role for job in agent.jobs]


def prompts_for(agent, role):
    return [job.prompt for job in agent.jobs if job.role == role]


def test_full_run_applies_only_the_winning_build(project):
    agent = FakeTeamAgent(dev_winner="a", design_winner="b")
    store, pipeline = team(project, agent)

    result = pipeline.run()

    assert result["applied"]
    page = (project / "site" / "index.html").read_text()
    assert "built by a on Stack One" in page
    assert "<!-- fixed -->" in page
    assert not (project / ".team").exists()
    assert (project / "README.md").read_text() == "# Old\n"
    assert store.status == "done"
    assert (store.root / "report.md").exists()
    assert (store.root / "docs" / "requirements.md").exists()
    assert (store.root / "docs" / "research.md").exists()
    assert (store.root / "docs" / "run.json").exists()


def test_stages_run_in_order(project):
    agent = FakeTeamAgent()
    _, pipeline = team(project, agent)

    pipeline.run()

    order = roles(agent)
    first = {role: order.index(role) for role in set(order)}
    assert first["pm"] < first["researcher"] < first["designer"] < first["judge"] < first["dev"]
    assert first["dev"] < first["attacker"] < first["verifier"] < first["fixer"]
    assert order.count("designer") == 2
    assert order.count("dev") == 2
    assert order.count("judge") == 2


def test_only_the_winning_design_reaches_the_developers(project):
    agent = FakeTeamAgent(design_winner="a")
    store, pipeline = team(project, agent)

    pipeline.run()

    assert agent.designs_seen == {"a": "design by a\n", "b": "design by a\n"}
    assert (store.root / "docs" / "design" / "design.md").read_text() == "design by a\n"


def test_judge_fixes_are_applied_by_the_winner_before_hand_off(project):
    fixes = {"design": ["design.md: add a footer"], "dev": ["site/index.html: fix title"]}
    agent = FakeTeamAgent(fixes=fixes)
    store, pipeline = team(project, agent)

    pipeline.run()

    assert (store.root / "docs" / "design" / "design.md").read_text().endswith("polished\n")
    assert "<!-- polished -->" in (project / "site" / "index.html").read_text()
    polishes = [job for job in agent.jobs if "won its stage" in job.prompt]
    assert [job.role for job in polishes] == ["designer", "dev"]


def test_skipped_stages_do_not_run(project):
    agent = FakeTeamAgent()
    _, pipeline = team(project, agent, skip=["research", "design", "security"])

    result = pipeline.run()

    assert result["applied"]
    assert set(roles(agent)) == {"pm", "dev", "judge"}
    assert agent.designs_seen == {"a": "", "b": ""}
    assert "Next.js" in prompts_for(agent, "dev")[0]


def test_solo_stages_have_one_entrant_and_no_judge(project):
    agent = FakeTeamAgent()
    store, pipeline = team(project, agent, solo=["design", "dev"], skip=["security"])

    pipeline.run()

    assert roles(agent).count("designer") == 1
    assert roles(agent).count("dev") == 1
    assert "judge" not in roles(agent)
    assert store.get("dev/judge")["reason"] == "Only entrant."
    assert "built by a" in (project / "site" / "index.html").read_text()


def test_developers_get_different_stacks_and_toolkits(project):
    agent = FakeTeamAgent()
    _, pipeline = team(project, agent, skip=["security"])

    pipeline.run()

    a, b = sorted(prompts_for(agent, "dev"), key=lambda p: "developer A" not in p)
    assert "Stack One" in a and "Stack Two" in b
    assert "responsive-craft" in a and "3d-web-experience" in b
    da, db = sorted(prompts_for(agent, "designer"), key=lambda p: "designer A" not in p)
    assert "ui-ux-pro-max" in da and "impeccable" in db


def test_security_loop_fixes_then_retests_until_clean(project):
    agent = FakeTeamAgent(vulnerable_rounds=1)
    store, pipeline = team(project, agent)

    pipeline.run()

    summary = store.get("security")
    assert summary["rounds"] == 2
    assert summary["open"] == []
    assert len(store.get("security/r1")["confirmed"]) == 1
    assert store.get("security/r2")["confirmed"] == []
    assert roles(agent).count("fixer") == 1
    retest = [p for p in prompts_for(agent, "attacker") if "confirmed last round" in p]
    assert len(retest) == 1 and "Session never expires" in retest[0]


def test_security_stops_at_the_round_limit_with_open_findings(project):
    agent = FakeTeamAgent(vulnerable_rounds=99)
    store, pipeline = team(project, agent, security_rounds=2)

    result = pipeline.run()

    assert result["applied"]
    summary = store.get("security")
    assert summary["rounds"] == 2
    assert [finding["id"] for finding in summary["open"]] == ["auth-1"]
    assert roles(agent).count("fixer") == 1
    assert "Open findings: 1" in report.render(store)


def test_security_keeps_findings_open_when_the_fixer_fails(project):
    agent = FakeTeamAgent(fixer_fails=True)
    store, pipeline = team(project, agent)

    pipeline.run()

    assert store.get("security")["rounds"] == 1
    assert len(store.get("security")["open"]) == 1
    assert "built by" in (project / "site" / "index.html").read_text()


def test_attackers_get_a_running_site(project):
    agent = FakeTeamAgent(serve=True, vulnerable_rounds=0)
    store, pipeline = team(project, agent, security_rounds=1)

    pipeline.run()

    prompt = prompts_for(agent, "attacker")[0]
    assert "running at http://127.0.0.1:" in prompt
    assert store.get("security/r1")["served"] is True


def test_attackers_test_statically_when_the_site_does_not_build(project):
    agent = FakeTeamAgent(broken_build_sides=("a", "b"), vulnerable_rounds=0)
    store, pipeline = team(project, agent, security_rounds=1)

    pipeline.run()

    assert "could not be started" in prompts_for(agent, "attacker")[0]
    assert store.get("security/r1")["served"] is False


def test_judge_sees_gradient_and_build_failures(project):
    agent = FakeTeamAgent(gradient_sides=("b",), broken_build_sides=("a",))
    store, pipeline = team(project, agent, skip=["security"])

    pipeline.run()

    design_prompt, dev_prompt = prompts_for(agent, "judge")
    assert "Checks on A:\nGradient rule: no gradients found." in design_prompt
    assert "Checks on B:\nGradient rule broken in 1 places:" in design_prompt
    assert "tokens.css:2" in design_prompt
    assert "build (false): FAILED" in dev_prompt
    assert store.get("dev/checks")["a"]["built"] is False
    assert store.get("dev/checks")["b"]["built"] is True


def test_judge_scores_against_the_acceptance_criteria(project):
    agent = FakeTeamAgent()
    store, pipeline = team(project, agent, skip=["security"])

    pipeline.run()

    prompt = prompts_for(agent, "judge")[1]
    assert "- AC-1: has a home page" in prompt and "- AC-2: no gradients" in prompt
    assert store.get("dev/judge")["acceptance"]["b"]["AC-1"] == "met"


def test_bad_judge_json_is_retried_once(project):
    agent = FakeTeamAgent(bad_json_once=("judge",))
    _, pipeline = team(project, agent, skip=["security", "design"])

    pipeline.run()

    assert roles(agent).count("judge") == 2
    assert "could not be used" in prompts_for(agent, "judge")[1]


def test_invalid_winner_twice_fails_the_run(project):
    agent = FakeTeamAgent(invalid_winner=True)
    _, pipeline = team(project, agent, skip=["security", "design"])

    with pytest.raises(BattleError, match="dev/judge failed twice"):
        pipeline.run()
    assert not (project / "site").exists()


def test_research_questions_get_answered_and_the_agent_continues(project):
    agent = FakeTeamAgent(ask_research=("dev",))
    store, pipeline = team(project, agent, skip=["security", "design"], solo=["dev"])

    pipeline.run()

    dev_prompts = prompts_for(agent, "dev")
    assert len(dev_prompts) == 2
    assert "Use Stripe." in dev_prompts[1]
    answers = [p for p in prompts_for(agent, "researcher") if "needs these answered" in p]
    assert len(answers) == 1 and "payment provider" in answers[0]
    assert store.has("dev/a/research")


def test_research_questions_are_ignored_when_research_is_skipped(project):
    agent = FakeTeamAgent(ask_research=("dev",))
    _, pipeline = team(project, agent, skip=["security", "design", "research"], solo=["dev"])

    pipeline.run()

    assert len(prompts_for(agent, "dev")) == 1
    assert "researcher" not in roles(agent)


def test_pm_failure_stops_the_run(project):
    agent = FakeTeamAgent(fail_roles=("pm",))
    _, pipeline = team(project, agent)

    with pytest.raises(BattleError, match="no requirements"):
        pipeline.run()


def test_research_failure_is_survived(project):
    agent = FakeTeamAgent(fail_roles=("researcher",))
    _, pipeline = team(project, agent, skip=["security", "design"])

    result = pipeline.run()

    assert result["applied"]
    assert "Next.js" in prompts_for(agent, "dev")[0]


def test_usage_limit_pauses_and_resume_finishes_without_repeating_work(project):
    first = FakeTeamAgent(limit_after=5)
    store, pipeline = team(project, first)

    with pytest.raises(UsageLimitReached):
        pipeline.run()
    assert store.has("pm") and store.has("brief")

    second = FakeTeamAgent()
    result = Pipeline(RunStore.open(store.id), [second], limit=4, log=lambda m: None).run()

    assert result["applied"]
    assert "pm" not in roles(second)
    assert "researcher" not in roles(second)


def test_no_apply_keeps_the_project_untouched(project):
    agent = FakeTeamAgent()
    store, pipeline = team(project, agent, apply=False, skip=["security"])

    result = pipeline.run()

    assert not result["applied"]
    assert not (project / "site").exists()
    assert (store.root / "champion" / "files" / "site" / "index.html").exists()


def test_estimate_runs_counts_every_stage():
    full = {"pm": 1, "research": 1, "design": 4, "dev": 4, "security": 11}
    assert estimate_runs(settings()) == full
    trimmed = {"pm": 1, "design": 4, "dev": 1}
    assert estimate_runs(settings(skip=["research", "security"], solo=["dev"])) == trimmed
    assert estimate_runs(settings(angles=list(ANGLES), security_rounds=3))["security"] == 23


def test_gradient_scan_finds_only_ui_files(tmp_path):
    (tmp_path / "a.css").write_text("x { background: linear-gradient(red, blue); }\n")
    (tmp_path / "b.tsx").write_text('<div className="bg-gradient-to-r" />\n')
    (tmp_path / "notes.md").write_text("no gradient here\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.css").write_text("gradient\n")

    hits = checks.gradients(tmp_path)

    assert [hit.split(":")[0] for hit in hits] == ["a.css", "b.tsx"]


def test_build_check_reports_a_missing_manifest(tmp_path):
    result = checks.check_build(tmp_path, MANIFEST, timeout=5)

    assert not result.built
    assert "run.json is missing" in result.render()


def test_serve_starts_the_site_and_stops_it(tmp_path):
    (tmp_path / "index.html").write_text("hi")
    manifest = {"start": STATIC_SERVER, "url": "http://127.0.0.1:{port}/index.html"}

    with checks.serve(tmp_path, manifest, tmp_path / "server.log") as url:
        assert url.startswith("http://127.0.0.1:")
        assert urllib.request.urlopen(url, timeout=5).read() == b"hi"
        port = int(url.rsplit(":", 1)[1].split("/")[0])
    deadline = time.monotonic() + 5
    while checks._listening(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not checks._listening(port)


def test_serve_gives_up_when_the_server_never_listens(tmp_path):
    with checks.serve(tmp_path, {"start": "true"}, tmp_path / "log", wait=2) as url:
        assert url is None


def test_claude_agent_adds_bash_for_jobs_that_ask_for_it(tmp_path):
    agent = ClaudeAgent()

    def tools(job):
        argv = agent.command(job)
        return argv[argv.index("--tools") + 1]

    assert "Bash" in tools(Job("judge", "p", tmp_path, writes=False, bash=True))
    assert "Bash" not in tools(Job("judge", "p", tmp_path, writes=False))


def test_host_publishes_the_job_subagent(tmp_path):
    jobs_dir = tmp_path / "jobs"
    host = HostAgent(jobs_dir, model="sonnet", poll_seconds=0.01)
    job = Job("attacker", "probe", tmp_path, writes=True, subagent="team-attacker")
    seen = {}

    def chat():
        while not (records := claim_jobs(jobs_dir)):
            time.sleep(0.01)
        seen.update(records[0])
        post_reply(jobs_dir, records[0]["id"], '{"summary": "done", "findings": []}')

    thread = threading.Thread(target=chat)
    thread.start()
    reply = host.run(job)
    thread.join()

    assert "done" in reply
    assert seen["subagent_type"] == "team-attacker"
    assert seen["group"] == ""


def test_cli_plan_prints_stages(capsys):
    code = main(["plan", "--skip", "research", "--angles", "auth,xss", "--security-rounds", "1"])

    out = capsys.readouterr().out
    assert code == 0
    assert "research    0  skipped" in out
    assert "security    3" in out


def test_cli_rejects_unknown_angles(capsys):
    assert main(["plan", "--angles", "auth,teapot"]) == 1
    assert "Unknown attack angles: teapot" in capsys.readouterr().err


def test_cli_run_with_a_command_agent(project, capsys):
    fake = Path(__file__).with_name("fake_team_cli.py")
    argv = [
        "run",
        "a site",
        "--project",
        str(project),
        "--agent",
        f"command:python3 {fake}",
        "--skip",
        "security",
        "--skip",
        "research",
        "--solo",
        "design",
        "--solo",
        "dev",
    ]

    code = main(argv)

    out = capsys.readouterr().out
    assert code == 0, out
    assert "Applied: 2 changed" in out
    assert (project / "site" / "index.html").exists()
