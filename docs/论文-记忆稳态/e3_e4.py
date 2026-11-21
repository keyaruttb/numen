# -*- coding: utf-8 -*-
"""E3：行为意向 → 实际动作（自主学习的来源）；E4：压缩回归闸的抽样覆盖与基线命中。"""
import json
import os
import shutil
import sqlite3
import time

SRC = r"D:\card.db"
TMP = r"E:\新建文件夹 (5)\_e3e4"
DST = os.path.join(TMP, "card.db")
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e3e4.json"

os.makedirs(TMP, exist_ok=True)
if os.path.exists(DST):
    os.remove(DST)
src = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True)
dst = sqlite3.connect(DST)
src.backup(dst)
dst.execute("DELETE FROM writer_lease")
dst.commit()
dst.close()
src.close()

import sys
sys.path.insert(0, "D:\\")
os.chdir("D:\\")
import card2  # noqa: E402

card = card2.MemoryCard(DST, clock=card2.Clock(time.time()))
db = card.db
c = db.cursor()


def count(sql, args=()):
    return c.execute(sql, args).fetchone()[0]


# ---------- E3 ----------
before_learn = count("SELECT count(*) FROM learn_task")
before_int = count("SELECT count(*) FROM intention")
iid = None
try:
    r = card.feedback._upsert_intention(
        direction="approach", kind="learn_more", mem_id=3158,
        drive="curiosity", scene_tag="study", urge=0.75, note="E3 实验")
    iid = r.get("id") if isinstance(r, dict) else r
except Exception as e:
    print("[e3] upsert err", e)
done = card.act_on_intentions(now=time.time() + 5)
after_learn = count("SELECT count(*) FROM learn_task")
after_int = count("SELECT count(*) FROM intention WHERE status='active'")
src_rows = [dict(zip([d[0] for d in c.description], row)) for row in
            c.execute("SELECT source, count(*) n FROM learn_task GROUP BY source")]
e3 = {"intentions_before": before_int, "intentions_active_after": after_int,
      "learn_tasks_before": before_learn, "learn_tasks_after": after_learn,
      "new_tasks": after_learn - before_learn, "actions": done,
      "learn_task_by_source": src_rows}
print("[e3]", json.dumps(e3, ensure_ascii=False)[:400])

# ---------- E4 ----------
sample = card._dream_regress_sample()
hit, lost = card._dream_regress_check(sample, top_k=5)
e4 = {"sample_n": len(sample),
      "hit_n": len(hit),
      "hit_rate": round(len(hit) / max(1, len(sample)), 4),
      "regions": sorted({str(x[0]) for x in [sample] and sample}) if False else None,
      "sample_mids": [x for x in sample][:15] if isinstance(sample, list) else str(sample)[:200]}
print("[e4] sample", e4["sample_n"], "hit", e4["hit_n"], "rate", e4["hit_rate"])
json.dump({"e3": e3, "e4": e4}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("saved", OUT)
