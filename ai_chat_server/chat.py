"""POST /api/chat：转发模型流式响应（SSE），并记录 token 用量。

安全性：model 必须在 models 表中且 enabled；消息条数/长度做基础校验。
API key 只在服务端持有，不会下发给浏览器。
"""

import json
import secrets
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import get_current_user
from .config import settings
from .db import execute, fetch_one, now_ms, transaction
from .limiter import check_user_limit
from .schemas import ChatMessage, ChatRequest
from .websearch import web_search

router = APIRouter(prefix="/api", tags=["chat"])

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
    user_id: int, session_id: str, content: str, assistant_msg_id: str, images: list[str]
) -> None:
    """流结束后把助手消息与会话元数据在同一事务写入；标题为空时用首条用户消息自动生成。"""
    now = now_ms()
    queries: list[tuple[str, list]] = []
    existing = fetch_one(
        "SELECT id FROM messages WHERE id = ? AND session_id = ?",
        (assistant_msg_id, session_id),
    )
    if existing:
        queries.append(
            (
                "UPDATE messages SET content = ?, images = ? WHERE id = ? AND session_id = ?",
                [content, json.dumps(images), assistant_msg_id, session_id],
            )
        )
    elif not content:
        return
    else:
        queries.append(
            (
                "INSERT INTO messages (id, session_id, role, content, images, created_at)"
                " VALUES (?, ?, 'assistant', ?, ?, ?)",
                [assistant_msg_id, session_id, content, json.dumps(images), now],
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


async def _build_search_context(body: ChatRequest) -> list:
    """联网搜索：取最后一条用户消息作为查询词，把结果作为 system 消息注入。"""
    last_user = next(
        (m.content for m in reversed(body.messages) if m.role == "user" and m.content.strip()),
        "",
    )
    if not last_user:
        return body.messages

    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    current_date = f"{now.year}年{now.month}月{now.day}日（星期{weekday}）"

    try:
        results = await web_search(last_user)
    except Exception as exc:  # noqa: BLE001 - 搜索失败不阻断聊天
        results = f"（联网搜索失败：{exc}。请直接回答用户问题，并说明当前无法获取实时信息。）"

    system_msg = ChatMessage(
        role="system",
        content=(
            "当前日期："
            f"{current_date}。\n"
            "以下是针对用户问题进行的实时联网搜索结果。请优先依据这些信息回答，"
            "引用来源时可给出对应链接；如果搜索结果与问题无关或信息不足，请如实说明，"
            "但涉及年份、日期、时间等时效性问题时，应优先使用上面给出的当前日期。\n\n"
            f"{results}"
        ),
    )
    return [system_msg, *body.messages]


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

    if body.web_search:
        body.messages = await _build_search_context(body)

    model = _get_model(body.model)
    payload = _build_payload(model, body)
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
        try:
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
        finally:
            _record_usage(user_id, model["model_key"], usage_holder or None)
            if user_id:
                execute(
                    "UPDATE users SET last_seen_at = ? WHERE id = ?",
                    (now_ms(), user_id),
                )
            if session_id and assistant_msg_id:
                _persist_assistant(
                    user_id, session_id, "".join(streamed), assistant_msg_id, []
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