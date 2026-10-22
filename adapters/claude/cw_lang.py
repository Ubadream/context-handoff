"""Select adapter message language without caching user configuration."""
import json
import os
from pathlib import Path


def lang():
    override = os.environ.get("CW_LANG")
    if override in ("en", "zh"):
        return override
    root = Path(os.environ.get("CW_STATE_ROOT") or Path.home() / ".local" / "state" / "wifeos")
    path = Path(os.environ.get("CW_THRESHOLDS") or root / "context-workbench" / "thresholds.json")
    try:
        value = (json.loads(path.read_text(encoding="utf-8")).get("claude") or {}).get("language")
        return value if value in ("en", "zh") else "zh"
    except (OSError, ValueError, AttributeError, TypeError):
        return "zh"


def t(zh, en):
    return en if lang() == "en" else zh
