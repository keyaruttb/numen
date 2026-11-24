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

V9.8.1（评审 R7）：判定之外补「执行状态」一栏，把「记了」和「做了」分开追。
    每条判定有自己的 exec_state：pending / applied / failed / retry / expired / superseded。
    调用方先 decide(exec_state='pending')，动手之后再 mark(gid, 'applied')；失败 mark('failed')。
    这样统计能回答「有多少判定停在半路」，而不是只数结论。
"""
import json
import time

DECISIONS = ("accept", "accept_with_caveat", "update", "supersede",
             "contradict", "pending", "reject", "needs_user")

# 哪些结论会真的动到数据
APPLIED = ("accept", "accept_with_caveat", "update", "supersede")

# 执行状态（R7）
EXEC_STATES = ("pending", "applied", "failed", "retry", "expired", "superseded")

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
    extra        TEXT NOT NULL DEFAULT '',
    exec_state   TEXT NOT NULL DEFAULT 'applied',
    applied_ts   REAL NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_gov_ts ON gov_decision(ts);
CREATE INDEX IF NOT EXISTS ix_gov_sub ON gov_decision(subject_kind, subject_id);
"""

_COLS = (
    ("exec_state", "TEXT NOT NULL DEFAULT 'applied'"),
    ("applied_ts", "REAL NOT NULL DEFAULT 0"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
)


def ensure(conn):
    conn.executescript(_DDL)
    have = set()
    for r in conn.execute("PRAGMA table_info(gov_decision)").fetchall():
        have.add(str(r[1]))
    for name, decl in _COLS:
        if name not in have:
            conn.execute("ALTER TABLE gov_decision ADD COLUMN %s %s" % (name, decl))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_gov_exec ON gov_decision(exec_state)")
    conn.commit()


def _default_state(action):
    if action in ("pending", "needs_user"):
        return "pending"
    return "applied"


def decide(conn, subject_kind, subject_id, action, reason="", source="auto",
           conflicts=0, extra="", now=None, exec_state=None):
    """记一条治理判定。action 必须是八种之一，否则抛——宁可炸，不要脏口径。"""
    a = str(action or "").strip()
    if a not in DECISIONS:
        raise ValueError("不认识的治理结论：%r（只认八种）" % (action,))
    st = str(exec_state or _default_state(a)).strip()
    if st not in EXEC_STATES:
        raise ValueError("不认识的执行状态：%r（只认 %s）" % (exec_state, "/".join(EXEC_STATES)))
    t = float(now if now is not None else time.time())
    cur = conn.execute(
        "INSERT INTO gov_decision(ts,subject_kind,subject_id,action,reason,source,conflicts,"
        "applied,extra,exec_state,applied_ts,attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (t, str(subject_kind or "mem"), int(subject_id or 0), a, str(reason or ""),
         str(source or "auto"), int(conflicts or 0),
         1 if (a in APPLIED and st == "applied") else 0, str(extra or ""),
         st, t if st == "applied" else 0.0, 1 if st == "applied" else 0))
    conn.commit()
    return {"ok": True, "id": int(cur.lastrowid), "action": a,
            "applied": a in APPLIED and st == "applied", "exec_state": st}


def mark(conn, gid, exec_state, note="", now=None):
    """把一条判定推到新执行状态。attempts +1；applied 时记 applied_ts。"""
    st = str(exec_state or "").strip()
    if st not in EXEC_STATES:
        raise ValueError("不认识的执行状态：%r" % (exec_state,))
    t = float(now if now is not None else time.time())
    row = conn.execute(
        "SELECT extra, attempts, applied_ts FROM gov_decision WHERE id=?", (int(gid),)).fetchone()
    if not row:
        return {"ok": False, "error": "no_such_decision"}
    extra = str(row[0] or "")
    if note:
        extra = (extra + " | " if extra else "") + "%s@%s" % (str(note)[:120], int(t))
    applied_ts = t if st == "applied" else float(row[2] or 0.0)
    conn.execute(
        "UPDATE gov_decision SET exec_state=?, attempts=?, applied_ts=?, extra=?,"
        " applied=CASE WHEN ?='applied' THEN applied ELSE applied END WHERE id=?",
        (st, int(row[1] or 0) + 1, applied_ts, extra[:800], st, int(gid)))
    conn.commit()
    return {"ok": True, "id": int(gid), "exec_state": st}


def log(conn, limit=20, exec_state=None):
    sql = ("SELECT id, ts, subject_kind, subject_id, action, reason, source, conflicts,"
           " applied, exec_state, applied_ts, attempts FROM gov_decision")
    args = []
    if exec_state:
        sql += " WHERE exec_state=?"
        args.append(str(exec_state))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "ts": r[1], "subject_kind": r[2], "subject_id": r[3],
            "action": r[4], "reason": r[5], "source": r[6], "conflicts": r[7],
            "applied": r[8], "exec_state": r[9], "applied_ts": r[10], "attempts": r[11]}
        out.append({k: (int(v) if isinstance(v, float) and v == int(v) else v)
                    for k, v in dict(d).items()})
    return out


def stats(conn):
    d = {a: 0 for a in DECISIONS}
    for a, n in conn.execute("SELECT action, COUNT(*) FROM gov_decision GROUP BY action").fetchall():
        d[str(a)] = int(n)
    e = {s: 0 for s in EXEC_STATES}
    for s, n in conn.execute("SELECT exec_state, COUNT(*) FROM gov_decision GROUP BY exec_state").fetchall():
        e[str(s)] = int(n)
    total = sum(d.values())
    return {"total": total, "by_action": d, "by_state": e,
            "unfinished": e["pending"] + e["retry"] + e["failed"]}
