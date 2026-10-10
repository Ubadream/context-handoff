# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Hooks-only adapter and its installer, on synthetic rollouts and a temporary CODEX_HOME."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import tomllib

import pytest

REPO = Path(__file__).resolve().parents[2]
SID = "01a12345-0000-7000-8000-000000000001"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hooks = load("cw_codex_hooks", REPO / "adapters/codex/hooks/cw_codex_hooks.py")
installer = load("install_codex_hooks", REPO / "adapters/codex/install_codex_hooks.py")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def line(kind, payload, ts="2026-10-10T10:00:00Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload}) + "\n"


def user(text, ts="2026-10-10T10:00:00Z"):
    return line("response_item", {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": text}]}, ts)


def tokens(used, window=10000):
    return line("event_msg", {"type": "token_count", "info": {"last_token_usage": {"total_tokens": used},
                                                               "model_context_window": window}})


def rollout(tmp_path, *lines, sid=SID):
    path = tmp_path / "rollout.jsonl"
    path.write_text(line("session_meta", {"id": sid}) + "".join(lines), encoding="utf-8")
    return path


def event(name, path, cwd, **extra):
    return {"hook_event_name": name, "session_id": SID, "transcript_path": str(path), "cwd": str(cwd), **extra}


def context(result):
    return result["hookSpecificOutput"]["additionalContext"]


def test_reminders_fire_once_per_threshold_and_restart_after_compaction(home, tmp_path):
    path = rollout(tmp_path, user("do the thing"), tokens(5000))
    assert hooks.receive(event("PostToolUse", path, tmp_path)) == {}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(tokens(6500))
    first = hooks.receive(event("PostToolUse", path, tmp_path))
    assert "6500 tokens (65% of 10000)" in context(first) and "next natural break" in context(first)
    assert hooks.receive(event("UserPromptSubmit", path, tmp_path)) == {}  # same threshold, not again
    with path.open("a", encoding="utf-8") as stream:
        stream.write(tokens(8200))
    last = hooks.receive(event("UserPromptSubmit", path, tmp_path))
    assert last["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "run /compact" in context(last)
    hooks.receive(event("PreCompact", path, tmp_path, trigger="auto"))
    hooks.receive(event("SessionStart", path, tmp_path, source="compact"))
    assert "8200 tokens" in context(hooks.receive(event("PostToolUse", path, tmp_path)))


def test_token_thresholds_and_handoff_named(home, tmp_path):
    (home / "context-handoff").mkdir()
    (home / "context-handoff" / "hooks.json").write_text('{"thresholds": [3000]}')
    (tmp_path / "HANDOFF_today.md").write_text("handoff")
    path = rollout(tmp_path, tokens(3100, window=None))
    text = context(hooks.receive(event("PostToolUse", path, tmp_path)))
    assert "3100 tokens." in text and str(tmp_path / "HANDOFF_today.md") in text and "/compact" in text


def test_compaction_note_index_and_get(home, tmp_path):
    handoff = tmp_path / "HANDOFF.md"
    path = rollout(tmp_path, user("<environment_context>cwd</environment_context>"),
                   user("first request", "2026-10-10T10:00:00Z"),
                   line("response_item", {"type": "function_call", "name": "exec_command", "arguments": "{}"}),
                   user("second request", "2026-10-10T10:05:00Z"),
                   line("response_item", {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": "working on it"}]}))
    handoff.write_text("handoff")
    os.utime(handoff, (time.time(), time.time()))
    hooks.receive(event("PreCompact", path, tmp_path, trigger="manual"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line("compacted", {"message": "summary"}))
    text = context(hooks.receive(event("SessionStart", path, tmp_path, source="compact")))
    assert f"Read your handoff first: {handoff}" in text and "it is not a new request" in text
    ident = f"{SID[:8]}-1"
    data = hooks.manifest(ident)
    assert [s["user"] for s in data["sections"]][1:] == ["first request", "second request"]
    assert data["sections"][-1]["end"] < path.stat().st_size  # the compaction record itself is not indexed
    got = hooks.get(ident + "§3", 8000)
    assert "verified" in got and "user: second request" in got and "assistant: working on it" in got
    assert "first request" not in got


def test_stale_and_missing_handoff(home, tmp_path):
    handoff = tmp_path / "HANDOFF.md"
    handoff.write_text("old")
    os.utime(handoff, (0, 0))
    path = rollout(tmp_path, user("newest request", "2026-10-10T10:00:00Z"))
    hooks.receive(event("PreCompact", path, tmp_path))
    assert "saved before the user's latest message" in context(
        hooks.receive(event("SessionStart", path, tmp_path, source="compact")))
    handoff.unlink()
    hooks.receive(event("PreCompact", path, tmp_path))
    assert "No handoff file was found" in context(hooks.receive(event("SessionStart", path, tmp_path, source="compact")))


def test_second_compaction_indexes_only_new_part(home, tmp_path):
    path = rollout(tmp_path, user("before"))
    hooks.receive(event("PreCompact", path, tmp_path))
    hooks.receive(event("SessionStart", path, tmp_path, source="compact"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(user("after"))
    hooks.receive(event("PreCompact", path, tmp_path))
    hooks.receive(event("SessionStart", path, tmp_path, source="compact"))
    assert [s["user"] for s in hooks.manifest(f"{SID[:8]}-2")["sections"]] == ["after"]


def test_changed_bytes_are_refused(home, tmp_path):
    path = rollout(tmp_path, user("original words"))
    hooks.receive(event("PreCompact", path, tmp_path))
    hooks.receive(event("SessionStart", path, tmp_path, source="compact"))
    path.write_text(path.read_text(encoding="utf-8").replace("original", "replaced"), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since indexing"):
        hooks.get(f"{SID[:8]}-1§2", 8000)


def test_foreign_transcript_and_subagents_are_ignored(home, tmp_path):
    path = rollout(tmp_path, tokens(9900), sid="01a12345-0000-7000-8000-00000000ffff")
    with pytest.raises(ValueError, match="does not belong"):
        hooks.receive(event("PostToolUse", path, tmp_path))
    assert hooks.receive(event("PostToolUse", path, tmp_path, agent_id="worker")) == {}


def test_hook_errors_never_stop_the_host(home, tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "PostToolUse", "session_id": "x"})))
    assert hooks.main([]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "{}" and "skipped" in out.err


# ---- installer --------------------------------------------------------------------------------

def test_apply_keeps_existing_config_and_enables_hooks(home):
    config = home / "config.toml"
    original = 'model = "x"\n\n[features]\nother = true\nhooks = false\n\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "mine"\n'
    config.write_text(original, encoding="utf-8")
    installer.apply(dry=False)
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["features"] == {"other": True, "hooks": True}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine"
    assert {e: len(data["hooks"][e]) for e, *_ in installer.EVENTS} == {
        "PostToolUse": 1, "UserPromptSubmit": 1, "PreCompact": 1, "SessionStart": 1}
    assert data["hooks"]["SessionStart"][0]["matcher"] == "compact"
    assert list(home.glob("config.toml.bak-context-handoff-*"))
    installer.apply(dry=False)  # idempotent: still one block
    assert config.read_text(encoding="utf-8").count(installer.BEGIN) == 1
    installer.remove(dry=False)
    left = tomllib.loads(config.read_text(encoding="utf-8"))
    assert "PreCompact" not in left["hooks"] and left["features"]["hooks"] is True


def test_apply_adds_features_table_when_absent_and_dry_run_writes_nothing(home, capsys):
    installer.apply(dry=True)
    assert not (home / "config.toml").exists()
    installer.apply(dry=False)
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["features"]["hooks"] is True


def test_damaged_block_is_refused(home):
    (home / "config.toml").write_text(installer.BEGIN + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        installer.apply(dry=False)
