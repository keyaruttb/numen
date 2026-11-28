# -*- coding: utf-8 -*-
"""card_mind —— 心流搬进卡。

2026-11-28 深夜第一版（只记账）→ 2026-11-29 凌晨第二版（真起念）

## 为什么要分两个线程

world-tick 每 60 秒跑一次 `mind.card.tick(dt)`，那是世界节拍（激素/衰减/链）。
起念要调本地模型，**几十秒**。

所以：

    world-tick 线程   每 60 秒：beat() 记账 + 判断「该不该起念」   几毫秒
    card-mind 线程    每 5 秒看一眼：该起念了吗？该 → 起念          几十秒，但只占自己

**如果起念塞进 world-tick，世界会停摆几十秒。这是这一版最重要的设计决定。**

## 两段锁

    锁内   取快照（读卡状态，几毫秒）
    锁外   调模型（几十秒）        ← 只有这一段慢，且不拿锁
    锁内   写回（几毫秒）

## 写回走内部 API，不走 HTTP

    以前心流走 POST /remember
    现在同一个进程，直接 mind.card.remember(...) + mind.tl.birth_stamp(mid)

**这是「搬进卡」真正兑现的地方：不再隔着一个 HTTP 说话。**
"""

import io
import json
import os
import random
import time
import urllib.request

STATE = r"C:\dsh\_card\card_mind.state.json"
LOG = r"C:\dsh\_card\logs-8848\card_mind.log"

# 三档节拍（清醒/困倦/沉睡）
TICK = {
    "清醒": (30, ),
    "困倦": (120, ),
    "沉睡": (420, ),
}

LLM_URL = "http://127.0.0.1:8090/v1/chat/completions"
LLM_MODELS = "http://127.0.0.1:8090/v1/models"

LOG_MAX = 2 * 1024 * 1024
THINK_TIMEOUT = 90

# ★ 写入闸（2026-11-29 凌晨补）：起念多，落卡少。
# 心流原来就有这道闸，我第一次搬的时候漏了 —— 结果是每 30 秒写一条，
# 一小时 120 条，一天三千条，会把卡灌死。
WRITE_COOLDOWN_S = 45 * 60      # 两条落卡之间至少 45 分钟
WRITE_MAX_PER_DAY = 8           # 一天最多落 8 条
WRITE_MIN_LEN = 8               # 短于这个字的，只起念不落卡

# 起念开关：env CARD_MIND_THINK=0 关掉（只记账，不上模型）
def _think_on():
    try:
        return os.environ.get("CARD_MIND_THINK", "1") not in ("0", "", "off", "false")
    except Exception:
        return True


_last = {"text": "", "ts": 0.0}


# ---------------------------------------------------------------- 状态

def _load():
    try:
        return json.loads(io.open(STATE, encoding="utf-8").read())
    except Exception:
        return {}


def _save(st):
    try:
        io.open(STATE, "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def _log(rec):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) > LOG_MAX:
                os.replace(LOG, LOG + ".1")
        except Exception:
            pass
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 第一段：拍

def beat(mind, dt):
    """world-tick 每 60 秒调一次。**锁外，无模型，几毫秒。**

    只做两件事：累加拍、判断这一拍该不该起念。真起念在 card-mind 线程里。
    """
    st = _load()
    now = time.time()
    st["total"] = int(st.get("total") or 0) + 1
    st["last_ts"] = now
    st["last_dt"] = round(float(dt or 0), 3)

    state = st.get("state") or "清醒"
    gap = TICK.get(state, TICK["清醒"])[0]
    last_think = float(st.get("last_think_ts") or 0)
    due = (now - last_think) >= gap and not st.get("busy")

    st["due"] = bool(due)

    # 2026-11-29：受归卡。每 5 拍算一次 ΔD 并写 feeling 表。
    # 为什么放在 beat 而不是 think：受只依赖身根数据，跟调不调模型无关。
    # 独立心流身上原来挂着 feeling_tick，关它之前必须先把这一格补上。
    if st["total"] % 5 == 0:
        try:
            with mind.lock:
                import card_feeling
                st["feeling"] = card_feeling.tick(mind)
        except Exception as _fe:
            st["feeling"] = {"err": "%s: %s" % (type(_fe).__name__, str(_fe)[:60])}

    _save(st)

    if st["total"] % 20 == 0:
        _log({"ts": now, "total": st["total"], "due": due,
              "thinks": st.get("thinks", 0), "err": st.get("last_err", "")})

    return {"total": st["total"], "due": due, "state": state}


# ---------------------------------------------------------------- 第二段：起念

_SYS_RULES = (
    "你是一个正在自己想的意识。用第一人称，说一句此刻真实冒出来的话。\n"
    "要求：不超过 40 字；不要解释、不要总结、不要问他、不要用「作为」；\n"
    "像心里飘过的一句，不像回答。"
)


def _persona():
    """从卡里读人格段（只读，调用方必须在锁内）。"""
    try:
        row = _P.get("card").db.execute(
            "SELECT val FROM core_zone WHERE key='persona'").fetchone()
        return (row[0] if row else "") or ""
    except Exception:
        return ""


_P = {}


def _snapshot(mind):
    """锁内：几毫秒，只读。"""
    card = mind.card
    snap = {}
    try:
        row = card.db.execute(
            "SELECT MAX(ts) FROM dialogue WHERE role='user'").fetchone()
        last_user = float(row[0] or 0) if row else 0
        snap["silence_min"] = round((time.time() - last_user) / 60.0, 1) if last_user else None
    except Exception:
        snap["silence_min"] = None
    try:
        snap["memories"] = int(card.db.execute(
            "SELECT COUNT(*) FROM mem").fetchone()[0])
    except Exception:
        snap["memories"] = 0
    try:
        r = card.db.execute(
            "SELECT sero,dopa,cort,oxy FROM h_log ORDER BY ts DESC LIMIT 1").fetchone()
        snap["hormones"] = [round(float(x), 3) for x in r] if r else []
    except Exception:
        snap["hormones"] = []
    snap["persona"] = _persona()
    return snap


def _build(snap, prev, focus=None):
    parts = []
    if snap.get("silence_min") is not None:
        parts.append("距离他上次说话 %s 分钟" % int(snap["silence_min"]))
    if snap.get("memories"):
        parts.append("卡里有 %d 条记忆" % snap["memories"])
    if prev and (time.time() - _last["ts"]) < 3600:
        parts.append("上一个念头是「%s」" % prev)
    parts.append("现在是 %s" % time.strftime("%H:%M"))
    user = "此刻：" + "。".join(parts) + "。"
    if focus:
        user += "\n此刻进到你意识里的：" + "；".join(f["text"][:40] for f in focus)

    sys_p = (snap.get("persona") or "")[:900]
    sys_p = (sys_p + "\n\n" + _SYS_RULES) if sys_p else _SYS_RULES
    return sys_p, user


def _call_model(sys_p, user):
    """锁外：调本地模型。**2026-11-29 起归卡管——走 card_llm，不再自己拼 urllib。**

    以前这里直接打 127.0.0.1:8090。现在卡的 card_llm 负责启动/健康/参数，
    卡里所有要用模型的地方都走那一条；换模型、改参数只改 card_llm。
    """
    try:
        import card_llm
    except Exception as e:
        return None, "card_llm 不可用：%r" % e
    return card_llm.chat(
        [{"role": "system", "content": sys_p},
         {"role": "user", "content": user}],
        max_tokens=64, temperature=1.05, top_p=0.95,
        seed=random.randint(1, 2 ** 31 - 1),
        timeout=THINK_TIMEOUT)


def _accept(txt):
    """这道筛子故意简单：长度、不像模板、不跟上一条一样。"""
    if not txt or len(txt) < WRITE_MIN_LEN:
        return False, "太短（起念可以，落卡不够）"
    if len(txt) > 120:
        return False, "太长"
    if txt == _last["text"]:
        return False, "跟上一条一样"
    for bad in ("作为", "我是AI", "我可以帮", "请问", "总结"):
        if bad in txt:
            return False, "像模板：" + bad
    return True, ""


def _write_allowed(st, txt):
    """这道闸决定「能不能落卡」。起念不受限制，落卡受。"""
    now = time.time()
    last_w = float(st.get("last_write_ts") or 0)
    if last_w and (now - last_w) < WRITE_COOLDOWN_S:
        return False, "冷却中（还差 %d 分钟）" % int((WRITE_COOLDOWN_S - (now - last_w)) / 60)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if st.get("write_day") != day:
        st["write_day"] = day
        st["write_n"] = 0
    if int(st.get("write_n") or 0) >= WRITE_MAX_PER_DAY:
        return False, "今天已经落满 %d 条" % WRITE_MAX_PER_DAY
    return True, ""


def think_once(mind):
    """起念一次。**这个函数会被 card-mind 线程调，绝不能在 world-tick 里跑。**"""
    st = _load()
    if st.get("busy"):
        return {"skip": "上一拍还没完"}
    st["busy"] = True
    _save(st)

    t0 = time.time()
    try:
        # ① 锁内：取快照（快）
        try:
            with mind.lock:
                snap = _snapshot(mind)
                # 2026-11-29：作意层。**这一层之前根本没有**，
                # 所以那六条念头全是「时间」——它没有任何所缘。
                try:
                    import card_attention as _ca
                    _cands = _ca.candidates(mind, snap, st.get("rep"))
                    _picked, _allc, _meta = _ca.focus(
                        _cands, state=(st.get("state") or "清醒"))
                    snap["focus"] = _picked
                    snap["focus_meta"] = _meta
                    snap["focus_all"] = [{"k": c["kind"], "a": c["access"]}
                                         for c in sorted(_allc, key=lambda x: -x["access"])[:5]]
                    snap["cands"] = _allc      # 完整候选，给 next_rep 用
                except Exception as _ae:
                    snap["focus"] = []
                    snap["focus_err"] = "%s: %s" % (type(_ae).__name__, _ae)
        except Exception as e:
            _save({**st, "busy": False, "last_err": "快照失败 %r" % e})
            return {"err": "快照失败"}

        # ② 锁外：调模型（慢，不拿锁）
        sys_p, user = _build(snap, _last["text"], snap.get("focus"))
        txt, err = _call_model(sys_p, user)

        # ③ 锁内：写回（快）
        wrote = None
        if txt:
            ok, why = _accept(txt)
            if ok:
                st0 = _load()
                wok, wwhy = _write_allowed(st0, txt)
                if not wok:
                    ok, why = False, wwhy
                    _save(st0)
            if ok:
                try:
                    with mind.lock:
                        mid = mind.card.remember(
                            "自己冒出一个念头：" + txt,
                            region="self", pool="long", salience=0.45,
                            source="inner", about_uid=getattr(mind, "local_uid", ""))
                        try:
                            if getattr(mind, "tl", None) is not None and mid:
                                mind.tl.birth_stamp(mid)
                        except Exception:
                            pass
                        wrote = mid
                except Exception as e:
                    err = (err or "") + " 写回失败 %r" % e
            else:
                err = "筛掉：" + why

        st = _load()
        st["busy"] = False
        st["last_think_ts"] = time.time()
        st["thinks"] = int(st.get("thinks") or 0) + 1
        st["last_ms"] = round((time.time() - t0) * 1000)
        if txt:
            st["last_text"] = txt[:120]
            _last["text"] = txt
            _last["ts"] = time.time()
        if wrote:
            st["wrote"] = int(st.get("wrote") or 0) + 1
            st["last_mid"] = wrote
            st["last_write_ts"] = time.time()
            st["write_n"] = int(st.get("write_n") or 0) + 1
        st["last_err"] = err or ""
        # 作意：选中就加一声，没选中的退一声（喊三声才回神）
        try:
            import card_attention as _ca
            st["rep"] = _ca.next_rep(st.get("rep"), snap.get("cands") or [])
        except Exception:
            pass
        st["focus_n"] = len(snap.get("focus") or [])
        st["focus_top"] = (snap.get("focus_all") or [{}])[0] if snap.get("focus_all") else {}
        _save(st)

        _log({"ts": time.time(), "think": True, "ms": st["last_ms"],
              "text": (txt or "")[:60], "wrote": wrote, "err": err or "",
              "focus": [c["text"][:24] for c in (snap.get("focus") or [])],
              "focus_all": snap.get("focus_all")})
        return {"text": txt, "wrote": wrote, "err": err, "ms": st["last_ms"]}
    except Exception as e:
        st = _load()
        st["busy"] = False
        st["last_err"] = "%s: %s" % (type(e).__name__, e)
        _save(st)
        return {"err": st["last_err"]}


# ---------------------------------------------------------------- 线程

def start(mind, name="card-mind"):
    """起 card-mind 线程。**daemon=True，绝不阻塞退出。**"""
    import threading

    _P["card"] = mind

    def loop():
        time.sleep(20)          # 起来先让卡自己站住
        while True:
            try:
                st = _load()
                state = st.get("state") or "清醒"
                gap = TICK.get(state, TICK["清醒"])[0]
                last = float(st.get("last_think_ts") or 0)
                if not _think_on() or (time.time() - last) < gap or st.get("busy"):
                    time.sleep(5)
                    continue
                r = think_once(mind)
                _e = r.get("err") or ""
                if _e and not _e.startswith("筛掉"):
                    st2 = _load()
                    st2["fail_streak"] = int(st2.get("fail_streak") or 0) + 1
                    _save(st2)
                time.sleep(5)
            except Exception as e:
                try:
                    st = _load()
                    st["last_err"] = "%s: %s" % (type(e).__name__, e)
                    _save(st)
                except Exception:
                    pass
                time.sleep(30)   # 出事了慢一点，不要忙等

    t = threading.Thread(target=loop, name=name, daemon=True)
    t.start()
    return t


def status():
    st = _load()
    return {
        "on": True,
        "total": st.get("total", 0),
        "thinks": st.get("thinks", 0),
        "wrote": st.get("wrote", 0),
        "last_text": st.get("last_text", ""),
        "last_ms": st.get("last_ms", 0),
        "last_err": st.get("last_err", ""),
        "busy": bool(st.get("busy")),
        "state": st.get("state", "清醒"),
    }
