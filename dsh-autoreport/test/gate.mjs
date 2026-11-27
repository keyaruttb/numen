// 门的三种情况测试：不碰真卡，只验「该放行的放行、该拒的拒」。
//
//   node test/gate.mjs
//
// 用 makeReporter 造一个 reporter，把 host 指向一个不存在的端口（127.0.0.1:1），
// 这样即使门放行，HTTP 也会失败并被 sent.delete 重试 —— 但我们只关心
// report() 的**返回值**：true = 放行并已发起，false = 没发起。
//
// 关键：我们要区分三种 false：
//   ① 门拒绝
//   ② 已经在 sent 里
//   ③ item 本身不完整
// 所以断言用返回值 + blocked 行为（第二次调用仍 false，且不重试）。

import { writeFileSync, mkdirSync, rmSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';
import { makeReporter } from '../lib/index.js';

const GATE = 'C:\\dsh\\_ops\\safe\\_gate_test.state.json';

// 把 reporter 的 host/port 指到黑洞，避免真的写卡
const CFG = { host: '127.0.0.1', port: 1, tokenFile: '', source: 'gatetest', gateFile: GATE };

function setGate(obj) {
  mkdirSync(dirname(GATE), { recursive: true });
  if (obj === null) {
    if (existsSync(GATE)) rmSync(GATE);
    return;
  }
  writeFileSync(GATE, typeof obj === 'string' ? obj : JSON.stringify(obj), 'utf8');
}

let pass = 0, fail = 0;
function check(name, got, want) {
  const ok = got === want;
  if (ok) pass += 1; else fail += 1;
  console.log('  ' + (ok ? '✓' : '✗') + ' ' + name + '  got=' + got + ' want=' + want);
}

const T = 1795750000000;          // 基准毫秒
const item = { turn: 100, ts: T, user: '测试这句话', reply: '测试回复', done: true };

console.log('='.repeat(60));
console.log(' 门测试');
console.log('='.repeat(60));

// ① 没有门文件 → 放行
setGate(null);
let r = makeReporter(CFG);
check('① 无状态文件 → 放行', r(item), true);

// ② 状态 ok=false 且是同一轮 → 拒绝
setGate({ ok: false, at: T, turn_ts: T, len: 0, err: 'empty_message' });
r = makeReporter(CFG);
check('② ok=false 同轮 → 拒绝', r(item), false);
check('② 拒绝后不再重试', r(item), false);

// ③ ok=false 但不是同一轮（差 10 秒）→ 放行
setGate({ ok: false, at: T, turn_ts: T - 10000, len: 0, err: 'x' });
r = makeReporter(CFG);
check('③ ok=false 非本轮 → 放行', r(item), true);

// ④ ok=true 同轮 → 放行
setGate({ ok: true, at: T, turn_ts: T, len: 4000, err: '' });
r = makeReporter(CFG);
check('④ ok=true 同轮 → 放行', r(item), true);

// ⑤ 文件坏掉（不是 JSON）→ 放行（退化，不能因为坏文件停记忆）
setGate('{ 这不是 json');
r = makeReporter(CFG);
check('⑤ 文件损坏 → 放行', r(item), true);

// ⑥ 没有 turn_ts（老版本写的状态）→ 放行
setGate({ ok: false, at: T, len: 0 });
r = makeReporter(CFG);
check('⑥ 缺 turn_ts → 放行', r(item), true);

// ⑦ 容差边界：差 1.9 秒 → 仍算同一轮 → 拒绝
setGate({ ok: false, at: T, turn_ts: T - 1900, len: 0, err: 'x' });
r = makeReporter(CFG);
check('⑦ 差 1.9 秒 → 算同轮 → 拒绝', r(item), false);

// ⑧ 容差边界：差 2.1 秒 → 不算同轮 → 放行
setGate({ ok: false, at: T, turn_ts: T - 2100, len: 0, err: 'x' });
r = makeReporter(CFG);
check('⑧ 差 2.1 秒 → 非本轮 → 放行', r(item), true);

// 收尾：删测试状态文件
setGate(null);

console.log('='.repeat(60));
console.log(' 通过 ' + pass + ' / 失败 ' + fail);
console.log('='.repeat(60));
process.exit(fail === 0 ? 0 : 1);
