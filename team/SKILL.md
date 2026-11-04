---
name: team
description: Runs a team of AI agents that turns a prompt into a finished, security-tested website. A PM writes requirements, a researcher studies the niche, two designers and then two developers fight with a judge picking each winner, and attackers probe the result until a fixer closes every confirmed flaw. Use when the user types /team or asks a team of agents to build a site.
argument-hint: "[prompt] [--skip research|design|security] [--solo design|dev]"
disable-model-invocation: true
allowed-tools: Bash("${CLAUDE_SKILL_DIR}/../bin/team" *)
---

# Team

The user's request:

$ARGUMENTS

Never do the task yourself. The team's agents do the work, and your only job is to run them and report the result. This holds even if the prompt says to fix or build something.

`team` below means `"${CLAUDE_SKILL_DIR}/../bin/team"`.

## Workflow

1. If the request has no prompt, ask what the team should build and end your turn. The user's next message is the prompt, so continue from step 2 with it.
2. Separate the prompt from any flags listed under Options. Write only the prompt to a new file in your scratchpad. Never put the prompt text in a shell command.
3. Create the run, then note the `Run:` id and `Log:` path it prints. It also prints the stages and their cost:

   ```bash
   team run --task-file <file> --project "$PWD" --prepare --agent host --model <model> <flags>
   ```

   For `<model>`, use `fable`, `opus`, `sonnet` or `haiku`: the one the user named, or else the one this chat is running on. Leave out `--agent host` if the user passed `--no-web`, or named other agents without Claude.
4. Start it. It runs on its own in the background, so this returns straight away:

   ```bash
   team resume <run id> --detach
   ```

5. With `--agent host`, run the loop below. Otherwise skip it, and use Monitor on `tail -f <log path>` to show each stage starting and finishing.
6. When the status is no longer `prepared` or `running`, stop every agent still running from this run, because they keep using the plan's allowance. Then handle the status:
   - **done:** read the `report` path. Tell the user which design and which build won and why, how the security rounds went and which findings are still open, which files changed, where the documents are (`docs/` beside the report), plus `team undo <run id>`.
   - **paused:** the plan's usage limit was reached and finished work is saved. Tell the user to run `team resume <run id>` after it resets. Do not resume it yourself.
   - **failed or stopped:** say what stopped it. `engine.log` beside the `Log:` path has the reason.

If the user asks you to stop, run `team stop <run id>` and stop every agent from this run.

## The loop

Repeat until the status is no longer `prepared` or `running`:

1. Run `team jobs <run id>`. It waits for work and prints JSON with `status` and `jobs`.
2. For each job, start one Agent in the background, using the job's `subagent_type`, `prompt` and `model`, with the `label` as its description. Start them all at once. Do not read a job's prompt file yourself.
3. For every job, send its agent's final message unchanged:

   ```bash
   team reply <job id> <run id> <<'BATTLE_REPLY'
   <its final message>
   BATTLE_REPLY
   ```

   Add `--usage-limit` if it stopped on the plan's usage limit, or `--failed` if it failed for another reason.
4. If `team jobs` returns no jobs while agents are still working, wait for those agents first.

## Options

By default every stage runs: PM, research, a design fight, a dev fight and up to 3 security rounds with 6 attackers each. `team plan <flags>` prints the stages and the number of agent runs.

| Flag | Meaning |
|---|---|
| `--skip research` | No research stage. Agents still search the web themselves |
| `--skip design` | No design fight. Developers design as they build |
| `--skip security` | No attackers or fixer |
| `--solo design`, `--solo dev` | One entrant instead of a fight, with no judge |
| `--angles auth,xss,...` | Which attack angles to run: auth, injection, xss, secrets, deps, access |
| `--security-rounds N` | Most attack, fix and re-test rounds (default 3) |
| `--at-once N` | Most agents running at the same time. Only pass it if the user asks |
| `--agent codex` | Add Codex as side B's agent alongside `--agent host`, or use only Codex without it |
| `--model NAME` | Model for every agent |
| `--no-web` | Turn off web search and page fetching. Pass it if the user says the code must stay private |
| `--no-apply` | Keep the finished site out of the project until `team apply` |
| `--keep` | Keep every workspace after the run |

Developers, judges, attackers and the fixer run shell commands, which are not sandboxed. Say so if the user asks about safety.

## Example

For `/team a booking site for a boutique climbing gym --skip research`, write `a booking site for a boutique climbing gym` to the task file, then `run` with `--skip research`.
