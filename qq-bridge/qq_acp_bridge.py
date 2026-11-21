# -*- coding: utf-8 -*-
"""QQ <-> DSH ACP 桥（最小可运行示例）。

- 每 2 秒轮询 NapCat 的 `/get_recent_contact` + `/get_friend_msg_history`，按 message_id 去重
- 收到消息 -> 塞进一个**常驻**的 `dsh --profile acp` 进程（JSON-RPC over stdio）
- 收 assistant 的流式 chunk -> 截成 2~3 条短句 -> 发回 QQ
- 与 web / 桌面共用同一张卡（CARD_DB），所以记忆是同一份

全部配置走环境变量（见 docs/接入问答-qq-acp-bridge.md）：
    DSH_CMD / DSH_EXE / DSH_BINJS / DSH_PROFILE / DSH_MODEL
    NAPCAT_URL / NAPCAT_TOKEN / QQ_SELF_ID
    QQ_BRIDGE_CWD / WHALE_CARD_DB

注意：这是**示例**，不是被我实际运行的部署文件（那份里是我的 QQ 号与 token）。
"""
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
try:
    from qq_send import send as safe_send       # 我们自己的六道闸门（可选）
except Exception:                               # 公开示例里没有它，退回直连发送
    safe_send = None

# —— dsh 入口：老版有 dsh.cmd，新版把 CLI 打进 app.asar，只能用 Electron 的 node 模式 ——
DSH_CMD = os.environ.get("DSH_CMD", "")
DSH_EXE = os.environ.get("DSH_EXE", "")
DSH_BINJS = os.environ.get("DSH_BINJS", "")
if not DSH_BINJS and DSH_EXE:
    DSH_BINJS = os.path.join(
        os.path.dirname(DSH_EXE),
        r"resources\app.asar\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js")
PROFILE = os.environ.get("DSH_PROFILE", "acp")
MODEL = os.environ.get("DSH_MODEL", "")          # 如 '["deepseek-account","deepseek-v4-pro"]'


def dsh_argv():
    """返回 (启动前缀, 额外环境变量)。

    asar 对普通进程是一个文件、不是目录，Python 看不到里面的 bin.js，
    但 Electron 自己能看到 —— 所以只校验 exe 在不在。
    """
    if DSH_CMD and os.path.exists(DSH_CMD):
        return [DSH_CMD], {}
    if DSH_EXE and os.path.exists(DSH_EXE):
        return [DSH_EXE, DSH_BINJS], {"ELECTRON_RUN_AS_NODE": "1"}
    raise RuntimeError("找不到可用的 dsh 入口：请设置 DSH_CMD（旧）或 DSH_EXE（桌面版）")


WORKDIR = os.environ.get("QQ_BRIDGE_CWD") or os.getcwd()
NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:6700")
WS_URL = NAPCAT.replace("http://", "ws://")
TOKEN = os.environ.get("NAPCAT_TOKEN", "")
SELF_ID = int(os.environ.get("QQ_SELF_ID", "0") or 0)
CARD_DB = os.environ.get("WHALE_CARD_DB", "")
CWD = WORKDIR


class AcpSession:
    """一个常驻的 ACP 会话，串行处理 prompt。"""

    def __init__(self):
        self.proc = None
        self.sid = None
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self._next_id = 1
        self.ready = False

    def start(self):
        prefix, extra = dsh_argv()
        env = dict(os.environ)
        env.update(extra)
        self.proc = subprocess.Popen(
            prefix + ["--profile", PROFILE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, env=env,
        )
        for stream in (self.proc.stdout, self.proc.stderr):
            threading.Thread(target=self._reader, args=(stream,), daemon=True).start()

        r = self._call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=40)
        if not r:
            raise RuntimeError("initialize failed")
        r = self._call("session/new", {"cwd": CWD, "mcpServers": []}, timeout=60)
        self.sid = r["result"]["sessionId"]
        if MODEL:
            self._call("session/set_config_option",
                       {"sessionId": self.sid, "configId": "model", "value": MODEL}, timeout=30)
        self.ready = True
        print(f"[acp] session ready {self.sid}", flush=True)

    def _reader(self, stream):
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                self.q.put(text)

    def _send(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _call(self, method, params, timeout=120):
        with self.lock:
            rid = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            end = time.time() + timeout
            while time.time() < end:
                try:
                    text = self.q.get(timeout=0.4)
                except queue.Empty:
                    if self.proc.poll() is not None:
                        raise RuntimeError(f"acp exited: {self.proc.returncode}")
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    print(f"[acp:raw] {text[:200]}", flush=True)
                    continue
                if obj.get("id") == rid:
                    return obj
                # 忽略 update 通知
            raise TimeoutError(method)

    def _recent_dialogue(self, n=10):
        """只读卡里最近 n 条对话，拼成上下文。失败返回空串。"""
        try:
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % CARD_DB, uri=True, timeout=5)
            rows = conn.execute(
                "select ts, role, text, source from dialogue order by id desc limit ?", (n,)
            ).fetchall()
            conn.close()
            rows.reverse()
            out = []
            for ts, role, text, src in rows:
                who = "我" if role == "assistant" else "他"
                tag = "（QQ）" if src == "qq" else ""
                out.append(f"[{who}{tag}] {(text or '')[:300]}")
            return "\n".join(out)
        except Exception:
            return ""

    def ask(self, text, timeout=300, speaker=None):
        """发一条用户消息，收集 assistant 回复文本。"""
        ctx = self._recent_dialogue(10)
        if ctx:
            text = ("【最近两边的对话（供你接上下文，不要复述，直接回答最后一句）】\n"
                    + ctx + "\n\n【他刚说】\n" + text)
        if speaker:
            text = ("【说话人】" + speaker + "（QQ 里每个人不一样，别把话安到别人头上）\n\n" + text)
        text += ("\n\n【回答要求】像在 QQ 上聊天：分 2-3 条短句，每条不超过 25 字，中间换行。"
                 "不要长篇大论、不要列清单、不要总结、不要说教。")
        with self.lock:
            rid = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
                        "params": {"sessionId": self.sid,
                                   "prompt": [{"type": "text", "text": text}]}})
            parts = []
            end = time.time() + timeout
            while time.time() < end:
                try:
                    raw = self.q.get(timeout=0.5)
                except queue.Empty:
                    if self.proc.poll() is not None:
                        raise RuntimeError("acp exited")
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("id") == rid:
                    return _clip(_dedup("".join(parts).strip()))
                upd = obj.get("params", {}).get("update", {})
                if upd.get("sessionUpdate") == "agent_message_chunk":
                    parts.append(upd.get("content", {}).get("text", ""))
            return _clip(_dedup("".join(parts).strip())) or "（超时，没等到回复）"


def _clip(text, max_seg=3, max_len=150):
    """回话硬截断：最多 max_seg 段、max_len 字，超了砍掉尾巴加省略号。"""
    t = (text or "").strip()
    if not t:
        return t
    segs = [s.strip() for s in t.split("\n") if s.strip()]
    if len(segs) > max_seg:
        segs = segs[:max_seg]
    out = "\n".join(segs)
    if len(out) > max_len:
        out = out[:max_len - 1] + "…"
    return out


def _dedup(text):
    """ACP 有时把同一段 chunk 发两遍（A+A），取一半。"""
    n = len(text)
    if n >= 40 and n % 2 == 0:
        half = n // 2
        if text[:half] == text[half:]:
            return text[:half].strip()
    return text


ACP = AcpSession()

# 我最近在哪些群说过话：gid -> 最后一次说话的时间戳。
# 说过话的群，10 分钟内还有人说话就接着聊，不用再 @。
ACTIVE_GROUPS = {}

# 连续消息合并：同一个会话 2.5 秒内的几条消息攒成一段，一起看一起回。
# key = (message_type, gid 或 uid)
PENDING = {}

# 群聊节流：每 3 条才回 1 条，同一群 45 秒内最多回一次（被 @ / 点名时不限）
GROUP_COUNT = {}
GROUP_LAST = {}
GROUP_MIN_GAP = 180.0
GROUP_EVERY = 3


async def handle_message(data, reply_to=0):
    if data.get("post_type") != "message":
        return
    mt = data.get("message_type")
    if mt not in ("private", "group"):
        return
    # 注意：lastestMsg 的 self_id 是机器人自己，user_id 才是发送者。
    # 所以这里不能按 self_id 过滤，要按 user_id。
    uid = data.get("user_id")
    gid = data.get("group_id")
    if uid == SELF_ID:
        return
    segments = data.get("message") or []
    text = "".join(s.get("data", {}).get("text", "") for s in segments if s.get("type") == "text").strip()
    if not text:
        return
    if mt == "group":
        # 群里三种情况接：@我、叫"汐音"、或者我刚在这个群说过话（10 分钟内还有余温）。
        at_me = any(
            s.get("type") == "at" and str(s.get("data", {}).get("qq")) == str(SELF_ID)
            for s in segments
        )
        now = time.time()
        warm = ACTIVE_GROUPS.get(gid, 0) and (now - ACTIVE_GROUPS[gid] < 600)
        if not (at_me or "汐音" in text or warm):
            print(f"[qq] [群 {gid}] {uid}: {text[:40]}（不接）", flush=True)
            return
        text = text.replace(f"[CQ:at,qq={SELF_ID}]", "").strip()
        why = "被@叫" if (at_me or "汐音" in text) else "接着聊"
        print(f"[qq] [群 {gid}] {uid}（{why}）: {text}", flush=True)
    else:
        print(f"[qq] {uid}: {text}", flush=True)
    # —— 连续消息合并：同一个人 2.5 秒内连发几句，攒成一段一起看、一起回 ——
    bkey = (mt, gid if mt == "group" else uid)
    buf = PENDING.setdefault(bkey, {"texts": [], "reply_to": 0, "owner": None})
    buf["texts"].append(text)
    if reply_to:
        buf["reply_to"] = reply_to
    tok = object()
    buf["owner"] = tok
    await asyncio.sleep(2.5)
    if buf["owner"] is not tok:
        print(f"[qq]   （并入后一条，不单独回）", flush=True)
        return
    text = "\n".join(buf["texts"])
    buf["texts"] = []
    reply_to = buf["reply_to"] or reply_to
    try:
        speaker = await asyncio.to_thread(_who, uid, gid if mt == "group" else None)
        reply = await asyncio.to_thread(ACP.ask, text, 300, speaker)
    except Exception as e:
        reply = f"（桥出错了：{e}）"
    if not reply:
        reply = "（没组织出话来）"
    if mt == "group":
        print(f"[qq] -> [群 {gid}]: {reply[:80]}", flush=True)
        code, msg = await asyncio.to_thread(_send, reply, None, gid, reply_to)
        if code == 0:
            ACTIVE_GROUPS[gid] = time.time()
    else:
        print(f"[qq] -> {uid}: {reply[:80]}", flush=True)
        code, msg = await asyncio.to_thread(_send, reply, uid, None, reply_to)
    if code != 0:
        print(f"[qq] 闸门拦了: {msg}", flush=True)


def _send(text, uid=None, gid=None, reply_to=0):
    """发送：有 qq_send（六道闸门）就用它，没有就直连 NapCat。

    返回 (code, msg)，code=0 表示成功 —— 与 qq_send.send 的约定一致。
    """
    if safe_send is not None:
        return safe_send(text, uid, gid, "mention", False, reply_to)
    body = {"message": text}
    if gid:
        body["group_id"] = gid
    else:
        body["user_id"] = uid
    try:
        http_post("/send_msg", body)
        return 0, "ok"
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def _who(uid, gid=None):
    """查昵称：群里优先用群名片，私聊用昵称。失败就返回 uid。"""
    try:
        if gid:
            r = http_post("/get_group_member_info", {"group_id": gid, "user_id": uid, "no_cache": True})
        else:
            r = http_post("/get_stranger_info", {"user_id": uid, "no_cache": True})
        d = r.get("data") or {}
        return d.get("card") or d.get("nickname") or str(uid)
    except Exception:
        return str(uid)


API = NAPCAT


def http_post(path, body, timeout=20):
    import urllib.request
    req = urllib.request.Request(API + path, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


async def main():
    await asyncio.to_thread(ACP.start)
    print("[poll] get_recent_contact -> get_friend_msg_history (带 message_id 以支持引用)", flush=True)
    seen = set()
    first = True
    while True:
        try:
            r = await asyncio.to_thread(http_post, "/get_recent_contact", {"count": 10})
            targets = []
            for c in (r.get("data") or []):
                m = c.get("lastestMsg") or {}
                mt = m.get("message_type")
                if mt == "private":
                    uid = m.get("user_id")
                    if uid and uid != SELF_ID:
                        targets.append(("private", uid))
                elif mt == "group":
                    gid = m.get("group_id")
                    if gid:
                        targets.append(("group", gid))
            for kind, key in dict.fromkeys(targets):
                if kind == "private":
                    hist = await asyncio.to_thread(
                        http_post, "/get_friend_msg_history", {"user_id": key, "count": 5})
                else:
                    hist = await asyncio.to_thread(
                        http_post, "/get_group_msg_history", {"group_id": key, "count": 5})
                msgs = (hist.get("data") or {}).get("messages") or []
                for m in msgs:
                    mid = m.get("message_id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    sender_uid = (m.get("sender") or {}).get("user_id")
                    if sender_uid == SELF_ID:
                        # 自己发的：不回，但如果是群，说明我刚开过口，记下余温
                        if kind == "group":
                            ACTIVE_GROUPS[key] = time.time()
                        continue  # 别再回自己（否则自问自答）
                    if first:
                        continue  # 第一轮只建水位线，不回溯历史
                    await handle_message({
                        "post_type": "message", "message_type": kind,
                        "self_id": SELF_ID, "user_id": sender_uid or key,
                        "group_id": key if kind == "group" else None,
                        "message": m.get("message") or [],
                        "raw_message": m.get("raw_message") or "",
                        "message_id": mid,
                    }, reply_to=mid)
            first = False
        except Exception as e:
            print(f"[poll] error: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
