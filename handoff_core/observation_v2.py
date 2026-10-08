"""Offline V2 native-tool receipt validation, never host execution or authentication."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

MAX_HANDOFF_BYTES = 32768
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_PAGES = 1024


def _uuid(value, *, version=None):
    if not isinstance(value, str):
        raise ValueError('Expected a UUID string')
    parsed = uuid.UUID(value)
    if str(parsed) != value or (version is not None and parsed.version != version):
        raise ValueError('Expected canonical UUID with compatible version')
    return value


def _digest(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{40}', value) is None:
        raise ValueError('Expected a lowercase SHA-1 compatibility digest')
    return value


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError('Unexpected or missing receipt fields; incompatible contract')


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('Expected a nonnegative integer byte offset')
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError('Expected a boolean receipt field')
    return value


def observe(envelope, *, session, exported_bytes, continuation):
    """Match captured V2 prepare/read receipts to an export, without trusting origin.

    Every input can be forged. A success states content consistency only, never
    that the host ran, the request is current, or the receipt author is authentic.
    """
    _uuid(session)
    if continuation not in ('continue', 'wait'):
        raise ValueError('Unsupported continuation intent')
    if not isinstance(exported_bytes, bytes) or not 0 < len(exported_bytes) <= MAX_HANDOFF_BYTES:
        raise ValueError('Export must contain 1..32768 UTF-8 bytes')
    export_text = exported_bytes.decode('utf-8')
    if not export_text.strip():
        raise ValueError('Export must be nonempty text')
    expected_digest = hashlib.sha1(exported_bytes).hexdigest()
    _keys(envelope, ['contract', 'host', 'capture_thread_id', 'prepare_result', 'read_results'])
    if envelope['contract'] != 'handoff-native-v2-observation/1' or envelope['host'] != 'codex':
        raise ValueError('Unsupported observation contract or host')
    if envelope['capture_thread_id'] != session:
        raise ValueError('Captured thread differs from requested thread')
    prepared = envelope['prepare_result']
    _keys(prepared, ['prepared', 'snapshot_saved', 'durability', 'requesting_turn_active',
                    'covers_current_request', 'compaction_ready', 'compaction_started',
                    'reconcile_required', 'reconciliation'])
    binding = prepared['prepared']
    _keys(binding, ['schema_version', 'thread_id', 'source_message_id', 'source_text_sha1',
                   'handoff_id', 'handoff_sha1', 'continuation'])
    if type(binding['schema_version']) is not int or binding['schema_version'] != 1:
        raise ValueError('Unsupported native binding schema')
    if binding['thread_id'] != session or binding['continuation'] != continuation:
        raise ValueError('Binding thread or continuation differs from export intent')
    handoff_id = _uuid(binding['handoff_id'], version=4)
    source_id = binding['source_message_id']
    if not isinstance(source_id, str) or not 0 < len(source_id.encode('utf-8')) <= 1024:
        raise ValueError('Invalid source message identity')
    _digest(binding['source_text_sha1'])
    if _digest(binding['handoff_sha1']) != expected_digest:
        raise ValueError('Binding digest differs from exported bytes')
    for key in ['snapshot_saved', 'requesting_turn_active', 'covers_current_request',
                'compaction_ready', 'compaction_started', 'reconcile_required']:
        _boolean(prepared[key])
    if prepared['snapshot_saved'] is not True or prepared['compaction_started'] is not False:
        raise ValueError('Not a compatible prepare-only receipt')
    durability = prepared['durability']
    if durability not in ('confirmed', 'visible_unconfirmed'):
        raise ValueError('Invalid durability state')
    expected_ready = durability == 'confirmed' and prepared['requesting_turn_active'] and prepared['covers_current_request']
    if prepared['compaction_ready'] != expected_ready or prepared['reconcile_required'] != (durability != 'confirmed'):
        raise ValueError('Contradictory prepare state')
    if not isinstance(prepared['reconciliation'], str):
        raise ValueError('Invalid reconciliation description')
    results = envelope['read_results']
    if not isinstance(results, list) or not 1 <= len(results) <= MAX_PAGES:
        raise ValueError('Complete bounded readback pages are required')
    assembled = bytearray()
    for index, result in enumerate(results):
        _keys(result, ['handoff_page', 'content_kind', 'durability'])
        if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 8192:
            raise ValueError('Serialized read result exceeds native 8 KiB limit')
        if result['content_kind'] != 'historical_data' or result['durability'] != 'confirmed_on_read':
            raise ValueError('Unexpected readback semantics')
        page = result['handoff_page']
        _keys(page, ['handoff_id', 'historical_data', 'source_message_id', 'offset',
                     'next_offset', 'total_bytes', 'content_sha1', 'text'])
        if len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 7168:
            raise ValueError('Serialized page exceeds native 7 KiB limit')
        if page['handoff_id'] != handoff_id or page['source_message_id'] != source_id:
            raise ValueError('Read page identity changed')
        if page['historical_data'] is not True or _digest(page['content_sha1']) != expected_digest:
            raise ValueError('Invalid historical content marker or digest')
        if _integer(page['total_bytes']) != len(exported_bytes) or _integer(page['offset']) != len(assembled):
            raise ValueError('Page total/offset changed or page order has gaps')
        if not isinstance(page['text'], str):
            raise ValueError('Page content must be UTF-8 text')
        raw = page['text'].encode('utf-8')
        if len(raw) > 4096 or len(assembled) + len(raw) > len(exported_bytes):
            raise ValueError('Page exceeds byte bounds')
        assembled.extend(raw)
        next_offset = page['next_offset']
        if index + 1 == len(results):
            if next_offset is not None or len(assembled) != len(exported_bytes):
                raise ValueError('Readback is incomplete')
        elif not raw or _integer(next_offset) != len(assembled) or len(assembled) >= len(exported_bytes):
            raise ValueError('Invalid next byte offset or premature end')
    if bytes(assembled) != exported_bytes:
        raise ValueError('Reassembled content differs from export')
    return {'adapter_contract': 'handoff-native-v2-observation/1', 'host': 'codex',
            'session': session, 'handoff_id': handoff_id, 'state': 'captured_content_matches_export',
            'content_sha1': expected_digest, 'bytes': len(assembled), 'pages': len(results),
            'historical_data': True, 'host_execution_verified': False,
            'receipt_authenticity': 'unverified', 'binding_freshness': 'unknown',
            'captured_prepare_durability': durability, 'captured_read_durability': 'confirmed_on_read',
            'captured_prepare_ready': prepared['compaction_ready'], 'scheduled': None,
            'completed': None, 'action_authorized': False,
            'note': 'Offline consistency check only. All supplied receipts may be forged or stale; recheck in the authenticated host before any action.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--export', dest='export_path', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--continuation', choices=['continue', 'wait'], required=True)
    args = parser.parse_args(argv)
    try:
        with args.export_path.open('rb') as stream:
            exported = stream.read(MAX_HANDOFF_BYTES + 1)
        with args.receipt.open('rb') as stream:
            receipt = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise ValueError('Receipt input exceeds the 8 MiB bound')
        result = observe(json.loads(receipt), session=args.session, exported_bytes=exported,
                         continuation=args.continuation)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
