"""交接分三塊（選用，預設關）：開關、上下文提醒那一行、交接檢查的格式軟檢查（合成資料）。"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

CLAUDE = Path(__file__).parents[2] / "adapters" / "claude"
HOOK = CLAUDE / "hooks" / "context_watch.py"
sys.path.insert(0, str(CLAUDE))


def env(tmp_path):
    return {**os.environ, "CW_STATE_ROOT": str(tmp_path / "state"), "CW_THRESHOLDS": str(tmp_path / "thresholds.json")}


def switch(tmp_path, value):
    subprocess.run([sys.executable, "-X", "utf8", str(CLAUDE / "cw_thresholds.py"), "set", "--handoff-sections", value],
                   check=True, capture_output=True, env=env(tmp_path))


def remind(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 420_000}}}) + "\n",
                          encoding="utf-8")
    (tmp_path / "state").mkdir(exist_ok=True)
    for f in (tmp_path / "state").rglob("*"):  # 每次重新算「跨過門檻」
        if f.is_file():
            f.unlink()
    data = {"transcript_path": str(transcript), "session_id": "s1", "hook_event_name": "UserPromptSubmit"}
    out = subprocess.run([sys.executable, "-X", "utf8", str(HOOK)], input=json.dumps(data), capture_output=True,
                         text=True, encoding="utf-8", env=env(tmp_path))
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def test_reminder_line_only_when_switched_on(tmp_path):
    import cw_paths
    assert "決策／情感／工作進度" not in remind(tmp_path)  # 預設關
    switch(tmp_path, "on")
    assert remind(tmp_path).endswith(cw_paths.HANDOFF_REMINDER)
    switch(tmp_path, "off")
    assert "決策／情感／工作進度" not in remind(tmp_path)


def test_switch_reads_thresholds_and_survives_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_THRESHOLDS", str(tmp_path / "thresholds.json"))
    import cw_paths
    importlib.reload(cw_paths)
    assert cw_paths.handoff_sections() is False
    (tmp_path / "thresholds.json").write_text("not json", encoding="utf-8")
    assert cw_paths.handoff_sections() is False
    (tmp_path / "thresholds.json").write_text('{"claude": {"handoff_sections": true}}', encoding="utf-8")
    assert cw_paths.handoff_sections() is True


def test_format_warnings_are_advisory():
    import cw_handoff_check as hc
    good = "## 決策\n- 10:05 老公說「先做 1、2」\n## 情感\n無\n## 工作進度\n- 做到測試"
    assert hc.format_warnings(good) == []
    bad = hc.format_warnings("## 決策\n- 做了一些事")
    assert any("情感／工作進度" in w for w in bad) and any("原話定位" in w for w in bad)
    assert hc.format_warnings("決策：ok\n情感：無\n工作進度：見 9bfa4859-40b4-435b-89b5-f0ea6f1baaf5") == []


def test_decision_items_without_source_warn():
    import cw_handoff_check as hc
    text = ("## 決策\n- 評測停在 1500 萬〔據 10-21 20:00「我估1500」〕\n- max turns 維持 12\n  - 子項不算\n"
            "- 改回原本的摘要〔取代「壓縮不是主因」〕〔據 9bfa4859§3〕\n## 情感\n無\n## 工作進度\n無\n")
    warnings = hc.format_warnings(text)
    assert len(warnings) == 1 and "1/3" in warnings[0]
    # 交接一段段往下接：只看最後一個決策塊
    assert hc.format_warnings("## 決策\n- 舊的沒來源\n## 情感\n無\n## 工作進度\n無\n" + text.replace("- max turns 維持 12\n", "")) == []
