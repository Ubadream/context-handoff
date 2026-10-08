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
- Codex: the native patches keep prepare, readback and automatic continuation disabled; end-to-end compaction and continuation are not verified. See [docs/HANDOFF_CORE.md](docs/HANDOFF_CORE.md).

## Privacy

Everything runs locally and is stored as plain text: tool archives, saved originals, handoffs. Nothing is sent anywhere unless you set `CW_MODEL_CMD`, and then only to the command you chose. There is no secret redaction; protect the state directory accordingly.

## License

Apache-2.0. See `LICENSE`, `NOTICE` and [LICENSE_REVIEW.md](LICENSE_REVIEW.md). Release boundary: [EXCLUSIONS.md](EXCLUSIONS.md). Verification records: [VERIFICATION.md](VERIFICATION.md), [CI_SCOPE.md](CI_SCOPE.md).
