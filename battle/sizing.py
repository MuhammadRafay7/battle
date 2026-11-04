from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

AGENT_MB = 500
RESERVED_MB = 512
SPECIALIST_JUDGES = 5
MAX_GROUPS = 26


@dataclass(frozen=True)
class Bracket:
    groups: int
    fighters: int
    rounds: int = 2
    final_rounds: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.groups <= MAX_GROUPS:
            raise ValueError(f"A battle needs 1 to {MAX_GROUPS} groups.")
        if self.fighters < 2:
            raise ValueError("A group needs at least 2 fighters.")
        if self.rounds < 1:
            raise ValueError("A group needs at least 1 round.")
        if self.final_rounds < 1:
            raise ValueError("The final needs at least 1 round.")

    @property
    def total_fighters(self) -> int:
        return self.groups * self.fighters

    @property
    def finalists(self) -> int:
        return self.groups

    def estimated_runs(self) -> int:
        group_fights = self.total_fighters * self.rounds
        group_judges = self.groups
        polish = 1
        if self.groups == 1:
            return group_fights + group_judges + polish
        directions = 1
        head_check = 1
        final = self.finalists * self.final_rounds
        champion_pick = 1
        return (
            directions
            + group_fights
            + group_judges
            + head_check
            + final
            + SPECIALIST_JUDGES
            + champion_pick
            + polish
        )


PRESETS = {
    "default": Bracket(groups=1, fighters=3, rounds=1),
    "tournament": Bracket(groups=2, fighters=4),
    "big": Bracket(groups=4, fighters=5),
}


def fighters_at_once(fighters: int, free_mb: int | None) -> int:
    if free_mb is None:
        return fighters
    room = (free_mb - RESERVED_MB) // AGENT_MB
    return max(1, min(fighters, room))


def parse_meminfo(text: str) -> int | None:
    available = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    if available is None:
        return None
    swap = re.search(r"^SwapFree:\s+(\d+)\s+kB", text, re.MULTILINE)
    kilobytes = int(available.group(1)) + (int(swap.group(1)) if swap else 0)
    return kilobytes // 1024


def parse_vm_stat(text: str) -> int | None:
    page = re.search(r"page size of (\d+) bytes", text)
    free = re.search(r"Pages free:\s+(\d+)", text)
    inactive = re.search(r"Pages inactive:\s+(\d+)", text)
    if not (page and free and inactive):
        return None
    pages = int(free.group(1)) + int(inactive.group(1))
    return pages * int(page.group(1)) // (1024 * 1024)


def free_memory_mb() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        return parse_meminfo(meminfo.read_text())
    try:
        output = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_vm_stat(output)
