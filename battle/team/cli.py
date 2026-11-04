from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from battle.agents import DEFAULT_TIMEOUT, UsageLimitReached
from battle.cli import (
    EXIT_ERROR,
    EXIT_PAUSED,
    EXIT_SIGNAL_BASE,
    HOST_AGENT,
    HOST_LIMIT,
    ROOT,
    STOP_WAIT_SECONDS,
    _build_agents,
    _exclude_from_git,
    _fail,
    _finish_first,
    _jobs,
    _live_status,
    _open,
    _print_line,
    _read_task,
    _reply,
    _stop_on_signals,
    _wait_until,
)
from battle.host import reset_jobs, subagent_model
from battle.sizing import fighters_at_once, free_memory_mb
from battle.store import AlreadyRunning, RunNotFound, RunStore
from battle.team import report
from battle.team.pipeline import (
    ANGLES,
    DEFAULT_CHECK_TIMEOUT,
    DEFAULT_SECURITY_ROUNDS,
    FIGHTS,
    SKIPPABLE,
    SUBAGENTS,
    Pipeline,
    apply_site,
    estimate_runs,
    roll_back_cut_off_apply,
    undo_site,
)

ENGINE_WAIT_SECONDS = 10
STAGE_LABELS = {
    "pm": "PM writes the requirements",
    "research": "Research on the niche, competitors, trends and stacks",
    "design": "UI/UX fight: 2 designers, 1 judge, 1 polish turn",
    "dev": "Dev fight: 2 developers, 1 judge, 1 polish turn",
    "security": "Security: attackers, a verifier and a fixer per round",
}
SOLO_LABELS = {
    "design": "UI/UX solo: 1 designer, no judge",
    "dev": "Dev solo: 1 developer, no judge",
}


def _parse_angles(text: str | None) -> list[str]:
    if not text:
        return list(ANGLES)
    angles = [item.strip() for item in text.split(",") if item.strip()]
    unknown = sorted(set(angles) - set(ANGLES))
    if unknown:
        raise ValueError(f"Unknown attack angles: {', '.join(unknown)}. Use {', '.join(ANGLES)}.")
    if not angles:
        raise ValueError("Give at least one attack angle.")
    return list(dict.fromkeys(angles))


def _settings_from(args: argparse.Namespace) -> dict[str, Any]:
    if args.security_rounds < 1:
        raise ValueError("--security-rounds must be at least 1.")
    if not args.timeout > 0:
        raise ValueError("--timeout must be more than 0.")
    if not args.check_timeout > 0:
        raise ValueError("--check-timeout must be more than 0.")
    if args.at_once is not None and args.at_once < 1:
        raise ValueError("--at-once must be at least 1.")
    return {
        "skip": sorted(set(args.skip or [])),
        "solo": sorted(set(args.solo or [])),
        "angles": _parse_angles(args.angles),
        "security_rounds": args.security_rounds,
        "at_once": args.at_once,
        "timeout": args.timeout,
        "check_timeout": args.check_timeout,
    }


def _describe(settings: dict[str, Any]) -> None:
    counts = estimate_runs(settings)
    print("Stages and agent runs:")
    for stage, label in STAGE_LABELS.items():
        if stage not in counts:
            print(f"  {stage:<9}   0  skipped")
            continue
        if stage in settings["solo"]:
            label = SOLO_LABELS[stage]
        if stage == "security":
            angles = len(settings["angles"])
            rounds = settings["security_rounds"]
            label = (
                f"Security: {angles} attackers, 1 verifier and 1 fixer per round, "
                f"up to {rounds} rounds"
            )
        print(f"  {stage:<9} {counts[stage]:>3}  {label}")
    total = sum(counts.values())
    free_mb = free_memory_mb()
    parallel = max(2, len(settings["angles"]))
    at_once = settings.get("at_once") or fighters_at_once(parallel, free_mb)
    memory = "unknown" if free_mb is None else f"{free_mb} MB"
    print(f"Agent runs: up to {total}, all from your plan's allowance")
    print(f"Free memory with swap: {memory}, so up to {at_once} agents run at once")
    print(
        "Developers, judges, attackers and the fixer run shell commands, "
        "which are not sandboxed."
    )


def _limit(settings: dict[str, Any]) -> int:
    parallel = max(2, len(settings.get("angles") or ANGLES))
    limit = settings.get("at_once") or fighters_at_once(parallel, free_memory_mb())
    return min(limit, HOST_LIMIT) if HOST_AGENT in settings["agents"] else limit


def _record_end(store: RunStore, error: Exception, stop_signal: int | None) -> int:
    if stop_signal is not None:
        store.set_status("stopped")
        _print_line(f"Stopped. Continue with: team resume {store.id}")
        return EXIT_SIGNAL_BASE + stop_signal
    if isinstance(error, UsageLimitReached):
        store.set_status("paused")
        _print_line(f"Paused: {error}")
        _print_line("All finished work is saved.")
        _print_line(f"After the limit resets, run: team resume {store.id}")
        return EXIT_PAUSED
    store.set_status("failed")
    store.note("".join(traceback.format_exception(error)))
    _print_line(f"Failed: {str(error) or type(error).__name__}")
    _print_line(f"Finished work is saved. Retry with: team resume {store.id}")
    return EXIT_ERROR


def _show_result(store: RunStore, result: dict[str, Any]) -> None:
    security = store.steps.get("security")
    if security:
        open_count = len(security.get("open") or [])
        state = "no confirmed findings remain" if not open_count else f"{open_count} open findings"
        print(f"Security: {security['rounds']} round(s), {state}")
    if result["applied"]:
        print(f"Applied: {len(result['changed'])} changed, {len(result['deleted'])} deleted")
        print(f"Undo with: team undo {store.id}")
    elif result.get("conflicts"):
        print("Not applied. These files changed during the run:")
        print("\n".join(f"  {path}" for path in result["conflicts"]))
        print(f"Apply anyway with: team apply {store.id} --force")
    elif result.get("refused"):
        print(f"Not applied: {result['refused']}")
    else:
        print(f"Not applied. Apply with: team apply {store.id}")
    print(f"Report: {store.root / 'report.md'}")
    print(f"Documents: {store.root / 'docs'}")


def _start(store: RunStore) -> int:
    with store.lock():
        (store.root / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
        if store.status == "done":
            with _stop_on_signals(_finish_first):
                rolled_back = roll_back_cut_off_apply(store)
            print(f"Team run {store.id} already finished.")
            if rolled_back:
                print("An apply was cut off, so the project was put back.")
                print(f"Apply with: team apply {store.id}")
            return 0
        settings = store.settings
        agents = _build_agents(
            settings["agents"],
            settings.get("model"),
            False,
            settings.get("web", True),
            store.root / "jobs",
        )
        pipeline = Pipeline(store, agents, _limit(settings), _print_line)
        with _stop_on_signals(pipeline.stop):
            try:
                reset_jobs(store.root / "jobs")
                store.set_status("running")
                result = pipeline.run()
            except Exception as error:
                return _record_end(store, error, pipeline.stop_signal)
    _show_result(store, result)
    return EXIT_ERROR if result.get("conflicts") or result.get("refused") else 0


def _require_valid_host(args: argparse.Namespace) -> None:
    if args.no_web:
        raise ValueError("--no-web needs separate agents, so it can't use --agent host.")
    if not args.prepare:
        raise ValueError("--agent host needs --prepare, because a Claude Code chat runs its agents.")
    if args.model is None:
        raise ValueError("--agent host needs --model, or agents would run on the chat's model.")
    if subagent_model(args.model) is None:
        raise ValueError(f"A Claude Code chat can't start {args.model}.")


def _plan(args: argparse.Namespace) -> int:
    _describe(_settings_from(args))
    return 0


def _run(args: argparse.Namespace) -> int:
    task = _read_task(args)
    project = args.project.resolve()
    if not project.is_dir():
        raise ValueError(f"{project} is not a folder.")
    settings = _settings_from(args)
    specs = args.agent or ["claude"]
    host = HOST_AGENT in specs
    if host:
        _require_valid_host(args)
    _build_agents(specs, args.model, False, not args.no_web)
    settings |= {
        "agents": specs,
        "model": args.model,
        "web": not args.no_web,
        "apply": not args.no_apply,
        "keep": args.keep,
    }
    _describe(settings)
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


def _detach(store: RunStore) -> int:
    with (store.root / "engine.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "team"), "resume", store.id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    started = _wait_until(
        lambda: store.engine_running() or process.poll() is not None, ENGINE_WAIT_SECONDS
    )
    if not started or process.poll() not in (None, 0):
        raise ValueError(f"The run didn't start. See {store.root / 'engine.log'}")
    print(f"Started team run {store.id} in the background.")
    return 0


def _resume(args: argparse.Namespace) -> int:
    store = _open(args)
    return _detach(store) if args.detach else _start(store)


def _stop(args: argparse.Namespace) -> int:
    store = _open(args)
    if not store.engine_running():
        raise ValueError(f"Team run {store.id} is not running.")
    pid = int((store.root / "engine.pid").read_text(encoding="utf-8"))
    os.kill(pid, signal.SIGTERM)
    if not _wait_until(lambda: not store.engine_running(), STOP_WAIT_SECONDS):
        raise ValueError(f"Team run {store.id} didn't stop. Its process is {pid}.")
    print(f"Stopped. Continue with: team resume {store.id}")
    return 0


def _status(args: argparse.Namespace) -> int:
    store = _open(args)
    print(f"Team run: {store.id}")
    print(f"Status: {_live_status(store)}")
    print(f"Task: {store.task}")
    print(f"Project: {store.project}")
    total = sum(estimate_runs(store.settings).values())
    print(f"Progress: {len(store.steps)} of about {total} steps")
    for stage in FIGHTS:
        if store.has(f"{stage}/judge"):
            print(f"{stage.capitalize()} winner: {store.get(f'{stage}/judge')['winner'].upper()}")
    if store.has("security"):
        print(f"Open findings: {len(store.get('security').get('open') or [])}")
    return 0


def _apply(args: argparse.Namespace) -> int:
    store = _open(args)
    if not (store.root / "champion").exists():
        raise ValueError("This run has no finished site yet.")
    with store.lock(), _stop_on_signals(_finish_first):
        outcome = apply_site(store, force=args.force)
    if outcome["applied"]:
        print(f"Applied: {len(outcome['changed'])} changed, {len(outcome['deleted'])} deleted")
        return 0
    if outcome.get("refused"):
        return _fail(outcome["refused"])
    files = ", ".join(outcome["conflicts"])
    return _fail(f"These files changed during the run: {files}. Use --force to overwrite them.")


def _undo(args: argparse.Namespace) -> int:
    store = _open(args)
    with store.lock(), _stop_on_signals(_finish_first):
        recorded = store.steps.get("result", {}).get("applied")
        if not recorded and not (store.root / "backup").exists():
            raise ValueError("This run's site is not applied.")
        undo_site(store)
    print("Project restored.")
    return 0


def _report(args: argparse.Namespace) -> int:
    store = _open(args)
    print(report.render(store), end="")
    return 0


def _install(args: argparse.Namespace) -> int:
    claude = Path.home() / ".claude"
    links = {claude / "skills" / "team": ROOT / "team"}
    for name in sorted(set(SUBAGENTS.values())):
        links[claude / "agents" / f"{name}.md"] = ROOT / "agents" / f"{name}.md"
    taken = [str(link) for link in links if link.exists() and not link.is_symlink()]
    if taken:
        raise ValueError("Refusing to replace: " + ", ".join(taken))
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    print("Installed /team and its agents. Open a new Claude Code session to use them.")
    return 0


def _add_stage_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skip", action="append", choices=SKIPPABLE, help="leave a stage out")
    parser.add_argument(
        "--solo", action="append", choices=FIGHTS, help="one entrant instead of a fight"
    )
    parser.add_argument("--angles", help="comma-separated attack angles: " + ", ".join(ANGLES))
    parser.add_argument("--security-rounds", type=int, default=DEFAULT_SECURITY_ROUNDS)
    parser.add_argument("--at-once", type=int, help="most agents to run at the same time")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per agent run"
    )
    parser.add_argument(
        "--check-timeout",
        type=float,
        default=DEFAULT_CHECK_TIMEOUT,
        help="seconds per install, build or test command",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="team",
        description="A team of AI agents builds a website: PM, research, design and dev fights, "
        "then attackers and a fixer.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="show the stages and cost without running anything")
    _add_stage_options(plan)
    plan.set_defaults(handler=_plan)

    run = commands.add_parser("run", help="start a team run")
    run.add_argument("task", nargs="?")
    run.add_argument("--task-file", type=Path, help="read the prompt from a file, or - for stdin")
    run.add_argument("--project", type=Path, default=Path("."))
    _add_stage_options(run)
    run.add_argument(
        "--agent",
        action="append",
        help="claude, codex, host or command:<cmd>. Repeat it to give side B a different agent",
    )
    run.add_argument("--model")
    run.add_argument("--no-web", action="store_true", help="turn off web search and page fetching")
    run.add_argument("--no-apply", action="store_true", help="keep the site out of the project")
    run.add_argument("--prepare", action="store_true", help="create the run without starting it")
    run.add_argument("--keep", action="store_true", help="keep every workspace after the run")
    run.set_defaults(handler=_run)

    resume = commands.add_parser("resume", help="start or continue a run")
    resume.add_argument("id", nargs="?", help="run id, or the latest one if left out")
    resume.add_argument("--detach", action="store_true", help="run on its own in the background")
    resume.set_defaults(handler=_resume)

    for name, handler, description in (
        ("stop", _stop, "stop a running team"),
        ("status", _status, "show a run's progress"),
        ("report", _report, "print a run's report"),
        ("undo", _undo, "restore the project from before the site was applied"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("id", nargs="?", help="run id, or the latest one if left out")
        command.set_defaults(handler=handler)

    jobs = commands.add_parser("jobs", help="hand waiting jobs to the chat as JSON")
    jobs.add_argument("id", nargs="?", help="run id, or the latest one if left out")
    jobs.add_argument("--wait-seconds", type=float, default=100, help="how long to wait for a job")
    jobs.add_argument("--group", help=argparse.SUPPRESS)
    jobs.set_defaults(handler=_jobs)

    reply = commands.add_parser("reply", help="send a subagent's final message from stdin")
    reply.add_argument("job")
    reply.add_argument("id", nargs="?", help="run id, or the latest one if left out")
    reply.add_argument("--failed", action="store_true", help="the subagent failed")
    reply.add_argument("--usage-limit", action="store_true", help="the plan's limit was reached")
    reply.set_defaults(handler=_reply)

    install = commands.add_parser("install", help="link /team and its agents into ~/.claude")
    install.set_defaults(handler=_install)

    apply = commands.add_parser("apply", help="apply the finished site to the project")
    apply.add_argument("id", nargs="?", help="run id, or the latest one if left out")
    apply.add_argument("--force", action="store_true", help="overwrite files that changed")
    apply.set_defaults(handler=_apply)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ValueError, OSError, RunNotFound, AlreadyRunning) as error:
        return _fail(str(error))
