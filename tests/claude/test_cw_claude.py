"""用合成的對話紀錄測 Claude 接頭（不讀真資料）。"""
import importlib.util
import json
from pathlib import Path

SOURCE = Path(__file__).parents[2] / "adapters" / "claude" / "cw_claude.py"


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("CW_COMPACT_GAP_DIR", str(tmp_path / "gap"))
    monkeypatch.setenv("CW_WIFE_COMPACT_DIR", str(tmp_path / "wc"))
    spec = importlib.util.spec_from_file_location("cw_claude", SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


def boundary(ts):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts,
            "compactMetadata": {"trigger": "manual", "preTokens": 300000, "durationMs": 1000}}


def summary(text):
    return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": text}}


def test_compactions_dedupe_and_match(tmp_path, monkeypatch):
    cw = load(tmp_path, monkeypatch)
    sid = "abcd1234-0000"
    write(tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl", [
        {"type": "user", "cwd": "C:/x", "message": {"role": "user", "content": "老婆 開始"}},
        boundary("2026-09-28T10:00:00.500Z"), summary("第一次摘要"),
        boundary("2026-09-28T10:00:00.500Z"),  # 續開的對話帶著同一個舊分界
        boundary("2026-09-28T12:00:00.500Z"), summary("第二次摘要"),
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90}}},
    ])
    # compact_gap 用本機時間、只到秒，比分界早零點幾秒
    local = lambda utc: __import__("datetime").datetime.fromisoformat(utc + "+00:00").astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    write(tmp_path / "gap" / "log.jsonl", [
        {"at": local("2026-09-28T12:00:00"), "session": sid, "source": "PostCompact"},
        {"at": local("2026-09-28T12:01:00"), "session": sid, "source": "prompt", "picked": 3, "picked_text": "三句"},
    ])
    (tmp_path / "wc").mkdir()
    (tmp_path / "wc" / "trace.log").write_text(
        "2026-09-28T10:00:05.000Z v0.1.9 session.compact C 完成 messages=2 skip=-\n"
        "2026-09-28T12:00:03.000Z v0.1.9 session.compact trigger=auto pending=- agentId=- messages=9 instructions=0\n",
        encoding="utf-8")
    items = cw.compactions(cw.find_session("abcd"))
    assert [c["summary_chars"] for c in items] == [5, 5]
    assert [c["mode"] for c in items] == ["C", "builtin"]
    assert items[0]["compact_gap"]["picked"] is None and items[1]["compact_gap"]["picked"] == 3
    row = cw.session_row(cw.find_session("abcd"))
    assert row["compaction_count"] == 2 and row["context_tokens"] == 100 and row["first_prompt"] == "老婆 開始"


def test_picked_lines_resolve_to_message(tmp_path, monkeypatch):
    cw = load(tmp_path, monkeypatch)
    sid = "pick0001"
    write(tmp_path / "claude" / "projects" / "p" / f"{sid}.jsonl", [
        {"type": "assistant", "uuid": "u-early", "timestamp": "2026-09-28T02:00:00Z",
         "message": {"content": [{"type": "text", "text": "先不動 **registry.json 正本**，只改測試副本。"}]}},
        {"type": "assistant", "uuid": "u-quote", "timestamp": "2026-09-28T03:00:00Z",
         "message": {"content": [{"type": "text", "text": "剛才說過：先不動 registry.json 正本，只改測試副本。"}]}},
        {"type": "attachment", "uuid": "u-cross", "timestamp": "2026-09-28T02:10:00Z",
         "attachment": {"type": "queued_command", "prompt": "<cross-session-message from=\"x\">排程都掛上主控台追蹤</cross-session-message>"}},
        boundary("2026-09-28T04:00:00.500Z"), summary("摘要"),
    ])
    at = lambda utc: __import__("datetime").datetime.fromisoformat(utc + "+00:00").astimezone().strftime("%Y-%m-%d %H:%M")
    picked = ("〔摘要沒蓋到的原話：…〕\n"
              f"- {at('2026-09-28T02:00:00')} 老婆：先不動 registry.json 正本，只改測試副本。（Jev 0.85）\n"
              f"- {at('2026-09-28T02:10:00')} 老公：排程都掛上主控台追蹤（Jev 0.80）")
    local = lambda utc: __import__("datetime").datetime.fromisoformat(utc + "+00:00").astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    write(tmp_path / "gap" / "log.jsonl", [
        {"at": local("2026-09-28T04:01:00"), "session": sid, "source": "prompt", "picked": 2, "picked_text": picked}])
    items = cw.compactions(cw.find_session("pick"))[0]["compact_gap"]["picked_items"]
    assert [i["source"]["message_uuid"] for i in items] == ["u-early", "u-cross"]  # 引用它的後文不算
    assert items[0]["jev"] == 0.85 and items[1]["source"]["kind"] == "cross_session"


def test_missing_tools_give_null(tmp_path, monkeypatch):
    cw = load(tmp_path, monkeypatch)
    write(tmp_path / "claude" / "projects" / "p" / "s1.jsonl", [boundary("2026-09-28T10:00:00Z"), summary("摘要")])
    item = cw.compactions(cw.find_session("s1"))[0]
    assert item["mode"] is None and item["compact_gap"] is None and item["originals_file"] is None
    assert cw.handoffs(cw.find_session("s1")) == []  # handoff_bind 跟著倉庫走，一定在；這個視窗沒寫過交接
    assert cw.session_row(cw.find_session("s1"))["context_tokens"] is None  # 壓完還沒有新回應
