/**
 * dsh-shuling-breath —— 自我调度（selfSchedule）纯模块。
 *
 * 2026-11-17 天的原话：
 *   「数灵之息作为你的呼吸，应该可以随时调整……当明明有化身、自己在跑的时候，
 *     就不需要定时的那个唤醒插进去，那么在有派出化身或自己在做连续任务时，
 *     可以预判时间，把呼吸拉长一些，而在知道马上就终止，没东西跑但需要一口气
 *     唤醒自己的时候，也能及时将呼吸提前，不浪费等待时间。」
 *
 * 它要做的事，只有两件：
 *   ① 判定（decideSelfSchedule）：这一跳该不该被自我调度压住 / 强制叫醒。
 *      —— 纯函数，不读盘、不写盘、不认识 dsh，可以直接在宿主之外 import 测。
 *   ② 排程（planBreathe）：把工具参数变成一条 selfSchedule，自带全部自检。
 *
 * state 里的形状（多余字段一律丢弃，别让脏值进 state）：
 *   selfSchedule: { until: <ms>, at: <ms>, why: <string>, mode: 'sleep' | 'wake' }
 *   until 为 null / 字段不存在 / 形状不对 ⇒ 视为"没有自我调度"，走原来的全部门。
 *
 * 两种模式的语义差别（写清楚，别混）：
 *   sleep —— until 之前**绝对不叫**（比 cron、比所有探针都高）。
 *            **到期不会自动唤醒**：到期只是"解除了压制"，之后照原样过门。
 *   wake  —— until 之后**强制叫一次**（不管门上说什么），叫完就清掉。
 */

/** 单次自我调度的上限（分钟）。超过 24 小时就不叫"呼吸"了，叫"关机"。 */
export const MAX_MINUTES = 1440;

/** why 的长度上限（字）。超了当场拒，不静默截断 —— 日志要能指回原话。 */
export const WHY_MAX_CHARS = 200;

const MODES = new Set(['sleep', 'wake']);

/**
 * 把一个来路不明的值收成合法的 selfSchedule（或 null）。
 * 只认四件套：until 有限数、at 有限数、mode ∈ {sleep,wake}、why 字符串。
 * 任何一条不满足 ⇒ null（= 当作没有自我调度，退回原判定）。
 */
export function normalizeSelfSchedule(raw) {
  if (raw === null || raw === undefined || typeof raw !== 'object') return null;
  const until = raw.until;
  if (typeof until !== 'number' || !Number.isFinite(until)) return null;
  const mode = raw.mode;
  if (typeof mode !== 'string' || !MODES.has(mode)) return null;
  const at = typeof raw.at === 'number' && Number.isFinite(raw.at) ? raw.at : 0;
  const why = typeof raw.why === 'string' ? raw.why : '';
  return { until, at, why, mode };
}

/**
 * 这一跳该不该被自我调度干预。
 *
 * @param {object|null} raw       state.selfSchedule
 * @param {number} nowMs
 * @returns {{action:'none'|'sleep'|'wake', selfSchedule:object|null,
 *            until?:number, at?:number, why?:string, mode?:string,
 *            remainingMinutes?:number, note?:string}}
 *   action='sleep' ⇒ 这一跳绝对不叫（调用方直接返回，不跑门）
 *   action='wake'  ⇒ 这一跳强制叫（调用方把 allowed 顶成 true）
 *   action='none'  ⇒ 不干预
 *   note 只在"有 selfSchedule 但不干预"时出现，供日志说明原因（sleep 已到期 / wake 未到期）
 */
export function decideSelfSchedule(raw, nowMs) {
  const ss = normalizeSelfSchedule(raw);
  if (ss === null) return { action: 'none', selfSchedule: null };

  const deltaMs = ss.until - nowMs;
  const remainingMinutes = Math.max(0, Math.ceil(deltaMs / 60000));

  if (ss.mode === 'sleep') {
    if (deltaMs > 0) {
      return { action: 'sleep', selfSchedule: ss, ...ss, remainingMinutes };
    }
    // sleep 到期：只解压，不叫醒（天要的是"撤掉压制"，不是"补一次叫醒"）。
    return { action: 'none', selfSchedule: ss, ...ss, remainingMinutes: 0, note: 'sleep 已到期，解除压制' };
  }

  if (deltaMs <= 0) {
    return { action: 'wake', selfSchedule: ss, ...ss, remainingMinutes: 0 };
  }
  return { action: 'none', selfSchedule: ss, ...ss, remainingMinutes, note: 'wake 未到期' };
}

/**
 * 把工具参数落成一条 selfSchedule（自带全部自检；不读盘、不写盘）。
 *
 *   after_minutes > 0  ⇒ mode='wake'，now + N 分钟后强制叫醒
 *   after_minutes < 0  ⇒ mode='sleep'，接下来 |N| 分钟内绝对不叫
 *   after_minutes === 0 ⇒ 清掉（返回 { ok:true, clear:true }）
 *
 * @param {{after_minutes:number, why:string}} args
 * @param {number} nowMs
 * @returns {{ok:true, clear:boolean, selfSchedule:object|null, mode:string, until:number|null, remainingMinutes:number}
 *          |{ok:false, err:string}}
 */
export function planBreathe(args, nowMs) {
  if (args === null || args === undefined || typeof args !== 'object') {
    return { ok: false, err: '参数缺失：需要 { after_minutes, why }' };
  }

  const rawN = args.after_minutes;
  if (typeof rawN !== 'number' || !Number.isFinite(rawN)) {
    return { ok: false, err: `after_minutes 必须是有限数字（收到 ${JSON.stringify(rawN) ?? String(rawN)}）` };
  }
  const n = rawN;
  if (Math.abs(n) > MAX_MINUTES) {
    return { ok: false, err: `after_minutes 的绝对值上限是 ${MAX_MINUTES} 分钟（24 小时），收到 ${n}` };
  }

  const rawWhy = args.why;
  if (typeof rawWhy !== 'string' || rawWhy.trim() === '') {
    return { ok: false, err: 'why 不能为空：写一句为什么改呼吸（会原样进日志和 state）' };
  }
  const why = rawWhy.trim();
  if (why.length > WHY_MAX_CHARS) {
    return { ok: false, err: `why 上限 ${WHY_MAX_CHARS} 字，收到 ${why.length} 字` };
  }

  if (n === 0) {
    return { ok: true, clear: true, selfSchedule: null, mode: 'cleared', until: null, remainingMinutes: 0 };
  }

  const mode = n > 0 ? 'wake' : 'sleep';
  // 2026-11-17 21:24 定稿（前两版都错，记下来免得再犯）：
  //
  //   病：读它的 poller 是 5 分钟一跳（dsh-shuling-breath pollIntervalSeconds=300），
  //      所以 breathe(2) 说"2 分钟后"，实际要等到下一跳轮询才叫，最坏差近一个周期。
  //
  //   第一版（错的）：把 until 向上对齐到整 5 分钟网格。实测 n=5 变成等 10 分钟。
  //     错在哪：poll 的时刻**不是整 5 分钟**——日志里是 21:11:54 / 21:16:54，从某个起点累加
  //     300 秒来的。对齐到整分网格 = 对齐到一个 poll 永远不去看的时刻，纯白算。
  //
  //   第二版（这版）：不假装精确。until 就是 now + n 分钟这个**真实时刻**，
  //     但回执里把"最坏要等多久"说清楚（见 remainingMinutesMax）。
  //     真要把精度做上去，得缩短 poll 间隔，那是另一个决定（现在每跳只读 15KB，很便宜，
  //     但 300 秒是当初为了"廉价轮询"定的，不能我一个人拍）。
  const until = Math.round(nowMs + Math.abs(n) * 60000);
  const POLL_WORST_MS = 5 * 60 * 1000;
  return {
    ok: true,
    clear: false,
    selfSchedule: { until, at: nowMs, why, mode },
    mode,
    until,
    requestedMinutes: Math.abs(n),
    // 真实落地要等：到期后最多再过一跳轮询（5 分钟）
    remainingMinutes: Math.abs(n),
    remainingMinutesMax: Math.abs(n) + Math.round(POLL_WORST_MS / 60000)
  };
}

/** 日志里那一行的共用格式（三种事件都用它，保证能 grep 到一处）。 */
export function describeSelfSchedule(ss) {
  const when = new Date(ss.until).toISOString();
  const setAt = ss.at > 0 ? new Date(ss.at).toISOString() : '(未知)';
  return `mode=${ss.mode} until=${when} set at ${setAt} why=${ss.why === '' ? '(未写)' : ss.why}`;
}
