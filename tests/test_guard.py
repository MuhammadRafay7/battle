import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from battle.guard import check

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    write(root / "app.py", "print('real')\n")
    write(root / ".env", "API_KEY=secret\n")
    run = root / ".battle" / "runs" / "r1"
    write(run / "workspace" / "A1" / "app.py", "print('a1')\n")
    write(run / "workspace" / "A2" / "app.py", "print('a2')\n")
    write(run / "review" / "group-A" / "A1" / "changes.diff", "diff\n")
    write(run / "baseline" / "app.py", "print('real')\n")
    return root


def workspace(project, fighter="A1"):
    return project / ".battle" / "runs" / "r1" / "workspace" / fighter


def call(project, tool, agent="agent-1", **tool_input):
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(project), "agent_id": agent}


def test_fighter_can_use_its_own_workspace(project):
    own = workspace(project)
    assert check(call(project, "Write", file_path=str(own / "new.py")), "fighter") is None
    assert check(call(project, "Read", file_path=str(own / "app.py")), "fighter") is None
    assert check(call(project, "Edit", file_path=str(own / "app.py")), "fighter") is None
    assert check(call(project, "Grep", path=str(own), pattern="x"), "fighter") is None
    assert check(call(project, "Glob", path=str(own), pattern="**/*.py"), "fighter") is None


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
def test_fighter_cannot_touch_the_real_project(project, tool):
    payload = call(project, tool, file_path=str(project / "app.py"))
    assert "outside your workspace" in check(payload, "fighter")


def test_fighter_cannot_read_secrets(project):
    assert check(call(project, "Read", file_path=str(project / ".env")), "fighter")


def test_a_relative_path_resolves_to_the_real_project_and_is_blocked(project):
    assert check(call(project, "Write", file_path="real.txt"), "fighter")


def test_fighter_cannot_climb_out_of_its_workspace(project):
    check(call(project, "Read", file_path=str(workspace(project) / "app.py")), "fighter")
    escape = workspace(project) / ".." / ".." / ".." / ".." / ".." / "app.py"
    assert check(call(project, "Write", file_path=str(escape)), "fighter")


def test_fighter_cannot_follow_a_symlink_out_of_its_workspace(project):
    own = workspace(project)
    check(call(project, "Read", file_path=str(own / "app.py")), "fighter")
    (own / "link.py").symlink_to(project / "app.py")
    assert check(call(project, "Edit", file_path=str(own / "link.py")), "fighter")


def test_fighter_is_locked_to_the_first_workspace_it_touches(project):
    check(call(project, "Write", file_path=str(workspace(project) / "x.py")), "fighter")
    rival = workspace(project, "A2") / "app.py"
    assert "another fighter" in check(call(project, "Read", file_path=str(rival)), "fighter")
    assert "another fighter" in check(call(project, "Write", file_path=str(rival)), "fighter")


def test_each_agent_gets_its_own_workspace(project):
    first = call(project, "Write", agent="one", file_path=str(workspace(project) / "x.py"))
    second = call(project, "Write", agent="two", file_path=str(workspace(project, "A2") / "x.py"))
    assert check(first, "fighter") is None
    assert check(second, "fighter") is None


def test_fighter_cannot_rewrite_the_claims(project):
    check(call(project, "Read", file_path=str(workspace(project) / "app.py")), "fighter")
    claim = project / ".battle" / "runs" / "r1" / "claims" / "agent-1"
    assert check(call(project, "Write", file_path=str(claim)), "fighter")


@pytest.mark.parametrize("role", ["fighter", "judge"])
def test_agents_can_read_their_prompt_file(project, role):
    prompt = write(project / ".battle" / "runs" / "r1" / "jobs" / "0001.prompt.md", "task")
    assert check(call(project, "Read", file_path=str(prompt)), role) is None


def job_prompt(project, name, cwd):
    jobs = project / ".battle" / "runs" / "r1" / "jobs"
    write(jobs / f"{name}.json", json.dumps({"cwd": str(cwd)}))
    return write(jobs / f"{name}.prompt.md", "task")


def test_reading_its_prompt_ties_a_fighter_to_that_workspace(project):
    prompt = job_prompt(project, "1", workspace(project))
    assert check(call(project, "Read", file_path=str(prompt)), "fighter") is None

    rival = workspace(project, "A2") / "app.py"
    assert "another fighter" in check(call(project, "Read", file_path=str(rival)), "fighter")


def test_fighter_cannot_read_a_rivals_prompt(project):
    own = job_prompt(project, "1", workspace(project))
    rival = job_prompt(project, "2", workspace(project, "A2"))
    check(call(project, "Read", file_path=str(own)), "fighter")

    assert "another fighter" in check(call(project, "Read", file_path=str(rival)), "fighter")


def test_fighter_cannot_read_a_judges_prompt(project):
    review = project / ".battle" / "runs" / "r1" / "review" / "group-A"
    prompt = job_prompt(project, "3", review)

    assert "judge" in check(call(project, "Read", file_path=str(prompt)), "fighter")
    assert check(call(project, "Read", file_path=str(prompt)), "judge") is None


def test_fighter_cannot_change_a_prompt_file(project):
    prompt = write(project / ".battle" / "runs" / "r1" / "jobs" / "0001.prompt.md", "task")
    assert check(call(project, "Write", file_path=str(prompt)), "fighter")


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_search_without_a_path_is_blocked_because_it_means_the_real_project(project, tool):
    assert check(call(project, tool, pattern="API_KEY"), "fighter")


@pytest.mark.parametrize("pattern", ["/etc/*", "../../*", "~/.ssh/*"])
def test_glob_patterns_cannot_escape(project, pattern):
    payload = call(project, "Glob", path=str(workspace(project)), pattern=pattern)
    assert check(payload, "fighter")


def test_grep_file_filter_cannot_escape(project):
    payload = call(project, "Grep", path=str(workspace(project)), pattern="x", glob="../../*")
    assert check(payload, "fighter")


def test_an_agent_without_an_id_is_blocked(project):
    payload = call(project, "Read", file_path=str(workspace(project) / "app.py"))
    del payload["agent_id"]
    assert check(payload, "fighter")


def test_judge_can_read_the_run_folder(project):
    run = project / ".battle" / "runs" / "r1"
    assert check(call(project, "Read", file_path=str(run / "baseline" / "app.py")), "judge") is None
    assert check(call(project, "Grep", path=str(run / "review"), pattern="x"), "judge") is None


def test_judge_cannot_read_secrets_in_the_real_project(project):
    assert check(call(project, "Read", file_path=str(project / ".env")), "judge")


def test_judge_cannot_search_the_real_project(project):
    assert check(call(project, "Grep", path=str(project), pattern="API_KEY"), "judge")
    assert check(call(project, "Grep", pattern="API_KEY"), "judge")


@pytest.mark.parametrize("role", ["fighter", "judge"])
def test_a_battle_cannot_reach_another_projects_run(tmp_path, project, role):
    other = write(tmp_path / "other" / ".battle" / "runs" / "r9" / "baseline" / "app.py", "x")
    assert "another project" in check(call(project, "Read", file_path=str(other)), role)


def test_a_prompt_file_of_another_project_is_blocked(tmp_path, project):
    prompt = write(tmp_path / "other" / ".battle" / "runs" / "r9" / "jobs" / "1.prompt.md", "x")
    assert check(call(project, "Read", file_path=str(prompt)), "judge")


def test_a_chat_in_a_subfolder_still_reaches_its_own_run(project):
    baseline = project / ".battle" / "runs" / "r1" / "baseline" / "app.py"
    assert check(call(project / "src", "Read", file_path=str(baseline)), "judge") is None


@pytest.mark.parametrize("role", ["fighter", "judge"])
def test_a_home_path_is_blocked_before_the_tool_can_expand_it(project, role):
    payload = call(project, "Read", file_path="~/.battle/runs/old/baseline/.env")
    assert check(payload, role)


@pytest.mark.parametrize("pattern", ["{/etc,/root}/*", "{src,~/.ssh}/*"])
def test_brace_patterns_cannot_escape(project, pattern):
    payload = call(project, "Glob", path=str(workspace(project)), pattern=pattern)
    assert check(payload, "fighter")


def test_ordinary_brace_patterns_are_allowed(project):
    payload = call(project, "Glob", path=str(workspace(project)), pattern="src/{a,b}/*.py")
    assert check(payload, "fighter") is None


def test_a_fake_run_inside_a_rivals_workspace_does_not_change_the_owner(project):
    check(call(project, "Read", file_path=str(workspace(project) / "app.py")), "fighter")
    run = workspace(project, "A2") / ".battle" / "runs" / "fake"
    nested = write(run / "workspace" / "A1" / "app.py", "x")
    assert "another fighter" in check(call(project, "Write", file_path=str(nested)), "fighter")


def test_a_fake_prompt_inside_a_rivals_workspace_is_not_a_prompt(project):
    check(call(project, "Read", file_path=str(workspace(project) / "app.py")), "fighter")
    run = workspace(project, "A2") / ".battle" / "runs" / "fake"
    fake = write(run / "jobs" / "0001.prompt.md", "x")
    assert "another fighter" in check(call(project, "Read", file_path=str(fake)), "fighter")


def test_a_chosen_battle_home_outside_the_project_stays_reachable(tmp_path, project, monkeypatch):
    home = tmp_path / "elsewhere" / ".battle"
    target = write(home / "runs" / "r1" / "baseline" / "app.py", "x")
    monkeypatch.setenv("BATTLE_HOME", str(home))
    assert check(call(project, "Read", file_path=str(target)), "judge") is None


def saved_run(home, project, name):
    run = home / "runs" / name
    write(run / "state.json", json.dumps({"project": str(project)}))
    return write(run / "baseline" / "app.py", "x")


@pytest.mark.parametrize("role", ["fighter", "judge"])
def test_the_shared_home_folder_does_not_open_another_projects_run(tmp_path, role):
    user = tmp_path / "user"
    other = saved_run(user / ".battle", user / "other", "r9")
    payload = call(user / "mine", "Read", file_path=str(other))
    assert "another project" in check(payload, role)


def test_a_shared_battle_home_still_separates_projects(tmp_path, project, monkeypatch):
    home = tmp_path / "elsewhere" / ".battle"
    monkeypatch.setenv("BATTLE_HOME", str(home))
    other = saved_run(home, tmp_path / "other", "r9")
    assert "another project" in check(call(project, "Read", file_path=str(other)), "judge")


def test_a_run_in_a_shared_home_is_reachable_from_its_own_project(tmp_path, project):
    mine = saved_run(tmp_path / "user" / ".battle", project, "r1")
    assert check(call(project / "src", "Read", file_path=str(mine)), "judge") is None


def test_a_chat_above_the_project_still_reaches_its_own_run(tmp_path):
    repo = tmp_path / "repo"
    mine = saved_run(tmp_path / "user" / ".battle", repo / "pkg", "r1")
    assert check(call(repo, "Read", file_path=str(mine)), "judge") is None


def test_an_unreadable_run_state_is_blocked(project):
    state = write(project / ".battle" / "runs" / "r1" / "state.json", "not json")
    payload = call(project, "Read", file_path=str(state.parent / "baseline" / "app.py"))
    assert run_script("judge", json.dumps(payload)).returncode == 2


def test_parallel_calls_from_one_agent_never_block_each_other(project):
    payload = call(project, "Read", file_path=str(workspace(project) / "app.py"))
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: check(payload, "fighter"), range(64)))

    assert results == [None] * 64
    claims = project / ".battle" / "runs" / "r1" / "claims"
    assert [path.name for path in claims.iterdir()] == ["agent-1"]


BATTLE = str(ROOT / "bin" / "battle")


def lead_bash(project, command):
    return call(project, "Bash", command=command)


@pytest.mark.parametrize("agent", ["battle-fighter", "battle-judge"])
def test_lead_can_start_guarded_agents(project, agent):
    assert check(call(project, "Agent", subagent_type=agent, prompt="x"), "lead") is None


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("agent", ["general-purpose", "Explore", "battle-lead", None])
def test_lead_cannot_start_an_unguarded_agent(project, tool, agent):
    assert check(call(project, tool, subagent_type=agent, prompt="x"), "lead")


@pytest.mark.parametrize(
    "command",
    [
        f'"{BATTLE}" jobs 20261031-091900-467720 --group A',
        f'"{BATTLE}" jobs 20261031-091900-467720 --group final',
        f"\"{BATTLE}\" reply 1793 20261031-091900-467720 <<'BATTLE_REPLY'\n{{\"a\": \"$(x)\"}}\nBATTLE_REPLY",
        f"\"{BATTLE}\" reply 1793 20261031-091900-467720 --failed <<'BATTLE_REPLY'\ncrashed\nBATTLE_REPLY",
        f"\"{BATTLE}\" reply 1793 20261031-091900-467720 --usage-limit <<'BATTLE_REPLY'\nlimit\nBATTLE_REPLY",
    ],
)
def test_lead_can_run_battle_commands(project, command):
    assert check(lead_bash(project, command), "lead") is None


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "rm -rf .",
        f'"{BATTLE}" undo',
        f'"{BATTLE}" apply --force',
        f'"{BATTLE}" jobs r1 --group A; rm -rf .',
        f'"{BATTLE}" jobs r1 --group A && cat .env',
        f'"{BATTLE}" jobs $(cat .env) --group A',
        f'"{BATTLE}" jobs r1 --group A\nrm -rf .',
        '"/tmp/evil/bin/battle" jobs r1 --group A',
        f"\"{BATTLE}\" reply 1 r1 <<'BATTLE_REPLY'\ntext\nBATTLE_REPLY\nrm -rf .",
        f"\"{BATTLE}\" reply 1 r1 <<'BATTLE_REPLY'\nBATTLE_REPLY\nrm -rf .\nBATTLE_REPLY",
        f'"{BATTLE}" reply 1 r1 <<BATTLE_REPLY\n$(rm -rf .)\nBATTLE_REPLY',
        f"\"{BATTLE}\" reply 1 r1 <<'BATTLE_REPLY'\nno end",
    ],
)
def test_lead_cannot_run_anything_else(project, command):
    assert check(lead_bash(project, command), "lead")


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Grep", "Glob"])
def test_lead_has_no_file_tools(project, tool):
    assert check(call(project, tool, file_path=str(project / "app.py"), path=str(project)), "lead")


def run_script(role, stdin):
    return subprocess.run(
        [str(ROOT / "bin" / "battle-guard"), role], input=stdin, capture_output=True, text=True
    )


def test_script_allows_with_exit_zero(project):
    payload = call(project, "Read", file_path=str(workspace(project) / "app.py"))
    assert run_script("fighter", json.dumps(payload)).returncode == 0


def test_script_blocks_with_exit_two_and_explains(project):
    result = run_script("fighter", json.dumps(call(project, "Write", file_path="real.txt")))
    assert result.returncode == 2
    assert "outside your workspace" in result.stderr


@pytest.mark.parametrize("stdin", ["not json", "", "[]", '{"tool_name": "Read"}'])
def test_script_blocks_anything_it_cannot_understand(stdin):
    assert run_script("fighter", stdin).returncode == 2


def test_script_rejects_an_unknown_role(project):
    payload = call(project, "Read", file_path=str(workspace(project) / "app.py"))
    assert run_script("nobody", json.dumps(payload)).returncode == 2

