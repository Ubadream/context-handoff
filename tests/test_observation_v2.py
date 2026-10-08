import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from handoff_core import handoff
from handoff_core.observation_v2 import observe

# Synthetic UUIDs; never copied from a user session or receipt.
SESSION = '11111111-1111-4111-8111-111111111111'
OTHER_SESSION = '22222222-2222-4222-8222-222222222222'
HANDOFF = '33333333-3333-4333-8333-333333333333'
OTHER_HANDOFF = '44444444-4444-4444-8444-444444444444'


def capture(raw, *, chunks=None, durability='confirmed', active=True, current=True):
    digest = hashlib.sha1(raw).hexdigest()
    prepared = {'prepared': {'schema_version': 1, 'thread_id': SESSION,
        'source_message_id': 'synthetic-message', 'source_text_sha1': hashlib.sha1(b'synthetic original request').hexdigest(),
        'handoff_id': HANDOFF, 'handoff_sha1': digest, 'continuation': 'continue'},
        'snapshot_saved': True, 'durability': durability, 'requesting_turn_active': active,
        'covers_current_request': current, 'compaction_ready': durability == 'confirmed' and active and current,
        'compaction_started': False, 'reconcile_required': durability != 'confirmed',
        'reconciliation': 'Synthetic fixture, not runtime evidence.'}
    chunks = chunks if chunks is not None else [raw.decode('utf-8')]
    pages = []
    offset = 0
    for index, text in enumerate(chunks):
        end = offset + len(text.encode('utf-8'))
        pages.append({'handoff_page': {'handoff_id': HANDOFF, 'historical_data': True,
            'source_message_id': 'synthetic-message', 'offset': offset,
            'next_offset': end if index+1 < len(chunks) else None,
            'total_bytes': len(raw), 'content_sha1': digest, 'text': text},
            'content_kind': 'historical_data', 'durability': 'confirmed_on_read'})
        offset = end
    return {'contract': 'handoff-native-v2-observation/1', 'host': 'codex',
            'capture_thread_id': SESSION, 'prepare_result': prepared, 'read_results': pages}


class ObservationV2Tests(unittest.TestCase):
    def setUp(self):
        self.raw = 'Synthetic handoff: next verify 中文🙂.\n'.encode('utf-8')
        self.receipt = capture(self.raw)

    def check(self, receipt=None, raw=None):
        return observe(self.receipt if receipt is None else receipt, session=SESSION,
                       exported_bytes=self.raw if raw is None else raw, continuation='continue')

    def test_matched_content_never_claims_execution_authenticity_or_freshness(self):
        result = self.check()
        self.assertEqual(result['state'], 'captured_content_matches_export')
        self.assertFalse(result['host_execution_verified'])
        self.assertEqual(result['receipt_authenticity'], 'unverified')
        self.assertEqual(result['binding_freshness'], 'unknown')
        self.assertIsNone(result['scheduled'])
        self.assertIsNone(result['completed'])
        self.assertFalse(result['action_authorized'])

    def test_utf8_byte_pages_and_exact_digest(self):
        chunks = ['甲🙂', '乙\n', 'last']
        raw = ''.join(chunks).encode('utf-8')
        result = self.check(capture(raw, chunks=chunks), raw)
        self.assertEqual(result['bytes'], len(raw))
        self.assertEqual(result['pages'], 3)

    def test_cross_thread_wrong_id_or_stale_page_rejected(self):
        mutations = [lambda x: x.update(capture_thread_id=OTHER_SESSION),
                     lambda x: x['prepare_result']['prepared'].update(thread_id=OTHER_SESSION),
                     lambda x: x['read_results'][0]['handoff_page'].update(handoff_id=OTHER_HANDOFF),
                     lambda x: x['read_results'][0]['handoff_page'].update(source_message_id='new-message')]
        for mutate in mutations:
            value = copy.deepcopy(self.receipt); mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.check(value)

    def test_tampered_digest_or_bytes_rejected(self):
        for target in ['binding', 'page', 'text']:
            value = copy.deepcopy(self.receipt)
            if target == 'binding': value['prepare_result']['prepared']['handoff_sha1'] = '0'*40
            elif target == 'page': value['read_results'][0]['handoff_page']['content_sha1'] = '0'*40
            else: value['read_results'][0]['handoff_page']['text'] = 'x' * len(self.raw)
            with self.subTest(target=target), self.assertRaises(ValueError): self.check(value)

    def test_incomplete_gapped_reordered_pages_rejected(self):
        raw = b'abcdef'
        original = capture(raw, chunks=['ab', 'cd', 'ef'])
        bad = []
        x=copy.deepcopy(original);x['read_results'].pop();bad.append(x)
        x=copy.deepcopy(original);x['read_results'][1]['handoff_page']['offset']=3;bad.append(x)
        x=copy.deepcopy(original);x['read_results'].reverse();bad.append(x)
        x=copy.deepcopy(original);x['read_results'][0]['handoff_page']['next_offset']=True;bad.append(x)
        for value in bad:
            with self.assertRaises(ValueError): self.check(value, raw)

    def test_unknown_contract_legacy_path_or_schema_rejected(self):
        for mutate in [lambda x:x.update(contract='native-v1'),
                       lambda x:x['prepare_result']['prepared'].update(handoff_path='/synthetic/path'),
                       lambda x:x['prepare_result']['prepared'].update(schema_version=True)]:
            value=copy.deepcopy(self.receipt);mutate(value)
            with self.assertRaises(ValueError):self.check(value)

    def test_unconfirmed_or_inactive_prepare_is_historical_only(self):
        for kwargs in [{'durability':'visible_unconfirmed'},{'active':False},{'current':False}]:
            result=self.check(capture(self.raw, **kwargs))
            self.assertFalse(result['captured_prepare_ready'])
            self.assertFalse(result['action_authorized'])
            self.assertEqual(result['binding_freshness'],'unknown')

    def test_contradictory_prepare_and_future_flags_rejected(self):
        for change in [{'compaction_started':True},{'snapshot_saved':False},
                       {'compaction_ready':False},{'reconcile_required':True},
                       {'requesting_turn_active':1}]:
            value=copy.deepcopy(self.receipt);value['prepare_result'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):self.check(value)

    def test_page_json_escape_bound_and_raw_page_bound(self):
        raw=b'\x01'*1500
        with self.assertRaisesRegex(ValueError,'Serialized'):self.check(capture(raw),raw)
        raw=b'x'*4097
        with self.assertRaisesRegex(ValueError,'Page exceeds'):self.check(capture(raw),raw)

    def test_native_page_cap_is_stricter_than_tool_envelope_cap(self):
        raw=b'x'*1000
        receipt=capture(raw)
        source='\x01'*1000
        receipt['prepare_result']['prepared']['source_message_id']=source
        receipt['read_results'][0]['handoff_page']['source_message_id']=source
        with self.assertRaisesRegex(ValueError,'Serialized page'):
            self.check(receipt,raw)

    def test_max_export_with_escaped_metadata_and_small_pages(self):
        raw=b'\x01'*32768
        receipt=capture(raw,chunks=['\x01'*64]*512)
        source='\x02'*1024
        receipt['prepare_result']['prepared']['source_message_id']=source
        for result in receipt['read_results']:
            result['handoff_page']['source_message_id']=source
        result=self.check(receipt,raw)
        self.assertEqual(result['pages'],512)
        self.assertFalse(result['host_execution_verified'])

    def test_export_limit_and_invalid_utf8_rejected(self):
        for raw in [b'', b' '*10, b'x'*32769, b'\xff']:
            with self.assertRaises(ValueError):self.check(raw=raw)

    def test_v1_export_packet_bytes_accepted_without_changing_exporter(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'HANDOFF_STATE_DIR':d}):
            _, raw=handoff.packet('codex',SESSION,dict(mode='C',continuation='continue',handoff='Synthetic work done; continue next step.',instructions='Keep reasons.'),1)
            result=self.check(capture(raw),raw)
            self.assertEqual(result['content_sha1'],hashlib.sha1(raw).hexdigest())

    def test_cli_end_to_end_and_bad_receipt_exit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); export=root/'handoff.md';receipt=root/'capture.json'
            export.write_bytes(self.raw);receipt.write_text(json.dumps(self.receipt))
            command=[sys.executable,'-m','handoff_core.observation_v2','--session',SESSION,
                     '--export',str(export),'--receipt',str(receipt),'--continuation','continue']
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(json.loads(result.stdout)['host_execution_verified'])
            receipt.write_text('{}')
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,1)


if __name__ == '__main__':
    unittest.main()
