# Release boundary

Release by explicit file allowlist, never by copying a repository root.

Included implementation: `handoff_core/{__init__,__main__,state_paths,drafts,handoff,observation_v2}.py`, `tests/test_core.py`, `tests/test_observation_v2.py`, and `pyproject.toml`. Included review documentation: README, source manifest, exclusions, license review, and verification report. Build output and Python caches are excluded.

Excluded: all original Git history, production state and databases, transcripts and session trees, raw handoff packets, receipts, private sample identifiers, absolute private paths, credentials, prompt archives, donor installers, original private README/docs, desktop/web UI, runtime discovery, tool-original projection, external model routing, retired tools, broad source recipes, CI secrets, binaries, and model/service integrations.

The separately scoped `runtime-review/` and `runtime-review-v2/` distributions contains only the public upstream license/notice, experimental patch, minimal lockfile prerequisite, and public verification documentation. Public CI contains no private fetches, credentials, cache publishing, artifact upload, or deployment. Do not recursively publish a parent workspace. `PUBLIC_MANIFEST.json` enumerates this release file set and hashes.

## Claude Code adapter (added 2026-10-09)

`adapters/claude/` and `tests/claude/` are generated from the private source repository by an explicit allowlist exporter that refuses to write if it finds account names, email addresses or absolute user paths. Not included: `compact_gap` (needs a private local ranking model and recall tool) and `cw_advice` (needs the private panel core). The installer skips hooks whose scripts are absent. No transcripts, state, tool archives or settings files are included.
