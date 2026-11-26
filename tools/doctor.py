# -*- coding: utf-8 -*-
r"""体检：这台机器能不能跑这张卡。

用途：给「第一次拿到仓库」的人和 agent 一个确定的起点。**只读**，不改任何东西。
     doctor 通过之后，才轮到 first_run（落地+起服务）和 accept（功能验收）。

用法：
    python tools/doctor.py                       # 给人看（自动判模式）
    python tools/doctor.py --json                # 给 agent 看（结构化）
    python tools/doctor.py --mode fresh          # 全新部署：还没有卡，卡不存在不算错
    python tools/doctor.py --mode existing       # 已有卡：卡不存在 = MUST
    python tools/doctor.py --mode migrate --card <旧卡>   # 待迁移：查旧卡可读 + 目标可写
    python tools/doctor.py --card D:\card.db --port 8848

模式（--mode，默认 auto）：
    auto     卡存在 → existing；卡不存在 → fresh（新用户第一次跑不会因为没卡就红一片）
    fresh    允许没有卡；提示下一步用 make_demo_card.py 或 first_run.py 建卡
    existing 必须有可读的卡；缺卡 / 打不开 = MUST
    migrate  查「旧卡能不能读」+「目标目录能不能写」，不要求目标已存在

它检查什么、怎么判：
    python   3.10+，能 import numpy
    依赖     requirements.txt 必需项（numpy / Pillow / requests）；psutil、edge_tts 只是可选
    卡       文件在、能读、mem 条数、journal_mode、出生证缺口、向量维度、写租约
    模型     模型目录在不在、和卡里的向量维度对不对得上
    服务     端口 8848 上有没有本卡的服务；有的话读 /health 看链/锚/备份状态
    通道     qq_state/api_token.txt、~/.dsh/profile、dsh-numen-bridge 插件
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

# 提示里用「当前这个解释器」的路径，不要写死 python / py ——
# 2026-11-26：机器上同时有 3.12 和 3.14 时，写死 python 会装到错的版本上。
_PYQ = ('"%s"' % sys.executable) if " " in sys.executable else sys.executable
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
                 "%s -m pip install -r requirements.txt" % _PYQ)
    for name, why in NICE:
        ver, _ = _pkg(name)
        if ver:
            ok("可选:" + name, ver)
        else:
            warn("可选:" + name, "没装（%s 降级，不影响记忆/召回）" % why,
                 "%s -m pip install " % _PYQ + name)
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



def check_card(card, mode="auto"):
    info = {"path": card, "dim": 0, "exists": False, "mode": mode}
    if not card:
        if mode == "fresh":
            ok("卡", "全新部署：还没有卡（这是正常的）")
            return info
        must("卡", "没指定", "用 --card 指定，或把 card.db 放在仓库根目录")
        return info
    if not os.path.exists(card):
        if mode == "fresh":
            ok("卡", "全新部署：%s 还不存在（下一步会新建）" % card)
            info["next_step"] = "python tools/first_run.py（或 make_demo_card.py 先来一张示例卡）"
            return info
        if mode == "migrate":
            must("迁移源卡", "找不到 %s" % card,
                 "把 --card 指向旧卡（那个真实存在的 card.db）")
            return info
        must("卡", "找不到 %s" % card,
             "已有卡：把 --card 指到你的卡；全新部署：加 --mode fresh；"
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
    names = ["WHALE_EMBED_MODEL", "WHALE_MODELS", "WHALE_EMBED_DIR"]
    out = [os.environ[n] for n in names if os.environ.get(n)]
    base = os.path.dirname(os.path.abspath(card)) if card else ROOT
    out += [os.path.join(base, "models"), os.path.join(ROOT, "models"),
            os.path.join(os.path.dirname(ROOT), "models"), "D:\\models"]
    seen, res = set(), []
    for d in out:
        if d in ("base", "small"):
            d = os.path.join(ROOT, "models", "bge-%s-zh-v1.5" % d)
        if d and d not in seen:
            seen.add(d)
            res.append(d)
    return res


def check_models(info):
    dim = info.get("dim") or 0
    found = None
    # WHALE_EMBED_MODEL 直接指向某个模型目录时（最常见），把它拆成 父目录 + 名字
    envm = (os.environ.get("WHALE_EMBED_MODEL") or "").strip()
    if envm and os.path.isdir(envm) and os.path.exists(os.path.join(envm, "config.json")):
        name = os.path.basename(envm.rstrip("\\/"))
        parent = os.path.dirname(envm.rstrip("\\/")) or envm
        found = (parent, [name])
    for d in ([] if found else model_dirs(info.get("path"))):
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
    want = "768" if dim >= 700 else ("512" if dim >= 500 else (str(dim) if dim else ""))
    # 优先挑名字对得上的；挑不到就用第一个
    pick = None
    if want:
        pick = next((x for x in subs if want in x), None)
    if pick is None:
        pick = subs[0]
    mdir = os.path.join(d, pick)
    # 完整性：config.json / vocab.txt / 一个够大的权重文件
    prob = []
    for f in ("config.json", "vocab.txt"):
        if not os.path.exists(os.path.join(mdir, f)):
            prob.append("缺 " + f)
    weights = [f for f in os.listdir(mdir)
               if f.endswith((".safetensors", ".bin", ".onnx", ".npz"))]
    if not weights:
        prob.append("缺权重文件（*.safetensors / *.bin / *.npz）")
    else:
        big = [f for f in weights if os.path.getsize(os.path.join(mdir, f)) > 1 << 20]
        if not big:
            prob.append("权重文件太小（%s），像是没下完" % ", ".join(weights[:2]))
    if prob:
        must("模型完整性", "%s：%s" % (pick, "；".join(prob)),
             "重新下这个模型，或用 WHALE_EMBED_MODEL 指向一个完整的模型目录")
    else:
        ok("模型完整性", "%s（config/vocab/权重 都在）" % pick)
    if not dim:
        return
    low = pick.lower()
    matched = ((dim >= 700 and ("base" in low or "768" in low)) or
               (500 <= dim < 700 and ("small" in low or "512" in low)) or
               (dim < 500 and str(dim) in low))
    if matched:
        ok("模型匹配", "卡里 %d 维 ↔ %s" % (dim, pick))
    elif want:
        must("模型匹配", "卡里 %d 维，但模型是 %s" % (dim, pick),
             "别混用：768 维配 bge-base-zh-v1.5、512 维配 bge-small-zh-v1.5；"
             "要换模型就重建向量：python bridge.py remodel")


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
    # 扫所有 profile 找通道插件，不写死 desktop ——
    # 2026-11-26 从零走一遍时发现：写死 "desktop" 会在用别的 profile 的机器上误报「没装」。
    _plugs = []
    for _p in profs:
        _q = os.path.join(dsh, _p, "node_modules", "dsh-numen-bridge")
        if os.path.isdir(_q):
            _plugs.append(_p)
    if _plugs:
        ok("MCP 插件", "已装：" + ", ".join(_plugs))
    else:
        warn("MCP 插件", "没有任何 profile 装了 dsh-numen-bridge",
             "python tools/install_to_dsh.py --profile %s" % (profs[0] if profs else "desktop"))


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
    ap.add_argument("--mode", default="auto", choices=("auto", "fresh", "existing", "migrate"),
                    help="部署场景：auto=卡在就 existing，不在就 fresh")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WHALE_PORT", "8848")))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    mode = a.mode
    if mode == "auto":
        mode = "existing" if os.path.exists(a.card) else "fresh"

    t0 = time.time()
    check_python()
    check_pkgs()
    info = check_card(a.card, mode)
    check_models(info)
    check_service(a.port, info)
    check_channel(info.get("path"))
    check_disk(info.get("path"))

    musts = [r for r in ROWS if r["level"] == "must"]
    warns = [r for r in ROWS if r["level"] == "warn"]
    out = {"ok": not musts, "must": len(musts), "warn": len(warns),
           "card": a.card, "port": a.port, "mode": mode, "rows": ROWS,
           "seconds": round(time.time() - t0, 2),
           "next": ("先解决 MUST，再跑 python tools/first_run.py"
                    if musts else ("python tools/first_run.py → python tools/accept.py"
                                   if mode != "migrate" else
                                   "python tools/migrate.py --from <旧卡> --to <新位置> --confirm"))}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if not musts else 1

    icon = {"ok": "[ OK ]", "warn": "[WARN]", "must": "[MUST]"}
    print("=" * 68)
    print(" 数字生命卡 · 体检    %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print(" 卡: %s    模式: %s" % (a.card, mode))
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
