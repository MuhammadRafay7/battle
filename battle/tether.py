from __future__ import annotations

import os
import signal
import sys
import time
from typing import Sequence

MARK_VARIABLE = "BATTLE_AGENT_MARK"
START_FAILED = "battle-tether: "
EXIT_CANNOT_START = 127
PROC = "/proc"
SWEEPS = 5
SWEEP_PAUSE_SECONDS = 0.05


def _carries(pid: str, entry: bytes) -> bool:
    try:
        with open(f"{PROC}/{pid}/environ", "rb") as environ:
            return entry in environ.read().split(b"\0")
    except OSError:
        return False


def _marked(mark: str) -> list[int]:
    if not os.path.isdir(PROC):
        return []
    entry = f"{MARK_VARIABLE}={mark}".encode()
    pids = (name for name in os.listdir(PROC) if name.isdigit() and int(name) != os.getpid())
    return [int(pid) for pid in pids if _carries(pid, entry)]


def kill_marked(mark: str) -> None:
    for _ in range(SWEEPS):
        pids = _marked(mark)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(SWEEP_PAUSE_SECONDS)


def _watch(link: int, mark: str) -> None:
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, signal.SIG_IGN)
    os.read(link, 1)
    kill_marked(mark)
    os.killpg(0, signal.SIGKILL)


def _start_watcher(link: int, mark: str) -> None:
    child = os.fork()
    if child:
        os.waitpid(child, 0)
        return
    try:
        if not os.fork():
            _watch(link, mark)
    finally:
        os._exit(0)


def main(argv: Sequence[str]) -> int:
    link, mark, program = int(argv[0]), argv[1], list(argv[2:])
    _start_watcher(link, mark)
    os.close(link)
    for number in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(number, signal.SIG_DFL)
    try:
        os.execvp(program[0], program)
    except FileNotFoundError:
        reason = "is not installed or not on PATH."
    except OSError as error:
        reason = f"could not start: {error}"
    os.write(sys.stderr.fileno(), f"{START_FAILED}{reason}".encode())
    return EXIT_CANNOT_START


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
