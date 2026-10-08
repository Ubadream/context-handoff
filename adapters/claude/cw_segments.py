"""把一個 Claude 對話「上一次壓縮之後」的上下文切段、分類，列出建議篩選清單（只讀，不動上下文）。

  python adapters/claude/cw_segments.py <session 前幾碼> [--no-model] [--out 資料夾]

切法：老公的一句話（含對話中途插進來的話、別的視窗傳來的訊息）開一段，接到下一句之前的老婆回覆和工具往返都算這段。
每段的 ID＝對話 ID 前 8 碼＋「#」＋這段第一則訊息 uuid 前 8 碼（不會因為之後的訊息而變）；seq 只是顯示用的順序。
分類、標題、留／拿掉與原因由模型給（cw_model：CW_MODEL_CMD 或本機 Jev），--no-model 或沒有模型時只切段不分類。
結果存 JSON（給面板）和 Markdown（給人看）到 ~/.local/state/wifeos/context-workbench/segments/。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_claude  # noqa: E402
import cw_model  # noqa: E402

from cw_paths import WORKBENCH_STATE  # noqa: E402

OUT = WORKBENCH_STATE / "segments"
PROMPT_VERSION = "segments-v1"
CATEGORIES = ["決定", "限制", "進行中", "已完成", "已過時或被取代", "查證過程", "工具輸出", "閒聊與感情", "其他"]
SEGMENT_CHARS = 2500  # 給模型看的每段摘錄上限；原文不截
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

PROMPT = """下面是一段對話被切成的段落（老公是使用者，老婆是助理）。這些段落在下一次壓縮時可能被保留或移出上下文；移出的段落原文會存檔，需要時可以用段號找回。

請為每一段給：
- title：十五字內的標題（繁體中文）
- category：從這些選一個：{categories}
- action：keep（之後的工作還會用到，要留在上下文）或 drop（可以移出，要時再找回）
- reason：一句理由。drop 的理由要具體，例如「已做完，第 N 段有結果」「被第 N 段的決定推翻」「工具輸出，結論已在第 N 段」。

判斷原則：
- 老公說過的決定、限制、偏好，除非後面明確被推翻，一律 keep。
- 還沒做完的事、下一步，keep。
- 已經做完而且後面有段落寫了結果的過程、查證的來回、工具輸出，可以 drop。
- 感情和相處的段落不是廢話；只有純粹的確認（「好」「收到」）才 drop。
- 拿不準就 keep。

只輸出 JSON 陣列，每段一個物件，照段號順序：
[{{"seq": 1, "title": "...", "category": "...", "action": "keep", "reason": "..."}}, ...]

段落：
{segments}
"""


def text_blocks(o):
    content = (o.get("message") or {}).get("content")
    if isinstance(content, str):
        return [("text", content)]
    out = []
    for c in content if isinstance(content, list) else []:
        if c.get("type") == "text":
            out.append(("text", c.get("text", "")))
        elif c.get("type") == "tool_use":
            arg = next((str(c["input"][k]) for k in ("file_path", "command", "pattern", "url", "description")
                        if isinstance(c.get("input"), dict) and k in c["input"]), "")
            out.append(("tool", f"{c.get('name')} {arg[:100]}".strip()))
        elif c.get("type") == "tool_result":
            body = c.get("content")
            size = len(body) if isinstance(body, str) else sum(len(x.get("text", "")) for x in body or [] if isinstance(x, dict))
            out.append(("result", size))
    return out


def speaker_prompt(o):
    """老公開新段的那一句：一般輸入、對話中途插進來的話、別的視窗的訊息。回 (誰, 文字) 或 None。"""
    if o.get("isMeta") or o.get("isCompactSummary") or o.get("isSidechain"):
        return None
    if o.get("type") == "attachment":
        prompt = (o.get("attachment") or {}).get("prompt")
        if isinstance(prompt, str) and prompt.strip() and not prompt.lstrip().startswith("<task-notification"):  # 背景任務通知不是有人說話
            return ("別的視窗" if "<cross-session-message" in prompt else "老公", prompt)
        return None
    if o.get("type") != "user":
        return None
    text = "\n".join(t for kind, t in text_blocks(o) if kind == "text")
    text = REMINDER.sub("", text).strip()
    if not text or text.startswith("<local-command") or text.startswith("<command-") or text.startswith("<task-notification"):
        return None
    return ("老公", text)


def live_rows(path):
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"compact_boundary"' in line:
                rows = []  # 只要上一次壓縮之後的
                continue
            if not any(k in line for k in ('"type":"user"', '"type":"assistant"', '"type":"attachment"')):
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def segment(path):
    sid8 = path.stem[:8]
    segs, cur = [], None
    for o in live_rows(path):
        said = speaker_prompt(o)
        if said:
            cur = {"seq": len(segs) + 1, "id": f"{sid8}#{(o.get('uuid') or '?')[:8]}", "first_uuid": o.get("uuid"),
                   "start": cw_claude.local(o.get("timestamp")), "end": None, "opened_by": said[0],
                   "prompt": said[1], "said": [], "tools": [], "result_chars": 0, "text_chars": len(said[1]),
                   "message_uuids": [o.get("uuid")]}
            segs.append(cur)
            continue
        if cur is None:
            continue
        cur["end"] = cw_claude.local(o.get("timestamp"))
        cur["message_uuids"].append(o.get("uuid"))
        for kind, value in text_blocks(o):
            if kind == "text" and o.get("type") == "assistant":
                text = value.strip()
                if text:
                    cur["said"].append(text)
                    cur["text_chars"] += len(text)
            elif kind == "tool":
                cur["tools"].append(value)
            elif kind == "result":
                cur["result_chars"] += value
    return segs


def excerpt(s):
    head = f"【第 {s['seq']} 段｜{s['start']}｜{s['opened_by']}開頭｜工具 {len(s['tools'])} 次】\n{s['opened_by']}：{s['prompt'][:800]}"
    body = "\n".join(f"老婆：{t}" for t in s["said"])
    if s["tools"]:
        body += "\n〔工具：" + "；".join(s["tools"][:12]) + ("…" if len(s["tools"]) > 12 else "") + "〕"
    text = f"{head}\n{body}"
    return text if len(text) <= SEGMENT_CHARS else text[:SEGMENT_CHARS] + "…（這段後面略）"


def classify(segs):
    prompt = PROMPT.format(categories="、".join(CATEGORIES), segments="\n\n".join(excerpt(s) for s in segs))
    try:
        text, secs = cw_model.ask(prompt)
    except cw_model.ModelUnavailable as exc:
        print(f"（{exc}）", file=sys.stderr)
        return None
    m = re.search(r"\[.*\]", text, re.S)
    labels = json.loads(m.group(0)) if m else []
    by_seq = {x.get("seq"): x for x in labels if isinstance(x, dict)}
    judged_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for s in segs:
        x = by_seq.get(s["seq"])
        s["label"] = None if x is None else {
            "title": x.get("title"), "category": x.get("category") if x.get("category") in CATEGORIES else "其他",
            "action": x.get("action") if x.get("action") in ("keep", "drop") else "keep",
            "reason": x.get("reason"), "by": cw_model.name(), "prompt": PROMPT_VERSION, "judged_at": judged_at}
        # 老公的話不整段拿掉：drop 只移出老婆的回覆和工具往返，老公那句留一行
        # （segments-v1 實測：把「可以啊老婆 就該如此」這種短短的同意連同工具輸出一起判成 drop）
        if s["label"] and s["label"]["action"] == "drop" and s["opened_by"] == "老公":
            s["label"]["keeps"] = "老公原話"
    return secs


def markdown(path, segs):
    lines = [f"# 分段與建議篩選：{path.stem[:8]}（上一次壓縮之後）", "",
             "| 段 | 時間 | 標題 | 分類 | 建議 | 理由 | 字數（文字／工具結果） |", "|---|---|---|---|---|---|---|"]
    cell = lambda t: re.sub(r"\s+", " ", str(t or "")).replace("|", "／")
    for s in segs:
        lab = {k: cell(v) for k, v in (s.get("label") or {}).items()}
        s = {**s, "prompt": cell(s["prompt"])}
        lines.append(f"| {s['seq']} `{s['id']}` | {(s['start'] or '')[5:16]} | {lab.get('title') or s['prompt'][:20]} | "
                     f"{lab.get('category', '')} | {lab.get('action', '')}{'（留老公原話）' if lab.get('keeps') else ''} | {lab.get('reason', '')} | {s['text_chars']}／{s['result_chars']} |")
    drop = [s for s in segs if (s.get("label") or {}).get("action") == "drop"]
    total = sum(s["text_chars"] + s["result_chars"] for s in segs) or 1
    freed = sum(s["text_chars"] + s["result_chars"] for s in drop)
    lines += ["", f"建議移出 {len(drop)}／{len(segs)} 段，約佔這段上下文字數的 {freed * 100 // total}%（工具結果算在內）。"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="分段＋建議篩選（只讀）")
    ap.add_argument("session")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    path = cw_claude.find_session(args.session)
    segs = segment(path)
    secs = None if args.no_model else classify(segs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    base = out / f"{path.stem[:8]}_{stamp}"
    manifest = {"session": path.stem, "host": "claude", "scope": "since_last_compaction", "created_at": stamp,
                "model_secs": secs, "segments": [{k: v for k, v in s.items() if k != "said"} for s in segs]}
    base.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".md").write_text(markdown(path, segs), encoding="utf-8")
    print(base.with_suffix(".md"))


if __name__ == "__main__":
    main()
