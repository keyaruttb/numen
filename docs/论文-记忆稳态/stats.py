# -*- coding: utf-8 -*-
"""论文用：真卡快照统计（只读），输出 JSON 供随稿复算。"""
import hashlib
import json
import os
import sqlite3
import time

DB = r"D:\card.db"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\stats.json"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    q = lambda s, a=(): c.execute(s, a).fetchone()[0]
    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "db": DB,
        "db_bytes": os.path.getsize(DB),
        "db_sha256": sha256(DB),
        "mem_alive": q("SELECT count(*) FROM mem WHERE dead=0"),
        "mem_dead": q("SELECT count(*) FROM mem WHERE dead=1"),
        "chunk": q("SELECT count(*) FROM chunk"),
        "dialogue": q("SELECT count(*) FROM dialogue"),
        "mem_birth": q("SELECT count(*) FROM mem_birth"),
        "synapse": q("SELECT count(*) FROM synapse"),
        "impression": q("SELECT count(*) FROM impression"),
        "tiers": dict(c.execute("SELECT tier, count(*) FROM mem WHERE dead=0 GROUP BY tier").fetchall()),
        "pools": dict(c.execute("SELECT pool, count(*) FROM mem WHERE dead=0 GROUP BY pool").fetchall()),
        "regions": dict(c.execute("SELECT region, count(*) FROM mem WHERE dead=0 GROUP BY region").fetchall()),
        "recall_days_ge3_mid": q("SELECT count(*) FROM mem WHERE tier='mid' AND dead=0 AND COALESCE(recall_days,0)>=3"),
        "mean_w": round(q("SELECT avg(w) FROM mem WHERE dead=0") or 0, 4),
        "feedback": dict(c.execute("SELECT kind, count(*) FROM feedback GROUP BY kind").fetchall()),
        "intention": dict(c.execute("SELECT status, count(*) FROM intention GROUP BY status").fetchall()),
        "gov": dict(c.execute("SELECT source, count(*) FROM gov_decision GROUP BY source").fetchall()),
        "learn_task": q("SELECT count(*) FROM learn_task"),
        "python": os.sys.version.split()[0],
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
