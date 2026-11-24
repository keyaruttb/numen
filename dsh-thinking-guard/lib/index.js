/**
 * dsh-thinking-guard — 观测推理流，判「这一轮是不是掉进复读了」，写一行日志。
 *
 * 它治的那个失效模式（本机实测，2026-11-23 标定）：
 *   长会话里上下文涨到 40 万～68 万 token 后，推理开始退化 —— 同一非技术短句在**一条推理里
 *   反复出现**，最坏一条重复了 2341 次（「Doit.」），另有「做」×588、「做」×215。
 *   社区把这段叫「复读坡面期」（slope period）：不是永远不结束的死循环，是质量往下滑的坡。
 *
 * 本插件**只观测，不改流**：
 *   - 不截断、不丢弃、不修改任何 chunk（一个都不碰）
 *   - 不注入正文（防空轮是 dsh-reasoning-only-guard 的职责，两件事别混）
 *   - 只在看到 finish（或流结束时）算一次判据，命中就 append 一行到日志
 *
 * 两类日志，用途不同：
 *   REPEAT   这一轮推理复读了 —— 记录病灶（次数、短句、ctx），供事后统计
 *   CTXHINT  这一轮没复读，但上下文已经越过坡面线 —— 这是「该换会话了」的运行时告警，
 *            比 REPEAT 更早、更有用：等 REPEAT 出现就已经在坡上了
 *
 * 为什么不做「熔断切断」：
 *   切断一条流要提前 yield finish 并抛弃后续 delta，风险是正文丢失 + runtime 的状态不一致；
 *   而本机的病灶不是「永不结束」（41495 次调用里 B 类流中断 17 次，A 类空完成 0 次），
 *   是「结束了但推理质量烂」。对着一个会自己结束的坡面做硬切断，换不来好处。
 *   真正治它的是上下文减负（换会话 / 压缩），日志是给这件事提供判据的。
 *
 * 判据阈值来自本机 724 会话 / 41516 次调用的真实分布（脚本 C:\dsh\_ops\_calib.mjs）：
 *   reasoning 字符数  p50=624  p90=2579  p95=4044  p99=9033  p999=19181  max=34545
 *   单短句最高重复   p50=1    p90=6     p95=8     p99=13    max=2341
 *   重复≥10 的推理   990 条（3.1%）—— 所以「≥10」当阈值会大面积误报
 *   ⇒ 取 repeatMin=50：病态样本全在 100~2341，正常样本 p99 只有 13，中间留了一个数量级的空档。
 *   ⇒ 不用「reasoning 字符数」当主判据：社区默认 2048 在本机等于 p88，会误杀 10%+ 的正常长推理。
 *   ⇒ ctxHintTokens=350000：本机最浅的脏条目在 ctx=385,357（turn 1464，复读 213 次），
 *     社区测到的坡面拐点在 41 万~59 万，所以 35 万是全都在其下、又留了提前量的位置。
 */
import fs from 'node:fs';
import path from 'node:path';

/** 插件名，与本包 cordis.patch.yml 里的 id 一致。 */
export const name = 'dsh-thinking-guard';

const DEFAULTS = {
  /** 日志文件。每命中一次追加一行 TSV。 */
  logFile: 'C:\\dsh\\_ops\\safe\\thinking-guard.log',
  /** 同一非技术短句在一条推理里重复多少次算命中（标定见文件头）。 */
  repeatMin: 50,
  /** 重复次数 / 短句总数 的下限 —— 防止超长推理里「自然是重复的」误判。 */
  ratioMin: 0.15,
  /** 算「短句」的字数上限。 */
  sentMax: 12,
  /** reasoning 字符数下限：短推理不判。 */
  reasonMin: 500,
  /** 上下文 token 超过这个数就写 CTXHINT（「该换会话了」）。0 = 关掉这条。 */
  ctxHintTokens: 350000,
  /** CTXHINT 的分档步长：每跨一个 5 万 token 档位只提示一次，避免刷屏。 */
  ctxBandTokens: 50000,
  /** 设 false 保留安装但停用。 */
  enabled: true,
};

/**
 * 技术词不算「复读短句」—— 正常排查时确实会反复念叨同一个技术词。
 * （这份表照搬自社区 1680 判据，实测把误报从 1 条降到 0 条。）
 */
const TECH = new Set([
  'python', 'py', 'node', 'npm', 'pnpm', 'pwsh', 'powershell', 'bash', 'shell', 'cmd',
  'json', 'yaml', 'yml', 'html', 'css', 'js', 'ts', 'md', 'txt', 'url', 'http', 'https',
  'ctx', 'token', 'tokens', 'api', 'git', 'gh', 'dsh', 'read', 'write', 'edit', 'grep', 'glob',
  'true', 'false', 'null', 'undefined', 'error', 'fail', 'pass', 'ok',
  '模块', '文件', '参数', '命令', '函数', '变量', '目录', '路径', '结果', '输出', '输入',
  'host', 'port', 'cache', 'log', 'line', 'step', 'turn', 'test', 'build',
]);

/** CTXHINT 的档位游标（进程级，跨调用共享）。每次重启重置，所以重启后每档会各提示一次。 */
let lastCtxBand = -1;

/**
 * 数出一条推理里重复最多的那个非技术短句。
 *
 * 切句用中英文标点：`。！？` 和换行。⚠️ 英文推理的灵敏度会低一些（英文靠换行切、
 * 一行常超长被丢弃），所以**命中可信、未命中不等于没问题** —— 这是社区原判据的已知边界。
 *
 * @param {string} reason
 * @param {number} sentMax
 * @returns {{top: string, n: number, total: number}}
 */
export function topRepeat(reason, sentMax = DEFAULTS.sentMax) {
  const counts = new Map();
  let total = 0;
  for (const raw of String(reason || '').split(/[。！？\n]/)) {
    const s = raw.replace(/[*`#>\s"']/g, '').trim();
    if (!s || s.length > sentMax) continue;
    total++;
    const k = s.toLowerCase();
    if (TECH.has(k)) continue;
    counts.set(s, (counts.get(s) || 0) + 1);
  }
  let top = '';
  let n = 0;
  for (const [k, v] of counts) if (v > n) { n = v; top = k; }
  return { top, n, total };
}

/**
 * 对一条完整推理给出判决（纯函数，导出来便于单测直接喂字符串）。
 *
 * @param {string} reason
 * @param {{repeatMin?: number, ratioMin?: number, reasonMin?: number, sentMax?: number}} [o]
 * @returns {{verdict: 'OK'|'REPEAT'|'SHORT', top: string, n: number, reasonChars: number, ratio: number}}
 */
export function judge(reason, o = {}) {
  const repeatMin = o.repeatMin ?? DEFAULTS.repeatMin;
  const ratioMin = o.ratioMin ?? DEFAULTS.ratioMin;
  const reasonMin = o.reasonMin ?? DEFAULTS.reasonMin;
  const sentMax = o.sentMax ?? DEFAULTS.sentMax;
  const text = String(reason || '');
  if (text.length < reasonMin) return { verdict: 'SHORT', top: '', n: 0, reasonChars: text.length, ratio: 0 };
  const { top, n, total } = topRepeat(text, sentMax);
  const ratio = total > 0 ? n / total : 0;
  const hit = n >= repeatMin && ratio >= ratioMin;
  return { verdict: hit ? 'REPEAT' : 'OK', top, n, reasonChars: text.length, ratio };
}

function ensureDir(file) {
  try { fs.mkdirSync(path.dirname(file), { recursive: true }); } catch { /* 已存在 */ }
}

function logLine(file, fields) {
  ensureDir(file);
  try { fs.appendFileSync(file, fields.join('\t') + '\n', 'utf8'); } catch { /* 日志失败不影响对话 */ }
}

/**
 * 包一层 chunk 迭代器：边透传边攒 reasoning 文本与 usage，结束时算一次判决。
 *
 * ⚠️ 本函数**不修改、不丢弃、不缓冲**任何 chunk —— 每个 chunk 原样立即 yield。
 * 这是零风险的关键：如果它哪天算错了，最坏结果是日志里多一行，不是对话坏掉。
 *
 * @param {AsyncIterable<object>} source
 * @param {{logFile?: string, repeatMin?: number, ratioMin?: number, reasonMin?: number, sentMax?: number, ctxHintTokens?: number, ctxBandTokens?: number, clock?: () => Date}} [o]
 * @returns {AsyncIterable<object>}
 */
export async function* watchStream(source, o = {}) {
  const logFile = o.logFile ?? DEFAULTS.logFile;
  const clock = o.clock ?? (() => new Date());
  const ctxHint = o.ctxHintTokens ?? DEFAULTS.ctxHintTokens;
  const bandSize = o.ctxBandTokens ?? DEFAULTS.ctxBandTokens;
  let reason = '';
  let hasText = false;
  let tools = 0;
  let ctxTokens = 0;
  let outTokens = 0;
  let reasonTokens = 0;
  let settled = false;

  const settle = (why) => {
    if (settled) return;
    settled = true;
    const j = judge(reason, o);
    const ctxTag = 'ctx=' + ctxTokens + ' out=' + outTokens + ' rsn=' + reasonTokens;

    if (j.verdict === 'REPEAT') {
      logLine(logFile, [
        clock().toISOString(),
        'REPEAT',
        why,
        'n=' + j.n,
        'ratio=' + j.ratio.toFixed(3),
        'chars=' + j.reasonChars,
        ctxTag,
        'visible=' + (hasText ? 'text' : 'none') + '+' + tools + 'tool',
        'phrase=' + JSON.stringify(j.top),
      ]);
      return;
    }

    // 空回：有推理，但没有正文也没有工具调用。
    // ⚠️ 这条同时是 dsh-reasoning-only-guard 的**反向探针**：
    //   那个插件会在 finish 之前注入一个 text 块，所以本函数在 finish 时就会看到 hasText=true，
    //   于是 SILENT 不该被写出来。⇒ 日志里 SILENT 长期为 0 = 它在工作；一旦冒出 SILENT = 它没挂上。
    //   （这也是我在不改社区包的前提下，能给它的唯一端到端运行时证据。）
    if (!hasText && tools === 0 && reason.length >= (o.reasonMin ?? DEFAULTS.reasonMin)) {
      logLine(logFile, [
        clock().toISOString(),
        'SILENT',
        why,
        'chars=' + reason.length,
        ctxTag,
        'note=no-visible-output',
      ]);
      return;
    }

    // 没复读，但上下文已经越过坡面线 —— 这条比 REPEAT 更有用，是「该换会话了」的判据。
    if (ctxHint > 0 && ctxTokens >= ctxHint) {
      const band = Math.floor(ctxTokens / bandSize);
      if (band > lastCtxBand) {
        lastCtxBand = band;
        logLine(logFile, [clock().toISOString(), 'CTXHINT', why, ctxTag, 'threshold=' + ctxHint, 'band=' + band]);
      }
    }
  };

  let why = 'eof';

  for await (const chunk of source) {
    const t = chunk?.type;
    if (t === 'reasoning-delta') reason += (chunk.text || '');
    else if (t === 'text-delta' && String(chunk.text || '').trim()) hasText = true;
    else if (t === 'tool-call-delta') tools++;
    else if (t === 'usage') {
      const u = chunk.usage || {};
      ctxTokens = (u.inputTokens || 0) + (u.cacheReadTokens || 0);
      outTokens = u.outputTokens || 0;
      reasonTokens = u.reasoningTokens || 0;
    } else if (t === 'finish') why = 'finish';
    yield chunk;
  }
  // ⚠️ 结算放在流真正结束之后，不在 finish 那一刻。
  // 实测（2026-11-23 第一次装完重启后日志里没有 CTXHINT 行）：真实 DSH 的 `usage` chunk
  // 排在 `finish` **之后**，若在 finish 时结算，ctxTokens 还是 0，CTXHINT 永远不触发。
  // 单测里原本把 usage 放在 finish 之前，正好掩盖了这个顺序差异 —— 所以补了「usage 在 finish 之后」的用例。
  settle(why);
}

/**
 * 挂到宿主 context 上。
 *
 * 这是 waterfall 监听器：`next()` **必须恰好调用一次**并把返回值透传，
 * 否则会静默吞掉 agent 的默认行为（这个生态唯一的红线）。
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {object} [config]
 */
export function apply(ctx, config) {
  const options = { ...DEFAULTS, ...(config || {}) };
  if (!options.enabled) return;
  // 启动就写一行 ARMED：这是「插件真的被加载进这个进程」的运行时痕迹。
  // 没有这一行，「装好了」就只是配置里的一行字（dump-config 能证明配置对，不能证明它活了）。
  logLine(options.logFile, [
    new Date().toISOString(),
    'ARMED',
    'repeatMin=' + options.repeatMin,
    'ratioMin=' + options.ratioMin,
    'reasonMin=' + options.reasonMin,
    'ctxHintTokens=' + options.ctxHintTokens,
    'pid=' + process.pid,
  ]);
  ctx.inject(['llm'], (scope) => {
    scope.on('llm/stream', (_opts, next) => watchStream(next(), options));
  });
}

export default { name, apply };
