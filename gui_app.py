"""autoDownloads 图形界面（tkinter/ttk，零第三方依赖）。

界面只负责"收集参数 -> 拼 argv -> 调现有 main() -> 贴日志"，下载逻辑一律
复用 bilibili_downloader.py / douyin_downloader.py / browser_engine.py，
本文件不改动其中任何一行。

线程模型：
    主线程（UI）  —— 只碰 tkinter 控件，用 root.after 轮询队列；
    工作线程      —— 跑 main(argv)/run_login()，用 contextlib.redirect_stdout
                     把输出写进 queue.Queue，绝不直接操作控件。

取消是协作式的：CancelToken 置位后，注入的 download 守卫会在"下一条目"开始
前抛出 TaskCancelled（KeyboardInterrupt 子类，不会被 main 的
except Exception 吞掉），yt-dlp 的 progress_hooks 也会立刻抛异常中断当前
文件。浏览器引擎（抖音单视频）没有 hook 接缝，只能保证"当前条目结束后不再
开始下一条"。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

import bilibili_downloader as bili
import browser_engine
import douyin_downloader as douyin


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

APP_TITLE = "autoDownloads 图形界面"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_DIR = PROJECT_DIR / "downloads"

CONFIG_DIR_NAME = "autoDownloads"
CONFIG_FILE_NAME = "gui.json"
BILIBILI_COOKIE_FILE_NAME = bili.COOKIE_FILE_NAME
# 平台 -> 真正干活的模块；单测可以替换这张表来验证线程/队列链路
PLATFORM_MODULES: dict[str, Any] = {"bilibili": bili, "douyin": douyin}

# B 站档位顺序即下拉顺序；2160/1440 需要大会员，但选项要给出来，
# 否则用户没法明确要求 4K，只会静默拿到 1080。
BILIBILI_QUALITIES = ("2160", "1440", "1080", "720", "480", "best")
# 下拉显示文案 -> 传给 argv 的真实取值（argv 只认 BILIBILI_QUALITIES 里的值）。
BILIBILI_QUALITY_LABELS = {
    "2160": "4K(2160)",
    "1440": "2K(1440)",
    "1080": "1080P",
    "720": "720P",
    "480": "480P",
    "best": "最佳(best)",
}
BILIBILI_QUALITY_CHOICES = tuple(BILIBILI_QUALITY_LABELS[q] for q in BILIBILI_QUALITIES)
DOUYIN_QUALITIES = ("best", "1080", "720", "480")
DOUYIN_FORMAT_MAP = {
    "best": "best",
    "1080": "best[height<=1080]",
    "720": "best[height<=720]",
    "480": "best[height<=480]",
}

LOGIN_TIMEOUT_DEFAULT = 300
LOGIN_TIMEOUT_MIN = 30
LOGIN_TIMEOUT_MAX = 1800
UP_LIMIT_MAX = 9999

QUEUE_POLL_MS = 100
LOG_MAX_LINES = 5000
# 日志区：默认窗口下必须稳定露出 LOG_MIN_VISIBLE_LINES 行以上，
# 下限按实测 linespace + 面板装饰高度算，不写死像素（DPI/字体变化时也成立）。
LOG_FONT = ("Consolas", 9)
LOG_TEXT_HEIGHT = 10
LOG_MIN_VISIBLE_LINES = 8
LOG_PANEL_MIN_HEIGHT = 180
# 面板除文本以外的装饰高度（LabelFrame 标题 + 清空日志按钮行 + 内边距）的保守估计
LOG_PANEL_CHROME = 96
# 关窗时等待工作线程收尾的上限与轮询间隔
CLOSE_WAIT_SECONDS = 15.0
CLOSE_WAIT_POLL_MS = 150  # 与 QUEUE_POLL_MS 错开，避免两个定时器同一轮触发

# 清晰度自动读取：链接停顿多久才发请求、失败结果多久后允许重试、读取超时
QUALITY_DEBOUNCE_MS = 800
QUALITY_RETRY_SECONDS = 30.0
# B 站档位读取：读一次要打 view + playurl 两个接口。本机实测：典型 9.7~10.1 秒，
# 长尾到过 34.6 秒（线路抖动）。所以预算给 45 秒覆盖长尾，而不是 10/20 秒那种必然撞线的值。
BILIBILI_QUALITY_TIMEOUT = 45.0
# 重试判断：第一次如果在 5 秒内就失败 = 连接被重置这类瞬时错误，值得重试一次；
# 慢慢磨到接近 timeout 才失败 = 链路就是慢，重试只会让用户再等 45 秒，故不重试。
QUALITY_FAST_FAILURE_SECONDS = 5.0
QUALITY_READ_TIMEOUT = BILIBILI_QUALITY_TIMEOUT  # 兼容旧名字
DOUYIN_QUALITY_TIMEOUT = 30.0
# 超时/网络类失败自动重试一次，间隔 0.5 秒（接口明确报错不重试）
QUALITY_RETRY_DELAY = 0.5
QUALITY_MAX_ATTEMPTS = 2
DOUYIN_QUALITIES_FALLBACK_TEXT = "best/1080/720/480"
# 防抖等待期间的状态行文案（让用户立刻知道"在读，只是要等输入停一下"）
QUALITY_PENDING_TEXT = {
    "bilibili": "正在读取清晰度…（等待输入停顿）",
    "douyin": "正在准备读取清晰度（会启动无头浏览器，约 3–10 秒）…",
}

CANCEL_NOTICE = "已请求停止，等待当前条目收尾"
DOUYIN_HOME = "https://www.douyin.com/"
DOUYIN_LOGIN_COOKIES = ("sessionid|sessionid_ss",)
# 持久化配置目录里的登录 Cookie 存放位置（按优先级）
DOUYIN_COOKIE_CANDIDATES = (
    "Default/Network/Cookies",
    "Default/Cookies",
    "cookies.txt",
    "Default/cookies.txt",
)

# 两个标签页的「UP 主 / 用户主页」区块文案（布局完全一致，只换文字）
UP_BOX_TEXTS = {
    "bilibili": {
        "frame": "UP 主（mid 或空间 URL，可选）",
        "check": "下载该 UP 主全部视频",
        "hint": "mid 或 https://space.bilibili.com/<mid>",
    },
    "douyin": {
        "frame": "抖音用户主页（主页 URL，可选）",
        "check": "下载该用户主页全部作品",
        "hint": "https://www.douyin.com/user/MS4w…",
    },
}

PROGRESS_RE = re.compile(r"\[download\]\s*(\d{1,3}(?:\.\d+)?)\s*%")
ENTRY_RE = re.compile(r"^=*\s*\[(\d+)/(\d+)\]")
DONE_SUMMARY_RE = re.compile(r"\[完成\]\s*成功\s*(\d+)\s*个[，,]\s*失败\s*(\d+)\s*个")
ERROR_MARKERS = ("[错误]", "下载失败", "ERROR:")
DONE_MARKERS = ("[完成]",)

DEFAULT_CONFIG: dict[str, Any] = {
    "output_dir": str(DEFAULT_DOWNLOAD_DIR),
    "bilibili_quality": "1080",
    "douyin_quality": "best",
    "up_limit": 0,
    "login_timeout": LOGIN_TIMEOUT_DEFAULT,
}


# --------------------------------------------------------------------------
# 纯逻辑：配置读写
# --------------------------------------------------------------------------


def bilibili_cookie_path() -> Path:
    """B 站登录态文件（脚本目录下的 cookies.txt，由 run_login 导出）。"""
    return PROJECT_DIR / BILIBILI_COOKIE_FILE_NAME


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """配置目录：%LOCALAPPDATA%\\autoDownloads（不写进项目目录）。"""
    environment = os.environ if env is None else env
    local = environment.get("LOCALAPPDATA") or ""
    if local.strip():
        return Path(local) / CONFIG_DIR_NAME
    return Path.home() / "AppData" / "Local" / CONFIG_DIR_NAME


def config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / CONFIG_FILE_NAME


def default_config() -> dict[str, Any]:
    return dict(DEFAULT_CONFIG)


def _coerce_quality(value: Any, allowed: Sequence[str], fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text if text in allowed else fallback


def quality_label(value: Any) -> str:
    """B 站档位 -> 下拉显示文案；未知值原样返回，绝不抛异常。"""
    text = str(value).strip() if value is not None else ""
    return BILIBILI_QUALITY_LABELS.get(text, text)


def default_cookie_path() -> Path:
    return PROJECT_DIR / BILIBILI_COOKIE_FILE_NAME


def read_quality_tiers(
    link: str,
    *,
    timeout: float = BILIBILI_QUALITY_TIMEOUT,
    error_sink: list[Any] | None = None,
    use_proxy: bool = False,
) -> list[dict[str, Any]] | None:
    """给界面用的档位读取（单次尝试）；失败返回 None，界面必须能回退。

    读取只走 B 站 web 接口，不需要 yt-dlp，也不会启动浏览器。
    error_sink 传入一个 list 时，失败原因（异常对象，或 None 表示"接口没给数据"）
    会被 append 进去，供上层分类成"超时 / 网络错误 / 接口返回异常"等可读原因——
    同时保持"任何失败都不抛异常"这个约束不变。

    use_proxy=False（默认）时**不传 proxy**，让 fetch_quality_tiers 用自己的默认策略：
    实测走系统代理时 playurl 会拖到 23 秒并失败，直连只要 5 秒，代理不能强加给自己。
    只有 use_proxy=True（兜底重试）且真的探测到系统代理时才传。
    """
    try:
        cookie_path = default_cookie_path()
        text = bili.read_cookie_text(cookie_path) or ""
        header = bili.cookie_header_for(bili.parse_netscape_cookies(text))
        kwargs: dict[str, Any] = {
            "cookies": header or None,
            "timeout": timeout,
        }
        if use_proxy:
            proxy = bili.detect_system_proxy()
            if proxy:
                kwargs["proxy"] = proxy
        tiers = bili.fetch_quality_tiers(link, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 界面永不因读档位崩溃
        if error_sink is not None:
            error_sink.append(exc)
        return None
    if not tiers and error_sink is not None:
        error_sink.append(None)
    return tiers


def bilibili_has_cookie() -> bool:
    """当前账号有没有 B 站登录 Cookie（读不到档位时用来区分"无 cookie"）。"""
    try:
        return bool((bili.read_cookie_text(default_cookie_path()) or "").strip())
    except Exception:  # noqa: BLE001
        return False


def classify_quality_error(
    exc: BaseException | None, *, timeout: float = BILIBILI_QUALITY_TIMEOUT, has_cookie: bool = True
) -> tuple[str, str]:
    """把读档位的失败原因分类成 (kind, 中文说明)。

    kind: timeout / network / api / no-cookie / unknown
    """
    if exc is None:
        if not has_cookie:
            return "no-cookie", "没有登录 Cookie，接口没有返回档位"
        return "unknown", "接口没有返回可用档位"
    name = type(exc).__name__
    lowered = f"{name}: {exc}".lower()
    if isinstance(exc, TimeoutError) or "timeout" in lowered or "timed out" in lowered:
        return "timeout", f"超时 {int(round(timeout))} 秒"
    if any(
        token in lowered
        for token in ("ssl", "connection", "network", "reset", "getaddrinfo", "urlerror", "timed out")
    ):
        return "network", f"网络错误（{name}）"
    if any(token in lowered for token in ("json", "decode", "value", "key", "index")):
        return "api", f"接口返回异常（{name}）"
    return "unknown", f"{name}: {exc}"


def read_bilibili_qualities(
    link: str,
    *,
    timeout: float = BILIBILI_QUALITY_TIMEOUT,
    max_attempts: int = QUALITY_MAX_ATTEMPTS,
    retry_delay: float = QUALITY_RETRY_DELAY,
    fast_failure_seconds: float | None = None,
    on_attempt: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """读 B 站档位：失败带可读原因；**按失败快慢**决定要不要重试一次。

    - 第一次在 fast_failure_seconds（默认 5 秒）内就失败：瞬时错误（连接重置等）→ 重试一次；
    - 第一次慢慢磨到接近 timeout 才失败：链路就是慢 → 不重试（否则用户再等 45 秒）；
    - 接口明确报错（无该视频/无权限/解析失败）→ 不重试。

    返回 {"ok", "tiers", "kind", "error", "attempts", "timeout", "retried",
          "attempt_seconds", "detail"}，绝不抛异常。
    on_attempt(第几次, 总次数, 上次失败原因, 本次路径) 每次尝试前调用。
    """
    payload: dict[str, Any] = {
        "ok": False,
        "tiers": [],
        "kind": "",
        "error": "",
        "attempts": 0,
        "timeout": float(timeout),
        "retried": False,
        "attempt_seconds": [],
        "detail": "",
    }
    total = max(1, int(max_attempts))
    threshold = (
        QUALITY_FAST_FAILURE_SECONDS if fast_failure_seconds is None else float(fast_failure_seconds)
    )
    has_cookie = bilibili_has_cookie()
    previous = ""
    attempt_seconds: list[float] = []
    for index in range(1, total + 1):
        # 第 1 次直连；只有重试时才兜底改用系统代理（实测代理对 playurl 有害）
        use_proxy = index > 1
        route = "直连（未传 proxy）"
        if use_proxy:
            detected = bili.detect_system_proxy()
            route = (
                f"系统代理 {detected}" if detected else "直连（系统未检测到代理，仍按直连重试）"
            )
        if on_attempt is not None:
            try:
                on_attempt(index, total, previous, route)
            except Exception:  # noqa: BLE001 - 回调不该影响读取
                pass
        sink: list[Any] = []
        started_at = time.monotonic()
        try:
            tiers = read_quality_tiers(link, timeout=timeout, error_sink=sink, use_proxy=use_proxy)
        except TypeError:
            # 兼容只接受 (link, timeout=...) 的旧实现/替身
            try:
                tiers = read_quality_tiers(link, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                sink.append(exc)
                tiers = None
        except Exception as exc:  # noqa: BLE001 - 底层实现抛异常也要分类，不能炸线程
            sink.append(exc)
            tiers = None
        attempt_seconds.append(time.monotonic() - started_at)
        payload["attempts"] = index
        payload["attempt_seconds"] = list(attempt_seconds)
        if tiers:
            payload.update(ok=True, tiers=list(tiers), kind="", error="")
            return payload
        kind, reason = classify_quality_error(
            sink[0] if sink else None, timeout=timeout, has_cookie=has_cookie
        )
        payload.update(kind=kind, error=reason)

        fast_failure = attempt_seconds[-1] < threshold
        if index >= total:
            break
        if kind not in ("timeout", "network"):
            payload["detail"] = "接口报错，未重试"
            break
        if not fast_failure:
            payload["detail"] = (
                f"本次读取耗时 {attempt_seconds[-1]:.1f} 秒，链路过慢故未重试"
            )
            break
        # 快失败（瞬时错误）：值得重试一次，第 2 次兜底改走系统代理
        payload["retried"] = True
        payload["detail"] = (
            f"上次 {attempt_seconds[-1]:.1f} 秒即失败（{'网络重置' if kind == 'network' else '超时'}）"
            f"，已重试 {min(total, index + 1) - index} 次"
        )
        previous = reason
        time.sleep(max(0.0, float(retry_delay)))
    if not payload["ok"]:
        if payload["retried"] and attempt_seconds:
            payload["detail"] = (
                f"上次 {attempt_seconds[0]:.1f} 秒即失败"
                f"（{'网络重置' if payload['kind'] == 'network' else '超时'}）"
                f"，已重试 {payload['attempts'] - 1} 次；本次耗时 {attempt_seconds[-1]:.1f} 秒"
            )
    return payload


def quality_tier_log_lines(tiers: Sequence[dict[str, Any]], link: str = "") -> list[str]:
    """把档位表排成界面日志行（B 站原文档位 + qn + 权限 + 可用性）。"""
    lines: list[str] = []
    if link:
        lines.append(f"[档位] 已读取清晰度：{link}")
    for tier in tiers:
        lines.append(
            "  %-12s qn=%-4d %s %s   编码 %s"
            % (
                tier.get("label", ""),
                int(tier.get("qn") or 0),
                "大会员" if tier.get("need_vip") else "免费  ",
                "可用  " if tier.get("available") else "不可用",
                "/".join(tier.get("codecs") or []) or "-",
            )
        )
    return lines


def quality_from_choice(text: Any) -> str:
    """下拉显示文案 -> 传给 argv 的真实取值（非法值回退默认档位）。

    同时兼容三种输入：显示文案（4K(2160)）、裸档位（2160）、
    以及任何带 "(数字)" 的变体，方便旧配置和手输场景。
    """
    raw = str(text).strip() if text is not None else ""
    for value, label in BILIBILI_QUALITY_LABELS.items():
        if raw == label:
            return value
    if raw in BILIBILI_QUALITIES:
        return raw
    match = re.search(r"\((\d{3,4})\)", raw)
    if match and match.group(1) in BILIBILI_QUALITIES:
        return match.group(1)
    return DEFAULT_CONFIG["bilibili_quality"]


def douyin_quality_label(item: Mapping[str, Any]) -> str:
    """抖音档位 -> 下拉显示文案：720P · 824kbps · h265（同分辨率靠码率区分）。"""
    label = str(item.get("label") or item.get("gear") or "").strip() or "未知档位"
    parts = [label]
    bitrate = item.get("bitrate")
    try:
        rate = int(bitrate) if bitrate is not None else 0
    except (TypeError, ValueError):
        rate = 0
    if rate > 0:
        parts.append(f"{rate}kbps")
    codec = str(item.get("codec") or "").strip()
    if codec:
        parts.append(codec)
    return " · ".join(parts)


def douyin_quality_log_lines(payload: Mapping[str, Any], elapsed: float = 0.0) -> list[str]:
    """抖音档位读取结果的日志行（标题/作者/时长 + 每档一行）。"""
    lines: list[str] = []
    title = str(payload.get("title") or "").strip()
    author = str(payload.get("author") or "").strip()
    head = f"[档位] 已读取抖音清晰度（耗时 {elapsed:.1f} 秒）"
    if title:
        head += f"：{title}"
    if author:
        head += f" · {author}"
    duration = payload.get("duration")
    try:
        seconds = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        head += f" · {seconds:.1f}s"
    lines.append(head)
    recommended = str(payload.get("recommended") or "")
    for item in payload.get("qualities") or []:
        if not isinstance(item, Mapping):
            continue
        mark = "（推荐）" if recommended and str(item.get("gear") or "") == recommended else ""
        lines.append(
            "  %-28s gear=%-10s 码率 %-6s 编码 %s%s"
            % (
                douyin_quality_label(item),
                str(item.get("gear") or ""),
                f"{item.get('bitrate')}kbps" if item.get("bitrate") else "-",
                str(item.get("codec") or "-"),
                mark,
            )
        )
    return lines


def douyin_quality_reader(module: Any | None = None) -> Callable[..., Any] | None:
    """抖音「读档位」能力的特性检测：拿不到就是 None，界面必须能回退。"""
    mod = douyin if module is None else module
    reader = getattr(mod, "list_qualities", None)
    return reader if callable(reader) else None


def read_douyin_qualities(
    url: str,
    *,
    timeout: float = DOUYIN_QUALITY_TIMEOUT,
    module: Any | None = None,
) -> dict[str, Any]:
    """读抖音档位；任何失败都返回 {"ok": False, "error": ...}，绝不抛给界面。

    list_qualities 不存在（功能还没落地）时也走这条路，并带上 unavailable 标记，
    让界面能给出「抖音档位读取暂不可用」这种可读提示。
    """
    reader = douyin_quality_reader(module)
    if reader is None:
        return {
            "ok": False,
            "unavailable": True,
            "error": "当前 douyin_downloader 还没有 list_qualities（抖音档位读取暂不可用）",
            "title": "",
            "author": "",
            "duration": 0.0,
            "qualities": [],
            "recommended": "",
        }
    result: Any = None
    try:
        result = reader(url, timeout=float(timeout))
    except TypeError:
        # 老签名/不吃 timeout 的实现：退一步只传 url
        try:
            result = reader(url)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "title": "",
                "author": "",
                "duration": 0.0,
                "qualities": [],
                "recommended": "",
            }
    except Exception as exc:  # noqa: BLE001 - 风控/网络/解析异常都按失败处理
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "title": "",
            "author": "",
            "duration": 0.0,
            "qualities": [],
            "recommended": "",
        }
    if not isinstance(result, dict):
        return {
            "ok": False,
            "error": f"list_qualities 返回了 {type(result).__name__}，不是 dict",
            "title": "",
            "author": "",
            "duration": 0.0,
            "qualities": [],
            "recommended": "",
        }
    payload = dict(result)
    payload.setdefault("qualities", [])
    payload.setdefault("recommended", "")
    return payload


def douyin_gear_supported(module: Any | None = None) -> bool:
    """抖音脚本是否支持 --gear（按 build_parser 里有没有 --gear 判断，做特性检测）。"""
    mod = douyin if module is None else module
    builder = getattr(mod, "build_parser", None)
    if not callable(builder):
        return False
    try:
        parser = builder()
    except Exception:  # noqa: BLE001 - 解析器构造失败就当不支持
        return False
    for action in getattr(parser, "_actions", ()) or ():
        if "--gear" in (getattr(action, "option_strings", ()) or ()):
            return True
    return False


def _coerce_int(value: Any, fallback: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def normalize_config(raw: Any) -> dict[str, Any]:
    """把任意来源（可能残缺/损坏）的配置规范成完整可用的字典。"""
    config = default_config()
    if not isinstance(raw, dict):
        return config

    output = raw.get("output_dir")
    if isinstance(output, str) and output.strip():
        config["output_dir"] = os.path.expandvars(os.path.expanduser(output.strip()))

    config["bilibili_quality"] = _coerce_quality(
        raw.get("bilibili_quality"), BILIBILI_QUALITIES, DEFAULT_CONFIG["bilibili_quality"]
    )
    config["douyin_quality"] = _coerce_quality(
        raw.get("douyin_quality"), DOUYIN_QUALITIES, DEFAULT_CONFIG["douyin_quality"]
    )
    config["up_limit"] = _coerce_int(
        raw.get("up_limit"), DEFAULT_CONFIG["up_limit"], minimum=0, maximum=UP_LIMIT_MAX
    )
    config["login_timeout"] = _coerce_int(
        raw.get("login_timeout"),
        DEFAULT_CONFIG["login_timeout"],
        minimum=LOGIN_TIMEOUT_MIN,
        maximum=LOGIN_TIMEOUT_MAX,
    )
    return config


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """读取配置；文件不存在、不是 JSON、类型不对都回退默认值，绝不抛异常。"""
    target = Path(path) if path is not None else config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default_config()
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return default_config()
    return normalize_config(raw)


def save_config(config: Mapping[str, Any], path: Path | str | None = None) -> Path | None:
    """写配置；UI 关窗/开始下载时调用。写失败只返回 None，不影响使用。"""
    target = Path(path) if path is not None else config_path()
    payload = normalize_config(dict(config))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return None
    return target


# --------------------------------------------------------------------------
# 纯逻辑：链接与 argv 构造
# --------------------------------------------------------------------------


def split_links(text: str) -> list[str]:
    """把输入框内容按行拆成链接列表：去空行、去首尾空白、按顺序去重。"""
    links: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        links.append(line)
    return links


def douyin_format_for(quality: str) -> str:
    """把界面清晰度映射成抖音（yt-dlp）的 --format 表达式。"""
    try:
        return DOUYIN_FORMAT_MAP[str(quality).strip()]
    except KeyError:
        raise ValueError(f"未知的抖音清晰度：{quality}（可选：{', '.join(DOUYIN_QUALITIES)}）")


def build_bilibili_argv(
    links: Iterable[str],
    output_dir: str | Path,
    quality: str,
    *,
    up: str | None = None,
    download_all: bool = False,
    limit: int = 0,
    proxy: str | None = None,
) -> list[str]:
    """拼 B 站下载命令行；未指定代理时绝不多塞 --proxy/--no-system-proxy。"""
    quality_text = str(quality).strip()
    if int(limit) < 0:
        raise ValueError("--limit 不能小于 0")

    if quality_text in BILIBILI_QUALITIES:
        argv: list[str] = ["-o", str(output_dir), "--quality", quality_text]
    elif quality_text.isdigit() and int(quality_text) in bili.KNOWN_QNS:
        # 从「读取清晰度」拿到的 B 站原文档位：传 --qn，绝不把界面文案塞进 argv
        argv = ["-o", str(output_dir), "--qn", quality_text]
    else:
        raise ValueError(
            f"未知的 B 站清晰度：{quality}"
            f"（可选：{', '.join(BILIBILI_QUALITIES)}，或 B 站 qn 数字）"
        )
    up_text = (up or "").strip()
    if up_text:
        argv += ["--up", up_text]
    if download_all:
        argv.append("--all")
    if int(limit) > 0:
        argv += ["--limit", str(int(limit))]
    proxy_text = (proxy or "").strip()
    if proxy_text:
        argv += ["--proxy", proxy_text]
    argv += [link for link in links if str(link).strip()]
    return argv


def build_douyin_argv(
    links: Iterable[str],
    output_dir: str | Path,
    quality: str,
    *,
    gear: str | None = None,
    up: str | None = None,
    download_all: bool = False,
    limit: int = 0,
    proxy: str | None = None,
    module: Any | None = None,
) -> list[str]:
    """拼抖音下载命令行：选中档位走 --gear，否则走 --format。

    抖音脚本没有 --up 参数：用户主页 URL 就是普通位置参数，配 --all 才会按主页
    收集作品。所以界面上填的主页 URL 追加进位置参数（与链接框内容按顺序去重）。
    gear 只在脚本真的支持 --gear 时才用（特性检测），否则回退到 --format。
    """
    if int(limit) < 0:
        raise ValueError("--limit 不能小于 0")

    targets: list[str] = []
    for link in links:
        text = str(link).strip()
        if text and text not in targets:
            targets.append(text)
    home = (up or "").strip()
    if home and home not in targets:
        targets.append(home)

    gear_text = str(gear or "").strip()
    argv: list[str] = ["-o", str(output_dir)]
    if gear_text and douyin_gear_supported(module):
        argv += ["--gear", gear_text]
    else:
        argv += ["--format", douyin_format_for(quality)]
    if download_all:
        argv.append("--all")
    if int(limit) > 0:
        argv += ["--limit", str(int(limit))]
    proxy_text = (proxy or "").strip()
    if proxy_text:
        argv += ["--proxy", proxy_text]
    argv += targets
    return argv


def parse_progress_percent(line: str) -> float | None:
    """从 "  [download]  42.3% of 208.10MiB at 5.13MiB/s" 里取出百分比。"""
    if not line or "[download]" not in line:
        return None
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return max(0.0, min(100.0, value))


def classify_log_line(line: str) -> str:
    """日志着色分类：返回 ttk Text 的 tag 名（空串表示不着色）。"""
    text = (line or "").strip()
    if any(marker in text for marker in ERROR_MARKERS):
        return "error"
    if any(marker in text for marker in DONE_MARKERS):
        return "done"
    return ""


class TaskCounter:
    """总进度计数：条目头 -> 上一个条目算成功；错误行 -> 当前条目算失败。"""

    def __init__(self) -> None:
        self.total = 0
        self.index = 0
        self.succeeded = 0
        self.failed = 0
        self._current_open = False

    def _close_current(self) -> None:
        if self._current_open:
            self.succeeded += 1
            self._current_open = False

    def feed(self, line: str) -> str | None:
        """吃一行日志，返回需要显示的总进度文字（None 表示不必刷新）。"""
        text = (line or "").strip()
        match = ENTRY_RE.match(text)
        if match:
            self._close_current()
            self.index = int(match.group(1))
            self.total = int(match.group(2))
            self._current_open = True
            return self.summary()

        summary = DONE_SUMMARY_RE.search(text)
        if summary:
            # 脚本自己给出的收尾统计最权威，直接以它为准。
            self.succeeded = int(summary.group(1))
            self.failed = int(summary.group(2))
            self._current_open = False
            return self.summary()

        if not self._current_open or not text:
            return None
        if any(marker in text for marker in ERROR_MARKERS):
            self.failed += 1
            self._current_open = False
            return self.summary()
        return None

    def finish(self) -> str:
        self._close_current()
        return self.summary()

    def reset(self) -> None:
        self.__init__()

    def summary(self) -> str:
        if self.total:
            return f"第 {self.index}/{self.total} 个 · {self.succeeded} 成功 {self.failed} 失败"
        if self.succeeded or self.failed:
            return f"{self.succeeded} 成功 {self.failed} 失败"
        return "尚未开始"


class CancelToken:
    """协作式取消标志：置位后不再开始新条目，并尽快中断当前下载。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()

    def should_start_next(self) -> bool:
        return not self._event.is_set()


class TaskCancelled(KeyboardInterrupt):
    """取消信号：继承 KeyboardInterrupt，避免被 main 的 except Exception 吞掉。"""


class QueueWriter(io.TextIOBase):
    """把子进程/线程的输出按"行"塞进队列的伪 stdout。

    yt-dlp 的进度行用 \r 原地刷新，这里把 \r 和 \n 都当行分隔符，
    避免整段进度挤成一行或者根本不出行。
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink
        self._buffer = ""

    def write(self, text: str) -> int:  # type: ignore[override]
        if not text:
            return 0
        if not isinstance(text, str):
            text = str(text)
        self._buffer += text
        while True:
            cut = self._next_break()
            if cut is None:
                break
            segment = self._buffer[:cut]
            self._buffer = self._buffer[cut + 1 :]
            self._emit(segment)
        return len(text)

    def _next_break(self) -> int | None:
        positions = [pos for pos in (self._buffer.find("\n"), self._buffer.find("\r")) if pos >= 0]
        return min(positions) if positions else None

    def _emit(self, segment: str) -> None:
        line = segment.rstrip()
        if line:
            self._sink(line)

    def flush(self) -> None:  # type: ignore[override]
        pending, self._buffer = self._buffer, ""
        self._emit(pending)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False


# --------------------------------------------------------------------------
# 取消注入（运行时猴子补丁，不修改现有模块）
# --------------------------------------------------------------------------


def install_cancel_hooks(module: Any, cancel: CancelToken) -> Callable[[], None]:
    """给下载器塞两个取消检查点，返回恢复原状的函数。

    1. Downloader.download 入口：条目结束后不再开始下一个；
    2. build_ydl_options 返回的 options["progress_hooks"]：中断当前文件。
    """

    patches: list[tuple[Any, str, Any]] = []

    def progress_hook(_status: Any) -> None:
        if cancel.is_set():
            raise TaskCancelled(CANCEL_NOTICE)

    downloader = getattr(module, "Downloader", None)
    original_download = getattr(downloader, "download", None)
    if downloader is not None and callable(original_download):
        def guarded_download(self: Any, *args: Any, **kwargs: Any) -> Any:
            if cancel.is_set():
                raise TaskCancelled(CANCEL_NOTICE)
            return original_download(self, *args, **kwargs)

        downloader.download = guarded_download
        patches.append((downloader, "download", original_download))

    original_builder = getattr(module, "build_ydl_options", None)
    if callable(original_builder):
        def builder_with_hook(*args: Any, **kwargs: Any) -> Any:
            options = original_builder(*args, **kwargs)
            if isinstance(options, dict):
                hooks = options.get("progress_hooks")
                if not isinstance(hooks, list):
                    hooks = []
                    options["progress_hooks"] = hooks
                hooks.append(progress_hook)
            return options

        module.build_ydl_options = builder_with_hook
        patches.append((module, "build_ydl_options", original_builder))

    def restore() -> None:
        for owner, name, original in reversed(patches):
            setattr(owner, name, original)

    return restore


# --------------------------------------------------------------------------
# 纯逻辑：状态行文案
# --------------------------------------------------------------------------


def format_timestamp(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "未知"
    if seconds <= 0:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


def bilibili_login_text(cookie_path: Path | str | None = None) -> str:
    """B 站登录态（只读 cookies.txt，不联网）。"""
    path = Path(cookie_path) if cookie_path is not None else bilibili_cookie_path()
    try:
        state = bili.inspect_cookie_file(path)
    except OSError:
        return "B 站登录态：读取 cookies.txt 失败"
    if not state["exists"]:
        return "B 站登录态：未检测到（没有 cookies.txt，请点「登录」）"
    if state["expired"]:
        return f"B 站登录态：已过期（SESSDATA 截止 {format_timestamp(state['expiry'])}），请重新登录"
    if state["logged_in"]:
        if state["expiry"]:
            return f"B 站登录态：有效（SESSDATA 截止 {format_timestamp(state['expiry'])}）"
        return "B 站登录态：有效（SESSDATA 存在，无过期时间）"
    return "B 站登录态：未登录（cookies.txt 里没有 SESSDATA）"


def douyin_login_cookie_file(profile_dir: Path | str | None = None) -> Path | None:
    """在持久化配置目录里找一个含 sessionid 的 Cookie 文件。"""
    root = Path(profile_dir) if profile_dir is not None else browser_engine.default_profile_dir()
    for relative in DOUYIN_COOKIE_CANDIDATES:
        candidate = root / relative
        if not candidate.is_file():
            continue
        try:
            with candidate.open("rb") as handle:
                blob = handle.read(8 * 1024 * 1024)
        except OSError:
            continue
        if b"sessionid" in blob:
            return candidate
    return None


def douyin_login_text(profile_dir: Path | str | None = None) -> str:
    """抖音登录态：只看持久化配置目录里有没有登录 Cookie（不启动浏览器）。"""
    root = Path(profile_dir) if profile_dir is not None else browser_engine.default_profile_dir()
    if not root.is_dir():
        return "抖音登录态：未检测到（配置目录不存在，首次下载会提示登录）"
    if douyin_login_cookie_file(root) is not None:
        return "抖音登录态：配置目录里已有登录 Cookie（可复用）"
    return "抖音登录态：配置目录里没有 sessionid（请点「登录」）"


def ffmpeg_text() -> str:
    try:
        path = bili.find_ffmpeg(None, PROJECT_DIR)
    except OSError:
        path = None
    if path:
        return f"ffmpeg：可用（{path}）"
    return "ffmpeg：未找到（1080P 是音视频分离流，必须安装 ffmpeg）"


def proxy_text() -> str:
    try:
        proxy = browser_engine.detect_system_proxy()
    except Exception:  # noqa: BLE001 - 注册表读取失败不该影响界面
        proxy = None
    return f"系统代理：{proxy}" if proxy else "系统代理：未检测到（直连）"


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


class LogPanel(ttk.Frame):
    """只读日志区：红色 [错误]、绿色 [完成]。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=(10, 0, 10, 8))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.text = tk.Text(
            self, height=LOG_TEXT_HEIGHT, wrap="none", state="disabled", font=LOG_FONT
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=self.scroll.set)
        self.text.tag_configure("error", foreground="#c0392b")
        self.text.tag_configure("done", foreground="#1e8449")
        self.text.tag_configure("notice", foreground="#b9770e")

    def append(self, line: str, tag: str | None = None) -> None:
        if tag is None:
            tag = classify_log_line(line)
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", (tag,) if tag else ())
        self._trim()
        self.text.configure(state="disabled")
        self.text.see("end")

    def _trim(self) -> None:
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.text.delete("1.0", f"{lines - LOG_MAX_LINES + 1}.0")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class DownloadTab(ttk.Frame):
    """一个平台的输入页；B 站页额外带 UP 主 / --all / --limit。"""

    def __init__(self, master: tk.Misc, app: "DownloadApp", platform: str) -> None:
        super().__init__(master, padding=(10, 8))
        self.app = app
        self.platform = platform
        self.columnconfigure(0, weight=1)

        self.output_var = tk.StringVar(value=app.config["output_dir"])
        quality_default = (
            app.config["bilibili_quality"] if platform == "bilibili" else app.config["douyin_quality"]
        )
        # 读到档位后，下拉项变成 B 站原文标签：label -> qn。
        self.quality_qn_map: dict[str, int] = {}
        # 抖音读到档位后：下拉文案 -> gear（720_2_1）
        self.quality_gear_map: dict[str, str] = {}
        # 读档位失败/没读过时用的固定档位（抖音回退 --format 时也用它）
        self._douyin_fixed_quality = str(quality_default)
        self.quality_read_done = False
        # B 站下拉显示可读文案（4K(2160)），抖音保持原样。
        self.quality_var = tk.StringVar(
            value=quality_label(quality_default) if platform == "bilibili" else quality_default
        )

        self._build_links()
        self._build_up()
        self._build_options()
        self._build_actions()
        self._build_status()
        self._build_progress()

    # -- 构建 ------------------------------------------------------------

    def _build_links(self) -> None:
        box = ttk.LabelFrame(self, text="链接（一个链接一行，可整段粘贴分享文案）", padding=8)
        box.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        box.columnconfigure(0, weight=1)

        self.links_text = tk.Text(box, height=6, wrap="none", undo=True)
        self.links_text.grid(row=0, column=0, sticky="nsew")
        links_scroll = ttk.Scrollbar(box, orient="vertical", command=self.links_text.yview)
        links_scroll.grid(row=0, column=1, sticky="ns")
        self.links_text.configure(yscrollcommand=links_scroll.set)
        # 链接一变就排队（防抖）去后台读清晰度；tkinter 的 <<Modified>> 需要手动复位标志位。
        self.links_text.bind("<<Modified>>", self._on_links_modified)

        row = ttk.Frame(box)
        row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.import_button = ttk.Button(row, text="从文件导入…", command=self._import_file)
        self.import_button.pack(side="left")
        self.clear_button = ttk.Button(row, text="清空链接", command=lambda: self.links_text.delete("1.0", "end"))
        self.clear_button.pack(side="left", padx=(6, 0))

    def _build_up(self) -> None:
        """「UP 主 / 用户主页」区块：两个标签页布局一致，只换文案。"""
        texts = UP_BOX_TEXTS[self.platform]
        box = ttk.LabelFrame(self, text=texts["frame"], padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        box.columnconfigure(0, weight=1)

        self.up_entry = ttk.Entry(box)
        self.up_entry.grid(row=0, column=0, sticky="ew")
        self.up_entry.configure(width=44)

        self.up_all_var = tk.BooleanVar(value=False)
        self.all_check = ttk.Checkbutton(box, text=texts["check"], variable=self.up_all_var)
        self.all_check.grid(row=0, column=1, sticky="w", padx=(10, 0))

        ttk.Label(box, text="最多").grid(row=0, column=2, sticky="e", padx=(12, 2))
        self.limit_var = tk.IntVar(value=int(self.app.config["up_limit"]))
        self.limit_spin = ttk.Spinbox(box, from_=0, to=UP_LIMIT_MAX, width=6, textvariable=self.limit_var)
        self.limit_spin.grid(row=0, column=3, sticky="w")
        ttk.Label(box, text="个（0=不限）").grid(row=0, column=4, sticky="w", padx=(2, 0))

        self.up_hint_label = ttk.Label(box, text=texts["hint"])
        self.up_hint_label.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))

    def _build_options(self) -> None:
        row = ttk.Frame(self)
        row.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        row.columnconfigure(3, weight=1)

        ttk.Label(row, text="清晰度").grid(row=0, column=0, sticky="w")
        if self.platform == "bilibili":
            values = BILIBILI_QUALITY_CHOICES
            width = 10  # 「最佳(best)」比 1080P 宽，窄了会被截断
        else:
            values = DOUYIN_QUALITIES
            width = 8
        self.quality_box = ttk.Combobox(
            row, values=list(values), state="readonly", width=width, textvariable=self.quality_var
        )
        self.quality_box.grid(row=0, column=1, sticky="w", padx=(6, 8))

        self.read_quality_button: Any = None
        if self.platform == "bilibili":
            # 档位现在是自动读的；这个按钮只负责"强制重读当前链接"。
            self.read_quality_button = ttk.Button(
                row, text="刷新清晰度", command=self._refresh_qualities
            )
            self.read_quality_button.grid(row=0, column=5, sticky="w", padx=(0, 14))

        ttk.Label(row, text="保存目录").grid(row=0, column=2, sticky="w")
        self.output_entry = ttk.Entry(row, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=3, sticky="ew", padx=(6, 6))
        self.browse_button = ttk.Button(row, text="浏览…", command=self._browse_output)
        self.browse_button.grid(row=0, column=4, sticky="e")

    def _build_actions(self) -> None:
        row = ttk.Frame(self)
        row.grid(row=3, column=0, sticky="w", pady=(0, 6))

        self.start_button = ttk.Button(row, text="开始下载", command=self.app.start_download)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="停止", command=self.app.stop_download, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        login_text = (
            "登录 B 站（会打开一次浏览器窗口）"
            if self.platform == "bilibili"
            else "登录抖音（会打开一次浏览器窗口）"
        )
        self.login_button = ttk.Button(row, text=login_text, command=self.app.start_login)
        self.login_button.pack(side="left", padx=(6, 0))
        self.open_dir_button = ttk.Button(row, text="打开保存目录", command=self._open_output_dir)
        self.open_dir_button.pack(side="left", padx=(6, 0))

    def _build_status(self) -> None:
        box = ttk.LabelFrame(self, text="状态", padding=8)
        box.grid(row=4, column=0, sticky="ew", pady=(0, 6))

        self.status_label = ttk.Label(box, text="准备就绪")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.login_label = ttk.Label(box, text="")
        self.login_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.env_label = ttk.Label(box, text="")
        self.env_label.grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.proxy_label = ttk.Label(box, text="")
        self.proxy_label.grid(row=3, column=0, sticky="w", pady=(2, 0))

        self.refresh_button = ttk.Button(box, text="刷新状态", command=self.app.refresh_status)
        self.refresh_button.grid(row=0, column=1, rowspan=4, sticky="e", padx=(10, 0))
        box.columnconfigure(0, weight=1)

    def _build_progress(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=5, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(box, orient="horizontal", mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(box, text="尚未开始")
        self.progress_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

    # -- 读取 ------------------------------------------------------------

    def links(self) -> list[str]:
        return split_links(self.links_text.get("1.0", "end"))

    def current_link(self) -> str:
        """当前要读档位的链接：链接框里第一条。"""
        links = self.links()
        return links[0] if links else ""

    def quality(self) -> str:
        """下拉里显示的是可读文案；B 站页要还原成 argv 认的取值。

        - 没读过档位：显示 4K(2160)/1080P…，返回 2160/1080…
        - 读到之后：显示 B 站原文（超清 4K/真彩 HDR…），返回 qn 数字（120/125…），
          交给 --qn 精确定档；界面文案永远不会进 argv。
        - 抖音读到档位后下拉显示「720P · 824kbps · h265」，此处仍返回固定档位，
          真正的档位由 selected_gear() 提供给 --gear。
        """
        text = self.quality_var.get().strip()
        if self.platform != "bilibili":
            if text in DOUYIN_QUALITIES:
                self._douyin_fixed_quality = text
                return text
            return self._douyin_fixed_quality
        if text in self.quality_qn_map:
            return str(self.quality_qn_map[text])
        return quality_from_choice(text)

    def selected_gear(self) -> str:
        """抖音选中项的 gear（B 站/未选中返回空串）。"""
        if self.platform != "douyin":
            return ""
        return self.quality_gear_map.get(self.quality_var.get().strip(), "")

    def _on_links_modified(self, _event: Any = None) -> None:
        """链接框内容变化 -> 复位 Modified 标志 -> 排队（防抖）读清晰度。"""
        try:
            if not self.links_text.edit_modified():
                return
            self.links_text.edit_modified(False)
        except tk.TclError:  # 控件已销毁
            return
        self.app.schedule_quality_read(self.platform)

    def _read_qualities(self) -> None:
        """旧入口（保留）：等价于「刷新清晰度」，强制重读当前链接。"""
        self._refresh_qualities()

    def _refresh_qualities(self) -> None:
        """「刷新清晰度」按钮：跳过缓存，立刻重读当前链接。"""
        if not self.current_link():
            self.app.log.append("[提示] 请先在链接框里填一条链接，再刷新清晰度。", "notice")
            return
        self.app.start_quality_read(self.platform, force=True)

    def apply_douyin_qualities(
        self,
        payload: Mapping[str, Any] | None,
        *,
        url: str = "",
        elapsed: float = 0.0,
        log_failure: bool = True,
    ) -> bool:
        """把抖音档位应用到下拉框；失败/不可用时回退固定档位。"""
        reason = ""
        if isinstance(payload, Mapping):
            reason = str(payload.get("error") or "").strip()
        items = list(payload.get("qualities") or []) if isinstance(payload, Mapping) else []
        if not isinstance(payload, Mapping) or not payload.get("ok") or not items:
            self.reset_quality_choices()
            if log_failure:
                if isinstance(payload, Mapping) and payload.get("unavailable"):
                    head = "[档位] 抖音档位读取暂不可用"
                else:
                    head = "[档位] 抖音清晰度读取失败"
                self.app.log.append(
                    f"{head}，已回退固定档位（{DOUYIN_QUALITIES_FALLBACK_TEXT}）。"
                    f"原因：{reason or '没有返回可用档位'}" + (f"（{url}）" if url else ""),
                    "notice" if isinstance(payload, Mapping) and payload.get("unavailable") else "error",
                )
            self.status_label.configure(
                text=f"清晰度：读取失败，已回退固定档位（{DOUYIN_QUALITIES_FALLBACK_TEXT}）"
            )
            return False

        labels: list[str] = []
        gear_map: dict[str, str] = {}
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            label = douyin_quality_label(item)
            if not label or label in gear_map:
                label = f"{label or '未知档位'} #{index + 1}"
            labels.append(label)
            gear_map[label] = str(item.get("gear") or "")
        if not labels:
            self.reset_quality_choices()
            if log_failure:
                self.app.log.append(
                    f"[档位] 抖音清晰度读取失败，已回退固定档位"
                    f"（{DOUYIN_QUALITIES_FALLBACK_TEXT}）。原因：档位列表为空",
                    "error",
                )
            self.status_label.configure(text="清晰度：读取失败，已回退固定档位")
            return False

        self.quality_gear_map = gear_map
        self.quality_read_done = True
        self.quality_box.configure(values=labels)
        recommended = str(payload.get("recommended") or "").strip()
        selected = next((lab for lab, gear in gear_map.items() if gear == recommended), labels[0])
        self.quality_var.set(selected)
        for line in douyin_quality_log_lines(payload, elapsed=elapsed):
            self.app.log.append(line)
        if douyin_gear_supported(PLATFORM_MODULES.get("douyin", douyin)):
            self.app.log.append("[档位] 选中后会以 --gear <档位名> 传入。")
        else:
            self.app.log.append(
                "[档位] 当前抖音脚本还不支持 --gear，下载会按 --format 传入固定档位。",
                "notice",
            )
        self.status_label.configure(
            text=f"清晰度：已读取抖音档位（{len(labels)} 档，耗时 {elapsed:.1f} 秒）"
        )
        return True

    def apply_quality_tiers(self, tiers: Sequence[dict[str, Any]] | None, link: str = "") -> bool:
        """把档位表应用到下拉框；失败时回退固定档位。返回是否成功。"""
        if not tiers:
            self.reset_quality_choices()
            self.app.log.append(
                "[档位] 读不到档位表，已回退到固定档位"
                "（4K(2160)/2K(1440)/1080P/720P/480P/最佳(best)）。"
                "可以先用命令行 --list-qualities 排查。",
                "notice",
            )
            return False
        labels = [str(tier.get("label") or tier.get("qn")) for tier in tiers]
        self.quality_qn_map = {
            str(tier.get("label") or tier.get("qn")): int(tier.get("qn") or 0)
            for tier in tiers
        }
        self.quality_box.configure(values=labels)
        # 默认选「当前账号可用的最高档」：档位表是按高到低给的，取第一个可用的；
        # 全不可用（或没有 available 字段）时退回第一项。
        selected = labels[0]
        for tier, label in zip(tiers, labels):
            if tier.get("available", True):
                selected = label
                break
        self.quality_read_done = True
        self.quality_var.set(selected)
        for line in quality_tier_log_lines(tiers, link=link):
            self.app.log.append(line)
        self.app.log.append(
            "[档位] 选中后会以 --qn <数字> 传入，界面文案不会进命令行。"
        )
        return True

    def reset_quality_choices(self) -> None:
        """回到没读过档位时的固定档位下拉。"""
        self.quality_qn_map = {}
        self.quality_gear_map = {}
        self.quality_read_done = False
        if self.platform == "bilibili":
            self.quality_box.configure(values=list(BILIBILI_QUALITY_CHOICES))
            self.quality_var.set(quality_label(self.app.config["bilibili_quality"]))
        else:
            self.quality_box.configure(values=list(DOUYIN_QUALITIES))
            self.quality_var.set(self.app.config["douyin_quality"])

    def output_dir(self) -> Path:
        text = self.output_var.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        return Path(os.path.expandvars(os.path.expanduser(text)))

    def up(self) -> str:
        """B 站：mid 或空间 URL；抖音：用户主页 URL（会作为位置参数传给脚本）。"""
        return self.up_entry.get().strip()

    def download_all(self) -> bool:
        return bool(self.up_all_var.get())

    def limit(self) -> int:
        try:
            return max(0, int(self.limit_var.get()))
        except (tk.TclError, ValueError):
            return 0

    def build_argv(self) -> list[str]:
        if self.platform == "bilibili":
            return build_bilibili_argv(
                self.links(),
                self.output_dir(),
                self.quality(),
                up=self.up(),
                download_all=self.download_all(),
                limit=self.limit(),
            )
        module = PLATFORM_MODULES.get("douyin", douyin)
        return build_douyin_argv(
            self.links(),
            self.output_dir(),
            self.quality(),
            gear=self.selected_gear(),
            up=self.up(),
            download_all=self.download_all(),
            limit=self.limit(),
            module=module,
        )

    # -- 交互 ------------------------------------------------------------

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择包含链接的文本文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            try:
                text = Path(path).read_text(encoding="gbk", errors="replace")
            except OSError as exc:
                messagebox.showerror("读取失败", f"无法读取文件：{exc}")
                return
        imported = text.strip("\n")
        if not imported:
            self.app.log.append(f"[提示] 文件里没有内容：{path}", "notice")
            return
        existing = self.links_text.get("1.0", "end-1c")
        if existing.strip():
            self.links_text.insert("end", "\n" + imported + "\n")
        else:
            self.links_text.insert("1.0", imported + "\n")
        self.app.log.append(f"[信息] 已从文件导入：{path}")

    def _browse_output(self) -> None:
        initial = self.output_var.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        chosen = filedialog.askdirectory(title="选择保存目录", initialdir=initial or str(PROJECT_DIR))
        if chosen:
            self.output_var.set(chosen)
            self.app.log.append(f"[信息] 保存目录改为：{chosen}")

    def _open_output_dir(self) -> None:
        target = self.output_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法创建目录：{exc}")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                self.app.log.append(f"[提示] 请手动打开目录：{target}")
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开目录：{exc}")

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def set_login_busy(self, busy: bool) -> None:
        self.login_button.configure(state="disabled" if busy else "normal")


class DownloadApp:
    """主窗口：Notebook 两个标签页 + 底部公共日志区 + 工作线程调度。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        config_path_override: Path | str | None = None,
        autostart_poll: bool = True,
    ) -> None:
        self.root = root
        self.config_path = Path(config_path_override) if config_path_override else config_path()
        self.config = load_config(self.config_path)
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = CancelToken()
        self.counter = TaskCounter()
        self.busy = False
        self.logged_in_progress = False
        self.closed = False
        self.worker: threading.Thread | None = None
        self._poll_id: str | None = None
        self._close_wait_id: str | None = None
        self._close_deadline = 0.0
        self.close_wait_seconds = CLOSE_WAIT_SECONDS
        self.destroyed = False
        self._window_width = 1000
        self._window_height = 800
        self.log_panel_min_height = LOG_PANEL_MIN_HEIGHT
        # 清晰度自动读取：每页一个请求序号（旧结果按序号丢弃）+ 防抖定时器 + 按 URL 缓存
        self.quality_debounce_ms = QUALITY_DEBOUNCE_MS
        self.quality_retry_delay = QUALITY_RETRY_DELAY
        self.quality_fast_failure_seconds = QUALITY_FAST_FAILURE_SECONDS
        self._quality_seq: dict[str, int] = {"bilibili": 0, "douyin": 0}
        self._quality_debounce: dict[str, str | None] = {"bilibili": None, "douyin": None}
        self._quality_cache: dict[tuple[str, str], tuple[float, Any]] = {}
        # (平台, URL) -> 该请求的序号；用来把同一 URL 的重复触发合并成一次接口调用
        self._quality_in_flight: dict[tuple[str, str], int] = {}

        root.title(APP_TITLE)
        root.geometry("1000x800")
        root.minsize(880, 680)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build()
        self._fit_log_panel()

        self.refresh_status()
        self.log.append("[信息] 界面只调用现有下载脚本，不改动任何下载逻辑。")
        if autostart_poll:
            self._poll_id = root.after(QUEUE_POLL_MS, self._poll)

    # -- 构建 ------------------------------------------------------------

    def _build(self) -> None:
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        self.container = container
        # 权重 1:1 让窗口变大时日志区跟着长；minsize 保证窗口变小时日志区不被压扁
        # （权重更大的行在空间不足时反而被压得更狠，所以这里不给日志行更大权重）。
        container.rowconfigure(0, weight=1)
        container.rowconfigure(1, weight=0)
        container.rowconfigure(2, weight=1, minsize=LOG_PANEL_MIN_HEIGHT)
        container.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(container)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))

        self.tabs: dict[str, DownloadTab] = {}
        for platform, title in (("bilibili", "B 站"), ("douyin", "抖音")):
            tab = DownloadTab(self.notebook, self, platform)
            self.notebook.add(tab, text=title)
            self.tabs[platform] = tab
        # 切页时顺手（防抖后）重试一次：抖音读取失败后没有单独按钮，这是重试入口之一
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        shared = ttk.Frame(container)
        shared.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        ttk.Label(shared, text="登录等待超时（秒）").pack(side="left")
        self.login_timeout_var = tk.IntVar(value=int(self.config["login_timeout"]))
        self.login_timeout_spin = ttk.Spinbox(
            shared,
            from_=LOGIN_TIMEOUT_MIN,
            to=LOGIN_TIMEOUT_MAX,
            increment=30,
            width=7,
            textvariable=self.login_timeout_var,
        )
        self.login_timeout_spin.pack(side="left", padx=(6, 12))
        ttk.Label(shared, text=f"（默认 {LOGIN_TIMEOUT_DEFAULT} 秒，只在点「登录」时使用）").pack(side="left")

        log_box = ttk.LabelFrame(container, text="运行日志", padding=(6, 4, 6, 2))
        log_box.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 8))
        log_box.rowconfigure(1, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.log = LogPanel(log_box)
        self.log.grid(row=1, column=0, sticky="nsew")
        clear_row = ttk.Frame(log_box)
        clear_row.grid(row=0, column=0, sticky="e")
        self.clear_log_button = ttk.Button(clear_row, text="清空日志", command=self.log.clear)
        self.clear_log_button.pack(side="right")
        self.log_box = log_box

    def _fit_log_panel(self) -> None:
        """保证日志区至少露出 LOG_MIN_VISIBLE_LINES 行，窗口不够就自己长高。

        行高随 DPI/字体变化，所以下限用实测 linespace + 面板装饰高度算：
        只靠 grid 的 weight 分配时，空间不足会让日志行被压到几乎看不见
        （权重越大的行反而被压得越狠），这里用 minsize 钉住下限。
        """
        try:
            self.root.update_idletasks()
            metrics = tkfont.Font(root=self.root, font=self.log.text.cget("font"))
            linespace = metrics.metrics("linespace") or 14
            # 面板自然请求高度就是"10 行 + 装饰"，直接拿它当下限最准（含 DPI 缩放）
            natural = int(self.log_box.winfo_reqheight())
            text_need = int(linespace * LOG_MIN_VISIBLE_LINES)
            min_panel = max(LOG_PANEL_MIN_HEIGHT, natural, text_need + LOG_PANEL_CHROME)
            self.container.rowconfigure(2, minsize=min_panel)
            self.log_panel_min_height = min_panel

            self.root.update_idletasks()
            needed = int(self.root.winfo_reqheight())
            width = self.root.winfo_width()
            width = width if width > 1 else self._window_width
            height = self.root.winfo_height()
            height = height if height > 1 else self._window_height
            screen = int(self.root.winfo_screenheight() or 0)
            target = max(height, needed + 8)
            if screen > 200:
                target = min(target, max(screen - 80, height))
            if target > height:
                self.root.geometry(f"{width}x{int(target)}")
        except tk.TclError:  # 没有显示器等极端情况：保持默认布局，不影响逻辑
            pass

    # -- 状态 ------------------------------------------------------------

    def refresh_status(self) -> None:
        bili_tab = self.tabs["bilibili"]
        douyin_tab = self.tabs["douyin"]
        proxy = proxy_text()
        bili_tab.login_label.configure(text=bilibili_login_text())
        bili_tab.env_label.configure(text=ffmpeg_text())
        bili_tab.proxy_label.configure(text=proxy)
        douyin_tab.login_label.configure(text=douyin_login_text())
        douyin_tab.env_label.configure(text="下载引擎：默认 auto（单视频走浏览器解析）")
        douyin_tab.proxy_label.configure(text=proxy)

    def current_tab(self) -> DownloadTab:
        current = self.notebook.index(self.notebook.select())
        return self.tabs["bilibili"] if current == 0 else self.tabs["douyin"]

    def _on_tab_changed(self, _event: Any = None) -> None:
        try:
            tab = self.current_tab()
        except (tk.TclError, IndexError):
            return
        if tab.current_link():
            self.schedule_quality_read(tab.platform, delay_ms=300)

    # -- 清晰度自动读取 --------------------------------------------------

    def schedule_quality_read(
        self, platform: str, *, delay_ms: int | None = None, force: bool = False
    ) -> None:
        """防抖：每次触发都重置定时器，连读输入只发最后一次请求。"""
        self._cancel_quality_debounce(platform)
        tab = self.tabs.get(platform)
        if tab is not None and tab.current_link():
            # 立刻给反馈：现在只是等输入停顿，避免用户以为界面没反应
            tab.status_label.configure(text=QUALITY_PENDING_TEXT[platform])
        delay = self.quality_debounce_ms if delay_ms is None else delay_ms
        try:
            self._quality_debounce[platform] = self.root.after(
                max(0, int(delay)), lambda: self.start_quality_read(platform, force=force)
            )
        except tk.TclError:  # 窗口已销毁
            self._quality_debounce[platform] = None

    def _cancel_quality_debounce(self, platform: str) -> None:
        pending = self._quality_debounce.get(platform)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
            self._quality_debounce[platform] = None

    def _quality_cache_lookup(self, platform: str, url: str) -> tuple[bool, Any]:
        """缓存命中判断：成功结果永久命中；失败结果只在 QUALITY_RETRY_SECONDS 内命中。"""
        entry = self._quality_cache.get((platform, url))
        if entry is None:
            return False, None
        stamp, payload = entry
        if platform == "bilibili":
            if isinstance(payload, Mapping):
                ok = bool(payload.get("ok"))
            else:
                ok = isinstance(payload, list) and bool(payload)
        else:
            ok = isinstance(payload, Mapping) and bool(payload.get("ok"))
        if ok:
            return True, payload
        if time.monotonic() - stamp < QUALITY_RETRY_SECONDS:
            return True, payload
        return False, None

    def start_quality_read(self, platform: str, *, force: bool = False) -> bool:
        """发起一次档位读取（后台线程）；返回是否去读了/用了缓存/复用了在途请求。

        同一个 (平台, URL) 已有请求在跑时**合并**：不再发第二个请求，直接等前一个的结果
        （所以给同一个链接只会打一次接口）。不同 URL 之间仍然按序号判过期。
        """
        self._cancel_quality_debounce(platform)
        tab = self.tabs.get(platform)
        if tab is None:
            return False
        url = tab.current_link()

        if not url:
            self._quality_seq[platform] = self._quality_seq.get(platform, 0) + 1
            if tab.quality_read_done:
                tab.reset_quality_choices()
                self.log.append("[档位] 链接为空，已回退固定档位。", "notice")
            return False

        # 1) 同一 URL 已在读取中：合并，绝不重复打接口
        pending_seq = self._quality_in_flight.get((platform, url))
        if pending_seq is not None:
            # 把序号收回在途请求上，这样它回来时仍然有效（不会被当成过期丢掉）
            self._quality_seq[platform] = pending_seq
            if force:
                self.log.append(
                    f"[档位] 当前链接已在读取中，刷新请求已合并（不重复请求）：{url}", "notice"
                )
            tab.status_label.configure(text=f"正在读取清晰度（沿用已在进行的请求）…（{url}）")
            return True

        # 2) 真正发起新请求：序号自增，任何更早发出的请求回来都会被判过期
        self._quality_seq[platform] = self._quality_seq.get(platform, 0) + 1
        seq = self._quality_seq[platform]

        if not force:
            hit, payload = self._quality_cache_lookup(platform, url)
            if hit:
                self._apply_quality_result(platform, seq, url, payload, 0.0, from_cache=True)
                return True

        self._quality_in_flight[(platform, url)] = seq
        if platform == "bilibili":
            tab.status_label.configure(
                text=f"正在读取清晰度（最多 {int(self.quality_timeout('bilibili'))} 秒，"
                f"失败会自动重试一次）…（{url}）"
            )
        else:
            tab.status_label.configure(
                text=f"正在启动无头浏览器读取清晰度（约 3–10 秒）…（{url}）"
            )
        threading.Thread(
            target=self._quality_worker,
            args=(platform, seq, url),
            name=f"autoDownloads-quality-{platform}",
            daemon=True,
        ).start()
        return True

    def quality_timeout(self, platform: str) -> float:
        """各平台读档位的超时（B 站 25 秒、抖音 30 秒）。"""
        return float(
            BILIBILI_QUALITY_TIMEOUT if platform == "bilibili" else DOUYIN_QUALITY_TIMEOUT
        )

    def _on_quality_attempt(
        self, platform: str, seq: int, index: int, total: int, previous: str, route: str = ""
    ) -> None:
        """工作线程里被调用：把"第几次尝试/走哪条路径"投进队列，由 UI 线程显示（不碰控件）。"""
        self.queue.put(("quality_attempt", platform, seq, index, total, previous, route))

    def _quality_worker(self, platform: str, seq: int, url: str) -> None:
        started = time.monotonic()
        payload: Any = None
        try:
            if platform == "bilibili":
                payload = read_bilibili_qualities(
                    url,
                    timeout=self.quality_timeout("bilibili"),
                    max_attempts=QUALITY_MAX_ATTEMPTS,
                    retry_delay=self.quality_retry_delay,
                    fast_failure_seconds=self.quality_fast_failure_seconds,
                    on_attempt=lambda index, total, previous, route="": self._on_quality_attempt(
                        platform, seq, index, total, previous, route
                    ),
                )
            else:
                payload = read_douyin_qualities(url, timeout=self.quality_timeout("douyin"))
        except BaseException:  # noqa: BLE001 - 读档位线程绝不能静默死掉
            payload = None
        elapsed = time.monotonic() - started
        self.queue.put(("qualities", platform, seq, url, payload, elapsed))

    def _show_quality_attempt(
        self,
        platform: str,
        seq: int,
        index: int,
        total: int,
        previous: str,
        route: str = "",
    ) -> None:
        """显示"第几次尝试 + 这次走哪条路径"：让用户知道是在重试，不是卡死。"""
        tab = self.tabs.get(platform)
        if tab is None or self.closed:
            return
        if seq != self._quality_seq.get(platform):
            return  # 已经换链接了，别把旧进度贴上去
        if index <= 1:
            return
        suffix = f"（上次：{previous}）" if previous else ""
        path = f"；本次路径：{route}" if route else ""
        tab.status_label.configure(
            text=f"正在读取清晰度（第 {index}/{total} 次尝试）…{suffix}{path}"
        )
        self.log.append(
            f"[档位] 第 {index}/{total} 次尝试读取清晰度…{suffix}{path}", "notice"
        )

    def _apply_quality_result(
        self,
        platform: str,
        seq: int,
        url: str,
        payload: Any,
        elapsed: float = 0.0,
        from_cache: bool = False,
    ) -> bool:
        """把结果落到下拉框；过期/链接已变的结果直接丢弃。"""
        # 这次请求已完成（无论结果是否被采纳），先把在途标记销掉
        self._quality_in_flight.pop((platform, url), None)
        tab = self.tabs.get(platform)
        if tab is None:
            return False
        if tab.current_link() != url:
            self.log.append(f"[档位] 丢弃过期结果（链接已变）：{url}")
            return False
        if seq != self._quality_seq.get(platform):
            self.log.append(
                f"[档位] 丢弃过期结果（请求 #{seq}，当前 #{self._quality_seq.get(platform)}）：{url}",
            )
            return False
        self._quality_cache[(platform, url)] = (time.monotonic(), payload)
        if platform == "bilibili":
            if isinstance(payload, Mapping):
                tiers = payload.get("tiers") or []
                reason = str(payload.get("error") or "未知原因")
                detail = str(payload.get("detail") or "").strip()
            else:
                tiers, reason, detail = payload, "未知原因", ""
            ok = tab.apply_quality_tiers(tiers, link=url)
            if not ok and not from_cache:
                # 把"为什么没重试/重试了几次"写清楚，并告诉用户怎么手动再来一次
                middle = f"：{detail}" if detail else ""
                self.log.append(
                    f"[档位] B 站清晰度读取失败，已回退固定档位（{reason}）{middle}"
                    f"；可点「刷新清晰度」重试（{url}）",
                    "error",
                )
            hint = "；可点「刷新清晰度」重试"
            tab.status_label.configure(
                text=(
                    "清晰度：已读取 B 站档位（选中即按 --qn 传入）"
                    if ok
                    else (
                        "清晰度：缓存里的读取是失败的，已回退固定档位"
                        if from_cache
                        else f"清晰度：读取失败（{reason}）"
                        + (f"：{detail}" if detail else "")
                        + hint
                    )
                )
            )
            return ok
        ok = tab.apply_douyin_qualities(
            payload, url=url, elapsed=elapsed, log_failure=not from_cache
        )
        if from_cache and not ok:
            tab.status_label.configure(text="清晰度：缓存里的读取是失败的，已回退固定档位")
        return ok

    def cancel_quality_reads(self) -> None:
        """打断档位读取：停掉防抖定时器，并让在飞的结果全部过期。

        线程本身是 daemon 且各自带超时，不会拖住关窗。
        """
        for platform in ("bilibili", "douyin"):
            self._cancel_quality_debounce(platform)
            self._quality_seq[platform] = self._quality_seq.get(platform, 0) + 1
        self._quality_in_flight.clear()
        for tab in self.tabs.values():
            if not self.busy:
                tab.status_label.configure(text="已取消清晰度读取")

    # -- 任务启动 --------------------------------------------------------

    def start_download(self) -> None:
        if self.busy:
            return
        tab = self.current_tab()
        links = tab.links()
        up = tab.up()
        if not links and not (up and tab.download_all()):
            messagebox.showwarning(
                "缺少链接", "请先粘贴至少一个链接（或填主页地址并勾选批量下载），或用「从文件导入…」导入。"
            )
            self.log.append("[提示] 没有链接，未开始下载。", "notice")
            return
        try:
            argv = tab.build_argv()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        namespace = self._parse_args_preview(tab.platform, argv)
        if namespace is None:
            messagebox.showwarning("参数不合法", "命令行参数没有通过脚本自检，详见日志。")
            return

        output_dir = tab.output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("目录不可用", f"无法创建保存目录：{exc}")
            return

        self._save_config_from_ui()
        self.cancel.reset()
        self.counter = TaskCounter()
        tab.progress.configure(value=0)
        tab.progress_label.configure(text="尚未开始")
        self.busy = True
        for item in self.tabs.values():
            item.set_busy(True)
        tab.status_label.configure(text="正在下载…")

        self.log.append(f"[信息] ---- 开始{ 'B 站' if tab.platform == 'bilibili' else '抖音'}任务 ----", "notice")
        self.log.append(f"[信息] 命令行参数：{argv}")
        if tab.platform == "bilibili":
            self.log.append(
                f"[信息] 参数自检通过：quality={namespace.quality} up={namespace.up or '（未指定）'}"
                f" all={bool(namespace.all)} limit={namespace.limit}"
            )
        else:
            self.log.append(
                f"[信息] 参数自检通过：format={namespace.format}"
                f" all={bool(namespace.all)} limit={namespace.limit}"
            )
        self.log.append(f"[信息] 保存目录：{output_dir}")

        self.worker = threading.Thread(
            target=self._run_task,
            args=(tab.platform, argv),
            name=f"autoDownloads-{tab.platform}",
            daemon=True,
        )
        self.worker.start()

    def _parse_args_preview(self, platform: str, argv: Sequence[str]) -> Any | None:
        """先用脚本自己的 build_parser().parse_args() 过一遍 argv。

        目的：参数不合法时立刻在日志里给出脚本原文的用法提示，不必等线程起来才失败；
        argparse 写出的内容同样走队列，不直接打印到界面。
        """
        module = PLATFORM_MODULES.get(platform, bili)
        writer = QueueWriter(lambda line: self.queue.put(("line", platform, line)))
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                return module.build_parser().parse_args(list(argv))
            except SystemExit:
                return None
            finally:
                writer.flush()

    def _run_task(self, platform: str, argv: Sequence[str]) -> None:
        module = PLATFORM_MODULES.get(platform, bili)
        writer = QueueWriter(lambda line: self.queue.put(("line", platform, line)))
        restore = install_cancel_hooks(module, self.cancel)
        code: int | None = None
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                code = module.main(list(argv))
        except SystemExit as exc:  # argparse 的参数错误
            code = exc.code if isinstance(exc.code, int) else 1
            self.queue.put(("line", platform, f"[错误] 参数被拒绝（退出码 {code}）"))
        except TaskCancelled:
            code = 130
            self.queue.put(("line", platform, f"[提示] {CANCEL_NOTICE}"))
        except BaseException:  # noqa: BLE001 - 线程里绝不能静默死掉
            code = 1
            self.queue.put(("line", platform, "[错误] 下载线程异常："))
            for chunk in traceback.format_exc().rstrip().splitlines():
                self.queue.put(("line", platform, "[错误] " + chunk))
        finally:
            restore()
            try:
                writer.flush()
            finally:
                self.queue.put(("finished", platform, code))

    def stop_download(self) -> None:
        """停止：既停下载，也打断还在进行的档位读取。"""
        self.cancel_quality_reads()
        if not self.busy:
            return
        self.cancel.request()
        self.log.append(f"[提示] {CANCEL_NOTICE}", "notice")
        for item in self.tabs.values():
            item.status_label.configure(text="已请求停止，正在收尾…")

    # -- 登录 ------------------------------------------------------------

    def start_login(self) -> None:
        tab = self.current_tab()
        if self.logged_in_progress:
            return
        try:
            timeout = max(LOGIN_TIMEOUT_MIN, min(LOGIN_TIMEOUT_MAX, int(self.login_timeout_var.get())))
        except (tk.TclError, ValueError):
            timeout = LOGIN_TIMEOUT_DEFAULT
            self.login_timeout_var.set(timeout)

        self.logged_in_progress = True
        for item in self.tabs.values():
            item.set_login_busy(True)
        tab.status_label.configure(text="等待登录…（会打开一次浏览器窗口）")
        platform_title = "B 站" if tab.platform == "bilibili" else "抖音"
        self.log.append(f"[登录] 开始{platform_title}登录，等待上限 {timeout} 秒。", "notice")

        threading.Thread(
            target=self._run_login,
            args=(tab.platform, timeout),
            name=f"autoDownloads-login-{tab.platform}",
            daemon=True,
        ).start()

    def _run_login(self, platform: str, timeout: int) -> None:
        writer = QueueWriter(lambda line: self.queue.put(("line", platform, line)))
        ok = False
        message = ""
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                if platform == "bilibili":
                    path = bili.run_login(
                        bilibili_cookie_path(),
                        proxy=browser_engine.detect_system_proxy(),
                        timeout=float(timeout),
                    )
                    message = f"[登录] 已保存登录态：{path}"
                else:
                    message = self._douyin_login(timeout)
            ok = True
        except BaseException as exc:  # noqa: BLE001 - 浏览器/网络异常都算登录失败
            ok = False
            message = f"[错误] 登录失败：{exc}"
        finally:
            try:
                writer.flush()
            finally:
                self.queue.put(("login_finished", platform, ok, message))

    def _douyin_login(self, timeout: int) -> str:
        browser = browser_engine.find_browser(None)
        profile = browser_engine.default_profile_dir()
        session = browser_engine.BrowserSession(
            browser,
            window_mode="visible",
            profile_dir=profile,
            initial_url=DOUYIN_HOME,
            wait_seconds=3.0,
        )
        with session:
            session.navigate(DOUYIN_HOME)
            deadline = time.monotonic() + timeout
            while True:
                if session.is_logged_in(DOUYIN_LOGIN_COOKIES):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待登录超时（{timeout} 秒）")
                time.sleep(2.0)
        return f"[登录] 登录成功，登录态保存在：{profile}"

    # -- 队列轮询 --------------------------------------------------------

    def _poll(self) -> None:
        # 先注销上一条已登记的定时器：手动调用 _poll() 时若不理它，
        # 那条链就再也没人取消，销毁窗口后 Tcl 会报 invalid command name。
        self._stop_poll()
        self._drain_queue()
        if not self.closed:
            try:
                self._poll_id = self.root.after(QUEUE_POLL_MS, self._poll)
            except tk.TclError:
                self._poll_id = None

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass

    def _stop_poll(self) -> None:
        pending, self._poll_id = self._poll_id, None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "line":
            _, platform, line = item
            self.log.append(line)
            self._update_progress(platform, line)
        elif kind == "finished":
            _, platform, code = item
            self._on_task_finished(platform, code)
        elif kind == "quality_attempt":
            _, platform, seq, index, total, previous, route = item
            self._show_quality_attempt(platform, seq, index, total, previous, route)
        elif kind == "qualities":
            _, platform, seq, url, payload, elapsed = item
            self._apply_quality_result(platform, seq, url, payload, elapsed)
        elif kind == "login_finished":
            _, platform, ok, message = item
            self._on_login_finished(platform, ok, message)

    def _update_progress(self, platform: str, line: str) -> None:
        tab = self.tabs.get(platform)
        if tab is None:
            return
        if ENTRY_RE.match(line.strip()):
            tab.progress.configure(value=0)
        percent = parse_progress_percent(line)
        if percent is not None:
            tab.progress.configure(value=percent)
        summary = self.counter.feed(line)
        if summary:
            tab.progress_label.configure(text=summary)

    def _on_task_finished(self, platform: str, code: int | None) -> None:
        self.busy = False
        for item in self.tabs.values():
            item.set_busy(False)
        tab = self.tabs.get(platform)
        cancelled = self.cancel.is_set()
        if tab is not None:
            tab.progress_label.configure(text=self.counter.finish())
            if cancelled:
                tab.status_label.configure(text="已停止（当前条目已收尾）")
                self.log.append("[提示] 已停止：不再开始剩余条目。", "notice")
            elif code == 0:
                tab.status_label.configure(text="上次任务已完成")
                tab.progress.configure(value=100)
                self.log.append("[完成] 任务结束，全部条目处理完成。")
            else:
                tab.status_label.configure(text=f"上次任务结束，退出码 {code}")
                self.log.append(f"[错误] 任务结束，退出码 {code}，请查看上面的日志。")
        self.cancel.reset()
        self.refresh_status()

    def _on_login_finished(self, platform: str, ok: bool, message: str) -> None:
        self.logged_in_progress = False
        for item in self.tabs.values():
            item.set_login_busy(False)
        tab = self.tabs.get(platform)
        if tab is not None:
            tab.status_label.configure(text="登录成功" if ok else "登录未完成")
        self.log.append(message if message else "[信息] 登录流程结束。", "done" if ok else "error")
        self.refresh_status()

    # -- 配置与退出 ------------------------------------------------------

    def _collect_config(self) -> dict[str, Any]:
        bili_tab = self.tabs["bilibili"]
        douyin_tab = self.tabs["douyin"]
        try:
            timeout = int(self.login_timeout_var.get())
        except (tk.TclError, ValueError):
            timeout = LOGIN_TIMEOUT_DEFAULT
        return {
            "output_dir": str(self.current_tab().output_dir()),
            "bilibili_quality": bili_tab.quality(),
            "douyin_quality": douyin_tab.quality(),
            # 两个标签页共用同一个"上次的 UP 主/主页限制"，存当前页的值
            "up_limit": self.current_tab().limit(),
            "login_timeout": timeout,
        }

    def _save_config_from_ui(self) -> None:
        saved = save_config(self._collect_config(), self.config_path)
        if saved is None:
            self.log.append(f"[提示] 配置保存失败（不影响使用）：{self.config_path}", "notice")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for attribute in ("_poll_id", "_close_wait_id"):
            pending = getattr(self, attribute, None)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        self.cancel.request()
        self.cancel_quality_reads()
        self._save_config_from_ui()

    def _worker_alive(self) -> bool:
        worker = self.worker
        if worker is None:
            return False
        try:
            return bool(worker.is_alive())
        except Exception:  # noqa: BLE001 - 打桩对象/已销毁线程一律按"已结束"处理
            return False

    def on_close(self) -> None:
        """关窗：先请工作线程收尾，最多等 close_wait_seconds 秒，再销毁窗口。"""
        if self.destroyed or self._close_wait_id is not None:
            return
        if not self._worker_alive():
            self._finish_close()
            return
        # 关窗等待期间不再需要定时轮询：等待循环自己抽队列，
        # 这样销毁解释器时不会有第二个 after 链的事件残留在就绪队列里。
        self._stop_poll()
        if not self.cancel.is_set():
            self.cancel.request()
            self.log.append("[提示] 已请求停止，正在等待当前条目收尾…", "notice")
        self._close_deadline = time.monotonic() + max(0.0, float(self.close_wait_seconds))
        self._wait_worker_then_close()

    def _wait_worker_then_close(self) -> None:
        self._close_wait_id = None
        if self.destroyed:
            return
        self._drain_queue()
        if not self._worker_alive():
            self._finish_close()
            return
        if time.monotonic() >= self._close_deadline:
            self.log.append(
                "[提示] 等待超时，已强制关闭，可能留下未完成的 .part 文件，下次运行会续传。",
                "notice",
            )
            self._finish_close()
            return
        try:
            self._close_wait_id = self.root.after(CLOSE_WAIT_POLL_MS, self._wait_worker_then_close)
        except tk.TclError:
            self._finish_close()

    def _finish_close(self) -> None:
        """收尾：先停轮询并存配置，再销毁窗口。"""
        if self.destroyed:
            return
        self.destroyed = True
        self.close()
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """gui.bat 的入口：打开单窗口界面。"""
    root = tk.Tk()
    DownloadApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
