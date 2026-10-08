# Verification

Verified on 2026-10-08, Python 3.12.14 on Linux:

- `python -m unittest discover -s tests -v`: 11 passed, 0 failed, 0 skipped.
- `python -m handoff_core --help`: passed.
- Offline wheel build using pip with `--no-index --no-deps --no-build-isolation`: passed (setuptools 84.0.0).
- Install the built wheel into a fresh disposable virtual environment with no index/dependencies: passed.
- Installed `handoff-core --help`: passed.
- Uninstall package from that disposable environment: passed.

Synthetic tests cover absent reads, idempotent saves/exports, stale revisions, concurrent writers, changes during observation, export tampering, size limits, exact session identity, forged verification markers, explicit state selection, binding file containment, binding integrity, and unknown freshness/completion.

Not run: native Codex build, live model compaction, native prepare/continue/wait integration, actual user-session access, Windows installation, or production runtime install/uninstall. Python tests do not establish native runtime correctness. This is an experimental source preview, not a production certification. Subsequent public CI results must be read for the exact published commit; no CI success is claimed in this snapshot.

## Additive offline V2 observer

The unchanged V1 tests plus 14 additive offline observer tests passed together on Linux: 25 passed, 0 failures, 0 skipped. The additive module and tests use only synthetic captures. No native prepare/read invocation or live integration is claimed. The prior wheel build/install/uninstall evidence above predates the observer addition; this combined preview has source-test coverage, not a newly claimed packaging pass. See `ADAPTER-V2.md`.
