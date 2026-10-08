# Verification

Verified on 2026-10-08, Python 3.12.14 on Linux:

- `python -m unittest discover -s tests -v`: 11 passed, 0 failed, 0 skipped.
- `python -m handoff_core --help`: passed.
- Offline wheel build using pip with `--no-index --no-deps --no-build-isolation`: passed (setuptools 84.0.0).
- Install the built wheel into a fresh disposable virtual environment with no index/dependencies: passed.
- Installed `handoff-core --help`: passed.
- Uninstall package from that disposable environment: passed.

Synthetic tests cover absent reads, idempotent saves/exports, stale revisions, concurrent writers, changes during observation, export tampering, size limits, exact session identity, forged verification markers, explicit state selection, binding file containment, binding integrity, and unknown freshness/completion.

Not completed: native Codex build, live model compaction, native prepare/continue/wait integration, actual user-session access, Windows installation, or production runtime install/uninstall. Python tests do not establish native runtime correctness. This is an experimental source preview, not a production certification. Subsequent public CI results must be read for the exact published commit; only the Python success from the exact first public run below is established.

## Additive offline V2 observer

The unchanged V1 tests plus 14 additive offline observer tests passed together on Linux: 25 passed, 0 failures, 0 skipped. The additive module and tests use only synthetic captures. No native prepare/read invocation or live integration is claimed. The prior wheel build/install/uninstall evidence above predates the observer addition; this combined preview has source-test coverage, not a newly claimed packaging pass. See `ADAPTER-V2.md`.

## First public CI and corrective source

Public commit `566a627dde8f1d437fa9825b19beeeb9f15cc7b6`, [run 37783812942](https://github.com/Ubadream/context-handoff/actions/runs/37783812942): Python succeeded. Native locked compilation reached `codex-core` and failed with E0308 at `core/src/handoff_binding.rs:79`; the focused native test and CLI build steps were skipped. No native pass is inferred from successful dependency compilation.

The separate `0003` correction adds only `.to_path_buf()` to the storage-root expression. Python source remains unchanged, and 25 synthetic Python tests plus CLI help passed again in the new staging tree. Local focused Rust checks and the new source tree are recorded in `runtime-review-v2/COMPILE-FIX.json`. After the correction, [CI run 37788233583](https://github.com/Ubadream/context-handoff/actions/runs/37788233583) on commit df87cd4 passed: Python job, focused locked compilation, focused native tests (disabled cases remain ignored) and debug CLI build. That is still not a full upstream test suite, Windows, Session integration or post-compaction end-to-end evidence. The runtime gates and ignored test cases remain unchanged.

Local correction checks passed: the 31-test production-module harness (31 passed, 0 failed, 0 ignored); an isolated probe using the actual pinned `codex-utils-absolute-path` crate reproduced E0308 with the old expression and passed one test with the corrected expression; scoped formatting, diff checks, and fresh four-layer patch application matched the pinned new tree. These checks do not compile the full `codex-core` crate, exercise Session integration, or establish post-fix hosted CI success. The upstream nightly-only formatting-option warning remains disclosed; no nightly/bootstrap change was made.
