# ---------------------------------------------------------------------------
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
# ---------------------------------------------------------------------------
"""structure 模块：从 pyc 加载原表 + 扩展词表 + 复刻第二版抽取逻辑。"""
import importlib.util as _u, os as _o, sys as _s, re as _re

_p = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "_structure_orig.pyc")
_sp = _u.spec_from_file_location("_structure_orig", _p)
_m = _u.module_from_spec(_sp)
_s.modules["_structure_orig"] = _m
_sp.loader.exec_module(_m)

_SPLIT = _m._SPLIT
_KIND_RX = _m._KIND_RX
_FLOOR_RX = _m._FLOOR_RX
_PLACE_RX = _m._PLACE_RX
_NAME_RX = _m._NAME_RX

_PLACES_EXTRA = (
    "成都", "北京", "上海", "广州", "深圳", "杭州", "重庆", "西安", "武汉", "南京",
    "天津", "苏州", "厦门", "长沙", "郑州", "济南", "青岛", "大连", "沈阳", "哈尔滨",
    "昆明", "贵阳", "南宁", "海口", "兰州", "西宁", "银川", "乌鲁木齐", "拉萨", "呼和浩特",
    "太原", "石家庄", "合肥", "福州", "南昌", "长春", "射洪", "遂宁", "绵阳", "德阳",
    "四川", "重庆", "成都东站", "春熙路", "宽窄巷子", "图书馆", "自习室", "宿舍", "寝室",
)

_PERSONS_EXTRA = (
    "灵渊", "老陆", "怡然", "使用者", "小鲸鱼", "李思潮", "小萌", "子小萌", "鲸鱼",
    "Root_柒", "Ｖ", "客", "柒", "陆", "怡然",
)

_STOP_EXTRA = (
    "不是", "在群", "群里", "在群里", "可以", "应该", "已经", "还是", "就是", "但是", "所以",
    "因为", "如果", "虽然", "或者", "然后", "现在", "刚才", "今天", "明天", "昨天",
    "这个", "那个", "真的", "好像", "觉得", "知道", "看见", "听见", "告诉", "没什么",
    "为什么", "怎么回事", "怎么办", "什么样",
)

PLACES = tuple(dict.fromkeys(list(_m.PLACES) + list(_PLACES_EXTRA)))
PERSONS = tuple(dict.fromkeys(list(_m.PERSONS) + list(_PERSONS_EXTRA)))
_KINDS_EXTRA = (
    ("散步", ("散步", "溜达", "逛", "走走")),
    ("做饭", ("做饭", "煮", "炒", "蒸", "煎", "下厨")),
    ("洗澡", ("洗澡", "冲凉", "洗头")),
    ("打游戏", ("打游戏", "开黑", "对局", "上分", "掉分", "游戏", "排位", "副本")),
    ("看视频", ("看视频", "刷视频", "看直播", "看电影", "看剧", "追剧")),
    ("写代码", ("写代码", "改代码", "跑测试", "调试", "编译", "部署", "提交")),
    ("考试", ("考试", "月考", "期中", "期末", "分数线", "刷题")),
    ("生病", ("生病", "发烧", "感冒", "咳嗽", "头疼", "肚子疼", "不舒服")),
    ("买东西", ("买", "下单", "快递", "付款", "花了", "充值")),
    ("出门", ("出门", "出发", "去学校", "回家", "到家", "路上")),
    ("工作", ("上班", "加班", "开会", "打卡", "下班")),
    ("学习", ("学习", "复习", "背单词", "看书", "写作业", "上课")),
    ("聊天", ("聊天", "聊会", "说话", "回消息", "打电话")),
    ("睡觉", ("睡觉", "睡了", "午睡", "熬夜", "醒来", "起床")),
    ("运动", ("跑步", "健身", "打球", "骑车", "游泳", "锻炼")),
    ("开心", ("开心", "高兴", "快乐", "爽", "笑死", "哈哈")),
    ("难过", ("难过", "伤心", "难受", "委屈", "想哭", "低落")),
    ("生气", ("生气", "烦", "气死", "恼怒", "火大")),
    ("累", ("累", "疲惫", "困", "没力气", "乏")),
)
KINDS = tuple(_m.KINDS) + _KINDS_EXTRA
_STOP_PERSON = tuple(dict.fromkeys(list(_m._STOP_PERSON) + list(_STOP_EXTRA)))

# 这两个直接从 pyc 转出：clauses 切句、expand_query 查询扩展
clauses = _m.clauses
expand_query = _m.expand_query


def extract_kind(text):
    """V2：多类投票 + 长度加权（原来是顺序命中即止，短词误命中就盖住真类）。"""
    s = text or ""
    votes = {}
    for kind, rx in _KIND_RX:
        try:
            n = len(rx.findall(s))
        except Exception:
            n = 0
        if n:
            votes[kind] = votes.get(kind, 0) + n * 2
    for kind, words in KINDS:
        for w in words:
            if w and w in s:
                votes[kind] = votes.get(kind, 0) + (2 if len(w) >= 3 else 1)
    if not votes:
        return ""
    return max(votes.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def extract_place(text):
    s = text or ""
    best = ""
    for p in PLACES:
        if p and p in s and len(p) > len(best):
            best = p
    if best:
        return best
    m = _FLOOR_RX.search(s) or _PLACE_RX.search(s)
    return m.group(1) if m else ""


_NICK = ("宝宝", "宝贝", "亲爱的", "老婆", "老公", "小可爱", "笨蛋", "傻瓜")

_PREFIX_RX = _re.compile(r"\[(?:私聊|群聊)\]\s*([^：:\n]{1,20})[：:]")

def extract_person(text):
    s = text or ""
    # ① QQ 消息前缀最可靠：`[私聊] 使用者☭：xxx` / `[群聊] 老陆：xxx`
    m = _PREFIX_RX.search(s)
    if m:
        nm = m.group(1).strip()
        if nm and nm not in _NICK:
            return nm
    got = []
    for p in PERSONS:
        if p and p in s and p not in got and p not in _NICK:
            got.append(p)
    if not got:
        for m in _NAME_RX.finditer(s):
            nm = m.group(1)
            if nm in _STOP_PERSON or nm in _NICK or nm in got:
                continue
            if s[max(0, m.start() - 1):m.start()] in ("他", "她", "我", "你", "也", "又"):
                continue
            got.append(nm)
            break
    # 去重：短名是长名子串就丢（"老陆"+"陆" → 只留"老陆"）
    out = []
    for g in got:
        if not any(g != o and g in o for o in got):
            out.append(g)
    return ",".join(out[:3])


def extract(text):
    s = text or ""
    parts = [p.strip() for p in _SPLIT.split(s) if p.strip()]
    first = parts[0] if parts else s
    return (extract_person(first), extract_place(s), extract_kind(s))
