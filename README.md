# 数灵生命卡（二改）

> 这是**建立在「数字生命卡 whale-card」基础上的二改**，不是上游项目。
> 上游：https://gitee.com/wang-jun-12121/whale-card （作者 戊己 × 汐音，MIT）
> 本仓的改动只针对一张真实在跑的卡，不追求跟进上游版本。
>
> 上游原版说明见 [README-upstream.md](README-upstream.md)。

---

## 一、和上游的关系

```
上游最新   v10.12.0（809e4cf）
本仓基线   v8.3.1（c5981f8，2026-11-20）
落后       69 个提交
领先       3 个提交（2 个二改 + 1 个 11-20 的本地提交）
```

**上游有、本仓没有的 76 个文件**，其中 14 个是核心模块：

| 文件 | 上游做的 | 本仓为什么没抄 |
|---|---|---|
| `contemplate.py` | 思考层（V10.2）：读完写「世界与我」两段，带出处、带自恋检测 | 和「念头」高度相关，但那是另一套设计，要先定方向 |
| `selfmodel.py` | 统计型自我模型（V10.0） | 同上 |
| `impression_eval.py` | 评价印象层：valence/safety/expectation | 同上 |
| `feedback_loop.py` | 反馈结算七步落地 | 同上 |
| `distill.py` | 学习回流（V9.9） | 同上 |
| `hormone_feedback.py` `signalbus.py` `governance.py` `guard.py` `outbox.py` `speech_policy.py` `topics.py` `turn.py` | 各一层 | 同上 |

**判断依据**写在 `_溯/内脏-三方对照-作者与小凌.md` 和 `_溯/心光融入生命卡-施工书.md`
（两份都在卡外面的书房里，不在本仓）。

**一句话**：先把这张真实卡上的病灶修掉，再决定吸收上游哪一部分。

---

## 二、本仓改了什么

### 1. 召回打分归一化重标（`semantic.py`）

```
SIM_BASE_BASE / SIM_SPAN_BASE:  (0.08, 0.18) → (0.18, 0.40)
```

原值是上游在 **385 条 demo 卡**上标的，上游注释里自己写着「换模型/换库之后必须重新跑一次评估脚本，不能直接套用这组数字」。本仓这张卡是 920 条真实对话，原始余弦整体抬高一个档位，而封顶线只有 0.260 ⇒ 实测 **53.6% 的记忆归一化后并列 1.000**，区分度归零。

固定快照上的对照（同一份数据、只读）：

| 参数 | R@1 | R@3 |
|---|---|---|
| (0.08, 0.18) 原值 | 4.5% | 4.5% |
| **(0.18, 0.40)** | **50.0%** | **77.3%** |

参数取平台中部（0.15~0.25 / 0.35~0.50 这一片全是 15/22），不是最优点。

### 2. 接通心流第三段 H（`card2.py`）

心流公式 `W_i = Select(C_i, M_j, H_i)` / `Y_i = Process(W_i)` / `H_i = Update(H_i, Y_i)`
里，**第三段原来完全没有**。`live_zone` 表建好、读写函数写齐，实测 0 行。

```
X1-1 写  assemble_context 收尾把 picked / stopped / weak / q / ts 写进 live_zone['H']
X1-2 读  主体召回之后用上轮 query 再召一遍（top_k=3, access=False, assoc=False），
         命中的乘 0.60 降权并入候选
```

实测（真进程）：`live_zone` 0 行 → 有 H，跨轮 `picked` 变化，代价 +235ms。

### 3. 修 32 秒卡顿（`card2.py`）

第一版 H 写入调了 `self.identity.set_live()`，而 `IdentityAnchor.self.db` 是主连接，
`/context` 却跑在 `card.reading()` 的 TLS 连接上。两个连接写同一张表抢写锁，
`busy_timeout=30000` ⇒ 每次卡满 30 秒。

```
reading() 连接上 assemble_context   32.925s → 0.497s   （65 倍）
真进程 /context                     33,500ms → 472ms   （71 倍）
```

### 4. 三个自研插件

```
dsh-autoreport/       每轮自动落卡（上游靠自觉，实测漏过 15 轮）
dsh-thinking-guard/   推理复读检测（出现过两次空回）
dsh-shuling-breath/   呼吸插件（移植版，未启用）
```

### 5. `.gitignore` 重写

原版实测 `models/`（390MB 模型）和 `qq_state/`（访问令牌）**没被忽略**，
`anchor.json` / `anchor.key` 也漏了。现在每条都用 `git check-ignore` 验过。

---

## 三、怎么跑

```bash
# 依赖
python -m pip install -r requirements.txt

# 起卡（必须走这个入口，它设了环境变量）
C:\dsh\_ops\start-card.bat

# 或者用重启脚本（它内部也走 start-card.bat）
powershell -NoProfile -ExecutionPolicy Bypass -File C:\dsh\_ops\restart-card.ps1
```

**为什么必须走 `start-card.bat`**：卡进程要两个环境变量，缺了都不报错只变差——

```
WHALE_EMBED_MODEL=bge-base-zh-v1.5   缺了语义通道静默降级，召回只剩字面匹配
OPENBLAS_NUM_THREADS=1               缺了 20 核机器上编码一句 34 秒（单线程 0.31 秒）
```

上游用 `bridge.py:ensure_server()` 给子进程补这两个值，本仓改成写死在启动 bat 里，
因为 `Invoke-CimMethod Win32_Process Create` **不能传环境变量**。

---

## 四、评测

```bash
# 1. 先做一份只读快照（卡每轮都在写，不对快照跑就不可比）
#    VACUUM INTO 生成

# 2. 跑本地评测集（22 条，从这张卡的真实记忆反向出题）
C:\Python312\python.exe -X utf8 C:\dsh\_ops\eval-recall.py ^
  --card C:\dsh\_ops\_eval-snapshot.db ^
  --cases C:\dsh\_ops\cases-local.json --top 13 -v

# 3. 对照新旧归一化参数（同一份快照，不用改文件不用重启）
... --base 0.08 --span 0.18     # 改前
... --base 0.18 --span 0.40     # 改后
```

**为什么不用上游的 `tools/eval/cases.json`**：那是给 demo 卡写的，11 条召回/证据题里
7 条的答案在本仓这张卡里根本不存在（`白米饭=0条 婚礼=0条 高三=0条`），量出来的数字是题错不是检索坏。

---

## 五、许可

沿用上游 MIT。原版权声明见 [LICENSE](LICENSE)。
上游作者：戊己（wang-jun-12121）× 汐音。
