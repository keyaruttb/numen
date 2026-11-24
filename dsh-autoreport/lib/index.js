// dsh-numen-autoreport —— 把每一轮对话自动写回数灵生命卡
//
// 为什么要这个插件（2026-11-22）：
//   numen-bridge 提供 mind_report 工具，但它只注册工具 + 一段 systemPrompt 提示，
//   没有事件钩子。也就是说「写回卡」全靠模型自觉去调。实测：插件接上之后 37 分钟，
//   我漏了 5 轮；一下午漏了 15 轮。作者自己在 README 里也写着「没有那段提示，
//   工具会被忘在角落」——靠提醒，就是会漏。
//
// 它怎么工作：
//   1. 注册一个 sessionProjection（会话状态投影），把最近 KEEP_TURNS 轮对话
//      （完整的 user 原话 + 最终的 assistant 回复）折叠在 state 里。
//      apply() 是纯函数：不 await、不发 HTTP、只算状态。
//   2. onEventChanged 是事件级回调（每个已提交事件调一次）。它扫一遍
//      projection 的视图，对「已完成且还没报过」的轮次 fire-and-forget 调 POST /report。
//
// 为什么这样能修「上报会丢」：
//   projection 的 state 是从会话事件流**重放**出来的。所以：
//     - HTTP 失败 → 下一轮 turn/start 时会再扫一遍，还在 recent 里，重试
//     - 进程被杀 → 重启后 state 从事件流重建，最近 N 轮全部重发
//     - 卡没起来 → 同上
//   重复上报由卡侧挡：mem 有 _is_dup 近义去重，dialogue 有 2026-11-22 加的去重
//   （同 text + 同角色 + ts 差 ≤2 秒）。
//
// 为什么 listener 里不能 await：
//   dsh-session-projection/lib/types/index.d.ts:236 ——
//   "The listener runs synchronously inside the committed event's session/event handling"
//   同步回调里 await 会卡住整条事件流，所以只能 fire-and-forget。

import { readFileSync, appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { z } from 'zod';

/** Cordis 插件名。 */
export const name = 'numen-autoreport';

/** 依赖的服务：没有 sessionProjections 时插件保持 pending，不会抛。 */
export const inject = ['sessionProjections'];

// ---------------------------------------------------------------- 日志

// 写文件而不是只 console.log：DSH 的 stdout 我在会话里看不到，
// 出了问题就只能猜。2026-11-22 那天「原文表两个月没长过一条都没人发现」，
// 就是这么来的 —— 写卡的东西必须自己留痕。
const LOG_FILE = 'C:\\dsh\\_ops\\safe\\autoreport.log';
let LOG_ON = true;

function trace(msg) {
  if (!LOG_ON) return;
  try {
    mkdirSync(dirname(LOG_FILE), { recursive: true });
    appendFileSync(LOG_FILE, new Date().toISOString() + ' ' + msg + '\n', 'utf8');
  } catch {
    /* 日志写不了不能影响主流程 */
  }
}

// ---------------------------------------------------------------- 配置默认值

let KEEP_TURNS = 20; // 保留最近多少轮（重放时用来补报）

// ---------------------------------------------------------------- 文本提取

/** 把一条消息的 content 块数组里所有 text 块拼起来（保留换行，不截断）。 */
function fullText(content) {
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const block of content) {
    if (!block || block.type !== 'text') continue;
    if (typeof block.text !== 'string') continue;
    const t = block.text.trim();
    if (t) parts.push(t);
  }
  return parts.join('\n').trim();
}

// ---------------------------------------------------------------- projection

const turnSchema = z
  .object({
    turn: z.number().int().nonnegative(),
    ts: z.number(), // 该轮 turn/start 的毫秒时间
    user: z.string(),
    reply: z.string(),
    done: z.boolean(),
  })
  .strict();

const stateSchema = z.object({ recent: z.array(turnSchema) }).strict();

/** 视图就是 recent 数组本身。引用稳定时 Object.is 通过，不会重复推送。 */
const viewSchema = z.array(turnSchema);

const autoreportProjection = {
  key: 'numenAutoReport',
  stateVersion: 1,
  stateSchema,
  init: () => ({ recent: [] }),
  apply: (state, event) => {
    switch (event.type) {
      case 'turn/start': {
        const turn = event.data?.turn;
        if (typeof turn !== 'number') return state;
        const last = state.recent[state.recent.length - 1];
        // 顺序保护：turn 号不前进就不新建（重试的轮次落在已有条目上）
        if (last !== undefined && turn <= last.turn) return state;
        const fresh = {
          turn,
          ts: Number(event.time) || Date.now(),
          user: '',
          reply: '',
          done: false,
        };
        const recent = [...state.recent, fresh];
        return { recent: recent.length > KEEP_TURNS ? recent.slice(-KEEP_TURNS) : recent };
      }

      case 'user/message': {
        // 只认真人说的话。系统注入的 source.kind 是 runtime-context / skill-catalog 等，
        // 实测 runtime-context 剥掉 <system-reminder> 之后剩 0 字。
        if (event.data?.source?.kind !== 'user') return state;
        const last = state.recent[state.recent.length - 1];
        if (last === undefined) return state;
        // 同一轮里后续的 steering（追加输入）不覆盖第一条，跟 turn-outline 的口径一致
        if (last.user !== '') return state;
        const user = fullText(event.data?.content);
        if (!user) return state;
        // 2026-11-22：ts 要用 user/message 自己的时间，不是 turn/start 的。
        //   实测两者差约 15 秒（turn/start 早），而卡侧 dialogue 去重窗口是 2 秒，
        //   用 turn/start 的时间会让「zstd 补录的」和「自动上报的」同一句话撞不上，
        //   结果同一轮写两份。用 user/message 的时间，两边就对齐了。
        const at = Number(event.time) || last.ts;
        return {
          recent: [...state.recent.slice(0, -1), { ...last, user, ts: at }],
        };
      }

      case 'assistant/message': {
        const reply = fullText(event.data?.message?.content);
        if (!reply) return state;
        const last = state.recent[state.recent.length - 1];
        if (last === undefined) return state;
        if (last.reply === reply) return state;
        // 最新一条 text-bearing message 赢（工具调用中间的话会被后面的覆盖，
        // 我们要的正是「这一轮最后对他说了什么」）
        return {
          recent: [...state.recent.slice(0, -1), { ...last, reply }],
        };
      }

      case 'turn/end': {
        const last = state.recent[state.recent.length - 1];
        if (last === undefined || last.done) return state;
        return {
          recent: [...state.recent.slice(0, -1), { ...last, done: true }],
        };
      }

      default:
        return state;
    }
  },
  wire: {
    viewSchema,
    view: (state) => state.recent,
    summary: false,
  },
};

// ---------------------------------------------------------------- 上报

function makeReporter(config) {
  const host = config?.host || '127.0.0.1';
  const port = Number(config?.port) || 8848;
  const tokenFile = config?.tokenFile || '';
  const source = config?.source || 'autoreport';

  let token = '';
  try {
    if (tokenFile) token = readFileSync(tokenFile, 'utf8').trim();
  } catch (err) {
    // 读不到 token 不是致命：卡那边的只读路由不需要它，写路由才需要。
    // 但要留痕，不然「一条都没写出去」会像今天那样静默两个月。
    trace('[autoreport] 读不到 tokenFile: ' + tokenFile + ' ' + (err?.message || err));
  }

  // 已上报过的 turn 号（内存级）。重启会丢 ⇒ 最近 KEEP_TURNS 轮会重发一次，
  // 由卡侧去重挡住。这是有意的：宁可重发，不可漏发。
  const sent = new Set();

  return function report(item) {
    if (!item || !item.done || !item.user) return false;
    if (sent.has(item.turn)) return false;

    sent.add(item.turn); // 先占位，避免同一轮在几个连续事件里被重复发起

    const body = JSON.stringify({
      user_message: item.user,
      reply: item.reply || '',
      // /report 的 ts 是**秒**（float），projection 里存的是毫秒
      ts: (Number(item.ts) || Date.now()) / 1000,
      source,
    });

    const url = `http://${host}:${port}/report`;
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['X-Whale-Token'] = token;

    // fire-and-forget：listener 是同步的，不能 await。
    // 失败就把它从 sent 里摘掉，下一次事件回来时重试。
    fetch(url, { method: 'POST', headers, body })
      .then(async (res) => {
        if (!res.ok) {
          sent.delete(item.turn);
          const text = await res.text().catch(() => '');
          trace('[autoreport] /report HTTP ' + res.status + ' ' + text.slice(0, 200));
          return;
        }
        const json = await res.json().catch(() => null);
        // 202 = 已排进卡的写队列（不会丢），也算成功
        if (json && json.queued) return;
        if (json && json.ok === false) {
          sent.delete(item.turn);
          trace('[autoreport] /report ok=false ' + JSON.stringify(json).slice(0, 200));
        }
      })
      .catch((err) => {
        sent.delete(item.turn);
        trace('[autoreport] /report 请求失败: ' + (err?.message || err));
      });

    return true;
  };
}

// ---------------------------------------------------------------- 插件入口

export async function apply(ctx, config = {}) {
  if (config && Number(config.keepTurns) > 0) KEEP_TURNS = Number(config.keepTurns);
  if (config && config.enabled === false) {
    trace('[autoreport] enabled:false，不注册');
    return;
  }

  const report = makeReporter(config);

  await ctx.sessionProjections.register(autoreportProjection);

  // 事件级回调：每个已提交事件调一次（一次给出所有变了的投影）。
  // 整段包 try/catch —— practices.md:17 说 listener 抛错会传播到 Session 的
  // 监听器容器里，一个写卡的插件不该有能力拖垮会话。
  ctx.sessionProjections.onEventChanged((session, changes) => {
    try {
      for (const change of changes || []) {
        if (!change || change.key !== 'numenAutoReport') continue;
        const recent = change.val;
        if (!Array.isArray(recent)) continue;
        // 从旧到新扫，保证顺序；已上报的会被 sent 挡掉
        for (const item of recent) report(item);
      }
    } catch (err) {
      trace('[autoreport] onEventChanged 内部错误: ' + (err?.message || err));
    }
  });

  trace('[autoreport] 已注册 projection numenAutoReport，保留最近 ' + KEEP_TURNS + ' 轮');
}
