# -*- coding: utf-8 -*-
"""E1c：显示阈值的敏感性（预注册网格）+ K 的敏感性。

只用 e1b_hits.json（一次召回结果与其权重），不重新跑检索，
因此结论是「这批召回结果在给定阈值下的可见/门控表现」，不是新的检索实验。

网格（看结果前固定）：
  阈值 TH = [0.05, 0.10, 0.15, 0.20, 0.30]（0.15 为当前实现，标为基准点）
  截断 K  = [5, 10, 13]
  时间点  = 0 / 30 / 90 天
输出每个格子的：R@K（不过滤，应恒定）、证据仍可见率、门控后 R@K。
"""
import io
import json
import sys

sys.path.insert(0, "D:\\")
import poolmath  # noqa: E402

HITS = r"E:\新建文件夹 (5)\论文-记忆稳态\e1b_hits.json"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e1b_sweep.json"
TH = [0.05, 0.10, 0.15, 0.20, 0.30]
KS = [5, 10, 13]
DAYS = [0, 30, 90]
BASE_TH = 0.15


def main():
    probes = json.load(io.open(HITS, encoding="utf-8"))
    n = float(len(probes))
    out = {"n": len(probes), "note": "hits-only sensitivity; no re-retrieval",
           "thresholds": TH, "ks": KS, "days": DAYS, "base_th": BASE_TH, "grid": {}}
    for days in DAYS:
        for k in KS:
            for th in TH:
                r_k = 0
                vis = 0
                gate_r_k = 0
                for p in probes:
                    base = []
                    for h in p["hits"][:k]:
                        wT = poolmath.weight_now(h["w"], h["pool"], h["t_anchor"],
                                                 p["now"] + days * 86400.0, tier=h["tier"])
                        base.append((h["rel"], float(wT)))
                    rels = [1 if b[0] else 0 for b in base]
                    r_k += 1 if any(rels) else 0
                    kept = [b[0] for b in base if b[1] >= th]
                    gate_r_k += 1 if any(kept) else 0
                    if rels and rels[0]:
                        pass
                    # 证据仍可见：第一条相关命中是否过阈值
                    first_rel = next((j for j, x in enumerate(base) if x[0]), None)
                    if first_rel is not None and base[first_rel][1] >= th:
                        vis += 1
                out["grid"]["%dd_k%d_th%.2f" % (days, k, th)] = {
                    "r_at_k": round(100 * r_k / n, 1),
                    "ev_visible": round(100 * vis / n, 1),
                    "gate_r_at_k": round(100 * gate_r_k / n, 1),
                }
        print("[%dd] done" % days, flush=True)

    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))

    print("\n=== K=13, 门控 R@13 / 证据可见率（分母 n=%d）===" % len(probes))
    print("threshold |  0d R@13 / 可见 | 30d R@13 / 可见 | 90d R@13 / 可见")
    for th in TH:
        cells = []
        for days in DAYS:
            g = out["grid"]["%dd_k13_th%.2f" % (days, th)]
            cells.append("%5.1f%% / %5.1f%%" % (g["gate_r_at_k"], g["ev_visible"]))
        print("  %.2f    | %s" % (th, " | ".join(cells)))
    print("\n=== 阈值 0.15 基准下，K 的影响（门控命中 / 证据可见）===")
    for days in DAYS:
        row = []
        for k in KS:
            g = out["grid"]["%dd_k%d_th%.2f" % (days, k, BASE_TH)]
            row.append("K=%d: %5.1f%%/%5.1f%%" % (k, g["gate_r_at_k"], g["ev_visible"]))
        print("%2dd  %s" % (days, "  ".join(row)))
    print("\nsaved", OUT)


if __name__ == "__main__":
    main()
