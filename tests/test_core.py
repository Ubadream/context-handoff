import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from handoff_core import drafts, handoff, read


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.observation = self.root / 'observation.json'
        self.observation.write_text(json.dumps({'host': 'codex', 'session': 'demo-session', 'context_tokens': 100}), encoding='utf-8')
        self.env = patch.dict(os.environ, {'HANDOFF_STATE_DIR': str(self.root/'state'), 'HANDOFF_OBSERVATION_FILE': str(self.observation)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.candidate = dict(mode='C', continuation='continue', handoff='Synthetic completed step; next verify result.', instructions='Preserve the reason.')

    def save(self):
        return drafts.save('codex', 'demo-session', self.candidate, 0)

    def test_absent_load_does_not_write(self):
        self.assertEqual(drafts.load('codex', 'demo-session')['state'], 'absent')
        self.assertFalse(drafts.database().exists())

    def test_idempotent_save_and_cas(self):
        self.save()
        self.assertEqual(drafts.save('codex', 'demo-session', self.candidate, 1)['revision'], 1)
        with self.assertRaises(drafts.Conflict):
            drafts.save('codex', 'demo-session', self.candidate, 0)

    def test_concurrent_writers(self):
        self.save()
        def attempt(value):
            try:
                return drafts.save('codex', 'demo-session', {**self.candidate, 'handoff': value}, 1)['revision']
            except drafts.Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(map(str, pool.map(attempt, ['first', 'second']))), ['2', 'conflict'])

    def test_export_idempotent_and_never_applied(self):
        self.save()
        result = handoff.export('codex', 'demo-session', self.candidate, 1)
        self.assertFalse(result['host_applied'])
        self.assertIsNone(result['scheduled'])
        self.assertEqual(result, handoff.export('codex', 'demo-session', self.candidate, 1))
        self.assertEqual(handoff.status('codex', 'demo-session', self.candidate, 1)['state'], 'binding_not_observed')

    def test_binding_integrity_not_freshness_or_completion(self):
        self.save()
        handoff.export('codex', 'demo-session', self.candidate, 1)
        _, raw = handoff.packet('codex', 'demo-session', self.candidate, 1)
        snapshot = self.root / 'snapshot.md'
        snapshot.write_bytes(raw)
        binding = self.root / 'binding.json'
        binding.write_text(json.dumps({'thread_id': 'demo-session', 'handoff_path': str(snapshot), 'handoff_sha1': hashlib.sha1(raw).hexdigest(), 'continuation': 'continue'}))
        os.environ['HANDOFF_BINDING_FILE'] = str(binding)
        result = handoff.status('codex', 'demo-session', self.candidate, 1)
        self.assertEqual(result['state'], 'binding_observed')
        self.assertEqual(result['binding_freshness'], 'unknown')
        self.assertIsNone(result['completed'])
        snapshot.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            handoff.status('codex', 'demo-session', self.candidate, 1)

    def test_tampered_export_not_overwritten(self):
        self.save()
        handoff.export('codex', 'demo-session', self.candidate, 1)
        path, _ = handoff.packet('codex', 'demo-session', self.candidate, 1)
        path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'readback'):
            handoff.export('codex', 'demo-session', self.candidate, 1)
        self.assertEqual(path.read_bytes(), b'tampered')

    def test_changed_during_observation(self):
        self.save()
        original = drafts.preview
        def race(*args):
            result = original(*args)
            drafts.save('codex', 'demo-session', {**self.candidate, 'handoff': 'new'}, 1)
            return result
        with patch.object(drafts, 'preview', race), self.assertRaises(drafts.Conflict):
            handoff.export('codex', 'demo-session', self.candidate, 1)
        self.assertFalse((self.root/'state'/'exports').exists())

    def test_invalid_and_oversized_candidates(self):
        for change in [{'mode': 'B'}, {'continuation': 'automatic'}, {'handoff': 'x'*32769}, {'instructions': None}, {'extra': 'field'}]:
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                drafts.save('codex', 'demo-session', {**self.candidate, **change}, 0)
        with self.assertRaisesRegex(ValueError, 'Combined'):
            handoff.packet('codex', 'demo-session', {**self.candidate, 'handoff': 'x'*32000, 'instructions': 'y'*1000}, 1)
        self.assertFalse(drafts.database().exists())

    def test_exact_identity_and_untrusted_verified_marker(self):
        with self.assertRaises(ValueError):
            read('codex', 'show', ['demo'])
        value = json.loads(self.observation.read_text())
        value['handoffs'] = [{'binding_integrity': 'verified'}]
        self.observation.write_text(json.dumps(value))
        self.assertEqual(read('codex', 'show', ['demo-session'])['handoffs'], [])

    def test_explicit_state_required(self):
        os.environ.pop('HANDOFF_STATE_DIR')
        with self.assertRaises(ValueError):
            drafts.load('codex', 'demo-session')

    def test_binding_path_cannot_escape_selected_directory(self):
        directory = self.root/'binding'
        directory.mkdir()
        binding = directory/'binding.json'
        binding.write_text(json.dumps({'thread_id': 'demo-session', 'handoff_path': str(self.observation)}))
        os.environ['HANDOFF_BINDING_FILE'] = str(binding)
        with self.assertRaisesRegex(ValueError, 'contained'):
            read('codex', 'show', ['demo-session'])


if __name__ == '__main__':
    unittest.main()
