import json

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


def test_lock_stops_a_second_process_on_the_same_run(home, tmp_path):
    store = new_run(tmp_path)
    with store.lock():
        with pytest.raises(AlreadyRunning):
            with RunStore.open(store.id).lock():
                pass


def test_lock_is_released_after_use(home, tmp_path):
    store = new_run(tmp_path)
    with store.lock():
        pass
    with RunStore.open(store.id).lock():
        pass
