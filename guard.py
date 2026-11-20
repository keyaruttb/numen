# -*- coding: utf-8 -*-
"""R4：本地接口的限流与审计（评审：现在只有 token 闸，没有限流、没有审计）。

三层，各管一件事：
1. `check()` —— 滑动窗口限流。写接口比读接口严：默认 写 120 次/分钟、读 600 次/分钟，
   超了回 429 + 还要等几秒。按键是客户端 IP，内存里存几十个键就够（本地服务）。
2. `note()` / `flush()` —— 审计。每个回包记一行：时间、方法路径、状态码、耗时、IP。
   先攒内存（每 20 条或 5 秒落一次盘），`/audit` 读的时候会先 flush，不会漏。
3. `recent()` —— 给 `/audit` 用，落库之后按时间倒序读。

为什么限流放在这一层：卡是本机单写者，最容易被自己人写的脚本打爆 ——
轮询 `/context` 每秒一次、回环重试没退避，都会把写锁拖死。限流是最后一道闸，
它不替代 token（那是「你是谁」），只管「你来多快」。
"""
import os
import time
from collections import deque

W_PER_MIN = int(os.environ.get("WHALE_RL_WRITE", "120") or 120)
R_PER_MIN = int(os.environ.get("WHALE_RL_READ", "600") or 600)
AUDIT_FLUSH_N = 5
AUDIT_FLUSH_SEC = 5.0

_w = {}      # ip -> deque[ts]  写
_r = {}      # ip -> deque[ts]  读
_pending = []
_last_flush = [0.0]
_ensured = 0
_stats = {"limited": 0, "audited": 0, "flush_err": 0}

_DDL = """
CREATE TABLE IF NOT EXISTS audit(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  ip      TEXT,
  path    TEXT,
  status  INTEGER,
  ms      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
"""


def ensure(conn):
    for stmt in [s.strip() for s in _DDL.split(";") if s.strip()]:
        conn.execute(stmt)
    conn.commit()


def _trim(dq, now, window=60.0):
    while dq and now - dq[0] > window:
        dq.popleft()


def check(ip, kind="read", now=None):
    """返回 (ok, retry_after_sec)。"""
    now = time.time() if now is None else now
    if kind == "write":
        dq = _w.setdefault(ip, deque())
        limit = W_PER_MIN
    else:
        dq = _r.setdefault(ip, deque())
        limit = R_PER_MIN
    _trim(dq, now)
    if len(dq) >= limit:
        _stats["limited"] += 1
        wait = max(1, int(60 - (now - dq[0])) + 1)
        return False, wait
    dq.append(now)
    # 键太多就清一清（本地服务，几十个键就够）
    if len(_w) > 200:
        for k in list(_w)[:50]:
            if not _w[k]:
                _w.pop(k, None)
    if len(_r) > 200:
        for k in list(_r)[:50]:
            if not _r[k]:
                _r.pop(k, None)
    return True, 0


def note(ip, path, status, ms, now=None, conn=None):
    """记一行审计；攒够条数或超过 5 秒再落盘。

    conn 必须由调用方传进来：服务进程里 mind_server 是 __main__，
    在别的模块 `import mind_server` 会再执行一遍整个模块（踩过一次）。
    """
    now = time.time() if now is None else now
    _pending.append((now, ip, path, int(status), int(ms or 0)))
    _stats["audited"] += 1
    if conn is None:
        return
    if len(_pending) >= AUDIT_FLUSH_N or (now - (_last_flush[0] or 0)) >= AUDIT_FLUSH_SEC:
        try:
            flush(conn)
        except Exception:
            pass


def flush(conn):
    if not _pending:
        _last_flush[0] = time.time()
        return 0
    global _ensured
    rows = _pending[:]
    del _pending[:]
    try:
        if not _ensured:
            ensure(conn)
            _ensured = 1
        conn.executemany(
            "INSERT INTO audit(ts, ip, path, status, ms) VALUES(?,?,?,?,?)", rows)
        conn.commit()
        _last_flush[0] = time.time()
        # 只留最近 20000 行，别让审计表把卡撑大
        conn.execute("DELETE FROM audit WHERE id <= (SELECT max(id) FROM audit) - 20000")
        conn.commit()
        return len(rows)
    except Exception:
        _stats["flush_err"] += 1
        _pending.extend(rows[-200:])
        return 0


def recent(conn, limit=50):
    flush(conn)
    cur = conn.execute(
        "SELECT ts, ip, path, status, ms FROM audit ORDER BY id DESC LIMIT ?",
        (int(limit),))
    out = []
    for r in cur.fetchall():
        out.append({"ts": r[0], "ip": r[1], "path": r[2],
                    "status": r[3], "ms": r[4]})
    return out


def stats(conn):
    try:
        flush(conn)
        n = conn.execute("SELECT count(*) FROM audit").fetchone()[0]
    except Exception:
        n = -1
    return {"audit_rows": n, "write_per_min": W_PER_MIN, "read_per_min": R_PER_MIN,
            "limited": _stats["limited"], "pending": len(_pending),
            "flush_err": _stats["flush_err"]}


def reset_for_test():
    """测试用：把内存计数清掉。"""
    _w.clear()
    _r.clear()
    del _pending[:]
    _stats.update({"limited": 0, "audited": 0, "flush_err": 0})
