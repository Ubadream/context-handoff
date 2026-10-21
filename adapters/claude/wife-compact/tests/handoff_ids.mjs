// node adapters/claude/wife-compact/tests/handoff_ids.mjs
// Run the real hook handlers and handoff checker; only the snapshot CLI and native summarizer are fixtures.
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const workbench = fileURLToPath(new URL('../../../../', import.meta.url));
const fixtureParent = resolve(tmpdir());
const root = mkdtempSync(join(fixtureParent, 'cw-handoff-ids-'));
const flush = () => new Promise(setImmediate);
let serial = 0;

async function fixture({ prepare = 'success', spawn = 'success', stale = false } = {}) {
  const directory = join(root, `${++serial} 交接 fixture`);
  const session = 'abcd1234-0000-4000-8000-000000000068';
  const transcript = join(directory, 'claude', 'projects', 'fixture', `${session}.jsonl`);
  const handoff = join(directory, 'HANDOFF_交接.md');
  const entry = join(directory, prepare === 'missing' ? 'missing.py' : 'handoff_ids.py');
  const log = join(directory, 'calls.jsonl');
  mkdirSync(dirname(transcript), { recursive: true });
  writeFileSync(handoff, '# 決策\n22:00「接上壓縮」\n# 情感\n無\n# 工作進度\n已寫交接\n');
  const rows = [{ type: 'assistant', timestamp: '2026-10-17T14:00:00Z',
    message: { content: [{ type: 'tool_use', name: 'Write', input: { file_path: handoff } }] } }];
  if (stale) rows.push({ type: 'system', subtype: 'compact_boundary', timestamp: '2026-10-17T14:01:00Z' });
  writeFileSync(transcript, rows.map((row) => JSON.stringify(row)).join('\n') + '\n');
  if (prepare !== 'missing') writeFileSync(entry, `import json, sys, time
from pathlib import Path
log = Path(${JSON.stringify(log)})
with log.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
command = sys.argv[1]
mode = ${JSON.stringify(prepare)} if command == 'prepare' else ${JSON.stringify(spawn)}
if mode == 'disabled':
    print('DISABLED')
    sys.exit(0)
if mode == 'exit2':
    print('fixture failure', file=sys.stderr)
    sys.exit(2)
if mode == 'timeout':
    time.sleep(60)
if mode == 'empty':
    sys.exit(0)
if command == 'prepare':
    handoff = Path(sys.argv[sys.argv.index('--handoff') + 1])
    session = Path(sys.argv[sys.argv.index('--session-file') + 1])
    assert handoff.is_file() and session.is_file()
    sidecar = handoff.with_suffix('.sources') / 'fixture.json'
    sidecar.parent.mkdir(exist_ok=True)
    sidecar.write_text(json.dumps({'session': str(session), 'handoff': str(handoff)}), encoding='utf-8')
    print('fixture prepare log')
    print(sidecar.resolve())
elif command == 'spawn-snapshot':
    assert Path(sys.argv[sys.argv.index('--sidecar') + 1]).is_file()
    print('68001')  # Contract: return the PID, never run/wait for snapshot here.
else:
    sys.exit(2)
`);
  const env = { ...process.env, CLAUDE_HOME: join(directory, 'claude'), CW_WORKBENCH: workbench,
    CW_STATE_ROOT: join(directory, 'state'), CW_ORIGINALS: join(directory, 'originals'),
    USERPROFILE: directory, CW_PYTHON: process.env.CW_TEST_PYTHON || 'python', WM_HANDOFF_IDS_ENTRY: entry };
  const hooks = {}, calls = [], events = [], ui = [];
  let facts;
  const nativeResult = { messages: [{ role: 'user', text: 'fixture native summary', toolUses: [] }] };
  const host = { env, calls, events, ui, nativeResult, native: async () => nativeResult };
  const $ = {
    env: { get: async (key) => env[key] },
    fs: { exists: async (path) => existsSync(path), read: async (path) => readFileSync(path, 'utf8'),
      write: async (path, text) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, text); } },
    ui: { log: (text) => ui.push(text), toast: (text) => ui.push(text) },
    tool: { register: async (spec) => ({ tool: `mcp__wife-compact__${spec.name}` }) },
    session: { id: async () => session, compact: async ({ instructions }) => host.compact({ instructions }) },
    process: { run: async (argv, options) => {
      calls.push({ argv, options });
      if (argv[3].endsWith('cw_sections.py')) return { exitCode: 0, stdout: '', stderr: '' };
      if (argv[3] === entry) events.push(argv[4]);
      let result;
      try {
        const out = await execute(argv[0], argv.slice(1), { cwd: options.cwd, env,
          timeout: options.timeoutMs, windowsHide: true, encoding: 'utf8' });
        result = { exitCode: 0, ...out };
      } catch (err) {
        if (err.killed) throw new Error(`fixture process timed out after ${options.timeoutMs} ms`);
        result = { exitCode: err.code, stdout: err.stdout || '', stderr: err.stderr || err.message };
      }
      if (argv[3].endsWith('cw_handoff_check.py') && result.exitCode === 0) facts = JSON.parse(result.stdout).facts;
      return result;
    } },
  };
  const { register } = await import(`../hooks/wife-compact.ts?fixture=${serial}`);
  register((name, handler) => { hooks[name] = handler; });
  await hooks['session.start']($, {}, async () => ({}));
  host.schedule = (mode = 'A', extra = {}) => hooks['tool.call']($,
    { tool: 'mcp__wife-compact__compact', mode, reason: 'fixture', ...extra }, async () => ({}));
  host.compact = (extra = {}) => hooks['session.compact']($,
    { trigger: 'manual', messages: [{ role: 'user', text: 'fixture original', toolUses: [] }], ...extra },
    async (event) => { events.push('native'); return host.native(event); });
  host.turn = () => hooks['turn.complete']($, { reason: 'answer' }, async () => ({}));
  host.cliCalls = () => existsSync(log) ? readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse) : [];
  host.trace = async () => { await flush(); return readFileSync(join(env.CW_STATE_ROOT, 'wife-compact', 'trace.log'), 'utf8'); };
  host.facts = () => facts;
  host.paths = { transcript, handoff, entry };
  return host;
}

try {
  for (const mode of ['A', 'C']) await test(`${mode}: prepare uses facts; snapshot only after completed compaction`, async () => {
    const host = await fixture();
    const scheduled = await host.schedule(mode);
    assert.equal(scheduled.deny, undefined);
    assert.match(scheduled.result, /原文範圍已存：/);
    assert.equal(host.facts().session_file, host.paths.transcript);
    const [prepared] = host.cliCalls();
    assert.deepEqual(prepared, ['prepare', '--session-file', host.paths.transcript, '--handoff', host.facts().handoffs[0].path]);
    const sidecar = scheduled.result.match(/原文範圍已存：([^\n]+)/)[1];
    assert.ok(existsSync(sidecar));
    let finish;
    let started;
    const nativeStarted = new Promise((resolve) => { started = resolve; });
    host.native = () => new Promise((resolve) => { finish = resolve; started(); });
    const running = host.turn();
    await Promise.race([nativeStarted, running.then(() => assert.fail('compaction did not reach native summarizer'))]);
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare']);
    finish(host.nativeResult);
    await running;
    assert.deepEqual(host.cliCalls()[1], ['spawn-snapshot', '--sidecar', sidecar]);
    assert.deepEqual(host.events, ['prepare', 'native', 'spawn-snapshot']);
    assert.deepEqual(host.calls.filter(({ argv }) => argv[3] === host.paths.entry)
      .map(({ options }) => options.timeoutMs), [15000, 10000]);
    assert.match(await host.trace(), /handoff_ids spawn-snapshot 已交出：68001/);
  });

  for (const prepare of ['disabled', 'exit2', 'timeout', 'missing', 'empty']) await test(`prepare ${prepare}: compaction remains available`, async () => {
    const host = await fixture({ prepare });
    const scheduled = await host.schedule();
    assert.equal(scheduled.deny, undefined);
    assert.match(scheduled.result, /已排定/);
    assert.doesNotMatch(scheduled.result, /原文範圍已存：/);
    if (['exit2', 'timeout', 'empty'].includes(prepare)) {
      assert.match(scheduled.result, /軟性提醒（不擋壓縮）：原文範圍沒存成/);
      assert.match(scheduled.result, { exit2: /exit 2：fixture failure/, timeout: /15000 ms/, empty: /sidecar 路徑/ }[prepare]);
    } else assert.doesNotMatch(scheduled.result, /DISABLED|原文範圍|軟性提醒/);
    assert.equal(await host.compact(), host.nativeResult);
    assert.equal(host.cliCalls().some((args) => args[0] === 'spawn-snapshot'), false);
  });

  for (const spawn of ['exit2', 'timeout', 'disabled']) await test(`spawn ${spawn}: trace only; no repeated compaction`, async () => {
    const host = await fixture({ spawn });
    await host.schedule();
    assert.equal(await host.compact(), host.nativeResult);
    assert.equal(host.events.filter((event) => event === 'native').length, 1);
    assert.equal(host.ui.some((text) => /失敗|DISABLED|timed out/.test(text)), false);
    assert.match(await host.trace(), spawn === 'disabled' ? /spawn-snapshot DISABLED/ : /spawn-snapshot 失敗（不影響壓縮）/);
  });

  for (const result of [{ skip: 'fixture skip' }, undefined]) await test(`no completed messages (${result?.skip || 'undefined'}): no snapshot`, async () => {
    const host = await fixture();
    await host.schedule();
    host.native = async () => result;
    assert.equal(await host.compact(), result);
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare']);
  });

  await test('cancel and replacement discard their previous sidecar', async () => {
    const host = await fixture();
    await host.schedule();
    await host.schedule('cancel');
    await host.compact();
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare']);
    await host.schedule();
    host.env.WM_HANDOFF_IDS_ENTRY = join(root, 'missing replacement.py');
    await host.schedule();
    await host.compact();
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare', 'prepare']);
  });

  await test('precompute/subagent events do not consume the sidecar; successful use runs once', async () => {
    const host = await fixture();
    await host.schedule();
    await host.compact({ trigger: 'precompute' });
    await host.compact({ agentId: 'fixture-agent' });
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare']);
    await host.compact();
    await host.compact();
    assert.deepEqual(host.cliCalls().map((args) => args[0]), ['prepare', 'spawn-snapshot']);
  });

  await test('stale handoff is still denied; excused compaction does not snapshot an old handoff', async () => {
    const host = await fixture({ stale: true });
    assert.match((await host.schedule()).deny, /先寫交接/);
    assert.equal(host.cliCalls().length, 0);
    assert.match((await host.schedule('A', { no_handoff_reason: 'fixture chat' })).result, /已排定/);
    await host.compact();
    assert.equal(host.cliCalls().length, 0);
  });

  await test('native fallback success still spawns; complete failure does not', async () => {
    for (const recovers of [true, false]) {
      const host = await fixture();
      await host.schedule();
      let attempts = 0;
      host.native = async () => {
        if (++attempts === 1 || !recovers) throw new Error('fixture native failure');
        return host.nativeResult;
      };
      if (recovers) assert.equal(await host.compact(), host.nativeResult);
      else await assert.rejects(host.compact(), /fixture native failure/);
      assert.deepEqual(host.cliCalls().map((args) => args[0]), recovers ? ['prepare', 'spawn-snapshot'] : ['prepare']);
    }
  });
} finally {
  await flush();
  assert.equal(dirname(root), fixtureParent); // Delete only this test's mkdtemp directory.
  rmSync(root, { recursive: true, force: true });
}
