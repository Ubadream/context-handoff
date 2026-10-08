"""Claude 固定開銷瘦身的開關（wife-compact 0.3.13 起）。

  python adapters/claude/cw_diet.py show   # 開或關、延後了哪些工具、技能清單怎麼縮
  python adapters/claude/cw_diet.py on     # 開（刪掉 DIET_OFF）
  python adapters/claude/cw_diet.py off    # 關（建 DIET_OFF：工具與技能清單全部照原樣）

瘦身＝少用的工具移到 ToolSearch 後面（平常只列名字）、技能清單每個留「做什麼」＋「什麼時候用」兩句。
外掛在 tool.describe／prompt.attachment 時讀這個檔，工具清單在開窗時定下，所以改完要重開 Claude 才生效。
延後名單寫在外掛程式裡（DIET_DEFER），這支只讀出來顯示。
"""
import json
import re
import sys
from pathlib import Path

from cw_paths import WIFE_COMPACT_STATE  # noqa: E402

OFF = WIFE_COMPACT_STATE / "DIET_OFF"
PLUGIN = Path(__file__).resolve().parent / "wife-compact" / "hooks" / "wife-compact.ts"


def deferred():
    src = PLUGIN.read_text(encoding="utf-8")
    names = re.search(r"const DIET_DEFER = new Set\(\[(.*?)\]\)", src, re.S)
    prefixes = re.search(r"const DIET_DEFER_PREFIX = \[(.*?)\]", src, re.S)
    grab = lambda m: re.findall(r"'([^']+)'", m.group(1)) if m else []
    return grab(names), grab(prefixes)


def show():
    names, prefixes = deferred()
    return {
        "enabled": not OFF.exists(),
        "switch_file": str(OFF),
        "applies": "重開 Claude 後",
        "deferred_tools": names,
        "deferred_prefixes": prefixes,
        "skill_listing": "每個技能留「做什麼」＋「什麼時候用」兩句",
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "on":
        OFF.unlink(missing_ok=True)
    elif cmd == "off":
        OFF.parent.mkdir(parents=True, exist_ok=True)
        OFF.write_text("瘦身關閉：wife-compact 的工具延後與技能清單縮短都不做。刪掉這個檔（或 cw_diet.py on）再重開就恢復。\n", encoding="utf-8")
    elif cmd != "show":
        sys.exit("用法：cw_diet.py show|on|off")
    sys.stdout.buffer.write((json.dumps(show(), ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
