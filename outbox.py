# -*- coding: utf-8 -*-
# 小鲸鱼 · 写入 outbox（幂等 + 退避重试 + 死信）
# 由 jiang（他）与 Claude 一起维护。改动请照 V9 铁律来。
"""写入 outbox：主提交先落，派生步骤失败就排队重来。

为什么要有这个：一次 remember 里除了正文，还要派生突触边、事实、印象。这些步骤
现在都是「失败就 soft_fail 记一笔」，也就是**悄悄丢了**——正文在，边没了，以后
召回就少一条线索。outbox 只做一件事：把没做成的派生记下来，退避重试，
真做不成就进死信，等人看。
"""

import json
import time

STATE = ("queued", "running", "done", "dead")
MAX_ATTEMPTS = 5
BACKOFF = (30.0, 120.0, 600.0, 1800.0, 7200.0)

_DDL = (
    "CREATE TABLE IF NOT EXISTS write_outbox("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " idem TEXT NOT NULL UNIQUE,"
    " kind TEXT NOT NULL,"
    " ref_id INTEGER,"
    " payload TEXT DEFAULT '',"
    " state TEXT DEFAULT 'queued',"
    " attempts INTEGER DEFAULT 0,"
    " next_ts REAL DEFAULT 0,"
    " last_error TEXT DEFAULT '',"
    " created_ts REAL DEFAULT 0,"
    " updated_ts REAL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS ix_ob_state ON write_outbox(state, next_ts)",
    "CREATE INDEX IF NOT EXISTS ix_ob_ref ON write_outbox(kind, ref_id)",
)


def ensure(conn):
    for sql in _DDL:
        conn.execute(sql)
    conn.commit()


def enqueue(conn, kind, ref_id=None, payload="", idem=None, now=None):
    """记一条待重试。同一个 idem 再来只更新错误，不重复排队（幂等）。"""
    now = time.time() if now is None else float(now)
    idem = str(idem or "%s:%s" % (kind, ref_id))
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO write_outbox(idem,kind,ref_id,payload,state,attempts,next_ts,"
        "last_error,created_ts,updated_ts) VALUES(?,?,?,?,'queued',0,?,?,?,?)"
        " ON CONFLICT(idem) DO UPDATE SET state='queued', updated_ts=excluded.updated_ts",
        (idem, str(kind), None if ref_id is None else int(ref_id), str(payload or ""),
         now, "", now, now))
    conn.commit()
    return cur.lastrowid


def due(conn, now=None, limit=5):
    now = time.time() if now is None else float(now)
    rows = conn.execute(
        "SELECT * FROM write_outbox WHERE state='queued' AND next_ts<=?"
        " ORDER BY next_ts LIMIT ?", (now, int(limit))).fetchall()
    return [dict(r) for r in rows]


def claim(conn, oid, now=None):
    now = time.time() if now is None else float(now)
    conn.execute("UPDATE write_outbox SET state='running', attempts=attempts+1,"
                 " updated_ts=? WHERE id=?", (now, int(oid)))
    conn.commit()


def done(conn, oid, now=None):
    now = time.time() if now is None else float(now)
    conn.execute("UPDATE write_outbox SET state='done', last_error='', updated_ts=?"
                 " WHERE id=?", (now, int(oid)))
    conn.commit()


def fail(conn, oid, err="", now=None):
    """失败：不到上限就退避重排，到了上限进死信（不静默丢）。"""
    now = time.time() if now is None else float(now)
    r = conn.execute("SELECT attempts FROM write_outbox WHERE id=?", (int(oid),)).fetchone()
    n = int((r["attempts"] if r else 0) or 0) + 1
    if n >= MAX_ATTEMPTS:
        conn.execute("UPDATE write_outbox SET state='dead', attempts=?, last_error=?,"
                     " updated_ts=? WHERE id=?", (n, str(err)[:300], now, int(oid)))
    else:
        wait = BACKOFF[min(n - 1, len(BACKOFF) - 1)]
        conn.execute("UPDATE write_outbox SET state='queued', attempts=?, next_ts=?,"
                     " last_error=?, updated_ts=? WHERE id=?",
                     (n, now + wait, str(err)[:300], now, int(oid)))
    conn.commit()
    return n >= MAX_ATTEMPTS


def stats(conn):
    out = {"queued": 0, "running": 0, "done": 0, "dead": 0, "by_kind": {}}
    for r in conn.execute("SELECT state, kind, COUNT(*) n FROM write_outbox GROUP BY state, kind"):
        out[r["state"]] = out.get(r["state"], 0) + int(r["n"] or 0)
        if r["state"] in ("queued", "dead"):
            out["by_kind"][r["kind"]] = out["by_kind"].get(r["kind"], 0) + int(r["n"] or 0)
    return out


def listing(conn, state=None, limit=20):
    if state:
        rows = conn.execute("SELECT * FROM write_outbox WHERE state=? ORDER BY id DESC LIMIT ?",
                            (str(state), int(limit))).fetchall()
    else:
        rows = conn.execute("SELECT * FROM write_outbox ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
    return [dict(r) for r in rows]
