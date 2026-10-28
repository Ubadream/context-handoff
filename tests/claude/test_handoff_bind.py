import importlib.util
import json
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "adapters" / "claude" / "hooks" / "handoff_bind.py"
spec = importlib.util.spec_from_file_location("handoff_bind_claude", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def write_line(ts, path):
    return json.dumps({"type": "assistant", "timestamp": ts, "cwd": str(Path(path).parent), "isSidechain": False,
                       "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]}})


def test_recopied_old_message_after_compaction_does_not_hide_newer_write(tmp_path):
    handoff = tmp_path / "HANDOFF_a.md"
    handoff.write_text("x", encoding="utf8")
    transcript = tmp_path / "t.jsonl"
    # 壓縮後檔尾會重抄舊訊息（帶舊時間），不能蓋掉較晚的真寫入
    transcript.write_text("\n".join([write_line("2026-10-26T07:55:46Z", str(handoff)),
                                     write_line("2026-10-26T14:16:54Z", str(handoff)),
                                     write_line("2026-10-26T07:55:46Z", str(handoff))]) + "\n", encoding="utf8")
    assert [t for _, t in hook.written_handoffs(str(transcript))] == ["2026-10-26T14:16:54Z"]
