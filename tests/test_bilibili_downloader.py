from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bilibili_downloader as bd  # noqa: E402


class ExtractUrlsTests(unittest.TestCase):
    def test_extracts_url_from_share_text(self):
        text = "【标题】这个视频真好看 https://www.bilibili.com/video/BV1wh4P6XEkC 复制此链接打开"
        self.assertEqual(
            bd.extract_urls(text),
            ["https://www.bilibili.com/video/BV1wh4P6XEkC"],
        )

    def test_extracts_multiple_and_deduplicates(self):
        text = """
        https://www.bilibili.com/video/BV1wh4P6XEkC
        https://space.bilibili.com/3546984120059960/video
        https://www.bilibili.com/video/BV1wh4P6XEkC
        """
        self.assertEqual(
            bd.extract_urls(text),
            [
                "https://www.bilibili.com/video/BV1wh4P6XEkC",
                "https://space.bilibili.com/3546984120059960/video",
            ],
        )

    def test_ignores_lookalike_domains(self):
        text = (
            "https://example.com/video/BV1wh4P6XEkC "
            "https://bilibili.com.evil.example/video/BV1wh4P6XEkC "
            "https://notbilibili.com/video/BV1wh4P6XEkC "
            "https://www.bilibili.com/video/BV1PF4g6ME26"
        )
        self.assertEqual(
            bd.extract_urls(text),
            ["https://www.bilibili.com/video/BV1PF4g6ME26"],
        )

    def test_upgrades_http_to_https(self):
        self.assertEqual(
            bd.extract_urls("http://www.bilibili.com/video/BV1wh4P6XEkC"),
            ["https://www.bilibili.com/video/BV1wh4P6XEkC"],
        )

    def test_does_not_swallow_chinese_punctuation(self):
        text = "看这个 https://www.bilibili.com/video/BV1wh4P6XEkC，很好看！"
        self.assertEqual(
            bd.extract_urls(text),
            ["https://www.bilibili.com/video/BV1wh4P6XEkC"],
        )

    def test_accepts_short_link(self):
        self.assertEqual(bd.extract_urls("https://b23.tv/abcd1234"), ["https://b23.tv/abcd1234"])


class IdParsingTests(unittest.TestCase):
    def test_extract_bvid_from_url(self):
        self.assertEqual(
            bd.extract_bvid("https://www.bilibili.com/video/BV1wh4P6XEkC?p=1"),
            "BV1wh4P6XEkC",
        )

    def test_extract_bvid_from_share_text(self):
        self.assertEqual(bd.extract_bvid("【标题】BV1wh4P6XEkC 复制打开"), "BV1wh4P6XEkC")

    def test_extract_bvid_rejects_longer_token(self):
        self.assertIsNone(bd.extract_bvid("XXBV1wh4P6XEkCXX"))

    def test_extract_av_id(self):
        self.assertEqual(bd.extract_av_id("https://www.bilibili.com/video/av170001"), "170001")
        self.assertIsNone(bd.extract_av_id("https://www.bilibili.com/video/BV1wh4P6XEkC"))

    def test_extract_mid_from_space_url(self):
        self.assertEqual(
            bd.extract_mid("https://space.bilibili.com/3546984120059960/video"), "3546984120059960"
        )

    def test_extract_mid_from_bare_number(self):
        self.assertEqual(bd.extract_mid("3546984120059960"), "3546984120059960")
        self.assertIsNone(bd.extract_mid("BV1wh4P6XEkC"))

    def test_url_builders(self):
        self.assertEqual(
            bd.video_url("BV1wh4P6XEkC"), "https://www.bilibili.com/video/BV1wh4P6XEkC"
        )
        self.assertEqual(
            bd.space_url("123"), "https://space.bilibili.com/123/video"
        )

    def test_describe_target(self):
        self.assertEqual(
            bd.describe_target("https://space.bilibili.com/123/video"), ("space", "123")
        )
        self.assertEqual(
            bd.describe_target("https://www.bilibili.com/video/BV1wh4P6XEkC"),
            ("video", "BV1wh4P6XEkC"),
        )
        self.assertEqual(
            bd.describe_target("https://www.bilibili.com/video/av170001"), ("video", "av170001")
        )
        self.assertEqual(bd.describe_target("https://example.com/x"), ("other", "https://example.com/x"))

    def test_is_short_link(self):
        self.assertTrue(bd.is_short_link("https://b23.tv/abcd1234"))
        self.assertFalse(bd.is_short_link("https://www.bilibili.com/video/BV1wh4P6XEkC"))


class CollectUrlsTests(unittest.TestCase):
    def parse(self, argv):
        return bd.build_parser().parse_args(argv)

    def test_collects_links_and_up(self):
        args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC", "--up", "123"])
        self.assertEqual(
            bd.collect_urls(args),
            [
                "https://www.bilibili.com/video/BV1wh4P6XEkC",
                "https://space.bilibili.com/123/video",
            ],
        )

    def test_reads_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "links.txt"
            path.write_text(
                "https://www.bilibili.com/video/BV1wh4P6XEkC\nBV1PF4g6ME26\n",
                encoding="utf-8",
            )
            args = self.parse(["--file", str(path)])
            self.assertEqual(
                bd.collect_urls(args),
                [
                    "https://www.bilibili.com/video/BV1wh4P6XEkC",
                    "https://www.bilibili.com/video/BV1PF4g6ME26",
                ],
            )

    def test_bare_bv_and_av_on_command_line(self):
        args = self.parse(["BV1wh4P6XEkC", "av170001"])
        self.assertEqual(
            bd.collect_urls(args),
            [
                "https://www.bilibili.com/video/BV1wh4P6XEkC",
                "https://www.bilibili.com/video/av170001",
            ],
        )

    def test_bare_mid_line_becomes_space_url(self):
        args = self.parse(["3546984120059960", "BV1wh4P6XEkC"])
        self.assertEqual(
            bd.collect_urls(args),
            [
                "https://space.bilibili.com/3546984120059960/video",
                "https://www.bilibili.com/video/BV1wh4P6XEkC",
            ],
        )

    def test_parse_rate(self):
        self.assertEqual(bd.parse_rate("500K"), 500 * 1024)
        self.assertEqual(bd.parse_rate("1.5M"), int(1.5 * 1024 ** 2))
        self.assertEqual(bd.parse_rate("2048"), 2048)
        with self.assertRaises(Exception):
            bd.parse_rate("fast")


class CookieTests(unittest.TestCase):
    SAMPLE = (
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tabc%2Cdef\n"
        ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tbili_jct\ttoken\n"
        ".example.com\tTRUE\t/\tFALSE\t1999999999\tOTHER\tx\n"
        "\n"
        "bad-line-without-tabs\n"
    )

    def test_parse_skips_comments_and_broken_lines(self):
        cookies = bd.parse_netscape_cookies(self.SAMPLE)
        self.assertEqual([c["name"] for c in cookies], ["SESSDATA", "bili_jct", "OTHER"])

    def test_parses_httponly_prefixed_lines(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tsecret\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tbuvid3\tval\n"
        )
        cookies = bd.parse_netscape_cookies(text)
        self.assertEqual([c["name"] for c in cookies], ["SESSDATA", "buvid3"])
        self.assertTrue(bd.has_login_cookie(cookies))
        self.assertIn("SESSDATA=secret", bd.cookie_header_for(cookies))

    def test_exported_cookie_file_is_recognised(self):
        # browser_engine 导出的 HttpOnly Cookie 必须能被自己读回来
        text = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tabc\n"
        )
        cookies = bd.parse_netscape_cookies(text)
        self.assertTrue(bd.has_login_cookie(cookies))

    def test_has_login_cookie(self):
        cookies = bd.parse_netscape_cookies(self.SAMPLE)
        self.assertTrue(bd.has_login_cookie(cookies))
        self.assertFalse(bd.has_login_cookie([]))
        self.assertEqual(bd.cookie_names(cookies), {"SESSDATA", "bili_jct", "OTHER"})

    def test_cookie_header_only_keeps_bilibili_entries(self):
        cookies = bd.parse_netscape_cookies(self.SAMPLE)
        header = bd.cookie_header_for(cookies)
        self.assertIn("SESSDATA=abc%2Cdef", header)
        self.assertNotIn("OTHER", header)

    def test_inspect_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = bd.inspect_cookie_file(Path(tmp) / "cookies.txt")
        self.assertFalse(state["exists"])
        self.assertFalse(state["logged_in"])
        self.assertEqual(state["count"], 0)

    def test_inspect_valid_and_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(self.SAMPLE, encoding="utf-8")
            state = bd.inspect_cookie_file(path)
            self.assertTrue(state["logged_in"])
            self.assertFalse(state["expired"])

            expired = Path(tmp) / "old.txt"
            expired.write_text(
                ".bilibili.com\tTRUE\t/\tFALSE\t1000000000\tSESSDATA\tx\n", encoding="utf-8"
            )
            old = bd.inspect_cookie_file(expired)
            self.assertTrue(old["expired"])
            self.assertFalse(old["logged_in"])
            self.assertTrue(old["has_sessdata"])

    def test_resolve_cookie_file_defaults_to_script_dir(self):
        args = bd.build_parser().parse_args([])
        script_dir = Path("E:/tmp/project")
        self.assertEqual(
            bd.resolve_cookie_file(args, script_dir), script_dir / bd.COOKIE_FILE_NAME
        )

    def test_resolve_cookie_file_honours_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.txt"
            path.write_text("", encoding="utf-8")
            args = bd.build_parser().parse_args(["--cookies", str(path)])
            self.assertEqual(bd.resolve_cookie_file(args, Path(tmp)), path)


class FfmpegTests(unittest.TestCase):
    def test_search_paths_prefer_project_tools(self):
        paths = bd.ffmpeg_search_paths(Path("E:/proj"), env={})
        self.assertEqual(paths[0], str(Path("E:/proj/tools/ffmpeg.exe")))
        self.assertIn(r"D:\EVCapture\ffmpeg.exe", paths)

    def test_env_override_replaces_common_paths(self):
        env = {bd.FFMPEG_PATHS_ENV: os.pathsep.join([r"X:\a\ffmpeg.exe", r"X:\b\ffmpeg.exe"])}
        paths = bd.ffmpeg_search_paths(Path("E:/proj"), env=env)
        self.assertIn(r"X:\a\ffmpeg.exe", paths)
        self.assertNotIn(r"D:\EVCapture\ffmpeg.exe", paths)

    def test_empty_override_means_no_common_scan(self):
        paths = bd.ffmpeg_search_paths(Path("E:/proj"), env={bd.FFMPEG_PATHS_ENV: ""})
        self.assertEqual(len(paths), 2)

    def test_no_scan_sentinel(self):
        for value in ("none", "OFF", "0"):
            paths = bd.ffmpeg_search_paths(Path("E:/proj"), env={bd.FFMPEG_PATHS_ENV: value})
            self.assertNotIn(r"D:EVCapturefmpeg.exe", paths)
            self.assertEqual(len(paths), 2)

    def test_find_ffmpeg_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg.exe"
            fake.write_bytes(b"x")
            self.assertEqual(bd.find_ffmpeg(str(fake), env={}), str(fake))

    def test_find_ffmpeg_via_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg.exe"
            fake.write_bytes(b"x")
            env = {"PATH": "", bd.FFMPEG_PATHS_ENV: str(fake)}
            self.assertEqual(bd.find_ffmpeg(None, Path(tmp), env), str(fake))

    def test_find_ffmpeg_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "", bd.FFMPEG_PATHS_ENV: ""}
            self.assertIsNone(bd.find_ffmpeg(None, Path(tmp), env))

    def test_require_ffmpeg_raises_with_install_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "", bd.FFMPEG_PATHS_ENV: ""}
            with self.assertRaises(bd.FfmpegMissingError) as ctx:
                bd.require_ffmpeg(None, Path(tmp), env)
            message = str(ctx.exception)
            self.assertIn("没有找到 ffmpeg", message)
            self.assertIn("gyan.dev", message)
            self.assertIn("tools\\ffmpeg.exe", message)

    def test_require_ffmpeg_rejects_bad_explicit_path(self):
        with self.assertRaises(bd.FfmpegMissingError):
            bd.require_ffmpeg(r"X:\nope\ffmpeg.exe", env={})


class QualityTests(unittest.TestCase):
    INFO = {
        "formats": [
            {"format_id": "30280", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1720000},
            {"format_id": "30032", "height": 480, "vcodec": "avc1.640033", "acodec": "none"},
            {"format_id": "30080", "height": 1080, "vcodec": "avc1.640033", "acodec": "none"},
            {"format_id": "30077", "height": 1080, "vcodec": "hvc1.1.6.L150.90", "acodec": "none"},
        ]
    }

    def test_build_format_selector(self):
        self.assertEqual(
            bd.build_format_selector("1080"),
            "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        )
        self.assertEqual(
            bd.build_format_selector("720"),
            "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        )
        self.assertEqual(bd.build_format_selector("best"), "bv*+ba/b")

    def test_max_video_height_ignores_audio_only(self):
        self.assertEqual(bd.max_video_height(self.INFO), 1080)
        self.assertIsNone(bd.max_video_height({"formats": []}))

    def test_quality_rows_covers_audio_and_video(self):
        rows = bd.quality_rows(self.INFO)
        self.assertEqual(len(rows), 4)
        self.assertTrue(any("音频" in row for row in rows))
        self.assertTrue(any("1080P" in row for row in rows))

    def test_describe_quality_gap(self):
        self.assertIsNone(bd.describe_quality_gap(self.INFO, "1080"))
        self.assertIsNone(bd.describe_quality_gap(self.INFO, "best"))
        low = {"formats": [{"format_id": "1", "height": 480, "vcodec": "avc1", "acodec": "none"}]}
        gap = bd.describe_quality_gap(low, "1080")
        self.assertIsNotNone(gap)
        self.assertIn("480P", gap)
        self.assertIn("--login-once", gap)


class WbiTests(unittest.TestCase):
    # 真实接口取到的 WBI key 与对应 mixinKey（2026-05 实测）。
    IMG_KEY = "7cd084941338484aae1ad9425b84077c"
    SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
    MIXIN = "ea1db124af3c7062474693fa704f4ff8"

    def test_mixin_key_matches_real_value(self):
        self.assertEqual(bd.mixin_key_from(self.IMG_KEY, self.SUB_KEY), self.MIXIN)

    def test_mixin_key_rejects_short_keys(self):
        with self.assertRaises(bd.BilibiliError):
            bd.mixin_key_from("abc", "def")

    def test_key_from_image_url(self):
        self.assertEqual(
            bd._key_from_image_url(
                "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
            ),
            self.IMG_KEY,
        )

    def test_sign_params_shape(self):
        client = bd.WbiClient(opener=object())
        client._img_key = self.IMG_KEY
        client._sub_key = self.SUB_KEY
        query = client.sign_params({"mid": "123", "ps": "30", "pn": "1"})
        self.assertIn("w_rid=", query)
        self.assertIn("wts=", query)
        params = query.split("&")
        keys = [item.split("=")[0] for item in params]
        self.assertEqual(keys, ["mid", "pn", "ps", "wts", "w_rid"])
        digest = query.rsplit("w_rid=", 1)[1]
        self.assertRegex(digest, r"^[0-9a-f]{32}$")

    def test_sign_params_changes_with_time(self):
        client = bd.WbiClient(opener=object())
        client._img_key = self.IMG_KEY
        client._sub_key = self.SUB_KEY
        with patch.object(bd.time, "time", return_value=1700000000):
            first = client.sign_params({"mid": "1"})
        with patch.object(bd.time, "time", return_value=1700000100):
            second = client.sign_params({"mid": "1"})
        self.assertNotEqual(first, second)

    def test_space_error_classification(self):
        self.assertIsInstance(bd._space_error(-101, "账号未登录"), bd.LoginRequiredError)
        self.assertIsInstance(bd._space_error(-352, "风控校验失败"), bd.LoginRequiredError)
        err = bd._space_error(-404, "啥都木有")
        self.assertIsInstance(err, bd.BilibiliError)
        self.assertNotIsInstance(err, bd.LoginRequiredError)
        self.assertIn("-404", str(err))


class FakeSession:
    def __init__(self, *, ready_after: int = 1, raise_on_export: bool = False) -> None:
        self.calls = 0
        self.ready_after = ready_after
        self.raise_on_export = raise_on_export
        self.navigated: list[str] = []
        self.exported: Path | None = None
        self.closed = False
        self.factory_kwargs: dict[str, object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def navigate(self, url: str) -> None:
        self.navigated.append(url)

    def is_logged_in(self, names) -> bool:
        self.calls += 1
        return self.calls >= self.ready_after

    def export_cookies(self, path) -> Path:
        if self.raise_on_export:
            raise RuntimeError("boom")
        target = Path(path)
        target.write_text(
            ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tabc\n", encoding="utf-8"
        )
        self.exported = target
        return target


class DefaultSessionFactoryTests(unittest.TestCase):
    """default_session_factory 必须让登录窗口直接停在 B 站首页，不能先串到抖音。"""

    def build(self, **kwargs):
        import browser_engine as be

        captured: dict[str, object] = {}

        class FakeBrowserSession:
            def __init__(self, browser, **session_kwargs):
                captured["browser"] = browser
                captured.update(session_kwargs)

        with patch.object(be, "BrowserSession", FakeBrowserSession):
            with patch.object(be, "find_browser", lambda explicit=None: "fake-browser"):
                with patch.object(
                    be, "default_profile_dir", lambda base=None: Path("profile-x")
                ):
                    bd.default_session_factory(**kwargs)
        return captured

    def test_factory_passes_bilibili_initial_url(self):
        captured = self.build(window_mode="visible", wait_seconds=3.0)
        self.assertEqual(captured["initial_url"], bd.BILIBILI_HOME)
        self.assertEqual(captured["window_mode"], "visible")
        self.assertEqual(captured["profile_dir"], Path("profile-x"))
        self.assertEqual(captured["browser"], "fake-browser")

    def test_factory_honours_explicit_initial_url(self):
        self.assertIsNone(self.build(initial_url=None)["initial_url"])
        self.assertEqual(
            self.build(initial_url="https://example.test/")["initial_url"],
            "https://example.test/",
        )

    def test_factory_keeps_explicit_browser_and_profile(self):
        captured = self.build(browser_path="custom.exe", profile_dir=Path("my-profile"))
        self.assertEqual(captured["browser"], "custom.exe")
        self.assertEqual(captured["profile_dir"], Path("my-profile"))


class LoginFlowTests(unittest.TestCase):
    def test_run_login_polls_then_exports(self):
        session = FakeSession(ready_after=3)
        sleeps: list[float] = []

        def factory(**kwargs):
            session.factory_kwargs = kwargs
            return session

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cookies.txt"
            result = bd.run_login(
                target,
                session_factory=factory,
                sleep=sleeps.append,
                clock=lambda: 0.0,
            )
            self.assertEqual(result, target)
            self.assertTrue(target.is_file())
        self.assertEqual(session.calls, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(session.navigated, [bd.BILIBILI_HOME])
        self.assertTrue(session.closed)
        self.assertEqual(session.factory_kwargs.get("window_mode"), "visible")

    def test_run_login_times_out(self):
        session = FakeSession(ready_after=10 ** 6)
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bd.LoginRequiredError):
                bd.run_login(
                    Path(tmp) / "cookies.txt",
                    timeout=10.0,
                    poll_interval=2.0,
                    session_factory=lambda **kwargs: session,
                    sleep=sleep,
                    clock=clock,
                )
        self.assertTrue(session.closed)

    def test_run_login_wraps_browser_failure(self):
        def factory(**_kwargs):
            raise OSError("cannot start chromium")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bd.BilibiliError) as ctx:
                bd.run_login(Path(tmp) / "cookies.txt", session_factory=factory)
        self.assertIn("无法启动浏览器", str(ctx.exception))

    def test_run_login_wraps_export_failure(self):
        session = FakeSession(ready_after=1, raise_on_export=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bd.BilibiliError) as ctx:
                bd.run_login(
                    Path(tmp) / "cookies.txt",
                    session_factory=lambda **kwargs: session,
                    sleep=lambda _s: None,
                )
        self.assertIn("导出 Cookie 失败", str(ctx.exception))


class YdlOptionsTests(unittest.TestCase):
    def parse(self, argv):
        return bd.build_parser().parse_args(argv)

    def test_defaults(self):
        args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC"])
        options = bd.build_ydl_options(args, Path("out"), None, r"D:\ff\ffmpeg.exe")
        self.assertEqual(options["format"], bd.build_format_selector("1080"))
        self.assertEqual(options["merge_output_format"], "mp4")
        self.assertEqual(options["ffmpeg_location"], r"D:\ff\ffmpeg.exe")
        self.assertNotIn("cookiefile", options)
        self.assertTrue(options["download_archive"].endswith(bd.archive_file_name("1080")))
        self.assertTrue(options["windowsfilenames"])

    def test_archive_differs_per_quality(self):
        archives = set()
        for quality in ("1080", "2160", "720"):
            args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC", "--quality", quality])
            options = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
            archives.add(options["download_archive"])
        self.assertEqual(len(archives), 3)

    def test_folded_archive_when_force_or_no_archive(self):
        for flag in ("--force", "--no-archive"):
            args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC", flag])
            options = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
            self.assertNotIn("download_archive", options)

    def test_cookiefile_and_cookie_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(
                ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tzzz\n", encoding="utf-8"
            )
            args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC"])
            options = bd.build_ydl_options(args, Path(tmp), path, "ffmpeg")
        self.assertEqual(options["cookiefile"], str(path))
        self.assertIn("SESSDATA=zzz", options["http_headers"]["Cookie"])

    def test_dry_run_and_list_formats(self):
        args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC", "--dry-run"])
        options = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
        self.assertTrue(options["skip_download"])
        self.assertNotIn("download_archive", options)

        args = self.parse(["https://www.bilibili.com/video/BV1wh4P6XEkC", "--list-formats"])
        options = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
        self.assertTrue(options["listformats"])
        self.assertTrue(options["skip_download"])

    def test_proxy_rate_and_author_folder(self):
        args = self.parse(
            [
                "https://www.bilibili.com/video/BV1wh4P6XEkC",
                "--proxy",
                "http://127.0.0.1:7890",
                "--limit-rate",
                "500K",
                "--folder-by-author",
                "--quality",
                "720",
            ]
        )
        options = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
        self.assertEqual(options["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(options["ratelimit"], 500 * 1024)
        self.assertEqual(options["outtmpl"]["default"], bd.AUTHOR_TEMPLATE)
        self.assertEqual(options["format"], bd.build_format_selector("720"))


class LoginErrorDetectionTests(unittest.TestCase):
    def test_markers(self):
        self.assertTrue(bd.is_login_error(Exception("ERROR: [BiliBili] 账号未登录")))
        self.assertTrue(bd.is_login_error(Exception("HTTP Error 401: Unauthorized")))
        self.assertTrue(bd.is_login_error(Exception("SESSDATA expired")))
        self.assertFalse(bd.is_login_error(Exception("Video unavailable")))

    def test_proxy_error_markers(self):
        self.assertTrue(bd.is_proxy_error(Exception("EOF occurred in violation of protocol")))
        self.assertTrue(bd.is_proxy_error(Exception("[Errno 2] No such file or directory")))
        self.assertTrue(bd.is_proxy_error(Exception("[WinError 10054]")))
        self.assertFalse(bd.is_proxy_error(Exception("Video unavailable")))
        self.assertIn("NO_PROXY", bd.PROXY_HINT)


class ParserTests(unittest.TestCase):
    def test_help_lists_required_options(self):
        help_text = bd.build_parser().format_help()
        for flag in (
            "--file",
            "--output",
            "--up",
            "--all",
            "--limit",
            "--quality",
            "--login-once",
            "--logout",
            "--check-login",
            "--dry-run",
            "--list-formats",
            "--folder-by-author",
            "--force",
            "--no-archive",
            "--limit-rate",
            "--proxy",
            "--retries",
            "--version",
            "-v",
        ):
            self.assertIn(flag, help_text)

    def test_quality_choices_and_default(self):
        args = bd.build_parser().parse_args([])
        self.assertEqual(args.quality, "1080")
        for value in ("1080", "720", "480", "best"):
            self.assertEqual(bd.build_parser().parse_args(["--quality", value]).quality, value)

    def test_invalid_quality_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bd.build_parser().parse_args(["--quality", "4k"])


class MainValidationTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bd.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_main_expect_exit(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                bd.main(argv)
        return err.getvalue()

    def test_negative_retries_rejected(self):
        err = self.run_main_expect_exit(["--retries", "-1", "BV1wh4P6XEkC"])
        self.assertIn("--retries", err)

    def test_negative_limit_rejected(self):
        err = self.run_main_expect_exit(["--limit", "-1", "BV1wh4P6XEkC"])
        self.assertIn("--limit", err)

    def test_all_without_target_rejected(self):
        err = self.run_main_expect_exit(["--all"])
        self.assertIn("--all", err)

    # 下面三条都用临时 --cookies 路径，避免依赖（也避免破坏）项目里真实的 cookies.txt。
    def test_missing_cookie_file_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "cookies.txt")
            code, out, _err = self.run_main(["--check-login", "--cookies", missing])
        self.assertEqual(code, 1)
        self.assertIn("未检测到 B 站登录态", out)
        self.assertIn("--login-once", out)
        self.assertIn("cookies.txt", out)

    def test_logout_without_file_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "cookies.txt")
            code, out, _err = self.run_main(["--logout", "--cookies", missing])
        self.assertEqual(code, 0)
        self.assertIn("没有找到 Cookie 文件", out)

    def test_require_login_blocks_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "cookies.txt"
            empty.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            code, out, err = self.run_main(
                ["--require-login", "--cookies", str(empty), "BV1wh4P6XEkC"]
            )
        self.assertEqual(code, 1)
        self.assertIn("未检测到 B 站登录态", out)
        self.assertIn("--require-login", err)

    def test_logout_removes_existing_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.txt"
            path.write_text(
                "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tz\n",
                encoding="utf-8",
            )
            code, out, _err = self.run_main(["--logout", "--cookies", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("已删除 Cookie 文件", out)
            self.assertFalse(path.exists())

    def test_missing_ffmpeg_blocks_before_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "", bd.FFMPEG_PATHS_ENV: "none"}
            with patch.dict(os.environ, env, clear=False):
                code, out, err = self.run_main(
                    ["--dry-run", "-o", str(Path(tmp) / "out"), "BV1wh4P6XEkC"]
                )
        self.assertEqual(code, 2)
        self.assertIn("没有找到 ffmpeg", err)
        self.assertIn("gyan.dev", err)
        self.assertIn("--ffmpeg", err)
        self.assertIn("tools\\ffmpeg.exe", err)
        self.assertNotIn("[完成]", out)

    def test_space_link_requires_all_or_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bd, "require_ffmpeg", return_value="ffmpeg"):
                code, _out, err = self.run_main(
                    ["-o", str(Path(tmp) / "out"), "https://space.bilibili.com/123/video"]
                )
        self.assertEqual(code, 1)
        self.assertIn("--all", err)
        self.assertIn("--limit", err)


class EnumerateOnlyTests(unittest.TestCase):
    class FakeDownloader:
        def __init__(self, info):
            self.info = info

        def probe(self, url):
            return self.info

        def account_status(self):
            return (None, None)

    def test_list_formats_prints_chinese_table(self):
        info = QualityTests.INFO | {"title": "测试标题", "uploader": "某UP", "duration": 79.833}
        args = bd.build_parser().parse_args(["--list-formats", "BV1wh4P6XEkC"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            failures = bd._enumerate_only(
                ["https://www.bilibili.com/video/BV1wh4P6XEkC"],
                args,
                self.FakeDownloader(info),
            )
        text = out.getvalue()
        self.assertEqual(failures, 0)
        self.assertIn("测试标题", text)
        self.assertIn("某UP", text)
        self.assertIn("01:19", text)
        self.assertIn("[清晰度]", text)
        self.assertIn("1080P", text)
        self.assertIn("[画质] 目标 1080 可达", text)

    def test_dry_run_reports_gap_without_download(self):
        info = {
            "title": "低画质",
            "formats": [{"format_id": "1", "height": 480, "vcodec": "avc1", "acodec": "none"}],
        }
        args = bd.build_parser().parse_args(["--dry-run", "BV1wh4P6XEkC"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bd._enumerate_only(["https://www.bilibili.com/video/BV1wh4P6XEkC"], args, self.FakeDownloader(info))
        text = out.getvalue()
        self.assertIn("--login-once", text)
        self.assertIn("[跳过] --dry-run", text)


class NetworkErrorTests(unittest.TestCase):
    def test_ssl_eof_message(self):
        self.assertTrue(
            bd.is_network_error(
                Exception("EOF occurred in violation of protocol (_ssl.c:997)")
            )
        )

    def test_ssl_exception_type(self):
        self.assertTrue(bd.is_network_error(ssl.SSLError("handshake failure")))
        self.assertTrue(bd.is_network_error(ssl.SSLEOFError("unexpected eof")))

    def test_winerror_10054_message(self):
        self.assertTrue(
            bd.is_network_error(Exception("[WinError 10054] 远程主机强迫关闭了一个现有的连接。"))
        )

    def test_transport_error_message(self):
        self.assertTrue(
            bd.is_network_error(
                Exception("TransportError('[Errno 2] No such file or directory')")
            )
        )

    def test_connection_reset_types(self):
        self.assertTrue(bd.is_network_error(ConnectionResetError("reset")))
        self.assertTrue(bd.is_network_error(ConnectionAbortedError("aborted")))

    def test_detects_wrapped_cause(self):
        wrapped = Exception("ERROR: [BiliBili] unable to download JSON metadata")
        wrapped.__cause__ = ssl.SSLEOFError("EOF occurred in violation of protocol")
        self.assertTrue(bd.is_network_error(wrapped))

    def test_non_network_errors_rejected(self):
        self.assertFalse(bd.is_network_error(Exception("Video unavailable")))
        self.assertFalse(bd.is_network_error(Exception("账号未登录")))


class CookieFileCopyTests(unittest.TestCase):
    def test_copy_is_a_different_writable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "cookies.txt"
            original.write_text(
                "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tv\n",
                encoding="utf-8",
            )
            original.chmod(0o444)
            copy = bd.prepare_cookiefile(original)
            self.assertIsNotNone(copy)
            self.assertNotEqual(Path(copy), original)
            self.assertTrue(Path(copy).is_file())
            self.assertEqual(Path(copy).read_text(encoding="utf-8"),
                             original.read_text(encoding="utf-8"))
            # 副本必须可写：yt-dlp 会回写
            Path(copy).write_text("x", encoding="utf-8")

    def test_none_stays_none(self):
        self.assertIsNone(bd.prepare_cookiefile(None))

    def test_header_still_built_from_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "cookies.txt"
            original.write_text(
                ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tabc\n",
                encoding="utf-8",
            )
            copy = bd.prepare_cookiefile(original)
            self.assertIn("SESSDATA=abc", bd._cookie_field_from_file(copy))


class LoginMarkerPrecisionTests(unittest.TestCase):
    def test_permission_error_is_not_login_failure(self):
        self.assertFalse(
            bd.is_login_error(
                Exception("[Errno 13] Permission denied: 'E:\\x\\cookies.txt'")
            )
        )

    def test_real_login_messages_still_detected(self):
        for text in (
            "ERROR: [BiliBili] 账号未登录",
            "需要登录后查看",
            "SESSDATA expired",
            "failed to load cookies",
            "HTTP Error 401: Unauthorized",
        ):
            self.assertTrue(bd.is_login_error(Exception(text)), text)


class ResolveProxyTests(unittest.TestCase):
    def parse(self, argv):
        return bd.build_parser().parse_args(argv)

    def clean_env(self):
        return {k: v for k, v in os.environ.items() if "PROXY" not in k.upper()}

    def test_explicit_proxy_wins(self):
        args = self.parse(["--proxy", "http://1.2.3.4:8080", "BV1wh4P6XEkC"])
        with mock.patch.object(bd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
            self.assertEqual(bd.resolve_proxy(args), ("http://1.2.3.4:8080", "explicit"))

    def test_env_proxy_wins_over_system(self):
        args = self.parse(["BV1wh4P6XEkC"])
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://5.6.7.8:9"}, clear=True):
            with mock.patch.object(bd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
                self.assertEqual(bd.resolve_proxy(args), (None, "env"))

    def test_system_proxy_used_when_nothing_else(self):
        args = self.parse(["BV1wh4P6XEkC"])
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            with mock.patch.object(bd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
                self.assertEqual(bd.resolve_proxy(args), ("http://127.0.0.1:7897", "system"))

    def test_no_system_proxy_disables_proactive_use(self):
        args = self.parse(["--no-system-proxy", "BV1wh4P6XEkC"])
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            with mock.patch.object(bd, "detect_system_proxy", return_value="http://127.0.0.1:7897"):
                self.assertEqual(bd.resolve_proxy(args), (None, "disabled"))

    def test_direct_when_no_system_proxy(self):
        args = self.parse(["BV1wh4P6XEkC"])
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            with mock.patch.object(bd, "detect_system_proxy", return_value=None):
                self.assertEqual(bd.resolve_proxy(args), (None, "none"))

    def test_env_proxy_configured_helper(self):
        self.assertTrue(bd.env_proxy_configured({"HTTPS_PROXY": "http://x:1"}))
        self.assertTrue(bd.env_proxy_configured({"all_proxy": "socks5://x:1"}))
        self.assertFalse(bd.env_proxy_configured({"HTTPS_PROXY": "  "}))
        self.assertFalse(bd.env_proxy_configured({}))

    def test_parser_has_no_system_proxy_flag(self):
        self.assertTrue(self.parse(["--no-system-proxy", "BV1wh4P6XEkC"]).no_system_proxy)
        self.assertFalse(self.parse(["BV1wh4P6XEkC"]).no_system_proxy)
        self.assertIn("--no-system-proxy", bd.build_parser().format_help())


class _FakeYDLCtx:
    def __init__(self, owner, options):
        self.owner = owner
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def download(self, urls):
        self.owner.seen_options.append(self.options)
        if self.owner.failures:
            self.owner.failures -= 1
            raise Exception("EOF occurred in violation of protocol (_ssl.c:997)")
        return self.owner.result

    def extract_info(self, url, download=False):
        self.owner.seen_options.append(self.options)
        if self.owner.failures:
            self.owner.failures -= 1
            raise Exception("EOF occurred in violation of protocol (_ssl.c:997)")
        return {"title": "ok", "formats": []}


class FakeYdl:
    """按调用次数决定成功/失败的假 yt-dlp。"""

    def __init__(self, failures, result=None):
        self.failures = failures
        self.result = 0 if result is None else result
        self.seen_options = []

    def YoutubeDL(self, options):
        return _FakeYDLCtx(self, options)


class ProxyFallbackTests(unittest.TestCase):
    def make(self, failures, proxy=None, system_proxy="http://127.0.0.1:7897", result=None):
        args = bd.build_parser().parse_args(["BV1wh4P6XEkC"])
        args.proxy = proxy
        fake = FakeYdl(failures, result=result)
        downloader = bd.Downloader(
            args,
            Path("out"),
            fake,
            Exception,
            None,
            "ffmpeg",
            proxy=proxy,
            system_proxy=system_proxy,
        )
        return downloader, fake

    def test_direct_failure_retries_once_with_system_proxy(self):
        downloader, fake = self.make(failures=1, proxy=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = downloader.download("https://www.bilibili.com/video/BV1wh4P6XEkC")
        self.assertEqual(result, 0)
        self.assertEqual(downloader.attempts, 2)
        self.assertEqual(len(fake.seen_options), 2)
        self.assertNotIn("proxy", fake.seen_options[0])
        self.assertEqual(fake.seen_options[1]["proxy"], "http://127.0.0.1:7897")
        self.assertIn(
            "[网络] 直连失败，改用系统代理 http://127.0.0.1:7897 重试", out.getvalue()
        )
        self.assertEqual(downloader.tried_system_proxy, "http://127.0.0.1:7897")

    def test_retry_happens_only_once_when_still_failing(self):
        downloader, fake = self.make(failures=5, proxy=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(Exception):
                downloader.download("https://www.bilibili.com/video/BV1wh4P6XEkC")
        self.assertEqual(downloader.attempts, 2)
        self.assertEqual(len(fake.seen_options), 2)
        self.assertEqual(out.getvalue().count("[网络] 直连失败"), 1)
        self.assertIn("已自动改用系统代理", downloader.proxy_failure_note())

    def test_no_retry_without_system_proxy(self):
        downloader, _fake = self.make(failures=1, proxy=None, system_proxy=None)
        with self.assertRaises(Exception):
            downloader.download("https://www.bilibili.com/video/BV1wh4P6XEkC")
        self.assertEqual(downloader.attempts, 1)
        self.assertIn("没有探测到 Windows 系统代理", downloader.proxy_failure_note())

    def test_no_retry_when_already_using_system_proxy(self):
        downloader, _fake = self.make(
            failures=1,
            proxy="http://127.0.0.1:7897",
            system_proxy="http://127.0.0.1:7897",
        )
        with self.assertRaises(Exception):
            downloader.download("https://www.bilibili.com/video/BV1wh4P6XEkC")
        self.assertEqual(downloader.attempts, 1)
        self.assertIn("已经在用系统代理", downloader.proxy_failure_note())

    def test_non_network_error_is_not_retried(self):
        args = bd.build_parser().parse_args(["BV1wh4P6XEkC"])
        downloader = bd.Downloader(
            args,
            Path("out"),
            FakeYdl(0),
            Exception,
            None,
            "ffmpeg",
            proxy=None,
            system_proxy="http://127.0.0.1:7897",
        )

        def boom(_proxy):
            raise Exception("Video unavailable")

        with self.assertRaises(Exception):
            downloader._with_fallback(boom)
        self.assertEqual(downloader.attempts, 1)

    def test_probe_also_falls_back(self):
        downloader, fake = self.make(failures=1, proxy=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            info = downloader.probe("https://www.bilibili.com/video/BV1wh4P6XEkC")
        self.assertEqual(info["title"], "ok")
        self.assertEqual(fake.seen_options[1]["proxy"], "http://127.0.0.1:7897")
        self.assertTrue(fake.seen_options[1]["skip_download"])
        self.assertIn("[网络] 直连失败", out.getvalue())

    def test_build_ydl_options_proxy_override(self):
        args = bd.build_parser().parse_args(
            ["--proxy", "http://1.2.3.4:8080", "BV1wh4P6XEkC"]
        )
        overridden = bd.build_ydl_options(
            args, Path("out"), None, "ffmpeg", "http://9.9.9.9:1"
        )
        self.assertEqual(overridden["proxy"], "http://9.9.9.9:1")
        plain = bd.build_ydl_options(args, Path("out"), None, "ffmpeg")
        self.assertEqual(plain["proxy"], "http://1.2.3.4:8080")


class QualityTierTests(unittest.TestCase):
    def test_tiers_include_4k_and_2k(self):
        self.assertEqual(bd.QUALITY_HEIGHTS["2160"], 2160)
        self.assertEqual(bd.QUALITY_HEIGHTS["1440"], 1440)
        self.assertIsNone(bd.QUALITY_HEIGHTS["best"])
        self.assertEqual(bd.QUALITY_CHOICES, ("2160", "1440", "1080", "720", "480", "best"))
        self.assertEqual(set(bd.VIP_QUALITIES), {"2160", "1440"})

    def test_selectors(self):
        self.assertEqual(
            bd.build_format_selector("2160"),
            "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
        )
        self.assertEqual(
            bd.build_format_selector("1440"),
            "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
        )
        self.assertEqual(bd.build_format_selector("best"), "bv*+ba/b")

    def test_parser_accepts_new_choices_and_keeps_default(self):
        parser = bd.build_parser()
        for value in ("2160", "1440", "1080", "720", "480", "best"):
            self.assertEqual(parser.parse_args(["--quality", value]).quality, value)
        self.assertEqual(parser.parse_args([]).quality, "1080")

    def test_invalid_quality_still_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bd.build_parser().parse_args(["--quality", "4320"])

    def test_help_mentions_4k_2k_and_vip(self):
        help_text = bd.build_parser().format_help()
        self.assertIn("2160=4K", help_text)
        self.assertIn("1440=2K", help_text)
        self.assertIn("大会员", help_text)
        self.assertIn("{2160,1440,1080,720,480,best}", help_text)

    def test_max_video_height_reads_2160(self):
        info = {"formats": [{"format_id": "x", "height": 2160, "vcodec": "avc1", "acodec": "none"}]}
        self.assertEqual(bd.max_video_height(info), 2160)


class QualityGapReasonTests(unittest.TestCase):
    LOW = {"formats": [{"format_id": "1", "height": 1080, "vcodec": "avc1", "acodec": "none"}]}
    UHD = {"formats": [{"format_id": "1", "height": 2160, "vcodec": "avc1", "acodec": "none"}]}

    def test_no_gap_when_reached(self):
        self.assertIsNone(bd.describe_quality_gap(self.UHD, "2160"))

    def test_reason_not_vip(self):
        text = bd.describe_quality_gap(self.LOW, "2160", vip_status=False, logged_in=True)
        self.assertIn("不是大会员", text)
        self.assertIn("4K 属于大会员专享", text)
        self.assertIn("--quality 1080", text)
        self.assertNotIn("网络", text)

    def test_reason_video_has_no_4k_source(self):
        text = bd.describe_quality_gap(self.LOW, "2160", vip_status=True, logged_in=True)
        self.assertIn("该视频本身没有 4K 源", text)
        self.assertIn("已是大会员", text)
        self.assertNotIn("不是大会员", text)

    def test_reason_not_logged_in(self):
        text = bd.describe_quality_gap(self.LOW, "2160", logged_in=False)
        self.assertIn("没有登录态", text)
        self.assertIn("--login-once", text)

    def test_reason_unknown_account(self):
        text = bd.describe_quality_gap(self.LOW, "2160")
        self.assertIn("两种可能", text)
        self.assertIn("--check-login", text)
        self.assertIn("--list-formats", text)

    def test_2k_uses_2k_wording(self):
        text = bd.describe_quality_gap(self.LOW, "1440", vip_status=False, logged_in=True)
        self.assertIn("2K", text)

    def test_legacy_1080_behaviour_unchanged(self):
        self.assertIsNone(bd.describe_quality_gap(self.LOW, "1080"))
        low = {"formats": [{"format_id": "1", "height": 480, "vcodec": "avc1", "acodec": "none"}]}
        text = bd.describe_quality_gap(low, "1080")
        self.assertIn("480P", text)
        self.assertIn("--login-once", text)


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    def __init__(self, payload=None, error=None) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[Any] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return _FakeHttpResponse(self.payload)


class AccountProbeTests(unittest.TestCase):
    def nav(self, *, is_login=True, vip_status=1, uname="GUOERSHUO"):
        return {
            "code": 0,
            "data": {
                "isLogin": is_login,
                "uname": uname,
                "vip": {"status": vip_status, "type": 2, "label": {"text": "年度大会员"}},
            },
        }

    def test_vip_detected(self):
        opener = _FakeOpener(self.nav())
        info = bd.fetch_account_info("SESSDATA=x", opener=opener)
        self.assertTrue(info["is_vip"])
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["uname"], "GUOERSHUO")
        self.assertTrue(bd.account_vip_status("SESSDATA=x", opener=opener))

    def test_logged_in_but_not_vip(self):
        opener = _FakeOpener(self.nav(vip_status=0))
        self.assertFalse(bd.account_vip_status("SESSDATA=x", opener=opener))

    def test_not_logged_in(self):
        opener = _FakeOpener(self.nav(is_login=False, vip_status=0))
        self.assertFalse(bd.account_vip_status(opener=opener))

    def test_api_error_code_returns_none(self):
        opener = _FakeOpener({"code": -101, "message": "账号未登录"})
        self.assertIsNone(bd.account_vip_status(opener=opener))
        self.assertIsNone(bd.fetch_account_info(opener=opener))

    def test_network_failure_returns_none(self):
        opener = _FakeOpener(error=URLError("offline"))
        self.assertIsNone(bd.account_vip_status(opener=opener))

    def test_garbage_payload_returns_none(self):
        class Bad:
            def open(self, request, timeout=None):
                class R:
                    def read(self):
                        return b"not-json"

                return R()

        self.assertIsNone(bd.account_vip_status(opener=Bad()))

    def test_cookie_header_is_sent(self):
        opener = _FakeOpener(self.nav())
        cookies = bd.parse_netscape_cookies(
            ".bilibili.com\tTRUE\t/\tFALSE\t1999999999\tSESSDATA\tabc\n"
        )
        bd.fetch_account_info(cookies, opener=opener)
        self.assertEqual(opener.requests[0].get_header("Cookie"), "SESSDATA=abc")

    def test_no_cookie_means_no_header(self):
        opener = _FakeOpener(self.nav(is_login=False, vip_status=0))
        bd.fetch_account_info(None, opener=opener)
        self.assertIsNone(opener.requests[0].get_header("Cookie"))


class DownloaderAccountStatusTests(unittest.TestCase):
    def make(self):
        args = bd.build_parser().parse_args(["BV1wh4P6XEkC"])
        args.proxy = None
        return bd.Downloader(
            args, Path("out"), FakeYdl(0), Exception, None, "ffmpeg", proxy=None
        )

    def test_caches_and_maps_vip(self):
        downloader = self.make()
        with mock.patch.object(
            bd, "fetch_account_info", return_value={"is_vip": True, "logged_in": True}
        ) as probe:
            self.assertEqual(downloader.account_status(), (True, True))
            self.assertEqual(downloader.account_status(), (True, True))
        self.assertEqual(probe.call_count, 1)

    def test_unknown_account_is_none(self):
        downloader = self.make()
        with mock.patch.object(bd, "fetch_account_info", return_value=None):
            self.assertEqual(downloader.account_status(), (None, None))


class Enumerate2160Tests(unittest.TestCase):
    class FakeDownloader:
        def __init__(self, info):
            self.info = info
            self.probed = 0

        def probe(self, url):
            return self.info

        def account_status(self):
            self.probed += 1
            return (True, True)

    def test_2160_reachable_message(self):
        info = {
            "title": "4K 视频",
            "formats": [
                {"format_id": "30116", "height": 1080, "vcodec": "avc1", "acodec": "none"},
                {"format_id": "30120", "height": 2160, "vcodec": "avc1", "acodec": "none"},
            ],
        }
        args = bd.build_parser().parse_args(["--list-formats", "--quality", "2160", "BV1"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            failures = bd._enumerate_only(
                ["https://www.bilibili.com/video/BV1"], args, self.FakeDownloader(info)
            )
        text = out.getvalue()
        self.assertEqual(failures, 0)
        self.assertIn("2160P", text)
        self.assertIn("[画质] 目标 2160 可达（最高 2160P）", text)

    def test_gap_uses_account_context_only_when_needed(self):
        info = {
            "title": "只有 1080",
            "formats": [{"format_id": "1", "height": 1080, "vcodec": "avc1", "acodec": "none"}],
        }
        fake = self.FakeDownloader(info)
        args = bd.build_parser().parse_args(["--dry-run", "--quality", "2160", "BV1"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bd._enumerate_only(["https://www.bilibili.com/video/BV1"], args, fake)
        self.assertIn("该视频本身没有 4K 源", out.getvalue())
        self.assertEqual(fake.probed, 1)

    def test_account_probe_skipped_when_quality_reached(self):
        info = {
            "title": "4K",
            "formats": [{"format_id": "1", "height": 2160, "vcodec": "avc1", "acodec": "none"}],
        }
        fake = self.FakeDownloader(info)
        args = bd.build_parser().parse_args(["--dry-run", "--quality", "2160", "BV1"])
        with contextlib.redirect_stdout(io.StringIO()):
            bd._enumerate_only(["https://www.bilibili.com/video/BV1"], args, fake)
        self.assertEqual(fake.probed, 0)


PLAYURL_FIXTURE = {
    "accept_quality": [125, 120, 116, 80],
    "accept_description": ["真彩 HDR", "超清 4K", "高清 1080P60", "高清 1080P"],
    "support_formats": [
        {"quality": 125, "display_desc": "HDR", "superscript": "", "codecs": ["hvc1.2.4.L153.90"]},
        {
            "quality": 120,
            "display_desc": "4K",
            "superscript": "",
            "codecs": ["av01.0.00M.10.0.110.01.01.01.0", "avc1.640034", "hev1.1.6.L153.90"],
        },
        {
            "quality": 116,
            "display_desc": "1080P",
            "superscript": "60帧",
            "codecs": ["av01.0.00M.10.0.110.01.01.01.0", "avc1.640032"],
        },
        {"quality": 80, "display_desc": "1080P", "superscript": "", "codecs": ["avc1.640032"]},
    ],
    "dash": {
        "video": [
            {"id": 125, "codecid": 12, "width": 3840, "height": 2160, "frameRate": 59.938},
            {"id": 120, "codecid": 13, "width": 3840, "height": 2160, "frameRate": 59.937},
            {"id": 120, "codecid": 7, "width": 3840, "height": 2160, "frameRate": 59.938},
            {"id": 120, "codecid": 12, "width": 3840, "height": 2160, "frameRate": 59.937},
            {"id": 116, "codecid": 7, "width": 1920, "height": 1080, "frameRate": 59.938},
            {"id": 80, "codecid": 7, "width": 1920, "height": 1080, "frameRate": 29.97},
        ]
    },
}

YDL_FORMATS_FIXTURE = {
    "formats": [
        {"format_id": "100029", "height": 2160, "fps": 59.94, "vcodec": "av01.0.08M", "acodec": "none"},
        {"format_id": "30121", "height": 2160, "fps": 59.94, "vcodec": "hev1.1.6", "acodec": "none"},
        {"format_id": "30120", "height": 2160, "fps": 59.94, "vcodec": "avc1.640034", "acodec": "none"},
        {"format_id": "30116", "height": 1080, "fps": 59.94, "vcodec": "avc1.640032", "acodec": "none"},
        {"format_id": "30080", "height": 1080, "fps": 29.97, "vcodec": "avc1.640032", "acodec": "none"},
        {"format_id": "30280", "height": None, "fps": None, "vcodec": "none", "acodec": "mp4a.40.2"},
    ]
}


class QualityTokenTests(unittest.TestCase):
    def parse(self, argv):
        return bd.build_parser().parse_args(argv)

    def test_resolution_choices_still_accepted(self):
        for value in ("2160", "1440", "1080", "720", "480", "best"):
            self.assertEqual(self.parse(["--quality", value]).quality, value)

    def test_qn_numbers_accepted(self):
        for value in ("127", "125", "120", "116", "112", "80", "64", "32", "16"):
            self.assertEqual(self.parse(["--quality", value]).quality, value)

    def test_bogus_values_rejected(self):
        for value in ("4320", "abc", ""):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.parse(["--quality", value])

    def test_qn_flag_and_list_qualities(self):
        args = self.parse(["--qn", "120", "BV1"])
        self.assertEqual(args.qn, 120)
        self.assertTrue(self.parse(["--list-qualities", "BV1"]).list_qualities)
        self.assertIsNone(self.parse(["BV1"]).qn)

    def test_help_mentions_new_flags(self):
        text = bd.build_parser().format_help()
        self.assertIn("--qn", text)
        self.assertIn("--list-qualities", text)
        self.assertIn("116=高清 1080P60", text)


class EffectiveQnTests(unittest.TestCase):
    def qn(self, argv):
        return bd.effective_qn(bd.build_parser().parse_args(argv))

    def test_qn_flag_wins(self):
        self.assertEqual(self.qn(["--qn", "120", "--quality", "1080", "BV1"]), 120)

    def test_resolution_quality_is_not_qn(self):
        for value in ("2160", "1440", "1080", "720", "480", "best"):
            self.assertIsNone(self.qn(["--quality", value, "BV1"]), value)

    def test_native_qn_quality(self):
        self.assertEqual(self.qn(["--quality", "116", "BV1"]), 116)
        self.assertEqual(self.qn(["--quality", "125", "BV1"]), 125)

    def test_default_has_no_qn(self):
        self.assertIsNone(self.qn(["BV1"]))


class BuildQualityTiersTests(unittest.TestCase):
    def tiers(self, **kwargs):
        return bd.build_quality_tiers(PLAYURL_FIXTURE, cid=123, **kwargs)

    def test_labels_are_bilibili_wording(self):
        tiers = self.tiers()
        self.assertEqual(
            [tier["label"] for tier in tiers],
            ["真彩 HDR", "超清 4K", "高清 1080P60", "高清 1080P"],
        )
        self.assertEqual([tier["qn"] for tier in tiers], [125, 120, 116, 80])

    def test_display_and_fps(self):
        tiers = {tier["qn"]: tier for tier in self.tiers()}
        self.assertEqual(tiers[125]["display"], "HDR")
        self.assertEqual(tiers[120]["display"], "4K")
        self.assertEqual(tiers[116]["display"], "1080P60帧")
        self.assertEqual(tiers[120]["height"], 2160)
        self.assertAlmostEqual(tiers[116]["fps"], 59.938, places=2)

    def test_codec_preference_order(self):
        tiers = {tier["qn"]: tier for tier in self.tiers()}
        self.assertEqual(tiers[120]["codecs"], ["avc1", "hevc", "av01"])
        self.assertEqual(tiers[120]["streams"][0]["codecid"], 7)

    def test_vip_gate(self):
        tiers = {tier["qn"]: tier for tier in self.tiers()}
        self.assertTrue(tiers[125]["need_vip"])
        self.assertTrue(tiers[120]["need_vip"])
        self.assertTrue(tiers[116]["need_vip"])
        self.assertFalse(tiers[80]["need_vip"])

    def test_availability_follows_vip_status(self):
        free = {tier["qn"]: tier for tier in self.tiers(vip_status=False)}
        self.assertFalse(free[120]["available"])
        self.assertTrue(free[80]["available"])
        vip = {tier["qn"]: tier for tier in self.tiers(vip_status=True)}
        self.assertTrue(vip[120]["available"])

    def test_empty_payload(self):
        self.assertEqual(bd.build_quality_tiers({}), [])


class FormatQualityTiersTests(unittest.TestCase):
    def test_rows_show_label_qn_and_vip(self):
        tiers = bd.build_quality_tiers(PLAYURL_FIXTURE, cid=1)
        lines = bd.format_quality_tiers(
            tiers,
            meta={"title": "测试", "bvid": "BV1", "cid": 1},
            logged_in=True,
            vip_status=True,
        )
        text = "\n".join(lines)
        self.assertIn("超清 4K", text)
        self.assertIn("qn=120", text)
        self.assertIn("大会员", text)
        self.assertIn("可用", text)
        self.assertIn("当前账号可用最高档：真彩 HDR（qn=125）", text)
        self.assertIn("编码 avc1/hevc/av01", text)

    def test_tier_lookup(self):
        tiers = bd.build_quality_tiers(PLAYURL_FIXTURE)
        self.assertEqual(bd.tier_for_qn(tiers, 116)["label"], "高清 1080P60")
        self.assertIsNone(bd.tier_for_qn(tiers, 127))


class ResolveQnFormatTests(unittest.TestCase):
    def tier(self, qn, **extra):
        tiers = bd.build_quality_tiers(PLAYURL_FIXTURE)
        base = dict(bd.tier_for_qn(tiers, qn))
        base.update(extra)
        return base

    def test_prefers_avc1_for_same_qn(self):
        video_id, chosen = bd.resolve_qn_format(YDL_FORMATS_FIXTURE, self.tier(120))
        self.assertEqual(video_id, "30120")
        self.assertEqual(chosen["vcodec"], "avc1.640034")

    def test_60fps_tier_picks_60fps_track(self):
        video_id, _chosen = bd.resolve_qn_format(YDL_FORMATS_FIXTURE, self.tier(116))
        self.assertEqual(video_id, "30116")

    def test_ignores_audio_only_formats(self):
        video_id, chosen = bd.resolve_qn_format(YDL_FORMATS_FIXTURE, self.tier(116))
        self.assertNotEqual(video_id, "30280")
        self.assertNotEqual(chosen["vcodec"], "none")

    def test_fps_mode_high_refuses_30fps_fallback(self):
        info = {
            "formats": [
                {"format_id": "30080", "height": 1080, "fps": 29.97, "vcodec": "avc1", "acodec": "none"}
            ]
        }
        tier = {"qn": 116, "label": "高清 1080P60", "height": 1080, "fps": None,
                "fps_mode": "high", "available": True, "codecs": [], "need_vip": True}
        self.assertEqual(bd.resolve_qn_format(info, tier), (None, None))

    def test_fps_mode_low_prefers_normal_fps(self):
        info = {
            "formats": [
                {"format_id": "30116", "height": 1080, "fps": 59.94, "vcodec": "avc1", "acodec": "none"},
                {"format_id": "30080", "height": 1080, "fps": 29.97, "vcodec": "avc1", "acodec": "none"},
            ]
        }
        tier = {"qn": 80, "label": "高清 1080P", "height": 1080, "fps": None,
                "fps_mode": "low", "available": True, "codecs": [], "need_vip": False}
        self.assertEqual(bd.resolve_qn_format(info, tier)[0], "30080")

    def test_missing_track_returns_none(self):
        tier = {"qn": 127, "label": "8K", "height": 4320, "fps": None,
                "available": True, "codecs": [], "need_vip": True}
        self.assertEqual(bd.resolve_qn_format(YDL_FORMATS_FIXTURE, tier), (None, None))

    def test_selector_string(self):
        self.assertEqual(bd.format_selector_for_video_id("30120"), "30120+ba")


class DescribeQnGapTests(unittest.TestCase):
    def test_unknown_tier(self):
        text = bd.describe_qn_gap(None, 127)
        self.assertIn("没有 qn=127", text)
        self.assertIn("--list-qualities", text)

    def test_unavailable_needs_vip(self):
        tier = {"qn": 120, "label": "超清 4K", "available": False}
        text = bd.describe_qn_gap(tier, 120, logged_in=True, vip_status=False)
        self.assertIn("大会员专享", text)

    def test_unavailable_needs_login(self):
        tier = {"qn": 120, "label": "超清 4K", "available": False}
        text = bd.describe_qn_gap(tier, 120, logged_in=False)
        self.assertIn("登录态", text)
        self.assertIn("--login-once", text)

    def test_no_matching_track(self):
        tier = {"qn": 116, "label": "高清 1080P60", "available": True}
        text = bd.describe_qn_gap(tier, 116)
        self.assertIn("没有找到对应轨道", text)


class _RouterOpener:
    """按 URL 关键字返回不同 payload 的假 opener。"""

    def __init__(self, routes):
        self.routes = routes

    def open(self, request, timeout=None):
        url = getattr(request, "full_url", str(request))
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeHttpResponse(payload)
        raise URLError(f"no route for {url}")


class FetchQualityTiersTests(unittest.TestCase):
    VIEW = {"code": 0, "data": {"bvid": "BV1R1e4zKEh1", "cid": 31870356198, "title": "4K 测试", "pages": []}}
    PLAY = {"code": 0, "data": PLAYURL_FIXTURE}

    def test_happy_path(self):
        opener = _RouterOpener({"web-interface/view": self.VIEW, "player/playurl": self.PLAY})
        tiers = bd.fetch_quality_tiers("BV1R1e4zKEh1", opener=opener)
        self.assertEqual([tier["qn"] for tier in tiers], [125, 120, 116, 80])

    def test_view_failure_returns_none(self):
        opener = _RouterOpener({"web-interface/view": {"code": -404}})
        self.assertIsNone(bd.fetch_quality_tiers("BV1R1e4zKEh1", opener=opener))

    def test_network_failure_returns_none(self):
        opener = _RouterOpener({"web-interface/view": URLError("offline")})
        with mock.patch.object(bd.time, "sleep", return_value=None):
            self.assertIsNone(bd.fetch_quality_tiers("BV1R1e4zKEh1", opener=opener))

    def test_playurl_failure_returns_none_but_keeps_meta(self):
        opener = _RouterOpener({"web-interface/view": self.VIEW, "player/playurl": {"code": -10403}})
        meta, tiers = bd.inspect_quality_tiers("BV1R1e4zKEh1", opener=opener)
        self.assertIsNotNone(meta)
        self.assertIsNone(tiers)

    def test_bad_link_returns_none(self):
        self.assertIsNone(bd.fetch_quality_tiers("https://example.com/x"))


class QualityArchiveTests(unittest.TestCase):
    def test_archive_is_split_by_quality(self):
        self.assertEqual(bd.archive_file_name("1080"), ".bilibili-archive-1080.txt")
        self.assertEqual(bd.archive_file_name("2160"), ".bilibili-archive-2160.txt")
        self.assertEqual(bd.archive_file_name("best"), bd.ARCHIVE_NAME)

    def test_archive_falls_back_for_unknown_quality(self):
        self.assertEqual(bd.archive_file_name(""), bd.ARCHIVE_NAME)
        self.assertEqual(bd.archive_file_name(None), bd.ARCHIVE_NAME)


class ExistingMediaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".tmp" / "existing-media-tests"
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        (self.root / "作者A").mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_finds_file_by_bvid_in_subfolder(self):
        target = self.root / "作者A" / "BV1xx411c7mD_标题.mp4"
        target.write_bytes(b"x")
        found = bd.find_existing_medias(self.root, "BV1xx411c7mD")
        self.assertEqual(found, [target])

    def test_ignores_non_media_and_other_videos(self):
        (self.root / "BV1xx411c7mD_标题.jpg").write_bytes(b"x")
        (self.root / "BV1yy411c7mD_别的.mp4").write_bytes(b"x")
        self.assertEqual(bd.find_existing_medias(self.root, "BV1xx411c7mD"), [])

    def test_returns_empty_without_match(self):
        self.assertEqual(bd.find_existing_medias(self.root, "BV1zz411c7mD"), [])

    def test_picks_newest_when_several_exist(self):
        older = self.root / "BV1xx411c7mD_old.mp4"
        newer = self.root / "BV1xx411c7mD_new.mp4"
        older.write_bytes(b"1")
        newer.write_bytes(b"2")
        os.utime(older, (1_600_000_000, 1_600_000_000))
        os.utime(newer, (1_700_000_000, 1_700_000_000))
        self.assertEqual(bd.find_existing_medias(self.root, "BV1xx411c7mD")[0], newer)


class MediaHeightTests(unittest.TestCase):
    def sample(self):
        return (
            b"Input #0, mov,mp4,m4a, from 'x.mp4':\n"
            b"  Stream #0:0: Video: h264, yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], 30 fps\n"
        )

    def test_parses_height_from_ffmpeg_output(self):
        proc = mock.Mock(returncode=1, stderr=self.sample())
        with mock.patch.object(bd.subprocess, "run", return_value=proc):
            self.assertEqual(bd.probe_media_info(Path("x.mp4"), "ffmpeg")[0], 1080)

    def test_returns_none_when_ffmpeg_missing(self):
        with mock.patch.object(bd.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertEqual(bd.probe_media_info(Path("x.mp4"), "ffmpeg"), (None, None))

    def test_returns_none_for_unparsable_output(self):
        proc = mock.Mock(returncode=1, stderr=b"Invalid data found when processing input")
        with mock.patch.object(bd.subprocess, "run", return_value=proc):
            self.assertEqual(bd.probe_media_info(Path("x.mp4"), "ffmpeg"), (None, None))


class OverwriteDecisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".tmp" / "overwrite-tests"
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def downloader(self, quality="2160", force=False):
        return bd.Downloader(
            bd.argparse.Namespace(quality=quality, force=force),
            self.root,
            yt_dlp=object(),
            download_error=Exception,
            cookiefile=None,
            ffmpeg_path="ffmpeg",
        )

    def make_media(self, name="BV1xx411c7mD_标题.mp4"):
        path = self.root / name
        path.write_bytes(b"x")
        return path

    def test_other_quality_is_saved_as_new_file(self):
        self.make_media()
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 30.0)):
            self.assertEqual(
                self.downloader("2160")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("quality", "2160P"),
            )

    def test_lower_quality_also_saved_separately(self):
        self.make_media()
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 30.0)):
            self.assertEqual(
                self.downloader("480")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("quality", "480P"),
            )

    def test_same_quality_is_skipped(self):
        self.make_media()
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 30.0)):
            self.assertEqual(
                self.downloader("1080")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("skip", ""),
            )

    def test_force_starts_fresh_download(self):
        self.make_media()
        with mock.patch.object(bd, "probe_media_info", return_value=(480, 30.0)):
            self.assertEqual(
                self.downloader("2160", force=True)._plan(
                    "https://www.bilibili.com/video/BV1xx411c7mD"
                ),
                ("fresh", ""),
            )

    def test_no_existing_file_means_normal_download(self):
        with mock.patch.object(bd, "probe_media_info", return_value=(None, None)):
            self.assertEqual(
                self.downloader("2160")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("fresh", ""),
            )

    def test_best_quality_skips_when_anything_exists(self):
        self.make_media()
        with mock.patch.object(bd, "probe_media_info", return_value=(480, 30.0)):
            self.assertEqual(
                self.downloader("best")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("skip", ""),
            )


class QualityTemplateTests(unittest.TestCase):
    def test_appends_quality_before_extension(self):
        self.assertEqual(
            bd.quality_outtmpl("%(id)s_%(title).80s.%(ext)s", "1080P"),
            "%(id)s_%(title).80s_1080P.%(ext)s",
        )

    def test_keeps_author_folder(self):
        self.assertEqual(
            bd.quality_outtmpl("%(uploader)s/%(id)s_%(title).80s.%(ext)s", "480P"),
            "%(uploader)s/%(id)s_%(title).80s_480P.%(ext)s",
        )


class QualityAliasTests(unittest.TestCase):
    def test_60fps_alias_maps_to_qn(self):
        self.assertEqual(bd.parse_quality_token("1080p60"), "116")
        self.assertEqual(bd.parse_quality_token("720P60"), "74")

    def test_plain_tokens_still_work(self):
        self.assertEqual(bd.parse_quality_token("1080"), "1080")
        self.assertEqual(bd.parse_quality_token("best"), "best")

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(Exception):
            bd.parse_quality_token("1080p120")

    def test_quality_target_for_alias_includes_fps(self):
        self.assertEqual(bd.quality_target("1080p60"), (1080, 60))

    def test_quality_target_for_plain_quality(self):
        self.assertEqual(bd.quality_target("2160"), (2160, None))

    def test_quality_target_for_qn(self):
        self.assertEqual(bd.quality_target("116"), (1080, 60))
        self.assertEqual(bd.quality_target("80"), (1080, None))

    def test_quality_target_for_best_is_none(self):
        self.assertIsNone(bd.quality_target("best"))
        self.assertIsNone(bd.quality_target(""))


class MediaInfoTests(unittest.TestCase):
    OUTPUT = (
        b"  Stream #0:0: Video: h264, yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], "
        b"59.94 fps, 59.94 tbr\n"
    )

    def test_returns_height_and_fps(self):
        proc = mock.Mock(returncode=1, stderr=self.OUTPUT)
        with mock.patch.object(bd.subprocess, "run", return_value=proc):
            height, fps = bd.probe_media_info(Path("x.mp4"), "ffmpeg")
        self.assertEqual(height, 1080)
        self.assertAlmostEqual(fps, 59.94, places=2)

    def test_height_only_output_still_parses(self):
        proc = mock.Mock(returncode=1, stderr=b"  Stream #0:0: Video: h264, 1280x720, 25 fps\n")
        with mock.patch.object(bd.subprocess, "run", return_value=proc):
            self.assertEqual(bd.probe_media_info(Path("x.mp4"), "ffmpeg"), (720, 25.0))

    def test_missing_ffmpeg_returns_none_tuple(self):
        with mock.patch.object(bd.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertEqual(bd.probe_media_info(Path("x.mp4"), "ffmpeg"), (None, None))


class OverwriteFpsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".tmp" / "overwrite-fps-tests"
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        (self.root / "BV1xx411c7mD_标题.mp4").write_bytes(b"x")

    def downloader(self, quality):
        return bd.Downloader(
            bd.argparse.Namespace(quality=quality, force=False),
            self.root,
            yt_dlp=object(),
            download_error=Exception,
            cookiefile=None,
            ffmpeg_path="ffmpeg",
        )

    def test_same_height_but_lower_fps_is_downloaded_as_new_file(self):
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 30.0)):
            self.assertEqual(
                self.downloader("1080p60")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("quality", "1080P60"),
            )

    def test_same_height_and_fps_is_skipped(self):
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 59.94)):
            self.assertEqual(
                self.downloader("1080p60")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("skip", ""),
            )

    def test_non_60fps_request_ignores_fps(self):
        with mock.patch.object(bd, "probe_media_info", return_value=(1080, 30.0)):
            self.assertEqual(
                self.downloader("1080")._plan("https://www.bilibili.com/video/BV1xx411c7mD"),
                ("skip", ""),
            )


if __name__ == "__main__":
    unittest.main()
