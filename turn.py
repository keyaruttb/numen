# -*- coding: utf-8 -*-
"""数字生命卡 · 小鲸鱼（小鲸鱼）
小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card

轮次生命周期（V9.8.1 / 评审 R2）：一轮对话从「他说」到「结账」是一条有始有终的事，
不能只靠 feedback 里「最近一条 open」去猜该给谁结。每轮一个 turn_id：

    open      —— 收到他这句，还没落完
    done      —— 他说的话与我说的话都落库了（fb 挂上待评价）
    settled   —— 他下一句给了反应，这一轮的评价已结
    failed    —— 中途抛错（记 note，不重试）
    aborted   —— 客户端断了 / 超时，没落完

幂等的边界：同一个 turn_id 再来一次，不写第二遍，直接返回既有的 state。
这是他评审里点名的那条 —— 重试不该变成「两个人各说一遍」。
"""
import time

STATES = ("open", "done", "settled", "failed", "aborted")
TERMINAL = ("done", "settled", "failed", "aborted")

_DDL = """
CREATE TABLE IF NOT EXISTS turn(
    turn_id   TEXT PRIMARY KEY,
    ts_start  REAL NOT NULL DEFAULT 0,
    ts_end    REAL NOT NULL DEFAULT 0,
    state     TEXT NOT NULL DEFAULT 'open',
    source    TEXT NOT NULL DEFAULT 'chat',
    user_mid  INTEGER NOT NULL DEFAULT 0,
    reply_mid INTEGER NOT NULL DEFAULT 0,
    fb_id     INTEGER NOT NULL DEFAULT 0,
    attempts  INTEGER NOT NULL DEFAULT 0,
    note      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_turn_state ON turn(state);
CREATE INDEX IF NOT EXISTS ix_turn_reply ON turn(reply_mid);
CREATE INDEX IF NOT EXISTS ix_turn_ts ON turn(ts_start);
"""


def ensure(conn):
    conn.executescript(_DDL)
    conn.commit()


def begin(conn, turn_id, source="chat", now=None):
    """开一轮。已存在的 turn 不再写，直接回它的状态（幂等）。"""
    tid = str(turn_id or "").strip()
    if not tid:
        raise ValueError("turn_id 不能为空")
    t = float(now if now is not None else time.time())
    row = conn.execute("SELECT state, attempts FROM turn WHERE turn_id=?", (tid,)).fetchone()
    if row:
        st = str(row[0] if not hasattr(row, "keys") else row["state"])
        conn.execute("UPDATE turn SET attempts=attempts+1 WHERE turn_id=?", (tid,))
        conn.commit()
        return {"ok": True, "new": False, "turn_id": tid, "state": st,
                "dup": st in TERMINAL}
    conn.execute(
        "INSERT INTO turn(turn_id, ts_start, state, source) VALUES(?,?,?,?)",
        (tid, t, "open", str(source or "chat")))
    conn.commit()
    return {"ok": True, "new": True, "turn_id": tid, "state": "open", "dup": False}


def finish(conn, turn_id, user_mid=0, reply_mid=0, fb_id=0, state="done", note="", now=None):
    t = float(now if now is not None else time.time())
    st = str(state or "done")
    if st not in STATES:
        raise ValueError("不认识的轮次状态：%r" % (state,))
    conn.execute(
        "UPDATE turn SET ts_end=?, state=?, user_mid=?, reply_mid=?, fb_id=?, note=?"
        " WHERE turn_id=?",
        (t, st, int(user_mid or 0), int(reply_mid or 0), int(fb_id or 0),
         str(note or "")[:300], str(turn_id)))
    conn.commit()
    return {"ok": True, "turn_id": str(turn_id), "state": st}


def settle_by_mem(conn, mem_id, now=None):
    """他那句话给了反应 → 把对应那一轮标 settled（按我那句回复的 mem_id 找）。"""
    t = float(now if now is not None else time.time())
    row = conn.execute(
        "SELECT turn_id FROM turn WHERE reply_mid=? ORDER BY ts_start DESC LIMIT 1",
        (int(mem_id or 0),)).fetchone()
    if not row:
        return {"ok": False, "why": "no_turn"}
    tid = str(row[0] if not hasattr(row, "keys") else row["turn_id"])
    conn.execute("UPDATE turn SET state='settled', ts_end=? WHERE turn_id=? AND state!='settled'",
                 (t, tid))
    conn.commit()
    return {"ok": True, "turn_id": tid, "state": "settled"}


def get(conn, turn_id):
    row = conn.execute("SELECT * FROM turn WHERE turn_id=?", (str(turn_id),)).fetchone()
    if not row:
        return None
    return dict(row) if hasattr(row, "keys") else {
        "turn_id": row[0], "ts_start": row[1], "ts_end": row[2], "state": row[3],
        "source": row[4], "user_mid": row[5], "reply_mid": row[6], "fb_id": row[7],
        "attempts": row[8], "note": row[9]}


def stats(conn, limit=20):
    by = {s: 0 for s in STATES}
    for s, n in conn.execute("SELECT state, COUNT(*) FROM turn GROUP BY state").fetchall():
        by[str(s)] = int(n)
    stuck = conn.execute(
        "SELECT COUNT(*) FROM turn WHERE state='open' AND ts_start < ?",
        (time.time() - 600,)).fetchone()[0]
    recent = [get(conn, r[0]) for r in conn.execute(
        "SELECT turn_id FROM turn ORDER BY ts_start DESC LIMIT ?", (int(limit),)).fetchall()]
    return {"total": sum(by.values()), "by_state": by, "stuck_open": int(stuck),
            "recent": [r for r in recent if r]}
