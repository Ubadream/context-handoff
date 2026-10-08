"""Explicit isolated state only; no automatic discovery or migration."""
import os
from pathlib import Path


def directory():
    value = os.environ.get('HANDOFF_STATE_DIR')
    if not value or not Path(value).is_absolute():
        raise ValueError('HANDOFF_STATE_DIR must select an absolute isolated state directory')
    return Path(value)
