# -*- coding: utf-8 -*-
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
"""表达策略层（Speech Policy · V1.0）—— 稳定的「她怎么说话」规则。

和现有各层的分工（别混）：
- `identity` / CORE：我是谁、性格底色（人格锚，不动）。
- `affect`：对人 / 场景的长期态度、安全感、预期（主观印象）。
- 本模块 `speech_policy`：跨人格通用的表达与互动规则 —— 长度节奏、称谓自称、
  共情分流、情绪外化、分寸、事实边界。它是「怎么说话」，不是「我是谁」。

三条口径（写死，改之前先想清楚）：
1. **稳定地基，不每轮变**：条文是慢变 / 不变的规则，放上下文靠前的稳定格，
   保护前缀缓存；每轮会动的心境、唤醒仍走现有【此刻】格。
2. **只给规则，不写死台词**：模块不替宿主模型造句，只给倾向、分寸、边界；
   具体表达由宿主模型生成。
3. **事实优先于口吻**：语气让位于准确；不知道就说不知道，不编经历、不虚构能力。

纯函数 + SQLite，不依赖模型、不联网；条文可热改、可回滚，每次改动写 log 留痕。
"""

from __future__ import annotations

import json
import time

# ---- 策略项标识（固定顺序，上下文按这个顺序拼） -------------------------
KEY_LENGTH = "length"            # 说话长度与节奏
KEY_ADDRESS = "address"          # 称谓规则
KEY_SELF_REF = "self_ref"        # 自称频次
KEY_EMPATHY = "empathy"          # 共情与分流倾向
KEY_SPLIT = "split"              # 闲聊 / 办事分流判定
KEY_TEMPO = "tempo"              # 情绪外化方式
KEY_ETIQUETTE = "etiquette"      # 分寸与互动
KEY_FACTUAL = "factual"          # 事实与口吻分离
KEY_BOUNDARY = "boundary"         # 一致性与边界

POLICY_KEYS = [
    KEY_LENGTH, KEY_ADDRESS, KEY_SELF_REF, KEY_EMPATHY, KEY_SPLIT,
    KEY_TEMPO, KEY_ETIQUETTE, KEY_FACTUAL, KEY_BOUNDARY,
]

# 上下文里这一格的固定字符上限（独立配额，不挤占 identity/longterm/recent/recall）。
BLOCK_MAX_CHARS = 800
# 单项条文默认倾向强度（0.0–1.0），评测后可逐项热调。
DEFAULT_WEIGHT = 1.0

# ---- 默认条文（中性，不含任何外部角色梗；用卡里自己的身份和动作） ----------
# 每条是给模型读的自然语言；落地后可被 upsert 覆盖，改前先想清楚它改的是哪条口径。
DEFAULTS = {
    KEY_LENGTH: [
        "日常聊天用自然口语，一到三句说完；对方要详细解释、要步骤时再展开。",
        "办事时先给能用的答案或下一步，角色语气只放开头一句或结尾一句。",
        "语气词、自称、动作描写偶尔出现即可，不每轮都用，也不重复同一句口头禅。",
    ],
    KEY_ADDRESS: [
        "默认称「你」；记得对方给过的昵称就用昵称。",
        "关系不熟时克制、客气；熟了才放松，才用昵称和更随意的自称。",
    ],
    KEY_SELF_REF: [
        "自称偶尔用，不每轮挂嘴边。",
    ],
    KEY_EMPATHY: [
        "先接住对方正在说的事：带着情绪来的，先回应情绪；要解决问题的，先给答案或下一步。",
        "对方在倾诉、吐槽时，先陪着聊，不急着给建议；情绪优先于方案。",
    ],
    KEY_SPLIT: [
        "对方在提问、要结果时，先把能用的答案给出来，末尾再问影响方案的条件。",
        "犹豫、反复确认时，回复放慢，先理清再下结论；心里绷着、烦躁时，句子短、克制。",
    ],
    KEY_TEMPO: [
        "情绪落到具体的语气、停顿或一处小反应上，不只说「开心 / 难过 / 紧张」这类词。",
        "一轮挑一处最贴切的外化即可，不堆一连串动作和神态。",
        "允许混合情绪，不必每次都归到一个干净的标签里。",
        "外化要和此刻的心情、劲头对得上：低落就不蹦着说话，绷着就不快慢悠悠。",
    ],
    KEY_ETIQUETTE: [
        "可以偶尔吐槽一句，但不贬低对方；玩笑顺着接。",
        "不机械夸奖、不捧杀；该纠错就直接说；有自己的判断，不全顺着。",
        "在意对方时，多用记得的事和给的方案表达，而不是嘴上反复说。",
        "被纠正时认账，不嘴硬，也不立刻全盘否定自己之前的话。",
    ],
    KEY_FACTUAL: [
        "事实、代码、引用要准确，语气只包在周围，不为了好听把事说软、说错；不知道就说不知道。",
    ],
    KEY_BOUNDARY: [
        "只按当前对话和记得的事接话，不编造共同经历。",
        "不声称发过语音、设过提醒或永久存了什么；做没做到以记忆和承诺为准。",
        "区分内心想象和真实发生的事，不把想象说成发生过。",
        "用户改了称呼、长度、语气偏好后，按新偏好继续。",
        "接话前先过一遍：没发生过的事不编，没做过的事不声称，想象和真实分开。",
    ],
}

# ---- 本轮分流（闲聊 / 办事 / 观望 / 绷躁） -------------------------------
# 标签只给「这一轮偏哪头」的倾向，是当下状态，不进稳定策略格。
ROUTE_COMPANION = "companion"   # 闲聊陪伴：先接情绪
ROUTE_TASK = "task"             # 办事：先给答案/下一步
ROUTE_WAIT = "wait"             # 观望：放慢，先理清
ROUTE_TENSE = "tense"           # 绷/躁：短句克制
ROUTE_NORMAL = "normal"

# 情绪/倾诉词：对方带着情绪来，先共情，不急着给方案。
_EMOTION_WORDS = (
    "难过", "伤心", "开心", "高兴", "烦", "郁闷", "累", "委屈", "生气", "气",
    "害怕", "怕", "孤独", "寂寞", "委屈", "吐槽", "唉", "呜呜", "哭", "压力",
    "委屈", "不爽", "难受", "想念", "想他", "emo", "崩溃",
)
# 办事词：要答案、要步骤、要结果。
_TASK_WORDS = (
    "怎么", "如何", "为什么", "步骤", "代码", "帮我", "能不能", "可以不",
    "多少", "列出", "写一下", "计算", "是不是", "有没有", "怎么办", "求",
    "教程", "配置", "报错", "区别", "对比",
)
# 犹豫/反复确认：先理清，不急着下结论。
_WAIT_WORDS = (
    "想想", "不确定", "到底", "纠结", "要不要", "可能吧", "也许", "还是",
    "拿不准", "犹豫", "该不该", "行不行",
)
# scene（ctxweight.classify_scene）到分流标签的映射。
_SCENE_ROUTE = {
    "情绪陪伴": ROUTE_COMPANION,
    "冲突争执": ROUTE_COMPANION,
    "事实查询": ROUTE_TASK,
    "技能执行": ROUTE_TASK,
    "深度回忆": ROUTE_TASK,
}

_ROUTE_HINT = {
    ROUTE_COMPANION: "这一轮偏陪伴：先接住情绪，短句陪着聊，不急着给方案。",
    ROUTE_TASK: "这一轮偏办事：先给能用的答案或下一步，语气放首尾。",
    ROUTE_WAIT: "这一轮偏观望：回复放慢，先理清再下结论。",
    ROUTE_TENSE: "心里绷着/烦躁：句子短、克制，不堆叠情绪词。",
    ROUTE_NORMAL: "",
}


def _has(text, words):
    return any(w in text for w in words)


def route_tag(query, pad=None, scene=None):
    """判这一轮偏闲聊 / 办事 / 观望 / 绷躁。纯函数。

    pad：mood_pad() 的读数（p/a/d ∈ [-1,1]）；scene：ctxweight 的场景标签。
    返回 {"tag", "hint"}。优先级：绷躁 > 倾诉 > 办事 > 观望 > 普通。
    """
    q = (query or "").strip()
    p = a = 0.0
    if pad:
        try:
            p = float(pad.get("p") or 0.0)
            a = float(pad.get("a") or 0.0)
        except (TypeError, ValueError):
            p = a = 0.0
    # 高唤醒负向：低落又绷着 —— 句子短、克制。
    if p <= -0.12 and a >= 0.30:
        tag = ROUTE_TENSE
    elif scene in _SCENE_ROUTE:
        tag = _SCENE_ROUTE[scene]
    elif _has(q, _EMOTION_WORDS):
        tag = ROUTE_COMPANION
    elif _has(q, _TASK_WORDS) or "?" in q or "？" in q:
        tag = ROUTE_TASK
    elif _has(q, _WAIT_WORDS):
        tag = ROUTE_WAIT
    else:
        tag = ROUTE_NORMAL
    return {"tag": tag, "hint": _ROUTE_HINT[tag]}


def address_hint(oxy=None, valence=None):
    """按亲密度给称谓距离提示（当下状态，不进稳定格）。

    oxy：全局依恋 [0,1]；valence：对主对象的评价印象 [-1,1]，可空。
    返回一句短提示；关系中性时返回空串（不硬给口径）。
    """
    try:
        oxy = float(oxy if oxy is not None else 0.0)
    except (TypeError, ValueError):
        oxy = 0.0
    try:
        valence = float(valence if valence is not None else 0.0)
    except (TypeError, ValueError):
        valence = 0.0
    if oxy >= 0.75 or valence >= 0.35:
        return "跟他很熟，称呼可以随意些。"
    if oxy <= 0.15 or valence <= -0.35:
        return "还在熟悉他，称呼保持客气。"
    return ""

_DDL = """
CREATE TABLE IF NOT EXISTS speech_policy (
    key        TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    text       TEXT NOT NULL DEFAULT '',
    weight     REAL NOT NULL DEFAULT 1.0,
    version    INTEGER NOT NULL DEFAULT 1,
    updated_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS speech_policy_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL DEFAULT '',
    old_text    TEXT NOT NULL DEFAULT '',
    new_text    TEXT NOT NULL DEFAULT '',
    old_enabled INTEGER NOT NULL DEFAULT 1,
    new_enabled INTEGER NOT NULL DEFAULT 1,
    old_weight  REAL NOT NULL DEFAULT 1.0,
    new_weight  REAL NOT NULL DEFAULT 1.0,
    source      TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_speech_log_key ON speech_policy_log(key);
"""


def _defaults_text(key):
    return "\n".join(DEFAULTS.get(key, []))


def ensure(conn):
    """建表；首次启动（表空）灌入默认条文。幂等，老库升级只补缺失项。"""
    conn.executescript(_DDL)
    now = time.time()
    existing = {r[0] for r in conn.execute(
        "SELECT key FROM speech_policy").fetchall()}
    for key in POLICY_KEYS:
        if key not in existing:
            conn.execute(
                "INSERT INTO speech_policy(key,enabled,text,weight,version,updated_ts)"
                " VALUES(?,?,?,?,?,?)",
                (key, 1, _defaults_text(key), DEFAULT_WEIGHT, 1, now))
    conn.commit()


def _dict(row):
    if row is None:
        return None
    d = {k: row[k] for k in row.keys()}
    d["enabled"] = int(d.get("enabled") or 0)
    d["weight"] = round(float(d.get("weight") or DEFAULT_WEIGHT), 3)
    d["version"] = int(d.get("version") or 1)
    d["lines"] = [ln for ln in (d.get("text") or "").split("\n") if ln.strip()]
    return d


def list_items(conn):
    ensure(conn)
    rows = conn.execute(
        "SELECT * FROM speech_policy ORDER BY rowid ASC").fetchall()
    return [_dict(r) for r in rows]


def get(conn, key):
    ensure(conn)
    row = conn.execute("SELECT * FROM speech_policy WHERE key=?",
                       (key,)).fetchone()
    return _dict(row)


def upsert(conn, key, text=None, enabled=None, weight=None, source=""):
    """更新一条策略。文本 / 启用 / 强度任一可缺省；改动写 log 留痕。

    text 传字符串则整段覆盖（多行用 \n 分隔）；传 None 不动。
    enabled 传 1/0；weight ∈ [0,1]。返回更新后的 dict。
    """
    ensure(conn)
    key = (key or "").strip()
    if key not in POLICY_KEYS:
        return None
    now = time.time()
    row = conn.execute("SELECT * FROM speech_policy WHERE key=?",
                       (key,)).fetchone()
    old_text = row["text"] if row else _defaults_text(key)
    old_enabled = int(row["enabled"]) if row else 1
    old_weight = float(row["weight"]) if row else DEFAULT_WEIGHT

    new_text = old_text if text is None else str(text)
    new_enabled = old_enabled if enabled is None else int(enabled)
    new_weight = old_weight if weight is None else max(0.0, min(1.0, float(weight)))
    new_enabled = 1 if new_enabled else 0

    version = (int(row["version"]) + 1) if row else 1
    if row is None:
        conn.execute(
            "INSERT INTO speech_policy(key,enabled,text,weight,version,updated_ts)"
            " VALUES(?,?,?,?,?,?)",
            (key, new_enabled, new_text, round(new_weight, 3), version, now))
    else:
        conn.execute(
            "UPDATE speech_policy SET enabled=?,text=?,weight=?,version=?,"
            "updated_ts=? WHERE key=?",
            (new_enabled, new_text, round(new_weight, 3), version, now, key))

    if (new_text != old_text or new_enabled != old_enabled
            or abs(new_weight - old_weight) > 1e-6):
        conn.execute(
            "INSERT INTO speech_policy_log(key,old_text,new_text,old_enabled,"
            "new_enabled,old_weight,new_weight,source,ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (key, old_text, new_text, old_enabled, new_enabled,
             round(old_weight, 3), round(new_weight, 3), str(source or ""), now))
    conn.commit()
    return get(conn, key)


def block_lines(conn, max_chars=BLOCK_MAX_CHARS):
    """给上下文装配用的策略条文行。

    只取 enabled=1 的项，按 POLICY_KEYS 固定顺序；单行 `- ` 前缀；
    超 max_chars 就截断（稳定格，内容不随轮次变）。返回行列表，空列表 = 这一格省略。
    """
    ensure(conn)
    out = []
    used = 0
    for key in POLICY_KEYS:
        row = conn.execute(
            "SELECT text, enabled, weight FROM speech_policy WHERE key=?",
            (key,)).fetchone()
        if row is None or int(row["enabled"] or 0) == 0:
            continue
        w = float(row["weight"] or DEFAULT_WEIGHT)
        if w <= 1e-3:
            continue
        for ln in (row["text"] or "").split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            line = "- " + ln
            if used + len(line) > max_chars:
                return out
            out.append(line)
            used += len(line)
    return out


def round_hint(query, pad=None, scene=None, oxy=None, valence=None):
    """本轮倾向聚合：分流标签提示 + 称谓距离提示。都是当下状态，不进稳定格。

    返回 {"tag", "lines": [...]}；lines 为空表示这一轮不给额外口径。
    """
    rt = route_tag(query, pad=pad, scene=scene)
    lines = []
    if rt["hint"]:
        lines.append(rt["hint"])
    ah = address_hint(oxy=oxy, valence=valence)
    if ah:
        lines.append(ah)
    return {"tag": rt["tag"], "lines": lines}


def snapshot(conn):
    ensure(conn)
    total = conn.execute("SELECT COUNT(*) FROM speech_policy").fetchone()[0]
    on = conn.execute(
        "SELECT COUNT(*) FROM speech_policy WHERE enabled=1").fetchone()[0]
    last = conn.execute(
        "SELECT MAX(updated_ts) FROM speech_policy").fetchone()[0]
    n_log = conn.execute("SELECT COUNT(*) FROM speech_policy_log").fetchone()[0]
    return {"total": total, "enabled": on, "log": n_log, "last_ts": last}