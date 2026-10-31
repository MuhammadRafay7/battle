import stat
from pathlib import Path

import pytest

from battle.agents import (
    AgentError,
    ClaudeAgent,
    CodexAgent,
    CommandAgent,
    Job,
    UsageLimitReached,
    child_env,
    extract_json,
    make_agent,
)


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def job(tmp_path, **changes):
    values = {"role": "fighter", "prompt": "do it", "cwd": tmp_path, "writes": True}
    return Job(**(values | changes))


def test_extract_json_reads_a_plain_object():
    assert extract_json('{"winner": "A1"}') == {"winner": "A1"}


def test_extract_json_finds_the_last_object_after_prose():
    text = 'I looked at {"draft": 1}.\n\n```json\n{"winner": "A2", "scores": {"A2": 9}}\n```\n'
    assert extract_json(text) == {"winner": "A2", "scores": {"A2": 9}}


def test_extract_json_skips_braces_that_are_not_json():
    assert extract_json('function() { return 1 } {"ok": true}') == {"ok": True}


def test_extract_json_raises_without_an_object():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_child_env_drops_the_parent_session_but_keeps_the_rest():
    env = child_env(
        {
            "PATH": "/bin",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "abc",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }
    )
    assert env == {"PATH": "/bin", "CLAUDE_CODE_USE_BEDROCK": "1"}


def test_claude_fighter_can_edit_but_gets_no_shell_by_default(tmp_path):
    argv = ClaudeAgent().command(job(tmp_path))

    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Grep,Glob,WebSearch,WebFetch"
    assert "--dangerously-skip-permissions" not in argv


def test_claude_fighter_gets_a_shell_only_when_allowed(tmp_path):
    argv = ClaudeAgent(allow_bash=True).command(job(tmp_path))

    tools = "Read,Edit,Write,Grep,Glob,Bash,WebSearch,WebFetch"
    assert argv[argv.index("--tools") + 1] == tools
    assert argv[argv.index("--allowedTools") + 1] == tools


def test_claude_judge_is_read_only_even_with_shell_allowed(tmp_path):
    argv = ClaudeAgent(allow_bash=True).command(
        job(tmp_path, role="judge", writes=False, read_dirs=(tmp_path / "baseline",))
    )

    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "baseline")


@pytest.mark.parametrize(
    "writes, tools", [(True, "Read,Edit,Write,Grep,Glob"), (False, "Read,Grep,Glob")]
)
def test_claude_agents_lose_web_tools_when_web_is_off(tmp_path, writes, tools):
    argv = ClaudeAgent(web=False).command(job(tmp_path, writes=writes))

    assert argv[argv.index("--tools") + 1] == tools
    assert argv[argv.index("--allowedTools") + 1] == tools


@pytest.mark.parametrize("writes", [True, False])
def test_claude_agents_never_load_your_mcp_servers(tmp_path, writes):
    argv = ClaudeAgent().command(job(tmp_path, writes=writes))
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_claude_passes_the_chosen_model(tmp_path):
    argv = ClaudeAgent(model="claude-sonnet-5-5").command(job(tmp_path))
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


def test_claude_reads_the_result_field(tmp_path):
    binary = script(
        tmp_path / "claude",
        """cat > /dev/null
printf '{"type":"result","is_error":false,"result":"all done"}'
""",
    )
    assert ClaudeAgent(binary=binary).run(job(tmp_path)) == "all done"


def test_claude_error_result_with_limit_text_is_a_usage_limit(tmp_path):
    binary = script(
        tmp_path / "claude",
        """cat > /dev/null
printf '{"type":"result","is_error":true,"result":"Claude usage limit reached. Resets at 5pm"}'
exit 1
""",
    )
    with pytest.raises(UsageLimitReached):
        ClaudeAgent(binary=binary).run(job(tmp_path))


def test_short_limit_message_with_exit_zero_is_still_a_usage_limit(tmp_path):
    binary = script(
        tmp_path / "claude",
        """cat > /dev/null
printf '{"type":"result","is_error":false,"result":"You have hit your session limit."}'
""",
    )
    with pytest.raises(UsageLimitReached):
        ClaudeAgent(binary=binary).run(job(tmp_path))


def test_long_reply_that_mentions_rate_limits_is_a_normal_reply(tmp_path):
    reply = "I added a rate limit to the login route. " * 20
    binary = script(
        tmp_path / "claude",
        f"""cat > /dev/null
printf '{{"type":"result","is_error":false,"result":"{reply}"}}'
""",
    )
    assert "rate limit" in ClaudeAgent(binary=binary).run(job(tmp_path))


def test_short_json_reply_about_rate_limits_is_a_normal_reply(tmp_path):
    agent = CommandAgent(
        script(tmp_path / "tool", """cat > /dev/null
printf '{"summary": "Added a rate limit to login."}'
""")
    )
    assert "rate limit" in agent.run(job(tmp_path))


def test_other_failures_are_agent_errors(tmp_path):
    binary = script(tmp_path / "claude", "cat > /dev/null\necho crashed >&2\nexit 2\n")
    with pytest.raises(AgentError, match="crashed") as error:
        ClaudeAgent(binary=binary).run(job(tmp_path))
    assert not isinstance(error.value, UsageLimitReached)


def test_missing_binary_is_an_agent_error(tmp_path):
    with pytest.raises(AgentError, match="not installed"):
        ClaudeAgent(binary="/no/such/claude").run(job(tmp_path))


def test_timeout_stops_the_agent(tmp_path):
    binary = script(tmp_path / "claude", "cat > /dev/null\nsleep 30\n")
    with pytest.raises(AgentError, match="timed out"):
        ClaudeAgent(binary=binary).run(job(tmp_path, timeout=0.2))


def test_codex_fighter_uses_the_workspace_sandbox(tmp_path):
    argv = CodexAgent().command(job(tmp_path), tmp_path / "reply.txt")

    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "-"


def test_codex_judge_is_read_only(tmp_path):
    argv = CodexAgent().command(job(tmp_path, writes=False), tmp_path / "reply.txt")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize("writes", [True, False])
def test_codex_gets_live_search_while_keeping_its_sandbox(tmp_path, writes):
    argv = CodexAgent().command(job(tmp_path, writes=writes), tmp_path / "reply.txt")

    assert argv[argv.index("-c") + 1] == 'web_search="live"'
    assert "--search" not in argv
    assert "danger-full-access" not in argv


def test_codex_search_is_disabled_when_web_is_off(tmp_path):
    argv = CodexAgent(web=False).command(job(tmp_path), tmp_path / "reply.txt")
    assert argv[argv.index("-c") + 1] == 'web_search="disabled"'


def test_codex_returns_its_last_message(tmp_path):
    binary = script(
        tmp_path / "codex",
        """cat > /dev/null
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then printf 'codex says hi' > "$2"; fi
  shift
done
""",
    )
    assert CodexAgent(binary=binary).run(job(tmp_path)) == "codex says hi"


def test_command_agent_gets_the_prompt_and_role(tmp_path):
    agent = CommandAgent(
        script(tmp_path / "tool", 'printf "%s:" "$BATTLE_ROLE"\ncat\n')
    )
    assert agent.run(job(tmp_path, prompt="hello")) == "fighter:hello"


@pytest.mark.parametrize("web, expected", [(True, "1"), (False, "0")])
def test_command_agent_is_told_whether_web_is_on(tmp_path, web, expected):
    agent = CommandAgent(
        script(tmp_path / "tool", 'cat > /dev/null\nprintf "%s" "$BATTLE_WEB"\n'), web=web
    )
    assert agent.run(job(tmp_path)) == expected


@pytest.mark.parametrize("spec", ["claude", "codex", "command:tool"])
def test_make_agent_passes_the_web_choice_on(spec):
    assert make_agent(spec, web=False).web is False
    assert make_agent(spec).web is True


def test_make_agent_picks_the_right_adapter(tmp_path):
    assert isinstance(make_agent("claude"), ClaudeAgent)
    assert isinstance(make_agent("codex"), CodexAgent)
    assert isinstance(make_agent("command:python3 tool.py"), CommandAgent)


def test_make_agent_rejects_unknown_agents():
    with pytest.raises(ValueError, match="gemini"):
        make_agent("gemini")


def test_prompt_is_sent_on_stdin_so_long_tasks_fit(tmp_path):
    long_prompt = "x" * 300_000
    agent = CommandAgent(script(tmp_path / "tool", "wc -c\n"))
    assert agent.run(job(tmp_path, prompt=long_prompt)).strip() == "300000"
