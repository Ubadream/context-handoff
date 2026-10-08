#!/usr/bin/env python3
"""PostToolUse: keep large tool output out of Claude's context.

Bash/PowerShell, Grep, WebFetch/WebSearch and the browser page-text MCP tools:
any string over the tool's threshold is saved whole under STORE and replaced by
its head, its tail and a pointer line.

Read is different: the file itself is the full copy, so nothing is saved. A
Read without offset/limit that comes back over READ_THRESHOLD keeps only its
first lines (line numbers stay right) plus a note saying where to continue.

Only string values change, so the rewritten response keeps the tool's output
shape (the host drops rewrites that don't match). Fail-open: any problem prints
nothing, which leaves the original output untouched.

Let one call through untrimmed: `# full-output` in a Bash command; offset/limit
on Read; head_limit on Grep (asked for a size on purpose).
"""

import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# 工具 → (超過幾字才外置, 留頭, 留尾)；Bash 量多又雜所以壓得最緊
LIMITS = {"Bash": (3000, 1000, 600), "PowerShell": (3000, 1000, 600)}
OTHER = (6000, 2500, 1000)  # Grep、網頁、瀏覽器讀頁：通常就是要看的內容，放寬一點
READ_THRESHOLD = 20000
READ_KEEP = 12000
KEEP_DAYS = 7
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adapters/claude：共用位置
from cw_paths import STATE_ROOT  # noqa: E402

STORE = STATE_ROOT / "claude-tool-output"


def prune_old():
    stamp = STORE / ".pruned"
    today = datetime.now().strftime("%Y%m%d")
    try:
        if stamp.read_text() == today:
            return
    except OSError:
        pass
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in STORE.glob("*/*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    for d in STORE.iterdir():
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    stamp.write_text(today)


def trim(text, label, source, limits):
    threshold, head, tail = limits
    if not isinstance(text, str) or len(text) <= threshold:
        return text, False
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    day = STORE / datetime.now().strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{digest}_{label}.txt"
    if not path.exists():
        path.write_text(f"# {source}\n{text}", encoding="utf-8")
    lines = text.count("\n") + 1
    note = (f"\n\n[… 中間略過；完整{label} {len(text):,} 字、{lines:,} 行已存到 {path} "
            f"（第 1 行是來源）。要看就用 Read（offset/limit）或 grep 這個檔。]\n\n")
    first, last = text[:head], text[-tail:]
    # 切在換行處，不留半行；整段沒換行就照字數切
    if "\n" in first:
        first = first[:first.rindex("\n")]
    if "\n" in last:
        last = last[last.index("\n") + 1:]
    return first + note + last, True


def walk(value, label, source, limits):
    """把回應裡每個超長字串換掉，其他照原樣（含 MCP 的 [{type, text}] 清單）。"""
    if isinstance(value, str):
        return trim(value, label, source, limits)
    if isinstance(value, list):
        out, hit = [], False
        for v in value:
            v, h = walk(v, label, source, limits)
            out.append(v)
            hit = hit or h
        return out, hit
    if isinstance(value, dict):
        out, hit = {}, False
        for k, v in value.items():
            v, h = walk(v, k if isinstance(v, str) else label, source, limits)
            out[k] = v
            hit = hit or h
        return out, hit
    return value, False


def trim_read(response, tool_input):
    """Read：原檔就是全文，不另存；只留前面幾行，行號照舊，告訴老婆從哪行接著讀。"""
    if "offset" in tool_input or "limit" in tool_input:
        return None
    f = response.get("file") if isinstance(response, dict) else None
    content = f.get("content") if isinstance(f, dict) else None
    if not isinstance(content, str) or len(content) <= READ_THRESHOLD:
        return None
    lines = content.split("\n")
    kept, size = [], 0
    for line in lines:
        if size + len(line) + 1 > READ_KEEP and kept:
            break
        kept.append(line)
        size += len(line) + 1
    start = f.get("startLine") or 1
    next_line = start + len(kept)
    total = f.get("totalLines") or (start + len(lines) - 1)
    kept.append(f"[… 只放前 {len(kept)} 行進上下文（這次讀到 {len(lines):,} 行、{len(content):,} 字，檔案共 {total:,} 行）。"
                f"要後面就 Read 同一個檔，offset={next_line}，帶 limit；要一次全讀就明寫 offset=1 和 limit。]")
    new_f = dict(f, content="\n".join(kept))
    if isinstance(f.get("numLines"), int):
        new_f["numLines"] = len(kept)
    return dict(response, file=new_f)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    response = event.get("tool_response")
    if response is None:
        return
    if tool == "Read":
        updated = trim_read(response, tool_input)
    else:
        if tool in ("Bash", "PowerShell"):
            source = "command: " + str(tool_input.get("command", ""))
            if "# full-output" in source:
                return
        elif tool == "Grep":
            if "head_limit" in tool_input:
                return
            source = f"Grep {tool_input.get('pattern', '')!r} in {tool_input.get('path', '.')}"
        else:
            source = f"{tool} {json.dumps(tool_input, ensure_ascii=False)[:300]}"
        STORE.mkdir(parents=True, exist_ok=True)
        updated, hit = walk(response, "輸出", source, LIMITS.get(tool, OTHER))
        if not hit:
            return
        try:
            prune_old()
        except OSError:
            pass
    if updated is None:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "updatedToolOutput": updated}},
              sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
