#!/usr/bin/env python3
"""B 站视频下载脚本（yt-dlp + 一次性浏览器登录，下载全程不弹窗）。

工作流程：
1. 首次使用执行一次 --login-once：打开一个可见浏览器窗口，登录 B 站，
   登录态自动导出到脚本目录的 cookies.txt，然后关闭浏览器。
2. 之后所有运行只读取 cookies.txt，不再启动浏览器、不弹任何窗口。
3. 下载使用 yt-dlp 的 Python API；1080P 是 DASH 音视频分离，必须由 ffmpeg 合并。

用法示例：
    python bilibili_downloader.py --login-once
    python bilibili_downloader.py "https://www.bilibili.com/video/BV1xx411c7mD"
    python bilibili_downloader.py --file links.txt
    python bilibili_downloader.py --up 3546984120059960 --all
    python bilibili_downloader.py "https://space.bilibili.com/3546984120059960/video" --all --limit 10

仅供个人备份和学习使用。请遵守哔哩哔哩用户协议、著作权法及当地法律法规，
不要下载、传播无权使用的视频。
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from browser_engine import detect_system_proxy

APP_NAME = "bilibili-downloader"
VERSION = "1.0.0"
DEFAULT_OUTPUT = "downloads"
DEFAULT_TEMPLATE = "%(id)s_%(title).80s.%(ext)s"
AUTHOR_TEMPLATE = "%(uploader,creator|bilibili).50s/" + DEFAULT_TEMPLATE
ARCHIVE_NAME = ".bilibili-archive.txt"
COOKIE_FILE_NAME = "cookies.txt"

# B 站登录态的唯一关键 Cookie；缺少它就一定拿不到 1080P 与 60 帧。
LOGIN_COOKIE_NAME = "SESSDATA"

# 可选清晰度；2160=4K、1440=2K 需要大会员登录态，best 表示不设上限。
QUALITY_HEIGHTS = {
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "best": None,
}
QUALITY_CHOICES = tuple(QUALITY_HEIGHTS)
# 同一分辨率的高帧率档位：B 站用独立 qn，界面要能单独选。
QUALITY_ALIASES = {
    "1080p60": 116,
    "720p60": 74,
}
# 超过 1080 的档位属于大会员专享。
VIP_QUALITIES = ("1440", "2160")
QUALITY_SHORT_LABELS = {"2160": "4K", "1440": "2K"}
ACCOUNT_API = "https://api.bilibili.com/x/web-interface/nav"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"

# B 站原生 qn（清晰度码）：--quality / --qn 都可以直接写这些数字。
# 112 及以上是大会员专享（112=1080P+，116=1080P60，120=4K，125=HDR，126=杜比，127=8K）。
KNOWN_QNS = (127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16, 6)
VIP_QN_MIN = 112
# 同一 qn 可能有多种编码：兼容性优先（avc1 最通用，其次 HEVC，最后 AV1）。
CODEC_GROUPS = (("avc1",), ("hev1", "hvc1"), ("av01",))
CODECID_NAMES = {7: "avc1", 12: "hev1", 13: "av01"}
# qn -> 常规分辨率；只有在读不到官方档位表时作为兜底匹配用。
QN_HEIGHT_HINTS = {
    127: 4320,
    126: 2160,
    125: 2160,
    120: 2160,
    116: 1080,
    112: 1080,
    80: 1080,
    74: 720,
    64: 720,
    32: 480,
    16: 360,
    6: 240,
}
# 60 帧档位：兜底匹配时必须显式要求高帧率，否则会把 1080P60 选成 1080P30。
QN_HIGH_FPS_QNS = (116, 74)
QN_DISPLAY = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}

BILIBILI_HOSTS = ("bilibili.com", "b23.tv", "acg.tv")
SHORT_LINK_HOSTS = ("b23.tv",)

BILIBILI_HOME = "https://www.bilibili.com/"
SPACE_VIDEO_URL = "https://space.bilibili.com/{mid}/video"
VIDEO_URL = "https://www.bilibili.com/video/{vid}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)
TRAILING_PUNCT = "，。！？、；：）】》」』…,.!?;:)]}>'\"”’"

BV_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{3,})(?![0-9A-Za-z])", re.I)
SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)", re.I)
RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kKmMgG]?)$")

# cookie 失效 / 需要登录的报错特征（小写匹配）。
LOGIN_ERROR_MARKERS = (
    "please log in",
    "please sign in",
    "log in to",
    "sign in to",
    "login required",
    "cookies are needed",
    "failed to load cookies",
    "cookie file",
    "sessdata",
    "unauthorized",
    "error code: 401",
    "需要登录",
    "请先登录",
    "登录态已失效",
    "账号未登录",
    "大会员",
    "premium member",
)

# 代理/传输层故障特征（小写匹配）。
PROXY_ERROR_MARKERS = (
    "no such file or directory",
    "eof occurred in violation of protocol",
    "winerror 10054",
    "connection reset",
    "connection aborted",
    "proxy",
    "ssl",
    "timed out",
)

# 网络层故障特征：直连被 TLS 重置、代理连不上、连接被强制关闭。
NETWORK_ERROR_MARKERS = (
    "eof occurred in violation of protocol",
    "winerror 10054",
    "winerror 10060",
    "winerror 10061",
    "connection reset",
    "connection aborted",
    "connection refused",
    "transporterror",
    "transport error",
    "remote end closed connection",
    "sslerror",
    "ssl: ",
    "[errno 104]",
    "[errno 111]",
    "no such file or directory",
)
NETWORK_ERROR_TYPES: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    ssl.SSLEOFError,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
)
# build_ydl_options 里用来区分"没传代理覆盖"和"显式要求不用代理"的哨兵。
_PROXY_UNSET = object()

PROXY_HINT = (
    "这看起来是代理/网络层的问题（不是登录态问题）。若本地代理无法访问 "
    "www.bilibili.com 或 api.bilibili.com，可用 NO_PROXY 环境变量把能直连的域名排除，"
    "例如：set NO_PROXY=www.bilibili.com"
)

# WBI 签名用的打乱表（B 站前端固定常量）。
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

FFMPEG_INSTALL_HELP = r"""\
[错误] 没有找到 ffmpeg，无法继续。
       B 站 1080P 是 DASH 音视频分离流，必须用 ffmpeg 合并音轨和视频轨。

请任选一种方式安装，然后重新运行：
  1. 下载官方静态包：https://www.gyan.dev/ffmpeg/builds/ （或 https://github.com/BtbN/FFmpeg-Builds/releases）
     解压后把其中的 bin 目录加入系统 PATH 环境变量；
  2. 用包管理器安装：winget install Gyan.FFmpeg  /  choco install ffmpeg  /  scoop install ffmpeg；
  3. 不想改 PATH 时，把 ffmpeg.exe 放到本项目下的 tools\ffmpeg.exe，
     或用 --ffmpeg D:\某处\bin\ffmpeg.exe 直接指定完整路径。

安装完成后新开一个终端，运行 ffmpeg -version 确认可用。"""

# 常见安装位置（不含 PATH 与项目 tools 目录，它们单独检测）。
FFMPEG_COMMON_PATHS = (
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
    r"E:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    r"D:\EVCapture\ffmpeg.exe",
    r"D:\sinsam\app\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"/usr/bin/ffmpeg",
    r"/usr/local/bin/ffmpeg",
    r"/opt/homebrew/bin/ffmpeg",
)

# 覆盖内置常见路径列表用的环境变量（os.pathsep 分隔）。
# 设为 none/off/0 时完全不扫描常见安装位置（命令行删不掉空变量，所以留了显式哨兵值）。
FFMPEG_PATHS_ENV = "BILIBILI_FFMPEG_PATHS"
FFMPEG_NO_SCAN_VALUES = ("none", "off", "0", "-")


class BilibiliError(RuntimeError):
    """B 站下载器的一般性错误。"""


class LoginRequiredError(BilibiliError):
    """登录态缺失或已失效。"""


class FfmpegMissingError(BilibiliError):
    """找不到 ffmpeg。"""


class TargetError(BilibiliError):
    """链接无法解析成可下载目标。"""


# --------------------------------------------------------------------------
# 链接与目标解析
# --------------------------------------------------------------------------


def extract_urls(text: str) -> list[str]:
    """从分享文案、命令行参数或文本里提取 B 站链接并按出现顺序去重。"""
    urls: list[str] = []
    seen: set[str] = set()

    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCT)
        if url.lower().startswith("http://"):
            url = "https://" + url[7:]
        if not is_bilibili_url(url):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)

    return urls


def is_bilibili_url(url: str) -> bool:
    """判断是不是 B 站域名（严格匹配，避免 bilibili.com.evil.example）。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in BILIBILI_HOSTS)


def is_short_link(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in SHORT_LINK_HOSTS)


def extract_bvid(text: str) -> str | None:
    match = BV_RE.search(text or "")
    return match.group(1) if match else None


def extract_av_id(text: str) -> str | None:
    match = AV_RE.search(text or "")
    return match.group(1) if match else None


def extract_mid(text: str) -> str | None:
    match = SPACE_RE.search(text or "")
    if match:
        return match.group(1)
    stripped = (text or "").strip()
    if stripped.isdigit():
        return stripped
    return None


def video_url(bvid: str) -> str:
    return VIDEO_URL.format(vid=bvid)


def space_url(mid: str) -> str:
    return SPACE_VIDEO_URL.format(mid=mid)


def make_absolute(url: str) -> str:
    return "https://" + url[7:] if url.lower().startswith("http://") else url


def resolve_short_url(
    url: str,
    timeout: float = 20.0,
    opener: Any = None,
) -> str:
    """把 b23.tv 短链解析成真实地址；解析失败时原样返回。"""
    if not is_short_link(url):
        return url
    try:
        client = opener or build_opener()
        response = client.open(
            Request(url, headers={"User-Agent": USER_AGENT}, method="GET"),
            timeout=timeout,
        )
        final = response.geturl() or url
        return make_absolute(final)
    except (URLError, OSError, ValueError):
        return url


def parse_rate(value: str) -> int:
    match = RATE_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"无法识别的限速值：{value}（示例：500K、2M）")
    number = float(match.group(1))
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2).lower()]
    return int(number * scale)


def read_input_file(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SystemExit(f"无法读取链接文件 {path}：{exc}") from exc


def collect_urls(args: argparse.Namespace) -> list[str]:
    """命令行链接 + --file + --up 合并成一份去重后的 URL 列表。"""
    parts = list(args.links)
    if args.file:
        parts.append(read_input_file(args.file))
    if args.up:
        text = args.up.strip()
        mid = extract_mid(text)
        parts.append(space_url(mid) if mid else text)

    # 逐行处理：文件里的裸 BV 号 / av 号 也要能识别。
    lines = [line for part in parts for line in part.splitlines()]
    text = "\n".join(lines)
    urls = extract_urls(text)

    for raw in lines:
        if extract_urls(raw):
            continue
        bvid = extract_bvid(raw)
        if bvid:
            urls.append(video_url(bvid))
        elif extract_av_id(raw):
            urls.append(f"https://www.bilibili.com/video/av{extract_av_id(raw)}")

    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


# --------------------------------------------------------------------------
# Cookie 文件
# --------------------------------------------------------------------------


def parse_netscape_cookies(text: str) -> list[dict[str, str]]:
    """解析 Netscape cookies.txt；跳过注释行和字段不完整的行。

    浏览器引擎导出的文件里，HttpOnly Cookie 带 "#HttpOnly_" 前缀
    （curl / yt-dlp 的通用写法）。B 站的 SESSDATA 正是 HttpOnly，
    所以这里必须先剥掉前缀再解析，否则登录态永远读不出来。
    """
    cookies: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, flag, path, secure, expiry, name, value = fields[:7]
        cookies.append(
            {
                "domain": domain,
                "flag": flag,
                "path": path,
                "secure": secure,
                "expiry": expiry,
                "name": name,
                "value": value,
            }
        )
    return cookies


def read_cookie_text(path: Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def cookie_names(cookies: Iterable[dict[str, str]]) -> set[str]:
    return {cookie.get("name", "") for cookie in cookies if cookie.get("name")}


def has_login_cookie(cookies: Iterable[dict[str, str]]) -> bool:
    return LOGIN_COOKIE_NAME in cookie_names(cookies)


def cookie_expiry(cookies: Iterable[dict[str, str]], name: str) -> float | None:
    for cookie in cookies:
        if cookie.get("name") == name:
            try:
                return float(cookie.get("expiry") or 0)
            except (TypeError, ValueError):
                return None
    return None


def cookie_header_for(cookies: Iterable[dict[str, str]], domain: str = "bilibili.com") -> str:
    """拼出用于 HTTP 请求头 Cookie 字段的字符串（只取目标域名下的条目）。"""
    parts: list[str] = []
    for cookie in cookies:
        name = cookie.get("name") or ""
        if not name:
            continue
        host = (cookie.get("domain") or "").lstrip(".").lower()
        if host and domain not in host and host not in domain:
            continue
        parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def inspect_cookie_file(path: Path) -> dict[str, Any]:
    """读取 cookie 文件并给出登录态结论（不联网）。"""
    text = read_cookie_text(path)
    cookies = parse_netscape_cookies(text or "")
    expiry = cookie_expiry(cookies, LOGIN_COOKIE_NAME) if cookies else None
    expired = bool(expiry and expiry > 0 and expiry < time.time())
    return {
        "path": Path(path),
        "exists": text is not None,
        "cookies": cookies,
        "count": len(cookies),
        "logged_in": has_login_cookie(cookies) and not expired,
        "has_sessdata": has_login_cookie(cookies),
        "expired": expired,
        "expiry": expiry,
    }


def resolve_cookie_file(args: argparse.Namespace, script_dir: Path) -> Path:
    if getattr(args, "cookies", None):
        return Path(args.cookies).expanduser()
    return Path(script_dir) / COOKIE_FILE_NAME


# --------------------------------------------------------------------------
# ffmpeg 探测
# --------------------------------------------------------------------------


def ffmpeg_search_paths(
    script_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """按优先级返回 ffmpeg 候选路径：项目 tools 目录 + 常见安装位置。"""
    environment = os.environ if env is None else env
    base = Path(script_dir) if script_dir else Path(__file__).resolve().parent
    candidates: list[str] = [
        str(base / "tools" / "ffmpeg.exe"),
        str(base / "tools" / "ffmpeg"),
    ]

    override = environment.get(FFMPEG_PATHS_ENV)
    if override is not None:
        # 显式设置时只用用户给的列表；none/off/0 表示不扫描常见路径。
        if override.strip().lower() in FFMPEG_NO_SCAN_VALUES:
            return candidates
        for item in override.split(os.pathsep):
            item = item.strip()
            if item:
                candidates.append(item)
        return candidates

    candidates.extend(FFMPEG_COMMON_PATHS)

    for variable, suffix in (
        ("LOCALAPPDATA", ("Programs", "ffmpeg", "bin", "ffmpeg.exe")),
        ("USERPROFILE", ("ffmpeg", "bin", "ffmpeg.exe")),
        ("ProgramFiles", ("ffmpeg", "bin", "ffmpeg.exe")),
    ):
        root = environment.get(variable)
        if root:
            candidates.append(str(Path(root).joinpath(*suffix)))

    return candidates


def _is_executable_file(path: str | Path) -> bool:
    target = Path(path)
    try:
        return target.is_file()
    except OSError:
        return False


def find_ffmpeg(
    explicit: str | None = None,
    script_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """按 PATH -> 项目 tools -> 常见安装位置 的顺序返回可用 ffmpeg 路径。"""
    if explicit:
        candidate = Path(explicit).expanduser()
        return str(candidate) if _is_executable_file(candidate) else None

    environment = os.environ if env is None else env
    on_path = shutil.which("ffmpeg", path=environment.get("PATH"))
    if on_path:
        return on_path

    for candidate in ffmpeg_search_paths(script_dir, environment):
        if _is_executable_file(candidate):
            return str(candidate)
    return None


def require_ffmpeg(
    explicit: str | None = None,
    script_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if explicit and not _is_executable_file(Path(explicit).expanduser()):
        raise FfmpegMissingError(
            f"--ffmpeg 指定的文件不存在或不可执行：{explicit}\n{FFMPEG_INSTALL_HELP}"
        )
    found = find_ffmpeg(explicit, script_dir, env)
    if not found:
        raise FfmpegMissingError(FFMPEG_INSTALL_HELP)
    return found


# --------------------------------------------------------------------------
# 清晰度
# --------------------------------------------------------------------------


def build_format_selector(quality: str) -> str:
    """把 --quality 映射成 yt-dlp 的 format 表达式（音视频分离时合并成 mp4）。"""
    height = QUALITY_HEIGHTS.get(quality, 1080)
    if height is None:
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def max_video_height(info: dict[str, Any]) -> int | None:
    heights = [
        fmt.get("height")
        for fmt in info.get("formats") or []
        if fmt.get("height") and (fmt.get("vcodec") or "none") != "none"
    ]
    heights = [h for h in heights if isinstance(h, int)]
    return max(heights) if heights else None


def quality_rows(info: dict[str, Any]) -> list[str]:
    """把 yt-dlp 的 formats 整理成中文清晰度表格行。"""
    rows: list[str] = []
    for fmt in info.get("formats") or []:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        size_text = f"{size / 1048576:.2f} MB" if size else "未知大小"
        fmt_id = fmt.get("format_id") or "?"
        if vcodec == "none" and acodec == "none":
            continue
        if vcodec == "none":
            rows.append(f"  音频    {fmt_id:<8} {acodec.split('.')[0]:<8} {size_text}")
        elif acodec == "none":
            rows.append(
                f"  {str(fmt.get('height') or 0):>4}P  {fmt_id:<8} "
                f"{vcodec.split('.')[0]:<8} {size_text}  视频轨"
            )
        else:
            rows.append(
                f"  {str(fmt.get('height') or 0):>4}P  {fmt_id:<8} "
                f"{vcodec.split('.')[0]:<8} {size_text}  混合流"
            )
    return rows


def describe_quality_gap(
    info: dict[str, Any],
    quality: str,
    *,
    vip_status: bool | None = None,
    logged_in: bool | None = None,
) -> str | None:
    """请求的清晰度拿不到时，返回一句能区分原因的中文说明（绝不静默降级）。

    三种原因分开讲，不把"没有大会员"说成"网络问题"：

    ① 没有登录态 / 没有大会员  -> 告诉用户去登录或升级；
    ② 视频本身没有这个清晰度源 -> 告诉用户换档位，别再折腾账号；
    ③ 拿不到账号信息（离线/接口失败）-> 同时提示两种可能，并给出自查命令。
    """
    wanted = QUALITY_HEIGHTS.get(quality)
    actual = max_video_height(info)
    if wanted is None or actual is None or actual >= wanted:
        return None

    label = QUALITY_SHORT_LABELS.get(quality, f"{wanted}P")
    needs_vip = quality in VIP_QUALITIES

    if logged_in is False:
        return (
            f"请求 {quality}P（{label}），但当前没有登录态，只取到最高 {actual}P。"
            "B 站 1080P 及以上都需要先登录：请执行一次 --login-once。"
        )
    if needs_vip and vip_status is False:
        return (
            f"请求 {quality}P（{label}），但当前账号不是大会员，只取到最高 {actual}P。"
            f"{label} 属于大会员专享；可改用 --quality 1080 或 --quality best。"
        )
    if needs_vip and vip_status is True:
        return (
            f"请求 {quality}P（{label}），但该视频本身没有 {label} 源："
            f"当前账号已是大会员，实际最高只有 {actual}P。"
            f"可改用 --quality {actual} 或 --quality best。"
        )
    if needs_vip:
        return (
            f"请求 {quality}P（{label}），实际最高只有 {actual}P。"
            f"两种可能：账号没有大会员，或该视频本身没有 {label} 源。"
            "先用 --check-login 确认登录态，或用 --list-formats 看真实轨道。"
        )
    return (
        f"请求 {quality}P，但当前账号只能取到最高 {actual}P。"
        "1080P/60 帧需要登录态：请先执行一次 --login-once。"
    )


def fetch_account_info(
    cookies: Iterable[dict[str, str]] | str | None = None,
    *,
    timeout: float = 20.0,
    proxy: str | None = None,
    opener: Any = None,
) -> dict[str, Any] | None:
    """只读地查一次账号信息（是否登录、是否大会员）。

    这是"锦上添花"的诊断入口：**任何失败都返回 None**，
    绝不会抛异常，也不会阻塞或改变下载流程。离线时就是 None。
    """
    header = cookies if isinstance(cookies, str) else cookie_header_for(cookies or [])
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BILIBILI_HOME,
        "Accept": "application/json, text/plain, */*",
    }
    if header:
        headers["Cookie"] = header
    if opener is not None:
        client = opener
    elif proxy:
        client = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    else:
        client = build_opener()
    payload: dict[str, Any] | None = None
    for attempt in range(2):
        try:
            response = client.open(Request(ACCOUNT_API, headers=headers), timeout=timeout)
            payload = json.loads(response.read().decode("utf-8", "replace"))
            break
        except Exception:  # noqa: BLE001 - 离线/超时/证书/解析失败一律当作"不知道"
            if attempt >= 1:
                return None
            time.sleep(1.0)
    if payload is None:
        return None
    if payload.get("code") != 0:
        return None
    data = payload.get("data") or {}
    vip = data.get("vip") or {}
    try:
        vip_status = int(vip.get("status") or 0)
    except (TypeError, ValueError):
        vip_status = 0
    return {
        "logged_in": bool(data.get("isLogin")),
        "uname": data.get("uname") or "",
        "vip_status": vip_status,
        "is_vip": vip_status == 1,
    }


def account_vip_status(
    cookies: Iterable[dict[str, str]] | str | None = None,
    **kwargs: Any,
) -> bool | None:
    """账号是否是大会员；查不到（离线/接口失败）返回 None，不影响下载。"""
    info = fetch_account_info(cookies, **kwargs)
    if info is None:
        return None
    return bool(info["is_vip"] and info["logged_in"])


# --------------------------------------------------------------------------
# WBI 签名 + UP 主投稿枚举
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# B 站官方档位表（qn）
# --------------------------------------------------------------------------


def parse_quality_token(value: str) -> str:
    """校验 --quality：既接受分辨率档位，也接受 B 站原生 qn 数字。"""
    text = str(value).strip()
    if text in QUALITY_CHOICES:
        return text
    alias = QUALITY_ALIASES.get(text.lower())
    if alias is not None:
        # 转成 qn，后面统一走按 qn 选档的路径（能区分 1080P30 / 1080P60）。
        return str(alias)
    if text.isdigit() and int(text) in KNOWN_QNS:
        return str(int(text))
    raise argparse.ArgumentTypeError(
        f"未知的清晰度：{value}（可选：{', '.join(QUALITY_CHOICES)}、"
        f"{', '.join(QUALITY_ALIASES)}，"
        "或 B 站 qn 数字如 116 / 120 / 125，可先用 --list-qualities 查看）"
    )


def quality_target(quality: Any) -> tuple[int, int | None] | None:
    """把清晰度档位或 qn 换成 (目标高度, 目标帧率)。

    帧率为 None 表示不挑帧率；60 表示要 1080P60 / 720P60 这类高帧率档。
    """
    text = str(quality or "").strip()
    alias = QUALITY_ALIASES.get(text.lower())
    if alias is not None:
        text = str(alias)
    if text in QUALITY_HEIGHTS:
        height = QUALITY_HEIGHTS[text]
        return (int(height), None) if height else None
    if text.isdigit() and int(text) in QN_HEIGHT_HINTS:
        qn = int(text)
        return QN_HEIGHT_HINTS[qn], (60 if qn in QN_HIGH_FPS_QNS else None)
    return None


def effective_qn(args: argparse.Namespace) -> int | None:
    """本次请求的目标 qn；没有按 qn 选档时返回 None（走分辨率语义）。"""
    qn = getattr(args, "qn", None)
    if qn:
        return int(qn)
    quality = str(getattr(args, "quality", "") or "").strip()
    # 分辨率档位优先：2160/1440/1080/720/480 也是数字，但它们走分辨率语义。
    if quality in QUALITY_CHOICES:
        return None
    if quality.isdigit() and int(quality) in KNOWN_QNS:
        return int(quality)
    return None


def _codec_family(codec: str | None) -> str:
    text = (codec or "").lower()
    if text.startswith("avc1"):
        return "avc1"
    if text.startswith("hev1") or text.startswith("hvc1"):
        return "hevc"
    if text.startswith("av01"):
        return "av01"
    return ""


def _codec_rank(family: str) -> int:
    """编码优先级：avc1 最通用，其次 HEVC，最后 AV1。"""
    order = {"avc1": 0, "hevc": 1, "av01": 2}
    return order.get(family, 9)


def _api_request(
    url: str,
    *,
    cookies: Iterable[dict[str, str]] | str | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
    opener: Any = None,
    attempts: int = 2,
) -> dict[str, Any] | None:
    """GET 一个 B 站 web 接口并解析 JSON；任何失败都返回 None。

    B 站线路偶发 TLS 重置，所以默认重试一次（attempts=2），再多就是浪费时间。
    """
    header = cookies if isinstance(cookies, str) else cookie_header_for(cookies or [])
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BILIBILI_HOME,
        "Accept": "application/json, text/plain, */*",
    }
    if header:
        headers["Cookie"] = header
    if opener is not None:
        client = opener
    elif proxy:
        client = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    else:
        client = build_opener()
    for attempt in range(max(1, attempts)):
        try:
            response = client.open(Request(url, headers=headers), timeout=timeout)
            return json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - 离线/超时/证书/解析失败一律当作"读不到"
            if attempt + 1 >= max(1, attempts):
                return None
            time.sleep(1.0)
    return None


def fetch_video_meta(
    bvid_or_url: str,
    *,
    cookies: Iterable[dict[str, str]] | str | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
    opener: Any = None,
) -> dict[str, Any] | None:
    """取视频基础信息（cid / 标题 / 分 P）；失败返回 None。"""
    text = str(bvid_or_url)
    bvid = extract_bvid(text)
    av_id = None if bvid else extract_av_id(text)
    if not bvid and not av_id:
        return None
    query = f"?bvid={bvid}" if bvid else f"?aid={av_id}"
    payload = _api_request(
        VIEW_API + query, cookies=cookies, timeout=timeout, proxy=proxy, opener=opener
    )
    if not payload or payload.get("code") != 0:
        return None
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    cid = data.get("cid") or (pages[0].get("cid") if pages else None)
    if not cid:
        return None
    return {
        "bvid": data.get("bvid") or bvid or "",
        "aid": data.get("aid"),
        "cid": int(cid),
        "title": data.get("title") or "",
        "pages": pages,
    }


def build_quality_tiers(
    data: dict[str, Any],
    *,
    cid: int | None = None,
    meta: dict[str, Any] | None = None,
    vip_status: bool | None = None,
) -> list[dict[str, Any]]:
    """把 playurl 的返回整理成档位表（纯函数，方便测试）。

    标签**完全照抄 B 站**：label 取 accept_description（如「真彩 HDR」），
    display 取 support_formats 的 display_desc + superscript（如「4K」「1080P60帧」）。
    """
    accept = data.get("accept_quality") or []
    description = data.get("accept_description") or []
    support: dict[int, dict[str, Any]] = {}
    for item in data.get("support_formats") or []:
        try:
            support.setdefault(int(item.get("quality")), item)
        except (TypeError, ValueError):
            continue
    streams_by_qn: dict[int, list[dict[str, Any]]] = {}
    for item in ((data.get("dash") or {}).get("video") or []):
        try:
            streams_by_qn.setdefault(int(item.get("id")), []).append(item)
        except (TypeError, ValueError):
            continue

    tiers: list[dict[str, Any]] = []
    for index, raw_qn in enumerate(accept):
        try:
            qn = int(raw_qn)
        except (TypeError, ValueError):
            continue
        info = support.get(qn) or {}
        label = (
            description[index]
            if index < len(description) and description[index]
            else (info.get("new_description") or str(qn))
        )
        display = str(info.get("display_desc") or QN_DISPLAY.get(qn) or qn)
        superscript = str(info.get("superscript") or "")
        if superscript and superscript not in display:
            display = f"{display}{superscript}"
        streams = sorted(
            streams_by_qn.get(qn, []),
            key=lambda item: _codec_rank(
                _codec_family("".join(CODECID_NAMES.get(int(item.get("codecid") or 0), "")))
            ),
        )
        families: list[str] = []
        for codec in info.get("codecs") or []:
            family = _codec_family(codec)
            if family and family not in families:
                families.append(family)
        for stream in streams:
            family = _codec_family(CODECID_NAMES.get(int(stream.get("codecid") or 0), ""))
            if family and family not in families:
                families.append(family)
        families.sort(key=_codec_rank)
        primary = streams[0] if streams else {}
        need_vip = qn >= VIP_QN_MIN
        tiers.append(
            {
                "qn": qn,
                "label": label,
                "display": display,
                "codecs": families,
                "need_vip": need_vip,
                "available": bool(not (need_vip and vip_status is False)),
                "width": primary.get("width"),
                "height": primary.get("height"),
                "fps": primary.get("frameRate"),
                "streams": streams,
                "cid": cid,
            }
        )
    return tiers


def fetch_quality_tiers(
    bvid_or_url: str,
    cid: int | None = None,
    *,
    cookies: Iterable[dict[str, str]] | str | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
    opener: Any = None,
    vip_status: bool | None = None,
) -> list[dict[str, Any]] | None:
    """读取 B 站官方档位表；任何失败都返回 None（绝不阻塞下载）。"""
    _meta, tiers = inspect_quality_tiers(
        bvid_or_url,
        cid,
        cookies=cookies,
        timeout=timeout,
        proxy=proxy,
        opener=opener,
        vip_status=vip_status,
    )
    return tiers or None


def inspect_quality_tiers(
    bvid_or_url: str,
    cid: int | None = None,
    *,
    cookies: Iterable[dict[str, str]] | str | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
    opener: Any = None,
    vip_status: bool | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    """一次拿全：视频基础信息 + 官方档位表。任何失败都返回 (None|meta, None)。"""
    meta = fetch_video_meta(
        bvid_or_url, cookies=cookies, timeout=timeout, proxy=proxy, opener=opener
    )
    if meta is None:
        return None, None
    use_cid = int(cid) if cid else meta["cid"]
    url = (
        f"{PLAYURL_API}?bvid={meta['bvid']}&cid={use_cid}"
        "&qn=127&fnval=4048&fourk=1"
    )
    payload = _api_request(
        url, cookies=cookies, timeout=timeout, proxy=proxy, opener=opener
    )
    if not payload or payload.get("code") != 0:
        return meta, None
    tiers = build_quality_tiers(
        payload.get("data") or {},
        cid=use_cid,
        meta=meta,
        vip_status=vip_status,
    )
    return meta, (tiers or None)


def tier_for_qn(tiers: Iterable[dict[str, Any]], qn: int) -> dict[str, Any] | None:
    for tier in tiers or []:
        if int(tier.get("qn") or 0) == int(qn):
            return tier
    return None


def format_quality_tiers(
    tiers: Sequence[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
    logged_in: bool | None = None,
    vip_status: bool | None = None,
) -> list[str]:
    """把档位表排成给人看的行（B 站原文标签 + qn + 权限 + 编码）。"""
    lines: list[str] = []
    if meta:
        lines.append(
            f"[档位] 视频：{meta.get('title') or ''}（{meta.get('bvid') or ''}，cid={meta.get('cid')}）"
        )
    state: list[str] = []
    if logged_in is not None:
        state.append("已登录" if logged_in else "未登录")
    if vip_status is not None:
        state.append("大会员" if vip_status else "非大会员")
    if state:
        lines.append("[档位] 账号：" + " / ".join(state))
    best = next((tier for tier in tiers if tier.get("available")), None)
    if best:
        lines.append(
            f"[档位] 当前账号可用最高档：{best['label']}（qn={best['qn']}）"
        )
    for tier in tiers:
        lines.append(
            "  %-12s qn=%-4d %s %s   编码 %s"
            % (
                tier["label"],
                tier["qn"],
                "大会员" if tier["need_vip"] else "免费  ",
                "可用  " if tier["available"] else "不可用",
                "/".join(tier["codecs"]) or "-",
            )
        )
    return lines


def _fps_matches(left: Any, right: Any, tolerance: float = 1.5) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return True


def resolve_qn_format(
    info: dict[str, Any],
    tier: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """在 yt-dlp 的 formats 里找出该 qn 对应的视频流（按编码优先级）。

    yt-dlp 的 format_id 就是这条 DASH 流的 id，所以这里返回的 id
    可以直接拼成 -f "<id>+ba"，而不是靠分辨率去猜。
    """
    formats = [
        fmt
        for fmt in (info.get("formats") or [])
        if (fmt.get("vcodec") or "none") != "none"
        and (fmt.get("acodec") or "none") == "none"
    ]
    height = tier.get("height")
    fps = tier.get("fps")
    fps_mode = tier.get("fps_mode")

    def _fps_ok(fmt: dict[str, Any]) -> bool:
        """已知确切帧率就按容差比；只知道"60 帧档/普通档"就按 50 分界卡。"""
        if fps:
            return _fps_matches(fmt.get("fps"), fps)
        if fps_mode == "high":
            try:
                return float(fmt.get("fps")) > 50
            except (TypeError, ValueError):
                return False
        if fps_mode == "low":
            try:
                return float(fmt.get("fps")) <= 50
            except (TypeError, ValueError):
                return True
        return True

    for group in CODEC_GROUPS:
        for fmt in formats:
            if not any(fmt.get("vcodec", "").lower().startswith(prefix) for prefix in group):
                continue
            if height and fmt.get("height") != height:
                continue
            if not _fps_ok(fmt):
                continue
            return str(fmt.get("format_id")), fmt
    # 兜底：只按分辨率 + 帧率匹配，不管编码族
    for fmt in formats:
        if height and fmt.get("height") == height and _fps_ok(fmt):
            return str(fmt.get("format_id")), fmt
    return None, None


def describe_qn_gap(
    tier: dict[str, Any] | None,
    qn: int,
    *,
    logged_in: bool | None = None,
    vip_status: bool | None = None,
) -> str:
    """按 qn 选档但拿不到时的说明（与 describe_quality_gap 同风格）。"""
    if tier is None:
        return (
            f"该视频没有 qn={qn} 这一档。"
            "先用 --list-qualities 看它到底有哪些档位，再挑一个可用的。"
        )
    if not tier.get("available"):
        if logged_in is False:
            return (
                f"qn={qn}（{tier['label']}）需要登录态：请先执行一次 --login-once。"
            )
        return (
            f"qn={qn}（{tier['label']}）是大会员专享，当前账号取不到。"
            "可改用较低档位，或升级大会员。"
        )
    return (
        f"qn={qn}（{tier['label']}）在当前视频的可用流里没有找到对应轨道。"
        "请用 --list-formats 确认，或改用 --quality 分辨率档位。"
    )


def format_selector_for_video_id(video_id: str, audio: str = "ba") -> str:
    """把 dash 视频流 id 拼成 yt-dlp 的 -f 表达式。"""
    return f"{video_id}+{audio}"


def mixin_key_from(img_key: str, sub_key: str) -> str:
    """由 nav 接口返回的两个 key 生成 WBI mixinKey（纯函数，便于测试）。"""
    raw = (img_key or "") + (sub_key or "")
    if len(raw) < 64:
        raise BilibiliError("WBI key 长度不足，无法生成签名")
    return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def _key_from_image_url(url: str) -> str:
    name = (url or "").rsplit("/", 1)[-1]
    return name.split(".")[0]


class WbiClient:
    """B 站 web 接口的最小客户端：buvid3 引导 + WBI 签名 + UP 主投稿翻页。"""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        cookie_header: str = "",
        opener: Any = None,
    ) -> None:
        self.timeout = timeout
        self.cookie_header = cookie_header
        self.headers = {
            "User-Agent": USER_AGENT,
            "Referer": BILIBILI_HOME,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if cookie_header:
            self.headers["Cookie"] = cookie_header
        if opener is not None:
            self._opener = opener
        elif proxy:
            self._opener = build_opener(
                ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            self._opener = build_opener()
        self._jar = http.cookiejar.CookieJar()
        self._img_key = ""
        self._sub_key = ""

    def _fetch(self, url: str) -> str:
        response = self._opener.open(
            Request(url, headers=self.headers), timeout=self.timeout
        )
        return response.read().decode("utf-8", "replace")

    def _fetch_json(self, url: str) -> dict[str, Any]:
        body = self._fetch(url)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise BilibiliError(f"B 站接口返回的不是 JSON：{body[:120]}") from exc

    def bootstrap(self) -> None:
        """访问首页拿到 buvid3（否则 space 接口返回 412），再取 WBI key。"""
        try:
            self._fetch(BILIBILI_HOME)
        except (URLError, OSError):
            pass
        payload = self._fetch_json("https://api.bilibili.com/x/web-interface/nav")
        data = payload.get("data") or {}
        wbi = data.get("wbi_img") or {}
        self._img_key = _key_from_image_url(wbi.get("img_url") or "")
        self._sub_key = _key_from_image_url(wbi.get("sub_url") or "")
        if not self._img_key or not self._sub_key:
            raise BilibiliError("无法从 nav 接口获取 WBI key")

    @property
    def mixin_key(self) -> str:
        if not self._img_key:
            self.bootstrap()
        return mixin_key_from(self._img_key, self._sub_key)

    def sign_params(self, params: dict[str, Any]) -> str:
        signed = {str(k): str(v) for k, v in params.items()}
        signed["wts"] = str(int(time.time()))
        query = urlencode(sorted(signed.items()))
        digest = hashlib.md5((query + self.mixin_key).encode("utf-8")).hexdigest()
        return f"{query}&w_rid={digest}"

    def space_videos(
        self,
        mid: str,
        limit: int = 0,
        page_size: int = 30,
    ) -> list[dict[str, Any]]:
        """翻页枚举某个 UP 主的全部投稿（按发布时间倒序）。"""
        videos: list[dict[str, Any]] = []
        page = 1
        max_pages = 200
        while page <= max_pages:
            params = {
                "mid": str(mid),
                "ps": str(page_size),
                "pn": str(page),
                "order": "pubdate",
                "platform": "web",
            }
            url = "https://api.bilibili.com/x/space/wbi/arc/search?" + self.sign_params(
                params
            )
            payload = self._fetch_json(url)
            code = payload.get("code")
            if code != 0:
                raise _space_error(code, payload.get("message"))
            data = payload.get("data") or {}
            vlist = ((data.get("list") or {}).get("vlist")) or []
            if not vlist:
                break
            for item in vlist:
                bvid = item.get("bvid")
                if not bvid:
                    continue
                videos.append(
                    {
                        "bvid": bvid,
                        "title": item.get("title") or "",
                        "length": item.get("length") or "",
                        "created": item.get("created"),
                        "author": item.get("author") or "",
                    }
                )
                if limit and len(videos) >= limit:
                    return videos
            total = (data.get("page") or {}).get("count") or 0
            if total and len(videos) >= int(total):
                break
            page += 1
        return videos


def _space_error(code: Any, message: Any) -> BilibiliError:
    text = f"B 站 space 接口返回 code={code}：{message or '无附加信息'}"
    if code in (-101, -400, -403, -352, 412, -412):
        hint = (
            "登录态缺失、已失效或被风控拦截。"
            "请先执行一次 --login-once 重新登录，或稍后再试。"
        )
        if code in (-101, -352):
            return LoginRequiredError(f"{text}\n提示：{hint}")
        return BilibiliError(f"{text}\n提示：{hint}")
    return BilibiliError(text)


# --------------------------------------------------------------------------
# 一次性登录（唯一允许弹出窗口的环节）
# --------------------------------------------------------------------------


def default_session_factory(**kwargs: Any) -> Any:
    """默认浏览器会话工厂：复用项目内持久化配置，启动后停在 B 站首页。

    显式传入 initial_url 时以传入值为准（传 None 表示启动后不导航），
    这样登录窗口不会先访问抖音再跳转到 B 站。
    """
    from browser_engine import BrowserSession, find_browser, default_profile_dir

    browser = kwargs.pop("browser_path", None) or find_browser(None)
    profile = kwargs.pop("profile_dir", None) or default_profile_dir()
    kwargs.setdefault("initial_url", BILIBILI_HOME)
    return BrowserSession(browser, profile_dir=profile, **kwargs)


def run_login(
    cookie_path: Path,
    *,
    browser_path: str | None = None,
    proxy: str | None = None,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
    session_factory: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Path:
    """打开可见浏览器让用户登录一次，登录态导出到 cookie_path 后关闭浏览器。"""
    factory = session_factory or default_session_factory
    target = Path(cookie_path).expanduser()

    try:
        session = factory(
            browser_path=browser_path,
            window_mode="visible",
            proxy=proxy,
            wait_seconds=3.0,
        )
    except Exception as exc:  # noqa: BLE001 - 浏览器启动失败原因很多
        raise BilibiliError(f"无法启动浏览器：{exc}") from exc

    with session:
        session.navigate(BILIBILI_HOME)
        deadline = clock() + timeout
        while True:
            try:
                logged_in = session.is_logged_in([LOGIN_COOKIE_NAME])
            except Exception as exc:  # noqa: BLE001
                raise BilibiliError(f"读取浏览器 Cookie 失败：{exc}") from exc
            if logged_in:
                break
            if clock() >= deadline:
                raise LoginRequiredError(
                    f"等待登录超时（{int(timeout)} 秒）。请重新运行 --login-once 完成登录。"
                )
            sleep(poll_interval)

        try:
            session.export_cookies(target)
        except Exception as exc:  # noqa: BLE001
            raise BilibiliError(f"导出 Cookie 失败：{exc}") from exc

    return target


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------


def load_ytdlp():
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        print("缺少依赖 yt-dlp。请先运行：", file=sys.stderr)
        print(f'  "{sys.executable}" -m pip install -r requirements.txt', file=sys.stderr)
        raise SystemExit(2) from exc

    return yt_dlp, DownloadError


def is_login_error(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in LOGIN_ERROR_MARKERS)


def is_proxy_error(error: BaseException) -> bool:
    """识别典型的代理/传输层故障（不是 B 站登录或权限问题）。"""
    text = str(error).lower()
    return any(marker in text for marker in PROXY_ERROR_MARKERS)


def is_network_error(error: BaseException) -> bool:
    """识别"直连被重置/代理不可用"这类网络层故障，用来决定是否改走系统代理。

    yt-dlp 会把底层异常包成 DownloadError，所以既要看异常类型，
    也要看整条 __cause__ / __context__ 链上的异常类型和文本。
    """
    for item in _error_chain(error):
        if isinstance(item, NETWORK_ERROR_TYPES):
            return True
    text = " ".join(str(item).lower() for item in _error_chain(error))
    return any(marker in text for marker in NETWORK_ERROR_MARKERS)


def _error_chain(error: BaseException, depth: int = 5) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < depth:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def archive_file_name(quality: Any) -> str:
    """已下载记录按清晰度分文件。

    只用一份归档时，同一链接换清晰度会被 yt-dlp 直接当"已下载"跳过，
    页面却报成功；分开记录才能让 1080P -> 4K 真的重下。
    """
    value = str(quality or "").strip()
    if value in QUALITY_HEIGHTS and value != "best":
        return f".bilibili-archive-{value}.txt"
    if value.isdigit():
        # 1080P60 / 4K 这类按 qn 选的档位也各记各的，换档才不会被归档挡住。
        return f".bilibili-archive-qn{value}.txt"
    return ARCHIVE_NAME


def find_existing_medias(output_dir: Path, video_id: str) -> list[Path]:
    """某个视频已经下载好的所有文件（作者分目录也找），新的在前。"""
    if not video_id:
        return []
    suffix_patterns = (".mp4", ".mkv", ".webm", ".flv", ".m4a")
    try:
        files = [path for path in output_dir.rglob("*") if path.is_file()]
    except OSError:
        return []
    matched = [
        path
        for path in files
        if path.suffix.lower() in suffix_patterns and video_id in path.name
    ]
    return sorted(matched, key=lambda path: path.stat().st_mtime, reverse=True)


def find_existing_media(output_dir: Path, video_id: str) -> Path | None:
    """在输出目录里找某个视频已经下载好的文件（作者分目录也找）。"""
    medias = find_existing_medias(output_dir, video_id)
    return medias[0] if medias else None


def quality_outtmpl(template: str, label: str) -> str:
    """给文件名加清晰度后缀，避免不同清晰度互相覆盖。"""
    stem, separator, extension = template.rpartition(".")
    if not separator:
        return f"{template}_{label}"
    return f"{stem}_{label}{separator}{extension}"


def probe_media_info(
    path: Path, ffmpeg_path: str | None = None
) -> tuple[int | None, float | None]:
    """用 ffmpeg -i 读出已有视频的 (高度, 帧率)；读不到的部分返回 None。"""
    executable = ffmpeg_path or "ffmpeg"
    try:
        proc = subprocess.run(
            [executable, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    text = (proc.stderr or b"").decode("utf-8", "replace")
    heights = [int(match.group(2)) for match in re.finditer(r"(\d{2,5})x(\d{2,5})", text)]
    fps_values = [float(match.group(1)) for match in re.finditer(r"([\d.]+) fps", text)]
    return (max(heights) if heights else None), (max(fps_values) if fps_values else None)


def probe_media_height(path: Path, ffmpeg_path: str | None = None) -> int | None:
    """用 ffmpeg -i 读出已有视频的分辨率高度；读不到返回 None。"""
    return probe_media_info(path, ffmpeg_path)[0]


def build_ydl_options(
    args: argparse.Namespace,
    output_dir: Path,
    cookiefile: Path | None,
    ffmpeg_path: str,
    proxy: Any = _PROXY_UNSET,
    format_selector: str | None = None,
) -> dict[str, Any]:
    template = AUTHOR_TEMPLATE if args.folder_by_author else DEFAULT_TEMPLATE

    options: dict[str, Any] = {
        "paths": {"home": str(output_dir)},
        "outtmpl": {"default": template},
        "format": format_selector or build_format_selector(args.quality),
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "trim_file_name": 180,
        "retries": args.retries,
        "fragment_retries": args.retries,
        "socket_timeout": args.timeout,
        "continuedl": True,
        "overwrites": bool(args.force),
        "force_overwrites": bool(args.force),
        "ignoreerrors": False,
        "quiet": not args.verbose,
        "no_warnings": not args.verbose,
        "noprogress": bool(args.no_progress),
        "consoletitle": False,
        "ffmpeg_location": ffmpeg_path,
        "http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": BILIBILI_HOME,
        },
    }

    if cookiefile is not None:
        options["cookiefile"] = str(cookiefile)
        options["http_headers"]["Cookie"] = _cookie_field_from_file(cookiefile)

    if args.limit_rate:
        options["ratelimit"] = args.limit_rate

    # proxy 显式覆盖时以覆盖值为准（兜底重试会传系统代理）；否则沿用 args.proxy。
    effective_proxy = args.proxy if proxy is _PROXY_UNSET else proxy
    if effective_proxy:
        options["proxy"] = effective_proxy

    if args.list_formats:
        options["listformats"] = True
        options["skip_download"] = True
    elif args.dry_run:
        options["skip_download"] = True

    if not args.force and not args.no_archive and not args.dry_run:
        options["download_archive"] = str(output_dir / archive_file_name(args.quality))

    return options


def prepare_cookiefile(path: Path | None) -> Path | None:
    """交给 yt-dlp 用的 Cookie 文件副本。

    yt-dlp 把 --cookies 指向的文件当成"可读可写"：下载结束后会把 Cookie
    写回原文件。直接给它用户的 cookies.txt 会有两个问题：

    1. 文件只读（或位于无写权限的位置）时，整个下载会因为
       PermissionError 被判失败，尽管视频已经下完了；
    2. 服务端回来的 Cookie 会悄悄改写用户手里的登录态文件。

    所以这里复制一份到临时目录，让 yt-dlp 爱怎么写怎么写。
    """
    if path is None:
        return None
    source = Path(path)
    try:
        directory = Path(tempfile.mkdtemp(prefix="bili-cookies-"))
        # 临时目录里是 Cookie 副本，进程结束时删掉，别留在磁盘上。
        atexit.register(shutil.rmtree, directory, True)
        target = directory / COOKIE_FILE_NAME
        shutil.copyfile(source, target)
        return target
    except OSError:
        # 复制不了就退回原文件：至少读得到，能不能写回交给 yt-dlp 自己处理。
        return source


def _cookie_field_from_file(path: Path) -> str:
    """把 cookies.txt 里 B 站相关的条目拼成 Cookie 头，提高接口成功率。"""
    text = read_cookie_text(path) or ""
    return cookie_header_for(parse_netscape_cookies(text))


class Downloader:
    """封装单次下载：负责 yt-dlp 选项、错误分类和登录态提示。"""

    def __init__(
        self,
        args: argparse.Namespace,
        output_dir: Path,
        yt_dlp: Any,
        download_error: type[BaseException],
        cookiefile: Path | None,
        ffmpeg_path: str,
        proxy: str | None = None,
        system_proxy: str | None = None,
    ) -> None:
        self.args = args
        self.output_dir = output_dir
        self.yt_dlp = yt_dlp
        self.download_error = download_error
        self.cookiefile = cookiefile
        self.ffmpeg_path = ffmpeg_path
        self.proxy = proxy
        # 兜底用的系统代理（和本次实际使用的代理可能不同）。
        self.system_proxy = system_proxy
        self.tried_system_proxy: str | None = None
        self.attempts = 0
        self._account_status: tuple[bool | None, bool | None] | None = None

    def account_status(self) -> tuple[bool | None, bool | None]:
        """(是否大会员, 是否已登录)；都查不到时是 (None, None)。

        只读、可离线失败，永远不抛异常，因此可以安全地用在诊断文案里。
        """
        if self._account_status is None:
            header = (
                _cookie_field_from_file(self.cookiefile) if self.cookiefile else ""
            )
            info = fetch_account_info(
                header or None,
                timeout=min(20.0, float(getattr(self.args, "timeout", 20.0) or 20.0)),
                proxy=self.proxy,
            )
            if info is None:
                self._account_status = (None, None)
            else:
                self._account_status = (bool(info["is_vip"]), bool(info["logged_in"]))
        return self._account_status

    def _options(
        self, proxy: Any = _PROXY_UNSET, format_selector: str | None = None
    ) -> dict[str, Any]:
        return build_ydl_options(
            self.args,
            self.output_dir,
            self.cookiefile,
            self.ffmpeg_path,
            self.proxy if proxy is _PROXY_UNSET else proxy,
            format_selector,
        )

    def _cookie_arg(self) -> str | None:
        if not self.cookiefile:
            return None
        return _cookie_field_from_file(self.cookiefile) or None

    def fetch_tiers(self, url: str) -> list[dict[str, Any]] | None:
        """按链接读取 B 站官方档位表；失败返回 None（不阻塞下载）。"""
        return fetch_quality_tiers(
            url,
            cookies=self._cookie_arg(),
            timeout=float(getattr(self.args, "timeout", 30.0) or 30.0),
            proxy=self.proxy,
        )

    def resolve_qn_selector(self, url: str, qn: int) -> str:
        """把 qn 解析成 -f "<dash_id>+ba"；拿不到就抛 BilibiliError 并说明原因。"""
        info = self.probe(url)
        tiers = self.fetch_tiers(url)
        tier = tier_for_qn(tiers or [], qn) if tiers else None
        if tier is None:
            # 档位表读不到时，退一步用 yt-dlp 自己的轨道 + qn 的常规分辨率推断
            guess = QN_HEIGHT_HINTS.get(qn)
            if guess is None:
                raise BilibiliError(
                    f"读不到档位表，也不认识 qn={qn}；请改用 --quality 分辨率档位"
                )
            fps_mode = "high" if qn in QN_HIGH_FPS_QNS else "low"
            tier = {
                "qn": qn,
                "label": QN_DISPLAY.get(qn, str(qn)),
                "height": guess,
                "fps": None,
                "fps_mode": fps_mode,
                "available": True,
                "codecs": [],
                "need_vip": qn >= VIP_QN_MIN,
            }
            print(
                f"[档位] 读不到 B 站档位表，按 qn={qn} 的常规规格匹配："
                f"{guess}P / {'60 帧档' if fps_mode == 'high' else '普通帧率档'}"
            )
        if not tier.get("available"):
            vip, logged = self.account_status()
            raise BilibiliError(
                describe_qn_gap(tier, qn, logged_in=logged, vip_status=vip)
            )
        video_id, chosen = resolve_qn_format(info, tier)
        if not video_id:
            vip, logged = self.account_status()
            raise BilibiliError(
                describe_qn_gap(tier, qn, logged_in=logged, vip_status=vip)
            )
        codec = (chosen.get("vcodec") or "").split(".")[0] or "?"
        print(
            f"[档位] qn={qn}（{tier.get('label')}）实际选用编码：{codec} (id={video_id})"
        )
        return format_selector_for_video_id(video_id)

    def _fallback_proxy(self, error: BaseException) -> str | None:
        """网络层失败时是否值得改用系统代理再试一次。"""
        if not is_network_error(error):
            return None
        fallback = self.system_proxy
        if not fallback:
            return None
        if fallback == self.proxy or fallback == self.tried_system_proxy:
            return None
        return fallback

    def _with_fallback(self, action: Callable[[str | None], Any]) -> Any:
        """执行一次 action(代理)；网络层失败且没用过系统代理时，只补试一次。"""
        self.attempts += 1
        try:
            return action(self.proxy)
        except Exception as exc:
            fallback = self._fallback_proxy(exc)
            if fallback is None:
                raise
            print(f"[网络] 直连失败，改用系统代理 {fallback} 重试")
            self.tried_system_proxy = fallback
            self.attempts += 1
            return action(fallback)

    def proxy_failure_note(self) -> str:
        """网络失败后给用户的"还剩什么招"说明。"""
        if self.tried_system_proxy:
            return (
                f"已自动改用系统代理 {self.tried_system_proxy} 重试，仍然失败；"
                "请检查该代理是否可用。"
            )
        if self.system_proxy:
            return f"本次已经在用系统代理 {self.system_proxy}，仍然失败。"
        return "没有探测到 Windows 系统代理，只能用 --proxy 手动指定可用的代理。"

    def probe(self, url: str) -> dict[str, Any]:
        def action(proxy: Any) -> dict[str, Any]:
            options = self._options(proxy)
            options["skip_download"] = True
            options.pop("listformats", None)
            with self.yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False) or {}

        return self._with_fallback(action)

    def _existing_for_url(self, url: str) -> list[tuple[Path, int | None, float | None]]:
        video_id = extract_bvid(url) or ""
        if not video_id:
            av_id = extract_av_id(url)
            video_id = f"av{av_id}" if av_id else ""
        if not video_id:
            return []
        return [
            (path, *probe_media_info(path, self.ffmpeg_path))
            for path in find_existing_medias(self.output_dir, video_id)
        ]

    def _plan(self, url: str) -> tuple[str, str]:
        """决定这次怎么下：("skip"|"fresh"|"quality", 清晰度标签)。

        - skip：目录里已经有同清晰度的文件，不重复下载；
        - fresh：目录里没有这个视频，用默认文件名；
        - quality：已有其它清晰度的文件，另存一份带清晰度后缀的文件，不覆盖。
        """
        existing = self._existing_for_url(url)
        if bool(getattr(self.args, "force", False)):
            return "fresh", ""
        target = quality_target(getattr(self.args, "quality", ""))
        if not existing:
            return "fresh", ""
        if not target:
            print(f"[画质] 目录里已有 {existing[0][0].name}，本次不会重复下载。")
            return "skip", ""
        wanted_height, wanted_fps = target
        label = f"{wanted_height}P{'60' if wanted_fps else ''}"
        for path, height, fps in existing:
            if height is None:
                continue
            same_height = height == wanted_height
            fps_ok = not wanted_fps or fps is None or fps >= wanted_fps - 5
            if same_height and fps_ok:
                print(f"[画质] 目录里已有 {height}P 的 {path.name}，本次不会重复下载。")
                return "skip", ""
        print(f"[画质] 目标 {label} 还没有：另存为带清晰度后缀的文件，不覆盖已有视频。")
        return "quality", label

    def download(self, url: str) -> int:
        qn = effective_qn(self.args)
        selector = self.resolve_qn_selector(url, qn) if qn else None
        plan, label = self._plan(url)
        if plan == "skip":
            # 同清晰度已在目录里，连归档都不用碰。
            return 0

        def action(proxy: Any) -> int:
            options = self._options(proxy, selector)
            if plan == "quality":
                # 另存一份：换文件名 + 不覆盖 + 不查归档（归档按 id 记账会直接跳过）。
                template = str(options["outtmpl"]["default"])
                options["outtmpl"]["default"] = quality_outtmpl(template, label)
                options["overwrites"] = False
                options["force_overwrites"] = False
                options.pop("download_archive", None)
            with self.yt_dlp.YoutubeDL(options) as ydl:
                return ydl.download([url])

        return self._with_fallback(action)


# --------------------------------------------------------------------------
# 提示输出
# --------------------------------------------------------------------------


def print_login_help(cookie_path: Path, python: str | None = None) -> None:
    interpreter = python or sys.executable
    print("[登录] 未检测到 B 站登录态（需要登录一次，之后长期复用）。")
    print(f"[登录] Cookie 文件：{cookie_path}")
    print("[登录] 1080P / 60 帧 / 大会员清晰度必须先登录才能获取。")
    print("[登录] 请执行一次下面这条命令（只此一次会弹出浏览器窗口）：")
    print(f'[登录]   "{interpreter}" bilibili_downloader.py --login-once')
    print("[登录] 登录完成后浏览器会自动关闭，Cookie 写入上面的文件；")
    print("[登录] 之后所有下载都不再启动浏览器、不弹任何窗口。")
    print("[登录] 也可以手动指定已有的 Cookie：--cookies 路径\\cookies.txt")


def print_login_expired() -> None:
    print("[登录] 登录态已失效（SESSDATA 已过期或被服务端作废）。")
    print("[登录] 请重新执行一次：bilibili_downloader.py --login-once")


def describe_target(url: str) -> tuple[str, str]:
    """把 URL 归类成 ('space'|'video'|'other', 标识)。"""
    mid = extract_mid(url)
    if mid and not extract_bvid(url):
        return "space", mid
    bvid = extract_bvid(url)
    if bvid:
        return "video", bvid
    av_id = extract_av_id(url)
    if av_id:
        return "video", f"av{av_id}"
    return "other", url


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilibili_downloader.py",
        description=(
            "B 站视频批量下载脚本（yt-dlp + 一次性浏览器登录；"
            "登录之外的全程不弹浏览器窗口，仅供个人备份）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python bilibili_downloader.py --login-once\n"
            '  python bilibili_downloader.py "https://www.bilibili.com/video/BV1xx411c7mD"\n'
            "  python bilibili_downloader.py --file links.txt\n"
            "  python bilibili_downloader.py --up 3546984120059960 --all\n"
            '  python bilibili_downloader.py "https://space.bilibili.com/3546984120059960/video" --all --limit 10\n'
            "  python bilibili_downloader.py --check-login\n"
            "\n"
            "首次使用请先执行一次 --login-once：只在那一次会打开可见浏览器窗口，\n"
            "登录态保存为脚本目录下的 cookies.txt 后长期复用。\n"
            "之后所有枚举与下载都只读取 cookies.txt，不再启动浏览器、不弹窗口。\n"
            "1080P 是 DASH 音视频分离流，必须安装 ffmpeg（脚本会自动探测）。\n"
        ),
    )

    parser.add_argument(
        "links",
        nargs="*",
        help="B 站链接或分享文案：BV 号、av 号、视频 URL、UP 主空间 URL、b23.tv 短链",
    )
    parser.add_argument(
        "--file",
        type=Path,
        metavar="FILE",
        help="从文本文件批量读取链接（文件里可以有分享文案，脚本会自动提取 URL）",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        metavar="DIR",
        help=f"保存目录（默认：{DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--up",
        metavar="MID_OR_URL",
        help="指定 UP 主（数字 mid 或空间 URL），配 --all 或 --limit 使用",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="下载该 UP 主下面的全部视频（空间链接或 --up 都算）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="最多处理 N 个视频（0 表示不限制；对 UP 主枚举生效）",
    )
    parser.add_argument(
        "--quality",
        type=parse_quality_token,
        metavar="{2160,1440,1080,720,480,best}",
        default="1080",
        help=(
            "目标清晰度（默认：1080，需要登录态）；"
            "2160=4K、1440=2K，需要大会员登录态且单文件可达 GB 级；"
            "best=不设上限（有大会员时会自动取到 4K）。"
            "注意 best 会跟着账号权限走，要明确锁 4K 就用 2160。"
            "也接受 B 站原生 qn 数字（如 116=高清 1080P60、120=超清 4K、125=真彩 HDR），"
            "先用 --list-qualities 看有哪些档位。"
        ),
    )
    parser.add_argument(
        "--qn",
        type=int,
        metavar="N",
        help=(
            "直接按 B 站 qn 选档（如 116=1080P60、120=4K、125=真彩 HDR）；"
            "与 --quality 同时给出时以 --qn 为准"
        ),
    )
    parser.add_argument(
        "--list-qualities",
        action="store_true",
        help="只读取并打印该视频的 B 站官方档位表（qn / 文案 / 是否大会员 / 是否可用），不下载",
    )
    parser.add_argument(
        "--login-once",
        action="store_true",
        help="打开可见浏览器登录一次并导出 cookies.txt（唯一会弹窗的环节）",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="删除已保存的 cookies.txt（退出登录态）",
    )
    parser.add_argument(
        "--check-login",
        action="store_true",
        help="只检查 cookies.txt 的登录态，不联网、不下载",
    )
    parser.add_argument(
        "--require-login",
        action="store_true",
        help="没有登录态时直接退出（默认是提示后继续，用实际清晰度告警）",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        metavar="FILE",
        help=f"指定 Netscape 格式 cookies.txt（默认：脚本目录下的 {COOKIE_FILE_NAME}）",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="只列出可用清晰度，不下载",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只枚举并显示将要下载的内容，不写文件",
    )
    parser.add_argument(
        "--folder-by-author",
        action="store_true",
        help="按 UP 主名字建立子目录",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略下载归档记录并覆盖已有文件",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help=f"不写入下载归档（默认写入 {ARCHIVE_NAME}，已下载的会跳过）",
    )
    parser.add_argument(
        "--limit-rate",
        type=parse_rate,
        metavar="RATE",
        help="限速，例如 500K、2M",
    )
    parser.add_argument(
        "--proxy",
        metavar="URL",
        help="代理地址，例如 http://127.0.0.1:7890（默认自动使用 Windows 系统代理）",
    )
    parser.add_argument(
        "--no-system-proxy",
        action="store_true",
        help=(
            "不主动使用 Windows 系统代理（默认会自动用）；"
            "网络失败时仍会兜底改试系统代理一次"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SEC",
        help="网络超时秒数（默认：30）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        metavar="N",
        help="失败重试次数（默认：5）",
    )
    parser.add_argument(
        "--ffmpeg",
        metavar="PATH",
        help="指定 ffmpeg 可执行文件（默认自动探测 PATH、项目 tools 目录和常见安装路径）",
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=300.0,
        metavar="SEC",
        help="--login-once 等待登录的秒数（默认：300）",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="不显示下载进度条",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="显示调试信息",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {VERSION}",
    )

    return parser


def _apply_proxy_env(proxy: str | None) -> None:
    """让 urllib / yt-dlp / 子进程统一走同一个代理。"""
    if not proxy:
        return
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = proxy


def env_proxy_configured(env: dict[str, str] | None = None) -> bool:
    """环境变量里是否已经有代理设置（有就让环境变量说了算）。"""
    environment = os.environ if env is None else env
    names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
    return any((environment.get(name) or "").strip() for name in names)


def resolve_proxy(args: argparse.Namespace) -> tuple[str | None, str]:
    """决定本次运行实际使用哪个代理。

    优先级：--proxy > 环境变量里的代理 > Windows 系统代理 > 直连。
    返回 (代理 URL 或 None, 来源)；来源取值 explicit / env / system / disabled / none。
    """
    if getattr(args, "proxy", None):
        return args.proxy, "explicit"
    if env_proxy_configured():
        return None, "env"
    if getattr(args, "no_system_proxy", False):
        return None, "disabled"
    detected = detect_system_proxy()
    if detected:
        return detected, "system"
    return None, "none"


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _report_login_state(cookie_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    state = inspect_cookie_file(cookie_path)
    if state["expired"]:
        print_login_expired()
    elif not state["logged_in"]:
        print_login_help(cookie_path)
    else:
        print(f"[登录] 已检测到登录态：{cookie_path}（{state['count']} 条 Cookie）")
    return state


def _expand_space_targets(
    urls: Sequence[str],
    args: argparse.Namespace,
    create_client: Callable[[], WbiClient],
) -> list[str]:
    """把 UP 主空间链接展开成视频 URL 列表。"""
    expanded: list[str] = []
    space_urls = [url for url in urls if describe_target(url)[0] == "space"]
    plain = [url for url in urls if describe_target(url)[0] != "space"]

    if space_urls and not args.all and not args.limit:
        raise TargetError(
            "枚举 UP 主投稿需要 --all（全部视频）或 --limit N（只取前 N 个），"
            "以免误下整个账号的内容。"
        )

    client: WbiClient | None = None
    for url in space_urls:
        _, mid = describe_target(url)
        if client is None:
            client = create_client()
        print(f"[UP主] 正在枚举 mid={mid} 的投稿列表...")
        videos = client.space_videos(mid, limit=args.limit)
        print(f"[UP主] 共获取 {len(videos)} 个视频")
        for item in videos:
            expanded.append(video_url(item["bvid"]))

    return plain + expanded


def print_quality_table(args: argparse.Namespace, cookie_path: Path, state: dict[str, Any]) -> int:
    """--list-qualities：打印 B 站官方档位表（不下载、不需要 ffmpeg）。"""
    try:
        urls = collect_urls(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not urls:
        print("没有找到 B 站链接。", file=sys.stderr)
        return 2

    header = _cookie_field_from_file(cookie_path) if state["logged_in"] else ""
    vip_status: bool | None = None
    logged_in: bool | None = None
    if state["logged_in"]:
        info = fetch_account_info(
            header or None, timeout=args.timeout, proxy=args.proxy
        )
        if info is not None:
            vip_status = bool(info["is_vip"])
            logged_in = bool(info["logged_in"])

    failures = 0
    for index, raw in enumerate(urls, 1):
        url = resolve_short_url(raw, timeout=args.timeout) if is_short_link(raw) else raw
        kind, ident = describe_target(url)
        if kind != "video":
            print(f"[档位] 跳过非视频链接：{url}")
            continue
        print()
        print(f"===== [{index}/{len(urls)}] {url} =====")
        meta, tiers = inspect_quality_tiers(
            ident if kind == "video" else url,
            cookies=header or None,
            timeout=args.timeout,
            proxy=args.proxy,
            vip_status=vip_status,
        )
        if not tiers:
            failures += 1
            print(
                "[档位] 读不到档位表，请改用 --quality 分辨率档位"
                "（2160/1440/1080/720/480/best）。",
                file=sys.stderr,
            )
            continue
        for line in format_quality_tiers(
            tiers, meta=meta, logged_in=logged_in, vip_status=vip_status
        ):
            print(line)
        print(
            "[档位] 用法：--qn <数字> 精确定档，例如 "
            + "、".join(
                f"{tier['qn']}={tier['label']}" for tier in tiers[:3]
            )
        )
    print()
    print(f"[完成] 读取档位 {len(urls) - failures} 个，失败 {failures} 个。")
    return 0 if failures == 0 else 1


def _enumerate_only(
    urls: Sequence[str],
    args: argparse.Namespace,
    downloader: Downloader,
) -> int:
    """--list-formats / --dry-run 的实现：只解析不写文件。"""
    failures = 0
    for index, url in enumerate(urls, 1):
        print()
        print(f"===== [{index}/{len(urls)}] {url} =====")
        info: dict[str, Any] | None = None
        try:
            try:
                info = downloader.probe(url)
            except Exception as exc:  # noqa: BLE001
                # B 站线路偶发抖动：网络层错误只补试一次。
                if not is_network_error(exc):
                    raise
                print("[网络] 网络抖动，重试一次…", file=sys.stderr)
                time.sleep(2.0)
                info = downloader.probe(url)
        except Exception as exc:  # noqa: BLE001 - yt-dlp 的异常层次很杂
            failures += 1
            print(f"[错误] 解析失败：{exc}", file=sys.stderr)
            if is_login_error(exc):
                print_login_expired()
            elif is_proxy_error(exc):
                print(f"[提示] {PROXY_HINT}", file=sys.stderr)
                print(f"[提示] {downloader.proxy_failure_note()}", file=sys.stderr)
            continue

        title = info.get("title") or "（无标题）"
        uploader = info.get("uploader") or info.get("channel") or "未知"
        duration = info.get("duration") or 0
        print(f"[信息] 标题：{title}")
        print(f"[信息] UP 主：{uploader} | 时长：{_format_duration(duration)}")
        if info.get("id"):
            print(f"[信息] 视频 ID：{info.get('id')}")

        if args.list_formats:
            rows = quality_rows(info)
            print(f"[清晰度] 共 {len(rows)} 条轨道：")
            for row in rows:
                print(row)

        target_qn = effective_qn(args)
        if target_qn is not None:
            tiers = None
            fetch_tiers = getattr(downloader, "fetch_tiers", None)
            if callable(fetch_tiers):
                try:
                    tiers = fetch_tiers(url)
                except Exception:  # noqa: BLE001 - 读不到档位表不影响枚举
                    tiers = None
            tier = tier_for_qn(tiers or [], target_qn) if tiers else None
            if tier and tier.get("available"):
                video_id, chosen = resolve_qn_format(info, tier)
                if video_id:
                    codec = (chosen.get("vcodec") or "").split(".")[0] or "?"
                    print(
                        f"[画质] 目标 qn={target_qn}（{tier['label']}）可达："
                        f"实际选用编码 {codec} (id={video_id})"
                    )
                else:
                    print(f"[提示] {describe_qn_gap(tier, target_qn)}")
            else:
                status = getattr(downloader, "account_status", None)
                vip, logged = status() if callable(status) else (None, None)
                print(
                    f"[提示] {describe_qn_gap(tier, target_qn, logged_in=logged, vip_status=vip)}"
                )
            if args.dry_run:
                print("[跳过] --dry-run：不写文件")
            continue

        gap = describe_quality_gap(info, args.quality)
        if gap:
            # 只有真的不达标才去查账号（只读、失败也照常继续）。
            vip_status, logged_in = downloader.account_status()
            gap = (
                describe_quality_gap(
                    info,
                    args.quality,
                    vip_status=vip_status,
                    logged_in=logged_in,
                )
                or gap
            )
            print(f"[提示] {gap}")
        else:
            print(f"[画质] 目标 {args.quality} 可达（最高 {max_video_height(info)}P）")
        if args.dry_run:
            print("[跳过] --dry-run：不写文件")

    return failures


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "未知"
    return f"{total // 60:02d}:{total % 60:02d}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.retries < 0:
        parser.error("--retries 不能小于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.limit < 0:
        parser.error("--limit 不能小于 0")
    if args.login_timeout <= 0:
        parser.error("--login-timeout 必须大于 0")
    if args.all and args.up is None and not args.links and not args.file:
        parser.error("--all 需要配合 UP 主空间链接或 --up 使用")
    if args.qn is not None and args.qn <= 0:
        parser.error("--qn 必须是正整数")

    # --qn 与 --quality 同时出现时，只有一种确定行为：以 --qn 为准，并说明一次。
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    if args.qn is not None and "--quality" in raw_argv:
        print("[提示] 同时指定了 --quality 与 --qn，以 --qn 为准。")

    script_dir = Path(__file__).resolve().parent
    cookie_path = resolve_cookie_file(args, script_dir)
    # --logout / --check-login 是自检入口，文件不存在时应当"如实报告"而不是报用法错误。
    if (
        not (args.logout or args.check_login)
        and args.cookies
        and not Path(args.cookies).expanduser().is_file()
    ):
        parser.error(f"Cookie 文件不存在：{args.cookies}")

    if args.logout:
        target = Path(cookie_path)
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                print(f"[错误] 删除 Cookie 失败：{exc}", file=sys.stderr)
                return 1
            print(f"[登录] 已删除 Cookie 文件：{target}")
        else:
            print(f"[登录] 没有找到 Cookie 文件：{target}")
        return 0

    if args.check_login:
        state = inspect_cookie_file(cookie_path)
        print(f"[登录] Cookie 文件：{state['path']}")
        print(f"[登录] 条目数：{state['count']}")
        print(f"[登录] 含 SESSDATA：{'是' if state['has_sessdata'] else '否'}")
        if state["expiry"]:
            print(f"[登录] SESSDATA 过期时间：{time.ctime(state['expiry'])}")
        if state["expired"]:
            print("[登录] 结论：登录态已过期，请重新 --login-once。")
            return 1
        if state["logged_in"]:
            print("[登录] 结论：登录态有效，可直接下载。")
            return 0
        print_login_help(cookie_path)
        print("[登录] 结论：尚未登录。")
        return 1

    # 代理必须在真正联网（登录/枚举/下载）之前定下来：
    # --proxy > 环境变量 > Windows 系统代理 > 直连。
    effective_proxy, proxy_source = resolve_proxy(args)
    if proxy_source == "system":
        print(f"[网络] 使用系统代理：{effective_proxy}")
    args.proxy = effective_proxy
    _apply_proxy_env(effective_proxy)

    if args.login_once:
        print("[登录] 即将打开可见浏览器窗口，请在窗口里完成 B 站登录。")
        print(f"[登录] 登录完成后会自动把 Cookie 写入：{cookie_path}")
        try:
            exported = run_login(
                cookie_path,
                proxy=args.proxy,
                timeout=args.login_timeout,
            )
        except LoginRequiredError as exc:
            print(f"[登录] {exc}", file=sys.stderr)
            return 1
        except BilibiliError as exc:
            print(f"[登录] 登录失败：{exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n[登录] 已取消。", file=sys.stderr)
            return 130
        print(f"[登录] 已保存登录态：{exported}")
        print("[登录] 之后所有下载都不会再启动浏览器。")
        return 0

    output_dir = _expand_path(args.output)
    args.cookies = Path(cookie_path)

    state = _report_login_state(cookie_path, args)
    if args.require_login and not state["logged_in"]:
        print("[登录] 已指定 --require-login，没有登录态时直接退出。", file=sys.stderr)
        return 1

    # --list-qualities 只是读接口，不需要 ffmpeg / yt-dlp。
    if getattr(args, "list_qualities", False):
        return print_quality_table(args, cookie_path, state)

    try:
        ffmpeg_path = require_ffmpeg(args.ffmpeg, script_dir)
    except FfmpegMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"[工具] ffmpeg：{ffmpeg_path}")

    yt_dlp, download_error = load_ytdlp()

    try:
        urls = collect_urls(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not urls:
        print("没有找到 B 站链接。", file=sys.stderr)
        print(
            '示例：python bilibili_downloader.py "https://www.bilibili.com/video/BV1xx411c7mD"',
            file=sys.stderr,
        )
        return 2

    resolved: list[str] = []
    for raw in urls:
        if is_short_link(raw):
            final = resolve_short_url(raw, timeout=args.timeout)
            if final != raw:
                print(f"[短链] {raw} -> {final}")
            resolved.append(final)
        else:
            resolved.append(raw)

    def create_client() -> WbiClient:
        text = read_cookie_text(cookie_path) or ""
        cookies = parse_netscape_cookies(text)
        return WbiClient(
            timeout=args.timeout,
            proxy=args.proxy,
            cookie_header=cookie_header_for(cookies),
        )

    try:
        targets = _expand_space_targets(resolved, args, create_client)
    except LoginRequiredError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print_login_expired()
        return 1
    except BilibiliError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    unique: list[str] = []
    seen: set[str] = set()
    for url in targets:
        if url in seen:
            print(f"[跳过] 重复链接：{url}")
            continue
        seen.add(url)
        unique.append(url)

    if not unique:
        print("没有可下载的视频。", file=sys.stderr)
        return 1

    if not args.dry_run and not args.list_formats:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{APP_NAME} {VERSION} | 输出目录：{output_dir}")
    print(f"共 {len(unique)} 个待处理视频，目标清晰度：{args.quality}")

    downloader = Downloader(
        args,
        output_dir,
        yt_dlp,
        download_error,
        prepare_cookiefile(cookie_path if state["logged_in"] else None),
        ffmpeg_path,
        proxy=args.proxy,
        system_proxy=detect_system_proxy(),
    )

    if args.list_formats or args.dry_run:
        failures = _enumerate_only(unique, args, downloader)
        print()
        print(f"[完成] 枚举 {len(unique)} 个，失败 {failures} 个。")
        return 0 if failures == 0 else 1

    succeeded = 0
    failed = 0
    login_failure = False
    for index, url in enumerate(unique, 1):
        print()
        print(f"===== [{index}/{len(unique)}] {url} =====")
        result: int | None = None
        failure: BaseException | None = None
        for attempt in range(2):
            try:
                result = downloader.download(url)
                failure = None
                break
            except KeyboardInterrupt:
                print("\n已取消。", file=sys.stderr)
                return 130
            except Exception as exc:  # noqa: BLE001 - 按错误类型分流处理
                failure = exc
                # B 站线路偶发抖动：网络层错误只补试一次，不做无限重试。
                if attempt == 0 and is_network_error(exc):
                    print("[网络] 网络抖动，重试一次…", file=sys.stderr)
                    time.sleep(2.0)
                    continue
                break

        if failure is not None:
            failed += 1
            login_failure = login_failure or is_login_error(failure)
            print(f"[错误] 下载失败：{failure}", file=sys.stderr)
            if is_proxy_error(failure):
                print(f"[提示] {PROXY_HINT}", file=sys.stderr)
                print(f"[提示] {downloader.proxy_failure_note()}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if result == 0:
            succeeded += 1
        else:
            failed += 1
            print(f"[错误] yt-dlp 返回码：{result}", file=sys.stderr)

    print()
    print(f"[完成] 成功 {succeeded} 个，失败 {failed} 个。")
    if login_failure:
        print_login_expired()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
