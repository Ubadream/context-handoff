# Public CI scope

The workflow uses only GitHub's standard `ubuntu-24.04` runner, a 360-minute native job ceiling, two Cargo build jobs, disabled incremental compilation and debug symbols. It does not request larger or paid runners. Runner disk exhaustion or timeout is a failed/incomplete validation, not evidence of correctness. It does not delete preinstalled tools or files outside the checkout.

The sole Action is the official `actions/checkout` v6.0.2, pinned to commit `de0fac2e4500dabe0009e67214ff5f5447ce83dd`, verified through the official repository's tag reference. Permissions are `contents: read`; checkout credentials are not persisted. There is no `pull_request_target`, deployment, service authentication, private checkout, cache, or artifact-upload step. Only ordinary push, pull-request and manual workflow triggers are used.

Python source tests use the runner's Python 3 and synthetic temporary state. They need no downloaded Python packages. Native validation checks out only public OpenAI Codex commit `ff6aec96948b70d94983af2641a6b67c94faeff5`, verifies all four patch hashes, their exact application order, the resulting source tree, and the resulting lockfile, and obtains Rust 1.95.0 through official rustup. Cargo dependencies are the pinned upstream lockfile's public registry and Git sources; the prerequisite changes only 159 workspace-local version fields, leaving 1,313 external package records unchanged. Building dependencies executes their normal public build scripts on the ephemeral runner. No user credentials, private conversation, personal state, or real-session fixture is supplied.

Native steps attempt locked checks of affected packages, focused Cargo test filters, and a debug-profile CLI build. Cargo's built-in test harness is used directly, not upstream's normal nextest recipe. This is not a full upstream test-suite run, release-profile build, packaged binary release, production runtime installation, Windows execution, or remote readback test. Tests remain synthetic; test output and source paths are public GitHub logs. No environment dump is requested.

Prepare and binding loading remain fixed disabled. Ten prepare-dependent tests and one Windows privilege-dependent test source are ignored. On Linux the Windows-only test is not compiled, so the runner's ignored count need not equal eleven. A green CI job does not prove automatic prepared-handoff continuation works or remove security and deployment blockers.

The initial source snapshot does not claim that this workflow has passed. Consult checks for the exact commit, and distinguish completed steps from steps skipped after a failure.

The official Ubuntu 24.04 runner image software list checked on 2026-10-08 does not list `libcap-dev`. The workflow installs only that required header package (and its required apt dependencies) from the runner's configured official Ubuntu package sources using apt; it adds no repository or installer. This supports the normal Linux sandbox build and does not skip it. All run steps use Bash, with explicit fail-fast/undefined-variable/pipe handling for the focused test sequence.

Runner software reference: https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md

The native source order is Gate B base patch, minimal workspace-local lockfile synchronization, then the V2 atomic-store/readback delta, followed by the one-line storage-root path-type fix. The final source tree must be `a5ae288ba4bfca381405c28f926a420a7d26b057` before compilation. Focused tests also cover `atomic_store`, `handoff_store`, and `handoff_stream`. The historical 31-test Linux production-module harness result is separate from these full-crate CI attempts. V2 does not lift prepare, controlled-readback, binding, or continuation gates. Python's V1 binding observer is not compatible with the V2 opaque-ID contract.

## First public run and compile correction

Run [37783812942](https://github.com/Ubadream/context-handoff/actions/runs/37783812942) for commit `566a627dde8f1d437fa9825b19beeeb9f15cc7b6` completed on 2026-10-08: Python succeeded; native reached `codex-core` and failed with E0308 in `storage_root` because an `AbsolutePathBuf` was returned where `PathBuf` was declared. Native tests and CLI build were skipped after that failure. The fourth patch adds `.to_path_buf()` to that return expression without changing filesystem operations, safety gates, or dependencies. A fresh CI run must establish whether the corrected full native check and subsequent tests/build pass; focused local checks are not a substitute.
