"""Claude 端各支程式共用的位置；換電腦或別人裝，只靠環境變數改，不改程式。

  CW_STATE_ROOT  狀態根目錄（工具存檔、分段、原話檔、門檻、trace…都在底下）；
                 預設 ~/.local/state/wifeos（老公這台一直用這裡，舊資料不搬）
  CW_RECALL      找原話的 wife_recall.py；預設 ~/.local/bin/wife_recall.py，沒有就不提示
"""
import os
from pathlib import Path

STATE_ROOT = Path(os.environ.get("CW_STATE_ROOT") or Path.home() / ".local" / "state" / "wifeos")
WORKBENCH_STATE = STATE_ROOT / "context-workbench"
WIFE_COMPACT_STATE = STATE_ROOT / "wife-compact"
RECALL = Path(os.environ.get("CW_RECALL") or Path.home() / ".local" / "bin" / "wife_recall.py")
