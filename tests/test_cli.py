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
