"""用編號找回原文（只讀）：工具呼叫編號，或分段的段號。

  python adapters/claude/cw_get.py <編號> [--full] [--max 20000]

  工具編號  0929-221021~221021_Bash_tail_sXtR（壓縮後那一行「〔工具 … → 完成｜編號〕」裡的）
            → tool_archive.py 存的全文：輸入、回傳、錯誤
  原文節號  9bfa4859@20260929T233012§3（cw_sections.py 的固定清單；C 壓縮後那行會列目錄）
            → 那一節的原文，並對 hash 說是不是原樣
  段號      9bfa4859#1a2b3c4d（cw_segments.py 給的）
            → 那一段的原文：開頭那句話、老婆的回覆、工具呼叫（附編號），到下一段開頭為止；
              不限上一次壓縮之後，已經壓掉的段也找得回來

預設每個欄位超過 --max 字會截斷並說明，--full 不截。輸出是給人看的純文字。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_claude  # noqa: E402
import cw_sections  # noqa: E402
import cw_segments  # noqa: E402

TOOL_CALLS = Path(os.environ.get("CW_TOOL_CALLS", cw_claude.STATE / "context-workbench" / "tool-calls"))
TOOL_ID = re.compile(r"^\d{4}-\d{6}~\d{6}_.+_[A-Za-z0-9_-]{1,4}$")
SECTION_ID = re.compile(r"^([0-9a-f]{8}@\d{8}T\d{6})[§:](\d+)$")
SEGMENT_ID = re.compile(r"^([0-9a-f]{4,})#([0-9a-f-]{4,})$")


def clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1)
    if limit and len(text) > limit:
        return f"{text[:limit]}\n…（後面還有 {len(text) - limit} 字，加 --full 看全部）"
    return text


def get_tool(call_id, limit):
    hits = sorted(TOOL_CALLS.glob(f"*/{call_id}.json"))
    if not hits:
        sys.exit(f"找不到工具編號 {call_id}（存在 {TOOL_CALLS}，留 30 天）")
    r = json.loads(hits[-1].read_text(encoding="utf-8"))
    out = [f"{r['id']}｜{r['group']}｜{r['status']}｜{r.get('started') or '?'} → {r['finished']}｜對話 {(r.get('session') or '')[:8]}",
           "", "【輸入】", clip(r.get("input"), limit)]
    if r.get("error"):
        out += ["", "【錯誤】", clip(r["error"], limit)]
    out += ["", "【回傳】", clip(r.get("response"), limit)]
    return "\n".join(out)


def call_ids(session):
    ids = {}
    path = TOOL_CALLS / "by-session" / f"{session}.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ids[r["u"]] = r["id"]
            except (ValueError, KeyError):
                pass
    return ids


def get_segment(sid8, uuid8, limit, parts=None):
    """parts 給一個 dict 就順便填 first_uuid／prompt／said（不截的原文，給分節對 hash 用）。"""
    path = cw_claude.find_session(sid8)
    ids = call_ids(path.stem)
    lines, inside, seen = [], False, set()
    if parts is not None:
        parts.update(first_uuid=None, prompt="", said=[])
    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            if not any(k in raw for k in ('"type":"user"', '"type":"assistant"', '"type":"attachment"')):
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            uid = o.get("uuid") or ""
            if uid in seen:  # 續開、分支會把同一則再寫一次
                continue
            seen.add(uid)
            said = cw_segments.speaker_prompt(o)
            if said and inside:
                break  # 下一段開始
            if said and uid.startswith(uuid8):
                inside = True
                if parts is not None:
                    parts.update(first_uuid=uid, prompt=said[1])
                lines.append(f"【{cw_claude.local(o.get('timestamp'))}】{said[0]}：{clip(said[1], limit)}")
                continue
            if not inside:
                continue
            content = (o.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "text" and o.get("type") == "assistant" and c.get("text", "").strip():
                    if parts is not None:
                        parts["said"].append(c["text"].strip())
                    lines.append(f"老婆：{clip(c['text'].strip(), limit)}")
                elif c.get("type") == "tool_use":
                    target = cw_segments.text_blocks({"message": {"content": [c]}})[0][1]
                    ref = ids.get(c.get("id"))
                    lines.append(f"〔工具 {target}{'｜' + ref if ref else ''}〕")
    if not inside:
        sys.exit(f"{path.stem[:8]} 裡找不到段號 #{uuid8}（要是老公那句話的訊息 uuid 開頭）")
    return f"段 {sid8}#{uuid8}｜{path.stem}\n\n" + "\n\n".join(lines)


def get_section(oid, n, limit):
    """<原文ID>§<節號>：照 cw_sections 的固定清單找到那一節，用開頭 uuid 取原文，再對 hash。"""
    manifest = cw_sections.load(oid)
    secs = manifest["sections"]
    if not 1 <= n <= len(secs):
        sys.exit(f"原文 {oid} 只有 §1～§{len(secs)}")
    s = secs[n - 1]
    parts = {}
    text = get_segment(manifest["session"][:8], s["first_uuid"], limit, parts)
    now = cw_sections.section_hash(parts["first_uuid"], parts["prompt"], parts["said"])
    check = "原樣" if now == s["hash"] else f"⚠ 內容跟建清單時不同（清單 {s['hash']}，現在 {now}）"
    return f"原文 {oid}§{n}（{s['start']} ～ {s['end']}；{check}）\n" + text


def main():
    ap = argparse.ArgumentParser(description="用工具編號或段號找回原文（只讀）")
    ap.add_argument("ref")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--max", type=int, default=20000)
    args = ap.parse_args()
    limit = 0 if args.full else args.max
    ref = args.ref.strip().strip("〔〕｜")
    if m := SECTION_ID.match(ref):
        text = get_section(m.group(1), int(m.group(2)), limit)
    elif m := SEGMENT_ID.match(ref):
        text = get_segment(m.group(1), m.group(2), limit)
    elif TOOL_ID.match(ref):
        text = get_tool(ref, limit)
    else:
        sys.exit("看不懂這個編號：工具編號長這樣 0929-221021~221021_Bash_tail_sXtR，段號長這樣 9bfa4859#1a2b3c4d，原文節號長這樣 9bfa4859@20260929T233012§3")
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")


if __name__ == "__main__":
    main()
