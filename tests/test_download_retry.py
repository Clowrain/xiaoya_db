import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

import solid
from sync_logging import SyncStats


class DownloadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name)
        self.attempts = 0
        self.failures = 0
        self.truncate = False
        self.slow = False
        self.original = None
        app = web.Application()
        app.router.add_get("/file.nfo", self.source)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/file.nfo"
        self.session = aiohttp.ClientSession(trust_env=False)
        self.addAsyncCleanup(self.session.close)

    async def source(self, request):
        self.attempts += 1
        if self.original is not None:
            self.assertEqual((self.media / "file.nfo").read_bytes(), self.original)
        if self.attempts <= self.failures:
            if self.slow:
                await asyncio.sleep(0.05)
            if self.truncate:
                response = web.StreamResponse(headers={"Content-Length": "100"})
                await response.prepare(request)
                await response.write(b"partial")
                request.transport.close()
                return response
            return web.Response(status=503)
        return web.Response(body=b"complete")

    async def run_download(self, attempts=3):
        stats = SyncStats()
        with self.assertLogs("emd", level="INFO") as logs:
            await solid.download(
                (self.url, "/file.nfo", 0, "8"), self.session,
                media=str(self.media), semaphore=asyncio.Semaphore(1), stats=stats,
                download_max_attempts=attempts, download_retry_delay=0.001,
            )
        return stats, "\n".join(logs.output)

    async def test_transient_http_failure_waits_and_succeeds_on_last_attempt(self):
        self.failures = 2
        stats, logs = await self.run_download()
        self.assertEqual(self.attempts, 3)
        self.assertEqual((stats.downloaded, stats.errors), (1, 0))
        self.assertEqual((self.media / "file.nfo").read_bytes(), b"complete")
        self.assertEqual(logs.count("等待 0.001 秒后重试"), 2)
        self.assertIn("重试下载成功（第 3/3 次）", logs)

    async def test_retry_exhaustion_counts_only_one_error_and_leaves_no_empty_file(self):
        self.failures = 100
        stats, logs = await self.run_download()
        self.assertEqual(self.attempts, 3)
        self.assertEqual((stats.downloaded, stats.errors), (0, 1))
        self.assertFalse((self.media / "file.nfo").exists())
        self.assertIn("已达最大尝试次数 3，放弃该文件", logs)

    async def test_one_attempt_disables_retries(self):
        self.failures = 100
        stats, logs = await self.run_download(attempts=1)
        self.assertEqual(self.attempts, 1)
        self.assertEqual(stats.errors, 1)
        self.assertNotIn("等待", logs)

    async def test_truncated_payload_retries_without_truncating_existing_file(self):
        self.failures = 2
        self.truncate = True
        self.original = b"original-valid-file"
        (self.media / "file.nfo").write_bytes(self.original)
        stats, logs = await self.run_download()
        self.assertEqual(self.attempts, 3)
        self.assertEqual((stats.downloaded, stats.errors), (1, 0))
        self.assertEqual((self.media / "file.nfo").read_bytes(), b"complete")
        self.assertIn("ClientPayloadError", logs)

    async def test_exhausted_payload_failures_preserve_existing_file(self):
        self.failures = 100
        self.truncate = True
        self.original = b"original-valid-file"
        (self.media / "file.nfo").write_bytes(self.original)
        stats, _ = await self.run_download()
        self.assertEqual(self.attempts, 3)
        self.assertEqual(stats.errors, 1)
        self.assertEqual((self.media / "file.nfo").read_bytes(), self.original)

    async def test_proxy_connection_failure_is_retried(self):
        listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        listener.close()
        await listener.wait_closed()
        await self.session.close()
        self.session = aiohttp.ClientSession(proxy=f"http://127.0.0.1:{port}", trust_env=False)
        self.addAsyncCleanup(self.session.close)
        stats, logs = await self.run_download()
        self.assertEqual(stats.errors, 1)
        self.assertEqual(logs.count("ClientProxyConnectionError"), 3)
        self.assertEqual(logs.count("等待 0.001 秒后重试"), 2)
        self.assertFalse((self.media / "file.nfo").exists())

    async def test_read_timeout_is_retried_then_recovers(self):
        self.failures = 2
        self.slow = True
        await self.session.close()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=1, sock_read=0.01), trust_env=False,
        )
        self.addAsyncCleanup(self.session.close)
        stats, logs = await self.run_download()
        self.assertEqual(self.attempts, 3)
        self.assertEqual((stats.downloaded, stats.errors), (1, 0))
        self.assertIn("Timeout", logs)

    async def test_invalid_retry_settings_fail_before_network_access(self):
        for name, values in {
            "DOWNLOAD_MAX_ATTEMPTS": ("0", "-1", "1.5", "invalid"),
            "DOWNLOAD_RETRY_DELAY": ("0", "-1", "nan", "inf"),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True), patch("sys.argv", ["solid.py", "--media", str(self.media)]), patch.object(solid, "fetch_pool_from_url") as fetch:
                    with self.assertRaises(SystemExit) as error:
                        await solid.main()
                    self.assertEqual(error.exception.code, 2)
                    fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
