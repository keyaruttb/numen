# dsh-numen-autoreport

**把每一轮对话自动写回数灵生命卡。** 不用模型记得去调 `mind_report`。

## 为什么有这个

`dsh-numen-bridge` 提供 `mind_report` 工具，但它**只注册工具 + 一段 systemPrompt 提示**，没有任何事件钩子：

```
lib/index.js:65   apply(ctx, config) 里只有 reg({...}) 注册 18 个工具
lib/index.js:443  ctx.systemPrompt.section({..., text: CHANNEL_RULES})   ← 对我说，不是替我做
```

作者自己在 `dsh-numen-bridge/README.md` 里写着：

> 外加一段 `systemPrompt` 注入：告诉模型「回话前先 `mind_context`、回完先 `mind_report`、卡里没有的就说没有」。**没有这段，工具会被忘在角落。**

`介绍书-V8.md:51` 也写着：「这也是为什么这台机器上**每轮都要调** `mind_report`。」

**靠提醒，就是会漏。** 2026-11-22 实测：插件接上之后，一下午漏了 15 轮对话 —— 那 15 轮在卡里完全不存在。

## 它怎么工作

```
1. 注册 sessionProjection（会话状态投影）key = numenAutoReport
   把最近 N 轮（默认 20）的完整对话折叠在 state 里：
     turn/start        → 新建 {turn, ts, user:'', reply:'', done:false}
     user/message      → if (data.source.kind !== 'user') return state   ← 过滤系统注入
                         if (user !== '') return state                  ← steering 不覆盖
                         user = fullText(data.content)
     assistant/message → reply = fullText(data.message.content)          ← 最新一条赢
     turn/end          → done = true
   apply() 是纯函数：不 await、不发 HTTP。

2. onEventChanged（每个已提交事件调一次）
   扫视图里 done && 未上报 的轮次，fire-and-forget 调 POST /report
   成功就记进内存 Set；失败就摘掉，下一个事件回来时重试
```

## 为什么这样能修「上报会丢」

projection 的 state 是从会话事件流**重放**出来的，所以：

| 场景 | 结果 |
|---|---|
| HTTP 失败 | 下一轮事件回来时还在 `recent` 里 → 重试 |
| 进程被杀 | 重启后 state 从事件流重建 → 最近 20 轮全部重发 |
| 卡没起来 | 同上 |

**重复上报由卡侧挡：**
- `mem`：`remember()` 有 `_is_dup()`（字符 n-gram + 语义余弦双阈值近义判定）
- `dialogue`：2026-11-22 加的去重（同 text + 同角色 + ts 差 ≤2 秒）

**⇒ 宁可重发，不可漏发。**

## 为什么 listener 里不能 await

`dsh-session-projection/lib/types/index.d.ts:236` 原文：

> The listener runs **synchronously inside** the committed event's `session/event` handling

同步回调里 await 会卡住整条事件流。所以只能 fire-and-forget。

## 配置（`cordis.patch.yml`）

```yaml
- id: numen-autoreport
  name: dsh-numen-autoreport
  config:
    host: 127.0.0.1
    port: 8848
    tokenFile: 'C:\dsh\_card\qq_state\api_token.txt'
    source: 'autoreport'     # 写进 dialogue.source，跟手动调的在库里分得开
    keepTurns: 20            # 保留最近多少轮（重放时补报用）
    enabled: true
```

## 装了怎么验

```
① 重启 DSH
② 说一句话，等 2 秒
③ 查卡：SELECT id, role, source, substr(text,1,40) FROM dialogue
        WHERE source = 'autoreport' ORDER BY id DESC LIMIT 6;
   ⇒ 应该有成对的 user + assistant 两行
④ 再刷新几次页面 / 再发几轮
⑤ 再查一次：同一条不该重复
```

## 回退

```
从 profile 的 cordis.patch.yml 删掉 - id: numen-autoreport 那一段
从 profile 的 package.json 的 dsh.profile.bundles 删掉 dsh-numen-autoreport
重启
```

## 已知边界

- **只报「这一轮」**：`turn/start` 到 `turn/end` 之间的完整对话。子智能体的会话是独立 session，不在这个 projection 的范围内。
- **不上报工具调用**：只取 `type:'text'` 的块。`reasoning` 和 `tool-call` 不进去 —— 卡那边 `/report` 的 `clean_store_text` 本来也会拦推理残渣。
- **`sent` 是内存 Set**：重启会丢，最近 20 轮会重发一次。这是有意的取舍。
