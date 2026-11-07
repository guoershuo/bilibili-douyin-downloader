#!/usr/bin/env python3
"""autoDownloads local web workbench.

A tiny, dependency-free local server that turns the existing Bilibili / Douyin
CLI downloaders into a browser UI.  Nothing leaves this computer: the server
binds to 127.0.0.1, jobs stream their stdout to the page, and files go straight
to the selected local folder.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib.util
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import bilibili_downloader as bili
import browser_engine
import douyin_downloader as douyin
import ffmpeg_install


APP_NAME = "autoDownloads Web"
APP_VERSION = "1.0.0"
PROJECT_DIR = Path(__file__).resolve().parent
WEB_DIR = PROJECT_DIR / "web"
BILI_SCRIPT = PROJECT_DIR / "bilibili_downloader.py"
DOUYIN_SCRIPT = PROJECT_DIR / "douyin_downloader.py"
DEFAULT_OUTPUT = PROJECT_DIR / "downloads"
BILI_COOKIE = PROJECT_DIR / bili.COOKIE_FILE_NAME
DOUYIN_PROFILE = browser_engine.default_profile_dir(PROJECT_DIR)
DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_LOGIN_COOKIES = ("sessionid|sessionid_ss",)
LOG_FILE = PROJECT_DIR / "web.log"
SETUP_LOG = PROJECT_DIR / "setup.log"
PORT_FILE = PROJECT_DIR / "web.port"
VENV_DIR = PROJECT_DIR / ".venv"
REQUIRED_MODULES = ("yt_dlp", "websocket")
BOOTSTRAP_ENV = "AUTODOWNLOADS_BOOTSTRAPPED"
MAX_BODY = 1_500_000
MAX_LOGS = 1800
DEFAULT_PORT = 8765
LOGIN_TIMEOUT = 300.0

BILIBILI_QUALITIES = [
    {"value": "2160", "label": "4K", "note": "大会员"},
    {"value": "1440", "label": "2K", "note": "大会员"},
    {"value": "1080", "label": "1080P", "note": "推荐"},
    {"value": "720", "label": "720P", "note": ""},
    {"value": "480", "label": "480P", "note": ""},
    {"value": "best", "label": "最佳", "note": "跟随账号"},
]
DOUYIN_QUALITIES = [
    {"value": "best", "label": "最佳", "note": "推荐"},
    {"value": "1080", "label": "1080P", "note": ""},
    {"value": "720", "label": "720P", "note": ""},
    {"value": "480", "label": "480P", "note": ""},
]
DOUYIN_FORMATS = {
    "best": "best",
    "1080": "best[height<=1080]",
    "720": "best[height<=720]",
    "480": "best[height<=480]",
}
PROGRESS_RE = re.compile(r"\[download\]\s*(\d{1,3}(?:\.\d+)?)\s*%")
DOUYIN_PROGRESS_RE = re.compile(r"下载中\s+(\d{1,3}(?:\.\d+)?)\s*%")
ENTRY_RE = re.compile(r"^=*\s*\[(\d+)/(\d+)\]")
SUMMARY_RE = re.compile(r"\[完成\]\s*成功\s*(\d+)\s*个[，,]\s*失败\s*(\d+)\s*个")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_MARKERS = ("[错误]", "下载失败", "ERROR:", "解析失败", "Traceback")
SUCCESS_MARKERS = ("[完成]", "登录成功", "登录态已保存")
SYSTEM_MARKERS = ("[提示]", "[登录]", "[信息]", "[配置]", "[窗口]", "[Cookie]", "[引擎]")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"),
    ],
)
LOG = logging.getLogger("autodownloads.web")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_print(*args: Any, **kwargs: Any) -> None:
    """pythonw.exe has no console handles; printing there would crash startup."""
    stream = kwargs.get("file") or sys.stdout
    if stream is None:
        return
    try:
        print(*args, **kwargs)
    except (OSError, AttributeError):
        pass


def format_expiry(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


def split_links(text: str) -> list[str]:
    """Keep the downloader's own URL/share-text extraction; only trim and dedupe."""
    links: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        links.append(line)
    return links


def clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def create_output_dir(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    path = path.resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"保存目录不可用（{exc}）：{path}") from exc
    if not path.is_dir():
        raise ValueError(f"保存目录不可用：{path}")
    return path


MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".flv", ".m4a", ".mp3", ".aac"}


def job_log_contains(job: "Job", needle: str) -> bool:
    with job.lock:
        return any(needle in str(entry.get("text") or "") for entry in job.logs)


def newest_media_file(output_dir: Path, since: float) -> Path | None:
    """任务开始后新出现的视频文件；没有就返回 None（用来核对下载是否真的落盘）。"""
    if not output_dir:
        return None
    cutoff = (since or 0) - 5
    newest: tuple[Path, float] | None = None
    try:
        for path in Path(output_dir).rglob("*"):
            if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if newest is None or mtime > newest[1]:
                newest = (path, mtime)
    except OSError:
        return None
    return newest[0] if newest else None


def runtime_info() -> dict[str, Any]:
    """当前进程以谁的身份运行，用来解释"打不开资源管理器"之类的问题。"""
    info: dict[str, Any] = {"user": os.environ.get("USERNAME", ""), "elevated": False}
    try:
        import ctypes

        info["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 非 Windows 或受限环境
        pass
    try:
        import ctypes

        session = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            info["session"] = int(session.value)
    except Exception:  # noqa: BLE001
        pass
    return info


def list_folder(path: str | Path) -> dict[str, Any]:
    """列出目录内容，供"打不开资源管理器"时在页面里直接看文件。"""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = PROJECT_DIR / target
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"目录不存在：{target}")
    entries: list[dict[str, Any]] = []
    try:
        items = sorted(
            target.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
        )[:200]
    except OSError as exc:
        raise ValueError(f"读取目录失败：{exc}") from exc
    for item in items:
        try:
            stat = item.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": item.name,
                "dir": item.is_dir(),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
            }
        )
    return {"path": str(target), "count": len(entries), "entries": entries}


def is_logged_in_bilibili() -> dict[str, Any]:
    try:
        state = bili.inspect_cookie_file(BILI_COOKIE)
    except OSError as exc:
        return {"loggedIn": False, "text": f"读取登录态失败：{exc}", "expiry": ""}
    if not state["exists"]:
        return {"loggedIn": False, "text": "未检测到登录态，1080P 以上可能失败", "expiry": ""}
    if state["expired"]:
        return {
            "loggedIn": False,
            "text": f"登录已过期（{format_expiry(state['expiry'])}）",
            "expiry": format_expiry(state["expiry"]),
        }
    if state["logged_in"]:
        expiry = format_expiry(state["expiry"])
        return {
            "loggedIn": True,
            "text": f"登录有效{(' · SESSDATA 至 ' + expiry) if expiry else ''}",
            "expiry": expiry,
        }
    return {"loggedIn": False, "text": "cookies.txt 中没有 SESSDATA", "expiry": ""}


def probe_bilibili_qualities(link: str, proxy: str = "") -> dict[str, Any]:
    """读取一个 B 站链接实际提供的清晰度档位。"""
    text = clean_text(link)
    if not text:
        raise ValueError("请先填入 B 站链接")
    cookie_text = bili.read_cookie_text(BILI_COOKIE) or ""
    cookies = bili.parse_netscape_cookies(cookie_text) if cookie_text else None
    header = bili.cookie_header_for(cookies) if cookies else ""
    vip_status = None
    if header:
        account = bili.fetch_account_info(
            header, timeout=12.0, proxy=proxy or None
        )
        if account is not None:
            vip_status = bool(account.get("is_vip"))
    meta, tiers = bili.inspect_quality_tiers(
        text,
        cookies=cookies,
        timeout=15.0,
        proxy=proxy or None,
        vip_status=vip_status,
    )
    if meta is None:
        raise ValueError("读不到这个视频的清晰度，请确认链接和网络")
    levels = [
        {
            "value": str(tier.get("qn")),
            "qn": tier.get("qn"),
            "label": tier.get("display") or tier.get("label") or str(tier.get("qn")),
            "height": tier.get("height"),
            "fps": tier.get("fps"),
            "needVip": bool(tier.get("need_vip")),
            "available": bool(tier.get("available")),
        }
        for tier in tiers or []
    ]
    return {
        "title": meta.get("title") or "",
        "bvid": meta.get("bvid") or "",
        "vip": vip_status,
        "levels": levels,
    }


def probe_douyin_qualities(link: str, proxy: str = "") -> dict[str, Any]:
    """用 yt-dlp 读一次抖音链接的可用清晰度。

    抖音的清晰度来自 yt-dlp 解析结果，读不到时抛 ValueError，页面会退回默认档位。
    """
    text = clean_text(link)
    if not text:
        raise ValueError("请先填入抖音链接")
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - venv 里一定有
        raise ValueError("缺少 yt-dlp，无法读取清晰度") from exc
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "http_headers": {"Referer": DOUYIN_HOME},
    }
    if proxy:
        options["proxy"] = proxy
    if DOUYIN_PROFILE.is_dir():
        # 抖音多数视频需要登录态，复用浏览器配置里的 Cookie。
        options["cookiesfrombrowser"] = ("edge", str(DOUYIN_PROFILE), None, None)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(text, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - 读不到就用默认档位
        LOG.info("douyin quality probe failed: %s", exc)
        raise ValueError("读不到这个抖音视频的清晰度，将使用默认档位") from exc
    heights = sorted(
        {
            int(fmt["height"])
            for fmt in info.get("formats") or []
            if fmt.get("height") and (fmt.get("vcodec") or "none") != "none"
        },
        reverse=True,
    )
    levels = [
        {
            "value": str(height),
            "label": f"{height}P",
            "height": height,
            "fps": None,
            "needVip": False,
            "available": True,
        }
        for height in heights
    ]
    return {"title": info.get("title") or "", "levels": levels}


def probe_qualities(platform: str, link: str, proxy: str = "") -> dict[str, Any]:
    if clean_text(platform).lower() == "douyin":
        return probe_douyin_qualities(link, proxy)
    return probe_bilibili_qualities(link, proxy)


def find_douyin_cookie_file() -> Path | None:
    candidates = (
        "Default/Network/Cookies",
        "Default/Cookies",
        "cookies.txt",
        "Default/cookies.txt",
    )
    for relative in candidates:
        candidate = DOUYIN_PROFILE / relative
        if not candidate.is_file():
            continue
        try:
            blob = candidate.read_bytes()
        except OSError:
            continue
        if b"sessionid" in blob:
            return candidate
    return None


def save_douyin_cookies(session: browser_engine.BrowserSession) -> Path | None:
    """把抖音登录态导出到配置目录。

    Chromium 被强行结束时 Cookie 可能只留在内存里，配置目录里查不到，
    页面就会一直显示未登录；顺手导出 cookies.txt 后状态判断才稳定。
    """
    try:
        DOUYIN_PROFILE.mkdir(parents=True, exist_ok=True)
        return session.export_cookies(DOUYIN_PROFILE / "cookies.txt")
    except Exception:  # noqa: BLE001 - 导出失败不该让登录流程失败
        LOG.exception("export douyin cookies failed")
        return None


def douyin_status() -> dict[str, Any]:
    if not DOUYIN_PROFILE.is_dir():
        return {
            "loggedIn": False,
            "text": "首次使用需在浏览器中登录一次",
            "profile": str(DOUYIN_PROFILE),
        }
    cookie = find_douyin_cookie_file()
    if cookie:
        try:
            source = cookie.relative_to(DOUYIN_PROFILE).as_posix()
        except ValueError:
            source = cookie.name
        return {
            "loggedIn": True,
            "text": f"已登录 · {source}",
            "profile": str(DOUYIN_PROFILE),
        }
    return {
        "loggedIn": False,
        "text": "配置目录中没有 sessionid，点「登录抖音」扫码一次",
        "profile": str(DOUYIN_PROFILE),
    }


def environment_status() -> dict[str, Any]:
    try:
        ffmpeg = bili.find_ffmpeg(None, PROJECT_DIR)
    except Exception:  # noqa: BLE001 - status must never take the page down
        ffmpeg = None
    try:
        browser = browser_engine.find_browser(None)
    except Exception:  # noqa: BLE001
        browser = None
    try:
        disk_target = DEFAULT_OUTPUT if DEFAULT_OUTPUT.exists() else PROJECT_DIR
        disk = shutil.disk_usage(disk_target)
        free_gb = round(disk.free / (1024**3), 1)
    except OSError:
        free_gb = None
    return {
        "ffmpeg": {"ok": bool(ffmpeg), "path": ffmpeg or "", "text": "可用" if ffmpeg else "未找到"},
        "browser": {"ok": bool(browser), "path": browser or "", "text": "可用" if browser else "未找到"},
        "bilibili": is_logged_in_bilibili(),
        "douyin": douyin_status(),
        "freeGb": free_gb,
        "python": sys.version.split()[0],
        "pythonExe": sys.executable,
        "runtime": runtime_info(),
        "dependencies": {"ok": not missing_dependencies(), "missing": missing_dependencies()},
        "outputDefault": str(DEFAULT_OUTPUT),
        "projectDir": str(PROJECT_DIR),
        "version": APP_VERSION,
    }


def log_level(line: str) -> str:
    text = line.strip()
    if any(marker in text for marker in ERROR_MARKERS):
        return "error"
    if any(marker in text for marker in SUCCESS_MARKERS):
        return "success"
    if any(marker in text for marker in SYSTEM_MARKERS):
        return "system"
    return "info"


def python_console_executable() -> str:
    """pythonw.exe has no stdout handles; child commands need python.exe."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(exe)


def missing_dependencies() -> list[str]:
    """Return the required modules that the running interpreter cannot import."""
    missing: list[str] = []
    for module in REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            missing.append(module)
    return missing


def _setup_env() -> dict[str, str]:
    """Environment for bootstrap commands, with temp files inside the project.

    Locked-down machines sometimes deny writes to the system %TEMP%, which makes
    ``pip`` (and ``ensurepip``) fail mid-install, so keep scratch files local.
    """
    env = dict(os.environ)
    tmp = PROJECT_DIR / ".tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
    except OSError:
        return env
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    return env


def _run_into_log(argv: list[str], *, label: str) -> None:
    """Run a setup command, appending its output to ``setup.log``."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    header = f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {label}: {' '.join(argv)}\n"
    with open(SETUP_LOG, "ab") as log:
        log.write(header.encode("utf-8", "replace"))
        log.flush()
        result = subprocess.run(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_DIR),
            env=_setup_env(),
            creationflags=creationflags,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"{label}失败（退出码 {result.returncode}），详见 {SETUP_LOG.name}"
        )


def prepare_venv() -> Path:
    """Create ``.venv`` when needed and install requirements into it."""
    python = python_console_executable()
    if not (VENV_DIR / "Scripts" / "python.exe").is_file():
        LOG.info("creating virtual environment in %s", VENV_DIR)
        _run_into_log([python, "-m", "venv", str(VENV_DIR)], label="创建虚拟环境")
    target = VENV_DIR / "Scripts" / "python.exe"
    if not target.is_file():
        raise RuntimeError("创建虚拟环境失败：找不到 .venv\\Scripts\\python.exe")
    if missing_dependencies_in(target):
        LOG.info("installing %s into %s", "requirements.txt", target.parent.parent)
        _run_into_log(
            [
                str(target),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(PROJECT_DIR / "requirements.txt"),
            ],
            label="安装依赖",
        )
    return target


def missing_dependencies_in(interpreter: Path) -> list[str]:
    """Check dependencies with a subprocess so a venv can be probed safely."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    code = "import " + ", ".join(REQUIRED_MODULES)
    try:
        result = subprocess.run(
            [str(interpreter), "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return list(REQUIRED_MODULES)
    return [] if result.returncode == 0 else list(REQUIRED_MODULES)


def ensure_runtime() -> None:
    """Guarantee yt-dlp and websocket-client before the UI starts.

    ``web_app.py`` is often started with whatever ``python`` happens to be on
    PATH (the system interpreter), which has neither package, so the login
    browser and every download job fail.  When that happens we make ``.venv``
    usable and re-launch inside it instead of dying later at login time.
    """
    missing = missing_dependencies()
    if not missing:
        return

    if os.environ.get(BOOTSTRAP_ENV) == "1":
        raise RuntimeError(
            "缺少依赖：" + "、".join(missing) + "。请运行 setup.bat 后重试。"
        )

    LOG.warning("current interpreter lacks %s", ", ".join(missing))
    try:
        interpreter = prepare_venv()
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{exc}；请运行 setup.bat 后再启动") from exc

    windowless = Path(sys.executable).name.lower() == "pythonw.exe"
    if windowless and interpreter.with_name("pythonw.exe").is_file():
        interpreter = interpreter.with_name("pythonw.exe")

    LOG.info("re-launching web UI with %s", interpreter)
    argv = [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]]
    env = dict(os.environ)
    env[BOOTSTRAP_ENV] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        os.execve(str(interpreter), argv, env)
    except OSError:
        LOG.exception("execve failed, falling back to a child process")
        subprocess.Popen(
            argv,
            env=env,
            cwd=str(PROJECT_DIR),
            creationflags=creationflags,
        )
        raise SystemExit(0) from None


def build_bilibili_argv(
    links: Iterable[str],
    output_dir: Path,
    quality: str,
    *,
    download_all: bool = False,
    limit: int = 0,
    folder_by_author: bool = False,
    force: bool = False,
    proxy: str = "",
    limit_rate: str = "",
) -> list[str]:
    argv = ["-o", str(output_dir), "--quality", quality]
    if download_all:
        argv.append("--all")
    if limit > 0:
        argv += ["--limit", str(limit)]
    if folder_by_author:
        argv.append("--folder-by-author")
    if force:
        argv.append("--force")
    if proxy.strip():
        argv += ["--proxy", proxy.strip()]
    if limit_rate.strip():
        argv += ["--limit-rate", limit_rate.strip()]
    argv += [link for link in links if link.strip()]
    return argv


def douyin_format(quality: str) -> str:
    """抖音的 --format：固定档位查表，页面探测出的任意高度也能用。"""
    if quality in DOUYIN_FORMATS:
        return DOUYIN_FORMATS[quality]
    if quality.isdigit():
        return f"best[height<={int(quality)}]"
    return "best"


def build_douyin_argv(
    links: Iterable[str],
    output_dir: Path,
    quality: str,
    *,
    download_all: bool = False,
    limit: int = 0,
    folder_by_author: bool = False,
    force: bool = False,
    proxy: str = "",
    limit_rate: str = "",
    engine: str = "auto",
    prefer_h264: bool = False,
    info: bool = False,
    thumbnail: bool = False,
) -> list[str]:
    targets: list[str] = []
    for link in links:
        text = str(link).strip()
        if text and text not in targets:
            targets.append(text)
    argv = ["-o", str(output_dir), "--format", douyin_format(quality)]
    if engine in {"browser", "yt-dlp"}:
        argv += ["--engine", engine]
    if download_all:
        argv.append("--all")
    if limit > 0:
        argv += ["--limit", str(limit)]
    if folder_by_author:
        argv.append("--folder-by-author")
    if force:
        argv.append("--force")
    if proxy.strip():
        argv += ["--proxy", proxy.strip()]
    if limit_rate.strip():
        argv += ["--limit-rate", limit_rate.strip()]
    if prefer_h264:
        argv.append("--prefer-h264")
    if info:
        argv.append("--info")
    if thumbnail:
        argv.append("--thumbnail")
    argv += targets
    return argv


@dataclass
class Job:
    id: str
    platform: str
    kind: str
    title: str
    output_dir: str = ""
    argv: list[str] = field(default_factory=list)
    status: str = "running"
    progress: float = 0.0
    item_index: int = 0
    item_total: int = 0
    summary: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    seq: int = 0
    logs: list[dict[str, Any]] = field(default_factory=list)
    process: subprocess.Popen[bytes] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def add_log(self, text: str, level: str | None = None) -> None:
        line = ANSI_RE.sub("", str(text)).replace("\x00", "").strip()
        if not line:
            return
        with self.lock:
            self.seq += 1
            self.logs.append(
                {
                    "seq": self.seq,
                    "time": time.strftime("%H:%M:%S"),
                    "level": level or log_level(line),
                    "text": line,
                }
            )
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]
            self._consume_progress(line)

    def _consume_progress(self, line: str) -> None:
        match = PROGRESS_RE.search(line) or DOUYIN_PROGRESS_RE.search(line)
        if match:
            try:
                self.progress = max(0.0, min(100.0, float(match.group(1))))
            except ValueError:
                pass
        entry = ENTRY_RE.match(line.strip())
        if entry:
            try:
                self.item_index = int(entry.group(1))
                self.item_total = int(entry.group(2))
            except ValueError:
                pass
        summary = SUMMARY_RE.search(line)
        if summary:
            self.summary = f"成功 {summary.group(1)} 个，失败 {summary.group(2)} 个"
        if self.error == "" and any(marker in line for marker in ERROR_MARKERS):
            self.error = line.strip()

    def snapshot(self, cursor: int = 0) -> dict[str, Any]:
        with self.lock:
            if cursor > 0:
                logs = [entry for entry in self.logs if entry["seq"] > cursor]
            else:
                logs = self.logs[-300:]
            truncated = bool(logs and self.logs and logs[0]["seq"] > self.logs[0]["seq"])
            return {
                "job": self.public(),
                "logs": logs,
                "nextCursor": self.seq,
                "truncated": truncated,
            }

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "platform": self.platform,
                "kind": self.kind,
                "title": self.title,
                "outputDir": self.output_dir,
                "status": self.status,
                "progress": round(self.progress, 1),
                "itemIndex": self.item_index,
                "itemTotal": self.item_total,
                "summary": self.summary,
                "error": self.error,
                "createdAt": now_iso() if not self.created_at else datetime.fromtimestamp(self.created_at).astimezone().isoformat(timespec="seconds"),
                "startedAt": datetime.fromtimestamp(self.started_at).astimezone().isoformat(timespec="seconds") if self.started_at else "",
                "finishedAt": datetime.fromtimestamp(self.finished_at).astimezone().isoformat(timespec="seconds") if self.finished_at else "",
                "indeterminate": self.kind == "login" or (self.item_total == 0 and self.progress == 0),
            }

    def finish(self, status: str, error: str = "") -> None:
        with self.lock:
            if self.status not in {"running", "queued"}:
                return
            self.status = status
            self.finished_at = time.time()
            if error:
                self.error = error
            if status == "succeeded" and self.kind == "download":
                self.progress = 100.0


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.active_id: str | None = None
        self.last_id: str | None = None
        self.lock = threading.RLock()

    def _new_job(self, platform: str, kind: str, title: str, output_dir: str = "") -> Job:
        with self.lock:
            if self.active_id:
                active = self.jobs.get(self.active_id)
                if active and active.status in {"queued", "running"}:
                    raise RuntimeError("已有任务正在运行，请先等待完成或停止")
            job = Job(
                id=secrets.token_hex(6),
                platform=platform,
                kind=kind,
                title=title,
                output_dir=output_dir,
            )
            job.started_at = time.time()
            self.jobs[job.id] = job
            self.active_id = job.id
            self.last_id = job.id
            self._trim_history()
            return job

    def _trim_history(self) -> None:
        if len(self.jobs) <= 16:
            return
        terminal = [
            job
            for job in self.jobs.values()
            if job.status not in {"queued", "running"} and job.id != self.active_id
        ]
        for job in sorted(terminal, key=lambda item: item.created_at)[: len(self.jobs) - 16]:
            self.jobs.pop(job.id, None)

    def start_download(self, payload: dict[str, Any]) -> Job:
        platform = clean_text(payload.get("platform")).lower()
        if platform not in {"bilibili", "douyin"}:
            raise ValueError("未知平台")
        links = split_links(str(payload.get("links") or ""))
        if not links:
            raise ValueError("请先填入至少一个链接")
        quality = clean_text(payload.get("quality"), "1080" if platform == "bilibili" else "best")
        if platform == "bilibili":
            # 页面直接使用视频返回的 qn 档位，所以这里按 CLI 的规则校验。
            try:
                quality = bili.parse_quality_token(quality)
            except Exception as exc:  # noqa: BLE001 - argparse 的校验错误一并转成 400
                raise ValueError(f"清晰度选项无效：{exc}") from exc
        elif quality != "best" and not quality.isdigit():
            raise ValueError("清晰度选项无效")
        output = create_output_dir(clean_text(payload.get("output"), str(DEFAULT_OUTPUT)))
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            options = {}
        try:
            limit = max(0, min(9999, int(options.get("limit") or 0)))
        except (TypeError, ValueError):
            limit = 0
        common = {
            "download_all": bool(options.get("all")),
            "limit": limit,
            "folder_by_author": bool(options.get("folder_by_author")),
            "force": bool(options.get("force")),
            "proxy": clean_text(options.get("proxy")),
            "limit_rate": clean_text(options.get("limit_rate")),
        }
        if platform == "bilibili":
            args = build_bilibili_argv(links, output, quality, **common)
            script = BILI_SCRIPT
        else:
            args = build_douyin_argv(
                links,
                output,
                quality,
                engine=clean_text(options.get("engine"), "auto"),
                prefer_h264=bool(options.get("prefer_h264")),
                info=bool(options.get("info")),
                thumbnail=bool(options.get("thumbnail")),
                **common,
            )
            script = DOUYIN_SCRIPT
        title = f"{'B 站' if platform == 'bilibili' else '抖音'} · {len(links)} 条链接"
        job = self._new_job(platform, "download", title, str(output))
        job.argv = [python_console_executable(), "-u", str(script), *args]
        job.add_log(f"[系统] 任务已创建：{title}", "system")
        job.add_log(f"[系统] 保存目录：{output}", "system")
        threading.Thread(target=self._run_process, args=(job,), name=f"autodl-{job.id}", daemon=True).start()
        return job

    def start_ffmpeg_install(self) -> Job:
        if bili.find_ffmpeg(None, PROJECT_DIR):
            raise ValueError("ffmpeg 已可用，不需要安装")
        job = self._new_job("system", "install", "安装 ffmpeg")
        job.add_log("[系统] 正在下载 ffmpeg 官方 Windows 构建（约 90 MB）。", "system")
        job.add_log("[系统] 下载完成后会自动放到 tools 目录，B 站下载即可用。", "system")
        threading.Thread(
            target=self._run_ffmpeg_install,
            args=(job,),
            name=f"autodl-{job.id}",
            daemon=True,
        ).start()
        return job

    def _run_ffmpeg_install(self, job: Job) -> None:
        def report(text: str) -> None:
            if job.cancel_event.is_set():
                raise ffmpeg_install.FfmpegInstallError("安装已取消")
            job.add_log(text)

        try:
            path = ffmpeg_install.install_ffmpeg(PROJECT_DIR / "tools", progress=report)
            job.add_log(f"[完成] ffmpeg 已安装：{path}", "success")
            job.summary = "ffmpeg 已安装"
            job.finish("succeeded")
        except Exception as exc:  # noqa: BLE001 - 失败必须反馈到页面
            if job.cancel_event.is_set():
                job.add_log("[系统] 安装已取消。", "system")
                job.finish("cancelled")
            else:
                LOG.exception("ffmpeg install failed")
                job.add_log(f"[错误] ffmpeg 安装失败：{exc}", "error")
                job.finish("failed", str(exc))
        finally:
            with self.lock:
                if self.active_id == job.id:
                    self.active_id = None

    def start_login(self, platform: str) -> Job:
        platform = clean_text(platform).lower()
        if platform not in {"bilibili", "douyin"}:
            raise ValueError("未知平台")
        title = f"登录{'B 站' if platform == 'bilibili' else '抖音'}"
        job = self._new_job(platform, "login", title)
        job.add_log("[登录] 即将打开浏览器，请完成登录后回到此页面。", "system")
        if platform == "bilibili":
            job.argv = [
                python_console_executable(),
                "-u",
                str(BILI_SCRIPT),
                "--login-once",
                "--login-timeout",
                str(int(LOGIN_TIMEOUT)),
            ]
            threading.Thread(target=self._run_process, args=(job,), name=f"autodl-{job.id}", daemon=True).start()
        else:
            threading.Thread(target=self._run_douyin_login, args=(job,), name=f"autodl-{job.id}", daemon=True).start()
        return job

    def _run_process(self, job: Job) -> None:
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                job.argv,
                cwd=str(PROJECT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=0,
                creationflags=creationflags,
            )
            job.process = proc
            assert proc.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            buffer = ""
            try:
                while True:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    buffer += decoder.decode(chunk)
                    while "\n" in buffer or "\r" in buffer:
                        positions = [pos for pos in (buffer.find("\n"), buffer.find("\r")) if pos >= 0]
                        pos = min(positions)
                        line, buffer = buffer[:pos], buffer[pos + 1 :]
                        if line.strip():
                            job.add_log(line)
            except OSError:
                # Closing the pipe after a stop/terminate is expected.
                pass
            buffer += decoder.decode(b"", final=True)
            if buffer.strip():
                job.add_log(buffer)
            code = proc.wait()
            if job.cancel_event.is_set():
                job.add_log("[系统] 任务已停止。", "system")
                job.finish("cancelled")
            elif code == 0:
                produced = newest_media_file(Path(job.output_dir), job.started_at) if job.output_dir else None
                if produced is not None:
                    job.summary = f"已保存 {produced.name}"
                    job.add_log(f"[系统] 已保存：{produced}", "success")
                    job.add_log("[系统] 任务完成。", "success")
                    job.finish("succeeded")
                elif job_log_contains(job, "本次不会重复下载"):
                    job.summary = "目录里已有同清晰度的文件，未重复下载"
                    job.add_log("[系统] 已存在同清晰度文件，跳过本次下载。", "system")
                    job.finish("succeeded")
                else:
                    # yt-dlp 跳过或只是写了归档时也会返回 0，这里必须核对产物。
                    job.add_log(
                        "[错误] 没有生成视频文件：可能是该清晰度不可用，"
                        "或目录里已有同清晰度文件被跳过（可勾选「覆盖下载」）。",
                        "error",
                    )
                    job.finish("failed", "没有生成视频文件")
            else:
                job.add_log(f"[系统] 进程退出码 {code}。", "error")
                job.finish("failed", job.error or f"下载进程退出码 {code}")
        except FileNotFoundError as exc:
            job.add_log(f"[错误] 找不到下载器：{exc}", "error")
            job.finish("failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - a job must always terminate
            LOG.exception("job %s failed", job.id)
            job.add_log(f"[错误] 任务异常：{exc}", "error")
            job.finish("failed", str(exc))
        finally:
            with self.lock:
                if self.active_id == job.id:
                    self.active_id = None

    def _run_douyin_login(self, job: Job) -> None:
        session = None
        try:
            browser = browser_engine.find_browser(None)
            job.add_log(f"[配置] 浏览器：{browser}", "system")
            job.add_log(f"[配置] 配置目录：{DOUYIN_PROFILE}", "system")
            session = browser_engine.BrowserSession(
                browser,
                window_mode="visible",
                profile_dir=DOUYIN_PROFILE,
                initial_url=DOUYIN_HOME,
                wait_seconds=3.0,
            )
            with session:
                session.navigate(DOUYIN_HOME)
                deadline = time.monotonic() + LOGIN_TIMEOUT
                last_note = 0.0
                while time.monotonic() < deadline:
                    if job.cancel_event.is_set():
                        job.add_log("[系统] 登录已取消。", "system")
                        job.finish("cancelled")
                        return
                    if session.is_logged_in(DOUYIN_LOGIN_COOKIES):
                        saved = save_douyin_cookies(session)
                        job.add_log("[登录] 登录成功，登录态已保存。", "success")
                        if saved:
                            job.add_log(f"[登录] Cookie 已写入：{saved}", "system")
                        else:
                            job.add_log(
                                "[提示] Cookie 未能导出，若状态仍显示未登录请重试一次。",
                                "system",
                            )
                        job.summary = "登录成功"
                        job.finish("succeeded")
                        return
                    if time.monotonic() - last_note > 20:
                        left = max(0, int(deadline - time.monotonic()))
                        job.add_log(f"[登录] 等待登录中，剩余约 {left} 秒…", "system")
                        last_note = time.monotonic()
                    time.sleep(1.0)
                job.add_log("[错误] 登录等待超时。", "error")
                job.finish("failed", "登录等待超时")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("douyin login failed")
            job.add_log(f"[错误] 登录失败：{exc}", "error")
            job.finish("failed", str(exc))
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            with self.lock:
                if self.active_id == job.id:
                    self.active_id = None

    def stop(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        if job.status not in {"queued", "running"}:
            return job
        job.cancel_event.set()
        job.add_log("[系统] 正在停止任务…", "system")
        proc = job.process
        if proc and proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=8,
                    )
                except Exception:  # noqa: BLE001
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        return job

    def stop_all(self) -> None:
        with self.lock:
            active = list(self.jobs.values())
        for job in active:
            if job.status in {"queued", "running"}:
                try:
                    self.stop(job.id)
                except Exception:  # noqa: BLE001
                    pass

    def current(self) -> Job | None:
        with self.lock:
            if self.active_id:
                job = self.jobs.get(self.active_id)
                if job and job.status in {"queued", "running"}:
                    return job
            if self.last_id:
                return self.jobs.get(self.last_id)
        return None

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("任务不存在")
        return job


MANAGER = JobManager()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], token: str):
        super().__init__(address, handler)
        self.token = token


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), format % args)

    def _headers(self, status: int, content_type: str, length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _authorized(self) -> bool:
        token = self.headers.get("X-App-Token", "")
        return bool(token) and secrets.compare_digest(token, self.server.token)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            raise ValueError("请求内容为空或过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON 格式不正确") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求格式不正确")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json(health_payload())
            return
        # Static files and the boot HTML must load without an API token. The
        # token is only required for data-changing / data-reading API routes.
        if path in {"/", "/index.html"}:
            self._serve_index()
            return
        if path == "/favicon.ico":
            self._headers(204, "image/x-icon", 0)
            return
        if path.startswith("/assets/"):
            self._serve_asset(path[len("/assets/") :])
            return
        if not self._authorized():
            self._error("访问令牌无效，请重新打开页面", 403)
            return
        if path == "/api/status":
            self._json(self._status_payload())
            return
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[3] if len(path.split("/")) > 3 else ""
            if not job_id:
                self._error("任务 ID 缺失", 400)
                return
            try:
                cursor = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
            except ValueError:
                cursor = 0
            try:
                job = MANAGER.get(job_id)
            except KeyError as exc:
                self._error(str(exc), 404)
                return
            self._json(job.snapshot(cursor))
            return
        self._error("页面不存在", 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._error("访问令牌无效，请重新打开页面", 403)
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/download":
                payload = self._read_json()
                job = MANAGER.start_download(payload)
                self._json({"job": job.public()}, 202)
                return
            if path == "/api/login":
                payload = self._read_json()
                job = MANAGER.start_login(clean_text(payload.get("platform")))
                self._json({"job": job.public()}, 202)
                return
            if path == "/api/install-ffmpeg":
                job = MANAGER.start_ffmpeg_install()
                self._json({"job": job.public()}, 202)
                return
            if path == "/api/qualities":
                payload = self._read_json()
                self._json(
                    probe_qualities(
                        clean_text(payload.get("platform"), "bilibili"),
                        clean_text(payload.get("link")),
                        clean_text(payload.get("proxy")),
                    )
                )
                return
            if path.startswith("/api/jobs/") and path.endswith("/stop"):
                job_id = path.split("/")[3]
                job = MANAGER.stop(job_id)
                self._json({"job": job.public()})
                return
            if path == "/api/open-folder":
                payload = self._read_json()
                target = create_output_dir(clean_text(payload.get("path"), str(DEFAULT_OUTPUT)))
                self._open_folder(target)
                self._json({"ok": True, "path": str(target)})
                return
            if path == "/api/list":
                payload = self._read_json()
                self._json(list_folder(clean_text(payload.get("path"), str(DEFAULT_OUTPUT))))
                return
            if path == "/api/shutdown":
                MANAGER.stop_all()
                self._json({"ok": True})
                threading.Thread(target=self._shutdown_later, daemon=True).start()
                return
            self._error("接口不存在", 404)
        except RuntimeError as exc:
            self._error(str(exc), 409)
        except (ValueError, KeyError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("POST %s failed", path)
            self._error(f"服务异常：{exc}", 500)

    def _status_payload(self) -> dict[str, Any]:
        status = environment_status()
        status.update(
            {
                "app": APP_NAME,
                "platforms": {
                    "bilibili": {
                        "label": "B 站",
                        "qualities": BILIBILI_QUALITIES,
                        "defaultQuality": "1080",
                    },
                    "douyin": {
                        "label": "抖音",
                        "qualities": DOUYIN_QUALITIES,
                        "defaultQuality": "best",
                    },
                },
            }
        )
        current = MANAGER.current()
        status["job"] = current.public() if current else None
        return status

    def _serve_index(self) -> None:
        file = WEB_DIR / "index.html"
        if not file.is_file():
            self._error("页面文件缺失", 500)
            return
        html = file.read_text(encoding="utf-8")
        boot = {
            "token": self.server.token,
            "version": APP_VERSION,
            "defaultOutput": str(DEFAULT_OUTPUT),
            "app": APP_NAME,
        }
        boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
        html = html.replace("__BOOT_JSON__", boot_json)
        body = html.encode("utf-8")
        self._headers(200, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _serve_asset(self, relative: str) -> None:
        relative = relative.split("?", 1)[0]
        assets_dir = (WEB_DIR / "assets").resolve()
        candidate = (assets_dir / relative).resolve()
        try:
            candidate.relative_to(assets_dir)
        except ValueError:
            self._error("非法路径", 403)
            return
        if not candidate.is_file():
            self._error("资源不存在", 404)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _open_folder(self, path: Path) -> None:
        """尝试用系统资源管理器打开目录。

        有些机器/安全策略不允许应用进程调起 shell（连 powershell.exe 都起不来），
        这时不再去试别的启动方式（只会继续弹系统错误框），直接抛错让页面
        用 /api/list 在网页里展示目录内容。
        """
        target = Path(path)
        if not target.is_dir():
            # 目录可能还没创建（例如自定义路径），退到最近的已存在目录。
            for candidate in (target.parent, PROJECT_DIR):
                if candidate.is_dir():
                    target = candidate
                    break
        if os.name == "nt":
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
                LOG.info("打开目录成功（startfile）：%s", target)
                return
            except OSError as exc:
                LOG.warning("打开目录失败 %s：startfile: %s", target, exc)
                raise RuntimeError(
                    f"系统拒绝打开资源管理器（{exc}）"
                ) from exc
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen(
                [opener, str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError(f"无法打开目录：{target}（{exc}）") from exc

    def _shutdown_later(self) -> None:
        time.sleep(0.35)
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            pass


def find_server_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        # Windows 的 SO_REUSEADDR 允许两个进程绑同一端口，先确认没有同类实例，
        # 否则新实例会和旧实例抢 8765，页面请求落到谁手里全看运气。
        if existing_instance(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用的本地端口")


def instance_info(port: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=0.7
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}
    return data if isinstance(data, dict) else {}


def existing_instance(port: int) -> bool:
    """端口上是否已有同类服务，不管它来自哪个项目目录。"""
    return instance_info(port).get("app") == "autodownloads-web"


def own_instance(port: int) -> bool:
    """端口上的实例是否属于当前项目目录，且能正常交出访问令牌。

    目录被改名或搬走之后，旧实例仍在监听端口，但它已经读不到页面文件，
    也拿不到令牌去关掉它；这种实例只能绕过，绝不能再复用。
    """
    info = instance_info(port)
    if info.get("app") != "autodownloads-web":
        return False
    if os.path.normcase(str(info.get("project") or "")) != os.path.normcase(str(PROJECT_DIR)):
        return False
    return bool(instance_token(port))


def report_unusable_instance(port: int) -> None:
    """端口被占着又没法复用时说清原因，免得看起来像启动失败。"""
    info = instance_info(port)
    if info.get("app") != "autodownloads-web":
        return
    project = str(info.get("project") or "未知目录")
    if os.path.normcase(project) == os.path.normcase(str(PROJECT_DIR)):
        safe_print(
            f"端口 {port} 上的实例取自本目录，但已经打不开页面（页面文件或代码缺失），"
            "未复用；本次改用新端口"
        )
        return
    safe_print(
        f"端口 {port} 上的实例来自另一个目录：{project}"
        "（不复用也不停止），本次改用新端口启动"
    )


def write_port_file(port: int) -> None:
    """Remember the live port so start.bat can find an already running server."""
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        LOG.warning("无法写入 %s", PORT_FILE)


def clear_port_file(port: int) -> None:
    try:
        if PORT_FILE.read_text(encoding="utf-8").strip() != str(port):
            return  # 另一个实例已经接管
    except OSError:
        return
    try:
        PORT_FILE.unlink()
    except OSError:
        pass


def recorded_port() -> int | None:
    try:
        value = PORT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def find_running_instance(port: int) -> int | None:
    """Return the port of an already running workbench, if any.

    Checks the recorded port first: scanning a range is slow, because a closed
    local port costs a full connection timeout before it fails.
    """
    candidates = []
    for candidate in (recorded_port(), port):
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if own_instance(candidate):
            return candidate
    return None


def code_stamp() -> str:
    """当前磁盘上后端代码的指纹，用来判断运行中的实例是不是旧代码。"""
    digest = hashlib.sha1()
    for name in (
        "web_app.py",
        "bilibili_downloader.py",
        "douyin_downloader.py",
        "browser_engine.py",
        "ffmpeg_install.py",
    ):
        try:
            stat = (PROJECT_DIR / name).stat()
        except OSError:
            continue
        digest.update(f"{name}:{int(stat.st_mtime)}:{stat.st_size};".encode("utf-8"))
    return digest.hexdigest()[:12]


# 进程启动时的代码指纹：健康检查要用它，而不是每次请求现算（现算永远等于磁盘）。
CODE_STAMP = code_stamp()


def health_payload() -> dict[str, Any]:
    """健康检查响应。project 让调用方分清同名服务来自哪个项目目录。"""
    return {
        "app": "autodownloads-web",
        "version": APP_VERSION,
        "ok": True,
        "code": CODE_STAMP,
        "project": str(PROJECT_DIR),
    }


def instance_code_stamp(port: int) -> str:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=0.7
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return ""
    return str(data.get("code") or "")


def instance_token(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            html = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError):
        return ""
    match = re.search(r'"token":\s*"([^"]+)"', html)
    return match.group(1) if match else ""


def print_log_tail(count: int) -> None:
    """打印最近几行日志，供 start.bat 启动失败时排错（不要把整份日志倒出来）。"""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines[-max(1, int(count)):]:
        print(line)


def stop_stale_instance(port: int) -> bool:
    """代码更新后停掉还在跑的旧实例；返回 True 表示确实停掉了。"""
    running = find_running_instance(port)
    if running is None:
        report_unusable_instance(port)
        return False
    stamp = instance_code_stamp(running)
    if stamp and stamp == code_stamp():
        safe_print(f"已在运行，代码也是最新的：http://127.0.0.1:{running}/")
        return False
    token = instance_token(running)
    if not token:
        safe_print(f"旧实例（端口 {running}）拿不到访问令牌，未自动停止")
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{running}/api/shutdown",
        method="POST",
        headers={"X-App-Token": token},
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except (OSError, urllib.error.URLError):
        return False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not existing_instance(running):
            safe_print(f"已停止旧实例（端口 {running}），准备用新代码启动")
            return True
        time.sleep(0.3)
    return False


def run(port: int, open_browser: bool) -> int:
    if open_browser:
        running = find_running_instance(port)
        if running is not None:
            webbrowser.open(f"http://127.0.0.1:{running}/")
            return 0

    actual_port = find_server_port(port)
    token = secrets.token_urlsafe(24)
    server = AppServer(("127.0.0.1", actual_port), Handler, token)
    write_port_file(actual_port)
    url = f"http://127.0.0.1:{actual_port}/"
    LOG.info("starting %s at %s", APP_NAME, url)
    LOG.info("runtime: %s", runtime_info())
    safe_print(f"{APP_NAME} {APP_VERSION}")
    safe_print(f"本地页面：{url}")
    safe_print("关闭页面中的「退出服务」即可停止。")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.35)
    except KeyboardInterrupt:
        safe_print("\n正在停止…")
    finally:
        MANAGER.stop_all()
        server.server_close()
        clear_port_file(actual_port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="只检测是否已有服务在运行，并输出它的端口（供 start.bat 使用）",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="代码已更新时停止还在跑的旧实例（供 start.bat 使用）",
    )
    parser.add_argument(
        "--log-tail",
        type=int,
        default=0,
        metavar="N",
        help="打印最近 N 行 web.log（供 start.bat 排错）",
    )
    args = parser.parse_args(argv)

    if args.log_tail:
        print_log_tail(args.log_tail)
        return 0

    if args.stop:
        stop_stale_instance(args.port)
        return 0

    if args.probe:
        running = find_running_instance(args.port)
        if running is None:
            return 1
        print(running)
        return 0

    try:
        ensure_runtime()
        return run(args.port, args.open)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("startup failed")
        safe_print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
