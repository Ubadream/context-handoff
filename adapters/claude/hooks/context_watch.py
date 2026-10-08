"""UserPromptSubmit hook：上下文跨過門檻時，在老婆這邊冒一句，讓她提醒老公。

只提醒、不壓縮、不改上限（老公 0925 定案：壓不壓由他喊，還在做就不壓）。
每個門檻每段只冒一次；壓縮後用量掉下來就重新算。失敗一律安靜放行。
"""
import json, os, sys, time

# 門檻可由 context-workbench 面板改（cw_thresholds.py），存在 thresholds.json 的 claude 段；每次發話重讀，沒有檔就用這組
DEFAULTS = {"remind": [250_000, 400_000, 600_000, 800_000, 900_000], "consider": 400_000, "urgent": 800_000}
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adapters/claude：共用位置
from cw_paths import STATE_ROOT, WORKBENCH_STATE  # noqa: E402

THRESHOLDS_FILE = str(WORKBENCH_STATE / "thresholds.json")


def thresholds():
    try:
        mine = json.load(open(THRESHOLDS_FILE, encoding="utf-8")).get("claude") or {}
    except Exception:
        mine = {}
    return {**DEFAULTS, **mine}
STATE_DIR = str(STATE_ROOT / "claude-context-watch")
TAIL_BYTES = 4 * 1024 * 1024


def last_context_tokens(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    for line in reversed(lines):
        if '"subtype":"compact_boundary"' in line:  # 壓縮後還沒有新回合，前面的用量是舊的
            return None
        if '"usage"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant" or o.get("isSidechain"):
            continue
        u = (o.get("message") or {}).get("usage") or {}
        n = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        if n:
            return n
    return None


def main():
    data = json.load(sys.stdin)
    path, sid = data.get("transcript_path"), data.get("session_id") or "unknown"
    if not path or not os.path.exists(path):
        return
    tokens = last_context_tokens(path)
    if not tokens:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"{sid}.json")
    try:
        state = json.load(open(state_file, encoding="utf-8"))
    except Exception:
        state = {"announced": 0}
    if tokens < state.get("announced", 0) * 0.6:  # 壓縮過了，重新算
        state["announced"] = 0
    th = thresholds()
    crossed = [t for t in th["remind"] if tokens >= t > state.get("announced", 0)]
    if not crossed:
        json.dump(state, open(state_file, "w", encoding="utf-8"))
        return
    top = crossed[-1]
    state.update(announced=top, at=time.strftime("%Y-%m-%d %H:%M:%S"), tokens=tokens)
    json.dump(state, open(state_file, "w", encoding="utf-8"))
    wan = tokens // 10_000
    if top >= th["urgent"]:
        tail = ("已經很滿，內建自動壓縮隨時會來。在下一個段落點用 wife-compact 的 compact 工具自己壓"
                "（先更新交接；細節正熱用 C），同一則回覆先跟老公說。")
    elif top >= th["consider"]:
        tail = ("老公授權老婆自己判斷時機（0925）：這段工作收尾、交接剛更新好時，可以用 wife-compact 的 compact 工具壓"
                "（A 預設／C 細節正熱／B 整段細節都要），先跟老公說；工具做到一半或他正在講就不壓。")
    else:
        tail = "還早，不用壓；接下來留意段落點。"
    msg = f"[上下文提醒｜約 {wan} 萬 token，剛跨過 {top // 10_000} 萬。{tail}]"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": msg}}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
