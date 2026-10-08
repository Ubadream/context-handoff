"""cw_get：用工具編號、段號找回原文（合成資料，不讀真資料）。"""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters" / "claude"))


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CW_TOOL_CALLS", str(tmp_path / "calls"))
    for name in ("cw_get", "cw_segments", "cw_claude"):
        sys.modules.pop(name, None)
    return importlib.import_module("cw_get")


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


def test_tool_and_segment(tmp_path, monkeypatch):
    cw = load(tmp_path, monkeypatch)
    sid = "abcd1234-0000"
    call = "0929-221021~221022_Bash_ls_XyZ9"
    write(tmp_path / "calls" / "20260929" / f"{call}.json", [{
        "id": call, "session": sid, "tool_use_id": "toolu_XyZ9", "group": "指令", "status": "ok",
        "started": "2026-09-29T22:10:21+08:00", "finished": "2026-09-29T22:10:22+08:00",
        "input": {"command": "ls"}, "response": {"stdout": "a" * 50}, "error": None}])
    write(tmp_path / "calls" / "by-session" / f"{sid}.jsonl", [{"u": "toolu_XyZ9", "id": call, "status": "ok"}])
    text = cw.get_tool(call, 10)
    assert "指令｜ok" in text and "後面還有 " in text

    write(tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl", [
        {"type": "user", "uuid": "aaaa1111", "timestamp": "2026-09-29T13:00:00Z", "message": {"role": "user", "content": "老婆 看一下"}},
        {"type": "assistant", "uuid": "bbbb", "message": {"content": [{"type": "tool_use", "id": "toolu_XyZ9", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "uuid": "cccc", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_XyZ9", "content": "a"}]}},
        {"type": "assistant", "uuid": "dddd", "message": {"content": [{"type": "text", "text": "看完了"}]}},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-29T13:05:00Z"},  # 壓掉的段也找得回來
        {"type": "user", "uuid": "eeee2222", "timestamp": "2026-09-29T13:10:00Z", "message": {"role": "user", "content": "下一段"}},
    ])
    seg = cw.get_segment("abcd1234", "aaaa1111", 0)
    assert "老公：老婆 看一下" in seg and f"｜{call}〕" in seg and "老婆：看完了" in seg and "下一段" not in seg
