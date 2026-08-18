"""离线 IP 归属地解析（ip2region xdb）。

数据文件位于 data/ip2region_v4.xdb，缺失或解析失败时静默降级为空归属地。
"""

import ipaddress
import os
import threading

from .config import settings

_searcher: object | None | bool = None
_lock = threading.Lock()

_PRIVATE = ("127.", "10.", "192.168.", "169.254.", "0.")


def _load() -> object | None:
    global _searcher
    if _searcher is not None:
        return _searcher or None
    with _lock:
        if _searcher is not None:
            return _searcher or None
        try:
            import ip2region.searcher as xdb
            import ip2region.util as util

            path = os.path.join(
                os.path.dirname(os.path.abspath(settings.db_path)), "ip2region_v4.xdb"
            )
            if not os.path.exists(path):
                _searcher = False
                return None
            _searcher = xdb.new_with_file_only(util.IPv4, path)
        except Exception:
            _searcher = False
        return _searcher or None


def resolve_ip(ip: str | None) -> tuple[str, str, str]:
    """解析 IP 归属地，返回 (省, 市, 区)。解析失败或本地/私有地址返回空。"""
    if not ip:
        return ("", "", "")
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return ("", "", "")
    if parsed.is_private or parsed.is_loopback or parsed.is_reserved or parsed.is_multicast:
        return ("", "", "")
    if ip.startswith(_PRIVATE):
        return ("", "", "")
    searcher = _load()
    if searcher is None:
        return ("", "", "")
    try:
        region = searcher.search(ip)
    except Exception:
        return ("", "", "")
    if not region:
        return ("", "", "")
    parts = region.split("|")
    if len(parts) < 3 or parts[0] != "中国":
        return ("", "", "")
    province = parts[1].strip()
    city = parts[2].strip()
    if city == "0":
        city = ""
    return (province, city, "")
