# Context Handoff

**Your coding agent decides when to compact, writes its own handoff first, and can get every original back later — instead of being cut off mid-task by automatic compaction.**

From the maintainer's daily Claude Code sessions since 2026-09-25:

- **24 compactions, none forced by the host.** Every one happened at a chosen break: scheduled by the agent, or by the user typing `/compact`. The day before, without this, the host compacted twice in the middle of work, once at 888K tokens.
- **Median context 412K → 89K tokens** per compaction (21 measurable; about 70K of the "after" is fixed overhead no compaction can touch).
- **579 messages → 30 lines** in one compaction, with every tool call and result still one `get <id>` away.

Works with **Claude Code** (in daily use) and **Codex** (hooks for the Codex you already have, or patches that let the agent compact itself). Everything runs locally.

→ [Quick start: Claude Code](#quick-start-claude-code) · [Quick start: Codex](#quick-start-codex-hooks) · [How it works](#how-it-fits-together)

## The problem

Long agent sessions hit the context limit, the host compacts, and then:

- decisions and constraints the user stated an hour ago are gone from the summary;
- the agent picks up an old handoff and continues from a stale state;
- work that was already done gets redone, because the last user message is replayed as if it were new;
- most of the window was tool output that nobody needed after the next step.

## What's here

| Part | For | Status |
|---|---|---|
| [Claude Code adapter](#claude-code-adapter-adaptersclaude) | Claude Code | In daily use since late September 2026; installs with one command |
| [Codex hooks](#codex-hooks-adapterscodexhooks) | The Codex you already have, no rebuild | Tested in real Codex 0.159.2 (CI, Linux and Windows); not yet used daily |
| [Codex patches](#codex-patches-runtime-review) | Codex, adding what hooks cannot: the agent choosing when to compact, and a handoff bound to the request it covers | Source preview; builds from source; not yet through a long real session on this public build |

Rule of thumb for Codex: start with the hooks. Use the patches if you want the agent itself to decide when to compact.

## Quick start (Claude Code)

Requirements: Claude Code with function hooks, Python 3.11+.

    git clone https://github.com/Ubadream/context-handoff
    cd context-handoff
    python adapters/claude/cw_install.py apply --dry-run   # shows what would change
    python adapters/claude/cw_install.py apply

Restart Claude Code, then check:

    python adapters/claude/cw_install.py doctor

`apply` backs up `~/.claude/settings.json` first, points the hooks at this checkout (existing hooks of the same scripts are updated in place, others untouched), sets `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`, `CW_WORKBENCH` and `CW_PYTHON`, and installs the `wife-compact` plugin from this directory as a local marketplace.

Tool output, saved originals and handoffs are stored as plain text under the state directory with no secret redaction; see [Privacy](#privacy).

**Optional model.** Segment labels and handoff-gap suggestions use any command that reads a prompt on stdin and prints an answer. Add it to the `env` block of `~/.claude/settings.json`, for example:

    "CW_MODEL_CMD": "claude -p --model haiku"

Without it, segmentation and the handoff freshness check still run; there are just no suggestions.

## Quick start (Codex hooks)

Requirements: Codex with hooks (tested with 0.159.2), Python 3.11+.

    python adapters/codex/install_codex_hooks.py apply --dry-run   # shows what would change
    python adapters/codex/install_codex_hooks.py apply
    python adapters/codex/install_codex_hooks.py trust             # or review the new hooks in Codex
    python adapters/codex/install_codex_hooks.py doctor            # all four should say trust=trusted

Then restart Codex. `apply` backs up `$CODEX_HOME/config.toml`, writes one block between `# BEGIN context-handoff hooks` and `# END context-handoff hooks`, and turns on `[features] hooks = true` (inside your existing `[features]` table if there is one). Codex only runs a hook after it is trusted; `trust` asks Codex itself for the hash of each of these four commands and records only those. `remove` takes the block out again.

Optional settings, in `$CODEX_HOME/context-handoff/hooks.json`:

    {"thresholds": [0.6, 0.8], "handoff_globs": ["HANDOFF*.md", "handoff*.md"]}

A threshold below 1 is a fraction of the model's context window; 1 or more is a token count. Reading a compacted section back:

    python adapters/codex/hooks/cw_codex_hooks.py list <ID>
    python adapters/codex/hooks/cw_codex_hooks.py get <ID>§2

To see exactly what Codex sends to the hooks, set `CONTEXT_HANDOFF_HOOK_LOG` to a file path.

For the Codex patches, see [their install section](#install-codex-patches).

## How it fits together

    user message ─► UserPromptSubmit: context_watch (threshold reminders)
    tool call ────► PreToolUse/PostToolUse: tool_archive (full copy + ID)
                                            tool_output_trim (large output → file)
    agent ────────► wife-compact tools: prepare / segments / handoff_check / compact / get / context
    end of turn ──► wife-compact: run the compaction the agent scheduled
                    (drop confirmed segments → tool calls to one line → A or C)
    after compact ► SessionStart: handoff_bind (hand the agent back its own handoff file)

State lives under `~/.local/state/wifeos/` (override with `CW_STATE_ROOT`).

The two compaction modes: **A** uses the host's built-in summary; **C** is the summary plus the full pre-compaction text saved to a file, indexed by section with content hashes, so any part can be read back.

## Claude Code adapter (`adapters/claude/`)

In daily use since late September 2026. Across the 21 compactions where the result is measurable, median context went from 412K tokens before to 89K after (range 61K to 112K); roughly 70K of the "after" is fixed overhead (tool definitions, instructions, memory files) that compaction cannot touch.

| Feature | What it does | Measured on a real session |
|---|---|---|
| Agent-chosen compaction | A `compact` tool lets the agent schedule compaction at a natural break, after updating its handoff, in mode A or C. Compaction is blocked if no handoff was written since the last one, unless the agent gives a reason. | |
| Tool round-trips to one line | At compaction every tool call becomes one line with an ID; the full call and result are archived locally and `get <id>` brings them back. | 579 messages → 30, 498,605 → 38,102 characters (one compaction, 2026-09-30) |
| Segment and drop | The conversation is split at each user message. An optional model labels each segment and suggests which are finished; the agent confirms which to drop. The user's own words in dropped segments are kept; the rest is retrievable by segment ID. | 3 segments dropped: 602,545 → 498,605 characters (same compaction) |
| Large output kept out of context | A PostToolUse hook saves large Bash/PowerShell, Grep, WebFetch/WebSearch and browser page-text output to a file and keeps head + tail + path. Large `Read` results keep only the first lines with correct line numbers and say where to continue. | a 37,322-character Grep result → about 3,500 |
| Handoff check | Before compacting: is the handoff newer than the last user message and file edit? The optional model lists what the handoff may have missed. | |
| Thresholds and idle reminder | Reminders at configurable context sizes. If the user goes idle with a large context, one reminder before the prompt cache expires asks the agent to write its handoff and compact, so the user doesn't pay to re-read the whole window. | |
| Fixed-overhead diet | Rarely used tools are deferred and long skill descriptions shortened (switchable). | |

## Codex hooks (`adapters/codex/hooks/`)

**No rebuild.** Added 2026-10-11. Four standard Codex hooks, one Python script, nothing compiled. Because it only uses Codex's documented hook events, it keeps working when Codex updates, unlike the patches.

| Hook | What it does |
|---|---|
| PostToolUse, UserPromptSubmit | Context-size reminders, once per threshold until the next compaction. Default: at 60% of the model's context window, update the handoff at the next natural break; at 80%, save it now and tell the user it is a good point to run `/compact`. |
| PreCompact | Records where the transcript ended, which handoff file is newest in the working directory, and when the user last spoke. |
| SessionStart (compact) | Tells the agent which handoff to read first, and whether it was saved before or after the user's latest message; says the last request in the summary is not a new one; points to an index of the part that was just compacted. |
| (command) | `list <ID>` and `get <ID>§N` read a section of the compacted transcript back, split at each user message, after checking its SHA-256 against the index. |

**What hooks cannot do, compared with the patches:**

| | Hooks | Patches |
|---|---|---|
| Who starts compaction | The user (`/compact`) or the host's automatic limit; the agent can only ask | The agent, with a `handoff_compact` tool, at a break it picks |
| Not replaying the last request | An instruction after compaction; the model can still ignore it | The handoff is bound to the request it covers; the host does not replay it |
| Handoff | Newest `HANDOFF*.md` in the working directory, with a freshness check | A copy stored and bound to the thread at prepare time |
| Codex upgrades | Keeps working | Pinned to 0.159.2 until the patches are rebased |
| Install | Edits one marked block in `config.toml` | Builds Codex from source (tens of GB, a long first build) |

**Install:** see [Quick start (Codex hooks)](#quick-start-codex-hooks).

<details>
<summary><b>Verified, and known gaps</b></summary>

**Verified:** in CI, with Codex 0.159.2 from npm on Linux and Windows: an isolated `CODEX_HOME`, a loopback model server, no account or credentials. The test installs the hooks, trusts them through Codex's own `hooks/list`, and runs one turn: two tool calls, the host's automatic compaction, one more request. It then checks that the reminder reached the model before compaction, that the note after compaction names the handoff and gives an index ID, and that `get` returns the user's original words. Unit tests cover thresholds, stale or missing handoffs, the second compaction indexing only the new part, refusing a transcript whose bytes changed, and installer edits that leave the rest of `config.toml` alone. On a real 1.69 GB Codex transcript, the first index took 11 seconds (hook timeout: 60).

**Known gaps:**

- Hook events carry no token counts, and Codex writes them to the transcript asynchronously, so a reminder arrives one tool call after the threshold is crossed. Keep the last threshold well below the host's automatic-compaction limit.
- Not yet used in long real sessions. The maintainer's daily Codex runs the private overlay, which has its own hooks.
- macOS untested.

</details>

## Codex patches (`runtime-review*/`)

**Source preview.** No prebuilt binary; the installer below builds from source. Daily use so far is in the maintainer's private overlay, not in this public build.

The same flow as a `handoff_compact` tool inside Codex. The agent writes a handoff file and calls `handoff_compact(mode="prepare")`, which stores a copy and binds it to the thread and to the user request it covers. The agent then compacts with A or C and either continues or waits. After compaction the agent receives an opaque handoff ID and reads the handoff back in bounded pages; the original request is not replayed.

**From the private overlay (same design, not these patches):** in the maintainer's ten longest-running Codex threads, the host compacted 574 times on its own before the overlay went live on 2026-09-28. Since then there have been 406 compactions: 397 scheduled by the agent after binding its handoff (362 A, 35 C), 7 by the host, and 2 where the host compacted after a prepare but before the agent chose a mode.

<details>
<summary><b>Verified, and known gaps</b></summary>

**Verified on the public patches:**

- Linux, public CI: focused locked compilation; handoff integration tests; store, binding, stream, no-follow and atomic-store tests; hooks and config tests; debug CLI build.
- Windows 10, maintainer's machine: the same list, plus Windows-only store tests (junction refused, replacement while the old binding is open). On Windows the storage directory is opened one component at a time without following junctions or symlinks, and the binding is replaced relative to that held directory handle.
- Prepare is open on Unix and Windows and closed on other platforms, with no bypass.

**Known gaps:**

- Not yet run in a long real Codex session on this public build.
- Windows runs in CI (installer end to end, and the focused native tests); macOS is untested.
- One deny-read test cannot run on the CI runner: Codex's filesystem sandbox helper aborts there, and upstream's own deny-read test fails the same way.
- Bugs found so far and fixed: patch 0004 fixed two in previously ignored tests (a blank handoff created its directory before being rejected; the deny-read test never started a turn). Patch 0005 fixes one found in daily use of the overlay: after one agent-scheduled compaction, a second one with no new user message in between could not bind its handoff, so the agent had to ask the user to say something.

</details>

### Install (Codex patches)

Linux or Windows; needs git, Python 3.11+ and [Rust](https://rustup.rs), plus the Visual Studio C++ build tools on Windows:

    python adapters/codex/install_codex.py plan      # checks tools, shows paths
    python adapters/codex/install_codex.py install   # builds from source; the first build takes a long time and tens of GB
    python adapters/codex/install_codex.py doctor

`install` fetches the pinned upstream Codex (0.159.2, commit `ff6aec9`), refuses to continue unless every patch matches the hash in `runtime-review-v2/PATCH_SERIES.json` and the patched source matches the reviewed tree, builds with upstream's own packaging script, and checks that `Cargo.lock` did not change. It installs into its own directory under `$CODEX_HOME/runtimes/` and writes a launcher there, `codex-handoff`, which runs this build with checkpoint reminders turned on. Your existing Codex, its config and the Codex desktop app are not touched; `uninstall` removes only what `install` created. `--profile dev-small` builds faster but unoptimized.

This build reads the same `~/.codex` as your normal Codex, and it is pinned to 0.159.2: it will not follow upstream releases until the patches are rebased.

## Handoff core (`handoff_core/`)

A small standard-library Python component for revisioned handoff drafts, previews, deterministic exports and binding integrity checks. It is not used by the Claude adapter or the Codex patches at runtime. Details, safety properties and verification: [docs/HANDOFF_CORE.md](docs/HANDOFF_CORE.md).

## Limitations

- Function hooks are an early-access Claude Code feature; the installer turns them on.
- Developed and used on Windows. CI runs the adapter's unit tests on Linux; a full install has only been run on the maintainer's machine.
- Context can only be rewritten at compaction time; between compactions, only new output can be kept small.
- The idle reminder has passed tests but has not yet been seen firing after a real idle period.
- Messages and labels are in Traditional Chinese.
- Not included from the private setup: recovering summary-dropped sentences with a local ranking model, and the control panel.
- Codex: see the known gaps for the [hooks](#codex-hooks-adapterscodexhooks) and the [patches](#codex-patches-runtime-review) above. Both are tested against a mock model server, not with a real model.

## Privacy

Everything runs locally and is stored as plain text: tool archives, saved originals, handoffs. The Codex hooks keep their state in `$CODEX_HOME/context-handoff/`: byte offsets, hashes and the first 160 characters of each user message; `get` reads the transcript Codex already keeps. Nothing is sent anywhere unless you set `CW_MODEL_CMD`, and then only to the command you chose. There is no secret redaction; protect the state directory accordingly.

## About

This repo is the public part of Context Workbench, the maintainer's private setup; `wife-compact` is the name of the Claude Code plugin inside it.

It was built by one human and the two AI coding partners he works with every day: a Claude one and a Codex one. He calls them his wives, which is why the code and its messages say 老公 (husband, the user) and 老婆 (wife, the assistant). We kept that.

## License

Apache-2.0. See `LICENSE`, `NOTICE` and [docs/LICENSE_REVIEW.md](docs/LICENSE_REVIEW.md). Release boundary: [docs/EXCLUSIONS.md](docs/EXCLUSIONS.md). Verification records: [docs/VERIFICATION.md](docs/VERIFICATION.md), [docs/CI_SCOPE.md](docs/CI_SCOPE.md).
