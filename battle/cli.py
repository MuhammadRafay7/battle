from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from battle.agents import DEFAULT_TIMEOUT, Agent, UsageLimitReached, make_agent
from battle.fight import Fight, apply_champion, roll_back_cut_off_apply, undo_champion
from battle.host import (
    HostAgent,
    claim_jobs,
    group_done,
    post_failure,
    post_reply,
    post_usage_limit,
    reset_jobs,
    subagent_model,
)
from battle.sizing import PRESETS, Bracket, fighters_at_once, free_memory_mb
from battle.store import AlreadyRunning, RunNotFound, RunStore, latest_run

EXIT_ERROR = 1
EXIT_PAUSED = 75
EXIT_SIGNAL_BASE = 128
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
WATCH_DONE = 0
INSTALLED_AGENTS = ("claude", "codex")
HOST_AGENT = "host"
HOST_LIMIT = 20
ROOT = Path(__file__).resolve().parent.parent
WAITING_STATUSES = ("prepared", "running")
ENGINE_WAIT_SECONDS = 10
STOP_WAIT_SECONDS = 30


def _fail(message: str) -> int:
    print(f"battle: {message}", file=sys.stderr)
    return EXIT_ERROR


def _print_line(message: str) -> None:
    with contextlib.suppress(OSError):
        print(message, flush=True)


@contextlib.contextmanager
def _stop_on_signals(stop: Callable[[int], None]) -> Iterator[None]:
    read_end, write_end = os.pipe()

    def notice(signum: int, _frame: Any) -> None:
        os.write(write_end, bytes([signum]))

    def watch() -> None:
        while (signum := os.read(read_end, 1)[0]) != WATCH_DONE:
            stop(signum)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    earlier = {
        number: signal.signal(number, notice)
        for number in STOP_SIGNALS
        if signal.getsignal(number) != signal.SIG_IGN
    }
    try:
        yield
    finally:
        for number, handler in earlier.items():
            signal.signal(number, handler)
        os.write(write_end, bytes([WATCH_DONE]))
        watcher.join()
        os.close(read_end)
        os.close(write_end)


def _finish_first(_signum: int) -> None:
    pass


def _bracket(args: argparse.Namespace) -> Bracket:
    overrides = {
        field: getattr(args, field)
        for field in ("groups", "fighters", "rounds", "final_rounds")
        if getattr(args, field) is not None
    }
    return dataclasses.replace(PRESETS[args.size], **overrides)


def _require_valid_at_once(args: argparse.Namespace) -> None:
    if args.at_once is not None and args.at_once < 1:
        raise ValueError("--at-once must be at least 1.")


def _require_valid_timeout(args: argparse.Namespace) -> None:
    if not args.timeout > 0:
        raise ValueError("--timeout must be more than 0.")


def _describe(bracket: Bracket, requested: int | None) -> None:
    free_mb = free_memory_mb()
    at_once = requested or fighters_at_once(bracket.total_fighters, free_mb)
    memory = "unknown" if free_mb is None else f"{free_mb} MB"
    if bracket.groups == 1:
        print(f"Size: 1 group of {bracket.fighters} fighters")
    else:
        print(
            f"Size: {bracket.groups} groups of {bracket.fighters} "
            f"({bracket.total_fighters} fighters)"
        )
    if bracket.groups == 1:
        print(f"Rounds: {bracket.rounds}, then one judge and a polish turn for the winner")
    else:
        print(f"Rounds: {bracket.rounds} per group, {bracket.final_rounds} in the final")
    print(f"Free memory with swap: {memory}, so up to {at_once} agents run at once")
    print(f"Agent runs: about {bracket.estimated_runs()}, all from your plan's allowance")


def _build_agents(
    specs: list[str],
    model: str | None,
    allow_bash: bool,
    web: bool,
    jobs_dir: Path = Path("jobs"),
) -> list[Agent]:
    agents: list[Agent] = [
        HostAgent(jobs_dir, model=model, web=web)
        if spec == HOST_AGENT
        else make_agent(spec, model, allow_bash, web)
        for spec in specs
    ]
    for spec in specs:
        if spec in INSTALLED_AGENTS and shutil.which(spec) is None:
            raise ValueError(f"{spec} is not installed or not on PATH.")
    return agents


def _read_task(args: argparse.Namespace) -> str:
    if args.task_file is None:
        task = args.task or ""
    elif str(args.task_file) == "-":
        task = sys.stdin.read()
    else:
        task = args.task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise ValueError("Give a task, for example: battle run \"add a login page\"")
    return task.strip()


def _open(args: argparse.Namespace) -> RunStore:
    run_id = args.id or latest_run()
    if run_id is None:
        raise ValueError("No battles yet.")
    return RunStore.open(run_id)


def _show_result(store: RunStore, result: dict[str, Any]) -> None:
    print(f"Champion: {result['champion']}")
    if result["applied"]:
        print(f"Applied: {len(result['changed'])} changed, {len(result['deleted'])} deleted")
        print(f"Undo with: battle undo {store.id}")
    elif result.get("conflicts"):
        print("Not applied. These files changed during the battle:")
        print("\n".join(f"  {path}" for path in result["conflicts"]))
        print(f"Apply anyway with: battle apply {store.id} --force")
    elif result.get("refused"):
        print(f"Not applied: {result['refused']}")
    else:
        print(f"Not applied. Apply with: battle apply {store.id}")
    print(f"Report: {store.root / 'report.md'}")


def _limit(settings: dict[str, Any]) -> int:
    fighters = settings["groups"] * settings["fighters"]
    limit = settings.get("at_once") or fighters_at_once(fighters, free_memory_mb())
    return min(limit, HOST_LIMIT) if HOST_AGENT in settings["agents"] else limit


def _record_end(store: RunStore, error: Exception, stop_signal: int | None) -> int:
    if stop_signal is not None:
        store.set_status("stopped")
        _print_line(f"Stopped. Continue with: battle resume {store.id}")
        return EXIT_SIGNAL_BASE + stop_signal
    if isinstance(error, UsageLimitReached):
        store.set_status("paused")
        _print_line(f"Paused: {error}")
        _print_line("All finished work is saved.")
        _print_line(f"After the limit resets, run: battle resume {store.id}")
        return EXIT_PAUSED
    store.set_status("failed")
    store.note("".join(traceback.format_exception(error)))
    _print_line(f"Failed: {str(error) or type(error).__name__}")
    _print_line(f"Finished work is saved. Retry with: battle resume {store.id}")
    return EXIT_ERROR


def _live_status(store: RunStore) -> str:
    if store.status != "running" or store.engine_running():
        return store.status
    latest = RunStore.open(store.id).status
    return "stopped" if latest == "running" else latest


def _start(store: RunStore) -> int:
    with store.lock():
        (store.root / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
        if store.status == "done":
            with _stop_on_signals(_finish_first):
                rolled_back = roll_back_cut_off_apply(store)
            print(f"Battle {store.id} already finished.")
            if rolled_back:
                print("An apply was cut off, so the project was put back.")
                print(f"Apply with: battle apply {store.id}")
            return 0
        settings = store.settings
        agents = _build_agents(
            settings["agents"],
            settings.get("model"),
            settings["allow_bash"],
            settings.get("web", False),
            store.root / "jobs",
        )
        fight = Fight(store, agents, _limit(settings), _print_line)
        with _stop_on_signals(fight.stop):
            try:
                reset_jobs(store.root / "jobs")
                store.set_status("running")
                result = fight.run()
            except Exception as error:
                return _record_end(store, error, fight.stop_signal)
    _show_result(store, result)
    return EXIT_ERROR if result.get("conflicts") or result.get("refused") else 0


def _plan(args: argparse.Namespace) -> int:
    _require_valid_at_once(args)
    _describe(_bracket(args), args.at_once)
    return 0


def _require_valid_host(args: argparse.Namespace) -> None:
    if args.allow_bash:
        raise ValueError("--allow-bash needs separate agents, so it can't use --agent host.")
    if args.no_web:
        raise ValueError("--no-web needs separate agents, so it can't use --agent host.")
    if not args.prepare:
        raise ValueError("--agent host needs --prepare, because a Claude Code chat runs its agents.")
    if args.model is None:
        raise ValueError("--agent host needs --model, or fighters would run on their lead's model.")
    if subagent_model(args.model) is None:
        raise ValueError(f"A Claude Code chat can't start {args.model}.")


def _exclude_from_git(project: Path) -> None:
    exclude = project / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".battle/" not in text.splitlines():
        separator = "\n" if text and not text.endswith("\n") else ""
        exclude.write_text(f"{text}{separator}.battle/\n", encoding="utf-8")


def _run(args: argparse.Namespace) -> int:
    task = _read_task(args)
    project = args.project.resolve()
    if not project.is_dir():
        raise ValueError(f"{project} is not a folder.")
    bracket = _bracket(args)
    _require_valid_at_once(args)
    _require_valid_timeout(args)
    specs = args.agent or ["claude"]
    host = HOST_AGENT in specs
    if host:
        _require_valid_host(args)
    _build_agents(specs, args.model, args.allow_bash, not args.no_web)
    settings = {
        **dataclasses.asdict(bracket),
        "agents": specs,
        "model": args.model,
        "allow_bash": args.allow_bash,
        "web": not args.no_web,
        "at_once": args.at_once,
        "apply": not args.no_apply,
        "keep": args.keep,
        "timeout": args.timeout,
    }
    _describe(bracket, args.at_once)
    home = project / ".battle" if host and "BATTLE_HOME" not in os.environ else None
    store = RunStore.create(task, project, settings, home=home)
    if not args.prepare:
        return _start(store)
    if host:
        _exclude_from_git(project)
    store.set_status("prepared")
    print(f"Run: {store.id}")
    print(f"Log: {store.root / 'log.txt'}")
    return 0


def _wait_until(ready: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not ready():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _detach(store: RunStore) -> int:
    with (store.root / "engine.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "battle"), "resume", store.id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    started = _wait_until(
        lambda: store.engine_running() or process.poll() is not None, ENGINE_WAIT_SECONDS
    )
    if not started or process.poll() not in (None, 0):
        raise ValueError(f"The battle didn't start. See {store.root / 'engine.log'}")
    print(f"Started battle {store.id} in the background.")
    return 0


def _resume(args: argparse.Namespace) -> int:
    store = _open(args)
    return _detach(store) if args.detach else _start(store)


def _stop(args: argparse.Namespace) -> int:
    store = _open(args)
    if not store.engine_running():
        raise ValueError(f"Battle {store.id} is not running.")
    pid = int((store.root / "engine.pid").read_text(encoding="utf-8"))
    os.kill(pid, signal.SIGTERM)
    if not _wait_until(lambda: not store.engine_running(), STOP_WAIT_SECONDS):
        raise ValueError(f"Battle {store.id} didn't stop. Its process is {pid}.")
    print(f"Stopped. Continue with: battle resume {store.id}")
    return 0


def _status(args: argparse.Namespace) -> int:
    store = _open(args)
    bracket = Bracket(*(store.settings[key] for key in ("groups", "fighters", "rounds", "final_rounds")))
    print(f"Battle: {store.id}")
    print(f"Status: {_live_status(store)}")
    print(f"Task: {store.task}")
    print(f"Project: {store.project}")
    print(f"Progress: {len(store.steps)} of about {bracket.estimated_runs()} steps")
    if store.has("champion"):
        print(f"Champion: {store.get('champion')['champion']}")
    return 0


def _apply(args: argparse.Namespace) -> int:
    store = _open(args)
    if not (store.root / "champion").exists():
        raise ValueError("This battle has no champion yet.")
    with store.lock(), _stop_on_signals(_finish_first):
        outcome = apply_champion(store, force=args.force)
    if outcome["applied"]:
        print(f"Applied: {len(outcome['changed'])} changed, {len(outcome['deleted'])} deleted")
        return 0
    if outcome.get("refused"):
        return _fail(outcome["refused"])
    files = ", ".join(outcome["conflicts"])
    return _fail(f"These files changed during the battle: {files}. Use --force to overwrite them.")


def _undo(args: argparse.Namespace) -> int:
    store = _open(args)
    with store.lock(), _stop_on_signals(_finish_first):
        recorded = store.steps.get("result", {}).get("applied")
        if not recorded and not (store.root / "backup").exists():
            raise ValueError("This battle's champion is not applied.")
        undo_champion(store)
    print("Project restored.")
    return 0


def _jobs(args: argparse.Namespace) -> int:
    run_id = _open(args).id
    deadline = time.monotonic() + args.wait_seconds
    while True:
        store = RunStore.open(run_id)
        jobs_dir = store.root / "jobs"
        status = _live_status(store)
        waiting = status in WAITING_STATUSES
        jobs = claim_jobs(jobs_dir, args.group) if waiting and jobs_dir.exists() else []
        done = bool(args.group) and group_done(jobs_dir, args.group)
        if jobs or done or not waiting or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    payload: dict[str, Any] = {"run": run_id, "status": status, "jobs": jobs}
    if args.group:
        payload["group_done"] = done
    if status == "done":
        payload["report"] = str(store.root / "report.md")
    print(json.dumps(payload, indent=2))
    return 0


def _reply(args: argparse.Namespace) -> int:
    jobs_dir = _open(args).root / "jobs"
    post = post_failure if args.failed else post_usage_limit if args.usage_limit else post_reply
    post(jobs_dir, args.job, sys.stdin.read())
    return 0


def _install(args: argparse.Namespace) -> int:
    claude = Path.home() / ".claude"
    links = {
        claude / "skills" / "battle": ROOT,
        claude / "agents" / "battle-fighter.md": ROOT / "agents" / "battle-fighter.md",
        claude / "agents" / "battle-judge.md": ROOT / "agents" / "battle-judge.md",
        claude / "agents" / "battle-lead.md": ROOT / "agents" / "battle-lead.md",
    }
    links = {
        link: target
        for link, target in links.items()
        if link.is_symlink() or not link.exists() or link.resolve() != target.resolve()
    }
    taken = [str(link) for link in links if link.exists() and not link.is_symlink()]
    if taken:
        raise ValueError("Refusing to replace: " + ", ".join(taken))
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    print("Installed /battle and its agents. Open a new Claude Code session to use them.")
    return 0


def _add_size_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--size", choices=sorted(PRESETS), default="default")
    parser.add_argument("--groups", type=int)
    parser.add_argument("--fighters", type=int, help="fighters in each group")
    parser.add_argument("--rounds", type=int, help="rounds in each group")
    parser.add_argument("--final-rounds", type=int)
    parser.add_argument("--at-once", type=int, help="most agents to run at the same time")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battle",
        description="AI agents fight over one task, and only the champion's work is kept.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="show a battle's size without running it")
    _add_size_options(plan)
    plan.set_defaults(handler=_plan)

    run = commands.add_parser("run", help="start a battle")
    run.add_argument("task", nargs="?")
    run.add_argument("--task-file", type=Path, help="read the task from a file, or - for stdin")
    run.add_argument("--project", type=Path, default=Path("."))
    _add_size_options(run)
    run.add_argument(
        "--agent",
        action="append",
        help="claude, codex, host or command:<cmd>. Repeat it to mix agents",
    )
    run.add_argument("--model")
    run.add_argument(
        "--allow-bash",
        action="store_true",
        help="let claude fighters run shell commands, which are not sandboxed",
    )
    run.add_argument("--no-web", action="store_true", help="turn off web search and page fetching")
    run.add_argument("--no-apply", action="store_true", help="keep the champion out of the project")
    run.add_argument("--prepare", action="store_true", help="create the battle without starting it")
    run.add_argument("--keep", action="store_true", help="keep every entry after the battle")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per agent run")
    run.set_defaults(handler=_run)

    resume = commands.add_parser("resume", help="start or continue a battle")
    resume.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
    resume.add_argument("--detach", action="store_true", help="run on its own in the background")
    resume.set_defaults(handler=_resume)

    for name, handler, description in (
        ("stop", _stop, "stop a running battle"),
        ("status", _status, "show a battle's progress"),
        ("undo", _undo, "restore the project from before the champion was applied"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
        command.set_defaults(handler=handler)

    jobs = commands.add_parser("jobs", help="hand waiting jobs to the chat as JSON")
    jobs.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
    jobs.add_argument("--wait-seconds", type=float, default=100, help="how long to wait for a job")
    jobs.add_argument("--group", help="only this group's jobs, for its lead")
    jobs.set_defaults(handler=_jobs)

    reply = commands.add_parser("reply", help="send a subagent's final message from stdin")
    reply.add_argument("job")
    reply.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
    reply.add_argument("--failed", action="store_true", help="the subagent failed")
    reply.add_argument("--usage-limit", action="store_true", help="the plan's limit was reached")
    reply.set_defaults(handler=_reply)

    install = commands.add_parser("install", help="link /battle and its agents into ~/.claude")
    install.set_defaults(handler=_install)

    apply = commands.add_parser("apply", help="apply the champion's work to the project")
    apply.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
    apply.add_argument("--force", action="store_true", help="overwrite files that changed")
    apply.set_defaults(handler=_apply)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, OSError, RunNotFound, AlreadyRunning) as error:
        return _fail(str(error))
