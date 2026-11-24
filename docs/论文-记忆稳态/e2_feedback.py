# -*- coding: utf-8 -*-
"""E2：情绪负反馈开关（抑制层 on / off），同一串输入下的激素峰值与恢复。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "D:\\")
os.chdir("D:\\")
import hormone as hm          # noqa: E402
import hormone_feedback as hf  # noqa: E402

REAL = r"D:\card.db"
OUT = r"E:\新建文件夹 (5)\论文-记忆稳态\e2.json"
BASES = {"h_sero": 0.66, "h_dopa": 0.48, "h_cort": 0.20, "h_oxy": 0.50}


def fresh_hs():
    return dict(BASES)


def run(with_fb, rounds=12, r=0.45, dt_h=1.0):
    """连续 12 记正向奖励（每分钟一次），看峰值与 12 小时后的回落。"""
    hs = fresh_hs()
    reg = hf.Regulator(cfg=hf.cfg_from_meta(None) if False else dict(hf.DEFAULTS))
    peak = {k: 0.0 for k in BASES}
    supp_events = 0
    hist = []
    for i in range(rounds):
        mods = {}
        if with_fb:
            mods, evs = reg.tick(hs, dt_hours=dt_h)
            supp_events += len(evs)
        cand = hm.apply_r(dict(hs), r, scale=0.22, severity=0.0)
        if with_fb:
            hs = hf.apply_gain(hs, cand, mods)
        else:
            hs = cand
        hs = hm.soft(hs)
        for k in BASES:
            peak[k] = max(peak[k], hs.get(k, 0.0))
        hist.append(round(hs["h_dopa"], 4))
    # 之后静置 12 小时（只回归，不再注入）
    for i in range(12):
        mods = {}
        if with_fb:
            mods, _ = reg.tick(hs, dt_hours=1.0)
            hs = hf.regulated_regress(hs, 1.0, mods)
        else:
            hs = hm.regress_all(hs, 1.0)
        hs = hm.soft(hs)
    return {"peak": {k: round(v, 4) for k, v in peak.items()},
            "after_12h": {k: round(hs.get(k, 0.0), 4) for k in BASES},
            "dopa_trace": hist,
            "suppress_events": supp_events}


def main():
    off = run(False)
    on = run(True)
    print("[off] peak", off["peak"], "after12h", off["after_12h"])
    print("[on ] peak", on["peak"], "after12h", on["after_12h"], "events", on["suppress_events"])
    print("[dopa off]", off["dopa_trace"][-4:], " [dopa on]", on["dopa_trace"][-4:])
    json.dump({"off": off, "on": on, "r": 0.45, "rounds": 12},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", OUT)


if __name__ == "__main__":
    main()
