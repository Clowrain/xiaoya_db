import asyncio
from contextlib import suppress
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

import solid
from sync_logging import (
    APP_VERSION, SyncStats, crawler_timeout, log_progress, log_proxy_route,
    log_startup, positive_env, proxy_route, report_progress, safe_url,
)


class SyncLoggingTests(unittest.TestCase):
    def test_startup_identifies_build_without_leaking_credentials(self):
        env = {
            "HTTP_PROXY": "http://proxy-user:proxy-password@127.0.0.1:7890",
            "HTTPS_PROXY": "http://proxy-user:proxy-password@127.0.0.1:7890",
            "XIAOYA_BUILD_REVISION": "0123456789abcdef",
            "XIAOYA_BUILD_DATE": "2026-10-15T00:00:00Z",
        }
        with patch.dict(os.environ, env, clear=True), self.assertLogs("emd", level="INFO") as logs:
            log_startup(crawler_timeout(), 15)
            log_proxy_route("https://example.test/")
        text = "\n".join(logs.output)
        self.assertIn(APP_VERSION, text)
        self.assertIn("0123456789ab", text)
        self.assertIn("127.0.0.1:7890", text)
        self.assertIn("认证=已配置", text)
        self.assertNotIn("proxy-password", text)
        self.assertNotIn("proxy-user", text)

    def test_no_proxy_bypass_and_missing_proxy_are_reported(self):
        with patch.dict(os.environ, {
            "HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": ".example.test",
        }, clear=True):
            self.assertTrue(proxy_route("https://host.example.test/").startswith("DIRECT"))
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.getproxies", return_value={}):
            self.assertTrue(proxy_route("https://example.test/").startswith("DIRECT"))

    def test_safe_url_removes_auth_query_and_fragment(self):
        self.assertEqual(
            safe_url("http://user:secret@[::1]:7890/path?api_key=secret#secret"),
            "http://[::1]:7890/path",
        )

    def test_route_is_logged_once_per_host_and_scheme(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": ""}, clear=True):
            stats = SyncStats()
            with self.assertLogs("emd", level="INFO") as logs:
                log_proxy_route("https://example.test/one", stats)
                log_proxy_route("https://example.test/two", stats)
            self.assertEqual(len(logs.output), 1)

    def test_timeout_configuration_rejects_invalid_values(self):
        for value in ("0", "-1", "nan", "inf", "not-a-number"):
            with patch.dict(os.environ, {"READ_TIMEOUT": value}, clear=True):
                with self.assertRaises(ValueError):
                    crawler_timeout()
        with patch.dict(os.environ, {"PROGRESS_INTERVAL": "0.25"}, clear=True):
            self.assertEqual(positive_env("PROGRESS_INTERVAL", 15), 0.25)

    def test_progress_reports_oldest_request_without_query_secrets(self):
        stats = SyncStats(directories=12, files=300, unchanged=300)
        stats.active[object()] = ("目录", safe_url("https://example.test/slow?key=private"), stats.started)
        with self.assertLogs("emd", level="INFO") as logs:
            log_progress(stats)
        text = "\n".join(logs.output)
        self.assertIn("目录=12，文件=300", text)
        self.assertIn("最久请求", text)
        self.assertNotIn("private", text)


class RequestProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_request_reports_progress_then_times_out(self):
        async def slow(request):
            await asyncio.sleep(0.15)
            return web.Response(text="<html></html>")

        app = web.Application()
        app.router.add_get("/slow", slow)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        stats = SyncStats()
        with self.assertLogs("emd", level="INFO") as logs:
            progress = asyncio.create_task(report_progress(stats, 0.005))
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=0.1, sock_read=0.03), trust_env=False,
                ) as session:
                    files, directories = await solid.parse(
                        f"http://127.0.0.1:{port}/slow", session,
                        semaphore=asyncio.Semaphore(1), stats=stats,
                    )
            finally:
                progress.cancel()
                with suppress(asyncio.CancelledError):
                    await progress
        self.assertEqual((files, directories), ([], []))
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.active, {})
        text = "\n".join(logs.output)
        self.assertIn("扫描进度", text)
        self.assertIn("最久请求", text)
        self.assertIn("Timeout", text)

    async def test_manifest_mismatch_preserves_files_and_reports_cleanup_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp)
            orphan = media / "orphan.nfo"
            orphan.write_text("keep")
            localdb, remotedb = media / "local.db", media / "remote.db"
            async with solid.aiosqlite.connect(localdb) as conn:
                await solid.create_table(conn)
                await solid.insert_files(conn, [("/orphan.nfo", None, None)])
            async with solid.aiosqlite.connect(remotedb) as conn:
                await solid.create_table(conn)
                await solid.insert_files(conn, [(f"/file-{i}", None, None) for i in range(28)])
            stats = SyncStats()
            with self.assertLogs("emd", level="WARNING") as logs:
                await solid.purge_removed_files(str(localdb), str(remotedb), str(media), 1, stats=stats)
            self.assertTrue(orphan.exists())
            self.assertEqual(stats.deleted, 0)
            self.assertEqual(stats.errors, 0)
            self.assertIn("+27", stats.purge_status)
            self.assertIn("跳过过期文件清理", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
