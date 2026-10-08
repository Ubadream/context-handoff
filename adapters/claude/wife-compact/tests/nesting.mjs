// 連續兩次 C：第二次的逐字塊不該包進第一次的逐字塊或摘要
import { register } from '../hooks/wife-compact.ts';
const hooks = {};
const files = [];
register((ev, fn) => { hooks[ev] = fn; }, {});
const $ = { ui: { log() {}, toast() {} }, env: { get: async () => undefined }, fs: { exists: async () => false, read: async () => '', write: async (p, t) => { if (p.includes('/originals/')) files.push(t); } }, tool: { register: async (s) => ({ tool: `mcp__wife-compact__${s.name}` }) } };
await hooks['session.start']($, {}, async () => ({}));
const builtin = async (e) => ({ messages: [{ role: 'user', text: 'This session is being continued from a previous conversation. 摘要', toolUses: [], handle: 's' }, ...e.messages.slice(-1)] });
let msgs = [];
for (let i = 0; i < 20; i++) msgs.push({ role: i % 2 ? 'assistant' : 'user', text: `第一段第${i}則`, toolUses: [], handle: `a${i}` });
const compactC = async (messages) => {
  await hooks['tool.call']($, { tool: 'mcp__wife-compact__compact', mode: 'C', reason: 't' }, async () => ({}));
  return (await hooks['session.compact']($, { trigger: 'manual', instructions: '', messages }, builtin)).messages;
};
let after1 = await compactC(msgs);
after1 = after1.map((m, i) => ({ ...m, handle: m.handle ?? `c1_${i}` }));
for (let i = 0; i < 6; i++) after1.push({ role: i % 2 ? 'assistant' : 'user', text: `第二段第${i}則`, toolUses: [], handle: `b${i}` });
const after2 = await compactC(after1);
const block = files[files.length - 1] ?? '';
console.log('第二次寫出的原話檔含「第一段」:', block.includes('第一段'), '含上次逐字塊:', (block.match(/〔壓縮前最近的原話/g) || []).length > 1, '含摘要:', block.includes('This session'));
console.log('第二次寫出的原話檔收了幾則「第二段」:', (block.match(/第二段/g) || []).length, '（應為 5，最後一則由內建保留）');
