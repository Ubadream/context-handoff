"""compact_gap：補回的原話附上交接快照裡的後續狀態；快照晚到就等user下一句再補一次（合成資料）。"""
import importlib.util
import io
import json
import sys
import time
from pathlib import Path

HOOK = Path(__file__).parents[2] / "adapters" / "claude" / "hooks" / "compact_gap.py"
CODEX_SID = "11111111-1111-1111-1111-111111111111"
CLAUDE_SID = "22222222-2222-2222-2222-222222222222"


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_STATE_ROOT", str(tmp_path / "state"))
    config = tmp_path / "thresholds.json"
    config.write_text(json.dumps({"claude": {"compact_gap": {"provider": "none", "snapshot_dir": str(tmp_path / "logs")}}}), encoding="utf-8")
    monkeypatch.setenv("CW_THRESHOLDS", str(config))
    sys.modules.pop("cw_paths", None)
    spec = importlib.util.spec_from_file_location("compact_gap", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def field(text, *mids, sid=CODEX_SID, provider="codex"):
    if not text:
        return {"text": "本次提供區間未記錄", "provenance": "當時未記錄", "evidence": []}
    return {"text": text, "provenance": "recorded",
            "evidence": [{"source_id": f"{provider}:{sid}:{m}:b0:{'0' * 64}", "quote": "…"} for m in mids]}


def item(name, vote_mid, conclusion, unresolved="", sid=CODEX_SID, provider="codex"):
    return {"work_item": name, "original_vote": field("原票", vote_mid, sid=sid, provider=provider),
            "new_material": field(""), "turn": field(""),
            "conclusion": field(conclusion, vote_mid + "-later", sid=sid, provider=provider),
            "unresolved": field(unresolved, vote_mid + "-later", sid=sid, provider=provider)}


def write_snapshot(tmp_path, stamp, sid, items):
    snap = tmp_path / "handoffs" / f"{stamp}_{sid[:8]}.snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"schema": "context-handoff.snapshot/v1", "status": "CANDIDATE",
                                "snapshots": items}, ensure_ascii=False), encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{stamp}_{sid[:8]}.123.status.json").write_text(
        json.dumps({"status": "CANDIDATE", "snapshot": str(snap)}), encoding="utf-8")


def test_matched_line_gets_state_and_same_item_once(tmp_path, monkeypatch):
    cg = load(monkeypatch, tmp_path)
    write_snapshot(tmp_path, "20261018-100000", CODEX_SID, [item("舊", "msg_a", "舊結論")])
    write_snapshot(tmp_path, "20261018-110000", CODEX_SID,
                   [item("README 圖", "msg_a", "圖已推上去 98c5b58", "native CI 還在跑")])
    lines, missing = cg.attach_states([("msg_a", "- user：做那張圖"), ("msg_a", "- user：用新數據"),
                                       ("msg_x", "- user：別的事"), (None, "- user：沒 id")], CODEX_SID)
    assert "結論：圖已推上去 98c5b58" in lines[0] and "未決：native CI 還在跑" in lines[0]
    assert "舊結論" not in lines[0]  # 同一則訊息取最新那份快照
    assert "模型整理、未人工核" in lines[0] and "轉折" not in lines[0]  # 當時未記錄的欄位不寫
    assert lines[1].endswith("同上一項")
    assert lines[2] == "- user：別的事" and lines[3] == "- user：沒 id"
    assert missing == ["msg_x"]


def test_claude_uuid_and_other_session_not_mixed(tmp_path, monkeypatch):
    cg = load(monkeypatch, tmp_path)
    uuid = "5b0c6e2e-1111-2222-3333-444455556666"
    write_snapshot(tmp_path, "20261018-120000", CLAUDE_SID,
                   [item("快照", uuid, "handoff_snapshot 已上線", sid=CLAUDE_SID, provider="claude")])
    assert "已上線" in cg.attach_states([(uuid, "- x")], CLAUDE_SID)[0][0]
    # 同前 8 碼不同視窗的證據不能拿來配
    other = CLAUDE_SID[:8] + "-ffff-ffff-ffff-ffffffffffff"
    assert cg.attach_states([(uuid, "- x")], other) == (["- x"], [uuid])


def test_late_snapshot_delivered_once_on_next_prompt(tmp_path, monkeypatch):
    cg = load(monkeypatch, tmp_path)
    cg.save_followup(CODEX_SID, "k1", [{"id": "msg_b", "line": "10-18 21:00 user：拆外掛…"}])
    assert cg.deliver_followup(CODEX_SID) == (None, None)  # 快照還沒出來：留著
    write_snapshot(tmp_path, "20261018-130000", CODEX_SID, [item("拆外掛", "msg_b", "已拆成三包")])
    text, stat = cg.deliver_followup(CODEX_SID)
    assert "拆外掛…" in text and "結論：已拆成三包" in text and stat["followup"] == 1
    assert cg.deliver_followup(CODEX_SID) == (None, None)  # 只補一次


def test_followup_expires_and_broken_snapshot_is_ignored(tmp_path, monkeypatch):
    cg = load(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"20261018-140000_{CODEX_SID[:8]}.9.status.json").write_text("not json", encoding="utf-8")
    (logs / f"20261018-140100_{CODEX_SID[:8]}.9.status.json").write_text(
        json.dumps({"snapshot": str(tmp_path / "missing.json")}), encoding="utf-8")
    assert cg.snapshots_for(CODEX_SID) == []
    cg.save_followup(CODEX_SID, "k1", [{"id": "msg_c", "line": "x"}])
    record = json.loads(cg.followup_path(CODEX_SID).read_text(encoding="utf-8"))
    record["created"] = time.time() - cg.FOLLOWUP_TTL - 1
    cg.followup_path(CODEX_SID).write_text(json.dumps(record), encoding="utf-8")
    assert cg.deliver_followup(CODEX_SID)[1]["reason"].startswith("快照一小時內")
    assert not cg.followup_path(CODEX_SID).exists()


def test_prompt_hook_delivers_followup_without_pending(tmp_path, monkeypatch, capsys):
    cg = load(monkeypatch, tmp_path)
    cg.save_followup(CODEX_SID, "k1", [{"id": "msg_d", "line": "user：接 compact_gap"}])
    write_snapshot(tmp_path, "20261018-150000", CODEX_SID, [item("compact_gap", "msg_d", "分支已推")])
    data = {"hook_event_name": "UserPromptSubmit", "session_id": CODEX_SID, "transcript_path": ""}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode())))
    monkeypatch.setattr(sys, "argv", ["compact_gap.py"])
    cg.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "結論：分支已推" in out["hookSpecificOutput"]["additionalContext"]


def test_message_ids_carried_from_both_hosts(tmp_path, monkeypatch):
    cg = load(monkeypatch, tmp_path)
    codex = {"type": "response_item", "timestamp": "t",
             "payload": {"type": "message", "role": "user", "id": "msg_e", "content": [{"text": "user要的那件事請照做"}]}}
    claude = {"type": "user", "uuid": "u-1", "timestamp": "t", "message": {"content": "README 那張壓縮前後圖要用這幾個禮拜的新數據來畫"}}
    assert cg.codex_msg(codex)["id"] == "msg_e" and cg.claude_msg(claude)["id"] == "u-1"
    assert cg.sentences([cg.claude_msg(claude)])[0]["id"] == "u-1"
