from __future__ import annotations

from typing import Any

from battle.prompts import CATEGORIES
from battle.store import RunStore, write_durable


def _group_section(group: str, verdict: dict[str, Any], direction: str) -> list[str]:
    heading = f"### Group {group}: {direction}" if direction else f"### Group {group}"
    lines = [heading, "", f"Winner: **{verdict['winner']}**", ""]
    if verdict.get("reason"):
        lines += [str(verdict["reason"]), ""]
    if verdict.get("scores"):
        lines += ["| Fighter | " + " | ".join(CATEGORIES) + " | Total |"]
        lines += ["|---" * (len(CATEGORIES) + 2) + "|"]
        for fighter, scores in verdict["scores"].items():
            values = [scores[category] for category in CATEGORIES]
            cells = " | ".join(str(value) for value in [*values, sum(values)])
            lines.append(f"| {fighter} | {cells} |")
        lines.append("")
    return lines


def _final_section(steps: dict[str, Any]) -> list[str]:
    table = {
        category: steps[f"verdict/{category}"]["scores"]
        for category in CATEGORIES
        if f"verdict/{category}" in steps
    }
    if len(table) != len(CATEGORIES):
        return []
    finalists = list(table[CATEGORIES[0]])
    lines = ["## Final scores", "", "| Category | " + " | ".join(finalists) + " |"]
    lines.append("|---" * (len(finalists) + 1) + "|")
    for category in CATEGORIES:
        cells = " | ".join(str(table[category][fighter]) for fighter in finalists)
        lines.append(f"| {category} | {cells} |")
    totals = " | ".join(
        str(sum(table[category][fighter] for category in CATEGORIES)) for fighter in finalists
    )
    return [*lines, f"| **Total** | {totals} |", ""]


def _result_section(result: dict[str, Any]) -> list[str]:
    if result.get("applied"):
        changed = ", ".join(result["changed"]) or "none"
        deleted = ", ".join(result["deleted"]) or "none"
        return [f"Applied to the project. Changed: {changed}. Deleted: {deleted}.", ""]
    if result.get("conflicts"):
        files = ", ".join(result["conflicts"])
        return [f"Not applied. These files changed during the battle: {files}.", ""]
    reason = result.get("refused") or result.get("reason", "")
    return [f"Not applied. {reason}".strip(), ""]


def _overturned(steps: dict[str, Any]) -> list[str]:
    rulings = steps.get("head-check", {}).get("rulings", {})
    lines = []
    for group, ruling in rulings.items():
        original = steps.get(f"group-{group}/judge", {}).get("winner")
        if original and ruling["winner"] != original:
            lines.append(
                f"- Group {group}: {original} replaced by {ruling['winner']}. "
                f"{ruling.get('reason', '')}"
            )
    return lines


def _polish_section(steps: dict[str, Any]) -> list[str]:
    fixes = steps.get("champion", {}).get("fixes") or []
    if not fixes:
        return []
    lines = ["## Fixes after judging", "", *(f"- {fix}" for fix in fixes), ""]
    polish = steps.get("polish", {})
    if polish.get("skipped"):
        lines += [f"Not made: {polish['skipped']}", ""]
    elif polish.get("summary"):
        lines += [str(polish["summary"]), ""]
    return lines


def render(store: RunStore) -> str:
    steps = store.steps
    directions = [item["name"] for item in steps.get("directions", {}).get("directions", [])]
    verdicts = sorted(
        key for key in steps if key.startswith("group-") and key.endswith("/judge")
    )
    lines = [f"# Battle {store.id}", "", f"**Task:** {store.task}", ""]

    if "champion" in steps:
        champion = steps["champion"]
        lines += [f"## Champion: {champion['champion']}", "", str(champion.get("reason", "")), ""]
    if "result" in steps:
        lines += _result_section(steps["result"])
    lines += _polish_section(steps)

    lines += _final_section(steps)

    if verdicts:
        lines += ["## Group verdicts", ""]
    for key in verdicts:
        group = key.removeprefix("group-").removesuffix("/judge")
        index = ord(group) - ord("A")
        direction = directions[index] if index < len(directions) else ""
        lines += _group_section(group, steps[key], direction)

    overturned = _overturned(steps)
    if overturned:
        lines += ["## Overturned by the head judge", "", *overturned, ""]
    runner_up = steps.get("head-check", {}).get("runner_up")
    if runner_up:
        lines += [f"Runner-up added to the final: {runner_up}", ""]

    forfeits = [
        f"- {key.rsplit('/', 1)[-1]} ({key.split('/')[0]}): {value['reason']}"
        for key, value in steps.items()
        if isinstance(value, dict) and value.get("forfeit")
    ]
    lines += ["## Forfeits", "", *(forfeits or ["None."]), ""]
    return "\n".join(lines)


def write(store: RunStore) -> None:
    write_durable(store.root / "report.md", render(store))
