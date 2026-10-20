"""The installed Codex receiver merges restored wording with existing handoff output."""
import importlib.util
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[2]


def test_codex_real_receiver_restores_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    sid = "11111111-1111-1111-1111-111111111111"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = [{"type": "session_meta", "payload": {"id": sid}},
            {"type": "response_item", "timestamp": now,
             "payload": {"type": "message", "role": "user", "id": "original-1",
                         "content": [{"type": "input_text", "text": "Do not change the source directory during migration."}]}}]
    path = tmp_path / "rollout-fixture.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("codex_gap_test", ROOT / "adapters/codex/hooks/cw_codex_hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    event = {"session_id": sid, "transcript_path": str(path), "cwd": str(tmp_path)}
    hooks.receive({**event, "hook_event_name": "PreCompact"})
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "compacted", "timestamp": now,
                                 "payload": {"message": "Migration implementation is ready."}}) + "\n")
    result = hooks.receive({**event, "hook_event_name": "SessionStart", "source": "compact"})
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "Do not change the source directory" in text
    assert "indexed as" in text
    assert "Do not change" not in hooks.receive({**event, "hook_event_name": "SessionStart", "source": "compact"})["hookSpecificOutput"]["additionalContext"]
