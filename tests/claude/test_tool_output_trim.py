"""tool_output_trim：各工具大輸出外置，形狀不變；Read 只截前段不另存；指定大小的不動。"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "adapters" / "claude" / "hooks" / "tool_output_trim.py"


def run(tmp_path, monkeypatch, tool, tool_input, response):
    env = {**__import__("os").environ, "CW_STATE_ROOT": str(tmp_path)}
    r = subprocess.run([sys.executable, "-X", "utf8", str(HOOK)], input=json.dumps(
        {"tool_name": tool, "tool_input": tool_input, "tool_response": response}),
        capture_output=True, text=True, encoding="utf-8", env=env)
    return json.loads(r.stdout)["hookSpecificOutput"]["updatedToolOutput"] if r.stdout.strip() else None


def shape(v):
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(x) for x in v]
    return type(v).__name__


def stored(tmp_path):
    return list((tmp_path / "claude-tool-output").glob("*/*.txt"))


def test_grep_web_and_mcp_keep_shape(tmp_path, monkeypatch):
    big = "\n".join(f"{i}: line {i} " + "x" * 40 for i in range(400))
    cases = [
        ("Grep", {"pattern": "x"}, {"mode": "content", "numFiles": 0, "filenames": [], "content": big, "numLines": 400}),
        ("WebFetch", {"url": "https://a.b", "prompt": "p"}, {"bytes": 1, "code": 200, "codeText": "OK", "result": big, "durationMs": 5, "url": "https://a.b"}),
        ("mcp__Claude_Browser__get_page_text", {"tabId": 1}, [{"type": "text", "text": big}]),
    ]
    for tool, inp, resp in cases:
        out = run(tmp_path, monkeypatch, tool, inp, resp)
        assert out is not None and shape(out) == shape(resp), tool
        text = json.dumps(out, ensure_ascii=False)
        assert "中間略過" in text and "line 0 " in text and "line 399 " in text and len(text) < len(big) / 2
    assert len(stored(tmp_path)) == 3  # 每個工具一份
    run(tmp_path, monkeypatch, *cases[0])
    assert len(stored(tmp_path)) == 3  # 同一份輸出再來一次不重存


def test_small_or_asked_for_size_untouched(tmp_path, monkeypatch):
    big = "y" * 10000
    assert run(tmp_path, monkeypatch, "Grep", {"pattern": "y"}, {"content": "short"}) is None
    assert run(tmp_path, monkeypatch, "Grep", {"pattern": "y", "head_limit": 0}, {"content": big}) is None
    assert run(tmp_path, monkeypatch, "Bash", {"command": "cat a # full-output"}, {"stdout": big, "stderr": ""}) is None
    assert run(tmp_path, monkeypatch, "Read", {"file_path": "a", "limit": 5000}, {"type": "text", "file": {"content": big * 3}}) is None


def test_bash_still_trimmed(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, "Bash", {"command": "ls"}, {"stdout": "z" * 5000, "stderr": "", "interrupted": False})
    assert "中間略過" in out["stdout"] and out["interrupted"] is False
    assert stored(tmp_path)[0].read_text(encoding="utf-8").startswith("# command: ls\n")


def test_read_keeps_head_lines_without_copy(tmp_path, monkeypatch):
    content = "\n".join(f"row {i} " + "r" * 60 for i in range(1, 1001))
    resp = {"type": "text", "file": {"filePath": "a.py", "content": content, "numLines": 1000, "startLine": 1, "totalLines": 1000}}
    out = run(tmp_path, monkeypatch, "Read", {"file_path": "a.py"}, resp)
    assert shape(out) == shape(resp)
    lines = out["file"]["content"].split("\n")
    assert lines[0].startswith("row 1 ") and lines[-2].startswith(f"row {len(lines) - 1} ")
    assert f"offset={len(lines)}" in lines[-1] and out["file"]["numLines"] == len(lines)
    assert len(out["file"]["content"]) < 13000 and not stored(tmp_path)
