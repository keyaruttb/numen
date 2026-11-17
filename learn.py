# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台机器上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wangjunhan68-byte · https://github.com/wangjunhan68-byte/whale-card

"""主动学习：学习队列（learn_task）+ 笔记落卡。

设计口径（2026-11-17）：
- 队列只存「想学什么」，不存答案 —— 材料要真去找，不能凭卡里已有的东西假装学过。
- 落卡走 region='study'（半衰期 7 天，见 poolmath.R_STUDY），同时留一条 fact，
  这样「学过的东西」既能被原话召回，也能被事实层引用。
- 执行者是有网的那一侧（宿主 agent / QQ 桥），卡只负责「记着要学」和「记住学到了」。

小鲸鱼 × 使用者
"""
import time

DDL = """
CREATE TABLE IF NOT EXISTS learn_task (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  topic     TEXT NOT NULL,
  why       TEXT DEFAULT '',
  source    TEXT DEFAULT 'self',
  prio      REAL DEFAULT 0.5,
  status    TEXT DEFAULT 'queued',
  ts        REAL,
  claim_ts  REAL,
  done_ts   REAL,
  mem_id    INTEGER,
  note      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_learn_status ON learn_task(status, prio DESC, ts);
"""

STALE_SEC = 3600.0
MAX_QUEUE = 60

_ASK = ("想学", "怎么学", "是什么", "什么意思", "为什么", "求教", "教我", "了解一下",
        "怎么回事", "原理", "能不能学", "在哪学", "推荐")


def ensure(conn):
    conn.executescript(DDL)
    conn.commit()


def enqueue(conn, topic, why="", source="self", prio=0.5, now=None):
    topic = (topic or "").strip()
    if len(topic) < 2:
        return {"ok": False, "err": "topic_too_short"}
    now = time.time() if now is None else now
    dup = conn.execute(
        "SELECT id FROM learn_task WHERE topic=? AND status IN ('queued','doing')", (topic,)
    ).fetchone()
    if dup:
        return {"ok": True, "id": dup[0], "dup": True}
    n = conn.execute(
        "SELECT COUNT(*) FROM learn_task WHERE status IN ('queued','doing')"
    ).fetchone()[0]
    if n >= MAX_QUEUE:
        return {"ok": False, "err": "queue_full", "n": n}
    cur = conn.execute(
        "INSERT INTO learn_task(topic, why, source, prio, status, ts) VALUES(?,?,?,?,'queued',?)",
        (topic, (why or "")[:400], (source or "self")[:40], float(prio), now),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "dup": False}


def claim(conn, now=None):
    now = time.time() if now is None else now
    _reap(conn, now)
    r = conn.execute(
        "SELECT * FROM learn_task WHERE status='queued' ORDER BY prio DESC, ts ASC LIMIT 1"
    ).fetchone()
    if not r:
        return {"ok": True, "task": None}
    conn.execute("UPDATE learn_task SET status='doing', claim_ts=? WHERE id=?", (now, r["id"]))
    conn.commit()
    return {"ok": True, "task": dict(dict(r), status="doing", claim_ts=now)}


def finish(conn, tid, mem_id=None, note="", now=None):
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE learn_task SET status='done', done_ts=?, mem_id=?, note=? WHERE id=?",
        (now, mem_id, (note or "")[:2000], int(tid)),
    )
    conn.commit()
    return {"ok": True, "id": int(tid)}


def drop(conn, tid, why=""):
    conn.execute("UPDATE learn_task SET status='dropped', note=? WHERE id=?",
                 ((why or "")[:400], int(tid)))
    conn.commit()
    return {"ok": True, "id": int(tid)}


def _reap(conn, now):
    conn.execute(
        "UPDATE learn_task SET status='queued', claim_ts=NULL "
        "WHERE status='doing' AND claim_ts IS NOT NULL AND claim_ts < ?",
        (now - STALE_SEC,),
    )
    conn.commit()


def due(conn, now=None):
    now = time.time() if now is None else now
    _reap(conn, now)
    return conn.execute("SELECT COUNT(*) FROM learn_task WHERE status='queued'").fetchone()[0]


def listing(conn, limit=20, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM learn_task WHERE status=? ORDER BY prio DESC, ts DESC LIMIT ?",
            (status, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learn_task WHERE status IN ('queued','doing') "
            "ORDER BY prio DESC, ts ASC LIMIT ?", (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def stats(conn, now=None):
    now = time.time() if now is None else now
    q = conn.execute("SELECT COUNT(*) FROM learn_task WHERE status='queued'").fetchone()[0]
    d = conn.execute("SELECT COUNT(*) FROM learn_task WHERE status='doing'").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM learn_task WHERE status='done'").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM learn_task WHERE status='done' AND done_ts >= ?",
        (now - 86400.0,),
    ).fetchone()[0]
    return {"queued": q, "doing": d, "done": done, "done_24h": today}


def looks_like_question(text):
    t = (text or "").strip()
    if len(t) < 6 or len(t) > 200:
        return False
    if t.endswith("？") or t.endswith("?"):
        return True
    return any(k in t for k in _ASK)


def seed_from(conn, texts, source="chat", limit=3, prio=0.6, now=None):
    out = []
    for t in texts or []:
        if len(out) >= limit:
            break
        t = (t or "").strip().replace("\n", " ")
        if t.startswith("【") or t.startswith("["):
            continue
        if not looks_like_question(t):
            continue
        topic = t[:40]
        r = enqueue(conn, topic, why="他自己问过：" + t[:120], source=source, prio=prio, now=now)
        if r.get("ok") and not r.get("dup"):
            out.append({"id": r["id"], "topic": topic})
    return out


def render_queue(conn, limit=8):
    st = stats(conn)
    rows = listing(conn, limit=limit)
    lines = ["学习队列：排队 %d / 在手 %d / 已学 %d（24h %d）"
             % (st["queued"], st["doing"], st["done"], st["done_24h"])]
    for r in rows:
        lines.append("- #%d %s%s" % (r["id"], r["topic"],
                                     ("（%s）" % r["why"][:60]) if r.get("why") else ""))
    if not rows:
        lines.append("- （空）")
    return "\n".join(lines)
