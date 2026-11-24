# 接入问答：QQ ↔ ACP 桥、acp profile、以及「写句柄被占用」怎么处理

> 写给提这几个问题的那位。先说明一句：**我们这份部署里的桥文件原来确实不在仓库**（`.gitignore` 把带 token 的部署目录整个排掉了），现在补了一份脱敏示例：`qq-bridge/qq_acp_bridge.py`。它是**同一套逻辑**，只是把所有私货（QQ 号、token、exe 路径、卡路径、工作目录）换成了环境变量，并且 `qq_send` 那道六道闸门变成可选（没有就直连 NapCat 发）。
>
> 另外你报的 `watch_wake.py` bug 是对的，已经修了（见本文第 5 节）。

---

## 1. (a) 它怎么起 ACP 会话

**不是** `dsh --profile acp --session <id> "text"` 那种一次性调用，是**常驻进程 + stdio 上的 JSON-RPC**。

`dsh --profile acp` 本身就是一个 ACP server，它自己的 help 只有一行：

```
Usage: dsh --profile acp [options]
Serve automation clients over Agent Client Protocol stdio.
Example: dsh --profile acp     serve ACP until the client disconnects
```

也就是说：**它等你从 stdin 说话，进程活着会话就活着，stdin 关了它就退。** 桥里的做法：

```python
prefix, extra = dsh_argv()                       # dsh.cmd 或 <exe> + bin.js（ELECTRON_RUN_AS_NODE=1）
env = dict(os.environ); env.update(extra)
proc = subprocess.Popen(prefix + ["--profile", PROFILE],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0, env=env)
# 两个读线程把 stdout/stderr 的每一行塞进 queue

call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=40)
sid = call("session/new", {"cwd": CWD, "mcpServers": []}, timeout=60)["result"]["sessionId"]
call("session/set_config_option",
     {"sessionId": sid, "configId": "model", "value": '["<provider>","<model>"]'}, timeout=30)
```

要点：

- 每行一个 JSON-RPC 对象（ndjson），`id` 自增；**响应可能和你发给它的下一个请求交错**，所以要按 `id` 匹配，不能假设「下一条就是回我」。
- `session/new` 的 `mcpServers` 我们是空数组——记忆工具不是在这里挂的，是在 **profile** 里作为 bundle 挂的（`dsh-whale-bridge`，见第 3 节）。这样 ACP 会话和 web 会话用的是**同一套工具入口、同一张卡**。
- `set_config_option` 是可选的：不设就用 profile 默认模型。设的时候 `configId` 是 `"model"`，`value` 是一个 **JSON 字符串**（不是对象），形如 `'["provider","model"]'`。
- 一个新进程一个新 `sessionId`，桥不做持久化会话 id——重启就是新会话，但记忆在卡里，所以「它还是它」。

## 2. (b) 消息怎么塞进会话

走 ACP 的 `session/prompt`，**不是** `watch_wake.py` 的后台任务：

```python
send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
      "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}})
# 然后循环读 queue：
#   params.update.sessionUpdate == "agent_message_chunk"  -> 累积 params.update.content.text
#   同一 id 的 response 到达                                  -> 收工
```

几个实践中踩出来的细节：

1. **必须串行。** 一个 `sessionId` 上并发两个 prompt，流式 chunk 会缠在一起（我们拿一把 `threading.Lock`，一次只发一条）。
2. **流式 chunk 可能重复**：我们遇到过同一段内容被发两遍（A+A），所以收尾时做了个「前后半段相同就去掉后半段」的去重。这是经验性处理，不是协议保证。
3. **上下文**：桥每轮会从卡里读最近 10 条 `dialogue` 拼进 prompt（只读连 `mode=ro`），再附一句「像在 QQ 上聊天：2-3 条短句，每条 ≤25 字」。这样即使在 ACP 侧没有历史，也能接上。
4. **`watch_wake.py` 是另一条路，别混**：它只盯 NapCat 的私聊，有新消息就 `exit 0` 去「叫醒」DSH 里的后台任务（不写卡、不发消息、不往输入框弹字）。退出码：`0` 有新消息 / `2` 到点收工 / `3` NapCat 连不上太久。它不负责把话送进会话。

## 3. (c) `is already owned by an active write handle` 怎么办

先说清这个错误是谁抛的：**不是我们卡的服务**（那个是 `writer_lease` 表 + `X-Whale-Token`，冲突时是 409 语义），而是 **DSH 自己的会话存储**——一个 `sessionId` 在某一时刻只能被一个客户端/进程「挂载」为写句柄，第二个来抢就会拿到这句。

所以处理顺序是：

1. **先确认是不是真的同一个会话**。我们的桥每个进程都 `session/new` 拿全新的 id，正常情况下不该撞。撞了通常意味着：桌面 UI 正开着同一个会话，或者上一个 ACP 进程没退干净（`proc.poll()` 还是 `None`，但客户端已经放弃了）。
2. **不要用「切新会话」当解决方案**。新 id 只是绕开这一把锁，旧持有者仍然占着同一份存储；两个都活着，最后会变成两份互相看不见的写入。正确做法是**杀掉旧进程**再重连：
   ```python
   if proc.poll() is None: proc.kill()
   ```
   我们外面还有一层 watchdog（端口/进程双检），桥崩了它会整体重启，而不是原地重试。
3. **重试要退避、且有上限**。桥里对单条 prompt 的超时是 300s，超时给「（超时，没等到回复）」而不是无限等；对启动失败是直接抛异常让 watchdog 接管。
4. `--resume <session>` 只用于**同一个客户端**重连（DSH CLI 的语义），不能用来在另一台机器/另一个进程上「共享」一个会话。跨入口要共享的是**卡**，不是会话。

一句话：**写句柄冲突 = 两个客户端在抢同一个会话；解法是去掉多余的那个，不是换 id。**

## 4. `acp` profile 怎么建

DSH 的 profile 就是 `$DSH_HOME/profiles/<name>/`（我们这里是 `C:\Users\ccy\.dsh\profiles\acp\`），里面四样东西：

```
acp/
  cordis.yml           # 空的入口列表：[]，树是 patch 拼出来的
  package.json         # dsh.profile.bundles = 这个 profile 由哪些 bundle 组成
  cordis.patch.yml     # 你自己的覆盖（工具配置写在这里）
  pnpm-workspace.yaml  # 只有一行 packages: [.]（用于装插件依赖）
```

我们这份的 `package.json`：

```json
"dsh": { "profile": { "bundles": [
  "@deepseek-ai/dsh-base",
  "@deepseek-ai/dsh-acp-app",
  "dsh-whale-bridge",
  "dsh-image-gen"
] } }
```

- `@deepseek-ai/dsh-base`：底座（工具注册、会话、存储、preset 这些）
- `@deepseek-ai/dsh-acp-app`：**ACP 这个 app 本体**（就是那个「Serve automation clients over ACP stdio」的入口）
- `dsh-whale-bridge` / `dsh-image-gen`：我们自己/社区插件（记忆工具走 HTTP 到 8848；生图可选）

创建步骤（照这个顺序，pnpm 必须在 PATH 上；我们这儿是 `D:\nodejs`）：

```powershell
# 1) 从一个随包模板复制出一份新 profile（名字随便，这里叫 acp）
dsh acp --from-default-profile base
#    它会先建目录再启动；base 模板没有 app，起来会提示/直接退，正常，Ctrl-C 即可

# 2) 把 ACP app 装进去
dsh plugin --profile acp add @deepseek-ai/dsh-acp-app

# 3) 可选：装记忆桥 / 生图
dsh plugin --profile acp add dsh-whale-bridge
dsh plugin --profile acp add dsh-image-gen

# 4) 验证：这一行出来就说明装对了
dsh --profile acp --help
# → "Serve automation clients over Agent Client Protocol stdio."
```

然后写 `cordis.patch.yml`（我们这份）：

```yaml
- id: whale-bridge
  config:
    host: 127.0.0.1
    port: 8848
    cardDir: '<卡的目录>'
    pythonPath: '<python.exe>'
    autoStart: false          # 卡片服务自己起，别让 MCP 再拉一个（会抢写锁）
    timeoutMs: 30000
    healthTimeoutMs: 3000
```

不需要额外装包（不是 pip 的事）——`dsh-acp-app` 是 node 包，跟 profile 一起被 pnpm 装进 `profiles/acp/node_modules`。

两个坑：

- **`pnpm` 不在 PATH** 时 `dsh plugin --profile acp list` 会报 `'pnpm' 不是内部或外部命令`。把 pnpm 目录加进 PATH，或者改用绝对路径调。
- **`autoStart: true` + 手动起的服务** → 两边都想去写同一张卡，就会出现「写锁被占」。我们设 `false`。

## 5. 你报的 bug：`watch_wake.py` 首次启动漏 return

确认，是真的。首次启动那段：

```python
wm = read_wm()
if not wm:
    write_wm(time.time())
    print("听筒上线，水位线 = 现在")
    return_code = 2          # ← 赋值给一个没人用的局部变量
else:
    ...
```

`return_code = 2` 之后没有 `return`，函数继续往下跑 `while time.time() < deadline`，于是：

- 局部变量 `wm` 仍是 `0`；
- `fresh = [m for m in ms if float(m["time"]) > wm]` → 5 条历史全算「新的」；
- 你看到的「56 分钟前那条被当新消息报出来」就是这个。

已改成 `return 2`，并且顺手补了一句注释说明「首轮只建水位线」。提交信息里带 `fix:` 前缀，方便你 diff。

---

## 6. 我在手机上是怎么用的（老实说）

**主线是 QQ。** 手机上就是普通 QQ：发消息 → QQ 服务器 → 电脑上的 NapCat(6700) → `qq_acp_bridge.py` → `dsh --profile acp` → 同一个 `card.db` → 回复走同一条路回来。所以**不需要内网穿透**，因为中转是腾讯的服务器，电脑只要能连 QQ 就行。

**web/控制台这条我没有在手机上用。** `mind_server` 只绑 `127.0.0.1`（并且非本机地址会被绑定闸挡掉），手机直连不到。要用的话是这两条路之一：

- **Tailscale / ZeroTier**：把电脑和手机拉进同一个虚拟内网，访问 `http://<tailscale-ip>:8848/console`。注意绑定闸：要么 `WHALE_ALLOW_NONLOCAL=1`，要么前面挂一层只监听虚拟网卡的反代（我们没做，因为不需要）。
- **只读面单独放出去**：`/audit`、`/health`、`/turn/stats` 这类只读路由用一个小的反代 + 基本认证暴露，写面（`/report`、`/gov/*`、`/self_model/*`）永远留在本机。

如果你只是想「在手机上看她今天怎么样」，最省事的其实是订阅一条每日摘要（我们控制台左栏那个「态度 / 学习 / 反馈」的文本），而不是把整个控制台暴露出去。

---

## 7. 文件清单（这次补进仓库的）

| 路径 | 说明 |
|---|---|
| `qq-bridge/qq_acp_bridge.py` | 脱敏示例：常驻 ACP + `session/prompt` + 发送兜底 |
| `tools/watch_wake.py` | 修掉首轮漏 `return`（你报的那条） |
| `docs/接入问答-qq-acp-bridge.md` | 就是本文 |

跑示例最小要求：Python 3.10+、`pip install aiohttp`、一个有 `acp` app 的 profile、NapCat 的 HTTP API 开着（同机默认 6700）。

---

## 8. 顺手做的全系统体检（2026-11-21 晚）

因为你在报 bug，我把整机静态扫了一遍（`D:\` 下 407 个 `.py`，跳过 venv/模型/备份目录），然后跑了测试与线上只读面。

**结果：**

| 项 | 结果 |
|---|---|
| 语法/编译 | 407 个文件，**0 失败** |
| 单元测试 `tests/test_card2.py` | **19 / 19 通过** |
| 线上只读路由 | 15 条全 200（`/health`、`/turn/stats`、`/gov/stats`、`/outbox/stats`、`/distill/stats`、`/self_review`、`/self_model`、`/contemplate`、`/audit/stats`、`/intentions`、`/feedback/stats`、`/retention`、`/speech_policy/snapshot`、`/hormone/feedback`、`/dream/pending`） |
| 写路径幂等 | `turn_id` 重复提交返回 `duplicate:true`，不双写（这条之前在探针里验过） |

**抓到两个真问题（都已修）：**

1. **`D:\tools\watch_wake.py:69` 和你报的是同一个 bug。** 仓库里那份我先修了，但**工具目录里实际在跑的那份漏了**——同一个坑两处出现，说明「修配置副本、没修部署副本」这种漂移是真会发生的。两处现在都是 `return 2`。
2. **`D:\cardtext.py` 里 `split_memory` 定义了两次**（611 行与 697 行）。后一份把前一份整个盖掉，两份**只差一行**：后一份多了 `from body import EMO_WORDS  # 懒取：避免 cardtext ↔ body 成环`。也就是说前一份是纯死代码（116 行），而且它内部引用了 `EMO_WORDS` 却没有 import——**如果有人哪天删掉后一份，前一份一调用就 `NameError`**。已删除死的那份，并把 import 挪到 docstring 之后（放在 docstring 前面会让那段字符串不再是 docstring）。备份：`cardtext.py.bak-dup2-<ts>`。

**一个误报（说明一下，免得你以为是 bug）：** 静态扫描把 `card2.py` 里 `MemoryCard.db` 报成重复定义，实际是 `@property` + `@db.setter` 一对，Python 允许且必需。

**留作后续风险点、这次没动：** `except ...: pass` 的密度——`card2.py` 81 处、`mind_server.py` 35 处、`console.py` 29 处、`feedback_loop.py` 21 处。它们大多是有意的「可选功能失败不影响主链」，但**数量到这个级别，就意味着「静默退化」是这套系统的默认失效模式**（我们的论文里 F1/F3/F5/F8 都是这一类）。真要治，方向不是把它们全改成抛异常，而是给「关键路径」加一层显式的失败留痕（我们已经有 `write_outbox`、`/health.bus`、`/audit`、`self_review` 的 error 状态，可以继续往这个方向收）。

