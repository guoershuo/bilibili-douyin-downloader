"""下载并解压 ffmpeg 静态构建到项目的 tools 目录。

B 站合并音视频需要 ffmpeg，但让用户自己去 winget 或官网装一份太折腾，
这里直接抓 gyan.dev 的官方 Windows 构建，只取 ffmpeg.exe / ffprobe.exe。
"""

from __future__ import annotations

import os
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
WANTED_BINARIES = ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe")
CHUNK = 1024 * 256
PROGRESS_STEP = 8 * 1024 * 1024

Progress = Callable[[str], None]


class FfmpegInstallError(RuntimeError):
    """下载或解压 ffmpeg 失败。"""


def _notify(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


def download_archive(
    url: str,
    destination: Path,
    *,
    progress: Progress | None = None,
    timeout: float = 60.0,
) -> Path:
    """把 ffmpeg 压缩包下载到 destination，按 8MB 汇报一次进度。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "autodownloads/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            next_mark = PROGRESS_STEP
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if done >= next_mark:
                        next_mark += PROGRESS_STEP
                        if total:
                            percent = done * 100 / total
                            _notify(
                                progress,
                                f"[下载] {done / 1048576:.0f} / {total / 1048576:.0f} MB"
                                f"（{percent:.0f}%）",
                            )
                        else:
                            _notify(progress, f"[下载] 已下载 {done / 1048576:.0f} MB")
    except (OSError, ValueError) as exc:
        raise FfmpegInstallError(f"下载 ffmpeg 失败：{exc}") from exc
    if destination.stat().st_size == 0:
        raise FfmpegInstallError("下载到的 ffmpeg 压缩包是空的")
    _notify(progress, f"[下载] 完成，共 {destination.stat().st_size / 1048576:.0f} MB")
    return destination


def extract_binaries(
    archive: Path,
    target_dir: Path,
    *,
    progress: Progress | None = None,
) -> list[Path]:
    """从压缩包里取出 ffmpeg / ffprobe，写入 target_dir。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = {
                Path(info.filename).name.lower(): info
                for info in bundle.infolist()
                if not info.is_dir() and Path(info.filename).name.lower() in WANTED_BINARIES
            }
            if not members:
                raise FfmpegInstallError("压缩包里没有找到 ffmpeg 可执行文件")
            for name, info in members.items():
                destination = target_dir / info.filename.split("/")[-1]
                partial = destination.with_suffix(destination.suffix + ".part")
                with bundle.open(info) as source, open(partial, "wb") as sink:
                    shutil.copyfileobj(source, sink, CHUNK * 4)
                os.replace(partial, destination)
                written.append(destination)
                _notify(progress, f"[解压] {destination.name}")
    except zipfile.BadZipFile as exc:
        raise FfmpegInstallError(f"ffmpeg 压缩包损坏：{exc}") from exc
    except OSError as exc:
        raise FfmpegInstallError(f"解压 ffmpeg 失败：{exc}") from exc
    return written


def install_ffmpeg(
    target_dir: Path | str,
    *,
    url: str = FFMPEG_DOWNLOAD_URL,
    progress: Progress | None = None,
    keep_archive: bool = False,
    timeout: float = 60.0,
) -> Path:
    """下载并安装 ffmpeg，返回可执行文件路径。"""
    target = Path(target_dir)
    archive = target / ".ffmpeg-download.zip"
    _notify(progress, f"[下载] 来源：{url}")
    download_archive(url, archive, progress=progress, timeout=timeout)
    try:
        written = extract_binaries(archive, target, progress=progress)
    finally:
        if not keep_archive:
            try:
                archive.unlink()
            except OSError:
                pass
    for path in written:
        if path.name.lower().startswith("ffmpeg") and "probe" not in path.name.lower():
            _notify(progress, f"[完成] ffmpeg 已就绪：{path}")
            return path
    raise FfmpegInstallError("解压后没有找到 ffmpeg 可执行文件")
