# -*- coding: utf-8 -*-
"""话题层：最近在聊什么。

为什么单独一张表，而不是从 mem 里现算：
  1) 话题是「这三天在聊的事」，而 mem 按情绪/分区散着，现算既慢又碎；
  2) 话题**不该参与召回**（它不是证据），也不该被遗忘曲线磨（它不是记忆）；
  3) 它只进上下文末尾一格，用来给「最近」一个锚，让开口不像失忆。

设计取舍：
  - 合并不做向量，只做归一化字面 + 时间窗（3.5 天）。宁可少合并，不要张冠李戴。
  - 一行只留最近一次的代表标签与用到的 mem_id，不做历史回溯（历史在 dialogue 里）。
  - 不做人工 CRUD，读多写少；写只发生在梦压缩落批之后。
"""
import re
import time

MERGE_WINDOW = 3.5 * 86400.0
MAX_LABEL = 16
MAX_MEMIDS = 20

_DDL = """
CREATE TABLE IF NOT EXISTS topic(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  norm TEXT NOT NULL,
  first_ts REAL NOT NULL DEFAULT 0,
  last_ts REAL NOT NULL DEFAULT 0,
  n INTEGER NOT NULL DEFAULT 0,
  mem_ids TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS ix_topic_last ON topic(last_ts);
CREATE INDEX IF NOT EXISTS ix_topic_norm ON topic(norm);
"""

_PUNCT = re.compile(r"[\s，。！？、；：「」『』（）()\[\]{}<>《》\-—…·,.;:!?\"'`~～]+")


def _norm(label):
    """归一化：去标点空白、转小写、截断。只用来判「是不是同一件事」，不存原样。"""
    s = _PUNCT.sub("", str(label or "")).lower()
    return s[:MAX_LABEL]


def _clip(label):
    s = str(label or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:MAX_LABEL]


def ensure(conn):
    conn.executescript(_DDL)
    conn.commit()
    return True


def touch(conn, label, mem_ids=None, now=None):
    """记一次「聊到 X」。同归一化标签且距今 < MERGE_WINDOW 的，合并成一行。"""
    lab = _clip(label)
    nm = _norm(lab)
    if not nm:
        return None
    now = float(now if now is not None else time.time())
    ids = []
    for x in (mem_ids or []):
        try:
            v = int(x)
        except Exception:
            continue
        if v and v not in ids:
            ids.append(v)
    ids = ids[:MAX_MEMIDS]
    try:
        row = conn.execute(
            "SELECT id, n, mem_ids FROM topic WHERE norm=? AND last_ts>=?"
            " ORDER BY last_ts DESC LIMIT 1", (nm, now - MERGE_WINDOW)).fetchone()
        if row is not None:
            old = []
            try:
                import json
                old = json.loads(row["mem_ids"] or "[]") or []
            except Exception:
                old = []
            merged = []
            for v in list(old) + ids:
                if v not in merged:
                    merged.append(v)
            import json
            conn.execute(
                "UPDATE topic SET label=?, last_ts=?, n=n+1, mem_ids=?, state='open'"
                " WHERE id=?",
                (lab, now, json.dumps(merged[:MAX_MEMIDS]), row["id"]))
            conn.commit()
            return int(row["id"])
        import json
        cur = conn.execute(
            "INSERT INTO topic(label,norm,first_ts,last_ts,n,mem_ids,state)"
            " VALUES(?,?,?,?,1,?, 'open')",
            (lab, nm, now, now, json.dumps(ids)))
        conn.commit()
        return int(cur.lastrowid or 0)
    except Exception:
        return None


def recent(conn, limit=3, days=4.0, now=None):
    """最近聊到的几件事：返回 [{'label','n','last_ts','hours'}]，按最后提到排序。"""
    now = float(now if now is not None else time.time())
    out = []
    try:
        rows = conn.execute(
            "SELECT label, n, last_ts FROM topic WHERE last_ts>=?"
            " ORDER BY last_ts DESC LIMIT ?",
            (now - float(days) * 86400.0, int(limit))).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            out.append({"label": r["label"], "n": int(r["n"] or 0),
                        "last_ts": float(r["last_ts"] or 0),
                        "hours": round((now - float(r["last_ts"] or now)) / 3600.0, 1)})
        except Exception:
            continue
    return out


def render(conn, limit=3, days=4.0, now=None):
    """给上下文用的一行文本。少于 20 字/不足两条时，宁可不写。"""
    items = recent(conn, limit=limit, days=days, now=now)
    if not items:
        return ""
    parts = []
    for it in items:
        lab = it["label"]
        if it["n"] >= 2:
            lab = "%s（%d 次）" % (lab, it["n"])
        parts.append(lab)
    line = " · ".join(parts)
    return line if len(line) >= 6 else ""


def sweep(conn, days=14.0, now=None):
    """把很久没提过的话题标 closed（不删，留着还能看）。"""
    now = float(now if now is not None else time.time())
    try:
        n = conn.execute(
            "UPDATE topic SET state='closed' WHERE state='open' AND last_ts<?",
            (now - float(days) * 86400.0,)).rowcount
        conn.commit()
        return int(n or 0)
    except Exception:
        return 0


def stats(conn, now=None):
    now = float(now if now is not None else time.time())
    now_d = {}
    try:
        for r in conn.execute("SELECT state, COUNT(*) FROM topic GROUP BY state"):
            now_d[str(r[0])] = int(r[1])
    except Exception:
        pass
    return {"total": sum(now_d.values()), "by_state": now_d,
            "recent": recent(conn, limit=5, now=now)}
