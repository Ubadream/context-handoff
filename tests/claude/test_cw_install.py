"""cw_install：舊路徑的 hook 原位換成倉庫路徑、不動別人的 hook、舊檔換轉接、doctor 前後對得上。"""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters" / "claude"))


def load(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    monkeypatch.setenv("CW_CLAUDE_HOME", str(home))
    sys.modules.pop("cw_install", None)
    cw = importlib.import_module("cw_install")
    (home / "hooks").mkdir(parents=True)
    (home / "plugins").mkdir()
    old = lambda name: f'"python" -X utf8 "{(home / "hooks" / name).as_posix()}"'
    settings = {
        "env": {"KEEP": "x"},
        "enabledPlugins": {"wife-compact@wife-local": True},
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": old("tool_archive.py"), "timeout": 5}]}],
            "PostToolUse": [
                {"matcher": "mcp__x", "hooks": [{"type": "command", "command": '"python" other.py'}]},
                {"hooks": [{"type": "command", "command": old("tool_archive.py"), "timeout": 5},
                           {"type": "command", "command": old("tool_archive.py"), "timeout": 5}]},
                {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": old("tool_output_trim.py"), "timeout": 10}]},
            ],
        },
    }
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    for name in cw.SHIMMED:
        (home / "hooks" / name).write_text("print('old full copy')\n", encoding="utf-8")
    (home / "plugins" / "known_marketplaces.json").write_text(
        json.dumps({"wife-local": {"source": {"source": "directory", "path": str(cw.HERE)}}}), encoding="utf-8")
    version = json.loads((cw.HERE / "wife-compact" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    (home / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"wife-compact@wife-local": [{"version": version}]}}), encoding="utf-8")
    return cw, home


def test_apply_then_doctor_clean(tmp_path, monkeypatch, capsys):
    cw, home = load(tmp_path, monkeypatch)
    assert cw.doctor() == 1
    cw.apply(dry=True)
    assert "old full copy" in (home / "hooks" / "tool_archive.py").read_text(encoding="utf-8")  # dry-run 不動

    cw.apply(dry=False)
    s = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert s["env"] == {"KEEP": "x", **cw.ENV}
    assert Path(s["env"]["CW_WORKBENCH"]).resolve() == Path(__file__).resolve().parents[2]
    post = s["hooks"]["PostToolUse"]
    assert post[0] == {"matcher": "mcp__x", "hooks": [{"type": "command", "command": '"python" other.py'}]}
    archive = [h for h in post[1]["hooks"] if "tool_archive.py" in h["command"]]
    assert len(archive) == 1 and str(cw.HERE.as_posix()) in archive[0]["command"]  # 原位換、重複的收掉
    trim = [g for g in post if any("tool_output_trim.py" in h["command"] for h in g["hooks"])]
    assert len(trim) == 1 and trim[0]["matcher"] == cw.TRIM_MATCHER  # 舊 matcher 原位改，不另加一組
    assert any("handoff_bind.py" in h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"])  # 沒有的補上
    assert list(home.glob("settings.json.bak-cw-*"))

    shim = (home / "hooks" / "context_watch.py").read_text(encoding="utf-8")
    assert "已搬到 context-workbench" in shim and (cw.HOOKS_DIR / "context_watch.py").as_posix() in shim
    assert list((home / "hooks").glob("context_watch.py.bak-cw-*"))

    capsys.readouterr()
    assert cw.doctor() == 0, capsys.readouterr().out
    before = (home / "settings.json").read_bytes()
    cw.apply(dry=False)  # 再跑一次不改
    assert (home / "settings.json").read_bytes() == before


def test_fresh_machine(tmp_path, monkeypatch):
    """全新電腦：沒有 settings.json、沒有市集；apply 不該先 remove，也不該因為沒設定檔就壞。"""
    home = tmp_path / ".claude"
    monkeypatch.setenv("CW_CLAUDE_HOME", str(home))
    sys.modules.pop("cw_install", None)
    cw = importlib.import_module("cw_install")
    calls = []
    monkeypatch.setattr(cw, "claude", lambda *argv: calls.append(argv))
    cw.apply(dry=False)
    s = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert s["env"] == cw.ENV and s["hooks"]["PostToolUse"]
    assert calls[0][:2] == ("marketplace", "add") and not any(c[1] == "remove" for c in calls)
