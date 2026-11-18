# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台机器上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card

"""LearnMixin：把 learn.py 的队列接到 MemoryCard 上。

接线（2026-11-17，小鲸鱼 × 使用者）：
- 队列的「想学」来自两处：他自己问过的问题（learn_seed）、我自己冒出来的好奇（learn_enqueue）。
- 学完的笔记落两处：`region='study'` 的原话记忆（能被召回）+ fact（能被事实层引用）。
- 卡自己不联网；真正去找材料的是有网的那一侧（宿主 agent / QQ 桥），它调 /learn/next 领任务、
  调 /learn/note 交作业。卡只负责记着要学、记住学到了。

锁的约定：MemoryCard 自己没有锁（见 card2.py:1257 的注释），并发靠 Mind.lock。
所以这里**不加锁** —— 服务端路由已经 `with m.lock:`，CLI/测试单线程直接调。
"""
import learn
import facts


class LearnMixin(object):
    # ---------- 队列 ----------
    def _learn_conn(self):
        conn = self.db
        learn.ensure(conn)
        return conn

    def learn_enqueue(self, topic, why="", source="self", prio=0.5):
        return learn.enqueue(self._learn_conn(), topic, why, source, prio)

    def learn_claim(self):
        return learn.claim(self._learn_conn())

    def learn_stats(self):
        return learn.stats(self._learn_conn())

    def learn_list(self, limit=20, status=None):
        return learn.listing(self._learn_conn(), limit=limit, status=status)

    def learn_seed(self, texts, source="chat", limit=3):
        return learn.seed_from(self._learn_conn(), texts, source=source, limit=limit)

    def learn_due(self):
        return learn.due(self._learn_conn())

    # ---------- 交作业 ----------
    def learn_note(self, text, topic=None, task_id=None, tags=None, source="learn",
                   salience=0.62):
        """把「学到了什么」写进卡：study 记忆（可召回）+ fact，然后把任务标 done。"""
        text = (text or "").strip()
        if len(text) < 6:
            return {"ok": False, "err": "note_too_short"}
        body = text
        if topic and topic not in text[:40]:
            body = "【学习】%s：%s" % (topic, text)
        # remember() 返回 mem id（int）；None 表示被去重/剥空了
        mid = self.remember(
            body, salience=salience, region="study", speaker="self",
            mtype="episode", pool="long", source="learn",
        )
        mid = int(mid) if isinstance(mid, int) else None
        fact_ok = False
        if mid and topic:
            # facts.record 第一参数是「库路径」（内部自己开连接），不是 Connection
            # 2026-11-17 修：之前传 self.db 会报
            # expected str, bytes or os.PathLike object, not Connection
            dbp = getattr(self, "db_path", None) or getattr(self, "_db_path", None)
            try:
                n_fact, _items = facts.record(
                    str(dbp) if dbp else self.db, mid,
                    "%s：%s" % (topic, text[:300]),
                    speaker="self", source="learn")
                fact_ok = int(n_fact or 0)
            except Exception as exc:      # 事实层挂了不该让整条笔记失败
                fact_ok = "err:%s" % exc
        done = None
        conn = self._learn_conn()
        if task_id:
            done = learn.finish(conn, task_id, mem_id=mid, note=text[:2000])
        else:
            row_t = conn.execute(
                "SELECT id FROM learn_task WHERE status='doing' ORDER BY claim_ts DESC LIMIT 1"
            ).fetchone()
            if row_t:
                done = learn.finish(conn, row_t[0], mem_id=mid, note=text[:2000])
        return {"ok": True, "mem_id": mid, "fact": fact_ok, "task": done}

    def learn_render(self, limit=8):
        return learn.render_queue(self._learn_conn(), limit=limit)
