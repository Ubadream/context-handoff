"""install_codex: the patch series matches what CI verifies; installs stay in their own directory."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "adapters" / "codex"))


def load(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    sys.modules.pop("install_codex", None)
    return importlib.import_module("install_codex")


def test_series_is_what_ci_applies(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    series = ic.load_series()
    workflow = (REPO / ".github" / "workflows" / "source-preview.yml").read_text(encoding="utf-8")
    assert f"ref: {series['upstream_commit']}" in workflow
    assert series["patched_tree"] in workflow
    assert series["cargo_lock_sha256"] in workflow
    applied = [line.split("../", 1)[1] for line in workflow.splitlines()
               if "git -C upstream apply ../" in line]
    assert applied == [p["path"] for p in series["patches"]]
    for patch in series["patches"]:
        assert f"echo '{patch['sha256']}  {patch['path']}'" in workflow


def test_patch_files_match_their_hashes(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    assert len(ic.verify_patches(ic.load_series())) == 6


def test_tampered_patch_is_refused(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    series = ic.load_series()
    copy = tmp_path / "0001.patch"
    copy.write_bytes((REPO / series["patches"][0]["path"]).read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(ic, "REPO", tmp_path)
    series["patches"] = [dict(series["patches"][0], path="0001.patch")]
    with pytest.raises(SystemExit, match="CRLF"):
        ic.verify_patches(series)


def test_install_dir_is_separate_and_launcher_turns_reminders_on(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    target = ic.install_dir(ic.load_series())
    assert target.parent == tmp_path / "codex-home" / "runtimes"
    target.mkdir(parents=True)
    launcher = ic.write_launcher(target)
    text = launcher.read_text(encoding="utf-8")
    assert "-c context_checkpoint_reminders=true" in text
    assert str(target / "bin") in text


def test_doctor_and_uninstall_only_touch_marked_installs(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    series = ic.load_series()
    target = ic.install_dir(series)
    assert ic.doctor(series) == 1
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("not ours")
    assert ic.uninstall(series) == 1
    assert (target / "keep.txt").exists()
    (target / ic.MARKER).write_text('{"patched_tree": "x"}')
    assert ic.uninstall(series) == 0
    assert not target.exists()


def test_prepare_refuses_a_checkout_of_another_commit(tmp_path, monkeypatch):
    ic = load(tmp_path, monkeypatch)
    work = tmp_path / "src"
    work.mkdir()
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["init", "-q"], check=True)
    (work / "f").write_text("x")
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    with pytest.raises(SystemExit, match="another commit"):
        ic.prepare(ic.load_series(), work)
