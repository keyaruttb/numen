/**
 * dsh-shuling-breath —— Tier 1：真唤醒的投递层。
 *
 * **纯模块**：不 import 任何 dsh 宿主包（dsh-tools / dsh-llm 那一批）。agents 是通过参数传进来的，
 * 所以 C:\dsh\_ops\safe\verify-tier1.mjs 能用桩 agents 把这条路径整段跑一遍，
 * 并断言 `resume` 到底被调没被调。
 *
 * 投递机制照 dsh-proactive 的做法（不是抄代码，是同一条契约）：
 *   1) 会话活着 ⇒ 复用 live agent；冷会话 ⇒ ctx.agents.resume 拿 handle
 *   2) agent.runMaintenance(() => agent.followup(message)) 抢占空闲相位
 *   3) 抢不到（正在跑一轮）⇒ outcome 'busy'，不排队、不硬塞
 *   4) agent.whenIdle() 等这一轮落定
 *   5) 自己 resume 出来的 handle 一定 dispose（会话本身留在盘上）
 */

/**
 * @param {object} deps
 * @param {object} deps.agents        ctx.agents（get / resume）
 * @param {string} deps.sessionId     目标会话 id
 * @param {object} deps.message       UserMessage（由 message.js 用 createUserMessage 造）
 * @param {Function} deps.log         (line) => void
 * @param {object} [deps.agentOptions] 冷 resume 时用的 {provider, model}
 * @param {Function} [deps.setup]     冷 resume 的 setup 回调
 * @param {object} [deps.silentMark]  静默标记（工具写、这里读）
 */
export async function deliverWake(deps) {
  const { agents, sessionId, message, log } = deps;
  if (typeof sessionId !== 'string' || sessionId === '') {
    log('wake.skipped: no target session id');
    return { outcome: 'no-target' };
  }

  const live = agents.get(sessionId);
  if (live !== undefined && live !== null) {
    if (live.status !== undefined && live.status !== 'idle') {
      log(`wake.skipped: agent ${sessionId} is ${live.status} (busy), not queueing`);
      return { outcome: 'busy', presence: 'live', sessionId };
    }
    log(`wake.reuse-live session=${sessionId}`);
    return await driveAgent({ agent: live, owned: null, ...deps });
  }

  log(`wake.resume session=${sessionId}`);
  let handle;
  try {
    // ⚠ 2026-11-23 移植：这里**保持原版的单参数写法**，不要改成两参数。
    //    当前 DSH 的签名是 `resume(ownerCtx, options)`（dsh-agent/index.d.ts:185），
    //    多了一个 ownerCtx。但实测（verify-tier1.mjs）：把 ownerCtx 直接塞进第一个位置，
    //    单参数的桩/旧调用方会把它当 options 收下 → `Cannot read properties of undefined (reading 'resumeSessionId')`。
    //    JS 不「忽略多传的参数」，它按位置匹配 —— 我原以为多传会被忽略，是错的。
    //    ⇒ 适配层放在调用方（index.js 传一个已封好的 resume），这个纯模块保持原版契约。
    handle = await agents.resume({
      resumeSessionId: sessionId,
      ...(deps.agentOptions !== undefined ? { agentOptions: deps.agentOptions } : {}),
      ...(deps.setup !== undefined ? { setup: deps.setup } : {})
    });
  } catch (err) {
    log(`wake.resume: FAILED session=${sessionId} — ${String(err && err.message ? err.message : err)}`);
    return { outcome: 'failed', error: String(err && err.message ? err.message : err), sessionId };
  }
  log(`wake.resume: ok session=${sessionId} (cold handle acquired)`);

  try {
    return await driveAgent({ agent: handle.agent, owned: handle, ...deps });
  } finally {
    try {
      await handle.dispose();
      log(`wake.resume: handle disposed session=${sessionId}`);
    } catch (err) {
      log(`wake.resume: dispose failed session=${sessionId} — ${String(err && err.message ? err.message : err)}`);
    }
  }
}

async function driveAgent(deps) {
  const { agent, owned, message, log, sessionId } = deps;
  const presence = owned === null ? 'live' : 'cold';

  const markStart = Date.now();
  let claimed = false;
  try {
    claimed = await agent.runMaintenance(() => {
      agent.followup(message);
      return Promise.resolve(true);
    });
  } catch (err) {
    log(`wake.followup: busy — ${String(err && err.message ? err.message : err)}`);
    claimed = false;
  }
  if (!claimed) return { outcome: 'busy', presence, sessionId };

  await agent.whenIdle();

  const silent = deps.silentMark !== undefined && deps.silentMark !== null && deps.silentMark.at >= markStart;
  log(`wake.turn: settled session=${sessionId} presence=${presence} silent=${silent ? 'yes' : 'no'}`);
  return { outcome: 'ok', presence, sessionId, silent };
}
