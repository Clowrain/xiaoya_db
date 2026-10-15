import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

from emby_refresh import DEFAULT_LIBRARY_NAMES, refresh_emby_after_sync
import solid


class EmbyRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.media = Path(self.temp.name)
        for folder in ("115", "每日更新", "纪录片（已刮削）", "音乐", "综艺"):
            (self.media / folder).mkdir()
            (self.media / folder / ".keep").touch()
        self.calls = []
        self.expected_key = "test-secret"
        self.libraries = [
            {"Name": name, "ItemId": str(index)}
            for index, name in enumerate(DEFAULT_LIBRARY_NAMES, 1)
        ] + [{"Name": "其他媒体库", "ItemId": "other"}]
        self.list_status = 200
        self.failed_ids = set()
        self.failed_directory = False
        self.failed_download = False
        self.expect_sync_finished = False
        app = web.Application()
        app.router.add_get("/emby/Library/VirtualFolders", self.list_libraries)
        app.router.add_post("/emby/Items/{item_id}/Refresh", self.refresh)
        app.router.add_get("/{path:.*}", self.crawler_source)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.environment = patch.dict(os.environ, {
            "EMBY_REFRESH_ENABLED": "true",
            "EMBY_URL": f"{self.base_url}/emby",
            "EMBY_API_KEY": "test-secret",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }, clear=True)
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def list_libraries(self, request):
        self.assertEqual(request.headers.get("X-Emby-Token"), self.expected_key)
        self.assertNotIn("api_key", request.query)
        if self.expect_sync_finished:
            self.assertTrue((self.media / "每日更新/sample.nfo").is_file())
            self.assertTrue((self.media / ".localfiles.db").is_file())
            self.assertFalse((self.media / ".tempfiles.db").exists())
        self.calls.append(("GET",))
        return web.json_response(self.libraries, status=self.list_status)

    async def refresh(self, request):
        self.assertEqual(request.headers.get("X-Emby-Token"), self.expected_key)
        self.assertEqual(dict(request.query), {
            "Recursive": "true",
            "MetadataRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
        })
        self.assertEqual(await request.read(), b"")
        item_id = request.match_info["item_id"]
        self.calls.append(("POST", item_id))
        return web.Response(status=500 if item_id in self.failed_ids else 204)

    async def crawler_source(self, request):
        if request.path == "/每日更新/":
            html = '<a href="sample.nfo">sample.nfo</a> 01-Jan-2026 00:00 4\n'
            if self.failed_download:
                html += '<a href="failed.jpg">failed.jpg</a> 01-Jan-2026 00:00 4\n'
            return web.Response(text=html)
        if request.path == "/每日更新/sample.nfo":
            return web.Response(body=b"test")
        if request.path == "/每日更新/failed.jpg":
            return web.Response(status=503)
        if self.failed_directory and request.path == "/综艺/":
            return web.Response(status=500)
        return web.Response(text="<html></html>")

    async def test_selected_libraries_and_direct_connection(self):
        # The environment contains an unusable proxy and no bypass rule.
        self.assertTrue(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [("GET",)] + [("POST", str(i)) for i in range(1, 7)])

    async def test_disabled_zero_downloads_or_sync_errors_skip_all_requests(self):
        self.assertTrue(await refresh_emby_after_sync(0))
        self.assertTrue(await refresh_emby_after_sync(1, errors=1))
        os.environ["EMBY_REFRESH_ENABLED"] = "false"
        self.assertTrue(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [])

    async def test_custom_library_names(self):
        os.environ["EMBY_LIBRARY_NAMES"] = '["综艺"]'
        self.assertTrue(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [("GET",), ("POST", "6")])

    async def test_api_key_file(self):
        key_file = self.media / "api_key.txt"
        key_file.write_text("test-secret\n")
        os.environ["EMBY_API_KEY"] = ""
        os.environ["EMBY_API_KEY_FILE"] = str(key_file)
        self.assertTrue(await refresh_emby_after_sync(1))

    async def test_api_key_file_is_reread_after_atomic_rotation(self):
        key_file = self.media / "api_key.txt"
        key_file.write_text("test-secret\n")
        os.environ.pop("EMBY_API_KEY")
        os.environ["EMBY_API_KEY_FILE"] = str(key_file)
        self.assertTrue(await refresh_emby_after_sync(1))
        replacement = self.media / "replacement.txt"
        replacement.write_text("rotated-secret\n")
        replacement.replace(key_file)
        self.expected_key = "rotated-secret"
        self.calls.clear()
        self.assertTrue(await refresh_emby_after_sync(1))
        self.assertEqual(len(self.calls), 7)

    async def test_missing_key_file_fails_without_requests(self):
        os.environ.pop("EMBY_API_KEY")
        os.environ["EMBY_API_KEY_FILE"] = str(self.media / "missing-key.txt")
        self.assertFalse(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [])

    async def test_empty_key_file_fails_without_requests(self):
        key_file = self.media / "empty-key.txt"
        key_file.write_text("\n")
        os.environ.pop("EMBY_API_KEY")
        os.environ["EMBY_API_KEY_FILE"] = str(key_file)
        self.assertFalse(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [])

    async def test_missing_credentials_or_invalid_names_fail_without_requests(self):
        os.environ["EMBY_API_KEY"] = ""
        self.assertFalse(await refresh_emby_after_sync(1))
        os.environ["EMBY_API_KEY"] = "test-secret"
        for names in ("invalid-json", "{}", "[]", '[""]'):
            os.environ["EMBY_LIBRARY_NAMES"] = names
            self.assertFalse(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [])

    async def test_library_list_failure_does_not_leak_key(self):
        self.list_status = 401
        with self.assertLogs("emd", level="ERROR") as logs:
            self.assertFalse(await refresh_emby_after_sync(1))
        self.assertNotIn("test-secret", "\n".join(logs.output))
        self.assertEqual(self.calls, [("GET",)])

    async def test_missing_library_names_are_reported(self):
        self.libraries = []
        self.assertFalse(await refresh_emby_after_sync(1))
        self.assertEqual(self.calls, [("GET",)])

    async def test_post_failure_does_not_skip_remaining_libraries(self):
        self.failed_ids = {"2"}
        self.assertFalse(await refresh_emby_after_sync(1))
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.calls[-1], ("POST", "6"))

    async def run_sync(self):
        os.environ["NO_PROXY"] = "127.0.0.1"
        args = ["solid.py", "--media", str(self.media), "--url", self.base_url + "/"]
        amount = 2 if self.failed_download else 1
        with patch("sys.argv", args), patch.object(solid, "fetch_pool_from_url", return_value=[]), patch.object(solid, "current_amount", return_value=amount):
            await solid.main()

    async def test_end_to_end_refresh_after_cleanup_and_skip_unchanged_run(self):
        self.expect_sync_finished = True
        await self.run_sync()
        self.assertEqual((self.media / "每日更新/sample.nfo").read_bytes(), b"test")
        self.assertEqual(len(self.calls), 7)
        self.calls.clear()
        await self.run_sync()
        self.assertEqual(self.calls, [])

    async def test_end_to_end_crawl_failure_skips_refresh(self):
        self.failed_directory = True
        await self.run_sync()
        self.assertTrue((self.media / "每日更新/sample.nfo").is_file())
        self.assertEqual(self.calls, [])

    async def test_end_to_end_partial_download_failure_skips_refresh(self):
        self.failed_download = True
        await self.run_sync()
        self.assertEqual((self.media / "每日更新/sample.nfo").read_bytes(), b"test")
        self.assertFalse((self.media / "每日更新/failed.jpg").exists())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
