"""cw_sections：固定節號清單、用 <原文ID>§n 找回、內容變了要說（合成資料，不讀真資料）。"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters" / "claude"))


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CW_TOOL_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for name in ("cw_get", "cw_sections", "cw_segments", "cw_claude"):
        sys.modules.pop(name, None)
    get = importlib.import_module("cw_get")
    sec = importlib.import_module("cw_sections")
    monkeypatch.setattr(sec, "OUT", tmp_path / "originals")
    return get, sec


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


def user(uuid, ts, text):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": text}}


def said(uuid, ts, text):
    return {"type": "assistant", "uuid": uuid, "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


def test_build_get_and_changed(tmp_path, monkeypatch):
    get, sec = load(tmp_path, monkeypatch)
    sid = "abcd1234-0000"
    jsonl = tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl"
    rows = [
        user("old00000", "2026-09-29T12:00:00Z", "壓縮前的舊話"),
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-29T12:30:00Z"},
        user("aaaa1111", "2026-09-29T13:00:00Z", "老婆 第一件事"),
        said("a2", "2026-09-29T13:00:05Z", "好，做第一件"),
        user("bbbb2222", "2026-09-29T13:10:00Z", "第二件"),
        said("b2", "2026-09-29T13:10:05Z", "第二件做好了"),
    ]
    write(jsonl, rows)
    m = sec.build(sid)
    assert [s["n"] for s in m["sections"]] == [1, 2]  # 只切上一次壓縮之後
    oid = m["id"]
    assert oid.startswith("abcd1234@")

    text = get.get_section(oid, 2, 0)
    assert "原樣" in text and "第二件做好了" in text and "第一件" not in text

    with pytest.raises(FileExistsError):  # 同一個 ID 不覆寫
        with open(sec.OUT / f"{oid.replace('@', '_')}.json", "x"):
            pass

    rows[5] = said("b2", "2026-09-29T13:10:05Z", "被改過的回覆")
    write(jsonl, rows)
    assert "⚠ 內容跟建清單時不同" in get.get_section(oid, 2, 0)

    with pytest.raises(SystemExit):
        get.get_section(oid, 3, 0)
