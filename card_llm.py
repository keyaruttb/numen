# -*- coding: utf-8 -*-
"""card_llm —— 本地模型归卡管。

2026-11-29 凌晨。目标第④项：llama-server 的**启动 / 健康 / 参数**由卡负责。

## 以前是什么样

```
心流（_xinliu/xinliu.py）
  LLAMA_EXE / LLAMA_MODEL / LLAMA_ARGS   ← 参数写在心流里
  _ensure_llm(st)                        ← 拉起也在心流里
  think() 里自己拼 urllib 调 8090         ← 调用也在心流里
```

**模型是「卡的身体」的一部分，但管家权在卡外面。**

## 现在

```
卡进程（mind_server）
  card_llm.start()      启动检查
  card_llm.health()     探活
  card_llm.ensure()     挂了就拉（连续 3 次失败才试，10 分钟不重复）
  card_llm.chat()       唯一的调用入口
  card_llm.info()       给 /health 看
```

参数集中在这个文件里，环境变量可覆盖：

    CARD_LLM_EXE        默认 C:\\dsh\\_xinliu\\llama\\llama-server.exe
    CARD_LLM_MODEL      默认 C:\\dsh\\_models\\qwen2.5-3b-instruct-q4_k_m.gguf
    CARD_LLM_CTX        默认 4096
    CARD_LLM_NGL        默认 99
    CARD_LLM_PORT       默认 8090
    CARD_LLM_AUTO=0     关掉自动拉起（只探活）

## 为什么「连续 3 次失败才拉起」

模型加载要十几秒、吃 3.7 GB 显存。一次探活超时可能只是忙，
立刻重启会变成「重启风暴」。所以留了三道：连败 3 次、冷却 10 分钟、拉起后等 60 秒。
"""

import json
import os
import subprocess
import time
import urllib.request

HERE = r"C:\dsh\_xinliu"

EXE = os.environ.get("CARD_LLM_EXE", os.path.join(HERE, "llama", "llama-server.exe"))
MODEL = os.environ.get("CARD_LLM_MODEL",
                       r"C:\dsh\_models\qwen2.5-3b-instruct-q4_k_m.gguf")
CTX = int(os.environ.get("CARD_LLM_CTX", "4096") or 4096)
NGL = int(os.environ.get("CARD_LLM_NGL", "99") or 99)
PORT = int(os.environ.get("CARD_LLM_PORT", "8090") or 8090)
BASE = "http://127.0.0.1:%d" % PORT

AUTO = os.environ.get("CARD_LLM_AUTO", "1") not in ("0", "", "off", "false")

# 2026-11-29：llama 的 stdout/stderr 归口到卡目录（原来在 _xinliu 下，是心流起它时留下的）
LOG = os.environ.get("CARD_LLM_LOG", r"C:\dsh\_card\logs-8848\llama-server.log")
LOG_MAX = 8 * 1024 * 1024

# 并发：3B 是单请求串行的，两个调用方同时打只会互相排队。
# 在卡里显式串起来，并且给等待设上限 —— 免得一个慢请求把别人堵死还不知道。
_CHAT_LOCK = None
_CHAT_WAIT_S = float(os.environ.get("CARD_LLM_WAIT_S", "120") or 120)

FAIL_BEFORE_RESTART = 3
RESTART_COOLDOWN_S = 600
WAIT_READY_S = 60
PROBE_TIMEOUT = 3

_state = {
    "fail_streak": 0,
    "last_restart": 0.0,
    "restarts": 0,
    "last_err": "",
    "last_ok_ts": 0.0,
    "last_ms": 0,
}


def _args():
    return ["-m", MODEL, "-ngl", str(NGL), "-c", str(CTX),
            "--host", "127.0.0.1", "--port", str(PORT)]


def health(timeout=PROBE_TIMEOUT):
    """探活。不抛出、不重启。"""
    try:
        with urllib.request.urlopen(BASE + "/v1/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure(force=False):
    """现在能用吗？不能用就（有条件地）拉起来。返回 True/False。

    **这是卡替「身体」做的一次自我维持。**

    force=True：跳过「连败 3 次」和「冷却 10 分钟」两道闸。
    2026-11-29 补：restart() 里 stop 完立刻 ensure，那时只有 1 次失败，
    会被这两道闸挡住 —— 结果是「stop 成功、restart 什么都没做」。
    显式要重起的时候，那两道闸不该拦。
    """
    if health():
        _state["fail_streak"] = 0
        _state["last_ok_ts"] = time.time()
        return True

    _state["fail_streak"] += 1
    if not AUTO and not force:
        _state["last_err"] = "CARD_LLM_AUTO=0，不自动拉起"
        return False
    if not force:
        if _state["fail_streak"] < FAIL_BEFORE_RESTART:
            return False
        if time.time() - _state["last_restart"] < RESTART_COOLDOWN_S:
            return False
    if not os.path.exists(EXE):
        _state["last_err"] = "没有 llama-server.exe：%s" % EXE
        return False
    if not os.path.exists(MODEL):
        _state["last_err"] = "没有模型文件：%s" % MODEL
        return False

    _state["last_restart"] = time.time()
    try:
        _rotate_log()
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        lf = open(LOG, "ab")
        subprocess.Popen([EXE] + _args(),
                         cwd=os.path.dirname(EXE),
                         stdout=lf, stderr=lf,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        lf.close()
        _state["last_err"] = ""
    except Exception as e:
        _state["last_err"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return False

    deadline = time.time() + WAIT_READY_S
    while time.time() < deadline:
        time.sleep(2)
        if health():
            _state["fail_streak"] = 0
            _state["restarts"] += 1
            _state["last_ok_ts"] = time.time()
            return True
    _state["last_err"] = "%ds 内没起来" % WAIT_READY_S
    return False


def _rotate_log():
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
    except Exception:
        pass


def _lock():
    global _CHAT_LOCK
    if _CHAT_LOCK is None:
        import threading
        _CHAT_LOCK = threading.Lock()
    return _CHAT_LOCK


def _pids():
    """找 llama-server.exe 的 pid。用 tasklist，避开 psutil 依赖。"""
    out = []
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe",
                            "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=0x08000000)
        for line in (r.stdout or "").splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "llama-server.exe":
                try:
                    out.append(int(parts[1]))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def stop(timeout=15):
    """停掉 llama-server。**换模型 / 调试时用，不是自动的。**

    卡替身体做的另一件事：不但能把它扶起来，也能让它先躺下。
    """
    pids = _pids()
    if not pids:
        return {"ok": True, "stopped": [], "note": "本来就没在跑"}
    stopped, failed = [], []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=0x08000000)
            stopped.append(pid)
        except Exception as e:
            failed.append({"pid": pid, "err": str(e)[:60]})
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pids():
            break
        time.sleep(0.5)
    _state["last_err"] = ""
    return {"ok": not _pids(), "stopped": stopped, "failed": failed,
            "still": _pids()}


def restart():
    """stop → ensure。换模型或卡住时用。"""
    r = stop()
    _state["last_restart"] = 0.0      # 显式重起，清掉冷却
    ok = ensure(force=True)
    return {"stop": r, "ok": ok, **info()}


def chat(messages, max_tokens=64, temperature=1.0, top_p=0.95,
         seed=None, timeout=90, ensure_first=True):
    """唯一的调用入口。返回 (text, err)。

    **卡里所有要用本地模型的地方都走这一条**，
    以后换模型、改参数、加限流，只改这里。
    """
    if ensure_first and not ensure():
        return None, "模型不可用：" + (_state["last_err"] or "探活失败")

    _lk = _lock()
    _t_wait = time.time()
    if not _lk.acquire(timeout=_CHAT_WAIT_S):
        return None, "模型忙：等了 %.0f 秒还没轮上" % _CHAT_WAIT_S
    try:
        return _chat_locked(messages, max_tokens, temperature, top_p, seed, timeout)
    finally:
        _lk.release()


def _chat_locked(messages, max_tokens, temperature, top_p, seed, timeout):

    body = {"messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(top_p)}
    if seed is not None:
        body["seed"] = int(seed)

    t0 = time.time()
    try:
        req = urllib.request.Request(
            BASE + "/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        _state["last_ms"] = round((time.time() - t0) * 1000)
        _state["last_ok_ts"] = time.time()
        txt = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return (txt or "").strip(), ""
    except Exception as e:
        _state["last_err"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return None, _state["last_err"]


def info():
    """/health 里那一小块。"""
    return {
        "up": health(),
        "port": PORT,
        "model": os.path.basename(MODEL),
        "ctx": CTX,
        "ngl": NGL,
        "auto": AUTO,
        "restarts": _state["restarts"],
        "fail_streak": _state["fail_streak"],
        "last_ms": _state["last_ms"],
        "last_err": _state["last_err"],
        "last_ok_age_s": (round(time.time() - _state["last_ok_ts"], 1)
                          if _state["last_ok_ts"] else None),
        "log": LOG,
        "log_mb": (round(os.path.getsize(LOG) / 1048576.0, 2)
                   if os.path.exists(LOG) else 0),
        "busy": bool(_CHAT_LOCK and _CHAT_LOCK.locked()),
    }


def start():
    """卡启动时调一次：探一下，不在就按规矩拉。不在起线程——拉起本身可能等 60 秒。"""
    ok = ensure()
    return {"ok": ok, **info()}
