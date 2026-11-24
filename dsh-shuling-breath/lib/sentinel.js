/**
 * dsh-shuling-breath —— Tier 0：哨兵探测。
 *
 * **纯模块**，无 dsh 依赖。判断依据只有两样东西：
 *   - mtime（毫秒）
 *   - 内容 sha1 前 16 位
 * 绝不做"语义判断"——语义要模型，那就贵了。这里只看"变没变"和"变出了什么可数的事实"。
 *
 * 一个哨兵的探测结果：
 *   { id, path, kind, exists, mtimeMs, hash, bytes, facts, change, stale }
 *   change ∈ null | 'appeared' | 'missing' | 'content' | 'mtime' | 'stale' | 'idle'
 *   （null = 没变，或首次见面建立基线）
 *
 * 第五类哨兵 kind:'session'（2026-11-13 23:5x 天令）—— 唯一一类"关于我自己"的哨兵：
 *   看的是会话投影缓存 json 的 **mtime**，只 stat，绝不读内容（3.6 MB）。
 *   静默超过 idleWakeAfterMinutes ⇒ change='idle'（这一跳叫醒，去看看我是不是停了）。
 *   唤醒后 idleCooldownMinutes 内一律不判（给那一轮跑完的时间，也防死循环）。
 *   ⚠️ 不能用 inject_log 表：它停在 2026-11-12T20:40，是 ling 时代的表，息壤不写它。
 *   ⚠️ 不能比 hash/mtime 变化：会话 json 每轮都写，比变化等于每跳都叫醒（recall 踩过这个坑）。
 */

import { createHash } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';

const PENDING_STATUSES = new Set(['pending', 'todo', '排队', '待办', '']);

function sha1(buf) {
  return createHash('sha1').update(buf).digest('hex').slice(0, 16);
}

function parseJson(text) {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
}

/** 队列：pending 几条、blocked 几条、总共几条。 */
function queueFacts(value) {
  if (!Array.isArray(value)) return { shape: 'not-array' };
  let pending = 0;
  let blocked = 0;
  let done = 0;
  for (const item of value) {
    if (item === null || typeof item !== 'object') continue;
    const status = typeof item['状态'] === 'string' ? item['状态'].trim() : '';
    if (status === 'blocked') blocked += 1;
    else if (status === 'done') done += 1;
    else if (PENDING_STATUSES.has(status)) pending += 1;
  }
  return { shape: 'array', total: value.length, pending, blocked, done };
}

/** 未完成账本：做了吗 不是 '做了' 的算未完成。 */
function unfinishedFacts(value) {
  if (!Array.isArray(value)) return { shape: 'not-array' };
  let unfinished = 0;
  for (const item of value) {
    if (item === null || typeof item !== 'object') continue;
    if (item['做了吗'] !== '做了') unfinished += 1;
  }
  return { shape: 'array', total: value.length, unfinished };
}

/** recall 落盘链：多久没更新 + 上次 round。 */
function recallFacts(value, mtimeMs, now, staleAfterMinutes) {
  const ageMinutes = mtimeMs === 0 ? null : Math.max(0, Math.round((now - mtimeMs) / 60000));
  const stale = ageMinutes !== null && ageMinutes > staleAfterMinutes;
  const round = value !== null && typeof value === 'object' && typeof value['round'] === 'number' ? value['round'] : null;
  return { ageMinutes, stale, round, staleAfterMinutes };
}

/**
 * 会话哨兵：只 stat，不读内容。
 * @param spec  {id, path, kind:'session'}
 * @param prev  上一次 state 条目
 * @param now   Date.now()
 * @param config 需要 idleWakeAfterMinutes
 * @param ctx   { idleCooldownUntil }  唤醒后的冷却截止时刻
 */
function probeSession(spec, prev, now, config, ctx) {
  const base = { id: spec.id, path: spec.path, kind: spec.kind, exists: false, mtimeMs: 0, hash: null, bytes: 0 };
  let st;
  try {
    st = statSync(spec.path);
  } catch {
    const change = prev === undefined ? null : prev.exists ? 'missing' : null;
    return { ...base, facts: { missing: true }, stale: false, change, text: spec.required ? `${spec.id} 缺失` : null };
  }

  const mtimeMs = Math.round(st.mtimeMs);
  const ageMinutes = Math.max(0, Math.round((now - mtimeMs) / 60000));
  const idleAfter = config.idleWakeAfterMinutes;
  const idle = ageMinutes > idleAfter;
  const cooldownUntil = ctx !== undefined && ctx !== null && Number.isFinite(ctx.idleCooldownUntil) ? ctx.idleCooldownUntil : 0;
  const cooling = now < cooldownUntil;
  const cooldownLeftMinutes = cooling ? Math.max(0, Math.round((cooldownUntil - now) / 60000)) : 0;

  // 只在"静默超阈值 且 不在冷却里"时报变化；首次见面不叫醒（先建立基线）。
  const change = idle && !cooling && prev !== undefined ? 'idle' : null;

  const facts = {
    ageMinutes,
    idleAfterMinutes: idleAfter,
    idle,
    cooling,
    cooldownLeftMinutes,
    // 最后活动时刻（北京时间）—— 唤醒 prompt 的"最后活动 23:18"就用它
    lastActiveLocal: new Date(mtimeMs + 8 * 3600000).toISOString().slice(11, 16)
  };
  const text = change === 'idle'
    ? `会话静默 ${describeAge(ageMinutes)}（阈值 ${idleAfter} 分，最后活动 ${facts.lastActiveLocal}）`
    : null;
  return { ...base, exists: true, mtimeMs, hash: null, bytes: 0, facts, stale: false, change, text };
}

function describeAge(minutes) {
  if (minutes === null) return '未知';
  if (minutes < 90) return `${minutes} 分钟`;
  const hours = Math.round((minutes / 60) * 10) / 10;
  return `${hours} 小时`;
}

/**
 * 探测一个哨兵并和上一次的探测结果比对。
 * @param spec  {id, path, kind, required}
 * @param prev  上一次的 state 条目（首次为 undefined）
 * @param now   Date.now()
 * @param config 需要 staleAfterMinutes / idleWakeAfterMinutes
 * @param ctx   { idleCooldownUntil }（只有 kind:'session' 用）
 */
export function probeSentinel(spec, prev, now, config, ctx) {
  if (spec.kind === 'session') return probeSession(spec, prev, now, config, ctx);

  const base = { id: spec.id, path: spec.path, kind: spec.kind, exists: false, mtimeMs: 0, hash: null, bytes: 0 };
  let st;
  try {
    st = statSync(spec.path);
  } catch {
    const change = prev === undefined ? null : prev.exists ? 'missing' : null;
    return {
      ...base,
      facts: { missing: true },
      stale: false,
      change,
      // 可选哨兵缺失是常态，不给可读短语；必需哨兵缺失要说清。
      text: spec.required ? `${spec.id} 缺失` : null
    };
  }

  const mtimeMs = Math.round(st.mtimeMs);
  let buf;
  try {
    buf = readFileSync(spec.path);
  } catch {
    return { ...base, exists: true, mtimeMs, facts: { unreadable: true }, stale: false, change: prev === undefined ? null : 'mtime', text: `${spec.id} 读不到` };
  }

  const hash = sha1(buf);
  const parsed = parseJson(buf.toString('utf8'));
  const value = parsed.ok ? parsed.value : null;

  let facts;
  if (spec.kind === 'queue') facts = queueFacts(value);
  else if (spec.kind === 'unfinished') facts = unfinishedFacts(value);
  else if (spec.kind === 'recall') facts = recallFacts(value, mtimeMs, now, config.staleAfterMinutes);
  else facts = { bytes: buf.length };
  if (!parsed.ok) facts = { ...facts, parseError: parsed.error };

  // ---- 变化判定：先建立基线（prev === undefined 时永不 wake），再逐条比 ----
  let change = null;
  if (prev !== undefined) {
    if (!prev.exists) change = 'appeared';
    // 2026-11-13 23:25：recall 是【我自己每轮写的】表（round 递增），hash 每 20 分钟必变
    //   ⇒ 用 hash 比"有没有变"等于每跳都叫醒（实测 wakes=3，每次都是 silent=yes，纯烧钱）。
    //   本意是"落盘链断了才叫"，那只该看 stale，不该看 content/mtime。
    else if (spec.kind !== 'recall' && prev.hash !== hash) change = 'content';
    else if (spec.kind !== 'recall' && Math.round(prev.mtimeMs) !== mtimeMs) change = 'mtime';
    // recall 的"落盘链断了"是一次性事件：只在 fresh -> stale 的那一跳算变化，
    // 之后一直 stale 也不再叫醒（否则每次轮询都是"有事"，等于没门）。
    if (spec.kind === 'recall' && facts.stale === true && prev.stale !== true && change === null) change = 'stale';
  }

  return {
    ...base,
    exists: true,
    mtimeMs,
    hash,
    bytes: buf.length,
    facts,
    stale: facts.stale === true,
    change,
    text: describe(spec, facts, change, prev)
  };
}

/** 一个哨兵在"变了"的时候给出一句可读短语；没变给 null。 */
function describe(spec, facts, change, prev) {
  if (change === null) return null;

  if (change === 'missing') return `${spec.id} 不见了`;
  if (change === 'appeared') return `${spec.id} 出现了`;

  if (spec.kind === 'queue') {
    const pending = facts.pending ?? 0;
    const blocked = facts.blocked ?? 0;
    const prevPending = prev && prev.facts && typeof prev.facts.pending === 'number' ? prev.facts.pending : null;
    const suffix = prevPending !== null && pending !== prevPending
      ? `（${pending > prevPending ? '新增 ' + (pending - prevPending) : '减少 ' + (prevPending - pending)}）`
      : '';
    const blocker = blocked > 0 ? `，blocked ${blocked} 件` : '';
    return `队列 pending ${pending} 件${suffix}${blocker}`;
  }

  if (spec.kind === 'unfinished') {
    const unfinished = facts.unfinished ?? 0;
    return `未完成.json 改了（未完成 ${unfinished} 件 / 共 ${facts.total ?? 0} 条）`;
  }

  if (spec.kind === 'recall') {
    if (change === 'stale') return `recall-state 停更 ${describeAge(facts.ageMinutes)}（阈值 ${facts.staleAfterMinutes} 分）`;
    const round = facts.round === null || facts.round === undefined ? '' : ` round=${facts.round}`;
    return `recall-state 更新${round}`;
  }

  if (spec.kind === 'session') return null;

  return `${spec.id} 改了（${facts.bytes ?? 0} 字节）`;
}

/**
 * 把"变了的那几条短语"拼成状态行的增量部分。
 * 例：`队列 pending 2 件（新增 1）/ 未完成.json 改了 / 无异常`
 */
export function renderDelta(results) {
  const parts = results.filter((r) => r.text !== null && r.text !== undefined).map((r) => r.text);
  if (parts.length === 0) parts.push('无可报字段');
  const abnormal = results.some((r) => r.stale === true || (r.change === 'missing' && r.text !== null));
  parts.push(abnormal ? '有异常' : '无异常');
  return parts.join(' / ');
}

/** 全部哨兵的当前事实，写进唤醒提示的第二行（模型要知道"现在是什么样"）。 */
export function renderFacts(results) {
  const parts = [];
  for (const r of results) {
    if (!r.exists) {
      parts.push(`${r.id}=缺失`);
      continue;
    }
    if (r.kind === 'queue') {
      parts.push(`queue.pending=${r.facts.pending ?? '?'} queue.blocked=${r.facts.blocked ?? '?'} queue.total=${r.facts.total ?? '?'}`);
    } else if (r.kind === 'unfinished') {
      parts.push(`unfinished=${r.facts.unfinished ?? '?'}/${r.facts.total ?? '?'}`);
    } else if (r.kind === 'recall') {
      parts.push(`recall.age=${r.facts.ageMinutes ?? '?'}m recall.round=${r.facts.round ?? '?'}`);
    } else if (r.kind === 'session') {
      const cool = r.facts.cooling === true ? ` cooldown=${r.facts.cooldownLeftMinutes ?? '?'}m` : '';
      parts.push(`session.idle=${r.facts.ageMinutes ?? '?'}m${cool}`);
    } else {
      parts.push(`${r.id}=${r.bytes}B`);
    }
  }
  return parts.join(' ');
}
