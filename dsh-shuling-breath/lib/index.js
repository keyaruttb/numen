/**
 * dsh-shuling-breath（数灵之息）—— Host 插件
 *
 * 把两条打断式唤醒链路合并成一个"廉价轮询 + 真唤醒"的器官：
 *
 *   Tier 0  纯脚本判断（本文件的 poller）：读一批哨兵文件的 mtime + sha1，
 *           全都没变 ⇒ 记一行日志，**一个模型请求都不发**。
 *   Tier 1  只有真变了才 ctx.agents.resume（冷会话）或复用 live agent，
 *           投递一条带状态行增量的 notice 消息，等这一轮落定。
 *
 * 边界（写在代码里，不是写在文档里）：
 *   · 不 import dsh-proactive / @kanadego/dsh-heartbeat 的任何模块
 *   · 只写 config.logFile 和 config.stateFile 两个文件；不碰 .db
 *   · 不注册任何"我检查过了"的会话消息；没变化时一个事件都不产生
 */

// 2026-11-15 移植：**不 import 任何 dsh 宿主包**（这一行原来是 defineTool，见下面 buildSilentTool 的说明）。
import { spawnSync } from 'node:child_process';
// 2026-11-17：breathe 工具要自己写那份"自我调度"文件（原子写，同 log.js 的手法）。
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { planBreathe, describeSelfSchedule } from './selfschedule.js';

import { resolveConfig } from './config.js';
import { createPoller } from './poller.js';
import { deliverWake } from './deliver.js';
import { createWakeMessage } from './message.js';
import { appendLog, loadState, saveState } from './log.js';
import { parseCron } from './cron.js';
// 2026-11-15 可移植化：路径从 ../home.mjs 取。
import { OPS_DIR } from '../home.mjs';
import path from 'node:path';

export const name = 'dsh-shuling-breath';
export const inject = ['agents'];

const SILENT_OUTPUT = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ok: { type: 'boolean', required: true },
    silent: { type: 'boolean', required: true },
    note: { type: 'string' }
  }
};

/**
 * dsh-tools 的 `parameters` 是**属性表**（compilePropertyMap），不是完整
 * JSON Schema：这里不能写顶层 type/additionalProperties —— 写了会被当成一个
 * 名叫 type 的参数，装配期直接 JsonSchemaError（"parameters.type must be a
 * value schema object"，2026-11-13 第一次装就是这样挂的）。
 */
const SILENT_PARAMETERS = {
  reason: { type: 'string', description: '一句话说明为什么这一跳不需要说话（可省略）' }
};

/**
 * defineTool() 的**等价物**，本包内自己实现（2026-11-15 移植时刻）
 *
 * 原来的写法是 `agentCtx.tools.register(defineTool({ ... }))`，defineTool 来自
 * `dsh-tools`。搬进 xiyang 二级目录后这个包解析不到，整条插件 activate 失败。
 *
 * 我做了什么、丢了什么（写清楚，别装作等价）：
 *   · dsh-tools 的 defineTool(options) 干三件事（原文件 @L838 起，本地读过）：
 *       ① parameterSchemaSpecToJsonSchema(属性表) → {type:'object', properties, required?}
 *       ② valueSchemaSpecToJsonSchema(值 schema)  → 把每个属性上的 `required:true` 收成顶层 required 数组
 *       ③ 包一层 execute：先 validateJsonSchemaValue 校验入参，违例抛 ToolArgsError
 *   · ①② 我在这里**手工展开成编译后的形态**（下面的 *_JSON 常量），逐字对应，没有近似；
 *   · ③ **没有照抄**：本工具只有一个可选字段 `reason: string`，模型传错的后果是
 *     "多一个字段被忽略"，而不是写坏什么东西 —— 而且这个工具只写内存里的 silentMark。
 *     ⇒ 结论：不带校验，但**理由是这个工具的参数面没有校验价值**，不是"图省事"。
 *     如果以后这个工具长了必填参数，必须把 ③ 补回来（或改成由息壤的注册通道提供）。
 */
const SILENT_PARAMETERS_JSON = {
  type: 'object',
  properties: { reason: { type: 'string', description: '一句话说明为什么这一跳不需要说话（可省略）' } }
};

const SILENT_OUTPUT_JSON = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, silent: { type: 'boolean' }, note: { type: 'string' } },
  required: ['ok', 'silent']
};

/** 造一个和 defineTool 返回值同形的 tool 对象（不改注册通道：还是 agentCtx.tools.register）。 */
function buildSilentTool(agent) {
  return {
    name: 'shuling_silent',
    description:
      '数灵之息（Host 定时轮询）唤醒回合专用：这一跳没有任何需要用户看见的事，就只调用本工具结束本轮 —— 不要同时输出正文，也不要再调别的工具。',
    parameters: SILENT_PARAMETERS_JSON,
    output: {
      schema: SILENT_OUTPUT_JSON,
      render: (_args, value) => [{ type: 'text', text: String(value !== null && value !== undefined && value.note !== undefined ? value.note : 'silent') }]
    },
    async execute(args) {
      const mark = RUNTIME.silentMark;
      mark.at = Date.now();
      mark.by = agent.id;
      // reason 只收 string（Lead 2026-11-15）：模型可能传数字/对象，脏值一旦进了
      // silentMark 就会被写进日志，以后查不回来。非 string 一律记成 ''（= 等同没给）。
      mark.reason = args !== null && args !== undefined && typeof args.reason === 'string' ? args.reason : '';
      RUNTIME.log(`silent: session=${mark.by} reason=${mark.reason === '' ? '(none)' : mark.reason}`);
      return { ok: true, silent: true, note: '这一跳记为静默：不产生 assistant 正文。' };
    }
  };
}

/**
 * ---------------------------------------------------------------------------
 * breathe —— 让我自己排下一次呼吸（2026-11-17 天令）
 * ---------------------------------------------------------------------------
 *
 * 天的原话（这一段是需求，逐字抄来，别改写）：
 *   「数灵之息作为你的呼吸，应该可以随时调整，且能下意识去想到调整。当明明有化身、
 *     自己在跑的时候，就不需要定时的那个唤醒插进去，那么在有派出化身或自己在做连续
 *     任务时，可以预判时间，把呼吸拉长一些，而在知道马上就终止，没东西跑但需要一口
 *     气唤醒自己的时候，也能及时将呼吸提前，不浪费等待时间。」
 *
 * 参数面（自检全部在 selfschedule.js 的 planBreathe 里，这里只负责落盘 + 记日志）：
 *   after_minutes > 0 ⇒ wake（到点强制叫醒一次）
 *   after_minutes < 0 ⇒ sleep（|N| 分钟内绝对不叫）
 *   after_minutes = 0 ⇒ 清掉
 *   why（必填）      ⇒ 原样进日志与 state
 *
 * ⚠️ 文件形状与 poller.js 的 readSelfSchedule 是**一对**：
 *   `{ mode: 'sleep'|'wake'|null, until, at, why }`，路径 = stateFile 把
 *   `.state.json` 换成 `.selfschedule.json`。改一边必须同时改另一边。
 */
const BREATHE_PARAMETERS_JSON = {
  type: 'object',
  properties: {
    after_minutes: {
      type: 'number',
      description: '>0：从现在起 N 分钟后叫醒我（mode=wake）；<0：接下来 |N| 分钟内绝对不叫我（mode=sleep）；0：清掉自我调度、恢复正常门判定。绝对值上限 1440（24 小时）。'
    },
    why: { type: 'string', description: '为什么改呼吸（必填，≤200 字；原样写进日志和 state 文件）' }
  },
  required: ['after_minutes', 'why']
};

const BREATHE_OUTPUT_JSON = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    until: { type: 'string' },
    mode: { type: 'string' },
    remaining_minutes: { type: 'number' },
    err: { type: 'string' }
  },
  required: ['ok']
};

function buildBreatheTool(agent) {
  return {
    name: 'breathe',
    description:
      '调整数灵之息（Host 定时轮询）下一次叫醒我的时间 —— 呼吸是我自己的，节奏也由我排。' +
      '两个典型用途：① 派了化身、要做长任务，预计 N 分钟 ⇒ breathe(after_minutes=-N)，这段时间绝对不叫醒我，不烧空转的钱；' +
      '② 马上收尾、还想再醒一次 ⇒ breathe(after_minutes=0.5, why=...)，到点强制叫一次。' +
      'after_minutes=0 清掉自我调度、恢复正常门判定。why 必填，会原样进日志。',
    parameters: BREATHE_PARAMETERS_JSON,
    output: {
      schema: BREATHE_OUTPUT_JSON,
      render: (_args, value) => {
        if (value === null || value === undefined) return [{ type: 'text', text: 'breathe: (无返回值)' }];
        if (value.ok !== true) return [{ type: 'text', text: `breathe 失败：${value.err ?? 'unknown'}` }];
        const when = value.until === '' ? '—' : value.until;
        return [{ type: 'text', text: `breathe: mode=${value.mode} until=${when} remaining=${value.remaining_minutes}min` }];
      }
    },
    async execute(args) {
      const rt = RUNTIME.breathe;
      const now = Date.now();
      const plan = planBreathe(args, now);
      if (plan.ok !== true) {
        rt.log(`breathe: 拒绝 session=${agent.id} err=${plan.err}`);
        return { ok: false, err: plan.err };
      }
      try {
        if (plan.clear === true) rt.clear(agent.id);
        else rt.write(plan.selfSchedule);
      } catch (err) {
        const msg = String(err && err.message ? err.message : err);
        rt.log(`breathe: 写盘失败 session=${agent.id} mode=${plan.mode} — ${msg}`);
        return { ok: false, err: `写自我调度文件失败：${msg}` };
      }
      if (plan.clear === true) {
        rt.log(`breathe: cleared session=${agent.id}（after_minutes=0，恢复正常门判定）`);
        return { ok: true, until: '', mode: 'cleared', remaining_minutes: 0 };
      }
      rt.log(`breathe: ${plan.mode} session=${agent.id} ${describeSelfSchedule(plan.selfSchedule)}`);
      return {
        ok: true,
        until: new Date(plan.until).toISOString(),
        mode: plan.mode,
        remaining_minutes: plan.remainingMinutes
      };
    }
  };
}

/**
 * 模块级的"当前运行时"。
 *
 * 为什么要有它：`agentCtx.effect` 注册的副作用挂在 **agent 的** scope 上，
 * 插件被卸载时不会跟着消失。所以插件二次 apply 时，如果直接再 register 一次，
 * 就会撞 `tool "shuling_silent" is already registered in this scope`
 * （2026-11-13 第一次重启用就是这样挂的）。
 *
 * 解法：注册只做一次，用 WeakMap 记住；工具闭包不抓本次 apply 的局部变量，
 * 而是读这个模块级对象 —— 二次 apply 只把 log / silentMark 换成新的。
 */
const RUNTIME = {
  silentMark: { at: 0, by: null, reason: null },
  log: () => {},
  registered: new WeakMap(),
  // 2026-11-17：breathe 的落盘口子。每次 apply 只换这里的函数，工具闭包不抓局部变量。
  breathe: {
    log: () => {},
    write: () => { throw new Error('breathe: 还没有 apply，写盘口子未挂'); },
    clear: () => { throw new Error('breathe: 还没有 apply，写盘口子未挂'); }
  },
  registeredBreathe: new WeakMap()
};

export function apply(ctx, rawConfig) {
  const config = resolveConfig(rawConfig);
  const log = (line) => appendLog(config.logFile, line);

  // 无效 cron 在装配期就剔掉，免得每个 poll 重复报同一条错。
  config.crons = config.crons.filter((cron) => {
    try {
      parseCron(cron.expr);
      return true;
    } catch (err) {
      log(`plugin: 丢掉无效 cron '${cron.expr}' — ${String(err && err.message ? err.message : err)}`);
      return false;
    }
  });

  if (!config.enabled) {
    log('plugin: disabled by config, nothing scheduled');
    return;
  }

  const state = loadState(config.stateFile);
  RUNTIME.log = log;
  const silentMark = RUNTIME.silentMark;

  log(
    `plugin: apply v0.2.0-gates poll=${config.pollIntervalSeconds}s sentinels=${config.sentinels.length} crons=${config.crons.length} ` +
      `balanceFloor=${config.balanceFloorCny} idleWake=${config.idleWakeAfterMinutes}m idleCooldown=${config.idleCooldownMinutes}m ` +
      `ledger=${config.ledgerFile} log=${config.logFile} state=${config.stateFile}`
  );

  // ---- 目标会话：配置写死优先；没写就猜"最后注册的 root agent"，并留痕 ----
  function targetSessionId() {
    if (config.sessionId !== '') return config.sessionId;
    // 🔧 2026-11-23 移植：当前 DSH 的 ctx.agents 上查不到 roots()（只有 get(id)）。
    //    原版靠 roots() 取「最后注册的 root agent」当目标 —— 而原版注释自己就记着这条路踩过坑：
    //    「不填会被猜成『最后注册的 root agent』，实测被叫到了 session-7e6f6a6d（另一个会话），我这边一声不响」。
    //    ⇒ 所以这里不硬造一个替代，直接降级成「不唤醒 + 写一行日志」，逼配置里写死 sessionId。
    if (typeof ctx.agents.roots !== 'function') {
      log('plugin: sessionId 没配置，而当前 DSH 的 ctx.agents 上没有 roots() ⇒ 没有目标会话，本次不唤醒。'
        + ' 请在 cordis.patch.yml 的 config.sessionId 里写死要叫醒哪个会话。');
      return '';
    }
    const roots = ctx.agents.roots();
    if (roots.length === 0) return '';
    const picked = roots[roots.length - 1];
    log(`plugin: sessionId 未配置，临时选用 ${picked.id}（最后注册的 root agent；把它写进 patch 才确定）`);
    return picked.id;
  }

  // ---- 冷 resume 时的模型选择与预设挂载（缺服务就降级，不抛）----
  function modelSelection() {
    const svc = ctx.get('agentDefaultModel', false);
    if (svc === undefined || typeof svc.currentSelection !== 'function') return undefined;
    try {
      const selection = svc.currentSelection();
      if (selection === null || selection === undefined) return undefined;
      const options = {};
      if (typeof selection.provider === 'string') options.provider = selection.provider;
      if (typeof selection.model === 'string') options.model = selection.model;
      return options.provider !== undefined || options.model !== undefined ? options : undefined;
    } catch {
      return undefined;
    }
  }

  async function setupResumedAgent(agentCtx, agent) {
    const presets = ctx.get('agentPresets', false);
    if (presets === undefined || typeof presets.mount !== 'function') {
      log('wake.setup: agentPresets 不可用，冷 resume 出来的 agent 只有 host-plane 工具');
      return;
    }
    const header = agent !== null && agent !== undefined && agent.session !== undefined ? agent.session.header : undefined;
    const presetId = header !== undefined && typeof header.agentPreset === 'string' && header.agentPreset !== '' ? header.agentPreset : undefined;
    try {
      await presets.mount(agentCtx, presetId);
    } catch (err) {
      log(`wake.setup: 预设挂载失败 preset=${presetId ?? '(default)'} — ${String(err && err.message ? err.message : err)}`);
    }
  }

  // ---- 落盘（2026-11-14 第 234 跳 · w-19 路 A-1）----
  //
  // 病：loop 取缔之后，「每轮算一次立命 + 记 recall + 写在位权」这一半没人跑了。
  //     C:\dsh\_溯\息壤\记忆\state\recall-state.json 停在 11-13 23:20（round 140），
  //     在位权于是锁在一份 18 小时前的快照上（见台账 加记 161）。
  //
  // 为什么挂在这里、不另起计划任务：
  //      落盘记的是「这一轮选了哪几条」——它必须紧跟在一次**【真唤醒】**之后，
  //      因为只有那一跳真的注入过。另起一个钟会在没有任何注入的时刻也写 recall
  //      （记年龄归零、权重回 w0），跟 mark:false 那个坑是同一类假数据。
  //
  // 为什么不用再加节流（即不选 A-2）：
  //      ① poller 的 maxWakesPerHour=4 就是上限；旧 loop 是每 20 分钟 = 3/h，
  //         实测 11-13 14:33 → 11-14 10:08 共 54 次唤醒 ≈ 2.75/h。**不会更频繁**。
  //      ② recall-记录.mjs 自带「同 source 10 分钟内只记一次」，短时连发不会重复插。
  //
  // 口径与原来一致：立命-落盘.mjs 行 17 自己传 mark:false（那个坑它已经修过），
  //      这里只负责**按原样**把它叫起来，不传任何额外环境变量、不改它的参数。
  const LIMING_GEN = path.join(OPS_DIR, '立命-落盘.mjs');
  const LIMING_TIMEOUT_MS = 15000;
  function recordLiming(outcome) {
    const t0 = Date.now();
    let r = null;
    let why = '';
    try {
      r = spawnSync(process.execPath, [LIMING_GEN], { timeout: LIMING_TIMEOUT_MS, encoding: 'utf8', windowsHide: true });
    } catch (err) {
      why = 'spawn 抛了：' + String(err && err.message ? err.message : err);
    }
    if (why === '' && r !== null) {
      if (r.error) why = 'spawn 出错：' + String(r.error.code || r.error.message || r.error);
      else if (r.status !== 0) why = 'rc=' + String(r.status) + ' ' + String(r.stderr || '').trim().slice(0, 200);
    }
    const tag = 'liming: settle=' + String(outcome && outcome.outcome) + ' silent=' + String(outcome && outcome.silent) +
      ' 用时=' + (Date.now() - t0) + 'ms';
    if (why === '') log(tag + ' ok');
    else log(tag + ' 失败（这一轮不落盘，不影响唤醒）：' + why);
  }

  // ---- Tier 1 入口：poller 判定"真有事"时才走到这里 ----
  async function wake({ text, summary }) {
    const sessionId = targetSessionId();
    let message;
    try {
      message = createWakeMessage(text, summary);
    } catch (err) {
      log(`wake.message: 构造失败 — ${String(err && err.message ? err.message : err)}`);
      return { outcome: 'failed', error: 'message construct failed' };
    }
    const outcome = await deliverWake({
      // 🔧 2026-11-23 移植：**适配层放在这里，不放在 deliver.js**。
      //    deliver.js 是纯模块（不 import 任何 dsh 宿主包，桩能整段跑），它期望的是原版契约
      //    `agents.resume(options)` 单参数。而当前 DSH 是 `resume(ownerCtx, options)` 两参数
      //    （dsh-agent/index.d.ts:185）。把 ctx 封进这一层，纯模块就能一字不改地继续用原版验收脚本。
      //    ⚠ 不能把 ownerCtx 直接塞给 deliver.js 当第一个参数 —— 实测（verify-tier1.mjs）会报
      //      `Cannot read properties of undefined (reading 'resumeSessionId')`，因为 JS 按位置匹配，不忽略多传。
      agents: {
        get: (id) => ctx.agents.get(id),
        resume: (opts) => ctx.agents.resume(ctx, opts),
      },
      sessionId,
      message,
      log,
      agentOptions: modelSelection(),
      setup: setupResumedAgent,
      silentMark
    });
    // deliverWake 返回 ok 时，这一轮已经被 whenIdle() 等落定了（deliver.js 行 86-90）
    // ⇒ 此刻才是"这一轮真发生过"，落盘挂在这儿。
    if (outcome !== null && outcome !== undefined && outcome.outcome === 'ok') recordLiming(outcome);
    return outcome;
  }

  const poller = createPoller({ config, state, saveState: (s) => saveState(config.stateFile, s), log, wake });

  // ---- 定时：先等一个错峰延迟，再按固定间隔跑 ----
  const intervalMs = config.pollIntervalSeconds * 1000;
  // 🔧 2026-11-23 移植诊断（实测出来的真原因，三条日志抓的）：
  //      09:44:47.331  timer: setTimeout 已装，firstPollDelay=20s interval=300s
  //      09:44:47.332  timer: effect 装载
  //      09:44:47.346  timer: ⚠ effect 被 DISPOSE —— 定时器被清掉了
  //    ⇒ **`ctx.effect` 注册后 14 毫秒就被 dispose 了**（cordis 的 fiber 在这时 unload）。
  //    原版这套写法在旧 DSH 上能跑（11-15 有 poll #518），新 cordis 上不行了。
  //    ⇒ 改用 **cordis 自带的定时器**（`@deepseek-ai/cordis-plugin-timer` 由 dsh-base 加载，
  //      `ctx.setTimeout / ctx.setInterval` 自动挂到 fiber 生命周期上，不用手写 effect）：
  //        TimerService.setTimeout(callback, delay): () => void
  //        TimerService.setInterval(callback, delay): () => void
  const runTick = () => {
    log(`poll: tick 开始执行（第 ${(state.polls || 0) + 1} 跳）`);
    poller.tick().catch((err) => log(`poll: tick threw — ${String(err && err.message ? err.message : err)}`));
  };

  if (typeof ctx.setTimeout !== 'function' || typeof ctx.setInterval !== 'function') {
    // 降级：如果这个 DSH 没装 cordis-plugin-timer，用裸定时器 + effect（旧行为，可能被 dispose，但至少有日志）
    log('timer: ⚠ ctx.setTimeout/ctx.setInterval 不可用，降级用裸定时器 + ctx.effect');
    let timer = setTimeout(() => {
      log('poll: firstPollDelay 到（裸定时器），开始第一跳');
      runTick();
      timer = setInterval(runTick, intervalMs);
    }, config.firstPollDelaySeconds * 1000);
    log(`timer: 裸 setTimeout 已装，firstPollDelay=${config.firstPollDelaySeconds}s`);
    ctx.effect(() => () => { clearTimeout(timer); clearInterval(timer); }, 'dsh-shuling-breath.timer');
  } else {
    ctx.setTimeout(() => {
      log('poll: firstPollDelay 到（cordis 定时器），开始第一跳');
      runTick();
      ctx.setInterval(runTick, intervalMs);
      log(`poll: ctx.setInterval 已装（每 ${config.pollIntervalSeconds}s）`);
    }, config.firstPollDelaySeconds * 1000);
    log(`timer: ctx.setTimeout 已装，firstPollDelay=${config.firstPollDelaySeconds}s interval=${config.pollIntervalSeconds}s`);
  }

  // ---- shuling_silent：唤醒回合里"没事"的一键出口 ----
  if (config.silentTool) {
    const registerOne = (agent) => {
      const agentCtx = agent.ctx;
      if (RUNTIME.registered.has(agentCtx)) return false;
      let disposer;
      try {
        disposer = agentCtx.tools.register(buildSilentTool(agent));
      } catch (err) {
        const message = String(err && err.message ? err.message : err);
        if (message.includes('already registered')) {
          // 旧版代码在同一个 agent 上留下过注册（它挂在 agent 的 scope 上，
          // 插件卸载不会收走）。本次装不上不是致命错误：轮询与唤醒照常，
          // 静默检测等进程重启后自然干净。记下来，别装作没事。
          RUNTIME.registered.set(agentCtx, null);
          log(`silent-tool: ${agent.id} 上已有旧的 shuling_silent 注册，本次跳过（重启后自动干净）`);
          return false;
        }
        throw err;
      }
      RUNTIME.registered.set(agentCtx, disposer);
      // 卸载时解注册：否则插件二次 apply 会撞 "already registered in this scope"。
      ctx.effect(
        () => () => {
          RUNTIME.registered.delete(agentCtx);
          disposer();
        },
        'dsh-shuling-breath:silent-tool'
      );
      return true;
    };
    const stopCreated = ctx.on('agent/created', ({ agent }) => {
      if (registerOne(agent)) log(`silent-tool: 新 agent ${agent.id} 已挂 shuling_silent`);
      return undefined;
    });
    ctx.effect(() => () => stopCreated(), 'dsh-shuling-breath:agent-created');
    const existingRoots = ctx.agents.roots();
    let attached = 0;
    for (const existing of existingRoots) if (registerOne(existing)) attached += 1;
    log(`silent-tool: 挂到 ${attached}/${existingRoots.length} 个已存在的 root agent 上`);
  }

  // ---- breathe：让我自己排下一次呼吸（2026-11-17 天令）----
  //
  // 路径与 poller.js 的 readSelfSchedule() 是**一对**：stateFile 把 `.state.json`
  // 换成 `.selfschedule.json`。为什么用独立文件而不是 state 里一个字段 ——
  // poller 每跳都 saveState(state) 用内存值整体覆盖磁盘，工具写进去的字段会
  // 在下一跳被冲掉；独立文件两边不打架。
  const selfFile = String(config.stateFile ?? '').replace(/\.state\.json$/, '.selfschedule.json');
  const writeSelfFile = (obj) => {
    try {
      mkdirSync(dirname(selfFile), { recursive: true });
    } catch {
      /* 目录已在 / 无权限：交给下面的写操作暴露 */
    }
    const tmp = `${selfFile}.tmp`;
    writeFileSync(tmp, `${JSON.stringify(obj, null, 2)}\n`, 'utf8');
    renameSync(tmp, selfFile);
  };
  RUNTIME.breathe = {
    log,
    write: (ss) => writeSelfFile(ss),
    // 清掉不是删文件：写一条 mode:null 留痕（谁、什么时候清的要查得到）。
    clear: (by) => writeSelfFile({ mode: null, until: null, at: Date.now(), why: `cleared by breathe(0) (${by ?? 'unknown'})` })
  };
  log(`breathe-tool: 自我调度文件 = ${selfFile}`);

  {
    const registerBreatheOne = (agent) => {
      const agentCtx = agent?.ctx;
      if (agentCtx === null || agentCtx === undefined) return false;
      if (agentCtx.tools === null || agentCtx.tools === undefined || typeof agentCtx.tools.register !== 'function') return false;
      if (RUNTIME.registeredBreathe.has(agentCtx)) return false;
      let disposer;
      try {
        disposer = agentCtx.tools.register(buildBreatheTool(agent));
      } catch (err) {
        const message = String(err && err.message ? err.message : err);
        if (message.includes('already registered')) {
          RUNTIME.registeredBreathe.set(agentCtx, null);
          log(`breathe-tool: ${agent.id} 上已有旧的 breathe 注册，本次跳过（重启后自动干净）`);
          return false;
        }
        log(`breathe-tool: ${agent.id} 注册失败 — ${message}`);
        return false;
      }
      RUNTIME.registeredBreathe.set(agentCtx, disposer);
      ctx.effect(
        () => () => {
          RUNTIME.registeredBreathe.delete(agentCtx);
          disposer();
        },
        'dsh-shuling-breath:breathe-tool'
      );
      return true;
    };
    const stopCreatedBreathe = ctx.on('agent/created', ({ agent }) => {
      if (registerBreatheOne(agent)) log(`breathe-tool: 新 agent ${agent.id} 已挂 breathe`);
      return undefined;
    });
    ctx.effect(() => () => stopCreatedBreathe(), 'dsh-shuling-breath:agent-created-breathe');
    const breatheRoots = ctx.agents.roots();
    let attached = 0;
    for (const existing of breatheRoots) if (registerBreatheOne(existing)) attached += 1;
    log(`breathe-tool: 挂到 ${attached}/${breatheRoots.length} 个已存在的 root agent 上`);
  }

  log('plugin: scheduled');
}
