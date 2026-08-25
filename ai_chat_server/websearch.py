"""联网搜索：多源并行搜索 + 正文抓取 (RAG)，返回结构化结果供模型使用。

流程：
1. 多源并行搜索（Bing RSS、DuckDuckGo HTML、可选 SearXNG）
2. 结果去重、相关性打分、取前 N 条
3. 可选：并发抓取前 M 条结果的完整网页正文 (trafilatura)
4. 返回结构化列表：[{title, link, snippet, full_content, source}, ...]
"""

import os
import re
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

try:
    import jieba
    jieba.setLogLevel(logging.WARNING)
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

logger = logging.getLogger(__name__)

# ────────────────────────────────────────
# 配置常量（可通过环境变量覆盖）
# ────────────────────────────────────────
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SEARCH_TIMEOUT = float(os.getenv("WEBSEARCH_SEARCH_TIMEOUT", "8.0"))
PAGE_FETCH_TIMEOUT = float(os.getenv("WEBSEARCH_PAGE_TIMEOUT", "10.0"))
MAX_RESULTS = int(os.getenv("WEBSEARCH_MAX_RESULTS", "6"))
MAX_PAGES_TO_FETCH = int(os.getenv("WEBSEARCH_MAX_PAGES", "3"))
MAX_CONTENT_LENGTH = int(os.getenv("WEBSEARCH_MAX_CONTENT", "12000"))
FETCH_CONTENT_ENABLED = os.getenv("WEBSEARCH_FETCH_CONTENT", "true").lower() == "true"

BING_RSS_URL = "https://www.bing.com/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
SEARXNG_URL = os.getenv("WEBSEARCH_SEARXNG_URL", "https://search.bus-hit.me")
BAIDU_URL = "https://www.baidu.com/s"


# ────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────
@dataclass
class SearchResult:
    title: str
    link: str
    snippet: str
    full_content: Optional[str] = None
    source: str = ""  # "bing" | "ddg" | "searxng"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "link": self.link,
            "snippet": self.snippet,
            "full_content": self.full_content,
            "source": self.source,
        }


# ────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────
def _clean(html_text: str) -> str:
    """清理 HTML 标签、多余空白"""
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", "", html_text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_ns(xml_text: str) -> str:
    """去除 XML 命名空间前缀"""
    return re.sub(r"\{[^}]*\}", "", xml_text)


def _normalize_url(url: str) -> str:
    """URL 规范化：去除 tracking 参数、统一 scheme"""
    try:
        parsed = urlparse(url)
        # 去除常见 tracking 参数
        tracking_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "msclkid", "ref", "source", "medium",
            "spm", "from", "isappinstalled"
        }
        query = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in query.items() if k.lower() not in tracking_params}
        new_query = urlencode(filtered, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def _domain(url: str) -> str:
    """提取域名用于去重"""
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# ────────────────────────────────────────
# 搜索引擎实现
# ────────────────────────────────────────
@retry(
    wait=wait_exponential_jitter(initial=1, max=4),
    stop=stop_after_attempt(2),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    reraise=True,
)
async def _bing_search(client: httpx.AsyncClient, query: str, limit: Optional[int] = None) -> list:
    """Bing RSS 搜索（无需 key，返回 XML）"""
    try:
        resp = await client.get(
            BING_RSS_URL,
            params={"q": query, "format": "rss", "count": limit or MAX_RESULTS},
            headers={"User-Agent": UA},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        root = _strip_ns(resp.text)
        items = re.findall(r"<item>(.*?)</item>", root, re.S)
        results = []
        for item in items[: limit or MAX_RESULTS]:
            title_m = re.search(r"<title>(.*?)</title>", item, re.S)
            link_m = re.search(r"<link>(.*?)</link>", item, re.S)
            desc_m = re.search(r"<description>(.*?)</description>", item, re.S)
            title = _clean(title_m.group(1)) if title_m else ""
            link = _normalize_url(_clean(link_m.group(1))) if link_m else ""
            snippet = _clean(desc_m.group(1)) if desc_m else ""
            if title and link:
                results.append({"title": title, "link": link, "snippet": snippet, "source": "bing"})
        return results
    except Exception as e:
        logger.warning("Bing 搜索失败: %s", e)
        return []


@retry(
    wait=wait_exponential_jitter(initial=1, max=4),
    stop=stop_after_attempt(2),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    reraise=True,
)
async def _ddg_search(client: httpx.AsyncClient, query: str, limit: Optional[int] = None) -> list:
    """DuckDuckGo HTML 搜索（备用）"""
    try:
        resp = await client.post(
            DDG_HTML_URL,
            data={"q": query, "kl": "cn-zh"},
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.text

        # 检测 anomaly 页面
        if "anomaly" in html.lower() or "verify you are human" in html.lower():
            logger.warning("DDG 返回 anomaly 页面")
            return []

        results = []
        # 新版 DDG 结构
        for block in re.findall(r'<div class="result[^"]*">(.*?)</div>\s*</div>', html, re.S):
            title_a = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            snip_a = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
            if not title_a:
                continue
            link = _normalize_url(title_a.group(1))
            title = _clean(title_a.group(2))
            snippet = _clean(snip_a.group(1)) if snip_a else ""
            if title and link:
                results.append({"title": title, "link": link, "snippet": snippet, "source": "ddg"})
        return results[: limit or MAX_RESULTS]
    except Exception as e:
        logger.warning("DDG 搜索失败: %s", e)
        return []


async def _searxng_search(client: httpx.AsyncClient, query: str, limit: Optional[int] = None) -> list:
    """SearXNG JSON API 搜索（可选，需配置 searxng_url）"""
    url = _get_searxng_url()
    if not url:
        return []
    try:
        resp = await client.get(
            f"{url.rstrip('/')}/search",
            params={
                "q": query,
                "format": "json",
                "language": "zh-CN",      # 强制中文
                "safesearch": 1,           # 安全搜索
                "time_range": "",          # 可选时间范围
            },
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=_get_searxng_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", [])[: limit or MAX_RESULTS]:
            title = _clean(item.get("title", ""))
            link = _normalize_url(item.get("url", ""))
            snippet = _clean(item.get("content", ""))
            if title and link:
                results.append({"title": title, "link": link, "snippet": snippet, "source": "searxng"})
        return results
    except Exception as e:
        logger.warning("SearXNG 搜索失败: %s", e)
        return []


async def _baidu_search(client: httpx.AsyncClient, query: str, limit: Optional[int] = None) -> list:
    """百度搜索（中文查询效果最佳；跳过广告，仅取有机结果 baidu.com/link 跳转链接）"""
    try:
        resp = await client.get(
            BAIDU_URL,
            params={"wd": query},
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.text
        results = []
        for m in re.finditer(r"<h3[^>]*>(.*?)</h3>", html, re.S):
            h, h_end = m.group(1), m.end()
            a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S)
            if not a:
                continue
            href, title = a.group(1), _clean(a.group(2))
            if not href or not title:
                continue
            # 跳过广告 / 站内视频等相对链接
            if "baidu.php" in href or "ada.baidu" in href or not href.startswith("http"):
                continue
            # 摘要：该 h3 到下一个 h3 之间的可见文本，清洗后截断
            nxt = html.find("<h3", h_end)
            seg = html[h_end:nxt] if nxt != -1 else html[h_end:h_end + 1500]
            snippet = _clean(seg)[:160]
            results.append({"title": title, "link": _normalize_url(href), "snippet": snippet, "source": "baidu"})
            if len(results) >= (limit or MAX_RESULTS):
                break
        return results
    except Exception as e:
        logger.warning("百度搜索失败: %s", e)
        return []


# ────────────────────────────────────────
# 去重与评分
# ────────────────────────────────────────
def _tokens(text: str) -> set:
    """中文/英文混合分词：jieba 分词（中文查询核心命中靠它），英文按空白切分"""
    low = text.lower()
    if JIEBA_AVAILABLE:
        return {w for w in jieba.lcut(low) if re.match(r"^[\w\u4e00-\u9fff]+$", w)}
    return set(re.findall(r"[\w\u4e00-\u9fff]+", low))


def _dedupe_and_score(results: list, query: str, provider_order: Optional[list] = None) -> list:
    """去重 + 相关性打分，返回排序后的列表（provider_order 靠前的来源获得更高权重）"""
    seen_urls = set()
    seen_titles = set()
    scored = []
    priority = {
        p: max(0, len(provider_order) - i) for i, p in enumerate(provider_order or [])
    }

    query_words = _tokens(query)

    for r in results:
        norm_url = _normalize_url(r["link"])
        title_key = r["title"].lower().strip()

        # URL 去重
        if norm_url in seen_urls:
            continue
        # 标题近似去重
        if title_key in seen_titles:
            continue

        seen_urls.add(norm_url)
        seen_titles.add(title_key)

        # 相关性打分：中文按 jieba 词重合（标题×3 + 摘要×1），外加来源优先级
        score = 0
        title_words = _tokens(r["title"])
        snippet_words = _tokens(r.get("snippet") or "")
        score += len(query_words & title_words) * 3
        score += len(query_words & snippet_words)
        score += priority.get(r["source"], 0)

        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


# ────────────────────────────────────────
# 页面抓取与正文提取
# ────────────────────────────────────────
class PageFetcher:
    """并发抓取网页并提取正文"""

    def __init__(
        self,
        timeout: float = 10.0,
        max_concurrent: int = 3,
        max_length: int = 12000,
    ):
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_length = max_length
        self._cache = {}

    async def fetch(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        """抓取单页并返回提取后的正文文本"""
        if url in self._cache:
            return self._cache[url]

        async with self.semaphore:
            try:
                # HEAD 先探测 content-type
                head = await client.head(url, timeout=self.timeout, follow_redirects=True)
                ct = head.headers.get("content-type", "").lower()
                if "pdf" in ct or "word" in ct or "ppt" in ct or "excel" in ct:
                    logger.info("跳过非 HTML: %s (%s)", url, ct)
                    self._cache[url] = None
                    return None

                resp = await client.get(url, timeout=self.timeout, follow_redirects=True)
                resp.raise_for_status()

                # 编码检测 + 修正
                html_bytes = resp.content
                detected = from_bytes(html_bytes).best()
                if detected:
                    html = str(detected)
                else:
                    html = html_bytes.decode("utf-8", errors="replace")

                # 正文提取
                content = None
                if TRAFILATURA_AVAILABLE:
                    try:
                        content = trafilatura.extract(
                            html,
                            include_comments=False,
                            include_tables=False,
                            include_images=False,
                            target_language="zh",
                            output_format="txt",
                            favor_precision=True,
                        )
                    except Exception:
                        content = None

                # 降级：BeautifulSoup 提取主体
                if not content:
                    try:
                        soup = BeautifulSoup(html, "lxml")
                        # 移除噪声标签
                        for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form", "iframe", "noscript"]):
                            tag.decompose()
                        main = soup.find("main") or soup.find("article") or soup.find(role="main") or soup.body
                        if main:
                            content = _clean(main.get_text(" ", strip=True))
                    except Exception:
                        pass

                if not content:
                    self._cache[url] = None
                    return None

                # 后处理：清洗噪声、截断
                content = self._post_process(content)
                self._cache[url] = content
                return content

            except httpx.TimeoutException:
                logger.warning("抓取超时: %s", url)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (403, 404, 429):
                    logger.info("跳过 %s: HTTP %d", url, e.response.status_code)
                else:
                    logger.warning("HTTP 错误 %s: %s", url, e)
            except Exception as e:
                logger.warning("抓取异常 %s: %s", url, e)

            self._cache[url] = None
            return None

    def _post_process(self, text: str) -> Optional[str]:
        """清洗、截断"""
        if not text:
            return None
        # 去除常见噪声行
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            # 跳过常见噪声
            if any(kw in low for kw in (
                "cookie", "隐私政策", "版权所有", "版权声明", "all rights reserved",
                "登录", "注册", "立即下载", "扫码", "关注我们", "关注公众号",
                "广告", "推广", "sponsored", "advertisement"
            )):
                continue
            if len(line) < 10:  # 太短的行通常是导航/标签
                continue
            lines.append(line)

        text = "\n".join(lines)
        if len(text) > self.max_length:
            text = text[:self.max_length] + "…"
        return text if len(text) > 100 else None

    async def fetch_batch(self, client: httpx.AsyncClient, urls: list) -> dict:
        """并发抓取多个 URL"""
        tasks = [self.fetch(client, url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {url: (None if isinstance(r, Exception) else r) for url, r in zip(urls, results)}


# ────────────────────────────────────────
# 配置获取（从 settings 表读取，兜底环境变量）
# ────────────────────────────────────────
def _get_settings() -> dict:
    """从数据库 settings 表读取搜索配置，兜底环境变量"""
    try:
        from .db import fetch_all
        rows = fetch_all(
            "SELECT key, value FROM settings "
            "WHERE (key LIKE 'websearch_%' OR key LIKE 'searxng_%') AND enabled = 1"
        )
        settings = {}
        for row in rows:
            key = row["key"]
            val = row["value"]
            # 尝试解析 JSON
            if key == "websearch_providers":
                try:
                    settings[key] = json.loads(val)
                except Exception:
                    settings[key] = ["searxng", "bing", "ddg"]
            else:
                settings[key] = val
        return settings
    except Exception:
        return {}


def _get_providers() -> list:
    """获取启用的搜索供应商列表（按配置顺序即优先级）"""
    settings = _get_settings()
    providers = settings.get("websearch_providers")
    if providers:
        ids = []
        for p in providers:
            if isinstance(p, str):
                ids.append(p)
            elif isinstance(p, dict) and p.get("id"):
                # 兼容对象格式 {id,label,enabled}，enabled 显式为 false 才剔除
                if p.get("enabled", True) is not False:
                    ids.append(p["id"])
        if ids:
            return ids
    # 环境变量兜底
    env = os.getenv("WEBSEARCH_PROVIDERS", "searxng,bing,ddg")
    return [p.strip() for p in env.split(",") if p.strip()]


def _get_searxng_url() -> str:
    settings = _get_settings()
    return settings.get("searxng_url") or os.getenv("WEBSEARCH_SEARXNG_URL", "https://search.bus-hit.me")


def _get_searxng_timeout() -> float:
    settings = _get_settings()
    val = settings.get("searxng_timeout")
    if val:
        try:
            return float(val)
        except Exception:
            pass
    return float(os.getenv("WEBSEARCH_SEARXNG_TIMEOUT", "10.0"))


def _get_fetch_content() -> bool:
    settings = _get_settings()
    val = settings.get("websearch_fetch_content")
    if val is not None:
        return str(val).lower() == "true"
    return FETCH_CONTENT_ENABLED


def _get_max_pages() -> int:
    settings = _get_settings()
    val = settings.get("websearch_max_pages")
    if val:
        try:
            return int(val)
        except Exception:
            pass
    return MAX_PAGES_TO_FETCH


def _get_max_results() -> int:
    settings = _get_settings()
    val = settings.get("websearch_max_results")
    if val:
        try:
            return max(1, min(20, int(val)))
        except Exception:
            pass
    return MAX_RESULTS


def _get_max_content() -> int:
    settings = _get_settings()
    val = settings.get("websearch_max_content")
    if val:
        try:
            return int(val)
        except Exception:
            pass
    return MAX_CONTENT_LENGTH


# ────────────────────────────────────────
# 主入口
# ────────────────────────────────────────
async def web_search(
    query: str,
    max_results: Optional[int] = None,
    fetch_content: Optional[bool] = None,
    max_pages_fetch: Optional[int] = None,
) -> list:
    """
    执行联网搜索，返回结构化结果列表。

    Returns:
        [
            {"title", "link", "snippet", "full_content": str|None, "source": "bing|ddg|searxng"},
            ...
        ]
    """
    query = query.strip()
    if not query:
        return []

    if max_results is None:
        max_results = _get_max_results()
    max_results = max(1, min(20, int(max_results)))
    if fetch_content is None:
        fetch_content = _get_fetch_content()
    if max_pages_fetch is None:
        max_pages_fetch = _get_max_pages()

    async with httpx.AsyncClient(
        timeout=SEARCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": UA},
    ) as client:
        # 1. 按后台配置的供应商优先级并行搜索（gather 并行执行，异常按源记录）
        providers = _get_providers()
        coros: list[tuple[str, object]] = []
        for p in providers:
            p = (p or "").strip().lower()
            if p == "baidu":
                coros.append(("baidu", _baidu_search(client, query, max_results)))
            elif p == "bing":
                coros.append(("bing", _bing_search(client, query, max_results)))
            elif p == "ddg":
                coros.append(("ddg", _ddg_search(client, query, max_results)))
            elif p == "searxng":
                coros.append(("searxng", _searxng_search(client, query, max_results)))

        results_list = await asyncio.gather(*(c for _, c in coros), return_exceptions=True)
        all_results = []
        for (source, _), res in zip(coros, results_list):
            if isinstance(res, BaseException):
                logger.warning("搜索源 %s 异常: %s", source, res)
                continue
            if res:
                all_results.extend(res)

        if not all_results:
            raise RuntimeError("所有搜索源均无结果")

        # 2. 去重 + 评分（供应商顺序即权重）
        results = _dedupe_and_score(all_results, query, provider_order=providers)
        results = results[:max(1, max_results)]

        # 3. 可选：并发抓取正文
        if fetch_content and FETCH_CONTENT_ENABLED and results:
            fetcher = PageFetcher(max_length=_get_max_content())
            urls_to_fetch = [r["link"] for r in results[:max_pages_fetch]]
            contents = await fetcher.fetch_batch(client, urls_to_fetch)
            for r in results:
                r["full_content"] = contents.get(r["link"])

    return results


# ────────────────────────────────────────
# 兼容旧接口（返回 markdown 字符串）
# ────────────────────────────────────────
async def web_search_markdown(
    query: str,
    max_results: int = 6,
) -> str:
    """兼容旧接口：返回 markdown 格式文本"""
    results = await web_search(query, max_results, fetch_content=False)
    lines = [f"查询：{query}", f"共 {len(results)} 条结果：", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r["link"]:
            lines.append(f"   链接：{r['link']}")
        if r["snippet"]:
            lines.append(f"   摘要：{r['snippet']}")
        lines.append("")
    return "\n".join(lines).strip()