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
from cw_paths import HANDOFF_REMINDER, handoff_sections  # noqa: E402

from cw_lang import t

OUT = cw_claude.STATE / "context-workbench" / "handoff-checks"
HANDOFF_TAIL = 8000  # 給 Jev 看的交接尾巴
PROMPT_VERSION = "handoff-check-v2"  # v2：同一次呼叫多給「現在要不要壓、用哪種」的建議
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# 開了 handoff_sections（cw_thresholds.py set --handoff-sections on）才加這段；預設照原本五類
SECTIONS_RULES = (t("另外：交接用三塊寫（決策／情感／工作進度）。多列一類 emotion（老婆有感觸處、老公反應與精彩討論），"
                  "建議寫法依三塊、每條附時間＋原話短句或原文 ID；錯誤只留錯誤簿指向。\n\n", 'Also: organize the handoff into Decisions, Feelings, and Work progress. Add an emotion category (agent reactions, user responses, and notable discussions). Suggestions should follow these sections, each with a timestamp and short quote or original-text ID; reference the error log for errors.\n\n'))

PROMPT = t("""老婆（助理）準備壓縮上下文，壓縮後她只剩摘要和交接檔。下面是上一次壓縮之後的對話分段，和交接檔的最後一段。
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
""", 'You are preparing to compact context; afterward only the summary and handoff remain. Below are conversation segments since the last compaction and the handoff tail.\nFind **items missing from the handoff that are needed to continue afterward**, in five categories:\n- decision: user decisions, constraints, preferences\n- request: user requests without a clearly reported result\n- done: completed work missing from the handoff (files, versions, verification)\n- open: unfinished, unverified, or blocked work\n- next: where to resume\n\nRules:\n- List only actual omissions; omit anything already covered, even in different words.\n- Give each item a concrete suggested handoff sentence (suggest) in English, with a filename or segment ID.\n- Omit casual chat and feelings unless the user explicitly requested future agent behavior.\n- Include uncertain items and write "uncertain" in why.\n- Return an empty array when nothing is missing.\n\nAlso advise whether to compact now and which mode (advice):\n- now: yes (a work boundary: work finished and the handoff is updated or can be completed) / wait (work or a tool chain is ongoing, or the user message is not fully addressed) / no (context is still small)\n- mode: A (native summary; default at a clean boundary with a fresh handoff) / C (summary plus archived original wording; detailed discussion may need later retrieval)\n- Current context usage: {tokens}\n\nOutput only one JSON object:\n{{"gaps": [{{"kind": "decision|request|done|open|next", "seg": segment_number, "what": "missing item", "suggest": "suggested handoff sentence", "why": "why needed"}}],\n "advice": {{"now": "yes|wait|no", "mode": "A|C", "why": "one or two sentences citing segment IDs"}}}}\n\n[Handoff tail] ({handoff_path})\n{handoff_tail}\n\n[Conversation segments since the last compaction]\n{segments}\n')


def load_handoff_bind():
    spec = importlib.util.spec_from_file_location("handoff_bind", Path(__file__).resolve().parent / "hooks" / "handoff_bind.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def format_warnings(text):
    """三塊與至少一處原話定位的軟檢查；不改新鮮度或壓縮許可。"""
    headings = set(re.findall(
        r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(決策|情感|工作進度|Decisions|Feelings|Work progress)(?:\*\*)?"
        r"(?:[ \t]*[:：].*|[ \t]*(?:[（(].*[）)])?[ \t]*)$", text, re.M))
    warnings = []
    required = (("決策", "Decisions"), ("情感", "Feelings"), ("工作進度", "Work progress"))
    missing = [t(zh, en) for zh, en in required if zh not in headings and en not in headings]
    if missing:
        warnings.append(t("交接缺少標題：", 'Missing handoff headings: ') + "／".join(missing))
    # UUID、段號／節號、明示原文 ID，或同一行的時間與引句。
    locator = re.compile(
        r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b"
        r"|\b[0-9a-f]{8}#[0-9a-f]{8}\b|\b[\w@.-]+§\d+"
        r"|(?:原文|original(?:-text)?)\s*ID\s*[:：=]\s*[`\"']?[A-Za-z0-9][\w.-]+"
        r"|(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?|\d{1,2}:\d{2})[^\n]*[「“\"][^」”\"\n]+[」”\"]"
        r"|〔據|\[source\b|\bwm1:", re.I)
    if not locator.search(text):
        warnings.append(t("交接沒有原話定位：補時間＋原話短句或原文 ID", 'Handoff has no original-text reference: add a timestamp and short quote or original-text ID'))
    # 決策塊每條帶〔據 …〕，改票再加〔取代 …〕；交接檔常是一段段往下接，只看最後一個決策塊
    blocks = list(re.finditer(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:決策|Decisions)(?:\*\*)?[^\n]*\n(.*?)"
                              r"(?=^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:情感|工作進度|Feelings|Work progress)(?:\*\*)?|\Z)",
                              text, re.M | re.S))
    block = blocks[-1] if blocks else None
    if block:
        items = [ln for ln in block.group(1).splitlines() if re.match(r"(?:[-*•]|\d+[.)、])\s+\S", ln)]  # 只算頂層條目
        bare = [ln for ln in items if not locator.search(ln)]
        if bare:
            warnings.append(t(f"決策 {len(bare)}/{len(items)} 條沒附〔據 時間「原話短句」〕；改票的再加〔取代 …〕",
                              f"{len(bare)}/{len(items)} decisions lack a source such as [source 10-25 20:30 \"short quote\"]; "
                              "when a decision changes, add [replaces ...]"))
    return warnings


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
    warnings = []
    if latest and handoff_sections():
        try:
            warnings = format_warnings(Path(latest[0]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            warnings = [t("交接格式未檢查：交接檔無法讀取", 'Handoff format not checked: cannot read the handoff file')]
    return {
        "session_file": str(Path(path).resolve()),
        "format_warnings": warnings,
        "handoffs": [{"path": p, "written_at": cw_claude.local(t)} for p, t in written],
        "last_compaction": cw_claude.local(last_boundary),
        "written_since_compaction": bool(latest and (not last_boundary or last_write > last_boundary)),
        "said_after_handoff": sum(1 for t, k in events if k == "said" and t > last_write) if latest else None,
        "edits_after_handoff": sum(1 for t, k in events if k == "edit" and t > last_write) if latest else None,
    }


def verdict(f):
    """給老婆看的一句話；程式只說事實，要不要補由她定。"""
    if not f["handoffs"]:
        return t("這個對話還沒寫過交接。", 'This session has no handoff yet.')
    if not f["written_since_compaction"]:
        return t(f"上一次壓縮（{f['last_compaction']}）之後還沒更新交接。", f'''Handoff has not been updated since the last compaction ({f['last_compaction']}).''')
    if f["said_after_handoff"] or f["edits_after_handoff"]:
        return t(f"交接最後寫在 {f['handoffs'][0]['written_at']}，之後老公又說了 {f['said_after_handoff']} 句、改了 {f['edits_after_handoff']} 次檔。", f'''Handoff last written at {f['handoffs'][0]['written_at']}; since then, {f['said_after_handoff']} user messages and {f['edits_after_handoff']} file edits.''')
    return t(f"交接最後寫在 {f['handoffs'][0]['written_at']}，之後沒有新的老公發話或改檔。", f'''Handoff last written at {f['handoffs'][0]['written_at']}; no user messages or file edits since then.''')


def jev_gaps(path, f, tokens=None):
    segs = cw_segments.segment(path)
    tail = t("（這個對話沒寫過交接）", '(This session has no handoff)')
    handoff_path = "-"
    if f["handoffs"]:
        handoff_path = f["handoffs"][0]["path"]
        text = Path(handoff_path).read_text(encoding="utf-8", errors="ignore")
        tail = text[-HANDOFF_TAIL:]
    template = PROMPT.replace(t("【交接檔最後一段】", '[Handoff tail]'), SECTIONS_RULES + t("【交接檔最後一段】", '[Handoff tail]')) if handoff_sections() else PROMPT
    prompt = template.format(handoff_path=handoff_path, handoff_tail=tail, tokens=f"{tokens:,} token" if tokens else t("不知道", 'unknown'),
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
        sys.exit(t(f"Jev 回的不是約定格式：{text[:300]}", f'''Jev returned an unexpected format: {text[:300]}'''))
    ids = {s["seq"]: s["id"] for s in segs}
    gaps = [g for g in out["gaps"] if isinstance(g, dict)]
    for g in gaps:
        g["segment_id"] = ids.get(g.get("seg"))
    advice = out.get("advice") if isinstance(out.get("advice"), dict) else None
    return gaps, advice, secs


def markdown(sid8, f, gaps, advice=None):
    lines = [t(f"# 交接檢查：{sid8}", f'''# Handoff check: {sid8}'''), "", f"**{verdict(f)}**", ""]
    if handoff_sections():
        lines += [HANDOFF_REMINDER, ""]
    lines += [t(f"- 軟性提醒（不擋壓縮）：{warning}", f'''- Advisory (does not block compaction): {warning}''') for warning in f.get("format_warnings", [])]
    if f["handoffs"]:
        lines += [t(f"- 交接檔：`{h['path']}`（{h['written_at']}）", f'''- Handoff: `{h['path']}` ({h['written_at']})''') for h in f["handoffs"]]
    if advice:
        now = {"yes": t("現在是段落點，可以壓", 'At a work boundary; compaction is suitable'), "wait": t("先別壓，還在做", 'Wait; work is ongoing'), "no": t("不必壓", 'No compaction needed')}.get(advice.get("now"), advice.get("now"))
        lines += ["", t(f"**Jev 對時機與模式的建議**：{now}；模式 {advice.get('mode')}。{advice.get('why') or ''}（只是建議，老婆定）", f'''**Jev timing and mode advice**: {now}; mode {advice.get('mode')}. {advice.get('why') or ''} (advisory; you decide)''')]
    if gaps is None:
        return "\n".join(lines) + "\n"
    names = {"decision": t("決定／限制", 'Decisions/constraints'), "emotion": t("情感", 'Feelings'), "request": t("老公的要求", 'User requests'), "done": t("做完沒記", 'Completed but unrecorded'), "open": t("未完／未驗", 'Unfinished/unverified'), "next": t("下一手", 'Next step')}
    lines += ["", t(f"## Jev 覺得交接可能漏的（{len(gaps)} 條；只是建議，要不要寫由老婆定）", f'''## Possible handoff omissions from Jev ({len(gaps)} items; advisory, you decide what to include)'''), ""]
    if not gaps:
        lines.append(t("沒有。", 'None.'))
    for g in gaps:
        lines.append(t(f"- 〔{names.get(g.get('kind'), g.get('kind'))}｜段 {g.get('seg')} `{g.get('segment_id') or '-'}`〕{g.get('what')}", f'''- [{names.get(g.get('kind'), g.get('kind'))} | segment {g.get('seg')} `{g.get('segment_id') or '-'}`] {g.get('what')}'''))
        lines.append(t(f"  - 建議寫：{g.get('suggest')}", f'''  - Suggested text: {g.get('suggest')}'''))
        if g.get("why"):
            lines.append(t(f"  - 為什麼：{g.get('why')}", f'''  - Why: {g.get('why')}'''))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=t("壓縮前交接檢查（只讀）", 'Pre-compaction handoff check (read-only)'))
    ap.add_argument("session")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tokens", type=int, help=t("目前上下文用量，給 Jev 判斷時機", 'Current context usage for Jev timing advice'))
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
