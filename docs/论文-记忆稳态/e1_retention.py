# -*- coding: utf-8 -*-
"""E1 遗忘三臂（真卡快照，用系统真实的 weight_now 与可见阈值）。

口径：
- 阈值 SHOW_HINT = 0.15（低于它，这条记忆在组装时只当"好像提过"的提示，正文推不出来）。
- 臂 a 分档 + 晋升：core/core_mut 不衰；keep 走 LAM_L（半衰期 ~371 天）；
  mid 走真实 LAM_M 曲线；并且 recall_days>=3 的中期记忆**已被晋升为 keep**（本系统现在的行为）。
- 臂 b 均匀衰减：所有档都按 mid 的 LAM_M 走（不分档、不晋升）。
- 臂 c 全留：权重不动。
用法：python -u _e1_retention.py
"""
import json
import sqlite3
from collections import Counter

REAL = r"D:\card.db"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e1.json"
SHOW_HINT = 0.15
HORIZONS = (30, 90, 180, 365)

import os
import sys
sys.path.insert(0, "D:\\")
os.chdir("D:\\")
import poolmath as pm  # noqa: E402

PROMOTE_DAYS = 3


def main():
    con = sqlite3.connect("file:%s?mode=ro" % REAL, uri=True)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute("SELECT id, w, pool, t_anchor, region, tier, recall_days, dead FROM mem"):
        if r["dead"]:
            continue
        rows.append(r)
    tiers = Counter(x["tier"] for x in rows)
    rd_elig = sum(1 for x in rows if int(x["recall_days"] or 0) >= PROMOTE_DAYS and x["tier"] == "mid")
    print("[i] 记忆 %d 行；分档 %s；可晋升（recall_days>=%d 且 mid）%d 条"
          % (len(rows), dict(tiers), PROMOTE_DAYS, rd_elig))

    def w_at(r, days, arm):
        t_anchor = float(r["t_anchor"] or 0.0)
        if not t_anchor:
            return float(r["w"] or 0.0)
        now = t_anchor + days * 86400.0
        if arm == "c":
            return float(r["w"] or 0.0)
        if arm == "b":
            return pm.weight_now(float(r["w"] or 0.0), r["pool"], t_anchor, now, r["region"], "mid")
        tier = r["tier"]
        if tier == "mid" and int(r["recall_days"] or 0) >= PROMOTE_DAYS:
            tier = "keep"   # 已晋升
        return pm.weight_now(float(r["w"] or 0.0), r["pool"], t_anchor, now, r["region"], tier)

    out = {"rows": len(rows), "tiers": dict(tiers), "promotable": rd_elig,
           "show_hint": SHOW_HINT, "by_arm": {}}
    for arm in ("a", "b", "c"):
        per = {}
        for days in HORIZONS:
            alive = 0
            wsum = 0.0
            per_tier = Counter()
            for r in rows:
                w = w_at(r, days, arm)
                if w >= SHOW_HINT:
                    alive += 1
                    wsum += w
                    per_tier[r["tier"]] += 1
            per[str(days)] = {
                "visible": round(alive / len(rows), 4),
                "mean_w": round(wsum / max(1, alive), 4),
                "by_tier": {k: round(v / tiers[k], 4) for k, v in per_tier.items()},
            }
            print("[%s] T=%3dd 可见 %.1f%% 均权 %.3f %s"
                  % (arm, days, 100.0 * alive / len(rows), wsum / max(1, alive),
                     {k: "%.0f%%" % (100.0 * v / tiers[k]) for k, v in per_tier.items()}))
        out["by_arm"][arm] = per
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", OUT)


if __name__ == "__main__":
    main()
