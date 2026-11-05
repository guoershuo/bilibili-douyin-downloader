from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import douyin_downloader as dd  # noqa: E402


class ExtractUrlsTests(unittest.TestCase):
    def test_extracts_url_from_share_text(self):
        text = (
            "7.43 复制打开抖音，看看【某人的作品】这个视频真好看 "
            "https://v.douyin.com/AbCdEfG/ 复制此链接，打开Dou音搜索"
        )
        self.assertEqual(
            dd.extract_urls(text),
            ["https://v.douyin.com/AbCdEfG/"],
        )

    def test_extracts_multiple_and_deduplicates(self):
        text = """
        https://www.douyin.com/video/7300000000000000001
        https://www.douyin.com/video/7300000000000000001
        https://v.douyin.com/xxxx/
        """
        self.assertEqual(
            dd.extract_urls(text),
            [
                "https://www.douyin.com/video/7300000000000000001",
                "https://v.douyin.com/xxxx/",
            ],
        )

    def test_ignores_non_douyin_and_lookalike_domains(self):
        text = (
            "https://example.com/video/123 "
            "https://douyin.com.evil.example/video/123 "
            "https://notdouyin.com/video/123 "
            "https://www.douyin.com/video/7300000000000000002"
        )
        self.assertEqual(
            dd.extract_urls(text),
            ["https://www.douyin.com/video/7300000000000000002"],
        )

    def test_upgrades_http_to_https(self):
        self.assertEqual(
            dd.extract_urls("http://www.douyin.com/video/7300000000000000003"),
            ["https://www.douyin.com/video/7300000000000000003"],
        )

    def test_does_not_swallow_chinese_punctuation(self):
        text = "看这个 https://v.douyin.com/abc/，很好看！"
        self.assertEqual(dd.extract_urls(text), ["https://v.douyin.com/abc/"])


class UrlHelpersTests(unittest.TestCase):
    def test_extract_aweme_id_patterns(self):
        self.assertEqual(
            dd.extract_aweme_id("https://www.douyin.com/video/7300000000000000001"),
            "7300000000000000001",
        )
        self.assertEqual(
            dd.extract_aweme_id(
                "https://www.iesdouyin.com/share/video/7300000000000000002/?region=CN"
            ),
            "7300000000000000002",
        )
        self.assertEqual(
            dd.extract_aweme_id(
                "https://www.douyin.com/discover?modal_id=7300000000000000003"
            ),
            "7300000000000000003",
        )

    def test_extract_sec_uid(self):
        self.assertEqual(
            dd.extract_sec_uid(
                "https://www.douyin.com/user/MS4wLjABAAAAexample?from_tab_name=main"
            ),
            "MS4wLjABAAAAexample",
        )
        self.assertIsNone(dd.extract_sec_uid("https://www.douyin.com/video/123"))

    def test_canonical_video_url(self):
        self.assertEqual(
            dd.canonical_video_url(
                "https://www.iesdouyin.com/share/video/7300000000000000002/?a=1"
            ),
            "https://www.douyin.com/video/7300000000000000002",
        )

    def test_prepare_url_keeps_user_page(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAA"
        self.assertEqual(dd.prepare_url(url), url)

    def test_prepare_url_rejects_note(self):
        with self.assertRaises(dd.ResolutionError):
            dd.prepare_url("https://www.douyin.com/note/7300000000000000004")

    def test_resolve_short_url(self):
        final = "https://www.iesdouyin.com/share/video/7300000000000000002/?region=CN"

        class FakeResponse:
            def __init__(self, url):
                self.url = url

            def geturl(self):
                return self.url

            def read(self, _size):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch.object(dd, "urlopen", return_value=FakeResponse(final)):
            self.assertEqual(dd.prepare_url("https://v.douyin.com/abc/"), "https://www.douyin.com/video/7300000000000000002")


class ParseHelpersTests(unittest.TestCase):
    def test_parse_browser_spec(self):
        self.assertEqual(dd.parse_browser_spec("edge"), ("edge", None, None, None))
        self.assertEqual(
            dd.parse_browser_spec("chrome:Profile 1"),
            ("chrome", "Profile 1", None, None),
        )
        self.assertEqual(
            dd.parse_browser_spec("firefox+keyring:default::work"),
            ("firefox", "default", "keyring", "work"),
        )

    def test_parse_rate(self):
        self.assertEqual(dd.parse_rate("500K"), 500 * 1024)
        self.assertEqual(dd.parse_rate("1.5M"), int(1.5 * 1024**2))
        self.assertEqual(dd.parse_rate("2048"), 2048)
        with self.assertRaises(Exception):
            dd.parse_rate("fast")

    def test_cookie_error_detection(self):
        self.assertTrue(dd.is_cookie_error(Exception("Fresh cookies are needed")))
        self.assertTrue(dd.is_cookie_error(Exception("请先登录")))
        self.assertFalse(dd.is_cookie_error(Exception("Video unavailable")))
        self.assertTrue(
            dd.is_browser_cookie_error(Exception("Permission denied: temporary.sqlite"))
        )

    def test_permanent_browser_error_detection(self):
        self.assertTrue(dd.is_permanent_browser_error(Exception("无法获取视频：作品不见了")))
        self.assertFalse(dd.is_permanent_browser_error(Exception("Uifid Not Found")))


class BrowserWindowArgTests(unittest.TestCase):
    def parse(self, argv):
        return dd.build_parser().parse_args(argv)

    def test_defaults_to_headless_and_persistent_profile(self):
        args = self.parse(["https://www.douyin.com/video/7300000000000000001"])
        self.assertFalse(args.visible)
        self.assertFalse(args.temp_profile)
        self.assertIsNone(args.browser_profile)
        self.assertEqual(dd.resolve_window_mode(args), "headless")

        profile = dd.resolve_profile_dir(args)
        self.assertEqual(profile.name, ".browser-profile")
        self.assertEqual(profile.parent, Path(dd.__file__).resolve().parent)

    def test_visible_forces_visible_window(self):
        args = self.parse(["--visible", "https://www.douyin.com/video/1"])
        self.assertEqual(dd.resolve_window_mode(args), "visible")

    def test_temp_profile_disables_persistence(self):
        args = self.parse(["--temp-profile", "https://www.douyin.com/video/1"])
        self.assertIsNone(dd.resolve_profile_dir(args))

    def test_browser_profile_overrides_default_dir(self):
        args = self.parse(["--browser-profile", "my-profile", "https://www.douyin.com/video/1"])
        resolved = dd.resolve_profile_dir(args)
        self.assertEqual(resolved, Path("my-profile").resolve())

    def test_temp_profile_conflicts_with_browser_profile(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                dd.main([
                    "--browser-profile", "my-profile",
                    "--temp-profile",
                    "https://www.douyin.com/video/7300000000000000001",
                ])
        self.assertIn("只能选一个", stderr.getvalue())

    def test_help_lists_new_options(self):
        help_text = dd.build_parser().format_help()
        self.assertIn("--temp-profile", help_text)
        self.assertIn("--visible", help_text)
        self.assertIn(".browser-profile", help_text)
        self.assertIn("无头", help_text)

    def test_login_cookies_definition(self):
        self.assertEqual(dd.DOUYIN_LOGIN_COOKIES, ("sessionid|sessionid_ss",))


class SystemProxyTests(unittest.TestCase):
    VIDEO = "https://www.douyin.com/video/7300000000000000001"

    def parse(self, argv):
        return dd.build_parser().parse_args(argv)

    def test_uses_system_proxy_when_no_explicit_proxy(self):
        args = self.parse([self.VIDEO])
        with patch.object(dd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
            options = dd.build_ydl_options(args, Path("out"))
        self.assertEqual(options["proxy"], "http://127.0.0.1:7897")

    def test_no_system_proxy_flag_disables_detection(self):
        args = self.parse(["--no-system-proxy", self.VIDEO])
        with patch.object(dd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
            options = dd.build_ydl_options(args, Path("out"))
        self.assertNotIn("proxy", options)

    def test_explicit_proxy_beats_system_proxy(self):
        args = self.parse(["--proxy", "http://1.2.3.4:8080", self.VIDEO])
        with patch.object(dd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
            options = dd.build_ydl_options(args, Path("out"))
        self.assertEqual(options["proxy"], "http://1.2.3.4:8080")

    def test_no_proxy_key_when_detection_returns_none(self):
        args = self.parse([self.VIDEO])
        with patch.object(dd, "detect_system_proxy", return_value=None):
            options = dd.build_ydl_options(args, Path("out"))
        self.assertNotIn("proxy", options)

    def test_help_and_default_for_no_system_proxy(self):
        self.assertFalse(self.parse([self.VIDEO]).no_system_proxy)
        self.assertIn("--no-system-proxy", dd.build_parser().format_help())


def make_sample_detail() -> dict:
    """实测 aweme_detail 形态：bit_rate[] 里 5 个 gear + 3 条默认流。"""

    def stream(height, width, url, uri=None):
        node = {"height": height, "width": width, "url_list": [url]}
        if uri:
            node["uri"] = uri
        return node

    gears = [
        ("720_2_1", 824000, 1280, 720),
        ("720_3_1", 644000, 1280, 720),
        ("720_4_1", 574000, 1280, 720),
        ("540_1_1", 827000, 1024, 576),
        ("540_2_1", 603000, 1024, 576),
    ]
    return {
        "aweme_id": "7300000000000000001",
        "desc": "烧烤毕业季，大学生露营烧烤就业指南全流程！",
        "author": {"nickname": "露营老王"},
        "video": {
            "duration": 209200,
            "width": 1280,
            "play_addr": stream(
                1024, 576, "https://v3-web.douyinvod.com/play_default.mp4?a=1", "v0200default"
            ),
            "play_addr_265": stream(
                1280, 720, "https://v3-web.douyinvod.com/play_265.mp4?a=1"
            ),
            "bit_rate": [
                {
                    "gear_name": gear,
                    "is_h265": True,
                    "bit_rate": bitrate,
                    "play_addr": stream(
                        height, width, f"https://v3-web.douyinvod.com/{gear}.mp4?a=1"
                    ),
                }
                for gear, bitrate, height, width in gears
            ],
        },
    }


class _FakeBrowserDownloader:
    """替身：不启动浏览器，直接返回构造好的 aweme_detail 或抛异常。"""

    detail: dict | None = None
    error: BaseException | None = None
    instances: list = []

    def __init__(self, output_dir, args, *, login_cookies=()):
        self.output_dir = output_dir
        self.args = args
        self.login_cookies = login_cookies
        self.start_calls: list = []
        self.closed = False
        self.aweme_id = None
        _FakeBrowserDownloader.instances.append(self)

    def start(self, prompt_login=True):
        self.start_calls.append(prompt_login)
        return self

    def fetch_detail(self, aweme_id, retries=None):
        if type(self).error is not None:
            raise type(self).error
        self.aweme_id = aweme_id
        return type(self).detail or {}

    def close(self):
        self.closed = True


class ListQualitiesTests(unittest.TestCase):
    def setUp(self):
        _FakeBrowserDownloader.instances = []
        _FakeBrowserDownloader.detail = make_sample_detail()
        _FakeBrowserDownloader.error = None
        patcher = patch.object(dd, "BrowserDownloader", _FakeBrowserDownloader)
        self.addCleanup(patcher.stop)
        patcher.start()
        url_patcher = patch.object(
            dd, "prepare_url", lambda url, timeout=20.0: "https://www.douyin.com/video/7300000000000000001"
        )
        self.addCleanup(url_patcher.stop)
        url_patcher.start()

    def call(self, **kwargs):
        return dd.list_qualities("https://v.douyin.com/zweK0YH0djA/", **kwargs)

    def test_ok_payload(self):
        payload = self.call()
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["title"], "烧烤毕业季，大学生露营烧烤就业指南全流程！")
        self.assertEqual(payload["author"], "露营老王")
        self.assertEqual(payload["duration"], 209.2)
        self.assertEqual(len(payload["qualities"]), 8)
        self.assertEqual(payload["recommended"], "720_2_1")
        self.assertEqual(
            set(payload), {"ok", "error", "title", "author", "duration", "qualities", "recommended"}
        )

    def test_is_read_only(self):
        payload = self.call()
        self.assertTrue(payload["ok"])
        downloader = _FakeBrowserDownloader.instances[-1]
        self.assertEqual(downloader.start_calls, [False])  # 不弹登录窗口
        self.assertTrue(downloader.closed)
        self.assertFalse(hasattr(downloader, "download"))

    def test_options_are_forwarded(self):
        self.call(
            browser="C:/chrome.exe",
            profile_dir="C:/profile-x",
            window_mode="offscreen",
            timeout=12.5,
        )
        downloader = _FakeBrowserDownloader.instances[-1]
        self.assertEqual(downloader.args.browser, "C:/chrome.exe")
        self.assertEqual(downloader.args.window_mode, "offscreen")
        self.assertEqual(downloader.args.timeout, 12.5)
        self.assertEqual(str(downloader.args.browser_profile).replace("\\", "/"), "C:/profile-x")
        self.assertEqual(downloader.aweme_id, "7300000000000000001")

    def test_risk_control_error(self):
        _FakeBrowserDownloader.error = dd.BrowserError(
            "抖音接口拒绝了浏览器请求（Uifid/风控拦截）：Blocked by ArgusSecurityPlugin"
        )
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("风控", payload["error"])
        self.assertIn("--visible", payload["error"])
        self.assertEqual(payload["qualities"], [])
        self.assertIsNone(payload["recommended"])

    def test_missing_video_error(self):
        _FakeBrowserDownloader.error = dd.BrowserError("无法获取视频：作品不见了")
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("视频不存在", payload["error"])

    def test_network_error(self):
        _FakeBrowserDownloader.error = dd.BrowserError(
            "浏览器调试端口没有启动：<urlopen error timed out>"
        )
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("网络或浏览器连接失败", payload["error"])

    def test_unknown_error_is_wrapped(self):
        _FakeBrowserDownloader.error = RuntimeError("boom")
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("读取清晰度失败", payload["error"])
        self.assertIn("boom", payload["error"])

    def test_empty_detail_reports_error(self):
        _FakeBrowserDownloader.detail = {"video": {}}
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("没有解析到任何清晰度档位", payload["error"])

    def test_user_page_url_is_rejected(self):
        with patch.object(
            dd, "prepare_url", lambda url, timeout=20.0: "https://www.douyin.com/user/MS4wLjABAAAA"
        ):
            payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("不是单个抖音视频", payload["error"])

    def test_resolution_error_is_reported(self):
        def boom(url, timeout=20.0):
            raise dd.ResolutionError("短链接解析超时")

        with patch.object(dd, "prepare_url", boom):
            payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("链接解析失败", payload["error"])
        self.assertIn("短链接解析超时", payload["error"])

    def test_never_raises(self):
        _FakeBrowserDownloader.error = MemoryError("boom")
        payload = self.call()
        self.assertFalse(payload["ok"])
        self.assertIn("读取清晰度失败", payload["error"])


class DescribeQualityErrorTests(unittest.TestCase):
    def test_classification(self):
        self.assertIn(
            "风控", dd.describe_quality_error(Exception("Blocked by ArgusSecurityPlugin"))
        )
        self.assertIn("风控", dd.describe_quality_error(Exception("需要登录后查看")))
        self.assertIn("视频不存在", dd.describe_quality_error(Exception("无法获取视频：作品不见了")))
        self.assertIn(
            "网络或浏览器连接失败",
            dd.describe_quality_error(Exception("URLError: <urlopen error timed out>")),
        )
        self.assertIn("读取清晰度失败", dd.describe_quality_error(Exception("奇怪的问题")))
        self.assertEqual(dd.describe_quality_error(Exception("")), "读取清晰度失败：Exception")


class PrintQualityTableTests(unittest.TestCase):
    def test_table_contains_label_bitrate_gear_and_recommendation(self):
        payload = {
            "ok": True,
            "error": None,
            "title": "烧烤毕业季",
            "author": "露营老王",
            "duration": 209.2,
            "qualities": dd.build_qualities(make_sample_detail()),
            "recommended": "720_2_1",
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            dd.print_quality_table(payload)
        output = buffer.getvalue()
        self.assertIn("时长：209.2 秒", output)
        self.assertIn("720P", output)
        self.assertIn("824 kbps", output)
        self.assertIn("720_2_1", output)
        self.assertIn("* 720P", output)
        self.assertIn("实际最高档", output)
        self.assertIn("--gear 720_2_1", output)

    def test_empty_qualities(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            dd.print_quality_table({"ok": False, "error": "x", "qualities": [], "recommended": None})
        self.assertIn("可用清晰度：无", buffer.getvalue())


class GearArgTests(unittest.TestCase):
    def parse(self, argv):
        return dd.build_parser().parse_args(argv)

    def test_gear_default_none(self):
        self.assertIsNone(self.parse(["https://v.douyin.com/abc/"]).gear)

    def test_gear_is_parsed(self):
        args = self.parse(["--gear", "720_2_1", "https://v.douyin.com/abc/"])
        self.assertEqual(args.gear, "720_2_1")

    def test_list_qualities_flag(self):
        self.assertTrue(self.parse(["--list-qualities", "url"]).list_qualities)

    def test_gear_format_note_only_on_conflict(self):
        self.assertIsNone(dd.gear_format_note(argparse.Namespace(gear=None, format="best")))
        self.assertIsNone(dd.gear_format_note(argparse.Namespace(gear="720_2_1", format="best")))
        note = dd.gear_format_note(argparse.Namespace(gear="720_2_1", format="worst"))
        self.assertIn("--gear 720_2_1", note)
        self.assertIn("优先于 --format worst", note)

    def test_gear_requires_browser_engine(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                dd.main(["--gear", "720_2_1", "--engine", "yt-dlp", "https://v.douyin.com/abc/"])
        self.assertIn("--gear 需要浏览器引擎", stderr.getvalue())

    def test_help_lists_new_options(self):
        help_text = dd.build_parser().format_help()
        self.assertIn("--list-qualities", help_text)
        self.assertIn("--gear", help_text)


if __name__ == "__main__":
    unittest.main()
