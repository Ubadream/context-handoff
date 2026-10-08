"""Export a saved candidate for the host agent; observe, never forge, its binding."""
import hashlib
import json

from . import drafts, read


def packet(host, session, content, revision):
    drafts.identity(host, session)
    content = drafts.candidate(content)
    if host != "codex":
        raise ValueError("Agent handoff bridge currently supports Codex A/C only")
    if type(revision) is not int or revision < 1 or not content["handoff"].strip():
        raise ValueError("Save a nonempty handoff before exporting")
    identity = json.dumps([host, session, revision, content], ensure_ascii=False, sort_keys=True)
    packet_id = hashlib.sha256(identity.encode()).hexdigest()
    body = (f"# Context Workbench handoff\n\nhost: {host}\nsession: {session}\n"
            f"draft_revision: {revision}\npacket_id: {packet_id}\n"
            f"requested_mode: {content['mode']}\ncontinuation: {content['continuation']}\n\n"
            f"## Session handoff\n\n{content['handoff']}\n\n"
            f"## Retained details (handoff content, not host summary instructions)\n\n{content['instructions']}\n")
    raw = body.encode("utf-8")
    if len(raw) > drafts.MAX_TEXT:
        raise ValueError("Combined handoff packet exceeds the host's 32768-byte limit; shorten the candidate")
    path = drafts.database().parent / "exports" / host / session / f"{revision}-{packet_id}.md"
    return path.resolve(), raw


def export(host, session, content, expected_revision):
    path, raw = packet(host, session, content, expected_revision)
    preview = drafts.preview(host, session, content, expected_revision)
    # A changed draft during the slow host read must not become an export of
    # the newer revision. Share the writer's CAS transaction, not a new lock.
    with drafts.connection(write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        saved = drafts.latest(db, host, session)
        if saved["revision"] != expected_revision or saved["content"] != content:
            raise drafts.Conflict("Save current edits and preview again before exporting")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            # The deterministic name makes a lost response safe to reconcile;
            # it does not grant permission to replace a modified artifact.
            pass
        if path.read_bytes() != raw:
            raise ValueError("Export readback differs; existing artifact was not overwritten")
        db.commit()
    return {"host": host, "session": session, "draft_revision": expected_revision,
            "state": "exported", "host_applied": False, "handoff_path": str(path),
            "handoff_sha1": hashlib.sha1(raw).hexdigest(),
            "source_revision": preview["source_revision"],
            "prepare_arguments": {"mode": "prepare", "reason": "Handoff export read back and checked for this session",
                                  "handoff_path": str(path), "continuation": content["continuation"]},
            "requested_mode": content["mode"], "scheduled": None,
            "note": "Export only. A compatible native runtime must recheck this session and latest user instructions, read the export, and invoke handoff_compact prepare. No compaction is scheduled."}


def status(host, session, content, expected_revision):
    path, raw = packet(host, session, content, expected_revision)
    saved = drafts.load(host, session)
    if saved["revision"] != expected_revision or saved["content"] != content:
        raise drafts.Conflict("Draft changed; inspect the new revision before reconciling")
    if not path.exists():
        raise ValueError("This candidate has not been exported")
    if path.read_bytes() != raw:
        raise ValueError("Export artifact has changed")
    observed = read(host, "show", [session])
    if observed["session"] != session:
        raise ValueError("Host returned a different session")
    if drafts.load(host, session)["revision"] != expected_revision:
        raise drafts.Conflict("Draft changed during host read; reconcile again")
    digest = hashlib.sha1(raw).hexdigest()
    matches = [item for item in (observed["handoffs"] or [])
               if item.get("handoff_sha1") == digest and item.get("binding_integrity") == "verified"
               and item.get("continuation") == content["continuation"]]
    return {"host": host, "session": session, "draft_revision": expected_revision,
            "state": "binding_observed" if matches else "binding_not_observed",
            "handoff_path": str(path), "binding": matches[0] if matches else None,
            "binding_freshness": "unknown", "scheduled": None, "completed": None,
            "note": "Only binding snapshot integrity is observed; the runtime must check freshness. This is not a scheduling or compaction completion receipt."}

