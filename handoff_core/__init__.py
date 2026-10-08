"""Draft/export support, not a live Codex compaction implementation."""
import hashlib
import json
import os
from pathlib import Path


def read(host, command, arguments=()):
    """Inspect only explicitly selected files. Snapshot freshness is unknown.

    An observation is untrusted metadata, never evidence that compaction ran.
    Binding integrity is independently checked against an explicit binding file.
    No session discovery, credentials, subprocesses, or network calls are used.
    """
    if host != 'codex' or command != 'show' or len(arguments) != 1:
        raise ValueError('Only an explicit Codex show observation is supported')
    selected = os.environ.get('HANDOFF_OBSERVATION_FILE')
    if not selected:
        raise ValueError('HANDOFF_OBSERVATION_FILE must select an observation snapshot')
    path = Path(selected)
    if not path.is_absolute():
        raise ValueError('Observation path must be absolute')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('host') != host or data.get('session') != arguments[0]:
        raise ValueError('Observation host/session does not match the full requested identity')
    # Never trust a caller-supplied "verified" marker or scheduling claim.
    observed = {key: data.get(key) for key in ('host', 'session', 'bytes', 'modified', 'context_tokens', 'last_compaction')}
    observed['handoffs'] = []
    binding_file = os.environ.get('HANDOFF_BINDING_FILE')
    if binding_file:
        binding_path = Path(binding_file)
        if not binding_path.is_absolute():
            raise ValueError('Binding path must be absolute')
        binding = json.loads(binding_path.read_text(encoding='utf-8'))
        if not isinstance(binding, dict) or binding.get('thread_id') != arguments[0]:
            raise ValueError('Binding identity differs from selected session')
        target = Path(binding['handoff_path'])
        # Bindings are data, not authority to read arbitrary filesystem paths.
        # This standalone adapter accepts only snapshots beside the binding.
        if not target.is_absolute() or not target.resolve().is_relative_to(binding_path.parent.resolve()):
            raise ValueError('Binding snapshot must be contained in the selected binding directory')
        raw = target.read_bytes()
        digest = hashlib.sha1(raw).hexdigest()
        if digest != binding.get('handoff_sha1'):
            raise ValueError('Binding snapshot fingerprint differs')
        observed['handoffs'] = [{'path': str(target), 'handoff_sha1': digest,
            'continuation': binding.get('continuation'), 'binding_integrity': 'verified',
            'binding_freshness': 'unknown'}]
    return observed
