# Experimental handoff compaction runtime patch

Historical base-patch checkpoint: apply the lockfile prerequisite and then the separately documented `../runtime-review-v2/` delta for the current source candidate. The blocker details and verification below describe this base snapshot, not the later delta. Native deployment remains blocked in both.

Status: EXPERIMENTAL SOURCE PREVIEW / NATIVE DEPLOYMENT BLOCKED / NOT COMPILED. Public research source only, not an installable or safe-to-deploy release. Prepare, binding loading, and prepared-handoff continuation remain fixed disabled. Do not apply it to a production runtime or interpret publication as security certification.

## Scope

The patch targets OpenAI Codex tag `rust-v0.159.2`, exact commit `ff6aec96948b70d94983af2641a6b67c94faeff5`. It is a fresh diff against that clean upstream tree, not an overlay-stack patch.

- `handoff_compact prepare` is DISABLED before any source read or store write. The review source contains the planned bounded handoff/thread-binding implementation, but a fixed fail-closed gate prevents use until safe atomic index publication exists. There is no configuration or environment-variable bypass.
- `A` schedules native summarization after the final response.
- `C` also exports visible dialogue text since the prior checkpoint into a local JSONL archive.
- The planned prepared-handoff `continue`/`wait` behavior is unavailable while the store gate is closed. Loading a binding returns no binding before touching disk, so native compaction retains ordinary user-request handling. A/C scheduling, new-input cancellation, and existing compaction hooks remain in the source.
- Opt-in checkpoints run at model-request boundaries, including tool-free turns. Review, prepare, and compact thresholds are configurable; reminders are deduplicated per context window and re-arm after compaction.
- A backwards-compatible optional PostToolUse field announces native reminder ownership so cooperating hooks can avoid duplicates.

It does not contain external-provider routing, retired-tool shims, a donor installer, private project instructions, live transcripts, release receipts, machine-specific paths, or account fixtures. Runtime names and storage are isolated under `handoff_compact` / `handoff-compact`; there is no migration from other runtimes' saved state. Generic reminders do not expand user authorization.

## Security release blockers

1. The original unrestricted source-path read has been replaced in source: the tool resolves an explicit or primary execution environment, uses its filesystem API with the current sandbox context for metadata and streaming reads, caps accumulated text at 32 KiB, and passes validated UTF-8 text into `prepare`. This source change and five new synthetic tests have NOT been compiled or executed; enforcement is not yet demonstrated. There is no unrestricted local fallback. Each filesystem stream chunk is separately bounded by the upstream API (currently 1 MiB).
2. The former path-based index writer has been removed. Snapshot/read/archive primitives now reuse upstream per-component no-follow handles: Unix openat with NOFOLLOW and exclusive creation; Windows the existing OBJ_DONT_REPARSE / FILE_CREATE wrapper. No new unsafe block was added. These changes are UNCOMPILED. A directory-handle-anchored atomic index replace API is still missing, so prepare remains disabled and load ignores saved indices. No remove-old/create-new or truncate fallback is used. The SHA-1 content fingerprint is change detection, not authentication.
3. Remote continuation is not validated: the current binding returns a runtime-host CODEX_HOME snapshot path, which may be inaccessible to an agent using a remote executor. The new remote source-selection test does not prove post-compaction readback. A thread-bound controlled readback interface or verified shared storage is required.
4. Mode C archives are plaintext, have no secret redaction, and have no automatic retention/deletion policy. They may contain sensitive user/assistant text. A failed C export may leave an unreferenced partial archive; no checkpoint pointer is emitted until completion. They deliberately omit tool-output bodies, media bytes, and analysis-channel messages, but that is not a secret-filter guarantee. Do not use C with sensitive sessions until policy and retention choices are reviewed.

No sensitive real-world file was used to investigate these issues. Current tests use temporary synthetic fixtures.

### Required remediation before a deployable release

- Compile and verify the implemented source-read migration against the execution-environment filesystem API. It follows upstream `core/src/tools/handlers/view_image.rs` for environment resolution and sandboxed metadata, then uses bounded `read_file_stream` rather than unbounded `read_file`. Review permission-denial behavior and cross-platform/remote path handling.
- Extend the existing `exec-server/src/no_follow` abstraction with atomic publication anchored to the same opened parent directory. The Unix design needs relative rename and directory durability; the Windows design needs the equivalent root-handle-anchored rename/replace through the existing platform abstraction. Preserve exclusive create-new snapshot creation. Create-new protects against overwriting an existing entry; it does not prove the path or contents remain immutable after the handle is closed. Define hostile same-user mutation, hardlink, and mount-namespace assumptions. Do not replace the fixed gate until this API and crash/race tests pass.
- Five new synthetic test sources now cover denied outside-workspace reads, unknown environment/no fallback, explicit local relative paths, invalid/oversized/blank UTF-8, and selected remote vs local namesake. Execute them. Add missing snapshot-store symlink/reparse-point escape and replacement-race tests, plus cancellation during read, source-growth races, foreign-OS routing, and archived-text policy tests.
- Compile and execute the affected core/config/hooks tests, schema generators, formatting, and lint checks on Linux and Windows, plus the remote/foreign-environment cases. Split the patch into reviewable core-handoff and checkpoint layers if proposing upstream integration.

## Configuration

Checkpoint reminders default to false. Legacy defaults are 240000 prepare tokens and 265000 compact tokens; review is disabled unless configured. A large-window example is:

```toml
context_checkpoint_reminders = true
context_checkpoint_review_tokens = 265000
context_checkpoint_prepare_tokens = 350000
context_checkpoint_compact_tokens = 430000
```

These settings do not enlarge a model's supported context window. The model's effective automatic-compaction limit must remain above the configured compact stage. A sudden jump can still trigger native automatic compaction first; reminders are not a hard-limit override.

## Reproduction in an isolated checkout only

```sh
git clone https://github.com/openai/codex.git
cd codex
git checkout --detach ff6aec96948b70d94983af2641a6b67c94faeff5
git status --porcelain
git apply --check /path/to/0000-sync-workspace-release-lockfile.patch
git apply /path/to/0000-sync-workspace-release-lockfile.patch
git apply --check /path/to/0001-handoff-compact-checkpoints.patch
git apply /path/to/0001-handoff-compact-checkpoints.patch
git diff --check
```

Use a disposable checkout containing no private data. Applying the patch is not evidence that it compiles or is safe. The source requires Rust 1.95.0 with rustfmt and Clippy. The upstream test recipe also requires cargo-nextest, `just`, Python, and dependencies recorded in the upstream Cargo.lock. The native feature patch adds no Cargo dependencies. The separately included `0000-sync-workspace-release-lockfile.patch` synchronizes 159 workspace-local package versions from 0.0.0 to 0.159.2; all 1,313 external package records are unchanged. Apply that prerequisite first for locked validation. Existing dependencies used include serde/serde_json, tokio, tempfile, uuid, sha1, futures, anyhow, and the upstream execution-environment/filesystem crates.

Follow upstream AGENTS.md. Commands to run once an approved toolchain exists, from `codex-rs`, are:

```sh
just write-config-schema
just write-hooks-schema
just test -p codex-core handoff_compact
just test -p codex-core handoff_binding
just test -p codex-exec-server no_follow_store
just test -p codex-core checkpoint_reminder
just test -p codex-hooks command_input_reports_native_checkpoint_owner_only_when_enabled
just test -p codex-config
just fix -p codex-core -p codex-hooks -p codex-config -p codex-exec-server
just fmt
cargo build --release -p codex-cli --bin codex
```

These commands were not successfully executed here. The full test suite was neither requested nor run; request separate authorization for that upstream-required broader stage after focused checks pass.

## Verification performed

- Verified the upstream tag resolves to the exact commit above.
- Fresh full-checkout `git apply --check`, application, and `git diff --check` passed.
- Static scope, namespace, schema JSON, synthetic-fixture and forbidden-private-literal checks passed; the exact evidence is recorded separately.
- At the original patch review checkpoint, Rust compiler, Cargo, and `just` were absent. Subsequent local build attempts did not complete full Codex compilation. This exact source preview has no completed native compilation or executable integration-test result. Rust formatting, Clippy, schema regeneration, Windows behavior, and remote-environment behavior are not validated. Public CI is an attempt to establish additional evidence, not a promised pass.
- The included Rust tests are source code, not executed evidence. They cover A/C scheduling, cancellation, archive boundaries, prepare/continue/wait, stale/damaged bindings, media identity preservation, checkpoint thresholds, deduplication/resume/re-arm, and optional hook ownership. New synthetic security test source is unexecuted and does not validate the remaining atomic-publication issue. Ten tests that require successful prepare are explicitly ignored while the gate is closed. An active source-level integration test checks rejection before environment/source lookup and storage writes. Five additional no-follow test sources cover bounded UTF-8, existing files/hardlinks, invalid UTF-8/directories, Unix links, and Windows reparse points; the Windows link test is explicitly ignored unless run in privileged security CI.

## Attribution and provenance limits

Original contributions in this preparation are licensed Apache-2.0, copyright 2026 Ubadream. OpenAI Codex and its existing third-party notices remain under their original terms. `UPSTREAM-LICENSE` and `UPSTREAM-NOTICE` are byte-for-byte copies from the pinned upstream commit; `NOTICE` preserves them and identifies these modifications.

No donor installer or donor source was incorporated in this preparation. Reviewed source paths support the stated runtime provenance; this is not an exhaustive historical authorship audit. A clean diff, renamed identifiers, or generic replacement text alone cannot establish provenance or remove an original license obligation.
