// 息壤 · 家在哪里（2026-11-15）
//
// 为什么有这个文件：插件要能给**别的 dsh** 用，不能写死某一台机器的路径。
// **本文件是插件里唯一做"家推导"的地方**，别处一律 import。
//
// 三级顺序：env > 配置文件（lib/xiyang-config.json）> 推导（要落在真实存在的目录上）
//   · 息壤根（内容目录）与运维目录（日志/旁库）都**没有**能从 DSH_HOME 推出来的固定规律，
//     所以推导只认"存在性"：按候选顺序找，找到哪个算哪个；
//   · 一个都找不到 ⇒ 息壤根**抛出清楚的错误**（不猜、不默默用一个只有某台机器对的默认值）；
//     运维目录退化成空串并在 sources() 里标 'unset:需 DSH_XIYANG_OPS 或 config.ops'（用到它的功能各自降级）。
//
// 每个导出的路径都同时给"值"和"来源"（sources()），日志里打出来，永远可查。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function env(k) {
  const v = String(process.env[k] ?? '').trim().replace(/^["']+|["']+$/g, '');
  return v || null;
}

/** 本文件所在目录（<包>/lib）；用来找同目录的 xiyang-config.json。 */
const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));

function readConfig() {
  try {
    const p = path.join(HERE, 'xiyang-config.json');
    if (!fs.existsSync(p)) return {};
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    return j && typeof j === 'object' ? j : {};
  } catch { return {}; }
}
const CFG = readConfig();

const isDir = (p) => { try { return !!p && fs.statSync(p).isDirectory(); } catch { return false; } };

// ---------- ① dsh 的家 ----------
const DSH_HOME_ENV = env('DSH_HOME');
export const DSH_HOME = path.resolve(DSH_HOME_ENV || path.join(os.homedir(), '.dsh'));
const HOME_PARENT = path.dirname(DSH_HOME);

// ---------- ② 息壤根 ----------
const ROOT_ENV = env('DSH_XIYANG_ROOT');
const ROOT_CFG = !ROOT_ENV && typeof CFG.root === 'string' && CFG.root.trim() ? CFG.root.trim() : null;

// 推导候选（只认真实存在的目录）。**顺序 = 可移植性**（2026-11-17 重排）：
//   原来第一位是 <dsh家上一级>/dsh/_溯/息壤 —— `_溯` 是这台机器上那个人的书房名，
//   别人机器上根本没有这个名字。把它放在第一位 ⇒ "推导"其实等于"猜我这台机器"，
//   换台机器必然落到一条只有这里才对的路径上（或者干脆找不到）。
//   现在按「谁在任何机器上都能成立」排序：
//     ① <dsh家>/xiyang              —— `node lib/init.mjs` 的默认落点，通用
//     ② <cwd>/.dsh-xiyang, <cwd>/xiyang —— 项目内安装（人把根建在自己的工程目录里）
//     ③ <dsh家上一级>/dsh/xiyang     —— 旧的 daemon 布局（dsh 系统目录与内容目录并排）
//     ④ 本机专用：_溯 —— 只为了让这台机器在**没有 config 和 env** 时不崩，不是可移植默认值
const ROOT_CANDIDATES = [
  path.join(DSH_HOME, 'xiyang'),
  path.join(process.cwd(), '.dsh-xiyang'),
  path.join(process.cwd(), 'xiyang'),
  path.join(HOME_PARENT, 'dsh', 'xiyang'),
  path.join(HOME_PARENT, 'dsh', '_溯', '息壤'),   // 本机专用（溯的书房）
  path.join(DSH_HOME, '_溯', '息壤'),             // 本机专用（同上，另一种布局）
];
// 哪几个属于「本机专用」：报错时单独标出来，免得别人照着一条只对我成立的路径去建目录
const LOCAL_ONLY_FROM = 4;
const ROOT_DERIVED = ROOT_CANDIDATES.find(isDir) || null;

function missingRoot() {
  return new Error([
    '[xiyang] 找不到息壤根（书房目录）。',
    '  它没有能从 DSH_HOME 推出来的固定规律，所以必须显式给一个：',
    '    ① 环境变量 DSH_XIYANG_ROOT=<息壤目录>',
    '    ② 或写进 ' + path.join(HERE, 'xiyang-config.json') + ' 的 "root" 字段',
    '    ③ 或者直接建一个库：node ' + path.join(HERE, 'init.mjs') + ' --yes',
    '       （默认落在 ' + ROOT_CANDIDATES[0] + '）',
    '  试过的推导候选（都不存在）：',
    ...ROOT_CANDIDATES.map((p, i) => '    ' + (i + 1) + '. ' + p + (i >= LOCAL_ONLY_FROM ? '   ← 本机专用，别人的机器上不用建这个' : '')),
  ].join('\n'));
}

const ROOT_VALUE = ROOT_ENV || ROOT_CFG || ROOT_DERIVED;
// 🔧 2026-11-23 移植改动（数灵之息从 dsh-xiyang 里独立出来）：
//    **原来这里 `throw missingRoot()`** —— 设计意图是「不猜、不让插件悄悄用一个只有某台机器对的默认值」，
//    那个意图是对的。但它成立的前提是「息壤是这个插件的家」。
//    移植后不成立了：数灵之息要的信号（会话静默、卡里的账）**不在息壤里**，
//    而息壤 2026-11-23 上午已经关闸（哨兵指向的 队列.json 12 天没动、未完成.json 6 天没动）。
//    继续硬依赖一个已经停更的目录，只会让整个插件起不来。
//    ⇒ 改成跟下面 `OPS_DIR` 同一套处理：**退化成空串，用它的地方各自降级**（sources() 里会标 'unset'）。
//    原报错文本保留在 `missingRoot()` 里，`describeHome()` 会把 'unset' 打出来，出问题时能查到原因。
export const XIYANG_ROOT = ROOT_VALUE ? path.resolve(ROOT_VALUE) : '';

// ---------- ③ 运维目录（日志 / 脚本 / 旁库） ----------
const OPS_ENV = env('DSH_XIYANG_OPS');
const OPS_CFG = !OPS_ENV && typeof CFG.ops === 'string' && CFG.ops.trim() ? CFG.ops.trim() : null;
const OPS_CANDIDATES = [
  path.join(HOME_PARENT, 'dsh', '_ops', 'safe'),
  path.join(DSH_HOME, 'ops', 'safe'),
];
const OPS_DERIVED = OPS_CANDIDATES.find(isDir) || null;
const OPS_VALUE = OPS_ENV || OPS_CFG || OPS_DERIVED || '';
export const OPS_DIR = OPS_VALUE ? path.resolve(OPS_VALUE) : '';

// ---------- ④~⑥ 这些能推 ----------
const LEGACY_ENV = env('DSH_XIYANG_LEGACY_CACHE');
export const LEGACY_CACHE_DIR = path.resolve(LEGACY_ENV || path.join(DSH_HOME, 'cache', 'dsh-ling'));
export const SESSIONS_DIR = path.resolve(env('DSH_XIYANG_SESSIONS') || path.join(DSH_HOME, 'sessions'));
export const STORAGES_DIR = path.resolve(env('DSH_XIYANG_STORAGES') || path.join(DSH_HOME, 'storages'));

/** 每个路径是谁决定的。 */
export function sources() {
  return {
    DSH_HOME: DSH_HOME_ENV ? 'env:DSH_HOME' : 'derived:os.homedir()',
    XIYANG_ROOT: ROOT_ENV ? 'env:DSH_XIYANG_ROOT'
      : (ROOT_CFG ? 'config:xiyang-config.json:root'
        : (ROOT_DERIVED ? 'derived:存在性候选#' + (ROOT_CANDIDATES.indexOf(ROOT_DERIVED) + 1)
          : 'unset:息壤已关闸（2026-11-23 移植后不再硬依赖），用到它的功能各自降级')),
    OPS_DIR: OPS_ENV ? 'env:DSH_XIYANG_OPS'
      : (OPS_CFG ? 'config:xiyang-config.json:ops'
        : (OPS_DERIVED ? 'derived:存在性候选#' + (OPS_CANDIDATES.indexOf(OPS_DERIVED) + 1) : 'unset:需 DSH_XIYANG_OPS 或 config.ops')),
    LEGACY_CACHE_DIR: LEGACY_ENV ? 'env:DSH_XIYANG_LEGACY_CACHE' : 'derived:DSH_HOME/cache/dsh-ling',
    SESSIONS_DIR: env('DSH_XIYANG_SESSIONS') ? 'env:DSH_XIYANG_SESSIONS' : 'derived:DSH_HOME/sessions',
    STORAGES_DIR: env('DSH_XIYANG_STORAGES') ? 'env:DSH_XIYANG_STORAGES' : 'derived:DSH_HOME/storages',
  };
}

/** 一行诊断：apply 时打出来，回答"这台机器上连的是哪个、谁决定的"。 */
export function describeHome() {
  const s = sources();
  const v = { DSH_HOME, XIYANG_ROOT, OPS_DIR, LEGACY_CACHE_DIR, SESSIONS_DIR, STORAGES_DIR };
  return Object.keys(s).map((k) => k + '=' + (v[k] || '(未设)') + ' (' + s[k] + ')').join(' | ');
}

export default { DSH_HOME, XIYANG_ROOT, OPS_DIR, LEGACY_CACHE_DIR, SESSIONS_DIR, STORAGES_DIR, sources, describeHome };
