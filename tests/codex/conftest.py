"""Gap hooks in tests must never read or write the developer's live state."""
import json

import pytest


@pytest.fixture(autouse=True)
def isolated_gap_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_STATE_ROOT", str(tmp_path / "gap-state"))
    config = tmp_path / "gap-thresholds.json"
    config.write_text(json.dumps({"claude": {"compact_gap": {"provider": "none"}}}), encoding="utf-8")
    monkeypatch.setenv("CW_THRESHOLDS", str(config))
