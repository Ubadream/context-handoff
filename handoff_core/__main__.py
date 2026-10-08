"""Feature-scoped local draft and export CLI."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from . import drafts, handoff


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--observation', type=Path)
    parser.add_argument('--binding', type=Path)
    parser.add_argument('command', choices=['load', 'save', 'preview', 'export', 'status'])
    parser.add_argument('session')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args(argv)
    os.environ['HANDOFF_STATE_DIR'] = str(args.state_dir.resolve())
    for key, value in [('HANDOFF_OBSERVATION_FILE', args.observation), ('HANDOFF_BINDING_FILE', args.binding)]:
        if value:
            os.environ[key] = str(value.resolve())
        else:
            os.environ.pop(key, None)
    try:
        if args.command == 'load':
            result = drafts.load('codex', args.session)
        else:
            if args.candidate is None or args.expected_revision is None:
                parser.error('--candidate and --expected-revision are required')
            content = json.loads(args.candidate.read_text(encoding='utf-8'))
            fn = {'save': drafts.save, 'preview': drafts.preview, 'export': handoff.export, 'status': handoff.status}[args.command]
            result = fn('codex', args.session, content, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
