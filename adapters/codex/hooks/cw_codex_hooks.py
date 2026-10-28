# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Hooks-only Context Handoff for stock Codex: no rebuild, survives Codex upgrades.

One script, registered for four Codex hook events by install_codex_hooks.py:

    PostToolUse, UserPromptSubmit   context-size reminders, once per threshold per compaction
    PreCompact                      remember where the transcript ended and which handoff exists,
                                    and keep a copy of that handoff (handoff-snapshots/, by SHA-256)
    SessionStart (source=compact)   point the agent at its handoff, say the last request is not new,
                                    and index the compacted part of the transcript for later lookup

"Its handoff" is the HANDOFF*.md this thread itself last wrote (see thread_handoffs.py); if it wrote
none, the newest HANDOFF*.md in the working directory.

Letting the agent choose the moment: Codex 0.164 and later have a native `new_context` tool behind
`[features] token_budget = true` (marked under development by Codex). It starts a fresh context
window without a summary and the same turn carries on; PreCompact and SessionStart(compact) still
fire, so the handoff pointer above arrives in the new window. When the flag is on, reminders tell
the agent to save its handoff and call `new_context` itself; otherwise they tell it to ask the user
to run /compact. What hooks still cannot do, and the patched build can: bind the handoff to the
request it covers so the host structurally never replays that request; here the post-compaction
note is an instruction the model can still ignore.

Reading the compacted originals back:

    python cw_codex_hooks.py list <ID>
    python cw_codex_hooks.py get <ID>§<N> [--max CHARS]

Configuration (optional), $CODEX_HOME/context-handoff/hooks.json:

    {"thresholds": [0.6, 0.8], "handoff_globs": ["HANDOFF*.md", "handoff*.md"]}

A threshold below 1 is a fraction of the model's context window; 1 or more is a token count.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import thread_handoffs  # noqa: E402

DEFAULTS = {"thresholds": [0.6, 0.8], "handoff_globs": ["HANDOFF*.md", "handoff*.md"]}
TAG = "[context-handoff]"
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# Context the host injects as user-role messages; not something the user typed.
INJECTED = ("<environment_context", "<user_instructions", "<permissions", "<turn_aborted",
            "# AGENTS.md instructions", "<user_shell_command", "<subagent")
TAIL_BYTES = 1 << 20


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def state_dir() -> Path:
    return codex_home() / "context-handoff"


def settings() -> dict:
    path = state_dir() / "hooks.json"
    loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **{k: v for k, v in loaded.items() if k in DEFAULTS}}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1)
        temp = Path(stream.name)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def plain_path(value: str) -> Path:
    # The desktop app passes extended-length Windows paths; the CLI usually does not.
    text = str(Path(value).resolve())
    if os.name == "nt":
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\") and re.match(r"[a-zA-Z]:\\", text[4:]):
            text = text[4:]
    return Path(text)


def transcript(event: dict) -> Path:
    """The event's rollout file, after checking it belongs to the event's session."""
    sid = event.get("session_id", "")
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise ValueError("event has no complete session_id")
    path = plain_path(event["transcript_path"])
    with path.open("rb") as stream:
        first = json.loads(stream.readline())
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != sid:
        raise ValueError("transcript does not belong to this session")
    return path


def session_state(sid: str) -> tuple[Path, dict]:
    path = state_dir() / "sessions" / f"{sid}.json"
    return path, read_json(path, {})


# ---- reading the rollout ----------------------------------------------------------------------

def records(path: Path, start: int = 0, end: int | None = None):
    """Yield (offset, raw line, parsed record) for complete JSON lines in [start, end)."""
    with path.open("rb") as stream:
        stream.seek(start)
        offset = start
        for raw in stream:
            if end is not None and offset >= end:
                return
            here, offset = offset, offset + len(raw)
            if not raw.endswith(b"\n"):
                return  # still being written
            try:
                yield here, raw, json.loads(raw)
            except ValueError:
                continue


def user_text(record: dict) -> str | None:
    """Text the user typed, or None for any other record (including injected context)."""
    payload = record.get("payload") or {}
    if record.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return None
    text = "\n".join(part.get("text", "") for part in payload.get("content") or []
                     if part.get("type") in ("input_text", "text")).strip()
    if not text or text.startswith(INJECTED):
        return None
    return text


def context_size(path: Path) -> tuple[int | None, int | None]:
    """(tokens in the last model request, model context window) from the rollout's tail."""
    size = path.stat().st_size
    start = max(0, size - TAIL_BYTES)
    with path.open("rb") as stream:
        stream.seek(start)
        lines = stream.read().splitlines()
    for raw in reversed(lines[1:] if start else lines):
        if b'"token_count"' not in raw:
            continue
        try:
            info = (json.loads(raw).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        last = info.get("last_token_usage") or {}
        used = last.get("total_tokens") or last.get("input_tokens")
        if used:
            return int(used), info.get("model_context_window")
    return None, None


def newest_handoff(cwd: str | None, globs: list[str]) -> Path | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    found = {p for pattern in globs for p in Path(cwd).glob(pattern) if p.is_file()}
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def find_handoff(event: dict, path: Path) -> Path | None:
    """The handoff this thread last wrote; failing that, the newest one in the working directory."""
    try:
        written = thread_handoffs.written_handoffs(path, event.get("session_id", ""))
    except OSError:
        written = []
    return Path(written[0]) if written else newest_handoff(event.get("cwd"), settings()["handoff_globs"])


def native_new_context() -> bool:
    """Whether Codex offers the agent its own `new_context` tool ([features] token_budget = true)."""
    try:
        with (codex_home() / "config.toml").open("rb") as stream:
            return tomllib.load(stream).get("features", {}).get("token_budget") is True
    except (OSError, tomllib.TOMLDecodeError):
        return False


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


# ---- events -----------------------------------------------------------------------------------

def remind(event: dict) -> dict:
    path = transcript(event)
    used, window = context_size(path)
    if not used:
        return {}
    marks = []
    for value in settings()["thresholds"]:
        if isinstance(value, (int, float)) and 0 < value < 1 and window:
            marks.append(int(value * window))
        elif isinstance(value, (int, float)) and value >= 1:
            marks.append(int(value))
    marks.sort()
    state_path, state = session_state(event["session_id"])
    fired = set(state.get("fired", []))
    due = [m for m in marks if used >= m and m not in fired]
    if not due:
        return {}
    state["fired"] = sorted(fired | set(due))
    write_json(state_path, state)
    handoff = find_handoff(event, path)
    where = f"your handoff file ({handoff})" if handoff else "a handoff file (HANDOFF.md in the working directory)"
    share = f" ({used * 100 // window}% of {window})" if window else ""
    if max(due) == marks[-1] and native_new_context():
        text = (f"{TAG} Context is at {used} tokens{share}. At the next natural break (not in the middle of a "
                f"tool chain), save {where}: the user's standing requests, what is done, what is unfinished, "
                "operations in flight, and where to resume. Read it back, then call the `new_context` tool "
                "yourself. It starts a fresh window without a summary, so anything not in the handoff is left "
                "behind; you continue in the same turn and are pointed back to the handoff.")
    elif max(due) == marks[-1]:
        text = (f"{TAG} Context is at {used} tokens{share}. Save {where} now: the user's standing requests, "
                "what is done, what is unfinished, operations in flight, and where to resume. Then tell the "
                "user this is a good point to run /compact, before the host compacts on its own in the middle "
                "of work. Do not start large new work first.")
    else:
        text = (f"{TAG} Context is at {used} tokens{share}. At the next natural break, update {where}: the "
                "user's standing requests, what is done, what is unfinished, operations in flight, and where "
                "to resume.")
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext": text}}


def before_compact(event: dict) -> dict:
    path = transcript(event)
    end = path.stat().st_size
    state_path, state = session_state(event["session_id"])
    last_user = None
    for _, raw, record in records(path, state.get("boundary", 0), end):
        if b'"user"' in raw and user_text(record) is not None:
            last_user = record.get("timestamp")
    handoff = find_handoff(event, path)
    try:
        digest = snapshot_handoff(handoff, event) if handoff else None
    except OSError as exc:  # a failed copy must not lose the compaction bookkeeping below
        print(f"context-handoff: handoff snapshot skipped: {exc}", file=sys.stderr)
        digest = None
    state["pending"] = {"transcript": str(path), "end": end, "last_user": last_user or state.get("last_user"),
                        "handoff": str(handoff) if handoff else None,
                        "handoff_mtime": handoff.stat().st_mtime if handoff else None,
                        "handoff_sha256": digest}
    write_json(state_path, state)
    return {}


def snapshot_handoff(handoff: Path, event: dict) -> str:
    """Keep the handoff as it was at compaction time.

    Handoff files are overwritten in place, so the version a compaction relied on is otherwise
    lost. Content goes to handoff-snapshots/blobs/<sha256>.md once per distinct content; every
    compaction appends a line to handoff-snapshots/index.jsonl.
    """
    data = handoff.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    root = state_dir() / "handoff-snapshots"
    blob = root / "blobs" / f"{digest}.md"
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=blob.parent, delete=False) as stream:
            stream.write(data)
        os.replace(stream.name, blob)
    row = {"session": event.get("session_id"), "compacted_at": iso(datetime.now().timestamp()),
           "trigger": event.get("trigger"), "path": str(handoff), "sha256": digest, "bytes": len(data),
           "mtime": iso(handoff.stat().st_mtime)}
    with (root / "index.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return digest


def index_sections(path: Path, start: int, end: int) -> list[dict]:
    """Split [start, end) at each message the user typed; hash each byte range."""
    cuts, previews = [start], {}
    for offset, raw, record in records(path, start, end):
        if b'"user"' in raw and (text := user_text(record)) is not None:
            if offset > start:
                cuts.append(offset)
            previews[offset if offset > start else start] = (text[:160], record.get("timestamp"))
    cuts.append(end)
    sections = []
    with path.open("rb") as stream:
        for a, b in zip(cuts, cuts[1:]):
            if b <= a:
                continue
            stream.seek(a)
            digest = hashlib.sha256(stream.read(b - a)).hexdigest()
            preview, ts = previews.get(a, ("(host setup or work before the first user message here)", None))
            sections.append({"n": len(sections) + 1, "start": a, "end": b, "sha256": digest,
                             "user": preview, "timestamp": ts})
    return sections


def after_compact(event: dict) -> dict:
    path = transcript(event)
    sid = event["session_id"]
    state_path, state = session_state(sid)
    pending = state.pop("pending", None)
    state["fired"] = []  # reminders start over in the new context
    lines = [f"{TAG} The conversation was just compacted or moved to a new context window."]
    if not pending or pending.get("transcript") != str(path):
        write_json(state_path, state)
        lines.append("Read your handoff file before continuing. The user's last request was already "
                     "being worked on; it is not a new request. Do not redo finished work.")
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": " ".join(lines)}}
    start, end = state.get("boundary", 0), pending["end"]
    count = state.get("compactions", 0) + 1
    ident = f"{sid[:8]}-{count}"
    sections = index_sections(path, start, end)
    write_json(state_dir() / "originals" / f"{ident}.json",
               {"id": ident, "session": sid, "transcript": str(path), "created": iso(datetime.now().timestamp()),
                "sections": sections})
    state.update(boundary=end, compactions=count, last_user=pending.get("last_user"))
    write_json(state_path, state)

    handoff, mtime, last_user = pending.get("handoff"), pending.get("handoff_mtime"), parse_ts(pending.get("last_user"))
    if not handoff:
        lines.append("No handoff file was found (none written by this thread, none in the working directory); rebuild your bearings from the "
                     "summary (if any) and the originals below before acting.")
    elif last_user and mtime < last_user:
        lines.append(f"Your handoff is {handoff}, but it was last saved before the user's latest message, so it "
                     "may miss that request. Read it, then check the originals below for what came after.")
    else:
        lines.append(f"Read your handoff first: {handoff} (saved after the user's latest message).")
    if pending.get("handoff_sha256"):
        copy = state_dir() / "handoff-snapshots" / "blobs" / f"{pending['handoff_sha256']}.md"
        lines.append(f"The handoff as it was at this compaction is kept at {copy}.")
    lines.append("The user's last request was already being worked on; it is not a new request. Continue "
                 "what the handoff lists as unfinished and do not redo finished work.")
    script = Path(__file__).resolve()
    lines.append(f"The compacted part is indexed as {ident}: {len(sections)} sections, split at each user "
                 f"message. When you need exact earlier wording, run `python \"{script}\" list {ident}` and "
                 f"`python \"{script}\" get {ident}§N`.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": " ".join(lines)}}


def gap_response(event: dict) -> dict:
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "claude" / "hooks" / "compact_gap.py"
    spec = importlib.util.spec_from_file_location("context_handoff_gap", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module.receive(event)
    except (OSError, ValueError, TypeError, KeyError):
        # The existing handoff and originals index remain available if gap recovery fails.
        print("context-handoff: gap recovery unavailable", file=sys.stderr)
        return {}


def receive(event: dict) -> dict:
    if event.get("agent_id") or event.get("subagent"):
        return {}
    name = event.get("hook_event_name")
    if name == "PreCompact":
        return before_compact(event)
    result = {}
    if name in ("PostToolUse", "UserPromptSubmit"):
        result = remind(event)
    elif name == "SessionStart" and event.get("source") == "compact":
        result = after_compact(event)
    if name == "UserPromptSubmit" or name == "SessionStart" and event.get("source") == "compact":
        extra = gap_response(event)
        if extra.get("hookSpecificOutput"):
            context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
            extra["hookSpecificOutput"]["additionalContext"] = context + "\n" + extra["hookSpecificOutput"]["additionalContext"]
        result.update(extra)
    return result


# ---- reading originals back -------------------------------------------------------------------

def manifest(ident: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{8}-\d+", ident):
        raise ValueError(f"not an originals ID: {ident}")
    return json.loads((state_dir() / "originals" / f"{ident}.json").read_text(encoding="utf-8"))


def render(record: dict) -> str | None:
    payload = record.get("payload") or {}
    kind = payload.get("type")
    if record.get("type") == "compacted":
        if message := str(payload.get("message") or ""):
            return "[summary from an earlier compaction]\n" + message[:2000]
        # Remote compaction: the summary is an encrypted item only the model provider can read.
        kept = sum(1 for item in payload.get("replacement_history") or [] if item.get("type") == "message")
        return (f"[an earlier compaction happened here; the host compacted remotely and keeps its summary "
                f"encrypted, so it cannot be shown. {kept} messages were carried over verbatim.]")
    if record.get("type") != "response_item":
        return None
    if kind == "message" and payload.get("role") in ("user", "assistant"):
        if payload["role"] == "user" and user_text(record) is None:
            return None
        text = "\n".join(p.get("text", "") for p in payload.get("content") or [] if "text" in p)
        return f"{payload['role']}: {text}"
    if kind in ("function_call", "custom_tool_call"):
        return f"tool call {payload.get('name')}: {str(payload.get('arguments') or payload.get('input'))[:400]}"
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        output = output.get("content", output) if isinstance(output, dict) else output
        return f"tool output: {str(output)[:600]}"
    return None


def get(reference: str, limit: int) -> str:
    ident, _, number = reference.partition("§")
    data = manifest(ident)
    section = next((s for s in data["sections"] if str(s["n"]) == number), None)
    if section is None:
        raise ValueError(f"{ident} has sections 1..{len(data['sections'])}")
    path = Path(data["transcript"])
    with path.open("rb") as stream:
        stream.seek(section["start"])
        if hashlib.sha256(stream.read(section["end"] - section["start"])).hexdigest() != section["sha256"]:
            raise ValueError("transcript bytes changed since indexing; refusing to show them as the original")
    parts = [text for _, _, record in records(path, section["start"], section["end"]) if (text := render(record))]
    body = "\n\n".join(parts)
    note = f"\n\n[cut at {limit} of {len(body)} characters; pass --max to see more]" if len(body) > limit else ""
    return f"{ident}§{number} (verified, {section['timestamp'] or 'no timestamp'})\n\n{body[:limit]}{note}"


def main(argv: list[str]) -> int:
    if argv[:1] == ["list"] and len(argv) == 2:
        for s in manifest(argv[1])["sections"]:
            print(f"§{s['n']}  {s['timestamp'] or '-'}  {s['end'] - s['start']} bytes  {s['user'][:100]!r}")
        return 0
    if argv[:1] == ["get"] and len(argv) in (2, 4):
        limit = int(argv[3]) if len(argv) == 4 and argv[2] == "--max" else 8000
        print(get(argv[1], limit))
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
        if log := os.environ.get("CONTEXT_HANDOFF_HOOK_LOG"):  # troubleshooting: what Codex sent
            with open(log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        result = receive(event)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never stop the host over a reminder or an index; say why on stderr and stay out of the way.
        print(f"context-handoff hook skipped: {exc}", file=sys.stderr)
        result = {}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
