"""Unit tests for the local web workbench's pure request/argv logic."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402


class LinkParsingTests(unittest.TestCase):
    def test_split_links_trims_dedupes_and_keeps_share_text(self) -> None:
        text = "  https://b/1  \n\nhttps://b/2\nhttps://b/1\n复制打开抖音 abc https://v.douyin.com/x/  "
        self.assertEqual(
            web_app.split_links(text),
            ["https://b/1", "https://b/2", "复制打开抖音 abc https://v.douyin.com/x/"],
        )

    def test_split_links_empty(self) -> None:
        self.assertEqual(web_app.split_links(" \n\n "), [])


class ArgvTests(unittest.TestCase):
    def test_bilibili_argv(self) -> None:
        argv = web_app.build_bilibili_argv(
            ["BV1", "BV2"],
            Path("D:/out"),
            "1080",
            up="123",
            download_all=True,
            limit=5,
            folder_by_author=True,
            force=True,
            proxy="http://127.0.0.1:7890",
            limit_rate="2M",
        )
        self.assertEqual(
            argv,
            [
                "-o",
                "D:\\out",
                "--quality",
                "1080",
                "--up",
                "123",
                "--all",
                "--limit",
                "5",
                "--folder-by-author",
                "--force",
                "--proxy",
                "http://127.0.0.1:7890",
                "--limit-rate",
                "2M",
                "BV1",
                "BV2",
            ],
        )

    def test_douyin_argv_adds_home_and_options(self) -> None:
        argv = web_app.build_douyin_argv(
            ["https://v.douyin.com/x/"],
            Path("D:/out"),
            "1080",
            up="https://www.douyin.com/user/abc",
            download_all=True,
            limit=3,
            engine="browser",
            prefer_h264=True,
            info=True,
            thumbnail=True,
        )
        self.assertEqual(argv[:6], ["-o", "D:\\out", "--format", "best[height<=1080]", "--engine", "browser"])
        self.assertIn("--all", argv)
        self.assertIn("--prefer-h264", argv)
        self.assertIn("--info", argv)
        self.assertIn("--thumbnail", argv)
        self.assertEqual(argv[-2:], ["https://v.douyin.com/x/", "https://www.douyin.com/user/abc"])

    def test_douyin_does_not_duplicate_home(self) -> None:
        home = "https://www.douyin.com/user/abc"
        argv = web_app.build_douyin_argv([home], Path("D:/out"), "best", up=home)
        self.assertEqual(argv.count(home), 1)


class OutputDirectoryTests(unittest.TestCase):
    def test_existing_directory_is_accepted(self) -> None:
        target = ROOT / "downloads"
        self.assertEqual(web_app.create_output_dir(target), target.resolve())
        self.assertTrue(target.is_dir())


class JobProgressTests(unittest.TestCase):
    def test_yt_dlp_progress_and_entry(self) -> None:
        job = web_app.Job(id="x", platform="bilibili", kind="download", title="t")
        job.add_log("[1/3] https://example.test")
        job.add_log("[download]  42.3% of 10MiB at 2MiB/s")
        self.assertEqual(job.item_index, 1)
        self.assertEqual(job.item_total, 3)
        self.assertAlmostEqual(job.progress, 42.3, places=1)

    def test_douyin_carriage_return_progress(self) -> None:
        job = web_app.Job(id="x", platform="douyin", kind="download", title="t")
        job.add_log("下载中  71.5%  12.0/16.8 MB  3.2 MB/s")
        self.assertAlmostEqual(job.progress, 71.5, places=1)

    def test_completion_summary_keeps_error(self) -> None:
        job = web_app.Job(id="x", platform="bilibili", kind="download", title="t")
        job.add_log("[错误] 下载失败：示例")
        job.add_log("[完成] 成功 1 个，失败 1 个")
        self.assertIn("成功 1 个", job.summary)
        self.assertIn("下载失败", job.error)


class MissingDependencyTests(unittest.TestCase):
    def test_returns_empty_when_everything_imports(self) -> None:
        with mock.patch.object(web_app, "REQUIRED_MODULES", ("json", "sys")):
            self.assertEqual(web_app.missing_dependencies(), [])

    def test_reports_missing_module(self) -> None:
        with mock.patch.object(web_app, "REQUIRED_MODULES", ("json", "no_such_module_xyz")):
            self.assertEqual(web_app.missing_dependencies(), ["no_such_module_xyz"])

    def test_find_spec_failure_counts_as_missing(self) -> None:
        with mock.patch.object(web_app, "REQUIRED_MODULES", ("broken_module",)):
            with mock.patch("importlib.util.find_spec", side_effect=ImportError("boom")):
                self.assertEqual(web_app.missing_dependencies(), ["broken_module"])


class EnsureRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        # 单元测试不应把 mock 痕迹写进正式的 web.log。
        logger = mock.patch.object(web_app, "LOG", mock.MagicMock())
        logger.start()
        self.addCleanup(logger.stop)

    def test_noop_when_dependencies_present(self) -> None:
        with mock.patch.object(web_app, "missing_dependencies", return_value=[]):
            with mock.patch.object(web_app, "prepare_venv") as prepare:
                web_app.ensure_runtime()
        prepare.assert_not_called()

    def test_relaunch_uses_venv_interpreter_and_marks_environment(self) -> None:
        venv_py = web_app.VENV_DIR / "Scripts" / "python.exe"
        with mock.patch.object(web_app, "missing_dependencies", return_value=["yt_dlp"]):
            with mock.patch.object(web_app, "prepare_venv", return_value=venv_py):
                with mock.patch.dict("os.environ", {}, clear=True):
                    with mock.patch("os.execve") as execve:
                        web_app.ensure_runtime()
        execve.assert_called_once()
        argv, env = execve.call_args.args[1], execve.call_args.args[2]
        self.assertEqual(argv[0], str(venv_py))
        self.assertTrue(str(argv[1]).endswith("web_app.py"))
        self.assertEqual(env[web_app.BOOTSTRAP_ENV], "1")

    def test_exec_failure_falls_back_to_child_process(self) -> None:
        venv_py = web_app.VENV_DIR / "Scripts" / "python.exe"
        with mock.patch.object(web_app, "missing_dependencies", return_value=["yt_dlp"]):
            with mock.patch.object(web_app, "prepare_venv", return_value=venv_py):
                with mock.patch.dict("os.environ", {}, clear=True):
                    with mock.patch("os.execve", side_effect=OSError("nope")):
                        with mock.patch("subprocess.Popen") as popen:
                            with self.assertRaises(SystemExit):
                                web_app.ensure_runtime()
        popen.assert_called_once()

    def test_second_pass_still_missing_raises_actionable_error(self) -> None:
        with mock.patch.object(web_app, "missing_dependencies", return_value=["websocket"]):
            with mock.patch.dict("os.environ", {web_app.BOOTSTRAP_ENV: "1"}):
                with self.assertRaises(RuntimeError) as ctx:
                    web_app.ensure_runtime()
        self.assertIn("websocket", str(ctx.exception))
        self.assertIn("setup.bat", str(ctx.exception))

    def test_bootstrap_failure_is_explained(self) -> None:
        venv_py = web_app.VENV_DIR / "Scripts" / "python.exe"
        with mock.patch.object(web_app, "missing_dependencies", return_value=["yt_dlp"]):
            with mock.patch.object(
                web_app, "prepare_venv", side_effect=RuntimeError(f"pip 失败：{venv_py}")
            ):
                with mock.patch.dict("os.environ", {}, clear=True):
                    with self.assertRaises(RuntimeError) as ctx:
                        web_app.ensure_runtime()
        self.assertIn("setup.bat", str(ctx.exception))


class SetupLogTests(unittest.TestCase):
    def test_output_is_appended_and_failure_raised(self) -> None:
        workdir = ROOT / ".tmp" / "setup-log-tests"
        workdir.mkdir(parents=True, exist_ok=True)
        log_path = workdir / "setup.log"
        log_path.unlink(missing_ok=True)
        try:
            with mock.patch.object(web_app, "SETUP_LOG", log_path):
                web_app._run_into_log(
                    [sys.executable, "-c", "print('hello from setup')"],
                    label="测试步骤",
                )
                body = log_path.read_text(encoding="utf-8")
                self.assertIn("测试步骤", body)
                self.assertIn("hello from setup", body)
                with self.assertRaises(RuntimeError) as ctx:
                    web_app._run_into_log(
                        [sys.executable, "-c", "raise SystemExit(3)"],
                        label="失败步骤",
                    )
                self.assertIn("setup.log", str(ctx.exception))
        finally:
            log_path.unlink(missing_ok=True)


class PortFileTests(unittest.TestCase):
    def setUp(self) -> None:
        workdir = ROOT / ".tmp" / "port-file-tests"
        workdir.mkdir(parents=True, exist_ok=True)
        self.port_file = workdir / "web.port"
        self.port_file.unlink(missing_ok=True)
        patcher = mock.patch.object(web_app, "PORT_FILE", self.port_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.port_file.unlink, missing_ok=True)

    def test_write_then_read_roundtrip(self) -> None:
        web_app.write_port_file(8765)
        self.assertEqual(web_app.recorded_port(), 8765)

    def test_recorded_port_is_none_without_file(self) -> None:
        self.assertIsNone(web_app.recorded_port())

    def test_clear_only_removes_matching_port(self) -> None:
        web_app.write_port_file(8765)
        web_app.clear_port_file(8766)
        self.assertEqual(web_app.recorded_port(), 8765)
        web_app.clear_port_file(8765)
        self.assertIsNone(web_app.recorded_port())

    def test_find_running_instance_prefers_recorded_port(self) -> None:
        web_app.write_port_file(8770)
        with mock.patch.object(
            web_app, "own_instance", side_effect=lambda port: port == 8770
        ) as probe:
            self.assertEqual(web_app.find_running_instance(8765), 8770)
        self.assertEqual(probe.call_count, 1)

    def test_find_running_instance_falls_back_to_base_port(self) -> None:
        web_app.write_port_file(8770)
        with mock.patch.object(
            web_app, "own_instance", side_effect=lambda port: port == 8765
        ):
            self.assertEqual(web_app.find_running_instance(8765), 8765)

    def test_find_running_instance_returns_none(self) -> None:
        with mock.patch.object(web_app, "own_instance", return_value=False):
            self.assertIsNone(web_app.find_running_instance(8765))


class InstanceIdentityTests(unittest.TestCase):
    """旧目录的实例、页面文件缺失的实例都不该被当成「已在运行」。"""

    def test_health_payload_carries_project(self) -> None:
        payload = web_app.health_payload()
        self.assertEqual(payload["app"], "autodownloads-web")
        self.assertEqual(payload["project"], str(web_app.PROJECT_DIR))

    def test_own_instance_accepts_same_project_with_token(self) -> None:
        info = {"app": "autodownloads-web", "project": str(web_app.PROJECT_DIR)}
        with (
            mock.patch.object(web_app, "instance_info", return_value=info),
            mock.patch.object(web_app, "instance_token", return_value="tok"),
        ):
            self.assertTrue(web_app.own_instance(8765))

    def test_own_instance_rejects_foreign_project(self) -> None:
        info = {"app": "autodownloads-web", "project": "E:\\other\\autoDownloads"}
        with (
            mock.patch.object(web_app, "instance_info", return_value=info),
            mock.patch.object(web_app, "instance_token", return_value="tok"),
        ):
            self.assertFalse(web_app.own_instance(8765))

    def test_own_instance_rejects_instance_without_page(self) -> None:
        # 目录被搬走后旧实例仍占着端口：健康检查通得过，但页面文件已丢失，
        # 拿不到令牌就停不掉它，只能绕过。
        info = {"app": "autodownloads-web", "project": str(web_app.PROJECT_DIR)}
        with (
            mock.patch.object(web_app, "instance_info", return_value=info),
            mock.patch.object(web_app, "instance_token", return_value=""),
        ):
            self.assertFalse(web_app.own_instance(8765))

    def test_existing_instance_still_sees_foreign_instance(self) -> None:
        info = {"app": "autodownloads-web", "project": "E:\\other\\autoDownloads"}
        with mock.patch.object(web_app, "instance_info", return_value=info):
            self.assertTrue(web_app.existing_instance(8765))

    def test_stop_reports_unusable_instance_and_keeps_it(self) -> None:
        info = {"app": "autodownloads-web", "project": "E:\\other\\autoDownloads"}
        out: list[str] = []
        with (
            mock.patch.object(web_app, "own_instance", return_value=False),
            mock.patch.object(web_app, "instance_info", return_value=info),
            mock.patch.object(web_app, "safe_print", side_effect=out.append),
        ):
            self.assertFalse(web_app.stop_stale_instance(8765))
        self.assertEqual(len(out), 1)
        self.assertIn("E:\\other\\autoDownloads", out[0])


class ServerPortTests(unittest.TestCase):
    def test_skips_port_used_by_another_instance(self) -> None:
        with mock.patch.object(
            web_app, "existing_instance", side_effect=lambda port: port == 8765
        ):
            self.assertEqual(web_app.find_server_port(8765), 8766)

    def test_uses_preferred_port_when_free(self) -> None:
        with mock.patch.object(web_app, "existing_instance", return_value=False):
            self.assertEqual(web_app.find_server_port(8765), 8765)


class QualityProbeTests(unittest.TestCase):
    def test_returns_levels_with_vip_flags(self) -> None:
        meta = {"title": "示例视频", "bvid": "BV1xx411c7mD"}
        tiers = [
            {"qn": 80, "label": "1080P", "height": 1080, "fps": 30, "need_vip": False, "available": True},
            {"qn": 120, "label": "4K", "height": 2160, "fps": 60, "need_vip": True, "available": False},
        ]
        with mock.patch.object(web_app.bili, "read_cookie_text", return_value=""):
            with mock.patch.object(
                web_app.bili, "inspect_quality_tiers", return_value=(meta, tiers)
            ):
                data = web_app.probe_bilibili_qualities("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(data["title"], "示例视频")
        self.assertEqual(len(data["levels"]), 2)
        self.assertTrue(data["levels"][0]["available"])
        self.assertFalse(data["levels"][1]["available"])
        self.assertTrue(data["levels"][1]["needVip"])

    def test_empty_link_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            web_app.probe_bilibili_qualities("   ")

    def test_unreadable_video_raises_value_error(self) -> None:
        with mock.patch.object(web_app.bili, "read_cookie_text", return_value=""):
            with mock.patch.object(web_app.bili, "inspect_quality_tiers", return_value=(None, None)):
                with self.assertRaises(ValueError):
                    web_app.probe_bilibili_qualities(
                        "https://www.bilibili.com/video/BV1xx411c7mD"
                    )


class OpenFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        # 避免测试里的 mock 文案写进正式 web.log。
        logger = mock.patch.object(web_app, "LOG", mock.MagicMock())
        logger.start()
        self.addCleanup(logger.stop)
        self.handler = web_app.Handler.__new__(web_app.Handler)
        self.target = ROOT / "downloads"
        self.target.mkdir(parents=True, exist_ok=True)

    def test_prefers_startfile(self) -> None:
        with mock.patch.object(web_app.os, "startfile", create=True) as startfile:
            with mock.patch.object(web_app.subprocess, "run") as run:
                web_app.Handler._open_folder(self.handler, self.target)
        startfile.assert_called_once()
        run.assert_not_called()

    def test_failure_is_reported_without_extra_dialogs(self) -> None:
        with mock.patch.object(
            web_app.os, "startfile", create=True, side_effect=PermissionError(13, "拒绝访问")
        ):
            with mock.patch.object(web_app.subprocess, "run") as run:
                with self.assertRaises(RuntimeError) as ctx:
                    web_app.Handler._open_folder(self.handler, self.target)
        self.assertIn("系统拒绝打开资源管理器", str(ctx.exception))
        run.assert_not_called()

    def test_missing_directory_falls_back_to_existing_parent(self) -> None:
        missing = self.target / "不存在子目录"
        with mock.patch.object(web_app.os, "startfile", create=True) as startfile:
            web_app.Handler._open_folder(self.handler, missing)
        opened = Path(startfile.call_args.args[0])
        self.assertTrue(opened.is_dir())


class MediaFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = ROOT / ".tmp" / "newest-media-tests"
        self.workdir.mkdir(parents=True, exist_ok=True)
        for item in self.workdir.glob("*"):
            if item.is_file():
                item.unlink()

    def test_finds_file_created_after_start(self) -> None:
        old = self.workdir / "old.mp4"
        old.write_bytes(b"x")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        new = self.workdir / "new.mp4"
        new.write_bytes(b"x")
        self.assertEqual(web_app.newest_media_file(self.workdir, time.time() - 60), new)

    def test_ignores_archives_and_old_files(self) -> None:
        (self.workdir / ".bilibili-archive-1080.txt").write_text("BV1\n", encoding="utf-8")
        old = self.workdir / "old.mp4"
        old.write_bytes(b"x")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        self.assertIsNone(web_app.newest_media_file(self.workdir, time.time()))

    def test_returns_none_for_missing_dir(self) -> None:
        self.assertIsNone(web_app.newest_media_file(self.workdir / "nope", 0))


class CodeStampTests(unittest.TestCase):
    def test_stamp_is_short_hex(self) -> None:
        stamp = web_app.code_stamp()
        self.assertEqual(len(stamp), 12)
        int(stamp, 16)


class DouyinQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        logger = mock.patch.object(web_app, "LOG", mock.MagicMock())
        logger.start()
        self.addCleanup(logger.stop)

    def test_levels_are_derived_from_yt_dlp_formats(self) -> None:
        info = {
            "title": "示例",
            "formats": [
                {"height": 1080, "vcodec": "h264"},
                {"height": 720, "vcodec": "h264"},
                {"height": 1080, "vcodec": "h264"},
                {"height": 0, "vcodec": "none"},
            ],
        }
        inner = mock.MagicMock()
        inner.extract_info.return_value = info
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = inner
        ydl.__exit__.return_value = False
        with mock.patch("yt_dlp.YoutubeDL", return_value=ydl):
            with mock.patch.object(web_app, "DOUYIN_PROFILE", ROOT / "no-such-profile"):
                data = web_app.probe_douyin_qualities("https://v.douyin.com/abc/")
        self.assertEqual([level["value"] for level in data["levels"]], ["1080", "720"])

    def test_probe_failure_raises_for_fallback(self) -> None:
        with mock.patch("yt_dlp.YoutubeDL", side_effect=RuntimeError("boom")):
            with mock.patch.object(web_app, "DOUYIN_PROFILE", ROOT / "no-such-profile"):
                with self.assertRaises(ValueError):
                    web_app.probe_douyin_qualities("https://v.douyin.com/abc/")

    def test_probe_dispatches_on_platform(self) -> None:
        with mock.patch.object(
            web_app, "probe_douyin_qualities", return_value={"levels": []}
        ) as douyin:
            web_app.probe_qualities("douyin", "https://v.douyin.com/x/")
        douyin.assert_called_once()


class ListFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = ROOT / ".tmp" / "list-folder-tests"
        if self.workdir.exists():
            for item in self.workdir.glob("*"):
                if item.is_file():
                    item.unlink()
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "a.mp4").write_bytes(b"x" * 2048)
        (self.workdir / "sub").mkdir(exist_ok=True)

    def test_lists_files_and_folders(self) -> None:
        data = web_app.list_folder(self.workdir)
        names = {entry["name"] for entry in data["entries"]}
        self.assertIn("a.mp4", names)
        self.assertIn("sub", names)
        video = next(e for e in data["entries"] if e["name"] == "a.mp4")
        self.assertFalse(video["dir"])
        self.assertEqual(video["size"], 2048)
        sub = next(e for e in data["entries"] if e["name"] == "sub")
        self.assertTrue(sub["dir"])

    def test_missing_directory_raises(self) -> None:
        with self.assertRaises(ValueError):
            web_app.list_folder(self.workdir / "nope")


if __name__ == "__main__":
    unittest.main()
