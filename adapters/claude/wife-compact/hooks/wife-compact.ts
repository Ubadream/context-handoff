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

let language: 'zh' | 'en' = 'zh';
const tr = (zh: string, en: string): string => language === 'en' ? en : zh;

async function refreshLanguage($: any): Promise<void> {
  try {
    const override = await $.env.get('CW_LANG');
    if (override === 'en' || override === 'zh') {
      language = override;
      return;
    }
    await locate($);
    const path = (await $.env.get('CW_THRESHOLDS')) || stateDir('context-workbench/thresholds.json');
    const value = JSON.parse(await $.fs.read(path))?.claude?.language;
    language = value === 'en' ? 'en' : 'zh';
  } catch {
    language = 'zh';
  }
}

type Mode = 'A' | 'C';
type HandoffIds = { entry: string; sidecar: string };
type Continuation = { epoch: number; queued?: boolean };
type Pending = { mode: Mode; reason: string; keep: string; drop: string[]; next: 'continue' | 'wait'; handoffIds?: HandoffIds; initiated?: boolean; promptEpoch?: number; continuation?: Continuation };

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
const RETRIEVE_NOTE = () =>
  tr('要找壓縮前的原話或工具原始輸出：叫 get 工具（帶工具編號、段號或原文 ID§節號），或讀這個 session 的 jsonl；大的工具輸出存在狀態資料夾（預設 ~/.local/state/wifeos）的 claude-tool-output/。', "To retrieve wording or raw tool output from before compaction: call get with a tool ID, segment ID, or original ID§section number, or read this session jsonl. Large tool outputs are in claude-tool-output/ under the state directory (default ~/.local/state/wifeos).");

let pending: Pending | null = null; // 排定的模式，session.compact 取用後清掉
let requested = false; // 這一輪結束要不要由老婆發起
let promptEpoch = 0; // 壓縮期間或排程送出前有新輸入，就讓那則輸入接手。
let continuation: Continuation | null = null;
const CONTINUE_PROMPT = () => tr('〔wife-compact：壓縮完成，續作〕這是外掛提示，不是老公的新要求。讀交接，從「下一步」接著做；只續做已授權未完的工作，等老公決定的事仍等待。壓縮前老公最後一句不是新要求，已做完的不要重做。', "[wife-compact: compaction complete, continue] This is a plugin prompt, not a new user request. Read the handoff and resume from the next step; continue only authorized unfinished work, and keep waiting on user decisions. The last user message before compaction is not a new request; do not repeat completed work.");
const toolSizes = new Map<string, { chars: number; deferred: boolean; provider: string }>(); // tool.describe 量到的說明長度
const attachSizes = new Map<string, { count: number; chars: number; last: number }>(); // prompt.attachment 各類附件的次數與字數
const toolNames: Record<string, string> = {}; // 名稱 → 宿主給的完整工具名（mcp__wife-compact__…）

const DESCRIPTION = () => [
  tr('老婆自己排定一次上下文壓縮：這一輪回完話就壓。老公同意由老婆判斷時機與方式（2026-09-25）。', "Schedule one context compaction after this reply finishes. The user authorized you to choose its timing and mode (2026-09-25)."),
  tr('時機：上下文大約 40 萬以上才考慮；只在一段工作做完、交接或記憶剛更新好的段落點；工具鏈做到一半、老公正在講的事還沒接完就不壓；超過 80 萬就在下一個段落點壓。', "Timing: consider compaction above about 400,000 tokens, only at a work boundary after updating the handoff or memory. Wait if a tool chain or the user discussion is ongoing. Above 800,000 tokens, compact at the next work boundary."),
  tr('同一則回覆裡先跟老公說「這輪結束用 X 壓，因為…」，他說不要就用 mode=cancel 取消。', "In the same reply, tell the user the mode and reason first. If the user declines, use mode=cancel."),
  tr('mode：A＝內建摘要（預設；交接剛寫好、段落乾淨時）；C＝內建摘要＋上一次壓縮後的原話全文存檔、上下文只留路徑（細節討論正熱、之後可能要回頭查原話時；壓完要細節就 Read 那個檔）；cancel＝取消已排定的。兩種模式都會把工具往返換成一行（附編號，全文用 get 工具拿編號找回）。', "mode: A = native summary (default at a clean boundary with a fresh handoff); C = native summary plus an archive of all original wording since the last compaction, leaving only a path in context (for detailed discussions that may need later retrieval; Read the file for details); cancel = cancel the schedule. Both modes collapse tool exchanges to one line with an ID; use get to retrieve the full exchange."),
  tr('交接：上一次壓縮後沒寫過交接會被擋（除非給 no_handoff_reason）；壓前可以先叫 handoff_check 看 Jev 列的漏項再補。', "Handoff: compaction is blocked if none has been written since the last compaction (unless no_handoff_reason is provided). Call handoff_check first to review Jev omissions and update the handoff."),
  tr('drop：想順便移出沒用的段，先叫 segments 工具看 Jev 的建議表，對照後把要移出的段號放進 drop（或 ["suggested"] 照建議全部移出）；老公開頭的段會留他的原話一行，其餘整段移出、原文用 get 找回。', "drop: to remove unneeded segments, first call segments and review Jev suggestions, then pass selected segment IDs in drop (or [\"suggested\"] to remove all suggested segments). User-opened segments retain one line of original user wording; the rest is removed and retrievable with get."),
  tr('keep：要摘要特別保留的東西（檔案、決定、未完成事項），A／C 會交給摘要器。', "keep: items the summary must preserve (files, decisions, unfinished work); passed to the summarizer in A/C."),
  tr('next：交接裡有已授權未完的工作選 continue，壓縮完成後外掛會送續作提示；收工或正在等老公決定選 wait（預設，相容舊呼叫）。', "next: choose continue when the handoff contains authorized unfinished work; the plugin sends a continuation prompt after compaction. Choose wait when finished or waiting for the user (default, compatible with older calls)."),
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

// 交接分三塊（選用，預設關；cw_thresholds.py set --handoff-sections on）。prepare／handoff_check 的 Python 輸出自己帶，
// 這裡只補外掛自己寫的字：閒置提醒和交接檢查失敗。字面跟 cw_paths.HANDOFF_REMINDER 同一句。
const HANDOFF_REMINDER = () => tr('交接：決策／情感／工作進度，各附原話定位；無則寫無，錯誤入簿。', "Handoff: Decisions / Feelings / Work progress, each with original-text references; write none when absent, and log errors in the error log.");

async function handoffReminder($: any): Promise<string> {
  try {
    await locate($);
    const on = JSON.parse(await $.fs.read((await $.env.get('CW_THRESHOLDS')) || stateDir('context-workbench/thresholds.json')))?.claude?.handoff_sections === true;
    return on ? `\n${HANDOFF_REMINDER()}` : '';
  } catch {
    return '';
  }
}

function clearIdle(): void {
  if (idleTimer != null && typeof clearTimeout === 'function') clearTimeout(idleTimer);
  idleTimer = null;
}

async function armIdle($: any): Promise<void> {
  clearIdle();
  if (typeof setTimeout !== 'function') {
    trace($, tr('閒置提醒：這個宿主沒有 setTimeout，排不了', "Idle reminder: this host has no setTimeout; cannot schedule"));
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
  const reminder = await handoffReminder($);
  idleTimer = setTimeout(() => {
    idleTimer = null;
    idleTurn = true;
    const text = tr(`〔wife-compact：閒置提醒〕外掛送的，不是老公。老公已經 ${cfg.minutes} 分鐘沒說話，上下文約 ${wan} 萬；提示快取約一小時過期，過期後他回來要全價重讀整份。現在先叫 prepare、把交接寫好讀回；是段落點就排 compact（照判斷選模式）。正在等老公決定的事寫進交接，不要自己往下做；回一兩句就好。${reminder}`, `[wife-compact: idle reminder] Sent by the plugin, not the user. No user message for ${cfg.minutes} minutes; context is about ${tokens.toLocaleString("en-US")} tokens. Prompt cache expires after about an hour; returning afterward requires a full-price reread. Call prepare, write and read back the handoff; at a work boundary, schedule compact with the mode you choose. Record pending user decisions in the handoff and wait for them; reply in one or two sentences.${reminder}`);
    Promise.resolve($.prompt.submit({ text }))
      .then(() => trace($, tr(`閒置提醒已送（約 ${wan} 萬）`, `Idle reminder sent (about ${tokens.toLocaleString("en-US")} tokens)`)))
      .catch((err: unknown) => trace($, tr(`閒置提醒送不出：${errText(err)}`, `Idle reminder could not be sent: ${errText(err)}`)));
  }, ms);
  trace($, tr(`閒置提醒已排：約 ${wan} 萬，${cfg.minutes} 分後`, `Idle reminder scheduled: about ${tokens.toLocaleString("en-US")} tokens, in ${cfg.minutes} minutes`));
}

function stripReminders(text: string): string {
  return text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '').trim();
}

function instructionsFor(p: Pending): string {
  const parts = [
    tr('這是老婆（助理）自己排定的壓縮。摘要用繁體中文，保留：正在做的工作與下一步、老公（使用者）說過的決定與原話重點、改過的檔案路徑、已驗與未驗。', "This compaction was scheduled by you, the agent. Write the summary in English. Preserve: current work and next steps, user decisions and key original wording, modified file paths, verified and unverified items."),
  ];
  if (p.keep) parts.push(tr(`特別保留：${p.keep}`, `Preserve in particular: ${p.keep}`));
  parts.push(tr(`壓縮理由：${p.reason}`, `Compaction reason: ${p.reason}`));
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
    if (text.startsWith('This session is being continued from a previous conversation') || text.startsWith('〔壓縮前最近的原話') || text.startsWith('[Recent original wording before compaction')) break;
    const who = m.role === 'user' ? tr('老公', "the user") : tr('老婆', "the agent");
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
    trace($, tr(`讀不到工具編號（照樣一行化，只是沒編號）：${errText(err)}`, `Cannot read tool IDs (still collapsing to lines, without IDs): ${errText(err)}`));
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

const STATUS_WORD = (): Record<string, string> => ({ ok: tr('完成', "completed"), failed: tr('失敗', "failed"), interrupted: tr('中斷', "interrupted") });

// answered：tool_use_id → 是否錯誤，從後面 user 訊息的 toolResults 收（呼叫本身不一定帶結果）
function toolLine(tool: any, ids: Map<string, CallRef>, answered: Map<string, boolean>): string {
  const ref = ids.get(tool.tool_use_id);
  const answeredError = answered.get(tool.tool_use_id);
  const status = ref?.status ? (STATUS_WORD()[ref.status] ?? ref.status)
    : tool.isError || answeredError ? tr('失敗', "failed")
    : answeredError === false || tool.text !== undefined || tool.result !== undefined ? tr('完成', "completed") : tr('沒有回件', "no response");
  const target = toolTarget(tool.input ?? {});
  const name = String(tool.tool ?? '?').replace(/^mcp__/, '');
  return tr(`〔工具 ${name}${target ? ` ${target}` : ''} → ${status}${ref ? `｜${ref.id}` : ''}〕`, `[Tool ${name}${target ? ` ${target}` : ""} -> ${status}${ref ? ` | ${ref.id}` : ""}]`);
}

// ── 分段篩選（context-workbench 的 cw_segments.py：程式切段、Jev 建議、老婆對照後決定）──
// 老公 0929：「程式可以主動列出 篩選掉的 然後她們在對照 … 保存起來 編號 後面需要可以找回」
type Seg = { seq: number; id: string; prompt: string; opened_by: string; label?: { title?: string; category?: string; action?: string; reason?: string; keeps?: string } | null };

async function runPython($: any, script: string, args: string[], timeoutMs: number): Promise<string> {
  await locate($);
  if (!at.workbench) throw new Error(tr('沒設 CW_WORKBENCH：跑 adapters/claude/cw_install.py apply 再重開', "CW_WORKBENCH is not set: run adapters/claude/cw_install.py apply and restart"));
  const r = await $.process.run([at.python, '-X', 'utf8', `${at.workbench}/adapters/claude/${script}`, ...args], { cwd: at.workbench, timeoutMs });
  if (r.exitCode !== 0) throw new Error(tr(`${script} 結束碼 ${r.exitCode}：${String(r.stderr || r.stdout).slice(-600)}`, `${script} exited with ${r.exitCode}: ${String(r.stderr || r.stdout).slice(-600)}`));
  return String(r.stdout);
}

// 原文範圍同步存；快照只交給背景入口。兩段失敗都不能擋壓縮，也不重試。
async function prepareHandoffIds($: any, facts: any): Promise<{ ids?: HandoffIds; note: string }> {
  const session = facts?.session_file;
  const handoff = facts?.handoffs?.[0]?.path;
  if (!facts?.written_since_compaction || !session || !handoff) return { note: '' };
  try {
    // 交接原文 ID 由外部記憶庫提供；沒設入口就不做（公開版沒有預設路徑）
    const entry = await $.env.get('WM_HANDOFF_IDS_ENTRY');
    if (!entry || !await $.fs.exists(entry)) return { note: '' };
    const r = await $.process.run([at.python, '-X', 'utf8', entry, 'prepare', '--session-file', session, '--handoff', handoff],
      { cwd: at.workbench, timeoutMs: 15_000 });
    if (r.exitCode !== 0) throw new Error(`exit ${r.exitCode}：${String(r.stderr || r.stdout).slice(-600)}`);
    const sidecar = String(r.stdout).trim().split(/\r?\n/).pop()!.trim();
    if (sidecar === 'DISABLED') return { note: '' };
    if (!sidecar || !await $.fs.exists(sidecar)) throw new Error(tr('入口沒有回傳存在的 sidecar 路徑', "Entry point did not return an existing sidecar path"));
    return { ids: { entry, sidecar }, note: tr(`\n原文範圍已存：${sidecar}\n`, `
Original-text range saved: ${sidecar}
`) };
  } catch (err) {
    return { note: tr(`\n軟性提醒（不擋壓縮）：原文範圍沒存成（${errText(err).replace(/\s+/g, ' ')}）。\n`, `
Advisory (does not block compaction): original-text range was not saved (${errText(err).replace(/\s+/g, " ")}).
`) };
  }
}

async function spawnHandoffSnapshot($: any, ids: HandoffIds): Promise<void> {
  try {
    const r = await $.process.run([at.python, '-X', 'utf8', ids.entry, 'spawn-snapshot', '--sidecar', ids.sidecar],
      { cwd: at.workbench, timeoutMs: 10_000 });
    if (r.exitCode !== 0) throw new Error(`exit ${r.exitCode}：${String(r.stderr || r.stdout).slice(-600)}`);
    const output = String(r.stdout).trim();
    trace($, output === 'DISABLED' ? 'handoff_ids spawn-snapshot DISABLED' : tr(`handoff_ids spawn-snapshot 已交出：${output}`, `handoff_ids spawn-snapshot dispatched: ${output}`));
  } catch (err) {
    trace($, tr(`handoff_ids spawn-snapshot 失敗（不影響壓縮）：${errText(err)}`, `handoff_ids spawn-snapshot failed (compaction unaffected): ${errText(err)}`));
  }
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
    if (said.length > 1500) said = tr(`${said.slice(0, 1500)}…（原話較長，全文用段號找回）`, `${said.slice(0, 1500)}... (long original wording; retrieve the full text by segment ID)`);
    const note = tr(`〔已移出第 ${s.seq} 段「${lab.title ?? ''}」（${lab.category ?? ''}；${lab.reason ?? ''}）｜段號 ${s.id}；找回：工具 get 或 cw_get.py ${s.id}〕`, `[Removed segment ${s.seq} "${lab.title ?? ""}" (${lab.category ?? ""}; ${lab.reason ?? ""}) | segment ID ${s.id}; retrieve with get or cw_get.py ${s.id}]`);
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
    await $.fs.write(path, tr(`# 壓縮前的原話（${p.mode}；理由：${p.reason}）\n\n${full}\n`, `# Original wording before compaction (${p.mode}; reason: ${p.reason})

${full}
`));
    where = tr(`上一次壓縮之後的原話全文 ${full.length} 字存在 \`${path}\`，${at.recall ? `要細節先用 \`python ${at.recall} "問題 關鍵字" --session <這個 session id 前 8 碼>\`（只印命中的那幾則），真的要整段` : '要細節'}才 Read 這個檔的片段，不要整份讀。`, `All original wording since the last compaction (${full.length} characters) is saved in \`${path}\`. ${at.recall ? `For details, first use \`python ${at.recall} "question keywords" --session <first 8 characters of this session ID>\` (prints only matching messages); if more context is needed, ` : "For details, "}Read relevant portions of this file rather than the whole file.`);
  } catch (err) {
    trace($, tr(`原話寫檔失敗：${errText(err)}`, `Failed to archive original wording: ${errText(err)}`));
    where = tr('原話寫檔失敗，只有下面這段逐字。', "Failed to archive original wording; only the verbatim text below remains.");
  }
  // 原文分節：固定節號清單（正本仍是 jsonl），上下文只放目錄；引用寫 <原文ID>§n，找回用 get
  try {
    const index = (await runPython($, 'cw_sections.py', ['build', String(await $.session.id())], 60_000)).trim();
    const oid = index.match(/^(?:原文 |Original )(\S+?)[：:]/)?.[1];
    if (oid) {
      where += tr(`\n原文分節 ${index}\n引用寫「${oid}§節號」；要那一節全文就叫 get 工具帶「${oid}§節號」（會對 hash 說是不是原樣）。`, `
Original sections ${index}
Cite "${oid}§section_number"; call get with that reference to retrieve a section and check its hash.`);
      trace($, tr(`原文分節 ${oid}`, `Original sections ${oid}`));
    }
  } catch (err) {
    trace($, tr(`原文分節失敗：${errText(err)}`, `Failed to index original text: ${errText(err)}`));
  }
  const block = {
    role: 'user',
    text: tail
      ? tr(`〔壓縮前最近的原話（老婆排定的 C 壓縮；理由：${p.reason}）。${where}最後幾句逐字：〕\n\n${tail}\n\n〔原話到此。${RETRIEVE_NOTE()}〕`, `[Recent original wording before compaction (agent-scheduled C compaction; reason: ${p.reason}). ${where}Last messages, verbatim:]

${tail}

[End of original wording. ${RETRIEVE_NOTE()}]`)
      : tr(`〔壓縮前最近的原話（老婆排定的 C 壓縮；理由：${p.reason}）。${where}${RETRIEVE_NOTE()}〕`, `[Recent original wording before compaction (agent-scheduled C compaction; reason: ${p.reason}). ${where}${RETRIEVE_NOTE()}]`),
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
const VERSION = '0.3.23';
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

function queueContinuation($: any, ticket?: Continuation): void {
  if (!ticket || ticket.queued || continuation !== ticket) return;
  ticket.queued = true;
  // 由宿主壓縮呼叫完成的回呼排送；手動 /compact 才從 hook 延後送。
  // 不 await prompt.submit（它可能要等這一輪閒下來），也不在失敗後重送。
  try {
    $.clock.after(1000, () => {
      if (continuation !== ticket || promptEpoch !== ticket.epoch) {
        if (continuation === ticket) continuation = null;
        trace($, tr('續作已取消：有新輸入、取消或另一次壓縮', "Continuation cancelled: new input, cancellation, or another compaction"));
        return;
      }
      continuation = null; // 送之前消耗票，同一壓縮最多嘗試一次，包括失敗。
      clearIdle();
      Promise.resolve().then(async () => {
        if (promptEpoch !== ticket.epoch) {
          trace($, tr('續作已取消：送出前有新輸入', "Continuation cancelled: new input before sending"));
          return;
        }
        await $.prompt.submit({ text: CONTINUE_PROMPT() });
        trace($, tr('壓縮後續作提示已送出', "Post-compaction continuation prompt sent"));
      })
        .catch((err: unknown) => trace($, tr(`壓縮後續作提示送不出：${errText(err)}`, `Post-compaction continuation prompt could not be sent: ${errText(err)}`)));
    });
  } catch (err) {
    if (continuation === ticket) continuation = null;
    trace($, tr(`壓縮後續作排程失敗：${errText(err)}`, `Failed to schedule post-compaction continuation: ${errText(err)}`));
  }
}

function compactFinished($: any, p: Pending, ticket: Continuation | null, result: any): void {
  if (!ticket) return;
  if (!result?.messages?.length || result.skip) {
    if (continuation === ticket) continuation = null;
    return;
  }
  p.continuation = ticket;
  if (!p.initiated) queueContinuation($, ticket);
}

// 發起壓縮。互動式 session 可以直接 $.session.compact；桌面版／SDK 不行
// （「compaction here runs inside a turn (a /compact prompt)」），就排一個 /compact 提示，
// 等 session 閒下來送出。不能 await：它要等閒下來才 resolve，而這個 hook 還沒結束。
// 排定的模式留在 pending，由 session.compact hook 取用。
async function startCompact($: any, p: Pending): Promise<void> {
  trace($, `startCompact ${p.mode}`);
  p.initiated = true;
  p.promptEpoch = promptEpoch;
  try {
    const { skip } = (await $.session.compact({ instructions: instructionsFor(p) })) ?? {};
    if (skip) {
      if (continuation === p.continuation) continuation = null;
      say($, tr(`wife-compact: 壓縮被略過：${skip}`, `wife-compact: compaction skipped: ${skip}`));
    } else queueContinuation($, p.continuation);
    return;
  } catch (err) {
    trace($, tr(`session.compact 不行：${errText(err)}；改用 command.run 送 compact`, `session.compact unavailable: ${errText(err)}; using command.run compact`));
  }
  // prompt.submit 不收 / 開頭（0.1.3 實測），改用 command.run；它在「這一輪正在等的 hook」裡會拒絕，
  // 所以延一秒，離開這個 hook 再叫
  $.clock.after(1000, () => {
    $.command.run({ command: 'compact', args: oneLine(instructionsFor(p)) }).then(
      () => {
        trace($, tr('command.run compact 已送出', "command.run compact sent"));
        queueContinuation($, p.continuation);
      },
      (err: unknown) => {
        if (continuation === p.continuation) continuation = null;
        say($, tr(`wife-compact: command.run compact 也不行（${errText(err)}）；排定保留，老公打 /compact 時照樣套用`, `wife-compact: command.run compact also failed (${errText(err)}); schedule retained for the next user /compact`));
        try {
          $.ui.toast(tr(`wife-compact：自己壓不了，排定的 ${p.mode} 會在你打 /compact 時套用`, `wife-compact: cannot initiate compaction; scheduled ${p.mode} will apply when the user enters /compact`));
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
    await refreshLanguage($);
    const result = await next(e);
    const specs = [
      {
        name: 'compact',
        description: DESCRIPTION(),
        inputSchema: {
          type: 'object',
          properties: {
            mode: { type: 'string', enum: ['A', 'C', 'cancel'] },
            next: { type: 'string', enum: ['continue', 'wait'], default: 'wait', description: tr('交接有已授權未完工作選 continue；收工或等老公決定選 wait（預設）', "Choose continue for authorized unfinished work in the handoff; choose wait when finished or waiting for the user (default)") },
            reason: { type: 'string', description: tr('為什麼現在壓、為什麼用這種', "Why compact now, and why this mode") },
            keep: { type: 'string', description: tr('摘要要特別保留的東西（可省略）', "Items the summary must preserve (optional)") },
            drop: { type: 'array', items: { type: 'string' }, description: tr('壓縮時要移出的段號（先用 segments 工具看清單）；["suggested"]＝照 Jev 建議全部移出。省略＝不移出任何段', "Segment IDs to remove during compaction (review segments first); [\"suggested\"] removes all Jev suggestions. Omit to keep all segments") },
            no_handoff_reason: { type: 'string', description: tr('上一次壓縮後沒寫交接也要壓時，寫為什麼（例如只是閒聊）；沒寫交接又沒給理由會被擋', "Reason to compact without a handoff written since the last compaction (e.g. casual chat); blocked if both handoff and reason are absent") },
          },
          required: ['mode', 'reason'],
        },
      },
      {
        name: 'segments',
        description: tr('把上一次壓縮之後的對話切段、由 Jev（Flash）給每段標題、分類、建議留或移出和理由，回一張表（約 1 分鐘）。老公的原話不會被移出。看完表，決定要移出哪些，再用 compact 的 drop 帶段號。只讀，不動上下文。', "Segment the conversation since the last compaction and ask Jev (Flash) for titles, categories, keep/drop suggestions and reasons; returns a table (about 1 minute). Original user wording is retained. Review the table, then pass chosen IDs to compact.drop. Read-only; does not change context."),
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'prepare',
        description: tr('壓縮前準備，一次到位（約 1.5 分鐘）：同時跑交接檢查（交接多新、Jev 列可能漏的＋建議寫法、Jev 建議現在壓不壓／用 A 或 C）和分段建議表（每段標題、分類、建議留或移出）。看完：補交接 → 決定要不要壓、用哪種、移出哪幾段 → 叫 compact。只讀，不動上下文。', "Prepare for compaction in one call (about 1.5 minutes): concurrently check handoff freshness, Jev omissions and suggested wording, timing/mode advice, and segment suggestions. Then update the handoff, choose whether/how to compact and which segments to remove, and call compact. Read-only; does not change context."),
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'context',
        description: tr('看這個視窗現在真正生效的上下文數字：用了多少 token、模型上限、內建自動壓縮的視窗與門檻、那個視窗是誰設的（env／settings／模型預設…）、自動壓縮開沒開。改過 CLAUDE_CODE_AUTO_COMPACT_WINDOW 重開後，用這個確認有沒有生效。', "Show effective context usage, model limit, automatic compaction window and threshold, their source (env/settings/model default), and whether automatic compaction is enabled. Use after changing CLAUDE_CODE_AUTO_COMPACT_WINDOW and restarting to verify the effective setting."),
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'handoff_check',
        description: tr('壓縮前的交接檢查（約 1 分鐘）：程式量交接多新、之後老公又說了幾句、改了幾次檔；Jev 對照上一次壓縮後的對話，列交接可能漏的決定、老公的要求、做完沒記、未完未驗、下一手，每條附建議寫法。只是建議，看完自己補交接。', "Pre-compaction handoff check (about 1 minute): measure freshness and later user messages/file edits. Jev compares conversation segments since the last compaction and suggests missing decisions, user requests, unrecorded completed work, unfinished/unverified work, and next steps, with suggested wording. Advisory; update the handoff yourself."),
        inputSchema: { type: 'object', properties: {} },
      },
      {
        name: 'get',
        description: tr('用編號找回原文：工具編號（壓縮後「〔工具 … → 完成｜編號〕」那串）回那次呼叫的完整輸入與回傳；段號（像 9bfa4859#1a2b3c4d）回那一段的原話、回覆與工具，已壓掉的也找得回來；原文節號（像 9bfa4859@20260929T233012§3，C 壓縮後目錄裡的）回那一節原文並對 hash 說是不是原樣。', "Retrieve original text by ID: tool IDs from collapsed tool lines return full inputs and responses; segment IDs (e.g. 9bfa4859#1a2b3c4d) return original messages, replies, and tools, including compacted segments; section IDs (e.g. 9bfa4859@20260929T233012§3, listed after C compaction) return section text and check its hash."),
        inputSchema: {
          type: 'object',
          properties: { ref: { type: 'string', description: tr('工具編號、段號或原文節號', "Tool ID, segment ID, or original section ID") }, full: { type: 'boolean', description: tr('不截斷每個欄位', "Do not truncate individual fields") } },
          required: ['ref'],
        },
      },
      {
        name: 'advice_reply',
        description: tr('回覆面板送來的參考建議（上下文裡「〔面板送來的參考建議…〕」那則）：寫你的判斷與處理（採用、不採用與理由、已排壓縮或等段落點…），面板會顯示。視窗身分由外掛取宿主真值，不用自己填。只記判斷，不會替你排壓縮。', "Reply to advisory suggestions sent by the panel: report your decision and handling (accept/reject with reasons, scheduled compaction or waiting for a boundary) for the panel to display. Session identity comes from the host automatically. Records your decision only; does not schedule compaction."),
        inputSchema: {
          type: 'object',
          properties: {
            message_id: { type: 'string', description: tr('建議的 message_id（那則裡的 id）', "The suggestion message_id (id in the message)") },
            text: { type: 'string', description: tr('判斷與處理結果（4096 bytes 以內）', "Decision and handling result (up to 4096 bytes)") },
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
        say($, tr(`wife-compact: 註冊 ${spec.name} 失敗 ${errText(err)}`, `wife-compact: failed to register ${spec.name}: ${errText(err)}`));
      }
    }
    trace($, tr(`session.start 已註冊 ${Object.values(toolNames).join('、')}`, `session.start registered ${Object.values(toolNames).join(", ")}`));
    return result;
  });

  on('tool.call', async ($: any, e: any, next: any) => {
    await refreshLanguage($);
    const which = Object.keys(toolNames).find((k) => toolNames[k] === e.tool);
    if (!which) return next(e);
    if (which === 'segments') {
      try {
        const mdPath = (await runPython($, 'cw_segments.py', [String(await $.session.id())], 300_000)).trim().split('\n').pop()!.trim();
        const md = await $.fs.read(mdPath.replace(/\\/g, '/'));
        trace($, tr(`segments 完成 ${mdPath}`, `segments completed ${mdPath}`));
        return { result: tr(`${md}\n（存在 ${mdPath}，同名 .json 給面板。要移出哪些，就在 compact 的 drop 帶段號，或 ["suggested"] 照建議全部移出。）`, `${md}
(Saved in ${mdPath}; matching .json is for the panel. To remove segments, pass their IDs in compact.drop, or ["suggested"] for all suggestions.)`) };
      } catch (err) {
        return { result: tr(`分段失敗：${errText(err)}`, `Segmentation failed: ${errText(err)}`) };
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
        return tr(`${await $.fs.read(mdPath.replace(/\\/g, '/'))}\n（存在 ${mdPath}）`, `${await $.fs.read(mdPath.replace(/\\/g, "/"))}
(Saved in ${mdPath})`);
      };
      const [check, segs] = await Promise.allSettled([
        readMd('cw_handoff_check.py', tokens ? [sid, '--tokens', tokens] : [sid]),
        readMd('cw_segments.py', [sid]),
      ]);
      const show = (r: PromiseSettledResult<string>, what: string) => (r.status === 'fulfilled' ? r.value : tr(`${what}失敗：${errText(r.reason)}`, `${what} failed: ${errText(r.reason)}`));
      trace($, tr(`prepare 完成 交接=${check.status} 分段=${segs.status}`, `prepare completed handoff=${check.status} segments=${segs.status}`));
      return {
        result: tr(`${show(check, '交接檢查')}\n\n---\n\n${show(segs, '分段')}\n\n下一步：補交接（照上面的建議寫法挑著用）→ 要壓就叫 compact（mode 照判斷，drop 帶要移出的段號或 ["suggested"]）。`, `${show(check, "Handoff check")}

---

${show(segs, "Segments")}

Next: update the handoff using relevant suggestions, then call compact if appropriate (choose mode; pass segment IDs or ["suggested"] in drop).`),
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
          categories: (b.categories ?? []).map((r: any) => tr(`${r.name}${r.isDeferred ? '（延後載入，不算）' : ''}：${r.tokens}`, `${r.name}${r.isDeferred ? " (deferred, excluded)" : ""}: ${r.tokens}`)),
          memory_files: (b.memoryFiles ?? []).map((m: any) => `${m.path}：${m.tokens}`),
          mcp_tools_top: [...(b.mcpTools ?? [])].sort((x: any, y: any) => y.tokens - x.tokens).slice(0, 8)
            .map((t: any) => tr(`${t.name}${t.isLoaded === false ? '（未載入）' : ''}：${t.tokens}`, `${t.name}${t.isLoaded === false ? " (not loaded)" : ""}: ${t.tokens}`)),
          skills_top: [...(b.skills?.skillFrontmatter ?? [])].sort((x: any, y: any) => y.tokens - x.tokens).slice(0, 15)
            .map((k: any) => `${k.name}${k.pluginName ? `（${k.pluginName}）` : ''}：${k.tokens}`),
          skills_count: b.skills ? tr(`${b.skills.includedSkills}/${b.skills.totalSkills}，共 ${b.skills.tokens}`, `${b.skills.includedSkills}/${b.skills.totalSkills}, total ${b.skills.tokens}`) : undefined,
          tool_descriptions_loaded: [...toolSizes].filter(([, v]) => !v.deferred).sort((x, y) => y[1].chars - x[1].chars)
            .map(([k, v]) => tr(`${k}（${v.provider}）：${v.chars} 字`, `${k} (${v.provider}): ${v.chars} characters`)),
          attachments_this_process: [...attachSizes].sort((x, y) => y[1].chars - x[1].chars)
            .map(([k, v]) => tr(`${k}：${v.count} 次、共 ${v.chars} 字、最近一次 ${v.last} 字`, `${k}: ${v.count} times, ${v.chars} characters total, last ${v.last} characters`)),
        };
        trace($, `context ${JSON.stringify(info)}`);
        return { result: JSON.stringify(info, null, 1) };
      } catch (err) {
        return { result: tr(`讀不到：${errText(err)}`, `Cannot read: ${errText(err)}`) };
      }
    }
    if (which === 'handoff_check') {
      try {
        const mdPath = (await runPython($, 'cw_handoff_check.py', [String(await $.session.id())], 300_000)).trim().split('\n').pop()!.trim();
        return { result: tr(`${await $.fs.read(mdPath.replace(/\\/g, '/'))}\n（存在 ${mdPath}）`, `${await $.fs.read(mdPath.replace(/\\/g, "/"))}
(Saved in ${mdPath})`) };
      } catch (err) {
        return { result: tr(`交接檢查失敗：${errText(err)}${await handoffReminder($)}`, `Handoff check failed: ${errText(err)}${await handoffReminder($)}`) };
      }
    }
    if (which === 'advice_reply') {
      try {
        const out = await runPython($, 'cw_advice.py', ['reply', String(await $.session.id()), String(e.message_id ?? ''), String(e.text ?? '')], 30_000);
        const row = JSON.parse(out);
        trace($, `advice_reply ${row.id} → ${row.state}`);
        return { result: tr(`已記：${row.state}（${row.replied_at}）。面板重讀就看得到。`, `Recorded: ${row.state} (${row.replied_at}). Visible when the panel refreshes.`) };
      } catch (err) {
        return { result: tr(`回覆沒記上：${errText(err)}`, `Reply was not recorded: ${errText(err)}`) };
      }
    }
    if (which === 'get') {
      try {
        const args = [String(e.ref ?? '')];
        if (e.full) args.push('--full');
        const out = await runPython($, 'cw_get.py', args, 60_000);
        return { result: out.length > GET_MAX ? tr(`${out.slice(0, GET_MAX)}\n…（超過 ${GET_MAX} 字截掉；要看完整的用 Bash 跑 cw_get.py 導到檔案再 Read 片段）`, `${out.slice(0, GET_MAX)}
... (truncated at ${GET_MAX} characters; for full text, redirect cw_get.py output to a file with Bash, then Read relevant portions)`) : out };
      } catch (err) {
        return { result: tr(`找不到：${errText(err)}`, `Not found: ${errText(err)}`) };
      }
    }
    const mode = String(e.mode ?? '');
    trace($, `tool.call mode=${mode} reason=${String(e.reason ?? '').slice(0, 80)} drop=${JSON.stringify(e.drop ?? [])}`);
    if (mode === 'cancel') {
      const had = pending;
      pending = null;
      requested = false;
      continuation = null;
      return { result: had ? tr(`已取消（原本排定 ${had.mode}）。`, `Cancelled (previously scheduled ${had.mode}).`) : tr('沒有排定的壓縮。', "No compaction scheduled.") };
    }
    if (mode !== 'A' && mode !== 'C') return { deny: tr('mode 要是 A、C 或 cancel（B 整段逐字 0930 已拿掉，要細節用 C）', "mode must be A, C, or cancel (verbatim B removed on 0930; use C for details)") };
    const after = e.next ?? 'wait';
    if (after !== 'continue' && after !== 'wait') return { deny: tr('next 要是 continue 或 wait', "next must be continue or wait") };
    let drop: string[] = [];
    let dropNote = '';
    const asked = Array.isArray(e.drop) ? e.drop.map(String) : [];
    if (asked.length) {
      const found = await latestSegments($).catch(() => null);
      if (!found) return { deny: tr('還沒有這個對話的分段；先叫 segments 工具看清單', "No segments for this session yet; call segments first") };
      const known = new Map(found.segs.map((s) => [s.id, s]));
      drop = asked.includes('suggested')
        ? found.segs.filter((s) => s.label?.action === 'drop').map((s) => s.id)
        : asked;
      const unknown = drop.filter((id) => !known.has(id));
      if (unknown.length) return { deny: tr(`這些段號不在最新的分段裡：${unknown.join('、')}`, `These IDs are absent from the latest segments: ${unknown.join(", ")}`) };
      dropNote = drop.length
        ? tr(`壓縮時移出 ${drop.length} 段：${drop.map((id) => `第 ${known.get(id)!.seq} 段`).join('、')}（依 ${found.path}）。`, `Remove ${drop.length} segments at compaction: ${drop.map((id) => `segment ${known.get(id)!.seq}`).join(", ")} (from ${found.path}).`)
        : tr('Jev 沒有建議移出的段，不移出。', "Jev suggested no removals; keeping all segments.");
    }
    // 交接新鮮度：只跑程式那段（幾秒）。上一次壓縮後沒寫過交接又沒給理由就擋；其餘只把事實帶給老婆看
    let handoffNote = '';
    let handoffIds: HandoffIds | undefined;
    try {
      const check = JSON.parse(await runPython($, 'cw_handoff_check.py', [String(await $.session.id()), '--no-model', '--json'], 60_000));
      const reason = String(e.no_handoff_reason ?? '').trim();
      if (!check.facts?.written_since_compaction && !reason) {
        return { deny: tr(`${check.verdict}先寫交接（可以先叫 handoff_check 看 Jev 列的漏項），或在 no_handoff_reason 說明為什麼不用。`, `${check.verdict} Write a handoff first (call handoff_check for Jev omissions), or explain why it is unnecessary in no_handoff_reason.`) };
      }
      handoffNote = reason && !check.facts?.written_since_compaction ? tr(`沒寫交接的理由：${reason}。`, `Reason for no handoff: ${reason}.`) : tr(`交接：${check.verdict}`, `Handoff: ${check.verdict}`);
      const warnings = check.facts?.format_warnings ?? [];
      if (warnings.length) handoffNote += tr(`\n軟性提醒（不擋壓縮）：${warnings.join('；')}。\n`, `
Advisory (does not block compaction): ${warnings.join("; ")}.
`);
      const prepared = await prepareHandoffIds($, check.facts);
      handoffIds = prepared.ids;
      handoffNote += prepared.note;
    } catch (err) {
      handoffNote = tr(`（交接檢查沒跑成：${errText(err)}）`, `(Handoff check did not complete: ${errText(err)})`);
    }
    continuation = null;
    pending = { mode, reason: String(e.reason ?? ''), keep: String(e.keep ?? ''), drop, next: after, handoffIds };
    requested = true;
    return { result: tr(`已排定：這一輪回完話用 ${mode} 壓縮；壓後${after === 'continue' ? '續做已授權未完的工作' : '等待老公'}。${dropNote}${handoffNote}記得在這則回覆裡跟老公說；他說不要就用 mode=cancel。${await handoffReminder($)}`, `Scheduled: compact with ${mode} after this reply; afterward ${after === "continue" ? "continue authorized unfinished work" : "wait for the user"}. ${dropNote}${handoffNote} Tell the user in this reply; if declined, use mode=cancel.${await handoffReminder($)}`) };
  });

  on('prompt.submit', async ($: any, e: any, next: any) => {
    clearIdle(); // 有人說話了，閒置提醒作廢
    promptEpoch++; // 保守地讓任何新輸入（包含其他外掛）接手，不與它重複開回合。
    continuation = null;
    return next(e);
  });

  on('turn.complete', async ($: any, e: any, next: any) => {
    await refreshLanguage($);
    const result = await next(e);
    if (!e.agentId && idleTurn) {
      idleTurn = false;
      trace($, tr('閒置提醒那一輪回完，不重排；等老公說話', "Idle-reminder turn finished; do not rearm until the user speaks"));
    } else if (!e.agentId && !pending && !continuation) await armIdle($);
    if (!requested || !pending || e.agentId || e.reason !== 'answer') return result;
    trace($, tr(`turn.complete 發起 ${pending.mode}`, `turn.complete initiating ${pending.mode}`));
    requested = false; // 只發起一次；pending 留著給 session.compact
    await startCompact($, pending);
    return result;
  });

  on('session.compact', async ($: any, e: any, next: any) => {
    await refreshLanguage($);
    trace($, `session.compact trigger=${e.trigger} pending=${pending?.mode ?? '-'} agentId=${e.agentId ?? '-'} messages=${e.messages?.length} instructions=${(e.instructions ?? '').length}`);
    if (!e.agentId && e.trigger !== 'precompute') continuation = null;
    if (!pending || e.agentId || e.trigger === 'precompute') return next(e);
    const p = pending;
    const ticket = p.next === 'continue' ? { epoch: p.promptEpoch ?? promptEpoch } : null;
    continuation = ticket;
    clearIdle();
    pending = null;
    requested = false;
    try {
      // 先照老婆確認的清單移出整段，再把所有模式的工具往返換成一行；A／C 交給內建摘要的也是換過的
      let source: readonly any[] = e.messages;
      if (p.drop.length) {
        const found = await latestSegments($);
        const r = found ? dropSegments(source, found.segs, new Set(p.drop)) : { messages: [...source], dropped: [], skipped: p.drop };
        source = r.messages;
        trace($, tr(`移出段 ${r.dropped.length}／${p.drop.length}${r.skipped.length ? `，對不到沒移：${r.skipped.join('、')}` : ''}；${chars(e.messages)}→${chars(source)} 字`, `Removed ${r.dropped.length}/${p.drop.length} segments${r.skipped.length ? `; unmatched, retained: ${r.skipped.join(", ")}` : ""}; ${chars(e.messages)} -> ${chars(source)} characters`));
      }
      const ids = await loadCallIds($);
      const messages = toolsToLines(source, ids);
      const numbered = source.reduce((n: number, m: any) => n + (m.toolUses ?? []).filter((t: any) => ids.has(t.tool_use_id)).length, 0);
      trace($, tr(`工具一行化 ${source.length}→${messages.length} 則、${chars(source)}→${chars(messages)} 字，有編號的呼叫 ${numbered}；去掉系統提醒 ${reminderMsgs} 則、空文字使用者訊息 ${emptyUserKept} 則`, `Collapsed tool exchanges: ${source.length} -> ${messages.length} messages, ${chars(source)} -> ${chars(messages)} characters, ${numbered} calls with IDs; removed ${reminderMsgs} system reminders, ${emptyUserKept} empty user messages`));
      const lined = { ...e, messages };
      let res: any;
      if (p.mode === 'C') res = await summaryPlusRecent($, withInstructions(lined, p), next, p);
      else res = await next(withInstructions(lined, p));
      trace($, tr(`session.compact ${p.mode} 完成 messages=${res?.messages?.length ?? '-'} skip=${res?.skip ?? '-'}`, `session.compact ${p.mode} completed messages=${res?.messages?.length ?? "-"} skip=${res?.skip ?? "-"}`));
      if (p.handoffIds && res?.messages && !res.skip) await spawnHandoffSnapshot($, p.handoffIds);
      compactFinished($, p, ticket, res);
      try {
        $.ui.toast(tr(`wife-compact：已用 ${p.mode} 壓縮（${p.reason}）`, `wife-compact: compacted with ${p.mode} (${p.reason})`));
      } catch {}
      return res;
    } catch (err) {
      say($, tr(`wife-compact: ${p.mode} 失敗，交回內建：${errText(err)}`, `wife-compact: ${p.mode} failed, returning to native compaction: ${errText(err)}`));
      let res: any;
      try {
        res = await next(e);
      } catch (failure) {
        if (continuation === ticket) continuation = null;
        throw failure;
      }
      if (p.handoffIds && res?.messages && !res.skip) await spawnHandoffSnapshot($, p.handoffIds);
      compactFinished($, p, ticket, res);
      return res;
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
