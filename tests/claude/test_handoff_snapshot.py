"""handoff_snapshot（PreCompact）：壓縮當下存交接全文＋SHA-256，同內容只存一次，標出工具紀錄外的改動（合成資料）。"""
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

HOOK = Path(__file__).parents[2] / "adapters" / "claude" / "hooks" / "handoff_snapshot.py"


def load(monkeypatch, tmp_path):
    monkeypatch.setenv("CW_STATE_ROOT", str(tmp_path / "state"))
    import sys
    sys.modules.pop("cw_paths", None)
    spec = importlib.util.spec_from_file_location("handoff_snapshot", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def transcript(tmp_path, handoff, ts):
    tool = {"type": "assistant", "timestamp": ts, "cwd": str(tmp_path),
            "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(handoff)}}]}}
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(tool) + "\n", encoding="utf-8")
    return path


def test_snapshot_keeps_the_version_at_compaction(tmp_path, monkeypatch):
    hs = load(monkeypatch, tmp_path)
    handoff = tmp_path / "HANDOFF_x.md"
    handoff.write_text("第一版", encoding="utf-8")
    now = time.time()
    os.utime(handoff, (now, now))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    event = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, handoff, stamp)), "trigger": "manual"}
    first = hs.snapshot(event)
    handoff.write_text("第二版：原地覆寫", encoding="utf-8")
    os.utime(handoff, (now, now))
    hs.snapshot(event)
    hs.snapshot(event)  # 內容沒變，blob 不重存，索引照記
    root = tmp_path / "state" / "context-workbench" / "handoff-snapshots"
    rows = [json.loads(l) for l in (root / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3 and rows[0]["trigger"] == "manual" and rows[0]["session"] == "s1"
    assert (root / "blobs" / f"{first[0]['sha256']}.md").read_text(encoding="utf-8") == "第一版"
    assert len(list((root / "blobs").iterdir())) == 2
    assert rows[1]["sha256"] == hashlib.sha256("第二版：原地覆寫".encode()).hexdigest()
    assert not any(r["changed_outside_tools"] for r in rows)


def test_change_by_script_is_flagged(tmp_path, monkeypatch):
    hs = load(monkeypatch, tmp_path)
    handoff = tmp_path / "HANDOFF_x.md"
    handoff.write_text("被腳本改過", encoding="utf-8")
    event = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, handoff, "2026-10-10T10:00:00Z"))}
    assert hs.snapshot(event)[0]["changed_outside_tools"] is True


def test_nothing_written_and_never_raises(tmp_path, monkeypatch, capsys):
    hs = load(monkeypatch, tmp_path)
    assert hs.snapshot({"transcript_path": str(tmp_path / "missing.jsonl")}) == []
    import io, sys
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert hs.main() == 0 and "skipped" in capsys.readouterr().err
