# -*- coding: utf-8 -*-
r"""验收：一张「我的卡」在一台新机器上是不是真的能跑起来。

它在临时目录里放一张示例卡（或你给的副本），起一个**独立端口**的服务，
把记忆写入、召回、上下文、内脏、账本、插件通道挨个敲一遍，最后写一份
可核对的结果（deploy-report.json / deploy-report.md）。

**不碰任何真卡**：没有 --card 就用 demo 卡复制，有 --card 也是复制到临时目录再跑。

用法：
    python tools/accept.py                        # 全自动，临时目录跑完就删
    python tools/accept.py --keep                 # 保留临时目录（排查用）
    python tools/accept.py --card D:\card.db      # 用一张卡的**副本**验收（只读源）
    python tools/accept.py --out reports          # 报告写到哪

退出码：0 = 全部通过；1 = 有 FAIL（看报告里的「未通过」段）。
"""
import argparse
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

RESULTS = []


def rec(name, ok, detail, evidence=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail,
                    "evidence": (evidence or "")[:500]})
    print("%s %-22s %s" % ("[PASS]" if ok else "[FAIL]", name, detail))
    return ok


def _model_dirs(card_path, src_card=None):
    """可能放 bge 模型的地方（和 doctor.py 的口径一致）。

    card_path 是**验收用的副本**，所以把源卡所在目录也带上 ——
    模型通常就放在源卡旁边（D:\\models），临时目录里找不到它。
    """
    env = os.environ.get("WHALE_MODELS")
    out = []
    if env:
        out.extend(x for x in env.split(os.pathsep) if x)
    out.append(os.path.join(os.path.dirname(os.path.abspath(card_path)), "models"))
    if src_card:
        out.append(os.path.join(os.path.dirname(os.path.abspath(src_card)), "models"))
    out.append(os.path.join(ROOT, "models"))
    return out


def _pick_model(src_card):
    """在源卡周围找 bge 模型（base 优先），返回绝对目录或 ""。"""
    for base in _model_dirs(src_card, src_card):
        for name in ("bge-base-zh-v1.5", "bge-small-zh-v1.5"):
            d = os.path.join(base, name)
            if os.path.exists(os.path.join(d, "config.json")):
                return d
    return ""


def free_port(start=18900):
    for p in range(start, start + 200):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("找不到空闲端口")


def http(port, path, body=None, method=None, token=None, timeout=60):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Whale-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw, time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw)
        except Exception:
            obj = raw
        return e.code, obj, time.time() - t0


def wait_health(port, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, obj, _ = http(port, "/health", timeout=5)
            if st == 200 and isinstance(obj, dict) and obj.get("ok"):
                return obj
        except Exception:
            pass
        time.sleep(0.4)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--out", default=ROOT)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=float, default=180.0)
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="wc_accept_")
    work_card = os.path.join(tmp, "card.db")
    src = a.card or os.path.join(ROOT, "demo", "demo_card.db")
    if not os.path.exists(src):
        print("找不到示例卡 %s，先跑 python tools/make_demo_card.py" % src)
        return 1
    shutil.copy2(src, work_card)
    for ext in ("-wal", "-shm"):
        if os.path.exists(work_card + ext):
            os.remove(work_card + ext)
    # 副本上清掉「写租约」：原卡可能正被另一台机器/另一个进程占着，
    # 不清的话副本会秒变只读（409 readonly_lease_held），验收测不下去。
    try:
        with sqlite3.connect(work_card, timeout=10) as con:
            have = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "writer_lease" in have:
                con.execute("DELETE FROM writer_lease")
                con.commit()
    except Exception as e:
        print("（清写租约失败，忽略：%s）" % e)
    port = a.port or free_port()
    print("=" * 70)
    print(" 验收：卡=%s（副本） 端口=%d" % (src, port))
    print("=" * 70)

    env = dict(os.environ)
    # 模型：embed_np 认 WHALE_EMBED_MODEL（绝对路径或 base/small）。
    # 副本在临时目录，模型通常留在源卡旁边，所以这里显式指过去；
    # 找不到就留空 —— 语义层自动降级，验收报告里会写清楚。
    _mdl = _pick_model(src)
    if _mdl:
        env["WHALE_EMBED_MODEL"] = _mdl
    env.update({
        "WHALE_DB": work_card,
        # 锚也隔离到临时目录：否则并行/上一次的锚文件会让「全新卡」被判成被篡改
        "WHALE_ANCHOR_LOCAL": os.path.join(tmp, "anchor.json"),
        "WHALE_ANCHOR": os.path.join(tmp, "anchor_ext.json"),
        "WHALE_HOME": tmp,
        "WHALE_REQUIRE_TOKEN": "0",
        "WHALE_PORT": str(port),
        "WHALE_MAINT_SEC": "0",
        "WHALE_INBOX_PICK_SEC": "0",
        "WHALE_WORLD_TICK_SEC": "0",
        "WHALE_CHECKPOINT_SEC": "0",
        "WHALE_VISCERA_SAMPLE_SEC": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    log = open(os.path.join(tmp, "server.log"), "wb")
    t_boot = time.time()
    proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(ROOT, "mind_server.py"),
                             "--port", str(port), "--db", work_card],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    h = None
    try:
        h = wait_health(port, a.timeout)
        boot_s = round(time.time() - t_boot, 1)
        if not rec("服务启动", h is not None,
                   "%.1fs 后 /health 就绪" % boot_s if h else "%.1fs 内没起来" % boot_s):
            raise SystemExit(1)

        # 1) 身份与账本
        rec("健康与身份", bool(h.get("ok")) and bool(h.get("sealed")),
            "ok=%s sealed=%s day=%s db=%s" % (h.get("ok"), h.get("sealed"),
                                              (h.get("timeline") or {}).get("day"), h.get("db")),
            json.dumps({"version": h.get("version"), "mem": h.get("memories")}, ensure_ascii=False))
        tl = h.get("timeline") or {}
        rec("时间线链", bool(tl.get("chain_ok")), "chain_ok=%s head=%s" % (tl.get("chain_ok"), tl.get("head")))
        rec("身份锚", (tl.get("anchor_verdict") or "") == "ok", tl.get("anchor_reason") or "")
        rg = ((h.get("reconcile") or {}).get("checks") or {})
        rec("出生证对账", rg.get("birth_gap") == 0, "birth_gap=%s" % rg.get("birth_gap"))
        sem = h.get("semantic") or {}
        if sem.get("ready"):
            rec("语义编码器", True, "model_dim=%s dims=%s ratio=%s" % (
                sem.get("model_dim"), sem.get("emb_dims"), sem.get("usable_ratio")))
        else:
            _has_model = any(os.path.isdir(p) for p in _model_dirs(work_card, src))
            rec("语义编码器", not _has_model,
                "没加载模型（%s）；%s" % (
                    sem.get("note") or sem.get("warn") or "health 里没有 semantic 段",
                    "本机有 models 目录却没加载 -> 要查" if _has_model
                    else "本机没有 models 目录，新机器上是正常现象，装好 bge 后重跑本脚本"))
        mem0 = h.get("memories") or 0

        # 2) 写入：一次 report 应该同时写他说的、我说的、对白、突触
        _tabs = ("mem", "dialogue", "inbox", "mem_birth", "synapse")

        def _counts():
            out = {}
            with sqlite3.connect(work_card, timeout=10) as con:
                have = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for t in _tabs:
                    out[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] \
                        if t in have else 0
            return out

        before = _counts()
        st, rep, dt = http(port, "/report", {"user_message": "验收：我最喜欢吃什么？",
                                             "reply": "白米饭。验收这句是工具自己写的，不是他说的。"})
        _rp = (rep or {}).get("report") if isinstance(rep, dict) else None
        if not isinstance(_rp, dict):
            _rp = rep if isinstance(rep, dict) else {}
        mid = _rp.get("mem_id")
        rid = _rp.get("reply_mem_id")
        after = _counts()
        delta = {k: after[k] - before[k] for k in before}
        ok_w = st == 200 and mid and rid and delta["mem"] >= 2 and delta["dialogue"] >= 2 and delta["mem_birth"] >= 2
        rec("记忆写入", bool(ok_w), "mem_id=%s reply_mem_id=%s %s 耗时%.2fs" % (mid, rid, delta, dt),
            json.dumps(rep, ensure_ascii=False)[:300])

        # 3) 召回：刚写进去的东西应该能找回来（同一张卡、同一个进程）
        st, rc, dt = http(port, "/recall", {"query": "我最喜欢吃什么？", "top_k": 5})
        items = (rc or {}).get("hits") if isinstance(rc, dict) else None
        items = items or []
        blob = json.dumps(rc, ensure_ascii=False)
        hit = ("白米饭" in blob) or (mid and (str(mid) in blob))
        rec("记忆召回", st == 200 and bool(items) and hit,
            "状态=%s 命中=%s 条数=%s 耗时%.2fs" % (st, hit, len(items), dt), blob[:300])

        # 3b) 突触：召回会做联想扩散，边是这时候长出来的
        after2 = _counts()
        rec("突触联动", after2["synapse"] > 0,
            "synapse %d→%d（recall 后；老卡上边已经长满，不涨是正常的）" % (
                before["synapse"], after2["synapse"]))

        # 4) 上下文装配（只算不写）
        st, cp, dt = http(port, "/ctx_preview", {"query": "今天中午吃什么"})
        rec("上下文装配", st == 200 and isinstance(cp, dict) and cp.get("ok") is not False,
            "状态=%s 耗时%.2fs" % (st, dt), json.dumps(cp, ensure_ascii=False)[:300])

        # 5) 内脏/情绪与状态快照（接口都套了一层 {ok, <name>: ...}）
        st, vc, dt = http(port, "/viscera")
        _v = (vc or {}).get("viscera") if isinstance(vc, dict) else None
        pad = (_v or {}).get("pad") if isinstance(_v, dict) else None
        hs = (_v or {}).get("h") if isinstance(_v, dict) else None
        rec("情绪内脏", st == 200 and isinstance(pad, dict) and bool(hs),
            "激素 %s 种 / pad keys=%s 耗时%.2fs" % (len(hs or {}), sorted((pad or {}).keys()), dt))
        st, stt, _ = http(port, "/status")
        _st = (stt or {}).get("status") if isinstance(stt, dict) else None
        syn = ((_st or {}).get("synapse") or {}) if isinstance(_st, dict) else {}
        rec("状态总览", st == 200 and bool(syn),
            "synapse edges=%s coverage=%s" % (syn.get("edges"), syn.get("coverage")))

        # 6) 通道：MCP 工具（真机上有就查，没有就说明是新机器）
        br = (h.get("bridge") or {})
        rec("MCP 工具", True, "path=%s tools=%s（新机器没装插件时为空，属正常）" % (
            "有" if br.get("path") else "无", br.get("tools")))

        # 7) 重启友好：能干净退出
        tok_path = os.path.join(os.path.dirname(work_card), "qq_state", "api_token.txt")
        token = open(tok_path, encoding="utf-8").read().strip() if os.path.exists(tok_path) else None
        st, sd, _ = http(port, "/shutdown", {}, token=token)
        rec("干净退出", st == 200, "状态=%s（token=%s）" % (st, "有" if token else "无"))
    finally:
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        try:
            log.close()
        except Exception:
            pass

    passed = [r for r in RESULTS if r["ok"]]
    failed = [r for r in RESULTS if not r["ok"]]
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": not failed,
        "pass": len(passed), "fail": len(failed),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "card_source": src,
        "card_bytes": os.path.getsize(src),
        "port": port,
        "host_card_db": h.get("db") if h else None,
        "checks": RESULTS,
    }
    os.makedirs(a.out, exist_ok=True)
    jp = os.path.join(a.out, "deploy-report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    mp = os.path.join(a.out, "deploy-report.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("# 部署验收报告\n\n")
        f.write("- 生成时间：%s\n- python：%s（%s）\n- 卡：%s → 副本验收\n- 端口：%d\n- 结论：**%s**（%d 通过 / %d 失败）\n\n" % (
            summary["generated_at"], summary["python"], summary["platform"],
            src, port, "通过" if summary["ok"] else "未通过", len(passed), len(failed)))
        f.write("| 检查项 | 结果 | 详情 |\n|---|---|---|\n")
        for r in RESULTS:
            f.write("| %s | %s | %s |\n" % (r["check"], "PASS" if r["ok"] else "FAIL",
                                            r["detail"].replace("|", "/")))
        if failed:
            f.write("\n## 未通过\n\n")
            for r in failed:
                f.write("- **%s**：%s\n  ```\n  %s\n  ```\n" % (r["check"], r["detail"], r["evidence"]))
        f.write("\n## 复现\n\n```\npython tools/doctor.py\npython tools/first_run.py\npython tools/accept.py\n```\n")

    print("-" * 70)
    print(" 结论：%s（%d 通过 / %d 失败）" % ("通过" if summary["ok"] else "未通过",
                                              len(passed), len(failed)))
    print(" 报告：%s" % jp)
    print("        %s" % mp)
    if a.keep:
        print(" 临时目录：%s（服务日志 server.log 在里面）" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 70)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
