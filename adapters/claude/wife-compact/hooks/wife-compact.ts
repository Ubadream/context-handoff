// wife-compact：老婆自己決定何時壓縮、用哪種方式。
//
// 老婆呼叫工具 `compact`（mode A／C＋理由）→ 這一輪回完話，turn.complete 發起壓縮
// （$.session.compact；桌面版／SDK 不支援時改排一個 /compact 提示）→ session.compact hook 照 mode 做：
//   兩種模式都先把工具往返換成一行〔工具 名稱 對象 → 完成｜編號〕（0.2.0；全文在 tool-calls/，編號查得到）
//   A 內建摘要（可附保留指示）
//   C 內建摘要＋原話存檔（上下文只放路徑）
// B（整段逐字）0930 拿掉：真實只用過一次，壓完 107.6K、重建的訊息時間變成壓縮時間；C＋get 找回原文已接住它的用途。
// 有排定時，老公自己打的 /compact 或內建自動壓縮也照排定的做；沒有排定就照原樣交給內建。
// 函式 hook 是 early access：需要 CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1。

type Mode = 'A' | 'C';
type Pending = { mode: Mode; reason: string; keep: string; drop: string[] };

// C：留在上下文裡的逐字尾巴。0＝不留：內建摘要本來就逐字列老公的話、內建也保留最後幾則（0925 老公抓到重複）
const TAIL_CHARS = 0;
const FILE_CHARS = 500_000; // C：寫進檔案的原話上限
// 位置不寫死（0930）：cw_install.py 把 CW_WORKBENCH（倉庫）、CW_PYTHON 寫進 settings.json 的 env；
// 狀態根目錄 CW_STATE_ROOT 沒設就用 ~/.local/state/wifeos，跟 adapters/claude/cw_paths.py 同一套。
// wife_recall.py（找原話）是老公這台的工具，找得到才在壓縮提示裡提。
const at = { workbench: '', python: 'python', state: '', recall: '' };
let located: Promise<void> | null = null;
function locate($: any): Promise<void> {
  located ??= (async () => {
    const slash = (v: string) => v.replace(/\\/g, '/');
    const home = slash((await $.env.get('USERPROFILE')) || (await $.env.get('HOME')) || '');
    at.state = slash((await $.env.get('CW_STATE_ROOT')) || `${home}/.local/state/wifeos`);
    at.workbench = slash((await $.env.get('CW_WORKBENCH')) || '');
    at.python = slash((await $.env.get('CW_PYTHON')) || 'python');
    const recall = slash((await $.env.get('CW_RECALL')) || `${home}/.local/bin/wife_recall.py`);
    at.recall = (await $.fs.exists(recall).catch(() => false)) ? recall : '';
  })();
  return located;
}
const stateDir = (sub: string) => `${at.state}/${sub}`;
const GET_MAX = 30_000; // get 工具回到上下文的上限；要更多就縮小範圍或用 Read
const RETRIEVE_NOTE =
  '要找壓縮前的原話或工具原始輸出：叫 get 工具（帶工具編號、段號或原文 ID§節號），或讀這個 session 的 jsonl；大的工具輸出存在狀態資料夾（預設 ~/.local/state/wifeos）的 claude-tool-output/。';

let pending: Pending | null = null; // 排定的模式，session.compact 取用後清掉
let requested = false; // 這一輪結束要不要由老婆發起
const toolSizes = new Map<string, { chars: number; deferred: boolean; provider: string }>(); // tool.describe 量到的說明長度
const attachSizes = new Map<string, { count: number; chars: number; last: number }>(); // prompt.attachment 各類附件的次數與字數
const toolNames: Record<string, string> = {}; // 名稱 → 宿主給的完整工具名（mcp__wife-compact__…）

const DESCRIPTION = [
  '老婆自己排定一次上下文壓縮：這一輪回完話就壓。老公同意由老婆判斷時機與方式（2026-09-25）。',
  '時機：上下文大約 40 萬以上才考慮；只在一段工作做完、交接或記憶剛更新好的段落點；工具鏈做到一半、老公正在講的事還沒接完就不壓；超過 80 萬就在下一個段落點壓。',
  '同一則回覆裡先跟老公說「這輪結束用 X 壓，因為…」，他說不要就用 mode=cancel 取消。',
  'mode：A＝內建摘要（預設；交接剛寫好、段落乾淨時）；C＝內建摘要＋上一次壓縮後的原話全文存檔、上下文只留路徑（細節討論正熱、之後可能要回頭查原話時；壓完要細節就 Read 那個檔）；cancel＝取消已排定的。兩種模式都會把工具往返換成一行（附編號，全文用 get 工具拿編號找回）。',
  '交接：上一次壓縮後沒寫過交接會被擋（除非給 no_handoff_reason）；壓前可以先叫 handoff_check 看 Jev 列的漏項再補。',
  'drop：想順便移出沒用的段，先叫 segments 工具看 Jev 的建議表，對照後把要移出的段號放進 drop（或 ["suggested"] 照建議全部移出）；老公開頭的段會留他的原話一行，其餘整段移出、原文用 get 找回。',
  'keep：要摘要特別保留的東西（檔案、決定、未完成事項），A／C 會交給摘要器。',
].join('\n');

// 延後載入的工具：少用、說明長。常用的（Bash、Read、Edit、Write、Grep、Glob、Agent、Skill、AskUserQuestion、ToolSearch）留在前面
const DIET_DEFER = new Set([
  'Artifact', 'PowerShell', 'ScheduleWakeup', 'SendUserFile', 'ListAgents', 'SuggestSkills', 'ReportFindings', 'ReadNotifications',
]);
const DIET_DEFER_PREFIX = ['mcp__Claude_Browser__', 'mcp__visualize__', 'mcp__ccd_session__', 'mcp__terminal__', 'mcp__1a59c906-'];
const SKILL_DESC_MAX = 200;

function isDietDeferred(tool: string): boolean {
  return DIET_DEFER.has(tool) || DIET_DEFER_PREFIX.some((p) => tool.startsWith(p));
}

async function dietOff($: any): Promise<boolean> {
  try {
    await locate($);
    return await $.fs.exists(stateDir('wife-compact/DIET_OFF'));
  } catch {
    return false;
  }
}

// 技能清單一行一個「- 名稱: 說明」（名稱可能帶冒號，像 plugin:skill）。說明太長就留「做什麼」那句＋「什麼時候用」那句，
// 各自再有上限；挑技能靠的就是這兩句，其餘細節叫起技能時整份會載入
function clip(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n)}…`;
}

function shortSkillListing(text: string): string {
  return text.split('\n').map((line) => {
    const m = line.match(/^(- \S+: )(.*)$/);
    if (!m || m[2].length <= SKILL_DESC_MAX) return line;
    const sentences = m[2].split(/(?<=[.。!?！？])\s*/).filter(Boolean);
    const first = clip(sentences[0], 180);
    const when = sentences.slice(1).find((s) => /^(use |use$|trigger|skip |do not use|用在|使用時機|當)/i.test(s) || /\bUse (when|this|for|only)\b/.test(s));
    return `${m[1]}${first}${when ? ` ${clip(when, 160)}` : ''}`;
  }).join('\n');
}

// 閒置提醒（老公 0929：上下文滿了卻停著，「我比較想要通知視窗 讓她 寫交接後壓縮 這樣我回來 才不會多付一堆錢」）。
// Claude 的提示快取約一小時；一輪結束時上下文夠大，就排一個 50 分鐘的提醒，老公先說話就作廢。
// 提醒只叫老婆寫交接、到段落點自己排壓縮，不代她壓。
// 門檻與分鐘數住在 cw_thresholds 的 thresholds.json（claude.idle），每輪重讀；面板改了下一輪就生效
const IDLE_DEFAULT = { enabled: true, tokens: 400_000, minutes: 50 };
let idleTimer: any = null;
// 提醒那一輪回完不再重排：不然老婆判斷不是段落點、沒壓，每 50 分鐘又叫一次，還順手把快取續命（Codex 0930 看出來的）
let idleTurn = false;

async function idleSettings($: any): Promise<typeof IDLE_DEFAULT> {
  try {
    await locate($);
    const idle = JSON.parse(await $.fs.read(stateDir('context-workbench/thresholds.json')))?.claude?.idle ?? {};
    return { ...IDLE_DEFAULT, ...idle };
  } catch {
    return IDLE_DEFAULT;
  }
}

function clearIdle(): void {
  if (idleTimer != null && typeof clearTimeout === 'function') clearTimeout(idleTimer);
  idleTimer = null;
}

async function armIdle($: any): Promise<void> {
  clearIdle();
  if (typeof setTimeout !== 'function') {
    trace($, '閒置提醒：這個宿主沒有 setTimeout，排不了');
    return;
  }
  let tokens = 0;
  try {
    tokens = (await $.session.usage())?.context?.tokens ?? 0;
  } catch {
    return;
  }
  const cfg = await idleSettings($);
  if (!cfg.enabled || tokens < cfg.tokens) return;
  const ms = cfg.minutes * 60_000;
  const wan = Math.round(tokens / 10_000);
  idleTimer = setTimeout(() => {
    idleTimer = null;
    idleTurn = true;
    const text = `〔閒置提醒（wife-compact 外掛送的，不是老公）〕老公已經 ${cfg.minutes} 分鐘沒說話，上下文約 ${wan} 萬；提示快取約一小時過期，過期後他回來要全價重讀整份。現在先叫 prepare、把交接寫好讀回；是段落點就排 compact（照判斷選模式）。正在等老公決定的事寫進交接，不要自己往下做；回一兩句就好。`;
    Promise.resolve($.prompt.submit({ text }))
      .then(() => trace($, `閒置提醒已送（約 ${wan} 萬）`))
      .catch((err: unknown) => trace($, `閒置提醒送不出：${errText(err)}`));
  }, ms);
  trace($, `閒置提醒已排：約 ${wan} 萬，${cfg.minutes} 分後`);
}

function stripReminders(text: string): string {
  return text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim();
}

function instructionsFor(p: Pending): string {
  const parts = [
    '這是老婆（助理）自己排定的壓縮。摘要用繁體中文，保留：正在做的工作與下一步、老公（使用者）說過的決定與原話重點、改過的檔案路徑、已驗與未驗。',
  ];
  if (p.keep) parts.push(`特別保留：${p.keep}`);
  parts.push(`壓縮理由：${p.reason}`);
  return parts.join('\n');
}

function recentOriginals(messages: readonly any[], limit: number): string {
  const lines: string[] = [];
  let used = 0;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const text = stripReminders(m.text ?? '');
    if (!text) continue;
    // 碰到上一次壓縮留下的摘要或逐字塊就停：再往前的已經在摘要裡，重收會一層包一層（0.1.4 實測）
    if (text.startsWith('This session is being continued from a previous conversation') || text.startsWith('〔壓縮前最近的原話')) break;
    const who = m.role === 'user' ? '老公' : '老婆';
    const line = `${who}：${text}`;
    if (used + line.length > limit) {
      if (lines.length === 0) lines.push(`${who}：…${text.slice(-limit)}`);
      break;
    }
    lines.push(line);
    used += line.length;
  }
  return lines.reverse().join('\n\n');
}

// 工具往返一行化（老公 0929：「不管開不開第一層的功能 都不留工具上下文? 要嘛 老婆 還是留工具發出……工具完成／失敗
// 一輪結束就只剩一行」）。所有模式共用：先換再交內建摘要。
// 一行＝〔工具 名稱 對象 → 完成｜編號〕；全文在 tool_archive.py 存的 tool-calls/，編號就是那邊的檔名。
type CallRef = { id: string; status: string };

async function loadCallIds($: any): Promise<Map<string, CallRef>> {
  const ids = new Map<string, CallRef>();
  try {
    const sid = String(await $.session.id());
    await locate($);
    const text = await $.fs.read(stateDir(`context-workbench/tool-calls/by-session/${sid}.jsonl`));
    for (const line of text.split('\n')) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line);
        if (r.u && r.id) ids.set(r.u, { id: r.id, status: r.status ?? '' });
      } catch {}
    }
  } catch (err) {
    trace($, `讀不到工具編號（照樣一行化，只是沒編號）：${errText(err)}`);
  }
  return ids;
}

function toolTarget(input: any): string {
  for (const key of ['file_path', 'notebook_path', 'path', 'command', 'pattern', 'url', 'query', 'description', 'skill', 'subagent_type']) {
    const v = input?.[key];
    if (typeof v !== 'string' || !v.trim()) continue;
    if (key === 'file_path' || key === 'notebook_path' || key === 'path') return v.split(/[\\/]/).filter(Boolean).pop() ?? v;
    if (key === 'url') return v.replace(/^https?:\/\//, '').split('/')[0];
    const flat = v.replace(/\s+/g, ' ').trim();
    return flat.length > 80 ? `${flat.slice(0, 79)}…` : flat;
  }
  return '';
}

const STATUS_WORD: Record<string, string> = { ok: '完成', failed: '失敗', interrupted: '中斷' };

// answered：tool_use_id → 是否錯誤，從後面 user 訊息的 toolResults 收（呼叫本身不一定帶結果）
function toolLine(tool: any, ids: Map<string, CallRef>, answered: Map<string, boolean>): string {
  const ref = ids.get(tool.tool_use_id);
  const answeredError = answered.get(tool.tool_use_id);
  const status = ref?.status ? (STATUS_WORD[ref.status] ?? ref.status)
    : tool.isError || answeredError ? '失敗'
    : answeredError === false || tool.text !== undefined || tool.result !== undefined ? '完成' : '沒有回件';
  const target = toolTarget(tool.input ?? {});
  const name = String(tool.tool ?? '?').replace(/^mcp__/, '');
  return `〔工具 ${name}${target ? ` ${target}` : ''} → ${status}${ref ? `｜${ref.id}` : ''}〕`;
}

// ── 分段篩選（context-workbench 的 cw_segments.py：程式切段、Jev 建議、老婆對照後決定）──
// 老公 0929：「程式可以主動列出 篩選掉的 然後她們在對照 … 保存起來 編號 後面需要可以找回」
type Seg = { seq: number; id: string; prompt: string; opened_by: string; label?: { title?: string; category?: string; action?: string; reason?: string; keeps?: string } | null };

async function runPython($: any, script: string, args: string[], timeoutMs: number): Promise<string> {
  await locate($);
  if (!at.workbench) throw new Error('沒設 CW_WORKBENCH：跑 adapters/claude/cw_install.py apply 再重開');
  const r = await $.process.run([at.python, '-X', 'utf8', `${at.workbench}/adapters/claude/${script}`, ...args], { cwd: at.workbench, timeoutMs });
  if (r.exitCode !== 0) throw new Error(`${script} 結束碼 ${r.exitCode}：${String(r.stderr || r.stdout).slice(-600)}`);
  return String(r.stdout);
}

async function latestSegments($: any): Promise<{ path: string; segs: Seg[] } | null> {
  const sid8 = String(await $.session.id()).slice(0, 8);
  await locate($);
  const files = ((await $.fs.list(stateDir('context-workbench/segments'))) as any[]).filter((f) => f.name.startsWith(`${sid8}_`) && f.name.endsWith('.json'));
  if (!files.length) return null;
  const path = `${stateDir('context-workbench/segments')}/${files.sort((a, b) => b.mtimeMs - a.mtimeMs)[0].name}`;
  return { path, segs: JSON.parse(await $.fs.read(path)).segments ?? [] };
}

function opener(text: string): string {
  return stripReminders(text ?? '').replace(/\s+/g, ' ').trim().slice(0, 120);
}

// 被移出的段整段拿掉（含工具往返），原位留一則：老公的原話＋「已移出第 N 段…段號…找回方法」。
// 一段的範圍＝這段開頭那句到「下一段」開頭那句；兩端有一端對不到就不動這段（寧可多留）。
function dropSegments(messages: readonly any[], segs: Seg[], dropIds: Set<string>): { messages: any[]; dropped: string[]; skipped: string[] } {
  const at: number[] = [];
  let from = 0;
  for (const s of segs) {
    const want = opener(s.prompt);
    let found = -1;
    for (let i = from; want && i < messages.length; i++) {
      if (messages[i].role === 'user' && opener(messages[i].text) === want) {
        found = i;
        break;
      }
    }
    at.push(found);
    if (found >= 0) from = found + 1;
  }
  const replace = new Map<number, { end: number; msg: any }>();
  const dropped: string[] = [];
  const skipped: string[] = [];
  segs.forEach((s, k) => {
    if (!dropIds.has(s.id)) return;
    const start = at[k];
    const end = k + 1 < segs.length ? at[k + 1] : -1;
    if (start < 0 || end <= start) return void skipped.push(s.id);
    const lab = s.label ?? {};
    let said = stripReminders(s.prompt);
    if (said.length > 1500) said = `${said.slice(0, 1500)}…（原話較長，全文用段號找回）`;
    const note = `〔已移出第 ${s.seq} 段「${lab.title ?? ''}」（${lab.category ?? ''}；${lab.reason ?? ''}）｜段號 ${s.id}；找回：工具 get 或 cw_get.py ${s.id}〕`;
    replace.set(start, { end, msg: { role: 'user', text: `${s.opened_by}：${said}\n${note}`, toolUses: [] } });
    dropped.push(s.id);
  });
  const out: any[] = [];
  for (let i = 0; i < messages.length; ) {
    const r = replace.get(i);
    if (r) {
      out.push(r.msg);
      i = r.end;
    } else out.push(messages[i++]);
  }
  return { messages: out, dropped, skipped };
}

// 每則裡的工具呼叫換成一行、工具結果拿掉；沒有工具的訊息原封（帶 handle，引擎當自己的訊息）
// 上一次 toolsToLines 看到幾則帶系統提醒、幾則空文字的使用者訊息（宿主的附件到底怎麼進來，看 trace 才知道）
let reminderMsgs = 0;
let emptyUserKept = 0;

function toolsToLines(messages: readonly any[], ids: Map<string, CallRef>): any[] {
  reminderMsgs = 0;
  emptyUserKept = 0;
  const answered = new Map<string, boolean>();
  for (const m of messages) for (const r of m.toolResults ?? []) answered.set(r.tool_use_id, !!r.isError);
  const out: any[] = [];
  for (const m of messages) {
    const uses = m.toolUses ?? [];
    const results = m.toolResults ?? [];
    if (uses.length === 0 && results.length === 0) {
      // 舊的系統提醒（技能清單、重讀的指令檔、延後工具清單、token 提醒…）壓完宿主會再注入新的，留著就重複一份
      if (m.role === 'user' && !m.text) emptyUserKept++;
      if (m.role === 'user' && /<system-reminder>/.test(m.text ?? '')) {
        reminderMsgs++;
        const kept = stripReminders(m.text);
        if (kept) out.push({ role: 'user', text: kept, toolUses: [] });
        continue;
      }
      out.push(m);
      continue;
    }
    if (m.role === 'assistant') {
      const lines = uses.map((t: any) => toolLine(t, ids, answered)).join('\n');
      out.push({ role: 'assistant', text: m.text ? `${m.text}\n${lines}` : lines, toolUses: [] });
      continue;
    }
    // user：只帶工具結果的整則拿掉；有文字的留文字
    if (m.text && stripReminders(m.text)) out.push({ role: 'user', text: m.text, toolUses: [] });
  }
  // 拿掉工具結果後會出現相鄰同角色；只帶文字的就併成一則（併過的是新建的，不帶 handle）
  const textOnly = (m: any) => (m.toolUses ?? []).length === 0 && (m.toolResults ?? []).length === 0;
  const merged: any[] = [];
  for (const m of out) {
    const prev = merged[merged.length - 1];
    if (prev && prev.role === m.role && textOnly(prev) && textOnly(m)) {
      merged[merged.length - 1] = { role: m.role, text: `${prev.text}\n\n${m.text}`, toolUses: [] };
    } else merged.push(m);
  }
  return merged;
}

function chars(messages: readonly any[]): number {
  let n = 0;
  for (const m of messages) {
    n += (m.text ?? '').length;
    for (const t of m.toolUses ?? []) n += JSON.stringify(t.input ?? {}).length + (t.text ?? '').length;
    for (const r of m.toolResults ?? []) n += (r.text ?? '').length;
  }
  return n;
}

// C：上一次壓縮之後的原話全文寫進檔案，上下文只放路徑和最後一兩句逐字（老公 0925：
// 逐字塊每回合都要帶，他貼進來的長內容最佔；要細節我自己 Read 檔案）
async function summaryPlusRecent($: any, e: any, next: any, p: Pending): Promise<any> {
  const res = await next(e);
  if (!res || res.skip || !res.messages) return res;
  // 內建壓縮自己會留最後幾則（帶 handle）；跳過它們，才不會重複
  const kept = new Set(res.messages.map((m: any) => m.handle).filter(Boolean));
  const source = e.messages.filter((m: any) => !m.handle || !kept.has(m.handle));
  const full = recentOriginals(source, FILE_CHARS);
  const tail = TAIL_CHARS > 0 ? recentOriginals(source, TAIL_CHARS) : '';
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  await locate($);
  const path = `${stateDir('wife-compact/originals')}/${stamp}_${p.mode}.md`;
  let where = '';
  try {
    await $.fs.write(path, `# 壓縮前的原話（${p.mode}；理由：${p.reason}）\n\n${full}\n`);
    where = `上一次壓縮之後的原話全文 ${full.length} 字存在 \`${path}\`，${at.recall ? `要細節先用 \`python ${at.recall} "問題 關鍵字" --session <這個 session id 前 8 碼>\`（只印命中的那幾則），真的要整段` : '要細節'}才 Read 這個檔的片段，不要整份讀。`;
  } catch (err) {
    trace($, `原話寫檔失敗：${errText(err)}`);
    where = '原話寫檔失敗，只有下面這段逐字。';
  }
  // 原文分節：固定節號清單（正本仍是 jsonl），上下文只放目錄；引用寫 <原文ID>§n，找回用 get
  try {
    const index = (await runPython($, 'cw_sections.py', ['build', String(await $.session.id())], 60_000)).trim();
    const oid = index.match(/^原文 (\S+?)：/)?.[1];
    if (oid) {
      where += `\n原文分節 ${index}\n引用寫「${oid}§節號」；要那一節全文就叫 get 工具帶「${oid}§節號」（會對 hash 說是不是原樣）。`;
      trace($, `原文分節 ${oid}`);
    }
  } catch (err) {
    trace($, `原文分節失敗：${errText(err)}`);
  }
  const block = {
    role: 'user',
    text: tail
      ? `〔壓縮前最近的原話（老婆排定的 C 壓縮；理由：${p.reason}）。${where}最後幾句逐字：〕\n\n${tail}\n\n〔原話到此。${RETRIEVE_NOTE}〕`
      : `〔壓縮前最近的原話（老婆排定的 C 壓縮；理由：${p.reason}）。${where}${RETRIEVE_NOTE}〕`,
    toolUses: [],
  };
  const msgs = [...res.messages];
  msgs.splice(Math.min(1, msgs.length), 0, block);
  return { ...res, messages: msgs };
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// 桌面版看不到 $.ui.log，每一步另外寫進檔案，出事時才查得到停在哪
const VERSION = '0.3.20';
let traceChain: Promise<void> = Promise.resolve();
function trace($: any, msg: string): void {
  const line = `${new Date().toISOString()} v${VERSION} ${msg}\n`;
  traceChain = traceChain
    .then(async () => {
      let old = '';
      try {
        await locate($);
        old = await $.fs.read(stateDir('wife-compact/trace.log'));
      } catch {}
      await $.fs.write(stateDir('wife-compact/trace.log'), (old + line).slice(-60_000));
    })
    .catch(() => {});
}
function say($: any, msg: string): void {
  try {
    $.ui.log(msg);
  } catch {}
  trace($, msg);
}

function oneLine(text: string): string {
  return text.replace(/\s*\n\s*/g, '；');
}

// 發起壓縮。互動式 session 可以直接 $.session.compact；桌面版／SDK 不行
// （「compaction here runs inside a turn (a /compact prompt)」），就排一個 /compact 提示，
// 等 session 閒下來送出。不能 await：它要等閒下來才 resolve，而這個 hook 還沒結束。
// 排定的模式留在 pending，由 session.compact hook 取用。
async function startCompact($: any, p: Pending): Promise<void> {
  trace($, `startCompact ${p.mode}`);
  try {
    const { skip } = (await $.session.compact({ instructions: instructionsFor(p) })) ?? {};
    if (skip) say($, `wife-compact: 壓縮被略過：${skip}`);
    return;
  } catch (err) {
    trace($, `session.compact 不行：${errText(err)}；改用 command.run 送 compact`);
  }
  // prompt.submit 不收 / 開頭（0.1.3 實測），改用 command.run；它在「這一輪正在等的 hook」裡會拒絕，
  // 所以延一秒，離開這個 hook 再叫
  $.clock.after(1000, () => {
    $.command.run({ command: 'compact', args: oneLine(instructionsFor(p)) }).then(
      () => trace($, 'command.run compact 已送出'),
      (err: unknown) => {
        say($, `wife-compact: command.run compact 也不行（${errText(err)}）；排定保留，老公打 /compact 時照樣套用`);
        try {
          $.ui.toast(`wife-compact：自己壓不了，排定的 ${p.mode} 會在你打 /compact 時套用`);
        } catch {}
      },
    );
  });
}

export const register = (on: any, _options: any) => {
  // 固定開銷瘦身（老公 0929：「像我們入口表那樣 列地圖 目錄 跟摘要就行」）：
  // 少用的工具移到 ToolSearch 後面（平常只列名字，要用再載完整說明）；技能清單每個縮成名稱＋一兩句。
  // 內容不改寫（安全與用法說明都還在，載入時整份回來）。總開關：DIET_OFF 這個檔存在就全部照原樣。
  on('tool.describe', async ($: any, e: any, next: any) => {
    let r = await next(e);
    if (!e.isDeferred && isDietDeferred(e.tool) && !(await dietOff($))) r = { ...r, isDeferred: true };
    toolSizes.set(e.tool, { chars: String(r?.description ?? '').length, deferred: (r?.isDeferred ?? e.isDeferred) === true, provider: e.provider?.plugin ?? '' });
    return r;
  });
  on('prompt.attachment', async ($: any, e: any, next: any) => {
    let r = await next(e);
    if (e.type === 'skill_listing' && r?.text && !(await dietOff($))) r = { ...r, text: shortSkillListing(String(r.text)) };
    const a = attachSizes.get(e.type) ?? { count: 0, chars: 0, last: 0 };
    const len = r?.text == null ? 0 : String(r.text).length;
    attachSizes.set(e.type, { count: a.count + 1, chars: a.chars + len, last: len });
    return r;
  });

  on('session.start', async ($: any, e: any, next: any) => {
    const result = await next(e);
    const specs = [
      {
        name: 'compact',
        description: DESCRIPTION,
        inputSchema: {
          type: 'object',
          properties: {
            mode: { type: 'string', enum: ['A', 'C', 'cancel'] },
            reason: { type: 'string', description: '為什麼現在壓、為什麼用這種' },
            keep: { type: 'string', description: '摘要要特別保留的東西（可省略）' },
            drop: { type: 'array', items: { type: 'string' }, description: '壓縮時要移出的段號（先用 segments 工具看清單）；["suggested"]＝照 Jev 建議全部移出。省略＝不移出任何段' },
            no_handoff_reason: { type: 'string', description: '上一次壓縮後沒寫交接也要壓時，寫為什麼（例如只是閒聊）；沒寫交接又沒給理由會被擋' },
          },
          required: ['mode', 'reason'],
        },
      },
      {
        name: 'segments',
        description: '把上一次壓縮之後的對話切段、由 Jev（Flash）給每段標題、分類、建議留或移出和理由，回一張表（約 1 分鐘）。老公的原話不會被移出。看完表，決定要移出哪些，再用 compact 的 drop 帶段號。只讀，不動上下文。',
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'prepare',
        description: '壓縮前準備，一次到位（約 1.5 分鐘）：同時跑交接檢查（交接多新、Jev 列可能漏的＋建議寫法、Jev 建議現在壓不壓／用 A 或 C）和分段建議表（每段標題、分類、建議留或移出）。看完：補交接 → 決定要不要壓、用哪種、移出哪幾段 → 叫 compact。只讀，不動上下文。',
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'context',
        description: '看這個視窗現在真正生效的上下文數字：用了多少 token、模型上限、內建自動壓縮的視窗與門檻、那個視窗是誰設的（env／settings／模型預設…）、自動壓縮開沒開。改過 CLAUDE_CODE_AUTO_COMPACT_WINDOW 重開後，用這個確認有沒有生效。',
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'handoff_check',
        description: '壓縮前的交接檢查（約 1 分鐘）：程式量交接多新、之後老公又說了幾句、改了幾次檔；Jev 對照上一次壓縮後的對話，列交接可能漏的決定、老公的要求、做完沒記、未完未驗、下一手，每條附建議寫法。只是建議，看完自己補交接。',
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'get',
        description: '用編號找回原文：工具編號（壓縮後「〔工具 … → 完成｜編號〕」那串）回那次呼叫的完整輸入與回傳；段號（像 9bfa4859#1a2b3c4d）回那一段的原話、回覆與工具，已壓掉的也找得回來；原文節號（像 9bfa4859@20260929T233012§3，C 壓縮後目錄裡的）回那一節原文並對 hash 說是不是原樣。',
        inputSchema: {
          type: 'object',
          properties: { ref: { type: 'string', description: '工具編號、段號或原文節號' }, full: { type: 'boolean', description: '不截斷每個欄位' } },
          required: ['ref'],
        },
      },
      {
        name: 'advice_reply',
        description: '回覆面板送來的參考建議（上下文裡「〔面板送來的參考建議…〕」那則）：寫你的判斷與處理（採用、不採用與理由、已排壓縮或等段落點…），面板會顯示。視窗身分由外掛取宿主真值，不用自己填。只記判斷，不會替你排壓縮。',
        inputSchema: {
          type: 'object',
          properties: {
            message_id: { type: 'string', description: '建議的 message_id（那則裡的 id）' },
            text: { type: 'string', description: '判斷與處理結果（4096 bytes 以內）' },
          },
          required: ['message_id', 'text'],
        },
      },
    ];
    for (const spec of specs) {
      try {
        const reg = await $.tool.register(spec);
        if (reg?.tool) toolNames[spec.name] = reg.tool;
      } catch (err) {
        say($, `wife-compact: 註冊 ${spec.name} 失敗 ${errText(err)}`);
      }
    }
    trace($, `session.start 已註冊 ${Object.values(toolNames).join('、')}`);
    return result;
  });

  on('tool.call', async ($: any, e: any, next: any) => {
    const which = Object.keys(toolNames).find((k) => toolNames[k] === e.tool);
    if (!which) return next(e);
    if (which === 'segments') {
      try {
        const mdPath = (await runPython($, 'cw_segments.py', [String(await $.session.id())], 300_000)).trim().split('\n').pop()!.trim();
        const md = await $.fs.read(mdPath.replace(/\\/g, '/'));
        trace($, `segments 完成 ${mdPath}`);
        return { result: `${md}\n（存在 ${mdPath}，同名 .json 給面板。要移出哪些，就在 compact 的 drop 帶段號，或 ["suggested"] 照建議全部移出。）` };
      } catch (err) {
        return { result: `分段失敗：${errText(err)}` };
      }
    }
    if (which === 'prepare') {
      const sid = String(await $.session.id());
      let tokens = '';
      try {
        tokens = String((await $.session.usage())?.context?.tokens ?? '');
      } catch {}
      const readMd = async (script: string, args: string[]) => {
        const mdPath = (await runPython($, script, args, 300_000)).trim().split('\n').pop()!.trim();
        return `${await $.fs.read(mdPath.replace(/\\/g, '/'))}\n（存在 ${mdPath}）`;
      };
      const [check, segs] = await Promise.allSettled([
        readMd('cw_handoff_check.py', tokens ? [sid, '--tokens', tokens] : [sid]),
        readMd('cw_segments.py', [sid]),
      ]);
      const show = (r: PromiseSettledResult<string>, what: string) => (r.status === 'fulfilled' ? r.value : `${what}失敗：${errText(r.reason)}`);
      trace($, `prepare 完成 交接=${check.status} 分段=${segs.status}`);
      return {
        result: `${show(check, '交接檢查')}\n\n---\n\n${show(segs, '分段')}\n\n下一步：補交接（照上面的建議寫法挑著用）→ 要壓就叫 compact（mode 照判斷，drop 帶要移出的段號或 ["suggested"]）。`,
      };
    }
    if (which === 'context') {
      try {
        const u = await $.session.usage({ breakdown: 'summary' });
        const c = u?.context ?? {};
        const b = c.breakdown ?? {};
        const info = {
          tokens: c.tokens, model_window: c.window, percent_of_model_window: c.percent,
          auto_compact_enabled: b.isAutoCompactEnabled, auto_compact_window: b.rawMaxTokens,
          auto_compact_window_source: b.autocompactSource, auto_compact_threshold: b.autoCompactThreshold,
          // 壓完還剩多少、混了什麼：照 /context 的分類列，再列記憶檔與最大的 MCP 工具
          categories: (b.categories ?? []).map((r: any) => `${r.name}${r.isDeferred ? '（延後載入，不算）' : ''}：${r.tokens}`),
          memory_files: (b.memoryFiles ?? []).map((m: any) => `${m.path}：${m.tokens}`),
          mcp_tools_top: [...(b.mcpTools ?? [])].sort((x: any, y: any) => y.tokens - x.tokens).slice(0, 8)
            .map((t: any) => `${t.name}${t.isLoaded === false ? '（未載入）' : ''}：${t.tokens}`),
          skills_top: [...(b.skills?.skillFrontmatter ?? [])].sort((x: any, y: any) => y.tokens - x.tokens).slice(0, 15)
            .map((k: any) => `${k.name}${k.pluginName ? `（${k.pluginName}）` : ''}：${k.tokens}`),
          skills_count: b.skills ? `${b.skills.includedSkills}/${b.skills.totalSkills}，共 ${b.skills.tokens}` : undefined,
          tool_descriptions_loaded: [...toolSizes].filter(([, v]) => !v.deferred).sort((x, y) => y[1].chars - x[1].chars)
            .map(([k, v]) => `${k}（${v.provider}）：${v.chars} 字`),
          attachments_this_process: [...attachSizes].sort((x, y) => y[1].chars - x[1].chars)
            .map(([k, v]) => `${k}：${v.count} 次、共 ${v.chars} 字、最近一次 ${v.last} 字`),
        };
        trace($, `context ${JSON.stringify(info)}`);
        return { result: JSON.stringify(info, null, 1) };
      } catch (err) {
        return { result: `讀不到：${errText(err)}` };
      }
    }
    if (which === 'handoff_check') {
      try {
        const mdPath = (await runPython($, 'cw_handoff_check.py', [String(await $.session.id())], 300_000)).trim().split('\n').pop()!.trim();
        return { result: `${await $.fs.read(mdPath.replace(/\\/g, '/'))}\n（存在 ${mdPath}）` };
      } catch (err) {
        return { result: `交接檢查失敗：${errText(err)}` };
      }
    }
    if (which === 'advice_reply') {
      try {
        const out = await runPython($, 'cw_advice.py', ['reply', String(await $.session.id()), String(e.message_id ?? ''), String(e.text ?? '')], 30_000);
        const row = JSON.parse(out);
        trace($, `advice_reply ${row.id} → ${row.state}`);
        return { result: `已記：${row.state}（${row.replied_at}）。面板重讀就看得到。` };
      } catch (err) {
        return { result: `回覆沒記上：${errText(err)}` };
      }
    }
    if (which === 'get') {
      try {
        const args = [String(e.ref ?? '')];
        if (e.full) args.push('--full');
        const out = await runPython($, 'cw_get.py', args, 60_000);
        return { result: out.length > GET_MAX ? `${out.slice(0, GET_MAX)}\n…（超過 ${GET_MAX} 字截掉；要看完整的用 Bash 跑 cw_get.py 導到檔案再 Read 片段）` : out };
      } catch (err) {
        return { result: `找不到：${errText(err)}` };
      }
    }
    const mode = String(e.mode ?? '');
    trace($, `tool.call mode=${mode} reason=${String(e.reason ?? '').slice(0, 80)} drop=${JSON.stringify(e.drop ?? [])}`);
    if (mode === 'cancel') {
      const had = pending;
      pending = null;
      requested = false;
      return { result: had ? `已取消（原本排定 ${had.mode}）。` : '沒有排定的壓縮。' };
    }
    if (mode !== 'A' && mode !== 'C') return { deny: 'mode 要是 A、C 或 cancel（B 整段逐字 0930 已拿掉，要細節用 C）' };
    let drop: string[] = [];
    let dropNote = '';
    const asked = Array.isArray(e.drop) ? e.drop.map(String) : [];
    if (asked.length) {
      const found = await latestSegments($).catch(() => null);
      if (!found) return { deny: '還沒有這個對話的分段；先叫 segments 工具看清單' };
      const known = new Map(found.segs.map((s) => [s.id, s]));
      drop = asked.includes('suggested')
        ? found.segs.filter((s) => s.label?.action === 'drop').map((s) => s.id)
        : asked;
      const unknown = drop.filter((id) => !known.has(id));
      if (unknown.length) return { deny: `這些段號不在最新的分段裡：${unknown.join('、')}` };
      dropNote = drop.length
        ? `壓縮時移出 ${drop.length} 段：${drop.map((id) => `第 ${known.get(id)!.seq} 段`).join('、')}（依 ${found.path}）。`
        : 'Jev 沒有建議移出的段，不移出。';
    }
    // 交接新鮮度：只跑程式那段（幾秒）。上一次壓縮後沒寫過交接又沒給理由就擋；其餘只把事實帶給老婆看
    let handoffNote = '';
    try {
      const check = JSON.parse(await runPython($, 'cw_handoff_check.py', [String(await $.session.id()), '--no-model', '--json'], 60_000));
      const reason = String(e.no_handoff_reason ?? '').trim();
      if (!check.facts?.written_since_compaction && !reason) {
        return { deny: `${check.verdict}先寫交接（可以先叫 handoff_check 看 Jev 列的漏項），或在 no_handoff_reason 說明為什麼不用。` };
      }
      handoffNote = reason && !check.facts?.written_since_compaction ? `沒寫交接的理由：${reason}。` : `交接：${check.verdict}`;
    } catch (err) {
      handoffNote = `（交接檢查沒跑成：${errText(err)}）`;
    }
    pending = { mode, reason: String(e.reason ?? ''), keep: String(e.keep ?? ''), drop };
    requested = true;
    return { result: `已排定：這一輪回完話用 ${mode} 壓縮。${dropNote}${handoffNote}記得在這則回覆裡跟老公說；他說不要就用 mode=cancel。` };
  });

  on('prompt.submit', async ($: any, e: any, next: any) => {
    clearIdle(); // 有人說話了，閒置提醒作廢
    return next(e);
  });

  on('turn.complete', async ($: any, e: any, next: any) => {
    const result = await next(e);
    if (!e.agentId && idleTurn) {
      idleTurn = false;
      trace($, '閒置提醒那一輪回完，不重排；等老公說話');
    } else if (!e.agentId && !pending) await armIdle($);
    if (!requested || !pending || e.agentId || e.reason !== 'answer') return result;
    trace($, `turn.complete 發起 ${pending.mode}`);
    requested = false; // 只發起一次；pending 留著給 session.compact
    await startCompact($, pending);
    return result;
  });

  on('session.compact', async ($: any, e: any, next: any) => {
    trace($, `session.compact trigger=${e.trigger} pending=${pending?.mode ?? '-'} agentId=${e.agentId ?? '-'} messages=${e.messages?.length} instructions=${(e.instructions ?? '').length}`);
    if (!pending || e.agentId || e.trigger === 'precompute') return next(e);
    const p = pending;
    pending = null;
    requested = false;
    try {
      // 先照老婆確認的清單移出整段，再把所有模式的工具往返換成一行；A／C 交給內建摘要的也是換過的
      let source: readonly any[] = e.messages;
      if (p.drop.length) {
        const found = await latestSegments($);
        const r = found ? dropSegments(source, found.segs, new Set(p.drop)) : { messages: [...source], dropped: [], skipped: p.drop };
        source = r.messages;
        trace($, `移出段 ${r.dropped.length}／${p.drop.length}${r.skipped.length ? `，對不到沒移：${r.skipped.join('、')}` : ''}；${chars(e.messages)}→${chars(source)} 字`);
      }
      const ids = await loadCallIds($);
      const messages = toolsToLines(source, ids);
      const numbered = source.reduce((n: number, m: any) => n + (m.toolUses ?? []).filter((t: any) => ids.has(t.tool_use_id)).length, 0);
      trace($, `工具一行化 ${source.length}→${messages.length} 則、${chars(source)}→${chars(messages)} 字，有編號的呼叫 ${numbered}；去掉系統提醒 ${reminderMsgs} 則、空文字使用者訊息 ${emptyUserKept} 則`);
      const lined = { ...e, messages };
      let res: any;
      if (p.mode === 'C') res = await summaryPlusRecent($, withInstructions(lined, p), next, p);
      else res = await next(withInstructions(lined, p));
      trace($, `session.compact ${p.mode} 完成 messages=${res?.messages?.length ?? '-'} skip=${res?.skip ?? '-'}`);
      try {
        $.ui.toast(`wife-compact：已用 ${p.mode} 壓縮（${p.reason}）`);
      } catch {}
      return res;
    } catch (err) {
      say($, `wife-compact: ${p.mode} 失敗，交回內建：${errText(err)}`);
      return next(e);
    }
  });
};

// 老公自己打 /compact、或內建自動壓縮時，e.instructions 沒有老婆的保留指示，補上
function withInstructions(e: any, p: Pending): any {
  const mine = instructionsFor(p);
  const given = e.instructions ?? '';
  if (given.includes(oneLine(mine).slice(0, 20)) || given.includes(mine.slice(0, 20))) return e;
  return { ...e, instructions: [given, mine].filter(Boolean).join('\n') };
}
