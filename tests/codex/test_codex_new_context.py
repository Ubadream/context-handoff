# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""The thread's own handoff, the new_context reminder, and `apply --new-context` (synthetic rollouts)."""
import json
import os
from pathlib import Path
import tomllib

import pytest

from test_codex_hooks import SID, context, event, home, hooks, installer, line, tokens, user  # noqa: F401

thread_handoffs = hooks.thread_handoffs


def call(command, call_id="write", workdir=None):
    args = {"cmd": command, **({"workdir": str(workdir)} if workdir else {})}
    return line("response_item", {"type": "function_call", "name": "exec_command", "call_id": call_id,
                                  "arguments": json.dumps(args)})


def done(call_id="write", code=0):
    return line("response_item", {"type": "function_call_output", "call_id": call_id,
                                  "output": json.dumps({"exit_code": code, "output": ""})})


def rollout(tmp_path, *lines):
    path = tmp_path / "rollout.jsonl"
    path.write_text(line("session_meta", {"id": SID, "cwd": str(tmp_path)}) + "".join(lines), encoding="utf-8")
    return path


def handoff(directory, name="HANDOFF.md"):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("handoff", encoding="utf-8")
    return path


def test_only_successful_writes_count(tmp_path):
    written = handoff(tmp_path)
    read_only = handoff(tmp_path, "HANDOFF_other.md")
    failed = handoff(tmp_path, "HANDOFF_failed.md")
    patched = handoff(tmp_path, "NEXT_HANDOFF.md")
    path = rollout(tmp_path,
                   call("$root=(Get-Location).Path\n[IO.File]::WriteAllText((Join-Path $root 'HANDOFF.md'), $t)"), done(),
                   call(f"Set-Content 'notes.txt' 'x'; Get-Content -LiteralPath '{read_only}'", "read"), done("read"),
                   call(f"Set-Content '{failed}' 'x'", "failed"), done("failed", 1),
                   line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "patch",
                                          "input": "*** Begin Patch\n*** Update File: " + str(patched) + "\n"}),
                   line("response_item", {"type": "custom_tool_call_output", "call_id": "patch",
                                          "output": "Success. Updated the following files:\nM " + str(patched)}))
    assert thread_handoffs.written_handoffs(path, SID) == [str(patched), str(written)]


def test_newest_write_wins_over_file_time_and_paths_with_spaces(tmp_path):
    first = handoff(tmp_path / "with space", "HANDOFF_first.md")
    second = handoff(tmp_path, "HANDOFF_second.md")
    os.utime(first, (2_000_000_000, 2_000_000_000))
    path = rollout(tmp_path, call(f'Set-Content -LiteralPath "{first}" "x"', "a"), done("a"),
                   call("Set-Content 'HANDOFF_second.md' 'x'", "b", workdir=tmp_path), done("b"))
    assert thread_handoffs.written_handoffs(path, SID) == [str(second), str(first)]


def test_here_string_text_is_not_a_write(tmp_path):
    handoff(tmp_path)
    command = "$body=@'\n[IO.File]::WriteAllText('HANDOFF.md', 'example')\n'@\nSet-Content 'notes.txt' $body"
    assert thread_handoffs.written_handoffs(rollout(tmp_path, call(command), done()), SID) == []


def test_posix_redirect_tee_and_heredoc(tmp_path):
    redirected = handoff(tmp_path, "HANDOFF_a.md")
    teed = handoff(tmp_path / "notes", "HANDOFF_b.md")
    command = (f"cat > HANDOFF_a.md <<'EOF'\n# not a write: echo x > HANDOFF_fake.md\nEOF\n"
               f"printf done | tee -a '{teed}' 2>&1 >/dev/null")
    path = rollout(tmp_path, call(command, workdir=tmp_path), done())
    assert set(thread_handoffs.written_handoffs(path, SID)) == {str(redirected), str(teed)}
    assert thread_handoffs.written_handoffs(rollout(tmp_path, call("cat HANDOFF_a.md > notes.txt"), done()), SID) == []


def test_thread_handoff_preferred_over_newer_file_in_cwd(home, tmp_path):
    elsewhere = handoff(tmp_path / "notes", "HANDOFF_task.md")
    newer = handoff(tmp_path, "HANDOFF_someone_else.md")
    os.utime(elsewhere, (1_000_000, 1_000_000))
    path = rollout(tmp_path, user("do it"), call(f"Set-Content '{elsewhere}' 'x'"), done())
    hooks.receive(event("PreCompact", path, tmp_path, trigger="auto"))
    text = context(hooks.receive(event("SessionStart", path, tmp_path, source="compact")))
    assert str(elsewhere) in text and str(newer) not in text
    assert "new context window" in text and "not a new request" in text


def test_reminder_names_new_context_only_when_enabled(home, tmp_path):
    path = rollout(tmp_path, user("do it"), tokens(8500))
    assert "run /compact" in context(hooks.receive(event("PostToolUse", path, tmp_path)))
    (home / "config.toml").write_text("[features]\nhooks = true\ntoken_budget = true\n", encoding="utf-8")
    hooks.receive(event("PreCompact", path, tmp_path))
    hooks.receive(event("SessionStart", path, tmp_path, source="compact"))
    text = context(hooks.receive(event("PostToolUse", path, tmp_path)))
    assert "`new_context` tool yourself" in text and "/compact" not in text


@pytest.mark.parametrize("existing", ["", 'model = "x"\n\n[features]\nhooks = false\ntoken_budget = false\n'])
def test_apply_new_context_sets_token_budget(home, existing):
    config = home / "config.toml"
    if existing:
        config.write_text(existing, encoding="utf-8")
    installer.apply(dry=False, new_context=True)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["features"]["hooks"] is True and data["features"]["token_budget"] is True
    installer.apply(dry=False)  # a later plain apply leaves the flag alone
    assert tomllib.loads(config.read_text(encoding="utf-8"))["features"]["token_budget"] is True
