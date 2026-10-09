# Verification

## Claude Code adapter (`adapters/claude/`)

- Daily use on the maintainer's Windows machine since 2026-09-25. From the session transcripts: 24 compactions since then, 0 automatic; median context 412K tokens before, 89K after (21 measurable). One compaction on 2026-09-30, from the plugin trace: 3 segments dropped (602,545 → 498,605 characters), tool round-trips to one line (579 → 30 messages, 498,605 → 38,102 characters).
- Large-output hook observed live on 2026-10-01: a 37,322-character Grep result kept at about 3,500 characters; a 32,798-character `Read` kept to its first 275 lines with correct numbering; WebFetch output saved to a file.
- Model step replaced by `CW_MODEL_CMD="claude -p --model haiku"` on 2026-10-09: 8 segments labelled in 83 s. Without any model, segmentation still runs with no labels (unit-tested).
- CI job `claude-adapter`: unit tests on synthetic data on Linux (installer on a fresh home, hook rewiring, output trimming shapes, model fallback, section hashes, retrieval). It does not run Claude Code.
- Not verified: installation on any machine other than the maintainer's; macOS; the idle reminder firing after a real idle period.

Codex, private runtime overlay (not this repository's gated patches): in ten long-running threads, 574 host-initiated compactions before 2026-09-28; 406 after, 397 of them scheduled by the agent through `wife_compact` (362 A, 35 C). Counted from `compacted` events in the rollout files, deduplicated across forks, with the preceding `wife_compact` call in the same window.

## Patch 0005: consecutive continue compactions

- Found in daily use of the private overlay on 2026-10-09: after one agent-scheduled compaction had removed the request it covered, the next prepare failed with "no current user request can be bound" until the user typed something. The public patches had the same logic.
- Fix: when the newest relevant item is this thread's `continue` notice, prepare and load use the request recorded in the current binding. A newer user message still wins; a `wait` notice is never carried over; a binding whose handoff ID already appears in that notice is treated as delivered. Local compaction now removes the covered request by message ID instead of removing the last user message.
- Windows 10 (maintainer's machine, Rust 1.95.0): 5 new unit tests and the new integration test `handoff_compact_second_continue_binds_without_new_message` pass, and the full CI test list passes again (handoff integration 15 passed, 1 ignored). The new integration test fails on the 0004 tree, at the second prepare.
- Linux: public CI on the commit that adds this patch.

## Patch 0004: prepared handoffs open

- Gate: `ensure_storage_available` opens on Unix and Windows and stays closed elsewhere; there is still no configuration or environment-variable bypass.
- Linux, public CI on the `codex-prepare-unix` branch: focused locked compilation, the handoff integration tests (14 passed, 1 ignored), `handoff_store`, `handoff_stream`, `handoff_binding`, `checkpoint_reminder`, `no_follow_store`, `atomic_store`, hooks and config tests, and the debug CLI build. The first branch run exposed two failures in the previously ignored tests that also occur on Linux; both were fixed: blank handoff text created the handoff directory before being rejected, and the deny-read test set its policy in a way that never started a turn.
- The deny-read test stays ignored in CI: Codex's filesystem sandbox helper aborts (SIGABRT) on the runner while loading AGENTS.md. An informational step ran it with `--ignored` next to upstream's own deny-read test `restricted_project_without_instructions_starts_successfully`; both failed the same way (exit 101).
- Windows 10 (maintainer's machine, Rust 1.95.0, `RUST_MIN_STACK=8388608`): the same list passes, including 23 handoff integration tests (1 ignored), 6 `atomic_store` tests (three shared with Unix, plus a real junction, replacement while the old binding is open, and refusing existing entries) and 10 `no_follow` tests (1 ignored, needs symlink privilege). Found on the way: resolving a whole `\??\X:` path with `OBJ_DONT_REPARSE` fails on the drive-letter link itself, so the existing Windows no-follow helpers could not open real files; local disk paths now walk components, UNC paths keep the original single-shot open that refuses `\\host\pipe\...`.
- Not verified: a long real Codex session on these public patches, macOS, Windows in CI, crash or power-loss durability.

## Handoff core and native Codex patches

Verified on 2026-10-08, Python 3.12.14 on Linux:

- `python -m unittest discover -s tests -v`: 11 passed, 0 failed, 0 skipped.
- `python -m handoff_core --help`: passed.
- Offline wheel build using pip with `--no-index --no-deps --no-build-isolation`: passed (setuptools 84.0.0).
- Install the built wheel into a fresh disposable virtual environment with no index/dependencies: passed.
- Installed `handoff-core --help`: passed.
- Uninstall package from that disposable environment: passed.

Synthetic tests cover absent reads, idempotent saves/exports, stale revisions, concurrent writers, changes during observation, export tampering, size limits, exact session identity, forged verification markers, explicit state selection, binding file containment, binding integrity, and unknown freshness/completion.

Not completed: native Codex build, live model compaction, native prepare/continue/wait integration, actual user-session access, Windows installation, or production runtime install/uninstall. Python tests do not establish native runtime correctness. This is an experimental source preview, not a production certification. Subsequent public CI results must be read for the exact published commit; only the Python success from the exact first public run below is established.

### Additive offline V2 observer

The unchanged V1 tests plus 14 additive offline observer tests passed together on Linux: 25 passed, 0 failures, 0 skipped. The additive module and tests use only synthetic captures. No native prepare/read invocation or live integration is claimed. The prior wheel build/install/uninstall evidence above predates the observer addition; this combined preview has source-test coverage, not a newly claimed packaging pass. See `ADAPTER-V2.md`.

### First public CI and corrective source

Public commit `566a627dde8f1d437fa9825b19beeeb9f15cc7b6`, [run 37783812942](https://github.com/Ubadream/context-handoff/actions/runs/37783812942): Python succeeded. Native locked compilation reached `codex-core` and failed with E0308 at `core/src/handoff_binding.rs:79`; the focused native test and CLI build steps were skipped. No native pass is inferred from successful dependency compilation.

The separate `0003` correction adds only `.to_path_buf()` to the storage-root expression. Python source remains unchanged, and 25 synthetic Python tests plus CLI help passed again in the new staging tree. Local focused Rust checks and the new source tree are recorded in `runtime-review-v2/COMPILE-FIX.json`. After the correction, [CI run 37788233583](https://github.com/Ubadream/context-handoff/actions/runs/37788233583) on commit df87cd4 passed: Python job, focused locked compilation, focused native tests (disabled cases remain ignored) and debug CLI build. That is still not a full upstream test suite, Windows, Session integration or post-compaction end-to-end evidence. The runtime gates and ignored test cases remain unchanged.

Local correction checks passed: the 31-test production-module harness (31 passed, 0 failed, 0 ignored); an isolated probe using the actual pinned `codex-utils-absolute-path` crate reproduced E0308 with the old expression and passed one test with the corrected expression; scoped formatting, diff checks, and fresh four-layer patch application matched the pinned new tree. These checks do not compile the full `codex-core` crate, exercise Session integration, or establish post-fix hosted CI success. The upstream nightly-only formatting-option warning remains disclosed; no nightly/bootstrap change was made.
