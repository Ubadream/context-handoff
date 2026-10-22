"""Claude 端各支程式共用的位置；換電腦或別人裝，只靠環境變數改，不改程式。

  CW_STATE_ROOT  狀態根目錄（工具存檔、分段、原話檔、門檻、trace…都在底下）；
                 預設 ~/.local/state/wifeos（老公這台一直用這裡，舊資料不搬）
  CW_RECALL      找原話的 wife_recall.py；預設 ~/.local/bin/wife_recall.py，沒有就不提示
"""
import os
from pathlib import Path

from cw_lang import t

STATE_ROOT = Path(os.environ.get("CW_STATE_ROOT") or Path.home() / ".local" / "state" / "wifeos")
WORKBENCH_STATE = STATE_ROOT / "context-workbench"
WIFE_COMPACT_STATE = STATE_ROOT / "wife-compact"
RECALL = Path(os.environ.get("CW_RECALL") or Path.home() / ".local" / "bin" / "wife_recall.py")

# 交接分三塊（決策／情感／工作進度，各附原話定位）：我們自己的寫法，選用、預設關。
# 開：python adapters/claude/cw_thresholds.py set --handoff-sections on（寫進 thresholds.json 的 claude.handoff_sections）。
# 開了以後，上下文提醒、閒置提醒、prepare／handoff_check 都多帶一行提醒，交接檢查另做格式軟檢查（不擋壓縮）。
HANDOFF_REMINDER = t("交接：決策／情感／工作進度，各附原話定位；無則寫無，錯誤入簿。", 'Handoff: Decisions / Feelings / Work progress, each with original-text references; write none when absent, and log errors in the error log.')


def handoff_sections():
    import json
    try:
        path = Path(os.environ.get("CW_THRESHOLDS") or WORKBENCH_STATE / "thresholds.json")
        claude = json.loads(path.read_text(encoding="utf-8")).get("claude") or {}
        return claude.get("handoff_sections") is True
    except (OSError, ValueError, AttributeError):
        return False
