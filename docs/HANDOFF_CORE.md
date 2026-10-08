# Handoff core and native Codex patches (preview)

> **Update 2026-10-09, patch 0004:** prepared handoffs, readback, binding loading and continuation are now open on Unix and Windows, and Windows has its own held-handle store. Statements below that these are "fixed disabled" describe the patches before 0004. See [VERIFICATION.md](VERIFICATION.md#patch-0004-prepared-handoffs-open) and [CI_SCOPE.md](CI_SCOPE.md).

Experimental source preview. Original contributions use Apache-2.0, copyright 2026 Ubadream. This part of the repository combines a tested Python draft/export component with separately scoped, partially verified native Codex patches. It is not a production runtime or installer.

## Implemented scope

A small Python 3.11+ standard-library component for saving revisioned handoff drafts, previewing them against an explicitly selected observation, exporting deterministic handoff files, and checking explicit host binding snapshots. It supports requested Codex modes A and C and continuation intent `continue` or `wait`.

This component does **not** compact a live Codex context, schedule a model request, install hooks, change Codex configuration, inject prompts, or implement native automatic continuation. Modes and continuation are requests recorded in an export. The native base patch in `runtime-review/` plus the V2 delta in `runtime-review-v2/` have prepare, binding loading, controlled readback, and prepared-handoff continuation fixed disabled. Full native-crate compilation and integration remain unverified; 11 test sources are ignored (10 require disabled prepare, one requires Windows symlink privileges).

Safety properties include SQLite compare-and-swap revisions, append-only draft history, no overwrite of changed exports, explicit state and observation inputs, exact session identity, and binding digest verification. A matching binding file establishes integrity only. Its freshness and whether compaction completed remain unknown. SHA-1 is retained solely for compatibility with the native binding format, not as an adversarial authenticity guarantee.

## Run without installation

From the repository root:

    python -m unittest discover -s tests -v
    python -m handoff_core --help

No network, model, API key, external program, or Python package is needed for this source-mode workflow. The tests use temporary synthetic data only. They never discover or open a real Codex home.

Create a candidate JSON file containing:

    {"mode":"C","continuation":"continue","handoff":"Synthetic completed step; next verify result.","instructions":"Preserve the reason."}

Create an observation JSON file containing:

    {"host":"codex","session":"demo-session","context_tokens":100}

Then select your own isolated state directory and files:

    python -m handoff_core --state-dir ./demo-state save demo-session --candidate candidate.json --expected-revision 0
    python -m handoff_core --state-dir ./demo-state --observation observation.json preview demo-session --candidate candidate.json --expected-revision 1
    python -m handoff_core --state-dir ./demo-state --observation observation.json export demo-session --candidate candidate.json --expected-revision 1

Use full identities. Snapshot data can be stale or forged and cannot authorize actions. Existing candidate revisions are never edited in place. Exporting does not call the host runtime. The returned prepare arguments are a suggested interface for a separately reviewed compatible runtime, not an executable promise.

`status` accepts the same arguments as `export`, with optional `--binding binding.json`. The adapter verifies the full thread identity and the SHA-1 of the bound handoff file. The bound file must be inside the explicitly selected binding file's directory. The adapter ignores observation-supplied verification markers. It does not verify signatures or establish trust in who created the binding.

## Optional isolated installation

Use a fresh virtual environment rather than replacing any existing runtime:

    python -m venv .venv
    .venv/bin/python -m pip install --no-deps --no-build-isolation .

Windows uses `.venv\Scripts\python.exe`. Building an installable wheel requires setuptools 77.0.3+ and wheel in that environment. This source-only test workflow does not require them. Offline installation needs those build dependencies already available; missing dependencies are a blocker, not a test pass.

## Uninstall and rollback

Run `python -m pip uninstall handoff-core-preview` inside the selected environment. Alternatively, stop using the isolated environment; it has no runtime integration to reverse. Do not delete the state directory when uninstalling. It contains historical drafts and exports and can be backed up as a whole while no writer is active. To revert to an earlier candidate, load the old content and save it as a new revision using the current revision as the expected value. Never rewind or edit the SQLite database to fake a revision.

No production install or uninstall has been executed. Native Codex runtime switching and rollback are outside this component's scope.

## Privacy and data handling

Draft history in SQLite and exported handoffs are stored as plaintext. Load, preview, export, and status output can contain handoff text or local paths. There is no secret detection or redaction. Choose a suitably protected local directory, review candidate content, and never commit or upload personal state or command output without checking it. Local-only operation does not mean encrypted storage.

## Source, attribution, and validation

See `SOURCE_MANIFEST.json`, `EXCLUSIONS.md`, and `LICENSE_REVIEW.md`. The published file set is deliberately limited to the handoff feature. No donor installer, UI, external model router, production receipt, transcript, or personal prompt is included. OpenAI upstream LICENSE and NOTICE are retained under `runtime-review/` alongside the patch.

The standard GitHub Actions workflow checks Python and attempts focused Rust checks, tests, and a Codex CLI build against the exact pinned public upstream revision. A green Python job is not a native runtime pass. The workflow is not a full upstream test suite, production installation, Windows validation, remote readback validation, or automatic-continuation demonstration. See `CI_SCOPE.md`.

## Native V2 and companion compatibility

The current native source candidate is the base Gate B patch plus the minimal lockfile prerequisite plus `runtime-review-v2/patches/0002-atomic-store-and-thread-readback.patch` and `runtime-review-v2/patches/0003-Fix-handoff-storage-root-path-type.patch`, in that order. The V2 delta implements Unix atomic store publication and thread-scoped opaque-ID readback source, but does not enable native prepare, readback, binding application, or automatic continuation. Windows atomic publication explicitly returns Unsupported. See the [V2 scope and test report](../runtime-review-v2/README.md).

31 focused Linux tests passed in a production-module harness, including independent boundary tests. Those results are not full-crate compilation, Session integration, Windows, remote execution, or post-compaction end-to-end evidence. A full local locked build failed with SIGKILL in unchanged upstream codex-protocol before reaching the feature code. The first public CI run reached `codex-core` but failed with a return-type mismatch; native tests and CLI build did not run. The fourth patch corrects that one type mismatch. CI run [37788233583](https://github.com/Ubadream/context-handoff/actions/runs/37788233583) on commit df87cd4 then passed focused locked compilation, the focused native tests (disabled cases still ignored) and the debug CLI build.

The Python draft/export interface remains usable for preparing candidate text. Its V1 `--binding` observer expects a path-and-SHA-1 binding file and does not interoperate with V2 opaque IDs and controlled readback. Do not use that observer's result as proof of V2 native connection. A separate opt-in offline V2 capture observer is included; it does not change the V1 interface or establish live native integration. See [ADAPTER-V2.md](ADAPTER-V2.md).

### Optional offline V2 capture observer

`python -m handoff_core.observation_v2` checks explicitly selected export bytes against a supplied captured prepare/read sequence using the native V2 opaque-ID contract. A successful result means only that the captured content matches the export. Receipt authenticity is unverified, freshness is unknown, and host execution and action authorization remain false/unverified. The command does not invoke native tools, perform compaction, fetch pages, or bypass the disabled gates. Supplied captures can be forged. See [the capture format and limitations](ADAPTER-V2.md).

The combined Python suite has 25 passing synthetic tests (11 original plus 14 observer tests). These tests do not establish a live native connection, Session behavior, Windows or remote integration.

## Compile correction

The first public run ([37783812942](https://github.com/Ubadream/context-handoff/actions/runs/37783812942)) passed Python and found E0308 in the native `storage_root` return value. The additive `0003` patch converts the existing `AbsolutePathBuf` result to the declared `PathBuf`; it does not change storage policy or enable any gated behavior. See [the exact fix manifest](../runtime-review-v2/COMPILE-FIX.json) and [CI scope](CI_SCOPE.md). A corrected source candidate is not a claim that the full native build or integration tests now pass.
