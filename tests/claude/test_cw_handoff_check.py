"""cw_handoff_check 的程式部分：交接寫了沒、多新、之後又發生多少事（合成資料；handoff_bind.py 用倉庫 adapters/claude/hooks 那份）。"""
import importlib
import json
import os
from types import SimpleNamespace
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters" / "claude"))


def write(path, rows, separators):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=separators) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": ")])
def test_facts(tmp_path, monkeypatch, separators):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    for name in ("cw_handoff_check", "cw_segments", "cw_claude"):
        sys.modules.pop(name, None)
    hc = importlib.import_module("cw_handoff_check")
    handoff = tmp_path / "HANDOFF_x.md"
    handoff.write_text("交接", encoding="utf-8")
    bash_path = handoff.as_posix()
    if handoff.drive:  # Windows fixtures exercise the Git Bash /c/... spelling.
        bash_path = "/" + bash_path[0].lower() + bash_path[2:]
    sid = "abcd1234-0000"
    say = lambda ts, text: {"type": "user", "uuid": ts, "timestamp": ts, "message": {"role": "user", "content": text}}
    tool = lambda ts, name, inp: {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
    write(tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl", [
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-29T13:00:00Z"},
        say("2026-09-29T13:01:00Z", "老婆 做吧"),
        tool("2026-09-29T13:02:00Z", "Bash", {"command": f"cat >> {bash_path} <<'EOF'\n- 做到這\nEOF"}),
        say("2026-09-29T13:03:00Z", "好啊老婆"),
        tool("2026-09-29T13:04:00Z", "Edit", {"file_path": "C:/x.py"}),
    ], separators)
    with (tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('null\n[]\n42\n"unrelated string"\n{broken JSON\n')
    f = hc.facts(hc.cw_claude.find_session("abcd1234"))
    assert f["written_since_compaction"] and f["said_after_handoff"] == 1 and f["edits_after_handoff"] == 1
    assert "之後老公又說了 1 句、改了 1 次檔" in hc.verdict(f)


@pytest.mark.parametrize("platform, source, expected", [
    ("posix", "/c/work/HANDOFF.md", "/c/work/HANDOFF.md"),
    ("posix", "/tmp/HANDOFF.md", "/tmp/HANDOFF.md"),
    ("nt", "/c/work/HANDOFF.md", "C:/work/HANDOFF.md"),
    ("nt", "C:/work/HANDOFF.md", "C:/work/HANDOFF.md"),
])
def test_handoff_shell_path_platform(monkeypatch, platform, source, expected):
    hc = importlib.import_module("cw_handoff_check")
    hook = hc.load_handoff_bind()
    monkeypatch.setattr(hook, "os", SimpleNamespace(name=platform, path=os.path))
    assert hook.bash_path(source) == expected
