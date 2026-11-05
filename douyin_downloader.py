#!/usr/bin/env python3
"""抖音视频下载脚本（真实浏览器 + yt-dlp）。

默认使用本机 Edge/Chrome/Chromium 的独立浏览器配置解析单个视频，
不读取日常浏览器 Cookie；播放列表或浏览器不可用时回退到 yt-dlp。

浏览器默认以无头模式运行（不弹窗、不抢焦点）；被抖音风控拦截时会自动降级为
屏幕外窗口重试。浏览器配置默认持久化在脚本所在目录的 .browser-profile 中，
首次运行若没有登录态会提示登录一次，之后自动复用。

用法示例：
    python douyin_downloader.py "https://v.douyin.com/xxxx/"
    python douyin_downloader.py "7.43 复制打开抖音... https://v.douyin.com/xxxx/"
    python douyin_downloader.py --file links.txt
    python douyin_downloader.py --engine browser "https://v.douyin.com/xxxx/"

仅供个人备份和学习使用。请遵守抖音用户协议、著作权法及当地法律法规，
不要下载、传播无权使用的视频。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from browser_engine import (
    PERMANENT_VIDEO_ERROR_MARKERS,
    BrowserDownloader,
    BrowserError,
    build_qualities,
    default_profile_dir,
    detect_system_proxy,
    recommended_gear,
)

APP_NAME = "douyin-downloader"
VERSION = "2.3.0"
DEFAULT_OUTPUT = "downloads"
DEFAULT_TEMPLATE = "%(id)s_%(title).80s.%(ext)s"
AUTHOR_TEMPLATE = "%(uploader,creator|douyin).50s/" + DEFAULT_TEMPLATE

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# 判断抖音登录态的关键 Cookie（任意一个存在即视为已登录）。
DOUYIN_LOGIN_COOKIES = ("sessionid|sessionid_ss",)

DOUYIN_DOMAINS = ("douyin.com", "iesdouyin.com")
SHORT_LINK_HOST = "v.douyin.com"

# 只匹配 URL 允许的 ASCII 字符，避免把紧跟其后的中文说明吞进链接。
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)
TRAILING_PUNCT = "，。！？、；：）】》」』…,.!?;:)]}>'\"”’"

AWEME_ID_PATTERNS = (
    re.compile(r"/video/(\d{6,})"),
    re.compile(r"/share/video/(\d{6,})"),
    re.compile(r"/note/(\d{6,})"),
    re.compile(r"[?&]modal_id=(\d{6,})"),
    re.compile(r"[?&]aweme_id=(\d{6,})"),
)

NOTE_URL_RE = re.compile(r"/(?:share/)?note/(\d{6,})")

COOKIE_ERROR_MARKERS = (
    "fresh cookies",
    "cookies are needed",
    "cookie",
    "login",
    "sign in",
    "log in",
    "failed to load cookies",
    "could not copy",
    "cookie database",
    "登录",
    "需要验证",
    "verify",
)

BROWSER_COOKIE_ERROR_MARKERS = COOKIE_ERROR_MARKERS + (
    "permission denied",
    "winerror 5",
    "拒绝访问",
    "temporary directory",
    "could not copy chrome cookie",
)

SUPPORTED_BROWSERS = {
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
}


class ResolutionError(RuntimeError):
    """短链接或分享链接解析失败。"""


def extract_urls(text: str) -> list[str]:
    """从分享文案、命令行参数或文本中提取抖音链接并按出现顺序去重。"""
    urls: list[str] = []
    seen: set[str] = set()

    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCT)
        if url.lower().startswith("http://"):
            url = "https://" + url[7:]

        if not is_douyin_url(url):
            continue

        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            urls.append(url)

    return urls


def is_douyin_url(url: str) -> bool:
    """判断 URL 是否属于抖音域名，避免脚本被用来请求任意地址。"""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False

    return any(
        host == domain or host.endswith("." + domain)
        for domain in DOUYIN_DOMAINS
    )


def extract_aweme_id(url: str) -> str | None:
    """从视频页、分享页或 modal_id 参数中提取作品 ID。"""
    for pattern in AWEME_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)

    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None

    for key in ("modal_id", "aweme_id", "item_id"):
        values = query.get(key)
        if values and values[0].isdigit():
            return values[0]

    return None


def extract_sec_uid(url: str) -> str | None:
    """从抖音用户主页 URL 中提取 sec_uid。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = re.search(r"/user/([^/?#]+)", path)
    if not match:
        return None
    return match.group(1)


def canonical_video_url(url: str) -> str:
    """把分享页 URL 规范成 yt-dlp 能识别的视频页 URL。"""
    aweme_id = extract_aweme_id(url)
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"
    return url


def resolve_short_url(url: str, timeout: float = 20.0) -> str:
    """跟随 v.douyin.com 短链接跳转，返回最终 URL。"""
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }

    def fetch(method: str) -> str:
        request = Request(url, headers=headers, method=method)
        with urlopen(request, timeout=timeout) as response:
            # HEAD 通常没有响应体；GET 只需读一小段，避免下载整页。
            if method != "HEAD":
                response.read(2048)
            return response.geturl()

    try:
        final_url = fetch("HEAD")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        final_url = ""

    if not final_url or final_url == url or not extract_aweme_id(final_url):
        try:
            final_url = fetch("GET")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ResolutionError(str(exc)) from exc

    return final_url


def prepare_url(url: str, timeout: float = 20.0) -> str:
    """解析短链接并返回可交给 yt-dlp 的规范 URL。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError as exc:
        raise ResolutionError(f"链接格式无效：{url}") from exc

    if host == SHORT_LINK_HOST:
        final_url = resolve_short_url(url, timeout=timeout)
        if NOTE_URL_RE.search(final_url):
            raise ResolutionError("这是图文/图集作品，当前脚本只下载视频。")
        if not extract_aweme_id(final_url):
            raise ResolutionError(f"短链接没有指向视频：{final_url}")
        return canonical_video_url(final_url)

    if NOTE_URL_RE.search(url):
        raise ResolutionError("这是图文/图集作品，当前脚本只下载视频。")

    return canonical_video_url(url)


def parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """解析 chrome、edge:Default、chrome:Profile 1 等浏览器 Cookie 参数。

    返回 yt-dlp 需要的 (browser, profile, keyring, container) 元组。
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("浏览器名称不能为空")

    main, _, container = spec.partition("::")
    browser_part, _, profile = main.partition(":")
    browser, _, keyring = browser_part.partition("+")

    browser = browser.strip().lower()
    if not browser:
        raise ValueError("浏览器名称不能为空")

    return (
        browser,
        profile.strip() or None,
        keyring.strip() or None,
        container.strip() or None,
    )


def parse_rate(value: str) -> int:
    """把 500K、2M、1.5MiB 等限速写法转换成字节/秒。"""
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?(?:/s)?\s*",
        value,
        re.I,
    )
    if not match:
        raise argparse.ArgumentTypeError("限速格式示例：500K、2M、1.5M")

    number = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return max(1, int(number * multipliers[unit]))


def _browser_data_exists(paths: Iterable[Path]) -> bool:
    try:
        return any(path.exists() for path in paths)
    except OSError:
        return False


def browser_candidates() -> list[str]:
    """按常见程度返回本机可能安装的浏览器名称。"""
    home = Path.home()
    local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    app_data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))

    if sys.platform == "win32":
        order = ["edge", "chrome", "firefox", "brave", "vivaldi", "chromium", "opera", "whale"]
        paths = {
            "edge": [local_app_data / "Microsoft" / "Edge" / "User Data"],
            "chrome": [local_app_data / "Google" / "Chrome" / "User Data"],
            "firefox": [app_data / "Mozilla" / "Firefox" / "Profiles"],
            "brave": [local_app_data / "BraveSoftware" / "Brave-Browser" / "User Data"],
            "vivaldi": [local_app_data / "Vivaldi" / "User Data"],
            "chromium": [local_app_data / "Chromium" / "User Data"],
            "opera": [app_data / "Opera Software" / "Opera Stable"],
            "whale": [local_app_data / "Naver" / "Naver Whale" / "User Data"],
        }
    elif sys.platform == "darwin":
        order = ["safari", "chrome", "edge", "firefox", "brave", "vivaldi", "opera", "chromium"]
        support = home / "Library" / "Application Support"
        paths = {
            "safari": [home / "Library" / "Safari"],
            "chrome": [support / "Google" / "Chrome"],
            "edge": [support / "Microsoft Edge"],
            "firefox": [support / "Firefox" / "Profiles"],
            "brave": [support / "BraveSoftware" / "Brave-Browser"],
            "vivaldi": [support / "Vivaldi"],
            "opera": [support / "com.operasoftware.Opera"],
            "chromium": [support / "Chromium"],
        }
    else:
        order = ["chrome", "chromium", "firefox", "brave", "edge", "vivaldi", "opera"]
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        paths = {
            "chrome": [config / "google-chrome"],
            "chromium": [config / "chromium"],
            "firefox": [home / ".mozilla" / "firefox"],
            "brave": [config / "BraveSoftware" / "Brave-Browser"],
            "edge": [config / "microsoft-edge"],
            "vivaldi": [config / "vivaldi"],
            "opera": [config / "opera"],
        }

    return [name for name in order if _browser_data_exists(paths[name])]


def is_cookie_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in COOKIE_ERROR_MARKERS)


def is_browser_cookie_error(error: BaseException) -> bool:
    """浏览器 Cookie 读取阶段的错误，允许继续尝试下一个浏览器。"""
    message = str(error).lower()
    return any(marker in message for marker in BROWSER_COOKIE_ERROR_MARKERS)


BROWSER_PERMANENT_ERROR_MARKERS = PERMANENT_VIDEO_ERROR_MARKERS + (
    "没有从视频信息中找到",
)


def is_permanent_browser_error(error: BaseException) -> bool:
    """视频本身不可用，回退 yt-dlp 也没有意义。"""
    message = str(error)
    return any(marker in message for marker in BROWSER_PERMANENT_ERROR_MARKERS)


def build_ydl_options(
    args: argparse.Namespace,
    output_dir: Path,
    cookie_browser: tuple[str, str | None, str | None, str | None] | None = None,
):
    template = args.template or (AUTHOR_TEMPLATE if args.folder_by_author else DEFAULT_TEMPLATE)

    options = {
        "paths": {"home": str(output_dir)},
        "outtmpl": {"default": template},
        "format": args.format,
        "merge_output_format": "mp4",
        "noplaylist": not args.playlist,
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
        "http_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
        "writethumbnail": bool(args.thumbnail),
        "writeinfojson": bool(args.info),
        "clean_infojson": True,
    }

    if args.cookies:
        options["cookiefile"] = str(args.cookies)

    if cookie_browser:
        options["cookiesfrombrowser"] = cookie_browser

    if args.limit_rate:
        options["ratelimit"] = args.limit_rate

    # Python / yt-dlp 不读注册表里的 Windows 系统代理，这里替它们接一下。
    # 浏览器引擎（BrowserSession）自己就走系统代理，不需要也不应该被这里影响。
    proxy = args.proxy or (
        None if getattr(args, "no_system_proxy", False) else detect_system_proxy()
    )
    if proxy:
        options["proxy"] = proxy

    if args.list_formats:
        options["listformats"] = True
        options["skip_download"] = True
    elif args.dry_run:
        options["skip_download"] = True

    if not args.force and not args.no_archive and not args.dry_run:
        options["download_archive"] = str(output_dir / ".douyin-archive.txt")

    return options


class Downloader:
    """封装单次下载以及浏览器 Cookie 的自动回退逻辑。"""

    def __init__(
        self,
        args: argparse.Namespace,
        output_dir: Path,
        yt_dlp,
        download_error,
    ) -> None:
        self.args = args
        self.output_dir = output_dir
        self.yt_dlp = yt_dlp
        self.download_error = download_error
        self.selected_browser: str | None = None
        self.candidates = [] if args.no_browser_cookies else browser_candidates()

    def _download(self, url: str, cookie_browser=None) -> int:
        options = build_ydl_options(self.args, self.output_dir, cookie_browser)
        with self.yt_dlp.YoutubeDL(options) as ydl:
            return ydl.download([url])

    def download(self, url: str) -> int:
        # 1. 用户显式指定 Cookie 文件或浏览器时，只使用该来源。
        if self.args.cookies:
            try:
                return self._download(url)
            except self.download_error as exc:
                if is_browser_cookie_error(exc):
                    raise self.download_error(
                        "Cookie 文件不可用，或其中缺少可访问该视频的抖音登录态。"
                        "请确认它是 Netscape 格式，并重新导出。"
                    ) from exc
                raise

        if self.args.cookies_from_browser:
            spec = self.args.cookies_from_browser
            if spec.lower() != "auto":
                try:
                    return self._download(url, parse_browser_spec(spec))
                except self.download_error as exc:
                    if is_browser_cookie_error(exc):
                        raise self.download_error(
                            f"无法从 {spec} 读取 Cookie。请确认浏览器已关闭，"
                            "并已在该浏览器打开过抖音；也可以改用 --cookies cookies.txt。"
                        ) from exc
                    raise
            # auto：直接走下面的浏览器回退逻辑。

        # 2. 已经成功找到可用浏览器时，后续视频直接复用。
        if self.selected_browser:
            return self._download(
                url,
                (self.selected_browser, None, None, None),
            )

        # 3. 先尝试无 Cookie。部分公开视频不需要 Cookie，能省去读取浏览器的步骤。
        try:
            return self._download(url)
        except self.download_error as exc:
            if not is_cookie_error(exc):
                raise
            first_error = exc

        if self.args.no_browser_cookies:
            raise self.download_error(
                "该视频需要抖音 Cookie。请先在浏览器打开抖音，"
                "然后使用 --cookies-from-browser edge 或 --cookies cookies.txt。"
            ) from first_error

        if not self.candidates:
            raise self.download_error(
                "该视频需要抖音 Cookie，但没有检测到可用的浏览器。"
                "请使用 --cookies cookies.txt 指定导出的 Cookie 文件。"
            ) from first_error

        print(
            f"[信息] 抖音要求 Cookie，正在尝试读取浏览器 Cookie："
            f"{', '.join(self.candidates)}"
        )

        # 4. 依次尝试本机浏览器的 Cookie。浏览器没装、数据库被占用、
        #    或没有抖音登录态时，继续尝试下一个。
        last_error: BaseException = first_error
        for browser in self.candidates:
            try:
                result = self._download(
                    url,
                    (browser, None, None, None),
                )
            except self.download_error as exc:
                last_error = exc
                if is_browser_cookie_error(exc):
                    if self.args.verbose:
                        print(f"[调试] 读取 {browser} Cookie 失败：{exc}", file=sys.stderr)
                    continue
                raise

            self.selected_browser = browser
            print(f"[Cookie] 已使用 {browser} 浏览器的 Cookie")
            return result

        raise self.download_error(
            "无法用浏览器 Cookie 访问该视频。请确认已在本机浏览器打开过抖音，"
            "或使用 --cookies cookies.txt 指定导出的 Cookie 文件。"
        ) from last_error


def read_input_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SystemExit(f"无法读取链接文件 {path}：{exc}") from exc


def collect_urls(args: argparse.Namespace) -> list[str]:
    parts = list(args.links)

    if args.file:
        parts.append(read_input_file(args.file))

    text = "\n".join(parts)
    urls = extract_urls(text)

    if not urls and not sys.stdin.isatty():
        urls = extract_urls(sys.stdin.read())

    if not urls and sys.stdin.isatty():
        print("请粘贴抖音分享链接或分享文案，然后按回车：")
        try:
            paste = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        urls = extract_urls(paste)

    return urls


def print_cookie_help() -> None:
    print()
    print("没有拿到视频数据。默认的浏览器引擎通常不需要手动导出 Cookie；")
    print("如果你使用了 --engine yt-dlp，可以依次尝试：")
    print("  1. 先在 Edge / Chrome 打开 https://www.douyin.com 并保持登录；")
    print('  2. 运行：python douyin_downloader.py --engine yt-dlp --cookies-from-browser edge "链接"')
    print("  3. 如果浏览器 Cookie 不可用，用浏览器扩展导出 Netscape 格式 cookies.txt，")
    print('     再运行：python douyin_downloader.py --engine yt-dlp --cookies cookies.txt "链接"')
    print("  4. 更新解析器：python -m pip install -U yt-dlp")


def print_browser_help() -> None:
    print()
    print("浏览器引擎没有拿到视频数据。可以检查：")
    print("  1. 本机是否安装了 Edge / Chrome / Chromium；")
    print("  2. 无头模式被风控拦截时脚本会自动降级为屏幕外窗口；仍失败可以加 --visible")
    print("     打开可见窗口观察页面提示；")
    print("  3. 用 --browser 指定浏览器路径，例如：")
    print('     python douyin_downloader.py --browser "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" "链接"')
    print("  4. 如果视频是私密、已删除或图文作品，脚本无法下载。")
    print(f"  5. 登录态失效时删掉浏览器配置目录（默认 .browser-profile）再运行，会重新提示登录一次。")


def resolve_window_mode(args: argparse.Namespace) -> str:
    """根据 --visible 决定浏览器窗口模式（默认无头，不弹窗）。"""
    return "visible" if getattr(args, "visible", False) else "headless"


def resolve_profile_dir(args: argparse.Namespace) -> Path | None:
    """决定浏览器配置目录。

    返回 None 表示使用一次性临时配置（--temp-profile）；否则优先用
    --browser-profile 指定的目录，默认是脚本所在目录下的 .browser-profile。
    """
    if getattr(args, "temp_profile", False):
        return None
    explicit = getattr(args, "browser_profile", None)
    if explicit:
        raw = os.path.expandvars(os.path.expanduser(str(explicit)))
        return Path(raw).resolve()
    return default_profile_dir()


def load_ytdlp():
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        print("缺少依赖 yt-dlp。请先运行：", file=sys.stderr)
        print(f'  "{sys.executable}" -m pip install -r requirements.txt', file=sys.stderr)
        raise SystemExit(2) from exc

    return yt_dlp, DownloadError


def gear_format_note(args: argparse.Namespace) -> str | None:
    """--gear 与 --format 冲突时返回那一行说明，没有冲突返回 None。"""
    gear = getattr(args, "gear", None)
    if gear and getattr(args, "format", "best") != "best":
        return f"[提示] 已指定 --gear {gear}，优先于 --format {args.format}（--format 被忽略）"
    return None


def quality_query_args(
    *,
    browser: str | None = None,
    profile_dir: str | Path | None = None,
    window_mode: str = "headless",
    timeout: float = 30.0,
    retries: int = 2,
) -> argparse.Namespace:
    """构造 BrowserDownloader 需要的最小参数集（只读查询用不到下载相关字段）。"""
    return argparse.Namespace(
        browser=browser,
        browser_profile=(
            Path(profile_dir).expanduser() if profile_dir else default_profile_dir()
        ),
        browser_wait=6.0,
        window_mode=window_mode,
        visible=str(window_mode).strip().lower() == "visible",
        timeout=timeout,
        proxy=None,
        retries=retries,
        login_timeout=300.0,
    )


def describe_quality_error(error: BaseException) -> str:
    """把读取档位时的异常翻译成中文原因（风控/登录、视频不存在、网络、其它）。"""
    text = str(error).strip() or error.__class__.__name__
    lowered = text.lower()
    if (
        "uifid" in lowered
        or "blocked by" in lowered
        or "captcha" in lowered
        or "verify" in lowered
        or "风控" in text
        or "登录" in text
    ):
        return (
            f"被抖音风控拦截或登录态失效：{text}\n"
            "提示：可以先加 --visible 观察页面提示，或完成一次登录后重试。"
        )
    if any(marker in text for marker in BROWSER_PERMANENT_ERROR_MARKERS):
        return f"视频不存在或不可访问：{text}"
    if any(
        marker in lowered
        for marker in (
            "timed out",
            "timeout",
            "urlopen",
            "connection",
            "网络",
            "调试端口",
            "超时",
            "proxy",
        )
    ):
        return f"网络或浏览器连接失败：{text}"
    return f"读取清晰度失败：{text}"


def list_qualities(
    url: str,
    *,
    browser: str | None = None,
    profile_dir: str | Path | None = None,
    window_mode: str = "headless",
    timeout: float = 30.0,
) -> dict:
    """读取一个抖音视频当前可选的清晰度档位。

    返回：
    {
      "ok": bool,
      "error": str | None,          # ok=False 时给中文原因
      "title": str, "author": str, "duration": float,
      "qualities": [                 # 按码率从高到低
         {"gear": "720_2_1", "label": "720P", "codec": "h265",
          "bitrate": 824, "height": 1280, "notes": ""},
      ],
      "recommended": "720_2_1",      # 默认选中项（最高档），无则 None
    }

    只读：不下载、不写下载记录、不弹登录窗口。任何失败都通过 ok=False 加中文
    error 返回，不向调用方抛异常。无头被风控拦截时沿用引擎的屏幕外窗口降级。
    """
    payload: dict[str, Any] = {
        "ok": False,
        "error": None,
        "title": "",
        "author": "",
        "duration": 0.0,
        "qualities": [],
        "recommended": None,
    }

    try:
        final_url = prepare_url(url, timeout=timeout)
    except ResolutionError as exc:
        payload["error"] = f"链接解析失败：{exc}"
        return payload
    except Exception as exc:  # noqa: BLE001 - 网络异常也要变成中文原因
        payload["error"] = describe_quality_error(exc)
        return payload

    aweme_id = extract_aweme_id(final_url)
    if not aweme_id:
        payload["error"] = (
            "这个链接不是单个抖音视频（需要 /video/<作品ID> 或 v.douyin.com 短链）"
        )
        return payload

    args = quality_query_args(
        browser=browser,
        profile_dir=profile_dir,
        window_mode=window_mode,
        timeout=timeout,
    )
    downloader: BrowserDownloader | None = None
    detail: dict[str, Any] | None = None
    try:
        downloader = BrowserDownloader(
            Path(tempfile.gettempdir()),
            args,
            login_cookies=DOUYIN_LOGIN_COOKIES,
        )
        downloader.start(prompt_login=False)
        detail = downloader.fetch_detail(aweme_id)
    except BrowserError as exc:
        payload["error"] = describe_quality_error(exc)
        return payload
    except Exception as exc:  # noqa: BLE001 - 保证界面永远拿到字典
        payload["error"] = describe_quality_error(exc)
        return payload
    finally:
        if downloader is not None:
            try:
                downloader.close()
            except Exception:  # noqa: BLE001
                pass

    data = detail or {}
    video = data.get("video") or {}
    qualities = build_qualities(data)
    payload.update(
        title=str(data.get("desc") or "").strip(),
        author=str((data.get("author") or {}).get("nickname") or ""),
        duration=round(int(video.get("duration") or 0) / 1000, 3),
        qualities=qualities,
        recommended=recommended_gear(qualities),
    )
    if not qualities:
        payload["error"] = "没有解析到任何清晰度档位（可能是图文作品或接口返回不完整）"
        return payload
    payload["ok"] = True
    return payload


def print_quality_table(payload: dict) -> None:
    """把 list_qualities 的结果打印成可读表格（命令行与界面共用同一份数据）。"""
    if payload.get("title"):
        print(f"  标题：{str(payload['title'])[:80]}")
    if payload.get("author"):
        print(f"  作者：{payload['author']}")
    if payload.get("duration"):
        print(f"  时长：{payload['duration']:.1f} 秒")

    qualities = payload.get("qualities") or []
    if not qualities:
        print("  可用清晰度：无")
        return

    recommended = payload.get("recommended")
    print("  可用清晰度档位（按码率从高到低）：")
    for item in qualities:
        star = "*" if item.get("gear") and item.get("gear") == recommended else " "
        bitrate = f"{item['bitrate']} kbps" if item.get("bitrate") else "-"
        height = f"{item['height']}p" if item.get("height") else "-"
        gear = item.get("gear") or "（默认流）"
        line = (
            f"    {star} {item['label']:<6} {item['codec']:<5} "
            f"{bitrate:>10}  {height:>7}  {gear}"
        )
        if item.get("notes"):
            line += f"  {item['notes']}"
        print(line)

    top = max(qualities, key=lambda q: (q.get("height") or 0, q.get("bitrate") or 0))
    top_gear = f"（gear {top['gear']}）" if top.get("gear") else "（默认流，无 gear）"
    print(f"  实际最高档：{top['label']} {top['codec']}{top_gear}")
    if recommended:
        print(f"  推荐档位：{recommended}（下载时加 --gear {recommended}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin_downloader.py",
        description="抖音视频批量下载脚本（真实浏览器解析 + yt-dlp 回退，仅供个人备份）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python douyin_downloader.py "https://v.douyin.com/xxxx/"\n'
            '  python douyin_downloader.py "复制打开抖音... https://v.douyin.com/xxxx/"\n'
            "  python douyin_downloader.py --file links.txt\n"
            '  python douyin_downloader.py --engine browser "https://v.douyin.com/xxxx/"\n'
            '  python douyin_downloader.py --engine yt-dlp --playlist "主页链接"\n'
            "\n"
            "默认 auto 引擎：单个视频用本机 Edge/Chrome 的独立浏览器配置解析，\n"
            "不会读取你日常浏览器的 Cookie；主页/合集等播放列表交给 yt-dlp。\n"
            "浏览器默认无头运行，不弹窗、不影响当前工作；被风控拦截时自动降级为\n"
            "屏幕外窗口重试。配置目录默认是脚本目录下的 .browser-profile，首次运行\n"
            "没有登录态时会提示登录一次，之后自动复用。\n"
        ),
    )

    parser.add_argument(
        "links",
        nargs="*",
        help="抖音分享链接、分享文案，或包含链接的文本（可一次传多个）",
    )
    parser.add_argument(
        "--file",
        type=Path,
        metavar="FILE",
        help="从文本文件读取链接；文件里可以有分享文案，脚本会自动提取 URL",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        metavar="DIR",
        help=f"保存目录（默认：{DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--format",
        default="best",
        metavar="FORMAT",
        help="格式选择（默认：best；例如 best[height<=1080]）",
    )
    parser.add_argument(
        "--prefer-h264",
        action="store_true",
        help="优先 H.264（兼容性更好，但可能只能选到较低分辨率）",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "browser", "yt-dlp"),
        default="auto",
        help="下载引擎：auto 单视频用浏览器、播放列表用 yt-dlp；browser 强制浏览器；yt-dlp 强制旧引擎",
    )
    parser.add_argument(
        "--browser",
        metavar="PATH",
        help="Chromium 系浏览器可执行文件路径（默认自动查找 Edge/Chrome/Chromium）",
    )
    parser.add_argument(
        "--browser-profile",
        metavar="DIR",
        help="持久化浏览器配置目录（默认：脚本目录下的 .browser-profile，登录一次后可复用）",
    )
    parser.add_argument(
        "--temp-profile",
        action="store_true",
        help="使用一次性临时浏览器配置（每次运行都要重新登录，旧行为）",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="强制使用可见浏览器窗口（调试用；默认无头运行，不弹窗）",
    )
    parser.add_argument(
        "--browser-headless",
        action="store_true",
        help="无头模式（现在已是默认行为，保留该参数以兼容旧命令）",
    )
    parser.add_argument(
        "--browser-wait",
        type=float,
        default=8.0,
        metavar="SEC",
        help="浏览器打开抖音首页后等待 Cookie 生成的秒数（默认：8）",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="只列出可用清晰度，不下载",
    )
    parser.add_argument(
        "--list-qualities",
        action="store_true",
        help="列出抖音提供的清晰度档位（含 gear 名、码率与推荐项），不下载",
    )
    parser.add_argument(
        "--gear",
        metavar="GEAR",
        help="按 gear_name 精确下载某一档，例如 720_2_1（优先于 --format；用 --list-qualities 查看）",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        metavar="FILE",
        help="Netscape 格式的 cookies.txt（仅 --engine yt-dlp 使用）",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER[:PROFILE]",
        help="从浏览器读取 Cookie（仅 --engine yt-dlp；auto 表示自动尝试）",
    )
    parser.add_argument(
        "--no-browser-cookies",
        action="store_true",
        help="禁止 yt-dlp 自动读取本机浏览器 Cookie（不影响浏览器引擎）",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="同时保存视频信息 .info.json",
    )
    parser.add_argument(
        "--thumbnail",
        action="store_true",
        help="同时保存封面图",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="下载用户主页的全部公开作品（需要浏览器引擎，可配合 --limit）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="配合 --all 使用，最多下载 N 个视频（0 表示全部）",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="允许下载主页/合集等播放列表（此模式使用 yt-dlp）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略下载记录并覆盖已有文件",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="不写入下载记录（默认会跳过已经下载过的视频）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析并显示信息，不下载文件",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="不显示下载进度条",
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
        help="不使用 Windows 系统代理（默认在 yt-dlp 引擎里自动探测并使用）",
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
        "--folder-by-author",
        action="store_true",
        help="按作者名建立子目录",
    )
    parser.add_argument(
        "--template",
        metavar="TEMPLATE",
        help="yt-dlp 输出模板（仅 --engine yt-dlp 生效）",
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.retries < 0:
        parser.error("--retries 不能小于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.browser_wait <= 0:
        parser.error("--browser-wait 必须大于 0")
    if args.engine == "browser" and args.playlist:
        parser.error("--engine browser 只支持单个视频，不能和 --playlist 一起使用")
    if args.all and args.playlist:
        parser.error("--all 和 --playlist 不能同时使用")
    if args.all and args.engine == "yt-dlp":
        parser.error("--all 需要浏览器引擎，请使用默认 auto 或 --engine browser")
    if args.limit < 0:
        parser.error("--limit 不能小于 0")
    if args.browser_profile and args.temp_profile:
        parser.error("--browser-profile 和 --temp-profile 只能选一个")
    if args.gear and args.engine == "yt-dlp":
        parser.error("--gear 需要浏览器引擎，请使用默认 auto 或 --engine browser")
    gear_note = gear_format_note(args)
    if gear_note:
        print(gear_note)
    if args.browser:
        browser_path = Path(args.browser).expanduser()
        if not browser_path.is_file():
            parser.error(f"浏览器不存在：{args.browser}")
        args.browser = str(browser_path)
    if args.cookies and not args.cookies.expanduser().is_file():
        parser.error(f"Cookie 文件不存在：{args.cookies}")

    if args.cookies and args.cookies_from_browser:
        parser.error("--cookies 和 --cookies-from-browser 只能选一个")

    if args.cookies_from_browser and args.no_browser_cookies:
        parser.error("--cookies-from-browser 和 --no-browser-cookies 不能同时使用")

    if args.cookies_from_browser and args.cookies_from_browser.lower() != "auto":
        try:
            browser, _, _, _ = parse_browser_spec(args.cookies_from_browser)
        except ValueError as exc:
            parser.error(f"浏览器参数无效：{exc}")
        if browser not in SUPPORTED_BROWSERS:
            parser.error(
                f"不支持的浏览器：{browser}（可选：{', '.join(sorted(SUPPORTED_BROWSERS))}, auto）"
            )

    args.cookies = args.cookies.expanduser() if args.cookies else None
    args.output = os.path.expandvars(os.path.expanduser(args.output))
    args.window_mode = resolve_window_mode(args)
    args.browser_profile = resolve_profile_dir(args)
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_urls = collect_urls(args)
    if not raw_urls:
        print("没有找到抖音链接。", file=sys.stderr)
        print('示例：python douyin_downloader.py "https://v.douyin.com/xxxx/"', file=sys.stderr)
        return 2

    yt_dlp, download_error = load_ytdlp()

    print(f"{APP_NAME} {VERSION} | 输出目录：{output_dir}")
    print(f"共收到 {len(raw_urls)} 个链接，开始解析...")

    prepared: list[str] = []
    failed = 0
    seen: set[str] = set()

    for index, raw_url in enumerate(raw_urls, 1):
        try:
            final_url = prepare_url(raw_url, timeout=args.timeout)
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return 130
        except ResolutionError as exc:
            failed += 1
            print(f"[{index}/{len(raw_urls)}] 解析失败：{raw_url}", file=sys.stderr)
            print(f"    原因：{exc}", file=sys.stderr)
            continue

        if final_url in seen:
            print(f"[{index}/{len(raw_urls)}] 跳过重复链接：{final_url}")
            continue

        seen.add(final_url)
        prepared.append(final_url)
        if final_url != raw_url:
            print(f"[{index}/{len(raw_urls)}] {raw_url}\n    -> {final_url}")

    if not prepared:
        print("没有可下载的链接。", file=sys.stderr)
        return 1

    if args.list_qualities:
        failures = 0
        for index, url in enumerate(prepared, 1):
            print()
            print(f"===== [{index}/{len(prepared)}] {url} =====")
            payload = list_qualities(
                url,
                browser=args.browser,
                profile_dir=args.browser_profile,
                window_mode=args.window_mode,
                timeout=args.timeout,
            )
            print_quality_table(payload)
            if not payload["ok"]:
                failures += 1
                if payload["error"]:
                    print(f"  读取失败：{payload['error']}", file=sys.stderr)
        print()
        print("=" * 60)
        if failures:
            print(f"清晰度读取完成：{failures}/{len(prepared)} 个失败")
            return 1
        print(f"清晰度读取完成：{len(prepared)} 个视频全部成功")
        return 0

    downloader = Downloader(args, output_dir, yt_dlp, download_error)
    succeeded = 0
    cookie_failure = False
    browser_failure = False

    browser_targets = [
        url for url in prepared
        if args.engine != "yt-dlp" and not args.playlist and extract_aweme_id(url)
    ]
    if args.engine == "browser" and not browser_targets and not args.all:
        print(
            "--engine browser 只支持单个视频；用户主页请加 --all，"
            "主页/合集也可以使用 --engine yt-dlp --playlist。",
            file=sys.stderr,
        )
        return 2

    browser_dl: BrowserDownloader | None = None
    if browser_targets or args.all:
        try:
            browser_dl = BrowserDownloader(
                output_dir,
                args,
                login_cookies=DOUYIN_LOGIN_COOKIES,
            ).start()
            print(f"[引擎] 浏览器解析：{browser_dl.browser_path}")
            if not args.cookies:
                exported = browser_dl.export_cookies(output_dir / "cookies.txt")
                if exported is not None:
                    args.cookies = exported
                    print(f"[Cookie] 已导出浏览器 Cookie：{exported}")
            if args.cookies or args.cookies_from_browser:
                print("[提示] 浏览器引擎不需要 Cookie 参数；它们只在 yt-dlp 回退时生效。")
        except BrowserError as exc:
            if args.engine == "browser" or args.all:
                print(f"浏览器启动失败：{exc}", file=sys.stderr)
                return 1
            print(f"[提示] 浏览器不可用：{exc}；改用 yt-dlp。")

    if args.all:
        if browser_dl is None:
            print("--all 需要浏览器引擎，但浏览器没有启动。", file=sys.stderr)
            return 1

        expanded: list[str] = []
        found_user = False
        for url in prepared:
            sec_uid = extract_sec_uid(url)
            if sec_uid and not extract_aweme_id(url):
                found_user = True
                print(f"[用户] 正在收集 {url} 的作品列表...")
                try:
                    videos = browser_dl.collect_user_videos(sec_uid, limit=args.limit)
                except BrowserError as exc:
                    print(f"[用户] 收集失败：{exc}", file=sys.stderr)
                    continue
                print(f"[用户] 共找到 {len(videos)} 个视频")
                expanded.extend(videos)
            else:
                expanded.append(url)

        if not found_user:
            print("--all 没有在输入中找到用户主页链接。", file=sys.stderr)
            return 2

        unique: list[str] = []
        seen_videos: set[str] = set()
        for url in expanded:
            if url not in seen_videos:
                seen_videos.add(url)
                unique.append(url)
        prepared = unique

        if not prepared:
            print("没有从用户主页收集到可下载的视频。", file=sys.stderr)
            return 1

    try:
        for index, url in enumerate(prepared, 1):
            print()
            print(f"===== [{index}/{len(prepared)}] {url} =====")

            aweme_id = extract_aweme_id(url)
            use_browser = (
                browser_dl is not None
                and aweme_id is not None
                and not args.playlist
            )

            if args.engine == "browser" and aweme_id is None:
                failed += 1
                browser_failure = True
                print("浏览器模式只支持单个视频链接。", file=sys.stderr)
                continue

            if use_browser:
                try:
                    browser_dl.download(url, aweme_id)
                except KeyboardInterrupt:
                    print("\n已取消。", file=sys.stderr)
                    return 130
                except BrowserError as exc:
                    browser_failure = True
                    if (
                        args.gear
                        or args.engine == "browser"
                        or is_permanent_browser_error(exc)
                    ):
                        failed += 1
                        print(f"浏览器下载失败：{exc}", file=sys.stderr)
                        if args.verbose:
                            import traceback

                            traceback.print_exc()
                        continue
                    print(f"[提示] 浏览器下载失败：{exc}；改用 yt-dlp。", file=sys.stderr)
                except Exception as exc:
                    browser_failure = True
                    if (
                        args.gear
                        or args.engine == "browser"
                        or is_permanent_browser_error(exc)
                    ):
                        failed += 1
                        print(f"浏览器下载失败：{exc}", file=sys.stderr)
                        if args.verbose:
                            import traceback

                            traceback.print_exc()
                        continue
                    print(f"[提示] 浏览器下载失败：{exc}；改用 yt-dlp。", file=sys.stderr)
                else:
                    succeeded += 1
                    continue

            try:
                result = downloader.download(url)
            except download_error as exc:
                failed += 1
                cookie_failure = cookie_failure or is_cookie_error(exc)
                print(f"下载失败：{exc}", file=sys.stderr)
                if args.verbose:
                    import traceback

                    traceback.print_exc()
                continue
            except KeyboardInterrupt:
                print("\n已取消。", file=sys.stderr)
                return 130
            except Exception as exc:
                failed += 1
                cookie_failure = cookie_failure or is_cookie_error(exc)
                print(f"下载失败：{exc}", file=sys.stderr)
                if args.verbose:
                    import traceback

                    traceback.print_exc()
                continue

            if result == 0:
                succeeded += 1
            else:
                failed += 1
                print(f"下载失败，yt-dlp 返回码：{result}", file=sys.stderr)
    finally:
        if browser_dl is not None:
            browser_dl.close()

    print()
    print("=" * 60)
    print(f"处理完成：成功 {succeeded} 个，失败 {failed} 个")
    print(f"保存目录：{output_dir}")

    if failed:
        if cookie_failure:
            print_cookie_help()
        elif browser_failure:
            print_browser_help()
        else:
            print()
            print("如果链接本身可以正常播放，可以尝试更新解析器：")
            print("  python -m pip install -U yt-dlp")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
