/**
 * 息壤 · 呼吸 —— 唤醒消息的构造（自包内实现，零宿主包依赖）
 *
 * 为什么这个文件要自己写一份：原来它是 `import { boundContextSummary, createUserMessage }
 * from '<scope>/dsh-llm'`。插件被搬进 xiyang 的二级目录后，ESM 路由按"包的根"注册
 * 宿主包的 scope 前缀，二级目录里解析不到 ⇒ 整条插件 `did not activate`，呼吸静默停摆。
 * 所以这里照 dsh-llm 的实现**逐条对抄**一份，并留下出处行号（将来能指回去对）。
 *
 * 出处（2026-11-15 读的是这个文件）：
 *   <dsh 安装目录>\node_modules\<scope>\dsh\node_modules\<scope>\dsh-llm\lib\index.js
 *   （此机上 <dsh 安装目录> = %APPDATA%\npm\node_modules，<scope> = 带 @ 的那个作用域名）
 *
 * 抄了什么、原值是什么（Lead 要拿这个跟 dsh-llm 对一遍）：
 *   ① `CONTEXT_SUMMARY_MAX_CHARS = 120`                                  — 原文件 L15
 *   ② `boundContextSummary(summary)`                                      — 原文件 L21-23
 *        返回 `summary.length <= 120 ? summary : summary.slice(0,119) + '…'`
 *        （注意：是 119 个字符 + 一个省略号 = 120 上限，不是 120+1）
 *   ③ `createMessage(input)`                                              — 原文件 L37-42
 *        `deepFreeze(structuredClone({ ...input, id: brandString(randomUUID()) }))`
 *        · `randomUUID` 来自 `dsh-util-crypto`（内部就是 node:crypto 的 randomUUID）
 *        · `brandString` 来自 `dsh-brand`：运行时是**恒等函数**，只做类型标记
 *        · `deepFreeze` 来自 `dsh-util-values`：递归 Object.freeze
 *        ⇒ 三者都不带语义，这里用 node:crypto / 恒等函数 / 本地 deepFreeze 等价替换
 *   ④ `createUserMessage(input)` = `createMessage({ ...input, role: 'user' })`  — 原文件 L59-64
 *
 * 消息形态（这一条是"唤醒不冒充用户说话"的落点，别改）：
 *   `source = { kind: <插件 id>, form: 'notice', summary: boundContextSummary(summary) }`
 *   GUI 见到 `form: 'notice'` 就渲染成一条克制的提示条 —— 不是用户气泡，也不是 assistant 正文。
 *   与 dsh-proactive 用的是同一条契约（照抄，不是巧合）。
 */

import { randomUUID } from 'node:crypto';

/**
 * 沿用旧插件的 id，**有意不改**：
 * 影子跑期间新旧两条链路同时在写，durable log 里同一个 kind 便于直接对比；
 * 等旧的停掉、新的定名，再一起改（改 kind 会让历史记录断成两代，那是另一个决定）。
 */
export const SOURCE_KIND = 'plugin:dsh-shuling-breath';

/** 对应 dsh-llm L15。 */
const CONTEXT_SUMMARY_MAX_CHARS = 120;

/** 对应 dsh-llm L21-23（逐字照抄）。 */
export function boundContextSummary(summary) {
  const text = String(summary);
  return text.length <= CONTEXT_SUMMARY_MAX_CHARS ? text : `${text.slice(0, CONTEXT_SUMMARY_MAX_CHARS - 1)}…`;
}

/**
 * 对应 dsh-util-values 的 deepFreeze：深拷贝后冻结整棵值树。
 * 只处理 JSON 值（对象 / 数组 / 原始值）—— 消息体一定是这个范围。
 */
function deepFreeze(value) {
  if (value === null || typeof value !== 'object') return value;
  if (Object.isFrozen(value)) return value;
  for (const key of Object.keys(value)) deepFreeze(value[key]);
  return Object.freeze(value);
}

/** 对应 dsh-llm L37-42 的 createMessage（id = randomUUID，brandString 运行时为恒等）。 */
function createMessage(input) {
  return deepFreeze(structuredClone({ ...input, id: randomUUID() }));
}

/** 对应 dsh-llm L59-64。 */
export function createUserMessage(input) {
  return createMessage({ ...input, role: 'user' });
}

/**
 * @param {string} text    完整提示词（poll 里拼好的、带状态行增量的那段）
 * @param {string} summary 一行摘要，GUI 折叠时显示
 */
export function createWakeMessage(text, summary) {
  return createUserMessage({
    content: [{ type: 'text', text }],
    source: {
      kind: SOURCE_KIND,
      form: 'notice',
      summary: boundContextSummary(summary)
    }
  });
}
