# -*- coding: utf-8 -*-
"""数字生命卡 · 小鲸鱼（小鲸鱼）
小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card

治理决策枚举（V9.7.2 / P1）：把「这条新来的东西该怎么处置」写成一份可审计的判定，
而不是散在各处的 if。八种结论固定，不允许临场发明新词。

    accept              —— 原样收下
    accept_with_caveat  —— 收下，但带着限定（可信度打折 / 标注来源）
    update              —— 更新既有那条（同 subject 的同一属性，时间更近）
    supersede           —— 新的取代旧的（旧的留档，标 superseded）
    contradict          —— 与既有冲突且无法判定谁对 → 两条并存，标冲突
    pending             —— 证据不够，先挂着
    reject              —— 不收
    needs_user          —— 只有本人能定

每条判定都写一行 gov_decision —— 谁、什么时候、对哪条、为什么、依据几条冲突。
判定不改数据，只出结论；真正动手的是调用方（这层只是「记账 + 统一口径」）。
"""
import time

DECISIONS = ("accept", "accept_with_caveat", "update", "supersede",
             "contradict", "pending", "reject", "needs_user")

# 哪些结论会真的动到数据
APPLIED = ("accept", "accept_with_caveat", "update", "supersede")

_DDL = """
CREATE TABLE IF NOT EXISTS gov_decision(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    subject_kind TEXT NOT NULL DEFAULT 'mem',
    subject_id   INTEGER NOT NULL DEFAULT 0,
    action       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'auto',
    conflicts    INTEGER NOT NULL DEFAULT 0,
    applied      INTEGER NOT NULL DEFAULT 0,
    extra        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gov_ts ON gov_decision(ts);
CREATE INDEX IF NOT EXISTS ix_gov_sub ON gov_decision(subject_kind, subject_id);
"""


def ensure(conn):
    conn.executescript(_DDL)
    conn.commit()


def decide(conn, subject_kind, subject_id, action, reason="", source="auto",
           conflicts=0, extra="", now=None):
    """记一条治理判定。action 必须是八种之一，否则抛——宁可炸，不要脏口径。"""
    a = str(action or "").strip()
    if a not in DECISIONS:
        raise ValueError("不认识的治理结论：%r（只认八种）" % (action,))
    t = float(now if now is not None else time.time())
    cur = conn.execute(
        "INSERT INTO gov_decision(ts,subject_kind,subject_id,action,reason,source,conflicts,applied,extra)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (t, str(subject_kind or "mem"), int(subject_id or 0), a, str(reason or ""),
         str(source or "auto"), int(conflicts or 0), 1 if a in APPLIED else 0, str(extra or "")))
    conn.commit()
    return {"ok": True, "id": int(cur.lastrowid), "action": a, "applied": a in APPLIED}


def log(conn, limit=20):
    rows = conn.execute(
        "SELECT id, ts, subject_kind, subject_id, action, reason, source, conflicts, applied"
        " FROM gov_decision ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "ts": r[1], "subject_kind": r[2], "subject_id": r[3],
            "action": r[4], "reason": r[5], "source": r[6], "conflicts": r[7], "applied": r[8]}
        out.append(d)
    return out


def stats(conn):
    d = {a: 0 for a in DECISIONS}
    for a, n in conn.execute("SELECT action, COUNT(*) FROM gov_decision GROUP BY action").fetchall():
        d[str(a)] = int(n)
    total = sum(d.values())
    return {"total": total, "by_action": d}
