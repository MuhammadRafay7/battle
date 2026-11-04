from __future__ import annotations

from typing import Any

from battle.store import RunStore, write_durable

FIGHT_TITLES = {"design": "UI/UX design", "dev": "Development"}


def _fight_section(store: RunStore, stage: str) -> list[str]:
    key = f"{stage}/judge"
    if not store.has(key):
        return [f"## {FIGHT_TITLES[stage]}", "", "Skipped.", ""]
    verdict = store.get(key)
    winner = verdict["winner"]
    headline = f"Winner: {winner.upper()}. {verdict.get('reason', '')}".strip()
    lines = [f"## {FIGHT_TITLES[stage]}", "", headline, ""]
    scores = verdict.get("scores") or {}
    if scores:
        criteria = list(next(iter(scores.values())))
        lines.append("| Entrant | " + " | ".join(criteria) + " | Total |")
        lines.append("|---|" + "---|" * (len(criteria) + 1))
        for side, table in scores.items():
            values = [table[criterion] for criterion in criteria]
            cells = " | ".join(str(value) for value in values)
            lines.append(f"| {side.upper()} | {cells} | {sum(values)} |")
        lines.append("")
    acceptance = (verdict.get("acceptance") or {}).get(winner) or {}
    if acceptance:
        lines.append("Acceptance criteria for the winner:")
        lines += [f"- {criterion}: {state}" for criterion, state in acceptance.items()]
        lines.append("")
    fixes = verdict.get("fixes") or []
    if fixes:
        polish = store.steps.get(f"{stage}/polish", {})
        lines.append("Judge's fixes:")
        lines += [f"- {fix}" for fix in fixes]
        if polish.get("summary"):
            lines += ["", f"Polish: {polish['summary']}"]
        lines.append("")
    return lines


def _security_section(store: RunStore) -> list[str]:
    if not store.has("security"):
        return ["## Security", "", "Skipped.", ""]
    summary = store.get("security")
    lines = ["## Security", ""]
    for number in range(1, summary["rounds"] + 1):
        round_key = f"security/r{number}"
        if not store.has(round_key):
            continue
        data = store.get(round_key)
        served = "running site" if data.get("served") else "code only"
        lines.append(
            f"- Round {number}: {data['reported']} reported, "
            f"{len(data['confirmed'])} confirmed ({served})."
        )
        fix = store.steps.get(f"{round_key}/fix")
        if fix and not fix.get("forfeit"):
            lines.append(f"  Fixer: {fix['summary']}")
    lines.append("")
    open_findings = summary.get("open") or []
    if open_findings:
        lines.append(f"Open findings: {len(open_findings)}")
        for finding in open_findings:
            lines.append(f"- [{finding['severity']}] {finding['id']}: {finding['title']}")
            lines.append(f"  Repro: {finding['repro']}")
    else:
        lines.append("No confirmed findings remain.")
    lines.append("")
    return lines


def _result_section(store: RunStore) -> list[str]:
    result: dict[str, Any] = store.steps.get("result", {})
    lines = ["## Result", ""]
    if result.get("applied"):
        lines.append(
            f"Applied to the project: {len(result['changed'])} files changed, "
            f"{len(result['deleted'])} deleted."
        )
        lines.append(f"Undo with: team undo {store.id}")
    elif result.get("conflicts"):
        lines.append("Not applied. These files changed during the run:")
        lines += [f"- {path}" for path in result["conflicts"]]
        lines.append(f"Apply anyway with: team apply {store.id} --force")
    elif result.get("refused"):
        lines.append(f"Not applied: {result['refused']}")
    else:
        lines.append(f"Not applied. {result.get('reason', '')}".strip())
    lines.append("")
    return lines


def render(store: RunStore) -> str:
    brief = store.steps.get("brief", {})
    lines = [f"# Team run {store.id}", "", f"Task: {store.task}", ""]
    if brief:
        lines += ["## Requirements", "", brief.get("summary", ""), ""]
        lines.append(f"UI mode: {brief.get('ui_mode')}")
        if brief.get("existing_stack"):
            lines.append(f"Existing stack: {brief['existing_stack']}")
        lines.append("")
        lines += [f"- {item}" for item in brief.get("acceptance_criteria", [])]
        lines.append("")
    research = store.steps.get("research")
    if research and not research.get("forfeit"):
        lines += ["## Research", "", research["summary"], ""]
    lines += _fight_section(store, "design")
    lines += _fight_section(store, "dev")
    lines += _security_section(store)
    lines += _result_section(store)
    lines.append(f"Documents: {store.root / 'docs'}")
    return "\n".join(lines) + "\n"


def write(store: RunStore) -> None:
    write_durable(store.root / "report.md", render(store))
