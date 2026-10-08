"""cw_model：CW_MODEL_CMD 優先；都沒有就丟 ModelUnavailable，分段退成只切不分類。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "adapters" / "claude"))
import cw_model  # noqa: E402
import cw_segments  # noqa: E402


def test_command_gets_prompt_on_stdin(monkeypatch):
    monkeypatch.setenv("CW_MODEL_CMD", f'"{sys.executable}" -c "import sys; print(sys.stdin.read().upper())"')
    text, secs = cw_model.ask("hello 老婆")
    assert text.strip() == "HELLO 老婆" and secs >= 0
    assert cw_model.name().endswith('upper())"')


def test_no_model_falls_back_to_rules(monkeypatch, tmp_path):
    monkeypatch.delenv("CW_MODEL_CMD", raising=False)
    monkeypatch.setattr(cw_model, "L2", tmp_path / "missing.py")
    assert cw_model.name() is None
    segs = [{"seq": 1, "id": "s#1", "prompt": "p", "opened_by": "老公", "tools": [], "said": "p", "text": "t",
             "text_chars": 1, "result_chars": 0, "start": None}]
    monkeypatch.setattr(cw_segments, "excerpt", lambda s: "x")
    assert cw_segments.classify(segs) is None and "label" not in segs[0]
