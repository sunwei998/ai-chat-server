"""管理端：用户提问高频词（只读分析）。

数据源：messages(role='user') JOIN sessions；分词 jieba；过滤 停用词+敏感词+长度/纯标点数字。
鉴权：require_admin（与 admin.py 一致）。缓存：进程内 TTL。

注意：本模块**不调用任何大模型**，纯本地 jieba + Counter 计数，零 API 成本。
"""
import logging
import re
import threading
from collections import Counter

import jieba
from fastapi import APIRouter, Depends, HTTPException, Query

from .admin import _PERIODS
from .auth import require_admin
from .db import fetch_all, now_ms

jieba.setLogLevel(logging.WARNING)

router = APIRouter(
    prefix="/api/admin",
    tags=["hot-words"],
    dependencies=[Depends(require_admin)],
)

# 精简中文 + 常见英文停用词，过滤无意义高频词。后续可按实际榜单持续扩充。
STOPWORDS: set[str] = {
    "的", "了", "和", "是", "在", "我", "有", "也", "就", "不", "人", "都", "一", "一个", "上", "来", "到",
    "时", "大", "地", "为", "子", "中", "你", "说", "生", "国", "年", "着", "那", "要", "下", "以", "得",
    "于", "他", "或", "把", "被", "让", "与", "及", "等", "这", "哪", "吗", "呢", "吧", "啊", "怎么",
    "什么", "怎样", "如何", "为什么", "可以", "需要", "请问", "帮我", "给我", "我想", "我们", "你们",
    "他们", "自己", "这个", "那个", "一些", "这些", "那些",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are", "i", "you", "we", "they", "it",
    "my", "your", "this", "that", "how", "what", "why", "can", "please", "me", "so", "do", "does", "be",
    "for", "with", "as", "at", "by", "from",
}

# TODO: 接入正式敏感词库 / 迁移到 settings 表。当前消息中已出现辱骂类原文，必须过滤。
SENSITIVE_WORDS: set[str] = set()

_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z]")

_CACHE_TTL_MS = 5 * 60 * 1000
_cache: dict[tuple, tuple[int, list[dict]]] = {}
_cache_lock = threading.Lock()


def _tokenize_and_filter(text: str) -> list[str]:
    out: list[str] = []
    for tok in jieba.lcut(text):
        tok = tok.strip().lower()
        if not tok or len(tok) < 2:
            continue
        if not _WORD_RE.search(tok):
            continue
        if tok in STOPWORDS or tok in SENSITIVE_WORDS:
            continue
        out.append(tok)
    return out


def _compute(period: str, limit: int) -> list[dict]:
    start = now_ms() - _PERIODS[period]
    rows = fetch_all(
        """
        SELECT m.content
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.role = 'user' AND m.created_at >= ?
        """,
        (start,),
    )
    counter: Counter = Counter()
    for r in rows:
        content = r["content"] or ""
        if content:
            counter.update(_tokenize_and_filter(content))
    return [{"word": w, "count": c} for w, c in counter.most_common(limit)]


@router.get("/hot-words")
def hot_words(
    period: str = Query("month", description="day/week/month/year"),
    limit: int = Query(20, ge=1, le=200, description="返回 Top N"),
):
    if period not in _PERIODS:
        raise HTTPException(status_code=400, detail="period 仅支持 day/week/month/year")
    key = (period, limit)
    now = now_ms()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_MS:
            return hit[1]
    result = _compute(period, limit)
    with _cache_lock:
        _cache[key] = (now, result)
    return result
