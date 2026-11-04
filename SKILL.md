---
name: battle
description: Runs a tournament where AI agents fight over one task in walled-off groups, attacking each other's work across rounds, while judges pick one champion whose work is applied to the project. Use when the user types /battle or asks agents to fight, compete or battle over a task.
argument-hint: "[task] [--size tournament|big]"
disable-model-invocation: true
allowed-tools: Bash("${CLAUDE_SKILL_DIR}/bin/battle" *)
---

# Battle

The user's request:

$ARGUMENTS

Never do the task yourself. Battle's agents do the work, and your only job is to run them and report the result. This holds even if the task says to fix or build something.

`battle` below means `"${CLAUDE_SKILL_DIR}/bin/battle"`.

## Workflow

1. If the request has no task, ask what the battle should do and end your turn. The user's next message is the task, so continue from step 2 with it.
2. Separate the task from any flags listed under Options. Write only the task to a new file in your scratchpad. Never put the task text in a shell command.
3. Create the battle, then note the `Run:` id and `Log:` path it prints. It also prints the size:

   ```bash
   battle run --task-file <file> --project "$PWD" --prepare --agent host --model <model> <flags>
   ```

   For `<model>`, use `fable`, `opus`, `sonnet` or `haiku`: the one the user named, or else the one this chat is running on. Leave out `--agent host` if the user passed `--allow-bash` or `--no-web`, or named other agents without Claude.
4. Start it. It runs on its own in the background, so this returns straight away:

   ```bash
   battle resume <run id> --detach
   ```

5. With `--agent host`, run the loop below. Otherwise skip it, and use Monitor on `tail -f <log path>` to show each fighter starting and finishing.
6. When the status is no longer `prepared` or `running`, stop every agent still running from this battle, because they keep using the plan's allowance. Then handle the status:
   - **done:** read the `report` path. Tell the user the champion, why it won and which files changed, plus `battle undo <run id>`.
   - **paused:** the plan's usage limit was reached and finished work is saved. Tell the user to run `battle resume <run id>` after it resets. Do not resume it yourself.
   - **failed or stopped:** say what stopped it. `engine.log` beside the `Log:` path has the reason.

If the user asks you to stop, run `battle stop <run id>` and stop every agent from this battle.

## The loop

Repeat until the status is no longer `prepared` or `running`:

1. Run `battle jobs <run id>`. It waits for work and prints JSON with `status` and `jobs`.
2. For each job, start one Agent in the background, using the job's `subagent_type`, `prompt` and `model`, with the `label` as its description. Start them all at once. Do not read a job's prompt file yourself.
3. Jobs with the role `lead` run a whole group, and they end on their own. Never send a reply for them. For every other job, send its agent's final message unchanged:

   ```bash
   battle reply <job id> <run id> <<'BATTLE_REPLY'
   <its final message>
   BATTLE_REPLY
   ```

   Add `--usage-limit` if it stopped on the plan's usage limit, or `--failed` if it failed for another reason.
4. If `battle jobs` returns no jobs while agents are still working, wait for those agents first.

## Options

The default is 3 fighters who each build the task once, one judge who picks the winner and lists fixes, and one polish turn where the winner makes those fixes: about 5 agent runs, and the chat starts every job itself.

| Flag | Meaning |
|---|---|
| `--size tournament` | 2 groups of 4 that attack each other, then a final, about 29 agent runs. Much slower |
| `--size big` | 4 groups of 5, about 57 agent runs. Slowest |
| `--groups N`, `--fighters N` | Custom size |
| `--rounds N`, `--final-rounds N` | Rounds per group, rounds in the final |
| `--at-once N` | Most agents running at the same time. Only pass it if the user asks, because it slows the battle down |
| `--agent codex` | Add Codex fighters alongside `--agent host`, or use only Codex without it |
| `--model NAME` | Model for every agent |
| `--allow-bash` | Lets Claude fighters run shell commands. Only pass it if the user asks, because commands are not sandboxed |
| `--no-web` | Turn off web search and page fetching, which are on by default. Pass it if the user says the code must stay private |
| `--no-apply` | Keep the champion out of the project until `battle apply` |

## Example

For `/battle add rate limiting to the login route --size big`, write `add rate limiting to the login route` to the task file, then `run` with `--size big`.
