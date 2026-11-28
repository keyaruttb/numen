# -*- coding: utf-8 -*-
"""card_feeling —— 「受」归卡（受 = 稳态奖励）

2026-11-29 凌晨。关独立心流之前发现它还挂着 feeling_tick：
直接关掉，受就不累积了。所以先把它搬进来。

## 这一版和之前两版的区别

```
feeling_tick.py（今晚早些时候）   算「欠账」∫max(0, drive-base)dt
                                  问题：符号被丢了，苦/乐分不出来
feeling_sp.py（今晚）            算 ΔD = D(t) − D(t+1)  ← 对的那个
                                  问题：只写报告，不入库
card_feeling.py（这份）           ΔD 公式 + 写进卡
```

## 公式（核过原文）

    D(t) = Σ wᵢ·((xᵢ − x*ᵢ)/tolᵢ)² / Σ wᵢ    加权归一化偏差
    r(t) = D(t) − D(t+1)                       偏差减少了多少

    r > 阈  靠近设定点  → 乐
    r < −阈 远离设定点  → 苦
    其余                → 舍

出处：Keramati & Gutkin 2011/2014；`r_t = D(H_t) − D(H_{t+1})`。

## 数据源

设定点：`setpoint` 表 40 条（viscera 24 / body 5 / hormone 4 / drive 7）
当前值：`h_log`（4 快激素）+ `viscera_hist`（24 慢激素 + 5 身体量）

**drive 那 7 条只存当前快照、没有历史，所以算不出 ΔD。**
它们只进「此刻偏差」，不进「变化」。这是硬限制，不糊弄。
"""

import json
import time

MAX_GAP_S = 900       # 超过这个间隔当断开，不跨段算 ΔD
THRESH = 0.02         # |r| 小于这个算「舍」


def _state_path():
    return r"C:\dsh\_card\card_feeling.state.json"


def _load_st():
    try:
        return json.loads(open(_state_path(), encoding="utf-8").read())
    except Exception:
        return {}


def _save_st(st):
    try:
        open(_state_path(), "w", encoding="utf-8").write(
            json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def load_setpoints(db):
    """→ {(layer, name): (target, tol, weight)}"""
    sp = {}
    try:
        for var, layer, target, tol, weight in db.execute(
                "SELECT var,layer,target,tol,weight FROM setpoint"):
            sp[(layer, var.split(".", 1)[1])] = (
                float(target), max(1e-6, float(tol)), float(weight))
    except Exception:
        pass
    return sp


def state_at(db, ts):
    """某一时刻的内部状态 {('layer', name): value}"""
    st = {}
    try:
        r = db.execute(
            "SELECT sero,dopa,cort,oxy FROM h_log WHERE ts<=? "
            "ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
        if r:
            for k, v in zip(("sero", "dopa", "cort", "oxy"), r):
                if v is not None:
                    st[("hormone", k)] = float(v)
    except Exception:
        pass
    try:
        v = db.execute(
            "SELECT h,temp,tension,hr,resp,face FROM viscera_hist "
            "WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
        if v:
            try:
                hh = json.loads(v[0]) if v[0] else {}
            except Exception:
                hh = {}
            for k, val in hh.items():
                try:
                    st[("viscera", k)] = float(val)
                except Exception:
                    pass
            for k, val in zip(("temp", "tension", "hr", "resp", "face"), v[1:]):
                if val is not None:
                    try:
                        st[("body", k)] = float(val)
                    except Exception:
                        pass
    except Exception:
        pass
    return st


def deviation(st, sp):
    """D = Σ w·((x−x*)/tol)² / Σ w。返回 (D, detail)。只看两边都有的量。"""
    num = den = 0.0
    detail = {}
    for key, x in st.items():
        if key not in sp:
            continue
        target, tol, w = sp[key]
        d = ((x - target) / tol) ** 2
        num += w * d
        den += w
        detail[key] = d
    if den <= 0:
        return None, {}
    return num / den, detail


def tick(mind):
    """算一次受并写进 feeling 表。**调用方须持 mind.lock（只读 + 一次插入，都很快）。**

    返回状态 dict 供 /health 或日志看。
    """
    db = mind.card.db
    st = _load_st()
    now = time.time()

    sp = load_setpoints(db)
    if not sp:
        return {"skip": "setpoint 表空"}

    # 上一次的采样时间与 D
    last_ts = float(st.get("last_ts") or 0)
    last_D = st.get("last_D")

    cur = state_at(db, now)
    D, detail = deviation(cur, sp)
    if D is None:
        return {"skip": "没有可用状态"}

    out = {"D": round(D, 3), "n": len(detail)}
    if last_ts and last_D is not None and 0 < (now - last_ts) <= MAX_GAP_S:
        r = float(last_D) - D
        label = "乐" if r > THRESH else ("苦" if r < -THRESH else "舍")
        top = max(detail.items(), key=lambda kv: kv[1]) if detail else None
        try:
            db.execute(
                "INSERT INTO feeling(ts,dialogue_id,valence,arousal,label,debt,note) "
                "VALUES(?,?,?,?,?,?,?)",
                # dialogue_id 用负数：feeling 表有 UNIQUE(dialogue_id)，
                # 真实对话行用正 id，这里的「身根一拍」用 -秒级时间戳区分。
                (now, -int(now), round(r, 4), round(min(1.0, abs(r) * 5), 3), label, "",
                 "设定点版 ΔD；最大偏差=%s" % (
                     (top[0][0] + "." + top[0][1]) if top else "")))
            db.commit()
        except Exception as e:
            out["write_err"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        out.update({"r": round(r, 4), "label": label})

    st["last_ts"] = now
    st["last_D"] = D
    _save_st(st)
    return out


def status():
    st = _load_st()
    return {"on": True,
            "last_D": st.get("last_D"),
            "last_ts": st.get("last_ts"),
            "age_s": round(time.time() - float(st.get("last_ts") or 0), 1)
            if st.get("last_ts") else None}
