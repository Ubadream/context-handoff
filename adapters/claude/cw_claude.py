"""context-workbench 的 Claude Code 接頭（第一刀：只看不改）。

讀 Claude Code 的對話紀錄和本機既有工具留下的紀錄，吐 JSON 給核心和面板用：
  python cw_claude.py capabilities           這個宿主能做、不能做的動作
  python cw_claude.py sessions [--days 7]    最近的對話
  python cw_claude.py show <session>         一個對話的現場
  python cw_claude.py compactions <session>  一個對話每次壓縮的前後（摘要、模式、補回的原話）
  python cw_claude.py summary <session> [n]  第 n 次（預設最後一次）壓縮的摘要全文
<session> 可以只給前幾碼。輸出格式見 docs/adapter-contract.md。

資料來源（路徑可用環境變數改）：
  CLAUDE_HOME          ~/.claude：projects/*/<session>.jsonl、hooks/handoff_bind.py
  CW_COMPACT_GAP_DIR   compact_gap 的 log 與 pending（補原話）
  CW_WIFE_COMPACT_DIR  wife-compact 外掛的 trace.log 與 originals/（A／C 模式）
這些工具沒裝的機器上，對應欄位回 null，不報錯。
"""
import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
from cw_paths import STATE_ROOT as STATE  # noqa: E402
GAP_DIR = Path(os.environ.get("CW_COMPACT_GAP_DIR", STATE / "compact-gap"))
WIFE_COMPACT_DIR = Path(os.environ.get("CW_WIFE_COMPACT_DIR", STATE / "wife-compact"))
TAIL_BYTES = 4 * 1024 * 1024
MATCH_SECONDS = 120  # wife-compact 的 trace 和原話檔沒有 session id，只能用時間對到壓縮

CAPABILITIES = [
    # id、做不做得到、怎麼做到、限制。面板照這張決定按鈕能不能按、不能按時顯示理由。
    {"id": "view.sessions", "supported": True, "via": "~/.claude/projects/*/<session>.jsonl"},
    {"id": "view.current_summary", "supported": True, "via": "壓縮分界後的 isCompactSummary 訊息"},
    {"id": "view.compaction_history", "supported": True, "via": "compact_boundary 紀錄＋compact_gap log＋wife-compact trace"},
    {"id": "view.handoffs", "supported": True, "via": "handoff_bind.py：這個對話自己 Write／Edit 過的 HANDOFF*.md"},
    {"id": "compact.schedule", "supported": True, "via": "wife-compact 外掛的 compact 工具（A／C；B 0930 拿掉）",
     "limits": "只能在對話裡由老婆呼叫；從外面排定還沒接"},
    {"id": "compact.instructions_next", "supported": True, "via": "/compact 附指示；wife-compact 的 keep 欄位"},
    {"id": "compact.replace_at_compaction", "supported": True, "via": "函式 hook session.compact 回傳自訂訊息列（B 模式就是這樣）",
     "limits": "函式 hook 是 early access，要 CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1；依項目換模板還沒做"},
    {"id": "summary.replace_after_compaction", "supported": False,
     "reason": "壓完後已生效的摘要，Claude Code 沒有入口可以換；只能補注入"},
    {"id": "inject.after_compaction", "supported": True, "via": "compact_gap.py：壓後第一句話由 UserPromptSubmit 補 additionalContext",
     "limits": "Claude 的 SessionStart 比 PostCompact 早到，所以要等老公下一句話才補"},
    {"id": "continue.after_compaction", "supported": False,
     "reason": "還沒做；函式 hook 的 prompt.submit 可能做得到，未試"},
    {"id": "notice.user", "supported": True, "via": "hook 輸出 systemMessage",
     "limits": "桌面版顯示成摺起來的「Claude Code notice」"},
]


def local(ts):
    """Claude 紀錄是 UTC；面板顯示本機時間。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ts


def parse_utc(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def transcripts():
    return (HOME / "projects").glob("*/*.jsonl")


def find_session(prefix):
    hits = [p for p in transcripts() if p.stem.startswith(prefix)]
    if not hits:
        sys.exit(f"找不到對話 {prefix}")
    if len({p.stem for p in hits}) > 1:
        sys.exit(f"{prefix} 對到多個對話：{', '.join(sorted({p.stem for p in hits}))}")
    return max(hits, key=lambda p: p.stat().st_mtime)


def message_text(o):
    content = (o.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def context_tokens(path):
    """最後一次回應時的上下文大小；壓縮後還沒有新回應時回 None。"""
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    for line in reversed(lines):
        if '"subtype":"compact_boundary"' in line:
            return None
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            usage = json.loads(line)["message"]["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None


def scan(path):
    """一次串流讀完：壓縮分界、摘要、cwd、第一句話。大檔只 parse 需要的行。"""
    info = {"cwd": None, "first_prompt": None, "compactions": []}
    waiting = None
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if info["cwd"] is None and '"cwd"' in line:
                try:
                    info["cwd"] = json.loads(line).get("cwd")
                except ValueError:
                    pass
            if '"subtype":"compact_boundary"' in line:
                o = json.loads(line)
                if any(c["at"] == o.get("timestamp") for c in info["compactions"]):
                    continue  # 同一次壓縮在紀錄裡出現兩次（續開的對話會帶著舊分界）
                meta = o.get("compactMetadata") or {}
                waiting = {"at": o.get("timestamp"), "trigger": meta.get("trigger"),
                           "pre_tokens": meta.get("preTokens"), "duration_ms": meta.get("durationMs"),
                           "summary": None}
                info["compactions"].append(waiting)
                continue
            if waiting is not None and '"isCompactSummary":true' in line:
                waiting["summary"] = message_text(json.loads(line))
                waiting = None
                continue
            if info["first_prompt"] is None and '"type":"user"' in line and '"isMeta":true' not in line:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                text = message_text(o).strip()
                if text and not text.startswith("<") and not o.get("isCompactSummary"):
                    info["first_prompt"] = text[:200]
    return info


def gap_entries(sid):
    log = GAP_DIR / "log.jsonl"
    if not log.exists():
        return None
    out = []
    with open(log, encoding="utf-8") as f:
        for line in f:
            if sid not in line:
                continue
            o = json.loads(line)
            if o.get("session") == sid:
                out.append(o)
    return out


def wife_compact_runs():
    """trace.log 裡每次 session.compact 的結果：[(UTC 時間, 模式或 builtin)]。"""
    trace = WIFE_COMPACT_DIR / "trace.log"
    if not trace.exists():
        return None
    runs = []
    for line in trace.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"(\S+) v\S+ session\.compact (?:([ABC]) 完成|trigger=\S+ pending=-)", line)
        if m:
            runs.append((parse_utc(m.group(1)), m.group(2) or "builtin"))
    return runs


def originals_files():
    folder = WIFE_COMPACT_DIR / "originals"
    if not folder.exists():
        return None
    out = []
    for p in folder.glob("*.md"):
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)_([ABC])", p.name)
        if m:
            when = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)
            out.append((when, str(p)))
    return out


def nearest(items, when):
    """(值, 是否有歧義)：時間窗裡有兩筆以上時取最近的，但標出來（兩個對話幾秒內一起壓縮就會這樣）。"""
    if items is None or when is None:
        return None, False
    near = sorted((abs((it[0] - when).total_seconds()), it[1]) for it in items)
    near = [v for d, v in near if d <= MATCH_SECONDS]
    return (near[0] if near else None), len(near) > 1


def gap_local_time(entry):
    """compact_gap 的 at 是本機時間、沒有時區。"""
    try:
        return datetime.fromisoformat(entry["at"]).astimezone().astimezone(timezone.utc)
    except (KeyError, ValueError):
        return None


PICKED_LINE = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) (老公貼來的|老公|老婆|別的視窗(?:（[^）]*）)?)：(.*?)(?:（Jev ([\d.]+)[^（）]*）)?$")
PROBE_CHARS = re.compile(r"[\w\u3400-\u9fff]{4,}")  # 找原文時用的一段字：只取字母和漢字，避開 JSON 跳脫


def parse_picked(text):
    """compact_gap 補回的每一句：時間、誰說的、原句、Jev 分數、有沒有截斷。"""
    items = []
    for line in (text or "").splitlines():
        m = PICKED_LINE.match(line.strip())
        if not m:
            continue
        body = m.group(3)
        items.append({"said_at": m.group(1), "speaker": m.group(2), "text": body,
                      "jev": float(m.group(4)) if m.group(4) else None,
                      "truncated": body.endswith("（截斷）")})
    return items


def resolve_sources(path, history):
    """把每句補回的原話對回對話紀錄裡的那一則訊息。
    出處用訊息的 uuid（Claude 紀錄裡不會變的 ID）；line 只給本機定位用，不當對外節號。"""
    wanted = []
    for c in history:
        gap = c["compact_gap"]
        if not gap or not gap.get("picked_text"):
            continue
        gap["picked_items"] = parse_picked(gap["picked_text"])
        for item in gap["picked_items"]:
            # 取最長的三段字：原句的 markdown、引號、空白會把字切斷，整句直接找找不到
            pieces = sorted(PROBE_CHARS.findall(item["text"].removesuffix("…（截斷）")), key=len, reverse=True)[:3]
            item["source"] = None
            if pieces:
                wanted.append(([p[:16] for p in pieces], item, "assistant" if item["speaker"] == "老婆" else "user"))
    if not wanted:
        return
    probes = {p for ps, _, _ in wanted for p in ps}
    candidates = {}  # probe → [(timestamp, uuid, line, role)]
    with open(path, encoding="utf-8", errors="ignore") as f:
        for n, line in enumerate(f, 1):
            hit = [p for p in probes if p in line]
            if not hit:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isCompactSummary"):
                continue
            prompt = (o.get("attachment") or {}).get("prompt")
            if o.get("type") in ("user", "assistant"):
                text, role, kind = message_text(o), o["type"], "message"
            elif isinstance(prompt, str):
                # 別的視窗傳來的訊息（cross-session-message）排進佇列後，以 attachment 記在這裡
                text, role, kind = prompt, "user", "cross_session" if "<cross-session-message" in prompt else "queued"
            else:
                continue
            for p in hit:
                if p in text:
                    candidates.setdefault(p, []).append((o.get("timestamp"), o.get("uuid"), n, role, kind))
    for pieces, item, role in wanted:
        said = datetime.strptime(item["said_at"], "%Y-%m-%d %H:%M").astimezone()
        # 三段字都要落在同一則訊息裡（找得到的那幾段），短的字才不會對錯地方
        found = [{r[1] for r in candidates.get(p, []) if r[3] == role} for p in pieces]
        found = [s for s in found if s]
        both = set.intersection(*found) if found else set()
        rows = [r for r in {r for p in pieces for r in candidates.get(p, [])} if r[1] in both and parse_utc(r[0])]
        if not rows:
            continue
        ts, uuid, n, _, kind = min(rows, key=lambda r: abs((parse_utc(r[0]) - said).total_seconds()))
        # kind＝cross_session 表示這句其實是別的視窗傳來的，compact_gap 標的「老公」不對
        item["source"] = {"session": path.stem, "message_uuid": uuid, "message_at": local(ts), "line": n,
                          "kind": kind, "matches": len(rows)}


def compactions(path, info=None):
    info = info or scan(path)
    sid = path.stem
    gaps = gap_entries(sid)
    runs = wife_compact_runs()
    originals = originals_files()
    items = info["compactions"]
    out = []
    for i, c in enumerate(items):
        start = parse_utc(c["at"])
        end = parse_utc(items[i + 1]["at"]) if i + 1 < len(items) else None
        gap = None
        if gaps is not None:
            # compact_gap 的時間只到秒，會比分界早零點幾秒；兩端都放 5 秒
            slack = timedelta(seconds=5)
            inside = [g for g in gaps if (t := gap_local_time(g)) and start and t >= start - slack
                      and (end is None or t < end - slack)]
            done = [g for g in inside if g.get("picked")] or [g for g in inside if g.get("source") in ("prompt", "compact", "sessionstart")]
            last = done[-1] if done else None
            gap = {"events": [{"at": g.get("at"), "source": g.get("source"), "picked": g.get("picked"),
                               "reason": g.get("reason")} for g in inside],
                   "picked": last.get("picked") if last else None,
                   "picked_text": last.get("picked_text") if last else None,
                   "summary_source": last.get("summary_source") if last else None}
        summary = c["summary"]
        mode, mode_ambiguous = nearest(runs, start)
        original, original_ambiguous = nearest(originals, start)
        out.append({
            "index": i + 1,
            "at": local(c["at"]),
            "trigger": c["trigger"],
            "pre_tokens": c["pre_tokens"],
            "duration_ms": c["duration_ms"],
            "mode": mode,  # A／B／C／builtin；None＝沒裝 wife-compact 或對不到
            "mode_matched_by": "time" if runs is not None else None,
            "mode_ambiguous": mode_ambiguous,
            "summary_chars": len(summary) if summary is not None else None,
            "summary_head": summary[:300] if summary else None,
            "originals_file": original,
            "originals_ambiguous": original_ambiguous,
            "compact_gap": gap,
        })
    resolve_sources(path, out)
    return out


def handoffs(path):
    script = Path(__file__).resolve().parent / "hooks" / "handoff_bind.py"  # 倉庫這份；~/.claude/hooks 只剩轉接
    if not script.exists():
        return None
    spec = importlib.util.spec_from_file_location("handoff_bind", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = mod.written_handoffs(str(path))
    return [{"path": p, "modified": datetime.fromtimestamp(os.path.getmtime(p)).astimezone().isoformat(timespec="seconds")}
            for p, _ in sorted(found, key=lambda v: os.path.getmtime(v[0]), reverse=True)]


def session_row(path, info=None):
    info = info or scan(path)
    st = path.stat()
    return {"session": path.stem, "host": "claude", "project_dir": path.parent.name, "cwd": info["cwd"],
            "transcript": str(path), "bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
            "first_prompt": info["first_prompt"], "compaction_count": len(info["compactions"]),
            "context_tokens": context_tokens(path)}


def cmd_sessions(args):
    cutoff = datetime.now().timestamp() - args.days * 86400
    paths = sorted((p for p in transcripts() if p.stat().st_mtime >= cutoff), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"sessions": [session_row(p) for p in paths[: args.limit]]}


def cmd_show(args):
    path = find_session(args.session)
    info = scan(path)
    history = compactions(path, info)
    pending = GAP_DIR / "pending" / f"{path.stem}.json"
    return {**session_row(path, info),
            "last_compaction": history[-1] if history else None,
            "handoffs": handoffs(path),
            "compact_gap_pending": pending.exists() if GAP_DIR.exists() else None,
            "scheduled_compaction": None,  # wife-compact 的排定只在對話的記憶體裡，外面看不到
            "scheduled_compaction_note": "外面讀不到；排定後老婆會在回覆裡說"}


def cmd_compactions(args):
    path = find_session(args.session)
    return {"session": path.stem, "compactions": compactions(path)}


def cmd_summary(args):
    path = find_session(args.session)
    items = scan(path)["compactions"]
    if not items:
        sys.exit("這個對話還沒壓縮過")
    n = args.n or len(items)
    if not 1 <= n <= len(items):
        sys.exit(f"只有 {len(items)} 次壓縮")
    c = items[n - 1]
    return {"session": path.stem, "index": n, "at": local(c["at"]), "summary": c["summary"]}


def main():
    ap = argparse.ArgumentParser(description="context-workbench：Claude Code 接頭（只讀）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities")
    s = sub.add_parser("sessions")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--limit", type=int, default=50)
    for name in ("show", "compactions"):
        sub.add_parser(name).add_argument("session")
    s = sub.add_parser("summary")
    s.add_argument("session")
    s.add_argument("n", nargs="?", type=int)
    args = ap.parse_args()
    if args.cmd == "capabilities":
        out = {"host": "claude", "capabilities": CAPABILITIES}
    else:
        out = {"sessions": cmd_sessions, "show": cmd_show, "compactions": cmd_compactions, "summary": cmd_summary}[args.cmd](args)
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
