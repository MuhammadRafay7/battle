import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from battle import cli
from battle.store import RunStore, latest_run

ROOT = Path(__file__).resolve().parent.parent
FAKE = shlex.quote(sys.executable) + " " + shlex.quote(str(ROOT / "tests" / "fake_agent.py"))
SMALL = ["--groups", "2", "--fighters", "2", "--rounds", "2", "--final-rounds", "1"]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    return root


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_CONFIG", json.dumps({"log": str(tmp_path / "agent.log")}))


def run_battle(project, *extra):
    return cli.main(
        ["run", "add a feature", "--project", str(project), "--agent", f"command:{FAKE}"]
        + SMALL
        + list(extra)
    )


def test_plan_shows_the_size_without_running_anything(capsys):
    assert cli.main(["plan", "--size", "big"]) == 0

    output = capsys.readouterr().out
    assert "4 groups of 5" in output
    assert "20 fighters" in output
    assert "80" in output
    assert latest_run() is None


def test_plan_lets_every_fighter_run_at_once_when_memory_allows(capsys, monkeypatch):
    monkeypatch.setattr(cli, "free_memory_mb", lambda: 64_000)

    cli.main(["plan"])

    output = capsys.readouterr().out
    assert "2 groups of 4" in output
    assert "up to 8 agents run at once" in output


def test_plan_caps_agents_when_memory_is_short(capsys, monkeypatch):
    monkeypatch.setattr(cli, "free_memory_mb", lambda: 2_012)

    cli.main(["plan"])

    assert "up to 3 agents run at once" in capsys.readouterr().out


def test_at_once_overrides_the_memory_check(capsys, monkeypatch):
    monkeypatch.setattr(cli, "free_memory_mb", lambda: 64_000)

    cli.main(["plan", "--at-once", "2"])

    assert "up to 2 agents run at once" in capsys.readouterr().out


def test_at_once_is_saved_so_resume_keeps_it(project, capsys):
    run_battle(project, "--at-once", "2")
    assert RunStore.open(latest_run()).settings["at_once"] == 2


def test_at_once_must_be_at_least_one(capsys):
    assert cli.main(["plan", "--at-once", "0"]) == 1
    assert "at-once" in capsys.readouterr().err


def test_plan_accepts_a_custom_size(capsys):
    cli.main(["plan", "--groups", "3", "--fighters", "2"])
    assert "3 groups of 2" in capsys.readouterr().out


def test_plan_explains_impossible_sizes(capsys):
    assert cli.main(["plan", "--fighters", "1"]) == 1
    assert "at least 2 fighters" in capsys.readouterr().err


def test_run_finishes_a_real_battle_through_subprocess_agents(project, capsys):
    assert run_battle(project) == 0

    output = capsys.readouterr().out
    assert "Champion: A2" in output
    assert (project / "work_A2.txt").exists()
    assert (RunStore.open(latest_run()).root / "report.md").exists()


def test_web_is_on_by_default(project, capsys):
    run_battle(project)
    assert RunStore.open(latest_run()).settings["web"] is True


def test_no_web_turns_it_off(project, capsys):
    run_battle(project, "--no-web")
    assert RunStore.open(latest_run()).settings["web"] is False


def test_run_reads_the_task_from_a_file(tmp_path, project, capsys):
    task_file = tmp_path / "task.txt"
    task_file.write_text("task with `quotes` and $(danger)\n")

    code = cli.main(
        ["run", "--task-file", str(task_file), "--project", str(project)]
        + ["--agent", f"command:{FAKE}"]
        + SMALL
    )

    assert code == 0
    assert RunStore.open(latest_run()).task == "task with `quotes` and $(danger)"


def test_run_needs_a_task(project, capsys):
    assert cli.main(["run", "--project", str(project)]) == 1
    assert "task" in capsys.readouterr().err


def test_run_rejects_a_missing_project(tmp_path, capsys):
    code = cli.main(["run", "x", "--project", str(tmp_path / "nowhere")])
    assert code == 1
    assert "not a folder" in capsys.readouterr().err


def test_run_rejects_an_unknown_agent(project, capsys):
    code = cli.main(["run", "x", "--project", str(project), "--agent", "gemini"])
    assert code == 1
    assert "gemini" in capsys.readouterr().err


def test_run_checks_the_agent_is_installed_before_starting(project, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli.main(["run", "x", "--project", str(project), "--agent", "codex"]) == 1

    assert "codex" in capsys.readouterr().err
    assert latest_run() is None


def test_usage_limit_pauses_with_a_clear_next_step(tmp_path, project, capsys, monkeypatch):
    log = str(tmp_path / "limit.log")
    monkeypatch.setenv("FAKE_CONFIG", json.dumps({"log": log, "limit_after": 3}))

    assert run_battle(project) == cli.EXIT_PAUSED

    run_id = latest_run()
    assert f"battle resume {run_id}" in capsys.readouterr().out
    assert RunStore.open(run_id).status == "paused"
    assert not (project / "work_A2.txt").exists()


def test_resume_finishes_the_latest_paused_battle(tmp_path, project, capsys, monkeypatch):
    log = str(tmp_path / "limit.log")
    monkeypatch.setenv("FAKE_CONFIG", json.dumps({"log": log, "limit_after": 3}))
    run_battle(project)
    monkeypatch.setenv("FAKE_CONFIG", json.dumps({"log": str(tmp_path / "agent.log")}))

    assert cli.main(["resume"]) == 0

    assert (project / "work_A2.txt").exists()
    assert RunStore.open(latest_run()).status == "done"


def test_resume_refuses_a_finished_battle(project, capsys):
    run_battle(project)
    capsys.readouterr()

    assert cli.main(["resume"]) == 0
    assert "already finished" in capsys.readouterr().out


def test_resume_explains_when_there_is_nothing_to_resume(capsys):
    assert cli.main(["resume"]) == 1
    assert "No battles" in capsys.readouterr().err


def test_status_shows_progress(project, capsys):
    run_battle(project)
    capsys.readouterr()

    assert cli.main(["status"]) == 0

    output = capsys.readouterr().out
    assert "done" in output
    assert "add a feature" in output


def test_apply_after_no_apply_then_undo(project, capsys):
    run_battle(project, "--no-apply")
    assert not (project / "work_A2.txt").exists()

    assert cli.main(["apply"]) == 0
    assert (project / "work_A2.txt").exists()

    assert cli.main(["undo"]) == 0
    assert not (project / "work_A2.txt").exists()


def test_apply_can_run_again_after_undo(project, capsys):
    run_battle(project)
    cli.main(["undo"])

    assert cli.main(["apply"]) == 0
    assert (project / "work_A2.txt").exists()


def test_apply_lists_conflicts_and_force_overrides_them(project, capsys):
    run_battle(project, "--no-apply")
    (project / "work_A2.txt").write_text("mine\n")
    capsys.readouterr()

    assert cli.main(["apply"]) == 1
    assert "work_A2.txt" in capsys.readouterr().err
    assert (project / "work_A2.txt").read_text() == "mine\n"

    assert cli.main(["apply", "--force"]) == 0
    assert "work by A2" in (project / "work_A2.txt").read_text()


def test_apply_twice_is_refused(project, capsys):
    run_battle(project)
    capsys.readouterr()

    assert cli.main(["apply"]) == 1
    assert "already" in capsys.readouterr().err


def test_undo_explains_when_nothing_was_applied(project, capsys):
    run_battle(project, "--no-apply")
    capsys.readouterr()

    assert cli.main(["undo"]) == 1
    assert "not applied" in capsys.readouterr().err


def test_apply_before_a_champion_exists_is_explained(tmp_path, project, capsys, monkeypatch):
    log = str(tmp_path / "limit.log")
    monkeypatch.setenv("FAKE_CONFIG", json.dumps({"log": log, "limit_after": 3}))
    run_battle(project)
    capsys.readouterr()

    assert cli.main(["apply"]) == 1
    assert "no champion" in capsys.readouterr().err


def test_launcher_works_through_a_symlink_from_any_folder(tmp_path):
    link = tmp_path / "battle"
    link.symlink_to(ROOT / "bin" / "battle")

    result = subprocess.run(
        [str(link), "plan"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=os.environ,
    )

    assert result.returncode == 0, result.stderr
    assert "2 groups of 4" in result.stdout


HOST = ["--agent", "host"]


@pytest.fixture
def in_project(project, monkeypatch):
    monkeypatch.delenv("BATTLE_HOME", raising=False)
    monkeypatch.chdir(project)
    return project


def prepare(project, *extra):
    return cli.main(["run", "add a feature", "--project", str(project), "--prepare"] + HOST + list(extra))


def test_prepare_creates_a_host_run_inside_the_project_without_starting(in_project, capsys):
    assert prepare(in_project) == 0

    output = capsys.readouterr().out
    run_id = latest_run()
    assert f"Run: {run_id}" in output
    store = RunStore.open(run_id)
    assert store.root.parent == in_project / ".battle" / "runs"
    assert store.status == "prepared"


def test_prepare_keeps_the_battle_folder_out_of_git(in_project, capsys):
    subprocess.run(["git", "init", "-q"], cwd=in_project, check=True)

    prepare(in_project)
    prepare(in_project)

    exclude = (in_project / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".battle/") == 1
    assert ".battle" not in subprocess.run(
        ["git", "status", "--porcelain"], cwd=in_project, capture_output=True, text=True
    ).stdout


def test_host_refuses_shell_access(in_project, capsys):
    assert prepare(in_project, "--allow-bash") == 1
    assert "--allow-bash" in capsys.readouterr().err


def test_host_refuses_no_web(in_project, capsys):
    assert prepare(in_project, "--no-web") == 1
    assert "--no-web" in capsys.readouterr().err


def test_host_refuses_to_run_without_a_chat_in_a_plain_run(in_project, capsys):
    code = cli.main(["run", "x", "--project", str(in_project)] + HOST)
    assert code == 1
    assert "--prepare" in capsys.readouterr().err


def test_host_never_asks_for_more_than_twenty_at_once(in_project, capsys, monkeypatch):
    monkeypatch.setattr(cli, "free_memory_mb", lambda: 64_000)
    prepare(in_project, "--groups", "5", "--fighters", "5")
    store = RunStore.open(latest_run())
    assert cli._limit(store.settings) == 20


def test_jobs_reports_a_prepared_run_and_no_jobs(in_project, capsys):
    prepare(in_project)
    run_id = latest_run()
    capsys.readouterr()

    assert cli.main(["jobs", run_id, "--wait-seconds", "0"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"run": run_id, "status": "prepared", "jobs": []}


def test_jobs_notices_a_dead_engine(in_project, capsys):
    prepare(in_project)
    store = RunStore.open(latest_run())
    store.set_status("running")
    capsys.readouterr()

    cli.main(["jobs", store.id, "--wait-seconds", "0"])

    assert json.loads(capsys.readouterr().out)["status"] == "stopped"


def test_jobs_returns_finished_status_with_the_report(project, capsys):
    run_battle(project)
    capsys.readouterr()

    cli.main(["jobs", "--wait-seconds", "0"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "done"
    assert payload["report"].endswith("report.md")


def test_reply_refuses_an_unknown_job(in_project, capsys, monkeypatch):
    prepare(in_project)
    capsys.readouterr()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("text"))

    assert cli.main(["reply", "0042"]) == 1
    assert "0042" in capsys.readouterr().err


def test_install_links_the_skill_and_both_agents(tmp_path, capsys, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))

    assert cli.main(["install"]) == 0

    assert (home / ".claude" / "skills" / "battle" / "SKILL.md").exists()
    assert (home / ".claude" / "agents" / "battle-fighter.md").is_symlink()
    assert (home / ".claude" / "agents" / "battle-judge.md").is_symlink()


def test_install_can_run_twice(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    cli.main(["install"])
    assert cli.main(["install"]) == 0


def test_install_refuses_to_replace_a_file_you_wrote(tmp_path, capsys, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))
    custom = home / ".claude" / "agents" / "battle-judge.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("mine")

    assert cli.main(["install"]) == 1

    assert custom.read_text() == "mine"
    assert "battle-judge.md" in capsys.readouterr().err


@pytest.mark.parametrize(
    "name, role, tools",
    [
        ("battle-fighter", "fighter", "Read, Edit, Write, Grep, Glob, WebSearch, WebFetch"),
        ("battle-judge", "judge", "Read, Grep, Glob, WebSearch, WebFetch"),
    ],
)
def test_agent_definitions_keep_their_tools_and_guard(name, role, tools):
    text = (ROOT / "agents" / f"{name}.md").read_text()
    assert f"tools: {tools}" in text
    assert f'battle-guard\\" {role}"' in text
    assert "Bash" not in text.split("---")[1].split("hooks:")[0]


def test_skill_carries_on_after_asking_for_a_missing_task():
    skill = (ROOT / "skill" / "SKILL.md").read_text()
    assert "continue from step 2" in skill


def test_skill_forbids_the_chat_from_doing_the_task_itself():
    skill = (ROOT / "skill" / "SKILL.md").read_text()
    assert "Never do the task yourself" in skill
