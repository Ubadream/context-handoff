// get／segments 兩個工具走外掛自己的呼叫路（真的跑 python）。node tests/tools.mjs <session id> <工具編號或段號> [segments]
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { register } from '../hooks/wife-compact.ts';
const [sid, ref, alsoSegments] = process.argv.slice(2);
const hooks = {};
register((ev, fn) => { hooks[ev] = fn; }, {});
const $ = {
  ui: { log() {}, toast() {} },
  env: { get: async (n) => process.env[n] ?? ({ CW_WORKBENCH: new URL('../../../../', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'), CW_PYTHON: 'python' })[n] },
  fs: { exists: async (p) => existsSync(p), read: async (p) => readFileSync(p, 'utf8'), write: async () => {}, list: async (d) => readdirSync(d).map((name) => ({ name, mtimeMs: statSync(`${d}/${name}`).mtimeMs })) },
  tool: { register: async (spec) => ({ tool: `mcp__wife-compact__${spec.name}` }) },
  session: { id: async () => sid },
  process: { run: async (argv, init) => { try { return { exitCode: 0, stdout: execFileSync(argv[0], argv.slice(1), { cwd: init?.cwd, encoding: 'utf8' }), stderr: '' }; } catch (e) { return { exitCode: e.status ?? 1, stdout: String(e.stdout ?? ''), stderr: String(e.stderr ?? '') }; } } },
};
await hooks['session.start']($, {}, async () => ({}));
const get = await hooks['tool.call']($, { tool: 'mcp__wife-compact__get', ref }, async () => ({ deny: 'x' }));
console.log('get →', get.result.slice(0, 200).replace(/\n/g, ' ⏎ '));
const bad = await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'A', reason: 't', drop: ['nope#1234'] }, async () => ({ deny: 'x' }));
console.log('壞段號 →', JSON.stringify(bad));
if (alsoSegments) {
  const seg = await hooks['tool.call']($, { tool: 'mcp__wife-compact__segments' }, async () => ({ deny: 'x' }));
  console.log('segments →', seg.result.slice(0, 200).replace(/\n/g, ' ⏎ '));
}
// 交接檢查：正常排定（帶交接事實）；再用沒寫過交接的對話看會不會被擋
const ok = await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'A', reason: 't' }, async () => ({ deny: 'x' }));
console.log('排定 →', JSON.stringify(ok).slice(0, 220));
await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'cancel', reason: 't' }, async () => ({ deny: 'x' }));
if (process.argv[5]) {
  $.session.id = async () => process.argv[5];
  const blocked = await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'A', reason: 't' }, async () => ({ deny: 'x' }));
  console.log('沒交接 →', JSON.stringify(blocked).slice(0, 220));
  const excused = await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'A', reason: 't', no_handoff_reason: '只是閒聊' }, async () => ({ deny: 'x' }));
  console.log('給理由 →', JSON.stringify(excused).slice(0, 220));
}
