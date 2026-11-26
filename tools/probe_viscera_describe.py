# -*- coding: utf-8 -*-
"""describe() 的验收探针：哪些话永远说不出来、满脉冲能不能把它推亮。

为什么要这个（2026-11-25 对照文档 §5 建议 4）：
  `内脏数据字典` 的脚本只测「推不推得动」，**不测「推到了阈值没有」**。
  于是「cort > 55」这种被 inject() 的带子物理封死的分支，一直躺在代码里，
  没有任何判据能发现它。作者自己在论文里写过同样的坑，并给了验收办法
  （`论文-会忘的本地智能体.md:183`：交替「夸三句」「骂三句」的对照 trial）——
  那套办法从没对 describe() 跑过。

这个脚本做两件事：

  ① 历史回放（只读）
     读 viscera_hist 全表，逐格跑 describe()，统计：
       · 每句话出现的次数与占比
       · 每条分支的命中率（含「永远 0 次」的）
       · describe() 与分支清单的一致性（分支 ≤ 3 时必须逐字相同）

  ② 对照 trial（纯内存，不碰库、不碰卡）
     交替 inject 满负脉冲 / 满正脉冲各 N 轮，看 describe() 的句子集合
     有没有变化。**这是唯一能回答「这条分支到底能不能亮」的方法** ——
     历史回放只能说明「过去没亮过」，说明不了「永远亮不了」。

用法：
    py -3.12 tools\\probe_viscera_describe.py                    # 读活卡（只读）
    py -3.12 tools\\probe_viscera_describe.py --db <卡路径>
    py -3.12 tools\\probe_viscera_describe.py --trials 12        # 每边 12 轮

退出码：0 = 全部通过；1 = 有分支恒不可达（需要人看）
"""
import argparse
import io
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import viscera as V  # noqa: E402


# ---------------------------------------------------------------------------
# 分支清单：与 describe() 平行写一份，用来算「每条分支亮没亮」。
#
# ⚠ 这份清单和 describe() 是两份代码，**会漂移**。所以下面 check_consistency()
#   每次都会比对：分支 ≤ MAX_SAY 时，describe() 的输出必须逐字等于分支拼接。
#   漂移了它会报错 —— 那说明有人改了 describe() 却没改这里，不是「探针过时了」。
# ---------------------------------------------------------------------------
# 标签 → 句子。**照抄 describe() 里的原文**，故意维护两份：
# 这里如果和 describe() 对不上，说明有人改了那边的句子而没同步 —— 那正是探针要报的。
SAY_TEXT = {
    "hr_max": "心跳得厉害，咚咚的，自己都听得见",
    "hr_hi": "心跳快了一拍",
    "hr_mid": "心跳比刚才快一点",
    "hr_low": "心跳慢下来了，整个人有点软",
    "resp_hi_fast": "呼吸有点急，话赶着话",
    "resp_hi": "呼吸变浅了",
    "resp_low": "呼吸变得又长又轻",
    "face_max": "脸上烧起来，连耳尖都烫",
    "face_hi": "耳尖有点发烫",
    "face_mid": "脸颊有一点点热",
    "tension_hi": "手指微微发紧，肩膀不由自主绷着",
    "tension_mid": "身体有点绷着",
    "adeno": "眼皮发沉，脑子转得慢半拍",
    "mel": "困劲儿上来了，语气自己就软",
    "cort": "胸口闷闷的，不太想说话",
    "kyn": "心里空落落的，说不清为什么",
    "ofq": "整个人像蒙了层东西，情绪离得有点远",
    "oxy": "心里是暖的，想靠近一点",
    "gonad": "有点不好意思，不太敢看你",
    "dopa": "精神头往上冲，话想往外冒",
    "sero_low": "心里毛毛躁躁的，容易往坏处想",
}


def branches(state):
    """返回所有命中的分支标签（顺序与 describe() 一致）。"""
    s = V.normalize(state)
    b = V.body(s)
    h = s["h"]
    T = V.SAY_TH
    out = []

    hr, resp, face, tension = b["hr"], b["resp"], b["face"], b["tension"]
    if hr >= T["hr_max"]:
        out.append("hr_max")
    elif hr >= T["hr_hi"]:
        out.append("hr_hi")
    elif hr >= T["hr_mid"]:
        out.append("hr_mid")
    elif hr <= T["hr_low"]:
        out.append("hr_low")

    if resp >= T["resp_hi_fast"]:
        out.append("resp_hi_fast")
    elif resp >= T["resp_hi"]:
        out.append("resp_hi")
    elif resp <= T["resp_low"]:
        out.append("resp_low")

    if face >= T["face_max"]:
        out.append("face_max")
    elif face >= T["face_hi"]:
        out.append("face_hi")
    elif face >= T["face_mid"]:
        out.append("face_mid")

    if tension >= T["tension_hi"]:
        out.append("tension_hi")
    elif tension >= T["tension_mid"]:
        out.append("tension_mid")

    if h["adeno"] > T["adeno"] and h["orex"] < T["orex_low"]:
        out.append("adeno")
    if h["mel"] > T["mel"]:
        out.append("mel")
    if h["cort"] > T["cort"]:
        out.append("cort")
    if h["kyn"] > T["kyn"] and h["cort"] < T["kyn_cort_below"]:
        out.append("kyn")
    if h["ofq"] > T["ofq"]:
        out.append("ofq")
    if h["oxy"] > T["oxy"] and h["cort"] < T["oxy_cort_below"]:
        out.append("oxy")
    if h["gonad"] > T["gonad"] and h["oxy"] > T["gonad_oxy_above"]:
        out.append("gonad")
    if h["dopa"] > T["dopa"]:
        out.append("dopa")
    if h["sero"] < T["sero_low"]:
        out.append("sero_low")

    return out


def check_consistency(state):
    """describe() 与分支清单是否一致。分支 ≤ MAX_SAY 时应逐字相同。"""
    br = branches(state)
    txt = V.describe(state)
    if not br:
        return txt == V.SAY_QUIET
    if len(br) <= V.MAX_SAY:
        return txt == "；".join(SAY_TEXT[b] for b in br)
    return True          # 超过 MAX_SAY 时被截断，不比


# ---------------------------------------------------------------------------
# ① 历史回放
# ---------------------------------------------------------------------------
def replay(db_path):
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM viscera_hist ORDER BY id").fetchall()
    except sqlite3.OperationalError as e:
        print("  读 viscera_hist 失败：%s" % e)
        con.close()
        return None
    con.close()

    from collections import Counter
    say = Counter()
    brc = Counter()
    inconsistent = 0
    total = 0
    for r in rows:
        d = dict(r)
        try:
            h = json.loads(d["h"]) if d.get("h") else {}
        except Exception:
            h = {}
        st = {"h": h,
              "symp": float(d.get("sym") or 0.0),
              "para": float(d.get("para") or 0.0),
              "ts": float(d.get("ts") or 0.0)}
        total += 1
        say[V.describe(st)] += 1
        for b in branches(st):
            brc[b] += 1
        if not check_consistency(st):
            inconsistent += 1
    return total, say, brc, inconsistent


# ---------------------------------------------------------------------------
# ② 对照 trial（纯内存）
# ---------------------------------------------------------------------------
# 四组脉冲，**覆盖每一个能被 describe 读到的方向**。
# 之前只推了「高方向」，于是 hr_low / resp_low 这些「低方向」分支被判成
# 「恒不可达」—— 那是探针没推，不是代码推不亮。这类假阳性比漏报更坏：
# 它会把一个正常分支记成死的，然后有人照着去改阈值。
PULSES = [
    # (标签, deltas, sym, para)
    ("交感+应激", {"cort": 1.0, "nora": 1.0, "sero": -1.0, "dopa": -1.0}, 1.0, 0.0),
    ("副交感+放松", {"cort": -1.0, "dopa": -1.0, "oxy": -1.0}, 0.0, 1.0),
    ("愉快+亲密", {"dopa": 1.0, "sero": 1.0, "oxy": 1.0, "endo": 1.0}, 0.35, 0.0),
    ("困倦+低落", {"mel": 1.0, "adeno": 1.0, "orex": -1.0, "kyn": 1.0,
                   "ofq": 1.0, "sero": -1.0, "gonad": 1.0}, 0.0, 0.6),
]


def trials(n=6):
    """每种脉冲**各从基线重新开始**，推 n 轮。

    为什么每组要重置：第一版让四种脉冲共享一个累积状态，结果「副交感+放松」
    起手时 sym 还是 1.000（上一组刚推满），6 轮只把 resp 拉到 13.40，
    于是 resp_low（阈值 13）被误判成「恒不可达」—— 那是探针的假阳性，
    不是代码的问题。**假阳性比漏报更坏：它会把一个正常分支记成死的，
    然后有人照着去改阈值。**

    这也正是作者论文里的做法（论文-会忘的本地智能体.md:183）：
    「同一副本上跑 6 个 trial……**每个 trial 前把四个激素写回基线**」。
    """
    seq = []
    for tag, deltas, sym, para in PULSES:
        st = V.base_state()          # ← 每组独立，不继承上一组的残留
        for i in range(n):
            st = V.inject(st, deltas, sym=sym, para=para)
            seq.append(("%s %d" % (tag, i + 1), V.describe(st), branches(st)))
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("WHALE_DB") or os.path.join(ROOT, "card.db"))
    ap.add_argument("--trials", type=int, default=6)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 68)
    print(" describe() 验收探针")
    print(" 卡: %s" % a.db)
    print("=" * 68)

    # ---- ① 历史回放 ----
    print("\n① 历史回放（只读）")
    got = replay(a.db)
    hit_branches = set()
    if got is None:
        print("  跳过")
    else:
        total, say, brc, inconsistent = got
        print("  回放 %d 格" % total)
        print("\n  输出分布（前 8）：")
        for s, c in say.most_common(8):
            print("    %5d  %5.1f%%  「%s」" % (c, c * 100.0 / total, s))
        print("\n  分支命中：")
        all_b = set(V.SAY_TH.keys()) - {"orex_low", "kyn_cort_below",
                                        "oxy_cort_below", "gonad_oxy_above"}
        for b in sorted(brc, key=lambda x: -brc[x]):
            print("    %-16s %5d 次  %5.1f%%" % (b, brc[b], brc[b] * 100.0 / total))
        hit_branches = set(brc.keys())
        dead = sorted(all_b - hit_branches)
        print("\n  历史上**一次都没亮过**的分支：%d 条" % len(dead))
        for b in dead:
            print("    · %s" % b)
        print("\n  describe() 与分支清单不一致：%d 格" % inconsistent)
        if inconsistent:
            print("    ⚠ 说明 describe() 被改过而探针没跟上，先对齐再谈别的")

    # ---- ② 对照 trial ----
    print("\n② 对照 trial（纯内存，不碰库）")
    seq = trials(a.trials)
    seen = set()
    for tag, txt, br in seq:
        seen.add(txt)
        print("    %-10s 「%s」  分支=%s" % (tag, txt, ",".join(br) or "-"))
    print("\n  不同输出 %d 种" % len(seen))
    trial_branches = set()
    for _, _, br in seq:
        trial_branches |= set(br)

    # ---- 结论 ----
    print("\n" + "=" * 68)
    all_b = set(V.SAY_TH.keys()) - {"orex_low", "kyn_cort_below",
                                    "oxy_cort_below", "gonad_oxy_above"}
    never = all_b - hit_branches - trial_branches
    print(" 恒不可达（历史没亮 + 满脉冲也推不亮）：%d 条" % len(never))
    for b in sorted(never):
        print("   · %s（阈值 %.3g）" % (b, V.SAY_TH[b]))
    if never:
        print("\n  这【不是】探针坏了 —— 是 inject() 的 min(cap, 2.5·base) 带子")
        print("  把这些阈值挡在了外面。改 describe 的阈值可解，改带子会带偏激素层。")
        print("=" * 68)
        return 1
    print("\n 全部分支可达。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
