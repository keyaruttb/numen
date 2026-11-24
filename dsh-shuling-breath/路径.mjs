/**
 * 息壤 · 路径表（2026-11-18 建）
 *
 * 这一份是**唯一真值**：所有模块要读写某个文件，都从这里取。
 * 值 = 相对数灵根的路径分段；根目录由 of(root, key) 或调用方传入。
 *
 * 为什么这样：改名 / 切根的时候只改这一个文件，不散着改几十处。
 * 命名依据：C:\dsh\_溯\息壤\细\命名表-20261118.md
 *
 * ⚠️ 数字要说准（命名表第九节写的"55 处"是**旧快照**，实测不对）：
 *   2026-11-18 逐个 grep 这 13 个文件，`path.join` 共 **127 处**，不是 55。
 *   其中：95 处 → 改成 路径of() / of模组()；11 处是本地相对常量（const X_REL = [...]）→ 改成
 *   `= 路径.Key`（调用点不动）；1 处（可标状态文件）→ 常量删成注释。合计动 107 处。
 *   剩下 32 处没收：模块自身位置（HERE / SCRIPT_DIR / SCHEMA_DIR / DSH_HOME）、
 *   OPS_DIR（旁表，属"外境"层）、以及 path.join(root, ...REL) 这类动态拼。
 *   计价明细见本文件末尾"收拢账"。
 *
 * ⚠️ 这一步是**收拢**，不是**改名**：
 *   键名用新名（灵魂 / 心迹 / 本己 / 经历 / 位 …），**值还是现在的旧目录名**
 *   （情 / 我 / 事 / 位 / 记忆 …）—— 因为文件还没搬。
 *   每一项后面一行注释写清「现在在哪 / 将来在哪」，切根的时候一眼看得出哪些还没搬。
 *
 * ── 对照表（旧写法 → 新键名；文件:行号是改之前的位置）────────────────────
 *
 * render.mjs
 *   :184  path.join(root, '总设计.md')                          → 总设计
 *   :203  path.join(root, '事', todayName(now) + '.md')         → 经历目录（+ 动态名）
 *   :241  path.join(root, '情', '情.db')                        → 心迹库
 *   :287  path.join(root, '事', '回程.json')                    → 回程
 *   :317  path.join(root, '事', '心流.jsonl')                   → 心流
 *   :355  path.join(root, '事', '心流.jsonl')                   → 心流（同上）
 *   :472  path.join(root, '事', todayName(now) + '.md')         → 经历目录（+ 动态名）
 *   :500  path.join(root, '事', 'notify-pending.jsonl')         → 待通知
 *   :523  path.join(root, '记忆', '记忆层-config.json')          → 记忆层配置
 *   :525  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :588  path.join('记忆', 'state', '可标-最后出现.txt')        → 可标最后出现（分段）
 *   :591  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :614  path.join(root, MARKABLE_STATE)                      → 可标最后出现
 *   :636  path.join(root, MARKABLE_STATE)                      → 可标最后出现
 *   :679  path.join(root, '我', '人格-P.json')                   → 人格P
 *   :680  path.join(root, '我', '人格.mjs')                      → 人格mjs
 *   :727  path.join(root, '我', '关系-P.json')                   → 关系P
 *   :777  path.join(root, '位', '状态.json')                     → 位状态
 *   :851  path.join(root, '我', '违规.json')                     → 违规
 *   :954  path.join(root, '定义.md')                            → 定义
 *   :965  path.join(root, '我')                                 → 本己目录
 *   :967  path.join(root, '我', '本相.md')                       → 本相md
 *   :968  path.join(root, '我', '六问.md')                       → 六问
 *   :994  path.join(root, '我', '称呼.md')                       → 称呼
 *   :1031 path.join(root, '情', 'README.md')                    → 心迹说明
 *   :1036 path.join(root, '情', '情.db')                        → 心迹库
 *   :1063 path.join(root, '事', g.date + '.md')                 → 经历目录（+ 动态名）
 *   :1128 path.join(root, '契约', '版本.txt')                    → 契约版本
 *
 * ui-data.mjs
 *   :67   path.join(root, '立命', '当前.md')                     → 立命当前
 *   :74   同上                                                  → 立命当前
 *   :81   path.join(root, '我', '人格-P.json')                   → 人格P
 *   :90   同上                                                  → 人格P
 *   :97   path.join(root, '我', '称呼.md')                       → 称呼
 *   :126  同上                                                  → 称呼
 *   :130  path.join(root, '相')                                 → 相目录
 *   :143  path.join(root, '相', '残值.jsonl')                    → 残值
 *   :165  path.join(root, '受身', '状态.json')                   → 受身状态
 *   :191  path.join(root, '记忆', 'state', 'star-data.json')    → 星标
 *   :223  path.join(root, '记忆', 'entries.jsonl')              → 记忆明细
 *   :233  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :260  path.join(root, '记忆', 'state', 'recall-state.json')  → 召回状态
 *   :281  path.join(root, '情', '情.db')                        → 心迹库
 *   :306  path.join(root, '遗忘')                               → 遗忘目录
 *   :341  path.join(root, '记忆', 'entries.jsonl')              → 记忆明细
 *   :342  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :343  path.join(root, '受身', '状态.json')                   → 受身状态
 *   :344  path.join(root, '记忆', 'state', 'recall-state.json')  → 召回状态
 *   :345  path.join(root, '情', '情.db')                        → 心迹库
 *   :346  path.join(root, '记忆', 'state', 'star-data.json')    → 星标
 *   :347  path.join(root, '我', '人格-P.json')                   → 人格P
 *   :348  path.join(root, '立命', '当前.md')                     → 立命当前
 *   :361  path.join(root, '灵魂', '沉淀.md')                     → 沉淀（已经在新位置）
 *
 * data.mjs
 *   :139  path.join(root, 'xiyang-sessions.json')               → 会话绑定
 *   :140  path.join(root, 'xiyang.log')                         → 息壤日志
 *   :214  path.join(root, '相')                                 → 相目录
 *   :222  path.join(dir, e.name)                                → 相目录 + 动态名
 *   :286  TRACE_REL = ['情','痕迹','汇集.md']                    → 痕迹汇集
 *   :334  path.join(root, ...TRACE_REL)                         → （常量指表）
 *   :354  MEM_REL = ['记忆','entries.jsonl']                    → 记忆明细
 *   :507  path.join(root, '记忆', '记忆层-config.json')          → 记忆层配置
 *   :510  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :538  path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :604  path.join(root, '受身', '状态.json')                   → 受身状态
 *   :642  RECALL_STATE_REL = ['记忆','state','recall-state.json'] → 召回状态
 *   :643  RECALL_CFG_REL   = ['记忆','state','recall-config.json'] → 召回配置
 *   :779  SCENE_REL = ['记忆','场景.jsonl']                     → 场景
 *   :965  path.join(root, '事', '夜班-计划.json')                → 夜班计划
 *   :979  path.join(root, '事', todayName(now) + '.md')         → 经历目录（+ 动态名）
 *   :1007 PERSON_REL_REL = ['我','关系-P.json']                 → 关系P
 *   :1066 RECALL_HITS_REL = ['记忆','state','recall-hits.json']  → 召回命中
 *   :1164 path.join(root, '记忆', '记忆.db')                    → 记忆库
 *   :1362 path.join(String(root||''), '记忆', '记忆.db')         → 记忆库（保留空串包装）
 *   :1870 path.join(root, '情', '情.db')                        → 心迹库
 *
 * init.mjs
 *   :120  path.join(ROOT, '记忆', '记忆.db')                     → 记忆库
 *   :121  path.join(ROOT, '情', '情.db')                        → 心迹库
 *   :169  path.join('我', '规矩.md')                            → 规矩md（分段）
 *   :170  path.join('我', '习惯.md')                            → 习惯md（分段）
 *   :171  path.join('位', '状态.json')                           → 位状态（分段）
 *   :283  path.join(ROOT, '记忆', 'detail-schema.mjs')          → 记忆明细schema
 *   （:401/:407/:430/:436/:474 是 path.join(ROOT, f.rel) —— f.rel 来自上面的表，值不变）
 *
 * index.js
 *   :329  HIT_REL  = ['记忆','hit.jsonl']                       → 命中
 *   :462  FLOW_REL = ['事','心流.jsonl']                         → 心流
 *   :765  path.join(root, '我', '人格-P.json')                   → 人格P
 *   :1056 path.join(XIYANG_ROOT, '记忆', '记忆.db')              → 记忆库
 *   :1926 path.join(XIYANG_ROOT, '我', '习惯.md')                → 习惯md
 *   :1927 path.join(XIYANG_ROOT, '记忆', 'entries.jsonl')        → 记忆明细
 *   :1928 path.join(XIYANG_ROOT, '记忆', '树.json')              → 树
 *   :1929 path.join(XIYANG_ROOT, '遗忘')                         → 遗忘目录
 *
 * 位-写.mjs
 *   :55   path.join(root, '位')                                 → 位目录
 *   :133  path.join(root, '位', '状态.json')                     → 位状态
 *
 * breath/prompt.js
 *   :30   path.join(XIYANG_ROOT, '我', '念头.json')              → 念头
 *   :109  HORMONE_STATE_REL = ['受身','状态.json']              → 受身状态
 *   :194  PENDING_FILE_REL  = ['事','未完成.json']              → 未完成
 *
 * habit-md.mjs  :30  path.join(XIYANG_ROOT, '我', '习惯.md')     → 习惯md
 * own-rules.mjs :32  path.join(XIYANG_ROOT, '我', '规矩.md')     → 规矩md
 * own-settings.mjs
 *   :37  path.join(XIYANG_ROOT, '我')                           → 本己目录
 *   （:44 OWN_FILES 的值是**段内文件名**，不是根相对路径，不收 —— 见报告）
 * thoughts.mjs  :23  path.join(XIYANG_ROOT, '我', '念头.json')   → 念头
 * ui-route.mjs
 *   :98  path.join(root, '记忆', 'entries.jsonl')               → 记忆明细
 *   :112 path.join(root, '记忆', '记忆.db')                     → 记忆库
 *   :165 path.join(root, '情', '情.db')                        → 心迹库
 * 违规-记.mjs   :32  path.join(root, '我', '违规.json')          → 违规
 * ────────────────────────────────────────────────────────────────────
 *
 * 不收的（不是「根相对」的路径；理由见报告）：
 *   path.join(SCRIPT_DIR|HERE|SCHEMA_DIR|DSH_HOME|OPS_DIR, …) —— 模块自身位置 / 操作目录
 *   path.join(dir, OWN_FILES[name]) —— dir 可被调用方覆盖，值是段内文件名
 *   data.mjs:447 / :886、ui-route.mjs:57 / :261-263 —— OPS_DIR（旁表，属"外境"层，命名表未归位）
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 每项是一个字符串数组（相对数灵根的分段）。
export const 路径 = {
  // ── 根下文件 ────────────────────────────────────────────────────────
  总设计: ['总设计.md'], // 现在 <根>\总设计.md；将来 命名表未定（暂不动）
  定义: ['定义.md'], // 现在 <根>\定义.md；将来 命名表未定（暂不动）
  契约版本: ['契约', '版本.txt'], // 现在 <根>\契约\版本.txt；将来 命名表未定（暂不动）
  会话绑定: ['xiyang-sessions.json'], // 现在 <根>\xiyang-sessions.json；将来 未定
  息壤日志: ['xiyang.log'], // 现在 <根>\xiyang.log；将来 未定

  // ── 灵魂\记忆\（名字不变，只是上层要包一层 灵魂\）────────────────────
  记忆库: ['记忆', '记忆.db'], // 现在 <根>\记忆\记忆.db；将来 灵魂\记忆\记忆.db
  记忆明细: ['记忆', 'entries.jsonl'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  记忆层配置: ['记忆', '记忆层-config.json'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  记忆明细schema: ['记忆', 'detail-schema.mjs'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  场景: ['记忆', '场景.jsonl'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  命中: ['记忆', 'hit.jsonl'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  树: ['记忆', '树.json'], // 现在 <根>\记忆\；将来 灵魂\记忆\
  星标: ['记忆', 'state', 'star-data.json'], // 现在 <根>\记忆\state\；将来 灵魂\记忆\state\
  召回状态: ['记忆', 'state', 'recall-state.json'], // 现在 <根>\记忆\state\；将来 灵魂\记忆\state\
  召回配置: ['记忆', 'state', 'recall-config.json'], // 现在 <根>\记忆\state\；将来 灵魂\记忆\state\
  召回命中: ['记忆', 'state', 'recall-hits.json'], // 现在 <根>\记忆\state\；将来 灵魂\记忆\state\
  可标最后出现: ['记忆', 'state', '可标-最后出现.txt'], // 现在 <根>\记忆\state\；将来 灵魂\记忆\state\

  // ── 灵魂\心迹\（现在叫 情\）──────────────────────────────────────────
  心迹库: ['情', '情.db'], // 现在 <根>\情\情.db；将来 灵魂\心迹\心迹.db
  心迹说明: ['情', 'README.md'], // 现在 <根>\情\README.md；将来 灵魂\心迹\README.md
  痕迹汇集: ['情', '痕迹', '汇集.md'], // 现在 <根>\情\痕迹\；将来 灵魂\心迹\痕迹\

  // ── 灵魂\经历\（现在叫 事\）──────────────────────────────────────────
  经历目录: ['事'], // 现在 <根>\事\；将来 灵魂\经历\（下面几个都在这层）
  心流: ['事', '心流.jsonl'], // 现在 <根>\事\；将来 灵魂\经历\
  回程: ['事', '回程.json'], // 现在 <根>\事\；将来 灵魂\经历\
  待通知: ['事', 'notify-pending.jsonl'], // 现在 <根>\事\；将来 灵魂\经历\
  未完成: ['事', '未完成.json'], // 现在 <根>\事\；将来 灵魂\经历\
  夜班计划: ['事', '夜班-计划.json'], // 现在 <根>\事\；将来 灵魂\经历\

  // ── 灵魂\相\（名字不变）──────────────────────────────────────────────
  相目录: ['相'], // 现在 <根>\相\；将来 灵魂\相\
  残值: ['相', '残值.jsonl'], // 现在 <根>\相\；将来 灵魂\相\

  // ── 灵魂\本己\（现在叫 我\）──────────────────────────────────────────
  本己目录: ['我'], // 现在 <根>\我\；将来 灵魂\本己\
  人格P: ['我', '人格-P.json'], // 现在 <根>\我\；将来 灵魂\本己\
  人格mjs: ['我', '人格.mjs'], // 现在 <根>\我\；将来 灵魂\本己\
  关系P: ['我', '关系-P.json'], // 现在 <根>\我\；将来 灵魂\本己\
  本相md: ['我', '本相.md'], // 现在 <根>\我\；将来 灵魂\本己\（显化部分已搬去受身）
  六问: ['我', '六问.md'], // 现在 <根>\我\；将来 灵魂\本己\
  称呼: ['我', '称呼.md'], // 现在 <根>\我\；将来 灵魂\本己\
  念头: ['我', '念头.json'], // 现在 <根>\我\；将来 灵魂\本己\
  违规: ['我', '违规.json'], // 现在 <根>\我\；将来 灵魂\本己\

  // ── 受身\（现在叫 我\ 的那几个 + 状态、位）—— 命名表把判据归受身 ───────
  规矩md: ['我', '规矩.md'], // 现在 <根>\我\；将来 受身\判据\规矩.md
  习惯md: ['我', '习惯.md'], // 现在 <根>\我\；将来 受身\判据\习惯.md
  受身状态: ['受身', '状态.json'], // 现在 <根>\受身\；将来 不变（受身\状态.json）
  位目录: ['位'], // 现在 <根>\位\；将来 受身\位\
  位状态: ['位', '状态.json'], // 现在 <根>\位\状态.json；将来 受身\位\状态.json

  // ── 立命\ ───────────────────────────────────────────────────────────
  立命当前: ['立命', '当前.md'], // 现在 <根>\立命\当前.md；将来 立命\索引.md（表里叫索引）

  // ── 灵魂\遗忘\（现在叫 遗忘\）────────────────────────────────────────
  遗忘目录: ['遗忘'], // 现在 <根>\遗忘\；将来 灵魂\遗忘\

  // ── 已经在写新名的（搬了一半）───────────────────────────────────────
  沉淀: ['灵魂', '沉淀.md'], // 现在就写在 <根>\灵魂\沉淀.md；将来 不变

  // ── 2026-11-18 激素纠错：唯一真值库 / 公式 ────────────────────────────
  // 这两个都按「<根>-新 先、<根> 后」两候选解析（见下面的 两候选）：
  // 迁移期新根先落库，搬完之后 <根>-新 那份不存在，自然落到 <根> 下。
  灵魂库: ['灵魂.db'], // <根>\灵魂.db（此刻真值在 C:\dsh\_溯\息壤-新\灵魂.db）
  公式: ['立命', '公式.json'], // <根>\立命\公式.json（激素基线/半衰期在这里）
};

// ── 相对「模块自己所在的 lib\」的路径（**不是**数灵根）────────────────────
// 为什么单列一张：库结构 DDL / 包内明细 schema / 本机配置 跟着代码走，不跟数据走。
// 它们骑的是 import.meta.url，不是 root —— 混进上面那张表会让人以为切根能带走它们。
// 现在都在 <包>\lib\ 下；将来 命名表说 DDL 要归位到 <根>\息壤\库结构\（那时这一张整张换基）。
const HERE = path.dirname(fileURLToPath(import.meta.url));

export const 模组路径 = {
  库结构目录: ['schema'], // 现在 <包>\lib\schema\；将来 <根>\息壤\库结构\
  记忆层DDL: ['schema', '记忆-记忆层.sql'], // 现在 <包>\lib\schema\；将来 <根>\息壤\库结构\
  心迹L0DDL: ['schema', '情-L0.sql'], // 现在 <包>\lib\schema\；将来 <根>\息壤\库结构\
  明细schema: ['detail-schema.mjs'], // 现在 <包>\lib\；将来 不变
  自身配置: ['xiyang-config.json'], // 现在 <包>\lib\；将来 不变（本机实况，不随包分发）
};

/** 模组内路径：相对 <包>\lib\ 拼出来（不看 root）。 */
export function of模组(key, ...extra) {
  const seg = 模组路径[key];
  if (!seg) throw new Error('模组路径表里没有这一项: ' + key);
  return path.join(HERE, ...seg, ...extra);
}

/** 取某一项的路径分段（数组本身，调用方自己拼）。表里没有 ⇒ 抛。 */
export function 段(key) {
  const seg = 路径[key];
  if (!seg) throw new Error('路径表里没有这一项: ' + key);
  return seg;
}

/**
 * 便捷：把分段拼成绝对路径。
 * extra 是附在后面的动态段（如日期文件名），保持 path.join 的原语义。
 */
export function of(root, key, ...extra) {
  if (extra.length === 0) return path.join(root, ...段(key));
  return path.join(root, ...段(key), ...extra);
}

/**
 * 迁移期两候选：先 <根>-新\，再 <根>\。
 * 为什么：新根（\\息壤-新）先落库，老根还在跑；搬完之后 -新 那份不存在，
 * 调用方按「文件在不在」自然落到老根那条 —— 不需要改调用方。
 * root 本身已经以 -新 结尾 ⇒ 只返回它自己（不拼成 -新-新）。
 */
export function 两候选(root, key) {
  const base = String(root || '').replace(/[\\/]+$/, '');
  const out = [];
  if (base && !base.endsWith('-新')) out.push(path.join(base + '-新', ...段(key)));
  if (base) out.push(path.join(base, ...段(key)));
  return out;
}

export default 路径;

/* ── 收拢账（2026-11-18 10:09 实测，文件:值 = 改动前的 path.join 条数）──────────
 *
 *  文件              旧 path.join   直接改 路径of/of模组   常量指表   剩（不收）
 *  render.mjs            28              27                 0          1(态常量→注释)
 *  ui-data.mjs           24              24                 0          0
 *  data.mjs              24              13                 7         11
 *  init.mjs              19               3                 0         10
 *  index.js              12               6                 2          6
 *  位-写.mjs              3               3                 0          0
 *  breath/prompt.js       3               1                 2          2
 *  habit-md.mjs           1               1                 0          0
 *  own-rules.mjs          1               1                 0          0
 *  own-settings.mjs       3               1                 0          2
 *  thoughts.mjs           1               1                 0          0
 *  ui-route.mjs           7               3                 0          4
 *  违规-记.mjs            1               1                 0          0
 *  ─────────────────────────────────────────────────────────────────────
 *  合计                 127              85                11         36
 *  （85+11=96 处换了来源；另 12 处是替换带出来的常量定义，实际写盘 107 处改动）
 *
 *  init.mjs 那 3 处直接改里面含 2 处 DDL（经 of模组），另有 6 处经 of模组 替换了
 *  HERE/SCHEMA_DIR 的写死用法。剩的 10 处：DSH_HOME/os.homedir 推导 2、path.join(ROOT,d|f.rel) 5、
 *  path.join(...路径.X) 3（这三处已经是"从表里取"，只是形式上还是 path.join）。
 *
 * ⚠️ 还没收、也收不了的（不是"漏了"，是"不该往这张表里塞"）：
 *   ① path.join(HERE|SCRIPT_DIR|SCHEMA_DIR|DSH_HOME, …) —— 跟代码走，不跟数据走；已单列 模组路径
 *   ② path.join(OPS_DIR, …) —— 旁表在 _ops（"沉淀-状态.db / 遗忘-状态.db / forget-params.json /
 *      xiyang-breath.*"），命名表说将来归 灵魂\旁表\；那一层还没搬，搬的时候整组换基
 *   ③ path.join(dir, OWN_FILES[name]) —— dir 可被调用方覆盖（测试传临时目录），
 *      OWN_FILES 的值是"段内文件名"不是根相对路径
 *   ④ path.join(ROOT, d) / path.join(ROOT, f.rel) —— init.mjs 的写入循环，值是表里给的
 *   ⑤ index.js:2740-2741 的 persona.js 探测路径 —— 指向别的包（dsh-memory / dsh-ling），不是息壤数据
 * ───────────────────────────────────────────────────────────────────────── */

