"""ffmpeg 自动安装的下载 / 解压逻辑。"""

from __future__ import annotations

import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ffmpeg_install  # noqa: E402


class FfmpegInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = ROOT / ".tmp" / "ffmpeg-install-tests"
        self.workdir.mkdir(parents=True, exist_ok=True)
        for item in self.workdir.iterdir():
            try:
                item.unlink()
            except OSError:
                pass
        self.target = self.workdir / "tools"

    def make_archive(self, name: str = "ffmpeg.zip", *, with_binary: bool = True) -> Path:
        archive = self.workdir / name
        with zipfile.ZipFile(archive, "w") as bundle:
            if with_binary:
                bundle.writestr("ffmpeg-6.0-essentials_build/bin/ffmpeg.exe", b"MZ-native")
                bundle.writestr("ffmpeg-6.0-essentials_build/bin/ffprobe.exe", b"MZ-probe")
            bundle.writestr("ffmpeg-6.0-essentials_build/README.txt", b"ignore me")
        return archive

    def test_extract_writes_only_binaries(self) -> None:
        archive = self.make_archive()
        written = ffmpeg_install.extract_binaries(archive, self.target)
        names = sorted(path.name for path in written)
        self.assertEqual(names, ["ffmpeg.exe", "ffprobe.exe"])
        self.assertEqual((self.target / "ffmpeg.exe").read_bytes(), b"MZ-native")
        self.assertFalse((self.target / "README.txt").exists())

    def test_extract_reports_progress(self) -> None:
        archive = self.make_archive()
        messages: list[str] = []
        ffmpeg_install.extract_binaries(archive, self.target, progress=messages.append)
        self.assertTrue(any("ffmpeg.exe" in message for message in messages))

    def test_extract_rejects_archive_without_ffmpeg(self) -> None:
        archive = self.make_archive(with_binary=False)
        with self.assertRaises(ffmpeg_install.FfmpegInstallError):
            ffmpeg_install.extract_binaries(archive, self.target)

    def test_extract_rejects_corrupt_archive(self) -> None:
        broken = self.workdir / "broken.zip"
        broken.write_bytes(b"not a zip at all")
        with self.assertRaises(ffmpeg_install.FfmpegInstallError):
            ffmpeg_install.extract_binaries(broken, self.target)

    def test_install_from_local_archive_url(self) -> None:
        archive = self.make_archive()
        messages: list[str] = []
        installed = ffmpeg_install.install_ffmpeg(
            self.target,
            url=archive.as_uri(),
            progress=messages.append,
        )
        self.assertEqual(installed.name, "ffmpeg.exe")
        self.assertEqual(installed.read_bytes(), b"MZ-native")
        self.assertFalse((self.target / ".ffmpeg-download.zip").exists())
        self.assertTrue(any("完成" in message for message in messages))

    def test_install_keeps_archive_when_asked(self) -> None:
        archive = self.make_archive()
        ffmpeg_install.install_ffmpeg(
            self.target, url=archive.as_uri(), keep_archive=True
        )
        self.assertTrue((self.target / ".ffmpeg-download.zip").is_file())

    def test_install_reports_missing_source(self) -> None:
        with self.assertRaises(ffmpeg_install.FfmpegInstallError):
            ffmpeg_install.install_ffmpeg(
                self.target, url=(self.workdir / "nope.zip").as_uri()
            )


if __name__ == "__main__":
    unittest.main()
