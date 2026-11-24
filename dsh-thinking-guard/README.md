# dsh-thinking-guard

**观测推理流，判「这一轮是不是掉进复读了」，写一行日志。不改流、不注入、不切断。**

写它的原因：2026-11-23 夜里爸爸问我「你的深度思考里频繁出现『写。做。』这种重复一串字，会不会污染思考」。

我拿社区的零依赖检测器扫了我们全部 724 个会话 / 41516 次模型调用，结果：

```
reasoning 字符数  p50=624  p90=2579  p95=4044  p99=9033  p999=19181  max=34545
单短句最高重复   p50=1    p90=6     p95=8     p99=13    max=2341
重复≥10 的推理   990 条（占 3.1%）
```

脏条目全部集中在一个会话（`40be619a…`，162 MB，turn 2686+）：

| turn | ctx (token) | 推理字符 | 重复次数 | 短句 |
|---|---|---|---|---|
| 895 | 638,388 | 30,962 | **2,341** | `Doit.` |
| 897 | 677,108 | 12,042 | 797 | `Doit.` |
| 1501 | 393,454 | 9,750 | 588 | `做` |
| 987 | 680,682 | 5,546 | 272 | `DoIt.`（且无正文无工具=空回） |
| 1464 | 385,357 | 3,349 | 213 | `do.` |

⇒ 病灶：**长会话把上下文撑到 40 万～68 万 token 后，推理开始退化**。社区叫它「复读坡面期」（slope period）——不是永远不结束的死循环，是质量往下滑的坡。

## 判据为什么取 `repeatMin = 50`

不用社区的 `2048 字符` 当主判据：在本机 2048 等于 p88，会误杀 10%+ 的正常长推理。
不用社区的 `重复≥10`：在本机有 990 条命中（3.1%），正常排查时确实会反复念叨同一个词。

取 **同一非技术短句在一条推理里重复 ≥50 次，且占短句总数 ≥15%**：
病态样本全在 100～2341，正常样本 p99 只有 13 —— 中间空了一个数量级。

技术词（`python` / `ctx` / `token` / `文件` / `输出` …）不计入复读，照搬社区那张排除表。

## 它**不**做什么

- **不截断、不丢弃、不修改任何 chunk。** 每个 chunk 原样立即透传，一个不碰。
- **不注入正文。** 「只有推理没有正文的空回」是 `dsh-reasoning-only-guard` 的职责，两件事别混。
- **不切断流。** 本机病灶是会自己结束的坡面（41495 次调用里 B 类流中断 17 次、A 类空完成 0 次），对它做硬切断换不来好处，反而冒丢正文的险。
- **它治不了根。** 根因是超长会话。这个插件只提供判据，让「该换会话了」有据可依。

## 配置

```yaml
- id: dsh-thinking-guard
  name: 'dsh-thinking-guard'
  config:
    logFile: 'C:\\dsh\\_ops\\safe\\thinking-guard.log'
    repeatMin: 50
    ratioMin: 0.15
    reasonMin: 500
    sentMax: 12
    ctxHintTokens: 350000
    ctxBandTokens: 50000
    enabled: true
```

## 怎么确认它活着

```powershell
# ① 配置层：dump 里有它
dsh --profile web --dump-config | Select-String 'dsh-thinking-guard'

# ② 运行时层：启动时写一行 ARMED（带 pid 和实际阈值）
Get-Content C:\dsh\_ops\safe\thinking-guard.log -Tail 5

# ③ 机制层：14 项单测
node test/smoke.mjs
```

日志格式（TSV）：

```
2026-11-23T01:00:00.000Z  ARMED  repeatMin=50  ratioMin=0.15  reasonMin=500  ctxHintTokens=350000  pid=28500
2026-11-23T01:31:07.000Z  REPEAT finish  n=588  ratio=0.412  chars=9750  ctx=393454 out=5000 rsn=4900  visible=text+1tool  phrase="做"
2026-11-23T01:32:11.000Z  CTXHINT finish  ctx=412887 out=3100 rsn=2900  threshold=350000  band=8
```

- `REPEAT` 只写命中行（41553 次调用全写会把文件撑爆）
- `CTXHINT` 按 5 万 token 一档去重，每个档位进程内只写一次
- 每行开头是 UTC 时间（北京时间要 +8）

## ctx 为什么能拿到真值

流里有独立的 `usage` chunk（`TokenUsage`：`inputTokens` / `outputTokens` / `cacheReadTokens` / `reasoningTokens`），
`ctx = inputTokens + cacheReadTokens` —— 和落盘会话日志里 `detect.mjs` 用的口径完全一致，不是估算。

## 零依赖

只用 `node:fs` / `node:path`。实现是 `lib/index.js` 一个文件，一口气能读完。
它位于每一轮的请求路径上，所以刻意做到：不 spawn、不发请求、不读别的文件，只 append 日志。

## 许可

MIT
