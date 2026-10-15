"""Build identity, redacted proxy routes and periodic crawler progress."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import math
import os
import time
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies

from aiohttp import ClientTimeout
from aiohttp.helpers import get_env_proxy_for_url
from yarl import URL


APP_VERSION = "1.6.2-clowrain"
logger = logging.getLogger("emd")


@dataclass
class SyncStats:
    downloaded: int = 0
    errors: int = 0
    directories: int = 0
    files: int = 0
    unchanged: int = 0
    deleted: int = 0
    purge_status: str = "未启用"
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    active: dict = field(default_factory=dict)
    routes: set = field(default_factory=set)


def safe_url(value):
    """Omit credentials, query strings and fragments from diagnostic URLs."""
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        path = unquote(parsed.path).replace("\n", "\\n").replace("\r", "\\r")
        return f"{parsed.scheme}://{host}{port}{path}"
    except ValueError:
        return "<无效URL>"


def proxy_route(url):
    try:
        proxy, auth = get_env_proxy_for_url(URL(url))
    except LookupError:
        return "DIRECT（未配置代理或命中 NO_PROXY）"
    except ValueError:
        return "代理配置无效"
    return f"HTTP代理 {safe_url(proxy)}（认证={'已配置' if auth else '未配置'}）"


def log_proxy_route(url, stats=None, trust_env=True):
    parsed = urlsplit(url)
    route = proxy_route(url) if trust_env else "DIRECT（会话未启用代理环境）"
    key = (parsed.scheme, parsed.netloc, route)
    if stats is None or key not in stats.routes:
        logger.info("请求路由：%s://%s → %s", parsed.scheme, parsed.hostname, route)
        if stats is not None:
            stats.routes.add(key)


def positive_env(name, default):
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 必须为正数") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


def crawler_timeout():
    connect = positive_env("CONNECT_TIMEOUT", 15)
    return ClientTimeout(
        total=positive_env("REQUEST_TIMEOUT", 300),
        connect=connect,
        sock_connect=connect,
        sock_read=positive_env("READ_TIMEOUT", 60),
    )


def log_startup(timeout, interval):
    logger.info(
        "小雅元数据同步器 %s | 构建提交=%s | 构建时间=%s",
        APP_VERSION, os.getenv("XIAOYA_BUILD_REVISION", "local")[:12],
        os.getenv("XIAOYA_BUILD_DATE", "本地源码"),
    )
    proxies = getproxies()
    logger.info(
        "代理配置：HTTP=%s；HTTPS=%s；NO_PROXY=%s",
        safe_url(proxies["http"]) if proxies.get("http") else "未配置",
        safe_url(proxies["https"]) if proxies.get("https") else "未配置",
        proxies.get("no", "未配置"),
    )
    logger.info("代理最终选择节点还是 DIRECT，由 Mihomo 规则决定；请求路由日志仅表示是否连接 HTTP 代理")
    logger.info(
        "请求超时：连接=%gs，连续无数据=%gs，单次总计=%gs；进度日志间隔=%gs",
        timeout.connect, timeout.sock_read, timeout.total, interval,
    )


@asynccontextmanager
async def tracked_request(session, url, stats, operation):
    log_proxy_route(url, stats, trust_env=session.trust_env)
    token = object()
    stats.active[token] = (operation, safe_url(url), time.monotonic())
    try:
        async with session.get(url) as response:
            yield response
    finally:
        stats.active.pop(token, None)
        stats.last_activity = time.monotonic()


def log_progress(stats):
    now = time.monotonic()
    logger.info(
        "扫描进度：目录=%d，文件=%d，下载成功=%d，无需下载=%d，错误=%d，进行中请求=%d，耗时=%.0fs",
        stats.directories, stats.files, stats.downloaded, stats.unchanged,
        stats.errors, len(stats.active), now - stats.started,
    )
    if stats.active:
        operation, url, started = min(stats.active.values(), key=lambda item: item[2])
        logger.info("最久请求：%s %s | 已用时=%.0fs", operation, url[:240], now - started)


async def report_progress(stats, interval):
    while True:
        await asyncio.sleep(interval)
        log_progress(stats)
