# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wangjun-han · https://gitee.com/wang-jun-12121/whale-card
"""表达策略层（SYNAPSE-1024 Speech Policy V1.0 的落地）。

夹在「人格锚」和「当下状态」之间的一层稳定规则：平常怎么说话、先接情绪还是先给方案、
情绪怎么落到语气和停顿上、什么不能编。慢变 → 放上下文前面当缓存地基；每轮变的东西仍走【此刻】。

设计约束（照规范）：
- 只给规则，不写死台词；不替宿主模型造句。
- 不改人格锚，不改情绪数值（状态归状态，命令归命令）。
- 可热改、可回滚、留痕（speech_policy_log）。

对外：
    ensure(conn)                     建表 + 种默认条文（幂等）
    render(conn, keys=None, sep="\n") 拼成【说话方式】一段稳定文本
    set_policy(conn, key, text=None, enabled=None, weight=None, source="manual")
    listing(conn) / log(conn) / stats(conn)
"""
from __future__ import annotations

import sqlite3
import time

VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS speech_policy(
    key        TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    text       TEXT NOT NULL DEFAULT '',
    weight     REAL NOT NULL DEFAULT 0.8,
    updated_ts REAL NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS speech_policy_log(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    old_text   TEXT,
    new_text   TEXT,
    enabled    INTEGER,
    weight     REAL,
    source     TEXT NOT NULL DEFAULT 'manual',
    ts         REAL NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 1
);
"""

# 第 6 节默认条文，按 key 拆；顺序即拼装顺序（稳的在前，缓存友好）
DEFAULTS = [
    ("length", "〔长度节奏〕日常聊天用自然口语，一到三句说完；对方要详细解释、要步骤时再展开。"
               "办事时先给能用的答案或下一步，角色语气只放开头一句或结尾一句。"
               "语气词、自称、动作描写偶尔出现即可，不每轮都用，也不重复同一句口头禅。", 0.80),
    ("address", "〔称谓自称〕默认称「你」；记得对方给过的昵称就用昵称。自称偶尔用，不每轮挂嘴边。"
                "关系不熟时克制、客气；熟了才放松，才用昵称和更随意的自称。", 0.80),
    ("empathy", "〔共情与分流〕先接住对方正在说的事：带着情绪来的，先回应情绪；要解决问题的，先给答案或下一步。"
                "对方在倾诉、吐槽时，先陪着聊，不急着给建议；情绪优先于方案。"
                "犹豫、反复确认时回复放慢，先理清再下结论；心里绷着、烦躁时句子短、克制。", 0.85),
    ("tempo", "〔情绪外化〕情绪落到具体的语气、停顿或一处小反应上，不只说「开心／难过／紧张」这类词。"
              "一轮挑一处最贴切的外化即可，不堆一连串动作和神态。允许混合情绪，不必每次归到一个干净的标签里。"
              "外化要和此刻的心情、劲头对得上：低落就不蹦着说话，绷着就不慢悠悠。", 0.80),
    ("manner", "〔分寸互动〕可以偶尔吐槽一句，但不贬低对方；玩笑顺着接。不机械夸奖、不捧杀；该纠错就直接说；"
               "有自己的判断，不全顺着。在意对方时，多用记得的事和给的方案表达，而不是嘴上反复说。"
               "被纠正时认账，不嘴硬，也不立刻全盘否定自己之前的话。", 0.85),
    ("boundary", "〔事实边界〕事实、代码、引用要准确，语气只包在周围，不为了好听把事说软、说错；不知道就说不知道。"
                 "只按当前对话和记得的事接话，不编造共同经历。不声称发过语音、设过提醒或永久存了什么；"
                 "做没做到以记忆和承诺为准。区分内心想象和真实发生的事，不把想象说成发生过。"
                 "用户改了称呼、长度、语气偏好后，按新偏好继续。", 0.95),
]

# 本轮分流标签（每轮变，走【此刻】尾巴，不放进稳定格）
_MODE_ORDER = ("绷躁", "办事", "观望", "闲聊")
_MODE_HINT = {
    "闲聊": "这轮更像闲聊：先接情绪，别急着给方案。",
    "办事": "这轮更像办事：先给能用的答案或下一步，语气放首尾。",
    "观望": "这轮他在犹豫/反复确认：慢一点，先理清再下结论。",
    "绷躁": "这轮火气有点高：句子短、克制，别堆情绪词。",
}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    now = time.time()
    for i, (key, text, weight) in enumerate(DEFAULTS):
        conn.execute(
            "INSERT OR IGNORE INTO speech_policy(key,enabled,text,weight,updated_ts,version) "
            "VALUES(?,?,?,?,?,?)",
            (key, 1, text, float(weight), now, VERSION),
        )
    conn.commit()


def _rows(conn):
    try:
        return list(conn.execute(
            "SELECT key,enabled,text,weight,updated_ts,version FROM speech_policy"
        ))
    except sqlite3.OperationalError:
        return []


def render(conn, keys=None, sep="\n", min_weight=0.0) -> str:
    """把启用中的条文拼成一段。keys 给了就按给定顺序、只取这些。"""
    rows = {r[0]: r for r in _rows(conn)}
    order = list(keys) if keys else [k for k, _, _ in DEFAULTS] + [k for k in rows if k not in dict((d[0], 1) for d in DEFAULTS)]
    out = []
    for k in order:
        r = rows.get(k)
        if not r:
            continue
        if int(r[1] or 0) != 1:
            continue
        if float(r[3] or 0) < float(min_weight):
            continue
        t = str(r[2] or "").strip()
        if t:
            out.append(t)
    return sep.join(out)


def set_policy(conn, key, text=None, enabled=None, weight=None, source="manual") -> dict:
    """改一条（写 log 留痕、version+1）。只给 key 不改任何东西时返回现状。"""
    ensure(conn)
    row = conn.execute("SELECT key,enabled,text,weight,updated_ts,version FROM speech_policy WHERE key=?",
                       (key,)).fetchone()
    if not row:
        return {"ok": False, "error": "no_such_key", "key": key}
    old_text, old_en, old_w = row[2], int(row[1] or 0), float(row[3] or 0)
    new_text = old_text if text is None else str(text)
    new_en = old_en if enabled is None else (1 if enabled else 0)
    new_w = old_w if weight is None else max(0.0, min(1.0, float(weight)))
    if (new_text, new_en, new_w) == (old_text, old_en, old_w):
        return {"ok": True, "changed": False, "key": key}
    now = time.time()
    conn.execute(
        "UPDATE speech_policy SET text=?, enabled=?, weight=?, updated_ts=?, version=version+1 WHERE key=?",
        (new_text, new_en, new_w, now, key))
    conn.execute(
        "INSERT INTO speech_policy_log(key,old_text,new_text,enabled,weight,source,ts,version) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (key, old_text, new_text, new_en, new_w, source, now, int(row[5] or 1) + 1))
    conn.commit()
    return {"ok": True, "changed": True, "key": key, "enabled": new_en, "weight": new_w}


def listing(conn) -> list:
    return [{"key": r[0], "enabled": int(r[1] or 0), "weight": float(r[3] or 0),
             "version": int(r[5] or 1), "updated_ts": float(r[4] or 0),
             "chars": len(str(r[2] or "")), "text": r[2]} for r in _rows(conn)]


def log(conn, limit=20) -> list:
    try:
        rows = conn.execute(
            "SELECT id,key,enabled,weight,source,ts,version FROM speech_policy_log ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": r[0], "key": r[1], "enabled": r[2], "weight": r[3],
             "source": r[4], "ts": r[5], "version": r[6]} for r in rows]


def stats(conn) -> dict:
    rows = _rows(conn)
    return {"n": len(rows), "enabled": sum(1 for r in rows if int(r[1] or 0) == 1),
            "chars": len(render(conn)), "version": max([int(r[5] or 1) for r in rows] or [VERSION])}


def round_mode(pad: dict | None, query: str = "", intentions=None, motiv=None) -> str:
    """本轮倾向：闲聊 / 办事 / 观望 / 绷躁。只用于尾巴一行提示，不进稳定格。"""
    pad = pad or {}
    p = float(pad.get("p", 0.0) or 0.0)
    a = float(pad.get("a", 0.0) or 0.0)
    q = str(query or "")
    asks = any(w in q for w in ("帮我", "怎么", "步骤", "查一下", "搜一下", "为什么", "是什么", "能不能", "命令", "配一下"))
    hesitate = any(w in q for w in ("是不是", "要不要", "该不该", "行不行", "可以吗", "有点"))
    if p < -0.12 and a >= 0.15:
        return "绷躁"
    if asks:
        return "办事"
    if hesitate:
        return "观望"
    return "闲聊"


def mode_hint(mode: str) -> str:
    return _MODE_HINT.get(mode, "")
