# -*- coding: utf-8 -*-
r"""体检：这台机器能不能跑这张卡。

用途：给「第一次拿到仓库」的人和 agent 一个确定的起点。**只读**，不改任何东西。
     doctor 通过之后，才轮到 first_run（落地+起服务）和 accept（功能验收）。

用法：
    python tools/doctor.py                       # 给人看
    python tools/doctor.py --json                # 给 agent 看（结构化）
    python tools/doctor.py --card D:\card.db --port 8848

它检查什么、怎么判：
    python   3.10+，能 import numpy
    依赖     requirements.txt 必需项（numpy / Pillow / requests）；psutil、edge_tts 只是可选
    卡       文件在、能读、mem 条数、journal_mode、出生证缺口、向量维度、写租约
    模型     模型目录在不在、和卡里的向量维度对不对得上
    服务     端口 8848 上有没有本卡的服务；有的话读 /health 看链/锚/备份状态
    通道     qq_state/api_token.txt、~/.dsh/profile、dsh-whale-bridge 插件
    磁盘     剩余空间
    DSH      在不在（不在也能跑卡，只是接不上 agent）

退出码：0 = 可以跑；1 = 有 MUST 级问题（看最后那段）。
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NEED = [("numpy", "向量与记忆计算"), ("PIL", "图片/媒体记忆"), ("requests", "少量联网脚本")]
NICE = [("psutil", "控制台 CPU/内存曲线"), ("edge_tts", "语音条")]

ROWS = []


def add(level, key, msg, fix=""):
    ROWS.append({"level": level, "key": key, "msg": msg, "fix": fix})


def ok(k, m):
    add("ok", k, m)


def warn(k, m, fix=""):
    add("warn", k, m, fix)


def must(k, m, fix=""):
    add("must", k, m, fix)


def _pkg(name):
    try:
        m = __import__(name)
    except Exception as e:
        return None, str(e)
    v = getattr(m, "__version__", None)
    if not v:
        try:
            from importlib import metadata
            v = metadata.version("Pillow" if name == "PIL" else name)
        except Exception:
            v = "?"
    return v, ""


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        ok("python", "%d.%d.%d  %s" % (v[0], v[1], v[2], sys.executable))
    else:
        must("python", "版本 %d.%d，需要 3.10+" % (v[0], v[1]),
             "装 3.10+ 之后用它跑 python tools/doctor.py")


def check_pkgs():
    for name, why in NEED:
        ver, err = _pkg(name)
        if ver:
            ok("依赖:" + name, "%s（%s）" % (ver, why))
        else:
            must("依赖:" + name, "缺（%s）：%s" % (why, err[:100]),
                 "python -m pip install -r requirements.txt")
    for name, why in NICE:
        ver, _ = _pkg(name)
        if ver:
            ok("可选:" + name, ver)
        else:
            warn("可选:" + name, "没装（%s 降级，不影响记忆/召回）" % why,
                 "python -m pip install " + name)
    # structure.py 要加载随仓库分发的 _structure_orig.pyc（编译自 3.12）
    try:
        sys.path.insert(0, ROOT)
        import structure as _st
        _probe = _st.clauses("今天中午吃了白米饭，天气不错。")
        if _probe:
            ok("结构抽取", "可用（clauses/expand_query 正常）")
        else:
            warn("结构抽取", "加载了但没切出句子", "看 structure.py 的 pyc 版本")
    except Exception as e:
        warn("结构抽取", "不可用（%s）—— 记忆照常写入，只是 subj/verb/obj 留空" % str(e)[:80],
             "随仓库的 _structure_orig.pyc 是 Python 3.12 编的；用 3.12 跑服务即可启用，"
             "3.13 下只是降级，不影响召回主链路")



def check_card(card):
    info = {"path": card, "dim": 0, "exists": False}
    if not card:
        must("卡", "没指定", "用 --card 指定，或把 card.db 放在仓库根目录")
        return info
    if not os.path.exists(card):
        must("卡", "找不到 %s" % card,
             "全新部署：python tools/make_demo_card.py 先生成 demo 卡；"
             "迁移：python tools/migrate.py --from <旧卡> --to <新位置> --confirm")
        return info
    info["exists"] = True
    ok("卡文件", "%s（%.1f MB）" % (card, os.path.getsize(card) / 1048576.0))
    try:
        con = sqlite3.connect("file:%s?mode=ro" % card.replace("\\", "/"), uri=True, timeout=8)
    except Exception as e:
        must("卡可读", "打不开：%s" % e, "检查路径；确认没有别的进程把它锁死")
        return info
    try:
        jm = con.execute("PRAGMA journal_mode").fetchone()[0]
        mem = con.execute("SELECT COUNT(*) FROM mem").fetchone()[0]
        ok("卡内容", "mem %d 条，journal_mode=%s" % (mem, jm))
        info["mem"] = mem
        gap = con.execute("SELECT COUNT(*) FROM mem m LEFT JOIN mem_birth b ON b.mid=m.id "
                          "WHERE b.mid IS NULL").fetchone()[0]
        info["birth_gap"] = gap
        if gap == 0:
            ok("出生证", "无缺口")
        else:
            warn("出生证", "%d 条缺出生证" % gap,
                 "看明细：SELECT id FROM mem WHERE id NOT IN (SELECT mid FROM mem_birth)")
        row = con.execute("SELECT LENGTH(emb) FROM mem WHERE emb IS NOT NULL AND LENGTH(emb)>0 LIMIT 1").fetchone()
        if row and row[0]:
            n = int(row[0])
            info["dim"] = n // 4 if n > 512 else n
            ok("向量维度", "%d 维" % info["dim"])
        lease = con.execute("SELECT holder FROM writer_lease LIMIT 1").fetchone()
        info["lease"] = (lease[0] if lease else "") or ""
    except Exception as e:
        warn("卡内容", "查询失败：%s" % e)
    finally:
        con.close()
    return info


def model_dirs(card):
    names = ["WHALE_MODELS", "WHALE_EMBED_DIR"]
    out = [os.environ[n] for n in names if os.environ.get(n)]
    base = os.path.dirname(os.path.abspath(card)) if card else ROOT
    out += [os.path.join(base, "models"), os.path.join(ROOT, "models"),
            os.path.join(os.path.dirname(ROOT), "models"), "D:\\models"]
    seen, res = set(), []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            res.append(d)
    return res


def check_models(info):
    dim = info.get("dim") or 0
    found = None
    for d in model_dirs(info.get("path")):
        if os.path.isdir(d):
            subs = [s for s in os.listdir(d) if os.path.isdir(os.path.join(d, s))]
            if subs:
                found = (d, subs)
                break
    if not found:
        warn("模型", "没找到模型目录（找过：%s）" % " | ".join(model_dirs(info.get("path"))[:4]),
             "放 bge 模型进去（bge-base-zh-v1.5=768 维 / bge-small-zh-v1.5=512 维），"
             "或用环境变量 WHALE_MODELS 指定。没有模型也能跑，召回会退化成字面匹配")
        return
    d, subs = found
    ok("模型目录", "%s → %s" % (d, ", ".join(subs[:4])))
    if not dim:
        return
    want = "768" if dim >= 700 else ("512" if dim >= 500 else str(dim))
    hit = [s for s in subs if want in s or ("base" in s.lower() and dim >= 700)]
    if hit:
        ok("模型匹配", "卡里 %d 维 ↔ %s" % (dim, hit[0]))
    else:
        warn("模型匹配", "卡里 %d 维，目录里没有明显对得上的候选：%s" % (dim, ", ".join(subs[:4])),
             "确认 /health 的 emb.model 与目录名一致；换模型要重建向量：python bridge.py remodel")


def check_service(port, info):
    s = socket.socket()
    s.settimeout(2)
    alive = False
    try:
        s.connect(("127.0.0.1", port))
        alive = True
    except Exception:
        pass
    finally:
        s.close()
    if not alive:
        ok("服务:%d" % port, "没在跑（正常，first_run 会拉起来）")
        return None
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=8) as r:
            h = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        warn("服务:%d" % port, "端口被占，但不是本卡的服务：%s" % e,
             "换端口 --port 8849，或先关掉那个进程")
        return None
    ok("服务:%d" % port, "跑着：ok=%s mem=%s db=%s" % (h.get("ok"), h.get("memories"), h.get("db")))
    tl = h.get("timeline") or {}
    if tl.get("chain_ok"):
        ok("链", "chain_ok=True，head=%s，第 %s 天" % (tl.get("head"), tl.get("day")))
    else:
        warn("链", "chain_ok=%s" % tl.get("chain_ok"), "先备份再修：python snapshot.py make")
    if (tl.get("anchor_verdict") or "") == "ok":
        ok("锚", tl.get("anchor_reason") or "一致")
    else:
        warn("锚", "verdict=%s：%s" % (tl.get("anchor_verdict"), tl.get("anchor_reason")),
             "别急着搬：先看 docs/MOVE-CHECKLIST.md")
    rec = h.get("reconcile") or {}
    if isinstance(rec, dict) and rec.get("checks", {}).get("birth_gap") is not None:
        g = rec["checks"]["birth_gap"]
        (ok if g == 0 else warn)("账本", "birth_gap=%s" % g)
    sem = h.get("semantic") or {}
    if sem.get("ready"):
        ok("语义", "model_dim=%s dims=%s ratio=%s" % (
            sem.get("model_dim"), sem.get("emb_dims"), sem.get("usable_ratio")))
    else:
        warn("语义", "semantic.ready=False（召回退化）", "看 /health 的 semantic.warn")
    bk = h.get("backup") or {}
    if bk.get("ok"):
        ok("备份", "最近一份 %.0f 小时前" % (bk.get("newest_age_h") or 0))
    else:
        warn("备份", "没通过：%s" % (bk.get("problems") or bk.get("red")),
             "python snapshot.py make")
    return h


def check_channel(card):
    st = os.path.join(os.path.dirname(os.path.abspath(card)) if card else ROOT, "qq_state")
    tok = os.path.join(st, "api_token.txt")
    if os.path.exists(tok):
        ok("API token", tok)
    else:
        warn("API token", "没有 %s（服务第一次起来会生成）" % tok,
             "起服务后再看：python bridge.py health")
    dsh = os.path.join(os.path.expanduser("~"), ".dsh", "profiles")
    if not os.path.isdir(dsh):
        warn("DSH", "没装 DSH（不影响卡本身，只是接不上 agent）",
             "接法见 docs/接入指南-V7.md；装好后 python tools/install_to_dsh.py")
        return
    profs = [d for d in os.listdir(dsh) if os.path.isdir(os.path.join(dsh, d))]
    ok("DSH profile", ", ".join(profs[:6]))
    plug = os.path.join(dsh, "desktop", "node_modules", "dsh-whale-bridge")
    if os.path.isdir(plug):
        ok("MCP 插件", plug)
    else:
        warn("MCP 插件", "desktop profile 里没装 dsh-whale-bridge",
             "python tools/install_to_dsh.py --profile desktop")


def check_disk(card):
    base = os.path.dirname(os.path.abspath(card)) if card else ROOT
    try:
        _t, _u, free = shutil.disk_usage(base)
    except Exception as e:
        warn("磁盘", "查不了：%s" % e)
        return
    if free < 1024 ** 3:
        must("磁盘", "只剩 %.0f MB" % (free / 1048576.0), "卡会长大（含快照），先腾出 1GB+")
    else:
        ok("磁盘", "剩余 %.1f GB（%s）" % (free / 1073741824.0, base))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default=os.environ.get("WHALE_DB", os.path.join(ROOT, "card.db")))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WHALE_PORT", "8848")))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    check_python()
    check_pkgs()
    info = check_card(a.card)
    check_models(info)
    check_service(a.port, info)
    check_channel(info.get("path"))
    check_disk(info.get("path"))

    musts = [r for r in ROWS if r["level"] == "must"]
    warns = [r for r in ROWS if r["level"] == "warn"]
    out = {"ok": not musts, "must": len(musts), "warn": len(warns),
           "card": a.card, "port": a.port, "rows": ROWS,
           "seconds": round(time.time() - t0, 2),
           "next": ("先解决 MUST，再跑 python tools/first_run.py"
                    if musts else "python tools/first_run.py → python tools/accept.py")}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if not musts else 1

    icon = {"ok": "[ OK ]", "warn": "[WARN]", "must": "[MUST]"}
    print("=" * 68)
    print(" 数字生命卡 · 体检    %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print(" 卡: %s" % a.card)
    print("=" * 68)
    for r in ROWS:
        print("%s %-16s %s" % (icon[r["level"]], r["key"], r["msg"]))
        if r["fix"]:
            print("        → %s" % r["fix"])
    print("-" * 68)
    print(" MUST %d / WARN %d（%.2fs）" % (len(musts), len(warns), out["seconds"]))
    print(" 下一步: %s" % out["next"])
    print("=" * 68)
    return 0 if not musts else 1


if __name__ == "__main__":
    sys.exit(main())
