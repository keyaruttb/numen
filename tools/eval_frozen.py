# -*- coding: utf-8 -*-
"""R5：冻结测试集 + 检索指标（Recall@k / MRR / NDCG / 证据率 / 证据丢失率 / 时延分位）。

设计口径（写死，别再改）：
- 题目池：bench22 的 M01 全部考题（跟以前那 417 题同一来源）。
- 冻结方式：按 sha256(id) 排序取前 N=120 做 **Test**，其余是 Dev。
  Test 的 id 列表与整体 sha256 落盘到 eval_frozen_test.json，**一旦生成就不再重新抽**；
  以后调参只看 Dev，Test 只在「要发版/要宣称数字」时跑一次。
- 相关性判定：沿用旧口径——检索到的 mem.raw 里出现证据轮的 10 字窗即算命中；
  只在证据轮里出现的字段（用户消息 / 助手回复）两处都查。
- 指标：R@1 / R@3 / R@13、MRR@13、NDCG@10（二值相关）、证据率（组装后 prompt 里有没有）、
  证据丢失率（召回前 13 里有、组装后 prompt 里没有）、时延 P50/P95/P99。
用法：python -u eval_frozen.py [--split test|dev] [--n 120] [--rebuild]
"""
import os
import sys
import json
import re
import time
import math
import hashlib
import argparse
from datetime import datetime

D_DIR = "D:" + "\\"
sys.path.insert(0, D_DIR)
os.chdir(D_DIR)

BENCH = r"E:\新建文件夹 (5)\_bench\card.db"
B = r"D:\_projects\bench22\benchmark-2.2\dataset"
FREEZE = r"E:\新建文件夹 (5)\eval_frozen_test.json"


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def norm(s):
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", s or "")


def windows(s, w=10, step=3):
    n = norm(s)
    if not n:
        return []
    return [n[i:i + w] for i in range(0, max(1, len(n) - w + 1), step)]


def relevant(raw, ev_wins):
    """这条记忆里有没有任何证据窗。"""
    n = norm(raw)
    if not n:
        return False
    return any(w in n for w in ev_wins)


def build_freeze(ids, n):
    hs = sorted(ids, key=lambda x: hashlib.sha256(str(x).encode()).hexdigest())
    test = hs[:n]
    payload = {"rule": "sha256(id) 升序取前 n", "n": n, "test": test,
               "pool_n": len(hs), "sha256": hashlib.sha256(("\n".join(test)).encode()).hexdigest(),
               "frozen_at": datetime.now().isoformat(timespec="seconds")}
    with open(FREEZE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def load_freeze():
    with open(FREEZE, encoding="utf-8") as f:
        p = json.load(f)
    now = hashlib.sha256(("\n".join(p["test"]).encode())).hexdigest()
    if now != p.get("sha256"):
        raise SystemExit("冻结集被改了：sha256 对不上（%s != %s）" % (now, p.get("sha256")))
    return p


def dcg(gains):
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test", choices=["test", "dev", "all"])
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--k", type=int, default=13)
    a = ap.parse_args()

    os.environ.pop("WHALE_MULTI_VEC", None)
    import card2

    hist = load(os.path.join(B, "histories", "M01.jsonl"))[:7043]
    ex = [e for e in load(os.path.join(B, "exam-inputs.jsonl")) if e["persona"] == "M01"]
    key = {x["id"]: x for x in load(os.path.join(B, "review-only", "answer-key.jsonl"))}
    ids = [e["id"] for e in ex]
    print("[i] 题池 %d 道" % len(ids), flush=True)

    if a.rebuild or not os.path.exists(FREEZE):
        p = build_freeze(ids, a.n)
        print("[i] 冻结新生成：test %d / pool %d / sha %s…"
              % (len(p["test"]), p["pool_n"], p["sha256"][:12]), flush=True)
    else:
        p = load_freeze()
        print("[i] 冻结集：test %d / pool %d / 冻结于 %s / sha %s…"
              % (len(p["test"]), p["pool_n"], p.get("frozen_at"), p["sha256"][:12]), flush=True)

    tset = set(p["test"])
    if a.split == "test":
        ex = [e for e in ex if e["id"] in tset]
    elif a.split == "dev":
        ex = [e for e in ex if e["id"] not in tset]

    class VC:
        def __init__(self, t):
            self.t = t

        def now(self):
            return self.t

        def __call__(self):
            return self.t

    vc = VC(0.0)
    card = card2.MemoryCard(BENCH, clock=vc, card_dir=os.path.dirname(BENCH))
    print("[i] sem_ready", card.sem_ready(), "题 %d" % len(ex), flush=True)

    K = a.k
    rows = []
    for i, e in enumerate(ex):
        vc.t = datetime.fromisoformat(e["now"]).timestamp()
        ev = key[e["id"]].get("evidence_turns") or []
        ev_wins = []
        for tn in ev:
            if not (1 <= tn <= len(hist)):
                continue
            r = hist[tn - 1]
            ev_wins += windows(r.get("用户消息") or "") + windows(r.get("助手回复") or "")
        if not ev_wins:
            continue

        t0 = time.time()
        hits = card.recall(e["question"], top_k=K, access=False)
        dt = (time.time() - t0) * 1000.0

        def _raw(h):
            # recall 返回 [(score, wn, row), ...]
            try:
                row = h[2]
                return str(row["raw"] if not isinstance(row, dict) else row.get("raw") or "")
            except Exception:
                return str(getattr(h, "raw", "") or "")

        # 2026-11-22（外部评审 §5.1）：recall 会顺着突触追加「联想尾巴」，返回值可能比
        # top_k 长。R@K / MRR 必须只看前 K 条，否则分数被尾巴抬高。
        hit_k = hits[:K]
        rels_all = [1 if relevant(_raw(h), ev_wins) else 0 for h in hits]
        rels = rels_all[:K]

        t1 = time.time()
        ctx = card.assemble_context(e["question"], identity="", recent_lines=None,
                                    access=False, touch=False)
        dt2 = (time.time() - t1) * 1000.0
        txt = norm(ctx.get("text") if isinstance(ctx, dict) else str(ctx))
        in_prompt = 1 if any(w in txt for w in ev_wins) else 0
        in_recall = 1 if any(rels) else 0

        first = next((j for j, r in enumerate(rels) if r), None)
        rr = 1.0 / (first + 1) if first is not None else 0.0
        ndcg = dcg(rels_all[:10]) / dcg([1] * sum(rels_all[:10])) if any(rels_all[:10]) else 0.0

        rows.append({"id": e["id"], "r1": rels[0] if rels else 0,
                     "r3": 1 if any(rels[:3]) else 0, "rK": in_recall,
                     "mrr": rr, "ndcg": ndcg, "ev_prompt": in_prompt,
                     "ev_lost": 1 if (in_recall and not in_prompt) else 0,
                     "ms_recall": dt, "ms_asm": dt2})
        if (i + 1) % 20 == 0:
            print("  %d/%d  R@1 %.1f%%  R@%d %.1f%%  证据 %.1f%%"
                  % (i + 1, len(ex), 100.0 * sum(x["r1"] for x in rows) / len(rows), K,
                     100.0 * sum(x["rK"] for x in rows) / len(rows),
                     100.0 * sum(x["ev_prompt"] for x in rows) / len(rows)), flush=True)

    n = len(rows)
    if not n:
        raise SystemExit("没有可用题目")

    def avg(k):
        return sum(x[k] for x in rows) / n

    def pct(k, p):
        s = sorted(x[k] for x in rows)
        return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]

    out = {
        "split": a.split, "n": n, "k": K,
        "R@1": round(100 * avg("r1"), 1),
        "R@3": round(100 * avg("r3"), 1),
        "R@%d" % K: round(100 * avg("rK"), 1),
        "MRR@%d" % K: round(avg("mrr"), 4),
        "NDCG@10": round(avg("ndcg"), 4),
        "evidence_in_prompt": round(100 * avg("ev_prompt"), 1),
        "evidence_lost": round(100 * avg("ev_lost"), 1),
        "ms_recall_p50": round(pct("ms_recall", 0.50), 1),
        "ms_recall_p95": round(pct("ms_recall", 0.95), 1),
        "ms_recall_p99": round(pct("ms_recall", 0.99), 1),
        "ms_asm_p95": round(pct("ms_asm", 0.95), 1),
    }
    print("\n[i] ===== %s n=%d =====" % (a.split, n), flush=True)
    for k, v in out.items():
        print("  %-18s %s" % (k, v), flush=True)
    fn = r"E:\新建文件夹 (5)\eval_result_%s.json" % a.split
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"result": out, "rows": rows}, f, ensure_ascii=False)
    print("[i] 明细", fn, flush=True)


if __name__ == "__main__":
    main()
