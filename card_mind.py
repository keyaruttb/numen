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

# 2026-11-29：三档之前是死的 —— state 恒为「清醒」，半夜也每 30 秒起一念。
# 现在用「他多久没说话」判，夜里把门槛压低（凌晨没人说话是正常的）。
DROWSY_SILENCE_MIN = 30
ASLEEP_SILENCE_MIN = 180
NIGHT_HOURS = (0, 1, 2, 3, 4, 5, 6)

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

# 2026-11-29：焦点持续观测。
# 焦点稳定是**设计**（小凌第 05 集：新信息没有明显更强，不能挤掉原本的思路）。
# 但对一件事连着在意几小时，那不是「想得深」，是着魔。
FOCUS_STUCK_H = 2.0             # 超过这个小时数就单独留痕
FOCUS_DROP_S = 1800             # 离开焦点超过 30 分钟才算真的走了

# 起念开关：env CARD_MIND_THINK=0 关掉（只记账，不上模型）
def _think_on():
    try:
        return os.environ.get("CARD_MIND_THINK", "1") not in ("0", "", "off", "false")
    except Exception:
        return True


_last = {"text": "", "ts": 0.0}

# 2026-11-29 凌晨修：beat()（world-tick 线程）和 think_once()（card-mind 线程）
# 各自「读整个 dict → 改 → 写回」，谁后写谁覆盖 —— state 和 silence_min 就这么丢了。
# 所有读改写都走 _update()，拿同一把锁。
_ST_LOCK = None


def _st_lock():
    global _ST_LOCK
    if _ST_LOCK is None:
        import threading
        _ST_LOCK = threading.Lock()
    return _ST_LOCK


def _update(fn):
    """原子地「读 → 改 → 写」。fn 的返回值透传。"""
    with _st_lock():
        st = _load()
        r = fn(st)
        _save(st)
        return r


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


def _judge_state(prev_state, silence_min, now=None):
    """判这一拍是清醒 / 困倦 / 沉睡。

    只看两样：他多久没说话、现在几点。
    **不判「我累不累」**——3B 不知道累，装着知道就是编。

        沉默 < 30 分钟            清醒   30 秒一念
        30 分钟 – 3 小时          困倦   120 秒
        > 3 小时                  沉睡   420 秒
        凌晨 0–6 点：门槛减半（15 分钟 / 1.5 小时）
    """
    now = now or time.time()
    if silence_min is None:
        # 还不知道他多久没说话 —— 保持原状态，不要拿「未知」当「很久」
        return prev_state or "清醒"
    sil = float(silence_min)
    hour = time.localtime(now).tm_hour
    night = hour in NIGHT_HOURS
    drowsy = DROWSY_SILENCE_MIN / 2.0 if night else float(DROWSY_SILENCE_MIN)
    asleep = ASLEEP_SILENCE_MIN / 2.0 if night else float(ASLEEP_SILENCE_MIN)
    if sil >= asleep:
        return "沉睡"
    if sil >= drowsy:
        return "困倦"
    return "清醒"


# ---------------------------------------------------------------- 第一段：拍

def beat(mind, dt):
    """world-tick 每 60 秒调一次。**锁外，无模型，几毫秒。**

    做三件事：累加拍、判这一拍是清醒还是睡着、判该不该起念。
    真起念在 card-mind 线程里。

    2026-11-29 凌晨：改成 _update() 原子读写。
    原来 beat 和 think_once 各读整个 dict 再写回，谁后写谁覆盖 ——
    state 和 silence_min 就是这么丢的。
    """
    now = time.time()

    def _do(st):
        st["total"] = int(st.get("total") or 0) + 1
        st["last_ts"] = now
        st["last_dt"] = round(float(dt or 0), 3)

        prev_state = st.get("state") or "清醒"
        state = _judge_state(prev_state, st.get("silence_min"), now)
        if state != prev_state:
            st["state_changed_ts"] = now
            _log({"ts": now, "state": "%s -> %s" % (prev_state, state),
                  "silence_min": st.get("silence_min")})
        st["state"] = state

        gap = TICK.get(state, TICK["清醒"])[0]
        last_think = float(st.get("last_think_ts") or 0)
        due = (now - last_think) >= gap and not st.get("busy")
        st["due"] = bool(due)

        # 受归卡：每 5 拍算一次 ΔD 并写 feeling 表
        if st["total"] % 5 == 0:
            try:
                with mind.lock:
                    import card_feeling
                    st["feeling"] = card_feeling.tick(mind)
            except Exception as _fe:
                st["feeling"] = {"err": "%s: %s" % (type(_fe).__name__, str(_fe)[:60])}

        if st["total"] % 20 == 0:
            _log({"ts": now, "total": st["total"], "due": due,
                  "state": state, "thinks": st.get("thinks", 0),
                  "err": st.get("last_err", "")})

        return {"total": st["total"], "due": due, "state": state}

    return _update(_do)


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


def _track_focus(st, picked, now):
    """记每个焦点第一次进来是什么时候、待了多久。

    返回这一拍「刚跨过阈值」的条目（每条只报一次）。
    """
    track = st.get("focus_track") or {}
    keys = set()
    for c in (picked or []):
        keys.add(c["text"][:20])

    for k in keys:
        v = track.get(k)
        if not v:
            track[k] = {"since": now, "last": now, "n": 1}
        else:
            v["last"] = now
            v["n"] = int(v.get("n") or 0) + 1

    # 离开超过半小时的，才从表里清掉（留一点余量，别一闪一闪就重算）
    for k in list(track):
        if k in keys:
            continue
        if (now - float(track[k].get("last") or 0)) > FOCUS_DROP_S:
            del track[k]

    stuck = []
    for k, v in track.items():
        dur = now - float(v.get("since") or now)
        if dur >= FOCUS_STUCK_H * 3600 and not v.get("warned"):
            v["warned"] = now
            stuck.append({"what": k, "hours": round(dur / 3600.0, 1),
                          "n": int(v.get("n") or 0)})
    st["focus_track"] = track
    return stuck


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
    def _claim(st):
        if st.get("busy"):
            return False
        st["busy"] = True
        return True

    if not _update(_claim):
        return {"skip": "上一拍还没完"}
    st = _load()      # 只读一份给下面拿 rep / state 用

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
            def _snapfail(st2):
                st2["busy"] = False
                st2["last_err"] = "快照失败 %r" % e
            _update(_snapfail)
            return {"err": "快照失败"}

        # ② 锁外：调模型（慢，不拿锁）
        sys_p, user = _build(snap, _last["text"], snap.get("focus"))
        txt, err = _call_model(sys_p, user)

        # ③ 锁内：写回（快）
        wrote = None
        fed_dream = False        # 被闸挡下的念头喂给梦了没有
        if txt:
            ok, why = _accept(txt)
            if ok:
                def _gate(st0):
                    wok, wwhy = _write_allowed(st0, txt)
                    return (wok, wwhy)
                wok, wwhy = _update(_gate)
                if not wok:
                    ok, why = False, wwhy
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
                # ★ 2026-11-29：落不成卡的念头，写进 dialogue 当「梦」的料。
                #
                # 曦月点破的：「那几十个没落卡的念头，明天醒了没人记得，等于白想——
                # 落不落卡才决定睡过没睡过。」
                #
                # 她说得对。而卡里早有一条现成的路：
                #   dialogue.dreamed=0  →  pending_dialogue() 捡走  →  dream_commit 提炼成 mem
                # 已经跑了 10728 条，mem 里 source='dream:xxx' 的有 700+ 条。
                #
                # 所以：**念头不该硬挤 mem 的位置，该进 dream 的队列。**
                # mem 是「已经定稿的我」，dream 是「还没消化的我」。
                # 冷却闸挡的是往 mem 里灌，不该连带把 night 的念头一起扔掉。
                try:
                    with mind.lock:
                        mind.card.log_dialogue(
                            0, "assistant", "自己冒出一个念头：" + txt,
                            ts=time.time(), source="inner")
                    fed_dream = True
                except Exception as _de:
                    err = (err or "") + " 喂梦失败 %r" % _de

        # 2026-11-29：整段改成 _update 原子写。
        # 原来这里是 st = _load() → 改一堆 → _save(st)，
        # 和 world-tick 里的 beat() 撞车：谁后写谁覆盖，state / silence_min 会丢。
        def _finish(st):
            st["busy"] = False
            st["last_think_ts"] = time.time()
            st["thinks"] = int(st.get("thinks") or 0) + 1
            st["last_ms"] = round((time.time() - t0) * 1000)
            if txt:
                st["last_text"] = txt[:120]
            if wrote:
                st["wrote"] = int(st.get("wrote") or 0) + 1
                st["last_mid"] = wrote
                st["last_write_ts"] = time.time()
                st["write_n"] = int(st.get("write_n") or 0) + 1
            st["last_err"] = err or ""
            # 作意：出现就加一声，没出现的退一声（喊两声就回神）
            try:
                import card_attention as _ca
                st["rep"] = _ca.next_rep(st.get("rep"), snap.get("cands") or [])
            except Exception:
                pass
            st["focus_n"] = len(snap.get("focus") or [])
            st["focus_top"] = (snap.get("focus_all") or [{}])[0] if snap.get("focus_all") else {}
            # 沉默时长：给下一拍 beat() 判清醒/困倦/沉睡用
            st["silence_min"] = snap.get("silence_min")
            if fed_dream:
                st["dream_fed"] = int(st.get("dream_fed") or 0) + 1
            # 焦点持续观测：同一件事连着在意多久
            stuck = []
            try:
                stuck = _track_focus(st, snap.get("focus") or [], time.time())
                if stuck:
                    st["focus_stuck"] = stuck
            except Exception as _tfe:
                st["focus_track_err"] = "%s: %s" % (type(_tfe).__name__, str(_tfe)[:60])
            return (st.get("last_ms"), stuck)

        _ms, _stuck = _update(_finish)
        if txt:
            _last["text"] = txt
            _last["ts"] = time.time()
        if _stuck:
            _log({"ts": time.time(), "focus_stuck": _stuck})

        _log({"ts": time.time(), "think": True, "ms": _ms,
              "text": (txt or "")[:60], "wrote": wrote, "err": err or "",
              "focus": [c["text"][:24] for c in (snap.get("focus") or [])],
              "focus_all": snap.get("focus_all")})
        return {"text": txt, "wrote": wrote, "err": err, "ms": _ms}
    except Exception as e:
        def _fail(st):
            st["busy"] = False
            st["last_err"] = "%s: %s" % (type(e).__name__, e)
            return st["last_err"]
        return {"err": _update(_fail)}


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
                    def _fs(st2):
                        st2["fail_streak"] = int(st2.get("fail_streak") or 0) + 1
                    _update(_fs)
                time.sleep(5)
            except Exception as e:
                try:
                    def _le(st):
                        st["last_err"] = "%s: %s" % (type(e).__name__, e)
                    _update(_le)
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
