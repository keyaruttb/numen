# -*- coding: utf-8 -*-
"""E4：压缩回归闸抽样覆盖与基线命中（在副本上，不写任何东西）。"""
import json
import os
import sys
import time

sys.path.insert(0, "D:\\")
os.chdir("D:\\")
import card2  # noqa: E402

DB = r"E:\新建文件夹 (5)\_e3e4\card.db"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e4.json"

card = card2.MemoryCard(DB, clock=card2.Clock(time.time()))
sample = card._dream_regress_sample()
res = card._dream_regress_check(sample, top_k=5)
print("sample", len(sample), "check", res)

info = []
c = card.db.cursor()
for item in sample:
    mid = item[0] if isinstance(item, (tuple, list)) else item
    row = c.execute("SELECT id, region, tier, length(raw) L FROM mem WHERE id=?", (mid,)).fetchone()
    if row:
        info.append({"id": row[0], "region": row[1], "tier": row[2], "len": row[3]})
regions = sorted({x["region"] for x in info})
print("regions", regions, "lens", [x["len"] for x in info])
json.dump({"sample_n": len(sample), "check": res, "items": info,
           "regions": regions}, open(OUT, "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("saved", OUT)
