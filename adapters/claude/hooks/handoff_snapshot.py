"""PreCompact：壓縮當下，把這個視窗寫過的交接全文連同 SHA-256 存一份。

交接檔是原地覆寫的，事後拿不到「壓縮那一刻」的版本；記憶庫（wife-memory 經歷層）要拿這份快照當來源。

存放（CW_STATE_ROOT/context-workbench/handoff-snapshots/）：
  blobs/<sha256>.md   內容，同內容只存一次
  index.jsonl         每次壓縮每份交接一行：視窗、壓縮時間、觸發方式、原路徑、sha256、大小、
                      檔案修改時間、工具紀錄裡最後一次寫它的時間；修改時間比工具紀錄晚（被腳本改過、
                      工具掃不到）就標 changed_outside_tools

不輸出任何東西，失敗也不擋壓縮。
"""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cw_paths import WORKBENCH_STATE  # noqa: E402

ROOT = WORKBENCH_STATE / "handoff-snapshots"


def written_handoffs(transcript):
    spec = importlib.util.spec_from_file_location("handoff_bind", HERE / "handoff_bind.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.written_handoffs(transcript)


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def later_than_tools(mtime, tool_at):
    """工具呼叫的時間記在寫檔之前；晚超過一分鐘才算是工具紀錄外的改動。"""
    try:
        return mtime - datetime.fromisoformat(tool_at.replace("Z", "+00:00")).timestamp() > 60
    except (AttributeError, ValueError):
        return False


def snapshot(event, now=None):
    transcript = event.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return []
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = []
    for path, tool_at in written_handoffs(transcript):
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        blob = ROOT / "blobs" / f"{digest}.md"
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=blob.parent, delete=False) as f:
                f.write(data)
            os.replace(f.name, blob)
        mtime = utc(os.path.getmtime(path))
        rows.append({"session": event.get("session_id"), "compacted_at": now, "trigger": event.get("trigger"),
                     "path": path, "sha256": digest, "bytes": len(data), "mtime": mtime,
                     "last_tool_write": tool_at, "changed_outside_tools": later_than_tools(os.path.getmtime(path), tool_at)})
    if rows:
        ROOT.mkdir(parents=True, exist_ok=True)
        with open(ROOT / "index.jsonl", "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def main():
    try:
        snapshot(json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}"))
    except Exception as exc:  # noqa: BLE001 — 快照失敗不能擋壓縮
        print(f"handoff_snapshot skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
