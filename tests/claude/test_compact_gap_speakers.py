"""Claude transcript provenance: real Oct 20 idle reminder must never become user."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/claude_program_messages.json").read_text(encoding="utf-8"))


@pytest.fixture
def cg(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_STATE_ROOT", str(tmp_path / "state"))
    sys.modules.pop("cw_paths", None)
    spec = importlib.util.spec_from_file_location("compact_gap_speakers", ROOT / "adapters/claude/hooks/compact_gap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_real_transcript_shapes(cg, case):
    result = cg.claude_msg(case["record"])
    if case["name"].startswith("queued-human"):
        assert result["role"] == "user"
    else:
        assert result is None


@pytest.mark.parametrize("origin", [{"kind": "plugin", "name": "another"}, {"kind": "hook"}, {"kind": "system"}, {"kind": "task-notification"}])
def test_nonhuman_origin_without_prefix(cg, origin):
    assert cg.claude_msg({"type": "user", "origin": origin, "message": {"content": "這是沒有任何前綴的自動程式訊息，不是使用者發言。"}}) is None


@pytest.mark.parametrize("text", [
    "The wife-compact plugin sent a message:\n沒有前綴的舊訊息",
    "〔閒置提醒（wife-compact 外掛送的，不是user）〕現在先叫 prepare。",
    "〔wife-compact：壓縮完成，續作〕讀交接，從下一步接著做。",
    "<task-notification>不要把背景通知當真人要求。</task-notification>",
    "<system-reminder>只是一段系統提醒</system-reminder>",
    "<system-reminder>提醒</system-reminder>\n<task-notification>背景工作完成</task-notification>",
])
def test_legacy_program_content_without_origin(cg, text):
    assert cg.claude_msg({"type": "user", "message": {"content": text}}) is None


def test_human_quotes_are_preserved_and_reminders_stripped(cg):
    text = "〔wife-compact：壓縮完成，續作〕這個提醒的說法請改好。"
    for content in (text, [{"type": "text", "text": text}]):
        row = {"type": "user", "origin": {"kind": "human"}, "message": {"content": content}}
        assert cg.claude_msg(row)["text"] == text
    row = {"type": "user", "message": {"content": "請看這段 wife-compact 的輸出。<system-reminder>隱藏提醒</system-reminder>"}}
    assert cg.claude_msg(row)["text"] == "請看這段 wife-compact 的輸出。"
    assert cg.claude_msg({"type": "assistant", "message": {"content": "我會先查清楚來源，再接著修改這個功能。"}})["role"] == "assistant"
    assert cg.claude_msg({"type": "assistant", "message": {"content": text}})["role"] == "assistant"
    assert cg.claude_msg({"type": "user", "message": {"content": '<cross-session-message from-name="reviewer">只讀審查回件</cross-session-message>'}})["role"] == "program"


@pytest.mark.parametrize("field,value", [("promptSource", "system"), ("turnOrigin", "system"), ("turnOrigin", "task_notification"), ("isMeta", True)])
def test_host_system_flags(cg, field, value):
    assert cg.claude_msg({"type": "user", field: value, "message": {"content": "沒有前綴的系統通知，不能變成user原話。"}}) is None


def test_segment_and_postcompact_pending_do_not_store_plugin_as_human(cg, tmp_path):
    idle = next(c["record"] for c in FIXTURE["cases"] if c["name"] == "idle-plugin-real")
    # segment() sees exactly Claude's compact JSON format, not only a direct helper call.
    rows = [
        {"type": "user", "uuid": "human", "timestamp": idle["timestamp"], "message": {"content": "請完成這個修改，不要動到 main 的未提交檔案。"}},
        copy.deepcopy(idle),
    ]
    transcript = tmp_path / "fixture.jsonl"
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows), encoding="utf-8")
    pending = cg.save_pending({"session_id": "fixture", "transcript_path": str(transcript), "compact_summary": "摘要", "prompt_id": "p1"})
    assert [m["id"] for m in pending["messages"]] == ["human"]
    rows += [{"type": "system", "subtype": "compact_boundary", "timestamp": idle["timestamp"]},
             {"type": "user", "isCompactSummary": True, "message": {"content": "摘要"}}]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows), encoding="utf-8")
    _, messages, _ = cg.segment(transcript)
    assert [m["id"] for m in messages] == ["human"]
    assert all("prepare" not in unit["text"] for unit in cg.sentences(messages))
