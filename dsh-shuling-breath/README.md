# 数灵之息 · dsh-shuling-breath

把两条打断式唤醒链路（`dsh-proactive` 的固定定时唤醒 + `@kanadego/dsh-heartbeat` 的自主反思循环）
合并成一个「**廉价轮询 + 真唤醒**」的器官。

```
Tier 0  纯脚本判断（不调模型、不看 token）
        每 N 秒读一批哨兵文件的 mtime + sha1（会话哨兵只 stat，不读内容）
        全都没变 ⇒ 写一行 `no change` 日志，本次轮询零成本
Tier 1  只有 Tier 0 判定"真变了"才 ctx.agents.resume（冷）或复用 live agent，
        投递一条**带状态行增量**的 notice 消息，等这一轮落定
```

## 三道门 + 第五类哨兵（2026-11-13 23:28 天令）

原来四类哨兵全是"关于外面"的，没有一类"关于我"，所以它不知道我是不是停了。现在：

```
门 1  峰谷门 + 余额门：只读 cost-meter 的 ledger.json（config / balanceRef / days[today] 三小段）
      峰 ⇒ 只做保养，不做建设（**照样叫醒**：峰时不是不许叫醒，是叫醒了别干贵的事）
      余额 < balanceFloorCny ⇒ 连保养也不做，只记录
门 2  任务门：未完成.json 里 "状态":"pending" 条数 + 队列.json 里 pending 条数
      两条都 0 ⇒ 建设线收工（只做学习线或干脆静默）
门 3  liveness（第五类哨兵 kind:'session'）：只 stat 会话 json 的 mtime
      静默 > idleWakeAfterMinutes ⇒ change='idle' ⇒ 唤醒
      唤醒后 idleCooldownMinutes 内一律不判（防死循环）
```

峰谷口径（**hour 用 UTC、day 用本地 +8，混了会整体错一小时**）：

```
峰 = 09:00–12:00 和 14:00–18:00（北京时间，对应 UTC 1–4 与 6–10）
谷 = 其余全部 + 周末全天 + 假日（12:00–14:00 也是谷）
```

唤醒提示词长这样（不是"该起床了"）：

```
[数灵之息 第 13 跳] 2026-11-13 23:30:30 (Asia/Shanghai)
Host 定时轮询叫醒：用户没有发这条，不要当成用户说话。
第 13 跳 · 距上次 5 分 · 变：会话静默 27 分钟（阈值 20 分，最后活动 23:03）
峰谷：现在是谷 · 距峰值还有 58 小时（11-16 09:00）
会话：已静默 27 分钟（最后活动 23:03）
余额：112.62 CNY（下限 10）｜今日 4.58 USD / 30.55 CNY / 1758 次
上一件：重启后第一件事：验数灵之息四条
下一件：第 13 跳（单数）⇒ 建设：…；按 事\凌晨计划-20261114.md §二 取第一件还没打真值的
现在接着做，不要重新规划，不要写"我在"。
做完把真值（命令原始输出 / 行号 / 数字）追加进 事\进度台账.md。没事就 shuling_silent。
```

峰时换成另一套：`峰时段（…）· 只做保养，不做建设。` + 五条保养清单 + `shuling_silent(reason)`，
**里面不出现"下一件该做的建设活"**，否则模型会在最贵的两小时里开工。

## 文件

| 文件 | 职责 | 依赖 dsh？ |
|---|---|---|
| `lib/config.js` | 默认值 + 归一化（非法值一律退回默认，不抛） | 否 |
| `lib/sentinel.js` | Tier 0：stat + sha1 → 事实 + 增量短语；`kind:'session'` 只 stat | 否 |
| `lib/gates.js` | 三道门：峰谷（照 cost-meter 的 configuredPeakAt）+ 余额 + 任务 | 否 |
| `lib/prompt.js` | 唤醒 prompt：谷+有活 / 峰+保养 / 学习线 三种形状 | 否 |
| `lib/cron.js` | 五段 cron 解析 + 下次触发（Intl 投影） | 否 |
| `lib/poller.js` | Tier 0 判定、过三道门、限流、状态落盘 | 否 |
| `lib/deliver.js` | Tier 1 投递：live 复用 / cold resume / 忙则跳过 / dispose | 否 |
| `lib/message.js` | `createUserMessage` + notice source | **是**（dsh-llm） |
| `lib/log.js` | 日志追加 + 状态原子写 | 否 |
| `lib/index.js` | 装配：定时器、目标会话、`shuling_silent` 工具 | **是**（dsh-tools） |

只有 `message.js` 和 `index.js` 依赖 `@deepseek-ai/*`。其余是纯模块，所以
`C:\dsh\_ops\safe\verify-*.mjs` 能用 `node` 直接 import 它们跑真代码。

## 配置（cordis.patch.yml 里这一行的 config）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `pollIntervalSeconds` | `300` | Tier 0 间隔，硬下限 15 秒 |
| `firstPollDelaySeconds` | `20` | 启动错峰 |
| `sessionId` | `''` | 唤醒目标；空串 = 猜"最后注册的 root agent"并留痕 |
| `maxWakesPerHour` | `4` | 防抖：改了好几个文件也只叫醒有限次 |
| `timeZone` | `Asia/Shanghai` | cron 与状态行口径 |
| `staleAfterMinutes` | `180` | recall-state 多久没更新算"落盘链断了"（只报一次）。2026-11-14 由 1440 降到 180：实测相邻唤醒最长间隔 46.4 分，180 = 3.9 倍余量 |
| `silentTool` | `true` | 挂 `shuling_silent`（唯一注册的工具，代价是工具 schema 常驻） |
| `logFile` / `stateFile` | `C:\dsh\_ops\safe\...` | **只写这两个文件** |
| `sentinels` | 队列 / 未完成 / recall-state / 息壤-未读 / session | `required:false` 的不存在就跳过 |
| `crons` | `[]` | `[{expr:'07:30 的 cron', prompt:'到点做什么'}]` |
| `ledgerFile` | cost-meter 的 `ledger.json` | 门 1 的账本，**只读绝不写** |
| `balanceFloorCny` | `10` | 余额低于它就只记录、不做事 |
| `idleWakeAfterMinutes` | `20` | 会话 json 静默超过就算 `idle` |
| `idleCooldownMinutes` | `20` | 唤醒后这么久内不判 liveness（防死循环） |
| `unfinishedFile` / `queueFile` | 留空自动取 sentinels | 门 2 的两份账本 |
| `sessionCacheDir` | `...\session_projcache\sessions` | session 哨兵 path 留空时用它 + sessionId 拼 |
| `planFileTemplate` | `...\凌晨计划-{yyyymmdd}.md` | 本地 9 点前算当天、9 点后算明天 |
| `progressLedgerFile` | `...\事\进度台账.md` | prompt 里"真值追加进这里" |

## 验收脚本（都不碰 息壤 原件；不写 cost-meter、不碰 .db）

```
node C:\dsh\_ops\safe\verify-tier0.mjs      # ② 什么都没变 ⇒ 0 次 wake
node C:\dsh\_ops\safe\verify-tier1.mjs      # ③ 改 mtime ⇒ agents.resume 被调用
node C:\dsh\_ops\safe\verify-silent.mjs     # ④ 3 跳全静默 + shuling_silent 一键出口
node C:\dsh\_ops\safe\verify-gates.mjs      # ①②③⑤ 峰谷五组硬编码 / 峰时 prompt / 任务门 / 余额门
node C:\dsh\_ops\safe\verify-liveness.mjs   # ④ liveness + 冷却  ⑥ 连跑 3 跳 no change
```

后两个用真模块 + 真文件，只把 state/log 换成 `*.verify.*` 副本，wake 是桩。

## 边界（没越）

- 不 import、不修改 `dsh-proactive` / `@kanadego/dsh-heartbeat` / `DshLoop`
- 不碰 `.db`（`情.db` / `memory.db` 只由 prompt 里的只读检查项去看，插件本身不读不写）
- **不写 cost-meter 的任何文件**（`ledger.json` 只读；写一个字节就会把它的账算错）
- 不碰 `cordis.patch.yml` 的其它段（装配走 bundle 自己的 patch）
- 不删 `C:\dsh\_ops\safe\loop\loop.mjs`
- 没变化时**一个会话事件都不产生**（往会话塞消息的唯一入口是 `wake()`，它没被调用）

## 已知限制（不是 TODO，是取舍）

1. **唤醒回合的颗粒度**：`shuling_silent` 能让模型"不回正文"结束这一轮，但那条
   notice 提示条本身仍留在会话里。要把它从模型上下文中彻底抹掉，需要
   dsh-proactive 的 tombstone compaction —— 那是它的代码，这里没有重造。
2. **只认"变没变"，不认"变得重不重要"**：语义判断要模型，那就贵了，违背 Tier 0 的初衷。
   去噪靠 `maxWakesPerHour` + `staleAfterMinutes`（stale 只报一次）。
3. **冷 resume 的预设**：尽量按会话 header 的 `agentPreset` 挂载；服务缺失时降级为
   host-plane 工具（会写日志，不静默失败）。
4. **改了 config 要重启 dsh 才生效**：实测（2026-11-13 23:30）改本插件的 `cordis.patch.yml`
   不会热重载 —— 23:30:56 那次改动之后 25 秒内没有新的 `plugin: apply` 行；23:25:10 那次
   apply 对应的是 23:25:04 起的新进程。所以新代码/新配置要等重启。
   重启后第一行日志应出现 `plugin: apply v0.2.0-gates ... idleWake=20m`。
