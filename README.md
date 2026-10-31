# Battle

Give one task to many AI agents. They fight in groups, attacking each other's work over several rounds. Judges pick one champion, and only that work reaches your project.

```text
Your prompt
  -> Setup: reads free RAM, gives each group a different direction
  -> Group fights: one group at a time, every fighter attacks every other
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

Link the `skill` folder into a skills folder, then type `/battle add a login page`.

```bash
# For all your projects
ln -s /path/to/battle/skill ~/.claude/skills/battle

# For one folder of projects
mkdir -p /path/to/projects/.claude/skills
ln -s /path/to/battle/skill /path/to/projects/.claude/skills/battle
```

The one-folder link doesn't reach projects that are their own git repositories, because Claude Code stops looking for skills at the repository root. Those need the all-projects link.

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
| Claude Code | `--agent claude` (default) |
| Codex | `--agent codex` |
| Any other CLI | `--agent "command:my-tool --flag"` |

Repeat `--agent` to put different agents in the same fight. The first agent does all the judging. A custom command gets the prompt on stdin and prints its reply, and must end the reply with the JSON object the prompt asks for.

## Limits

- **RAM:** Battle allows 500 MB per agent and keeps 512 MB free. If RAM is short, fighters in a group take turns. Free RAM is only read automatically on Linux and macOS.
- **Plan usage:** every run comes from your plan's allowance, so a big battle can hit a usage limit. Battle saves after every agent run and exits with code 75, and `battle resume` continues after the limit resets. Finished work isn't redone.
- **Time:** a 20-run battle on Haiku with 2 fighters at once took about 16 minutes. Bigger sizes and stronger models take longer, so run big battles in the background.
- **Fighters only see rivals between rounds.** They get each rival's latest finished round, not live edits.
- **Judges read code but never run it.** By default Claude fighters can't run code either, so nobody executes the tests. Pass `--allow-bash` if you want fighters to run them.

## Safety

- Fighters work on copies. Your project changes only when the champion is applied.
- Folders like `.git` and `node_modules` are not copied, so fighters can't use them. Neither are secrets such as `.env`, `*.pem` and `*.key`.
- Applying stops if you edited one of the same files during the battle. `battle apply --force` overwrites them.
- `battle undo` restores the project from a backup taken just before the champion was applied.
- Judges are read-only. Claude fighters get no shell unless you pass `--allow-bash`, and those commands are **not sandboxed**, because a copy of your project doesn't stop a command from reaching the rest of your computer. Codex fighters run shell commands inside Codex's own sandbox.
- Rivals write the text that judges read, so every prompt tells agents to treat that text as data, not instructions. That lowers the risk of an entry manipulating a judge but doesn't remove it. Read the champion's diff before you ship it.

Each battle is stored in `~/.battle/runs/<id>/`, or under `BATTLE_HOME` if you set it, including `report.md`, `log.txt` and the champion's `changes.diff`. It also keeps a copy of your project without the skipped folders and secrets, so delete old runs to free the space.

## Tests

```bash
python3 -m pytest
```

The tests use a scripted fake agent, so they spend none of your plan.
