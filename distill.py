# -*- coding: utf-8 -*-
# 小鲸鱼数字生命卡 · 学习回流层 V9.9
# 真名：distill.py
# 用途：把「学到的」沉淀成五类产物里的可执行那几类 —— 技能 / 经验 / 印象 / 自我认知 / 反思
# 作者：小鲸鱼（与使用者一起）
"""学习回流（distillation）。

为什么需要它（2026-11-21）：
    learn_note 只把内容写成 study 记忆（可召回），review 只写 episode 记忆。
    `skill` 表有完整的 SM-2 维护（decay / use / search），但**没有任何路径往里写**
    —— 又一次「看起来在工作」：表会衰减、检索会命中，可里面永远只有手写的那几条。

本层的职责是把已经「结过账」的学习结果，分流到它该去的载体：

    study 记忆 + learn_eval(kept)   → 技能（skill）
    review(promoted)                → 自我认知候选（region=self，走治理 pending）
    impression / affect             → 行为印象与态度（已有，不动）

硬规则（写死的，不给调用方留口子）：
    1. **不碰身份锚**。人格锚是封存的，这里只产生「候选」并记 pending 决策；
    2. 幂等。同一 (ref_kind, ref_id, out_kind) 只产出一次，重复调用不重复建；
    3. 只认证据。learn_eval.value < KEEP_VALUE 且没有两次印证的学习结果不建技能。
"""

import json
import time

TRY = "try"

# 技能名字长度上限（skill 表里 name 是唯一键）
NAME_MAX = 24
DESC_MAX = 400
# 达到这个评估值才认为「这条学习结果站得住」（与 feedback_loop._learn_verdict 的 kept 一致）
KEEP_VALUE = 0.66
# 观察级复盘也允许当「自我认知候选」的强度下限
WATCH_HINT = 0.45

_DDL = """
CREATE TABLE IF NOT EXISTS distill (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_kind TEXT NOT NULL,
    ref_id   INTEGER NOT NULL,
    out_kind TEXT NOT NULL,
    out_id   INTEGER,
    note     TEXT NOT NULL DEFAULT '',
    ts       REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_distill ON distill(ref_kind, ref_id, out_kind);
CREATE INDEX IF NOT EXISTS ix_distill_ts ON distill(ts);
"""

OUT_KINDS = ("skill", "insight", "experience")


def ensure(conn):
    conn.executescript(_DDL)
    conn.commit()


def _seen(conn, ref_kind, ref_id, out_kind):
    return conn.execute(
        "SELECT id FROM distill WHERE ref_kind=? AND ref_id=? AND out_kind=?",
        (ref_kind, int(ref_id), out_kind)).fetchone() is not None


def _mark(conn, ref_kind, ref_id, out_kind, out_id, note="", now=None):
    conn.execute(
        "INSERT OR IGNORE INTO distill(ref_kind,ref_id,out_kind,out_id,note,ts)"
        " VALUES(?,?,?,?,?,?)",
        (ref_kind, int(ref_id), out_kind,
         int(out_id) if isinstance(out_id, int) else None,
         note or "", time.time() if now is None else float(now)))
    conn.commit()


def skill_name_from(raw, topic=None):
    """从一条学习记录里取技能名：优先 topic，否则取正文开头到第一个分隔符。"""
    if topic:
        return str(topic).strip()[:NAME_MAX]
    txt = (raw or "").strip()
    # learn_note 会把内容写成「【学习】主题：正文」
    if txt.startswith("【"):
        i = txt.find("】")
        if i > 0:
            txt = txt[i + 1:].strip()
    for sep in ("：", ":", "。", "，", "\n"):
        i = txt.find(sep)
        if 0 < i <= NAME_MAX:
            return txt[:i].strip()
    return txt[:NAME_MAX].strip()


def make_skill(card, mid, raw, topic=None, now=None, tag="learn"):
    """把一条 study 记忆变成技能。已存在同名技能时只补一次记录，不重复建。"""
    import skill as skill_mod

    name = skill_name_from(raw, topic)
    if not name:
        return None
    desc = (raw or "").strip()[:DESC_MAX]
    if not desc:
        return None
    exist = card.skill.get(name)
    if exist is not None:
        return int(exist["id"])
    # 注意：skill.vec 是 **bigram 稀疏向量**（card2.to_vec），不是 768 维 embedding。
    # 2026-11-21 踩过：写成 {"e": [...]} 会让 skill.search() 抛
    # TypeError: can't multiply sequence by non-int of type 'list'。
    from card2 import to_vec as _to_vec
    vec = {}
    try:
        vec = _to_vec(name + " " + desc)
    except Exception:
        vec = {}
    sid = card.skill.add(name, desc, vec=vec, l=0.55, tag=tag, now=now)
    return sid


def book_learning(card, limit=10, now=None, min_value=None, idle_days=0.0):
    """把已经结过账的学习结果变成技能。返回 [(mid, sid, name)]。"""
    now = time.time() if now is None else float(now)
    min_value = KEEP_VALUE if min_value is None else float(min_value)
    rows = card.db.execute(
        "SELECT m.id AS mid, m.raw AS raw,"
        "       e.value AS value, e.times_confirmed AS tc"
        "  FROM mem m JOIN learn_eval e ON e.mem_id = m.id"
        " WHERE m.dead=0 AND m.region='study'"
        "   AND (e.value >= ? OR COALESCE(e.times_confirmed,0) >= 2)"
        " ORDER BY e.value DESC, m.id ASC LIMIT ?",
        (min_value, int(limit))).fetchall()
    out = []
    for r in rows:
        mid = int(r["mid"])
        if _seen(card.db, "mem", mid, "skill"):
            continue
        sid = make_skill(card, mid, r["raw"], None, now=now)
        if sid:
            _mark(card.db, "mem", mid, "skill", sid,
                  note="学习结果转技能（value=%.2f）" % float(r["value"] or 0), now=now)
            out.append((mid, sid, skill_name_from(r["raw"], None)))
    return out


def insight_from_review(card, review_row, now=None):
    """复盘的晋升结果 → 自我认知候选（region=self，同时记一条 pending 治理）。

    注意：这里写的是「我对自己的说法」，不是人格锚本身。锚不许改写。
    """
    import governance as gov_mod

    rid = int(review_row["id"])
    if _seen(card.db, "review", rid, "insight"):
        return None
    summary = (review_row["summary"] or review_row["base_summary"] or "").strip()
    if len(summary) < 6:
        return None
    body = "【自我认知】%s" % summary[:300]
    mid = card.remember(body, salience=0.58, region="self", speaker="self",
                        mtype="episode", pool="long", source="review")
    mid = int(mid) if isinstance(mid, int) else None
    if not mid:
        return None
    _mark(card.db, "review", rid, "insight", mid, note="复盘晋升转自我认知", now=now)
    try:
        gov_mod.decide(card.db, "self", rid, "pending",
                       reason="自我认知候选，等待人确认后才进表达策略",
                       source="distill", exec_state="pending")
    except Exception:
        pass
    return (rid, mid)


def sweep_reviews(card, limit=5, now=None):
    """扫最近的「已晋升、侧写为正」复盘，转成自我认知候选。"""
    # 2026-11-21 实测：49 条复盘全是 watch、晋升 0 条 —— 原条件 promoted=1
    # 让这条通路事实上永不触发（又一种「看起来在工作」）。改成：晋升的照收，
    # 另外把「证据≥2 且强度≥WATCH_HINT」的观察也当候选，但只记 pending，不改人格。
    rows = card.db.execute(
        "SELECT id, summary, base_summary, side, strength, mem_id"
        "  FROM review"
        " WHERE COALESCE(promoted,0)=1 AND COALESCE(confirmed,0)=0"
        "    OR (COALESCE(evidence_n,0)>=2 AND COALESCE(strength,0)>=?)"
        " ORDER BY COALESCE(strength,0) DESC, ts DESC LIMIT ?",
        (WATCH_HINT, int(limit))).fetchall()
    out = []
    for r in rows:
        got = insight_from_review(card, r, now=now)
        if got:
            out.append(got)
    return out


def stats(conn):
    out = {"by_out": {}, "total": 0}
    for r in conn.execute("SELECT out_kind, COUNT(*) n FROM distill GROUP BY out_kind"):
        out["by_out"][r["out_kind"]] = int(r["n"])
        out["total"] += int(r["n"])
    try:
        out["skills"] = int(conn.execute("SELECT COUNT(*) FROM skill").fetchone()[0])
    except Exception:
        out["skills"] = 0
    try:
        out["self_mems"] = int(conn.execute(
            "SELECT COUNT(*) FROM mem WHERE region='self' AND dead=0").fetchone()[0])
    except Exception:
        out["self_mems"] = 0
    return out
