import json
import os
import stat
import threading

import pytest

from battle.store import AlreadyRunning, RunNotFound, RunStore, latest_run


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BATTLE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def new_run(tmp_path):
    return RunStore.create("build a login page", tmp_path, {"groups": 2})


def test_create_saves_the_task_before_any_agent_runs(home, tmp_path):
    store = new_run(tmp_path)

    state = json.loads((home / "runs" / store.id / "state.json").read_text())
    assert state["task"] == "build a login page"
    assert state["project"] == str(tmp_path)
    assert state["settings"] == {"groups": 2}
    assert state["status"] == "running"
    assert state["steps"] == {}


def test_finished_steps_survive_a_new_process(home, tmp_path):
    store = new_run(tmp_path)
    store.put("group-A/r1/A1", {"summary": "done"})

    reopened = RunStore.open(store.id)

    assert reopened.has("group-A/r1/A1")
    assert reopened.get("group-A/r1/A1") == {"summary": "done"}
    assert not reopened.has("group-A/r1/A2")


def test_saving_leaves_no_temporary_files(home, tmp_path):
    store = new_run(tmp_path)
    store.put("directions", {"directions": []})

    names = {path.name for path in store.root.iterdir()}
    assert names == {"state.json"}


def test_open_rejects_an_unknown_run(home):
    with pytest.raises(RunNotFound):
        RunStore.open("missing")


def test_set_status_is_saved(home, tmp_path):
    store = new_run(tmp_path)
    store.set_status("paused")
    assert RunStore.open(store.id).status == "paused"


def test_latest_run_finds_the_newest(home, tmp_path):
    first = new_run(tmp_path)
    second = new_run(tmp_path)
    assert first.id != second.id
    assert latest_run() == second.id


def test_latest_run_is_none_without_runs(home):
    assert latest_run() is None


def test_engine_running_follows_the_lock(home, tmp_path):
    store = new_run(tmp_path)
    assert store.engine_running() is False
    with store.lock():
        assert RunStore.open(store.id).engine_running() is True
    assert store.engine_running() is False


def test_a_run_can_live_inside_a_project(tmp_path, monkeypatch):
    monkeypatch.delenv("BATTLE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    store = RunStore.create("task", project, {}, home=project / ".battle")

    assert store.root.parent == project / ".battle" / "runs"
    assert RunStore.open(store.id).id == store.id
    assert latest_run() == store.id


def test_lock_stops_a_second_process_on_the_same_run(home, tmp_path):
    store = new_run(tmp_path)
    with store.lock():
        with pytest.raises(AlreadyRunning):
            with RunStore.open(store.id).lock():
                pass


def test_taking_the_lock_picks_up_what_another_process_saved(home, tmp_path):
    stale = new_run(tmp_path)
    RunStore.open(stale.id).put("from-the-engine", {"done": True})

    with stale.lock():
        stale.put("from-undo", {"done": True})

    reopened = RunStore.open(stale.id)
    assert reopened.has("from-the-engine")
    assert reopened.has("from-undo")


def test_lock_is_released_after_use(home, tmp_path):
    store = new_run(tmp_path)
    with store.lock():
        pass
    with RunStore.open(store.id).lock():
        pass


def test_a_passing_status_check_does_not_look_like_a_running_battle(home, tmp_path):
    store = new_run(tmp_path)
    holder = RunStore.open(store.id)
    entered, release = threading.Event(), threading.Event()

    def hold_briefly():
        with holder.lock():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold_briefly)
    thread.start()
    entered.wait(5)
    threading.Timer(0.05, release.set).start()

    with store.lock():
        assert release.is_set()
    thread.join()


def test_forget_removes_steps_for_good(home, tmp_path):
    store = new_run(tmp_path)
    store.put("group-A/r1/A1", {"forfeit": True})
    store.put("group-A/r1/A2", {"summary": "done"})

    store.forget(["group-A/r1/A1"])

    reopened = RunStore.open(store.id)
    assert not reopened.has("group-A/r1/A1")
    assert reopened.has("group-A/r1/A2")


def test_a_save_reaches_the_disk_before_it_replaces_the_old_state(home, tmp_path, monkeypatch):
    store = new_run(tmp_path)
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor):
        calls.append("fsync")
        real_fsync(descriptor)

    def replace(source, target):
        calls.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    store.set_status("paused")

    assert calls == ["fsync", "replace", "fsync"]


def test_a_run_id_cannot_climb_out_of_the_runs_folder(home, tmp_path):
    store = new_run(tmp_path)
    with pytest.raises(RunNotFound):
        RunStore.open(f"../runs/{store.id}")


def test_only_you_can_read_a_runs_copy_of_your_project(home, tmp_path):
    assert stat.S_IMODE(new_run(tmp_path).root.stat().st_mode) == 0o700
