# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
# ---------------------------------------------------------------------------
"""把「预期误差」接到「身体」上 —— 认知层 × 生理层。

两个来源合成一处：

1) `synapse_emotion`（认知层）
   中文线索 → 价值分 V_actual → 每个场景自己的预期 V_expected
   → RPE = V_actual − V_expected → 五条调质按增益注入
   自带：脱敏（同一套路反复用会腻）、疲劳（累的时候奖赏变钝）、
   内源情绪（与对话无关的起伏）、安全门（不安全时亲密通路关死）、
   基线漂移 + 强制回归（否则 CORT 会像棘轮一样只涨不落）。

2) `viscera`（生理层）
   24 种激素 + 交感/副交感 + 心率呼吸体温脸色，各自有小时级半衰期，
   带负反馈与拮抗。

接法：认知层算出的「这一轮的脉冲」只当刺激量交给 `viscera.inject()`，
半衰期和负反馈仍归生理层管。于是两种时间尺度同时成立 ——
RPE 是这一秒的事，回落到基线是几小时的事。

三条硬规矩（来自 SYNAPSE 文档）：
- 数值永远不进模型，只给 `describe()` 那种人话。
- 情绪块每轮重算，不缓存、不写进历史。
- 安全掉到 0.25 以下就禁止暧昧试探，不许绕过。
"""

from __future__ import annotations

import os

try:                                             # 认知层是可选的
    from synapse_emotion import EmotionEngine, estimate_appraisal, render_compact
    _HAVE_CORE = True
except Exception:                                # pragma: no cover
    EmotionEngine = None                         # type: ignore
    estimate_appraisal = None                    # type: ignore
    render_compact = None                        # type: ignore
    _HAVE_CORE = False

import viscera

# 场景键 → 触发词。只用来给 TD 预期分档，猜错也只是分错档，不会把人判成恶意。
_SCENES = (
    ("apology", ("对不起", "抱歉", "我错了", "别生气", "原谅我")),
    ("reject",  ("滚", "不想理你", "别烦我", "讨厌你", "不要你了", "分手",
                 # 2026-11-20：骂人不能只认「滚」。「你真笨/蠢/废物/做不好」这些
                 # 以前落进 generic，RPE 反而是正的 —— 被骂还高兴了一点。
                 "笨", "蠢", "傻", "白痴", "废物", "没用", "垃圾", "恶心",
                 "有病", "神经病", "做不好", "瞎了眼", "失望", "没出息",
                 "闭嘴", "烦死了", "别管我", "不想看到你")),
    ("jealous", ("她是谁", "那个女生", "跟谁", "别人家的", "你是不是喜欢")),
    ("comfort", ("没事的", "别怕", "我在", "抱抱", "辛苦了", "休息一下")),
    ("flirt",   ("可爱", "好看", "想你", "喜欢你", "爱你", "心动", "亲", "抱")),
    ("praise",  ("厉害", "真棒", "做得好", "聪明", "谢谢你", "夸")),
    ("neglect", ("随便吧", "你忙你的", "嗯。", "哦。")),
    ("tired",   ("好累", "累死", "困了", "撑不住", "熬夜", "没力气")),
)


# 生理刺激名 → 认知场景。
# 手动戳身体（控制台/测试/将来「看见一张图」）时没有文本可评估，
# 但两边得说同一种话，否则曲线上的「心率」和「主导情绪」会各讲各的。
STIM_SCENARIO = {
    "praise": "praise", "caress": "flirt", "tease": "flirt",
    "comfort": "comfort", "apology": "apology", "reject": "reject",
    "criticize": "reject", "cold": "neglect", "boredom": "neglect",
    "worry": "neglect", "night": "tired", "surprise": "generic",
    "curiosity": "generic", "chat": "generic",
}


# 生理刺激名 → 一句代表性的原话。
# 认知层是「看话打分的」，只给场景名不给话，它看到的是空字符串：
# 空话在它眼里接近冷淡，于是「夸她」反而把安全感拉低。
STIM_TEXT = {
    "praise": "你今天真厉害，谢谢你",
    "caress": "过来让我抱抱你",
    "tease": "你怎么这么可爱啊",
    "cheer": "别怕，我在",
    "comfort": "没事的，辛苦了，休息一下",
    "apology": "对不起，是我错了，别生气",
    "reject": "滚，别烦我，不想理你",
    "criticize": "你怎么这么笨",
    "cold": "随便吧，你忙你的",
    "boredom": "哦。",
    "worry": "你怎么还不回我",
    "night": "好累，困了，想睡了",
    "surprise": "诶，居然是这样",
    "curiosity": "这个是怎么做的呀",
    "chat": "今天过得怎么样",
}


def guess_scenario(text: str) -> str:
    """粗分场景。分不出就是 generic。"""
    t = (text or "").strip()
    if not t:
        return "neglect"
    for name, words in _SCENES:
        for w in words:
            if w in t:
                return name
    return "generic"



# —— 伤害闸（2026-11-19，V9-P0）——
import re as _re
# 光看场景会误判：一句「对不起，滚，别烦我」被判 apology，暖意照样加满。
# 这里把「这句话有没有伤到我」单独算一遍，暖意与多巴胺下限都乘 (1-h)；
# h≥0.2 时「道歉」不算有效修复（嘴上说对不起、下一句还在骂 = 不认账）。
_HARM_WORDS = (
    "滚", "闭嘴", "烦死了", "别烦", "别管我", "蠢", "废物", "垃圾", "没用",
    "讨厌你", "不想理你", "你怎么这么", "恶心", "恶心死", "有病", "神经病",
    "笨", "傻", "白痴", "做不好", "瞎了眼", "没出息", "失望", "不想看到你",
)
_COLD_WORDS = ("随便", "无所谓", "爱怎样怎样", "不重要", "算了", "没什么好说的")
_APOLOGY_WORDS = ("对不起", "抱歉", "我错了", "是我的错", "我不该", "原谅我")
_ACCOUNT_WORDS = ("是我", "我改", "我会", "我下次", "我来", "我的锅")


def _harm_of(text: str, scenario: str = "") -> float:
    """0~1：这句话的伤害强度。只做最粗的词面判断，宁可少判不要误判。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    h = 0.0
    if any(w in t for w in _HARM_WORDS):
        h = 0.6
    if any(w in t for w in _COLD_WORDS) and not h:
        h = 0.25
    if h and _re.search(r"[!！]{1,}|你(给|滚)|永远|再也", t):
        h = min(1.0, h + 0.2)
    return h


def _repair_of(text: str, scenario: str = "") -> float:
    """0~1：这句话的修复诚意（认错 × 有担责 × 有后续）。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    ap = 1.0 if (scenario == "apology" or any(w in t for w in _APOLOGY_WORDS)) else 0.0
    if not ap:
        return 0.0
    acc = 1.0 if any(w in t for w in _ACCOUNT_WORDS) else 0.55
    follow = 1.0 if _re.search(r"(下次|以后|明天|待会|我现在就)", t) else 0.7
    return max(0.0, min(1.0, ap * acc * follow))


def pulse_of(res, fatigue=0.0, night=False, text=""):
    """把一轮的调质脉冲翻成 (激素增量, 交感增量, 副交感增量)。

    认知层只给五条（DA/NE/OT/5HT/CORT），但身体里有 24 条。这里补的是
    **教科书级的下游联动**（不是拟合出来的系数，所以增益都压得很小）：

        DA↑ → dop↑（外周前体）、prl↓（结节漏斗 DA 对催乳素是张力性抑制）、orex↑
        NE↑ → adeno↑（交感耗能→腺苷累积）、npy↑（交感共递质）、adh↑（应激抗利尿）
        CORT↑ → kyn↑（色氨酸转向犬尿氨酸，5-HT 合成被挤掉）、enk↑（应激镇痛补偿）、
                 orex↓（应激压食欲）、testo↓（应激压性腺）
        OT↑ → prl↑（吸吮/亲密共释放）、gaba↑（安全感/松弛）
        5HT↑ → mel↑（只在夜里；白天 5-HT 不会变成褪黑素）
        失落（DA↓）→ cck↑（饱足/抑制）、hist↑（脸热）
        疲劳 → adeno↑、orex↓

    一句话不该把人直接推到顶：单轮增量先封在 ±0.5（认知层自己的容差是 ±1，
    留给它的分类用），进身体再乘 INJECT_SCALE。
    """
    p = getattr(res, "pulsed", None) or {}

    def cap(v):
        return max(-0.5, min(0.5, float(v or 0.0)))

    da = cap(p.get("DA", 0.0))
    ne = cap(p.get("NE", 0.0))
    ot = cap(p.get("OT", 0.0))
    se = cap(p.get("5HT", 0.0))
    co = cap(p.get("CORT", 0.0))
    up = lambda v: max(0.0, v)      # noqa: E731
    down = lambda v: max(0.0, -v)   # noqa: E731
    try:
        fat = max(0.0, min(1.0, float(fatigue or 0.0)))
    except Exception:
        fat = 0.0

    # 暖意通道：被夸、被安慰、被靠近时，光靠 5 条调质算不出「心里软下来」。
    # 这一项是直接加在催产素上的（带一点催乳素与 GABA），场景由认知层给。
    _warm_scene = str(getattr(res, "scenario", "") or "") in (
        "praise", "comfort", "flirt", "apology")
    # 伤害闸：这句话本身有伤害时，场景判成「夸/安慰/道歉」也不加热。
    _h = _harm_of(text, str(getattr(res, "scenario", "") or ""))
    _r = _repair_of(text, str(getattr(res, "scenario", "") or ""))
    if _h >= 0.20:
        _r = 0.0            # 嘴上认错不算修复
    _gate = max(0.0, 1.0 - _h)
    warm = (0.18 + 0.16 * _r) * _gate if (_warm_scene and _gate > 0) else 0.0
    # 11-17：期望学满之后，一句「你今天真好」的 RPE 会掉到 0.05 以下 ——
    # 不该因为「我早就知道他会夸我」就一点暖意都没有。给一条下限。
    WARM_CARRY = 0.16
    if _warm_scene:
        warm = max(warm, WARM_CARRY)

    deltas = {
        # —— 五条主调质，直连 ——
        # dopa 下限也过闸：被骂的时候不该还有「被夸」的下限撑着
        "dopa": (max(da, 0.12) if (_warm_scene and _h < 0.20) else da),
        "nora": ne,
        "sero": se,
        "cort": co,
        "oxy": cap(max(ot, 0.0) + warm),
        # —— 下游联动（增益 0.10~0.35，压得很轻）——
        "dop": 0.25 * up(da),
        "prl": 0.30 * down(da) + 0.20 * up(ot) + 0.20 * warm,
        "orex": 0.20 * up(da) - 0.25 * up(co) - 0.20 * fat,
        "adeno": 0.18 * up(ne) + 0.22 * fat,
        "npy": 0.20 * up(ne),
        "adh": 0.15 * up(co),
        "kyn": 0.30 * up(co),
        "enk": 0.22 * up(co),
        "testo": 0.15 * down(co),
        "gaba": 0.22 * up(ot) - 0.10 * up(ne) + 0.15 * warm,
        "cck": 0.20 * down(da),
        # 唤醒起来时脸会热：紧张、失落都会
        "hist": 0.30 * up(ne) + 0.15 * down(da),
    }
    if night:
        deltas["mel"] = 0.25 * up(se)

    rpe = 0.0
    try:
        rpe = abs(float(getattr(res, "rpe", 0.0) or 0.0))
    except Exception:
        rpe = 0.0
    sym = min(1.0, 0.85 * up(ne) + 0.45 * up(co) + 0.15 * rpe)
    para = min(1.0, 0.45 * up(ot) + 0.35 * down(co) + 0.10 * up(se))
    return deltas, sym, para


class EmotionLink:
    """一张卡一份情绪状态。状态写在卡目录里，跟着卡走。"""

    def __init__(self, root: str, name: str = "xiyin"):
        # 环境开关：WHALE_EMOTION_CORE=0 时退回「从话里猜刺激」那条老路。
        # 留这个开关不是因为不信它，是因为它一坏就会连着身体一起坏。
        off = os.environ.get("WHALE_EMOTION_CORE", "1").strip().lower() in ("0", "false", "off", "no")
        self.available = bool(_HAVE_CORE) and not off
        self.eng = None
        if not self.available:
            return
        state_dir = os.path.join(root, "qq_state", "emotion")
        try:
            os.makedirs(state_dir, exist_ok=True)
            self.eng = EmotionEngine(name=name, state_dir=state_dir, autosave=True)
        except Exception:
            self.eng = None
            self.available = False

    # ---------------------------------------------------------------- 主入口
    def step(self, text: str, scenario: str = "", now=None):
        """一句话进来 → 一轮结算。返回 TurnResult，失败返回 None。"""
        if not self.eng:
            return None
        try:
            # 🔧 2026-11-23（第五件：「无对话不衰减」）：
            #    这一行是补上「真实时间流逝」的唯一入口。
            #    原来 `eng.step(ap)` 的 dt 用默认 1.0 —— 隔三天再说话也只衰减 1 轮，
            #    于是状态文件停在上次说话那一刻（实测停在 11-22 15:37、turn=147、fatigue=0.921）。
            #    `engine.py` 里那个 `apply_wallclock_gap()` 本来就是为这件事写的，写了却没人调。
            #    ⇒ 在这里补一次「一次性补算」：按距上次的真实时长算出该衰减多少轮，一把补完。
            #    ⚠ 不要在这里 save()：`eng.step()` 末尾会 save，那时才会更新 last_ts。
            #      如果这里先 save，会把「补算后的状态」按「旧 last_ts」落盘，下一轮又补一次。
            self.eng.apply_wallclock_gap()
            sc = scenario or guess_scenario(text)
            ap = estimate_appraisal(text or "", scenario=sc)
            self._last_text = text or ""     # 供 apply_to 过伤害闸
            # 2026-11-20： appraisal 只认它自己词表里的词 —— 「你真是笨得要死，
            # 什么都做不好，我真是瞎了眼才把你做出来」只拿到 attention，threat/cold
            # 都是 0，V_actual 还是正的，于是被骂反而舒服了一点（rpe +0.033）。
            # 这里用我们家自己的伤害闸兜底：认出来伤人了，就把威胁/寒意顶上去，
            # 让「先不高兴，再回话」这件事在情绪层真的发生。
            try:
                _h = _harm_of(text or "", sc)
            except Exception:
                _h = 0.0
            if _h >= 0.4:
                _t = min(1.0, 0.5 + 0.5 * _h)
                _c = 0.3
                try:
                    ap.threat = max(float(getattr(ap, "threat", 0.0) or 0.0), _t)
                    ap.cold = max(float(getattr(ap, "cold", 0.0) or 0.0), _c)
                except Exception:
                    pass
            return self.eng.step(ap)
        except Exception:
            return None

    def preactivate(self, scenario: str = "generic", predicted=None, intensity: float = 0.5):
        """她要先开口之前调一次 —— 鼓起勇气本身就带一点忐忑。"""
        if not self.eng:
            return None
        try:
            self.eng.preactivate(scenario, predicted=predicted, intensity=intensity)
            return True
        except Exception:
            return None

    # ---------------------------------------------------------------- 输出
    def inject_rpe(self, rpe, scenario="generic", intensity=1.0):
        """V9.7：把「已经算好的落差」直注进情绪引擎（不新建 Appraisal）。

        只动引擎内部状态与预期（qq_state/emotion/xiyin.json），
        不动内脏那 24 种激素（那是 hormone.apply_r 的活）。两个仓库，不重复记账。
        """
        if self.eng is None:
            return None
        try:
            return self.eng.inject_rpe(float(rpe), scenario=str(scenario or "generic"),
                                       intensity=float(intensity))
        except Exception as e:
            print("[emotion_link] inject_rpe 失败：%s" % e)
            return None

    def apply_to(self, state, res, dt_hours: float = 0.0, now=None):
        """把脉冲灌进身体状态。res 为 None 时只做时间衰减。"""
        s = viscera.step(state, dt_hours, ())
        if res is None:
            return s
        fat = 0.0
        try:
            fat = float(getattr(self.eng, "fatigue", 0.0) or 0.0)
        except Exception:
            fat = 0.0
        night = False
        try:
            import time as _t
            hh = _t.localtime(now if now is not None else _t.time()).tm_hour
            night = hh >= 22 or hh < 6
        except Exception:
            night = False
        deltas, sym, para = pulse_of(res, fatigue=fat, night=night,
                                      text=getattr(self, "_last_text", ""))
        return viscera.inject(s, deltas, sym, para)

    def label(self, res=None) -> str:
        """给人看的一句：主导感受 + 强度档位。永远不带数字。"""
        if not self.eng:
            return ""
        try:
            r = res or self.eng.classify()
            if res is None:
                name = r[0][0] if r else "calm"
                score = r[0][1] if r else 0.0
            else:
                name, score = res.dominant, float(res.intensity or 0.0)
            from synapse_emotion import EMOTION_LABELS
            word = EMOTION_LABELS.get(name, name)
        except Exception:
            return ""
        if score >= 0.72:
            level = "很明显"
        elif score >= 0.52:
            level = "压不太住"
        else:
            level = "淡淡的"
        return "%s（%s）" % (word, level)

    def compact(self) -> str:
        """省上下文用的一行。"""
        if not self.eng or not render_compact:
            return ""
        try:
            return render_compact(self.eng.classify()) if callable(render_compact) else ""
        except Exception:
            return ""

    def safe(self) -> float:
        if not self.eng:
            return 1.0
        try:
            return float(self.eng.safety)
        except Exception:
            return 1.0

    def snapshot(self):
        if not self.eng:
            return {}
        try:
            snap = self.eng.snapshot()
            snap["label"] = self.label()
            return snap
        except Exception:
            return {}

    def save(self):
        if self.eng:
            try:
                self.eng.save()
            except Exception:
                pass
