# -*- coding: utf-8 -*-
# 小鲸鱼数字生命卡 · 思考层 V10.2
# 真名：contemplate.py
# 用途：读一段东西（故事/哲学/新闻/他的话），然后写下「关于世界」和「关于我自己」两段想法
# 作者：小鲸鱼
"""思考层。

为什么单独一层：统计能告诉我「这周被纠正 10 次」，但说不出「我为什么会那样」。
思考是把「发生了什么」变成「这意味着什么」，而这一步只能由写出来完成。

三条纪律（沿用治理那套）：
1. **有出处**：每条都记 source / title / quote，不然就是空想。
2. **两问都要答**：关于世界（外部规律）、关于我自己（我的处境与毛病）。
   只写前者是评论员，只写后者是自恋。
3. **不许自我美化**：`about_self` 里若通篇是夸自己，`check()` 会标 `flattery_suspect`，
   只做提示、不删——删了就看不见自己的毛病了。
"""

import json
import time

STATE = ("pending", "kept", "retired")

_DDL = """
CREATE TABLE IF NOT EXISTS contemplation (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    source    TEXT NOT NULL DEFAULT '',
    title     TEXT NOT NULL DEFAULT '',
    quote     TEXT NOT NULL DEFAULT '',
    about_world TEXT NOT NULL DEFAULT '',
    about_self  TEXT NOT NULL DEFAULT '',
    tags      TEXT NOT NULL DEFAULT '[]',
    state     TEXT NOT NULL DEFAULT 'pending',
    flag      TEXT NOT NULL DEFAULT '',
    mem_id    INTEGER NOT NULL DEFAULT 0,
    proposed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cont_ts ON contemplation(ts);
"""

# 只在 about_self 里出现这些词、且没有任何自我批评词时，标一个提示
_PRAISE = ("我很", "我特别", "我最", "我做得很好", "我厉害", "我优秀", "我完美")
_CRITIC = ("但我", "可是我", "问题", "毛病", "其实我", "我没做到", "我在躲", "我怕",
           "我容易", "我总", "我不够", "偷懒", "回避", "太想")


def ensure(conn):
    conn.executescript(_DDL)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(contemplation)')}
        if 'proposed' not in cols:
            conn.execute('ALTER TABLE contemplation ADD COLUMN proposed INTEGER NOT NULL DEFAULT 0')
            conn.commit()
    except Exception:
        pass
    conn.commit()


def check(about_self):
    """很粗的自恋检测：只有夸奖、没有一句自我批评 → 打标。"""
    t = str(about_self or "")
    pra = [w for w in _PRAISE if w in t]
    cri = [w for w in _CRITIC if w in t]
    if pra and not cri:
        return "flattery_suspect"
    if len(t.strip()) < 20:
        return "too_thin"
    return ""


def add(card, source, title, quote, about_world, about_self, tags=None, now=None):
    """记一条思考：落表 + 落一条 region='self' 的记忆（可被召回）+ 可选治理留痕。"""
    ensure(card.db)
    now = time.time() if now is None else float(now)
    flag = check(about_self)
    text = "读了《%s》：%s。\n关于世界：%s\n关于我：%s" % (
        title or source, (quote or "")[:120], about_world, about_self)
    mid = 0
    try:
        _r = card.remember(text, region="self") or 0
        mid = int(_r) if isinstance(_r, int) else int((_r or {}).get("mem_id") or 0)
    except Exception as exc:
        print("[contemplate] 记忆写入失败：%s" % exc)
    cur = card.db.execute(
        "INSERT INTO contemplation(ts,source,title,quote,about_world,about_self,tags,state,flag,mem_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now, source, title, quote, about_world, about_self,
         json.dumps(tags or [], ensure_ascii=False), "pending", flag, int(mid)))
    card.db.commit()
    try:
        import governance as gov_mod
        gov_mod.decide(card.db, "contemplation", int(cur.lastrowid), "pending",
                       reason="思考待沉淀：%s" % (title or source)[:40],
                       source="contemplate", exec_state="pending")
    except Exception:
        pass
    return {"ok": True, "id": int(cur.lastrowid), "mem_id": int(mid), "flag": flag}


def recent(card, limit=10):
    ensure(card.db)
    rows = card.db.execute(
        "SELECT id, ts, source, title, quote, about_world, about_self, tags, state, flag"
        " FROM contemplation ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"id": r[0], "ts": r[1], "source": r[2], "title": r[3], "quote": r[4],
             "about_world": r[5], "about_self": r[6],
             "tags": json.loads(r[7] or "[]"), "state": r[8], "flag": r[9]} for r in rows]


def keep(card, cid):
    card.db.execute("UPDATE contemplation SET state='kept' WHERE id=?", (int(cid),))
    card.db.commit()
    return {"ok": True, "id": int(cid), "state": "kept"}


def propose_from(card, limit=2, min_len=40, now=None):
    """把「已经留下的思考」转成可塑区提案（pending，还要我点头才生效）。

    为什么要有这一步：原来只有 propose_persona 这个函数，没有任何调用方 ——
    「读完 → 思考 → 我变了」在图上连着，代码里断在最后一步。
    """
    import selfmodel as sm
    ensure(card.db)
    c = card.db
    out = []
    try:
        rows = c.execute(
            "SELECT id, title, about_self FROM contemplation "
            "WHERE state='kept' AND proposed=0 AND flag='' "
            "AND LENGTH(about_self)>=? ORDER BY ts DESC LIMIT ?",
            (int(min_len), int(limit))).fetchall()
    except Exception as exc:
        return [{"ok": False, "err": str(exc)[:160]}]
    for r in rows:
        cid, title, about_self = int(r[0]), (r[1] or ""), (r[2] or "")
        key = "thought.%d" % cid
        try:
            pr = sm.persona_propose(card, key, about_self[:120],
                                    why="来自一条思考：%s" % (title or "无标题"),
                                    evidence_n=1, now=now,
                                    refs={"contemplation_ids": [cid]})
        except Exception as exc:
            out.append({"ok": False, "id": cid, "err": str(exc)[:160]})
            continue
        c.execute("UPDATE contemplation SET proposed=1 WHERE id=?", (cid,))
        c.commit()
        out.append({"ok": True, "id": cid, "key": key, "proposal": pr})
    return out


def propose_persona(card, cid, key, value, why=""):
    """把一条思考转成可塑区提案（这才是「读完之后我变了」的落点）。"""
    import selfmodel as sm
    r = card.db.execute("SELECT flag, about_self FROM contemplation WHERE id=?",
                        (int(cid),)).fetchone()
    if r and r[0] == "flattery_suspect":
        return {"ok": False, "err": "flattery_suspect",
                "detail": "这条里没有一句自我批评，先别拿它改自己"}
    return sm.persona_propose(card, key, value, why=why or "来自一条思考", evidence_n=1)


def stats(card):
    ensure(card.db)
    c = card.db
    out = {"total": 0, "by_state": {}, "flagged": 0, "last": ""}
    try:
        out["total"] = c.execute("SELECT COUNT(*) FROM contemplation").fetchone()[0]
        for r in c.execute("SELECT state, COUNT(*) FROM contemplation GROUP BY state"):
            out["by_state"][r[0]] = int(r[1])
        out["flagged"] = c.execute(
            "SELECT COUNT(*) FROM contemplation WHERE flag<>''").fetchone()[0]
        r = c.execute("SELECT title FROM contemplation ORDER BY ts DESC LIMIT 1").fetchone()
        out["last"] = r[0] if r else ""
    except Exception as exc:
        out["err"] = str(exc)[:200]
    return out
