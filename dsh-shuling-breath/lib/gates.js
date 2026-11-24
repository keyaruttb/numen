/**
 * dsh-shuling-breath —— 三道门（纯模块，无 dsh 依赖）。
 *
 * 三道门串联，只有全过才唤醒；它们决定"叫醒了该干什么"，不是"叫不叫"：
 *
 *   门 1  峰谷门：读 cost-meter 的 ledger.json（**只读，绝不写**）。
 *         峰 ⇒ 只做保养；谷 ⇒ 可以建设。余额低于 balanceFloorCny ⇒ 连保养也不做，只记录。
 *   门 2  任务门：未完成.json 里 "状态":"pending" 条数 + 队列.json 里 pending 条数。
 *         两条都 0 ⇒ 建设线收工（不是"不叫醒"，是"不干建设的活"）。
 *   门 3  liveness：在 sentinel.js 的 kind:'session' 里（只 stat 会话 json 的 mtime）。
 *
 * ⚠️ 峰谷判定的两个坑（照 dsh-cost-meter/lib/peak-schedule.js 的 configuredPeakAt 抄）：
 *   ① hour 用 UTC，day 用本地（+8）。混了会整体错一小时。
 *   ② 窗口是 UTC 小时：[{1,4},{6,10}]。换算成北京时间：
 *        峰 = 09:00–12:00 和 14:00–18:00
 *        谷 = 其余全部 + 周末全天 + 假日
 *   ③ 另一条容易看漏的：12:00–14:00 是谷（两个窗口之间的缝）。
 *
 * ledger.json 888 KB：这里只按 key 切出 config / balanceRef / days[today] 三小段再
 * JSON.parse，绝不整份 parse 到内存里留着。
 */

import { readFileSync } from 'node:fs';

const DAY_MS = 86400000;
const HOUR_MS = 3600000;
const BJ_OFFSET_MS = 8 * HOUR_MS;

/** 北京时间口径的峰谷人话（写进注释与日志，方便后人核对）。 */
export const PEAK_HUMAN = '峰 = 09:00–12:00 和 14:00–18:00（北京时间）；谷 = 其余 + 周末全天 + 假日';

const p2 = (v) => String(v).padStart(2, '0');

function localDate(atMs) {
  return new Date(atMs + BJ_OFFSET_MS).toISOString().slice(0, 10);
}

function isValidDateKey(value) {
  return typeof value === 'string' && Number.isFinite(Date.parse(`${value}T00:00:00Z`));
}

/**
 * 从 ledger.json 的 `config` 字段里取峰谷口径。
 * 缺字段时按"没开峰谷"处理（等价于全谷），绝不抛。
 */
export function peakConfigOf(ledgerConfig) {
  const cfg = ledgerConfig !== null && typeof ledgerConfig === 'object' ? ledgerConfig : {};
  const windows = Array.isArray(cfg.peakWindows)
    ? cfg.peakWindows
        .filter((w) => w !== null && typeof w === 'object' && Number.isFinite(w.start) && Number.isFinite(w.end))
        .map((w) => ({ start: w.start, end: w.end }))
    : [];
  const holidays = Array.isArray(cfg.peakHolidays) ? cfg.peakHolidays.filter(isValidDateKey) : [];
  const effectiveAtMs = Number.isFinite(Date.parse(cfg.peakEffectiveAt)) ? Date.parse(cfg.peakEffectiveAt) : null;
  return { enabled: cfg.peakEnabled === true, windows, holidays, effectiveAtMs };
}

/**
 * 峰谷判定 —— 语义照 dsh-cost-meter/lib/peak-schedule.js 的 configuredPeakAt()：
 *   默认 schedule = 'deepseek'（hour 用 UTC，day 用 UTC+8 的本地日）；
 *   peakEnabled !== true 或早于 peakEffectiveAt ⇒ 'base'；
 *   周末 / 假日 ⇒ 'offPeak'；落在某个 UTC 窗口里 ⇒ 'peak'，否则 'offPeak'。
 * 返回值多一个 'base'（"峰谷计价还没生效"），调用方一律当 'offPeak' 用。
 */
export function configuredPeakAt(peak, atMs) {
  if (peak === null || typeof peak !== 'object') return null;
  if (peak.enabled === true && !Number.isFinite(atMs)) return null;
  if (peak.enabled !== true || !Number.isFinite(atMs)) return 'base';
  if (Number.isFinite(peak.effectiveAtMs) && atMs < peak.effectiveAtMs) return 'base';
  const windows = peak.windows ?? [];
  const holidays = peak.holidays ?? [];
  const utc = new Date(atMs);
  const local = new Date(atMs + BJ_OFFSET_MS);
  const day = local.getUTCDay();
  if (day === 0 || day === 6) return 'offPeak';
  if (holidays.includes(localDate(atMs))) return 'offPeak';
  const hour = utc.getUTCHours() + utc.getUTCMinutes() / 60 + utc.getUTCSeconds() / 3600 + utc.getUTCMilliseconds() / 3600000;
  // 正常窗口 start < end；start > end 是跨零点窗口（照原实现支持，虽然当前配置用不到）。
  return windows.some((w) => (w.start < w.end ? hour >= w.start && hour < w.end : hour >= w.start || hour < w.end))
    ? 'peak'
    : 'offPeak';
}

/** 'base' 一律折成 'offPeak'：峰谷没生效时按谷算，别把"没生效"误当成峰。 */
export function phaseAt(peak, atMs) {
  const raw = configuredPeakAt(peak, atMs);
  return raw === 'peak' ? 'peak' : 'offPeak';
}

/**
 * 下一次峰谷切换的时刻（毫秒）。分钟级扫 8 天再二分到秒。
 * 只在一跳真的要唤醒时调用（一次 tick 最多 11520 次纯函数比较，不读盘）。
 */
export function nextPhaseBoundaryAt(peak, atMs) {
  const phase = phaseAt(peak, atMs);
  const limit = atMs + 8 * DAY_MS;
  let lo = atMs;
  for (let t = atMs + 60000; t <= limit; t += 60000) {
    if (phaseAt(peak, t) !== phase) {
      let a = t - 60000;
      let b = t;
      while (b - a > 1000) {
        const mid = Math.floor((a + b) / 2);
        if (phaseAt(peak, mid) === phase) a = mid;
        else b = mid;
      }
      return b;
    }
    lo = t;
  }
  return null;
}

/** 距离下一次切换还有多少小时（一位小数）；无边界返回 null。 */
export function hoursUntil(targetMs, atMs) {
  if (!Number.isFinite(targetMs)) return null;
  return Math.round(((targetMs - atMs) / HOUR_MS) * 10) / 10;
}

/** `2026-11-14 09:00`（北京时间口径，只用于状态行）。 */
export function formatBj(epoch) {
  const d = new Date(epoch + BJ_OFFSET_MS);
  return `${d.getUTCFullYear()}-${p2(d.getUTCMonth() + 1)}-${p2(d.getUTCDate())} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
}

/** `09:00` 这种时分，用于"距转峰还有 N 小时（09:00）"。 */
export function formatBjTime(epoch) {
  const d = new Date(epoch + BJ_OFFSET_MS);
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
}

// ---------------------------------------------------------------------------
// ledger.json 分段读
// ---------------------------------------------------------------------------

/**
 * 从 openIndex 处的 '{' 起做括号配对，返回配对 '}' 之后的下标。
 * 认字符串与转义，所以 `"a}b"` 这种值不会把配对带偏。
 */
function matchObject(text, openIndex) {
  let depth = 0;
  let inStr = false;
  let esc = false;
  for (let i = openIndex; i < text.length; i += 1) {
    const ch = text[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') {
      inStr = true;
      continue;
    }
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

/** 切出 `"key":{...}` 这一段并 parse；找不到返回 {ok:false}。 */
function sliceObject(text, key, fromIndex) {
  const at = text.indexOf(`"${key}":`, fromIndex ?? 0);
  if (at < 0) return { ok: false, error: `missing key ${key}` };
  const open = text.indexOf('{', at + key.length + 3);
  if (open < 0) return { ok: false, error: `missing object ${key}` };
  const end = matchObject(text, open);
  if (end < 0) return { ok: false, error: `unbalanced ${key}` };
  try {
    return { ok: true, value: JSON.parse(text.slice(open, end)) };
  } catch (err) {
    return { ok: false, error: `bad json ${key}: ${String(err && err.message ? err.message : err)}` };
  }
}

/**
 * 只读 ledger.json，取三样：
 *   config      —— 峰谷口径
 *   balanceRef  —— 余额（CNY）
 *   days[today] —— 今天 cost / costCny / calls
 * 读失败一律返回 ok:false（门按"不知道"处理，不误判成谷）。
 *
 * 只读，绝不写：这是 dsh-cost-meter 的账本，写一个字节都会把它的账算错。
 */
export function readLedger(ledgerPath, atMs) {
  let text;
  try {
    text = readFileSync(ledgerPath, 'utf8');
  } catch (err) {
    return { ok: false, error: `读不到 ledger：${String(err && err.message ? err.message : err)}` };
  }
  const config = sliceObject(text, 'config');
  const balance = sliceObject(text, 'balanceRef');
  const days = sliceObject(text, 'days');
  const today = localDate(atMs);
  let todayRow = null;
  if (days.ok) {
    const daysText = JSON.stringify(days.value);
    const one = sliceObject(daysText, today);
    if (one.ok) todayRow = one.value;
  }
  return {
    ok: true,
    path: ledgerPath,
    config: config.ok ? config.value : null,
    configError: config.ok ? null : config.error,
    balance: balance.ok ? balance.value : null,
    balanceError: balance.ok ? null : balance.error,
    today,
    todayCost: todayRow !== null && Number.isFinite(todayRow.cost) ? todayRow.cost : null,
    todayCostCny: todayRow !== null && Number.isFinite(todayRow.costCny) ? todayRow.costCny : null,
    todayCalls: todayRow !== null && Number.isFinite(todayRow.calls) ? todayRow.calls : null
  };
}

// ---------------------------------------------------------------------------
// 任务门
// ---------------------------------------------------------------------------

function readJsonArray(path) {
  try {
    const value = JSON.parse(readFileSync(path, 'utf8'));
    if (!Array.isArray(value)) return { ok: false, error: `不是数组：${path}` };
    return { ok: true, value };
  } catch (err) {
    return { ok: false, error: `读不到/解析不了：${String(err && err.message ? err.message : err)}` };
  }
}

/**
 * 门 2：未完成.json 与 队列.json 里 "状态":"pending" 的条数。
 * 只认字面 'pending' —— 老条目没有"状态"字段的不算 pending（历史账，不是活）；
 * 队列里的 done / blocked 也不算。
 *
 * 2026-11-17 21:38 加「可做数」（unfinishedDoable）：
 *   病（实测）：6 条 pending 里 3 条在等他拍板/他看（原状态写着"等天定"/"半成：界面等天看"/"等天"），
 *   门 2 只数条数 ⇒ 永远判"有活" ⇒ 叫醒了发现动不了 ⇒ 空转。
 *   判据：`原状态` 或 `为什么` 里明确写"等天/待天/等定/等看/要天拍板/让天定" ⇒ 不算可做。
 *   只认这几种明确的说法，不猜。
 *   原字段一个不动：unfinishedPending 仍是总数（诊断用），新增 unfinishedDoable 才是"我现在能干的"。
 */
const WAIT_ON_TIAN = /等天|待天|等天定|等天看|等天拍|等天认|要天定|要天拍|要天看|让天定|天拍板/;

/** 这一条是不是"动不了"（卡在他那里）。永不抛。 */
function isBlockedOnTian(item) {
  try {
    const a = typeof item['原状态'] === 'string' ? item['原状态'] : '';
    const b = typeof item['为什么'] === 'string' ? item['为什么'] : '';
    return WAIT_ON_TIAN.test(a + ' ' + b);
  } catch {
    return false;
  }
}

export function readTaskFacts(unfinishedPath, queuePath) {
  const unfinished = readJsonArray(unfinishedPath);
  const queue = readJsonArray(queuePath);
  let unfinishedPending = 0;
  let unfinishedDoable = 0;          // 新增：pending 里"我现在真能动手"的条数
  let blockedOnTian = 0;             // 新增：pending 里卡在他那里的条数
  let firstPendingText = null;
  let firstPendingId = null;
  let firstDoableText = null;        // 新增：第一条可做的（唤醒 prompt 该给这个）
  let firstDoableId = null;
  if (unfinished.ok) {
    for (const item of unfinished.value) {
      if (item === null || typeof item !== 'object') continue;
      const status = typeof item['状态'] === 'string' ? item['状态'].trim() : '';
      if (status !== 'pending') continue;
      unfinishedPending += 1;
      const said = item['我跟天说过'] ?? item['做的事'] ?? item['说的'];
      const saidText = typeof said === 'string' && said.trim() !== '' ? said.trim() : '(这条没写「我跟天说过」)';
      const saidId = typeof item.id === 'string' ? item.id : null;
      if (firstPendingText === null) {
        firstPendingText = saidText;
        firstPendingId = saidId;
      }
      if (isBlockedOnTian(item)) {
        blockedOnTian += 1;
      } else {
        unfinishedDoable += 1;
        if (firstDoableText === null) {
          firstDoableText = saidText;
          firstDoableId = saidId;
        }
      }
    }
  }
  let queuePending = 0;
  if (queue.ok) {
    for (const item of queue.value) {
      if (item === null || typeof item !== 'object') continue;
      const status = typeof item['状态'] === 'string' ? item['状态'].trim() : '';
      if (status === 'pending') queuePending += 1;
    }
  }
  return {
    unfinishedPending,
    unfinishedDoable,
    blockedOnTian,
    queuePending,
    // hasWork 保持旧口径（有 pending 就算有活），不破坏调用方；
    // 新增 hasDoableWork 才是"叫醒我去建设"的正确判据。
    hasWork: unfinishedPending + queuePending > 0,
    hasDoableWork: unfinishedDoable + queuePending > 0,
    firstPendingText,
    firstPendingId,
    firstDoableText,
    firstDoableId,
    errors: [unfinished.ok ? null : unfinished.error, queue.ok ? null : queue.error].filter((e) => e !== null)
  };
}

// ---------------------------------------------------------------------------
// 三道门的合成判定
// ---------------------------------------------------------------------------

/**
 * @param {object} input
 * @param {object} input.config           resolveConfig() 的结果
 * @param {number} input.atMs             现在
 * @param {object} input.ledger           readLedger() 的结果（可 ok:false）
 * @param {object} input.task             readTaskFacts() 的结果
 * @param {boolean} [input.sessionIdle]   这一跳的 session 哨兵是不是报了 idle
 * @param {boolean} [input.balanceOverride] 验收用：直接注入 balance.total（不读真文件）
 * @returns decision
 */
export function evaluateGates(input) {
  const { config, atMs, ledger, task } = input;
  const peakCfg = peakConfigOf(ledger !== null && ledger !== undefined && ledger.ok ? ledger.config : null);
  const peakRaw = configuredPeakAt(peakCfg, atMs);
  const peak = peakRaw === 'peak' ? 'peak' : 'offPeak';

  const boundaryAt = nextPhaseBoundaryAt(peakCfg, atMs);
  const boundaryHours = hoursUntil(boundaryAt, atMs);
  const boundaryLabel = peak === 'peak' ? '转谷' : '转峰';

  const rawTotal = input.balanceOverride !== undefined
    ? input.balanceOverride
    : ledger !== null && ledger !== undefined && ledger.ok && ledger.balance !== null && Number.isFinite(ledger.balance.total)
      ? ledger.balance.total
      : null;
  const floorCny = config.balanceFloorCny;
  const balance = {
    total: rawTotal,
    currency: ledger !== null && ledger !== undefined && ledger.ok && ledger.balance !== null
      ? ledger.balance.currency ?? 'CNY'
      : 'CNY',
    date: ledger !== null && ledger !== undefined && ledger.ok && ledger.balance !== null ? ledger.balance.date ?? null : null,
    floorCny,
    below: rawTotal !== null && rawTotal < floorCny,
    unknown: rawTotal === null
  };

  const taskFacts = task !== null && task !== undefined
    ? task
    : { unfinishedPending: 0, queuePending: 0, hasWork: false, firstPendingText: null, firstPendingId: null, errors: ['任务门没读到数据'] };

  const sessionIdle = input.sessionIdle === true;
  let mode;
  let reason;
  if (peak === 'peak') {
    mode = 'maintenance';
    reason = '峰时段：只做保养';
  } else if (taskFacts.hasDoableWork !== undefined ? taskFacts.hasDoableWork : taskFacts.hasWork) {
    // 2026-11-17 21:38：优先用 hasDoableWork（pending 里"我现在真能动手"的）。
    // 旧口径 hasWork 会把"卡在他那里"的活也算成有活 ⇒ 叫醒了动不了 ⇒ 空转。
    // 兼容：老调用方没传 hasDoableWork 时退回 hasWork。
    mode = 'work';
    reason = taskFacts.hasDoableWork === false
      ? `谷 + 有 pending 但都卡在等他（${taskFacts.blockedOnTian ?? '?'} 条）：不建设`
      : '谷 + 有可做的活：建设';
  } else if (sessionIdle) {
    mode = 'learn';
    reason = '谷 + 建设线收工 + 会话静默：学习线';
  } else {
    mode = 'closed';
    reason = '谷 + 建设线收工：这一跳不干建设的活';
  }

  // 余额门是唯一一道"完全不做事"的硬门：连保养也不做，只记录。
  if (balance.below) {
    mode = 'blocked-balance';
    reason = `余额 ${balance.total} < floor ${floorCny}：只记录，不做事`;
  }

  return {
    at: atMs,
    peak,
    peakRaw,
    peakCfg,
    boundaryAt,
    boundaryHours,
    boundaryLabel,
    balance,
    task: taskFacts,
    sessionIdle,
    mode,
    reason,
    allowed: mode !== 'blocked-balance' && mode !== 'closed',
    /** 谷模式下的"跳数奇偶"：单数建设、双数学习（事\凌晨计划 的规则）。 */
    parity: null
  };
}

/**
 * 一行状态（写日志 / 唤醒头都用它，保证日志与 prompt 口径一致）。
 * 例：`峰谷=offPeak(距转峰 9.5h·09:00) 余额=112.62/10 任务=pending 6+0 模式=work`
 */
export function describeDecision(decision) {
  const b = decision.balance;
  const balanceText = b.total === null ? '余额=未知' : `余额=${Number(b.total).toFixed(2)}/${b.floorCny}`;
  const boundary = decision.boundaryAt === null
    ? ''
    : `(${decision.boundaryLabel} ${decision.boundaryHours}h·${formatBjTime(decision.boundaryAt)})`;
  return `峰谷=${decision.peak}${boundary} ${balanceText} 任务=pending ${decision.task.unfinishedPending}+${decision.task.queuePending} 模式=${decision.mode}`;
}
