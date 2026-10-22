// Registered host hooks with synthetic input; no Claude account, model, or installation.
import assert from 'node:assert/strict';
import { test } from 'node:test';

let serial = 0;
const HAN = /[\u4e00-\u9fff]/;
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };
const check = (value, language) => {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  assert.ok(text.length > 0);
  assert.equal(HAN.test(text), language === 'zh', text);
};

async function fixture({ configured = 'zh', override, broken = false } = {}) {
  const hooks = {}, tools = {}, writes = new Map(), prompts = [], timers = [], logs = [];
  const host = { tools, writes, prompts, timers, logs, configured, override, broken, fresh: true, fail: '', section: null };
  const state = '/fixture/state';
  const configPath = '/fixture/custom-thresholds.json';
  const $ = {
    env: { get: async (key) => ({ CW_WORKBENCH: '/fixture/repo', CW_STATE_ROOT: state,
      CW_THRESHOLDS: configPath, CW_LANG: host.override, USERPROFILE: '/fixture/home' })[key] },
    fs: {
      exists: async () => false,
      read: async (path) => {
        if (path === configPath || path === `${state}/context-workbench/thresholds.json`) {
          if (host.broken) throw new Error('fixture unreadable settings');
          return JSON.stringify({ claude: { language: host.configured, handoff_sections: true,
            idle: { enabled: true, tokens: 400000, minutes: 50 } } });
        }
        if (writes.has(path)) return writes.get(path);
        if (path.endsWith('segments/fixture.json')) return JSON.stringify({ segments: [
          { seq: 1, id: 'fixture#00000001', prompt: 'First request', opened_by: 'the user', label: { title: 'first', action: 'drop' } },
          { seq: 2, id: 'fixture#00000002', prompt: 'Second request', opened_by: 'the user' },
        ] });
        if (path.endsWith('.md')) return 'fixture report';
        throw new Error('fixture missing file');
      },
      write: async (path, text) => { writes.set(path, text); },
      list: async () => [{ name: 'fixture.json', mtimeMs: 1 }],
    },
    ui: { log: (text) => logs.push(text), toast: (text) => logs.push(text) },
    tool: { register: async (spec) => { tools[spec.name] = spec; return { tool: `fixture_${spec.name}` }; } },
    process: { run: async (argv) => {
      const script = argv.find((x) => x.endsWith('.py'))?.split('/').pop();
      if (host.fail === script) return { exitCode: 2, stdout: '', stderr: 'fixture failure' };
      let stdout = '/fixture/report.md';
      if (script === 'cw_handoff_check.py' && argv.includes('--json')) stdout = JSON.stringify({
        facts: { written_since_compaction: host.fresh }, verdict: 'fixture handoff status' });
      if (script === 'cw_sections.py') stdout = (host.override || host.configured) === 'en'
        ? 'Original fixture@20261022T100000: 2 sections' : '原文 fixture@20261022T100000：2 節';
      if (script === 'cw_get.py') stdout = 'fixture original text';
      if (script === 'cw_advice.py') stdout = JSON.stringify({ id: 'fixture', state: 'replied', replied_at: 'now' });
      return { exitCode: 0, stdout, stderr: '' };
    } },
    clock: { after: (_ms, fn) => { timers.push(fn); return () => {}; } },
    prompt: { submit: async ({ text }) => { prompts.push(text); } },
    session: {
      id: async () => 'fixture-session',
      usage: async () => ({ context: { tokens: 420000, breakdown: { categories: [{ name: 'tools', tokens: 1, isDeferred: true }],
        mcpTools: [{ name: 'fixture', tokens: 1, isLoaded: false }], skills: { includedSkills: 1, totalSkills: 2, tokens: 3 } } } }),
      compact: async () => host.compact(),
    },
  };
  const { register } = await import(`../hooks/wife-compact.ts?language=${++serial}`);
  register((name, handler) => { hooks[name] = handler; });
  host.$ = $;
  host.start = () => hooks['session.start']($, {}, async () => ({}));
  host.call = (name, args = {}) => hooks['tool.call']($, { tool: `fixture_${name}`, ...args }, async () => ({}));
  host.turn = () => hooks['turn.complete']($, { reason: 'answer' }, async () => ({}));
  host.compact = () => hooks['session.compact']($, { trigger: 'manual', messages: [
    { role: 'user', text: 'First request', toolUses: [] },
    { role: 'assistant', text: 'Completed step', toolUses: [{ tool: 'Read', tool_use_id: 't', input: { path: 'fixture.py' }, text: 'original tool output' }] },
    { role: 'user', text: 'Second request', toolUses: [] },
  ] }, async (e) => {
    host.section = e;
    return { messages: [{ role: 'user', text: 'native summary', toolUses: [] }] };
  });
  await host.start();
  return host;
}

for (const language of ['en', 'zh']) {
  await test(`${language}: tool descriptions, replies, instructions, archive and continuation`, async () => {
    const h = await fixture({ override: language });
    check(h.tools, language);
    for (const name of ['segments', 'prepare', 'context', 'handoff_check', 'advice_reply']) {
      check(await h.call(name), language);
    }
    check(await h.call('compact', { mode: 'bad' }), language);
    check(await h.call('compact', { mode: 'A', next: 'bad' }), language);
    h.fresh = false;
    check(await h.call('compact', { mode: 'A' }), language);
    h.fresh = true;
    for (const mode of ['A', 'C']) {
      check(await h.call('compact', { mode, reason: 'fixture reason', next: 'continue' }), language);
      await h.turn();
      check(h.section.instructions, language);
      check(h.section.messages, language);
      if (mode === 'C') check([...h.writes].filter(([p]) => p.includes('/originals/')).map(([, text]) => text).join('\n'), language);
      assert.ok(h.timers.length);
      h.timers.shift()();
      await flush();
      check(h.prompts.at(-1), language);
      if (language === 'en') assert.match(h.prompts.at(-1), /authorized unfinished work/);
    }
    for (const [name, script] of [['get', 'cw_get.py'], ['handoff_check', 'cw_handoff_check.py'], ['segments', 'cw_segments.py'], ['advice_reply', 'cw_advice.py']]) {
      h.fail = script;
      check(await h.call(name), language);
    }
    h.fail = '';
    check(await h.call('compact', { mode: 'cancel' }), language);
  });

  await test(`${language}: idle reminder and handoff reminder`, async () => {
    const timers = [];
    const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
    globalThis.setTimeout = (fn) => { timers.push(fn); return 1; };
    globalThis.clearTimeout = () => {};
    try {
      const h = await fixture({ override: language });
      await h.turn();
      assert.equal(timers.length, 1);
      timers.shift()();
      await flush();
      check(h.prompts.at(-1), language);
      if (language === 'en') assert.match(h.prompts.at(-1), /Handoff: Decisions/);
    } finally {
      globalThis.setTimeout = originalSet;
      globalThis.clearTimeout = originalClear;
    }
  });
}

await test('threshold setting, environment precedence, hot reread, and invalid config fallback', async () => {
  const h = await fixture({ configured: 'en' });
  check(h.tools, 'en');
  h.override = 'zh';
  await h.start();
  check(h.tools, 'zh');
  h.override = undefined;
  h.configured = 'en';
  await h.start();
  check(h.tools, 'en');
  h.broken = true;
  await h.start();
  check(h.tools, 'zh');
  h.override = 'en';
  await h.start();
  check(h.tools, 'en');
});
