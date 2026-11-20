# -*- coding: utf-8 -*-
"""习惯与印象：`MemoryCard` 的一块。

拆门面第 6 批的第 1 块。这里只放方法，状态仍然在 MemoryCard 上
（`self.db` 之类的属性由 MemoryCard.__init__ 提供），所以这个类
不能单独实例化。"""

import habits as habits_mod
import impression as impression_mod
import os
from cardcfg import (CLARITY_GC, DEDUPE_JACCARD, EMB_ADAPTER_VERSION, GAIN_REPEAT,
                     GROUP_SAL_MAX, GROUP_SOURCE_PREFIX, KEEP_DETAIL, RE_NUM, RE_PLACE,
                     SHORT_MAX_TTL, THETA_LONG_STRICT, THETA_MEDIUM, TIER_MID,
                     TRUST_BLOCK_PROMOTE, TRUST_WARN, W_CEIL_RECALL, W_FLOOR_LONG,
                     W_FLOOR_MEDIUM)
from poolmath import (POOL_LONG, POOL_MEDIUM, POOL_SHORT, R_CHATTER, R_FLASH,
                      TIER_CORE_MUTABLE, _REGION_LAMBDA_ON, wn_row)
from cardtext import (classify_region, embed_text_for, guess_mtype, guess_speaker, jaccard,
                      region_by_source, self_speech_kind, split_memory, split_predicate,
                      strip_sysmark)
from cardtime import normalize_time, parse_event_time
from body import EMO_WORDS, assess_trust, assess_value, oxy_signal, update_oxy
from vmath import to_vec
from diag import soft_fail
import semantic as semantic_mod
import impression as impression_mod
import facts as facts_mod
import math
import os
import re


class MixHabit(object):
    """习惯 / 印象的读取与维护。方法原样搬来，一行没改。"""

    def impression_top(self, subject=None, limit=8, min_w=0.0):
        return impression_mod.top(self.db, subject=subject, limit=limit, min_w=min_w)

    def impression_hints(self, subject=None, limit=3, min_w=None):
        return impression_mod.hint_lines(self.db, subject=subject, limit=limit, min_w=min_w)

    def impression_hints_for(self, query="", subject=None, limit=3, min_w=None, pool=12):
        """按「这句话」挑印象（2026-11-20）。

        原来的读法是固定取权重最高的三条 —— 于是不管问什么，蹦出来的都是
        「他常说『回来了回来了』」。印象该有自己的选择：先在候选池里，
        按与这句话的字面重合重排，重合都一样才退回原来的权重序。
        """
        try:
            cand = list(impression_mod.hint_lines(self.db, subject=subject,
                                                  limit=pool, min_w=min_w) or [])
        except Exception:
            return []
        if not cand or not query:
            return cand[:limit]

        def _g(s):
            t = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(s or ""))
            return set(t[i:i + 2] for i in range(len(t) - 1))

        qg = _g(query)
        if not qg:
            return cand[:limit]
        scored = [(len(qg & _g(line)), _i, line) for _i, line in enumerate(cand)]
        hit = [x for x in scored if x[0] > 0]
        if not hit:
            return cand[:limit]
        hit.sort(key=lambda x: (-x[0], x[1]))
        # 命中的先上，不够 limit 就用权重序里的其它行补齐（别只剩一行）
        picked = [x[2] for x in hit[:limit]]
        if len(picked) < limit:
            for line in cand:
                if len(picked) >= limit:
                    break
                if line not in picked:
                    picked.append(line)
        return picked

    def impression_snapshot(self):
        return impression_mod.snapshot(self.db)

    def impression_fade(self, now=None):
        return impression_mod.fade(self.db, now=now if now is not None else self.clock.now())

    def _impress_from_assoc(self, ids, now=None):
        """联想带出来的旧记忆 → 间接印象（打折、低频）。2026-11-20。"""
        if os.environ.get("WHALE_IMPRESSION", "1") == "0":
            return 0
        ids = [int(i) for i in (ids or [])][:2]
        if not ids:
            return 0
        now = float(now if now is not None else self.clock.now())
        try:
            _last = self._meta_float("imp_assoc_ts")
        except Exception:
            _last = 0.0
        if now - _last < 1800:      # 半小时只闻一次，别把联想变成记仇
            return 0
        done = 0
        try:
            q = ("SELECT id, raw, speaker FROM mem WHERE id IN (%s)"
                 % ",".join("?" * len(ids)))
            for r in self.db.execute(q, ids).fetchall():
                raw = r[1] if not isinstance(r, dict) else r["raw"]
                spk = r[2] if not isinstance(r, dict) else r["speaker"]
                if str(spk or "") == "self":
                    continue
                if self._impress_write(str(raw or ""), now=now, via="indirect"):
                    done += 1
        except Exception as _ae:
            print("[card2] 联想印象失败: %s" % _ae)
            return 0
        if done:
            try:
                self._set_meta_float("imp_assoc_ts", now)
            except Exception:
                pass
        return done

    def _impress_write(self, text, ts=None, speaker="user", about_uid=None, via="direct"):
        """印象写入的独立入口（2026-11-20）。

        放进 MixHabit 而不是散在 remember 里：印象层有自己的触发点和失败边界，
        写坏了不该把整条记忆写入带崩。语义没变 —— 只记别人的话。
        """
        try:
            impression_mod.observe(
                self.db,
                subject=(about_uid or ("user" if (speaker or "") == "user" else "")),
                text=text, ts=(ts if ts is not None else self.clock.now()),
                subject_kind=("person" if (about_uid or speaker) else "thing"),
                now=self.clock.now(), via=via)
            return True
        except Exception as _ie:
            print("[card2] 印象记账失败: %s" % _ie)
            return False

    # ---- V9.3 评价印象（affect）门面，2026-11-20 ------------------------
    def affect_match(self, target_kind=None, target_tag=None, min_salience=0.0):
        try:
            import impression_eval as aff
            return aff.match(self.db, target_kind, target_tag, min_salience)
        except Exception:
            return None

    def affect_read(self, target_kind, target_tag):
        try:
            import impression_eval as aff
            return aff.read(self.db, target_kind, target_tag)
        except Exception:
            return None

    def affect_attach(self, target_kind, target_tag, mem_id):
        try:
            import impression_eval as aff
            return aff.attach_mem(self.db, target_kind, target_tag, mem_id)
        except Exception:
            return []

    def affect_hints(self, query="", limit=3):
        """【态度】段的取数：他（人物）常驻一条 + 和这句话沾边的场景。"""
        try:
            import impression_eval as aff
        except Exception:
            return []
        lines = []
        try:
            uid = self._affect_uid()
            r = aff.read(self.db, "person", str(uid))
            if r and float(r["salience"]) >= 0.08:
                lines += aff.hint_lines(self.db, "person", str(uid), limit=1)
        except Exception:
            pass
        try:
            for it in aff.snapshot(self.db, limit=20):
                if it["target_kind"] != "scene":
                    continue
                tag = str(it["target_tag"] or "")
                if float(it["salience"]) < 0.08:
                    continue
                if tag and (tag in (query or "") or (query or "") in tag):
                    lines += aff.hint_lines(self.db, "scene", tag, limit=1)
                if len(lines) >= int(limit):
                    break
        except Exception:
            pass
        if not lines:
            try:
                lines = aff.hint_lines(self.db, limit=min(2, int(limit)))
            except Exception:
                lines = []
        out, seen = [], set()
        for x in lines:
            if x not in seen:
                out.append(x)
                seen.add(x)
        return out[:int(limit)]

    def affect_snapshot(self, limit=50):
        try:
            import impression_eval as aff
            return aff.snapshot(self.db, limit=limit)
        except Exception:
            return []

    def affect_nudge(self, hs, target_kind, target_tag, step=None):
        try:
            import impression_eval as aff
            return aff.mood_nudge(self.db, hs, target_kind, target_tag, step=step)
        except Exception:
            return {}

    def affect_linked(self, uid=None, limit=32):
        """他/这件事绑着哪些记忆（impression_eval 的 linked_mems）。"""
        try:
            import impression_eval as aff
            r = aff.read(self.db, "person", str(uid or self._affect_uid()))
        except Exception:
            return []
        if not r:
            return []
        out = []
        for x in (r.get("linked_mems") or []):
            try:
                out.append(int(x))
            except Exception:
                continue
        return out[:int(limit)]

    def _affect_uid(self):
        for attr in ("_affect_uid_cache", "about_uid", "_about_uid"):
            v = getattr(self, attr, None)
            if v:
                return str(v)
        try:
            row = self.db.execute(
                "SELECT about_uid FROM mem WHERE speaker='user' AND about_uid<>''"
                " ORDER BY create_ts DESC LIMIT 1").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return "user"

    def _affect_learn(self, rpe, scenario="generic", uid=None, text=None):
        """一句身体反应磨一次评价印象（人物 + 场景两个维度）。"""
        try:
            import impression_eval as aff
        except Exception:
            return 0
        r = float(rpe or 0.0)
        sev = min(1.0, abs(r))
        n = 0
        try:
            aff.update(self.db, "person", str(uid or self._affect_uid()), r,
                       severity=sev, note=str(scenario or "")[:40])
            n += 1
        except Exception:
            pass
        try:
            aff.update(self.db, "scene", str(scenario or "generic"), r,
                       severity=sev, note=(text or "")[:60])
            n += 1
        except Exception:
            pass
        return n

    def habit_hints(self, limit=5, max_chars=220):
        """从 habits 表捞几条稳定习惯，拼成短句给【我是谁】用。

        为什么要有它：卡里 565 条记忆全是「发生了什么」，没有一条是
        「我这人平时怎么说话」。挖出来的习惯如果只躺在报告里，
        那她还是那个每轮靠提示词临时扮演的自己。

        三条自我约束（都是为了别变成背台词）：
          1. 只取 stable / pinned，候选和衰减中的不进来 —— 凭印象说不如不说；
          2. 最多 5 条、总字数 ≤220（密度刹车）；
          3. 默认开，`WHALE_HABITS=0` 可一键关掉。
        表不存在（还没挖过）时返回空列表，不报错、不建表。
        """
        if os.environ.get("WHALE_HABITS", "1") == "0":
            return []
        try:
            rows = self.db.execute(
                "SELECT key, category, trigger, behavior, weight, confidence,"
                " status, pinned, hits FROM habits"
                " WHERE status IN (?, ?, ?) AND key != ''"
                " ORDER BY pinned DESC, weight DESC, confidence DESC, hits DESC"
                " LIMIT ?",
                (habits_mod.ST_STABLE, habits_mod.ST_PINNED, habits_mod.ST_WEAK,
                 max(1, int(limit) * 3))).fetchall()
        except Exception:
            return []
        out = []
        used = 0
        weak_n = 0
        for r in rows:
            st = str(r["status"] or "")
            if st == habits_mod.ST_WEAK:
                if weak_n >= habits_mod.HAB_WEAK_MAX:
                    continue
            trig = (r["trigger"] or "").strip()
            beh = (r["behavior"] or "").strip()
            key = (r["key"] or "").strip()
            if trig and beh:
                line = "当%s的时候，我一般会%s" % (trig, beh)
            elif key:
                line = "我会用「%s」这种说法" % key
            else:
                continue
            if st == habits_mod.ST_WEAK:
                line += "（这条还弱，别当口头禅硬套）"
                weak_n += 1
            if line in out:
                continue
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
            if len(out) >= int(limit):
                break
        return out
