# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
"""
跨系统协议总线（SYNAPSE-1024 · V1.1）

目的
----
闭环原本在 ``feedback_loop._apply`` 里硬编码直连激素/记忆/印象/动机/意向各模块。
本模块在不删既有函数的前提下，立一层标准内部协议：

  - 统一信号信封 ``Signal``；
  - 标准事件目录（``EV_*``）；
  - 适配器接口：新系统接进来 = 写一个 handler + 登记订阅，不改闭环；
  - 调度表：按 ``order`` 顺序分发；单个适配器软失败（记日志），不拖垮其余；
  - 降级：总线不可用 / 无适配器时，闭环仍可走原有内联路径。

设计红线（对齐既有原则）
------------------------
- 总线只做路由与溯源，不替任何模块做决策；不写人格锚、不删记忆。
- 适配器内部复用闭环既有私有方法，本层不重写业务逻辑。
- 事件载荷只放标量与标识；跨轮复杂状态由各模块自己落库。
"""

import time
import traceback

# ---------------------------------------------------------------------------
# 事件目录（首版）
# ---------------------------------------------------------------------------

# 结算后广播：r / sign / amp / severity / kind / mem_id / dialogue_id。
# 现有五步（激素、记忆突触、行为印象、评价印象、动机、意向）都是它的订阅适配器。
EV_REWARD = "REWARD_APPLIED"

# 外部标量落差直注情绪引擎：payload = {rpe, scenario, intensity, source}。
# 引擎适配器消费；引擎不在线时静默降级，仍由四慢激素层兜底。
EV_RPE_PULSE = "RPE_PULSE"

# 激素扰动（apply_r 通路）。
EV_HORMONE_TAP = "HORMONE_TAP"

# 记忆 / 突触结构调整。
EV_MEM_REINFORCE = "MEM_REINFORCE"
EV_MEM_WEAKEN = "MEM_WEAKEN"

# 行为模式印象（只描述不评价）。
EV_IMPRESSION_OBSERVE = "IMPRESSION_OBSERVE"

# 评价印象慢磨（预期 / 安全感 / 态度 / 联想池）。
EV_AFFECT_UPDATE = "AFFECT_UPDATE"

# 动机：正向满足 / 负向勾起改进欲。
EV_DRIVE_BUMP = "DRIVE_BUMP"
EV_DRIVE_ITCH = "DRIVE_ITCH"

# 行为意向候选（approach / avoid）。
EV_INTENTION_PROPOSE = "INTENTION_PROPOSE"

# ---- 预留事件（当前无适配器，登记目录备查） ----
EV_SCENE_ENTER = "SCENE_ENTER"          # 进入某场景 → 触发印象预调
EV_PERSON_ID = "PERSON_IDENTIFIED"      # 对话主体识别 → person 维度印象
EV_FACT_SUPERSEDED = "FACT_SUPERSEDED"  # 事实被推翻 → 隐式 disconfirm

ALL_EVENTS = (
    EV_REWARD, EV_RPE_PULSE, EV_HORMONE_TAP, EV_MEM_REINFORCE, EV_MEM_WEAKEN,
    EV_IMPRESSION_OBSERVE, EV_AFFECT_UPDATE, EV_DRIVE_BUMP, EV_DRIVE_ITCH,
    EV_INTENTION_PROPOSE, EV_SCENE_ENTER, EV_PERSON_ID, EV_FACT_SUPERSEDED,
)


class Signal:
    """一条标准内部信号。

    payload: 事件专属标量 / 标识；ctx: 共享上下文（now/db/card/mem_id...）；
    meta: 溯源（severity, trust_ok, source, 力度, 来源模块）。
    """

    __slots__ = ("event", "source", "ts", "payload", "ctx", "meta")

    def __init__(self, event, source="bus", payload=None, ctx=None, meta=None):
        self.event = event
        self.source = source
        self.ts = time.time()
        self.payload = payload or {}
        self.ctx = ctx or {}
        self.meta = meta or {}

    def get(self, key, default=None):
        return self.payload.get(key, default)

    def to_dict(self):
        return {
            "event": self.event,
            "source": self.source,
            "ts": round(self.ts, 3),
            "payload": self.payload,
            "ctx": {k: v for k, v in self.ctx.items() if k != "card"},
            "meta": self.meta,
        }


class Adapter:
    """一个可订阅若干事件的处理器。

    handler(sig: Signal) -> None | dict
    内部必须自己 try/except；总线再兜一层软失败。
    """

    __slots__ = ("name", "events", "handler", "enabled", "order")

    def __init__(self, name, events, handler, order=100, enabled=True):
        self.name = name
        self.events = tuple(events)
        self.handler = handler
        self.order = order
        self.enabled = enabled

    def subscribes(self, event):
        return event in self.events


class Bus:
    """内存内部分发总线。

    首版不持久化未消费信号（闭环是同步结算，不存在异步堆积）；
    预留 ``queue`` 接口供日后接延迟归因 / 离线回放。
    """

    def __init__(self, card=None, logger=None):
        self.card = card
        self._adapters = []
        self._logger = logger

    # ---- 注册 ----
    def add(self, name, events, handler, order=100, enabled=True):
        ad = Adapter(name, events, handler, order=order, enabled=enabled)
        self._adapters.append(ad)
        self._adapters.sort(key=lambda a: a.order)
        return ad

    def register(self, adapter):
        self._adapters.append(adapter)
        self._adapters.sort(key=lambda a: a.order)
        return adapter

    def _subscribers(self, event):
        return [a for a in self._adapters if a.enabled and a.subscribes(event)]

    # ---- 分发 ----
    def dispatch(self, event, payload=None, ctx=None, meta=None, source="bus"):
        """按调度顺序把信号分发给订阅适配器。返回本次执行的适配器条数。"""
        sig = Signal(event, source=source, payload=payload, ctx=ctx, meta=meta)
        ran = 0
        for ad in self._subscribers(event):
            try:
                ad.handler(sig)
            except Exception as e:
                self._warn("适配器 %s 处理 %s 失败: %s" % (ad.name, event, e))
                self._warn(traceback.format_exc())
            ran += 1
        return ran

    # ---- 观测 / 调试 ----
    def snapshot(self):
        return [
            {"name": a.name, "events": list(a.events),
             "order": a.order, "enabled": a.enabled}
            for a in self._adapters
        ]

    def set_enabled(self, name, enabled):
        for a in self._adapters:
            if a.name == name:
                a.enabled = bool(enabled)
                return True
        return False

    # ---- 日志 ----
    def _warn(self, msg):
        if self._logger is not None:
            try:
                self._logger(msg)
            except Exception:
                pass