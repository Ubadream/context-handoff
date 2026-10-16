"""context_watch：工具迴圈裡（PostToolUse）跨門檻也要提醒；每個門檻只冒一次（合成資料）。"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "adapters" / "claude" / "hooks" / "context_watch.py"


def usage(tokens):
    return json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10}}}) + "\n"


def run(tmp_path, transcript, event):
    data = {"transcript_path": str(transcript), "session_id": "s1", "hook_event_name": event}
    out = subprocess.run([sys.executable, "-X", "utf8", str(HOOK)], input=json.dumps(data), capture_output=True,
                         text=True, encoding="utf-8", env={**os.environ, "CW_STATE_ROOT": str(tmp_path / "state")})
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_reminds_inside_a_tool_loop_once_per_threshold(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(usage(120_000), encoding="utf-8")
    assert run(tmp_path, transcript, "PostToolUse") is None
    with transcript.open("a", encoding="utf-8") as f:
        f.write(usage(420_000))
    first = run(tmp_path, transcript, "PostToolUse")["hookSpecificOutput"]
    assert first["hookEventName"] == "PostToolUse" and "剛跨過 40 萬" in first["additionalContext"]
    assert "工具迴圈" in first["additionalContext"] and "B " not in first["additionalContext"]
    assert run(tmp_path, transcript, "PostToolUse") is None
    assert run(tmp_path, transcript, "UserPromptSubmit") is None  # 同一門檻換事件也不重冒
    with transcript.open("a", encoding="utf-8") as f:
        f.write(usage(610_000))
    assert "剛跨過 60 萬" in run(tmp_path, transcript, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
