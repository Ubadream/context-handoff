# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Build the patched Codex from source and install it next to, not over, the user's Codex.

    python adapters/codex/install_codex.py plan         # what would happen; checks tools
    python adapters/codex/install_codex.py prepare      # fetch pinned upstream, verify and apply patches
    python adapters/codex/install_codex.py install      # prepare + build + package + install
    python adapters/codex/install_codex.py doctor       # check the installed copy
    python adapters/codex/install_codex.py uninstall    # remove the installed copy

Nothing in ~/.codex/config.toml is changed. The install gets its own directory under
$CODEX_HOME/runtimes and a launcher, `codex-handoff`, that runs it with checkpoint
reminders turned on for that invocation only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERIES_PATH = REPO / "runtime-review-v2" / "PATCH_SERIES.json"
MARKER = "context-handoff-install.json"
LAUNCHER = "codex-handoff"
WINDOWS = os.name == "nt"


def load_series() -> dict:
    return json.loads(SERIES_PATH.read_text(encoding="utf-8"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def install_dir(series: dict) -> Path:
    return codex_home() / "runtimes" / f"context-handoff-{series['upstream_version']}-{series['patched_tree'][:8]}"


def default_workdir() -> Path:
    # Codex's Windows build has deep paths; keep the checkout near the drive root there.
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "context-handoff" / "src"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "context-handoff" / "src"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(cmd: list[str], cwd: Path | None = None, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=True, **kw)


def git(workdir: Path, *args: str, capture: bool = False) -> str:
    base = ["git", "-C", str(workdir), "-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
    if capture:
        return subprocess.run(base + list(args), check=True, capture_output=True, text=True).stdout.strip()
    run(base + list(args))
    return ""


def verify_patches(series: dict) -> list[Path]:
    """Every patch must match its recorded hash byte for byte before anything is applied."""
    paths = []
    for patch in series["patches"]:
        path = REPO / patch["path"]
        data = path.read_bytes()
        if sha256(data) != patch["sha256"]:
            hint = " (it has CRLF line endings; re-clone with core.autocrlf=false)" if b"\r\n" in data else ""
            raise SystemExit(f"patch hash mismatch: {patch['path']}{hint}")
        paths.append(path)
    return paths


def patched_tree(workdir: Path) -> str:
    git(workdir, "add", "-A")
    return git(workdir, "write-tree", capture=True)


def prepare(series: dict, workdir: Path) -> None:
    patches = verify_patches(series)
    commit = series["upstream_commit"]
    if (workdir / ".git").exists():
        if git(workdir, "rev-parse", "HEAD", capture=True) != commit:
            raise SystemExit(f"{workdir} is a checkout of another commit; remove it or pass --workdir")
        if patched_tree(workdir) == series["patched_tree"]:
            print(f"source already prepared: {workdir}")
            return
        raise SystemExit(f"{workdir} has changes that are not exactly this patch series; remove it or pass --workdir")
    workdir.mkdir(parents=True, exist_ok=True)
    git(workdir, "init", "-q")
    git(workdir, "config", "core.autocrlf", "false")
    git(workdir, "config", "core.longpaths", "true")
    git(workdir, "fetch", "--depth", "1", series["upstream_repository"], commit)
    git(workdir, "checkout", "-q", "FETCH_HEAD")
    if git(workdir, "rev-parse", "HEAD", capture=True) != commit:
        raise SystemExit("fetched upstream is not the pinned commit")
    for path in patches:
        git(workdir, "apply", "--check", str(path))
        git(workdir, "apply", str(path))
    tree = patched_tree(workdir)
    if tree != series["patched_tree"]:
        raise SystemExit(f"patched tree {tree} differs from the reviewed {series['patched_tree']}")
    print(f"source prepared and verified: {workdir}")


def check_lock(series: dict, workdir: Path) -> None:
    lock = workdir / "codex-rs" / "Cargo.lock"
    if sha256(lock.read_bytes()) != series["cargo_lock_sha256"]:
        raise SystemExit("Cargo.lock changed during the build; dependencies are no longer the reviewed set")


def write_launcher(target: Path) -> Path:
    exe = target / "bin" / ("codex.exe" if WINDOWS else "codex")
    if WINDOWS:
        launcher = target / f"{LAUNCHER}.cmd"
        launcher.write_text(f'@echo off\r\n"{exe}" -c context_checkpoint_reminders=true %*\r\n', encoding="utf-8")
    else:
        launcher = target / LAUNCHER
        launcher.write_text(f'#!/bin/sh\nexec "{exe}" -c context_checkpoint_reminders=true "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def install(series: dict, workdir: Path, profile: str) -> None:
    prepare(series, workdir)
    target = install_dir(series)
    if target.exists():
        raise SystemExit(f"{target} already exists; run uninstall first")
    staging = target.with_name(target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    run(
        [sys.executable, "scripts/build_codex_package.py", "--package-dir", str(staging),
         "--cargo-profile", profile],
        cwd=workdir,
        # What upstream's justfile sets for `just assemble-codex-package`.
        env={**os.environ, "CODEX_REPO_ROOT": str(workdir),
             "RUST_MIN_STACK": os.environ.get("RUST_MIN_STACK", "8388608")},
    )
    check_lock(series, workdir)
    exe = staging / "bin" / ("codex.exe" if WINDOWS else "codex")
    marker = {
        "upstream_commit": series["upstream_commit"],
        "patched_tree": series["patched_tree"],
        "patches": series["patches"],
        "cargo_profile": profile,
        "codex_sha256": sha256(exe.read_bytes()),
    }
    (staging / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    staging.rename(target)
    launcher = write_launcher(target)
    print(f"\ninstalled: {target}\nrun it with: {launcher}\nyour existing Codex and its config are unchanged")


def read_marker(target: Path) -> dict | None:
    try:
        return json.loads((target / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def doctor(series: dict) -> int:
    target = install_dir(series)
    marker = read_marker(target)
    if marker is None:
        print(f"not installed: {target}")
        return 1
    problems = []
    if marker["patched_tree"] != series["patched_tree"]:
        problems.append("installed from a different patch series than this checkout")
    exe = target / "bin" / ("codex.exe" if WINDOWS else "codex")
    if not exe.exists() or sha256(exe.read_bytes()) != marker["codex_sha256"]:
        problems.append("codex executable is missing or changed since install")
    else:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
        print(f"{exe}: {out.stdout.strip() or out.stderr.strip()}")
        if out.returncode != 0:
            problems.append("codex --version failed")
    launcher = target / (f"{LAUNCHER}.cmd" if WINDOWS else LAUNCHER)
    if not launcher.exists():
        problems.append("launcher missing")
    for problem in problems:
        print("problem:", problem)
    if not problems:
        print(f"ok: {target}\nlauncher: {launcher}")
    return 1 if problems else 0


def uninstall(series: dict) -> int:
    target = install_dir(series)
    if read_marker(target) is None:
        print(f"nothing installed by this script at {target}")
        return 1
    shutil.rmtree(target)
    print(f"removed {target}")
    return 0


def plan(series: dict, workdir: Path, profile: str) -> int:
    verify_patches(series)
    print(f"upstream   {series['upstream_repository']} @ {series['upstream_commit']} ({series['upstream_version']})")
    print(f"patches    {len(series['patches'])}, hashes verified; reviewed tree {series['patched_tree']}")
    print(f"source     {workdir}")
    print(f"install to {install_dir(series)}  (profile {profile})")
    missing = [tool for tool in ("git", "cargo", "rustup") if shutil.which(tool) is None]
    for tool in missing:
        print(f"missing: {tool} (install Rust from https://rustup.rs; the pinned toolchain is fetched automatically)")
    if WINDOWS:
        print("note: Rust on Windows needs the Visual Studio C++ build tools")
    print("the first build takes a long time and tens of GB of disk")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "prepare", "install", "doctor", "uninstall"])
    parser.add_argument("--workdir", type=Path, default=default_workdir(), help="source checkout to create or reuse")
    parser.add_argument("--profile", default="release", help="cargo profile: release (default) or dev-small (faster build)")
    args = parser.parse_args(argv)
    series = load_series()
    if args.command == "plan":
        return plan(series, args.workdir, args.profile)
    if args.command == "prepare":
        prepare(series, args.workdir)
        return 0
    if args.command == "install":
        install(series, args.workdir, args.profile)
        return 0
    if args.command == "doctor":
        return doctor(series)
    return uninstall(series)


if __name__ == "__main__":
    raise SystemExit(main())
