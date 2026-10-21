// Real registered hooks; virtual host completion/timers make input and compaction races deterministic.
// node --experimental-strip-types --test adapters/claude/wife-compact/tests/continue.mjs
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';

const parent = resolve(tmpdir());
const root = mkdtempSync(join(parent, 'cw-compact-continue-'));
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
let serial = 0;

async function fixture({ route = 'command', submitFailure = false } = {}) {
  const state = join(root, `${++serial}`);
  const hooks = {}, tools = {}, timers = [], prompts = [], events = [], ui = [];
  const result = { messages: [{ role: 'user', text: 'native summary', toolUses: [] }] };
  const host = { timers, prompts, events, ui, native: async () => result, apply: async () => {}, result };
  const $ = {
    env: { get: async (key) => ({ CW_WORKBENCH: '/fixture', CW_STATE_ROOT: state, USERPROFILE: state })[key] },
    fs: {
      exists: async () => false,
      read: async (path) => readFileSync(path, 'utf8'),
      write: async (path, text) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, text); },
    },
    ui: { log: (text) => ui.push(text), toast: (text) => ui.push(text) },
    tool: { register: async (spec) => { tools[spec.name] = spec; return { tool: `fixture_${spec.name}` }; } },
    process: { run: async (argv) => ({ exitCode: 0, stdout: argv.some((s) => s.endsWith('cw_handoff_check.py'))
      ? JSON.stringify({ facts: { written_since_compaction: true }, verdict: 'fixture fresh handoff' }) : '', stderr: '' }) },
    clock: { after: (ms, fn) => { timers.push(fn); return () => {}; } },
    session: {
      id: async () => 'fixture',
      usage: async () => ({ context: { tokens: 0 } }),
      compact: async () => {
        if (route === 'command') throw new Error('not available in a headless session');
        const res = await host.compact();
        await host.apply(); // Hook result exists before the host has applied it.
        events.push('applied');
        return res;
      },
    },
    command: { run: async ({ command }) => {
      assert.equal(command, 'compact');
      const res = await host.compact();
      await host.apply();
      events.push('applied');
      return res;
    } },
    prompt: { submit: (args) => {
      prompts.push(args);
      events.push('submit');
      assert.ok(!args.text.startsWith('/'));
      if (submitFailure === 'sync') throw new Error('fixture synchronous submit failure');
      if (submitFailure) return Promise.reject(new Error('fixture asynchronous submit failure'));
      if (host.submit) return host.submit();
      return hooks['prompt.submit']($, args, async () => ({}));
    } },
  };
  const { register } = await import(`../hooks/wife-compact.ts?continue=${serial}`);
  register((name, handler) => { hooks[name] = handler; });
  await hooks['session.start']($, {}, async () => ({}));
  host.schedule = (next, mode = 'A') => hooks['tool.call']($,
    { tool: 'fixture_compact', mode, reason: 'fixture', ...(next === undefined ? {} : { next }) }, async () => ({}));
  host.compact = (extra = {}) => hooks['session.compact']($,
    { trigger: 'manual', messages: [{ role: 'user', text: 'original', toolUses: [] }], ...extra }, host.native);
  host.turn = (extra = {}) => hooks['turn.complete']($, { reason: 'answer', ...extra }, async () => ({}));
  host.input = () => hooks['prompt.submit']($, { text: 'new human request' }, async () => ({}));
  host.tick = async () => { const timer = timers.shift(); assert.ok(timer, 'expected scheduled callback'); timer(); await flush(); };
  host.trace = async () => { await flush(); return readFileSync(join(state, 'wife-compact/trace.log'), 'utf8'); };
  host.tools = tools;
  return host;
}

try {
  for (const route of ['direct', 'command']) for (const mode of ['A', 'C']) {
    await test(`${route} ${mode}: submit once, only after host completion; fresh compaction can submit again`, async () => {
      const h = await fixture({ route });
      assert.equal(h.tools.compact.inputSchema.properties.next.default, 'wait');
      assert.deepEqual(h.tools.compact.inputSchema.properties.next.enum, ['continue', 'wait']);
      assert.match(h.tools.compact.description, /已授權未完.*continue/);
      const gate = deferred();
      h.apply = () => gate.promise;
      await h.schedule('continue', mode);
      const running = h.turn();
      if (route === 'command') { await running; await h.tick(); }
      else await flush();
      assert.equal(h.prompts.length, 0);
      assert.equal(h.timers.length, 0, 'hook returning messages does not yet schedule a prompt');
      gate.resolve();
      await running;
      await flush();
      await h.tick();
      assert.equal(h.prompts.length, 1);
      assert.match(h.prompts[0].text, /^〔wife-compact：壓縮完成，續作〕/);
      assert.match(h.prompts[0].text, /最後一句不是新要求，已做完的不要重做/);
      assert.deepEqual(h.events.slice(0, 2), ['applied', 'submit']);
      await h.turn();
      await h.compact(); // duplicate/unplanned compaction cannot reuse the consumed request
      assert.equal(h.timers.length, 0);
      await h.schedule('continue', mode);
      await h.turn();
      if (route === 'command') await h.tick();
      await h.tick();
      assert.equal(h.prompts.length, 2);
    });
  }

  for (const next of [undefined, 'wait']) await test(`next=${next}: old calls wait`, async () => {
    const h = await fixture({ route: 'direct' });
    await h.schedule(next);
    await h.turn();
    assert.equal(h.timers.length, 0);
    assert.equal(h.prompts.length, 0);
  });

  for (const when of ['command-delay', 'compacting', 'host-apply', 'before-submit']) await test(`new input at ${when} cancels continuation`, async () => {
    const h = await fixture();
    const native = deferred(), applied = deferred();
    h.native = () => native.promise;
    h.apply = () => applied.promise;
    await h.schedule('continue');
    await h.turn();
    if (when === 'command-delay') await h.input();
    await h.tick(); // enters the native compactor; does not await it
    if (when === 'compacting') await h.input();
    native.resolve(h.result);
    await flush();
    if (when === 'host-apply') await h.input();
    applied.resolve();
    await flush();
    if (when === 'before-submit') await h.input();
    while (h.timers.length) await h.tick();
    assert.equal(h.prompts.length, 0);
  });

  for (const submitFailure of [true, 'sync']) await test(`submit failure ${submitFailure}: trace only, never retry`, async () => {
    const h = await fixture({ route: 'direct', submitFailure });
    await h.schedule('continue');
    await h.turn();
    await h.tick();
    assert.equal(h.prompts.length, 1);
    assert.match(await h.trace(), /壓縮後續作提示送不出：fixture/);
    await h.turn();
    assert.equal(h.timers.length, 0);
    assert.equal(h.ui.some((s) => s.includes('submit failure')), false);
  });

  await test('input between timer callback and submit microtask cancels send', async () => {
    const h = await fixture({ route: 'direct' });
    await h.schedule('continue');
    await h.turn();
    h.timers.shift()();
    await h.input();
    await flush();
    assert.equal(h.prompts.length, 0);
    assert.match(await h.trace(), /送出前有新輸入/);
  });

  await test('prompt.submit that waits for idle does not block completion hook', async () => {
    const h = await fixture({ route: 'direct' });
    const sent = deferred();
    h.submit = () => sent.promise;
    await h.schedule('continue');
    await h.turn(); // even an unresolved submit must not be awaited by this hook
    await h.tick();
    assert.equal(h.prompts.length, 1);
    assert.doesNotMatch(await h.trace(), /壓縮後續作提示已送出/);
    sent.resolve();
    assert.match(await h.trace(), /壓縮後續作提示已送出/);
  });

  for (const result of [{ skip: 'cancelled' }, undefined, { messages: [] }]) await test(`incomplete result ${JSON.stringify(result)} never continues`, async () => {
    const h = await fixture({ route: 'direct' });
    h.native = async () => result;
    await h.schedule('continue');
    await h.turn();
    assert.equal(h.timers.length, 0);
    assert.equal(h.prompts.length, 0);
  });

  await test('precompute and subagent events do not consume next; cancellation invalidates queued send', async () => {
    const h = await fixture({ route: 'direct' });
    await h.schedule('continue');
    await h.compact({ trigger: 'precompute' });
    await h.compact({ agentId: 'child' });
    assert.equal(h.timers.length, 0);
    await h.turn();
    await h.schedule(undefined, 'cancel');
    await h.tick();
    assert.equal(h.prompts.length, 0);
  });

  await test('replacement wait invalidates a queued continue; invalid next is denied', async () => {
    const h = await fixture({ route: 'direct' });
    assert.match((await h.schedule('bad')).deny, /next/);
    await h.schedule('continue');
    await h.turn();
    await h.schedule('wait');
    await h.tick();
    await h.turn();
    assert.equal(h.prompts.length, 0);
  });

  await test('native fallback continues only on success', async () => {
    for (const recovers of [true, false]) {
      const h = await fixture();
      let attempts = 0;
      h.native = async () => { if (++attempts === 1 || !recovers) throw new Error('fixture failed native'); return h.result; };
      await h.schedule('continue');
      await h.turn();
      await h.tick();
      if (recovers) await h.tick();
      assert.equal(h.prompts.length, recovers ? 1 : 0);
    }
  });
} finally {
  await flush();
  assert.equal(dirname(root), parent);
  rmSync(root, { recursive: true, force: true });
}
