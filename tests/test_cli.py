import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from battle import cli
from battle.store import RunStore, latest_run
from battle.workspace import apply_entry, undo_apply

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
    assert "57" in output
    assert latest_run() is None


def test_plan_lets_every_fighter_run_at_once_when_memory_allows(capsys, monkeypatch):
    monkeypatch.setattr(cli, "free_memory_mb", lambda: 64_000)

    cli.main(["plan"])

    output = capsys.readouterr().out
    assert "1 group of 3 fighters" in output
    assert "up to 3 agents run at once" in output
    assert "about 5" in output


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


def test_undo_finishes_when_a_crash_cut_it_off_before_it_was_saved(project, capsys):
    run_battle(project)
    store = RunStore.open(latest_run())
    undo_apply(store.root / "backup", project)

    assert cli.main(["undo"]) == 0

    assert RunStore.open(store.id).get("result")["applied"] is False


def test_resume_of_a_finished_battle_puts_back_a_cut_off_apply(project, capsys):
    run_battle(project, "--no-apply")
    root = RunStore.open(latest_run()).root
    apply_entry(root / "champion", root / "baseline", project, root / "backup")
    capsys.readouterr()

    assert cli.main(["resume"]) == 0

    assert "battle apply" in capsys.readouterr().out
    assert not (project / "work_A2.txt").exists()
    assert not (root / "backup").exists()


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
    assert "1 group of 3 fighters" in result.stdout


HOST = ["--agent", "host"]


@pytest.fixture
def in_project(project, monkeypatch):
    monkeypatch.delenv("BATTLE_HOME", raising=False)
    monkeypatch.chdir(project)
    return project


def prepare(project, *extra):
    return cli.main(
        ["run", "add a feature", "--project", str(project), "--prepare", "--model", "fable"]
        + HOST
        + list(extra)
    )


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


def test_host_needs_a_model_so_fighters_do_not_inherit_the_cheap_lead(in_project, capsys):
    code = cli.main(["run", "x", "--project", str(in_project), "--prepare"] + HOST)
    assert code == 1
    assert "--model" in capsys.readouterr().err


def test_host_refuses_a_model_the_chat_cannot_start(in_project, capsys):
    code = cli.main(
        ["run", "x", "--project", str(in_project), "--prepare", "--model", "gpt-9"] + HOST
    )
    assert code == 1
    assert "gpt-9" in capsys.readouterr().err


def test_jobs_for_a_group_says_when_that_group_is_done(in_project, capsys):
    prepare(in_project)
    store = RunStore.open(latest_run())
    jobs = store.root / "jobs"
    jobs.mkdir()
    capsys.readouterr()

    cli.main(["jobs", store.id, "--group", "A", "--wait-seconds", "0"])
    assert json.loads(capsys.readouterr().out)["group_done"] is False

    (jobs / "A.closed").touch()
    cli.main(["jobs", store.id, "--group", "A", "--wait-seconds", "0"])
    assert json.loads(capsys.readouterr().out)["group_done"] is True


def wait_for(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_detached_battle_outlives_the_command_that_started_it(project, capsys):
    run_battle(project, "--prepare")
    run_id = latest_run()

    assert cli.main(["resume", run_id, "--detach"]) == 0

    assert wait_for(lambda: RunStore.open(run_id).status == "done")
    assert (project / "work_A2.txt").exists()


def test_detach_returns_with_the_engine_already_running(project, capsys):
    code = cli.main(
        ["run", "x", "--project", str(project), "--agent", "command:sleep 30", "--prepare"]
    )
    assert code == 0
    run_id = latest_run()

    cli.main(["resume", run_id, "--detach"])
    try:
        assert RunStore.open(run_id).engine_running()
    finally:
        cli.main(["stop", run_id])


def test_stop_ends_a_detached_battle(project, capsys):
    cli.main(["run", "x", "--project", str(project), "--agent", "command:sleep 30", "--prepare"])
    run_id = latest_run()
    cli.main(["resume", run_id, "--detach"])

    assert cli.main(["stop", run_id]) == 0

    assert not RunStore.open(run_id).engine_running()
    assert RunStore.open(run_id).status == "stopped"


def test_stop_explains_when_nothing_is_running(project, capsys):
    run_battle(project)
    capsys.readouterr()

    assert cli.main(["stop"]) == 1
    assert "not running" in capsys.readouterr().err


def test_install_links_the_skill_and_both_agents(tmp_path, capsys, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))

    assert cli.main(["install"]) == 0

    assert (home / ".claude" / "skills" / "battle" / "SKILL.md").exists()
    assert (home / ".claude" / "agents" / "battle-fighter.md").is_symlink()
    assert (home / ".claude" / "agents" / "battle-judge.md").is_symlink()
    assert (home / ".claude" / "agents" / "battle-lead.md").is_symlink()


def test_install_can_run_twice(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    cli.main(["install"])
    assert cli.main(["install"]) == 0


def test_install_works_when_unzipped_straight_into_the_skills_folder(tmp_path):
    home = tmp_path / "user"
    unzipped = home / ".claude" / "skills" / "battle"
    shutil.copytree(
        ROOT, unzipped, ignore=shutil.ignore_patterns(".git", ".battle", "__pycache__")
    )

    result = subprocess.run(
        [sys.executable, str(unzipped / "bin" / "battle"), "install"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not unzipped.is_symlink()
    assert (unzipped / "SKILL.md").exists()
    fighter = home / ".claude" / "agents" / "battle-fighter.md"
    assert fighter.resolve() == unzipped / "agents" / "battle-fighter.md"


def test_install_refuses_to_replace_a_file_you_wrote(tmp_path, capsys, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))
    custom = home / ".claude" / "agents" / "battle-judge.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("mine")

    assert cli.main(["install"]) == 1

    assert custom.read_text() == "mine"
    assert "battle-judge.md" in capsys.readouterr().err


def test_lead_definition_is_cheap_and_guards_every_tool_it_has():
    text = (ROOT / "agents" / "battle-lead.md").read_text()
    assert "tools: Bash, Agent" in text
    assert "model: haiku" in text
    assert 'matcher: "Bash|Agent|Task"' in text
    assert 'battle-guard\\" lead"' in text


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
    skill = (ROOT / "SKILL.md").read_text()
    assert "continue from step 2" in skill


def test_skill_forbids_the_chat_from_doing_the_task_itself():
    skill = (ROOT / "SKILL.md").read_text()
    assert "Never do the task yourself" in skill


def test_skill_stops_leftover_agents_when_the_battle_ends():
    skill = (ROOT / "SKILL.md").read_text()
    assert "stop every agent still running" in skill


STUBBORN = """#!/bin/sh
cat > /dev/null
while true; do date >> "$BEAT"; sleep 0.05; done
"""


def start_stubborn_battle(tmp_path, project, beat, ignored=()):
    tool = tmp_path / "stubborn"
    tool.write_text(STUBBORN)
    tool.chmod(0o755)

    def reset_signals():
        for number in cli.STOP_SIGNALS:
            signal.signal(number, signal.SIG_IGN if number in ignored else signal.SIG_DFL)

    return subprocess.Popen(
        [str(ROOT / "bin" / "battle"), "run", "add a feature", "--project", str(project)]
        + ["--agent", "command:" + shlex.quote(str(tool))]
        + SMALL,
        env={**os.environ, "BEAT": str(beat)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=reset_signals,
    )


def wait_for_beat(beat):
    deadline = time.monotonic() + 20
    while not beat.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def assert_agent_is_dead(beat):
    time.sleep(0.2)
    size = beat.stat().st_size
    time.sleep(0.4)
    assert beat.stat().st_size == size


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_signal_kills_the_agents_and_records_a_truthful_stop(tmp_path, project, signum):
    beat = tmp_path / "beat"
    battle = start_stubborn_battle(tmp_path, project, beat)
    try:
        wait_for_beat(beat)
        battle.send_signal(signum)
        assert battle.wait(timeout=20) == 128 + signum
    finally:
        battle.kill()

    assert_agent_is_dead(beat)
    store = RunStore.open(latest_run())
    assert store.status == "stopped"
    assert not store.steps


def test_nohup_keeps_a_battle_alive_through_a_hangup(tmp_path, project):
    beat = tmp_path / "beat"
    battle = start_stubborn_battle(tmp_path, project, beat, ignored=(signal.SIGHUP,))
    try:
        wait_for_beat(beat)
        battle.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        assert battle.poll() is None
        battle.send_signal(signal.SIGTERM)
        assert battle.wait(timeout=20) == 128 + signal.SIGTERM
    finally:
        battle.kill()


def test_signal_handlers_are_put_back_after_a_battle():
    before = signal.getsignal(signal.SIGTERM)

    with cli._stop_on_signals(lambda signum: None):
        assert signal.getsignal(signal.SIGTERM) != before

    assert signal.getsignal(signal.SIGTERM) == before


def test_the_stop_path_runs_outside_the_signal_handler():
    stopped = []
    done = threading.Event()

    def stop(signum):
        stopped.append((signum, threading.current_thread()))
        done.set()

    with cli._stop_on_signals(stop):
        os.kill(os.getpid(), signal.SIGTERM)
        assert done.wait(5)

    assert stopped[0][0] == signal.SIGTERM
    assert stopped[0][1] is not threading.main_thread()


@pytest.mark.parametrize("error", [KeyError("bug"), ValueError("bad entry"), RuntimeError()])
def test_any_error_is_recorded_and_ends_with_the_retry_line(project, capsys, monkeypatch, error):
    def broken(_fight):
        raise error

    monkeypatch.setattr(cli.Fight, "run", broken)

    assert run_battle(project) == cli.EXIT_ERROR

    store = RunStore.open(latest_run())
    assert store.status == "failed"
    output = capsys.readouterr().out
    assert "Failed: " in output
    assert f"battle resume {store.id}" in output
    assert type(error).__name__ in (store.root / "log.txt").read_text()


def test_a_stop_is_never_recorded_as_a_failure_even_if_an_error_lands_with_it(project, monkeypatch):
    def stopped_then_broken(fight):
        fight.stop(signal.SIGTERM)
        raise KeyError("bug")

    monkeypatch.setattr(cli.Fight, "run", stopped_then_broken)

    assert run_battle(project) == 128 + signal.SIGTERM
    assert RunStore.open(latest_run()).status == "stopped"


def test_old_jobs_are_cleared_before_the_run_counts_as_running(project, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli, "reset_jobs", lambda _jobs: seen.append(RunStore.open(latest_run()).status)
    )
    run_battle(project, "--prepare")

    assert cli.main(["resume"]) == 0

    assert seen == ["prepared"]


def test_a_failure_clearing_old_jobs_is_recorded(project, capsys, monkeypatch):
    def broken(_jobs_dir):
        raise OSError("disk trouble")

    monkeypatch.setattr(cli, "reset_jobs", broken)

    assert run_battle(project) == cli.EXIT_ERROR
    assert RunStore.open(latest_run()).status == "failed"


def test_status_tells_the_truth_after_the_engine_died(project, capsys):
    run_battle(project)
    store = RunStore.open(latest_run())
    store.set_status("running")
    capsys.readouterr()

    assert cli.main(["status"]) == 0

    assert "Status: stopped" in capsys.readouterr().out


def test_undo_keeps_steps_saved_after_it_opened_the_run(project, capsys, monkeypatch):
    run_battle(project)
    store = RunStore.open(latest_run())
    real_lock = cli.RunStore.lock

    @contextlib.contextmanager
    def lock_after_a_late_save(locked):
        RunStore.open(locked.id).put("late", {"saved": True})
        with real_lock(locked):
            yield

    monkeypatch.setattr(cli.RunStore, "lock", lock_after_a_late_save)

    assert cli.main(["undo", store.id]) == 0

    assert RunStore.open(store.id).has("late")


def test_a_closed_stdout_cannot_stop_a_battle(monkeypatch):
    def broken(*_args, **_kwargs):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", broken)

    cli._print_line("still running")


def test_a_missing_task_file_is_explained(tmp_path, project, capsys):
    missing = tmp_path / "nowhere.txt"
    code = cli.main(["run", "--task-file", str(missing), "--project", str(project)])

    assert code == 1
    assert "nowhere.txt" in capsys.readouterr().err


def test_jobs_hands_out_nothing_once_the_battle_is_over(in_project, capsys):
    prepare(in_project)
    store = RunStore.open(latest_run())
    jobs_dir = store.root / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "0001.json").write_text(json.dumps({"id": "0001"}))
    store.set_status("stopped")
    capsys.readouterr()

    cli.main(["jobs", store.id, "--wait-seconds", "0"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stopped"
    assert payload["jobs"] == []
    assert not (jobs_dir / "0001.claimed").exists()


@pytest.mark.parametrize("command", ["apply", "undo"])
def test_apply_and_undo_wait_for_a_running_battle(project, capsys, command):
    run_battle(project, "--no-apply" if command == "apply" else "--keep")
    store = RunStore.open(latest_run())
    capsys.readouterr()

    with store.lock():
        assert cli.main([command]) == cli.EXIT_ERROR

    assert "already running" in capsys.readouterr().err


def test_a_resume_that_lost_the_race_to_the_finish_changes_nothing(project, capsys):
    run_battle(project)
    stale = RunStore.open(latest_run())
    stale._state["status"] = "failed"
    capsys.readouterr()

    assert cli._start(stale) == 0

    assert "already finished" in capsys.readouterr().out
    assert RunStore.open(stale.id).status == "done"


def test_a_battle_that_ends_during_the_check_is_not_called_stopped(project, capsys, monkeypatch):
    run_battle(project)
    store = RunStore.open(latest_run())
    store.set_status("running")
    stale = RunStore.open(store.id)
    store.set_status("done")
    monkeypatch.setattr(RunStore, "engine_running", lambda _store: False)

    assert cli._live_status(stale) == "done"


@pytest.mark.parametrize("command", ["apply", "undo"])
def test_a_signal_cannot_cut_an_apply_or_undo_in_half(project, capsys, monkeypatch, command):
    run_battle(project, "--no-apply")
    if command == "undo":
        cli.main(["apply"])
    target = "apply_champion" if command == "apply" else "undo_champion"
    real = getattr(cli, target)

    def interrupted(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, target, interrupted)

    assert cli.main([command]) == 0
    assert (project / "work_A2.txt").exists() is (command == "apply")


@pytest.mark.parametrize("seconds", ["0", "-1", "nan"])
def test_timeout_must_be_a_positive_number(project, capsys, seconds):
    code = cli.main(["run", "x", "--project", str(project), "--timeout", seconds])

    assert code == 1
    assert "--timeout" in capsys.readouterr().err
    assert latest_run() is None


def test_prepare_never_joins_the_battle_folder_onto_your_last_exclude_line(in_project, capsys):
    subprocess.run(["git", "init", "-q"], cwd=in_project, check=True)
    exclude = in_project / ".git" / "info" / "exclude"
    exclude.write_text("*.log")

    prepare(in_project)

    assert exclude.read_text().splitlines() == ["*.log", ".battle/"]


def test_a_symlink_in_the_way_ends_the_battle_with_the_champion_saved(tmp_path, project, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    (project / "work_A2.txt").symlink_to(outside)

    assert run_battle(project) == cli.EXIT_ERROR

    assert "symlink" in capsys.readouterr().out
    assert RunStore.open(latest_run()).status == "done"
    assert outside.read_text() == "keep me"

    assert cli.main(["apply"]) == cli.EXIT_ERROR
    assert "symlink" in capsys.readouterr().err

    (project / "work_A2.txt").unlink()
    assert cli.main(["apply"]) == 0
    assert "work by A2" in (project / "work_A2.txt").read_text()
