# Native V2 observation adapter proposal

This independent addition retains every V1 source file and command. It adds a versioned, offline observation command for the native opaque-ID/tool-read contract. It does not replace the V1 path-based binding adapter, enable native prepare/read, or claim a working live integration.

Native contract reference: frozen feature delta SHA-256 `82a42653f77077507f834af350f8458d7909af152ea9c2d6874bc759411f61f6`. V2 is the native feature revision; its binding schema_version is 1.

## Interface boundary

- V1 exported UTF-8 packet bytes remain valid native V2 prepare inputs (at most 32768 bytes). The export SHA-1 matches the native `handoff_sha1` and page `content_sha1` on the exact bytes, including line endings.
- Native `source_text_sha1` belongs to canonical native user-message text. The adapter validates its syntax only and never substitutes a Python-generated summary digest.
- Native prepare returns an opaque UUIDv4. Read requests are made in the authenticated current thread using that ID and byte offsets. A newer binding makes the old ID unavailable.
- Native read pages carry historical data, at most 4096 raw UTF-8 bytes each; the serialized page is bounded at 7168 bytes and complete serialized result at 8192 bytes. Offsets count bytes, not Unicode characters. Escaping can make pages smaller.
- Preparation, durable persistence, request freshness, scheduling, and completion are separate states. An inactive request may retain a saved historical snapshot without being eligible for compaction.

## Offline capture format

An explicitly selected JSON capture uses exactly these outer fields:

- `contract`: `handoff-native-v2-observation/1`
- `host`: `codex`
- `capture_thread_id`: the full expected thread UUID
- `prepare_result`: the exact decoded native prepare JSON result
- `read_results`: the ordered decoded native read JSON results, through `next_offset: null`

The outer wrapper is an adapter capture format, not a newly invented native API. The inner objects are checked against the frozen native V2 contract. Do not synthesize a prepare/read result for a real observation. Test fixtures are deliberately fabricated and never counted as native runtime evidence.

Run:

    python -m handoff_core.observation_v2 --session <full-thread-uuid> --export <explicit-export-file> --receipt <explicit-capture-file> --continuation continue

The command reads only the two selected files, with bounded reads (32768-byte export, 8 MiB capture, at most 1024 pages). It does not scan runtime directories, call a model, invoke native tools, modify state, fetch pages, or follow paths embedded in receipts.

## What success means

`captured_content_matches_export` means the supplied prepare and complete page sequence agree with the selected export bytes and expected thread/continuation. Receipt authenticity remains unverified, freshness unknown, host execution unverified, and scheduled/completed unknown. A caller can fabricate every input. Even a captured `compaction_ready: true` is a historical field, never permission or evidence that a current action is safe. SHA-1 provides compatibility/change detection, not adversarial authenticity.

All captures, handoff files, and output may contain personal data and are plaintext. No automatic redaction is provided. Keep them out of source control and public uploads.

## Verification and remaining integration

Run all V1 and V2 tests with `python -m unittest discover -s tests -v`. Synthetic tests exercise exact exported-byte compatibility, UTF-8 paging, identity/continuation mismatch, stale/mixed IDs, digest tampering, incomplete/reordered pages, page and serialized-size limits, schema drift, contradictory readiness flags, and the real CLI.

Native prepare/read remain gated. A genuine live end-to-end run must capture authenticated native outputs, confirm cross-thread denial and newer-request freshness handling, and exercise the real Session/native-compaction path before calling the combined system operational. Windows and remote policy integration are not established by this adapter. This proposal is suitable for review as an additive observation layer; it is not a workaround for the native safety gate.
