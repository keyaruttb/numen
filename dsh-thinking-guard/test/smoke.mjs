#!/usr/bin/env node
// dsh-thinking-guard smoke test —— 用真实数据的形态构造假流，断言四件事：
//   ① 复读流判 REPEAT 并写一行日志
//   ② 正常流不写
//   ③ 技术词重复不误报
//   ④ chunk 一个不少、一个不改、next() 恰好调一次
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { apply, judge, topRepeat, watchStream, name } from '../lib/index.js';

let pass = 0;
const ok = (label) => { console.log('  ✓ ' + label); pass++; };

const tmp = path.join(os.tmpdir(), 'thinking-guard-test-' + Date.now());
const logFile = path.join(tmp, 'guard.log');
const clock = () => new Date('2026-11-23T01:00:00.000Z');

async function* stream(chunks) { for (const c of chunks) yield c; }
const collect = async (it) => { const out = []; for await (const c of it) out.push(c); return out; };

const rdelta = (text) => ({ type: 'reasoning-delta', index: 0, text });
const tdelta = (text) => ({ type: 'text-delta', index: 1, text });
const finish = { type: 'finish', reason: { kind: 'stop' } };

console.log('dsh-thinking-guard smoke test\n');

// ---- 0. 形状 ----
assert.equal(name, 'dsh-thinking-guard');
assert.equal(typeof apply, 'function');
ok('插件导出 DSH 需要的 shape');

// ---- 1. 纯函数：判据 ----
{
  const bad = Array.from({ length: 400 }, () => '做。').join('') + '查。写。';
  const j = judge(bad);
  assert.equal(j.verdict, 'REPEAT');
  assert.equal(j.n, 400);
  assert.equal(j.top, '做');
  ok('400 次「做。」判 REPEAT（本机实测形态：做 x588）');

  const good = '我先看配置。然后读源码。再看日志。验证一遍。';
  assert.equal(judge(good).verdict, 'SHORT', '短推理应判 SHORT 而不是 OK');
  const long = Array.from({ length: 120 }, (_, i) => '第' + i + '步看第' + i + '个文件。').join('');
  assert.equal(judge(long).verdict, 'OK');
  ok('长但不重复的推理判 OK');

  const tech = Array.from({ length: 400 }, () => 'python。').join('');
  assert.equal(judge(tech).verdict, 'OK');
  ok('技术词重复 200 次不误报（排 TECH 表）');

  const hit = topRepeat('A。A。A。B。');
  assert.equal(hit.top, 'A');
  assert.equal(hit.n, 3);
  ok('topRepeat 选出最高频短句');
}

// ---- 2. 流：复读命中写日志 ----
{
  const chunks = [rdelta('想。'), ...Array.from({ length: 400 }, () => rdelta('做。')), tdelta('好的。'), finish];
  const out = await collect(watchStream(stream(chunks), { logFile, clock }));
  assert.deepEqual(out, chunks, 'chunk 必须一字不改、一个不少');
  ok('chunk 原样透传（数量与内容全等）');

  const log = fs.readFileSync(logFile, 'utf8').trim();
  assert.ok(log.includes('REPEAT'), '应写下 REPEAT');
  assert.ok(log.includes('n=400'), '应记重复次数');
  assert.ok(log.includes('"做"'), '应记短句原文');
  assert.equal(log.split('\n').length, 1, '一次命中只写一行');
  ok('复读命中写下一行日志（REPEAT / n=400 / 短句）');
}

// ---- 3. 流：正常不写 ----
{
  fs.rmSync(logFile, { force: true });
  const chunks = [rdelta('查配置。读源码。核日志。'), tdelta('好了。'), finish];
  await collect(watchStream(stream(chunks), { logFile, clock }));
  assert.equal(fs.existsSync(logFile), false, '正常流不该创建日志');
  ok('正常流不写日志');
}

// ---- 4. 流：只有 reasoning 的轮（空回形态）也被观测，但不注入任何东西 ----
{
  fs.rmSync(logFile, { force: true });
  const chunks = [rdelta('做。'.repeat(400)), finish];
  const out = await collect(watchStream(stream(chunks), { logFile, clock }));
  assert.deepEqual(out, chunks);
  const log = fs.readFileSync(logFile, 'utf8');
  assert.ok(log.includes('visible=none+0tool'), '应记下这一轮没有可见输出');
  ok('空回形态记进日志（visible=none+0tool），且不注入正文');
}

// ---- 5. 流：没有 finish 直接结束也要结算 ----
{
  fs.rmSync(logFile, { force: true });
  await collect(watchStream(stream([rdelta('做。'.repeat(400))]), { logFile, clock }));
  assert.ok(fs.readFileSync(logFile, 'utf8').includes('eof'), '应记 why=eof');
  ok('流没有 finish 时也在 eof 结算');
}

// ---- 6. apply：注册恰好一个 waterfall，next() 只调一次 ----
{
  let listeners = [];
  const calls = { next: 0 };
  const ctx = {
    inject(deps, fn) {
      assert.deepEqual(deps, ['llm']);
      fn({ on: (ev, l) => { assert.equal(ev, 'llm/stream'); listeners.push(l); } });
    },
  };
  apply(ctx, { logFile, enabled: true });
  assert.equal(listeners.length, 1);
  ok('apply 注册恰好一个 llm/stream 监听器');

  const it = listeners[0]({}, () => { calls.next++; return stream([rdelta('x'), finish]); });
  assert.equal(calls.next, 1);
  await collect(it);
  assert.equal(calls.next, 1, 'next() 必须只被调一次');
  ok('监听器调用 next() 恰好一次并透传结果');

  listeners = [];
  apply(ctx, { enabled: false });
  assert.equal(listeners.length, 0);
  ok('enabled:false 时什么也不注册');
}

// ---- 7. apply 留启动痕迹（ARMED）----
{
  fs.rmSync(logFile, { force: true });
  const listeners2 = [];
  const ctx2 = { inject: (d, fn) => fn({ on: (ev, l) => listeners2.push(l) }) };
  apply(ctx2, { logFile });
  const l = fs.readFileSync(logFile, 'utf8');
  assert.ok(l.includes('ARMED'), 'apply 应写一行 ARMED');
  assert.ok(l.includes('repeatMin=50'), 'ARMED 行应带实际阈值');
  assert.ok(l.includes('pid='), 'ARMED 行应带 pid');
  ok('apply 写下 ARMED 行（插件真被加载的运行时痕迹）');
}

// ---- 8. usage chunk：真实 ctx token 被记进 REPEAT 行 ----
{
  fs.rmSync(logFile, { force: true });
  const usage = { type: 'usage', usage: { inputTokens: 300000, cacheReadTokens: 338388, outputTokens: 5000, reasoningTokens: 4900 } };
  const chunks = [rdelta('做。'.repeat(400)), usage, tdelta('好。'), finish];
  await collect(watchStream(stream(chunks), { logFile, clock }));
  const log = fs.readFileSync(logFile, 'utf8');
  assert.ok(log.includes('ctx=638388'), 'ctx 应等于 inputTokens+cacheReadTokens（本机 turn 895 的真实值）');
  assert.ok(log.includes('out=5000'), '应记 outputTokens');
  ok('usage chunk 的真实 ctx/output 记进 REPEAT 行');
}

// ---- 9. CTXHINT：没复读但 ctx 过线也要报，且不刷屏 ----
{
  fs.rmSync(logFile, { force: true });
  const usage = { type: 'usage', usage: { inputTokens: 400000, cacheReadTokens: 0 } };
  const clean = [rdelta('查配置。读源码。核日志。'), usage, tdelta('好了。'), finish];
  await collect(watchStream(stream(clean), { logFile, clock, ctxHintTokens: 350000, ctxBandTokens: 50000 }));
  const first = fs.readFileSync(logFile, 'utf8');
  assert.ok(first.includes('CTXHINT'), '越过 35 万应写 CTXHINT');
  assert.ok(first.includes('threshold=350000'), '应记阈值');
  ok('CTXHINT：没复读但 ctx 超线也报（这是「该换会话」的判据）');

  await collect(watchStream(stream(clean), { logFile, clock, ctxHintTokens: 350000, ctxBandTokens: 50000 }));
  assert.equal(fs.readFileSync(logFile, 'utf8').trim().split('\n').length, 1, '同一档位不该重复写');
  ok('CTXHINT 同一档位只写一次（不刷屏）');
}

// ---- 10. SILENT：有推理无正文无工具 → 写一行（这是 reasoning-only-guard 的反向探针）----
{
  fs.rmSync(logFile, { force: true });
  const silentStream = [rdelta('我在想很久很久，但什么都不说。'.repeat(40)), finish];
  await collect(watchStream(stream(silentStream), { logFile, clock }));
  const l = fs.readFileSync(logFile, 'utf8');
  assert.ok(l.includes('SILENT'), '空回应写 SILENT');
  assert.ok(l.includes('no-visible-output'), '应标明没有可见输出');
  ok('SILENT：有推理无正文无工具时写一行（即 guard 未生效的可观测信号）');
}

// ---- 11. guard 在 finish 前注入 text 后，同一轮不再判 SILENT ----
{
  fs.rmSync(logFile, { force: true });
  // 模拟 dsh-reasoning-only-guard 的注入：reasoning 之后、finish 之前来一个 text-delta
  const guarded = [
    rdelta('我在想很久很久，但什么都不说。'.repeat(40)),
    { type: 'block-start', index: 1, blockType: 'text' },
    tdelta('[no visible output]'),
    finish,
  ];
  await collect(watchStream(stream(guarded), { logFile, clock }));
  assert.equal(fs.existsSync(logFile), false, '注入占位正文后不该再判 SILENT');
  ok('注入占位正文后不再判 SILENT（证明这条探针方向是对的）');
}

// ---- 12. usage 排在 finish 之后（真实 DSH 的顺序）也必须算到 ----
{
  fs.rmSync(logFile, { force: true });
  const lateUsage = { type: 'usage', usage: { inputTokens: 500000, cacheReadTokens: 0 } };
  const chunks = [rdelta('查配置。读源码。核日志。'), finish, lateUsage];
  await collect(watchStream(stream(chunks), { logFile, clock, ctxHintTokens: 350000, ctxBandTokens: 50000 }));
  const log = fs.readFileSync(logFile, 'utf8');
  assert.ok(log.includes('CTXHINT'), 'usage 在 finish 之后到达时仍必须写出 CTXHINT');
  assert.ok(log.includes('ctx=500000'), '应记到真实 ctx');
  ok('usage 排在 finish 之后也能算到（真实 DSH 的顺序，第一版就栽在这）');
}

fs.rmSync(tmp, { recursive: true, force: true });
console.log(`\n${pass} checks passed.`);
