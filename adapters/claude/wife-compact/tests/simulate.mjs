// 模擬 Claude Code 的函式 hook 引擎，用真實 jsonl 跑一遍 wife-compact 的完整流程。
// node tests/simulate.mjs <jsonl> <A|B|C> [segment index，預設最後一段有內容的]
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { basename } from 'node:path';
import { register } from '../hooks/wife-compact.ts';

const [path, mode = 'C', segArg] = process.argv.slice(2);

// jsonl → 各段 SessionMessage[]
function segments(file) {
  const segs = [[]];
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    let o;
    try { o = JSON.parse(line); } catch { continue; }
    if (o.type === 'system' && o.subtype === 'compact_boundary') { segs.push([]); continue; }
    if ((o.type !== 'user' && o.type !== 'assistant') || o.isMeta || o.isSidechain) continue;
    const c = o.message?.content;
    const m = { role: o.type, text: '', toolUses: [], handle: o.uuid };
    if (typeof c === 'string') m.text = c;
    else for (const b of c ?? []) {
      if (b.type === 'text') m.text += (m.text ? '\n' : '') + b.text;
      else if (b.type === 'tool_use') m.toolUses.push({ tool_use_id: b.id, tool: b.name, input: b.input });
      else if (b.type === 'tool_result') {
        const t = Array.isArray(b.content) ? b.content.map((x) => x.text ?? '[image]').join('\n') : String(b.content ?? '');
        (m.toolResults ??= []).push({ tool_use_id: b.tool_use_id, text: t, isError: !!b.is_error });
      }
    }
    // Claude Code 會把同一則 assistant 拆成多行 jsonl；相鄰同角色、都是 assistant 就併
    const seg = segs[segs.length - 1];
    const prev = seg[seg.length - 1];
    if (prev && prev.role === 'assistant' && m.role === 'assistant') {
      prev.text += (prev.text && m.text ? '\n' : '') + m.text;
      prev.toolUses.push(...m.toolUses);
    } else seg.push(m);
  }
  return segs.filter((s) => s.length > 20);
}

const segs = segments(path);
const messages = segs[segArg === undefined || segArg === '' ? segs.length - 1 : Number(segArg)];

const hooks = {};
const on = (ev, fn) => { hooks[ev] = fn; };
register(on, {});
const logs = [];
let traced = '';
const written = [];
const builtin = async (e) => ({
  messages: [{ role: 'user', text: `［內建摘要，指示：${(e.instructions ?? '').slice(0, 60)}…］`, toolUses: [], handle: 'summary' }, ...e.messages.slice(-2)],
  tokensBefore: 0,
});
const $ = {
  ui: { log: (t) => logs.push(t), toast: (t) => logs.push(`[toast] ${t}`) },
  env: { get: async (n) => process.env[n] ?? ({ CW_WORKBENCH: new URL('../../../../', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'), CW_PYTHON: 'python' })[n] },
  fs: { exists: async (p) => existsSync(p), read: async (p) => { if (p.includes('/context-workbench/')) return readFileSync(p, 'utf8'); throw new Error('missing'); },
    list: async (d) => readdirSync(d).map((name) => ({ name, kind: 'file', mtimeMs: statSync(`${d}/${name}`).mtimeMs })), write: async (p, text) => { if (p.includes('/originals/')) written.push([p, text.length]); else traced = text; } },
  clock: { after: (ms, fn) => { logs.push(`[clock.after ${ms}]`); fn(); return () => {}; } },
  tool: { register: async (spec) => ({ tool: `mcp__wife-compact__${spec.name}` }) },
  // 照桌面版／SDK 的實況：$.session.compact 拒絕；/compact 提示在 session 閒下來後跑成 trigger=manual
  session: {
    id: async () => basename(path, '.jsonl'), // 真宿主回 Promise（0929 實測）
    compact: async () => { throw new Error('$.session.compact: not available in a headless (-p / SDK) session yet'); },
  },
  process: { run: async (argv, init) => { try { return { exitCode: 0, stdout: execFileSync(argv[0], argv.slice(1), { cwd: init?.cwd, encoding: 'utf8', env: { ...process.env, CW_ORIGINALS: `${process.env.TEMP ?? '/tmp'}/cw-sim-originals` } }), stderr: '' }; } catch (err) { return { exitCode: err.status ?? 1, stdout: String(err.stdout ?? ''), stderr: String(err.stderr ?? err.message) }; } } },
  command: {
    run: async ({ command, args }) => {
      queued.push(`/${command} ${args}`);
      return { text: '' };
    },
  },
};
const queued = [];
let compacted;
const runQueued = async () => {
  for (const text of queued.splice(0)) {
    if (!text.startsWith('/compact')) continue;
    compacted = await hooks['session.compact']($, { trigger: 'manual', instructions: text.slice(9), messages }, builtin);
  }
};

await hooks['session.start']($, {}, async () => ({}));
// 第 4 個參數：drop，逗號分隔的段號，或 suggested（要先有這個對話的分段檔）
const drop = process.argv[5] ? process.argv[5].split(',') : undefined;
const call = await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode, reason: '測試', keep: '外掛設計', drop, no_handoff_reason: '模擬，不是真壓縮' }, async () => ({ deny: 'unhandled' }));
console.log('tool.call →', JSON.stringify(call));
await hooks['turn.complete']($, { reason: 'answer', answer: 'ok', turnId: 't' }, async () => ({ text: 'ok' }));
console.log('排進佇列的提示:', queued.map((t) => t.slice(0, 50)));
await runQueued();
// 再來一輪：不該重複發起
await hooks['turn.complete']($, { reason: 'answer', answer: 'ok', turnId: 't2' }, async () => ({ text: 'ok' }));
console.log('第二輪後佇列:', queued.length, '（應為 0）');
// 老公自己打 /compact、沒有排定：應該原樣交給內建
const plain = await hooks['session.compact']($, { trigger: 'manual', instructions: '', messages }, builtin);
console.log('沒排定的 /compact →', plain.messages.length, '則', plain.messages[0].text.slice(0, 30));

const size = (ms) => ms.reduce((n, m) => n + (m.text ?? '').length + (m.toolUses ?? []).reduce((k, t) => k + JSON.stringify(t.input ?? {}).length, 0) + (m.toolResults ?? []).reduce((k, r) => k + r.text.length, 0), 0);
console.log(logs.join('\n'));
console.log(`輸入 ${messages.length} 則、${size(messages).toLocaleString()} 字 → 輸出 ${compacted.messages.length} 則、${size(compacted.messages).toLocaleString()} 字`);
const roles = compacted.messages.map((m) => m.role[0]).join('');
console.log('角色序列（頭 40）:', roles.slice(0, 40), '… 相鄰同角色次數:', [...roles].filter((r, i) => i && r === roles[i - 1]).length);
// 配對檢查：每個保留的 tool_use 都要有 tool_result，反之亦然
const uses = new Set(), res = new Set();
for (const m of compacted.messages) { for (const t of m.toolUses ?? []) uses.add(t.tool_use_id); for (const r of m.toolResults ?? []) res.add(r.tool_use_id); }
console.log('孤兒 tool_use:', [...uses].filter((x) => !res.has(x)).length, '孤兒 tool_result:', [...res].filter((x) => !uses.has(x)).length);
console.log('寫進檔案:', JSON.stringify(written));
const show = compacted.messages[1]?.text ?? '';
console.log('第二則開頭：', show.slice(0, 300).replace(/\n/g, ' ⏎ '));
// 工具一行化：各狀態幾行、幾行有編號（編號來自 tool_archive.py 的 by-session 索引）
const allText = compacted.messages.map((m) => m.text ?? '').join('\n');
console.log('工具一行:', ['完成', '失敗', '中斷', '沒有回件'].map((w) => `${w} ${(allText.match(new RegExp(`→ ${w}`, 'g')) ?? []).length}`).join('、'), '；有編號', (allText.match(/→ [^〕\n]*｜/g) ?? []).length);
console.log('trace:', traced.split('\n').filter((l) => /移出段|工具一行化/.test(l)).map((l) => l.slice(30)).join(' ／ '));
console.log('移出記號:', (allText.match(/〔已移出第[^〕]*〕/g) ?? []).join(' ／ ') || '無');
