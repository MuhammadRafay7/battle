import json
import subprocess
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

