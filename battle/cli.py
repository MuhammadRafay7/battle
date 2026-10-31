from __future__ import annotations

import argparse
import dataclasses
import functools
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from battle.agents import DEFAULT_TIMEOUT, Agent, AgentError, UsageLimitReached, make_agent
from battle.fight import BattleError, Fight, apply_champion, undo_champion
from battle.sizing import PRESETS, Bracket, fighters_at_once, free_memory_mb
from battle.store import AlreadyRunning, RunNotFound, RunStore, latest_run

EXIT_ERROR = 1
EXIT_PAUSED = 75
EXIT_INTERRUPTED = 130
INSTALLED_AGENTS = ("claude", "codex")


def _fail(message: str) -> int:
    print(f"battle: {message}", file=sys.stderr)
    return EXIT_ERROR


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


def _describe(bracket: Bracket, requested: int | None) -> None:
    free_mb = free_memory_mb()
    at_once = requested or fighters_at_once(bracket.total_fighters, free_mb)
    memory = "unknown" if free_mb is None else f"{free_mb} MB"
    print(
        f"Size: {bracket.groups} groups of {bracket.fighters} "
        f"({bracket.total_fighters} fighters)"
    )
    print(f"Rounds: {bracket.rounds} per group, {bracket.final_rounds} in the final")
    print(f"Free memory with swap: {memory}, so up to {at_once} agents run at once")
    print(f"Agent runs: about {bracket.estimated_runs()}, all from your plan's allowance")


def _build_agents(
    specs: list[str], model: str | None, allow_bash: bool, web: bool
) -> list[Agent]:
    agents = [make_agent(spec, model, allow_bash, web) for spec in specs]
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
    else:
        print(f"Not applied. Apply with: battle apply {store.id}")
    print(f"Report: {store.root / 'report.md'}")


def _start(store: RunStore) -> int:
    settings = store.settings
    agents = _build_agents(
        settings["agents"],
        settings.get("model"),
        settings["allow_bash"],
        settings.get("web", False),
    )
    fighters = settings["groups"] * settings["fighters"]
    limit = settings.get("at_once") or fighters_at_once(fighters, free_memory_mb())
    log = functools.partial(print, flush=True)
    with store.lock():
        store.set_status("running")
        try:
            result = Fight(store, agents, limit, log).run()
        except UsageLimitReached as error:
            store.set_status("paused")
            print(f"Paused: {error}")
            print("All finished work is saved.")
            print(f"After the limit resets, run: battle resume {store.id}")
            return EXIT_PAUSED
        except (BattleError, AgentError) as error:
            store.set_status("failed")
            print(f"Stopped: {error}")
            print(f"Finished work is saved. Retry with: battle resume {store.id}")
            return EXIT_ERROR
        except KeyboardInterrupt:
            store.set_status("paused")
            print(f"Stopped. Continue with: battle resume {store.id}")
            return EXIT_INTERRUPTED
    _show_result(store, result)
    return 0 if result["applied"] or not result.get("conflicts") else EXIT_ERROR


def _plan(args: argparse.Namespace) -> int:
    _require_valid_at_once(args)
    _describe(_bracket(args), args.at_once)
    return 0


def _run(args: argparse.Namespace) -> int:
    task = _read_task(args)
    project = args.project.resolve()
    if not project.is_dir():
        raise ValueError(f"{project} is not a folder.")
    bracket = _bracket(args)
    _require_valid_at_once(args)
    specs = args.agent or ["claude"]
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
    return _start(RunStore.create(task, project, settings))


def _resume(args: argparse.Namespace) -> int:
    store = _open(args)
    if store.status == "done":
        print(f"Battle {store.id} already finished.")
        return 0
    return _start(store)


def _status(args: argparse.Namespace) -> int:
    store = _open(args)
    bracket = Bracket(*(store.settings[key] for key in ("groups", "fighters", "rounds", "final_rounds")))
    print(f"Battle: {store.id}")
    print(f"Status: {store.status}")
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
    outcome = apply_champion(store, force=args.force)
    if outcome["applied"]:
        print(f"Applied: {len(outcome['changed'])} changed, {len(outcome['deleted'])} deleted")
        return 0
    files = ", ".join(outcome["conflicts"])
    return _fail(f"These files changed during the battle: {files}. Use --force to overwrite them.")


def _undo(args: argparse.Namespace) -> int:
    store = _open(args)
    if not (store.root / "backup").exists():
        raise ValueError("This battle's champion is not applied.")
    undo_champion(store)
    print("Project restored.")
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
        help="claude, codex or command:<cmd>. Repeat it to mix agents",
    )
    run.add_argument("--model")
    run.add_argument(
        "--allow-bash",
        action="store_true",
        help="let claude fighters run shell commands, which are not sandboxed",
    )
    run.add_argument("--no-web", action="store_true", help="turn off web search and page fetching")
    run.add_argument("--no-apply", action="store_true", help="keep the champion out of the project")
    run.add_argument("--keep", action="store_true", help="keep every entry after the battle")
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per agent run")
    run.set_defaults(handler=_run)

    for name, handler, description in (
        ("resume", _resume, "continue a paused or failed battle"),
        ("status", _status, "show a battle's progress"),
        ("undo", _undo, "restore the project from before the champion was applied"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
        command.set_defaults(handler=handler)

    apply = commands.add_parser("apply", help="apply the champion's work to the project")
    apply.add_argument("id", nargs="?", help="battle id, or the latest one if left out")
    apply.add_argument("--force", action="store_true", help="overwrite files that changed")
    apply.set_defaults(handler=_apply)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, RunNotFound, AlreadyRunning) as error:
        return _fail(str(error))
