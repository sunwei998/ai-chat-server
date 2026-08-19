"""联网搜索：把查询发送给免费搜索引擎，返回整理后的文本结果。

主用 Bing RSS（无需 key、返回干净 XML），失败或为空时降级到 DuckDuckGo HTML。
结果统一格式化成 markdown 文本，供 chat 注入系统消息使用。
"""

import re

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SEARCH_TIMEOUT = 8.0
MAX_RESULTS = 6

BING_RSS_URL = "https://www.bing.com/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"


def _clean(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", "", html_text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_ns(tag: str) -> str:
    return re.sub(r"\{[^}]*\}", "", tag)


async def _bing_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await client.get(
        BING_RSS_URL,
        params={"q": query, "format": "rss", "count": MAX_RESULTS},
        headers={"User-Agent": UA},
    )
    resp.raise_for_status()
    root = _strip_ns(resp.text)
    items = re.findall(r"<item>(.*?)</item>", root, re.S)
    results: list[dict] = []
    for item in items:
        title = _clean(re.search(r"<title>(.*?)</title>", item, re.S).group(1) if re.search(r"<title>(.*?)</title>", item, re.S) else "")
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        desc = re.search(r"<description>(.*?)</description>", item, re.S)
        link_text = _clean(link.group(1)) if link else ""
        snippet = _clean(desc.group(1)) if desc else ""
        if title:
            results.append({"title": title, "link": link_text, "snippet": snippet})
    return results


async def _ddg_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await client.post(
        DDG_HTML_URL,
        data={"q": query},
        headers={"User-Agent": UA},
    )
    resp.raise_for_status()
    html = resp.text
    results: list[dict] = []
    for block in re.findall(r'<div class="result[^"]*">(.*?)</div>\s*</div>', html, re.S):
        title_a = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        snip_a = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        if not title_a:
            continue
        results.append(
            {
                "title": _clean(title_a.group(2)),
                "link": title_a.group(1),
                "snippet": _clean(snip_a.group(1)) if snip_a else "",
            }
        )
    return results


async def web_search(query: str, max_results: int = MAX_RESULTS) -> str:
    """执行联网搜索，返回 markdown 格式的文本（失败抛异常）。"""
    query = query.strip()
    if not query:
        return ""

    async with httpx.AsyncClient(
        timeout=SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA},
    ) as client:
        results: list[dict] = []
        errors: list[str] = []
        for fn in (_ddg_search, _bing_search):
            try:
                results = await fn(client, query)
                if results:
                    break
            except Exception as exc:  # noqa: BLE001 - 搜索降级，逐个尝试
                errors.append(f"{fn.__name__}: {exc}")

    if not results:
        raise RuntimeError(
            "搜索失败: " + ("；".join(errors) if errors else "无搜索结果")
        )

    lines = [f"查询：{query}", f"共 {len(results)} 条结果：", ""]
    for i, r in enumerate(results[:max_results], start=1):
        title = r.get("title") or "无标题"
        link = r.get("link") or ""
        snippet = r.get("snippet") or ""
        lines.append(f"{i}. {title}")
        if link:
            lines.append(f"   链接：{link}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
        lines.append("")
    return "\n".join(lines).strip()
