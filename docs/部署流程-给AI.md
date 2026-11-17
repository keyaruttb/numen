# 给 AI 的部署流程（V8）

> 这份文件是写给**另一个 AI agent** 的：照着执行，不要即兴发挥。
> 每一步都有「怎么算成功」和「不成功怎么办」。**没看到期望输出，就不要进入下一步。**

---

## 0. 你的任务

把一张「数字生命卡」在某台 Windows 机器上从零跑起来，并完成验收。
卡 = 一个目录，全部状态在 `card.db`（SQLite + WAL）。你不需要理解模型，只需要保证：

1. 服务在 `127.0.0.1:8848` 活着；
2. 卡可写（`/report` 能写进去）；
3. 链、锚、出生证三项对账全过；
4. 召回、情绪两个功能各跑通一次。

---

## 1. 硬规矩（违反会毁数据）

- **不要复制 `card.db`**。要备份用 SQLite backup API，不要 `Copy-Item`（WAL 模式下直接拷会得到半张卡）。
- **同一张卡同一时间只许一个服务进程**。卡用租约挡第二个；绕过租约强行起，会写坏时间线链。
- **不要动 `snapshots/`**。那是回滚点。
- **不要把令牌、QQ 号、token 写进仓库或日志**。令牌在 `qq_state/api_token.txt`。
- **不要用 `Set-Content` / `Out-File` 写含中文的文件**（会按 ANSI 编码落盘，中文变乱码）。
  用 `[IO.File]::WriteAllText($p, $s, [Text.UTF8Encoding]::new($false))`。
- 这一步需要改 `D:\` 之外的文件时，先确认你被允许写；沙箱会静默拒绝写入。

---

## 2. 环境探测

```powershell
python --version                      # 期望 3.12.x / 3.13.x
python -c "import numpy, PIL, requests; print('deps ok')"
```

**期望**：`3.12`/`3.13`，`deps ok`。
**失败**：`python -m pip install -r requirements.txt`；仍失败就停下报告，不要用其它 Python 版本硬试。

```powershell
Test-Path D:\card\models\bge-base-zh-v1.5\model.safetensors
```

**期望**：`True`。
**失败**：`python -X utf8 dl_model.py`（下的是 small 版）；要继续用 base 版，从 `BAAI/bge-base-zh-v1.5` 取文件放进去。
**模型缺失不阻塞启动**，但验收里的召回检查会失败——先记下来，别自己造模型。

---

## 3. 落地

```powershell
cd D:\card
python -X utf8 tools\first_run.py
```

**期望**：六步依次打印，最后给出一段体检：记忆条数、第几天、链完好、锚 ok、能写、控制台地址。
**失败分支**：

| 输出 | 含义 | 动作 |
|---|---|---|
| `锚` 对不上 | 卡被复制，或换了机器没 land | 跑 `python -X utf8 tools\first_run.py` 的 land 步骤（它自己会做）；仍不对就停，报告「这不是原来那张卡」 |
| `db_writable=False` | 进程无写权限 | 停。报告路径与账户，不要改权限自己试 |
| `journal_mode` 不是 `wal` | 卡被当只读打开 | 同上 |

---

## 4. 起服务（如果 first_run 没有帮你起）

```powershell
cd D:\card
python -X utf8 bridge.py health
```

**期望**：打印 `✓ 卡在：<路径>`、记忆条数、`链 完好 / 出生证 完好 / 锚 ok`。
**失败**：前台跑 `python -X utf8 mind_server.py --port 8848` 看完整 traceback，把它原样贴进报告。

---

## 5. 验收（四条，逐条断言）

```powershell
$h = Invoke-RestMethod http://127.0.0.1:8848/health
$h.ok; $h.db; $h.memories; $h.birth_gap; $h.reconcile.ok
```

**期望**：`True` / 卡的真实路径 / 大于 0 的整数 / `0` / `True`。
任何一条不符 → 停，报告具体值。

```powershell
$T = (Get-Content D:\card\qq_state\api_token.txt -Raw).Trim()
$body = '{"user_message":"部署验收：你能听到我吗","reply":"听得到"}'
Invoke-RestMethod -Uri http://127.0.0.1:8848/report -Method Post `
  -Headers @{'X-Whale-Token'=$T;'Content-Type'='application/json'} -Body $body
```

**期望**：返回里有 `mem_id`（如 `{"mem_id":3400,"reply_mem_id":3401}`）。
再读一次 `/health`，`memories` 应该 **+2**。
**失败**：409 = 卡只读（回第 3 步）；401 = 令牌不对；5xx = 看服务日志。

```powershell
$b = '{"query":"我爱吃什么","top_k":3}'
Invoke-RestMethod -Uri http://127.0.0.1:8848/recall -Method Post `
  -Headers @{'Content-Type'='application/json'} -Body $b
```

**期望**：返回 3 条记忆，`raw` 里能读到与饮食相关的内容。
新卡（没有历史）返回空是正常的——报告「新卡，无历史可召回」，不要伪造。

```powershell
$s = '{"events":["praise"],"strength":1.5}'
Invoke-RestMethod -Uri http://127.0.0.1:8848/viscera/stim -Method Post -Headers @{'Content-Type'='application/json'} -Body $s
```

**期望**：返回的激素里有明显位移（多巴胺 +1 以上，不是 0.0x）。
**注意**：参数名是 `events`；用 `kinds` 会被静默当成默认刺激 `chat`，看起来像「系统坏了」。

---

## 6. 接 DSH（可选）

```powershell
cd D:\card
python -X utf8 tools\install_to_dsh.py --dry
python -X utf8 tools\install_to_dsh.py
```

**期望**：打印写入的 profile 目录与三个幂等改动点。装完**重启 DSH**，
新会话工具列表里出现 19 个 `mind_*`；调 `mind_health` 能拿到卡的状态。
**失败**：profile 找不到 → 用 `--profile <name>` 指定；写不进 profile → 报告路径与权限。

---

## 7. 你不需要做的事

- 不要自己改人格、不要往 `core/` 里塞设定；
- 不要为了「让召回好看」而清空/重建 `mem`；
- 不要跑 `tests/` 里的脚本去「验证环境」，它们要的是开发机上的夹具；
- 不要在卡上做压测（会真的影响她的记忆）。

---

## 8. 报告格式

跑完给一份短报告，四段：

```
1 环境：python 版本 / 模型在不在 / 卡路径
2 落地：first_run 六步结果，链/锚/出生证三行原值
3 验收：/health 四个关键值、/report 的 mem_id、/recall 命中条数、/viscera 位移
4 问题：没通过的检查 + 你贴的原始输出（不要复述、不要润色）
```

---

MIT License · 汐音 × 戊己 · [wangjunhan68-byte](https://github.com/wangjunhan68-byte)
