"""Refresh selected Emby libraries after a completed metadata sync."""

import asyncio
import json
import logging
import os

import aiofiles
from aiohttp import ClientError, ClientSession, ClientTimeout


logger = logging.getLogger("emd")
DEFAULT_LIBRARY_NAMES = [
    "电视剧/每日更新",
    "电影/每日更新",
    "动漫/每日更新",
    "纪录片",
    "动漫/每日更新 剧场版",
    "综艺",
]


async def refresh_emby_after_sync(downloaded, errors=0):
    """Return false on refresh failure; never undo a completed metadata sync."""
    if os.getenv("EMBY_REFRESH_ENABLED", "false").lower() not in ("true", "1", "yes"):
        logger.info("Emby 自动刷新未启用")
        return True
    if errors:
        logger.warning("跳过 Emby 刷新：本轮同步存在 %d 个操作错误", errors)
        return True
    if not downloaded:
        logger.info("跳过 Emby 刷新：本轮没有新文件下载")
        return True

    try:
        api_key = os.getenv("EMBY_API_KEY", "").strip()
        if not api_key and os.getenv("EMBY_API_KEY_FILE"):
            async with aiofiles.open(os.environ["EMBY_API_KEY_FILE"], encoding="utf-8") as file:
                api_key = (await file.read()).strip()
        if not api_key:
            logger.error("Emby 刷新缺少密钥：请配置 EMBY_API_KEY 或 EMBY_API_KEY_FILE")
            return False

        names = json.loads(os.getenv("EMBY_LIBRARY_NAMES", json.dumps(DEFAULT_LIBRARY_NAMES)))
        if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            logger.error("EMBY_LIBRARY_NAMES must be a non-empty JSON array of library names")
            return False
        base_url = os.getenv("EMBY_URL", "http://127.0.0.1:2345/emby").rstrip("/")

        # Emby is local: crawler proxy settings must never route these requests.
        async with ClientSession(
            headers={"X-Emby-Token": api_key},
            timeout=ClientTimeout(total=30),
            trust_env=False,
        ) as session:
            async with session.get(f"{base_url}/Library/VirtualFolders") as response:
                response.raise_for_status()
                libraries = await response.json()
            if not isinstance(libraries, list) or not all(
                isinstance(library, dict) for library in libraries
            ):
                logger.error("Emby returned an invalid library list")
                return False
            selected = [library for library in libraries if library.get("Name") in names]
            missing = set(names) - {library["Name"] for library in selected}
            for name in sorted(missing):
                logger.warning("Emby library not found: %s", name)
            if not selected:
                logger.error("No Emby libraries matched the configured names")
                return False

            accepted = 0
            for library in selected:
                item_id = library.get("ItemId")
                if not item_id:
                    logger.error("Emby library has no ItemId: %s", library["Name"])
                    continue
                try:
                    async with session.post(
                        f"{base_url}/Items/{item_id}/Refresh",
                        params={
                            "Recursive": "true",
                            "MetadataRefreshMode": "Default",
                            "ReplaceAllMetadata": "false",
                        },
                        data=b"",
                    ) as response:
                        response.raise_for_status()
                        await response.read()
                    accepted += 1
                    logger.info("已提交 Emby 刷新：%s（ID：%s）", library["Name"], item_id)
                except (ClientError, asyncio.TimeoutError) as error:
                    logger.error(
                        "Emby refresh failed for %s: %s (HTTP %s)",
                        library["Name"], type(error).__name__, getattr(error, "status", "n/a"),
                    )
            logger.info("Emby 已接受 %d/%d 个刷新请求，媒体库扫描由 Emby 后台执行", accepted, len(selected))
            return accepted == len(selected) and not missing
    except (ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
        # Avoid logging URLs, headers or response bodies containing credentials.
        logger.error("Emby refresh failed: %s (HTTP %s)", type(error).__name__, getattr(error, "status", "n/a"))
        return False
