# B 站下载器（bilibili_downloader.py）

B 站视频批量下载脚本。下载核心是 **yt-dlp 的 Python API**（不需要另装 exe）；
浏览器只在**登录那一次**出现，之后所有枚举与下载都不会启动浏览器、不弹任何窗口。

> 仅供个人备份和学习使用。请遵守哔哩哔哩用户协议、著作权法及当地法律法规，
> 不要下载、传播无权使用的视频。

---

## 快速开始

```bat
:: 1. 首次使用：登录一次（唯一会弹出可见浏览器窗口的环节）
bilibili.bat --login-once

:: 2. 之后随便批量下载，全程无窗口
bilibili.bat "https://www.bilibili.com/video/BV1wh4P6XEkC"
bilibili.bat --file links.txt
bilibili.bat --up 3546984120059960 --all --limit 10
```

也可以直接用 Python：

```bat
".venv\Scripts\python.exe" bilibili_downloader.py --login-once
```

---

## 为什么必须先登录一次

B 站的 **1080P 及以上是 DASH 音视频分离流**，并且高清轨只对登录用户开放。
未登录时脚本虽然能跑，但通常只能取到 480P/720P，1080P 60 帧更是直接缺失。

所以默认流程是：

1. 检查脚本目录下的 `cookies.txt` 里有没有 `SESSDATA`；
2. 没有就打印一条 `[登录]` 提示，给出**唯一一条**要执行的命令（`--login-once`）；
3. 登录一次后 Cookie 长期复用，之后不再要求登录；
4. 如果实际拿到的最高分辨率低于 `--quality`，脚本会打印
   `[提示] 请求 1080P，但当前账号只能取到最高 720P ...`，**绝不静默降级**。

想强制"没登录就不干活"，加 `--require-login`。

### 按 B 站原生档位选：`--list-qualities` + `--qn`

分辨率只能表达"多少 P"，区分不了 B 站自己的档位——同样是 1080P，
「高清 1080P」和「高清 1080P60」是两档；4K 还有「超清 4K」和「真彩 HDR」之分。

先用 `--list-qualities` 看这个视频**到底有哪些档**（标签完全照抄 B 站）：

```
> bilibili_downloader.py --list-qualities "https://www.bilibili.com/video/BV1R1e4zKEh1"
[档位] 视频：【4KHDR 真彩®】…（BV1R1e4zKEh1，cid=31870356198）
[档位] 账号：已登录 / 大会员
[档位] 当前账号可用最高档：真彩 HDR（qn=125）
  真彩 HDR       qn=125  大会员 可用     编码 hevc
  超清 4K        qn=120  大会员 可用     编码 avc1/hevc/av01
  高清 1080P60   qn=116  大会员 可用     编码 avc1/hevc/av01
  高清 1080P     qn=80   免费   可用     编码 avc1/hevc/av01
  高清 720P      qn=64   免费   可用     编码 avc1/hevc/av01
  清晰 480P      qn=32   免费   可用     编码 avc1/hevc/av01
  流畅 360P      qn=16   免费   可用     编码 avc1/hevc/av01
```

然后用 qn 精确定档：

```bat
bilibili.bat --qn 120 "<视频链接>"        :: 超清 4K
bilibili.bat --qn 116 "<视频链接>"        :: 高清 1080P60（不是 1080P30）
bilibili.bat --quality 125 "<视频链接>"   :: 等价写法：真彩 HDR
```

实现要点：

- qn 通过 `/x/player/playurl` 的 `dash.video[].id` 精确对应到 DASH 流，
  再交给 yt-dlp 的 `-f "<流id>+ba"`——**不靠分辨率或帧率猜档**；
- 同一个 qn 有多种编码时，优先 `avc1` → `hevc` → `av01`（兼容性优先），
  并在日志里打印 `[档位] qn=116（高清 1080P60）实际选用编码：avc1 (id=30116)`；
- 拿不到该档（视频没有这一档 / 账号没有大会员）会明确说明原因并给替代建议，
  **绝不静默降级**；
- 档位表读不到（离线/接口变更）时，`--qn` 会退化为"按该 qn 的常规规格匹配"并打印提示，
  60 帧档（116/74）强制要求高帧率，不会把 1080P60 悄悄换成 1080P30。

### 4K / 2K 与 `best` 的区别

| 档位 | 含义 | 前提 |
| --- | --- | --- |
| `--quality 2160` | 明确要 **4K**（2160P DASH 轨） | 大会员登录态；单文件常见 2–3 GB |
| `--quality 1440` | 明确要 **2K**（1440P） | 大会员登录态 |
| `--quality 1080` | 1080P（默认档） | 普通登录态即可 |
| `--quality best` | **不设上限**，账号有 4K 就拿 4K | 跟着账号权限走 |

**`best` 和 `2160` 的差别很重要**：`best` 的结果取决于当前账号——同一条命令，
大会员账号拿 4K、普通账号拿 1080P，换账号结果就变了。想"锁死 4K"用
`--quality 2160`；只想"能多高就多高"才用 `best`。

默认档位仍然是 `1080`：4K 单个文件可能到 3 GB，不能默认把用户的磁盘填满。

拿不到 4K 时脚本会区分三种原因，**不会把"没大会员"说成"网络问题"**：

```
[提示] 请求 2160P（4K），但当前账号不是大会员，只取到最高 1080P。4K 属于大会员专享；可改用 --quality 1080 或 --quality best。
[提示] 请求 2160P（4K），但该视频本身没有 4K 源：当前账号已是大会员，实际最高只有 1080P。可改用 --quality 1080 或 --quality best。
[提示] 请求 2160P（4K），实际最高只有 1080P。两种可能：账号没有大会员，或该视频本身没有 4K 源。先用 --check-login 确认登录态，或用 --list-formats 看真实轨道。
```

分别对应：①没登录 / 没大会员、②视频本身没有该清晰度源、③查不到账号信息（离线）。
第 ③ 种不会阻塞下载——账号探测失败只影响这句提示，下载照常进行。

### 高帧率档位与"换清晰度"的行为

- `--quality 1080p60`、`--quality 720p60` 是 `--qn 116` / `--qn 74` 的别名，
  会走到 60 帧轨道；`--quality 1080` 只表示"1080P 这一档"，不挑帧率。
- 已下载记录按清晰度分开（`.bilibili-archive-1080.txt`、`.bilibili-archive-qn116.txt`……），
  所以同一链接换清晰度不会被"已下载过"直接跳过。
- 目标清晰度与目录里已有文件不同时，会**另存一份带清晰度后缀的文件**
  （`标题_480P.mp4`），不会覆盖原视频；只有同分辨率（且帧率够）才跳过。
  需要覆盖原文件时加 `--force`。

### 登录流程做了什么

1. 用 `browser_engine.BrowserSession` 以 **visible** 模式启动本机 Edge/Chrome，
   配置目录是脚本目录下的 `.browser-profile`（已存在就直接复用）；
2. 轮询 `SESSDATA` 是否出现，出现后调用 `export_cookies()`
   把 Netscape 格式的 `cookies.txt` 原子写入脚本目录；
3. 关闭浏览器。

**只有这一步会弹窗。** 之后脚本不会再调用 `BrowserSession`。

---

## 参数

| 参数 | 说明 |
| --- | --- |
| `links ...` | 位置参数：BV/av 号、数字 mid、视频 URL、UP 主空间 URL、b23.tv 短链、分享文案（可多个） |
| `--file FILE` | 从文本文件批量读链接（文件里可以有分享文案；裸 BV/av 号、数字 mid 也能识别） |
| `-o, --output DIR` | 保存目录，默认 `downloads` |
| `--up MID_OR_URL` | 指定 UP 主（数字 mid 或空间 URL） |
| `--all` | 下载该 UP 主下面的全部视频 |
| `--limit N` | 最多处理 N 个视频（0 = 不限制） |
| `--quality {2160,1440,1080,720,480,best}` | 目标清晰度，默认 `1080`；`2160`=4K、`1440`=2K，**需要大会员登录态**，单文件可达数 GB。也接受 `1080p60` / `720p60` 与 B 站原生 qn 数字（如 `116`） |
| `--qn N` | 直接按 B 站 qn 选档，等价于 `--quality <qn>`；与 `--quality` 同时给出时**以 `--qn` 为准**（会打一行提示） |
| `--list-qualities` | 只读取并打印该视频的 B 站官方档位表（B 站原文档位名 / qn / 是否大会员 / 当前是否可用 / 编码），不下载、不需要 ffmpeg |
| `--login-once` | 打开可见浏览器登录一次并导出 cookies.txt |
| `--logout` | 删除已保存的 cookies.txt |
| `--check-login` | 只检查 cookies.txt 的登录态，不联网、不下载 |
| `--require-login` | 没有登录态时直接退出 |
| `--cookies FILE` | 指定别的 cookies.txt |
| `--list-formats` | 只列出真实标题与清晰度列表，不下载 |
| `--dry-run` | 只枚举并显示将要下载的内容，不写文件 |
| `--folder-by-author` | 按 UP 主名字建子目录 |
| `--force` | 忽略下载归档并覆盖已有文件 |
| `--no-archive` | 不写归档文件（默认写 `.bilibili-archive.txt`） |
| `--limit-rate RATE` | 限速，例如 `500K`、`2M` |
| `--proxy URL` | 代理地址 |
| `--timeout SEC` / `--retries N` | 网络超时（默认 30）/ 重试次数（默认 5） |
| `--ffmpeg PATH` | 手动指定 ffmpeg |
| `--login-timeout SEC` | `--login-once` 等待登录的秒数，默认 300 |
| `--no-progress` | 不显示进度条 |
| `-v, --verbose` | 调试信息 |
| `--version` | 版本号 |

### UP 主枚举规则

UP 主目标（空间链接、数字 mid 或 `--up`）必须显式声明范围，避免误下整个账号：

```bat
bilibili.bat --up 3546984120059960 --all             :: 全部投稿
bilibili.bat --up 3546984120059960 --all --limit 20  :: 只取最近 20 个
bilibili.bat "https://space.bilibili.com/3546984120059960/video" --all --dry-run
```

只给空间链接、不给 `--all`/`--limit` 时，脚本直接报错说明原因，不会偷偷枚举。

---

## ffmpeg 依赖（必需）

1080P 是 DASH 音视频分离流，必须用 ffmpeg 合并音视频轨。脚本按下面的顺序自动探测：

1. `--ffmpeg PATH` 显式指定；
2. 系统 `PATH` 里的 `ffmpeg`；
3. 项目目录下的 `tools\ffmpeg.exe`；
4. 常见安装位置：`C:\ffmpeg\bin\ffmpeg.exe`、`D:\ffmpeg\bin\ffmpeg.exe`、
   `C:\Program Files\ffmpeg\bin\ffmpeg.exe`、`D:\EVCapture\ffmpeg.exe`、
   `D:\sinsam\app\bin\ffmpeg.exe`、`%LOCALAPPDATA%\Programs\ffmpeg\bin\ffmpeg.exe`、
   `%USERPROFILE%\ffmpeg\bin\ffmpeg.exe`、`C:\ProgramData\chocolatey\bin\ffmpeg.exe` 等。

**全都找不到时直接报错并给安装指引，不会降级成低画质。** 报错发生在任何枚举/下载动作之前：

```
[错误] 没有找到 ffmpeg，无法继续。
       B 站 1080P 是 DASH 音视频分离流，必须用 ffmpeg 合并音轨和视频轨。
...
```

### 环境变量 BILIBILI_FFMPEG_PATHS

只影响"常见安装位置"这一步：设置后**替换**内置列表（`os.pathsep` 分隔）；
设为 `none` / `off` / `0` 表示完全不扫描常见位置，只认 `--ffmpeg`、`PATH` 和 `tools\`。
（命令行里把变量赋成空值等于删掉变量，所以留了显式哨兵值。）

CI 和自动化测试用它做确定性验证：

```bat
set "BILIBILI_FFMPEG_PATHS=none"
set "PATH=C:\Windows\system32"
".venv\Scripts\python.exe" bilibili_downloader.py --dry-run BV1wh4P6XEkC
```

### 代理与 NO_PROXY

`--proxy http://127.0.0.1:7890` 会把代理同时用于 yt-dlp 和脚本自己的接口调用
（内部同步设置 `HTTP_PROXY`/`HTTPS_PROXY`）。

如果本机代理只能访问部分 B 站域名（例如代理能过 `api.bilibili.com` 却过不了
`www.bilibili.com`），用 `NO_PROXY` 把能直连的域名排除即可：

```bat
set "NO_PROXY=www.bilibili.com"
bilibili.bat --proxy http://127.0.0.1:7897 --list-formats BV1wh4P6XEkC
```

脚本遇到传输层故障（连接被重置、SSL EOF 等）会额外提示这是网络问题，
不会误报成"登录态失效"。

---

## 断点续传与跳过已下载

默认把已完成的条目写进输出目录的 `.bilibili-archive.txt`（yt-dlp 归档格式，
内容形如 `bilibili BV1wh4P6XEkC`）。再次运行同样链接会自动跳过；
`--force` 忽略归档并覆盖，`--no-archive` 干脆不写归档。
半截文件由 yt-dlp 的 `continuedl` 续传。

---

## 登录态失效

枚举或下载返回未登录 / 401 / 需要登录时，脚本会识别并打印：

```
[登录] 登录态已失效（SESSDATA 已过期或被服务端作废）。
[登录] 请重新执行一次：bilibili_downloader.py --login-once
```

不会崩溃，也不会静默降级画质。`--check-login` 可以离线自检；
`--logout` 删除本地 Cookie。

---

## 实现说明（与推荐方向的差异）

* **UP 主枚举没有走 yt-dlp 的 space 播放列表。** 实测把
  `https://space.bilibili.com/{mid}/video` 交给 yt-dlp 的 `BilibiliSpaceVideo`
  会返回 `HTTP Error 412 Precondition Failed`（缺少 `buvid3`，且该接口要求 WBI 签名）。
  因此改为脚本自己实现：先访问首页种下 `buvid3`，再从 `/x/web-interface/nav` 取 WBI key，
  用 `/x/space/wbi/arc/search` 翻页枚举，签名为 `md5(query + wts + mixinKey)`。
  枚举出来的每一条再交给 yt-dlp 下载——**下载链路仍然是 yt-dlp**。
* 短链 `b23.tv` 由脚本自己跟随重定向解析成真实 URL，再交给 yt-dlp。

---

## 文件

| 文件 | 说明 |
| --- | --- |
| `bilibili_downloader.py` | 主脚本 |
| `bilibili.bat` | Windows 启动器（自动选用 .venv，缺依赖时跑 setup.bat） |
| `BILIBILI.md` | 本文件 |
| `tests\test_bilibili_downloader.py` | 单元测试（`python -m unittest discover -s tests`） |
| `cookies.txt` | 登录态（自动生成，含凭据，勿提交） |
| `.bilibili-archive.txt` | 下载归档（在输出目录里） |
