# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台机器上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wangjunhan68-byte · https://github.com/wangjunhan68-byte/whale-card

"""评价印象层（V9.3）：我对某个人 / 某种场景的稳定态度、安全感与预期。

和行为印象（impression.py）并列、但口径不同：

    行为印象  记「谁在什么情境下常做什么、常说什么」  —— 只描述、不评价、不预测
    评价印象  记「我心里对他 / 这种场景是什么感觉」  —— 主观态度，可被新经历慢慢改写

三条红线（写死）：
1. 慢磨，不突变：一条反馈只挪一小步（默认 lr=0.08），多次同向才看得出偏移；
   印象再深也不到 1，永远留着被新经历改写的余地。
2. 是底色，不是当下反馈：命中时只做轻微情绪预调和联想预热，不当台词、不直接变成动作。
3. 不替人格锚：valence / safety 是「此刻对他的感受」，会褪色、会被新经历修正，
   不写身份层、不改人格锚。

纯函数 + 一张表，不依赖模型、不联网。
"""

from __future__ import annotations

import json
import time

DAY = 86400.0

# ---- 参数（可在 meta 里热调 fb_affect_lr） ---------------------------------
AFFECT_LR = 0.08
SAFETY_UP = 0.6            # 正反馈抬安全感的系数
SAFETY_DOWN = 0.8          # 负反馈压安全感的系数（比评价更敏感）
SALIENCE_SCALE = 6.0
FADE_HALF_DAYS = 90.0
SALIENCE_DEAD = 0.06
MOOD_STEP = 0.08           # 命中时激素底色扰动步长
LINKED_MAX = 20

_KIND_PERSON = "person"
_KIND_SCENE = "scene"

_DDL = """
CREATE TABLE IF NOT EXISTS affect (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL DEFAULT 'scene',
    target_tag TEXT NOT NULL DEFAULT '',
    valence REAL NOT NULL DEFAULT 0.0,
    safety REAL NOT NULL DEFAULT 0.5,
    expectation TEXT NOT NULL DEFAULT '',
    salience REAL NOT NULL DEFAULT 0.0,
    linked_mems TEXT NOT NULL DEFAULT '[]',
    n_pos INTEGER NOT NULL DEFAULT 0,
    n_neg INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    first_ts REAL NOT NULL DEFAULT 0.0,
    last_ts REAL NOT NULL DEFAULT 0.0,
    last_decay_ts REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_affect_target ON affect(target_kind, target_tag);
CREATE INDEX IF NOT EXISTS idx_affect_w ON affect(status, salience);
"""


def ensure(conn):
    conn.executescript(_DDL)
    conn.commit()


def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _row(conn, kind, tag):
    cur = conn.execute(
        "SELECT id, target_kind, target_tag, valence, safety, expectation, salience,"
        " linked_mems, n_pos, n_neg, version, first_ts, last_ts, last_decay_ts, status, note"
        " FROM affect WHERE target_kind=? AND target_tag=?", (kind, tag))
    r = cur.fetchone()
    if not r:
        return None
    cols = ("id", "target_kind", "target_tag", "valence", "safety", "expectation",
            "salience", "linked_mems", "n_pos", "n_neg", "version", "first_ts",
            "last_ts", "last_decay_ts", "status", "note")
    return dict(zip(cols, r))


def expectation_of(valence, safety):
    """缺省规则模板；模型润色是宿主侧的钩子，这里不依赖 LLM。"""
    if valence <= -0.35:
        ev = "对他有点戒备，不太踏实"
    elif valence >= 0.35:
        ev = "和他相处起来比较安心"
    else:
        ev = "印象还在形成中"
    if safety <= 0.35:
        sv = "安全感偏低，容易紧张"
    elif safety >= 0.7:
        sv = "觉得安全、放松"
    else:
        sv = "安全感一般"
    return ev + "；" + sv


def salience_of(n):
    return min(0.95, 1.0 - 0.5 ** (float(n) / SALIENCE_SCALE))


def read(conn, target_kind, target_tag):
    ensure(conn)
    return _row(conn, target_kind, target_tag)


def match(conn, target_kind=None, target_tag=None, min_salience=0.0):
    """按（种类, 标签）精确命中；不给标签时返回该种类下显著度最高的一条。"""
    ensure(conn)
    if target_kind is not None and target_tag is not None:
        r = _row(conn, target_kind, target_tag)
        return r if (r and float(r["salience"]) >= min_salience) else None
    q = "SELECT target_kind, target_tag FROM affect WHERE status!='faded' AND salience>=?"
    args = [float(min_salience)]
    if target_kind is not None:
        q += " AND target_kind=?"
        args.append(target_kind)
    q += " ORDER BY salience DESC LIMIT 1"
    r = conn.execute(q, args).fetchone()
    return _row(conn, r[0], r[1]) if r else None


def attach_mem(conn, target_kind, target_tag, mem_id):
    ensure(conn)
    r = _row(conn, target_kind, target_tag)
    now = time.time()
    if not r:
        conn.execute(
            "INSERT INTO affect(target_kind, target_tag, first_ts, last_ts, last_decay_ts)"
            " VALUES(?,?,?,?,?)", (target_kind, target_tag, now, now, now))
        r = _row(conn, target_kind, target_tag)
    try:
        ids = json.loads(r["linked_mems"] or "[]")
    except Exception:
        ids = []
    mid = int(mem_id)
    ids = [x for x in ids if int(x) != mid] + [mid]
    ids = ids[-LINKED_MAX:]
    conn.execute("UPDATE affect SET linked_mems=?, last_ts=? WHERE id=?",
                 (json.dumps(ids), now, r["id"]))
    conn.commit()
    return ids


def update(conn, target_kind, target_tag, r, severity=0.5, mem_id=None, note="",
           lr=None, now=None):
    """一条反馈磨一小步。r ∈ [-1, 1]：正=好经历，负=坏经历。"""
    ensure(conn)
    now = float(now if now is not None else time.time())
    try:
        r = _clip(float(r), -1.0, 1.0)
    except Exception:
        return None
    sev = _clip(float(severity if severity is not None else 0.5), 0.0, 1.0)
    lr = float(AFFECT_LR if lr is None else lr)
    amp = abs(r) * (1.0 if r > 0 else (0.6 + 0.4 * sev))
    cur = _row(conn, target_kind, target_tag)
    if not cur:
        conn.execute(
            "INSERT INTO affect(target_kind, target_tag, first_ts, last_ts, last_decay_ts)"
            " VALUES(?,?,?,?,?)", (target_kind, target_tag, now, now, now))
        cur = _row(conn, target_kind, target_tag)

    val = float(cur["valence"])
    saf = float(cur["safety"])
    if r >= 0:
        val += lr * amp
        saf += lr * SAFETY_UP * amp
    else:
        val -= lr * amp
        saf -= lr * SAFETY_DOWN * amp
    val = _clip(val, -1.0, 1.0)
    saf = _clip(saf, 0.0, 1.0)
    n_pos = int(cur["n_pos"]) + (1 if r >= 0 else 0)
    n_neg = int(cur["n_neg"]) + (1 if r < 0 else 0)
    sal = salience_of(n_pos + n_neg)
    try:
        ids = json.loads(cur["linked_mems"] or "[]")
    except Exception:
        ids = []
    if mem_id is not None:
        ids = [x for x in ids if int(x) != int(mem_id)] + [int(mem_id)]
        ids = ids[-LINKED_MAX:]
    conn.execute(
        "UPDATE affect SET valence=?, safety=?, expectation=?, salience=?, linked_mems=?,"
        " n_pos=?, n_neg=?, version=version+1, last_ts=?, status='active',"
        " note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
        (val, saf, expectation_of(val, saf), sal, json.dumps(ids), n_pos, n_neg,
         now, str(note or ""), str(note or "")[:200], cur["id"]))
    conn.commit()
    return _row(conn, target_kind, target_tag)


def fade(conn, now=None, half_days=None):
    """向中性缓慢回归；不删除，再接触会重新被磨出来。"""
    ensure(conn)
    now = float(now if now is not None else time.time())
    hd = float(FADE_HALF_DAYS if half_days is None else half_days)
    touched = 0
    for r in conn.execute(
            "SELECT id, valence, safety, salience, last_ts, last_decay_ts FROM affect").fetchall():
        last = max(float(r[4] or 0.0), float(r[5] or 0.0))
        dt_days = (now - last) / DAY if last > 0 else 0.0
        if dt_days <= 0.5:
            continue
        k = 0.5 ** (dt_days / hd)
        val = float(r[1]) * k
        saf = 0.5 + (float(r[2]) - 0.5) * k
        sal = float(r[3]) * k
        status = "fading" if sal < SALIENCE_DEAD else "active"
        conn.execute(
            "UPDATE affect SET valence=?, safety=?, salience=?, expectation=?,"
            " status=?, last_decay_ts=? WHERE id=?",
            (val, saf, sal, expectation_of(val, saf), status, now, r[0]))
        touched += 1
    conn.commit()
    return touched


def mood_nudge(conn, hs, target_kind, target_tag, step=None):
    """命中评价印象 → 轻微调整激素底色（不尖叫、不突变）。"""
    r = read(conn, target_kind, target_tag)
    if not r:
        return {}
    step = float(MOOD_STEP if step is None else step)
    sal = _clip(float(r["salience"]), 0.0, 1.0)
    if sal <= 0:
        return {}
    val, saf = float(r["valence"]), float(r["safety"])
    out = {}
    if val > 0:
        d = step * sal * val
        out["oxy"] = out.get("oxy", 0.0) + d
        out["dopa"] = out.get("dopa", 0.0) + d * 0.8
    elif val < 0:
        d = step * sal * (-val)
        out["cort"] = out.get("cort", 0.0) + d
        out["sero"] = out.get("sero", 0.0) - d * 0.6
    if saf < 0.35:
        out["cort"] = out.get("cort", 0.0) + step * sal * (0.35 - saf)
    if isinstance(hs, dict):
        for k, dv in out.items():
            if k in hs and isinstance(hs[k], (int, float)):
                hs[k] = hs[k] + dv
    return out


def hint_lines(conn, target_kind=None, target_tag=None, limit=3, min_salience=0.08):
    """给上下文的【态度】段 —— 只写主观感受，不预测行为、不当台词。"""
    ensure(conn)
    q = ("SELECT target_kind, target_tag, valence, safety, salience, n_pos, n_neg"
         " FROM affect WHERE status!='faded' AND salience>=?")
    args = [float(min_salience)]
    if target_kind is not None and target_tag is not None:
        q += " AND target_kind=? AND target_tag=?"
        args += [target_kind, target_tag]
    elif target_kind is not None:
        q += " AND target_kind=?"
        args.append(target_kind)
    q += " ORDER BY salience DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for kind, tag, val, saf, sal, np_, nn_ in conn.execute(q, args).fetchall():
        who = "他" if kind == _KIND_PERSON else ("「%s」这种场景" % tag if tag else "这种场景")
        if val >= 0.35:
            word = "心里踏实"
        elif val <= -0.35:
            word = "心里有点发紧"
        else:
            word = "说不上好坏"
        out.append("%s：%s（安全感 %.2f；%d 次好 / %d 次坏）"
                   % (who, word, float(saf), int(np_), int(nn_)))
    return out


def snapshot(conn, limit=50):
    ensure(conn)
    rows = conn.execute(
        "SELECT target_kind, target_tag, valence, safety, expectation, salience,"
        " n_pos, n_neg, version, last_ts, status FROM affect"
        " ORDER BY salience DESC LIMIT ?", (int(limit),)).fetchall()
    cols = ("target_kind", "target_tag", "valence", "safety", "expectation", "salience",
            "n_pos", "n_neg", "version", "last_ts", "status")
    return [dict(zip(cols, r)) for r in rows]


def stats(conn):
    ensure(conn)
    n = conn.execute("SELECT COUNT(*) FROM affect").fetchone()[0]
    pos = conn.execute("SELECT COUNT(*) FROM affect WHERE valence>=0.35").fetchone()[0]
    neg = conn.execute("SELECT COUNT(*) FROM affect WHERE valence<=-0.35").fetchone()[0]
    return {"affect": int(n), "affect_positive": int(pos), "affect_negative": int(neg)}
