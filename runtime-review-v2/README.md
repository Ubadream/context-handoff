# Atomic store and thread-scoped readback: review delta

Status: gated, experimental, and not ready for deployment. The previous Gate B artifact is unchanged. Prepared handoffs, binding application, and handoff readback remain disabled before runtime storage access. This delta does not unlock them.

## Apply in this exact order

Base: OpenAI Codex `rust-v0.159.2`, commit `ff6aec96948b70d94983af2641a6b67c94faeff5`.

1. Existing `0001-handoff-compact-checkpoints.patch`, SHA-256 `ed28854b7ee5e048620ce60ebaaea17cd58750350852bd4e5caad29c24b61e54`.
2. Separate, reviewed `0000-sync-workspace-release-lockfile.patch`, SHA-256 `1b99ed3fc93196fac8535ab01b0f9d3dfedb9593eb071613e83c4568c9fc2d0d`. It only synchronizes 159 local workspace versions from 0.0.0 to 0.159.2; external dependency identities, pins, checksums, and dependency lists are unchanged.
3. This bundle's `patches/0002-atomic-store-and-thread-readback.patch`.

The exact input tree after steps 1 and 2 is `705a0fb809b2a610413b01d5645eedad5d9d192f`. The resulting tree after step 3 is `a28cfbae42e0dcfe1533a410adc001ff3f9b2f5e`. All three patches were checked and applied to a fresh full checkout; its changed files match the implementation byte-for-byte. The delta contains no Cargo.lock change.

## Changes

- Unix atomic index publication uses one held no-follow directory descriptor, exclusive private temporary creation, file synchronization, same-directory rename, and directory synchronization. It never opens/truncates an existing destination. Pre-rename failures preserve the previous binding. Post-rename synchronization failures explicitly report that the new entry is visible but durability is unconfirmed.
- Windows atomic directory publication returns Unsupported. No new platform-unsafe FFI code is introduced.
- Snapshot lookup accepts an opaque UUID and derives storage from the authenticated current thread. It accepts no storage path or caller-selected thread. Newer bindings supersede older IDs.
- Readback separates historical data retrieval from the latest-request freshness required for compaction. It verifies the current binding and content digest on every read.
- Pages contain at most 4096 raw UTF-8 bytes. JSON escaping can produce a smaller page: the serialized page is capped at 7 KiB and the complete serialized tool result at 8 KiB. The returned byte offset always advances on a valid UTF-8 boundary.
- Source streams reject growth beyond 32 KiB and can be cancelled while waiting for another chunk.
- Session/tool source now rechecks active-turn, cancellation, and current-request state after reading and after storage work. A snapshot already saved is reported as saved even if the turn subsequently becomes inactive; its receipt cannot claim compaction readiness unless durability, active turn, and request coverage all hold.

## What was actually verified

31 focused Rust tests passed, with 0 failures and 0 ignored tests in that run. The harness compiles the real production no-follow, atomic-directory, handoff-store, and stream modules through path references. Its shims only reproduce the upstream FileMetadata and filesystem-stream data shapes and crate wiring; filesystem security, publication, validation, paging, and cancellation logic are the production source. The run combines 18 production tests with 13 independently written boundary tests.

Coverage includes bounded allocation on an 8 GiB sparse fixture, exclusive creation, hardlink and symlink behavior, held-directory behavior after parent replacement, pre/post-rename error handling, request freshness, copied cross-thread records, superseded IDs, modified snapshots, UTF-8 boundaries, high-escape serialized-output caps, source growth, stream cancellation, and I/O failures. Fault injection is not a real power-loss or process-crash test.

Stable Rust 1.95.0 formatting checks passed. The repository's imports_granularity setting emits a nightly-only warning on that stable formatter. Patch applicability, unchanged upstream attribution, and the whole-diff private-literal scan passed.

## What remains unverified or disabled

The full locked build progressed through dependencies, then rustc compiling the unchanged codex-protocol crate was terminated by SIGKILL before reaching the new feature code. The cause is not established by a per-process OOM record. This is a real failed build, not a successful native integration run.

Full exec-server/core compilation, Session cancellation/steering behavior, actual local/remote tool routing and post-compaction continuation, schema regeneration, Clippy, Windows behavior, and genuine crash/recovery tests remain outstanding. Ten tests requiring enabled prepared handoffs remain explicitly ignored; a separate Windows symlink test requires privileged security CI. None of those are counted in the 31 passing focused tests.

Run the following on a sufficiently provisioned isolated runner before considering a gate change:

```sh
cd codex-rs
cargo check --locked -p codex-exec-server --lib
cargo check --locked -p codex-core --tests
cargo test --locked -p codex-exec-server --lib no_follow_store
cargo test --locked -p codex-exec-server --lib atomic_store
cargo test --locked -p codex-core --lib handoff_store
cargo test --locked -p codex-core --lib handoff_stream
cargo test --locked -p codex-core --test all handoff_compact
cargo run --locked -p codex-config-schema --bin codex-write-config-schema
cargo run --locked -p codex-hooks --bin write_hooks_schema_fixtures
cargo fmt --all --check
```

These direct focused Cargo commands were authorized for this isolated validation. Follow upstream tooling requirements for broader CI, formatting, and lint stages. Tests requiring enabled preparation need a separately reviewed integration-validation plan; do not silently remove the gate or reinterpret ignored tests as passing.

## Python companion compatibility

The existing Python offline companion remains unchanged. Its handoff text can still serve as a candidate source, but its `--binding` observer expects the earlier path-and-SHA-1 binding-file contract. This delta uses opaque IDs and controlled tool readback. That observer is not a working V2 native adapter, and its status must not be presented as evidence of native connection. A separately reviewed adapter update is required; it is outside this delta.

## Security boundary and attribution

A held directory descriptor protects path traversal and ancestor substitution. It does not authenticate data against a hostile process with the same OS identity and write access to the opened directory. Content hashes detect changes; they are not authentication. Failed unpublished temporary files and old snapshots can remain unreferenced because no racy cleanup or automatic retention policy is added.

Mode C archives remain plaintext, without secret redaction or automatic retention, and may leave an unreferenced partial file after a failed export. This delta does not claim those paths or their contents remain immutable after a handle closes.

Original contributions are Apache-2.0, copyright 2026 Ubadream. Upstream licenses and notices are preserved verbatim. This bundle contains no private transcripts, donor installer, runtime receipts, external-provider routing, or private project instructions.
