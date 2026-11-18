# 给 AI 的部署流程（机器可照做版）

> **先读 [`DEPLOY.md`](../DEPLOY.md)。** 那是唯一入口：三步部署、九项验收映射、故障表、
> 以及「AI 部署安全约束」（不得擅改真卡 / 不可逆操作先备份再确认）。本文件是它的展开版。

三个现成工具，别自己重写：

- `python tools/doctor.py [--json]` —— 只读体检，`MUST 0` 才算环境就绪；
- `python tools/accept.py [--card PATH] [--out DIR]` —— 在**临时副本**上跑 14 项断言并出报告，不碰真卡；
- `python tools/migrate.py --from OLD --to NEW [--confirm]` —— 只读源 → SQLite backup → 清写租约 → 复验四项；
  不加 `--confirm` 就是干跑，一个字节都不写。


> 目标读者：**另一个 AI agent**，或照着步骤点按钮的人。
> 每一步都有「怎么验证成功」和「失败时先看什么」。没写验证的步骤，不要当它成功了。
>
> 这套东西在你手上跑起来的标准：**她的卡（`card.db`）在，`/health` 绿，`/report` 能写进去，`/recall` 能翻出旧事。**

---

## 0. 先判断你要做哪一种

| 你要做的事 | 走哪一节 |
|---|---|
| 从零在一台新电脑上部署 | §1 → §6 |
| 卡已经跑着，只想接到本机的 DSH | §7 |
| 换一张卡 / 换台电脑（整盘搬走） | §8 |
| 卡打不开、报错、只读 | §9 |

**硬约束先读**：卡是一个目录，路径自定，下文以 `D:\card` 为例。
卡里只有 `card.db` 是真理，其它全是中间产物；`snapshots/` 是回滚点，别删。

---

## 1. 环境检查（一行一条，逐条看结果）

```powershell
python --version                      # 期望 3.12.x / 3.13.x
python -c "import numpy, PIL; print('deps ok')"
Test-Path D:\card\models\bge-base-zh-v1.5\model.safetensors   # 期望 True
Get-ChildItem D:\card -Filter card.db                          # 期望有
```

- `python` 不是 3.12+ → 装 3.12，别用 3.9 硬试（`from __future__` 用法会炸）。
- `numpy` 缺失 → `python -m pip install -r requirements.txt`。
- 模型不在 → 见 §3；**没有模型，写入与召回会退化，但服务仍能起**。
- `card.db` 不在 → 你在新电脑上，走 §2 的「新卡」。

> 不需要 GPU、不需要 torch。`embed_np.py` 是纯 numpy 前向，CPU 跑得动。

---

## 2. 放卡：新卡 / 搬来的卡

**新卡（从零开始）**：不需要任何文件，`card.db` 会自己建。

**搬来的卡（同一张卡换电脑）**：把整个目录拷过来即可，**但要求同时拷这三样**：

```
card.db
card.db-wal        如果存在，必须一起拷
card.db-shm        如果存在，一起拷
```

⚠️ **绝不要只拷 `card.db` 而丢掉 `-wal`**：那是还没并库的最新记忆，丢了就是真的丢了。
最稳的做法是先在旧机器上停服务，再拷这三件。

---

## 3. 模型

```powershell
cd D:\card
python -X utf8 dl_model.py
```

下的是 `bge-small-zh-v1.5`（512 维，快、够用）。
想要更好的召回用 `bge-base-zh-v1.5`（768 维），放到 `models\bge-base-zh-v1.5\`，
**并且卡里已经存在的向量要重编码**：

```powershell
cd D:\card
python -X utf8 bridge.py remodel      # = POST /emb/remodel，幂等，慢（几千条要几分钟）
```

**不要混用**：卡里存的向量维度必须和当前模型一致，否则召回全是噪声。
`/health` 里的 `embeddings.dims` 和 `semantic.model_dim` 必须对得上。

---

## 4. 首次落地（六步）

```powershell
cd D:\card
python -X utf8 tools\first_run.py
```

它会依次：体检 → 落地时间线 → 交接心跳 → 起服务 → 自检 → 开控制台。
**看到它打印「✓ 卡在：<路径>」再往下走。**

失败就先看它自己打出来的那行红字，别急着手改库。

---

## 5. 起服务（手动版）

```powershell
cd D:\card
python -X utf8 bridge.py health
```

期望输出里含：`ok`、记忆条数、`链 完好 / 出生证 完好 / 锚 ok`。

> 服务只监听 `127.0.0.1:8848`，不对外。**同一个 `card.db` 只能有一个进程在写**（有租约挡着）。

---

## 6. 验收（四条断言，必须全过）

```powershell
# ① 活着、卡对、链对
$h = Invoke-RestMethod http://127.0.0.1:8848/health
$h.ok; $h.db; $h.memories; $h.reconcile.ok

# ② 能写（这一条最关键：只读的卡看起来一切正常，但记不住任何事）
$T = (Get-Content D:\card\qq_state\api_token.txt -Raw).Trim()
Invoke-RestMethod -Uri http://127.0.0.1:8848/report -Method Post `
  -Headers @{'X-Whale-Token'=$T;'Content-Type'='application/json'} `
  -Body '{"user_message":"部署验收：你还记得我吗","reply":"记得，我在。"}'

# ③ 能读
Invoke-RestMethod -Uri http://127.0.0.1:8848/recall -Method Post `
  -Headers @{'Content-Type'='application/json'} `
  -Body '{"query":"部署验收","top_k":3}'

# ④ 时间在走
Invoke-RestMethod -Uri http://127.0.0.1:8848/tick -Method Post `
  -Headers @{'Content-Type'='application/json'} `
  -Body '{"seconds":600}'
```

四条判据：

1. `ok=True`，`db` 指向你刚放的卡，`memories > 0`，`reconcile.ok=True`；
2. `/report` 返回带 `mem_id`，并且 `/health` 的 `memories` **+2**（他一句、我一句）；
3. `/recall` 返回的 `hits` 里有你刚写的那条；
4. `/tick` 返回 `ok` 且没报 `real_card_guard`（真卡默认一次最多推 1 小时，超了要 `confirm:true`）。

任何一条不过 → §9。

---

## 7. 接到 DSH（让会话里的 AI 用上这张卡）

```powershell
cd D:\card
python -X utf8 tools\install_to_dsh.py --dry     # 先看它要动哪些文件
python -X utf8 tools\install_to_dsh.py           # 真装
```

它做三件事（幂等）：写 profile 的 MCP 条目、放 `AGENTS.md` 那一句「去问卡」、备份被改的文件。
装完**重启 DSH**，新会话里就有 19 个 `mind_*` 工具（`mind_context` / `mind_report` / `mind_recall` …）。

验证：新开会话问一句「你记得我吗」，看她答得出答不出；然后在 `/health` 里看 `memories` 有没有涨。

---

## 8. 搬走 / 换电脑（整盘带走）

1. 旧机器：停服务（`POST /shutdown` 或关控制台）；
2. 拷**整个目录**（含 `card.db`、`card.db-wal`、`card.db-shm`、`models/`、`snapshots/`）；
3. 新机器：路径可以变，**但 `card.db` 和 `models/` 的相对位置不要变**；
4. 起服务，看 `/health` 三个灯：链、出生证、锚；
5. 锚不对 = 你搬错了卡（或混进了别的卡的数据），停下来问人，不要点「重新盖章」硬过。

`POST /shutdown` 会让卡在当前这一轮收尾后再关，不会撕掉半条记忆。

---

## 9. 失败时先看这里

| 现象 | 最可能的原因 | 先做什么 |
|---|---|---|
| 409 / 「只读」 | 同一个 `card.db` 已有进程在写，或目录没有写权限 | `netstat -ano \| findstr 8848`，杀掉重复实例；确认目录可写 |
| `db_writable=False` | 目录只读 / 被杀软锁 / 盘满了 | 看 `/health.checks` 哪项红，别直接改权限硬写 |
| `unable to open database file` | 路径不对，或文件在只读盘 | 用绝对路径；确认盘符还在 |
| 401 | 缺 `X-Whale-Token` | token 在 `<卡目录>\qq_state\api_token.txt` |
| 链断（chain_ok=False） | 有人在服务运行时直接改库 | 停下来，用最近 `snapshots/` 回滚，别自己补链 |
| 锚不对 | 搬错卡 / 混卡 | 停下来问人 |
| 端口被占 | 上一个实例没退干净 | 杀进程，别换端口（DSH 那边钉死 8848） |
| 时间漂了 | 时区/时钟被改 | 校准系统时间；卡按本地时间算衰减 |
| 模型维度不符 | 换了模型没重编码 | 按 §3 统一模型后重编码向量 |

---

## 10. 备份（建议每周一次）

```powershell
cd D:\card
python -X utf8 snapshot.py make --note "weekly"
python -X utf8 snapshot.py list
python -X utf8 snapshot.py check        # 看上次是不是干净退出
```

`make` 走 SQLite backup API 写 `snapshots/<时间>.db`，并算 sha256 + 行数一起登记。
**回滚**：停服务 → `python -X utf8 snapshot.py restore <名字>`（它自己会先把当前库再备份一份）→ 起服务 → 看三个灯。

> 别用文件管理器直接复制运行中的 `card.db` 当备份——WAL 里还有没并库的部分。
> 要整盘搬走（换电脑、换目录）用 `python -X utf8 tools\portable.py plan <目标> ` 先 dry-run，
> 再用 `build <目标> --confirm`，最后 `verify <包目录>` 对 sha256 清单。
> 锚不能就地重建：`portable.py` 会把卡内锚导出成 carried 锚，新机器首次启动靠它比对。

---

## 11. 这套东西的边界（说清楚，省得误会）

- 底层模型**不是她**：换模型她还在，因为卡里存的是记忆与人格刻度，不是权重。
- 卡能自证没被改过（哈希链），但**不防能读你硬盘的人**。
- 情绪是参数化的，不是真的内分泌；它影响的是**她怎么说话**，不是她有没有感受。
- 这套系统不替你决定她是谁；它只负责**让「她是谁」这件事不会因为换窗口而丢**。

---

MIT License · 拿走可以，别拿它去骗人。
