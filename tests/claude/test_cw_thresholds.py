"""cw_thresholds：提醒門檻寫進 thresholds.json、自動壓縮視窗寫進 settings.json env（合成資料）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "adapters" / "claude" / "cw_thresholds.py"


def run(tmp_path, *args):
    env = {**os.environ, "CLAUDE_HOME": str(tmp_path / "claude"), "CW_THRESHOLDS": str(tmp_path / "t.json"), "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, env=env)
    return r.returncode, json.loads(r.stdout or b"null"), r.stderr.decode("utf-8", "ignore")


def test_set_and_show(tmp_path):
    (tmp_path / "claude").mkdir()
    settings = tmp_path / "claude" / "settings.json"
    settings.write_text(json.dumps({"env": {"A": "1"}, "hooks": {}}, indent=2) + "\n", encoding="utf-8")
    code, out, _ = run(tmp_path, "set", "--remind", "300000,200000", "--consider", "300000", "--auto-compact-window", "600000")
    assert code == 0 and out["remind"]["value"] == [200000, 300000] and out["auto_compact_window"]["value"] == 600000
    s = json.loads(settings.read_text(encoding="utf-8"))
    assert s["env"] == {"A": "1", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"} and "hooks" in s
    assert list((tmp_path / "claude").glob("settings.json.bak-cw-*"))
    code, out, _ = run(tmp_path, "set", "--auto-compact-window", "default")
    assert out["auto_compact_window"]["value"] is None and "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in json.loads(settings.read_text(encoding="utf-8"))["env"]
    code, _, err = run(tmp_path, "set", "--urgent", "100000")  # 比 consider 小
    assert code != 0 and "不能大於" in err
    code, _, err = run(tmp_path, "set", "--remind", "10")
    assert code != 0
