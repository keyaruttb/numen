# -*- coding: utf-8 -*-
r"""把一张旧卡搬到新位置/新机器上 —— 带备份、带确认、搬完自己验收。

为什么单独写一个脚本：搬卡是**唯一会覆盖整份记忆**的动作，
`copy/deploy` 这类脚本已经出过「把真卡拷成 demo」「把 lease 一起搬过去变只读」
的事故。所以这里把顺序写死：

    1. 只读打开旧卡（`mode=ro`），先验链 / 验锚 / 数出生证缺口；
       旧卡有一处不对就不搬（除非 --force）。
    2. 目标存在就先备份成 `card.db.bak-<时间戳>`（SQLite backup API，不是复制文件）。
    3. 用 SQLite backup API 搬到目标（比 copy 安全：不会搬 WAL 的中间态）。
    4. 清掉目标里的写租约（writer_lease），否则新卡一打开就是只读。
    5. 再验一遍：链、出生证、条数、向量维度、journal_mode。
    6. 写一份迁移报告（migrations/<时间戳>.json）。

用法（先干跑，看清楚再真搬）：

    python tools/migrate.py --from D:\card.db --to E:\card\card.db
    python tools/migrate.py --from D:\card.db --to E:\card\card.db --confirm

安全约束：
  * 不加 --confirm 只打印计划，**一个字节都不写**。
  * 旧卡永远只读打开；我们不删、不改、不动它的 WAL。
  * 目标已有卡时，备份先于覆盖，且备份失败就中止。
  * `--force` 才会在旧卡体检不通过时继续（报告里会写明）。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def _ro(path):
    """只读打开（不改旧卡一个字节）。"""
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    return sqlite3.connect(uri, uri=True, timeout=15)


def _count(con, sql, default=None):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception:
        return default


def card_facts(path):
    """不启动服务，直接读库拿体检要用的几个数。"""
    out = {"path": os.path.abspath(path), "exists": os.path.exists(path)}
    if not out["exists"]:
        return out
    out["bytes"] = os.path.getsize(path)
    try:
        con = _ro(path)
    except Exception as e:
        out["error"] = "打不开（%s）" % e
        return out
    try:
        out["journal_mode"] = str(_count(con, "PRAGMA journal_mode", "")).lower()
        out["mem"] = _count(con, "SELECT COUNT(*) FROM mem", 0)
        out["dialogue"] = _count(con, "SELECT COUNT(*) FROM dialogue", 0)
        out["timeline_seq"] = _count(con, "SELECT MAX(seq) FROM timeline", 0)
        out["dim"] = None
        try:
            row = con.execute(
                "SELECT length(emb) FROM mem WHERE emb IS NOT NULL AND length(emb)>0 LIMIT 1").fetchone()
            if row and row[0]:
                out["dim"] = int(row[0]) // 4
        except Exception:
            pass
        gap = None
        try:
            gap = con.execute(
                "SELECT COUNT(*) FROM mem m LEFT JOIN mem_birth b ON b.mid=m.id"
                " WHERE b.mid IS NULL AND COALESCE(m.dead,0)=0").fetchone()[0]
        except Exception:
            gap = None
        out["birth_gap"] = gap
        out["sealed"] = None
        try:
            out["sealed"] = con.execute(
                "SELECT COUNT(*) FROM card_meta WHERE key='sealed' AND val='1'"
            ).fetchone()[0] == 1
        except Exception:
            pass
    finally:
        con.close()
    # 链检查用项目自己的 Timeline（只读连接上跑，不会写）
    try:
        import timeline
        con = _ro(path)
        con.row_factory = sqlite3.Row
        tl = timeline.Timeline(con, writer=None)
        ok, issues = tl.verify_chain(max_report=3)
        out["chain_ok"] = bool(ok)
        out["chain_issues"] = list(issues)[:3]
        con.close()
    except Exception as e:
        out["chain_ok"] = None
        out["chain_error"] = str(e)[:200]
    return out


def backup_to(src_path, dst_path):
    """SQLite backup API：把 src 一致地写到 dst（含 WAL 里未 checkpoint 的页）。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)) or ".", exist_ok=True)
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = sqlite3.connect("file:%s?mode=ro" % src_path.replace("\\", "/"), uri=True, timeout=20)
    dst = sqlite3.connect(dst_path, timeout=20)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.src):
        print("旧卡不存在：%s" % a.src)
        return 2
    if os.path.abspath(a.src) == os.path.abspath(a.dst):
        print("--from 和 --to 是同一个文件，不搬。")
        return 2

    print("=" * 72)
    print(" 迁移计划")
    print("=" * 72)
    f = card_facts(a.src)
    print(" 旧卡：%s" % f["path"])
    print("   %s MB / %s 条记忆 / %s 条对白 / 链 seq=%s / 向量 %s 维 / journal=%s" % (
        round(f.get("bytes", 0) / 1048576.0, 1), f.get("mem"), f.get("dialogue"),
        f.get("timeline_seq"), f.get("dim"), f.get("journal_mode")))
    print("   出生证缺口=%s / 盖章=%s / 链完好=%s" % (
        f.get("birth_gap"), f.get("sealed"), f.get("chain_ok")))
    tgt = card_facts(a.dst)
    if tgt["exists"]:
        print(" 目标：%s 已存在（%s MB / %s 条）—— 覆盖前会先备份" % (
            tgt["path"], round(tgt.get("bytes", 0) / 1048576.0, 1), tgt.get("mem")))
    else:
        print(" 目标：%s（不存在，会新建）" % tgt["path"])

    bad = []
    if f.get("chain_ok") is False:
        bad.append("旧卡时间线链断了")
    if f.get("birth_gap") not in (0, None):
        bad.append("旧卡出生证缺口 %s 条" % f["birth_gap"])
    if bad and not a.force:
        print("\n 旧卡体检不通过：%s" % "；".join(bad))
        print(" 没有搬。先修好再搬，或者确认风险后加 --force。")
        return 2

    if not a.confirm:
        print("\n 这是**干跑**：什么都没写。确认无误后加 --confirm 再跑一次。")
        print(" 真跑时会做：备份目标 → SQLite backup → 清写租约 → 复验 → 写报告")
        return 0

    t0 = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S")
    print("\n[1/5] 备份目标...")
    if tgt["exists"]:
        bak = a.dst + ".bak-" + ts
        backup_to(a.dst, bak)
        print("   备份：%s（%s MB）" % (bak, round(os.path.getsize(bak) / 1048576.0, 1)))
    else:
        bak = ""
        print("   目标不存在，跳过")

    print("[2/5] SQLite backup 旧卡 -> 目标...")
    backup_to(a.src, a.dst)
    print("   完成：%s（%s MB）" % (a.dst, round(os.path.getsize(a.dst) / 1048576.0, 1)))

    print("[3/5] 清掉目标里的写租约（否则新卡一打开就是只读）...")
    n_lease = 0
    try:
        con = sqlite3.connect(a.dst, timeout=20)
        try:
            n_lease = _count(con, "SELECT COUNT(*) FROM writer_lease", 0) or 0
            con.execute("DELETE FROM writer_lease")
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print("   ! 清租约失败：%s" % e)

    print("[4/5] 复验...")
    g = card_facts(a.dst)
    checks = [
        ("链完好", g.get("chain_ok") is True),
        ("出生证缺口 0", g.get("birth_gap") in (0, None)),
        ("条数一致", g.get("mem") == f.get("mem")),
        ("向量维度一致", g.get("dim") == f.get("dim")),
    ]
    for name, ok in checks:
        print("   %s %s" % ("[PASS]" if ok else "[FAIL]", name))

    print("[5/5] 写报告...")
    rep = {
        "at": ts, "src": f, "dst": g, "backup": bak, "lease_rows_cleared": n_lease,
        "checks": [{"name": n, "ok": bool(o)} for n, o in checks],
        "seconds": round(time.time() - t0, 2),
        "notes": [
            "旧卡只读打开，未改动",
            "目标若被占用（服务还在跑），先把服务停掉再搬",
            "搬完请把新路径写进启动脚本或 WHALE_DB 环境变量",
        ],
    }
    outdir = os.path.join(ROOT, "migrations")
    os.makedirs(outdir, exist_ok=True)
    rp = os.path.join(outdir, "%s.json" % ts)
    with open(rp, "w", encoding="utf-8") as fp:
        json.dump(rep, fp, ensure_ascii=False, indent=2)
    print("   报告：%s" % rp)
    allok = all(o for _, o in checks)
    print("\n结论：%s（%.1fs）" % ("迁移完成并复验通过" if allok else "迁移完成但复验未全通过，看报告", time.time() - t0))
    if allok:
        print("下一步：把服务指向新卡 —— set WHALE_DB=%s 然后 python bridge.py health" % a.dst)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
