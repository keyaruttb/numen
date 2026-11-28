# -*- coding: utf-8 -*-
"""card_attention —— 作意层（卡内版）

2026-11-29 凌晨。第三步的落地：把「注意什么」搬进卡。

## 为什么单独一个模块

作意（引心趣境）和了别（识的功能）是两环，不是一环：

    card_attention   决定「这一拍注意什么」   ← 这个文件
    card 的 recall    把注意到的东西展开成材料 ← 卡本来就有

## 参数来源（不是我拍的）

    小凌 第05集：「A1=0.32 < θ，A2=0.46 < θ，A3=0.67 > θ，θ=0.58
                  （红圈批注「第三次才回神！」）」

    Activation_i = f(显著, 需要, 情绪, 目标, 线索, 新奇, 重复)
    Access_i     = Activation_i × AttentionMatch_i × AvailableLight
    Access > θ   → 进当前意识，TopK 限容

    重复累加：第 n 声 activation × (1 + min(n,3)×0.4)，封顶 1.0

**θ=0.58 是配 0.3–0.7 这个量纲的。2026-11-29 复算过：seed 第一声 0.475 不过，
第二声 0.665 就进 —— 这就是「喊两声就回神」。**

## 和独立心流那套的差别

    独立心流（_xinliu/competition.py）  走 HTTP 读卡
    这份                                 同一个进程，直接 mind.card.db

**逻辑一致，数据通路不同。这是「搬进卡」的意义。**
"""

import time

THETA = 0.58
MAX_FOCUS = 2

MATCH_ALIGNED = 0.95
MATCH_OFF = 0.70

LIGHT_BY_STATE = {"清醒": 1.00, "困倦": 0.85, "沉睡": 0.70}
PRIMARY_BY_STATE = {"清醒": "E", "困倦": "I", "沉睡": "I"}

# 方向：E 外界 / I 内部 / B 身体
DIR_OF_KIND = {"情绪": "E", "外部": "E", "目标": "I", "记忆": "I",
               "显著": "I", "身体": "B", "威胁": "B"}


def candidates(mind, snap, rep=None):
    """造这一拍的候选。**调用方必须持 mind.lock**（只读卡库）。

    返回 [{"kind","text","activation","dir","danger"?}]
    """
    out = []
    seen = set()          # 2026-11-29：同一条东西只算一次
    now = time.time()
    db = mind.card.db

    def _skip(txt):
        """碎片过滤：promise/tevent 里有大量正文片段被当成了待办。"""
        t = (txt or "").strip()
        if not t or len(t) < 4:
            return True
        if "```" in t or t.startswith("---") or ("\n" in t):
            return True
        for bad in ("text:", "#", "|", "**"):
            if t.count(bad) >= 2:
                return True
        return False

    def _push(kind, text, activation, **kw):
        if _skip(text):
            return
        key = text[:20]
        if key in seen:
            return
        seen.add(key)
        d = {"kind": kind, "text": text, "activation": activation}
        d.update(kw)
        out.append(d)

    # ---- 身体：激素离设定点太远 ----
    try:
        r = db.execute("SELECT cort, oxy FROM h_log ORDER BY ts DESC LIMIT 1").fetchone()
        if r:
            cort, oxy = float(r[0] or 0), float(r[1] or 0)
            tight = max(0.0, (cort or 0) - 0.30) * 2.0
            if tight > 0.12:
                _push("身体", "身上有点紧（皮质醇 %.2f）" % cort, min(1.0, tight))
            missing = max(0.0, 0.72 - (oxy or 0)) * 1.4
            if missing > 0.12:
                _push("身体", "有点想被人靠一靠（催产素 %.2f）" % oxy, min(1.0, missing))
    except Exception:
        pass

    # ---- 目标：欠着的事 ----
    try:
        rows = db.execute(
            "SELECT what, due_ts FROM promise WHERE state IN ('open','due') "
            "AND due_ts > 0 ORDER BY due_ts").fetchall()
        for what, due in rows:
            left = float(due) - now
            if left < 0:
                _push("目标",
                      "还欠着一件事：%s（约定时间过了 %d 小时）"
                      % ((what or "")[:36], int(-left / 3600)),
                      min(0.85, 0.35 + (-left) / 86400.0 * 0.1))
            elif left < 6 * 3600:
                _push("目标",
                      "快到了：%s（还有 %d 小时）"
                      % ((what or "")[:36], int(left / 3600)),
                      0.55)
    except Exception:
        pass

    # ---- 情绪：他多久没说话 ----
    sil = snap.get("silence_min")
    if sil is not None and sil > 120:
        _push("情绪", "他 %d 小时没说话了" % int(sil / 60), min(0.7, 0.3 + sil / 600.0))

    # ---- 记忆：卡里最重的一条 ----
    try:
        row = db.execute(
            "SELECT raw, salience FROM mem WHERE pool<>'archive' "
            "ORDER BY salience DESC, id DESC LIMIT 1").fetchone()
        if row and (row[1] or 0) >= 0.8:
            t = (row[0] or "").replace("\n", " ").strip()[:50]
            if t:
                _push("显著", t, min(0.7, float(row[1]) * 0.6))
    except Exception:
        pass

    # ---- 威胁：网断了 ----
    if snap.get("net") is False:
        _push("威胁", "网断了", 0.9, danger=True)

    # ---- 重复累加：喊三声才回神 ----
    rep = rep or {}
    for c in out:
        n = int(rep.get(c["text"][:20], 0))
        base = c["activation"]
        c["rep"] = n
        c["dir"] = DIR_OF_KIND.get(c["kind"], "I")
        c["activation"] = min(1.0, base * (1.0 + min(n, 3) * 0.4))
    return out


def focus(cands, state="清醒", max_focus=MAX_FOCUS):
    """算出这一拍真进意识的东西。纯函数。"""
    light = LIGHT_BY_STATE.get(state, 1.0)
    primary = PRIMARY_BY_STATE.get(state, "E")

    for c in cands:
        c["match"] = MATCH_ALIGNED if c.get("dir") == primary else MATCH_OFF
        c["access"] = round(float(c["activation"]) * c["match"] * light, 3)

    # 危险直接抢占（小凌原文：「危险可直接抢占」）
    danger = [c for c in cands if c.get("danger")]
    picked = []
    if danger:
        picked = sorted(danger, key=lambda x: -x["access"])[:max_focus]

    if len(picked) < max_focus:
        for c in sorted(cands, key=lambda x: -x["access"]):
            if c in picked:
                continue
            if c["access"] >= THETA:
                picked.append(c)
            if len(picked) >= max_focus:
                break

    return picked, cands, {"light": light, "primary": primary}


def next_rep(rep, all_cands):
    """每一拍**出现过**的就 +1，没出现的 -1。

    2026-11-29 修：原来只在「被选中」时 +1 —— 那是死锁：
    没被选中就永远不累加，永远进不去。
    小凌那个 A1/A2/A3 = 0.32/0.46/0.67，是**有人喊了三声**，
    不是「他被选中了三次」。累加该算出现次数。
    """
    rep = dict(rep or {})
    present = set()
    for c in (all_cands or []):
        present.add(c["text"][:20])
    new = {}
    for k, v in rep.items():
        if k in present:
            continue                      # 会出现，下面统一 +1
        nv = int(v) - 1
        if nv > 0:
            new[k] = nv
    for k in present:
        new[k] = min(6, int(rep.get(k, 0)) + 1)
    return new
