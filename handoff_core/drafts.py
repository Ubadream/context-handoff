"""Workbench candidates only. Never writes host transcripts, bindings or hooks."""
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import json
import re
import sqlite3

from . import state_paths

FIELDS = {"mode", "continuation", "handoff", "instructions"}
MAX_TEXT = 32768


class Conflict(ValueError):
    pass


def identity(host, session):
    if host not in ("codex", "claude") or not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}", session):
        raise ValueError("Invalid host/session identity")


def candidate(content):
    if not isinstance(content, dict) or set(content) != FIELDS:
        raise ValueError("Candidate requires mode, continuation, handoff and instructions")
    if content["mode"] not in ("A", "C") or content["continuation"] not in ("continue", "wait"):
        raise ValueError("Invalid mode or continuation")
    for field in ("handoff", "instructions"):
        if not isinstance(content[field], str) or len(content[field].encode("utf-8")) > MAX_TEXT:
            raise ValueError(f"{field} must be UTF-8 text within {MAX_TEXT} bytes")
    return dict(content)


def database():
    return state_paths.directory() / "drafts.sqlite3"


@contextmanager
def connection(write=False):
    path = database()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path if write else path.resolve().as_uri() + "?mode=ro", uri=not write, timeout=5)
    try:
        if write:
            db.execute("CREATE TABLE IF NOT EXISTS drafts (host TEXT, session TEXT, revision INTEGER, content TEXT, saved_at TEXT, PRIMARY KEY(host, session, revision))")
        yield db
    finally:
        db.close()


def latest(db, host, session):
    row = db.execute("SELECT revision, content, saved_at FROM drafts WHERE host=? AND session=? ORDER BY revision DESC LIMIT 1", (host, session)).fetchone()
    return {"host": host, "session": session, "revision": row[0] if row else 0,
            "content": json.loads(row[1]) if row else None, "saved_at": row[2] if row else None,
            "state": "draft" if row else "absent", "host_applied": False}


def load(host, session):
    identity(host, session)
    if not database().exists():
        return {"host": host, "session": session, "revision": 0, "content": None,
                "saved_at": None, "state": "absent", "host_applied": False}
    with connection() as db:
        return latest(db, host, session)


def save(host, session, content, expected_revision):
    identity(host, session)
    content = candidate(content)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a nonnegative integer")
    with connection(write=True) as db:
        # One transaction covers read/compare/append, including other CLI processes.
        db.execute("BEGIN IMMEDIATE")
        previous = latest(db, host, session)
        if previous["revision"] != expected_revision:
            raise Conflict(f"Draft changed: expected {expected_revision}, current {previous['revision']}; reload before merging")
        if previous["content"] != content:
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?)",
                       (host, session, expected_revision + 1, json.dumps(content, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        db.commit()
        return latest(db, host, session)


def preview(host, session, content, expected_revision):
    from . import read
    identity(host, session)
    content = candidate(content)
    previous = load(host, session)
    if type(expected_revision) is not int or previous["revision"] != expected_revision:
        raise Conflict("Draft changed; reload before preview")
    observed = read(host, "show", [session])
    if observed["session"] != session:
        raise ValueError("Use the complete resolved session ID for a candidate")
    # A bounded observation, not a promise that the host will still match later.
    basis = {key: observed.get(key) for key in ("host", "session", "bytes", "modified", "context_tokens", "last_compaction", "handoffs")}
    fingerprint = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    before = json.dumps(previous["content"] or {}, ensure_ascii=False, indent=2).splitlines()
    after = json.dumps(content, ensure_ascii=False, indent=2).splitlines()
    warnings = ["Local candidate preview only; no binding, scheduling, or host mutation.",
                "No runtime execution connection. Recheck the latest user message and host state before use."]
    if not content["handoff"].strip():
        warnings.append("Empty handoff: record active requirements, completed work, remaining work, and next entry point.")
    if host == "claude" and content["continuation"] == "continue":
        warnings.append("Claude continuation is not verified; continue only records intent.")
    return {"host": host, "session": session, "state": "preview", "host_applied": False,
            "draft_revision": previous["revision"], "source_revision": fingerprint,
            "observed_at": datetime.now(timezone.utc).isoformat(), "context_tokens": observed.get("context_tokens"),
            "handoff_sources": observed.get("handoffs"), "content": content,
            "diff": "\n".join(difflib.unified_diff(before, after, fromfile="saved-draft", tofile="candidate", lineterm="")),
            "warnings": warnings, "executable": False}

