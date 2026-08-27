"""POST /api/chat：转发模型流式响应（SSE），并记录 token 用量。

安全性：model 必须在 models 表中且 enabled；消息条数/长度做基础校验。
API key 只在服务端持有，不会下发给浏览器。
"""

import json
import logging
import re
import secrets
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import ROLE_USER, get_current_user
from .config import settings
from .db import execute, fetch_one, now_ms, transaction
from .limiter import check_user_limit
from .schemas import ChatMessage, ChatRequest
from .websearch import web_search

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)

MAX_MESSAGES = 40
MAX_CONTENT_LEN = 32_000
TITLE_LEN = 30


def _auto_title(content: str) -> str:
    text = " ".join(content.split())
    return text[:TITLE_LEN] if text else "新对话"


def _persist_user_message(user_id: int, session_id: str, msg: ChatMessage, msg_id: str) -> None:
    execute(
        "INSERT OR IGNORE INTO messages (id, session_id, role, content, images, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, session_id, msg.role, msg.content, json.dumps(msg.images), now_ms()),
    )


def _persist_assistant(
    user_id: int,
    session_id: str,
    content: str,
    assistant_msg_id: str,
    images: list[str],
    citations: list | None = None,
) -> None:
    """流结束后把助手消息与会话元数据在同一事务写入；标题为空时用首条用户消息自动生成。"""
    now = now_ms()
    citations = citations or []
    queries: list[tuple[str, list]] = []
    existing = fetch_one(
        "SELECT id FROM messages WHERE id = ? AND session_id = ?",
        (assistant_msg_id, session_id),
    )
    if existing:
        queries.append(
            (
                "UPDATE messages SET content = ?, images = ?, citations = ? WHERE id = ? AND session_id = ?",
                [content, json.dumps(images), json.dumps(citations), assistant_msg_id, session_id],
            )
        )
    elif not content:
        return
    else:
        queries.append(
            (
                "INSERT INTO messages (id, session_id, role, content, images, citations, created_at)"
                " VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
                [assistant_msg_id, session_id, content, json.dumps(images), json.dumps(citations), now],
            )
        )
    session = fetch_one(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    )
    if not session:
        return
    queries.append(("UPDATE sessions SET updated_at = ? WHERE id = ?", [now, session_id]))
    if not session["title"]:
        first_user = fetch_one(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user'"
            " ORDER BY created_at, id LIMIT 1",
            (session_id,),
        )
        if first_user:
            queries.append(
                ("UPDATE sessions SET title = ? WHERE id = ?", [_auto_title(first_user["content"]), session_id])
            )
    transaction(queries)


def _get_model(model_key: str) -> dict:
    row = fetch_one(
        "SELECT * FROM models WHERE model_key = ? AND enabled = 1", (model_key,)
    )
    if not row:
        raise HTTPException(status_code=400, detail=f"模型不可用: {model_key}")
    return dict(row)


def _check_model_access(user: dict, model: dict) -> None:
    """普通用户只能使用免费模型；订阅用户与管理员可用全部模型。"""
    if user["role"] == ROLE_USER and not model["free"]:
        raise HTTPException(status_code=403, detail="普通用户无法使用付费模型，请升级订阅")


def _endpoint(model: dict) -> str:
    base = settings.siliconflow_base_url.rstrip("/")
    if model["provider"] == "ollama":
        return f"{base}/api/chat"
    return f"{base}/chat/completions"


def _build_payload(model: dict, body: ChatRequest) -> dict:
    if model["provider"] == "ollama":
        messages = [
            {"role": m.role, "content": m.content}
            for m in body.messages
        ]
        return {"model": model["model_key"], "messages": messages, "stream": True}
    messages = []
    for m in body.messages:
        if m.images:
            content: list = [{"type": "text", "text": m.content}]
            for url in m.images:
                content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": m.role, "content": content})
        else:
            messages.append({"role": m.role, "content": m.content})
    return {
        "model": model["model_key"],
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _headers(model: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if model["provider"] != "ollama":
        headers["Authorization"] = f"Bearer {settings.siliconflow_api_key}"
    return headers


def _extract_delta(line: str) -> str | None:
    """从 SSE 的 data 行中提取增量文本（OpenAI 兼容流式块）。"""
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
    content = delta.get("content")
    return content if isinstance(content, str) else None


def _extract_usage(line: str) -> dict | None:
    """从 SSE 的 data 行中解析 usage（OpenAI 流式最后一块带 usage）。"""
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = obj.get("usage")
    if not usage:
        return None
    if isinstance(usage, dict):
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return None


def _record_usage(user_id: int, model_key: str, usage: dict | None) -> None:
    if not usage:
        return
    execute(
        "INSERT INTO token_usage (user_id, model_key, prompt_tokens, completion_tokens, total_tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            user_id,
            model_key,
            usage["prompt_tokens"],
            usage["completion_tokens"],
            usage["total_tokens"],
            now_ms(),
        ),
    )


def _chat_rate_limit(user: dict = Depends(get_current_user)) -> dict:
    check_user_limit(user["id"], "chat", settings.chat_rate_per_minute, 60.0)
    return user


# 搜索注入预算：正文单条截断、总字符预算（近似 token 控制，防撑爆小模型上下文）
SEARCH_BUDGET_CHARS = 5000
SEARCH_CONTENT_CAP = 1200
MAX_CITATIONS = 6

# 查询词清理：去除常见指令前缀（避免把"帮我搜索 XX"整句拿去搜）
_QUERY_PREFIXES = (
    "帮我搜索一下", "帮我搜索", "帮我搜一下", "帮我搜", "帮我查一下", "帮我查",
    "搜索一下", "搜索", "查找一下", "查找", "查一下", "帮我", "请帮我", "请问", "请",
    "你好", "您好", "嗨", "hi", "hello",
)


def _clean_query(text: str) -> str:
    """清理查询词：去指令前缀、剥 markdown 链接、压缩空白、去首尾标点。"""
    q = text.strip()
    lowered = q.lower()
    for pre in _QUERY_PREFIXES:
        if lowered.startswith(pre.lower()):
            q = q[len(pre):].strip()
            break
    q = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", q)
    q = re.sub(r"\s+", " ", q).strip(" \t\r\n，。！？,.!?;；:：")
    return q


def _build_search_query(body: ChatRequest) -> str:
    """取最后一条非空用户消息；清理后过短（纯寒暄）时回退用会话第一条用户消息。"""
    user_msgs = [m.content for m in body.messages if m.role == "user" and m.content.strip()]
    if not user_msgs:
        return ""
    cleaned = _clean_query(user_msgs[-1])
    if len(cleaned) < 4:
        cleaned = _clean_query(user_msgs[0])
    return cleaned


def _with_search_context(body: ChatRequest, content: str) -> list[ChatMessage]:
    """把搜索上下文作为 user 消息插到最后一条用户消息之前。

    小模型上下文窗口小，消息超长时上游会从最前面开始截断；若搜索内容是第一条
    system 消息会最先被丢弃，模型根本读不到。紧贴问题注入可最大程度存活。
    """
    messages = list(body.messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            messages.insert(i, ChatMessage(role="user", content=content))
            return messages
    return messages


async def _build_search_context(body: ChatRequest, query: str) -> tuple[list[ChatMessage], dict]:
    """联网搜索并把结果注入上下文；返回 (messages, meta)，meta 含状态/耗时/引用。"""
    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    current_date = f"{now.year}年{now.month}月{now.day}日（星期{weekday}）"

    started = time.perf_counter()
    meta: dict = {"query": query}

    try:
        search_results = await web_search(query, fetch_content=True)
    except Exception as exc:  # noqa: BLE001 - 搜索失败不阻断聊天
        meta.update({
            "status": "failed",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc)[:200],
            "citations": [],
        })
        results_text = "（本次联网搜索失败，请基于自身知识回答，并告知用户暂无实时信息。）"
        return _with_search_context(body, f"当前日期：{current_date}。\n{results_text}"), meta

    duration_ms = int((time.perf_counter() - started) * 1000)

    if not search_results:
        meta.update({"status": "no_results", "duration_ms": duration_ms, "citations": []})
        results_text = "（本次联网搜索未返回有效结果，请基于自身知识回答，并告知用户暂无实时信息。）"
        return _with_search_context(body, f"当前日期：{current_date}。\n{results_text}"), meta

    # 已按相关度排序；按字符预算逐条拼装，超出即停止，实际注入的条目同时生成 citations
    context_lines = [
        "以下是针对用户问题进行的实时联网搜索结果。请优先依据这些信息回答，",
        "引用来源时可给出对应链接；如果搜索结果与问题无关或信息不足，请如实说明，",
        "但涉及年份、日期、时间等时效性问题时，应优先使用上面给出的当前日期。",
        "",
    ]
    citations = []
    budget = 0
    for r in search_results:
        block = f"{len(citations) + 1}. {r['title']}"
        if r.get("link"):
            block += f"\n   链接：{r['link']}"
        if r.get("snippet"):
            block += f"\n   摘要：{r['snippet']}"
        if r.get("full_content"):
            block += f"\n   正文：{r['full_content'][:SEARCH_CONTENT_CAP]}"
        block += "\n"
        if budget + len(block) > SEARCH_BUDGET_CHARS:
            break
        context_lines.append(block)
        budget += len(block)
        citations.append({"title": r["title"], "link": r["link"], "source": r.get("source", "")})
        if len(citations) >= MAX_CITATIONS:
            break

    results_text = "\n".join(context_lines)
    meta.update({
        "status": "done",
        "count": len(citations),
        "duration_ms": duration_ms,
        "sources": sorted({c["source"] for c in citations if c["source"]}),
        "citations": citations,
    })
    return _with_search_context(body, f"当前日期：{current_date}。\n{results_text}"), meta


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: dict = Depends(_chat_rate_limit),
):
    user_id: int | None = user["id"]

    if len(body.messages) > MAX_MESSAGES:
        raise HTTPException(status_code=400, detail=f"消息条数超限（最多 {MAX_MESSAGES} 条）")
    for m in body.messages:
        if len(m.content) > MAX_CONTENT_LEN:
            raise HTTPException(status_code=400, detail="单条消息过长")

    session_id: str | None = None
    if body.session_id:
        if not user_id:
            raise HTTPException(status_code=401, detail="未登录")
        session = fetch_one(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
            (body.session_id, user_id),
        )
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        session_id = body.session_id
        last_user = next(
            (m for m in reversed(body.messages) if m.role == "user"), None
        )
        if last_user and body.user_message_id:
            _persist_user_message(user_id, session_id, last_user, body.user_message_id)

    model = _get_model(body.model)
    _check_model_access(user, model)
    url = _endpoint(model)
    headers = _headers(model)

    if not settings.siliconflow_api_key and model["provider"] != "ollama":
        raise HTTPException(status_code=500, detail="服务端未配置 API key")

    assistant_msg_id = body.assistant_message_id or (
        f"assistant_{now_ms()}_{secrets.token_hex(4)}" if session_id else ""
    )
    usage_holder: dict = {}

    def error_event(msg: str) -> str:
        return "data: " + json.dumps({"error": msg}) + "\n\n"

    async def gen():
        streamed: list[str] = []
        search_meta: dict | None = None
        try:
            # 联网搜索在流式生成器内执行：响应头立即返回，先下发搜索状态事件
            if body.web_search and not model.get("supports_search", 1):
                # 模型不支持联网搜索：不注入搜索上下文，明确告知前端
                yield "data: " + json.dumps({
                    "search": {"status": "unsupported", "query": _build_search_query(body),
                               "error": "该模型不支持联网搜索", "citations": []}
                }) + "\n\n"
            elif body.web_search:
                try:
                    query = _build_search_query(body)
                    yield "data: " + json.dumps({"search": {"status": "started", "query": query}}) + "\n\n"
                    body.messages, search_meta = await _build_search_context(body, query)
                    yield "data: " + json.dumps({"search": search_meta}) + "\n\n"
                except Exception as exc:  # noqa: BLE001 - 搜索阶段异常绝不能让流静默中断
                    logger.warning("搜索阶段异常: %s", exc)
                    search_meta = {"status": "failed", "query": query, "error": str(exc)[:200], "citations": []}
                    yield "data: " + json.dumps({"search": search_meta}) + "\n\n"

            payload = _build_payload(model, body)
            timeout = httpx.Timeout(settings.request_timeout, connect=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as resp:
                    if resp.status_code != 200:
                        detail = await resp.aread()
                        yield error_event(
                            f"上游返回 {resp.status_code}: {detail.decode('utf-8', 'ignore')[:200]}"
                        )
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        usage = _extract_usage(line)
                        if usage:
                            usage_holder.update(usage)
                        content = _extract_delta(line)
                        if content:
                            streamed.append(content)
                        yield line + "\n\n"
        except httpx.HTTPError as exc:
            yield error_event(f"上游连接中断: {exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要下发错误事件，不能让流静默中断
            logger.warning("chat 流异常: %s", exc)
            yield error_event(f"服务异常: {exc}")
        finally:
            _record_usage(user_id, model["model_key"], usage_holder or None)
            if user_id:
                execute(
                    "UPDATE users SET last_seen_at = ? WHERE id = ?",
                    (now_ms(), user_id),
                )
            if session_id and assistant_msg_id:
                _persist_assistant(
                    user_id, session_id, "".join(streamed), assistant_msg_id, [],
                    (search_meta or {}).get("citations") or [],
                )
        if session_id:
            try:
                yield "data: " + json.dumps(
                    {"meta": {"assistant_id": assistant_msg_id, "usage": usage_holder or None}}
                ) + "\n\n"
            except RuntimeError:
                pass

    from fastapi.responses import StreamingResponse

    return StreamingResponse(gen(), media_type="text/event-stream")