"""Exercise the actual CLI/hook entry points using synthetic, ASCII-only inputs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / 'adapters/claude'
HAN = re.compile(r'[\u4e00-\u9fff]')


def environment(tmp_path, language=None):
    env = {**os.environ, 'CW_STATE_ROOT': str(tmp_path / 'state'),
           'CW_THRESHOLDS': str(tmp_path / 'thresholds.json'),
           'CLAUDE_HOME': str(tmp_path / 'claude'), 'CW_CLAUDE_HOME': str(tmp_path / 'claude'),
           'CW_L2': str(tmp_path / 'missing-model.py'), 'PYTHONUTF8': '1'}
    env.pop('CW_MODEL_CMD', None)
    env.pop('CW_LANG', None)
    if language is not None:
        env['CW_LANG'] = language
    return env


def run(tmp_path, script, *args, language=None, event=None):
    return subprocess.run([sys.executable, '-X', 'utf8', str(ADAPTER / script), *args],
                          input=json.dumps(event) if event else None, capture_output=True,
                          text=True, encoding='utf-8', env=environment(tmp_path, language), timeout=30)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def assert_language(text, language):
    assert text.strip()
    assert bool(HAN.search(text)) == (language == 'zh'), text


@pytest.mark.parametrize('language', ['en', 'zh'])
@pytest.mark.parametrize('tokens', [260_000, 420_000, 810_000])
def test_context_watch_injection(tmp_path, language, tokens):
    path = tmp_path / 'transcript.jsonl'
    write_rows(path, [{'type': 'assistant', 'message': {'usage': {'input_tokens': tokens}}}])
    (tmp_path / 'thresholds.json').write_text('{"claude":{"handoff_sections":true}}', encoding='utf-8')
    result = run(tmp_path, 'hooks/context_watch.py', language=language,
                 event={'hook_event_name': 'PostToolUse', 'session_id': 'fixture', 'transcript_path': str(path)})
    assert result.returncode == 0, result.stderr
    assert_language(json.loads(result.stdout)['hookSpecificOutput']['additionalContext'], language)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_handoff_bind_injection(tmp_path, language):
    handoff = tmp_path / 'HANDOFF_fixture.md'
    handoff.write_text('Completed fixture; next step is review.', encoding='utf-8')
    path = tmp_path / 'transcript.jsonl'
    write_rows(path, [{'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'Write', 'input': {'file_path': str(handoff)}}]}}])
    result = run(tmp_path, 'hooks/handoff_bind.py', language=language,
                 event={'source': 'resume', 'transcript_path': str(path)})
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert_language(output['hookSpecificOutput']['additionalContext'], language)
    assert_language(output['systemMessage'], language)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_compact_gap_injection(tmp_path, language):
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    path = tmp_path / 'transcript.jsonl'
    write_rows(path, [{'type': 'user', 'uuid': 'fixture', 'timestamp': now,
                      'message': {'content': 'Do not overwrite the original source files.'}}])
    (tmp_path / 'thresholds.json').write_text('{"claude":{"compact_gap":{"provider":"none"}}}', encoding='utf-8')
    event = {'hook_event_name': 'PostCompact', 'session_id': 'fixture', 'transcript_path': str(path),
             'compact_summary': 'Implementation completed.', 'prompt_id': 'fixture'}
    assert run(tmp_path, 'hooks/compact_gap.py', language=language, event=event).returncode == 0
    result = run(tmp_path, 'hooks/compact_gap.py', language=language, event={**event, 'hook_event_name': 'UserPromptSubmit'})
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
    assert 'Do not overwrite' in context
    assert_language(context, language)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_handoff_check_and_thresholds(tmp_path, language):
    path = tmp_path / 'claude/projects/fixture/session.jsonl'
    write_rows(path, [{'type': 'user', 'uuid': 'fixture', 'message': {'content': 'Review this work.'}}])
    for script, args in [('cw_handoff_check.py', ('session', '--no-model', '--json')),
                         ('cw_thresholds.py', ('show',))]:
        result = run(tmp_path, script, *args, language=language)
        assert result.returncode == 0, result.stderr
        assert_language(result.stdout, language)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_doctor_missing_and_existing_settings(tmp_path, language):
    result = run(tmp_path, 'cw_install.py', 'doctor', language=language)
    assert result.returncode == 1
    assert_language(result.stdout, language)
    (tmp_path / 'claude').mkdir()
    (tmp_path / 'claude/settings.json').write_text('{}', encoding='utf-8')
    result = run(tmp_path, 'cw_install.py', 'doctor', language=language)
    assert result.returncode == 1, result.stderr
    assert_language(result.stdout, language)
    if language == 'en':
        assert 'not connected' in result.stdout and 'missing file' in result.stdout and 'off' in result.stdout


def test_setting_roundtrip_and_env_precedence(tmp_path):
    config = tmp_path / 'thresholds.json'
    config.write_text('{"other":17,"claude":{"handoff_sections":true,"urgent":900000}}', encoding='utf-8')
    result = run(tmp_path, 'cw_thresholds.py', 'set', '--language', 'en')
    assert result.returncode == 0, result.stderr
    assert_language(result.stdout, 'en')
    assert json.loads(config.read_text()) == {'other': 17, 'claude': {'handoff_sections': True, 'urgent': 900000, 'language': 'en'}}
    assert json.loads(result.stdout)['language']['value'] == 'en'
    assert_language(run(tmp_path, 'cw_thresholds.py', 'show', language='zh').stdout, 'zh')
    result = run(tmp_path, 'cw_thresholds.py', 'set', '--language', 'zh')
    assert result.returncode == 0
    assert_language(result.stdout, 'zh')
    config.write_text('not json', encoding='utf-8')
    assert_language(run(tmp_path, 'cw_thresholds.py', 'show').stdout, 'zh')
    assert_language(run(tmp_path, 'cw_thresholds.py', 'show', language='en').stdout, 'en')


def test_install_language_is_optional_and_dry_run_does_not_write(tmp_path, monkeypatch):
    sys.path.insert(0, str(ADAPTER))
    import cw_install
    import cw_thresholds
    monkeypatch.setenv('CW_THRESHOLDS', str(tmp_path / 'thresholds.json'))
    monkeypatch.setattr(cw_thresholds, 'THRESHOLDS', tmp_path / 'thresholds.json')
    monkeypatch.setattr(cw_install, 'SETTINGS', tmp_path / 'claude/settings.json')
    monkeypatch.setattr(cw_install, 'OLD_HOOKS', tmp_path / 'claude/hooks')
    monkeypatch.setattr(cw_install, 'plugin_state', lambda: (str(cw_install.HERE), 'fixture', 'fixture'))
    monkeypatch.setattr(cw_install, 'claude', lambda *args: pytest.fail('must not install real plugins'))
    cw_install.apply(dry=True, language='en')
    assert not cw_thresholds.THRESHOLDS.exists() and not cw_install.SETTINGS.exists()
    cw_install.apply(dry=False, language='en')
    assert json.loads(cw_thresholds.THRESHOLDS.read_text())['claude']['language'] == 'en'
    cw_install.apply(dry=False)
    assert json.loads(cw_thresholds.THRESHOLDS.read_text())['claude']['language'] == 'en'


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_all_cli_help_and_read_only_messages(tmp_path, language):
    for script in ['cw_claude.py', 'cw_get.py', 'cw_handoff_check.py', 'cw_install.py', 'cw_sections.py', 'cw_segments.py', 'cw_thresholds.py']:
        result = run(tmp_path, script, '--help', language=language)
        assert result.returncode == 0, result.stderr
        assert_language(result.stdout, language)
    for script, args in [('cw_claude.py', ('capabilities',)), ('cw_diet.py', ('show',)),
                         ('cw_install.py', ('apply', '--dry-run'))]:
        result = run(tmp_path, script, *args, language=language)
        assert result.returncode == 0, result.stderr
        assert_language(result.stdout, language)


def test_format_and_prompt_languages(tmp_path, monkeypatch):
    sys.path.insert(0, str(ADAPTER))
    import importlib
    import cw_handoff_check as check
    import cw_segments
    monkeypatch.setenv('CW_LANG', 'en')
    importlib.reload(check)
    importlib.reload(cw_segments)
    assert_language(check.PROMPT, 'en')
    assert_language(cw_segments.PROMPT, 'en')
    assert check.format_warnings('## Decisions\n10:00 "ready"\n## Feelings\nnone\n## Work progress\ndone') == []
    assert check.format_warnings('## 決策\n10:00「ready」\n## 情感\n無\n## 工作進度\ndone') == []
    assert_language('\n'.join(check.format_warnings('missing headings')), 'en')
    monkeypatch.setenv('CW_LANG', 'zh')
    importlib.reload(check)
    importlib.reload(cw_segments)
