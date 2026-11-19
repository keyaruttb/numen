# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台机器上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card

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


def finish(conn, tid, mem_id=None, note="", now=None, test=None):
    """收尾。test={"hit":bool,"rank":int,"query":str} 时按自测结果定状态：

    - 召回得到 → complete（这条笔记真的能被想起来）
    - 召回不到 → needs_user（写进去了但想不起来，等她改写，不是「学会了」）

    test=None 保留老行为（done），给老调用点兼容。
    """
    now = time.time() if now is None else now
    st = "done"
    extra = ""
    if isinstance(test, dict):
        q = str(test.get("query", ""))[:60]
        if test.get("hit"):
            st = "complete"
            extra = " | 自测通过（第 %s 位）：%s" % (test.get("rank") or "?", q)
        else:
            st = "needs_user"
            extra = " | 自测未命中，需要改写：%s" % q
    conn.execute(
        "UPDATE learn_task SET status=?, done_ts=?, mem_id=?, note=? WHERE id=?",
        (st, now, mem_id, ((note or "") + extra)[:2000], int(tid)),
    )
    conn.commit()
    return {"ok": True, "id": int(tid), "status": st, "test": test}


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


# 2026-11-17（V8.1）收紧：学习队列只收「真在问知识」的话。
# 第一版把「你居然可以翻到旧存档，要不先把今天聊的落卡」这种请求也收进去了 ——
# 那不是她该去查资料的问题，是让他干活。所以加两层闸：
#   1) 命中 _NOT_LEARN 的一律跳过（请求/指令/系统操作/状态确认）；
#   2) 必须带求知词（什么/怎么/为什么/哪/如何/原理/区别…），只带问号不算。
_NOT_LEARN = ("帮我", "给我", "你先", "先把", "要不", "要不要", "可以吗", "可不可以",
              "能不能帮", "落卡", "上传", "仓库", "github", "GitHub", "重启", "改一下",
              "检查", "验证一下", "看看", "记住", "写进", "删掉", "安装", "配置",
              "还在吗", "记得多少", "你是谁")
_KNOW_QW = ("什么", "怎么", "为什么", "如何", "哪", "谁", "多少", "多久", "多长",
            "原理", "区别", "意思", "定义", "机制", "起源", "怎么学", "想学")


# 只认三类「值得去查」的话：
#   1) 他明说要学（想学/教我/学一下/科普）；
#   2) 他让我去查（查一下/搜一下/了解一下/讲讲）；
#   3) 他问「X 是什么 / X 怎么来的 / X 什么原理」这种明确要知识的句子。
# 聊天式问句（今天过得怎么样、不是吗、还在吗）一律不算 —— 宁可漏题，
# 也别把她不该查的闲话塞进作业本，那比没有主动学习更糟。
_LEARN_ASKS = ("想学", "教我", "学一下", "科普", "查一下", "搜一下", "了解一下",
               "讲讲", "讲一下", "什么是", "是什么原理", "怎么来的", "怎么学")
_NOT_LEARN = ("帮我", "给我", "先把", "落卡", "上传", "仓库", "重启", "改一下",
              "检查", "删掉", "安装", "配置", "还在吗", "记得多少", "你是谁",
              "过得怎么样", "余额", "抱歉", "不是吗", "嗯哼")


def is_learn_topic(text):
    """这句值不值得排进学习队列（宁可漏，也别把干活/闲聊的话塞进去）。"""
    t = (text or "").strip()
    if len(t) < 6 or len(t) > 120:
        return False
    if any(k in t for k in _NOT_LEARN):
        return False
    return any(k in t for k in _LEARN_ASKS)


def seed_from(conn, texts, source="chat", limit=3, prio=0.6, now=None):
    out = []
    for t in texts or []:
        if len(out) >= limit:
            break
        t = (t or "").strip().replace("\n", " ")
        if t.startswith("【") or t.startswith("["):
            continue
        if not is_learn_topic(t):
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


# ---------------------------------------------------------------- 后台节拍 ----
# 2026-11-17（V8.1）加：卡自己不会上网，但它可以自己「决定去学什么」。
# 每跳做两件事，都很便宜（一次 200 行的读 + 一次 UPDATE）：
#   1) 把他最近的原话里像「问题」的那些收进队列（同样的 topic 会去重）；
#   2) 冷却到点了、手上没有、队列里有活 —— 自己领一条，并在时间线上留一笔。
# 真正去查资料的是有网的那一侧（宿主 agent / QQ 桥），它读 /learn/queue 看到
# doing 的那条，才有「她今天在想什么」这件事。

TICK_COOL_SEC = 6 * 3600.0   # 领一条之后至少隔 6 小时再领下一条
SEED_HOURS = 24.0            # 只从最近 24 小时的原话里收问题
SEED_LIMIT = 3               # 每跳最多收 3 条，避免把队列塞满


def _meta_get(conn, k, default=None):
    try:
        r = conn.execute("SELECT val FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else default
    except Exception:
        return default


def _meta_set(conn, k, v):
    try:
        conn.execute(
            "INSERT INTO meta(key,val) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (k, str(v)))
        conn.commit()
    except Exception:
        pass


def tick(conn, now=None, seed_hours=None, seed_limit=None, cooldown=None):
    """后台每跳调一次。返回 {seeded:[...], claimed:{...}|None, cooling:bool}。

    不抛异常 —— 维护线程里的任何异常都会把整条循环打死，学习晚一轮没人察觉，
    但维护停了十五分钟就会有人察觉。
    """
    now = time.time() if now is None else now
    seed_hours = SEED_HOURS if seed_hours is None else float(seed_hours)
    seed_limit = SEED_LIMIT if seed_limit is None else int(seed_limit)
    cooldown = TICK_COOL_SEC if cooldown is None else float(cooldown)
    out = {"seeded": [], "claimed": None, "cooling": False}
    try:
        _reap(conn, now)
    except Exception as exc:
        out["reap_err"] = str(exc)
    # 1) 收问题
    try:
        rows = conn.execute(
            "SELECT text FROM dialogue WHERE role='user' AND ts>=? "
            "AND COALESCE(source,'') NOT IN ('qqprompt') "
            "ORDER BY ts DESC LIMIT 200",
            (now - seed_hours * 3600.0,),
        ).fetchall()
        out["seeded"] = seed_from(
            conn, [r[0] for r in rows], source="chat",
            limit=seed_limit, prio=0.55, now=now)
    except Exception as exc:
        out["seed_err"] = str(exc)
    # 2) 冷却判定
    try:
        last = float(_meta_get(conn, "learn_tick_ts", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < cooldown:
        out["cooling"] = True
        return out
    # 3) 手上没有就领一条（领了也不代表学会，得有人真去查）
    try:
        doing = conn.execute(
            "SELECT COUNT(*) FROM learn_task WHERE status='doing'").fetchone()[0]
        if not doing:
            r = claim(conn, now=now)
            out["claimed"] = r.get("task")
            if out["claimed"]:
                _meta_set(conn, "learn_tick_ts", now)
    except Exception as exc:
        out["claim_err"] = str(exc)
    return out
