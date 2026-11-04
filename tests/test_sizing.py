import pytest

from battle.sizing import (
    AGENT_MB,
    RESERVED_MB,
    Bracket,
    PRESETS,
    fighters_at_once,
    parse_meminfo,
    parse_vm_stat,
)


def test_presets_match_the_battle_sizes():
    assert PRESETS["default"] == Bracket(groups=1, fighters=3, rounds=1)
    assert PRESETS["tournament"] == Bracket(groups=2, fighters=4)
    assert PRESETS["big"] == Bracket(groups=4, fighters=5)


def test_estimated_runs_counts_every_agent_in_the_tournament():
    assert PRESETS["default"].estimated_runs() == 3 + 1 + 1
    assert PRESETS["tournament"].estimated_runs() == 29
    assert PRESETS["big"].estimated_runs() == 57


def test_one_group_skips_directions_head_check_and_final():
    bracket = Bracket(groups=1, fighters=3, rounds=2, final_rounds=2)
    assert bracket.finalists == 1
    assert bracket.estimated_runs() == 6 + 1 + 1


@pytest.mark.parametrize(
    "changes",
    [{"groups": 0}, {"groups": 27}, {"fighters": 1}, {"rounds": 0}, {"final_rounds": 0}],
)
def test_bracket_rejects_sizes_that_cannot_fight(changes):
    values = {"groups": 2, "fighters": 4, "rounds": 3, "final_rounds": 2} | changes
    with pytest.raises(ValueError):
        Bracket(**values)


def test_fighters_at_once_follows_free_ram():
    assert fighters_at_once(5, RESERVED_MB + 4 * AGENT_MB) == 4


def test_fighters_at_once_never_exceeds_the_group():
    assert fighters_at_once(4, 64_000) == 4


def test_fighters_at_once_always_lets_one_run():
    assert fighters_at_once(5, 100) == 1


def test_fighters_at_once_uses_group_size_when_ram_is_unknown():
    assert fighters_at_once(5, None) == 5


def test_parse_meminfo_reads_available_memory():
    assert parse_meminfo("MemTotal: 7000000 kB\nMemAvailable: 2662400 kB\n") == 2600


def test_parse_meminfo_counts_free_swap():
    text = "MemAvailable: 2097152 kB\nSwapFree: 10485760 kB\n"
    assert parse_meminfo(text) == 2048 + 10240


def test_parse_meminfo_returns_none_without_available_memory():
    assert parse_meminfo("MemTotal: 7000000 kB\n") is None


def test_parse_vm_stat_counts_free_and_inactive_pages():
    output = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1000.\n"
        "Pages inactive:                           3000.\n"
    )
    assert parse_vm_stat(output) == 62
