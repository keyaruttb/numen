/**
 * dsh-shuling-breath —— Tier 0 轮询器（纯模块）。
 *
 * 一次 tick 做的事：
 *   读哨兵（stat + sha1；会话哨兵只 stat）→ 比对上次 → 拼字符串 → **过三道门** → 判断
 *     · 全都没变 ⇒ 记一行 `no change`，**不调用 wake**（零模型成本，也零额外读盘）
 *     · 有一条真变化（或 cron 到点）⇒ 过门：
 *         余额低于下限        ⇒ 不唤醒，只记录（硬门）
 *         峰                  ⇒ 唤醒，但 prompt 只给保养清单
 *         谷 + 有活            ⇒ 唤醒，接着建设
 *         谷 + 建设线收工 + 静默 ⇒ 唤醒，只做学习线
 *         谷 + 建设线收工        ⇒ 不唤醒（cron 到点除外）
 *
 * 它自己不认识 agents、不认识 dsh：wake 是注入进来的回调（宿主里是
 * deliverWake，验收脚本里是计数器/桩）。这就是"廉价轮询"那一半的全部。
 */

import { readFileSync, unlinkSync } from 'node:fs';
import { nextOccurrence } from './cron.js';
import { probeSentinel, renderDelta, renderFacts } from './sentinel.js';
import { evaluateGates, readLedger, readTaskFacts, describeDecision } from './gates.js';
import { buildWakeText, buildSummary } from './prompt.js';

const HOUR_MS = 3600000;

/**
 * 这个进程是什么时候起的（模块加载那一刻）—— 2026-11-18 00:10 新加。
 *
 * 为什么要有它：`state.startedAt` 只在**第一次** tick 时写一次、之后永不更新，
 *   所以它记的是"这个 state 文件多早开始用的"，不是"这个进程多早起的"。
 *   结果就是：重启之后进程是新的，state 却看不出任何区别 ⇒ 冷却（idleCooldownUntil）
 *   照旧生效 ⇒ 重启后最长 90 分钟内不会醒。
 *   实测（2026-11-18 00:04 那次重启）：state.startedAt 停在 11-15 17:22，
 *   而 idleCooldownUntil 指到 01:30 ⇒ 中间一句话都没有。
 *
 * 现在：`bootAt` 记进程启动时刻，第一跳发现 `bootAt` 变了 ⇒ 知道自己是新进程；
 *   若同时还发现"距上次 poll 已经超过 2 个周期"，那这一跳**跳过冷却**（当作从重启里被叫醒）。
 *   只跳一次：跳完 state.bootAt 就更新成本进程的，后续跳照常吃冷却。
 */
const BOOT_AT = Date.now();


/**
 * 2026-11-17：自我调度文件路径。
 * 不写进 state 文件——poller 每跳都会 saveState(state)，用内存值整体覆盖磁盘，
 * 工具刚写进去的东西下一跳就被冲掉。独立文件两边不打架。
 */
export function selfScheduleFileOf(config) {
  const base = String(config.stateFile ?? '');
  return base.replace(/\.state\.json$/, '.selfschedule.json');
}

/**
 * 读自我调度。返回 null（没有/坏了）或 {mode, until, at, why}。
 * 坏文件按"没有"处理——一个坏掉的调度不该让呼吸停摆。
 */
export function readSelfSchedule(file) {
  try {
    const raw = readFileSync(file, 'utf8');
    const j = JSON.parse(raw);
    if (!j || typeof j !== 'object') return null;
    const mode = j.mode;
    const until = Number(j.until);
    if (mode !== 'sleep' && mode !== 'wake') return null;
    if (!Number.isFinite(until)) return null;
    return { mode, until, at: Number(j.at) || 0, why: String(j.why ?? '') };
  } catch {
    return null;
  }
}

/**
 * 自我调度判定（2026-11-17 加）：比 cron、比所有探针都高。
 *   'sleep' ⇒ 到期前绝对不叫（返回 {verdict:'sleep', ...}）
 *   'wake'  ⇒ 到期后强制叫（返回 {verdict:'wake', ...}）
 *   其它    ⇒ null，不干预
 * 注意：sleep 到期 ≠ 自动唤醒，只是解除压制；只有 wake 到期才强制叫。
 */
export function judgeSelfSchedule(self, at) {
  if (!self) return null;
  if (self.mode === 'sleep' && at < self.until) return { verdict: 'sleep', remainingMin: Math.round((self.until - at) / 60000), why: self.why };
  if (self.mode === 'wake' && at >= self.until) return { verdict: 'wake', lateMin: Math.round((at - self.until) / 60000), why: self.why };
  return null;
}

function minutesSince(from, now) {
  if (typeof from !== 'number' || !Number.isFinite(from)) return null;
  return Math.max(0, Math.round((now - from) / 60000));
}

/** 门 3 的冷却截止：显式记的 idleCooldownUntil 与"上次唤醒 + 冷却"取大的那个。 */
function idleCooldownUntilOf(state, config) {
  const explicit = Number.isFinite(state.idleCooldownUntil) ? state.idleCooldownUntil : 0;
  const fallback = Number.isFinite(state.lastWakeAt)
    ? state.lastWakeAt + config.idleCooldownMinutes * 60000
    : 0;
  return Math.max(explicit, fallback);
}

/**
 * @param {object} deps
 * @param {object} deps.config   resolveConfig() 的结果
 * @param {object} deps.state    loadState() 的结果（会被原地更新）
 * @param {Function} deps.saveState  (state) => void
 * @param {Function} deps.log        (line) => void  只写自己的日志文件
 * @param {Function} deps.wake       async ({text, summary, report}) => {outcome, ...}
 * @param {Function} [deps.now]
 * @param {Function} [deps.probe]
 * @param {Function} [deps.readLedger]    可注入的桩（验收用）
 * @param {Function} [deps.readTaskFacts] 可注入的桩（验收用）
 * @param {Function} [deps.evaluate]      可注入的桩（验收用）
 */
export function createPoller(deps) {
  const { config, state, saveState, log, wake } = deps;
  const now = deps.now ?? (() => Date.now());
  const probe = deps.probe ?? probeSentinel;
  const readLedgerFn = deps.readLedger ?? readLedger;
  const readTaskFactsFn = deps.readTaskFacts ?? readTaskFacts;
  const evaluateFn = deps.evaluate ?? evaluateGates;

  let inFlight = false;

  function wakeBudgetSpent(at) {
    state.wakeTimes = (state.wakeTimes ?? []).filter((t) => typeof t === 'number' && at - t < HOUR_MS);
    return state.wakeTimes.length >= config.maxWakesPerHour;
  }

  /**
   * cron 是否到点：首次见面只登记"上次触发=现在"，绝不刚装上就误触发一次。
   *
   * 2026-11-14 00:30 改：**这里不再推进 state.crons**。
   *   病（实测）：23:30:30 的 `30 8,23 * * *` 到点了，却因为 maxWakesPerHour=4 被限流
   *   （日志 poll #13: change () but rate-limited），而 crons 已经记成"触发过"
   *   ⇒ 那一跳的写回永久丢了，下一跳也不会再补。
   *   现在：due 带 next，真正唤醒成功之后才由 advanceCrons() 推进；
   *   限流/失败都不推进 ⇒ 下一跳还会 due，能补上。
   */
  function dueCrons(at) {
    const due = [];
    for (const cron of config.crons) {
      const lastFired = typeof state.crons[cron.expr] === 'number' ? state.crons[cron.expr] : undefined;
      if (lastFired === undefined) {
        state.crons[cron.expr] = at; // 基线，必须写：否则每跳都会当成"首次见面"
        continue;
      }
      let next;
      try {
        next = nextOccurrence(cron.expr, cron.timeZone, lastFired);
      } catch (err) {
        log(`cron 表达式无效，已跳过：${cron.expr} — ${String(err && err.message ? err.message : err)}`);
        continue;
      }
      if (next !== null && next <= at) {
        // 注意：不写 state.crons —— 推进交给 advanceCrons()
        due.push({ ...cron, firedAt: next, nextDue: next });
      }
    }
    return due;
  }

  /** 真正发出去之后才推进 cron 基线。只推进本次 due 且已成功投递的那些。 */
  function advanceCrons(due) {
    for (const c of due ?? []) {
      if (typeof c.nextDue === 'number') state.crons[c.expr] = c.nextDue;
    }
  }

  /** 三道门：只有真要唤醒时才读 ledger，no change 的跳一次额外读盘都没有。 */
  function runGates(at, sessionIdle) {
    const ledger = readLedgerFn(config.ledgerFile, at);
    const task = readTaskFactsFn(config.unfinishedFile, config.queueFile);
    const decision = evaluateFn({ config, atMs: at, ledger, task, sessionIdle });
    return { ledger, task, decision };
  }

  async function tick() {
    if (inFlight) {
      log(`poll #${state.polls}: skipped (previous tick still running)`);
      return { skipped: 'inflight' };
    }
    inFlight = true;
    try {
      const at = now();
      state.polls = (state.polls ?? 0) + 1;
      if (state.startedAt === null || state.startedAt === undefined) state.startedAt = at;

      // ---- 重启识别（2026-11-18 00:10）----
      // 见文件顶部 BOOT_AT 的注释：state.startedAt 认不出重启，这里用 bootAt 认。
      const lastBoot = Number(state.bootAt) || 0;
      const isNewProcess = BOOT_AT > lastBoot + 1000;   // 1 秒容差：同一进程重复 tick 不会误判
      const sinceLastPoll = at - (Number(state.lastPollAt) || 0);
      const pollMs = Math.max(60 * 1000, Number(config.pollIntervalSeconds || 300) * 1000);
      // 新进程 + 距上次 poll 超过 2 个周期 ⇒ 这是一次"从重启里回来"，这一跳跳过冷却。
      const wokeFromRestart = isNewProcess && sinceLastPoll > pollMs * 2;
      if (isNewProcess) {
        state.bootAt = BOOT_AT;
        state.boots = (Number(state.boots) || 0) + 1;
        state.lastBootGapMs = sinceLastPoll > 0 ? sinceLastPoll : 0;
        log(`boot #${state.boots}: 新进程起来（距上次 poll ${Math.round(sinceLastPoll / 1000)} 秒）` +
          (wokeFromRestart ? ` ⇒ 跳过冷却，直接判这一跳` : ''));
      } else if (!Number.isFinite(state.bootAt)) {
        // 老 state 没有 bootAt ⇒ 补上，不假装知道自己起于何时
        state.bootAt = BOOT_AT;
        state.boots = (Number(state.boots) || 0);
      }

      // ---- 自我调度（2026-11-17 加）：比 cron、比所有探针都高，必须在这之前的早退里就考虑到 ----
      const selfFile = selfScheduleFileOf(config);
      const self = readSelfSchedule(selfFile);
      const selfV = judgeSelfSchedule(self, at);
      if (selfV && selfV.verdict === 'sleep') {
        log(`poll #${state.polls}: self-sleep: 还剩 ${selfV.remainingMin} 分钟（why=${selfV.why}）`);
      }

      // ---- Tier 0：纯脚本判断，一个模型请求都不发 ----
      // 2026-11-18 00:10：`wokeFromRestart` 时把冷却当 0 —— 重启后第一次跳不该被 90 分钟冷却挡住。
      const idleCooldownUntil = wokeFromRestart ? 0 : idleCooldownUntilOf(state, config);
      const results = config.sentinels.map((spec) => probe(spec, state.sentinels[spec.id], at, config, { idleCooldownUntil }));
      for (const r of results) {
        state.sentinels[r.id] = {
          exists: r.exists,
          mtimeMs: r.mtimeMs,
          hash: r.hash,
          stale: r.stale === true,
          facts: { ...r.facts },
          at
        };
      }

      const crons = dueCrons(at);
      const changed = results.filter((r) => r.change !== null && r.change !== undefined);
      state.lastPollAt = at;

      // self-wake 到期时，就算什么都没变也要走到门那里去（否则强制叫醒永远穿不透这一层）
      const selfForcedWake = selfV && selfV.verdict === 'wake';
      if (changed.length === 0 && crons.length === 0 && !selfForcedWake) {
        saveState(state);
        log(`poll #${state.polls}: no change (${results.length} sentinels, ${results.reduce((n, r) => n + r.bytes, 0)} bytes, 0 wake)`);
        return { poll: state.polls, at, changed: [], crons: [], woke: false, skipped: 'no-change' };
      }

      // ---- 三道门（串联）----
      const sessionIdle = changed.some((r) => r.kind === 'session' && r.change === 'idle');
      const { ledger, task, decision } = runGates(at, sessionIdle);
      state.lastGate = {
        at,
        peak: decision.peak,
        peakRaw: decision.peakRaw,
        mode: decision.mode,
        reason: decision.reason,
        balance: decision.balance.total,
        balanceFloorCny: decision.balance.floorCny,
        unfinishedPending: decision.task.unfinishedPending,
        queuePending: decision.task.queuePending,
        // 2026-11-17 21:38：gates 算出"可做数"了，但这里手写字段列表会把它丢掉 ——
        // 算出来 ≠ 写进去。补上，否则外面永远看不见门 2 判的是什么。
        unfinishedDoable: decision.task.unfinishedDoable,
        blockedOnTian: decision.task.blockedOnTian,
        hasDoableWork: decision.task.hasDoableWork,
        boundaryAt: decision.boundaryAt
      };
      log(`poll #${state.polls}: ${describeDecision(decision)}`);

      // 门不放行：只记录。阻塞的两种原因分开写清，别让人误以为"什么都没发生"。
      // 顺序（2026-11-17 加自我调度后）：
      //   ① 余额不足   —— 最硬，谁也盖不过（没钱了不许叫）
      //   ② self-sleep —— 我睡下去了，这一跳绝对不叫
      //   ③ self-wake  —— 我设的闹钟到了，盖过 closed 也要叫
      //   ④ closed     —— 收工了，只有 cron 到点还叫
      let allowed;
      if (decision.mode === 'blocked-balance') allowed = false;
      else if (selfV && selfV.verdict === 'sleep') allowed = false;
      else if (selfForcedWake) allowed = true;
      else if (decision.mode === 'closed') allowed = crons.length > 0; // 建设线收工：只有 cron 到点还叫
      else allowed = true;

      if (!allowed) {
        state.lastDelta = decision.reason;
        saveState(state);
        log(
          `poll #${state.polls}: no wake — ${decision.reason}` +
            ` (changed=[${changed.map((c) => `${c.id}:${c.change}`).join(',')}] crons=[${crons.map((c) => c.expr).join(',')}])`
        );
        return {
          poll: state.polls,
          at,
          changed: changed.map((c) => c.id),
          crons: crons.map((c) => c.expr),
          woke: false,
          skipped: decision.mode,
          decision
        };
      }

      const delta = changed.length > 0
        ? renderDelta(results)
        : `到点：${crons.map((c) => c.expr).join(' / ')}`;
      const text = buildWakeText({ config, state, at, results, delta, crons, decision, ledger });
      const summary = buildSummary(state, delta, decision);

      // 2026-11-17 21:36 改（天：有什么好限流的，万一真的需要频繁重启怎么办）：
      //   原来 selfForcedWake 也吃 maxWakesPerHour=4 的配额。但这两件事完全不同：
      //     · 系统探测叫醒（探针变了/门判有活）  ⇒ 可能刷屏，该限流
      //     · 我自己设的 wake（breathe 到点）    ⇒ 我已经知道要醒，这不是"判断失误"，
      //       是"我安排的事"，限流它等于让闹钟失效 —— 今晚重启四次，每次都靠它兜底，
      //       四次就把 4/h 用光了，第五次起永远叫不醒。
      //   现在：selfForcedWake 不走配额（但成功叫醒后照样 push 一条留痕，好复盘）。
      if (!selfForcedWake && wakeBudgetSpent(at)) {
        saveState(state);
        log(`poll #${state.polls}: change [${changed.map((c) => `${c.id}:${c.change}`).join(',')}] but rate-limited (${config.maxWakesPerHour}/h) gate=${decision.mode}`);
        return {
          poll: state.polls,
          at,
          changed: changed.map((c) => c.id),
          crons: crons.map((c) => c.expr),
          woke: false,
          skipped: 'rate-limited',
          decision,
          wakeText: text
        };
      }

      // 冷却在"投递"这一刻就起算（不等 round 落定）：否则失败/busy 时会每跳重试。
      state.idleCooldownUntil = at + config.idleCooldownMinutes * 60000;

      // ★ 2026-11-18 00:12 修（实测出来的）：**状态先落盘，再投递**。
      //   病：原来 saveState 在 `await wake(...)` **之后**（:355）。而 wake 投递到 "cold handle acquired"
      //       之后可能迟迟不 resolve ⇒ 下面一行都跑不到 ⇒ state 冻在**上一跳**。
      //       实测：poll #557 打了 boot #1、走了 wake、日志停在 `wake.resume: ok`，
      //       而 state 文件 mtime 还是 00:05:17（上一跳 #556 写的）⇒ polls 一直是 556 不涨。
      //   为什么必须先落盘：冷却/bootAt/计数器都是"这一跳已经发生"的事实，
      //       它们不该等一次网络投递的结果。投递失败是下一跳的事（重试逻辑在下面照常）。
      //   投递结果照旧在 :355 再 save 一次 —— 两次写，后者带 outcome，前者保底。
      saveState(state);

      log(`poll #${state.polls}: change [${changed.map((c) => `${c.id}:${c.change}`).join(',')}]${crons.length > 0 ? ` +cron [${crons.map((c) => c.expr).join(',')}]` : ''} -> wake`);

      let outcome;
      try {
        outcome = await wake({ text, summary, at, delta, decision });
      } catch (err) {
        outcome = { outcome: 'failed', error: String(err && err.message ? err.message : err) };
      }

      // 只有"系统叫的"才计入配额。self-wake 是我自己排的闹钟，不计（见上面 21:36 那段说明）。
      if (!selfForcedWake) state.wakeTimes.push(at);
      if (outcome?.outcome === 'ok') {
        state.wakes = (state.wakes ?? 0) + 1;
        state.lastWakeAt = at;
        // 2026-11-14：只有真叫醒了才推进 cron 基线 —— 限流/失败都留到下一跳重试。
        advanceCrons(crons);
        // 2026-11-17：self-wake 成功叫醒之后必须清掉，否则每一跳都会强制叫。
        // 失败/限流不清 —— 留着下一跳重试，跟 cron 基线同一套口径。
        if (selfForcedWake) {
          try {
            unlinkSync(selfFile);
            log(`poll #${state.polls}: self-wake 已叫醒，调度清除（why=${selfV.why}）`);
          } catch (e) {
            log(`poll #${state.polls}: self-wake 叫醒了但清不掉文件：${String(e && e.message ? e.message : e)}`);
          }
        }
      }
      state.lastDelta = delta;
      state.lastFacts = renderFacts(results);
      saveState(state);

      log(`poll #${state.polls}: wake outcome=${outcome?.outcome ?? 'unknown'}${outcome?.presence ? ` presence=${outcome.presence}` : ''}${outcome?.error ? ` error=${outcome.error}` : ''}`);
      return {
        poll: state.polls,
        at,
        changed: changed.map((c) => c.id),
        crons: crons.map((c) => c.expr),
        woke: outcome?.outcome === 'ok',
        outcome: outcome?.outcome ?? 'unknown',
        sessionId: outcome?.sessionId,
        presence: outcome?.presence,
        decision,
        wakeText: text
      };
    } finally {
      inFlight = false;
    }
  }

  return { tick, state };
}
