# -*- coding: utf-8 -*-
# 签名：c861f3f3b8d72eaf | 小鲸鱼（WG-5E70BCC9） | 2026-11-20 18:40 | 落卡：真卡
# 备份：备份盘/工坊/source.hormone_feedback.py | 验收：_test_hormone_feedback.py
# 回退：删本文件并回退 card2.py/feedback_loop.py 的 V9.8 四处
# 备注：激素负反馈调节（高浓度降增益、加速清除、滞回防抖）
"""激素负反馈调节器（SYNAPSE-1024 · V9.8）

## 它解决什么

既有的「拉回」有两层：钳位 `_clamp` 管值域，稳态回归 `regress_all` 按指数往基线落。
但持续刺激下（连夸十句、连骂三句），某条激素会一路顶到软带顶部，后续增量被
`(1-h)` 惯性公式压成一条直线 —— 情绪看着就「死」了。

本模块补的是**仿内分泌的自我抑制**：高浓度时自动下调后续分泌增益、拉高清除速率；
浓度回落后抑制缓慢解除、增益逐步恢复。**带滞回**，阈值附近不抖。

三条通路分工：
| 机制 | 负责什么 | 归属 |
|---|---|---|
| 钳位 | 值域 [0,1] | hormone（既有） |
| 稳态回归 | 基线阻尼指数回落 | hormone（既有） |
| 底色软带 | 超 0.85 / 低于 0.15 只收 30% | hormone（既有） |
| **负反馈** | 动态调「分泌增益 gain_mod」「清除系数 clear_mod」 | 本模块 |

红线：调节器**不覆盖激素值**。已经上去的水平靠回归 + 加速清除慢慢落。

## 与原方案（豆包稿）的差异 —— 我们这边的基线不一样

方案给的是绝对阈值 `sat_th=0.82 / rel_th=0.60`。但真卡基线是
`sero 0.66 / dopa 0.48 / cort 0.20 / oxy 0.50`（`bases_at()` 带昼夜偏移）。
血清素基线 0.66 > 释放阈值 0.60 —— 一旦进抑制，它落到 0.66 就停住，
**永远回不到 0.60 以下，抑制会永久卡死**。

所以这里把释放阈值改成「绝对值与基线取大」：

    rel_eff = max(rel_th, base + rel_margin)      默认 rel_margin = 0.07

再夹在 `sat_th - 0.02` 以内，保滞回带不为空。血清素就是 0.82 触发、落到 0.73 释放。

## 接线位置

- 回归前先 `reg.tick(hs, dt_hours)` 拿 `mods`；
- 回归用 `regulated_regress(...)`（清除加速）；
- 注入（apply_event / apply_r / tone_nudge 的候选）用 `apply_gain(...)` 折减增量；
- 抑制/解除事件走 `card._hf_events(...)` 记时间线，并可选发给信号总线。
"""

import json

import hormone

# ---- 短名 ↔ 激素键 ----
KEY_MAP = {
    "oxy": "h_oxy",
    "dopa": "h_dopa",
    "sero": "h_sero",
    "cort": "h_cort",
}
INV_MAP = {v: k for k, v in KEY_MAP.items()}

# ---- 事件名（供 signalbus 目录登记） ----
EV_SUPPRESS = "HORMONE_SUPPRESS"
EV_RELIEVE = "HORMONE_RELIEVE"

# ---- 默认参数（meta 表 hf_* 键可热覆盖） ----
DEFAULTS = {
    "sat_th": 0.82,         # 触发负反馈的高阈值
    "rel_th": 0.60,         # 解除强抑制的绝对下限（还会与基线取大）
    "rel_margin": 0.07,     # 释放阈值 = max(rel_th, base + rel_margin)
    "max_suppress": 0.65,   # 最大抑制比例 → gain_mod 下限 0.35
    "clear_boost": 1.45,    # 高浓度时清除加速系数（乘在回归 λ 上）
    "suppress_rate": 0.10,  # 抑制期增益每小时下降步长
    "recover_rate": 0.04,   # 解除后增益每小时恢复步长
}

META_STATE_KEY = "hf_state"


class RegState:
    """单条激素的抑制状态。"""

    __slots__ = ("gain_mod", "clear_mod", "suppressed")

    def __init__(self):
        self.gain_mod = 1.0      # 分泌增益：1.0 正常，越低越被抑制
        self.clear_mod = 1.0     # 清除系数：1.0 正常，>1 加速回落
        self.suppressed = False  # 滞回标记


class Regulator:
    """四激素负反馈调节器。状态可序列化随卡保存。"""

    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            for k in DEFAULTS:
                if k in cfg:
                    try:
                        self.cfg[k] = float(cfg[k])
                    except Exception:
                        pass
        self.states = {short: RegState() for short in KEY_MAP}
        self.last_mods = {}
        self.events = []

    # ------------------------------------------------------------ 阈值
    def _rel_eff(self, hk):
        """释放阈值：绝对值与「基线 + 余量」取大，夹在 sat_th 之内。"""
        base = float(hormone.H_BASE.get(hk, 0.5))
        try:
            base = float(hormone.bases_at(None).get(hk, base))
        except Exception:
            pass
        rel = max(float(self.cfg["rel_th"]), base + float(self.cfg["rel_margin"]))
        return min(rel, float(self.cfg["sat_th"]) - 0.02)

    # ------------------------------------------------------------ 状态机
    def tick(self, hs, dt_hours=1.0):
        """读当前激素水平，按滞回规则更新增益/清除系数。

        返回 ``(mods, events)``：
          mods:   {激素键: {gain_mod, clear_mod, suppressed}}
          events: 本轮新产生的抑制/解除事件（只在进入/退出时报）
        """
        dt = max(0.0, float(dt_hours))
        floor_gain = 1.0 - float(self.cfg["max_suppress"])
        sat = float(self.cfg["sat_th"])
        mods = {}
        events = []

        for short, hk in KEY_MAP.items():
            st = self.states[short]
            level = float(hs.get(hk, hormone.H_BASE.get(hk, 0.5)))
            rel = self._rel_eff(hk)

            if level >= sat:
                entering = not st.suppressed
                st.suppressed = True
                st.gain_mod = max(floor_gain,
                                  st.gain_mod - float(self.cfg["suppress_rate"]) * dt)
                st.clear_mod = float(self.cfg["clear_boost"])
                if entering:
                    events.append({"event": EV_SUPPRESS, "hormone": hk,
                                   "level": round(level, 3),
                                   "gain_mod": round(st.gain_mod, 3)})
            elif level < rel:
                if st.suppressed:
                    events.append({"event": EV_RELIEVE, "hormone": hk,
                                   "level": round(level, 3)})
                st.suppressed = False
                st.gain_mod = min(1.0,
                                  st.gain_mod + float(self.cfg["recover_rate"]) * dt)
                st.clear_mod = 1.0
            # 滞回带（rel <= level < sat）：维持现状，不主动改。

            mods[hk] = {"gain_mod": round(st.gain_mod, 4),
                        "clear_mod": round(st.clear_mod, 4),
                        "suppressed": st.suppressed}

        self.last_mods = mods
        self.events = events
        return mods, events

    # ------------------------------------------------------------ 取值
    def gain_of(self, hk, default=1.0):
        return self.last_mods.get(hk, {}).get("gain_mod", default)

    def clear_of(self, hk, default=1.0):
        return self.last_mods.get(hk, {}).get("clear_mod", default)

    def suppressed_keys(self):
        return [hk for hk, m in self.last_mods.items() if m.get("suppressed")]

    # ------------------------------------------------------------ 持久化
    def to_dict(self):
        return {short: {"gain_mod": st.gain_mod, "clear_mod": st.clear_mod,
                        "suppressed": st.suppressed}
                for short, st in self.states.items()}

    def load_dict(self, d):
        if not isinstance(d, dict):
            return
        for short, sv in d.items():
            if short not in self.states or not isinstance(sv, dict):
                continue
            st = self.states[short]
            try:
                st.gain_mod = max(0.0, min(1.0, float(sv.get("gain_mod", 1.0))))
                st.clear_mod = max(0.0, float(sv.get("clear_mod", 1.0)))
            except Exception:
                continue
            st.suppressed = bool(sv.get("suppressed", False))


# ---------------------------------------------------------------- 持久化辅助
def save_state(db, reg):
    """把调节器状态写进 meta 表 `hf_state`（JSON）。"""
    try:
        db.execute("INSERT INTO meta(key,val) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
                   (META_STATE_KEY, json.dumps(reg.to_dict())))
        db.commit()
    except Exception:
        pass


def load_state(db):
    """从 meta 表读调节器状态；无记录/损坏返回 None。"""
    try:
        row = db.execute("SELECT val FROM meta WHERE key=?",
                         (META_STATE_KEY,)).fetchone()
        if row and row["val"]:
            return json.loads(row["val"])
    except Exception:
        pass
    return None


def cfg_from_meta(db):
    """从 meta 表 hf_* 键读参数覆盖；缺省回 DEFAULTS。"""
    cfg = {}
    try:
        for key in DEFAULTS:
            row = db.execute("SELECT val FROM meta WHERE key=?",
                             ("hf_" + key,)).fetchone()
            if row and row["val"] is not None:
                cfg[key] = float(row["val"])
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------- 包装函数
def regulated_regress(hs, dt_hours, mods, bases=None, lam=None):
    """带清除加速的稳态回归。

    复用 hormone 的基线与衰减常数；偏离基线的部分按 `clear_mod` 放大回归 λ。
    """
    if bases is None:
        bases = hormone.bases_at(None)
    lam = hormone.LAM_H if lam is None else float(lam)
    out = {}
    for k in hormone.H_KEYS:
        b = bases.get(k, hormone.H_BASE.get(k, 0.5))
        h = hs.get(k, b)
        cm = 1.0
        if mods:
            try:
                cm = float(mods.get(k, {}).get("clear_mod", 1.0))
            except Exception:
                cm = 1.0
        if dt_hours > 0:
            import math
            k_exp = math.exp(-lam * cm * dt_hours)
            h = hormone._clamp(b + (h - b) * k_exp)
        out[k] = h
    return out


def apply_gain(hs, cand, mods):
    """把注入候选 `cand` 的增量按各激素 `gain_mod` 折减后叠回。

    不重算 hormone 的 ETA 公式，只在增量出口打折。
    """
    out = dict(hs)
    for k in hormone.H_KEYS:
        g = 1.0
        if mods:
            try:
                g = float(mods.get(k, {}).get("gain_mod", 1.0))
            except Exception:
                g = 1.0
        out[k] = hormone._clamp(hs[k] + (cand.get(k, hs[k]) - hs[k]) * g)
    return out


def snapshot(reg):
    """给控制台/路由用的一眼快照。"""
    return {"cfg": dict(reg.cfg),
            "rel_eff": {hk: round(reg._rel_eff(hk), 3) for hk in KEY_MAP.values()},
            "states": reg.last_mods or reg.to_dict(),
            "suppressed": reg.suppressed_keys()}
