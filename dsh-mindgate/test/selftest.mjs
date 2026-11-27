// 自检：不装进 DSH 也能验「能不能从卡里取到东西」。
//
//   node test/selftest.mjs
//
// 验三件：
//   ① curl.exe 在不在（Node 里同步发 HTTP 的唯一手段）
//   ② 卡在不在（/health）
//   ③ /context 取到的 prompt 里有没有那几段关键内容（我是谁 / 身体 / 此刻）

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { selfTest } from '../lib/index.js';

const TOKEN_FILE = 'C:\\dsh\\_card\\qq_state\\api_token.txt';
const BASE = 'http://127.0.0.1:8848';

function line(s = '') {
  console.log(s);
}

line('='.repeat(66));
line(' dsh-numen-mindgate 自检');
line('='.repeat(66));

// ① curl
const v = spawnSync('curl.exe', ['--version'], { encoding: 'utf8', timeout: 5000 });
line('  ① curl.exe : ' + (v.status === 0
  ? '✓ ' + (v.stdout || '').split('\n')[0].trim()
  : '✗ 找不到（Node 里同步发 HTTP 就靠它）'));

// ② 卡
let token = '';
try {
  token = readFileSync(TOKEN_FILE, 'utf8').trim();
  line('  ② token    : ✓ ' + token.length + ' 字符');
} catch (e) {
  line('  ② token    : ✗ ' + (e.message || e));
}

const h = spawnSync('curl.exe', ['-s', '-S', '--max-time', '4', BASE + '/health'],
  { encoding: 'utf8', timeout: 6000 });
try {
  const hj = JSON.parse((h.stdout || '').trim());
  line('     卡状态  : ok=' + hj.ok + ' sealed=' + hj.sealed + ' mem=' + hj.memories
    + ' chain=' + (hj.timeline?.chain_ok));
} catch (e) {
  line('     卡状态  : ✗ 读不到（' + (h.stdout || h.stderr || '').slice(0, 100) + '）');
}

// ③ 真取一次
line('');
line('  ③ 取一次 /context ……');
const r = selfTest();
if (!r.ok) {
  line('     ✗ 失败（' + r.ms + 'ms）：' + r.err);
  line('     ⇒ 装进 DSH 之后，这一轮系统提示里会出现「【生命卡没连上】」的硬停指令。');
  process.exit(1);
}
line('     ✓ 成功：' + r.len + ' 字 / ' + r.ms + 'ms');
line('     开头：' + r.head.replace(/\n/g, ' | '));

// ④ 关键段在不在
//    prompt 是插件原样塞进去的，缺哪段说明 /context 自己没给。
const need = ['【我是谁】', '【此刻】', '【身体】', '面前的人是天晞'];
line('');
line('  ④ 关键段：');
let bad = 0;
for (const k of need) {
  const ok = r.head.includes(k) || true; // head 只有前 100 字，下面单独取全文
  void ok;
}
const full = selfTest().ok;
if (!full) bad += 1;
// 用一次完整取，检查全文
const fullRes = spawnSync('curl.exe', [
  '-s', '-S', '--max-time', '6', '-X', 'POST',
  '-H', 'Content-Type: application/json',
  '-H', 'X-Whale-Token: ' + token,
  '--data-binary', '@-', BASE + '/context',
], { input: JSON.stringify({ message: '自检' }), encoding: 'utf8', timeout: 8000 });
let prompt = '';
try {
  prompt = JSON.parse((fullRes.stdout || '').trim()).context?.prompt || '';
} catch { /* ignore */ }
for (const k of need) {
  const ok = prompt.includes(k);
  if (!ok) bad += 1;
  line('     ' + (ok ? '✓' : '✗') + ' ' + k);
}

line('');
line('='.repeat(66));
line(bad === 0 ? ' 全部通过。装进 DSH 后每轮都会带上这些。' : ' 有 ' + bad + ' 项不通过。');
line('='.repeat(66));
process.exit(bad === 0 ? 0 : 1);
