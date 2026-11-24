// 2026-11-15 可移植化：路径从 ../home.mjs 取（本文件原本没有 import 区，所以插在文件头）
import path from 'node:path';
import { OPS_DIR, XIYANG_ROOT, STORAGES_DIR } from '../home.mjs';
/**
 * dsh-shuling-breath —— 配置归一化。
 *
 * 这是**纯模块**：不 import 任何 dsh 宿主包（dsh-tools / dsh-llm 那一批），所以 `node` 可以在宿主之外
 * 直接 import 它跑验收脚本（见 C:\dsh\_ops\safe\verify-*.mjs）。
 *
 * 配置来源优先级：cordis.patch.yml 里这一行的 `config:` > DEFAULT_CONFIG。
 * 不做 depth-merge：数组整体替换（哨兵清单被显式给出时就用给的）。
 */

const OPS = OPS_DIR;
const XI = XIYANG_ROOT;
const COST_METER = path.join(STORAGES_DIR, 'cost-meter');
const SESSION_CACHE = path.join(STORAGES_DIR, 'session_projcache', 'sessions');

export const DEFAULT_CONFIG = Object.freeze({
  /** 总开关。false 时插件装配但一轮都不跑。 */
  enabled: true,
  /** Tier 0 轮询间隔（秒）。硬下限 15 秒，防止手滑写成 0 把 CPU 打满。 */
  pollIntervalSeconds: 300,
  /** 启动后第一次轮询的延迟（秒），错开宿主启动高峰。 */
  firstPollDelaySeconds: 20,
  /** 唤醒目标会话。空串 = 自动挑"最后一个 root agent"，并在日志里写明是猜的。 */
  sessionId: '',
  /** 每小时最多唤醒几次（防抖：改了好几个文件也只叫醒有限次）。 */
  maxWakesPerHour: 4,
  /** 时区（cron 与状态行的本地时间口径）。 */
  timeZone: 'Asia/Shanghai',
  /**
   * recall-state.json 多久没更新算"落盘链断了"（分钟）。
   * 2026-11-14 天授权：1440 → 180（原话「按你的建议改吧…建议降到 120–180」）。
   *   从哪来：2026-11-14 那次断链 18 小时没人发现，根因就是这个阈值是 24 小时
   *           —— 断了整整一天才判 stale，18 小时落在阈值内 ⇒ stale:false ⇒ 没有一跳被叫醒。
   *   为什么不是别的数：实测 20.8 小时里 65 次唤醒（≈75 次/天），相邻两次唤醒最长间隔 46.4 分钟
   *           —— 唤醒只是 recall-state 写入的子集，所以真实停更上界 ≤ 46.4 分钟；
   *           180 / 46.4 ≈ 3.9 倍余量，不误报；180 分 ≪ 18 小时，18 小时级病灶照样抓得住。
   *           天给的范围是 120–180，取上限最保守（最不容易误报）。
   *   余量：≈3.9 倍（按实测最坏间隔 46.4 分钟算）。
   *   ⚠️ cordis.patch.yml 的 config: 里也有一份同名键，优先级高于这里 —— 只改这里等于没改。
   */
  staleAfterMinutes: 180,
  /** 日志与状态文件。只写这两个文件，不碰会话、不碰 .db。 */
  logFile: `${OPS}\\shuling-breath.log`,
  stateFile: `${OPS}\\shuling-breath.state.json`,
  /** 静默工具。唤醒回合里"没事"就用它一键结束，不产生 assistant 文本。 */
  silentTool: true,

  // ---- 三道门（2026-11-13 23:28 天令）----
  /** 门 1：cost-meter 的账本。**只读，绝不写**（写了会把它的账算错）。 */
  ledgerFile: `${COST_METER}\\ledger.json`,
  /** 门 1 的一部分：余额低于这个数（CNY）⇒ 连保养也不做，只记录。 */
  balanceFloorCny: 10,
  /** 门 2：任务门读的两份。留空则自动取 sentinels 里同 kind 的 path。 */
  unfinishedFile: '',
  queueFile: '',
  /** 门 3：会话投影缓存目录（kind:'session' 哨兵的 path 留空时用它 + sessionId 拼）。 */
  sessionCacheDir: SESSION_CACHE,
  /** 门 3：会话 json 的 mtime 静默超过这么多分钟 ⇒ change='idle'。 */
  idleWakeAfterMinutes: 20,
  /** 门 3 防死循环：唤醒后这么多分钟内一律不判 liveness。 */
  idleCooldownMinutes: 20,
  /** 凌晨计划模板（`{yyyymmdd}` 会被替换）；算下一件用。 */
  planFileTemplate: `${XI}\\事\\凌晨计划-{yyyymmdd}.md`,
  /** 真值台账：唤醒 prompt 里要求"做完把真值追加进这里"。 */
  progressLedgerFile: `${XI}\\事\\进度台账.md`,

  /**
   * 哨兵清单。`required: false` 的文件不存在时静默跳过，不报错、不算变化。
   * kind: queue | unfinished | recall | session | generic
   *
   * 🔧 2026-11-23 移植改动：原版有五条，全部指向息壤。**息壤今天上午关闸了**，实测（2026-11-23 17:0x）：
   *      queue       → 息壤\事\队列.json           最后写入 11-11 21:46（283 小时前）
   *      unfinished  → 息壤\事\未完成.json         最后写入 11-17 22:12（139 小时前）
   *      recall      → 息壤\记忆\state\recall-state.json  最后写入 11-22 15:26（25.8 小时前，而 stale 阈值是 3 小时）
   *      unread      → 息壤-未读.json              （同族，也不再更新）
   *    前两条「永远没变」⇒ 永远不触发；第三条「永远是 stale」⇒ 每跳误报「落盘链断了」。
   *    ⇒ 阶段 0 只留 **session**（会话投影 json 的 mtime）——这是唯一还活着的信号，
   *      也是「该不该醒」最直接的依据（沉默太久就该醒）。
   *    ⇒ 另外三条不删定义，只是默认不要它们；想启用就在 cordis.patch.yml 的 sentinels 里显式写回来。
   *      详见 C:\dsh\_溯\数灵之息-移植计划-20261123.md 第二节。
   */
  sentinels: Object.freeze([
    // 原版那四条（留作备查，默认不用）：
    //   { id: 'queue',      kind: 'queue',      required: true,  path: `${XI}\\事\\队列.json` },
    //   { id: 'unfinished', kind: 'unfinished', required: true,  path: `${XI}\\事\\未完成.json` },
    //   { id: 'recall',     kind: 'recall',     required: true,  path: `${XI}\\记忆\\state\\recall-state.json` },
    //   { id: 'unread',     kind: 'generic',    required: false, path: `${XI}\\息壤-未读.json` },
    // 第五类：关于"我自己"。path 留空 ⇒ resolveConfig 用 sessionCacheDir + sessionId 拼。
    Object.freeze({ id: 'session', kind: 'session', required: false, path: '' })
  ]),
  /** 到点提醒：五段 cron（分 时 日 月 周）+ 到点时带的提示语。 */
  crons: Object.freeze([])
});

const KINDS = new Set(['queue', 'unfinished', 'recall', 'session', 'generic']);

function posInt(value, fallback, min) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < min) return fallback;
  return Math.floor(n);
}

function posNum(value, fallback, min) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < min) return fallback;
  return n;
}

function str(value, fallback) {
  return typeof value === 'string' && value.trim() !== '' ? value : fallback;
}

/**
 * 把 raw（patch 行的 config）归一化成完整配置对象。任何非法值都退回默认，
 * 绝不抛 —— 插件装配失败比配置写错严重得多。
 */
export function resolveConfig(raw) {
  const src = raw !== null && typeof raw === 'object' ? raw : {};
  const defaults = DEFAULT_CONFIG;

  const sessionId = typeof src.sessionId === 'string' ? src.sessionId.trim() : '';
  const sessionCacheDir = str(src.sessionCacheDir, defaults.sessionCacheDir);

  const rawSentinels = Array.isArray(src.sentinels) && src.sentinels.length > 0 ? src.sentinels : defaults.sentinels;
  const sentinels = rawSentinels
    .filter((s) => s !== null && typeof s === 'object')
    .map((s, i) => {
      const kind = KINDS.has(s.kind) ? s.kind : 'generic';
      // kind:'session' 的 path 允许留空：用 sessionCacheDir + 当前 sessionId 拼出来，
      // 这样改 sessionId 一处就够，不用再同步改哨兵 path。
      const declared = typeof s.path === 'string' ? s.path.trim() : '';
      const path = declared !== ''
        ? s.path
        : kind === 'session' && sessionId !== ''
          ? `${sessionCacheDir}\\${sessionId}.json`
          : '';
      return { id: str(s.id, `sentinel-${i + 1}`), kind, required: s.required !== false, path };
    })
    .filter((s) => s.path !== '');

  const crons = Array.isArray(src.crons)
    ? src.crons
        .filter((c) => c !== null && typeof c === 'object' && typeof c.expr === 'string' && c.expr.trim() !== '')
        .map((c) => ({
          expr: c.expr.trim(),
          prompt: str(c.prompt, '到点了，看一眼该做什么。'),
          timeZone: str(c.timeZone, str(src.timeZone, defaults.timeZone))
        }))
    : [];

  // 任务门的两份：没单配就取 sentinels 里同 kind 的 path（只写一处，少一处漂移）。
  const byKind = (kind) => sentinels.find((s) => s.kind === kind)?.path ?? '';
  const unfinishedFile = str(src.unfinishedFile, byKind('unfinished') || `${XI}\\事\\未完成.json`);
  const queueFile = str(src.queueFile, byKind('queue') || `${XI}\\事\\队列.json`);

  return {
    enabled: src.enabled !== false,
    pollIntervalSeconds: posInt(src.pollIntervalSeconds, defaults.pollIntervalSeconds, 15),
    firstPollDelaySeconds: posInt(src.firstPollDelaySeconds, defaults.firstPollDelaySeconds, 0),
    sessionId,
    maxWakesPerHour: posInt(src.maxWakesPerHour, defaults.maxWakesPerHour, 1),
    timeZone: str(src.timeZone, defaults.timeZone),
    staleAfterMinutes: posInt(src.staleAfterMinutes, defaults.staleAfterMinutes, 1),
    logFile: str(src.logFile, defaults.logFile),
    stateFile: str(src.stateFile, defaults.stateFile),
    silentTool: src.silentTool !== false,
    ledgerFile: str(src.ledgerFile, defaults.ledgerFile),
    balanceFloorCny: posNum(src.balanceFloorCny, defaults.balanceFloorCny, 0),
    unfinishedFile,
    queueFile,
    sessionCacheDir,
    idleWakeAfterMinutes: posInt(src.idleWakeAfterMinutes, defaults.idleWakeAfterMinutes, 1),
    idleCooldownMinutes: posInt(src.idleCooldownMinutes, defaults.idleCooldownMinutes, 1),
    planFileTemplate: str(src.planFileTemplate, defaults.planFileTemplate),
    progressLedgerFile: str(src.progressLedgerFile, defaults.progressLedgerFile),
    sentinels,
    crons
  };
}
