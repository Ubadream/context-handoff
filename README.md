# Context Handoff

**Let a long-running coding agent decide when to compact its own context, write a handoff first, and get the exact original back later, instead of being cut off mid-task by automatic compaction.**

Part of Context Workbench. Built by one human and the two AI coding partners he works with every day: a Claude one and a Codex one. He calls them his wives, which is why the code and its messages say 老公 (husband, the user) and 老婆 (wife, the assistant). We kept that.

## The problem

Long agent sessions hit the context limit, the host compacts, and then:

- decisions and constraints the user stated an hour ago are gone from the summary;
- the agent picks up an old handoff and continues from a stale state;
- work that was already done gets redone, because the last user message is replayed as if it were new;
- most of the window was tool output that nobody needed after the next step.

## What works today

### Claude Code adapter (`adapters/claude/`), in daily use since late September 2026

**Since 2026-09-25 the maintainer's Claude Code sessions have compacted 24 times, none of them automatically.** Every one happened at a chosen break: scheduled by the agent, or by the user typing `/compact`. The day before, without this adapter, the host compacted twice in the middle of work, once at 888K tokens. Across the 21 compactions where the result is measurable, median context went from 412K tokens before to 89K after (range 61K to 112K); roughly 70K of the "after" is fixed overhead (tool definitions, instructions, memory files) that compaction cannot touch.

| Feature | What it does | Measured on a real session |
|---|---|---|
| Agent-chosen compaction | A `compact` tool lets the agent schedule compaction at a natural break, after updating its handoff. Mode A = built-in summary; mode C = summary plus the full pre-compaction text saved to a file, indexed by section with content hashes. Compaction is blocked if no handoff was written since the last one, unless the agent gives a reason. | |
| Tool round-trips to one line | At compaction every tool call becomes one line with an ID; the full call and result are archived locally and `get <id>` brings them back. | 579 messages → 30, 498,605 → 38,102 characters (one compaction, 2026-09-30) |
| Segment and drop | The conversation is split at each user message. An optional model labels each segment and suggests which are finished; the agent confirms which to drop. The user's own words in dropped segments are kept; the rest is retrievable by segment ID. | 3 segments dropped: 602,545 → 498,605 characters (same compaction) |
| Large output kept out of context | A PostToolUse hook saves large Bash/PowerShell, Grep, WebFetch/WebSearch and browser page-text output to a file and keeps head + tail + path. Large `Read` results keep only the first lines with correct line numbers and say where to continue. | a 37,322-character Grep result → about 3,500 |
| Handoff check | Before compacting: is the handoff newer than the last user message and file edit? The optional model lists what the handoff may have missed. | |
| Thresholds and idle reminder | Reminders at configurable context sizes. If the user goes idle with a large context, one reminder before the prompt cache expires asks the agent to write its handoff and compact, so the user doesn't pay to re-read the whole window. | |
| Fixed-overhead diet | Rarely used tools are deferred and long skill descriptions shortened (switchable). | |

### Handoff core and native Codex patches (`handoff_core/`, `runtime-review*/`)

The same flow also runs in the maintainer's Codex, through a private runtime overlay on Windows: the agent writes a handoff, calls `wife_compact(mode="prepare")` to bind it to the thread, then compacts with A or C and either continues or waits. In the maintainer's ten longest-running Codex threads, the host compacted 574 times on its own before the overlay went live on 2026-09-28. Since then there have been 406 compactions: 397 scheduled by the agent through `wife_compact` after binding its handoff (362 A, 35 C), 7 by the host, and 2 where the host compacted after a prepare but before the agent chose a mode. The patches here are the reviewed public extraction of that overlay. Patch 0004 opens prepare, readback and continuation on Unix and Windows. The tests that were ignored while they were closed now run: on Linux in public CI and on the maintainer's Windows machine; running them found and fixed two bugs. Windows also got the hardened store it was missing: the storage directory is opened one component at a time without following junctions or symlinks, and the binding is replaced in place relative to that held directory handle.

A small standard-library Python component for revisioned handoff drafts, previews, deterministic exports and binding integrity checks, plus native Codex patches whose runtime gates are still closed. Details, safety properties and verification: [docs/HANDOFF_CORE.md](docs/HANDOFF_CORE.md).

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

## How it fits together

    user message ─► UserPromptSubmit: context_watch (threshold reminders)
    tool call ────► PreToolUse/PostToolUse: tool_archive (full copy + ID)
                                            tool_output_trim (large output → file)
    agent ────────► wife-compact tools: prepare / segments / handoff_check / compact / get / context
    end of turn ──► wife-compact: run the compaction the agent scheduled
                    (drop confirmed segments → tool calls to one line → A or C)
    after compact ► SessionStart: handoff_bind (hand the agent back its own handoff file)

State lives under `~/.local/state/wifeos/` (override with `CW_STATE_ROOT`).

## Limitations

- Function hooks are an early-access Claude Code feature; the installer turns them on.
- Developed and used on Windows. CI runs the adapter's unit tests on Linux; a full install has only been run on the maintainer's machine.
- Context can only be rewritten at compaction time; between compactions, only new output can be kept small.
- The idle reminder has passed tests but has not yet been seen firing after a real idle period.
- Messages and labels are in Traditional Chinese.
- Not included from the private setup: recovering summary-dropped sentences with a local ranking model, and the control panel.
- Codex: the public patches are tested with Codex's own integration harness (mock model server), not yet in a long real session; end-to-end daily use so far is in the private overlay. Windows behaviour is tested on one machine, not in CI. One deny-read test cannot run on the CI runner, where Codex's filesystem sandbox helper aborts; upstream's own deny-read test fails the same way there. See [docs/HANDOFF_CORE.md](docs/HANDOFF_CORE.md).

## Privacy

Everything runs locally and is stored as plain text: tool archives, saved originals, handoffs. Nothing is sent anywhere unless you set `CW_MODEL_CMD`, and then only to the command you chose. There is no secret redaction; protect the state directory accordingly.

## License

Apache-2.0. See `LICENSE`, `NOTICE` and [docs/LICENSE_REVIEW.md](docs/LICENSE_REVIEW.md). Release boundary: [docs/EXCLUSIONS.md](docs/EXCLUSIONS.md). Verification records: [docs/VERIFICATION.md](docs/VERIFICATION.md), [docs/CI_SCOPE.md](docs/CI_SCOPE.md).
