# Battle

Give one task to many AI agents. They fight in groups, attacking each other's work over several rounds. Judges pick one champion, and only that work reaches your project.

```text
Your prompt
  -> Setup: reads free RAM, gives each group a different direction
  -> Group fights: all groups at once, every fighter attacks every other in its group
  -> Group judges: pick a winner in each group
  -> Head judge: checks each verdict, can overturn it or add a runner-up
  -> Final: group winners fight each other
  -> 5 specialist judges score it, then the head judge picks the champion
  -> Champion's work is applied. Everything else is thrown away
```

## Requirements

- Python 3.10 or newer, on Linux or macOS
- At least one agent CLI, logged in: `claude` or `codex`

There is nothing to install. Run `bin/battle`, or link it onto your PATH.

## Use it from Claude Code

Install it once, then open a new Claude Code session:

```bash
bin/battle install
```

This links the `/battle` skill and two agent definitions into `~/.claude`. Then type `/battle add a login page`. Put the task in the same message as `/battle`.

Claude agents run as real subagents of that chat, so you see them there. The chat has to stay open for the whole battle, and managing the agents uses some of your allowance too. Agent files are read from `~/.claude/agents` because hooks in a project's own agent files are skipped in folders you haven't trusted, and those hooks are what keep fighters in their own folders.

## Use it from a terminal

```bash
battle plan --size big                  # show size and cost, runs nothing
battle run "add a login page"           # start a battle in the current folder
battle run --task-file task.txt --project ../my-app
battle status                           # progress of the latest battle
battle resume                           # continue after a usage limit
battle undo                             # put the project back
battle apply                            # apply a champion kept out with --no-apply
```

## Sizes

| Size | Groups | Fighters | Agent runs |
|---|---|---|---|
| `--size default` | 2 groups of 4 | 8 | about 38 |
| `--size big` | 4 groups of 5 | 20 | about 80 |

Rounds end early when no fighter can land an attack with evidence. Change the size with `--groups`, `--fighters`, `--rounds` and `--final-rounds`.

## Which agents can fight

| Agent | Flag |
|---|---|
| Claude Code | `--agent claude` (default in a terminal) |
| Claude Code subagents | `--agent host` (default for `/battle`) |
| Codex | `--agent codex` |
| Any other CLI | `--agent "command:my-tool --flag"` |

Repeat `--agent` to put different agents in the same fight. The first agent does all the judging. A custom command gets the prompt on stdin and prints its reply, and must end the reply with the JSON object the prompt asks for.

## Rules

Every fighter and judge follows the rulebook in [`battle/rules.md`](battle/rules.md). It covers how to build, how to research and how to judge. Fighters can attack a broken rule, and judges penalize one.

The rules are about how to work, not about what is currently true. Anything that changes over time, such as library versions, APIs and security advice, is left for agents to research. Keep it that way if you edit the file, or the rules will go out of date. Edits apply to the next agent that starts.

## Limits

- **Memory:** all groups and fighters start at the same time. Battle allows 500 MB per agent, keeps 512 MB free, and counts free swap as well as free RAM. If that isn't enough, agents wait for a free slot. Running mostly on swap can make your computer slow, so lower it with `--at-once N`. Free memory is only read automatically on Linux and macOS.
- **Plan usage:** every run comes from your plan's allowance, so a big battle can hit a usage limit. Battle saves after every agent run and exits with code 75, and `battle resume` continues after the limit resets. Finished work isn't redone. If an outage knocks out every fighter, the battle fails and `battle resume` gives those fighters another try.
- **Stopping:** Ctrl-C, `kill` and closing the terminal kill every running agent at once, save finished work and set the status to `stopped`, and `battle resume` continues. The exit code is 128 plus the signal number. `kill -9` and crashes can't be caught, but every agent starts with a small tether that kills it the moment Battle's process is gone, so nothing keeps using your allowance. On Linux, a process that leaves its agent's process group is found by a mark in its environment and killed too, unless it wipes its environment. On macOS it can escape. With `--agent host` the chat has to stop its own subagents. Under `nohup`, closing the terminal doesn't stop the battle. A signal never cuts an apply or undo in half, because they finish first. If Battle dies mid-apply, no file is left half written, and `battle apply` puts the project back and applies again. `battle resume` does the same during a battle, and on a finished battle it just puts the project back. `battle undo` also still works.
- **Time:** bigger sizes and stronger models take longer, so run big battles in the background. Watch `log.txt` to see each fighter start and finish.
- **Fighters only see rivals between rounds.** They get each rival's latest finished round, not live edits. Groups can't see each other until the final.
- **Internet:** fighters and judges can search the web and read pages, so they can check official docs, best practices and known vulnerabilities. This uses more of your allowance and takes longer. Turn it off with `--no-web`.
- **Judges read code but never run it.** By default Claude fighters can't run code either, so nobody executes the tests. Pass `--allow-bash` if you want fighters to run them.

## Safety

- Fighters work on copies. Your project changes only when the champion is applied.
- Folders like `.git` and `node_modules` are not copied, so fighters can't use them. Neither are secrets such as `.env`, `*.pem` and `*.key`.
- Symlinks that point outside your project or at a secret are not copied, and applying and undoing refuse to write through any symlink, even with `--force`. A refused apply keeps the champion, so you can remove the link and run `battle apply`.
- Applying stops if you edited one of the same files during the battle. `battle apply --force` overwrites them.
- `battle undo` restores the project from a backup taken just before the champion was applied.
- Judges can't change files. Claude fighters get no shell unless you pass `--allow-bash`, and those commands are **not sandboxed**, because a copy of your project doesn't stop a command from reaching the rest of your computer. Codex fighters run shell commands inside Codex's own sandbox.
- Claude agents don't load your MCP servers, so they can't reach your browser, memory or connected apps. They also skip the copy's project settings and `CLAUDE.md`, so hooks in a copy's `.claude/settings.json` never run, whoever wrote them. `CLAUDE.md` is still there to read.
- Subagents share the chat's folder, so a guard hook (`bin/battle-guard`) keeps them apart. A fighter can only read and write its own copy, and a judge can only read the battle's folder. In a real test, a fighter told to write into the project and a rival's copy was blocked. If a hook can't run at all, nothing else stops a fighter, so keep the install in `~/.claude`.
- `--allow-bash` and `--no-web` don't work with subagents, because the guard can't check shell commands and subagents always have web tools. `/battle` runs separate agents for those.
- Web access is on by default, and it adds a real risk. A web page, or a rival's text, could try to trick an agent into sending your code to a website. Prompts forbid this, but that can't guarantee it, and secrets are protected only because they aren't copied. Use `--no-web` for code that must stay private.
- Rivals write the text that judges read, so every prompt tells agents to treat that text as data, not instructions. That lowers the risk of an entry manipulating a judge but doesn't remove it. Read the champion's diff before you ship it.

Each battle is stored in `~/.battle/runs/<id>/`, or under `BATTLE_HOME` if you set it, including `report.md`, `log.txt` and the champion's `changes.diff`. It also keeps a copy of your project without the skipped folders and secrets, so delete old runs to free the space.

## Tests

```bash
python3 -m pytest
```

The tests use a scripted fake agent, so they spend none of your plan.
