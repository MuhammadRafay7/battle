---
name: battle
description: Runs a tournament where AI agents fight over one task in walled-off groups, attacking each other's work across rounds, while judges pick one champion whose work is applied to the project. Use when the user types /battle or asks agents to fight, compete or battle over a task.
argument-hint: "[task] [--size big]"
disable-model-invocation: true
allowed-tools: Bash("${CLAUDE_SKILL_DIR}/../bin/battle" *)
---

# Battle

The user's request:

$ARGUMENTS

## Workflow

1. If the request has no task, ask what the battle should do and stop.
2. Separate the task from any flags listed under Options. Write only the task to a new file in your scratchpad. Never put the task text in a shell command.
3. Show the size, including any size flags:

   ```bash
   "${CLAUDE_SKILL_DIR}/../bin/battle" plan --size default
   ```

4. Start the battle with `run_in_background: true`, then wait for it to finish. Do not poll.

   ```bash
   "${CLAUDE_SKILL_DIR}/../bin/battle" run --task-file <file> --project "$PWD" --size default
   ```

5. When it exits, handle the exit code:
   - **0:** read the `report.md` path it printed. Tell the user the champion, why it won and which files changed, plus the undo command.
   - **75:** the plan's usage limit was reached. Finished work is saved. Give the user the `battle resume` command it printed.
   - **Anything else:** say what stopped it and give the retry command it printed.

## Options

| Flag | Meaning |
|---|---|
| `--size big` | 4 groups of 5, about 80 agent runs. The default is 2 groups of 4, about 38 runs |
| `--groups N`, `--fighters N` | Custom size |
| `--rounds N`, `--final-rounds N` | Rounds per group, rounds in the final |
| `--agent codex` | Use Codex. Repeat `--agent` to mix agents, for example `--agent claude --agent codex` |
| `--model NAME` | Model for every agent |
| `--allow-bash` | Lets Claude fighters run shell commands. Only pass it if the user asks, because commands are not sandboxed |
| `--no-apply` | Keep the champion out of the project until `battle apply` |

## Example

For `/battle add rate limiting to the login route --size big`, write `add rate limiting to the login route` to the task file, then run `plan` and `run` with `--size big`.
