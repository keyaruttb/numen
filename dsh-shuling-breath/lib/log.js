/**
 * dsh-shuling-breath —— 日志与状态落盘（纯模块）。
 *
 * 两条硬规矩：
 *   ① 只写 config.logFile / config.stateFile 两个文件，绝不写会话、绝不碰 .db。
 *   ② 写日志失败也绝不抛 —— 日志写不进去不该把轮询打断。
 */

import { appendFileSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export const STATE_SCHEMA = 'dsh-shuling-breath.state.v1';

function ensureDir(file) {
  try {
    mkdirSync(dirname(file), { recursive: true });
  } catch {
    /* 目录已存在 / 无权限：交给后面的写操作暴露 */
  }
}

/** 追加一行日志（UTC ISO 时间戳 + 文本）。 */
export function appendLog(file, line) {
  try {
    ensureDir(file);
    appendFileSync(file, `${new Date().toISOString()} ${line}\n`, 'utf8');
  } catch {
    /* 静默：日志不是主流程 */
  }
}

/** 读状态；文件缺失、损坏、版本不对统统从空状态开始（不抛）。 */
export function loadState(file) {
  // 注意：自我调度（breathe）**不放这里**。它存独立文件 xiyang-breath.selfschedule.json ——
  // poller 每跳都 saveState(state) 用内存值整体覆盖磁盘，放这里的话工具刚写进去的下一跳就被冲掉。（2026-11-17）
  const empty = { schema: STATE_SCHEMA, polls: 0, startedAt: null, lastPollAt: null, lastWakeAt: null, wakes: 0, wakeTimes: [], sentinels: {}, crons: {}, lastDelta: null };
  let text;
  try {
    text = readFileSync(file, 'utf8');
  } catch {
    return empty;
  }
  try {
    const parsed = JSON.parse(text);
    if (parsed === null || typeof parsed !== 'object' || parsed.schema !== STATE_SCHEMA) return empty;
    return {
      ...empty,
      ...parsed,
      sentinels: parsed.sentinels !== null && typeof parsed.sentinels === 'object' ? parsed.sentinels : {},
      crons: parsed.crons !== null && typeof parsed.crons === 'object' ? parsed.crons : {},
      wakeTimes: Array.isArray(parsed.wakeTimes) ? parsed.wakeTimes : []
    };
  } catch {
    return empty;
  }
}

/** 原子写：先写 .tmp 再 rename，避免半截 JSON。 */
export function saveState(file, state) {
  try {
    ensureDir(file);
    const tmp = `${file}.tmp`;
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
    renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}
