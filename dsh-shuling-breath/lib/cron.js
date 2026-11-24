/**
 * dsh-shuling-breath —— 五段 cron（纯模块，无 dsh 依赖）。
 *
 * 语法照 dsh-proactive/lib/cron.js 的约定抄：`分 时 日 月 周`，每段
 * `*` | 数字 | `a-b` | `*\/n` | `a-b/n`，逗号分隔；周 0 和 7 都是周日；
 * dom 与 dow 都受限时按 Vixie 语义 **或** 匹配。
 *
 * 为什么不直接 import dsh-proactive 的 cron.js：它 import 了同包的 domain.js
 * （24KB，还带一堆别的东西），为了一个纯函数把整个插件拖成依赖不划算。
 * 这里自己实现，用 Intl 做本地时间投影，DST 缝隙返回 null（跳过）、
 * 重叠取较早那一跳。
 */

const FIELDS = [
  { name: '分钟', min: 0, max: 59 },
  { name: '小时', min: 0, max: 23 },
  { name: '日', min: 1, max: 31 },
  { name: '月', min: 1, max: 12 },
  { name: '周', min: 0, max: 7 }
];

const ATOM = /^(?:(?<star>\*)|(?<start>\d+)(?:-(?<end>\d+))?)(?:\/(?<step>\d+))?$/;
const WEEKDAY = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
const WINDOW_DAYS = 731;

export class CronError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CronError';
    this.code = 'invalid_cron';
  }
}

function parseField(text, bounds, fieldName) {
  const values = new Set();
  for (const atom of text.split(',')) {
    if (atom === '') throw new CronError(`cron ${fieldName} 段有空项`);
    const m = ATOM.exec(atom);
    if (m === null) throw new CronError(`cron ${fieldName} 段的 '${atom}' 不合法（只认 * / 数字 / a-b / 步长）`);
    const g = m.groups;
    const step = g.step === undefined ? 1 : Number(g.step);
    if (!Number.isSafeInteger(step) || step < 1) throw new CronError(`cron ${fieldName} 段的步长必须是正整数`);
    let start;
    let end;
    if (g.star !== undefined) {
      start = bounds.min;
      end = bounds.max;
    } else {
      start = Number(g.start);
      end = g.end !== undefined ? Number(g.end) : step > 1 ? bounds.max : start;
      if (start > end) throw new CronError(`cron ${fieldName} 段的区间起点大于终点`);
    }
    if (start < bounds.min || start > bounds.max || end < bounds.min || end > bounds.max) {
      throw new CronError(`cron ${fieldName} 段的区间 ${start}-${end} 超出 ${bounds.min}..${bounds.max}`);
    }
    for (let v = start; v <= end; v += step) values.add(v);
  }
  if (values.size === 0) throw new CronError(`cron ${fieldName} 段什么都没匹配到`);
  return values;
}

/** 解析五段表达式；任何语法问题抛 CronError。 */
export function parseCron(expr) {
  if (typeof expr !== 'string' || expr.trim() !== expr || expr.length === 0) {
    throw new CronError('cron 必须是不带首尾空白的非空五段表达式');
  }
  const fields = expr.split(/\s+/);
  if (fields.length !== 5) throw new CronError(`cron 必须是五段（分 时 日 月 周），收到 ${fields.length} 段`);
  const dow = parseField(fields[4], FIELDS[4], FIELDS[4].name);
  if (dow.has(7)) dow.add(0);
  return {
    minute: parseField(fields[0], FIELDS[0], FIELDS[0].name),
    hour: parseField(fields[1], FIELDS[1], FIELDS[1].name),
    dom: parseField(fields[2], FIELDS[2], FIELDS[2].name),
    month: parseField(fields[3], FIELDS[3], FIELDS[3].name),
    dow
  };
}

const formatterCache = new Map();

function formatterFor(timeZone) {
  let f = formatterCache.get(timeZone);
  if (f === undefined) {
    try {
      f = new Intl.DateTimeFormat('en-US', {
        timeZone,
        hourCycle: 'h23',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        weekday: 'short'
      });
    } catch {
      throw new CronError(`未知时区：${timeZone}`);
    }
    formatterCache.set(timeZone, f);
  }
  return f;
}

/** epoch → 本地墙钟字段 {y,mo,d,h,mi,s,wd,offsetMs}。 */
export function project(epoch, timeZone) {
  const parts = formatterFor(timeZone).formatToParts(new Date(epoch));
  const pick = {};
  for (const p of parts) if (p.type !== 'literal') pick[p.type] = p.value;
  const y = Number(pick.year);
  const mo = Number(pick.month);
  const d = Number(pick.day);
  const h = Number(pick.hour);
  const mi = Number(pick.minute);
  const s = Number(pick.second);
  const wd = WEEKDAY[pick.weekday];
  const asUtc = Date.UTC(y, mo - 1, d, h, mi, s);
  return { y, mo, d, h, mi, s, wd, offsetMs: asUtc - Math.floor(epoch / 1000) * 1000 };
}

/** 本地墙钟字段 → epoch；落在 DST 缝隙里返回 null。 */
export function wallToEpoch(y, mo, d, h, mi, timeZone) {
  const guess = Date.UTC(y, mo - 1, d, h, mi, 0);
  let epoch = guess - project(guess, timeZone).offsetMs;
  epoch = guess - project(epoch, timeZone).offsetMs;
  const back = project(epoch, timeZone);
  if (back.y === y && back.mo === mo && back.d === d && back.h === h && back.mi === mi) return epoch;
  return null;
}

function dayMatches(parsed, day, weekday) {
  const domRestricted = parsed.dom.size < 31;
  const dowRestricted = parsed.dow.size < 8;
  if (domRestricted && dowRestricted) return parsed.dom.has(day) || parsed.dow.has(weekday);
  if (domRestricted) return parsed.dom.has(day);
  if (dowRestricted) return parsed.dow.has(weekday);
  return true;
}

/**
 * 严格晚于 afterEpoch 的下一个触发时刻；窗口内没有返回 null。
 * 纯函数：不读时钟、不随进程时区漂移。
 */
export function nextOccurrence(expr, timeZone, afterEpoch) {
  const parsed = parseCron(expr);
  const hours = [...parsed.hour].sort((a, b) => a - b);
  const minutes = [...parsed.minute].sort((a, b) => a - b);
  const start = project(afterEpoch, timeZone);
  const dayZero = Date.UTC(start.y, start.mo - 1, start.d);

  for (let i = 0; i < WINDOW_DAYS; i += 1) {
    const cursor = new Date(dayZero + i * 86400000);
    const y = cursor.getUTCFullYear();
    const mo = cursor.getUTCMonth() + 1;
    const d = cursor.getUTCDate();
    const wd = cursor.getUTCDay();
    if (!parsed.month.has(mo)) continue;
    if (!dayMatches(parsed, d, wd)) continue;
    for (const h of hours) {
      for (const mi of minutes) {
        if (i === 0) {
          // 第一天：只找严格晚于"现在"的墙钟时刻。
          if (h < start.h) continue;
          if (h === start.h && mi <= start.mi) continue;
        }
        const epoch = wallToEpoch(y, mo, d, h, mi, timeZone);
        if (epoch !== null && epoch > afterEpoch) return epoch;
      }
    }
  }
  return null;
}

/** 人读：`2026-11-14 07:30 (+08:00)`。 */
export function formatLocal(epoch, timeZone) {
  const p = project(epoch, timeZone);
  const p2 = (v) => String(v).padStart(2, '0');
  const sign = p.offsetMs < 0 ? '-' : '+';
  const abs = Math.abs(Math.round(p.offsetMs / 60000));
  const off = `${sign}${p2(Math.floor(abs / 60))}:${p2(abs % 60)}`;
  return `${p.y}-${p2(p.mo)}-${p2(p.d)} ${p2(p.h)}:${p2(p.mi)} (${off}, ${timeZone})`;
}
