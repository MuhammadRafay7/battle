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

Never do the task yourself. Battle's agents do the work, and your only job is to run them and report the result. This holds even if the task says to fix or build something.

`battle` below means `"${CLAUDE_SKILL_DIR}/../bin/battle"`.

## Workflow

1. If the request has no task, ask what the battle should do and end your turn. The user's next message is the task, so continue from step 2 with it.
2. Separate the task from any flags listed under Options. Write only the task to a new file in your scratchpad. Never put the task text in a shell command.
3. Show the size with `battle plan`, including any size flags.
4. Create the battle, then note the `Run:` id and `Log:` path it prints:

   ```bash
   battle run --task-file <file> --project "$PWD" --prepare --agent host <flags>
   ```

   Leave out `--agent host` if the user passed `--allow-bash` or `--no-web`, or named other agents without Claude.
5. Start it with `run_in_background: true`:

   ```bash
   battle resume <run id>
   ```

6. With `--agent host`, run the loop below. Otherwise skip it, and use Monitor on `tail -f <log path>` to show each fighter starting and finishing.
7. When the status is no longer `prepared` or `running`, handle it:
   - **done:** read the `report` path. Tell the user the champion, why it won and which files changed, plus `battle undo <run id>`.
   - **paused:** the plan's usage limit was reached and finished work is saved. Tell the user to run `battle resume <run id>` after it resets. Do not resume it yourself.
   - **failed or stopped:** say what stopped it. The background command's output has the reason.

## The loop

Repeat until the status is no longer `prepared` or `running`:

1. Run `battle jobs <run id>`. It waits for work and prints JSON with `status` and `jobs`.
2. For each job, start one Agent in the background, using the job's `subagent_type`, `prompt` and `model` when it isn't null, with the `label` as its description. Do not read the job's prompt file yourself.
3. As each agent finishes, send its final message unchanged:

   ```bash
   battle reply <job id> <run id> <<'BATTLE_REPLY'
   <its final message>
   BATTLE_REPLY
   ```

   Add `--usage-limit` if it stopped on the plan's usage limit, or `--failed` if it failed for another reason.
4. If `battle jobs` returns no jobs while agents are still working, wait for those agents first.

## Options

| Flag | Meaning |
|---|---|
| `--size big` | 4 groups of 5, about 80 agent runs. The default is 2 groups of 4, about 38 runs |
| `--groups N`, `--fighters N` | Custom size |
| `--rounds N`, `--final-rounds N` | Rounds per group, rounds in the final |
| `--at-once N` | Most agents running at the same time. All of them start together unless memory is short |
| `--agent codex` | Add Codex fighters alongside `--agent host`, or use only Codex without it |
| `--model NAME` | Model for every agent |
| `--allow-bash` | Lets Claude fighters run shell commands. Only pass it if the user asks, because commands are not sandboxed |
| `--no-web` | Turn off web search and page fetching, which are on by default. Pass it if the user says the code must stay private |
| `--no-apply` | Keep the champion out of the project until `battle apply` |

## Example

For `/battle add rate limiting to the login route --size big`, write `add rate limiting to the login route` to the task file, then run `plan` and `run` with `--size big`.
