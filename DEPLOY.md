# 部署与验收（唯一入口）

> 目标：**一个完全不了解鲸鱼娘的人（或另一个 AI）拿到这个仓库，能独立部署、自己验收、自己排障。**
> 不需要问作者，也不需要看懂全部代码。只要按这一页走。

---

## 0. 三十秒结论

| 你要做的事 | 一条命令 |
|---|---|
| 看这台机器能不能跑 | `python tools/doctor.py` |
| 从零部署（六步，含起服务开控制台） | `python tools/first_run.py` |
| 自己验收（14 项断言 + 出报告） | `python tools/accept.py` |
| 把旧卡搬到新位置（带备份/确认） | `python tools/migrate.py --from 旧 --to 新 --confirm` |
| 换掉人格（接自己的鱼） | 改 `core/` 三份文档 → `POST /identity` → `POST /seal` |

版本：**v8.3.1**（2026-11-18）。卡是一张 SQLite 文件；模型只提供算力，记忆、情绪、身份全在这张卡里。
给外部 AI 的操作手册见 [`START-HERE-FOR-AI.md`](START-HERE-FOR-AI.md)（九步 + 红线）。

---

## 1. 硬约束（先知道这个，能少走一天弯路）

1. **一张卡只能有一个写者。** 两个服务同时写同一张卡会各自长出一条时间线，事后合不回去。
   要双开就先 `snapshot.py make` 备份，再各用各的副本。
2. **卡不是全部可丢的。** `card.db` 里有身份锚（`identity_anchor` / `card_meta.sealed`）、
   记忆出生证（`mem_birth`）、时间线链（`timeline`）。这三样任一被改，卡会自报「被篡改」。
3. **别用文件复制的方式搬正在运行的卡。** 用 `tools/migrate.py`（SQLite backup API）或
   `snapshot.py`，不要 `cp card.db` —— WAL 里没 checkpoint 的页会丢。
4. **`writer_lease` 会跟着副本走。** 复制出来的卡如果带着租约，一打开就是只读
   （`409 readonly_lease_held`）。`migrate.py` 和 `accept.py` 都会自动清。

---

## 2. 先判断：你是哪种部署？

三种场景的「合格」标准不一样，`doctor.py --mode` 就是按这个分的：

| 场景 | 特征 | 体检命令 | 合格标准 |
|---|---|---|---|
| **A 全新** | 没有 `card.db` | `--mode fresh` | 卡不存在**不算错**，其余 MUST 0 |
| **B 接已有卡** | 手上有一张 `card.db` | `--mode existing` | 卡可读、链/锚/出生证过、MUST 0 |
| **C 迁移** | 旧卡要搬到新位置/新机器 | `--mode migrate` | 旧卡只读可读、目标可写、迁完四项复验过 |

不给 `--mode` 时是 `auto`：卡在 → existing，卡不在 → fresh。
**judge 错了会出事**：把 B 当 A 部署会另外长出一张空卡；把 A 当 B 验会红一片。

### 场景 A：从零部署（三步）

### 第 1 步：环境

- Python **3.10+**（推荐 3.12/3.13）。**不需要 GPU、不需要 torch**：向量编码是纯 numpy 前向。
- Windows 上如果 `npm`/`pip` 之类被「禁止运行脚本」挡住，统一用 `.cmd` 入口或 `python -m pip`。

```bash
python -m pip install -r requirements.txt
```

| 级别 | 包 | 没装会怎样 |
|---|---|---|
| **必需** | `numpy` | 起不来（向量与记忆计算全靠它） |
| **必需** | `Pillow` | 能跑，但媒体记忆/头像/缩略图报错 |
| **必需** | `requests` | 能跑，联网脚本（B站/QQ探针、下载）报错 |
| 建议 | `psutil` | 控制台少两块曲线图 |
| 可选 | `edge_tts` | 不发语音条 |

`tools/doctor.py` 把前三个当 MUST、后两个当 WARN。别装 `torch / transformers / sentence-transformers`——向量是 `embed_np.py` 用纯 numpy 手写前向，装了也用不上。

### 第 2 步：模型（语义记忆要用）

仓库**不分发**模型权重（100MB 级）。二选一：

- 买/下 `bge-base-zh-v1.5`（768 维，推荐）或 `bge-small-zh-v1.5`（512 维），
  放到 `<仓库>/models/bge-base-zh-v1.5/`（要有 `config.json`、`vocab.txt`、`model.safetensors`）；
- 或者随便放，然后把绝对路径告诉程序：

```bash
set WHALE_EMBED_MODEL=D:\models\bge-base-zh-v1.5
```

没有模型也能跑：语义召回自动降级成字面匹配，`tools/accept.py` 会把这条标成「未加载模型（正常）」。

### 第 3 步：建卡 + 起服务

```bash
python tools/make_demo_card.py          # 生成一张示例卡 demo/demo_card.db（编的记忆）
python bridge.py health                 # 起服务（默认 127.0.0.1:8848）
```

看到 `ok=True` 就成了。浏览器开 `http://127.0.0.1:8848/console`。

想用**空卡**从零开始（不加载示例记忆）：

```bash
python -c "import sys; sys.path.insert(0,'.'); from card2 import MemoryCard; MemoryCard('D:/card/card.db')"
```

### 场景 B：接已有卡

```bash
python tools/doctor.py --mode existing --card D:\card.db --json   # 必须 MUST 0
python tools/first_run.py --card D:\card.db
```

已有的卡**不要**重建、不要 remodel、不要跑 `make_demo_card.py`。想接进 DSH：
`python tools/install_to_dsh.py --dry` 先看它打算做什么，确认后去掉 `--dry`（幂等）。

### 场景 C：旧卡迁移

```bash
python tools/migrate.py --from D:\card.db --to E:\card\card.db          # 干跑，只打印计划
python tools/migrate.py --from D:\card.db --to E:\card\card.db --confirm # 真跑
```

真跑会：备份目标（若存在）→ SQLite backup API 复制 → 清 `writer_lease` → 复验四项（链 / 出生证 / 条数 / 维度）→
写 `migrations/<时间>.json`。**旧卡全程只读**，不加 `--confirm` 一个字节都不写。

> 2026-11-18 修的一个坑：全新卡以前缺 `mem.sess/who/place/ev_kind` 四列和
> `inbox/timeline/mem_birth` 三张表，第一次写记忆就报 `no such column: sess`。
> 现在 `MemoryCard.__init__` 里有 `_schema_heal()`，打开卡时自动补齐（幂等，老卡无感）。

---

## 3. 验收：九项要求 → 对应命令

`tools/accept.py` 会在**临时目录里跑一张副本**，绝不动你的真卡，最后写
`deploy-report.json` / `deploy-report.md`。

| # | 验收要求 | 怎么验 | 通过标准 |
|---|---|---|---|
| 1 | 仓库读取 | `git clone` 后 `python tools/doctor.py` | 能跑出分段体检（不报 ImportError） |
| 2 | 环境检查 | `tools/doctor.py` 的「依赖」段 | `MUST 0`；缺什么它自己给安装命令 |
| 3 | 模型检查 | `tools/doctor.py` 的「模型」段 | 找到目录且维度与卡一致（768↔base、512↔small） |
| 4 | 全新部署 | `python tools/first_run.py` | 六步全绿，`/health` 返回 `ok=True` |
| 5 | 数据迁移 | `tools/migrate.py --from … --to …`（先干跑） | 复验四项 PASS：链、出生证、条数、维度 |
| 6 | 功能测试 | `python tools/accept.py` | **14/14 PASS**（见下） |
| 7 | 故障处理 | 本文第 5 节故障表 | 每个症状都有原因 + 修法 |
| 8 | 数据安全 | 本页第 6 节「AI 部署安全约束」 | 只读/副本/备份/确认 四条守住 |
| 9 | 部署报告 | `accept.py` 产出的 `deploy-report.md` | 每项有 PASS/FAIL、耗时、复现命令 |

`accept.py` 的 14 项：服务启动、健康与身份、时间线链、身份锚、出生证对账、语义编码器、
记忆写入（一次 report 应让 `mem/dialogue/inbox/mem_birth` 各 +2）、记忆召回、
突触联动、上下文装配、情绪内脏（24 激素 + PAD）、状态总览、MCP 工具、干净退出。

三种卡都实测过（2026-11-18）：示例卡 14/14、真卡副本（2313 条）14/14、迁移后的卡 14/14。

---

## 4. 换自己的鱼（接你的人格）

「鱼」= 卡里的人格与口吻，和三份文档有关：

| 文件 | 作用 |
|---|---|
| `core/speech_style_v1.md` | 说话方式基线（句子长短、要不要拟人、忌讳词） |
| `core/少女口语风格规范.md` | 口语层（语气词、停顿、撒娇的分寸） |
| `core/说话像人指南-20261110.md` | 反 AI 腔的具体禁令 |

改完文档，再走接口把身份写进卡（否则控制台与上下文仍用旧身份）：

```bash
# 写名字/称呼/性格（示例，字段按你卡里的实际情况）
curl -X POST http://127.0.0.1:8848/identity -H "Content-Type: application/json" -d "{\"name\":\"小鱼\"}"
# 盖章：把身份锚定下来（没盖章的卡不会把人格拼进上下文）
curl -X POST http://127.0.0.1:8848/seal -H "Content-Type: application/json" -d "{}"
```

详情见 `docs/部署流程-V8.md` 的「换人格」一节。

---

## 5. 故障表（症状 → 原因 → 修法）

| 症状 | 原因 | 修法 |
|---|---|---|
| `no such column: sess` / `no such table: inbox` | 全新卡的旧坑（v8.1 已修） | 升级到本版；或手工 `MemoryCard(path)` 打开一次触发自愈 |
| `409 readonly_lease_held` | 卡被另一台机器/进程占着，或副本带着 `writer_lease` | 停掉旧服务；副本上 `DELETE FROM writer_lease`（`migrate.py` 会做） |
| `unable to open database file` | 目录不可写 / WAL 需要写权限 / 沙箱只读 | 换可写目录；沙箱模式下给 workspace-write |
| `401` | 写了需要令牌的接口 | 令牌在 `<卡目录>/qq_state/api_token.txt`，放 `X-Whale-Token` 头 |
| 控制台「卡槽 没文件」 | 卡没通过 `cardslot` 插进 DSH（`AGENTS.md` 不存在） | `POST /cardslot {"action":"inject"}`，或忽略（只影响 DSH 接入） |
| 控制台「盘」黄 | 只有主盘在线，备份盘离线 | 插回备份盘，或用 `snapshot.py make` 到另一块盘 |
| `[MUST] 模型完整性`：缺 config.json / vocab.txt / 权重 | 模型没下完或路径指错 | 重下整个模型目录；或用完整路径 `WHALE_EMBED_MODEL=<模型目录>` |
| `[MUST] 模型匹配`：卡里 768 维但模型是 512 维 | 卡与模型混用了 | 768↔`bge-base-zh-v1.5`、512↔`bge-small-zh-v1.5`；换模型要 `python bridge.py remodel` 重建向量 |
| 召回很差 | 没加载模型（降级成字面）或权重被环境变量改过 | `doctor.py` 看模型段；`SCORE_W` 默认 `(1.00,0.30,0,0)`，环境变量 `WHALE_EXACT=0` 可关精确通道 |
| `[card2] 结构化抽取失败：bad magic number` | 随仓库的 `_structure_orig.pyc` 是 **Python 3.12** 编的 | 用 3.12 跑服务即可；3.13 下只降级 subj/verb/obj，不影响召回主链路 |
| 服务端口冲突 | 8848 已占用 | `python mind_server.py --port 8899`，`bridge.py health` 也支持自定义端口 |
| PowerShell 报「禁止运行脚本」 | 执行策略 | 用 `.cmd`/`python -m pip`，不要 `npm.ps1`/`codex.ps1` |
| 中文写进接口变 `???` | PowerShell `ConvertTo-Json` 按本地代码页发 | 用 Python 发：`json.dumps(..., ensure_ascii=False).encode("utf-8")` |

---

## 6. AI 部署安全约束（给要来干活的 agent）

只读优先，写要显式：

1. **不得擅自删除或覆盖**：`card.db`、`snapshots/`、`qq_state/api_token.txt`、`identity_anchor`、
   `mem_birth`、`timeline`。判不准就先 `snapshot.py make`。
2. **不得改身份锚**，也不要用别的卡的锚去比（`anchor.py` 按卡的绝对路径分文件，别共用）。
3. **不可逆操作必须两步**：先干跑打印计划，再要人确认（`migrate.py` 的 `--confirm` 就是这个模式）。
4. **验收只碰副本**：`accept.py` 自动复制到临时目录；不要对真卡直接跑写测试。
5. 真卡上**先查后动**：`python tools/doctor.py` → 有问题先修 MUST，再谈部署。
6. 报告要能被第三方核对：命令、退出码、关键数字（条数、`birth_gap`、链头、耗时）都写进报告。

---

## 7. 已知问题（诚实清单）

- **模型权重不分发**：需要自己放 `models/`；没模型能跑但召回会降级。
- **`_structure_orig.pyc` 只适配 Python 3.12**：3.13 下结构抽取（subj/verb/obj）降级为空，不报错。
- **评测用例含作者生活细节**：`tools/eval/cases.json` 里有 8 条真人偏好（作者保留，用于自查召回）；
  换人格的人请自己替换。
- **`docs/吞异常台账-V7.json` 里有一个 11 位数字**，是 fp 哈希，不是手机号。
- **多卡热备没有**：只有主卡 + 备份盘；双机同写会分叉（见第 1 节硬约束 1）。

---

## 8. 复现这次验收（自己跑一遍）

```bash
python tools/doctor.py                      # 环境体检（MUST 必须为 0）
python tools/accept.py --out reports        # 14 项断言，出 reports/deploy-report.md
python tools/migrate.py --from D:\card.db --to E:\card\card.db     # 干跑，看计划
```

对外交付时，把 `reports/deploy-report.md` 一起给人 —— 那份报告就是他能不能信的凭据。
