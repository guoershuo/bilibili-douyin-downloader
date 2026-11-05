from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_engine as be  # noqa: E402


class SafeNameTests(unittest.TestCase):
    def test_replaces_windows_invalid_chars(self):
        self.assertEqual(be.safe_name('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_trims_and_limits(self):
        self.assertEqual(be.safe_name("  hello   world  "), "hello world")
        self.assertLessEqual(len(be.safe_name("x" * 100, limit=20)), 20)

    def test_reserved_name(self):
        self.assertEqual(be.safe_name("CON"), "_CON")

    def test_empty_title(self):
        self.assertEqual(be.safe_name("  "), "")


class CandidateTests(unittest.TestCase):
    def make_detail(self):
        return {
            "video": {
                "play_addr": {
                    "height": 1024,
                    "url_list": ["https://cdn.example/1024.mp4?a=1"],
                },
                "play_addr_265": {
                    "height": 1280,
                    "url_list": ["https://cdn.example/1280-hevc.mp4?a=1"],
                },
                "bit_rate": [{
                    "gear_name": "720_1_1",
                    "is_h265": False,
                    "bit_rate": 1200,
                    "play_addr": {
                        "height": 720,
                        "url_list": ["https://cdn.example/720.mp4?a=1"],
                    },
                }],
            },
        }

    def test_defaults_to_highest_resolution(self):
        candidates = be.pick_candidates(self.make_detail())
        self.assertEqual(candidates[0]["height"], 1280)
        self.assertEqual(candidates[0]["codec"], "h265")

    def test_prefer_h264(self):
        candidates = be.pick_candidates(self.make_detail(), prefer_h264=True)
        self.assertEqual(candidates[0]["url"], "https://cdn.example/1024.mp4?a=1")
        self.assertEqual(candidates[0]["codec"], "h264")

    def test_height_limit(self):
        candidates = be.pick_candidates(self.make_detail(), "best[height<=720]")
        self.assertEqual([c["height"] for c in candidates], [720])

    def test_height_limit_uses_short_side_for_vertical_video(self):
        detail = {
            "video": {
                "width": 720,
                "play_addr": {
                    "width": 720,
                    "height": 1280,
                    "url_list": ["https://cdn.example/vertical.mp4"],
                },
            },
        }
        candidates = be.pick_candidates(detail, "best[height<=1080]")
        self.assertEqual(candidates[0]["height"], 1280)

    def test_worst(self):
        candidates = be.pick_candidates(self.make_detail(), "worst")
        self.assertEqual(candidates[0]["height"], 720)

    def test_download_addr_fallback(self):
        detail = {
            "video": {
                "download_addr": {
                    "height": 720,
                    "url_list": ["https://cdn.example/download.mp4"],
                },
            },
        }
        self.assertEqual(
            be.pick_candidates(detail)[0]["url"],
            "https://cdn.example/download.mp4",
        )

    def test_parse_height_limit(self):
        self.assertEqual(be.parse_height_limit("best[height<=1080]"), 1080)
        self.assertEqual(be.parse_height_limit("best"), None)

    def test_ratio_for_format(self):
        self.assertEqual(be._ratio_for_format("best"), "1080p")
        self.assertEqual(be._ratio_for_format("best[height<=720]"), "720p")
        self.assertEqual(be._ratio_for_format("best[height<=540]"), "540p")
        self.assertEqual(be._ratio_for_format("worst"), "360p")

    def test_play_api_candidate_is_first(self):
        detail = {
            "video": {
                "play_addr": {
                    "height": 864,
                    "width": 576,
                    "uri": "v0200test",
                    "url_list": ["https://cdn.example/864.mp4"],
                },
                "play_addr_265": {
                    "height": 1080,
                    "width": 720,
                    "url_list": ["https://cdn.example/1080-hevc.mp4"],
                },
            },
        }
        candidates = be.pick_candidates(detail)
        self.assertTrue(candidates[0]["url"].startswith("https://aweme.snssdk.com/aweme/v1/play/"))
        self.assertIn("ratio=1080p", candidates[0]["url"])


def make_sample_detail() -> dict:
    """实测链接 https://v.douyin.com/zweK0YH0djA/ 的 aweme_detail 形态。"""

    def stream(height, width, url, uri=None):
        node = {"height": height, "width": width, "url_list": [url]}
        if uri:
            node["uri"] = uri
        return node

    return {
        "aweme_id": "7300000000000000001",
        "desc": "烧烤毕业季，大学生露营烧烤就业指南全流程！",
        "author": {"nickname": "露营老王"},
        "video": {
            "duration": 209200,
            "width": 1280,
            "play_addr": stream(
                1024,
                576,
                "https://v3-web.douyinvod.com/play_default.mp4?a=1",
                uri="v0200default",
            ),
            "play_addr_265": stream(
                1280, 720, "https://v3-web.douyinvod.com/play_265.mp4?a=1"
            ),
            "bit_rate": [
                {
                    "gear_name": "720_2_1",
                    "is_h265": True,
                    "bit_rate": 824000,
                    "play_addr": stream(
                        1280, 720, "https://v3-web.douyinvod.com/720_2_1.mp4?a=1"
                    ),
                },
                {
                    "gear_name": "720_3_1",
                    "is_h265": True,
                    "bit_rate": 644000,
                    "play_addr": stream(
                        1280, 720, "https://v3-web.douyinvod.com/720_3_1.mp4?a=1"
                    ),
                },
                {
                    "gear_name": "720_4_1",
                    "is_h265": True,
                    "bit_rate": 574000,
                    "play_addr": stream(
                        1280, 720, "https://v3-web.douyinvod.com/720_4_1.mp4?a=1"
                    ),
                },
                {
                    "gear_name": "540_1_1",
                    "is_h265": True,
                    "bit_rate": 827000,
                    "play_addr": stream(
                        1024, 576, "https://v3-web.douyinvod.com/540_1_1.mp4?a=1"
                    ),
                },
                {
                    "gear_name": "540_2_1",
                    "is_h265": True,
                    "bit_rate": 603000,
                    "play_addr": stream(
                        1024, 576, "https://v3-web.douyinvod.com/540_2_1.mp4?a=1"
                    ),
                },
            ],
        },
    }


class QualityLabelTests(unittest.TestCase):
    def test_gear_prefix_wins_over_resolution(self):
        self.assertEqual(be.quality_label("720_2_1", height=1280, width=720), "720P")
        self.assertEqual(be.quality_label("540_1_1", height=1024, width=576), "540P")
        self.assertEqual(be.quality_label("720_9_9", height=1920, width=1080), "720P")

    def test_vertical_long_side_inference(self):
        self.assertEqual(be.quality_label("", 1280), "720P")
        self.assertEqual(be.quality_label(None, 1024), "540P")
        self.assertEqual(be.quality_label("", 1920), "1080P")
        self.assertEqual(be.quality_label("", 720), "720P")

    def test_horizontal_pixel_height_inference(self):
        self.assertEqual(be.quality_label("", 1080, 1920), "1080P")
        self.assertEqual(be.quality_label("", 1024, 1820), "1024P")

    def test_vertical_with_both_sides_uses_short_side(self):
        self.assertEqual(be.quality_label("", 1280, 720), "720P")
        self.assertEqual(be.quality_label("", 1024, 576), "540P")
        self.assertEqual(be.quality_label("", 1920, 1080), "1080P")

    def test_gear_without_numeric_prefix_falls_back(self):
        self.assertEqual(be.quality_label("高清", 1280, 720), "720P")
        self.assertEqual(be.quality_label("custom", 0, 0), "custom")

    def test_unknown_resolution(self):
        self.assertEqual(be.quality_label(None), "未知")
        self.assertEqual(be.quality_label_from_resolution(), "")
        self.assertEqual(be.snap_quality_height(0), 0)

    def test_snap_quality_height(self):
        self.assertEqual(be.snap_quality_height(576), 540)
        self.assertEqual(be.snap_quality_height(1280), 1280)


class BuildQualitiesTests(unittest.TestCase):
    def setUp(self):
        self.detail = make_sample_detail()
        self.qualities = be.build_qualities(self.detail)

    def test_every_stream_is_listed(self):
        self.assertEqual(len(self.qualities), 8)

    def test_sorted_by_bitrate_desc(self):
        bitrates = [q["bitrate"] for q in self.qualities if q["bitrate"]]
        self.assertEqual(bitrates, [827, 824, 644, 603, 574])

    def test_gears_are_kept_and_distinguish_same_resolution(self):
        gears = [q["gear"] for q in self.qualities if q["gear"]]
        self.assertEqual(
            sorted(gears), ["540_1_1", "540_2_1", "720_2_1", "720_3_1", "720_4_1"]
        )
        labels = {q["gear"]: q["label"] for q in self.qualities if q["gear"]}
        self.assertEqual(labels["720_2_1"], "720P")
        self.assertEqual(labels["720_3_1"], "720P")
        self.assertEqual(labels["540_1_1"], "540P")

    def test_fields_are_complete(self):
        for item in self.qualities:
            self.assertEqual(
                set(item), {"gear", "label", "codec", "bitrate", "height", "notes"}
            )

    def test_gear_stream_fields(self):
        top = next(q for q in self.qualities if q["gear"] == "720_2_1")
        self.assertEqual(top["label"], "720P")
        self.assertEqual(top["codec"], "h265")
        self.assertEqual(top["bitrate"], 824)
        self.assertEqual(top["height"], 1280)
        self.assertEqual(top["notes"], "")

    def test_default_streams_have_no_gear_but_readable_label(self):
        gearless = [q for q in self.qualities if not q["gear"]]
        self.assertEqual(len(gearless), 3)
        self.assertEqual(
            sorted(q["notes"] for q in gearless), ["H.265", "高码率", "默认播放流"]
        )
        labels = {q["notes"]: q["label"] for q in gearless}
        self.assertEqual(labels["H.265"], "720P")
        self.assertEqual(labels["高码率"], "1080P")
        self.assertEqual(labels["默认播放流"], "540P")

    def test_recommended_is_highest_resolution_then_bitrate(self):
        self.assertEqual(be.recommended_gear(self.qualities), "720_2_1")

    def test_recommended_without_any_gear(self):
        detail = {
            "video": {"play_addr": {"height": 1024, "url_list": ["https://x/a.mp4"]}}
        }
        self.assertIsNone(be.recommended_gear(be.build_qualities(detail)))

    def test_empty_detail(self):
        self.assertEqual(be.build_qualities({}), [])


class CandidatesForGearTests(unittest.TestCase):
    def setUp(self):
        self.candidates = be.pick_candidates(make_sample_detail(), "best")

    def test_pick_candidates_attaches_gear_names(self):
        gears = [c["gear"] for c in self.candidates]
        self.assertIn("720_2_1", gears)
        self.assertIn("540_1_1", gears)
        self.assertEqual(gears.count(""), 3)

    def test_filter_returns_only_that_gear(self):
        picked = be.candidates_for_gear(self.candidates, "720_3_1")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["gear"], "720_3_1")
        self.assertIn("720_3_1.mp4", picked[0]["url"])

    def test_no_gear_returns_everything(self):
        self.assertEqual(
            [c["url"] for c in be.candidates_for_gear(self.candidates, None)],
            [c["url"] for c in self.candidates],
        )

    def test_unknown_gear_raises_with_available_list(self):
        with self.assertRaises(be.BrowserError) as ctx:
            be.candidates_for_gear(self.candidates, "999_9_9")
        message = str(ctx.exception)
        self.assertIn("999_9_9", message)
        self.assertIn("不存在", message)
        self.assertIn("720_2_1", message)
        self.assertIn("--list-qualities", message)

    def test_format_default_unchanged(self):
        best = be.pick_candidates(make_sample_detail(), "best")
        worst = be.pick_candidates(make_sample_detail(), "worst")
        limited = be.pick_candidates(make_sample_detail(), "best[height<=720]")
        self.assertEqual(best[0]["height"], 1280)
        self.assertEqual(best[0]["gear"], "720_2_1")
        # worst 仍然选最低码率那条（play 接口 360p 兜底流），旧行为未变
        self.assertEqual(worst[0]["height"], 360)
        self.assertIn("ratio=360p", worst[0]["url"])
        self.assertEqual(
            [c["height"] for c in limited], [1280, 1280, 1280, 1280, 1024, 1024, 1024, 720]
        )


class WindowModeTests(unittest.TestCase):
    def test_normalize_window_mode(self):
        self.assertEqual(be.normalize_window_mode("headless"), "headless")
        self.assertEqual(be.normalize_window_mode("OffScreen"), "offscreen")
        self.assertEqual(be.normalize_window_mode("visible"), "visible")

    def test_normalize_window_mode_accepts_legacy_bool(self):
        self.assertEqual(be.normalize_window_mode(True), "headless")
        self.assertEqual(be.normalize_window_mode(False), "visible")

    def test_normalize_window_mode_defaults_to_headless(self):
        self.assertEqual(be.normalize_window_mode(None), "headless")
        self.assertEqual(be.normalize_window_mode("   "), "headless")

    def test_normalize_window_mode_rejects_unknown(self):
        with self.assertRaises(be.BrowserError):
            be.normalize_window_mode("invisible")

    def test_window_mode_label_is_chinese(self):
        self.assertIn("无头", be.window_mode_label("headless"))
        self.assertIn("屏幕外", be.window_mode_label("offscreen"))
        self.assertIn("可见", be.window_mode_label("visible"))

    def test_window_arguments_per_mode(self):
        headless = be._window_arguments("headless")
        self.assertIn("--headless=new", headless)
        self.assertIn(f"--window-size={be.WINDOW_SIZE}", headless)

        offscreen = be._window_arguments("offscreen")
        self.assertNotIn("--headless=new", offscreen)
        self.assertIn("--window-position=-32000,-32000", offscreen)
        self.assertIn(f"--window-size={be.WINDOW_SIZE}", offscreen)

        visible = be._window_arguments("visible")
        self.assertNotIn("--headless=new", visible)
        self.assertFalse(any(arg.startswith("--window-position") for arg in visible))

    def test_session_defaults_to_headless(self):
        parameter = inspect.signature(be.BrowserSession.__init__).parameters["window_mode"]
        self.assertEqual(parameter.default, "headless")


class InitialUrlTests(unittest.TestCase):
    """启动导航：默认抖音首页；其它站点传自己的首页；传 None 不导航。"""

    def build_session(self, initial_url):
        session = object.__new__(be.BrowserSession)
        session.browser_path = "fake-browser"
        session.window_mode = "headless"
        session.headless = True
        session.wait_seconds = 1.0
        session.timeout = 5.0
        session.proxy = None
        session.initial_url = initial_url
        session._profile_dir = None
        session._owns_profile = True
        session._profile = None
        session._proc = None
        session._ws = object()
        session._next_id = 0
        session.navigated = []
        session.navigate = session.navigated.append
        session._call = lambda method, params=None, timeout=30.0: {}
        session._wait_for_page = lambda port, timeout: {
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"
        }
        session._websocket = mock.Mock()
        session.close = lambda: None
        return session

    def run_start(self, initial_url):
        session = self.build_session(initial_url)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(be.tempfile, "mkdtemp", return_value=tmp):
                with mock.patch.object(be.subprocess, "Popen") as popen:
                    with mock.patch.object(be.time, "sleep"):
                        popen.return_value = mock.Mock(pid=4242)
                        session._start()
        return session.navigated

    def test_default_initial_url_is_douyin_home(self):
        parameter = inspect.signature(be.BrowserSession.__init__).parameters["initial_url"]
        self.assertEqual(parameter.default, "https://www.douyin.com/")

    def test_default_start_navigates_to_douyin(self):
        self.assertEqual(
            self.run_start(be.DEFAULT_INITIAL_URL),
            ["https://www.douyin.com/"],
        )

    def test_custom_initial_url_skips_douyin(self):
        navigated = self.run_start("https://www.bilibili.com/")
        self.assertEqual(navigated, ["https://www.bilibili.com/"])
        self.assertFalse(any("douyin" in url for url in navigated))

    def test_none_initial_url_does_not_navigate(self):
        self.assertEqual(self.run_start(None), [])


class ProfileDirTests(unittest.TestCase):
    def test_default_profile_dir_sits_next_to_script(self):
        profile = be.default_profile_dir()
        self.assertEqual(profile.name, ".browser-profile")
        self.assertEqual(profile.parent, Path(be.__file__).resolve().parent)

    def test_default_profile_dir_is_cwd_independent(self):
        self.assertEqual(be.default_profile_dir(), be.default_profile_dir(base=be.SCRIPT_DIR))

    def test_default_profile_dir_accepts_base(self):
        base = Path(be.__file__).resolve().parent / "downloads"
        self.assertEqual(be.default_profile_dir(base=base), base.resolve() / ".browser-profile")


class CookieExportTests(unittest.TestCase):
    def make_cookies(self):
        return [
            {
                "name": "sessionid",
                "value": "abc123",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 1893456000.7,
            },
            {
                "name": "ttwid",
                "value": "1%7Cxyz",
                "domain": "www.douyin.com",
                "path": "/",
                "secure": False,
                "httpOnly": False,
                "expires": -1,
            },
        ]

    def data_lines(self, text):
        return [line for line in text.splitlines() if line and not line.startswith("# ")]

    def test_netscape_header(self):
        text = be.format_netscape_cookies(self.make_cookies())
        self.assertTrue(text.startswith("# Netscape HTTP Cookie File\n"))
        self.assertTrue(text.endswith("\n"))

    def test_netscape_fields_and_flags(self):
        lines = self.data_lines(be.format_netscape_cookies(self.make_cookies()))
        self.assertEqual(len(lines), 2)

        first = lines[0].split("\t")
        self.assertEqual(len(first), 7)
        self.assertEqual(first[0], "#HttpOnly_.douyin.com")
        self.assertEqual(first[1], "TRUE")
        self.assertEqual(first[2], "/")
        self.assertEqual(first[3], "TRUE")
        self.assertEqual(first[4], "1893456000")
        self.assertEqual(first[5], "sessionid")
        self.assertEqual(first[6], "abc123")

        second = lines[1].split("\t")
        self.assertEqual(second[0], "www.douyin.com")
        self.assertEqual(second[1], "FALSE")
        self.assertEqual(second[3], "FALSE")
        self.assertEqual(second[4], "0")

    def test_netscape_skips_incomplete_cookies(self):
        cookies = [{"name": "", "domain": "douyin.com"}, {"name": "a", "domain": ""}]
        self.assertEqual(self.data_lines(be.format_netscape_cookies(cookies)), [])

    def test_netscape_empty_cookie_list_keeps_header(self):
        text = be.format_netscape_cookies([])
        self.assertIn("# Netscape HTTP Cookie File", text)
        self.assertEqual(self.data_lines(text), [])


class LoginStateTests(unittest.TestCase):
    def test_cookie_names(self):
        self.assertEqual(
            be.cookie_names([{"name": "a"}, {"name": "b"}, {"value": "x"}]),
            {"a", "b"},
        )

    def test_logged_in_when_required_cookie_present(self):
        cookies = [{"name": "sessionid", "value": "1"}]
        self.assertTrue(be.is_logged_in(cookies, ("sessionid",)))
        self.assertFalse(be.is_logged_in(cookies, ("sessionid_ss",)))

    def test_alternatives_in_one_requirement(self):
        self.assertTrue(
            be.is_logged_in([{"name": "sessionid_ss"}], ("sessionid|sessionid_ss",))
        )
        self.assertFalse(be.is_logged_in([{"name": "ttwid"}], ("sessionid|sessionid_ss",)))

    def test_all_requirements_must_be_met(self):
        cookies = [{"name": "a"}, {"name": "b"}]
        self.assertTrue(be.is_logged_in(cookies, ("a", "b")))
        self.assertFalse(be.is_logged_in(cookies, ("a", "c")))

    def test_no_requirement_means_unknown_but_true(self):
        self.assertTrue(be.is_logged_in([], ()))

    def test_session_is_logged_in_uses_get_cookies(self):
        session = object.__new__(be.BrowserSession)
        session.get_cookies = lambda: [{"name": "sessionid", "value": "x"}]
        self.assertTrue(session.is_logged_in(("sessionid|sessionid_ss",)))
        session.get_cookies = lambda: []
        self.assertFalse(session.is_logged_in(("sessionid|sessionid_ss",)))


class _FakeSession:
    """测试用的浏览器会话替身：不启动真实 Chromium。"""

    def __init__(self, window_mode="headless", detail=None, error=None):
        self.window_mode = window_mode
        self.browser_path = "fake-browser"
        self.profile_dir = None
        self._detail = detail
        self._error = error
        self.calls = 0

    def fetch_detail(self, aweme_id, retries=3):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._detail or {"aweme_id": aweme_id}

    def is_logged_in(self, required=()):
        return bool(getattr(self, "logged_in", False))

    def close(self):
        pass


class BrowserDownloaderTests(unittest.TestCase):
    def make_args(self, **overrides):
        base = {
            "retries": 1,
            "window_mode": "headless",
            "visible": False,
            "timeout": 30.0,
            "browser_wait": 1.0,
            "gear": None,
            "format": "best",
            "prefer_h264": False,
            "force": False,
            "no_archive": True,
            "dry_run": False,
            "list_formats": False,
            "info": False,
            "thumbnail": False,
            "folder_by_author": False,
            "limit_rate": 0,
            "no_progress": True,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_candidates_filtered_by_gear(self):
        downloader = self.make_downloader(gear="720_3_1")
        candidates = downloader._candidates(make_sample_detail())
        self.assertEqual([c["gear"] for c in candidates], ["720_3_1"])

    def test_candidates_unchanged_without_gear(self):
        downloader = self.make_downloader()
        self.assertEqual(
            [c["url"] for c in downloader._candidates(make_sample_detail())],
            [c["url"] for c in be.pick_candidates(make_sample_detail(), "best")],
        )

    def test_unknown_gear_raises_before_downloading(self):
        downloader = self.make_downloader(gear="999_9_9")
        with self.assertRaises(be.BrowserError) as ctx:
            downloader._candidates(make_sample_detail())
        self.assertIn("999_9_9", str(ctx.exception))
        self.assertIn("不存在", str(ctx.exception))

    def make_downloader(self, **overrides):
        return be.BrowserDownloader(Path("."), self.make_args(**overrides))

    def patch_session(self, downloader, session):
        downloader.session = session
        return downloader

    def test_default_window_mode_is_headless(self):
        self.assertEqual(self.make_downloader().window_mode, "headless")
        self.assertEqual(self.make_downloader(visible=True).window_mode, "headless")

    def test_headless_failure_degrades_to_offscreen(self):
        downloader = self.make_downloader()
        opened = []

        def fake_open(window_mode):
            opened.append(window_mode)
            downloader.session = _FakeSession(window_mode=window_mode)
            return downloader.session

        downloader._open_session = fake_open
        downloader.session = _FakeSession(
            error=be.BrowserError("抖音接口拒绝了浏览器请求（Uifid/风控拦截）：Blocked by")
        )

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            detail = downloader.fetch_detail("7300000000000000001")

        self.assertEqual(detail["aweme_id"], "7300000000000000001")
        self.assertEqual(opened, ["offscreen"])
        log = buffer.getvalue()
        self.assertIn("[降级]", log)
        self.assertIn("Uifid", log)
        self.assertIn("屏幕外窗口", log)

    def test_degrades_only_once(self):
        downloader = self.make_downloader()
        opened = []

        def fake_open(window_mode):
            opened.append(window_mode)
            downloader.session = _FakeSession(
                window_mode=window_mode,
                error=be.BrowserError("抖音接口拒绝了浏览器请求（Uifid/风控拦截）"),
            )
            return downloader.session

        downloader._open_session = fake_open
        downloader.session = _FakeSession(
            error=be.BrowserError("抖音接口拒绝了浏览器请求（Uifid/风控拦截）")
        )
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(be.BrowserError):
                downloader.fetch_detail("7300000000000000001")
        self.assertEqual(opened, ["offscreen"])

    def test_permanent_error_does_not_degrade(self):
        downloader = self.make_downloader()
        opened = []
        downloader._open_session = lambda mode: opened.append(mode)
        downloader.session = _FakeSession(error=be.BrowserError("无法获取视频：作品不见了"))
        with self.assertRaises(be.BrowserError):
            downloader.fetch_detail("7300000000000000001")
        self.assertEqual(opened, [])

    def test_offscreen_session_does_not_degrade_again(self):
        downloader = self.make_downloader()
        opened = []
        downloader._open_session = lambda mode: opened.append(mode)
        downloader.session = _FakeSession(
            window_mode="offscreen", error=be.BrowserError("查询超时")
        )
        with self.assertRaises(be.BrowserError):
            downloader.fetch_detail("7300000000000000001")
        self.assertEqual(opened, [])

    def test_login_check_skipped_without_cookie_names(self):
        downloader = self.make_downloader()
        session = _FakeSession()
        downloader.session = session
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            downloader._prepare_login()
        self.assertEqual(buffer.getvalue(), "")

    def test_login_check_reports_existing_login(self):
        downloader = self.make_downloader()
        session = _FakeSession()
        session.logged_in = True
        downloader.session = session
        downloader.login_cookies = ("sessionid|sessionid_ss",)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            downloader._prepare_login()
        self.assertIn("已检测到登录态", buffer.getvalue())

    def test_login_prompt_failure_reports_timeout(self):
        downloader = self.make_downloader(login_timeout=1)
        downloader.login_cookies = ("sessionid|sessionid_ss",)
        downloader.session = _FakeSession()

        def fake_open(window_mode):
            downloader.session = _FakeSession(window_mode=window_mode)
            return downloader.session

        downloader._open_session = fake_open
        with mock.patch.object(be, "LOGIN_POLL_INTERVAL", 0.05):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(be.BrowserError) as ctx:
                    downloader._interactive_login()
        self.assertIn("登录超时", str(ctx.exception))

    def test_export_cookies_failure_is_reported_not_raised(self):
        downloader = self.make_downloader()
        downloader.session = _FakeSession()
        downloader.session.export_cookies = lambda path: (_ for _ in ()).throw(
            be.BrowserError("导出 Cookie 失败：拒绝访问")
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(downloader.export_cookies("x.txt"))
        self.assertIn("导出失败", stderr.getvalue())

    def test_session_export_cookies_writes_netscape_file(self):
        session = object.__new__(be.BrowserSession)
        session.get_cookies = lambda: [{
            "name": "sessionid",
            "value": "v",
            "domain": ".douyin.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1893456000,
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "cookies.txt"
            written = session.export_cookies(target)
            self.assertEqual(written, target)
            text = target.read_text(encoding="utf-8")
            self.assertIn("# Netscape HTTP Cookie File", text)
            self.assertIn("#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tv", text)


class ParseProxyServerTests(unittest.TestCase):
    def test_plain_host_port_gets_http_scheme(self):
        self.assertEqual(be.parse_proxy_server("127.0.0.1:7897"), "http://127.0.0.1:7897")

    def test_strips_whitespace(self):
        self.assertEqual(be.parse_proxy_server("  127.0.0.1:7897  "), "http://127.0.0.1:7897")

    def test_per_protocol_prefers_https_segment(self):
        self.assertEqual(
            be.parse_proxy_server("http=1.2.3.4:8080;https=1.2.3.4:8443"),
            "http://1.2.3.4:8443",
        )

    def test_per_protocol_falls_back_to_first_segment(self):
        self.assertEqual(
            be.parse_proxy_server("http=1.2.3.4:8080;ftp=1.2.3.4:2121"),
            "http://1.2.3.4:8080",
        )

    def test_per_protocol_ignores_empty_segments(self):
        self.assertEqual(
            be.parse_proxy_server("socks=;http=1.2.3.4:8080"),
            "http://1.2.3.4:8080",
        )

    def test_keeps_existing_scheme(self):
        self.assertEqual(
            be.parse_proxy_server("socks5://127.0.0.1:1080"),
            "socks5://127.0.0.1:1080",
        )

    def test_empty_and_none_return_none(self):
        self.assertIsNone(be.parse_proxy_server(""))
        self.assertIsNone(be.parse_proxy_server(None))
        self.assertIsNone(be.parse_proxy_server("   "))


class DetectSystemProxyTests(unittest.TestCase):
    def detect(self, settings, platform="win32"):
        with mock.patch.object(be.sys, "platform", platform):
            with mock.patch.object(be, "_read_ie_settings", return_value=settings):
                return be.detect_system_proxy()

    def test_plain_registry_value(self):
        settings = {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897", "AutoConfigURL": None}
        self.assertEqual(self.detect(settings), "http://127.0.0.1:7897")

    def test_per_protocol_registry_value(self):
        settings = {
            "ProxyEnable": 1,
            "ProxyServer": "http=1.2.3.4:8080;https=1.2.3.4:8443",
            "AutoConfigURL": None,
        }
        self.assertEqual(self.detect(settings), "http://1.2.3.4:8443")

    def test_proxy_disabled_returns_none(self):
        settings = {"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7897", "AutoConfigURL": None}
        self.assertIsNone(self.detect(settings))

    def test_missing_proxy_enable_returns_none(self):
        self.assertIsNone(self.detect({"ProxyServer": "127.0.0.1:7897"}))

    def test_pac_mode_returns_none(self):
        settings = {
            "ProxyEnable": 1,
            "ProxyServer": "127.0.0.1:7897",
            "AutoConfigURL": "http://pac.example/proxy.pac",
        }
        self.assertIsNone(self.detect(settings))

    def test_empty_proxy_server_returns_none(self):
        settings = {"ProxyEnable": 1, "ProxyServer": "", "AutoConfigURL": None}
        self.assertIsNone(self.detect(settings))

    def test_empty_settings_returns_none(self):
        self.assertIsNone(self.detect({}))

    def test_non_windows_returns_none(self):
        settings = {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"}
        self.assertIsNone(self.detect(settings, platform="linux"))
        self.assertIsNone(self.detect(settings, platform="darwin"))

    def test_registry_read_failure_returns_none(self):
        with mock.patch.object(be.sys, "platform", "win32"):
            with mock.patch.object(be, "_read_ie_settings", side_effect=OSError("denied")):
                self.assertIsNone(be.detect_system_proxy())
            with mock.patch.object(be, "_read_ie_settings", side_effect=ImportError("no winreg")):
                self.assertIsNone(be.detect_system_proxy())

    def test_never_raises_for_garbage_enable_value(self):
        settings = {"ProxyEnable": "not-a-number", "ProxyServer": "127.0.0.1:7897"}
        self.assertIsNone(self.detect(settings))


if __name__ == "__main__":
    unittest.main()
