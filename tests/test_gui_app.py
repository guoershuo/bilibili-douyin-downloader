"""gui_app 的单元测试：纯逻辑为主，GUI 只做无交互 smoke。

GUI 用例在拿不到显示（tk.Tk() 抛 TclError）时自动 skip，
所以无头环境里整套测试依然能跑绿。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bilibili_downloader as bili  # noqa: E402
import douyin_downloader as douyin  # noqa: E402
import gui_app as gui  # noqa: E402


FUTURE = 4102444800  # 2100-01-01
PAST = 1000000000  # 2001-09-09


def netscape_cookie(expiry: int, name: str = "SESSDATA", value: str = "abc123") -> str:
    return (
        "# Netscape HTTP Cookie File\n"
        f".bilibili.com\tTRUE\t/\tFALSE\t{expiry}\t{name}\t{value}\n"
    )


class SplitLinksTests(unittest.TestCase):
    def test_drops_blank_lines_and_strips(self):
        text = "  https://a.example/1  \n\n\t\nhttps://a.example/2\n"
        self.assertEqual(gui.split_links(text), ["https://a.example/1", "https://a.example/2"])

    def test_deduplicates_keeping_order(self):
        text = "https://a/1\nhttps://a/2\nhttps://a/1\n"
        self.assertEqual(gui.split_links(text), ["https://a/1", "https://a/2"])

    def test_empty_text_gives_empty_list(self):
        self.assertEqual(gui.split_links(""), [])
        self.assertEqual(gui.split_links("   \n \n"), [])

    def test_share_text_lines_are_kept_verbatim(self):
        line = "7.43 复制打开抖音，看看 https://v.douyin.com/AbCdEfG/ 复制此链接"
        self.assertEqual(gui.split_links(line + "\n"), [line])


class DouyinFormatTests(unittest.TestCase):
    def test_maps_quality_to_yt_dlp_format(self):
        self.assertEqual(gui.douyin_format_for("best"), "best")
        self.assertEqual(gui.douyin_format_for("1080"), "best[height<=1080]")
        self.assertEqual(gui.douyin_format_for("720"), "best[height<=720]")
        self.assertEqual(gui.douyin_format_for("480"), "best[height<=480]")

    def test_unknown_quality_raises(self):
        with self.assertRaises(ValueError):
            gui.douyin_format_for("4k")


class BuildBilibiliArgvTests(unittest.TestCase):
    def test_minimal_argv_has_no_proxy_and_no_up(self):
        argv = gui.build_bilibili_argv(["https://b/1"], "D:/out", "1080")
        self.assertEqual(argv, ["-o", "D:/out", "--quality", "1080", "https://b/1"])
        self.assertNotIn("--proxy", argv)
        self.assertNotIn("--no-system-proxy", argv)
        self.assertNotIn("--up", argv)
        self.assertNotIn("--all", argv)
        self.assertNotIn("--limit", argv)

    def test_up_all_and_limit(self):
        argv = gui.build_bilibili_argv(
            [], "D:/out", "720", up=" 3546984120059960 ", download_all=True, limit=10
        )
        self.assertEqual(
            argv,
            ["-o", "D:/out", "--quality", "720", "--up", "3546984120059960", "--all", "--limit", "10"],
        )

    def test_zero_limit_is_omitted(self):
        argv = gui.build_bilibili_argv(["u1"], "D:/out", "best", up="123", limit=0)
        self.assertNotIn("--limit", argv)
        self.assertIn("--up", argv)

    def test_proxy_only_when_given(self):
        argv = gui.build_bilibili_argv(["u1"], "D:/out", "480", proxy="http://127.0.0.1:7890")
        self.assertIn("--proxy", argv)
        self.assertIn("http://127.0.0.1:7890", argv)
        blank = gui.build_bilibili_argv(["u1"], "D:/out", "480", proxy="   ")
        self.assertNotIn("--proxy", blank)

    def test_unknown_quality_and_negative_limit_raise(self):
        with self.assertRaises(ValueError):
            gui.build_bilibili_argv(["u"], "D:/out", "4320")
        with self.assertRaises(ValueError):
            gui.build_bilibili_argv(["u"], "D:/out", "1080", limit=-1)

    def test_argv_is_accepted_by_real_parser(self):
        argv = gui.build_bilibili_argv(
            ["https://www.bilibili.com/video/BV1xx411c7mD"],
            "D:/out",
            "720",
            up="3546984120059960",
            download_all=True,
            limit=3,
        )
        args = bili.build_parser().parse_args(argv)
        self.assertEqual(args.quality, "720")
        self.assertEqual(args.output, "D:/out")
        self.assertEqual(args.up, "3546984120059960")
        self.assertTrue(args.all)
        self.assertEqual(args.limit, 3)
        self.assertIsNone(args.proxy)
        self.assertEqual(args.links, ["https://www.bilibili.com/video/BV1xx411c7mD"])


class BuildDouyinArgvTests(unittest.TestCase):
    def test_minimal_argv(self):
        argv = gui.build_douyin_argv(["https://v.douyin.com/x/"], "D:/out", "best")
        self.assertEqual(argv, ["-o", "D:/out", "--format", "best", "https://v.douyin.com/x/"])
        self.assertNotIn("--proxy", argv)

    def test_quality_maps_to_height_filter(self):
        argv = gui.build_douyin_argv(["u"], "D:/out", "1080")
        self.assertEqual(argv[3], "best[height<=1080]")

    def test_proxy_only_when_given(self):
        argv = gui.build_douyin_argv(["u"], "D:/out", "720", proxy=" http://127.0.0.1:7890 ")
        self.assertIn("--proxy", argv)
        self.assertIn("http://127.0.0.1:7890", argv)

    def test_argv_is_accepted_by_real_parser(self):
        argv = gui.build_douyin_argv(["https://v.douyin.com/x/"], "D:/out", "480")
        args = douyin.build_parser().parse_args(argv)
        self.assertEqual(args.format, "best[height<=480]")
        self.assertEqual(args.output, "D:/out")
        self.assertIsNone(args.proxy)
        self.assertEqual(args.links, ["https://v.douyin.com/x/"])

    def test_all_flag_and_limit_for_user_home(self):
        home = "https://www.douyin.com/user/MS4wLjABAAAA"
        argv = gui.build_douyin_argv([], "D:/out", "best", up=home, download_all=True, limit=5)
        self.assertEqual(
            argv, ["-o", "D:/out", "--format", "best", "--all", "--limit", "5", home]
        )

    def test_zero_limit_omitted_and_all_only_when_checked(self):
        home = "https://www.douyin.com/user/MS4w"
        with_all = gui.build_douyin_argv(
            [], "D:/out", "best", up=home, download_all=True, limit=0
        )
        self.assertIn("--all", with_all)
        self.assertNotIn("--limit", with_all)
        without_all = gui.build_douyin_argv(["u"], "D:/out", "best", up=home, limit=0)
        self.assertNotIn("--all", without_all)
        self.assertNotIn("--limit", without_all)
        self.assertIn(home, without_all)

    def test_home_url_is_positional_and_deduped(self):
        home = "https://www.douyin.com/user/MS4w"
        argv = gui.build_douyin_argv(
            ["https://v.douyin.com/x/", home], "D:/out", "best", up=home, download_all=True, limit=3
        )
        self.assertEqual(argv.count(home), 1)
        self.assertEqual(
            argv,
            ["-o", "D:/out", "--format", "best", "--all", "--limit", "3", "https://v.douyin.com/x/", home],
        )

    def test_all_limit_argv_parses_with_real_parser(self):
        home = "https://www.douyin.com/user/MS4wLjABAAAA"
        argv = gui.build_douyin_argv([home], "D:/out", "720", up=home, download_all=True, limit=7)
        args = douyin.build_parser().parse_args(argv)
        self.assertTrue(args.all)
        self.assertEqual(args.limit, 7)
        self.assertEqual(args.output, "D:/out")
        self.assertEqual(args.format, "best[height<=720]")
        self.assertIsNone(args.proxy)
        self.assertEqual(args.links, [home])

    def test_negative_limit_raises(self):
        with self.assertRaises(ValueError):
            gui.build_douyin_argv(["u"], "D:/out", "best", limit=-2)


class ParseProgressTests(unittest.TestCase):
    def test_reads_percent_from_yt_dlp_line(self):
        line = "  [download]  42.3% of 208.10MiB at 5.13MiB/s"
        self.assertAlmostEqual(gui.parse_progress_percent(line) or -1, 42.3, places=3)

    def test_reads_integer_percent(self):
        self.assertEqual(gui.parse_progress_percent("[download] 100% of 5.00MiB"), 100.0)

    def test_ignores_other_lines(self):
        self.assertIsNone(gui.parse_progress_percent("[信息] 共 3 个待处理视频"))
        self.assertIsNone(gui.parse_progress_percent(""))
        self.assertIsNone(gui.parse_progress_percent("[download] Destination: a.mp4"))

    def test_clamps_out_of_range(self):
        self.assertEqual(gui.parse_progress_percent("[download] 140.0% of 1MiB"), 100.0)


class TaskCounterTests(unittest.TestCase):
    def test_counts_success_and_failure_across_entries(self):
        counter = gui.TaskCounter()
        counter.feed("===== [1/3] https://b/1 =====")
        self.assertEqual(counter.summary(), "第 1/3 个 · 0 成功 0 失败")
        counter.feed("[错误] 下载失败：boom")
        self.assertEqual(counter.summary(), "第 1/3 个 · 0 成功 1 失败")
        counter.feed("===== [2/3] https://b/2 =====")
        counter.feed("===== [3/3] https://b/3 =====")
        self.assertEqual(counter.summary(), "第 3/3 个 · 1 成功 1 失败")
        self.assertEqual(counter.finish(), "第 3/3 个 · 2 成功 1 失败")

    def test_error_does_not_double_count_same_entry(self):
        counter = gui.TaskCounter()
        counter.feed("[1/2] https://v.douyin.com/x/")
        counter.feed("[错误] 下载失败：a")
        counter.feed("[错误] 下载失败：b")
        self.assertEqual(counter.failed, 1)

    def test_done_summary_is_authoritative(self):
        counter = gui.TaskCounter()
        counter.feed("[1/1] u")
        summary = counter.feed("[完成] 成功 1 个，失败 0 个。")
        self.assertEqual(summary, "第 1/1 个 · 1 成功 0 失败")
        self.assertEqual((counter.succeeded, counter.failed), (1, 0))

    def test_done_summary_with_failures(self):
        counter = gui.TaskCounter()
        counter.feed("===== [1/4] https://b/1 =====")
        counter.feed("===== [2/4] https://b/2 =====")
        counter.feed("[完成] 成功 2 个，失败 2 个。")
        self.assertEqual(counter.summary(), "第 2/4 个 · 2 成功 2 失败")

    def test_summary_before_any_entry(self):
        self.assertEqual(gui.TaskCounter().summary(), "尚未开始")


class ClassifyLogLineTests(unittest.TestCase):
    def test_error_lines_are_red(self):
        self.assertEqual(gui.classify_log_line("[错误] 下载失败：x"), "error")
        self.assertEqual(gui.classify_log_line("ERROR: nope"), "error")

    def test_done_lines_are_green(self):
        self.assertEqual(gui.classify_log_line("[完成] 成功 2 个，失败 0 个。"), "done")

    def test_plain_lines_have_no_tag(self):
        self.assertEqual(gui.classify_log_line("[信息] 共 3 个待处理视频"), "")
        self.assertEqual(gui.classify_log_line(""), "")


class CancelTokenTests(unittest.TestCase):
    def test_starts_open_and_blocks_after_request(self):
        token = gui.CancelToken()
        self.assertFalse(token.is_set())
        self.assertTrue(token.should_start_next())
        token.request()
        self.assertTrue(token.is_set())
        self.assertFalse(token.should_start_next())
        token.reset()
        self.assertTrue(token.should_start_next())

    def test_task_cancelled_is_keyboard_interrupt(self):
        self.assertTrue(issubclass(gui.TaskCancelled, KeyboardInterrupt))
        self.assertFalse(issubclass(gui.TaskCancelled, Exception))


class QueueWriterTests(unittest.TestCase):
    def setUp(self):
        self.lines: list[str] = []
        self.writer = gui.QueueWriter(self.lines.append)

    def test_newline_splits_lines(self):
        self.writer.write("[信息] a\n[信息] b\n")
        self.assertEqual(self.lines, ["[信息] a", "[信息] b"])

    def test_carriage_return_splits_progress_frames(self):
        self.writer.write("[download]  10.0% of 1MiB\r[download]  55.5% of 1MiB\r")
        self.assertEqual(
            self.lines, ["[download]  10.0% of 1MiB", "[download]  55.5% of 1MiB"]
        )

    def test_partial_line_waits_then_flushes(self):
        self.writer.write("[download]  42.3% of 208.10MiB")
        self.assertEqual(self.lines, [])
        self.writer.flush()
        self.assertEqual(self.lines, ["[download]  42.3% of 208.10MiB"])

    def test_write_returns_length_and_is_not_a_tty(self):
        self.assertEqual(self.writer.write("abc"), 3)
        self.assertFalse(self.writer.isatty())
        self.assertTrue(self.writer.writable())

    def test_blank_segments_are_dropped(self):
        self.writer.write("\n\n   \n")
        self.assertEqual(self.lines, [])


class FakeDownloader:
    def download(self, url, cookie_browser=None):
        return 0


class InstallCancelHooksTests(unittest.TestCase):
    def _fake_module(self):
        return types.SimpleNamespace(
            Downloader=FakeDownloader,
            build_ydl_options=lambda *args, **kwargs: {"format": "best"},
        )

    def test_guards_entry_and_injects_progress_hook(self):
        module = self._fake_module()
        token = gui.CancelToken()
        restore = gui.install_cancel_hooks(module, token)
        try:
            downloader = module.Downloader()
            self.assertEqual(downloader.download("u"), 0)
            options = module.build_ydl_options()
            self.assertEqual(len(options["progress_hooks"]), 1)
            options["progress_hooks"][0]({"status": "downloading"})

            token.request()
            with self.assertRaises(gui.TaskCancelled):
                downloader.download("u")
            with self.assertRaises(gui.TaskCancelled):
                module.build_ydl_options()["progress_hooks"][0]({"status": "downloading"})
        finally:
            restore()

    def test_restore_puts_originals_back(self):
        module = self._fake_module()
        original_download = module.Downloader.download
        original_builder = module.build_ydl_options
        restore = gui.install_cancel_hooks(module, gui.CancelToken())
        self.assertIsNot(module.Downloader.download, original_download)
        self.assertIsNot(module.build_ydl_options, original_builder)
        restore()
        self.assertIs(module.Downloader.download, original_download)
        self.assertIs(module.build_ydl_options, original_builder)

    def test_real_modules_expose_the_seams(self):
        for module in (bili, douyin):
            original_download = module.Downloader.download
            original_builder = module.build_ydl_options
            restore = gui.install_cancel_hooks(module, gui.CancelToken())
            try:
                self.assertIsNot(module.Downloader.download, original_download)
                self.assertIsNot(module.build_ydl_options, original_builder)
            finally:
                restore()
            self.assertIs(module.Downloader.download, original_download)
            self.assertIs(module.build_ydl_options, original_builder)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "autoDownloads" / "gui.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        config = gui.load_config(self.path)
        self.assertEqual(config, gui.default_config())
        self.assertEqual(config["output_dir"], str(gui.DEFAULT_DOWNLOAD_DIR))

    def test_roundtrip(self):
        payload = {
            "output_dir": "D:/videos",
            "bilibili_quality": "720",
            "douyin_quality": "480",
            "up_limit": 7,
            "login_timeout": 120,
        }
        saved = gui.save_config(payload, self.path)
        self.assertEqual(saved, self.path)
        self.assertEqual(gui.load_config(self.path), payload)
        # 落在磁盘上的确实是 UTF-8 JSON
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["up_limit"], 7)

    def test_corrupt_json_falls_back(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(gui.load_config(self.path), gui.default_config())

    def test_non_dict_json_falls_back(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(gui.load_config(self.path), gui.default_config())

    def test_bad_values_are_normalized(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "output_dir": "   ",
                    "bilibili_quality": "4320",
                    "douyin_quality": "4k",
                    "up_limit": -9,
                    "login_timeout": "abc",
                }
            ),
            encoding="utf-8",
        )
        config = gui.load_config(self.path)
        self.assertEqual(config["output_dir"], gui.default_config()["output_dir"])
        self.assertEqual(config["bilibili_quality"], "1080")
        self.assertEqual(config["douyin_quality"], "best")
        self.assertEqual(config["up_limit"], 0)
        self.assertEqual(config["login_timeout"], gui.LOGIN_TIMEOUT_DEFAULT)

    def test_timeout_is_clamped(self):
        self.assertEqual(gui.normalize_config({"login_timeout": 1})["login_timeout"], gui.LOGIN_TIMEOUT_MIN)
        self.assertEqual(
            gui.normalize_config({"login_timeout": 999999})["login_timeout"], gui.LOGIN_TIMEOUT_MAX
        )

    def test_config_path_uses_localappdata(self):
        env = {"LOCALAPPDATA": str(self.tmp / "AppData")}
        self.assertEqual(
            gui.config_path(env), self.tmp / "AppData" / "autoDownloads" / "gui.json"
        )
        self.assertEqual(gui.config_dir(env).name, "autoDownloads")

    def test_config_path_is_not_inside_project(self):
        self.assertNotIn(gui.PROJECT_DIR, gui.config_path().parents)

    def test_save_failure_returns_none(self):
        blocked = self.tmp / "file.txt"
        blocked.write_text("x", encoding="utf-8")
        self.assertIsNone(gui.save_config({"up_limit": 1}, blocked / "gui.json"))


class BilibiliLoginTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "cookies.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_cookie_file(self):
        self.assertIn("未检测到", gui.bilibili_login_text(self.path))

    def test_valid_cookie(self):
        self.path.write_text(netscape_cookie(FUTURE), encoding="utf-8")
        text = gui.bilibili_login_text(self.path)
        self.assertIn("有效", text)
        self.assertIn("SESSDATA", text)

    def test_expired_cookie(self):
        self.path.write_text(netscape_cookie(PAST), encoding="utf-8")
        self.assertIn("已过期", gui.bilibili_login_text(self.path))

    def test_cookie_without_sessdata(self):
        self.path.write_text(netscape_cookie(FUTURE, name="buvid3"), encoding="utf-8")
        self.assertIn("没有 SESSDATA", gui.bilibili_login_text(self.path))


class DouyinLoginTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_profile_dir(self):
        self.assertIn("未检测到", gui.douyin_login_text(self.tmp / "nope"))
        self.assertIsNone(gui.douyin_login_cookie_file(self.tmp / "nope"))

    def test_profile_with_session_cookie(self):
        store = self.tmp / "Default" / "Network"
        store.mkdir(parents=True)
        (store / "Cookies").write_bytes(b"SQLite format 3\x00sessionid=abc\x00")
        found = gui.douyin_login_cookie_file(self.tmp)
        self.assertEqual(found, store / "Cookies")
        self.assertIn("可复用", gui.douyin_login_text(self.tmp))

    def test_profile_without_session_cookie(self):
        store = self.tmp / "Default" / "Network"
        store.mkdir(parents=True)
        (store / "Cookies").write_bytes(b"SQLite format 3\x00other=1\x00")
        self.assertIsNone(gui.douyin_login_cookie_file(self.tmp))
        self.assertIn("没有 sessionid", gui.douyin_login_text(self.tmp))


class EnvironmentTextTests(unittest.TestCase):
    def test_ffmpeg_text_never_raises(self):
        text = gui.ffmpeg_text()
        self.assertTrue(text.startswith("ffmpeg："))

    def test_proxy_text_never_raises(self):
        self.assertTrue(gui.proxy_text().startswith("系统代理："))

    def test_ffmpeg_text_reports_missing(self):
        with patch.object(bili, "find_ffmpeg", return_value=None):
            self.assertIn("未找到", gui.ffmpeg_text())
        with patch.object(bili, "find_ffmpeg", return_value=r"C:\ffmpeg.exe"):
            self.assertIn("可用", gui.ffmpeg_text())


class FormatTimestampTests(unittest.TestCase):
    def test_bad_values(self):
        self.assertEqual(gui.format_timestamp(None), "未知")
        self.assertEqual(gui.format_timestamp("abc"), "未知")
        self.assertEqual(gui.format_timestamp(0), "未知")

    def test_real_value(self):
        self.assertRegex(gui.format_timestamp(FUTURE), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")



class FakeTaskModule:
    """假的下载模块：只验证线程/队列/日志链路，不联网。"""

    class Downloader:
        def download(self, url, cookie_browser=None):
            return 0

    @staticmethod
    def build_ydl_options(*args, **kwargs):
        return {}

    @staticmethod
    def main(argv):
        print("[信息] 共 2 个待处理视频")
        print()
        print("===== [1/2] https://b/1 =====")
        print("  [download]  42.3% of 208.10MiB at 5.13MiB/s", file=sys.stderr)
        print("  [download] 100% of 208.10MiB at 5.13MiB/s")
        print("===== [2/2] https://b/2 =====")
        print("[错误] 下载失败：boom", file=sys.stderr)
        print("[完成] 成功 1 个，失败 1 个。")
        return 0


class RaisingTaskModule(FakeTaskModule):
    @staticmethod
    def main(argv):
        raise RuntimeError("模拟崩溃")


class SystemExitTaskModule(FakeTaskModule):
    @staticmethod
    def main(argv):
        raise SystemExit(2)



class FakeFullModule:
    """带真实 build_parser + 假 main 的模块：验证「开始下载」整条链路。"""

    class Downloader:
        def download(self, url, cookie_browser=None):
            return 0

    @staticmethod
    def build_ydl_options(*args, **kwargs):
        return {}

    @staticmethod
    def main(argv):
        print(f"[信息] fake main 收到参数：{argv}")
        print("===== [1/1] https://b/x =====")
        print("  [download] 100% of 1.00MiB at 1.00MiB/s")
        print("[完成] 成功 1 个，失败 0 个。")
        return 0


FakeFullModule.build_parser = staticmethod(bili.build_parser)


class FakeDouyinModule:
    """抖音侧的假模块：用抖音真实的 build_parser + 假 main。"""

    class Downloader:
        def download(self, url, cookie_browser=None):
            return 0

    @staticmethod
    def build_ydl_options(*args, **kwargs):
        return {}

    @staticmethod
    def main(argv):
        print(f"[信息] fake douyin main 收到参数：{argv}")
        print("===== [1/2] https://www.douyin.com/video/1 =====")
        print("  [download]  50.0% of 2.00MiB at 1.00MiB/s")
        print("===== [2/2] https://www.douyin.com/video/2 =====")
        print("  [download] 100% of 2.00MiB at 1.00MiB/s")
        print("[完成] 成功 2 个，失败 0 个。")
        return 0


FakeDouyinModule.build_parser = staticmethod(douyin.build_parser)


class GuiSmokeTests(unittest.TestCase):
    """构造并销毁主窗口；没有显示时自动 skip。"""

    def setUp(self):
        try:
            import tkinter as tk
        except ImportError as exc:  # pragma: no cover - 环境问题
            self.skipTest(f"没有 tkinter：{exc}")
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无头环境
            self.skipTest(f"没有可用显示：{exc}")
        self.root.withdraw()
        self._tmp = tempfile.TemporaryDirectory()
        self.config_file = Path(self._tmp.name) / "gui.json"
        self.app = gui.DownloadApp(self.root, config_path_override=self.config_file)

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            self.app.close()
        if getattr(self, "root", None) is not None:
            try:
                self.root.destroy()
            except Exception:  # noqa: BLE001 - 销毁阶段的 TclError 无所谓
                pass
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()

    def test_two_tabs_exist(self):
        notebook = self.app.notebook
        self.assertEqual(int(notebook.index("end")), 2)
        self.assertEqual([notebook.tab(i, "text") for i in range(2)], ["B 站", "抖音"])
        self.assertEqual(set(self.app.tabs), {"bilibili", "douyin"})

    def test_common_widgets_exist(self):
        names = (
            "links_text",
            "import_button",
            "quality_box",
            "output_entry",
            "browse_button",
            "start_button",
            "stop_button",
            "login_button",
            "open_dir_button",
            "progress",
            "progress_label",
            "status_label",
            "login_label",
            "env_label",
        )
        for platform, tab in self.app.tabs.items():
            for name in names:
                widget = getattr(tab, name)
                self.assertEqual(int(widget.winfo_exists()), 1, f"{platform}.{name} 不存在")

    def test_bilibili_extra_widgets(self):
        tab = self.app.tabs["bilibili"]
        self.assertEqual(int(tab.up_entry.winfo_exists()), 1)
        self.assertEqual(int(tab.all_check.winfo_exists()), 1)
        self.assertEqual(int(tab.limit_spin.winfo_exists()), 1)
        self.assertEqual(
            list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES)
        )
        self.assertEqual(tab.quality_box["values"][0], "4K(2160)")
        self.assertEqual(tab.quality(), "1080")
        self.assertFalse(tab.download_all())
        self.assertEqual(tab.limit(), 0)

    def test_bilibili_dropdown_offers_4k_and_2k_but_defaults_to_1080(self):
        tab = self.app.tabs["bilibili"]
        values = list(tab.quality_box["values"])
        self.assertIn("4K(2160)", values)
        self.assertIn("2K(1440)", values)
        self.assertEqual(tab.quality_var.get(), "1080P")
        self.assertEqual(tab.quality(), "1080")

    def test_selecting_4k_puts_2160_into_argv(self):
        tab = self.app.tabs["bilibili"]
        tab.quality_var.set("4K(2160)")
        self.assertEqual(tab.quality(), "2160")
        tab.links_text.insert("1.0", "https://www.bilibili.com/video/BV1R1e4zKEh1\n")
        tab.output_var.set(str(self._tmp.name))
        argv = tab.build_argv()
        self.assertEqual(argv[argv.index("--quality") + 1], "2160")
        self.assertIn("2160", argv)
        self.assertNotIn("4K(2160)", argv)

    def test_selecting_2k_puts_1440_into_argv(self):
        tab = self.app.tabs["bilibili"]
        tab.quality_var.set("2K(1440)")
        self.assertEqual(tab.quality(), "1440")
        self.assertEqual(tab.build_argv().count("1440"), 1)

    def test_read_quality_button_only_on_bilibili(self):
        self.assertIsNotNone(self.app.tabs["bilibili"].read_quality_button)
        self.assertIsNone(self.app.tabs["douyin"].read_quality_button)

    def test_unread_state_keeps_fixed_choices(self):
        tab = self.app.tabs["bilibili"]
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))
        self.assertEqual(tab.quality(), "1080")
        self.assertEqual(tab.quality_qn_map, {})

    def test_applying_tiers_replaces_dropdown_with_bilibili_labels(self):
        tab = self.app.tabs["bilibili"]
        ok = tab.apply_quality_tiers(TIERS_FIXTURE, link="BV1abc")
        self.assertTrue(ok)
        self.assertEqual(
            list(tab.quality_box["values"]),
            ["真彩 HDR", "超清 4K", "高清 1080P60", "高清 1080P"],
        )
        self.assertEqual(tab.quality_var.get(), "真彩 HDR")
        self.assertEqual(tab.quality(), "125")
        tab.quality_var.set("高清 1080P60")
        self.assertEqual(tab.quality(), "116")

    def test_selected_tier_reaches_argv_as_qn(self):
        tab = self.app.tabs["bilibili"]
        tab.apply_quality_tiers(TIERS_FIXTURE, link="BV1abc")
        tab.quality_var.set("超清 4K")
        tab.links_text.insert("1.0", "https://www.bilibili.com/video/BV1R1e4zKEh1\n")
        tab.output_var.set(str(self._tmp.name))
        argv = tab.build_argv()
        self.assertEqual(argv[argv.index("--qn") + 1], "120")
        self.assertNotIn("超清 4K", argv)
        self.assertNotIn("--quality", argv)

    def test_failed_read_falls_back_to_fixed_choices(self):
        tab = self.app.tabs["bilibili"]
        tab.apply_quality_tiers(TIERS_FIXTURE, link="BV1abc")
        self.assertFalse(tab.apply_quality_tiers(None))
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))
        self.assertEqual(tab.quality(), "1080")
        self.assertEqual(tab.quality_qn_map, {})

    def test_read_qualities_without_links_does_not_crash(self):
        tab = self.app.tabs["bilibili"]
        tab._read_qualities()
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))

    def test_douyin_quality_choices(self):
        tab = self.app.tabs["douyin"]
        self.assertEqual(list(tab.quality_box["values"]), list(gui.DOUYIN_QUALITIES))
        self.assertEqual(tab.quality(), "best")

    def test_default_output_dir_is_project_downloads(self):
        self.assertEqual(self.app.tabs["bilibili"].output_dir(), gui.DEFAULT_DOWNLOAD_DIR)

    def test_links_and_argv_from_widgets(self):
        tab = self.app.tabs["bilibili"]
        tab.links_text.insert("1.0", "https://www.bilibili.com/video/BV1xx411c7mD\n\n")
        tab.up_entry.insert(0, "3546984120059960")
        tab.up_all_var.set(True)
        tab.limit_var.set(5)
        tab.quality_var.set("720")
        tab.output_var.set(str(self._tmp.name))
        argv = tab.build_argv()
        self.assertEqual(
            argv,
            [
                "-o",
                self._tmp.name,
                "--quality",
                "720",
                "--up",
                "3546984120059960",
                "--all",
                "--limit",
                "5",
                "https://www.bilibili.com/video/BV1xx411c7mD",
            ],
        )

    def test_log_panel_is_readonly_and_tagged(self):
        self.assertEqual(self.app.log.text.cget("state"), "disabled")
        names = self.app.log.text.tag_names()
        self.assertIn("error", names)
        self.assertIn("done", names)
        self.app.log.append("[错误] 下载失败：x")
        self.app.log.append("[完成] 全部完成")
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("[错误] 下载失败：x", content)
        self.assertIn("[完成] 全部完成", content)

    def test_progress_bar_updates_from_download_line(self):
        tab = self.app.tabs["bilibili"]
        self.app._update_progress("bilibili", "  [download]  42.3% of 208.10MiB at 5.13MiB/s")
        self.assertAlmostEqual(float(tab.progress["value"]), 42.3, places=3)
        self.app._update_progress("bilibili", "===== [2/5] https://b/2 =====")
        self.assertEqual(float(tab.progress["value"]), 0.0)
        self.assertEqual(tab.progress_label["text"], "第 2/5 个 · 0 成功 0 失败")

    def test_status_row_is_filled(self):
        bili_tab = self.app.tabs["bilibili"]
        self.assertIn("B 站登录态", bili_tab.login_label["text"])
        self.assertTrue(bili_tab.env_label["text"].startswith("ffmpeg："))
        self.assertTrue(bili_tab.proxy_label["text"].startswith("系统代理："))
        douyin_tab = self.app.tabs["douyin"]
        self.assertIn("抖音登录态", douyin_tab.login_label["text"])

    def test_parse_args_preview_accepts_and_rejects(self):
        argv = gui.build_bilibili_argv(
            ["https://www.bilibili.com/video/BV1xx411c7mD"],
            self._tmp.name,
            "720",
            up="3546984120059960",
            download_all=True,
            limit=5,
        )
        namespace = self.app._parse_args_preview("bilibili", argv)
        self.assertIsNotNone(namespace)
        self.assertEqual(namespace.quality, "720")
        self.assertEqual(namespace.up, "3546984120059960")
        self.assertTrue(namespace.all)
        self.assertEqual(namespace.limit, 5)
        self.assertIsNone(namespace.proxy)

        self.assertIsNone(self.app._parse_args_preview("bilibili", ["--quality", "4320", "x"]))
        self.app._poll()
        self.assertIn("usage:", self.app.log.text.get("1.0", "end-1c"))

    def test_start_download_runs_task_and_returns_to_idle(self):
        tab = self.app.tabs["bilibili"]
        tab.links_text.insert("1.0", "https://www.bilibili.com/video/BV1xx411c7mD\n")
        tab.output_var.set(self._tmp.name)
        with patch.dict(gui.PLATFORM_MODULES, {"bilibili": FakeFullModule}), patch.object(
            gui.messagebox, "showwarning"
        ) as warn, patch.object(gui.messagebox, "showerror") as err:
            self.app.start_download()
            self.assertTrue(self.app.busy)
            self.assertIsNotNone(self.app.worker)
            self.app.worker.join(10)
            self.app._poll()
        self.assertFalse(warn.called, "不应弹缺少链接/参数警告")
        self.assertFalse(err.called, "不应弹错误对话框")
        self.assertFalse(self.app.busy)
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("参数自检通过：quality=1080", content)
        self.assertIn("fake main 收到参数", content)
        self.assertIn("[完成] 成功 1 个，失败 0 个。", content)
        self.assertEqual(float(tab.progress["value"]), 100.0)
        self.assertEqual(str(tab.start_button["state"]), "normal")
        self.assertEqual(str(tab.stop_button["state"]), "disabled")

    def test_start_download_without_links_does_not_start(self):
        with patch.object(gui.messagebox, "showwarning") as warn:
            self.app.start_download()
        self.assertTrue(warn.called)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.worker)

    def test_config_is_written_on_close(self):
        tab = self.app.tabs["bilibili"]
        tab.quality_var.set("480")
        tab.limit_var.set(3)
        tab.output_var.set(str(self._tmp.name))
        self.app.login_timeout_var.set(180)
        self.app.close()
        stored = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["bilibili_quality"], "480")
        self.assertEqual(stored["up_limit"], 3)
        self.assertEqual(stored["login_timeout"], 180)
        self.assertEqual(stored["output_dir"], str(self._tmp.name))

    def test_config_is_loaded_into_widgets(self):
        self.config_file.write_text(
            json.dumps(
                {
                    "output_dir": str(self._tmp.name),
                    "bilibili_quality": "720",
                    "douyin_quality": "480",
                    "up_limit": 9,
                    "login_timeout": 60,
                }
            ),
            encoding="utf-8",
        )
        app = gui.DownloadApp(self.root, config_path_override=self.config_file)
        try:
            self.assertEqual(app.tabs["bilibili"].quality(), "720")
            self.assertEqual(app.tabs["douyin"].quality(), "480")
            self.assertEqual(app.tabs["bilibili"].limit(), 9)
            self.assertEqual(int(app.login_timeout_var.get()), 60)
            self.assertEqual(app.tabs["bilibili"].output_dir(), Path(self._tmp.name))
        finally:
            app.close()

    def test_worker_task_feeds_queue_and_restores_buttons(self):
        tab = self.app.tabs["bilibili"]
        self.app.busy = True
        tab.set_busy(True)
        with patch.dict(gui.PLATFORM_MODULES, {"bilibili": FakeTaskModule}):
            self.app._run_task("bilibili", ["x"])
            self.app._poll()
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("===== [1/2] https://b/1 =====", content)
        self.assertIn("[错误] 下载失败：boom", content)
        self.assertIn("[完成] 成功 1 个，失败 1 个。", content)
        self.assertFalse(self.app.busy)
        self.assertEqual(str(tab.start_button["state"]), "normal")
        self.assertEqual(str(tab.stop_button["state"]), "disabled")
        self.assertIn("1 成功 1 失败", tab.progress_label["text"])
        # hook 已还原，不会残留
        self.assertIs(FakeTaskModule.Downloader.download.__name__, "download")

    def test_worker_crash_is_reported_not_swallowed(self):
        tab = self.app.tabs["bilibili"]
        self.app.busy = True
        tab.set_busy(True)
        with patch.dict(gui.PLATFORM_MODULES, {"bilibili": RaisingTaskModule}):
            self.app._run_task("bilibili", ["x"])
            self.app._poll()
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("[错误] 下载线程异常：", content)
        self.assertIn("模拟崩溃", content)
        self.assertFalse(self.app.busy)
        self.assertEqual(str(tab.start_button["state"]), "normal")

    def test_system_exit_is_reported(self):
        self.app.busy = True
        with patch.dict(gui.PLATFORM_MODULES, {"bilibili": SystemExitTaskModule}):
            self.app._run_task("bilibili", ["x"])
            self.app._poll()
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("[错误] 参数被拒绝（退出码 2）", content)
        self.assertFalse(self.app.busy)

    def test_cancel_marks_stopped_and_restores_buttons(self):
        tab = self.app.tabs["bilibili"]
        self.app.busy = True
        tab.set_busy(True)
        self.app.cancel.request()
        self.app.log.append(f"[提示] {gui.CANCEL_NOTICE}", "notice")
        with patch.dict(gui.PLATFORM_MODULES, {"bilibili": FakeTaskModule}):
            self.app._run_task("bilibili", ["x"])
            self.app._poll()
        self.assertFalse(self.app.busy)
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("已停止：不再开始剩余条目。", content)
        self.assertFalse(self.app.cancel.is_set())
        self.assertIn("已停止", tab.status_label["text"])

    # -- 缺陷 1：日志区高度 -------------------------------------------------

    def _pump(self, predicate=None, timeout: float = 3.0) -> bool:
        """跑一会儿 Tk 事件循环；窗口被销毁时返回 predicate()。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.root.update()
            except Exception:  # noqa: BLE001 - 已销毁
                return bool(predicate()) if predicate else True
            if predicate is not None and predicate():
                return True
            time.sleep(0.02)
        return bool(predicate()) if predicate else True

    def test_log_panel_shows_at_least_eight_lines(self):
        self.root.deiconify()
        self.root.geometry("1000x800")
        self.root.update_idletasks()
        self.root.update()

        text = self.app.log.text
        height = text.winfo_height()
        if height <= 1:
            self.skipTest("当前环境不映射窗口，winfo_height 不可测")
        metrics = tkfont.Font(root=self.root, font=text.cget("font"))
        linespace = int(metrics.metrics("linespace"))
        print(
            f"\n[实测 1000x800] log_text.winfo_height()={height} linespace={linespace} "
            f"可见行数={height / linespace:.2f}",
            end="",
        )
        self.assertGreaterEqual(height, linespace * 8)

        self.root.geometry("1200x1000")
        self.root.update_idletasks()
        self.root.update()
        bigger = text.winfo_height()
        print(
            f"\n[实测 1200x1000] log_text.winfo_height()={bigger} "
            f"可见行数={bigger / linespace:.2f}",
            end="",
        )
        self.assertGreater(bigger, height)

    def test_log_panel_min_height_is_pixel_floor_for_panel(self):
        self.assertGreaterEqual(self.app.log_panel_min_height, gui.LOG_PANEL_MIN_HEIGHT)
        self.assertIsInstance(self.app.log_panel_min_height, int)

    # -- 缺陷 2：抖音主页控件 ------------------------------------------------

    def test_douyin_has_same_up_block_as_bilibili(self):
        for platform in ("bilibili", "douyin"):
            tab = self.app.tabs[platform]
            for name in ("up_entry", "all_check", "limit_spin", "up_hint_label"):
                self.assertEqual(
                    int(getattr(tab, name).winfo_exists()), 1, f"{platform}.{name} 不存在"
                )
        bili_box = self.app.tabs["bilibili"].up_entry.master
        douyin_box = self.app.tabs["douyin"].up_entry.master
        self.assertIn("UP 主", bili_box.cget("text"))
        self.assertIn("抖音用户主页", douyin_box.cget("text"))
        self.assertEqual(
            self.app.tabs["bilibili"].all_check.cget("text"), "下载该 UP 主全部视频"
        )
        self.assertEqual(
            self.app.tabs["douyin"].all_check.cget("text"), "下载该用户主页全部作品"
        )
        # 两页控件同宽同列，视觉对齐
        self.assertEqual(
            self.app.tabs["bilibili"].up_entry.winfo_reqwidth(),
            self.app.tabs["douyin"].up_entry.winfo_reqwidth(),
        )
        self.assertEqual(
            self.app.tabs["bilibili"].limit_spin.winfo_reqwidth(),
            self.app.tabs["douyin"].limit_spin.winfo_reqwidth(),
        )

    def test_douyin_tab_argv_from_widgets(self):
        tab = self.app.tabs["douyin"]
        home = "https://www.douyin.com/user/MS4wLjABAAAA"
        tab.up_entry.insert(0, home)
        tab.up_all_var.set(True)
        tab.limit_var.set(4)
        tab.quality_var.set("720")
        tab.output_var.set(self._tmp.name)
        self.assertEqual(
            tab.build_argv(),
            [
                "-o",
                self._tmp.name,
                "--format",
                "best[height<=720]",
                "--all",
                "--limit",
                "4",
                home,
            ],
        )
        # 0 -> 不传 --limit
        tab.limit_var.set(0)
        self.assertNotIn("--limit", tab.build_argv())
        # 取消勾选 -> 不传 --all，主页 URL 仍是位置参数
        tab.up_all_var.set(False)
        argv = tab.build_argv()
        self.assertNotIn("--all", argv)
        self.assertIn(home, argv)

    def test_douyin_batch_can_start_without_pasted_links(self):
        tab = self.app.tabs["douyin"]
        tab.up_entry.insert(0, "https://www.douyin.com/user/MS4wLjABAAAA")
        tab.up_all_var.set(True)
        tab.output_var.set(self._tmp.name)
        with patch.dict(gui.PLATFORM_MODULES, {"douyin": FakeDouyinModule}), patch.object(
            gui.messagebox, "showwarning"
        ) as warn:
            self.app.notebook.select(1)
            self.app.start_download()
            self.assertIsNotNone(self.app.worker, "填了主页 URL 并勾选批量时应该能开始")
            self.app.worker.join(10)
            self.app._poll()
        self.assertFalse(warn.called, "填了主页 URL 并勾选批量时不应提示缺少链接")
        content = self.app.log.text.get("1.0", "end-1c")
        self.assertIn("参数自检通过：format=best all=True limit=0", content)
        self.assertIn("fake douyin main 收到参数", content)
        self.assertIn("https://www.douyin.com/user/MS4wLjABAAAA", content)

    # -- 改进 3：关窗等待 ---------------------------------------------------

    def _record_log(self):
        recorded: list[str] = []
        original = self.app.log.append

        def append(line, tag=None):
            recorded.append(line)
            try:
                original(line, tag)
            except Exception:  # noqa: BLE001 - 窗口已销毁
                pass

        self.app.log.append = append
        return recorded

    def test_on_close_waits_for_worker_then_destroys(self):
        app = self.app
        recorded = self._record_log()
        calls = {"n": 0}

        def is_alive():
            calls["n"] += 1
            return calls["n"] <= 2

        app.worker = types.SimpleNamespace(is_alive=is_alive, join=lambda timeout=None: None)
        app.close_wait_seconds = 5.0
        app.on_close()
        self.assertFalse(app.destroyed, "第一次不应立刻销毁")
        self.assertTrue(app.cancel.is_set())
        self.assertTrue(self._pump(lambda: app.destroyed))
        self.assertTrue(any("正在等待当前条目收尾" in line for line in recorded), recorded)
        self.assertFalse(any("等待超时" in line for line in recorded), recorded)

    def test_on_close_times_out_and_force_closes(self):
        app = self.app
        recorded = self._record_log()
        app.worker = types.SimpleNamespace(is_alive=lambda: True, join=lambda timeout=None: None)
        app.close_wait_seconds = 0.3
        started = time.monotonic()
        app.on_close()
        self.assertTrue(self._pump(lambda: app.destroyed, timeout=5.0))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "超时分支不应真等 15 秒")
        self.assertTrue(any("等待超时" in line for line in recorded), recorded)
        self.assertTrue(any(".part" in line for line in recorded), recorded)

    def test_on_close_without_worker_closes_immediately(self):
        app = self.app
        app.worker = None
        app.on_close()
        self.assertTrue(app.destroyed)

    def test_busy_state_toggles_buttons(self):
        tab = self.app.tabs["bilibili"]
        tab.set_busy(True)
        self.assertEqual(str(tab.start_button["state"]), "disabled")
        self.assertEqual(str(tab.stop_button["state"]), "normal")
        tab.set_busy(False)
        self.assertEqual(str(tab.start_button["state"]), "normal")
        self.assertEqual(str(tab.stop_button["state"]), "disabled")


class BilibiliQualityChoiceTests(unittest.TestCase):
    def test_dropdown_offers_4k_and_2k(self):
        self.assertEqual(
            gui.BILIBILI_QUALITY_CHOICES,
            ("4K(2160)", "2K(1440)", "1080P", "720P", "480P", "最佳(best)"),
        )
        self.assertIn("4K(2160)", gui.BILIBILI_QUALITY_CHOICES)
        self.assertIn("2K(1440)", gui.BILIBILI_QUALITY_CHOICES)
        self.assertEqual(gui.BILIBILI_QUALITIES[0], "2160")

    def test_label_roundtrip_for_every_tier(self):
        for value in gui.BILIBILI_QUALITIES:
            label = gui.quality_label(value)
            self.assertEqual(gui.quality_from_choice(label), value)

    def test_bare_and_legacy_values_accepted(self):
        self.assertEqual(gui.quality_from_choice("2160"), "2160")
        self.assertEqual(gui.quality_from_choice("1440"), "1440")
        self.assertEqual(gui.quality_from_choice("1080"), "1080")

    def test_illegal_falls_back_to_default(self):
        self.assertEqual(gui.quality_from_choice("4320"), "1080")
        self.assertEqual(gui.quality_from_choice("胡写"), "1080")
        self.assertEqual(gui.quality_from_choice(""), "1080")
        self.assertEqual(gui.quality_label(None), "")

    def test_argv_gets_2160(self):
        argv = gui.build_bilibili_argv(
            ["https://www.bilibili.com/video/BV1R1e4zKEh1"], "out", "2160"
        )
        self.assertEqual(argv[:4], ["-o", "out", "--quality", "2160"])

    def test_argv_gets_1440(self):
        argv = gui.build_bilibili_argv(
            ["https://www.bilibili.com/video/BV1R1e4zKEh1"], "out", "1440"
        )
        self.assertIn("1440", argv)
        self.assertEqual(argv[argv.index("--quality") + 1], "1440")

    def test_unknown_quality_still_rejected_by_argv_builder(self):
        with self.assertRaises(ValueError):
            gui.build_bilibili_argv(["BV1"], "out", "4320")

    def test_config_accepts_new_values(self):
        for value in ("2160", "1440"):
            config = gui.normalize_config({"bilibili_quality": value})
            self.assertEqual(config["bilibili_quality"], value)

    def test_old_config_still_loads(self):
        config = gui.normalize_config({"bilibili_quality": "1080"})
        self.assertEqual(config["bilibili_quality"], "1080")
        config = gui.normalize_config({"bilibili_quality": "480"})
        self.assertEqual(config["bilibili_quality"], "480")

    def test_illegal_config_value_falls_back(self):
        self.assertEqual(
            gui.normalize_config({"bilibili_quality": "4320"})["bilibili_quality"], "1080"
        )
        self.assertEqual(
            gui.normalize_config({"bilibili_quality": 9999})["bilibili_quality"], "1080"
        )
        # 数字形态的合法值仍然被接受（JSON 里手写成 2160 也能用）
        self.assertEqual(
            gui.normalize_config({"bilibili_quality": 2160})["bilibili_quality"], "2160"
        )

    def test_douyin_dropdown_unchanged(self):
        self.assertEqual(gui.DOUYIN_QUALITIES, ("best", "1080", "720", "480"))
        self.assertNotIn("2160", gui.DOUYIN_QUALITIES)
        self.assertEqual(
            gui.normalize_config({"douyin_quality": "2160"})["douyin_quality"], "best"
        )
        self.assertEqual(
            gui.normalize_config({"douyin_quality": "720"})["douyin_quality"], "720"
        )

    def test_default_bilibili_quality_unchanged(self):
        self.assertEqual(gui.DEFAULT_CONFIG["bilibili_quality"], "1080")
        self.assertEqual(gui.normalize_config({})["bilibili_quality"], "1080")


TIERS_FIXTURE = [
    {"qn": 125, "label": "真彩 HDR", "need_vip": True, "available": True, "codecs": ["hevc"]},
    {"qn": 120, "label": "超清 4K", "need_vip": True, "available": True, "codecs": ["avc1", "hevc", "av01"]},
    {"qn": 116, "label": "高清 1080P60", "need_vip": True, "available": True, "codecs": ["avc1"]},
    {"qn": 80, "label": "高清 1080P", "need_vip": False, "available": True, "codecs": ["avc1"]},
]


class BilibiliQnArgvTests(unittest.TestCase):
    def test_qn_selection_uses_qn_flag(self):
        argv = gui.build_bilibili_argv(["BV1"], "out", "120")
        self.assertEqual(argv[:4], ["-o", "out", "--qn", "120"])
        self.assertNotIn("--quality", argv)

    def test_qn_125_used_for_hdr(self):
        argv = gui.build_bilibili_argv(["BV1"], "out", "125")
        self.assertEqual(argv[argv.index("--qn") + 1], "125")

    def test_fixed_quality_still_uses_quality_flag(self):
        for value in ("2160", "1440", "1080", "720", "480", "best"):
            argv = gui.build_bilibili_argv(["BV1"], "out", value)
            self.assertEqual(argv[argv.index("--quality") + 1], value)

    def test_bogus_quality_still_rejected(self):
        for value in ("4320", "真彩 HDR", ""):
            with self.assertRaises(ValueError):
                gui.build_bilibili_argv(["BV1"], "out", value)

    def test_tier_log_lines(self):
        lines = gui.quality_tier_log_lines(TIERS_FIXTURE, link="BV1abc")
        text = chr(10).join(lines)
        self.assertIn("已读取清晰度：BV1abc", text)
        self.assertIn("真彩 HDR", text)
        self.assertIn("qn=125", text)
        self.assertIn("大会员", text)
        self.assertIn("可用", text)
        self.assertIn("编码 avc1/hevc/av01", text)

    def test_read_quality_tiers_failure_returns_none(self):
        with patch.object(gui.bili, "fetch_quality_tiers", side_effect=RuntimeError("boom")):
            self.assertIsNone(gui.read_quality_tiers("BV1"))
        with patch.object(gui.bili, "fetch_quality_tiers", return_value=None):
            self.assertIsNone(gui.read_quality_tiers("BV1"))

    def test_read_quality_tiers_returns_tiers(self):
        with patch.object(gui.bili, "fetch_quality_tiers", return_value=TIERS_FIXTURE) as probe:
            with patch.object(gui.bili, "read_cookie_text", return_value=""):
                self.assertEqual(gui.read_quality_tiers("BV1"), TIERS_FIXTURE)
        self.assertTrue(probe.called)




# --------------------------------------------------------------------------
# 清晰度自动读取（防抖 / 过期丢弃 / 缓存 / 抖音两分支 / 控件级）
# --------------------------------------------------------------------------


def _parser_with_gear():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--gear", metavar="GEAR")
    parser.add_argument("links", nargs="*")
    return parser


TIERS_HDR_LOCKED = [
    {"qn": 125, "label": "真彩 HDR", "need_vip": True, "available": False, "codecs": ["hevc"]},
    {"qn": 120, "label": "超清 4K", "need_vip": True, "available": True, "codecs": ["avc1"]},
    {"qn": 80, "label": "高清 1080P", "need_vip": False, "available": True, "codecs": ["avc1"]},
]
TIERS_OTHER = [
    {"qn": 116, "label": "高清 1080P60", "need_vip": True, "available": True, "codecs": ["avc1"]},
    {"qn": 74, "label": "高清 720P", "need_vip": False, "available": True, "codecs": ["avc1"]},
]
DOUYIN_PAYLOAD_OK = {
    "ok": True,
    "title": "演示视频",
    "author": "某作者",
    "duration": 12.5,
    "qualities": [
        {"gear": "720_2_1", "label": "720P", "codec": "h265", "bitrate": 824, "height": 1280},
        {"gear": "720_1_1", "label": "720P", "codec": "h264", "bitrate": 1200, "height": 1280},
    ],
    "recommended": "720_2_1",
}


class DouyinQualityReaderTests(unittest.TestCase):
    """list_qualities 的特性检测与容错（不需要 Tk）。"""

    def test_missing_reader_reports_unavailable(self):
        payload = gui.read_douyin_qualities("https://v.douyin.com/x/", module=types.SimpleNamespace())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["unavailable"])
        self.assertIn("list_qualities", payload["error"])
        self.assertEqual(payload["qualities"], [])

    def test_feature_detection_never_calls_the_real_reader(self):
        """特性检测只看函数在不在，绝不真的去启动浏览器。"""
        reader = gui.douyin_quality_reader(douyin)
        if callable(getattr(douyin, "list_qualities", None)):
            self.assertIsNotNone(reader)
        else:
            self.assertIsNone(reader)
        self.assertIsNone(gui.douyin_quality_reader(types.SimpleNamespace()))

    def test_real_reader_signature_matches_our_call(self):
        """真实 list_qualities 的签名必须能吃下 (url, timeout=...)，否则界面得回退。"""
        reader = getattr(douyin, "list_qualities", None)
        if not callable(reader):
            self.skipTest("当前 douyin_downloader 尚无 list_qualities")
        import inspect

        params = inspect.signature(reader).parameters
        self.assertIn("url", params)
        self.assertIn("timeout", params)
        self.assertEqual(params["timeout"].default, 30.0)

    def test_reader_exception_becomes_error_payload(self):
        def boom(url, timeout=0):
            raise RuntimeError("风控拦截")

        payload = gui.read_douyin_qualities("u", module=types.SimpleNamespace(list_qualities=boom))
        self.assertFalse(payload["ok"])
        self.assertIn("风控拦截", payload["error"])

    def test_reader_ok_payload_passthrough(self):
        module = types.SimpleNamespace(list_qualities=lambda url, timeout=0: dict(DOUYIN_PAYLOAD_OK))
        payload = gui.read_douyin_qualities("u", module=module)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["qualities"], DOUYIN_PAYLOAD_OK["qualities"])
        self.assertEqual(payload["recommended"], "720_2_1")

    def test_reader_with_positional_only_signature(self):
        seen = {}

        def reader(url):
            seen["url"] = url
            return {"ok": True, "qualities": [], "recommended": ""}

        payload = gui.read_douyin_qualities("u", module=types.SimpleNamespace(list_qualities=reader))
        self.assertTrue(payload["ok"])
        self.assertEqual(seen["url"], "u")

    def test_non_dict_result_is_error(self):
        module = types.SimpleNamespace(list_qualities=lambda url, timeout=0: "nope")
        payload = gui.read_douyin_qualities("u", module=module)
        self.assertFalse(payload["ok"])
        self.assertIn("str", payload["error"])

    def test_falsy_ok_payload_keeps_error(self):
        module = types.SimpleNamespace(
            list_qualities=lambda url, timeout=0: {"ok": False, "error": "风控拦截（HTTP 403）"}
        )
        payload = gui.read_douyin_qualities("u", module=module)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "风控拦截（HTTP 403）")


class DouyinGearDetectionTests(unittest.TestCase):
    def test_true_when_parser_has_gear(self):
        self.assertTrue(gui.douyin_gear_supported(types.SimpleNamespace(build_parser=_parser_with_gear)))

    def test_false_without_build_parser(self):
        self.assertFalse(gui.douyin_gear_supported(types.SimpleNamespace()))

    def test_false_when_builder_raises(self):
        def boom():
            raise RuntimeError("x")

        self.assertFalse(gui.douyin_gear_supported(types.SimpleNamespace(build_parser=boom)))

    def test_real_module_follows_its_own_parser(self):
        parser = douyin.build_parser()
        expected = any("--gear" in (action.option_strings or ()) for action in parser._actions)
        self.assertEqual(gui.douyin_gear_supported(douyin), expected)

    def test_default_module_argument_used(self):
        self.assertEqual(gui.douyin_gear_supported(), gui.douyin_gear_supported(douyin))


class BuildDouyinArgvGearTests(unittest.TestCase):
    def test_gear_flag_when_supported(self):
        module = types.SimpleNamespace(build_parser=_parser_with_gear)
        argv = gui.build_douyin_argv(["u"], "out", "best", gear="720_2_1", module=module)
        self.assertEqual(argv, ["-o", "out", "--gear", "720_2_1", "u"])

    def test_falls_back_to_format_when_unsupported(self):
        argv = gui.build_douyin_argv(
            ["u"], "out", "720", gear="720_2_1", module=types.SimpleNamespace()
        )
        self.assertEqual(argv, ["-o", "out", "--format", "best[height<=720]", "u"])

    def test_no_gear_keeps_format_untouched(self):
        argv = gui.build_douyin_argv(["u"], "out", "best", module=types.SimpleNamespace())
        self.assertEqual(argv, ["-o", "out", "--format", "best", "u"])


class BilibiliQualityReadTests(unittest.TestCase):
    """B 站档位读取：25 秒超时、失败原因分类、超时/网络重试一次、接口错误不重试。"""

    def test_timeout_constants(self):
        self.assertEqual(gui.BILIBILI_QUALITY_TIMEOUT, 45.0)
        self.assertEqual(gui.QUALITY_READ_TIMEOUT, 45.0)
        self.assertEqual(gui.QUALITY_FAST_FAILURE_SECONDS, 5.0)
        self.assertEqual(gui.DOUYIN_QUALITY_TIMEOUT, 30.0)
        self.assertEqual(gui.QUALITY_RETRY_DELAY, 0.5)
        self.assertEqual(gui.QUALITY_MAX_ATTEMPTS, 2)
        import inspect

        self.assertEqual(
            inspect.signature(gui.read_quality_tiers).parameters["timeout"].default, 45.0
        )
        self.assertEqual(
            inspect.signature(gui.read_bilibili_qualities).parameters["timeout"].default, 45.0
        )

    def test_classify_error_kinds(self):
        self.assertEqual(gui.classify_quality_error(TimeoutError("timed out"))[0], "timeout")
        self.assertIn("超时 45 秒", gui.classify_quality_error(TimeoutError("x"))[1])
        self.assertEqual(gui.classify_quality_error(OSError("SSL: reset by peer"))[0], "network")
        self.assertEqual(gui.classify_quality_error(ValueError("bad json"))[0], "api")
        self.assertEqual(gui.classify_quality_error(None)[0], "unknown")
        self.assertEqual(gui.classify_quality_error(None, has_cookie=False)[0], "no-cookie")
        self.assertIn("没有登录 Cookie", gui.classify_quality_error(None, has_cookie=False)[1])

    def test_timeout_retries_once_then_succeeds(self):
        with patch.object(
            gui.bili,
            "fetch_quality_tiers",
            side_effect=[TimeoutError("timed out"), TIERS_HDR_LOCKED],
        ) as probe:
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(payload["tiers"], TIERS_HDR_LOCKED)
        self.assertEqual(probe.call_args.kwargs["timeout"], 45.0)
        self.assertTrue(payload["retried"], "快失败（即时抛错）应当重试一次")
        self.assertIn("已重试 1 次", payload["detail"])
        # 第一次必须直连（不能强加系统代理），重试才兜底改走代理
        self.assertNotIn("proxy", probe.call_args_list[0].kwargs)
        detected = gui.bili.detect_system_proxy()
        if detected:
            self.assertEqual(probe.call_args_list[1].kwargs.get("proxy"), detected)
        else:
            self.assertNotIn("proxy", probe.call_args_list[1].kwargs)

    def test_network_error_also_retries_once(self):
        with patch.object(
            gui.bili,
            "fetch_quality_tiers",
            side_effect=[OSError("URLError: connection reset"), TIERS_OTHER],
        ) as probe:
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(probe.call_count, 2)

    def test_api_error_is_not_retried(self):
        with patch.object(gui.bili, "fetch_quality_tiers", return_value=None) as probe:
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(payload["kind"], "unknown")

        with patch.object(
            gui.bili, "fetch_quality_tiers", side_effect=ValueError("bad json")
        ) as probe2:
            payload2 = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertFalse(payload2["ok"])
        self.assertEqual(payload2["attempts"], 1)
        self.assertEqual(probe2.call_count, 1)
        self.assertEqual(payload2["kind"], "api")

    def test_retry_reports_previous_reason(self):
        seen: list[tuple[int, int, str, str]] = []
        with patch.object(
            gui.bili, "fetch_quality_tiers", side_effect=[TimeoutError("timed out"), TIERS_OTHER]
        ), patch.object(gui.bili, "detect_system_proxy", return_value="http://127.0.0.1:7890"):
            gui.read_bilibili_qualities(
                "BV1",
                retry_delay=0.0,
                on_attempt=lambda i, n, prev, route="": seen.append((i, n, prev, route)),
            )
        self.assertEqual(seen[0][:3], (1, 2, ""))
        self.assertIn("直连", seen[0][3])
        self.assertEqual(seen[1][0], 2)
        self.assertEqual(seen[1][1], 2)
        self.assertIn("超时 45 秒", seen[1][2])
        self.assertIn("系统代理 http://127.0.0.1:7890", seen[1][3])

    def test_success_path_never_passes_proxy(self):
        with patch.object(gui.bili, "fetch_quality_tiers", return_value=TIERS_OTHER) as probe, patch.object(
            gui.bili, "detect_system_proxy", return_value="http://127.0.0.1:7890"
        ):
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertTrue(payload["ok"])
        self.assertEqual(probe.call_count, 1)
        self.assertNotIn("proxy", probe.call_args.kwargs)

    def test_retry_stays_direct_when_no_proxy_detected(self):
        seen: list[tuple[int, int, str, str]] = []
        with patch.object(
            gui.bili, "fetch_quality_tiers", side_effect=[TimeoutError("timed out"), TIERS_OTHER]
        ) as probe, patch.object(gui.bili, "detect_system_proxy", return_value=None):
            gui.read_bilibili_qualities(
                "BV1",
                retry_delay=0.0,
                on_attempt=lambda i, n, prev, route="": seen.append((i, n, prev, route)),
            )
        self.assertNotIn("proxy", probe.call_args_list[1].kwargs)
        self.assertIn("系统未检测到代理", seen[1][3])

    def test_read_quality_tiers_use_proxy_flag_only_adds_proxy(self):
        module = gui.bili
        with patch.object(module, "fetch_quality_tiers", return_value=TIERS_OTHER) as probe, patch.object(
            module, "detect_system_proxy", return_value="http://127.0.0.1:7890"
        ):
            gui.read_quality_tiers("BV1", use_proxy=False)
            self.assertNotIn("proxy", probe.call_args.kwargs)
            gui.read_quality_tiers("BV1", use_proxy=True)
            self.assertEqual(probe.call_args.kwargs["proxy"], "http://127.0.0.1:7890")

    def test_timeout_reason_reaches_payload(self):
        with patch.object(gui.bili, "fetch_quality_tiers", side_effect=TimeoutError("timed out")):
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "timeout")
        self.assertIn("超时 45 秒", payload["error"])
        self.assertEqual(payload["attempts"], 2)
        self.assertTrue(payload["retried"])
        self.assertIn("已重试 1 次", payload["detail"])


    def test_slow_timeout_does_not_retry(self):
        """慢慢磨到接近 timeout 才失败 -> 不重试（否则用户再等一个 45 秒）。"""
        calls: list[str] = []

        def slow_fail(url, **kwargs):
            calls.append(url)
            time.sleep(0.2)
            raise TimeoutError("timed out")

        with patch.object(gui.bili, "fetch_quality_tiers", side_effect=slow_fail):
            payload = gui.read_bilibili_qualities(
                "BV1", retry_delay=0.0, fast_failure_seconds=0.05
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(len(calls), 1, "慢超时不应重试")
        self.assertFalse(payload["retried"])
        self.assertIn("链路过慢故未重试", payload["detail"])
        self.assertIn("本次读取耗时", payload["detail"])

    def test_fast_failure_retries_once(self):
        """<5 秒就失败（连接重置类）-> 重试一次。"""
        calls: list[str] = []

        def fast_fail(url, **kwargs):
            calls.append(url)
            raise TimeoutError("timed out")

        with patch.object(gui.bili, "fetch_quality_tiers", side_effect=fast_fail):
            payload = gui.read_bilibili_qualities(
                "BV1", retry_delay=0.0, fast_failure_seconds=5.0
            )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(len(calls), 2, "快失败应重试一次")
        self.assertTrue(payload["retried"])
        self.assertIn("已重试 1 次", payload["detail"])
        self.assertIn("上次", payload["detail"])

    def test_api_error_detail_says_not_retried(self):
        with patch.object(gui.bili, "fetch_quality_tiers", return_value=None):
            payload = gui.read_bilibili_qualities("BV1", retry_delay=0.0)
        self.assertEqual(payload["attempts"], 1)
        self.assertFalse(payload["retried"])
        self.assertEqual(payload["detail"], "接口报错，未重试")

class DouyinQualityLabelTests(unittest.TestCase):
    def test_label_includes_bitrate_and_codec(self):
        self.assertEqual(
            gui.douyin_quality_label({"label": "720P", "bitrate": 824, "codec": "h265"}),
            "720P · 824kbps · h265",
        )

    def test_label_degrades_gracefully(self):
        self.assertEqual(gui.douyin_quality_label({"label": "1080P"}), "1080P")
        self.assertEqual(gui.douyin_quality_label({"gear": "x1"}), "x1")
        self.assertEqual(gui.douyin_quality_label({}), "未知档位")
        self.assertEqual(gui.douyin_quality_label({"label": "720P", "bitrate": "x"}), "720P")

    def test_log_lines_include_recommend_mark(self):
        lines = gui.douyin_quality_log_lines(DOUYIN_PAYLOAD_OK, elapsed=4.2)
        text = chr(10).join(lines)
        self.assertIn("已读取抖音清晰度（耗时 4.2 秒）", text)
        self.assertIn("演示视频", text)
        self.assertIn("某作者", text)
        self.assertIn("gear=720_2_1", text)
        self.assertIn("（推荐）", text)


class QualityAutoReadGuiTests(unittest.TestCase):
    """控件级行为：防抖、过期丢弃、缓存、强制刷新、抖音两分支、argv 片段。"""

    def setUp(self):
        try:
            import tkinter as tk
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"没有 tkinter：{exc}")
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover
            self.skipTest(f"没有可用显示：{exc}")
        self.root.withdraw()
        self._tmp = tempfile.TemporaryDirectory()
        self.config_file = Path(self._tmp.name) / "gui.json"
        self.app = gui.DownloadApp(self.root, config_path_override=self.config_file)
        self.app.quality_debounce_ms = 40
        self.logs: list[str] = []
        original = self.app.log.append

        def record(line, tag=None):
            self.logs.append(line)
            original(line, tag)

        self.app.log.append = record

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            self.app.close()
        if getattr(self, "root", None) is not None:
            try:
                self.root.destroy()
            except Exception:  # noqa: BLE001
                pass
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _pump(self, predicate=None, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.root.update()
            except Exception:  # noqa: BLE001
                return bool(predicate()) if predicate else True
            if predicate is not None and predicate():
                return True
            time.sleep(0.01)
        return bool(predicate()) if predicate else True

    def _set_links(self, tab, text: str) -> None:
        tab.links_text.delete("1.0", "end")
        tab.links_text.insert("1.0", text)
        self.root.update()

    # -- B 站：防抖 / 缓存 / 过期丢弃 / 强制刷新 ----------------------------

    def test_pending_status_text_appears_before_request(self):
        bili = self.app.tabs["bilibili"]
        dou = self.app.tabs["douyin"]
        self.app.quality_debounce_ms = 5000  # 不让它真的发请求
        self._set_links(bili, "https://www.bilibili.com/video/BV1abc" + chr(10))
        self._set_links(dou, "https://www.douyin.com/video/1" + chr(10))
        self.assertIn("等待输入停顿", bili.status_label["text"])
        self.assertIn("会启动无头浏览器", dou.status_label["text"])
        self.assertIn("3–10 秒", dou.status_label["text"])

    def test_debounce_sends_only_last_request(self):
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        with patch.object(gui, "read_quality_tiers", fake_reader):
            for url in ("https://b/1", "https://b/2", "https://b/3"):
                self._set_links(tab, url + chr(10))
                self.root.update()
                time.sleep(0.01)
            self.assertEqual(calls, [], "连续输入期间不应发请求")
            self.assertTrue(self._pump(lambda: bool(calls), 2.0))
            self._pump(lambda: tab.quality_read_done, 2.0)
        self.assertEqual(calls, ["https://b/3"], "停滞后只读最后一个链接")

    def test_cache_prevents_duplicate_request(self):
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self.app.quality_debounce_ms = 5000  # 挡掉 <<Modified>> 自动触发，只测手动两次
        self._set_links(tab, "https://b/cache" + chr(10))
        self.app._cancel_quality_debounce("bilibili")
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: len(calls) == 1, 2.0))
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0), "第一个结果应已落地并写进缓存")
            self.app.start_quality_read("bilibili")
            self._pump(timeout=0.3)
        self.assertEqual(len(calls), 1, "同一 URL 命中缓存不应重复请求")

    def test_refresh_button_forces_reread(self):
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://b/refresh" + chr(10))
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: len(calls) == 1, 2.0))
            # 等第一次结果落地（在途请求结束）后再点刷新，才应该真的再读一次
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0))
            tab._refresh_qualities()
            self.assertTrue(self._pump(lambda: len(calls) == 2, 2.0))
        self.assertEqual(calls, ["https://b/refresh", "https://b/refresh"])

    def test_stale_result_is_discarded(self):
        gate = threading.Event()
        first_started: list[str] = []

        def fake_reader(link, timeout=0.0):
            if link.endswith("/1"):
                first_started.append(link)
                gate.wait(5.0)
                return TIERS_HDR_LOCKED
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://b/1" + chr(10))
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: bool(first_started), 2.0), "第一个请求应已进入线程")
            self._set_links(tab, "https://b/2" + chr(10))
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0), "第二个结果应先落地")
            labels_after_second = list(tab.quality_box["values"])
            self.assertEqual(labels_after_second, [tier["label"] for tier in TIERS_OTHER])
            gate.set()
            self._pump(timeout=0.5)
            self.assertEqual(
                list(tab.quality_box["values"]),
                labels_after_second,
                "过期结果不得覆盖新结果",
            )
            self.assertEqual(tab.quality_var.get(), TIERS_OTHER[0]["label"])
        self.assertTrue(any("丢弃过期结果" in line for line in self.logs), self.logs[-6:])

    def test_failed_bilibili_read_falls_back_with_log(self):
        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://b/fail" + chr(10))
        with patch.object(gui, "read_quality_tiers", lambda link, timeout=0.0: None):
            self.app.start_quality_read("bilibili")
            self.assertTrue(
                self._pump(lambda: any("回退到固定档位" in line for line in self.logs), 2.0)
            )
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))
        self.assertEqual(tab.quality(), "1080")
        self.assertIn("读取失败", tab.status_label["text"])

    def test_stop_button_cancels_pending_quality_read(self):
        calls: list[str] = []
        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://b/stop" + chr(10))
        self.app.quality_debounce_ms = 5000
        tab.status_label.configure(text="正在读取清晰度…")
        with patch.object(gui, "read_quality_tiers", lambda link, timeout=0.0: calls.append(link)):
            seq_before = self.app._quality_seq["bilibili"]
            self.app.stop_download()
            self.assertIsNone(self.app._quality_debounce["bilibili"])
            self.assertGreater(self.app._quality_seq["bilibili"], seq_before)
            self.assertEqual(tab.status_label["text"], "已取消清晰度读取")
            self._pump(timeout=0.3)
        self.assertEqual(calls, [], "取消后不应再发请求")

    # -- B 站失败原因 / 重试（真窗口） --------------------------------------

    def test_widget_retry_success_after_timeout(self):
        tab = self.app.tabs["bilibili"]
        self.app.quality_retry_delay = 0.0
        self._set_links(tab, "https://www.bilibili.com/video/BV1abc" + chr(10))
        tab.output_var.set(self._tmp.name)
        with patch.object(
            gui.bili,
            "fetch_quality_tiers",
            side_effect=[TimeoutError("timed out"), TIERS_HDR_LOCKED],
        ) as probe:
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 3.0))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(list(tab.quality_box["values"]), ["真彩 HDR", "超清 4K", "高清 1080P"])
        self.assertEqual(tab.quality_var.get(), "超清 4K")
        argv = tab.build_argv()
        self.assertEqual(argv[argv.index("--qn") + 1], "120")
        text = chr(10).join(self.logs)
        self.assertIn("第 2/2 次尝试", text)
        self.assertIn("超时 45 秒", text)
        self.assertIn("本次路径", text)

    def test_widget_failure_log_shows_reason_and_attempts(self):
        tab = self.app.tabs["bilibili"]
        self.app.quality_retry_delay = 0.0
        self._set_links(tab, "https://www.bilibili.com/video/BV1fail" + chr(10))
        with patch.object(
            gui.bili, "fetch_quality_tiers", side_effect=TimeoutError("timed out")
        ) as probe:
            self.assertTrue(
                self._pump(lambda: any("B 站清晰度读取失败" in line for line in self.logs), 3.0)
            )
        self.assertEqual(probe.call_count, 2)
        failure = [line for line in self.logs if "B 站清晰度读取失败" in line][-1]
        self.assertIn("已回退固定档位（超时 45 秒）", failure)
        self.assertIn("已重试 1 次", failure)
        self.assertIn("可点「刷新清晰度」重试", failure)
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))
        self.assertIn("读取失败（超时 45 秒）", tab.status_label["text"])

    def test_paste_once_makes_single_request_and_no_stale_log(self):
        """真窗口 + 真 <<Modified>> 路径：粘贴一次只打一次接口，且没有过期日志。"""
        gate = threading.Event()
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            gate.wait(5.0)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self.app.quality_debounce_ms = 40
        with patch.object(gui, "read_quality_tiers", fake_reader):
            tab.links_text.insert("1.0", "https://www.bilibili.com/video/BV1once" + chr(10))
            self.root.update()
            self.assertTrue(self._pump(lambda: len(calls) == 1, 2.0), "粘贴后应只发一次请求")
            # 再触发几次同样的读取（切页/刷新/重复 mtime 都算），都该被合并
            self.app.start_quality_read("bilibili")
            tab._refresh_qualities()
            self._pump(timeout=0.3)
            gate.set()
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 3.0))
        self.assertEqual(len(calls), 1, "同一个链接只允许一次接口调用")
        text = chr(10).join(self.logs)
        self.assertNotIn("丢弃过期结果", text)
        self.assertEqual(list(tab.quality_box["values"]), [t["label"] for t in TIERS_OTHER])

    # -- 同一 URL 在途去重 --------------------------------------------------

    def test_same_url_second_trigger_merges_inflight(self):
        gate = threading.Event()
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            gate.wait(5.0)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self.app.quality_debounce_ms = 5000
        self._set_links(tab, "https://b/same" + chr(10))
        self.app._cancel_quality_debounce("bilibili")
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: len(calls) == 1, 2.0), "第一次请求应已进入线程")
            self.app.start_quality_read("bilibili")
            self.app.start_quality_read("bilibili", force=True)
            self._pump(timeout=0.3)
            self.assertEqual(len(calls), 1, "同一 URL 在途时必须合并，只打一次接口")
            self.assertIn("刷新请求已合并", chr(10).join(self.logs))
            gate.set()
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 3.0))
        self.assertEqual(list(tab.quality_box["values"]), [t["label"] for t in TIERS_OTHER])
        self.assertEqual(tab.quality_var.get(), TIERS_OTHER[0]["label"])
        self.assertNotIn("丢弃过期结果", chr(10).join(self.logs), "同 URL 合并时不应出现过期日志")
        self.assertEqual(self.app._quality_in_flight, {})

    def test_different_urls_still_two_calls_and_results_switch(self):
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            return TIERS_OTHER if link.endswith("/2") else TIERS_HDR_LOCKED

        tab = self.app.tabs["bilibili"]
        self.app.quality_debounce_ms = 5000
        self._set_links(tab, "https://b/1" + chr(10))
        self.app._cancel_quality_debounce("bilibili")
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0))
            self.assertEqual(list(tab.quality_box["values"]), [t["label"] for t in TIERS_HDR_LOCKED])
            self._set_links(tab, "https://b/2" + chr(10))
            self.app.start_quality_read("bilibili")
            self.assertTrue(
                self._pump(lambda: tab.quality_var.get() == TIERS_OTHER[0]["label"], 2.0)
            )
        self.assertEqual(len(calls), 2, "不同 URL 必须各读一次")
        self.assertEqual(list(tab.quality_box["values"]), [t["label"] for t in TIERS_OTHER])

    def test_refresh_while_inflight_merges_not_new_request(self):
        gate = threading.Event()
        calls: list[str] = []

        def fake_reader(link, timeout=0.0):
            calls.append(link)
            gate.wait(5.0)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self.app.quality_debounce_ms = 5000
        self._set_links(tab, "https://b/refresh2" + chr(10))
        self.app._cancel_quality_debounce("bilibili")
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: len(calls) == 1, 2.0))
            tab._refresh_qualities()
            self._pump(timeout=0.3)
            self.assertEqual(len(calls), 1, "刷新按钮不应在读取进行中开出第二个请求")
            self.assertIn("刷新请求已合并", chr(10).join(self.logs))
            self.assertIn("沿用已在进行的请求", tab.status_label["text"])
            gate.set()
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 3.0))
        self.assertEqual(tab.quality_var.get(), TIERS_OTHER[0]["label"])

    def test_widget_slow_timeout_falls_back_without_retry(self):
        """真窗口：慢超时 -> 直接回退固定档位，且状态行/日志写清"为什么没重试"。"""
        tab = self.app.tabs["bilibili"]
        self.app.quality_fast_failure_seconds = 0.05
        calls: list[str] = []

        def slow_fail(link, **kwargs):
            calls.append(link)
            time.sleep(0.2)
            raise TimeoutError("timed out")

        self._set_links(tab, "https://www.bilibili.com/video/BV1slow" + chr(10))
        with patch.object(gui, "read_quality_tiers", slow_fail):
            self.assertTrue(
                self._pump(
                    lambda: any("B 站清晰度读取失败" in line for line in self.logs), 3.0
                ),
                "慢超时应在约 0.2 秒内回退（不重试）",
            )
        self.assertEqual(len(calls), 1, "慢超时不得重试")
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))
        failure = [line for line in self.logs if "B 站清晰度读取失败" in line][-1]
        self.assertIn("已回退固定档位（超时 45 秒）", failure)
        self.assertIn("链路过慢故未重试", failure)
        self.assertIn("可点「刷新清晰度」重试", failure)
        status = tab.status_label["text"]
        self.assertIn("链路过慢故未重试", status)
        self.assertIn("可点「刷新清晰度」重试", status)

    # -- E：控件级（读取成功后的下拉取值 + argv） --------------------------

    def test_bilibili_widget_level_selection_and_argv(self):
        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://www.bilibili.com/video/BV1abc" + chr(10))
        tab.output_var.set(self._tmp.name)
        with patch.object(gui, "read_quality_tiers", lambda link, timeout=0.0: TIERS_HDR_LOCKED):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0))
        self.assertEqual(list(tab.quality_box["values"]), ["真彩 HDR", "超清 4K", "高清 1080P"])
        self.assertEqual(tab.quality_var.get(), "超清 4K")
        self.assertEqual(tab.quality(), "120")
        argv = tab.build_argv()
        self.assertEqual(argv[argv.index("--qn") + 1], "120")
        self.assertNotIn("--quality", argv)
        self.assertNotIn("超清 4K", argv)

    def test_douyin_widget_level_gears_and_argv(self):
        tab = self.app.tabs["douyin"]
        gear_module = types.SimpleNamespace(build_parser=_parser_with_gear)
        with patch.dict(gui.PLATFORM_MODULES, {"douyin": gear_module}):
            self.assertTrue(
                tab.apply_douyin_qualities(
                    DOUYIN_PAYLOAD_OK, url="https://www.douyin.com/v/1", elapsed=4.2
                )
            )
            self.assertEqual(
                list(tab.quality_box["values"]),
                ["720P · 824kbps · h265", "720P · 1200kbps · h264"],
            )
            self.assertEqual(tab.quality_var.get(), "720P · 824kbps · h265")
            self.assertEqual(tab.selected_gear(), "720_2_1")
            self._set_links(tab, "https://www.douyin.com/video/1" + chr(10))
            tab.output_var.set(self._tmp.name)
            argv = tab.build_argv()
            self.assertEqual(argv[argv.index("--gear") + 1], "720_2_1")
            self.assertNotIn("--format", argv)
            tab.quality_var.set("720P · 1200kbps · h264")
            argv2 = tab.build_argv()
            self.assertEqual(argv2[argv2.index("--gear") + 1], "720_1_1")
        tab.quality_var.set("720P · 824kbps · h265")
        argv3 = tab.build_argv()
        if gui.douyin_gear_supported():
            # 真模块已经支持 --gear：选中档位应当直接进 argv
            self.assertEqual(argv3[argv3.index("--gear") + 1], "720_2_1")
            self.assertNotIn("--format", argv3)
        else:
            self.assertNotIn("--gear", argv3)
            self.assertEqual(argv3[argv3.index("--format") + 1], "best")
        self.assertEqual(tab.quality(), "best")

    def test_douyin_unavailable_branch_uses_fixed_tiers_and_logs(self):
        tab = self.app.tabs["douyin"]
        payload = gui.read_douyin_qualities("u", module=types.SimpleNamespace())
        ok = tab.apply_douyin_qualities(payload, url="https://v.douyin.com/x/")
        self.assertFalse(ok)
        self.assertEqual(list(tab.quality_box["values"]), list(gui.DOUYIN_QUALITIES))
        self.assertEqual(tab.quality(), "best")
        text = chr(10).join(self.logs)
        self.assertIn("抖音档位读取暂不可用", text)
        self.assertIn("已回退固定档位（best/1080/720/480）", text)

    def test_douyin_failed_read_branch_uses_fixed_tiers_and_reason(self):
        tab = self.app.tabs["douyin"]
        payload = {"ok": False, "error": "风控拦截（HTTP 403）", "qualities": [], "recommended": ""}
        ok = tab.apply_douyin_qualities(payload, url="https://v.douyin.com/x/")
        self.assertFalse(ok)
        self.assertEqual(list(tab.quality_box["values"]), list(gui.DOUYIN_QUALITIES))
        text = chr(10).join(self.logs)
        self.assertIn("原因：风控拦截（HTTP 403）", text)
        self.assertIn("读取失败", tab.status_label["text"])

    def test_douyin_payload_without_qualities_falls_back(self):
        tab = self.app.tabs["douyin"]
        ok = tab.apply_douyin_qualities({"ok": True, "qualities": [], "recommended": ""})
        self.assertFalse(ok)
        self.assertEqual(list(tab.quality_box["values"]), list(gui.DOUYIN_QUALITIES))
        self.assertIn("原因：没有返回可用档位", chr(10).join(self.logs))

    def test_douyin_worker_path_applies_gears(self):
        tab = self.app.tabs["douyin"]
        self._set_links(tab, "https://www.douyin.com/video/7" + chr(10))
        with patch.object(
            gui, "read_douyin_qualities", lambda url, timeout=0.0: dict(DOUYIN_PAYLOAD_OK)
        ):
            self.app.start_quality_read("douyin")
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 2.0))
        self.assertEqual(tab.selected_gear(), "720_2_1")
        self.assertIn("已读取抖音档位", tab.status_label["text"])
        self.assertIn("耗时", tab.status_label["text"])
        self.assertTrue(any("已读取抖音清晰度" in line for line in self.logs), self.logs[-5:])

    def test_douyin_loading_status_mentions_headless_browser(self):
        tab = self.app.tabs["douyin"]
        self._set_links(tab, "https://www.douyin.com/video/9" + chr(10))
        release = threading.Event()

        def slow(url, timeout=0.0):
            release.wait(3.0)
            return dict(DOUYIN_PAYLOAD_OK)

        with patch.object(gui, "read_douyin_qualities", slow):
            self.app.start_quality_read("douyin")
            self.assertIn("正在启动无头浏览器读取清晰度", tab.status_label["text"])
            self.assertIn("3–10 秒", tab.status_label["text"])
            release.set()
            self.assertTrue(self._pump(lambda: tab.quality_read_done, 3.0))

    def test_cancel_quality_reads_discards_inflight(self):
        gate = threading.Event()
        started: list[str] = []

        def fake_reader(link, timeout=0.0):
            started.append(link)
            gate.wait(5.0)
            return TIERS_OTHER

        tab = self.app.tabs["bilibili"]
        self._set_links(tab, "https://b/cancel" + chr(10))
        with patch.object(gui, "read_quality_tiers", fake_reader):
            self.app.start_quality_read("bilibili")
            self.assertTrue(self._pump(lambda: bool(started), 2.0))
            self.app.cancel_quality_reads()
            gate.set()
            self._pump(timeout=0.6)
        self.assertFalse(tab.quality_read_done)
        self.assertEqual(list(tab.quality_box["values"]), list(gui.BILIBILI_QUALITY_CHOICES))


if __name__ == "__main__":
    unittest.main()
