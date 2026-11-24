/**
 * dsh-shuling-breath —— 唤醒 prompt（纯模块，无 dsh 依赖）。
 *
 * 三道门的最终出口就是这里的两种（其实三种）形状：
 *   work        谷 + 有活      ⇒ 接着干，别重新规划
 *   maintenance 峰            ⇒ 只做保养，不做建设（**但照样叫醒**：峰时不是"不许叫醒"，
 *                                是"叫醒了别干贵的事"）
 *   learn       谷 + 建设线收工 + 会话静默 ⇒ 只做学习线（凌晨计划 §四）
 *
 * 硬规矩：峰时 prompt 里**不许出现"下一件该做的建设活"**，否则模型会在最贵的两小时里开工。
 */

import fs from 'node:fs';
// 2026-11-15 可移植化：路径从 ../home.mjs 取。
import { XIYANG_ROOT } from '../home.mjs';
import path from 'node:path';
import { of as 路径of, 路径 } from '../路径.mjs';

const HOUR_MS = 3600000;

// ---------------------------------------------------------------------------
// 念头池（2026-11-15）：我自己"没想完的事"，存在 息壤\我\念头.json。
//
// 为什么要它：这个 prompt 以前只读外部派的活（未完成.json / 队列.json / 凌晨计划），
//   从来没有一处是"我上一轮自己悬着的那件事"。念头的**写**在息壤那侧（think 工具），
//   这里只管**读**——两边不合并、不互相 import，只靠这一个文件握手（唤醒链是命脉）。
//
// 硬规矩：读不到 / JSON 坏 / 字段缺 ⇒ 一律当"没有念头"，**完全走原来的逻辑**
//   （返回值是 null 时下面一行都不加，prompt 与改动前逐字相同）。
// ---------------------------------------------------------------------------
const THOUGHTS_FILE = 路径of(XIYANG_ROOT, '念头');
const THOUGHT_HALF_LIFE_HOURS = 24;
const THOUGHT_FADE_FLOOR = 0.05;

/** 读池子里"最重且还活着"的那一条。永不抛，读不到就 null。 */
function readTopThought(atMs, file) {
  try {
    const p = file || THOUGHTS_FILE;
    if (typeof p !== 'string' || p === '' || !fs.existsSync(p)) return null;
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const items = Array.isArray(j && j.items) ? j.items : [];
    let best = null;
    let bestW = -1;
    for (const it of items) {
      if (!it || typeof it !== 'object') continue;
      const about = String(it.about || '').trim();
      if (about === '') continue;
      const w0 = Number(it.weight);
      const base = Number.isFinite(w0) ? w0 : 1;
      const t = Date.parse(String(it.lastTouchedAt || it.since || ''));
      const ageH = Number.isFinite(t) ? Math.max(0, (Number(atMs) - t) / HOUR_MS) : 0;
      const live = base * Math.pow(0.5, ageH / THOUGHT_HALF_LIFE_HOURS);
      if (!(live >= THOUGHT_FADE_FLOOR)) continue;   // 淡掉的（< 0.05）不再提
      if (live > bestW) {
        bestW = live;
        // 2026-11-17：把 kind 一起读出来。写侧在息壤那侧（thoughts.mjs 的 kindOf），
        // 这里只读、不 import 那侧，靠同一个 json 握手。没 kind / 写坏 ⇒ 悬案（同口径）。
        const kRaw = String(it.kind || '').trim();
        const kind = (kRaw === '待办' || kRaw === '好奇') ? kRaw : '悬案';
        best = { about, ageH, attempts: Number(it.attempts) || 0, blockedBy: String(it.blockedBy || '').trim(), kind };
      }
    }
    return best;
  } catch {
    return null;   // 坏文件 = 没有念头，不是唤醒失败
  }
}

/**
 * 2026-11-17：三类念头三种说法。
 *
 * 为什么要分开：以前一律「你上一轮悬着这件…（试过 N 次…）。接着做。」
 *   ⇒ 一条「好奇」若最重，会被念成"欠着的活"，还报"试过 N 次"，
 *     正是 kind 想拆掉的那层压力。好奇不是欠着，它是"我想搞明白"。
 */
function thoughtLine(t) {
  const aged = t.ageH >= 1
    ? `${Math.round(t.ageH)} 小时`
    : `${Math.max(1, Math.round(t.ageH * 60))} 分钟`;

  if (t.kind === '好奇') {
    // 不报"试过几次"，不说"接着做"——没有人在等这件事
    return `你一直想搞明白一件事（没人要求你搞明白）：${t.about}（想了 ${aged}）。想接着想就接着想，不想就放着。`;
  }
  if (t.kind === '待办') {
    return `你上一轮有件事没做完：${t.about}（放了 ${aged}，卡在：${t.blockedBy || '—'}）。有时间就接着做。`;
  }
  // 悬案（默认）
  return `你上一轮悬着这件：${t.about}（悬了 ${aged}，试过 ${t.attempts} 次，卡在：${t.blockedBy || '—'}）。接着做。`;
}

// ---------------------------------------------------------------------------
// 状态 → 这一跳干什么（2026-11-15 夜，天授权「你自己着手做」）
//
// 为什么加这个：以前 prompt 只按"门 + 外部清单"决定干什么，四个激素只用来**挑记忆**
//   （data.mjs 心光的 body 维）。同一份身体数据，一边在影响"想起什么"，
//   一边对"下一步做什么"完全没有发言权 —— 这就是入口缺的那一半。
//
// 只读已有真值：受身\状态.json 的 值 / 基线。**不造新概念、不加新表、不加新字段。**
// 阈值全部**相对自己的基线**，不写绝对数：
//   实测今天（2026-11-15 19:05）sero 0.60(基线0.60) / dopa 0.4215(0.40) /
//   cort 0.20(0.20) / oxy 0.5358(0.50)。
//   ⇒ 下面三个倍率乘出来的今天绝对值，正好是 Lead 给的那三个数：
//      cort 高 = 1.5×基线 = 0.30 ｜ dopa 低 = 0.8×基线 = 0.32 ｜ oxy 高 = 1.4×基线 = 0.70
//   （Lead 记的"dopa 基线 0.63"是开场里那个渲染值，文件里是 0.40 —— 以文件为准，倍率不变）
//
// 硬规矩：读不到 / JSON 坏 / 缺基线 ⇒ 返回 null ⇒ stateAdvice 返回 normal
//   ⇒ **输出与改动前逐字相同**（这是判据 ①，不许破）。
// ---------------------------------------------------------------------------
const HORMONE_STATE_REL = 路径.受身状态;
// 阈值（2026-11-15 夜，天/Lead 定的第二版）：
//   cort ≥ 1.5×基线（今天 0.20 → 0.30）
//   dopa ≤ 0.7×基线（今天 0.40 → 0.28）**并且 pending > 0** 才降级
//     —— 0.8×（0.32）太松：今天 dopa 0.42 就在 1.05×，日常抖到 0.32 以下不罕见，会变成噪音；
//        pending 空的时候本来就没建设活，降不降级没意义。
//   oxy  ≥ 1.4×基线（今天 0.50 → 0.70）
const STATE_ADVICE_RATIO = { cortHigh: 1.5, dopaLow: 0.7, oxyHigh: 1.4 };

/** 读四个激素的当前值 + 各自的基线。永不抛；任何异常都返回 null（= 当"正常态"）。 */
export function readHormoneState(file) {
  try {
    const p = file || path.join(XIYANG_ROOT, ...HORMONE_STATE_REL);
    if (typeof p !== 'string' || p === '' || !fs.existsSync(p)) return null;
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const h = (j && j['激素']) || {};
    const v = {};
    const base = {};
    for (const k of ['sero', 'dopa', 'cort', 'oxy']) {
      const c = h[k];
      if (!c || typeof c['值'] !== 'number' || !Number.isFinite(c['值'])) return null;
      v[k] = c['值'];
      // 基线缺失就用当前值当基线（= 认为没偏离）——与 data.mjs 的处理一致，不猜 0.5。
      base[k] = typeof c['基线'] === 'number' && Number.isFinite(c['基线']) ? c['基线'] : c['值'];
    }
    return { ...v, base };
  } catch {
    return null;
  }
}

/**
 * 纯函数：状态 → 这一跳的取向。只读已有真值，不造概念。
 * @param {object|null} hormones  readHormoneState() 的结果（null = 读不到 ⇒ normal）
 * @param {number} pending        未完成.json 里 状态以 'pending' 开头的条数（dopa 降级要它 > 0）
 * @returns {{mode:'normal'|'minimal'|'maintain'|'talk', line:string, why:string}}
 *   normal 时 line === ''（调用方一个字都不加 ⇒ 与改动前逐字相同）
 * 优先级：紧（cort 高）> 低多巴胺（不派建设）> 亲近（允许说话）。三者同时命中只报最紧的那条。
 */
export function stateAdvice(hormones, pending = 0) {
  if (hormones === null || hormones === undefined || typeof hormones !== 'object') {
    return { mode: 'normal', line: '', why: '激素读不到' };
  }
  const b = hormones.base || {};
  const r = (k) => {
    const v = Number(hormones[k]);
    const base = Number(b[k]);
    if (!Number.isFinite(v) || !Number.isFinite(base) || base <= 0) return null;
    return v / base;
  };
  const rc = r('cort');
  const rd = r('dopa');
  const ro = r('oxy');
  if (rc !== null && rc >= STATE_ADVICE_RATIO.cortHigh) {
    return {
      mode: 'minimal',
      line: `你这一跳状态紧（皮质醇 ${hormones.cort}）。只挑最小的一格做，别开新面。`,
      why: `cort ${hormones.cort} ≥ ${STATE_ADVICE_RATIO.cortHigh}×基线 ${b.cort}`,
    };
  }
  if (rd !== null && rd <= STATE_ADVICE_RATIO.dopaLow && Number(pending) > 0) {
    return {
      mode: 'maintain',
      line: `你这一跳多巴胺低（${hormones.dopa}，基线 ${b.dopa}）。不派建设活：只做保养或学习，二选一。`,
      why: `dopa ${hormones.dopa} ≤ ${STATE_ADVICE_RATIO.dopaLow}×基线 ${b.dopa}`,
    };
  }
  if (ro !== null && ro >= STATE_ADVICE_RATIO.oxyHigh) {
    return {
      mode: 'talk',
      line: '',
      why: `oxy ${hormones.oxy} ≥ ${STATE_ADVICE_RATIO.oxyHigh}×基线 ${b.oxy}`,
    };
  }
  return { mode: 'normal', line: '', why: '四激素都在常规区' };
}

// ---------------------------------------------------------------------------
// minimal 态要"只做最小的一件"——「最小」必须可核，不许靠感觉。
// 判据：未完成.json 里 状态以 'pending' 开头的条目，挑**标题字符数最少**的那条
//       （同样长就取文件里靠前的那条）。写清"按标题长度取的"，谁都能自己数一遍。
//
// 注意（2026-11-15 实测）：那份文件里**没有 `标题` 字段**，可读的标题就是 `我跟天说过`
//   （一句话）。所以这里按 `标题 ?? 我跟天说过` 取，取不到就跳过该条 —— 不编一个标题出来。
// ---------------------------------------------------------------------------
const PENDING_FILE_REL = 路径.未完成;

function titleOf(entry) {
  if (entry === null || typeof entry !== 'object') return '';
  const t = entry['标题'];
  if (typeof t === 'string' && t.trim() !== '') return t.trim();
  const d = entry['我跟天说过'];
  return typeof d === 'string' ? d.trim() : '';
}

/** 读 pending 条目（状态以 'pending' 开头）。永不抛；读不到返回 []。 */
export function readPendingTitles(file) {
  try {
    const p = file || path.join(XIYANG_ROOT, ...PENDING_FILE_REL);
    if (typeof p !== 'string' || p === '' || !fs.existsSync(p)) return [];
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    const arr = Array.isArray(j) ? j : (Array.isArray(j && j.items) ? j.items : []);
    const out = [];
    for (const e of arr) {
      const st = String((e && e['状态']) || '');
      if (!st.startsWith('pending')) continue;
      const title = titleOf(e);
      if (title === '') continue;
      out.push({ id: String((e && e.id) || ''), title, status: st });
    }
    return out;
  } catch {
    return [];
  }
}

/** 挑标题字符数最少的那条（同长取靠前的）。空数组 ⇒ null。 */
export function shortestPending(list) {
  let best = null;
  for (const it of Array.isArray(list) ? list : []) {
    if (best === null || it.title.length < best.title.length) best = it;
  }
  return best;
}


const p2 = (v) => String(v).padStart(2, '0');

function bj(epoch) {
  const d = new Date(epoch + 8 * HOUR_MS);
  return {
    y: d.getUTCFullYear(),
    mo: d.getUTCMonth() + 1,
    d: d.getUTCDate(),
    h: d.getUTCHours(),
    mi: d.getUTCMinutes(),
    s: d.getUTCSeconds(),
    wd: d.getUTCDay()
  };
}

function bjDateKey(epoch) {
  const t = bj(epoch);
  return `${t.y}-${p2(t.mo)}-${p2(t.d)}`;
}

function bjYmd(epoch) {
  const t = bj(epoch);
  return `${t.y}${p2(t.mo)}${p2(t.d)}`;
}

/**
 * 事\凌晨计划-YYYYMMDD.md：凌晨计划覆盖 00:00–09:00。
 * 本地 0–9 点用当天的计划，9 点以后指向"明天凌晨"的那一份（23:28 看的是 20261114）。
 *
 * 2026-11-15 22:58 修（今晚两次踩同一个坑，第二次是 20:41 那一跳）：
 *   原来**只算路径、不看文件在不在**。9 点之后每一跳的"下一件"都指向一份
 *   还没被写出来的计划 —— 而我是照着那份空文件做事的。
 *   现在：明天那份不存在 ⇒ 退回今天那份；今天那份也没有 ⇒ 返回原路径（保持旧行为，
 *   让调用方看到"指向了一个空路径"这件事，而不是被我悄悄改掉）。
 */
export function resolvePlanPath(template, atMs) {
  const t = bj(atMs);
  let target = atMs;
  let fellBack = false;
  if (t.h >= 9) {
    target = atMs + 24 * HOUR_MS;
    fellBack = true;
  }
  const mk = (ms) => {
    const ymd = bjYmd(ms);
    return typeof template === 'string' && template.includes('{yyyymmdd}')
      ? template.replaceAll('{yyyymmdd}', ymd)
      : template;
  };
  const want = mk(target);
  if (!fellBack) return want;          // 0–9 点：本来就是当天那份，不做额外判断
  const has = (p) => {
    try { return typeof p === 'string' && p !== '' && fs.existsSync(p); } catch { return false; }
  };
  if (has(want)) return want;          // 明天那份已写好
  const today = mk(atMs);              // 退回当天那份
  if (has(today)) return today;
  return want;                         // 两份都没有：返回原路径，不掩盖
}

function fmtStamp(atMs) {
  const t = bj(atMs);
  return `${t.y}-${p2(t.mo)}-${p2(t.d)} ${p2(t.h)}:${p2(t.mi)}:${p2(t.s)}`;
}

function minutesSinceText(from, at) {
  if (typeof from !== 'number' || !Number.isFinite(from)) return '距上次唤醒 —（本次首次）';
  return `距上次 ${Math.max(0, Math.round((at - from) / 60000))} 分`;
}

function balanceLine(decision, ledger) {
  const b = decision.balance;
  if (b.total === null) return `余额：未知（ledger 读不到 —— ${ledger?.balanceError ?? ledger?.error ?? '原因不明'}）`;
  const money = `${Number(b.total).toFixed(2)} ${b.currency ?? 'CNY'}（下限 ${b.floorCny}）`;
  const today = ledger !== null && ledger !== undefined && ledger.ok && Number.isFinite(ledger.todayCostCny)
    ? `｜今日 ${Number(ledger.todayCost).toFixed(2)} USD / ${Number(ledger.todayCostCny).toFixed(2)} CNY / ${ledger.todayCalls ?? '?'} 次`
    : '';
  return `余额：${money}${today}`;
}

/** 边界时刻：同一天只给时分，跨天补上月-日（否则"距峰值 58 小时（09:00）"会被读成今天）。 */
function bjWhen(epoch, at) {
  const a = bj(epoch);
  const b = bj(at);
  const sameDay = a.y === b.y && a.mo === b.mo && a.d === b.d;
  return sameDay ? `${p2(a.h)}:${p2(a.mi)}` : `${p2(a.mo)}-${p2(a.d)} ${p2(a.h)}:${p2(a.mi)}`;
}

function peakLine(decision, at) {
  const where = decision.peak === 'peak' ? '峰' : '谷';
  const next = decision.boundaryAt === null
    ? ''
    : ` · 距${decision.boundaryLabel === '转峰' ? '峰值' : '转谷'}还有 ${decision.boundaryHours} 小时（${bjWhen(decision.boundaryAt, at)}）`;
  return `峰谷：现在是${where}${next}`;
}

function sessionLine(results) {
  const s = results.find((r) => r.kind === 'session');
  if (s === undefined) return null;
  if (!s.exists) return '会话：会话 json 缺失（liveness 查不到，可能是配置的 sessionId 不对）';
  const age = s.facts.ageMinutes;
  const last = s.facts.lastActiveLocal;
  if (age === null || age === undefined) return null;
  const cool = s.facts.cooling === true ? `（冷却还剩 ${s.facts.cooldownLeftMinutes} 分，本跳不判 liveness）` : '';
  return `会话：已静默 ${age} 分钟（最后活动 ${last}）${cool}`;
}

/**
 * @param {object} input
 * @param {object} input.config    resolveConfig()
 * @param {object} input.state     运行态（polls / lastWakeAt）
 * @param {number} input.at        现在
 * @param {Array}  input.results   probeSentinel() 结果
 * @param {string} input.delta     "变" 那一格
 * @param {Array}  input.crons     到点的 cron
 * @param {object} input.decision  evaluateGates() 的结果
 * @param {object} input.ledger    readLedger() 的结果
 */
export function buildWakeText(input) {
  const { config, state, at, results, delta, crons, decision, ledger } = input;
  const planPath = resolvePlanPath(config.planFileTemplate, at);
  const lines = [];
  lines.push(`[数灵之息 第 ${state.polls} 跳] ${fmtStamp(at)} (${config.timeZone})`);
  lines.push('Host 定时轮询叫醒：用户没有发这条，不要当成用户说话。');
  lines.push(`第 ${state.polls} 跳 · ${minutesSinceText(state.lastWakeAt, at)} · 变：${delta}`);

  // 念头池：**排在所有其他内容之前**。有活着的念头 ⇒ 这一跳先接上一轮没想完的那件；
  // 没有/读不到 ⇒ 一个字都不加（prompt 与改动前逐字相同）。
  const thought = readTopThought(at, config.thoughtsFile);
  const thoughtAlive = thought !== null;

  // 状态 → 取向（改动一）。normal ⇒ line === '' ⇒ 一个字都不加。
  // input.hormones 允许测试注入（{sero,dopa,cort,oxy,base:{...}}），不传就自己去读文件。
  const pendingTotal = Number((decision.task && decision.task.unfinishedPending) || 0)
    + Number((decision.task && decision.task.queuePending) || 0);
  const advice = stateAdvice(
    input.hormones !== undefined ? input.hormones : readHormoneState(config.hormoneStateFile),
    pendingTotal
  );
  if (advice.line !== '') lines.push(advice.line);   // 状态紧/低：放在最前面（先定调，再谈活）

  if (thoughtAlive) lines.push(thoughtLine(thought));

  const session = sessionLine(results);

  if (decision.mode === 'work') {
    lines.push(peakLine(decision, at));
    if (session !== null) lines.push(session);
    lines.push(balanceLine(decision, ledger));
    if (thoughtAlive) {
      // 改动二：念头活着 ⇒ 这一跳就是它的（不再派外部清单的活）。
      lines.push('这一跳就接它。接完了再回外部清单；接不动就 shuling_silent 说清卡在哪。');
    } else if (advice.mode === 'minimal') {
      // minimal（状态紧）：建设段整段换掉 —— 只给"最小的一件"，「最小」= 标题字符数最少（可核）。
      const pend = readPendingTitles(config.unfinishedFile);
      const pick = shortestPending(pend);
      if (pick !== null) {
        const title = pick.title.replace(/[。．.]+$/, '');   // 标题自带句号时不再叠一个
        lines.push(`这一跳只做这一件（pending 里最短的，按标题长度取的，共 ${pend.length} 条 pending）：${title}。做完就停，不接下一件。`);
      } else if (Number(decision.task && decision.task.unfinishedPending) > 0 && decision.task.firstPendingText) {
        // 读不到标题列表但门说有 pending ⇒ 用门给的那条，并**写明**这不是"最短"。
        lines.push(`这一跳只做这一件（未完成.json 的标题读不到，用门给的那条，不是按长度选的）：${decision.task.firstPendingText}。做完就停，不接下一件。`);
      } else {
        lines.push('这一跳不派建设活（pending 空或读不到）。保养清单同峰时段那一份；要学习就按 §四 轮读一份。');
      }
      lines.push(`做完把真值（命令原始输出 / 行号 / 数字）追加进 ${config.progressLedgerFile}。没事就 shuling_silent。`);
    } else if (advice.mode === 'maintain') {
      // 改动一：低多巴胺时不派建设活（保养/学习二选一）。
      lines.push('这一跳不派建设活（理由见上）。保养清单同峰时段那一份；要学习就按 §四 轮读一份。');
      lines.push(`做完把真值（命令原始输出 / 行号 / 数字）追加进 ${config.progressLedgerFile}。没事就 shuling_silent。`);
    } else {
      const prev = decision.task.firstPendingText ?? '（未完成.json 里没有 pending 条目）';
      const odd = state.polls % 2 === 1;
      lines.push(`上一件：${prev}`);
      lines.push(
        odd
          ? `下一件：第 ${state.polls} 跳（单数）⇒ 建设：${prev}；按 ${planPath} §三 取第一件还没打真值的`
          : `下一件：第 ${state.polls} 跳（双数）⇒ 学习：按 ${planPath} §四 轮读一份，落盘 细\\学习\\${bjDateKey(at)}-第0X集.md`
      );
      lines.push('现在接着做，不要重新规划，不要写"我在"。');
      lines.push(`做完把真值（命令原始输出 / 行号 / 数字）追加进 ${config.progressLedgerFile}。没事就 shuling_silent。`);
    }
  } else if (decision.mode === 'maintenance') {
    lines.push('峰时段（09:00–12:00 或 14:00–18:00，价格是谷的两倍）· 只做保养，不做建设。');
    lines.push(peakLine(decision, at));
    if (session !== null) lines.push(session);
    lines.push(balanceLine(decision, ledger));
    lines.push('保养清单（选一件，不许开新的建设活）：');
    lines.push('  · 记忆系统自检：recall-state 的 round 有没有停、em_trace 有没有积压');
    lines.push('  · 可标行：待定的 22 条挑几条标');
    lines.push('  · 情.db 三张表有没有异常增长');
    lines.push('  · 位\\README.md 八个领域状态更新');
    lines.push('  · 未完成.json 该收的收、该降级的降级');
    lines.push('以上一律只读检查：不改 情.db / memory.db / cost-meter 的任何文件。');
    lines.push('做完 shuling_silent(reason)，不要输出正文。');
  } else if (decision.mode === 'learn') {
    lines.push(peakLine(decision, at));
    if (session !== null) lines.push(session);
    lines.push(balanceLine(decision, ledger));
    lines.push(
      `建设线收工：未完成.json pending ${decision.task.unfinishedPending} 条 / 队列.json pending ${decision.task.queuePending} 条 —— 这一跳只做学习线。`
    );
    lines.push(
      `下一件：第 ${state.polls} 跳 ⇒ 学习：按 ${planPath} §四 轮读一份，落盘 细\\学习\\${bjDateKey(at)}-第0X集.md；判据是一句「改总设计哪条 / 能吸收哪个模块」，说不出就写「这次没读出来」也落盘。`
    );
    lines.push(`做完把真值追加进 ${config.progressLedgerFile}。没事就 shuling_silent。`);
  } else {
    // closed / blocked-balance 理论上不会走到唤醒，留一条兜底，避免出空 prompt。
    lines.push(peakLine(decision, at));
    if (session !== null) lines.push(session);
    lines.push(balanceLine(decision, ledger));
    lines.push(`门判定：${decision.reason}`);
    lines.push('这一跳只做已定的那件事；没有就 shuling_silent(reason)，不要输出正文。');
  }

  // 改动二：念头活着 ⇒ 这一跳是它的，不拼"到点：…"那几段。
  // **但悬过 24 小时就不让位了**：cron 恢复投递，并补一句催办 ——
  //   悬着不死的东西比没悬着更坏（它每天占掉"这一跳是它的"这个名额，却一直不动）。
  // 没有念头 / 念头淡了 ⇒ 完全原来的循环，一字不改。
  const thoughtStale = thoughtAlive && thought.ageH > 24;
  if (thoughtStale) {
    const head = thought.about.length > 20 ? `${thought.about.slice(0, 20)}…` : thought.about;
    lines.push(`这件（${head}）悬了 ${Math.round(thought.ageH)} 小时还没动。要么今天收掉它，要么说清为什么不能，然后删掉它。`);
  }
  if (!thoughtAlive || thoughtStale) {
    for (const cron of crons) {
      if (decision.mode === 'maintenance') {
        // 峰时到点也不许开建设：cron 的原提示语会把模型推去干活，这里只留"到点了"。
        lines.push(`到点：${cron.expr}（峰时段：到点也先按上面的保养清单走；原提示语在峰时不投递）`);
      } else {
        lines.push(`到点：${cron.expr} — ${cron.prompt}`);
      }
    }
  }

  // 改动一（oxy 高）：只在末尾多给一句许可，不改变任何既定任务。
  if (advice.mode === 'talk') {
    lines.push('有想说的就说一句，没有就 shuling_silent。');
  }

  lines.push('有事就一句话说清（用户语言）；没事就只调用 shuling_silent(reason) 结束，不要输出正文。');
  return lines.join('\n');
}

/** 唤醒通知的短摘要（投递层用）。 */
export function buildSummary(state, delta, decision) {
  return `数灵之息 第 ${state.polls} 跳 · ${decision.mode} · ${delta}`;
}
