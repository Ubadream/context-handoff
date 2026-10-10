# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Register the hooks-only Context Handoff with the Codex you already have. Nothing is built.

    python adapters/codex/install_codex_hooks.py doctor              # what is registered and trusted
    python adapters/codex/install_codex_hooks.py apply [--dry-run]   # add the hooks to config.toml
    python adapters/codex/install_codex_hooks.py trust               # trust exactly these hooks
    python adapters/codex/install_codex_hooks.py remove [--dry-run]  # take them out again

`apply` backs up $CODEX_HOME/config.toml, then writes one marked block and, if needed, turns on
`[features] hooks = true`. Codex runs a new hook only after it is trusted: review it in Codex, or run
`trust`, which asks Codex itself (app-server `hooks/list`) for the hash of each of these commands and
records only those. Restart Codex afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "hooks" / "cw_codex_hooks.py"
BEGIN, END = "# BEGIN context-handoff hooks", "# END context-handoff hooks"
EVENTS = [("PostToolUse", None, 10, None), ("UserPromptSubmit", None, 10, None),
          ("PreCompact", "auto|manual", 60, "Saving the compaction point"),
          ("SessionStart", "compact", 60, "Indexing the compacted transcript")]


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_path() -> Path:
    return codex_home() / "config.toml"


def commands() -> tuple[str, str]:
    args = [sys.executable, "-X", "utf8", "-B", str(HOOK)]
    return shlex.join(args), "& " + " ".join("'" + a.replace("'", "''") + "'" for a in args)


def block(text: str) -> str:
    posix, windows = commands()
    out = [BEGIN]
    for event, matcher, timeout, status in EVENTS:
        out.append(f"[[hooks.{event}]]")
        if matcher:
            out.append(f"matcher = {json.dumps(matcher)}")
        out += [f"[[hooks.{event}.hooks]]", 'type = "command"', f"command = {json.dumps(posix)}",
                f"command_windows = {json.dumps(windows)}", f"timeout = {timeout}"]
        if status:
            out.append(f"statusMessage = {json.dumps(status)}")
    if not re.search(r"(?m)^\[features\]\s*$", text):
        out += ["[features]", "hooks = true"]
    out.append(END)
    return "\n".join(out) + "\n"


def bounds(text: str):
    if BEGIN not in text and END not in text:
        return None
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(END) < text.index(BEGIN):
        raise SystemExit("config.toml has a damaged context-handoff block; fix it by hand first")
    start, stop = text.index(BEGIN), text.index(END) + len(END)
    return start, stop + (1 if text[stop:stop + 1] == "\n" else 0)


def without_block(text: str) -> str:
    span = bounds(text)
    return text if span is None else text[:span[0]] + text[span[1]:]


def features_on(text: str) -> str:
    """Set hooks = true inside an existing [features] table, leaving everything else as it is."""
    match = re.search(r"(?m)^\[features\]\s*$", text)
    if not match:
        return text
    body_end = re.search(r"(?m)^\[", text[match.end():])
    body_end = match.end() + body_end.start() if body_end else len(text)
    body = text[match.end():body_end]
    if re.search(r"(?m)^hooks\s*=", body):
        body = re.sub(r"(?m)^hooks\s*=.*$", "hooks = true", body)
    else:
        body = "\nhooks = true" + body
    return text[:match.end()] + body + text[body_end:]


def write(path: Path, old: str, new: str, dry: bool) -> None:
    tomllib.loads(new)  # refuse to write a config Codex could not read
    if dry:
        print(new if not old else f"--- would change {path} ({len(old)} -> {len(new)} characters)")
        return
    if path.exists():
        backup = path.with_name(f"config.toml.bak-context-handoff-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")
    print(f"wrote: {path}")


def apply(dry: bool) -> None:
    path = config_path()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    base = features_on(without_block(old))
    new = base.rstrip("\n") + ("\n\n" if base.strip() else "") + block(base)
    write(path, old, new, dry)
    if not dry:
        print("next: trust the hooks (review them in Codex, or run `trust`), then restart Codex")


def remove(dry: bool) -> None:
    path = config_path()
    old = path.read_text(encoding="utf-8")
    if bounds(old) is None:
        print("nothing to remove")
        return
    write(path, old, without_block(old), dry)
    print("note: [features] hooks was left as it is; other hooks may rely on it")


def find_codex(given: str | None) -> str:
    found = given or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if not found:
        raise SystemExit("codex not found; pass --codex PATH")
    return found


def listed_hooks(codex: str, cwd: Path) -> list[dict]:
    """Ask Codex which hooks it sees and how it hashes them; nothing is computed here."""
    proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=cwd,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    inbox: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [inbox.put(json.loads(l)) for l in proc.stdout if l.startswith("{")] + [inbox.put(None)],
                     daemon=True).start()

    def rpc(ident, method, params):
        proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while (message := inbox.get(timeout=60)) is not None:
            if message.get("id") == ident:
                if "error" in message:
                    raise SystemExit(f"codex {method}: {message['error']}")
                return message["result"]
        raise SystemExit("codex app-server exited early")

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "context-handoff-install", "version": "1"},
                              "capabilities": {"experimentalApi": True}})
        proc.stdin.write('{"method":"initialized","params":{}}\n')
        proc.stdin.flush()
        hooks = [h for entry in rpc(2, "hooks/list", {"cwds": [str(cwd)]})["data"] for h in entry["hooks"]]
        return [h for h in hooks if str(HOOK) in h.get("command", "")]
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def trust(codex: str) -> None:
    path = config_path()
    text = path.read_text(encoding="utf-8")
    span = bounds(text)
    if span is None:
        raise SystemExit("run apply first")
    hooks = listed_hooks(codex, Path.cwd())
    if len(hooks) != len(EVENTS):
        raise SystemExit(f"Codex lists {len(hooks)} of the {len(EVENTS)} hooks; is the config valid and hooks enabled?")
    entries = "".join(f"[hooks.state.{json.dumps(h['key'])}]\ntrusted_hash = {json.dumps(h['currentHash'])}\n"
                      for h in hooks)
    inner = re.sub(r"(?ms)^\[hooks\.state\.[^\n]*\]\ntrusted_hash = [^\n]*\n", "", text[span[0]:span[1]])
    inner = inner.replace(END, entries + END)
    write(path, text, text[:span[0]] + inner + text[span[1]:], dry=False)


def doctor(codex: str | None) -> None:
    path = config_path()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = tomllib.loads(text) if text else {}
    print(f"config:   {path}")
    print(f"block:    {'present' if bounds(text) else 'absent'}")
    print(f"features: hooks = {data.get('features', {}).get('hooks')}")
    print(f"script:   {HOOK} ({'ok' if HOOK.is_file() else 'MISSING'})")
    print(f"settings: {codex_home() / 'context-handoff' / 'hooks.json'} (optional)")
    if bounds(text):
        try:
            for h in listed_hooks(find_codex(codex), Path.cwd()):
                print(f"codex:    {h.get('eventName', h['key'])}  enabled={h.get('enabled')}  trust={h.get('trustStatus')}")
        except (SystemExit, OSError, queue.Empty) as exc:
            print(f"codex:    could not ask Codex ({exc}); pass --codex PATH to check trust")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["doctor", "apply", "trust", "remove"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--codex", help="codex executable used for trust/doctor (default: CODEX_CLI_PATH or PATH)")
    args = parser.parse_args()
    if args.command == "apply":
        apply(args.dry_run)
    elif args.command == "remove":
        remove(args.dry_run)
    elif args.command == "trust":
        trust(find_codex(args.codex))
    else:
        doctor(args.codex)


if __name__ == "__main__":
    main()
