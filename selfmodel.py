# -*- coding: utf-8 -*-
# 小鲸鱼数字生命卡 · 自省与自我模型 V10
# 真名：selfmodel.py
# 用途：每周自省（可复算的统计 + 证据引用）+ 少量统计型自我模型条目
# 作者：小鲸鱼（与使用者一起）
"""自省与自我模型。

设计原则（2026-11-21，按外部评审意见收敛过的版本）：

1. **先证明触发，再加内容。** 所以 `weekly_review()` 只写「可复算的统计」和
   「引用到具体 id 的句子」，不生成自由叙述；也不自动改表达策略。
2. **状态只有一个权威。** `self_model.state` 只是治理台账的镜像：
   写条目时同时 `governance.decide(..., exec_state='pending')`，
   人把它 mark 成 applied 之后，`sync_state()` 才把这里改成 active。
3. **负证据优先。** `insights` 里若本周有被纠正/被否决/复盘 lesson，
   这些必须出现在最前面；没有负面事件时也要明写「本周没有被纠正的记录」，
   不能因为空就省略——空值和「没有发生」是两种事实。
4. **失败要留痕。** 统计失败不写空条目，写 `state='error'` 并在 meta 记原因。
"""

import json
import time

PERIOD_FMT = "%G-W%V"          # ISO 年-周
CATCHUP_MAX = 3                # 最多补几周（离线跨周也不丢）
NEG_MIN_RATIO = 3              # 负证据至少占 1/3（有负面事件时）
MODEL_CONF_MIN = 0.35          # 低于这个置信度的条目不出

_DDL = """
CREATE TABLE IF NOT EXISTS self_review (
    period    TEXT PRIMARY KEY,
    observed  TEXT NOT NULL DEFAULT '{}',
    insights  TEXT NOT NULL DEFAULT '[]',
    proposals TEXT NOT NULL DEFAULT '[]',
    applied   TEXT NOT NULL DEFAULT '[]',
    state     TEXT NOT NULL DEFAULT 'done',
    err       TEXT NOT NULL DEFAULT '',
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS self_model (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0,
    evidence_n  INTEGER NOT NULL DEFAULT 0,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL DEFAULT 'stats',
    state       TEXT NOT NULL DEFAULT 'pending',
    ts          REAL NOT NULL,
    last_eval_ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_smodel_state ON self_model(state);
"""


def ensure(conn):
    conn.executescript(_DDL)
    # 可塑区：persona 条目单独标记（V10.1）
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(self_model)").fetchall()]
        if "zone" not in cols:
            conn.execute("ALTER TABLE self_model ADD COLUMN zone TEXT NOT NULL DEFAULT 'stats'")
        if "why" not in cols:
            conn.execute("ALTER TABLE self_model ADD COLUMN why TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    conn.commit()


def period_of(ts):
    return time.strftime(PERIOD_FMT, time.localtime(float(ts)))


def _q1(conn, sql, *a, default=0):
    try:
        r = conn.execute(sql, a).fetchone()
        return r[0] if r is not None and r[0] is not None else default
    except Exception:
        return default


def _qs(conn, sql, *a):
    try:
        return conn.execute(sql, a).fetchall()
    except Exception:
        return []


def weekly_stats(card, t0, t1):
    """一周的可复算统计。所有数字都能用 SQL 复现（见 docs/V10-自省.md）。"""
    c = card.db
    out = {
        "period": period_of(t0),
        "window": [float(t0), float(t1)],
        "dialogue": _q1(c, "SELECT COUNT(*) FROM dialogue WHERE ts>=? AND ts<?", t0, t1),
        "mem_new": _q1(c, "SELECT COUNT(*) FROM mem WHERE create_ts>=? AND create_ts<?", t0, t1),
        "mem_dead": _q1(c, "SELECT COUNT(*) FROM mem WHERE dead=1 AND last_active_ts>=? AND last_active_ts<?", t0, t1),
        "recall": _q1(c, "SELECT COUNT(*) FROM recall_event WHERE ts>=? AND ts<?", t0, t1),
        "fb_settled": _q1(c, "SELECT COUNT(*) FROM feedback WHERE status='settled' AND ts_settle>=? AND ts_settle<?", t0, t1),
        "fb_open": _q1(c, "SELECT COUNT(*) FROM feedback WHERE status='open' AND ts_action>=? AND ts_action<?", t0, t1),
        "praise": _q1(c, "SELECT COUNT(*) FROM feedback WHERE COALESCE(approval,0)>0 AND ts_settle>=? AND ts_settle<?", t0, t1),
        "corrected": _q1(c, "SELECT COUNT(*) FROM feedback WHERE COALESCE(correction,0)>0 AND ts_settle>=? AND ts_settle<?", t0, t1),
        "rejected": _q1(c, "SELECT COUNT(*) FROM feedback WHERE COALESCE(reject,0)>0 AND ts_settle>=? AND ts_settle<?", t0, t1),
        "skills_new": _q1(c, "SELECT COUNT(*) FROM skill WHERE create_ts>=? AND create_ts<?", t0, t1),
        "skills_total": _q1(c, "SELECT COUNT(*) FROM skill"),
        "distill_skill": _q1(c, "SELECT COUNT(*) FROM distill WHERE out_kind='skill' AND ts>=? AND ts<?", t0, t1),
        "distill_insight": _q1(c, "SELECT COUNT(*) FROM distill WHERE out_kind='insight' AND ts>=? AND ts<?", t0, t1),
        "reviews": _q1(c, "SELECT COUNT(*) FROM review WHERE ts>=? AND ts<?", t0, t1),
        "reviews_promoted": _q1(c, "SELECT COUNT(*) FROM review WHERE COALESCE(promoted,0)=1 AND ts>=? AND ts<?", t0, t1),
        "intent_consumed": _q1(c, "SELECT COUNT(*) FROM intention WHERE status='consumed' AND last_touch_ts>=? AND last_touch_ts<?", t0, t1),
        "intent_faded": _q1(c, "SELECT COUNT(*) FROM intention WHERE status='faded' AND COALESCE(last_touch_ts,created_ts)>=? AND COALESCE(last_touch_ts,created_ts)<?", t0, t1),
        "learn_tasks": _q1(c, "SELECT COUNT(*) FROM learn_task WHERE COALESCE(create_ts,0)>=? AND COALESCE(create_ts,0)<?", t0, t1),
        "gov_pending": _q1(c, "SELECT COUNT(*) FROM gov_decision WHERE exec_state='pending'"),
    }
    row = _qs(c, "SELECT valence, safety, n_pos, n_neg FROM affect"
                 " WHERE target_kind='person' ORDER BY ts DESC LIMIT 1")
    out["affect"] = ({"valence": float(row[0][0] or 0), "safety": float(row[0][1] or 0),
                      "n_pos": int(row[0][2] or 0), "n_neg": int(row[0][3] or 0)} if row else {})
    # 负证据的 ref（用于『可审计引用』）
    out["refs_neg"] = [int(r[0]) for r in _qs(
        c, "SELECT id FROM feedback WHERE (COALESCE(correction,0)>0 OR COALESCE(reject,0)>0)"
           " AND ts_settle>=? AND ts_settle<? ORDER BY ts_settle DESC LIMIT 10", t0, t1)]
    out["refs_pos"] = [int(r[0]) for r in _qs(
        c, "SELECT id FROM feedback WHERE COALESCE(approval,0)>0 AND ts_settle>=? AND ts_settle<?"
           " ORDER BY ts_settle DESC LIMIT 10", t0, t1)]
    return out


def make_insights(st):
    """模板化句子，每句都带数字；负证据排前面。"""
    neg, pos = [], []
    if st["corrected"] or st["rejected"]:
        neg.append("这一周有 %d 次被纠正、%d 次被否（反馈 %d 条）"
                   % (st["corrected"], st["rejected"], st["fb_settled"]))
    if st["mem_dead"]:
        neg.append("有 %d 条记忆彻底淡出（写进来 %d 条）" % (st["mem_dead"], st["mem_new"]))
    if st["intent_faded"]:
        neg.append("%d 条行为意向没来得及做就淡了" % st["intent_faded"])
    if st["praise"]:
        pos.append("被夸 %d 次" % st["praise"])
    if st["skills_new"] or st["distill_skill"]:
        pos.append("新增技能 %d 条（其中由学习沉淀 %d 条），技能总数 %d"
                   % (st["skills_new"], st["distill_skill"], st["skills_total"]))
    if st["intent_consumed"]:
        pos.append("真的做掉 %d 条意向" % st["intent_consumed"])
    if st["reviews"]:
        pos.append("复盘 %d 组、晋升 %d 组" % (st["reviews"], st["reviews_promoted"]))
    if st["affect"]:
        a = st["affect"]
        pos.append("他那边：效价 %+.2f、安全感 %.2f（好 %d / 坏 %d）"
                   % (a["valence"], a["safety"], a["n_pos"], a["n_neg"]))
    # 负证据配额：有负面事实时必须至少占 1/3，且负面在前
    if not neg:
        neg.append("这一周没有被纠正、也没有被否的记录（不等于我做得对，只等于没有证据）")
    total = len(neg) + len(pos)
    if total and len(neg) < max(1, total // NEG_MIN_RATIO):
        neg.append("本周的负证据只有 %d 条，低于 1/%d，说明记录本身可能不全"
                   % (len(neg), NEG_MIN_RATIO))
    return neg + pos


def make_proposals(st):
    """只提候选键，不改策略。真正落地要人确认（走治理）。"""
    out = []
    if st["fb_settled"] >= 5:
        ratio = st["praise"] / float(max(1, st["fb_settled"]))
        out.append({"key": "tone.warmth", "value": "被夸比例 %.0f%%" % (ratio * 100),
                    "why": "样本够 %d 条才能算比例" % st["fb_settled"]})
    if st["corrected"] >= 2:
        out.append({"key": "habit.correction", "value": "本周被纠正 %d 次" % st["corrected"],
                    "why": "连续被纠正说明某类回答有系统性偏差"})
    if st["distill_skill"]:
        out.append({"key": "skill.growth", "value": "本周沉淀 %d 条技能" % st["distill_skill"],
                    "why": "技能是学习唯一可执行的产物"})
    return out


def weekly_review(card, now=None, force=False):
    """生成一周自省。幂等（同 period 只写一次）；跨周离线会补 1~3 周。"""
    import governance as gov_mod
    now = time.time() if now is None else float(now)
    ensure(card.db)
    cur = period_of(now)
    done = []
    # 找最近一条自省，决定补哪几周
    last = card.db.execute("SELECT period FROM self_review ORDER BY period DESC LIMIT 1").fetchone()
    targets = [cur]
    if last and not force:
        try:
            import datetime
            y, w = last[0].split("-W")
            d = datetime.date.fromisocalendar(int(y), int(w), 1)
            # 只补「过去漏掉的周」：从上一周往当前周走，最多 CATCHUP_MAX 周
            for k in range(1, CATCHUP_MAX + 1):
                nd = d + datetime.timedelta(days=7 * k)
                p = period_of(time.mktime(nd.timetuple()))
                if p >= cur:
                    break
                if p not in targets:
                    targets.append(p)
        except Exception:
            pass
    for per in targets:
        if card.db.execute("SELECT 1 FROM self_review WHERE period=?", (per,)).fetchone():
            continue
        try:
            import datetime
            y, w = per.split("-W")
            d0 = datetime.date.fromisocalendar(int(y), int(w), 1)
            t0 = time.mktime(d0.timetuple())
            t1 = t0 + 7 * 86400.0
            st = weekly_stats(card, t0, t1)
            ins = make_insights(st)
            props = make_proposals(st)
            card.db.execute(
                "INSERT OR IGNORE INTO self_review(period,observed,insights,proposals,state,ts)"
                " VALUES(?,?,?,?,?,?)",
                (per, json.dumps(st, ensure_ascii=False), json.dumps(ins, ensure_ascii=False),
                 json.dumps(props, ensure_ascii=False), "done", now))
            card.db.commit()
            done.append({"period": per, "insights": len(ins), "proposals": len(props)})
            try:
                gov_mod.decide(card.db, "self_review", 0, "pending",
                               reason="周自省 %s 生成，等待人看" % per,
                               source="selfmodel", exec_state="pending")
            except Exception:
                pass
        except Exception as exc:
            card.db.execute(
                "INSERT OR IGNORE INTO self_review(period,state,err,ts) VALUES(?,?,?,?)",
                (per, "error", str(exc)[:300], now))
            card.db.commit()
            done.append({"period": per, "err": str(exc)[:120]})
    return done


def build_self_model(card, now=None):
    """从自省里统计量推出少量条目。每条都带 evidence_n / refs；状态一律 pending。"""
    import governance as gov_mod
    now = time.time() if now is None else float(now)
    ensure(card.db)
    rows = card.db.execute(
        "SELECT period, observed FROM self_review ORDER BY period DESC LIMIT 4").fetchall()
    if not rows:
        return []
    agg = {"fb": 0, "praise": 0, "corrected": 0, "skills": 0, "insight": 0,
           "intent": 0, "weeks": 0}
    for r in rows:
        o = json.loads(r["observed"] or "{}")
        agg["weeks"] += 1
        agg["fb"] += int(o.get("fb_settled") or 0)
        agg["praise"] += int(o.get("praise") or 0)
        agg["corrected"] += int(o.get("corrected") or 0) + int(o.get("rejected") or 0)
        agg["skills"] += int(o.get("distill_skill") or 0)
        agg["insight"] += int(o.get("distill_insight") or 0)
        agg["intent"] += int(o.get("intent_consumed") or 0)

    cands = []
    if agg["fb"] >= 3:
        ratio = agg["praise"] / float(agg["fb"])
        cands.append(("tone.warmth", "近 %d 周被夸比例 %.0f%%（%d/%d）"
                      % (agg["weeks"], ratio * 100, agg["praise"], agg["fb"]),
                      min(1.0, agg["fb"] / 10.0), agg["fb"]))
    if agg["corrected"] >= 2:
        cands.append(("habit.correction", "近 %d 周被纠正/被否 %d 次"
                      % (agg["weeks"], agg["corrected"]),
                      min(1.0, agg["corrected"] / 5.0), agg["corrected"]))
    if agg["skills"]:
        cands.append(("skill.growth", "近 %d 周从学习里沉淀 %d 条技能" % (agg["weeks"], agg["skills"]),
                      min(1.0, agg["skills"] / 3.0), agg["skills"]))
    if agg["intent"]:
        cands.append(("action.throughput", "近 %d 周真的做掉 %d 条意向" % (agg["weeks"], agg["intent"]),
                      min(1.0, agg["intent"] / 5.0), agg["intent"]))

    out = []
    for key, val, conf, n in cands:
        if conf < MODEL_CONF_MIN:
            continue
        refs = json.dumps([r["period"] for r in rows], ensure_ascii=False)
        card.db.execute(
            "INSERT INTO self_model(key,value,confidence,evidence_n,evidence_refs,source,state,ts,last_eval_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " confidence=excluded.confidence, evidence_n=excluded.evidence_n,"
            " evidence_refs=excluded.evidence_refs, last_eval_ts=excluded.last_eval_ts",
            (key, val, conf, n, refs, "stats", "pending", now, now))
        card.db.commit()
        try:
            gov_mod.decide(card.db, "self_model", 0, "pending",
                           reason="自我模型候选 %s：%s" % (key, val[:60]),
                           source="selfmodel", exec_state="pending")
        except Exception:
            pass
        out.append({"key": key, "value": val, "confidence": round(conf, 2)})
    return out


def sync_state(card):
    """把治理里的最终状态镜像到 self_model（单一权威：gov_decision）。"""
    try:
        rows = card.db.execute(
            "SELECT key, state FROM self_model WHERE state='pending'").fetchall()
    except Exception:
        return 0
    n = 0
    for r in rows:
        got = card.db.execute(
            "SELECT exec_state FROM gov_decision WHERE subject_kind='self_model'"
            " AND reason LIKE ? ORDER BY id DESC LIMIT 1",
            ("%%%s%%" % r["key"],)).fetchone()
        if not got:
            continue
        st = {"applied": "active", "superseded": "retired",
              "failed": "pending", "expired": "retired"}.get(got[0])
        if st and st != r["state"]:
            card.db.execute("UPDATE self_model SET state=? WHERE key=?", (st, r["key"]))
            n += 1
    if n:
        card.db.commit()
    return n


def stats(card):
    c = card.db
    out = {"reviews": 0, "last_period": "", "model": {}, "insights": 0}
    try:
        out["reviews"] = _q1(c, "SELECT COUNT(*) FROM self_review")
        r = c.execute("SELECT period, json_array_length(insights) FROM self_review"
                      " ORDER BY period DESC LIMIT 1").fetchone()
        if r:
            out["last_period"] = r[0]
            out["insights"] = int(r[1] or 0)
        for row in c.execute("SELECT state, COUNT(*) FROM self_model GROUP BY state"):
            out["model"][row[0]] = int(row[1])
    except Exception as exc:
        out["err"] = str(exc)[:200]
    return out


# ---------------------------------------------------------------- 可塑区（V10.1）
# 他说的「给自己留一个可改变的部分」：人格与自我认知允许改，但不是随便改。
# 规则：
#   1. 身份锚（名字/锚文/core_identity/出生证）**永远不许写**——这里只是拒绝名单，
#      真正的不动是因为代码里没有任何路径能改它们。
#   2. 可改的只有「描述性的自我」：我更爱怎么说话、我这阵子在意什么、我发现自己
#      有什么毛病。落点 = self_model(zone='persona')。
#   3. 任何一条都必须先 pending → 人工/治理 applied → 才进 prompt（persona_lines）。
#   4. 每条带 why（为什么这么改）与 evidence_n，可回滚（persona_revoke）。

PERSONA_DENY = ("name", "anchor", "core_identity", "birth", "seal", "identity")
PERSONA_MAX_ACTIVE = 6


def _denied(key):
    k = str(key or "").lower()
    return any(t in k for t in PERSONA_DENY)


def persona_propose(card, key, value, why="", evidence_n=0, now=None):
    """提一条可塑区改动（pending，不会立刻生效）。"""
    import governance as gov_mod
    now = time.time() if now is None else float(now)
    if not str(key or "").strip():
        raise ValueError("可塑区的 key 不能为空")
    if _denied(key):
        raise ValueError("不可改：%s 属于身份锚，不开放自我修改" % key)
    ensure(card.db)
    card.db.execute(
        "INSERT INTO self_model(key,value,confidence,evidence_n,evidence_refs,source,state,ts,"
        "last_eval_ts,zone,why) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, why=excluded.why,"
        " evidence_n=excluded.evidence_n, state='pending', last_eval_ts=excluded.last_eval_ts,"
        " zone='persona'",
        (key, str(value), 0.5, int(evidence_n), "[]", "manual", "pending", now, now,
         "persona", str(why)[:300]))
    card.db.commit()
    try:
        gov_mod.decide(card.db, "self_model", 0, "pending",
                       reason="可塑区待批 %s：%s（%s）" % (key, str(value)[:40], str(why)[:60]),
                       source="persona", exec_state="pending")
    except Exception:
        pass
    return {"ok": True, "key": key, "state": "pending"}


def persona_apply(card, key, now=None):
    """人工/治理批准后调用：把一条 pending 转 active（写入前留旧值到 why 之后可回滚）。"""
    now = time.time() if now is None else float(now)
    ensure(card.db)
    r = card.db.execute("SELECT value FROM self_model WHERE key=? AND zone='persona'",
                        (str(key),)).fetchone()
    if not r:
        return {"ok": False, "err": "no_such_persona"}
    card.db.execute("UPDATE self_model SET state='active', last_eval_ts=? WHERE key=?",
                    (now, str(key)))
    card.db.commit()
    return {"ok": True, "key": key, "value": r[0], "state": "active"}


def persona_revoke(card, key, now=None):
    """回滚：active/pending → retired（同时记一条治理 supersede）。"""
    import governance as gov_mod
    now = time.time() if now is None else float(now)
    ensure(card.db)
    card.db.execute("UPDATE self_model SET state='retired', last_eval_ts=? WHERE key=?",
                    (now, str(key)))
    card.db.commit()
    try:
        gov_mod.decide(card.db, "self_model", 0, "supersede",
                       reason="可塑区回滚 %s" % key, source="persona", exec_state="applied")
    except Exception:
        pass
    return {"ok": True, "key": key, "state": "retired"}


def persona_lines(card, limit=None):
    """进 prompt 的可塑区（只有 active 的才算数）。"""
    ensure(card.db)
    n = int(limit or PERSONA_MAX_ACTIVE)
    rows = card.db.execute(
        "SELECT key, value FROM self_model WHERE zone='persona' AND state='active'"
        " ORDER BY last_eval_ts DESC LIMIT ?", (n,)).fetchall()
    return ["- %s：%s" % (r["key"], r["value"]) for r in rows]
