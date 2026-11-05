"""用真实 Chromium 浏览器解析并下载抖音视频。

抖音 Web API 会校验浏览器环境；直接在 Python 里请求通常会被风控拦截。
本模块启动一个独立的 Edge/Chrome/Chromium 实例，在页面上下文里调用抖音
自己的详情接口，然后从返回的 play_addr 下载视频。它不读取用户日常浏览器
的 Cookie，也不会修改系统浏览器配置。

浏览器窗口有三种模式（BrowserSession 的 window_mode 参数）：

- "headless"  无头模式，屏幕上不出现任何窗口（默认）；
- "offscreen" 真实窗口，但启动位置在屏幕外坐标，用户看不到、也不抢焦点，
              页面可见性仍然是真实的，用于无头模式被风控拦截时降级；
- "visible"   正常可见窗口，只在调试或需要人工登录时使用。

浏览器配置默认持久化在脚本目录下的 .browser-profile 里，登录一次后可以复用。
对外提供的通用能力（B 站等其它下载器也可以直接复用，都不写死站点域名）：

- default_profile_dir()      解析项目内的默认持久化配置目录；
- normalize_window_mode()    规范化窗口模式参数；
- BrowserSession.export_cookies(path)
                             把当前会话的 Cookie 导出成 Netscape 格式 cookies.txt，
                             供 yt-dlp --cookies 使用；
- is_logged_in(cookies, required) / BrowserSession.is_logged_in(required)
                             通用登录态检测（按关键 Cookie 名判断）；
- detect_system_proxy()      探测 Windows 系统代理，返回带 scheme 的代理 URL。

这个模块只处理单个视频；主页/合集等播放列表仍交给 yt-dlp。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
ARCHIVE_NAME = ".douyin-archive.txt"
# 常见清晰度档位（短边像素）。用于把像素分辨率对齐成可读档位。
STANDARD_QUALITY_HEIGHTS = (360, 480, 540, 720, 1080, 1440, 2160)
# gear_name 形如 720_2_1 / 540_1_1，前缀就是档位高度。
GEAR_PREFIX_RE = re.compile(r"^(\d{3,4})_")
DEFAULT_PROFILE_DIR_NAME = ".browser-profile"
# 浏览器启动后默认先打开的页面。抖音侧依赖它写入访客 Cookie；其它站点
# （例如 B 站）必须通过 initial_url 传入自己的首页，避免启动时串站访问抖音。
DEFAULT_INITIAL_URL = "https://www.douyin.com/"
SCRIPT_DIR = Path(__file__).resolve().parent

# 浏览器窗口模式：headless 无头；offscreen 窗口在屏幕外；visible 正常可见窗口。
WINDOW_MODES = ("headless", "offscreen", "visible")
WINDOW_MODE_LABELS = {
    "headless": "无头模式（不显示窗口）",
    "offscreen": "屏幕外窗口（屏幕内不可见，不抢焦点）",
    "visible": "可见窗口",
}
OFFSCREEN_WINDOW_POSITION = "-32000,-32000"
WINDOW_SIZE = "1280,900"
LOGIN_POLL_INTERVAL = 2.0
DEFAULT_LOGIN_TIMEOUT = 300.0
PERMANENT_VIDEO_ERROR_MARKERS = (
    "作品不见了",
    "作品权限",
    "无法获取视频",
    "已删除",
    "私密",
    "图文",
)
COOKIE_FILE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.se/docs/http-cookies.html\n"
    "# Exported by autoDownloads browser engine for yt-dlp --cookies.\n"
)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class BrowserError(RuntimeError):
    """浏览器启动、接口请求或下载失败。"""


def find_browser(explicit: str | None = None) -> str:
    """查找可用的 Chromium 系浏览器可执行文件。"""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise BrowserError(f"指定的浏览器不存在：{explicit}")

    env_path = os.environ.get("DOUYIN_BROWSER")
    if env_path:
        path = Path(env_path).expanduser()
        if path.is_file():
            return str(path.resolve())

    home = Path.home()
    local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))

    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates.extend([
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            program_files_x86 / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            local_app_data / "Chromium" / "Application" / "chrome.exe",
        ])
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        candidates.extend([
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            support / "Microsoft Edge" / "Edge.app" / "Contents" / "MacOS" / "Microsoft Edge",
        ])
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "brave-browser",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for path in candidates:
        try:
            if path.is_file():
                return str(path.resolve())
        except OSError:
            continue

    for name in ("msedge", "chrome", "chromium", "brave", "brave-browser"):
        found = shutil.which(name)
        if found:
            return found

    raise BrowserError(
        "没有找到 Edge/Chrome/Chromium。请安装其中一个浏览器，"
        "或用 --browser 指定可执行文件路径。"
    )


def default_profile_dir(base: str | Path | None = None) -> Path:
    """项目内默认的持久化浏览器配置目录。

    默认按本文件（脚本）所在目录解析，因此与启动时的当前工作目录无关。
    该目录里含有登录 Cookie，必须和 .gitignore 一起管理。
    """
    root = Path(base).expanduser().resolve() if base else SCRIPT_DIR
    return root / DEFAULT_PROFILE_DIR_NAME


def normalize_window_mode(mode: str | bool | None) -> str:
    """把窗口模式参数规范成 "headless" / "offscreen" / "visible" 之一。

    兼容旧的布尔参数：True 等价"headless"，False 等价"visible"，
    空值按默认的 "headless" 处理。
    """
    if mode is True:
        return "headless"
    if mode is False:
        return "visible"
    text = str(mode or "").strip().lower()
    if not text:
        return "headless"
    if text not in WINDOW_MODES:
        raise BrowserError(
            f"未知的浏览器窗口模式：{mode}（可选：{', '.join(WINDOW_MODES)}）"
        )
    return text


def window_mode_label(mode: str | bool | None) -> str:
    """窗口模式的中文说明，用于日志输出。"""
    return WINDOW_MODE_LABELS[normalize_window_mode(mode)]


# 当前用户（HKCU）的 IE/系统代理设置所在的注册表位置。
IE_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
IE_SETTINGS_VALUES = ("ProxyEnable", "ProxyServer", "AutoConfigURL")


def parse_proxy_server(value: str | None) -> str | None:
    """把注册表 ProxyServer 的原始值规范成带 scheme 的代理 URL。

    支持两种写法：

    - "127.0.0.1:7897"                        单一代理；
    - "http=1.2.3.4:8080;https=1.2.3.4:8443"  按协议分别配置。

    分协议时优先取 https 段，没有就取第一个非空段。返回值一律带 scheme
    （原始值缺 scheme 时补 http://），因为 pip / yt-dlp 都要求完整 URL。
    空值或无法解析时返回 None。
    """
    text = (value or "").strip()
    if not text:
        return None

    if "=" in text:
        entries: dict[str, str] = {}
        for chunk in text.split(";"):
            name, _, address = chunk.partition("=")
            name = name.strip().lower()
            address = address.strip()
            if address:
                entries.setdefault(name, address)
        if not entries:
            return None
        text = entries.get("https") or next(iter(entries.values()))

    text = text.strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    return text


def _read_ie_settings() -> dict[str, Any]:
    """读取 HKCU 下的 IE/系统代理设置；读不到的项直接跳过。

    只在 Windows 上可用；非 Windows 调用会抛 ImportError，由调用方兜住。
    """
    import winreg

    settings: dict[str, Any] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, IE_SETTINGS_KEY) as key:
        for name in IE_SETTINGS_VALUES:
            try:
                settings[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                continue
    return settings


def detect_system_proxy() -> str | None:
    """探测 Windows 系统代理，返回带 scheme 的代理 URL；没有则返回 None。

    Python / yt-dlp / pip 只读 HTTP(S)_PROXY 环境变量，不读注册表里的系统代理，
    所以"系统代理已经开着"这件事必须由调用方显式接进来，否则会出现
    "浏览器能上网、脚本直连却被重置"的矛盾现象。

    以下情况返回 None：

    - 非 Windows 平台；
    - ProxyEnable 不是 1（未启用系统代理）；
    - AutoConfigURL（PAC 脚本）非空 —— 那是脚本地址，不是代理地址；
    - ProxyServer 为空或无法解析；
    - 读注册表报错（权限不足、键不存在、winreg 不可用）。

    任何异常都会被吞掉并返回 None，调用方不需要再包 try。
    """
    if sys.platform != "win32":
        return None
    try:
        settings = _read_ie_settings()
    except Exception:  # noqa: BLE001 - 权限/缺键/winreg 不可用都按"没有代理"处理
        return None
    if not settings:
        return None
    if str(settings.get("AutoConfigURL") or "").strip():
        return None
    try:
        enabled = int(settings.get("ProxyEnable") or 0)
    except (TypeError, ValueError):
        return None
    if enabled != 1:
        return None
    return parse_proxy_server(str(settings.get("ProxyServer") or ""))


def _window_arguments(mode: str) -> list[str]:
    """按窗口模式返回需要的 Chromium 启动参数。"""
    size = [f"--window-size={WINDOW_SIZE}"]
    if mode == "headless":
        return ["--headless=new", *size]
    if mode == "offscreen":
        # 真实窗口，只是落在屏幕外坐标：用户看不到也不会抢焦点，
        # 同时保持页面可见性为 visible，渲染和风控信号才是真实的。
        return [f"--window-position={OFFSCREEN_WINDOW_POSITION}", *size]
    return size


def _cookie_field(cookie: Any, key: str, default: Any = "") -> Any:
    """同时兼容 dict 和对象形态的 Cookie。"""
    if isinstance(cookie, dict):
        return cookie.get(key, default)
    return getattr(cookie, key, default)


def cookie_names(cookies: Iterable[Any]) -> set[str]:
    """取出 Cookie 名集合（登录态检测用）。"""
    names: set[str] = set()
    for cookie in cookies or ():
        name = _cookie_field(cookie, "name", "")
        if name:
            names.add(str(name))
    return names


def is_logged_in(cookies: Iterable[Any], required: Iterable[str] = ()) -> bool:
    """通用登录态检测：会话 Cookie 是否覆盖了全部必需项。

    required 的每一项是一个必需的 Cookie 名；同一项写成 "a|b" 表示其中
    任意一个存在就算满足（例如 "sessionid|sessionid_ss"）。required 为空
    时无从判断，返回 True。各站点只要传入自己的关键 Cookie 名即可复用。
    """
    groups = [str(item).split("|") for item in required or ()]
    if not groups:
        return True
    names = cookie_names(cookies)
    for alternatives in groups:
        if not any(name.strip() and name.strip() in names for name in alternatives):
            return False
    return True


def format_netscape_cookies(cookies: Iterable[Any]) -> str:
    """把 CDP 返回的 Cookie 列表转换成 Netscape cookies.txt 文本。

    字段顺序：domain、include_subdomains、path、secure、expiry、name、value。
    HttpOnly 的 Cookie 用 "#HttpOnly_" 前缀标记（curl 与 yt-dlp 都认这种写法）。
    """
    lines = [COOKIE_FILE_HEADER]
    for cookie in cookies or ():
        domain = str(_cookie_field(cookie, "domain", "")).strip()
        name = str(_cookie_field(cookie, "name", "")).strip()
        if not domain or not name:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(_cookie_field(cookie, "path", "/") or "/")
        secure = "TRUE" if _cookie_field(cookie, "secure", False) else "FALSE"
        try:
            expires = int(float(_cookie_field(cookie, "expires", 0) or 0))
        except (TypeError, ValueError):
            expires = 0
        if expires < 0:
            expires = 0
        value = str(_cookie_field(cookie, "value", ""))
        prefix = "#HttpOnly_" if _cookie_field(cookie, "httpOnly", False) else ""
        lines.append("\t".join([
            f"{prefix}{domain}",
            include_subdomains,
            path,
            secure,
            str(expires),
            name,
            value,
        ]))
    return "\n".join(lines) + "\n"


def parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    """把 Netscape cookies.txt 解析成可以直接交给 CDP 的 Cookie 字典列表。

    只保留未过期的条目：文件可能是几周前导出的，过期 Cookie 灌回浏览器只会
    让登录检查误判成功。
    """
    now = int(time.time())
    parsed: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        domain = domain.strip()
        name = name.strip()
        if not domain or not name:
            continue
        try:
            expiry = int(float(expires))
        except ValueError:
            expiry = 0
        if expiry and expiry < now:
            continue
        parsed.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure.strip().upper() == "TRUE",
                "httpOnly": http_only,
                "expires": expiry or -1,
            }
        )
    return parsed


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def safe_name(text: str | None, limit: int = 60) -> str:
    """把视频标题转成安全的文件名（不含扩展名）。"""
    clean = (text or "").strip()
    clean = clean.replace("\r", " ").replace("\n", " ")
    clean = re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", clean)
    clean = INVALID_FILENAME_CHARS.sub("_", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    if len(clean) > limit:
        clean = clean[:limit].rstrip(" .")
    if not clean:
        return ""
    if clean.upper() in WINDOWS_RESERVED_NAMES:
        clean = f"_{clean}"
    return clean


def parse_height_limit(fmt: str | None) -> int | None:
    """从 yt-dlp 风格的 format 中提取 height<=N。"""
    if not fmt:
        return None
    match = re.search(r"height\s*<=\s*(\d+)", fmt, re.I)
    if not match:
        return None
    return int(match.group(1))


def _ratio_for_format(fmt: str | None) -> str:
    """把 format 映射成抖音播放接口的 ratio 参数。"""
    if fmt and "worst" in fmt.lower():
        return "360p"
    max_height = parse_height_limit(fmt)
    if max_height:
        if max_height <= 360:
            return "360p"
        if max_height <= 480:
            return "480p"
        if max_height <= 540:
            return "540p"
        if max_height <= 720:
            return "720p"
    return "1080p"


def pick_candidates(
    detail: dict[str, Any],
    fmt: str | None = "best",
    *,
    prefer_h264: bool = False,
) -> list[dict[str, Any]]:
    """从 aweme_detail.video 中提取可用视频直链，按优先级排序。"""
    video = detail.get("video") or {}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        node: Any,
        label: str,
        codec: str,
        bitrate: int = 0,
        gear: str = "",
    ) -> None:
        if not isinstance(node, dict):
            return
        urls = node.get("url_list") or []
        if not urls:
            return
        url = str(urls[0])
        if not url.startswith(("http://", "https://")):
            return
        # 同一个 CDN 路径可能在不同字段中重复出现，按路径前缀去重。
        key = url.split("?", 1)[0]
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "url": url,
            "height": int(node.get("height") or 0),
            "width": int(node.get("width") or video.get("width") or 0),
            "bitrate": bitrate or int(node.get("bit_rate") or 0),
            "codec": codec,
            "label": label,
            "gear": gear,
        })

    # play_addr 通常是无水印播放流；download_addr 可能带水印，放在最后兜底。
    add(video.get("play_addr_h264"), "H.264", "h264")
    add(video.get("play_addr"), "默认播放流", "h264")
    add(video.get("play_addr_265"), "H.265", "h265")

    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        node = item.get("play_addr") or {}
        codec = "h265" if item.get("is_h265") else "h264"
        gear = str(item.get("gear_name") or "")
        add(node, gear or "高清", codec, int(item.get("bit_rate") or 0), gear)

    if not candidates:
        add(video.get("download_addr"), "下载流", "h264")

    # 抖音 App 的 play 接口通常能拿到比网页 play_addr 更高码率的 H.264 流。
    # 只需要 play_addr.uri，不需要额外签名。
    play_uri = (video.get("play_addr") or {}).get("uri")
    if play_uri:
        ratio = _ratio_for_format(fmt)
        play_height = int(ratio.rstrip("p"))
        candidates.append({
            "url": (
                "https://aweme.snssdk.com/aweme/v1/play/"
                f"?video_id={play_uri}&ratio={ratio}&line=0"
            ),
            "height": play_height,
            "width": play_height,
            "bitrate": 0,
            "codec": "h264",
            "label": f"{ratio} 高码率",
            "gear": "",
            "user_agent": MOBILE_UA,
        })

    max_height = parse_height_limit(fmt)
    if max_height:
        def short_side(candidate: dict[str, Any]) -> int:
            height = candidate["height"] or 0
            width = candidate["width"] or 0
            return min(height, width) if height and width else height

        limited = [c for c in candidates if not c["height"] or short_side(c) <= max_height]
        if limited:
            candidates = limited

    if fmt and "worst" in fmt.lower():
        candidates.sort(key=lambda c: (c["height"] or 0, c["codec"] == "h264", c["bitrate"]))
    elif prefer_h264:
        # 播放器兼容性优先：宁可低一点，也优先选 H.264。
        candidates.sort(
            key=lambda c: (c["codec"] == "h264", c["height"] or 0, c["bitrate"]),
            reverse=True,
        )
    else:
        # 默认按分辨率/码率选最高画质；同分辨率时优先 H.264。
        candidates.sort(
            key=lambda c: (c["height"] or 0, c["codec"] == "h264", c["bitrate"]),
            reverse=True,
        )

    return candidates


def snap_quality_height(value: int) -> int:
    """把像素高度对齐到常见清晰度档位（误差 12% 以内视为同一档）。"""
    if value <= 0:
        return 0
    nearest = min(STANDARD_QUALITY_HEIGHTS, key=lambda height: abs(height - value))
    if abs(nearest - value) <= max(24, value * 0.12):
        return nearest
    return value


def quality_label_from_resolution(height: int = 0, width: int = 0) -> str:
    """按分辨率推断清晰度档位标签（抖音竖屏时 height 是长边）。

    规则：
    - 明确横屏（width > height）：直接用像素高度，如 1920x1080 → 1080P；
    - 竖屏且宽高都有：短边就是档位，如 1280x720 → 720P、1024x576 → 540P；
    - 只有一个数值：本身是标准档位就当作横屏像素高（1080 → 1080P），否则按
      竖屏长边折算成 16:9 短边再对齐档位（1280 → 720P、1024 → 540P、1920 → 1080P）。
    """
    h = int(height or 0)
    w = int(width or 0)
    if h <= 0 and w <= 0:
        return ""
    if h and w:
        if w > h:
            return f"{h}P"
        return f"{snap_quality_height(min(h, w))}P"
    value = h or w
    if value in STANDARD_QUALITY_HEIGHTS:
        return f"{value}P"
    return f"{snap_quality_height(int(round(value * 9 / 16)))}P"


def quality_label(gear: str | None, height: int = 0, width: int = 0) -> str:
    """清晰度档位标签：优先用 gear_name 的数字前缀，缺失时按分辨率推断。"""
    text = str(gear or "").strip()
    match = GEAR_PREFIX_RE.match(text)
    if match:
        return f"{match.group(1)}P"
    return quality_label_from_resolution(height=height, width=width) or (text or "未知")


def _quality_sort_key(item: dict[str, Any]) -> tuple:
    """码率从高到低；没有码率的排在后面，同码率按分辨率、gear 排序。"""
    return (
        item.get("bitrate") is None,
        -(item.get("bitrate") or 0),
        -(item.get("height") or 0),
        str(item.get("gear") or ""),
    )


def build_qualities(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """把 aweme_detail 里的可用流整理成给界面用的清晰度档位列表。

    纯函数：不联网、不写文件。每一项固定包含 gear / label / codec /
    bitrate（kbps，未知为 None）/ height / notes 六个字段。
    """
    qualities: list[dict[str, Any]] = []
    for candidate in pick_candidates(detail, "best"):
        gear = str(candidate.get("gear") or "")
        bitrate_bps = int(candidate.get("bitrate") or 0)
        source_label = str(candidate.get("label") or "")
        if gear:
            notes = "" if GEAR_PREFIX_RE.match(gear) else f"原始 gear：{gear}"
        elif "高码率" in source_label:
            notes = "高码率"
        else:
            notes = source_label
        bitrate_kbps = int(round(bitrate_bps / 1000)) if bitrate_bps else 0
        qualities.append({
            "gear": gear,
            "label": quality_label(
                gear,
                height=int(candidate.get("height") or 0),
                width=int(candidate.get("width") or 0),
            ),
            "codec": str(candidate.get("codec") or ""),
            "bitrate": bitrate_kbps or None,
            "height": int(candidate.get("height") or 0) or None,
            "notes": notes,
        })
    qualities.sort(key=_quality_sort_key)
    return qualities


def recommended_gear(qualities: list[dict[str, Any]]) -> str | None:
    """默认选中项：分辨率最高、同分辨率里码率最高的那个带 gear 的档位。

    没有带 gear 的档位（只有默认流）时返回 None；不能按码率最高来挑，因为
    抖音不同分档之间的码率并不单调（540P 可能比 720P 码率高）。
    """
    best: dict[str, Any] | None = None
    for item in qualities or ():
        if not str(item.get("gear") or ""):
            continue
        if best is None:
            best = item
            continue
        current = (int(item.get("height") or 0), int(item.get("bitrate") or 0))
        champion = (int(best.get("height") or 0), int(best.get("bitrate") or 0))
        if current > champion:
            best = item
    return str(best.get("gear")) if best else None


def candidates_for_gear(candidates: list[dict[str, Any]], gear: str | None) -> list[dict[str, Any]]:
    """按 gear_name 精确挑选候选流；指定档位不存在时抛错（绝不静默降级）。"""
    target = str(gear or "").strip()
    if not target:
        return list(candidates)
    matched = [c for c in candidates if str(c.get("gear") or "") == target]
    if matched:
        return matched
    available = sorted({str(c.get("gear")) for c in candidates if c.get("gear")})
    hint = "、".join(available) if available else "无（这个视频只提供默认流，没有 gear）"
    raise BrowserError(
        f"--gear {target} 不存在：这个视频可选档位是 {hint}。"
        "可用 --list-qualities 查看可用档位，脚本不会自动改用其它档位。"
    )


class BrowserSession:
    """一个带 CDP 控制的独立 Chromium 实例。

    window_mode 决定窗口怎么出现：headless（默认，不弹窗）、offscreen
    （真实窗口但放在屏幕外，用户看不到）或 visible（正常可见窗口）。
    profile_dir 为空时使用一次性临时目录，传入路径则持久化浏览器配置。
    initial_url 是启动后自动打开的页面（默认抖音首页，抖音侧依赖它生成访客
    Cookie）；其它站点必须传入自己的首页，避免启动时串站访问抖音；传 None
    表示启动后不做任何导航。
    """

    def __init__(
        self,
        browser_path: str,
        *,
        window_mode: str = "headless",
        headless: bool | None = None,
        wait_seconds: float = 8.0,
        timeout: float = 30.0,
        proxy: str | None = None,
        profile_dir: str | Path | None = None,
        initial_url: str | None = DEFAULT_INITIAL_URL,
    ) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise BrowserError(
                "缺少 websocket-client。请运行：python -m pip install -r requirements.txt"
            ) from exc

        self._websocket = websocket
        self.browser_path = browser_path
        # 兼容旧参数：显式传入 headless 时以它为准，否则用 window_mode。
        self.window_mode = normalize_window_mode(
            headless if headless is not None else window_mode
        )
        # 旧代码可能读取 .headless，这里保持等价含义。
        self.headless = self.window_mode == "headless"
        self.wait_seconds = wait_seconds
        self.timeout = timeout
        self.proxy = proxy
        self.initial_url = initial_url
        self._profile_dir = profile_dir
        self._owns_profile = False
        self._proc: subprocess.Popen | None = None
        self._profile: Path | None = None
        self._ws = None
        self._next_id = 0
        self._debug_port: int | None = None
        self._start()

    @property
    def profile_dir(self) -> str | None:
        """当前使用的浏览器配置目录；None 表示一次性临时目录。"""
        if self._profile is None or self._owns_profile:
            return None
        return str(self._profile)

    def _start(self) -> None:
        port = _free_port()
        if self._profile_dir:
            try:
                profile = Path(self._profile_dir).expanduser().resolve()
                if profile.exists() and not profile.is_dir():
                    raise BrowserError(f"浏览器配置路径不是文件夹：{profile}")
                profile.mkdir(parents=True, exist_ok=True)
                self._profile = profile
                self._owns_profile = False
            except BrowserError:
                raise
            except OSError as exc:
                raise BrowserError(f"无法创建浏览器配置目录：{exc}") from exc
        else:
            try:
                self._profile = Path(tempfile.mkdtemp(prefix="douyin-dl-browser-"))
                self._owns_profile = True
            except OSError as exc:
                raise BrowserError(f"无法创建浏览器临时目录：{exc}") from exc
        command = [
            self.browser_path,
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--lang=zh-CN",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._profile}",
            *_window_arguments(self.window_mode),
        ]
        # 独立临时浏览器只访问抖音；部分 Edge/Chrome 版本启用沙箱时
        # CDP 命令会无响应，因此这里关闭浏览器沙箱以保证自动化可用。
        if self.proxy:
            command.append(f"--proxy-server={self.proxy}")
        command.append("about:blank")

        try:
            self._proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            page = self._wait_for_page(port, timeout=30.0)
            self._debug_port = port

            self._ws = self._websocket.create_connection(
                page["webSocketDebuggerUrl"],
                timeout=self.timeout,
                suppress_origin=True,
                max_size=128 * 1024 * 1024,
            )
            self._call("Page.enable")
            self._call("Runtime.enable")
            self._call("Network.enable")
            self._restore_saved_cookies()
            if self.initial_url:
                self.navigate(self.initial_url)
            # 留时间让页面写入 s_v_web_id / UIFID 等访客 Cookie。
            time.sleep(max(1.0, self.wait_seconds))
        except Exception as exc:
            self.close()
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError(f"启动浏览器失败：{exc}") from exc

    def _restore_saved_cookies(self) -> int:
        """把配置目录里的 cookies.txt 灌回浏览器。

        Chromium 被强行结束时 Cookie 可能没落盘，只剩内存；用导出的文件补回去，
        登录态才不会因为一次异常退出就丢掉。
        """
        if self._profile is None:
            return 0
        path = self._profile / "cookies.txt"
        if not path.is_file():
            return 0
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        restored = 0
        for cookie in parse_netscape_cookies(text):
            try:
                result = self._call("Network.setCookie", cookie, timeout=5)
            except BrowserError:
                continue
            if result.get("success"):
                restored += 1
        return restored

    def _wait_for_page(self, port: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json",
                    timeout=2,
                ) as response:
                    targets = json.loads(response.read())
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    return pages[0]
            except Exception as exc:  # 浏览器启动期间连接失败是正常的
                last_error = exc
            time.sleep(0.4)

        raise BrowserError(f"浏览器调试端口没有启动：{last_error or '超时'}")

    def _call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0):
        if self._ws is None:
            raise BrowserError("浏览器连接已关闭")

        self._next_id += 1
        request_id = self._next_id
        self._ws.send(json.dumps({
            "id": request_id,
            "method": method,
            "params": params or {},
        }))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._ws.settimeout(1.0)
                message = json.loads(self._ws.recv())
            except self._websocket.WebSocketTimeoutException:
                continue
            except self._websocket.WebSocketConnectionClosedException as exc:
                raise BrowserError("浏览器连接已断开，请重试") from exc
            except Exception:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise BrowserError(f"浏览器命令 {method} 失败：{message['error']}")
            return message.get("result") or {}

        raise BrowserError(f"浏览器命令超时：{method}")

    def navigate(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})

    def evaluate(self, expression: str, timeout: float = 30.0):
        result = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise BrowserError(f"页面脚本执行失败：{result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    def get_cookies(self) -> list[dict[str, Any]]:
        """读取当前浏览器会话里的全部 Cookie（CDP Network.getAllCookies）。"""
        if self._ws is None:
            raise BrowserError("浏览器连接已关闭")
        result = self._call("Network.getAllCookies", timeout=self.timeout)
        cookies = result.get("cookies") if isinstance(result, dict) else None
        return [cookie for cookie in cookies or [] if isinstance(cookie, dict)]

    def is_logged_in(self, required_cookie_names: Iterable[str] = ()) -> bool:
        """通用登录态检测：当前会话是否已具备指定的关键 Cookie。

        required_cookie_names 的写法见模块级 is_logged_in()。该入口与站点无关，
        由调用方决定用哪些 Cookie 名判断登录。
        """
        return is_logged_in(self.get_cookies(), required_cookie_names)

    def export_cookies(self, path: str | Path) -> Path:
        """把当前会话的 Cookie 导出成 Netscape cookies.txt，供 yt-dlp --cookies 使用。

        返回写入的文件路径。文件里含登录凭据，调用方需要把目标路径加入忽略列表。
        """
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            text = format_netscape_cookies(self.get_cookies())
            temp = target.with_name(target.name + ".part")
            temp.write_text(text, encoding="utf-8")
            temp.replace(target)
        except OSError as exc:
            raise BrowserError(f"导出 Cookie 失败：{exc}") from exc
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def fetch_detail(self, aweme_id: str, retries: int = 3) -> dict[str, Any]:
        if not aweme_id.isdigit():
            raise BrowserError(f"作品 ID 格式无效：{aweme_id}")

        script = f"""(async () => {{
            const r = await fetch('/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}', {{
                credentials: 'include',
                headers: {{accept: 'application/json, text/plain, */*'}}
            }});
            return await r.text();
        }})()"""

        last_message = ""
        for attempt in range(max(1, retries)):
            result = self._call(
                "Runtime.evaluate",
                {
                    "expression": script,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=40.0,
            )
            if result.get("exceptionDetails"):
                raise BrowserError(f"抖音接口执行失败：{result['exceptionDetails']}")

            body = (result.get("result") or {}).get("value") or ""
            if body.startswith("Blocked by"):
                last_message = body
                time.sleep(2)
                continue

            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                last_message = body[:120]
                time.sleep(2)
                continue

            detail = payload.get("aweme_detail")
            if isinstance(detail, dict) and detail:
                return detail

            filter_info = payload.get("filter_detail") or {}
            message = (
                filter_info.get("detail_msg")
                or filter_info.get("notice")
                or payload.get("status_msg")
                or "接口没有返回视频信息"
            )
            raise BrowserError(f"无法获取视频：{message}")

        if last_message.startswith("Blocked by"):
            raise BrowserError(
                f"抖音接口拒绝了浏览器请求（Uifid/风控拦截）：{last_message}"
            )
        raise BrowserError(f"抖音接口返回异常：{last_message}")

    def _request_browser_close(self) -> bool:
        """请求 Chromium 自己退出，好让它把 Cookie 写进配置目录。

        直接 taskkill 只会丢掉内存里的登录态：下次用同一份配置仍然显示未登录，
        所以关闭时先走 CDP 的 Browser.close，退出失败再强制结束。
        """
        port = self._debug_port
        if not port:
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3
            ) as response:
                endpoint = json.loads(response.read().decode("utf-8")).get(
                    "webSocketDebuggerUrl"
                )
            if not endpoint:
                return False
            ws = self._websocket.create_connection(
                endpoint, timeout=5, suppress_origin=True
            )
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def close(self) -> None:
        ws = self._ws
        self._ws = None
        proc = self._proc
        self._proc = None
        self._debug_port = None

        graceful = proc is not None and self._request_browser_close()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

        if proc is not None:
            exited = False
            if graceful:
                try:
                    proc.wait(timeout=12)
                    exited = True
                except Exception:
                    exited = False
            if not exited:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                    else:
                        proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        profile = self._profile
        self._profile = None
        if profile is not None and self._owns_profile:
            shutil.rmtree(profile, ignore_errors=True)

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *_args) -> bool:
        self.close()
        return False


def _archive_path(output_dir: Path) -> Path:
    return output_dir / ARCHIVE_NAME


def is_archived(output_dir: Path, aweme_id: str) -> bool:
    path = _archive_path(output_dir)
    if not path.is_file():
        return False
    try:
        target = f"douyin {aweme_id}"
        return any(
            line.strip() == target
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False


def record_archive(output_dir: Path, aweme_id: str) -> None:
    try:
        with _archive_path(output_dir).open("a", encoding="utf-8") as handle:
            handle.write(f"douyin {aweme_id}\n")
    except OSError:
        pass


class BrowserDownloader:
    """管理浏览器生命周期，并下载单个抖音视频。

    window_mode 来自 CLI（默认无头）。无头模式解析失败时自动降级为屏幕外
    窗口重试，用户依旧看不到窗口；配置目录持久化时首次运行会提示登录一次。
    """

    def __init__(
        self,
        output_dir: Path,
        args,
        *,
        login_cookies: Iterable[str] = (),
    ) -> None:
        self.output_dir = output_dir
        self.args = args
        # 判断登录态的关键 Cookie 名由调用方给定（本模块不写死站点 Cookie）。
        self.login_cookies = tuple(login_cookies or ())
        self.window_mode = normalize_window_mode(
            getattr(args, "window_mode", None)
        )
        self.session: BrowserSession | None = None
        self._degraded = False

    def start(self, prompt_login: bool = True) -> BrowserDownloader:
        """启动浏览器会话；prompt_login=False 时不弹登录窗口（只读查询用）。"""
        self._open_session(self.window_mode)
        if prompt_login:
            self._prepare_login()
        return self

    def _candidates(self, detail: dict[str, Any]) -> list[dict[str, Any]]:
        """按 --format 生成候选流；指定 --gear 时只保留那一档（不存在就报错）。"""
        candidates = pick_candidates(
            detail,
            self.args.format,
            prefer_h264=bool(getattr(self.args, "prefer_h264", False)),
        )
        gear = getattr(self.args, "gear", None)
        if gear:
            return candidates_for_gear(candidates, gear)
        return candidates

    def _open_session(self, window_mode: str) -> BrowserSession:
        """按指定窗口模式（重新）打开浏览器会话。"""
        if self.session is not None:
            self.session.close()
            self.session = None
        browser = find_browser(getattr(self.args, "browser", None))
        window_mode = normalize_window_mode(window_mode)
        profile_dir = getattr(self.args, "browser_profile", None)
        # 先打印窗口模式和配置目录，浏览器启动失败时也能看到实际使用的策略。
        print(f"[窗口] {window_mode_label(window_mode)}")
        if profile_dir:
            print(f"[配置] 浏览器配置目录：{Path(profile_dir).expanduser()}")
        else:
            print("[配置] 使用一次性临时配置（本次退出后不保留登录态）")
        self.session = BrowserSession(
            browser,
            window_mode=window_mode,
            wait_seconds=float(getattr(self.args, "browser_wait", 8.0)),
            timeout=float(self.args.timeout),
            proxy=getattr(self.args, "proxy", None),
            profile_dir=profile_dir,
        )
        return self.session

    def _prepare_login(self) -> None:
        """检查登录态；没有登录态时按需打开可见窗口让用户登录一次。"""
        if self.session is None or not self.login_cookies:
            return
        if self.session.is_logged_in(self.login_cookies):
            print("[登录] 已检测到登录态，直接复用浏览器配置")
            return
        if self.window_mode == "visible":
            print("[登录] 未检测到登录态：窗口已经打开，可以直接在里面登录（不阻塞下载）")
            return
        if getattr(self.args, "browser_profile", None) is None:
            print("[登录] 未检测到登录态：当前是一次性临时配置，登录态不会保存，跳过登录提示")
            return
        if not sys.stdin.isatty():
            print("[登录] 未检测到登录态；当前不是交互式终端，跳过登录提示，直接尝试解析")
            return
        self._interactive_login()

    def _interactive_login(self) -> None:
        """临时打开可见窗口等待用户登录，成功后回到默认窗口模式继续。"""
        timeout = float(getattr(self.args, "login_timeout", DEFAULT_LOGIN_TIMEOUT) or 0)
        if timeout <= 0:
            timeout = DEFAULT_LOGIN_TIMEOUT
        print("[登录] 需要登录一次：正在打开浏览器窗口，请完成抖音登录 ...")
        self._open_session("visible")
        deadline = time.monotonic() + max(1.0, timeout)
        logged_in = False
        while time.monotonic() < deadline:
            try:
                if self.session.is_logged_in(self.login_cookies):
                    logged_in = True
                    break
            except BrowserError:
                break
            time.sleep(LOGIN_POLL_INTERVAL)
        if not logged_in:
            self.close()
            raise BrowserError(
                "等待登录超时，没有检测到登录态。可以加 --visible 打开窗口登录后重试。"
            )
        print("[登录] 登录成功，登录态已保存到浏览器配置目录")
        print("[登录] 以后运行会自动复用该配置，不再需要登录")
        self._open_session(self.window_mode)

    def _should_degrade(self, error: BaseException) -> bool:
        """无头模式失败时是否值得降级为屏幕外窗口重试。"""
        if self._degraded or self.session is None:
            return False
        if self.session.window_mode != "headless":
            return False
        if bool(getattr(self.args, "visible", False)):
            return False
        message = str(error)
        return not any(marker in message for marker in PERMANENT_VIDEO_ERROR_MARKERS)

    def _degrade_to_offscreen(self, reason: str) -> None:
        """无头模式被拦截时降级为屏幕外真实窗口，并记录降级原因。"""
        self._degraded = True
        print(f"[降级] 无头模式解析失败：{reason}")
        print("[降级] 改用屏幕外窗口重试（不在屏幕内显示，不影响当前工作）")
        self._open_session("offscreen")

    def export_cookies(self, path: str | Path) -> Path | None:
        """导出当前会话 Cookie 给 yt-dlp 使用；失败只提示不中断。"""
        if self.session is None:
            return None
        try:
            return self.session.export_cookies(path)
        except BrowserError as exc:
            print(f"[Cookie] 导出失败：{exc}", file=sys.stderr)
            return None

    def fetch_detail(self, aweme_id: str, retries: int | None = None) -> dict[str, Any]:
        """获取作品详情；无头失败时自动降级为屏幕外窗口重试一次。"""
        if self.session is None:
            raise BrowserError("浏览器尚未启动")
        attempt = max(1, int(self.args.retries if retries is None else retries))
        try:
            return self.session.fetch_detail(aweme_id, retries=attempt)
        except BrowserError as exc:
            if not self._should_degrade(exc):
                raise
            self._degrade_to_offscreen(str(exc))
            return self.session.fetch_detail(aweme_id, retries=attempt)

    @property
    def browser_path(self) -> str:
        return self.session.browser_path if self.session is not None else ""

    def collect_user_videos(self, sec_uid: str, limit: int = 0) -> list[str]:
        """滚动用户主页，收集作品视频链接；无头被拦截时降级重试一次。"""
        if self.session is None:
            raise BrowserError("浏览器尚未启动")
        if not sec_uid:
            raise BrowserError("用户主页链接里没有 sec_uid")
        try:
            return self._collect_user_videos(sec_uid, limit)
        except BrowserError as exc:
            if not self._should_degrade(exc):
                raise
            self._degrade_to_offscreen(str(exc))
            return self._collect_user_videos(sec_uid, limit)

    def _collect_user_videos(self, sec_uid: str, limit: int = 0) -> list[str]:
        """滚动用户主页，收集作品视频链接。"""
        if self.session is None:
            raise BrowserError("浏览器尚未启动")

        self.session.navigate(f"https://www.douyin.com/user/{sec_uid}")
        time.sleep(max(8.0, float(getattr(self.args, "browser_wait", 8.0))))

        strict_js = """JSON.stringify(Array.from(document.querySelectorAll('a[href*="/video/"]'))
            .filter(a => !a.href.includes('source=') && (a.className || '').split(/\\s+/).length >= 3)
            .map(a => a.href.split('?')[0]))"""
        loose_js = """JSON.stringify(Array.from(document.querySelectorAll('a[href*="/video/"]'))
            .filter(a => !a.href.includes('source='))
            .map(a => a.href.split('?')[0]))"""

        ids: list[str] = []
        seen: set[str] = set()
        quiet_rounds = 0

        for round_index in range(300):
            raw = self.session.evaluate(strict_js)
            links = json.loads(raw or "[]")
            if not links:
                raw = self.session.evaluate(loose_js)
                links = json.loads(raw or "[]")

            new_count = 0
            for link in links:
                match = re.search(r"/video/(\d+)", str(link))
                if not match:
                    continue
                video_id = match.group(1)
                if video_id not in seen:
                    seen.add(video_id)
                    ids.append(video_id)
                    new_count += 1
                    if limit and len(ids) >= limit:
                        break

            if limit and len(ids) >= limit:
                break

            if round_index % 5 == 0:
                print(f"[用户] 已发现 {len(ids)} 个视频...", flush=True)

            self.session.evaluate(
                "window.scrollTo(0, Math.max(document.body.scrollHeight, "
                "document.documentElement.scrollHeight));"
                "document.documentElement.scrollTop = document.documentElement.scrollHeight;"
                "document.body.scrollTop = document.body.scrollHeight;"
            )
            time.sleep(2.5)

            if new_count == 0:
                quiet_rounds += 1
                if quiet_rounds >= 8:
                    break
            else:
                quiet_rounds = 0

        if not ids:
            raise BrowserError(
                "没有从用户主页收集到视频。可能需要登录，"
                "或该用户没有公开作品。"
            )

        return [f"https://www.douyin.com/video/{video_id}" for video_id in ids]

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _dest_path(self, detail: dict[str, Any], aweme_id: str) -> Path:
        title = safe_name(detail.get("desc"), limit=60)
        filename = f"{aweme_id}_{title}.mp4" if title else f"{aweme_id}.mp4"
        parent = self.output_dir
        if getattr(self.args, "folder_by_author", False):
            author = safe_name((detail.get("author") or {}).get("nickname"), limit=40)
            if author:
                parent = parent / author
        return parent / filename

    def _info_path(self, dest: Path) -> Path:
        return dest.with_name(dest.stem + ".info.json")

    def _thumbnail_path(self, dest: Path) -> Path:
        return dest.with_name(dest.stem + ".jpg")

    def download(self, _url: str, aweme_id: str) -> None:
        if self.session is None:
            raise BrowserError("浏览器尚未启动")

        if (
            not self.args.force
            and not self.args.no_archive
            and not self.args.dry_run
            and not self.args.list_formats
            and is_archived(self.output_dir, aweme_id)
        ):
            print(f"[跳过] {aweme_id} 已在下载记录中（需要重新下载请加 --force）")
            return

        detail = self.fetch_detail(aweme_id)
        desc = (detail.get("desc") or "").strip()
        author = (detail.get("author") or {}).get("nickname") or ""
        video = detail.get("video") or {}
        duration_ms = int(video.get("duration") or 0)
        candidates = self._candidates(detail)

        print(f"  标题：{desc[:80] or '(无标题)'}")
        if author:
            print(f"  作者：{author}")
        if duration_ms:
            print(f"  时长：{duration_ms / 1000:.1f} 秒")

        if self.args.list_formats:
            print("  可用清晰度：")
            for candidate in candidates:
                height = f"{candidate['height']}p" if candidate["height"] else "未知"
                bitrate = (
                    f"{candidate['bitrate'] / 1000:.0f} kbps"
                    if candidate["bitrate"] else "-"
                )
                print(
                    f"    {height:>6}  {candidate['codec']:<4}  "
                    f"{bitrate:>10}  {candidate['label']}"
                )
            return

        if self.args.dry_run:
            if candidates:
                best = candidates[0]
                print(
                    f"  最佳清晰度：{best['height'] or '?'}p "
                    f"{best['codec']} {best['label']}"
                )
            print("  （dry-run：未下载）")
            return

        dest = self._dest_path(detail, aweme_id)
        if dest.exists() and not self.args.force:
            print(f"[跳过] 文件已存在：{dest}")
            if not self.args.no_archive and not self.args.force:
                record_archive(self.output_dir, aweme_id)
            return

        if not candidates:
            raise BrowserError("没有从视频信息中找到可下载的 mp4 直链")

        last_error: BrowserError | None = None
        downloaded = False

        for refresh in range(2):
            for candidate in candidates:
                print(
                    f"  清晰度：{candidate['height'] or '?'}p "
                    f"{candidate['codec']} {candidate['label']}"
                )
                try:
                    self._download_file(
                        candidate["url"],
                        dest,
                        referer=f"https://www.douyin.com/video/{aweme_id}",
                        user_agent=candidate.get("user_agent", DEFAULT_UA),
                    )
                except BrowserError as exc:
                    last_error = exc
                    if len(candidates) > 1:
                        print(f"  该清晰度不可用，尝试下一个：{exc}", file=sys.stderr)
                    continue
                downloaded = True
                break

            if downloaded:
                break

            if refresh == 0 and last_error and "403" in str(last_error):
                print("  直链已过期，正在重新获取 ...")
                detail = self.fetch_detail(aweme_id)
                candidates = self._candidates(detail)
                if candidates:
                    continue
            break

        if not downloaded:
            raise last_error or BrowserError("所有清晰度都下载失败")

        if self.args.info:
            self._write_info(detail, dest)
        if self.args.thumbnail:
            self._download_thumbnail(detail, dest)

        if not self.args.no_archive and not self.args.force:
            record_archive(self.output_dir, aweme_id)

        print(f"[完成] {dest}")

    def _download_file(
        self,
        url: str,
        dest: Path,
        *,
        referer: str,
        show_progress: bool = True,
        user_agent: str = DEFAULT_UA,
    ) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

        attempts = max(1, int(self.args.retries) + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": user_agent,
                        "Referer": referer,
                        "Accept": "*/*",
                    },
                )
                start = time.monotonic()
                downloaded = 0
                last_report = 0.0

                with urllib.request.urlopen(request, timeout=float(self.args.timeout)) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "text/html" in content_type:
                        raise BrowserError("CDN 返回了网页而不是视频，直链可能已过期")

                    total = int(response.headers.get("Content-Length") or 0)
                    with part.open("wb") as handle:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)

                            if self.args.limit_rate:
                                expected = downloaded / self.args.limit_rate
                                delay = expected - (time.monotonic() - start)
                                if delay > 0:
                                    time.sleep(min(delay, 0.5))

                            if show_progress and not self.args.no_progress:
                                now = time.monotonic()
                                if now - last_report >= 0.3:
                                    last_report = now
                                    speed = downloaded / max(0.001, now - start)
                                    if total:
                                        percent = downloaded * 100 / total
                                        line = (
                                            f"\r  下载中 {percent:5.1f}%  "
                                            f"{downloaded / 1048576:.1f}/"
                                            f"{total / 1048576:.1f} MB  "
                                            f"{speed / 1048576:.1f} MB/s"
                                        )
                                    else:
                                        line = (
                                            f"\r  下载中 {downloaded / 1048576:.1f} MB  "
                                            f"{speed / 1048576:.1f} MB/s"
                                        )
                                    print(line, end="", flush=True)

                if show_progress and not self.args.no_progress:
                    print()

                if downloaded <= 0:
                    raise BrowserError("下载到的文件是空的")

                part.replace(dest)
                return downloaded

            except BrowserError:
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
                raise
            except Exception as exc:
                last_error = exc
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
                if attempt < attempts - 1:
                    wait = min(5.0, 1.0 + attempt)
                    print(f"\n  下载失败，{wait:.0f} 秒后重试：{exc}", file=sys.stderr)
                    time.sleep(wait)

        raise BrowserError(f"下载失败：{last_error}")

    def _write_info(self, detail: dict[str, Any], dest: Path) -> None:
        video = detail.get("video") or {}
        info = {
            "id": detail.get("aweme_id"),
            "title": detail.get("desc"),
            "author": (detail.get("author") or {}).get("nickname"),
            "author_id": (detail.get("author") or {}).get("uid"),
            "create_time": detail.get("create_time"),
            "duration_ms": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "statistics": detail.get("statistics"),
            "url": f"https://www.douyin.com/video/{detail.get('aweme_id')}",
            "video": {
                "format": video.get("format"),
                "ratio": video.get("ratio"),
                "duration": video.get("duration"),
            },
        }
        try:
            self._info_path(dest).write_text(
                json.dumps(info, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"  警告：保存视频信息失败：{exc}", file=sys.stderr)

    def _download_thumbnail(self, detail: dict[str, Any], dest: Path) -> None:
        cover = ((detail.get("video") or {}).get("cover") or {}).get("url_list") or []
        if not cover:
            return
        try:
            self._download_file(
                cover[0],
                self._thumbnail_path(dest),
                referer=f"https://www.douyin.com/video/{detail.get('aweme_id')}",
                show_progress=False,
            )
        except Exception as exc:
            print(f"  警告：封面下载失败：{exc}", file=sys.stderr)
