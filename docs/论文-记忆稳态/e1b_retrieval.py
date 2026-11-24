# -*- coding: utf-8 -*-
"""E1b：在各衰减臂、各时间点上「真跑一遍检索」，把可见率推进到 R@K。

做法：
  1. 用冻结测试集（tools/eval_frozen.py 同一口径）对每个问题跑一次真实 recall，
     记下 top-K 的 mem_id、当前权重、以及每条命中是不是「证据」。
  2. 对每个臂 × 时间点，用 poolmath.weight_now 把权重外推到 T 天，
     再判「这条还看得见（w >= SHOW_HINT）」。
  3. 两套指标：
     - 可见性口径：证据排序不变（因为排序里没有权重项），只算证据是否仍可见；
     - 门控口径（arm d）：把不可见的候选从候选池里剔掉再重排，模拟「真按权重筛」。
输出 e1b.json。
"""
import io
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, r"D:\_projects\whale-card-oss")
sys.path.insert(0, r"D:\_projects\whale-card-oss\tools")
sys.path.insert(0, "D:\\")

import poolmath  # noqa: E402

ROOT = r"D:\_projects\whale-card-oss"
B = r"D:\_projects\bench22\benchmark-2.2\dataset"
BENCH = r"E:\新建文件夹 (5)\_bench\card.db"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e1b.json"
SHOW_HINT = 0.15
HORIZONS = [0, 30, 90, 180, 365]
ARMS = {"a_tier": None, "b_uniform_mid": "mid", "c_keep_all": "core_mutable"}

import eval_frozen as EF  # noqa: E402


def main():
    hist = EF.load(os.path.join(B, "histories", "M01.jsonl"))[:7043]
    ex = [e for e in EF.load(os.path.join(B, "exam-inputs.jsonl")) if e["persona"] == "M01"]
    key = {x["id"]: x for x in EF.load(os.path.join(B, "review-only", "answer-key.jsonl"))}
    tset = set(EF.load_freeze()["test"])

    import card2  # noqa: E402

    class VC:
        def __init__(self, t):
            self.t = t

        def now(self):
            return self.t

        def __call__(self):
            return self.t

    vc = VC(0.0)
    card = card2.MemoryCard(BENCH, clock=vc, card_dir=os.path.dirname(BENCH))
    print("[i] sem_ready", card.sem_ready(), flush=True)

    probes = []
    for i, e in enumerate(ex):
        if e["id"] not in tset:
            continue
        vc.t = datetime.fromisoformat(e["now"]).timestamp()
        ev = key[e["id"]].get("evidence_turns") or []
        ev_wins = []
        for tn in ev:
            if not (1 <= tn <= len(hist)):
                continue
            r = hist[tn - 1]
            ev_wins += EF.windows(r.get("用户消息") or "") + EF.windows(r.get("助手回复") or "")
        if not ev_wins:
            continue
        hits = card.recall(e["question"], top_k=13, access=False)
        rows = []
        for rank, h in enumerate(hits, 1):
            try:
                r = h[2]
            except Exception:
                r = None
            raw = str(r["raw"]) if r is not None else ""
            rel = 1 if EF.relevant(raw, ev_wins) else 0
            rows.append({
                "id": int(r["id"]) if r is not None else -1,
                "rel": rel,
                "w": float(r["w"] or 0.0) if r is not None else 0.0,
                "pool": str(r["pool"] or "") if r is not None else "",
                "t_anchor": float(r["t_anchor"] or vc.t) if r is not None else vc.t,
                "tier": str(r["tier"] or "") if r is not None else "",
            })
        probes.append({"id": e["id"], "now": vc.t, "hits": rows})
        if (i + 1) % 20 == 0:
            print("  probe %d/%d" % (i + 1, len(ex)), flush=True)
    print("[i] probes", len(probes), flush=True)

    out = {"n": len(probes), "show_hint": SHOW_HINT, "horizons": HORIZONS, "arms": {}}
    for arm, force_tier in ARMS.items():
        res = {}
        for days in HORIZONS:
            r1 = r3 = r13 = 0
            vis_ev = 0          # 证据仍可见（排序不变）
            gate_r1 = gate_r3 = gate_r13 = 0
            for p in probes:
                base = []
                for h in p["hits"]:
                    tier = force_tier or h["tier"]
                    wT = poolmath.weight_now(h["w"], h["pool"], h["t_anchor"],
                                             p["now"] + days * 86400.0, tier=tier)
                    base.append((h["rel"], float(wT), h["id"]))
                rels = [1 if b[0] else 0 for b in base]
                r1 += rels[0] if rels else 0
                r3 += 1 if any(rels[:3]) else 0
                r13 += 1 if any(rels) else 0
                first_rel = next((j for j, x in enumerate(base) if x[0]), None)
                if first_rel is not None and base[first_rel][1] >= SHOW_HINT:
                    vis_ev += 1
                kept = [b for b in base if b[1] >= SHOW_HINT]
                grels = [1 if b[0] else 0 for b in kept]
                gate_r1 += grels[0] if grels else 0
                gate_r3 += 1 if any(grels[:3]) else 0
                gate_r13 += 1 if any(grels) else 0
            n = float(len(probes))
            res[str(days)] = {
                "r1": round(100 * r1 / n, 1), "r3": round(100 * r3 / n, 1),
                "r13": round(100 * r13 / n, 1), "ev_visible": round(100 * vis_ev / n, 1),
                "gate_r1": round(100 * gate_r1 / n, 1), "gate_r3": round(100 * gate_r3 / n, 1),
                "gate_r13": round(100 * gate_r13 / n, 1),
            }
        out["arms"][arm] = res
        print("[%s]" % arm, json.dumps(res, ensure_ascii=False), flush=True)

    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    print("saved", OUT)


if __name__ == "__main__":
    main()



