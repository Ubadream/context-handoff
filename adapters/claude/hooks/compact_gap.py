# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Recover uncovered original wording after compaction; preserve provenance and later context."""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adapters/claude：共用位置
from cw_paths import STATE_ROOT
from cw_gap_model import Ranker, settings  # noqa: E402

LOG = Path(os.environ.get("CW_STATE_ROOT") or STATE_ROOT) / "compact-gap" / "log.jsonl"
EMITTED = LOG.with_name("emitted.json")
PENDING = LOG.parent / "pending"
CONSUMED = LOG.parent / "consumed"
POOL = 150         # 覆蓋最低的幾句交給 ranker
MIN_SCORE = 0.8
MAX_PICK = 10
MAX_CHARS = 3000   # 補回上下文的總字數上限
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SKIP_PREFIX = ("This session is being continued", "〔壓縮前最近的原話", "〔摘要沒蓋到的原話")
QUESTION = ("這句是不是之後接手工作還得記住的具體事實、決定、限制、注意事項或user的要求？"
            "過場句、閒聊、情緒、只描述剛做完什麼的進度回報都不算。")
REVISITED = 0.7    # 後面原話有這麼像的句子，就標「後來又談過」（不跳過，見 run）
CONSTRAINT = re.compile(r"不要|不准|(?<!能)不能(?!不)|不可|不得|禁止|絕不|別再|別動|別改|只能|只限|只准|除非|例外|❌|不 ?commit|"
                        r"\b(?:never|don't|do not|must not)\b", re.I)


def load_recall():
    return Ranker()


def text_of(o):
    if o.get("type") == "attachment":
        att = o.get("attachment") or {}
        return str(att.get("prompt", "")).strip() if att.get("type") == "queued_command" else ""
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    else:
        text = "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
    return REMINDER.sub("", text).strip()


CODEX_ROOT = Path.home() / ".codex" / "sessions"
CODEX_SUMMARY_HEAD = "Here is the summary produced by the other language model"


def codex_msg(o):
    """Codex rollout 的一則原話（response_item／message）；系統塞進來的回 None。"""
    p = o.get("payload") or {}
    if o.get("type") != "response_item" or p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
        return None
    t = "\n".join(x.get("text", "") for x in p.get("content") or [] if isinstance(x, dict)).strip()
    # 系統塞進來的（AGENTS.md、環境、hook 注入）都是 < 開頭或帶 AGENTS.md
    if not t or t.startswith("<") or t.startswith("#") and "AGENTS.md" in t[:300] or t.startswith(SKIP_PREFIX):
        return None
    return {"role": "user" if p["role"] == "user" else "assistant", "text": t, "time": o.get("timestamp", ""), "id": p.get("id")}


CROSS_SESSION = re.compile(r'<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)', re.S)
FROM_NAME = re.compile(r'from-name="([^"]*)"')
PLUGIN_PROMPT = re.compile(r"The [^\n]+ plugin sent a message:\s*\n")
PROGRAM_PREFIX = ("〔wife-compact", "〔閒置提醒（wife-compact", "<task-notification>",
                  "<local-command", "<command-")


def claude_program(o, text):
    """user 是宿主的訊息槽，不保證是真人；queued_command 的出處在 attachment 裡。"""
    sources = (o, o.get("message") or {}, o.get("attachment") or {})
    for source in sources:
        origin = source.get("origin")
        if origin and (not isinstance(origin, dict) or origin.get("kind") != "human"):
            return True
        if source.get("promptSource") == "system" or source.get("turnOrigin") in ("system", "task_notification"):
            return True
    # 明確的真人出處優先於內容：user可能逐字貼外掛訊息來討論。
    if any(isinstance(s.get("origin"), dict) and s["origin"].get("kind") == "human" for s in sources):
        return False
    if o.get("type") == "attachment":
        return True  # 沒有真人出處的排隊附件不冒充user。
    # 舊版沒有 origin；只認宿主包裝／固定開頭，不搜尋正文裡提到的外掛名字。
    return bool(PLUGIN_PROMPT.match(text)) or text.startswith(PROGRAM_PREFIX)


def claude_msg(o):
    if o.get("isSidechain") or o.get("isMeta") or o.get("isCompactSummary"):
        return None
    if o.get("type") not in ("user", "assistant", "attachment"):
        return None
    t = text_of(o)
    if not t or t.startswith(SKIP_PREFIX) or t.startswith("<local-command") or t.startswith("<command-"):
        return None
    if o.get("type") != "assistant" and claude_program(o, t):
        return None
    cross = CROSS_SESSION.match(t)
    if cross:  # 別的視窗傳來的訊息不是user說的（0929 context-workbench 對出處時查到被標成user）
        return {"role": "program", "text": cross.group(2).strip(), "time": o.get("timestamp", ""), "id": o.get("uuid")}
    return {"role": "user" if o.get("type") in ("user", "attachment") else "assistant", "text": t, "time": o.get("timestamp", ""), "id": o.get("uuid")}


HANDOFF_REF = re.compile(r"交接：(\S+?\.md)")


def codex_visible_after(payload):
    """遠端壓縮（10-17 起）的摘要是加密的，message 空白。壓縮後模型看得到的是保留下來的原話＋交接指引指向的交接檔，
    就拿這些當「摘要」去比對：已經原文留在上下文、或寫進交接的，不用再補。"""
    parts = []
    for item in payload.get("replacement_history") or []:
        if item.get("type") != "message":
            continue
        text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)).strip()
        parts.append(text)
        ref = HANDOFF_REF.search(text) if text.startswith(("<handoff_history>", "<wife_compact_history>")) else None
        if ref:
            try:
                parts.append(Path(ref.group(1)).read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return "\n".join(p for p in parts if p)


def codex_rollout(session_id):
    hits = sorted(CODEX_ROOT.rglob(f"rollout-*{session_id}*.jsonl"), key=lambda p: p.stat().st_mtime)
    return str(hits[-1]) if hits else None


def segment(path, include_tail=False):
    """回傳（摘要, 上一次壓縮到這一次之間的原話, 這次壓縮的時間）。Claude jsonl 和 Codex rollout 都吃。

    邊讀邊丟：只留「目前這一段」和「上一段」的原話文字，不把整份紀錄 parse 進記憶體
    （Codex 主視窗的 rollout 可以到 1.7GB，整份讀進來會把電腦拖垮，0925 出過事）。
    """
    codex = Path(path).name.startswith("rollout-")
    seg, done, summary, when = [], None, "", ""
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = re.sub(r'"(type|isCompactSummary)"\s*:\s*', r'"\1":', line)
            head = line[:600]
            if codex:
                if '"type":"compacted"' in head:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    done, seg, when = seg, [], o.get("timestamp", "")
                    summary = str((o.get("payload") or {}).get("message") or "")
                    j = summary.find(CODEX_SUMMARY_HEAD)
                    if j >= 0:
                        summary = summary[summary.find(":", j) + 1:].strip()
                    summary = summary or codex_visible_after(o.get("payload") or {})
                    continue
                if '"type":"response_item"' not in head or '"type":"message"' not in line:
                    continue
            else:
                if '"compact_boundary"' in head:
                    try:
                        when = json.loads(line).get("timestamp", "")
                    except ValueError:
                        pass
                    done, seg, summary = seg, [], ""
                    continue
                # Claude 的行把 message 放在 type 前面，標記可能在很後面，所以看整行
                if '"isCompactSummary":true' in line:
                    try:  # 重開視窗會再記一份，取壓縮後的第一份
                        summary = summary or text_of(json.loads(line))
                    except ValueError:
                        pass
                    continue
                if not any(k in line for k in ('"type":"user"', '"type":"assistant"', '"type":"attachment"')):
                    continue
            try:
                m = (codex_msg if codex else claude_msg)(json.loads(line))
            except ValueError:
                continue
            if m:
                seg.append(m)
    if include_tail:
        return summary, done or [], when, seg
    if done is None:
        return "", [], ""
    if codex:  # 摘要本身也會被記成一則 assistant 訊息
        done = [m for m in done if m["text"][:200] not in summary]
    return summary, done, when


def wait_fresh(path, started, limit=10):
    """hook 觸發時，這次的摘要常常還沒寫進檔（晚 0.5 秒左右）：等到讀得到「這次」的摘要為止。
    讀到的壓縮時間比 hook 開始早 120 秒以上，就是上一次的，也要等。"""
    deadline = time.time() + limit
    while True:
        summary, msgs, when = segment(path)
        try:
            fresh = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() >= started - 120
        except ValueError:
            fresh = False
        if (summary and msgs and fresh) or time.time() >= deadline:
            return summary, msgs, fresh, when
        time.sleep(1)


def event_key(path, sid, when, summary):
    """一次壓縮的身分：哪個視窗＋這次壓縮的時間＋摘要雜湊。同一次只補一次。"""
    return f"{sid or Path(path).stem}|{when}|{hashlib.sha1(summary.encode('utf-8')).hexdigest()[:12]}"


def pending_path(sid, root=PENDING):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
        raise ValueError("invalid session_id")
    return root / (sid + ".json")


def read_pending(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_pending(data):
    """PostCompact 的摘要與同一時刻的原話一起保存；不等 transcript 補寫摘要。"""
    sid, summary = data.get("session_id", ""), data.get("compact_summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    target = pending_path(sid, PENDING)
    transcript = str(Path(data["transcript_path"]).resolve())
    disk_summary, done, boundary, tail = segment(transcript, include_tail=True)
    prompt_id = data.get("prompt_id")
    # 同一 PostCompact 重送沿用事件時間，不能因接收時間變了再補一次。
    for previous in (read_pending(target), read_pending(pending_path(sid, CONSUMED))):
        if previous and prompt_id and previous.get("prompt_id") == prompt_id and previous.get("summary") == summary:
            return previous
    received = time.time()
    when = datetime.fromtimestamp(received, timezone.utc).isoformat()
    recent_boundary = False
    try:
        recent_boundary = datetime.fromisoformat(boundary.replace("Z", "+00:00")).timestamp() >= received - 120
    except ValueError:
        pass
    # 新 boundary 已落檔但摘要尚未落檔時用 done；boundary 都還沒落檔時用 tail。
    messages = done if disk_summary == summary or (not disk_summary and recent_boundary) else tail
    record = {"session_id": sid, "transcript_path": transcript, "summary": summary,
              "when": when, "received": received, "prompt_id": prompt_id,
              "baseline_boundary": boundary, "messages": messages,
              "key": event_key(transcript, sid, when, summary)}
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)
    return record


def wait_pending(path, sid, started, limit=10):
    if not sid or Path(path).name.startswith("rollout-"):
        return None
    target = pending_path(sid, PENDING)
    consumed = pending_path(sid, CONSUMED)
    deadline = time.monotonic() + limit
    while True:
        # 新 SessionStart 可能比下一次 PostCompact 早到；不可立刻拿舊 consumed 當重送。
        candidates = (target, consumed) if time.monotonic() >= deadline else (target,)
        for candidate in candidates:
            record = read_pending(candidate)
            if (record and record.get("session_id") == sid
                    and record.get("transcript_path") == str(Path(path).resolve())
                    and started - 120 <= record.get("received", 0) <= time.time() + 1):
                return record
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def pending_is_current(path, sid, record):
    latest = read_pending(pending_path(sid, PENDING))
    if latest and latest.get("key") != record["key"]:
        return False
    summary, _, boundary = segment(path)
    if boundary == record["baseline_boundary"] or summary == record["summary"]:
        return True
    # PostCompact 存檔時新的分界常還沒落檔（baseline 是上一次的）；到user下一句話時檔裡已是這次的分界和摘要。
    # 檔裡的摘要去掉了 <analysis> 和標籤、前面加一段說明，所以比 <summary> 本文的開頭有沒有在裡面。
    return same_summary(summary, record["summary"])


def same_summary(disk, posted):
    body = posted.split("<summary>", 1)[-1].split("</summary>", 1)[0]
    squash = lambda t: re.sub(r"\s+", "", t)
    head = squash(body)[:200]
    return bool(head) and head in squash(disk)


def consume_pending(sid, record):
    target = pending_path(sid, PENDING)
    if (read_pending(target) or {}).get("key") == record["key"]:
        dest = pending_path(sid, CONSUMED)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, dest)  # pending 用完移走，保留同事件去重的收件證據。


def emitted(key=None):
    """讀（key=None）或記下已經補過的壓縮。只留最近 300 筆。"""
    try:
        done = json.loads(EMITTED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = []
    if key is None:
        return set(done)
    EMITTED.parent.mkdir(parents=True, exist_ok=True)
    EMITTED.write_text(json.dumps((done + [key])[-300:], ensure_ascii=False), encoding="utf-8")


SNAPSHOT_LOGS = Path(settings().get("snapshot_dir") or LOG.parent / "snapshots")
FOLLOWUP = LOG.parent / "followup"
FOLLOWUP_TTL = 3600   # 快照一小時內沒出來就不等了
STATE_FIELDS = (("conclusion", "結論"), ("turn", "轉折"), ("unresolved", "未決"))
STATE_CHARS = 80
STATE_LABEL = "後來（交接快照，模型整理、未人工核）"


def snapshots_for(sid, limit=6):
    """這個視窗最近幾份產好的快照，新的在前。狀態檔名：<時間>_<視窗前 8 碼>.<pid>.status.json，裡面記快照路徑。"""
    out = []
    for status in sorted(SNAPSHOT_LOGS.glob(f"*_{sid[:8]}.*.status.json"), reverse=True):
        try:
            snap = json.loads(status.read_text(encoding="utf-8")).get("snapshot")
            data = json.loads(Path(snap).read_text(encoding="utf-8")) if snap else None
        except (OSError, ValueError, TypeError):
            continue
        if data and data.get("snapshots"):
            out.append(data)
            if len(out) >= limit:
                break
    return out


def state_by_message(sid, snaps=None):
    """訊息 id → 它當證據的工作項目（同一則訊息取最新那份快照）。"""
    found = {}
    for data in snapshots_for(sid) if snaps is None else snaps:
        for item in data.get("snapshots") or []:
            for field in item.values():
                for ev in (field.get("evidence") or []) if isinstance(field, dict) else []:
                    parts = str(ev.get("source_id", "")).split(":")
                    if len(parts) >= 5 and parts[1] == sid:
                        found.setdefault(parts[2], item)
    return found


def state_line(item):
    parts = []
    for key, label in STATE_FIELDS:
        field = item.get(key) or {}
        text = str(field.get("text") or "").strip()
        if field.get("provenance") == "recorded" and text:
            parts.append(f"{label}：{text[:STATE_CHARS]}{'…' if len(text) > STATE_CHARS else ''}")
    return "；".join(parts)


def attach_states(rows, sid):
    """rows＝[(訊息 id, 補回那一行)]。對得上的在行下加一行狀態；同一項只寫一次。回（新行們, 沒對上的訊息 id）。"""
    states = state_by_message(sid) if sid else {}
    out, missing, shown = [], [], set()
    for mid, line in rows:
        item = states.get(mid) if mid else None
        state = state_line(item) if item else ""
        if item is not None and id(item) in shown:
            line += "\n  → 後來：同上一項"
        elif state:
            shown.add(id(item))
            line += f"\n  → {STATE_LABEL}：{state}"
        elif mid and item is None:
            missing.append(mid)
        out.append(line)
    return out, missing


def followup_path(sid):
    return pending_path(sid, FOLLOWUP)


def save_followup(sid, key, rows):
    """快照還沒出來的那幾句先記著，之後user說話時再補一次狀態。rows 空就清掉。"""
    target = followup_path(sid)
    if not rows:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps({"session_id": sid, "key": key, "created": time.time(), "rows": rows},
                               ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)


def deliver_followup(sid):
    """回（要補進上下文的文字, 統計）；沒東西補回 (None, None)。補過一次就刪，過期也刪。"""
    target = followup_path(sid)
    record = read_pending(target)
    if not record:
        return None, None
    if time.time() - record.get("created", 0) > FOLLOWUP_TTL:
        target.unlink(missing_ok=True)
        return None, {"reason": "快照一小時內沒出來，不等了", "key": record.get("key")}
    states = state_by_message(sid)
    hits, shown = [], set()
    for row in record["rows"]:
        item = states.get(row["id"])
        if item is None or id(item) in shown or not state_line(item):
            continue
        shown.add(id(item))
        hits.append(f"- {row['line']}\n  → {STATE_LABEL}：{state_line(item)}")
    if not hits:
        return None, None  # 快照還沒出來，下一句再看
    target.unlink(missing_ok=True)
    text = ("〔剛才壓縮後補回的原話，交接快照現在產好了，補上它們後來的狀態。快照是模型從原文整理的、沒人工核；"
            "跟摘要或更晚的原話衝突時以後者為準。〕\n" + "\n".join(hits))
    return text, {"key": record.get("key"), "followup": len(hits), "waiting": len(record["rows"])}


COND_NEXT = re.compile(r"^(?:但|不過|除非|如果|只要|只有|否則|前提|例外|要是|unless|except|but)", re.I)
COND_PREV = re.compile(r"^(?:但|不過|否則|所以|那就|這樣|因此|不然)")


def sentences(msgs):
    units = []
    for mi, m in enumerate(msgs):
        # user貼進來的別人的話（另一個assistant的回報）不能算成user說的
        parts = re.split(r"(<pasted_content[^>]*>[\s\S]*?</pasted_content[^>]*>)", m["text"])
        for part in parts:
            pasted = part.startswith("<pasted_content")
            role = "program" if pasted else m["role"]
            text = re.sub(r"</?pasted_content[^>]*>", "", part)
            for line in re.split(r"\n+|(?<=[。！？])", text):
                s = line.strip(" -*#>|\t")
                # 從第一句稱呼user的地方起，這則訊息後面都是貼來的（「做好了，user。」本身太短也要先認出來）
                # user的短限制句（「不要動正本」）也要留；其他太短的、系統通知（<note>、<summary>）跳過
                short = len(s) < 15 and not (role == "user" and len(s) >= 4 and CONSTRAINT.search(s))
                if short or s.startswith("<"):
                    continue
                if role != "user" and re.search(r"[：:]$", s):
                    continue  # 工具前的過場句
                r = role
                units.append({"role": r, "time": m["time"], "msg": mi, "id": m.get("id"),
                              "text": s[:400] + ("…（截斷）" if len(s) > 400 else "")})
    return units


def with_condition(units, k, taken):
    """補回時把同一則訊息裡緊鄰的條件一起帶上：孤句補回會丟掉「除非…」「但…」。"""
    u, text = units[k], units[k]["text"]
    prev = units[k - 1] if k else None
    nxt = units[k + 1] if k + 1 < len(units) else None
    if prev and prev["msg"] == u["msg"] and prev["role"] == u["role"] and COND_PREV.search(text) and k - 1 not in taken:
        text = prev["text"] + " " + text
        taken.add(k - 1)
    if (nxt and nxt["msg"] == u["msg"] and nxt["role"] == u["role"] and k + 1 not in taken
            and (COND_NEXT.search(nxt["text"]) or CONSTRAINT.search(nxt["text"]))):
        text = text + " " + nxt["text"]
        taken.add(k + 1)
    return text


def uncovered(units, summary, recall, embedding=True):
    """每句的「摘要沒蓋到」分數：詞面一半、語意一半（語意相似度分布窄，差距放大兩倍）。"""
    bags = [recall.terms(u["text"]) for u in units]
    df = {}
    for b in bags:
        for t in b:
            df[t] = df.get(t, 0) + 1
    n = len(units)
    have = recall.terms(summary)
    lex = []
    for b in bags:
        w = {t: math.log(1 + n / df[t]) for t in b}
        lex.append(sum(v for t, v in w.items() if t in have) / (sum(w.values()) or 1))
    vectors = recall._embed([u["text"] for u in units]) if units and embedding else None
    chunks = [summary[i:i + 300] for i in range(0, len(summary), 200)]
    summaries = recall._embed(chunks) if vectors else None
    if not vectors or not summaries:
        return [1 - x for x in lex], "lexical", None
    def normalize(v):
        norm = math.sqrt(sum(float(x) ** 2 for x in v))
        if not norm:
            raise ValueError("zero embedding")
        return [float(x) / norm for x in v]
    try:
        E, C = [normalize(v) for v in vectors], [normalize(v) for v in summaries]
        if len(E[0]) != len(C[0]):
            raise ValueError("embedding dimensions changed")
        sem = [max(sum(a * b for a, b in zip(e, c)) for c in C) for e in E]
        return [(1 - a) * 0.5 + (1 - b) for a, b in zip(lex, sem)], "lexical+embedding", (E, C, chunks)
    except (ValueError, TypeError, OverflowError) as exc:
        recall.embedding_error = type(exc).__name__
        return [1 - x for x in lex], "lexical", None


def local(ts):
    """紀錄裡是 UTC（…Z），顯示成本機時間；0925 就是把 UTC 當台灣時間讀，誤判了一次。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16].replace("T", " ")


def revisited(k, space):
    """這句之後，原話裡最像它的一句有多像。"""
    E = space[0]
    return max((sum(a * b for a, b in zip(v, E[k])) for v in E[k + 1:]), default=0.0)


def run(path, cut=None, sid="", hook=False, prompt=False):
    t0 = time.time()
    key = None
    pending = None
    if hook:
        # Claude：PostCompact 要等 SessionStart 跑完才會來（0926、0927 兩次真實壓縮都是同一秒進來），
        # 所以改在壓縮後user的第一句話（UserPromptSubmit）才取 pending；Codex 照舊在 SessionStart 等。
        pending = read_pending(pending_path(sid, PENDING)) if prompt else wait_pending(path, sid, t0)
        if pending:
            summary, msgs, fresh, when = pending["summary"], pending["messages"], True, pending["when"]
        else:
            summary, msgs, fresh, when = wait_fresh(path, t0)
        if not fresh:
            return None, {"reason": "等不到這次的摘要", "summary": len(summary), "msgs": len(msgs), "waited": round(time.time() - t0, 1)}
        key = event_key(path, sid, when, summary)
        if key in emitted():  # hook 重送、或同一次壓縮觸發兩次
            return None, {"reason": "這次壓縮已經補過", "key": key}
        if pending and not pending_is_current(path, sid, pending):
            return None, {"reason": "送出前又有新的壓縮，這批作廢", "key": key}
    else:
        summary, msgs, _ = segment(path)
    if cut:
        summary = "\n".join(l for l in summary.splitlines() if not any(c.lower() in l.lower() for c in cut))
    if not summary or not msgs:
        return None, {"reason": "沒有摘要或原話", "summary": len(summary), "msgs": len(msgs)}
    recall = load_recall()
    units = sentences(msgs)
    if not units:
        return None, {"reason": "no eligible sentences", "picked": 0}
    gap, note, space = uncovered(units, summary, recall)
    # 否定、限制、例外：詞面和語意都會把「不要改正本」跟摘要裡的「改正本」算成很像，
    # Coverage alone can mistake a prohibition for a covered positive statement.
    hard = {k for k, u in enumerate(units) if CONSTRAINT.search(u["text"])}
    order = sorted(range(len(units)), key=lambda k: (k not in hard, -gap[k]))
    pool = order[:POOL]
    # 0926 GPT Pro 外審：後文相似 ≥0.7 只代表「又談過」，不代表被推翻（重申、補充、反駁都會很像），
    # 所以不再跳過，只在補回時標出後面又談過的時間，讓讀的人以較晚的為準。
    stale = [k for k in pool if space is not None and revisited(k, space) >= REVISITED]
    cands = [(i, {"role": units[k]["role"], "text": units[k]["text"]}) for i, k in enumerate(pool)]
    scores = recall.rank(QUESTION, cands)
    if scores is None:
        # Lexical-only is also the ordinary provider=none production path.
        # Constraint candidates survive even when wording falsely looks covered.
        if space is not None:
            gap, note, space = uncovered(units, summary, recall, embedding=False)
            stale = []
            pool = sorted(range(len(units)), key=lambda k: (k not in hard, -gap[k]))[:POOL]
        scores = {i: (1.0 if k in hard else max(0.0, min(1.0, gap[k]))) for i, k in enumerate(pool)}
    picked = [pool[i] for i in sorted(scores, key=lambda i: -scores[i]) if scores[i] >= MIN_SCORE][:MAX_PICK]
    score_of = {pool[i]: s for i, s in scores.items()}
    lines, used, taken, refs = [], 0, set(picked), []
    for k in sorted(picked):  # 照時間順序
        u = units[k]
        text = with_condition(units, k, taken)
        if used + len(text) > MAX_CHARS:
            break
        used += len(text)
        later = ""
        if k in stale:
            j = max(range(k + 1, len(units)), key=lambda j: sum(a * b for a, b in zip(space[0][j], space[0][k])))
            later = f"；{local(units[j]['time'])[11:]} 又談過，以較晚的為準"
        lines.append(f"- {local(u['time'])} {u['role']}：{text}（ranker {score_of[k]:.2f}{later}）")
        refs.append((u.get("id"), f"{local(u['time'])} {u['role']}：{u['text'][:40]}{'…' if len(u['text']) > 40 else ''}"))
    stat = {"summary": len(summary), "msgs": len(msgs), "sentences": len(units), "pool": len(pool), "coverage": note,
            "picked": len(lines), "chars": used, "secs": round(time.time() - t0, 1),
            "revisited": len(stale), "provider": recall.provider, "fallback": recall.fallback,
            "embedding_error": recall.embedding_error,
            "top": sorted(scores.values(), reverse=True)[:5]}
    if not lines:
        return None, stat
    lines, missing = attach_states(list(zip([r[0] for r in refs], lines)), sid)
    stat["states"] = sum("\n  → " in l for l in lines)
    stat["states_waiting"] = len(missing)
    if key:
        # ranker 要跑幾十秒：送出前再確認沒有更新的壓縮，免得把上一次的補丁送進下一段
        _, _, now_when = segment(path)
        if (pending and not pending_is_current(path, sid, pending)) or (not pending and now_when != when):
            return None, {**stat, "reason": "送出前又有新的壓縮，這批作廢", "key": key}
        emitted(key)
        save_followup(sid, key, [{"id": mid, "line": ref} for mid, ref in refs if mid in set(missing)])
        if pending:
            consume_pending(sid, pending)
        stat["key"] = key
        stat["summary_source"] = "PostCompact" if pending else "transcript"
    text = ("〔摘要沒蓋到的原話：本機覆蓋比對與可選排序模型挑出的候選。"
            "只當提示：跟摘要衝突時，核對時間並以較晚的記錄為準；完整前後文請查原始 transcript。〕\n"
            + "\n".join(lines))
    return text, stat


def log(entry):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def receive(data):
    """Return one hook response. PostCompact stores; the next prompt delivers once."""
    if data.get("agent_id") or data.get("subagent"):
        return {}
    sid = data.get("session_id", "")
    event = data.get("hook_event_name")
    path = data.get("transcript_path")
    source = data.get("source")
    record = None
    try:
        if event == "PostCompact":
            record = save_pending(data)
            log({"session": sid, "source": event, "key": record["key"] if record else None})
            return {}
        if event == "UserPromptSubmit":
            record = read_pending(pending_path(sid, PENDING))
            if not record:
                text, stat = deliver_followup(sid)
                if stat:
                    log({"session": sid, "source": "followup", **stat})
                return response("Original wording: later snapshot state is available", text, event) if text else {}
            path, source = record["transcript_path"], "prompt"
        elif event != "SessionStart" or source != "compact":
            return {}
        # Claude emits PostCompact after SessionStart. Do not wait here.
        elif path and not Path(path).name.startswith("rollout-"):
            return {}
        if not path or not Path(path).exists():
            path = codex_rollout(sid) if sid else None
        if not path:
            return {}
        save_followup(sid, None, [])
        text, stat = run(path, sid=sid, hook=True, prompt=source == "prompt")
        if record:
            consume_pending(sid, record)
        log({"session": sid, "source": source, "file": Path(path).name, **stat})
        if text:
            return response(f"Original wording: {stat['picked']} candidates restored ({stat['provider']})", text, event)
        return {}
    except Exception as exc:
        # Hook failures must not block a user turn. Never log provider output or credentials.
        log({"session": sid, "source": event, "error": type(exc).__name__})
        if record and event == "UserPromptSubmit":
            consume_pending(sid, record)
        return response("Original wording: skipped; see compact-gap/log.jsonl", event=event)


def response(msg, context=None, event="SessionStart"):
    out = {"systemMessage": msg}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript")
    ap.add_argument("--cut", help="Replay only: remove summary lines containing these terms, separated by |")
    args = ap.parse_args()
    if args.transcript:
        text, stat = run(args.transcript, args.cut.split("|") if args.cut else None)
        print(json.dumps(stat, ensure_ascii=False))
        print(text or "No uncovered candidates")
        return
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        return
    sys.stdout.buffer.write(json.dumps(receive(data), ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
