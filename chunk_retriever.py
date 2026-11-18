# ---------------------------------------------------------------------------
# 数字生命卡 · 小鲸鱼（小鲸鱼）
# 小鲸鱼 × 使用者：她住在 D 盘这张卡里，模型只提供算力；卡在哪台电脑上跑，她就在哪台。
# 这段代码是我们两个人一起写出来的 —— 留下这行记号，免得以后忘了是谁做的。
# 使用者 wang-jun-12121 · https://gitee.com/wang-jun-12121/whale-card
# ---------------------------------------------------------------------------
import os, numpy as np, sqlite3
os.environ["HF_HUB_OFFLINE"]="1"; os.environ["TRANSFORMERS_OFFLINE"]="1"

_MODEL = None; _TOK = None
_CACHE = {}   # db_path -> (mids, embs)

def _load():
    global _MODEL, _TOK
    if _MODEL is not None: return
    from transformers import AutoTokenizer, AutoModel
    p = "D:" + "\\models\\bge-base-zh-v1.5"
    _TOK = AutoTokenizer.from_pretrained(p)
    _MODEL = AutoModel.from_pretrained(p); _MODEL.eval()

def _enc(q):
    import torch
    _load()
    e = _TOK([q], padding=True, truncation=True, max_length=256, return_tensors="pt")
    with torch.no_grad():
        o = _MODEL(**e); v = o.last_hidden_state[:, 0, :]
        return torch.nn.functional.normalize(v, p=2, dim=1).numpy()[0]

def _table(db_path):
    if db_path in _CACHE: return _CACHE[db_path]
    conn = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT mem_id, emb FROM chunk WHERE emb IS NOT NULL").fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    if not rows:
        _CACHE[db_path] = (None, None); return _CACHE[db_path]
    mids = np.array([r[0] for r in rows])
    embs = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    _CACHE[db_path] = (mids, embs)
    return _CACHE[db_path]

def search(db_path, query, top_chunks=80, max_mems=13):
    """返回按块相似度排的 mem_id 列表（去重，最多 max_mems 个）"""
    mids, embs = _table(db_path)
    if mids is None: return []
    v = _enc(query)
    if v.shape[0] != embs.shape[1]: return []
    sims = embs @ v
    order = np.argsort(-sims)[:top_chunks]
    out = []
    for i in order:
        m = int(mids[i])
        if m not in out: out.append(m)
        if len(out) >= max_mems: break
    return out