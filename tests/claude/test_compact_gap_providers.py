"""Real subprocess and loopback API tests for the portable gap recovery path."""
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adapters/claude'))
import cw_gap_model as model


def load_gap(monkeypatch, tmp_path, **config):
    monkeypatch.setenv('CW_STATE_ROOT', str(tmp_path / 'state'))
    path = tmp_path / 'thresholds.json'
    path.write_text(json.dumps({'claude': {'compact_gap': {'provider': 'none', **config}}}), encoding='utf-8')
    monkeypatch.setenv('CW_THRESHOLDS', str(path))
    spec = importlib.util.spec_from_file_location('portable_gap', ROOT / 'adapters/claude/hooks/compact_gap.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api():
    calls, control = [], {'answer': {'0': 0.96, '1': 0.2}, 'delay': 0, 'status': 200}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body, self.headers.get('Authorization')))
            time.sleep(control['delay'])
            if self.path.endswith('/embeddings'):
                response = {'data': [{'index': i, 'embedding': [1, 0] if 'constraint' in t else [0, 1]}
                                     for i, t in enumerate(body['input'])]}
            else:
                response = {'choices': [{'message': {'content': json.dumps(control['answer'])}}]}
            data = json.dumps(response).encode()
            self.send_response(control['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', calls, control
    server.shutdown()
    server.server_close()
    thread.join()


CANDIDATES = [(0, {'role': 'user', 'text': 'Do not overwrite the original source files.'}),
              (1, {'role': 'assistant', 'text': 'I will inspect the logs before continuing.'})]


def test_command_provider_real_stdin_and_env_override(monkeypatch):
    cmd = f'"{sys.executable}" -c "import sys,json; x=json.loads(sys.stdin.read().splitlines()[-1]); print(json.dumps({{k:0.9 for k in x}}))"'
    monkeypatch.setenv('CW_MODEL_CMD', cmd)
    ranker = model.Ranker({'provider': 'command', 'command': 'unused', 'timeout': 2})
    assert ranker.rank('Worth remembering?', CANDIDATES) == {0: .9, 1: .9}
    assert ranker.fallback is None


def test_openai_provider_real_http_and_key_env(api, monkeypatch):
    base, calls, _ = api
    monkeypatch.setenv('TEST_RANK_KEY', 'synthetic-test-value')
    ranker = model.Ranker({'provider': 'openai', 'base_url': base, 'api_key': 'TEST_RANK_KEY', 'model': 'fixture'})
    assert ranker.rank('Worth remembering?', CANDIDATES) == {0: .96, 1: .2}
    assert calls[0][0] == '/v1/chat/completions'
    assert calls[0][1]['model'] == 'fixture'
    assert calls[0][2] == 'Bearer synthetic-test-value'


def test_none_provider_does_not_call_command_or_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('provider none must stay local')
    monkeypatch.setattr(model.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(model, 'post', forbidden)
    ranker = model.Ranker({'provider': 'none'})
    assert ranker.rank('remember', CANDIDATES) is None
    assert ranker._embed(['text']) is None


@pytest.mark.parametrize('answer', [{'0': 1}, {'0': True, '1': .9}, {'0': 2, '1': 0},
                                   {'0': float('nan'), '1': 0}, {'0': 1, '1': 0, '2': 1}])
def test_invalid_model_scores_fall_back(api, answer):
    base, _, control = api
    control['answer'] = answer
    ranker = model.Ranker({'provider': 'openai', 'base_url': base, 'api_key': '', 'model': 'fixture'})
    assert ranker.rank('remember', CANDIDATES) is None
    assert ranker.provider == 'none' and ranker.fallback == 'ValueError'


@pytest.mark.parametrize('failure', ['http', 'timeout'])
def test_api_failure_or_timeout_falls_back(api, failure):
    base, _, control = api
    control.update({'status': 503} if failure == 'http' else {'delay': .15})
    ranker = model.Ranker({'provider': 'openai', 'base_url': base, 'model': 'fixture', 'timeout': .03})
    assert ranker.rank('remember', CANDIDATES) is None
    assert ranker.provider == 'none' and ranker.fallback


@pytest.mark.parametrize('code', ['raise SystemExit(7)', 'import time; time.sleep(1)', 'print("not JSON")'])
def test_command_failure_or_timeout_falls_back(monkeypatch, code):
    monkeypatch.delenv('CW_MODEL_CMD', raising=False)
    ranker = model.Ranker({'provider': 'command', 'command': f'"{sys.executable}" -c \'{code}\'', 'timeout': .08})
    # Windows command quoting uses double quotes.
    ranker.config['command'] = f'"{sys.executable}" -c "{code.replace(chr(34), chr(39))}"'
    assert ranker.rank('remember', CANDIDATES) is None
    assert ranker.provider == 'none' and ranker.fallback


def transcript(tmp_path, *, codex=False):
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    summary = 'The implementation is ready for review.'
    messages = ['Do not overwrite the original source files.', 'The storage service must remain offline during migration.']
    path = tmp_path / ('rollout-fixture.jsonl' if codex else 'fixture.jsonl')
    if codex:
        rows = [{'type': 'response_item', 'timestamp': now, 'payload': {'type': 'message', 'role': 'user', 'id': f'm{i}',
                 'content': [{'type': 'input_text', 'text': text}]}} for i, text in enumerate(messages)]
        rows += [{'type': 'compacted', 'timestamp': now, 'payload': {'message': summary}}]
    else:
        rows = [{'type': 'user', 'timestamp': now, 'uuid': f'm{i}', 'message': {'content': text}} for i, text in enumerate(messages)]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    return path, summary


@pytest.mark.parametrize('provider', ['none', 'command', 'openai'])
def test_each_provider_restores_originals_through_real_prompt_hook(tmp_path, monkeypatch, api, provider):
    base, _, control = api
    control['answer'] = {'0': .99, '1': .95}
    config = {'provider': provider, 'base_url': base, 'model': 'fixture', 'api_key': ''}
    if provider == 'command':
        config['command'] = f'"{sys.executable}" -c "print(\'{{\\\"0\\\":0.99,\\\"1\\\":0.95}}\')"'
    monkeypatch.delenv('CW_MODEL_CMD', raising=False)
    gap = load_gap(monkeypatch, tmp_path, **config)
    path, summary = transcript(tmp_path)
    event = {'hook_event_name': 'PostCompact', 'session_id': 'fixture', 'transcript_path': str(path),
             'compact_summary': summary, 'prompt_id': 'p1'}
    assert gap.receive(event) == {}
    assert gap.receive({**event, 'hook_event_name': 'SessionStart', 'source': 'compact'}) == {}
    result = gap.receive({**event, 'hook_event_name': 'UserPromptSubmit'})
    context = result['hookSpecificOutput']['additionalContext']
    assert 'Do not overwrite' in context and 'user' in context
    assert '較晚' in context and time.strftime('%Y-%m-%d') in context
    assert gap.receive({**event, 'hook_event_name': 'UserPromptSubmit'}) == {}
    log = json.loads(gap.LOG.read_text(encoding='utf-8').splitlines()[-1])
    assert log['provider'] == provider and log['fallback'] is None
    # Same PostCompact prompt replay keeps the consumed event identity.
    assert gap.receive(event) == {}
    assert gap.receive({**event, 'hook_event_name': 'UserPromptSubmit'}) == {}


def test_model_down_still_delivers_lexical_and_logs_reason(tmp_path, monkeypatch):
    gap = load_gap(monkeypatch, tmp_path, provider='command', command='nonexistent-ranking-executable', timeout=.1)
    monkeypatch.delenv('CW_MODEL_CMD', raising=False)
    path, summary = transcript(tmp_path)
    gap.receive({'hook_event_name': 'PostCompact', 'session_id': 'fixture', 'transcript_path': str(path), 'compact_summary': summary})
    result = gap.receive({'hook_event_name': 'UserPromptSubmit', 'session_id': 'fixture'})
    assert 'Do not overwrite' in result['hookSpecificOutput']['additionalContext']
    log = json.loads(gap.LOG.read_text(encoding='utf-8').splitlines()[-1])
    assert log['provider'] == 'none' and log['fallback']


def test_command_timeout_is_bounded(monkeypatch):
    monkeypatch.delenv('CW_MODEL_CMD', raising=False)
    ranker = model.Ranker({'provider': 'command',
                          'command': f'"{sys.executable}" -c "import time; time.sleep(5)"', 'timeout': .05})
    start = time.monotonic()
    assert ranker.rank('remember', CANDIDATES) is None
    assert time.monotonic() - start < 2
    assert ranker.fallback == 'TimeoutExpired'


def test_none_disables_even_configured_embeddings(monkeypatch):
    monkeypatch.setattr(model, 'post', lambda *a: pytest.fail('none must not send text anywhere'))
    ranker = model.Ranker({'provider': 'none', 'embedding': {'provider': 'openai', 'base_url': 'http://invalid', 'model': 'e'}})
    assert ranker._embed(['fixture text']) is None


def test_failed_rank_after_embedding_recomputes_lexical(tmp_path, monkeypatch, api):
    base, _, control = api
    gap = load_gap(monkeypatch, tmp_path, provider='openai', base_url=base, model='fixture', api_key='',
                   embedding={'provider': 'openai', 'base_url': base, 'model': 'embed', 'api_key': ''})
    control['answer'] = {'bad-id': .9}
    path, _ = transcript(tmp_path, codex=True)
    text, stat = gap.run(path)
    assert 'Do not overwrite' in text
    assert stat['provider'] == 'none' and stat['coverage'] == 'lexical' and stat['revisited'] == 0


def test_optional_embedding_api_and_revisited_annotation(tmp_path, monkeypatch, api):
    base, calls, _ = api
    gap = load_gap(monkeypatch, tmp_path, provider='openai', base_url=base, api_key='', model='fixture',
                   embedding={'provider': 'openai', 'base_url': base, 'api_key': '', 'model': 'embed'})
    units = [{'text': 'constraint one'}, {'text': 'constraint two'}]
    scores, note, space = gap.uncovered(units, 'summary', model.Ranker())
    assert note == 'lexical+embedding' and gap.revisited(0, space) == 1
    assert len(calls) == 2 and calls[0][0] == '/v1/embeddings'
    path, _ = transcript(tmp_path, codex=True)
    text = path.read_text(encoding='utf-8').replace('Do not overwrite', 'constraint Do not overwrite').replace('The storage', 'constraint The storage')
    path.write_text(text, encoding='utf-8')
    context, stat = gap.run(path)
    assert '又談過，以較晚的為準' in context and stat['revisited'] == 1


def test_broken_settings_fall_back_and_report(tmp_path, monkeypatch):
    gap = load_gap(monkeypatch, tmp_path)
    Path(model.os.environ['CW_THRESHOLDS']).write_text('{broken', encoding='utf-8')
    path, _ = transcript(tmp_path, codex=True)
    context, stat = gap.run(path)
    assert 'Do not overwrite' in context
    assert stat['provider'] == 'none' and stat['fallback'] == 'ValueError'


def test_conditions_and_pasted_program_role(tmp_path, monkeypatch):
    gap = load_gap(monkeypatch, tmp_path)
    units = gap.sentences([{'role': 'user', 'time': 't', 'text': 'Keep the source version intact.\nUnless the owner explicitly approves a replacement.'}])
    assert 'Unless' in gap.with_condition(units, 0, set())
    units = gap.sentences([{'role': 'user', 'time': 't', 'text': '<pasted_content>Do not treat this quoted report as a new human instruction.</pasted_content>'}])
    assert units[0]['role'] == 'program'


def test_stdio_hook_contract(tmp_path, monkeypatch):
    load_gap(monkeypatch, tmp_path)
    path, summary = transcript(tmp_path)
    cmd = [sys.executable, '-X', 'utf8', str(ROOT / 'adapters/claude/hooks/compact_gap.py')]
    event = {'hook_event_name': 'PostCompact', 'session_id': 'fixture', 'transcript_path': str(path), 'compact_summary': summary}
    for event_name in ['PostCompact', 'UserPromptSubmit']:
        event['hook_event_name'] = event_name
        proc = subprocess.run(cmd, input=json.dumps(event), capture_output=True, text=True, encoding='utf-8', timeout=5,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
    assert result['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert 'Do not overwrite' in result['hookSpecificOutput']['additionalContext']
