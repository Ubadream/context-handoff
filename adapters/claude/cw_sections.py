"""原文分節（只讀 Claude 對話紀錄，另寫一份固定的分節清單）。

  python adapters/claude/cw_sections.py build <session>   # 上一次壓縮之後到現在，切節、寫清單、印目錄
  python adapters/claude/cw_sections.py list <原文ID>      # 印那份清單的目錄

原文正本是 Claude 的 jsonl；這裡不另存原文，只存「第幾節＝哪幾則訊息」的清單。
- 原文 ID：<session 前 8 碼>@<建立時間>，例如 9bfa4859@20260929T233012。清單寫一次就不再改，
  所以同一個 ID 的節號永遠指同一段；之後再切是另一個 ID。
- 一節＝老公（或別的視窗）一句話開頭，到下一句為止，跟 cw_segments 的段同一套切法；
  每節記開頭訊息的 uuid（不會變）和內容 hash，找回時對得上才算原樣。
- 引用寫 <原文ID>§<節號>；找回用 cw_get.py <原文ID>§<節號>。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_claude  # noqa: E402
import cw_segments  # noqa: E402

OUT = Path(os.environ.get("CW_ORIGINALS", cw_claude.STATE / "context-workbench" / "originals"))
INDEX_LINES = 40  # 目錄最多列幾節（給上下文看的；清單本身全收）


def section_hash(first_uuid, prompt, said):
    h = hashlib.sha256()
    for part in [first_uuid or "", prompt, *said]:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def opener(text, n=36):
    one = " ".join(cw_segments.REMINDER.sub("", text).split())
    return one if len(one) <= n else one[:n] + "…"


def index_lines(manifest):
    secs = manifest["sections"]
    rows = [f"§{s['n']} {s['start'][11:16]} {s['opened_by']}「{s['opener']}」"
            + (f"（工具 {s['tools']}）" if s["tools"] else "") for s in secs[:INDEX_LINES]]
    if len(secs) > INDEX_LINES:
        rows.append(f"…另有 {len(secs) - INDEX_LINES} 節，用 list {manifest['id']} 看全部")
    return rows


def build(session):
    path = cw_claude.find_session(session)
    segs = cw_segments.segment(path)
    if not segs:
        sys.exit(f"{path.stem[:8]} 上一次壓縮之後沒有老公開頭的段，沒東西可分節")
    now = datetime.now()
    oid = f"{path.stem[:8]}@{now.strftime('%Y%m%dT%H%M%S')}"
    sections = [{
        "n": i,
        "segment_id": s["id"],
        "first_uuid": s["first_uuid"],
        "last_uuid": s["message_uuids"][-1],
        "messages": len(s["message_uuids"]),
        "start": s["start"],
        "end": s["end"] or s["start"],
        "opened_by": s["opened_by"],
        "opener": opener(s["prompt"]),
        "tools": len(s["tools"]),
        "chars": s["text_chars"],
        "hash": section_hash(s["first_uuid"], s["prompt"], s["said"]),
    } for i, s in enumerate(segs, 1)]
    manifest = {
        "id": oid,
        "session": path.stem,
        "transcript": str(path),
        "created": now.isoformat(timespec="seconds"),
        "from": sections[0]["start"],
        "to": sections[-1]["end"],
        "sections": sections,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{oid.replace('@', '_')}.json", "x", encoding="utf-8") as f:  # 同名就失敗：清單不覆寫
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def load(oid):
    p = OUT / f"{oid.replace('@', '_')}.json"
    if not p.exists():
        sys.exit(f"找不到原文 {oid} 的分節清單（{p}）")
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="原文分節：固定的節號清單，引用寫 <原文ID>§<節號>")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build").add_argument("session")
    sub.add_parser("list").add_argument("id")
    args = ap.parse_args()
    m = build(args.session) if args.cmd == "build" else load(args.id)
    head = f"原文 {m['id']}：{len(m['sections'])} 節，{m['from']} ～ {m['to']}"
    sys.stdout.buffer.write(("\n".join([head, *index_lines(m)]) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
