"""壓縮前的交接檢查（只讀）：程式量交接新不新，Jev 列「交接可能漏的事」和建議句，老婆決定怎麼補。

  python adapters/claude/cw_handoff_check.py <session 前幾碼> [--no-model] [--json]

程式部分（--no-model 只跑這段，幾秒）：
  - 這個對話自己寫過的交接檔（Write／Edit／Bash 追加都算，跟 handoff_bind.py 同一套）與最後寫入時間
  - 上一次壓縮之後有沒有寫過；最後一次寫之後，老公又說了幾句、老婆又改了幾次檔
Jev 部分：把上一次壓縮後的分段摘錄和交接的尾巴給 Flash，列出交接沒寫到的決定、老公的要求、做完的事、
  沒做完的事和下一手，每條附一句建議寫法。只是建議；交接由老婆寫。
結果存 ~/.local/state/wifeos/context-workbench/handoff-checks/<sid8>_<時間>.json/.md，stdout 印 .md 路徑（--json 改印 JSON）。
"""
import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cw_claude  # noqa: E402
import cw_segments  # noqa: E402

OUT = cw_claude.STATE / "context-workbench" / "handoff-checks"
HANDOFF_TAIL = 8000  # 給 Jev 看的交接尾巴
PROMPT_VERSION = "handoff-check-v2"  # v2：同一次呼叫多給「現在要不要壓、用哪種」的建議
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

PROMPT = """老婆（助理）準備壓縮上下文，壓縮後她只剩摘要和交接檔。下面是上一次壓縮之後的對話分段，和交接檔的最後一段。
請找出**交接裡沒寫到、但壓縮後接著做事會需要的東西**，分五類：
- decision：老公說過的決定、限制、偏好
- request：老公要求、還沒明確交代結果的事
- done：已經做完、交接沒記的成果（檔案、版本、驗證結果）
- open：做到一半、還沒驗、卡住的事
- next：下一手從哪裡動

規則：
- 只列交接真的沒寫到的；交接已經寫了（用詞不同也算）就不要列。
- 每條給一句建議寫法（suggest），像交接裡的一行：具體、帶檔名或段號、繁體中文。
- 閒聊與感情不用列，除非老公明確要老婆以後怎麼做。
- 拿不準要不要列就列，並在 why 寫「不確定」。
- 沒有漏的就回空陣列。

另外，對「現在要不要壓縮、用哪種」給建議（advice）：
- now：yes（剛好是段落點：一段工作做完、交接已更新或補完就能壓）／wait（最後一段還在做、工具做到一半、老公的話還沒接完）／no（上下文還不多，不必壓）
- mode：A（內建摘要；預設，交接寫好、段落乾淨時）／C（摘要＋原話全文存檔；細節討論正熱、之後可能要回頭查原話）
- 目前上下文用量：{tokens}

只輸出一個 JSON 物件，不要其他文字：
{{"gaps": [{{"kind": "decision|request|done|open|next", "seg": 段號數字, "what": "漏了什麼", "suggest": "建議寫進交接的一句", "why": "為什麼需要"}}],
 "advice": {{"now": "yes|wait|no", "mode": "A|C", "why": "一兩句理由，引用段號"}}}}

【交接檔最後一段】（{handoff_path}）
{handoff_tail}

【上一次壓縮之後的對話分段】
{segments}
"""


def load_handoff_bind():
    spec = importlib.util.spec_from_file_location("handoff_bind", Path(__file__).resolve().parent / "hooks" / "handoff_bind.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def facts(path):
    """程式量得到的：交接寫了沒、多新、之後又發生多少事。"""
    written = sorted(load_handoff_bind().written_handoffs(str(path)), key=lambda v: v[1], reverse=True)
    last_boundary = None
    events = []  # (時間, 種類)
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"compact_boundary"' in line:
                try:
                    last_boundary = json.loads(line).get("timestamp") or last_boundary
                except ValueError:
                    pass
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") not in ("user", "assistant", "attachment"):
                continue
            ts = o.get("timestamp") or ""
            if cw_segments.speaker_prompt(o):
                events.append((ts, "said"))
            for c in (o.get("message") or {}).get("content") or [] if isinstance((o.get("message") or {}).get("content"), list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in WRITE_TOOLS:
                    events.append((ts, "edit"))
    latest = written[0] if written else None
    last_write = latest[1] if latest else ""
    return {
        "handoffs": [{"path": p, "written_at": cw_claude.local(t)} for p, t in written],
        "last_compaction": cw_claude.local(last_boundary),
        "written_since_compaction": bool(latest and (not last_boundary or last_write > last_boundary)),
        "said_after_handoff": sum(1 for t, k in events if k == "said" and t > last_write) if latest else None,
        "edits_after_handoff": sum(1 for t, k in events if k == "edit" and t > last_write) if latest else None,
    }


def verdict(f):
    """給老婆看的一句話；程式只說事實，要不要補由她定。"""
    if not f["handoffs"]:
        return "這個對話還沒寫過交接。"
    if not f["written_since_compaction"]:
        return f"上一次壓縮（{f['last_compaction']}）之後還沒更新交接。"
    if f["said_after_handoff"] or f["edits_after_handoff"]:
        return f"交接最後寫在 {f['handoffs'][0]['written_at']}，之後老公又說了 {f['said_after_handoff']} 句、改了 {f['edits_after_handoff']} 次檔。"
    return f"交接最後寫在 {f['handoffs'][0]['written_at']}，之後沒有新的老公發話或改檔。"


def jev_gaps(path, f, tokens=None):
    segs = cw_segments.segment(path)
    tail = "（這個對話沒寫過交接）"
    handoff_path = "-"
    if f["handoffs"]:
        handoff_path = f["handoffs"][0]["path"]
        text = Path(handoff_path).read_text(encoding="utf-8", errors="ignore")
        tail = text[-HANDOFF_TAIL:]
    prompt = PROMPT.format(handoff_path=handoff_path, handoff_tail=tail, tokens=f"{tokens:,} token" if tokens else "不知道",
                           segments="\n\n".join(cw_segments.excerpt(s) for s in segs))
    try:
        text, secs = cw_segments.cw_model.ask(prompt)
    except cw_segments.cw_model.ModelUnavailable as exc:
        print(f"（{exc}）", file=sys.stderr)
        return None, None, None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else None
    except ValueError:
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("gaps"), list):
        sys.exit(f"Jev 回的不是約定格式：{text[:300]}")
    ids = {s["seq"]: s["id"] for s in segs}
    gaps = [g for g in out["gaps"] if isinstance(g, dict)]
    for g in gaps:
        g["segment_id"] = ids.get(g.get("seg"))
    advice = out.get("advice") if isinstance(out.get("advice"), dict) else None
    return gaps, advice, secs


def markdown(sid8, f, gaps, advice=None):
    lines = [f"# 交接檢查：{sid8}", "", f"**{verdict(f)}**", ""]
    if f["handoffs"]:
        lines += [f"- 交接檔：`{h['path']}`（{h['written_at']}）" for h in f["handoffs"]]
    if advice:
        now = {"yes": "現在是段落點，可以壓", "wait": "先別壓，還在做", "no": "不必壓"}.get(advice.get("now"), advice.get("now"))
        lines += ["", f"**Jev 對時機與模式的建議**：{now}；模式 {advice.get('mode')}。{advice.get('why') or ''}（只是建議，老婆定）"]
    if gaps is None:
        return "\n".join(lines) + "\n"
    names = {"decision": "決定／限制", "request": "老公的要求", "done": "做完沒記", "open": "未完／未驗", "next": "下一手"}
    lines += ["", f"## Jev 覺得交接可能漏的（{len(gaps)} 條；只是建議，要不要寫由老婆定）", ""]
    if not gaps:
        lines.append("沒有。")
    for g in gaps:
        lines.append(f"- 〔{names.get(g.get('kind'), g.get('kind'))}｜段 {g.get('seg')} `{g.get('segment_id') or '-'}`〕{g.get('what')}")
        lines.append(f"  - 建議寫：{g.get('suggest')}")
        if g.get("why"):
            lines.append(f"  - 為什麼：{g.get('why')}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="壓縮前交接檢查（只讀）")
    ap.add_argument("session")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tokens", type=int, help="目前上下文用量，給 Jev 判斷時機")
    args = ap.parse_args()
    path = cw_claude.find_session(args.session)
    f = facts(path)
    gaps, advice, secs = (None, None, None) if args.no_model else jev_gaps(path, f, args.tokens)
    out = {"session": path.stem, "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "verdict": verdict(f), "facts": f, "gaps": gaps, "advice": advice, "tokens": args.tokens,
           "model": None if secs is None else cw_segments.cw_model.name(),
           "prompt": None if secs is None else PROMPT_VERSION, "model_secs": secs}
    if args.json:
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f"{path.stem[:8]}_{datetime.now():%Y%m%dT%H%M%S}"
    base.with_suffix(".json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    base.with_suffix(".md").write_text(markdown(path.stem[:8], f, gaps, advice), encoding="utf-8")
    print(base.with_suffix(".md"))


if __name__ == "__main__":
    main()
