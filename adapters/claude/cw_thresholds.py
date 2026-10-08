"""Claude 端的上下文門檻：看與改（面板按鈕和老婆都走這支）。

  python adapters/claude/cw_thresholds.py show
  python adapters/claude/cw_thresholds.py set [--remind 250000,400000,...] [--consider 400000] [--urgent 800000]
                                              [--auto-compact-window 600000|default]
                                              [--idle on|off] [--idle-tokens 400000] [--idle-minutes 50]

三組數字、各自住在宿主真的會讀的地方，這支不另存副本：
  remind／consider／urgent  提醒門檻 → ~/.local/state/wifeos/context-workbench/thresholds.json，
                            adapters/claude/hooks/context_watch.py 每次老公發話都重讀，改了下一句就生效。
                            remind＝跨過就冒一句提醒；consider＝從這裡起提醒裡說「段落點可以自己壓」；urgent＝說「下一個段落點就壓」。
  auto-compact-window       內建自動壓縮的視窗 → ~/.claude/settings.json 的 env CLAUDE_CODE_AUTO_COMPACT_WINDOW，
                            Claude Code 開視窗時讀，改了要重開才生效；default＝拿掉，回到模型自己的上限。
  idle                      閒置提醒 → 同一個 thresholds.json 的 claude.idle；wife-compact 每輪結束時重讀，
                            上下文 ≥ tokens 就排 minutes 分鐘後的提醒（老公先說話就作廢）。改了下一輪就生效。
改 settings.json 前先留一份當天的前像（settings.json.bak-cw-<日期>，同一天只留第一份）。
輸出一律 JSON：目前值、各自在哪、何時生效。
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
SETTINGS = HOME / "settings.json"
from cw_paths import WORKBENCH_STATE  # noqa: E402

THRESHOLDS = Path(os.environ.get("CW_THRESHOLDS", WORKBENCH_STATE / "thresholds.json"))
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
DEFAULTS = {"remind": [250_000, 400_000, 600_000, 800_000, 900_000], "consider": 400_000, "urgent": 800_000,
            "idle": {"enabled": True, "tokens": 400_000, "minutes": 50}}
LOW, HIGH = 50_000, 1_000_000  # Claude 目前的模型上限 1M


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def current():
    t = {**DEFAULTS, **(load_json(THRESHOLDS, {}).get("claude") or {})}
    window = (load_json(SETTINGS, {}).get("env") or {}).get(WINDOW_ENV)
    return {
        "host": "claude",
        "remind": {"value": t["remind"], "consider": t["consider"], "urgent": t["urgent"],
                   "stored_in": str(THRESHOLDS), "takes_effect": "下一句老公發話（context_watch.py 每次重讀）"},
        "idle": {**DEFAULTS["idle"], **(t.get("idle") or {}), "stored_in": str(THRESHOLDS),
                 "takes_effect": "下一輪結束（wife-compact 每次重讀）", "note": "Claude 提示快取約一小時，minutes 要小於 60 才來得及"},
        "auto_compact_window": {"value": int(window) if window else None, "default_means": "模型自己的上限",
                                "stored_in": f"{SETTINGS} env.{WINDOW_ENV}", "takes_effect": "重開 Claude 之後"},
    }


def check(n, what):
    if not LOW <= n <= HIGH:
        sys.exit(f"{what} 要在 {LOW:,}～{HIGH:,} 之間：{n:,}")
    return n


def set_values(args):
    if args.remind or args.consider or args.urgent or args.idle or args.idle_tokens or args.idle_minutes:
        data = load_json(THRESHOLDS, {})
        claude = dict(data.get("claude") or {})  # 只寫這次改的欄位；沒存過的沿用預設、不補寫（面板讀回要逐欄對得上）
        if args.remind:
            claude["remind"] = sorted({check(int(x), "remind") for x in args.remind.split(",") if x.strip()})
        if args.consider:
            claude["consider"] = check(args.consider, "consider")
        if args.urgent:
            claude["urgent"] = check(args.urgent, "urgent")
        if args.idle or args.idle_tokens or args.idle_minutes:
            idle = {**DEFAULTS["idle"], **(claude.get("idle") or {})}
            if args.idle:
                idle["enabled"] = args.idle == "on"
            if args.idle_tokens:
                idle["tokens"] = check(args.idle_tokens, "idle-tokens")
            if args.idle_minutes:
                if not 1 <= args.idle_minutes <= 59:
                    sys.exit("idle-minutes 要在 1～59 之間（快取約一小時）")
                idle["minutes"] = args.idle_minutes
            claude["idle"] = idle
        if claude.get("consider", DEFAULTS["consider"]) > claude.get("urgent", DEFAULTS["urgent"]):
            sys.exit(f"consider（{claude.get('consider', DEFAULTS['consider']):,}）不能大於 urgent（{claude.get('urgent', DEFAULTS['urgent']):,}）")
        data["claude"] = claude
        THRESHOLDS.parent.mkdir(parents=True, exist_ok=True)
        THRESHOLDS.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.auto_compact_window:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        env = settings.setdefault("env", {})
        if args.auto_compact_window == "default":
            env.pop(WINDOW_ENV, None)
        else:
            env[WINDOW_ENV] = str(check(int(args.auto_compact_window), "auto-compact-window"))
        backup = SETTINGS.with_name(f"settings.json.bak-cw-{datetime.now():%Y%m%d}")
        if not backup.exists():
            shutil.copy2(SETTINGS, backup)
        SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser(description="Claude 上下文門檻：看與改")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("--remind", help="逗號分隔的 token 數，例如 250000,400000,600000")
    s.add_argument("--consider", type=int)
    s.add_argument("--urgent", type=int)
    s.add_argument("--auto-compact-window", help="token 數，或 default")
    s.add_argument("--idle", choices=["on", "off"])
    s.add_argument("--idle-tokens", type=int)
    s.add_argument("--idle-minutes", type=int)
    args = ap.parse_args()
    if args.cmd == "set":
        set_values(args)
    sys.stdout.buffer.write(json.dumps(current(), ensure_ascii=False, indent=1).encode("utf-8") + b"\n")


if __name__ == "__main__":
    main()
