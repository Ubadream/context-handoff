"""PreToolUse／PostToolUse／PostToolUseFailure：每一次工具呼叫的全文存檔，給 context-workbench 面板收件。

老公 0929：「全文就抓到面板那 可以分工具項目 作用對象 發出時間 回來時間 去分類 我想擴到基本上全部工具」
「編號最好能直接看出 工具項目 作用對象 發出時間 回來時間」

這一版只存檔、不改上下文（改上下文的是 tool_output_trim.py 和壓縮）。
編號：MMDD-HHMMSS~HHMMSS_工具_對象_4碼，例：0929-215304~215306_Bash_compact_gap.py_a1b2
存放：~/.local/state/wifeos/context-workbench/tool-calls/<日期>/<編號>.json，另有 index.jsonl（不含全文）。
圖片的 base64 不存（只記長度）。保留 30 天。任何錯誤都安靜放行，不擋工具。
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adapters/claude：共用位置
from cw_paths import WORKBENCH_STATE  # noqa: E402

ROOT = WORKBENCH_STATE / "tool-calls"
STARTS = ROOT / "_starts"
BY_SESSION = ROOT / "by-session"  # 對話 id → 這個對話的 {tool_use_id, 編號, 狀態}
KEEP_DAYS = 30
TARGET_KEYS = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query", "description", "skill", "subagent_type")
GROUPS = [  # 分類：工具名 → 類別
    (re.compile(r"^(Read|NotebookRead)$"), "讀檔"),
    (re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit)$"), "寫檔"),
    (re.compile(r"^(Grep|Glob)$"), "搜尋"),
    (re.compile(r"^(Bash|PowerShell)$"), "指令"),
    (re.compile(r"^(WebFetch|WebSearch)$"), "網路"),
    (re.compile(r"^mcp__(Claude_Browser|claude-in-chrome)__"), "瀏覽器"),
    (re.compile(r"^mcp__computer-use__"), "電腦操作"),
    (re.compile(r"^(Agent|Task|SendMessage)$"), "子代理"),
    (re.compile(r"^(Skill|ToolSearch)$"), "技能與工具"),
    (re.compile(r"^mcp__"), "MCP"),
]


def group(tool):
    return next((name for pat, name in GROUPS if pat.search(tool)), "其他")


def target(tool, tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for key in TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            if key in ("file_path", "notebook_path", "path"):
                return Path(value).name
            if key == "url":
                return re.sub(r"^https?://", "", value).split("/")[0]
            return value.strip().split()[0] if key == "command" else value.strip()
    return ""


def slug(text, limit):
    text = re.sub(r'[\\/:*?"<>|\s]+', "-", text).strip("-")
    return text[:limit] or "-"


def strip_images(obj):
    """截圖回傳的 base64 很大，面板要的是發生了什麼；只留長度。"""
    if isinstance(obj, dict):
        if obj.get("type") == "image" or ("data" in obj and isinstance(obj.get("data"), str) and len(obj["data"]) > 2000):
            return {k: (f"〔base64 {len(v)} 字，未存〕" if k == "data" and isinstance(v, str) else strip_images(v)) for k, v in obj.items()}
        return {k: strip_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_images(v) for v in obj]
    return obj


def prune():
    mark = ROOT / ".pruned"
    today = datetime.now().strftime("%Y%m%d")
    if mark.exists() and mark.read_text() == today:
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for day in ROOT.iterdir():
        if day.is_dir() and re.fullmatch(r"\d{8}", day.name) and day.stat().st_mtime < cutoff:
            for f in day.iterdir():
                f.unlink()
            day.rmdir()
    for f in STARTS.glob("*.json") if STARTS.exists() else []:
        if f.stat().st_mtime < time.time() - 86400:
            f.unlink()
    for f in BY_SESSION.glob("*.jsonl") if BY_SESSION.exists() else []:
        if f.stat().st_mtime < cutoff:
            f.unlink()
    mark.write_text(today)


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    event, use_id = data.get("hook_event_name"), data.get("tool_use_id") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", use_id):
        return
    now = datetime.now().astimezone()
    if event == "PreToolUse":
        STARTS.mkdir(parents=True, exist_ok=True)
        (STARTS / f"{use_id}.json").write_text(json.dumps({"started": now.isoformat(timespec="seconds")}), encoding="utf-8")
        return
    if event not in ("PostToolUse", "PostToolUseFailure"):
        return
    start_file = STARTS / f"{use_id}.json"
    try:
        started = datetime.fromisoformat(json.loads(start_file.read_text(encoding="utf-8"))["started"])
        start_file.unlink()
    except (OSError, ValueError, KeyError):
        started = None
    tool = data.get("tool_name") or "?"
    obj = target(tool, data.get("tool_input"))
    if event == "PostToolUseFailure":
        status = "interrupted" if data.get("is_interrupt") else "failed"
    else:
        resp = data.get("tool_response")
        status = "failed" if isinstance(resp, dict) and (resp.get("is_error") or resp.get("interrupted")) else "ok"
    t0 = started or now
    call_id = f"{t0:%m%d-%H%M%S}~{now:%H%M%S}_{slug(tool.replace('mcp__', ''), 32)}_{slug(obj, 40)}_{use_id[-4:]}"
    record = {"id": call_id, "session": data.get("session_id"), "tool_use_id": use_id, "tool": tool, "group": group(tool),
              "target": obj, "started": started.isoformat(timespec="seconds") if started else None,
              "finished": now.isoformat(timespec="seconds"), "status": status,
              "input": strip_images(data.get("tool_input")), "response": strip_images(data.get("tool_response")),
              "error": data.get("error"), "cwd": data.get("cwd")}
    day = ROOT / now.strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, ensure_ascii=False)
    (day / f"{call_id}.json").write_text(body, encoding="utf-8")
    index = {k: record[k] for k in ("id", "session", "tool_use_id", "tool", "group", "target", "started", "finished", "status")}
    index["bytes"] = len(body.encode("utf-8"))
    with open(ROOT / "index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(index, ensure_ascii=False) + "\n")
    # 壓縮時 wife-compact 用 tool_use_id 查編號；外掛一次只能讀 4 MiB，總索引會超過，所以每個對話另存一份小的
    session = data.get("session_id") or ""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
        BY_SESSION.mkdir(parents=True, exist_ok=True)
        with open(BY_SESSION / f"{session}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"u": use_id, "id": call_id, "status": status}, ensure_ascii=False) + "\n")
    prune()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
