# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
"""反馈与自主反思闭环（V9.0 – V9.3 落地）。

一句话总纲：
    动作先落卡 → 等结果 → 折成奖励标量 r ∈ [-1,1] → r 先进激素/情绪底色
    → 再按方向惰性更新记忆、学习评价、行为印象、动机、行为意向、评价印象；
    反思条目单独存，攒够同类才允许碰自我叙事草稿，绝不直接改人格锚。

红线（代码层守住）：
  · 单条 |r| ≤ R_CAP(0.5)；结构层小步惰性
  · 负反馈只压权重/记印象/弱化联想边，不删记忆、不碰 identity_anchor
  · promoted 只是草稿标记；confirmed 只表示"采纳为参考结论"
  · trust_ok=0 只记录不结算
  · 事实对账只认已标 superseded 的旧说法，不做主动语义匹配

不强制依赖 LLM：severity_model / polisher 都是可选钩子，缺省纯规则。
"""

import json
import time

R_CAP = 0.5
POS_TH = 0.15
NEG_TH = -0.15
MEM_W_STEP = 0.04
MEM_SAL_STEP = 0.05
LEARN_VALUE_STEP = 0.10
MOTIV_STEP = 0.10
SEV_MILD = 0.35
SEV_STRONG = 0.80
SEV_AVOID_LINE = 0.55
INTENTION_FADE = 0.08
AFFECT_LR = 0.08

DECAY_HL = {"replay": 20.0, "explore": 30.0, "learn_more": 30.0,
            "stay": 26.0, "avoid_scene": 18.0, "self_check": 24.0}
KIND_DRIVE = {"replay": "competence", "explore": "curiosity",
              "learn_more": "curiosity", "stay": "attachment",
              "self_check": "order", "avoid_scene": "competence"}

DEFAULTS = {
    "fb_w_approval": 0.30,
    "fb_w_correction": 0.30,
    "fb_w_reject": 0.20,
    "fb_w_confirm_strong": 0.15,
    "fb_w_confirm_weak": 0.05,
    "fb_w_disconfirm": 0.15,
    "fb_w_reuse": 0.05,
    "fb_w_promise": 0.05,
    "fb_min_n": 3,
    "fb_max_n": 8,
    "fb_promo_strength": 0.60,
    "fb_half_life_days": 14.0,
    "fb_nightly_hours": 6.0,
    "fb_affect_lr": AFFECT_LR,
}

DDL = [
    """CREATE TABLE IF NOT EXISTS feedback (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'self_utterance',
        mem_id       INTEGER,
        dialogue_id  INTEGER,
        actor        TEXT NOT NULL DEFAULT 'self',
        ts_action    REAL NOT NULL,
        ts_settle    REAL,
        status       TEXT NOT NULL DEFAULT 'open',
        approval     REAL NOT NULL DEFAULT 0,
        correction   REAL NOT NULL DEFAULT 0,
        reject       REAL NOT NULL DEFAULT 0,
        confirm      REAL NOT NULL DEFAULT 0,
        confirm_weak REAL NOT NULL DEFAULT 0,
        confirm_level TEXT NOT NULL DEFAULT 'none',
        disconfirm   REAL NOT NULL DEFAULT 0,
        reuse        REAL NOT NULL DEFAULT 0,
        promise      REAL NOT NULL DEFAULT 0,
        severity     REAL NOT NULL DEFAULT 0.0,
        r            REAL NOT NULL DEFAULT 0,
        r_bucket     TEXT NOT NULL DEFAULT 'neutral',
        note         TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT 'explicit',
        trust_ok     INTEGER NOT NULL DEFAULT 1,
        ts           REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fb_status ON feedback(status, ts_settle)",
    "CREATE INDEX IF NOT EXISTS ix_fb_mem ON feedback(mem_id)",
    """CREATE TABLE IF NOT EXISTS learn_eval (
        mem_id             INTEGER PRIMARY KEY,
        value              REAL NOT NULL DEFAULT 0.5,
        times_used         INTEGER NOT NULL DEFAULT 0,
        times_confirmed    INTEGER NOT NULL DEFAULT 0,
        times_disconfirmed INTEGER NOT NULL DEFAULT 0,
        confirm_strength   REAL NOT NULL DEFAULT 0.0,
        last_eval_ts       REAL NOT NULL DEFAULT 0,
        verdict            TEXT NOT NULL DEFAULT 'open'
    )""",
    """CREATE TABLE IF NOT EXISTS review (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        mem_id           INTEGER,
        side             TEXT NOT NULL DEFAULT 'positive',
        angle            TEXT NOT NULL DEFAULT 'watch',
        summary          TEXT NOT NULL DEFAULT '',
        base_summary     TEXT NOT NULL DEFAULT '',
        polished_summary TEXT NOT NULL DEFAULT '',
        polished         INTEGER NOT NULL DEFAULT 0,
        fb_ids           TEXT NOT NULL DEFAULT '[]',
        related_mems     TEXT NOT NULL DEFAULT '[]',
        evidence_n       INTEGER NOT NULL DEFAULT 0,
        evidence_strength REAL NOT NULL DEFAULT 0.0,
        strength         REAL NOT NULL DEFAULT 0.0,
        promoted         INTEGER NOT NULL DEFAULT 0,
        confirmed        INTEGER NOT NULL DEFAULT 0,
        ts               REAL NOT NULL,
        ts_reviewed      REAL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_review_promo ON review(promoted)",
    """CREATE TABLE IF NOT EXISTS intention (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        fb_id        INTEGER,
        mem_id       INTEGER,
        direction    TEXT NOT NULL DEFAULT 'approach',
        kind         TEXT NOT NULL DEFAULT 'replay',
        drive        TEXT NOT NULL DEFAULT 'competence',
        scene_tag    TEXT NOT NULL DEFAULT '',
        urge         REAL NOT NULL DEFAULT 0.0,
        decay_hl     REAL NOT NULL DEFAULT 24.0,
        status       TEXT NOT NULL DEFAULT 'active',
        note         TEXT NOT NULL DEFAULT '',
        created_ts   REAL NOT NULL,
        last_touch_ts REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_int_dir ON intention(direction, status)",
    "CREATE INDEX IF NOT EXISTS ix_int_mem ON intention(mem_id)",
]


def ensure(conn):
    """建表 + 幂等。老库缺列就地 ALTER（只补已知列）。"""
    c = conn.cursor()
    for sql in DDL:
        c.execute(sql)
    want = {
        "feedback": [("confirm_weak", "REAL NOT NULL DEFAULT 0"),
                     ("confirm_level", "TEXT NOT NULL DEFAULT 'none'"),
                     ("source", "TEXT NOT NULL DEFAULT 'explicit'"),
                     ("trust_ok", "INTEGER NOT NULL DEFAULT 1")],
        "review": [("confirmed", "INTEGER NOT NULL DEFAULT 0"),
                   ("polished_summary", "TEXT NOT NULL DEFAULT ''"),
                   ("polished", "INTEGER NOT NULL DEFAULT 0"),
                   ("related_mems", "TEXT NOT NULL DEFAULT '[]'"),
                   ("evidence_strength", "REAL NOT NULL DEFAULT 0.0"),
                   ("base_summary", "TEXT NOT NULL DEFAULT ''")],
        "intention": [("scene_tag", "TEXT NOT NULL DEFAULT ''"),
                      ("decay_hl", "REAL NOT NULL DEFAULT 24.0"),
                      ("drive", "TEXT NOT NULL DEFAULT 'competence'")],
    }
    for table, cols in want.items():
        try:
            have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
        except Exception:
            continue
        for name, decl in cols:
            if name not in have:
                try:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                except Exception:
                    pass
    # 缺省参数种进 meta（INSERT OR IGNORE：只补没有的，不覆盖手调过的）
    try:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, val TEXT)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO meta(key, val) VALUES(?, ?)", (k, str(v)))
    except Exception:
        pass
    conn.commit()
    return True


def _clip(x, lo, hi):
    return max(lo, min(hi, float(x)))


class FeedbackLoop(object):
    """动作—结果对账 + 学习评价 + 意向池 + 夜间反思 + 评价印象。"""

    def __init__(self, card, severity_model=None, polisher=None):
        self.card = card
        self.severity_model = severity_model
        self.polisher = polisher
        self.db = card.db
        try:
            ensure(self.db)
        except Exception:
            pass

    # ---------------- 基础设施 ----------------
    def _now(self, now=None):
        if now is not None:
            return float(now)
        try:
            return float(self.card.clock.now())
        except Exception:
            return time.time()

    def _p(self, key):
        dflt = DEFAULTS.get(key, 0.0)
        try:
            row = self.db.execute(
                "SELECT val FROM meta WHERE key=?", (key,)).fetchone()
            if row is not None and row["val"] not in (None, ""):
                return float(row["val"])
        except Exception:
            pass
        return float(dflt)

    def _row(self, sql, args=()):
        try:
            return self.db.execute(sql, args).fetchone()
        except Exception:
            return None

    def _rows(self, sql, args=()):
        try:
            return self.db.execute(sql, args).fetchall()
        except Exception:
            return []

    # ---------------- ① 登记动作 ----------------
    def record(self, kind="self_utterance", mem_id=None, dialogue_id=None,
               actor="self", ts=None, note="", source="explicit", trust_ok=True):
        now = self._now(ts)
        c = self.db.cursor()
        c.execute(
            "INSERT INTO feedback(kind, mem_id, dialogue_id, actor, ts_action,"
            " status, note, source, trust_ok, ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(kind or "self_utterance"), mem_id, dialogue_id, str(actor or "self"),
             now, "open", str(note or ""), str(source or "explicit"),
             1 if trust_ok else 0, now))
        self.db.commit()
        return int(c.lastrowid or 0)

    # ---------------- ② 合成奖励标量 ----------------
    def _sev_alg(self, correction, reject, disconfirm, promise):
        alg = (0.35 * float(correction) + 0.30 * float(reject)
               + 0.20 * float(disconfirm) + 0.15 * max(0.0, -float(promise)))
        alg = _clip(alg, 0.0, 1.0)
        return SEV_MILD + (SEV_STRONG - SEV_MILD) * alg

    def _compose(self, approval=0.0, correction=0.0, reject=0.0, confirm=0.0,
                 confirm_weak=0.0, disconfirm=0.0, reuse=0.0, promise=0.0,
                 severity=None, row=None, signals=None):
        w = {k: self._p("fb_w_" + k) for k in
             ("approval", "correction", "reject", "confirm_strong",
              "confirm_weak", "disconfirm", "reuse", "promise")}
        r = (w["approval"] * float(approval)
             - w["correction"] * float(correction)
             - w["reject"] * float(reject)
             + w["confirm_strong"] * float(confirm)
             + w["confirm_weak"] * float(confirm_weak)
             - w["disconfirm"] * float(disconfirm)
             + w["reuse"] * float(reuse)
             + w["promise"] * float(promise))
        r = _clip(r, -R_CAP, R_CAP)
        bucket = "positive" if r > POS_TH else ("negative" if r < NEG_TH else "neutral")
        if float(confirm) > 0:
            level = "strong"
        elif float(confirm_weak) > 0:
            level = "weak"
        else:
            level = "none"
        sev = None
        if severity is not None:
            sev = _clip(float(severity), 0.2, 0.9)
        elif self.severity_model is not None:
            try:
                got = self.severity_model(row or {}, signals or {})
                if got is not None:
                    sev = _clip(float(got), 0.2, 0.9)
            except Exception:
                sev = None
        if sev is None:
            sev = self._sev_alg(correction, reject, disconfirm, promise)
        return r, bucket, sev, level

    # ---------------- ③ 结算 ----------------
    def settle(self, fb_id, approval=0.0, correction=0.0, reject=0.0,
               confirm=0.0, confirm_weak=0.0, disconfirm=0.0, reuse=0.0,
               promise=0.0, note=None, trust_ok=None, severity=None, now=None):
        row = self._row("SELECT * FROM feedback WHERE id=?", (int(fb_id),))
        if row is None:
            return {"ok": False, "err": "no_such_feedback", "fb_id": fb_id}
        if trust_ok is None:
            trust_ok = bool(row["trust_ok"])
        if not trust_ok:
            try:
                self.db.execute(
                    "UPDATE feedback SET status='disputed', note=? WHERE id=?",
                    ((row["note"] if note is None else str(note)), int(fb_id)))
                self.db.commit()
            except Exception:
                pass
            return {"ok": True, "fb_id": int(fb_id), "r": 0.0, "bucket": "neutral",
                    "severity": 0.0, "confirm_level": "none", "kind": row["kind"],
                    "mem_id": row["mem_id"], "dialogue_id": row["dialogue_id"],
                    "skipped": "trust_ok=0"}
        signals = {"approval": approval, "correction": correction,
                   "reject": reject, "confirm": confirm,
                   "confirm_weak": confirm_weak, "disconfirm": disconfirm,
                   "reuse": reuse, "promise": promise}
        r, bucket, sev, level = self._compose(
            approval, correction, reject, confirm, confirm_weak, disconfirm,
            reuse, promise, severity=severity, row=dict(row), signals=signals)
        nowv = self._now(now)
        try:
            self.db.execute(
                "UPDATE feedback SET approval=?, correction=?, reject=?, confirm=?,"
                " confirm_weak=?, confirm_level=?, disconfirm=?, reuse=?, promise=?,"
                " severity=?, r=?, r_bucket=?, status='settled', ts_settle=?, note=?"
                " WHERE id=?",
                (float(approval), float(correction), float(reject), float(confirm),
                 float(confirm_weak), level, float(disconfirm), float(reuse),
                 float(promise), float(sev), float(r), bucket, nowv,
                 (row["note"] if note is None else str(note)), int(fb_id)))
            self.db.commit()
        except Exception as e:
            return {"ok": False, "err": str(e), "fb_id": int(fb_id)}
        steps = self._apply(dict(row), r, sev, level, now=nowv)
        return {"ok": True, "fb_id": int(fb_id), "r": round(r, 4),
                "bucket": bucket, "severity": round(sev, 4),
                "confirm_level": level, "kind": row["kind"],
                "mem_id": row["mem_id"], "dialogue_id": row["dialogue_id"],
                "steps": steps}

    def implicit(self, kind="decision", mem_id=None, level="confirm", note="",
                 source="implicit", reuse=0.0, trust_ok=True, severity=None,
                 now=None):
        fid = self.record(kind=kind, mem_id=mem_id, note=note, source=source,
                          trust_ok=trust_ok, ts=now)
        kw = {}
        if level == "confirm":
            kw["confirm"] = 1.0
        elif level == "confirm_weak":
            kw["confirm_weak"] = 1.0
        elif level == "disconfirm":
            kw["disconfirm"] = 1.0
        elif level == "reuse":
            kw["reuse"] = float(reuse) if reuse else 1.0
        return self.settle(fid, severity=severity, trust_ok=trust_ok, now=now, **kw)

    # ---------------- ④ 落地链路 ----------------
    def _apply_inline(self, row, r, sev, level, now=None):
        """顺序执行七步（总线不可用时的兜底；原来就是这份）。"""
        row = dict(row) if row is not None and not isinstance(row, dict) else (row or {})
        steps = {}
        mid = row.get("mem_id")
        for name, fn in (
                ("hormone", lambda: self._apply_hormone(r, sev)),
                ("mem", lambda: self._apply_mem(mid, r, sev)),
                ("synapse", lambda: self._apply_synapse(mid, r)),
                ("learn", lambda: self._apply_learn(mid, r, level, now)),
                ("impression", lambda: self._apply_impression(row, r, now)),
                ("motiv", lambda: self._apply_motiv(r, sev)),
                ("intention", lambda: self._make_intentions(row, r, sev, now)),
                ("affect", lambda: self._apply_affect(row, r, sev, now))):
            try:
                steps[name] = fn()
            except Exception as e:
                steps[name] = "err:%s" % e
        return steps

    # ---- V9.7 信号总线：七步结算改由事件派发（他的形态 + 我们的数学）---------
    def _bus(self):
        """惰性建总线并注册适配器；拿不到 signalbus 就返回 None（自动退回顺序执行）。"""
        if getattr(self, "_bus_obj", None) is not None:
            return self._bus_obj
        if getattr(self, "_bus_disabled", False):
            return None
        try:
            import signalbus as sb
        except Exception as e:
            self._bus_disabled = True
            print("[feedback] 总线不可用，退回顺序结算：%s" % e)
            return None
        bus = sb.Bus(card=self.card)
        bus.add("hormone_tap", (sb.EV_HORMONE_TAP,), self._h_hormone, order=10)
        bus.add("memory_struct", (sb.EV_MEM_REINFORCE, sb.EV_MEM_WEAKEN), self._h_memory,
                order=20)
        bus.add("rpe_pulse", (sb.EV_RPE_PULSE,), self._h_rpe, order=15)
        bus.add("impression_observe", (sb.EV_IMPRESSION_OBSERVE,), self._h_impression,
                order=30)
        bus.add("affect_update", (sb.EV_AFFECT_UPDATE,), self._h_affect, order=40)
        bus.add("drive_adjust", (sb.EV_DRIVE_BUMP, sb.EV_DRIVE_ITCH), self._h_drive,
                order=50)
        bus.add("intention_propose", (sb.EV_INTENTION_PROPOSE,), self._h_intention,
                order=60)
        self._bus_obj = bus
        return bus

    def bus_snapshot(self):
        bus = getattr(self, "_bus_obj", None)
        return bus.snapshot() if bus is not None else {"enabled": False}

    def _apply(self, row, r, sev, level, now=None):
        """总线版：先派发事件；任何一环炸掉就退回顺序执行。"""
        row = dict(row) if row is not None and not isinstance(row, dict) else (row or {})
        bus = self._bus()
        if bus is None:
            return self._apply_inline(row, r, sev, level, now)
        try:
            import signalbus as sb
            mid = row.get("mem_id")
            payload = {"row": row, "mid": mid, "r": r, "sev": sev, "level": level,
                       "now": now, "card": self.card}
            steps = {}
            bus.dispatch(sb.EV_HORMONE_TAP, payload)
            steps["hormone"] = payload.get("out_hormone")
            bus.dispatch(sb.EV_MEM_REINFORCE if float(r) >= 0 else sb.EV_MEM_WEAKEN, payload)
            steps["mem"] = payload.get("out_mem")
            bus.dispatch(sb.EV_RPE_PULSE, payload)
            steps["rpe"] = payload.get("out_rpe")
            bus.dispatch(sb.EV_IMPRESSION_OBSERVE, payload)
            steps["impression"] = payload.get("out_impression")
            bus.dispatch(sb.EV_AFFECT_UPDATE, payload)
            steps["affect"] = payload.get("out_affect")
            bus.dispatch(sb.EV_DRIVE_BUMP if float(r) >= 0 else sb.EV_DRIVE_ITCH, payload)
            steps["motiv"] = payload.get("out_motiv")
            bus.dispatch(sb.EV_INTENTION_PROPOSE, payload)
            steps["intention"] = payload.get("out_intention")
            # 学习评价/突触这一步没有独立事件，跟着记忆走（同一个语义）
            steps["synapse"] = self._apply_synapse(mid, r)
            steps["learn"] = self._apply_learn(mid, r, level, now)
            if not steps.get("mem"):
                steps["mem"] = self._apply_mem(mid, r, sev)
            if not steps.get("intention"):
                steps["intention"] = self._make_intentions(row, r, sev, now)
            return steps
        except Exception as e:
            print("[feedback] 总线派发失败，退回顺序结算：%s" % e)
            self._bus_disabled = True
            return self._apply_inline(row, r, sev, level, now)

    # ---- 适配器：签名统一 (sig) -> 返回值写进 payload["out_*"] --------------
    def _h_hormone(self, sig):
        sig.payload["out_hormone"] = self._apply_hormone(sig.get("r"), sig.get("sev"))

    def _h_memory(self, sig):
        r = float(sig.get("r") or 0.0)
        sig.payload["out_mem"] = self._apply_mem(sig.get("mid"), r, sig.get("sev"))

    def _h_rpe(self, sig):
        """RPE 直注：只改情绪引擎的预期与脉冲；内脏激素上面那一步已经改过了。"""
        try:
            link = self.card._emotion()
        except Exception as e:
            sig.payload["out_rpe"] = "no_link:%s" % e
            return
        r = float(sig.get("r") or 0.0)
        rpe = max(-1.0, min(1.0, r / float(R_CAP or 1.0)))
        res = link.inject_rpe(rpe, scenario=str(sig.get("row", {}).get("kind") or "generic"),
                              intensity=1.0)
        if res is None:
            sig.payload["out_rpe"] = "skip"
            return
        sig.payload["out_rpe"] = {"rpe": round(float(getattr(res, "rpe", 0.0)), 4),
                                  "scenario": getattr(res, "scenario", ""),
                                  "turn": getattr(res, "turn", None)}

    def _h_impression(self, sig):
        sig.payload["out_impression"] = self._apply_impression(
            sig.get("row"), float(sig.get("r") or 0.0), sig.get("now"))

    def _h_affect(self, sig):
        sig.payload["out_affect"] = self._apply_affect(
            sig.get("row"), float(sig.get("r") or 0.0), sig.get("sev"), sig.get("now"))

    def _h_drive(self, sig):
        sig.payload["out_motiv"] = self._apply_motiv(
            float(sig.get("r") or 0.0), sig.get("sev"))

    def _h_intention(self, sig):
        sig.payload["out_intention"] = self._make_intentions(
            sig.get("row"), float(sig.get("r") or 0.0), sig.get("sev"), sig.get("now"))

    def _apply_hormone(self, r, sev):
        import hormone
        hs = getattr(self.card, "h", None)
        if not isinstance(hs, dict):
            return "no_card_hormone"
        self.card.h = hormone.apply_r(hs, r, scale=0.22, severity=sev)
        try:
            self.card._save_hormones()
        except Exception:
            pass
        return {k: round(float(v), 4) for k, v in self.card.h.items()}

    def _apply_mem(self, mid, r, sev):
        if not mid:
            return "no_mem"
        amp = min(1.0, abs(r) / R_CAP) * (1.0 if r > 0 else (0.6 + 0.4 * sev))
        sign = 1 if r > 0 else -1
        dw = MEM_W_STEP * amp * sign
        ds = MEM_SAL_STEP * amp * sign
        self.db.execute(
            "UPDATE mem SET w=MAX(0.02, MIN(1.0, COALESCE(w,0.5)+?)),"
            " salience=MAX(0.0, MIN(1.0, COALESCE(salience,0.5)+?)) WHERE id=?",
            (dw, ds, int(mid)))
        self.db.commit()
        return {"dw": round(dw, 4), "ds": round(ds, 4)}

    def _apply_synapse(self, mid, r):
        if not mid:
            return "no_mem"
        net = getattr(self.card, "synapse", None)
        if net is None:
            return "no_synapse_net"
        if r > 0:
            net.reinforce([int(mid)])
            return "reinforce"
        if hasattr(net, "weaken"):
            net.weaken([int(mid)], factor=0.5)
            return "weaken"
        return "weaken_missing"

    def _apply_learn(self, mid, r, level, now):
        if not mid:
            return "no_mem"
        row = self._row("SELECT region FROM mem WHERE id=?", (int(mid),))
        if row is None or str(row["region"]) != "study":
            return "not_study"
        nowv = self._now(now)
        step = LEARN_VALUE_STEP * (1 if r > 0 else -1) * min(1.0, abs(r) / R_CAP)
        self._learn_eval_row(int(mid))
        conf_step = {"strong": 1.0, "weak": 0.4}.get(level, 0.0)
        if r < 0 and level == "none":
            conf_step = -1.0
        self.db.execute(
            "UPDATE learn_eval SET value=MAX(0.0,MIN(1.0,value+?)),"
            " confirm_strength=confirm_strength+?,"
            " times_confirmed=times_confirmed+?,"
            " times_disconfirmed=times_disconfirmed+?, last_eval_ts=? WHERE mem_id=?",
            (step, conf_step, 1 if conf_step > 0 else 0,
             1 if conf_step < 0 else 0, nowv, int(mid)))
        self.db.commit()
        self._learn_verdict(int(mid))
        return {"dvalue": round(step, 4), "conf_step": conf_step}

    def _learn_eval_row(self, mid):
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO learn_eval(mem_id) VALUES(?)", (int(mid),))
            self.db.commit()
        except Exception:
            pass

    def _learn_verdict(self, mid):
        row = self._row("SELECT * FROM learn_eval WHERE mem_id=?", (int(mid),))
        if row is None:
            return "open"
        val = float(row["value"] or 0.0)
        cs = float(row["confirm_strength"] or 0.0)
        if int(row["times_disconfirmed"] or 0) >= 2 or cs < 0:
            verdict = "outdated"
        elif val > 0.66:
            verdict = "kept"
        else:
            verdict = "open"
        try:
            self.db.execute("UPDATE learn_eval SET verdict=? WHERE mem_id=?",
                            (verdict, int(mid)))
            self.db.commit()
        except Exception:
            pass
        return verdict

    def _apply_impression(self, row, r, now):
        import impression as _imp
        note = str(row.get("note") or "") or ("反馈 %.2f" % r)
        nowv = self._now(now)
        try:
            _imp.observe(self.db, subject="self", text=note, ts=nowv,
                         subject_kind="person", now=nowv)
        except TypeError:
            _imp.observe(self.db, "self", note, ts=nowv)
        return "self_observed"

    def _apply_motiv(self, r, sev):
        import motiv
        mo = getattr(self.card, "mo", None)
        if not isinstance(mo, dict) or "d" not in mo:
            return "no_card_motiv"
        amp = MOTIV_STEP * min(1.0, abs(r) / R_CAP) * (0.6 + 0.4 * sev)
        drive = "competence" if r > 0 else "order"
        mo["d"] = (motiv.bump(mo["d"], drive, amp) if r > 0
                   else motiv.itch(mo["d"], drive, amp))
        try:
            self.card._save_motiv()
        except Exception:
            pass
        return {"drive": drive, "amp": round(amp, 4)}

    def _make_intentions(self, row, r, sev, now=None):
        # 中性（|r| 没到阈值）不生意向：一件说不上好坏的事，不该让我「想躲」。
        if abs(float(r or 0.0)) <= float(POS_TH) + 1e-9:
            return []
        if abs(r) < 0.02:
            return []
        mid = row.get("mem_id")
        nowv = self._now(now)
        urge0 = min(1.0, abs(r) * (0.6 + 0.4 * sev))
        is_study = False
        if mid:
            row2 = self._row("SELECT region FROM mem WHERE id=?", (int(mid),))
            is_study = bool(row2 is not None and str(row2["region"]) == "study")
        is_promise = str(row.get("kind") or "") == "promise"
        plan = []
        if r > 0:
            if is_study:
                plan = [("approach", "explore", "curiosity"),
                        ("approach", "learn_more", "curiosity")]
            elif is_promise:
                plan = [("approach", "stay", "attachment")]
            else:
                plan = [("approach", "replay", "competence")]
        else:
            if sev >= SEV_AVOID_LINE:
                plan = [("avoid", "avoid_scene", "competence")]
            else:
                plan = [("avoid", "self_check", "order")]
        out = []
        for direction, kind, drive in plan:
            out.append(self._upsert_intention(
                fb_id=row.get("id"), mem_id=mid, direction=direction, kind=kind,
                drive=drive, urge=urge0 if kind != "learn_more" else urge0 * 0.8,
                now=nowv, note=str(row.get("note") or "")[:80]))
        return out

    def _upsert_intention(self, fb_id=None, mem_id=None, direction="approach",
                          kind="replay", drive="competence", urge=0.0,
                          now=None, note="", scene_tag="", status="active"):
        nowv = self._now(now)
        urge = min(1.0, float(urge))
        hl = float(DECAY_HL.get(kind, 24.0))
        ex = self._row(
            "SELECT * FROM intention WHERE direction=? AND kind=?"
            " AND COALESCE(mem_id,-1)=COALESCE(?,-1) AND status='active'"
            " ORDER BY id DESC LIMIT 1", (direction, kind, mem_id))
        if ex is not None:
            new = min(1.0, float(ex["urge"] or 0.0) + urge * 0.5)
            try:
                self.db.execute(
                    "UPDATE intention SET urge=?, last_touch_ts=?, note=? WHERE id=?",
                    (new, nowv, note or ex["note"], int(ex["id"])))
                self.db.commit()
            except Exception:
                pass
            return {"id": int(ex["id"]), "urge": round(new, 4), "merged": True}
        c = self.db.cursor()
        c.execute(
            "INSERT INTO intention(fb_id, mem_id, direction, kind, drive, scene_tag,"
            " urge, decay_hl, status, note, created_ts, last_touch_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fb_id, mem_id, direction, kind, drive, scene_tag, urge, hl, status,
             note, nowv, nowv))
        self.db.commit()
        return {"id": int(c.lastrowid or 0), "urge": round(urge, 4), "merged": False}

    def _apply_affect(self, row, r, sev, now):
        import impression_eval as _ie
        lr = self._p("fb_affect_lr")
        nowv = self._now(now)
        tag = str(row.get("kind") or "") or "feedback"
        note = str(row.get("note") or "")
        done = []
        try:
            _ie.update(self.db, "scene", tag, r, sev, mem_id=row.get("mem_id"),
                       note=note, lr=lr, now=nowv)
            done.append("scene:" + tag)
        except Exception:
            pass
        uid = None
        try:
            uid = self.card._affect_uid()
        except Exception:
            uid = None
        if uid:
            try:
                _ie.update(self.db, "person", str(uid), r, sev,
                           mem_id=row.get("mem_id"), note=note, lr=lr, now=nowv)
                done.append("person:" + str(uid))
            except Exception:
                pass
        return done

    # ---------------- ⑤ 意向池 ----------------
    def consume_intention(self, iid, note="", now=None):
        """意向做完了：标 consumed，别让它永远挂着重复触发。"""
        now = float(now or time.time())
        try:
            self.db.execute(
                "UPDATE intention SET status='consumed', last_touch_ts=?, note=?"
                " WHERE id=? AND status='active'",
                (now, str(note or "")[:120], int(iid)))
            self.db.commit()
            return True
        except Exception:
            return False

    def decay_intentions(self, now=None):
        nowv = self._now(now)
        rows = self._rows("SELECT * FROM intention WHERE status='active'")
        n_faded = 0
        for row in rows:
            hl = float(row["decay_hl"] or 24.0)
            dt_h = max(0.0, (nowv - float(row["last_touch_ts"] or nowv)) / 3600.0)
            urge = float(row["urge"] or 0.0) * (0.5 ** (dt_h / max(0.1, hl)))
            try:
                if urge < INTENTION_FADE:
                    self.db.execute(
                        "UPDATE intention SET urge=?, status='faded',"
                        " last_touch_ts=? WHERE id=?", (urge, nowv, int(row["id"])))
                    n_faded += 1
                elif dt_h > 0:
                    self.db.execute(
                        "UPDATE intention SET urge=?, last_touch_ts=? WHERE id=?",
                        (urge, nowv, int(row["id"])))
            except Exception:
                pass
        self.db.commit()
        return {"checked": len(rows), "faded": n_faded}

    def active_intentions(self, limit=30, now=None):
        rows = self._rows(
            "SELECT * FROM intention WHERE status='active' ORDER BY urge DESC LIMIT ?",
            (int(limit),))
        return [{"id": int(r["id"]), "mem_id": r["mem_id"],
                 "direction": r["direction"], "kind": r["kind"],
                 "drive": r["drive"], "urge": round(float(r["urge"] or 0.0), 4),
                 "note": r["note"]} for r in rows]

    # ---------------- ⑥ 隐式对账 ----------------
    def reconcile_facts(self, now=None):
        rows = self._rows(
            "SELECT mem_id, COUNT(*) AS n FROM fact WHERE status='superseded'"
            " AND mem_id IS NOT NULL GROUP BY mem_id LIMIT 200")
        n_new = 0
        for row in rows:
            mid = int(row["mem_id"])
            hit = self._row(
                "SELECT id FROM feedback WHERE mem_id=? AND source='fact_superseded'"
                " LIMIT 1", (mid,))
            if hit is not None:
                continue
            try:
                self.implicit(kind="decision", mem_id=mid, level="disconfirm",
                              note="旧说法被新事实推翻", source="fact_superseded",
                              now=now)
                n_new += 1
            except Exception:
                pass
        return {"scanned": len(rows), "new": n_new}

    def learn_eval_use(self, mem_id, now=None):
        row = self._row("SELECT region FROM mem WHERE id=?", (int(mem_id),))
        if row is None or str(row["region"]) != "study":
            return False
        self._learn_eval_row(int(mem_id))
        nowv = self._now(now)
        dk = time.strftime("%Y-%m-%d", time.localtime(nowv))
        key = "fb_used_%d" % int(mem_id)
        r2 = self._row("SELECT val FROM meta WHERE key=?", (key,))
        if r2 is not None and r2["val"] == dk:
            return False
        try:
            self.db.execute(
                "UPDATE learn_eval SET times_used=times_used+1, last_eval_ts=?"
                " WHERE mem_id=?", (nowv, int(mem_id)))
            self.db.execute(
                "INSERT INTO meta(key,val) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, dk))
            self.db.commit()
        except Exception:
            return False
        return True

    # ---------------- ⑦ 夜间反思 ----------------
    def drop_stale(self, days=3.0, now=None):
        """过期未结算的 open 直接标 dropped：没有证据的评价不该一直挂着。"""
        now = float(now or self._now())
        cutoff = now - float(days) * 86400.0
        c = self.card.db.cursor()
        c.execute("UPDATE feedback SET status='dropped'"
                  " WHERE status='open' AND ts_action < ?", (cutoff,))
        n = c.rowcount or 0
        self.card.db.commit()
        return n

    def nightly(self, now=None, force=False):
        nowv = self._now(now)
        gap_h = self._p("fb_nightly_hours")
        last = 0.0
        row = self._row("SELECT val FROM meta WHERE key='fb_nightly_ts'")
        if row is not None:
            try:
                last = float(row["val"])
            except Exception:
                last = 0.0
        if not force and last and (nowv - last) < gap_h * 3600.0:
            return {"ran": False, "reason": "throttled",
                    "next_in_h": round(gap_h - (nowv - last) / 3600.0, 2)}
        out = {"ran": True, "now": round(nowv, 3)}
        try:
            out["dropped"] = self.drop_stale(now=nowv)
        except Exception as e:
            out["dropped"] = "err:%s" % e
        try:
            out["facts"] = self.reconcile_facts(now=nowv)
        except Exception as e:
            out["facts"] = "err:%s" % e
        try:
            out["intentions"] = self.decay_intentions(now=nowv)
        except Exception as e:
            out["intentions"] = "err:%s" % e
        try:
            import impression_eval as _ie
            out["affect"] = _ie.fade(self.db, now=nowv)
        except Exception as e:
            out["affect"] = "err:%s" % e
        try:
            out["reviews"] = self.run_reviews(now=nowv)
        except Exception as e:
            out["reviews"] = "err:%s" % e
        try:
            self.db.execute(
                "INSERT INTO meta(key,val) VALUES('fb_nightly_ts',?)"
                " ON CONFLICT(key) DO UPDATE SET val=excluded.val", (repr(nowv),))
            self.db.commit()
        except Exception:
            pass
        return out

    def run_reviews(self, now=None):
        nowv = self._now(now)
        min_n = int(self._p("fb_min_n"))
        max_n = int(self._p("fb_max_n"))
        half = self._p("fb_half_life_days")
        promo = self._p("fb_promo_strength")
        rows = self._rows(
            "SELECT * FROM feedback WHERE status='settled' AND r_bucket!='neutral'"
            " ORDER BY ts_settle DESC LIMIT 400")
        groups = {}
        for row in rows:
            side = "positive" if float(row["r"] or 0) > 0 else "negative"
            key = "%s:%s" % (side, row["mem_id"] if row["mem_id"] else
                             ("kind:" + str(row["kind"])))
            g = groups.setdefault(key, {"side": side, "rows": []})
            if len(g["rows"]) < max_n:
                g["rows"].append(row)
        made = []
        for key, g in groups.items():
            items = g["rows"]
            if not items:
                continue
            strength = 0.0
            for row in items:
                age_d = max(0.0, (nowv - float(row["ts_settle"] or nowv)) / 86400.0)
                sev = float(row["severity"] or 0.0)
                strength += (abs(float(row["r"] or 0.0))
                             * (0.5 ** (age_d / max(0.1, half)))
                             * (0.6 + 0.4 * sev))
            n = len(items)
            promoted = 1 if (n >= min_n and strength >= promo) else 0
            angle = g["side"] == "positive" and "good" or "lesson"
            if not promoted:
                angle = "watch"
            fb_ids = [int(r["id"]) for r in items]
            review = self._build_review(g["side"], angle, items, fb_ids,
                                        items[0]["mem_id"], n, strength,
                                        promoted, nowv)
            made.append(review)
            try:
                self._seed_review_intentions(review)
            except Exception:
                pass
        return {"groups": len(groups), "made": len(made),
                "promoted": sum(1 for r in made if r["promoted"]),
                "reviews": made}

    def _build_review(self, side, angle, items, fb_ids, mid, n, strength,
                      promoted, nowv):
        notes = [str(r["note"] or "") for r in items if str(r["note"] or "").strip()]
        head = notes[0][:60] if notes else (
            "被认可的那类事" if side == "positive" else "出了岔子的那类事")
        if angle == "good":
            base = "这类事做对了：%s。（%d 条同向证据，强度 %.2f）" % (head, n, strength)
        elif angle == "lesson":
            base = "上次这类事没做好：%s。下次先查再说，别凭印象答。（%d 条同向证据）" % (head, n)
        else:
            base = "这类事还看不准：%s。先记下，继续观察。（%d 条证据，强度 %.2f）" % (
                head, n, strength)
        summary = base
        polished = 0
        polished_summary = ""
        if self.polisher is not None:
            try:
                got = self.polisher(side, angle, base, fb_ids)
                if got:
                    polished_summary = str(got)
                    summary = polished_summary
                    polished = 1
            except Exception:
                pass
        mem_id = mid
        try:
            mem_id = self.card.remember(summary, salience=0.55, region="episode",
                                        speaker="self", mtype="episode",
                                        pool="long", source="review")
        except Exception:
            mem_id = mid
        c = self.db.cursor()
        c.execute(
            "INSERT INTO review(mem_id, side, angle, summary, base_summary,"
            " polished_summary, polished, fb_ids, related_mems, evidence_n,"
            " evidence_strength, strength, promoted, confirmed, ts, ts_reviewed)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, side, angle, summary, base, polished_summary, polished,
             json.dumps(fb_ids, ensure_ascii=False),
             json.dumps([mid] if mid else [], ensure_ascii=False),
             n, strength, strength, promoted, 0, nowv, nowv))
        self.db.commit()
        return {"id": int(c.lastrowid or 0), "side": side, "angle": angle,
                "strength": round(strength, 4), "promoted": promoted,
                "mem_id": mem_id, "n": n}

    def _seed_review_intentions(self, review):
        strength = float(review.get("strength") or 0.0)
        urge = min(1.0, 0.35 + strength * 0.5)
        mid = review.get("mem_id")
        if review["side"] == "positive":
            if review["promoted"]:
                self._upsert_intention(mem_id=mid, direction="approach",
                                       kind="replay", drive="competence",
                                       urge=urge, note="复盘：做对了，可以延续")
                self._upsert_intention(mem_id=mid, direction="approach",
                                       kind="explore", drive="curiosity",
                                       urge=urge * 0.8, note="复盘：往这个方向再深挖")
            else:
                self._upsert_intention(mem_id=mid, direction="approach",
                                       kind="explore", drive="curiosity",
                                       urge=urge * 0.4, note="复盘：看着不错，再观察")
        else:
            if review["promoted"]:
                self._upsert_intention(mem_id=mid, direction="avoid",
                                       kind="self_check", drive="order",
                                       urge=urge, note="复盘：先自查再动手")
                self._upsert_intention(mem_id=mid, direction="approach",
                                       kind="learn_more", drive="curiosity",
                                       urge=urge * 0.7, note="复盘：短板补一下")
            else:
                self._upsert_intention(mem_id=mid, direction="avoid",
                                       kind="self_check", drive="order",
                                       urge=urge * 0.4, note="复盘：有点不稳，留意")

    def confirm_review(self, review_id, confirmed=1):
        try:
            self.db.execute("UPDATE review SET confirmed=? WHERE id=?",
                            (1 if confirmed else 0, int(review_id)))
            self.db.commit()
            return {"ok": True, "review_id": int(review_id),
                    "confirmed": 1 if confirmed else 0}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    # ---------------- ⑧ 只读视图 ----------------
    def list(self, limit=20, status=None):
        if status:
            rows = self._rows(
                "SELECT * FROM feedback WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, int(limit)))
        else:
            rows = self._rows("SELECT * FROM feedback ORDER BY id DESC LIMIT ?",
                              (int(limit),))
        return [dict(r) for r in rows]

    def list_reviews(self, limit=20, promoted=None):
        if promoted:
            rows = self._rows(
                "SELECT * FROM review WHERE promoted=1 ORDER BY id DESC LIMIT ?",
                (int(limit),))
        else:
            rows = self._rows("SELECT * FROM review ORDER BY id DESC LIMIT ?",
                              (int(limit),))
        return [dict(r) for r in rows]

    def list_intentions(self, limit=50, direction=None):
        if direction:
            rows = self._rows(
                "SELECT * FROM intention WHERE direction=? ORDER BY urge DESC LIMIT ?",
                (direction, int(limit)))
        else:
            rows = self._rows("SELECT * FROM intention ORDER BY urge DESC LIMIT ?",
                              (int(limit),))
        return [dict(r) for r in rows]

    def learn_eval(self, mem_id=None):
        if mem_id:
            row = self._row("SELECT * FROM learn_eval WHERE mem_id=?",
                            (int(mem_id),))
            return dict(row) if row is not None else None
        return [dict(r) for r in self._rows(
            "SELECT * FROM learn_eval ORDER BY last_eval_ts DESC LIMIT 50")]

    def stats(self, now=None):
        def n(sql, args=()):
            r = self._row(sql, args)
            try:
                return int(r[0] or 0)
            except Exception:
                return 0
        out = {
            "open": n("SELECT COUNT(*) FROM feedback WHERE status='open'"),
            "settled": n("SELECT COUNT(*) FROM feedback WHERE status='settled'"),
            "positive": n("SELECT COUNT(*) FROM feedback WHERE r_bucket='positive'"),
            "negative": n("SELECT COUNT(*) FROM feedback WHERE r_bucket='negative'"),
            "neutral": n("SELECT COUNT(*) FROM feedback WHERE r_bucket='neutral'"),
            "reviews": n("SELECT COUNT(*) FROM review"),
            "reviews_promoted": n("SELECT COUNT(*) FROM review WHERE promoted=1"),
            "reviews_confirmed": n("SELECT COUNT(*) FROM review WHERE confirmed=1"),
            "intentions_approach": n("SELECT COUNT(*) FROM intention"
                                     " WHERE status='active' AND direction='approach'"),
            "intentions_avoid": n("SELECT COUNT(*) FROM intention"
                                 " WHERE status='active' AND direction='avoid'"),
        }
        try:
            import impression_eval as _ie
            out.update(_ie.stats(self.db))
        except Exception:
            out.update({"affect": 0, "affect_positive": 0, "affect_negative": 0})
        return out
