# 视频下载脚本（抖音 + B站）

本仓库有两个相互独立的下载器，各自有启动脚本，互不影响：

| 站点 | 主脚本 | 双击启动 | 文档 |
| --- | --- | --- | --- |
| 抖音 | `douyin_downloader.py` | `douyin.bat` | 本文 |
| B站 | `bilibili_downloader.py` | `bilibili.bat` | [BILIBILI.md](BILIBILI.md) |

**不想用命令行？双击 `gui.bat`** 打开图形界面：两个标签页（B站 / 抖音），链接批量粘贴、下载某个 UP 主/博主全部视频、清晰度与保存目录选择、实时进度条与日志，全部点点鼠标就能完成。详见 [GUI.md](GUI.md)。

下面是**抖音**下载器的说明。B站下载器（批量下载、下载 UP 主全部视频、登录一次拿 1080P）请看 [BILIBILI.md](BILIBILI.md)。

---

一个用**真实 Chromium 浏览器 + yt-dlp** 实现的抖音视频下载工具。

默认情况下，单个视频由本机的 Edge / Chrome / Chromium 在独立浏览器配置中解析，
不需要导出 Cookie。浏览器默认**无头运行**（新版 headless），下载过程中不会弹出任何
窗口，也不抢焦点；只有首次需要登录时才临时打开一次可见窗口。
主页/合集等播放列表仍交给 yt-dlp。

> **请仅下载自己有权保存的内容，用于个人备份和学习。**
> 不要用本脚本批量搬运、二次分发或商业使用他人作品。使用前请阅读抖音用户协议、
> 著作权法及你所在地区的法律法规。

---

## 功能

- 直接粘贴抖音分享链接或整段分享文案
- 自动识别 `v.douyin.com` 短链接并转换成视频页
- 一次下载多个链接，自动去重
- 默认使用真实浏览器上下文，避开纯 Python 请求常见的抖音风控
- 默认无头运行，不弹窗、不抢焦点；被风控拦截时自动降级为屏幕外窗口重试
- 支持下载某个用户主页的全部公开作品（`--all`，可配合 `--limit`）
- 默认持久化浏览器配置（`.browser-profile`），登录一次后可复用，失效时提示再登录
- 浏览器不可用或处理播放列表时自动回退到 yt-dlp
- 可选保存视频信息、封面图、按作者分目录
- 断点续传（yt-dlp 引擎）、失败重试、下载记录
- 支持限速、代理、清晰度选择、`--dry-run`、`--list-formats`

---

## 快速开始（Windows）

### 1. 安装 Python

安装 Python 3.10 或更高版本：<https://www.python.org/downloads/>

安装时务必勾选 **Add Python to PATH**。

### 2. 安装依赖

双击 **`setup.bat`**。脚本会创建 `.venv` 并安装：

- `yt-dlp`：回退引擎和播放列表
- `websocket-client`：控制浏览器

也可以手动执行：

```powershell
python -m pip install -r requirements.txt
```

### 3. 下载

双击 **`douyin.bat`**，粘贴抖音分享链接或整段分享文案，回车即可。

或者使用命令行：

```powershell
python douyin_downloader.py "https://v.douyin.com/xxxx/"
```

分享文案也可以直接粘贴：

```powershell
python douyin_downloader.py "7.43 复制打开抖音，看看【某人的作品】 https://v.douyin.com/xxxx/ 复制此链接"
```

默认保存到当前目录的 `downloads\`。

> 运行过程中默认不会出现任何浏览器窗口：脚本用无头模式在独立配置里访问抖音页面。
> 如果抖音风控拦截了无头请求，脚本会自动降级为“屏幕外窗口”重试（窗口在屏幕之外，
> 你同样看不到），并在日志里说明降级原因。
>
> 首次运行如果还没有登录态，脚本会**临时打开一个有窗口的浏览器**提示你登录一次，
> 登录完成后自动保存到 `.browser-profile` 并继续，之后不再需要登录。
> 需要自己观察页面时加 `--visible`。

---

## 三个引擎

| 引擎 | 说明 |
| --- | --- |
| `auto`（默认） | 单个视频用浏览器解析；主页/合集等播放列表用 yt-dlp |
| `browser` | 强制使用本机 Chromium 浏览器，只支持单个视频 |
| `yt-dlp` | 强制使用 yt-dlp，适合调试或播放列表，可能需要 Cookie |

```powershell
# 默认 auto
python douyin_downloader.py "https://v.douyin.com/xxxx/"

# 强制浏览器
python douyin_downloader.py --engine browser "https://v.douyin.com/xxxx/"

# 强制 yt-dlp
python douyin_downloader.py --engine yt-dlp "https://www.douyin.com/video/7300000000000000001"
```

### 为什么默认用浏览器？

抖音 Web 接口会检查浏览器环境。直接用 Python/urllib 请求时，经常遇到
`Fresh cookies (not necessarily logged in) are needed`，即使提供了 cookies 也
可能返回空 JSON。浏览器引擎在真实的 `www.douyin.com` 页面上下文里调用抖音自己的
详情接口，通常更稳定。

浏览器引擎使用**独立的用户目录**（默认是脚本目录下的 `.browser-profile`），
不会读取、修改你日常浏览器的 Cookie 或登录状态；不想要持久化时加
`--temp-profile`，脚本会恢复成“每次运行用一次性临时目录、退出即删除”的旧行为。

---

## 常用命令

```powershell
# 下载单个视频（默认 auto）
python douyin_downloader.py "https://v.douyin.com/xxxx/"

# 从文件批量读取（每行一个链接，也可以直接放分享文案）
python douyin_downloader.py --file links.txt

# 下载某个用户主页的全部公开作品（先建议用 --limit 试跑）
python douyin_downloader.py --all --limit 20 "https://www.douyin.com/user/MS4wLjABAAAA..."

# 下载该用户全部作品（数量可能很多，注意磁盘空间）
python douyin_downloader.py --all "https://www.douyin.com/user/MS4wLjABAAAA..."

# 默认就会持久化配置并复用登录态；需要登录的页面首次会自动提示登录一次
python douyin_downloader.py --all --browser-wait 60 "https://www.douyin.com/user/self"

# 换一个配置目录，或恢复旧的一次性临时配置
python douyin_downloader.py --browser-profile "D:\DouyinProfile" "https://v.douyin.com/xxxx/"
python douyin_downloader.py --temp-profile "https://v.douyin.com/xxxx/"

# 调试时强制打开可见窗口
python douyin_downloader.py --visible "https://v.douyin.com/xxxx/"

# 指定保存目录
python douyin_downloader.py -o "D:\Videos\Douyin" "https://v.douyin.com/xxxx/"

# 按作者建立子目录，并保存视频信息和封面
python douyin_downloader.py --folder-by-author --info --thumbnail "https://v.douyin.com/xxxx/"

# 只下载 1080p 及以下
python douyin_downloader.py --format "best[height<=1080]" "https://v.douyin.com/xxxx/"

# 默认最高画质；如果播放器不支持 H.265，优先选 H.264（可能降分辨率）
python douyin_downloader.py --prefer-h264 "https://v.douyin.com/xxxx/"

# 查看可用清晰度，不下载
python douyin_downloader.py --list-formats "https://v.douyin.com/xxxx/"

# 只解析，不下载
python douyin_downloader.py --dry-run "https://v.douyin.com/xxxx/"

# 使用代理并限速 2 MB/s
python douyin_downloader.py --proxy http://127.0.0.1:7890 --limit-rate 2M "https://v.douyin.com/xxxx/"

# 重新下载已经下载过的视频
python douyin_downloader.py --force "https://v.douyin.com/xxxx/"

# 下载主页/合集里的多个视频（自动使用 yt-dlp）
python douyin_downloader.py --playlist "https://www.douyin.com/user/xxxx"

# 指定浏览器路径
python douyin_downloader.py --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" "链接"
```

`--format` 在浏览器引擎中支持 `best`、`worst` 和 `best[height<=数字]` 这类简单
写法；完整的 yt-dlp 格式表达式只在 `--engine yt-dlp` 下生效。

### 关于“原视频”和 1080p

抖音网页接口返回的是平台转码后的播放流，不一定是创作者上传的原始文件。脚本会
优先调用抖音 App 的 `play` 接口获取高码率 H.264 流（通常是真正的 1080p），
失败后再回退到网页详情里的 `play_addr` / `play_addr_265`。

有些视频只有 **H.265** 编码才提供高分辨率。H.265 文件更小，但 Windows 自带
播放器可能打不开，建议用 VLC、PotPlayer 或 MPV 播放。如果必须用 H.264，也可以
加 `--prefer-h264`。

如果之前已经下载过较低画质版本，需要加 `--force` 才会覆盖旧文件；否则下载记录
会直接跳过：

```powershell
python douyin_downloader.py --force "链接"
```

---

## 输出规则

默认文件名：

```text
downloads\{作品ID}_{标题}.mp4
```

例如：

```text
downloads\7300000000000000001_这是一个视频标题.mp4
```

使用 `--folder-by-author` 后：

```text
downloads\作者名\7300000000000000001_这是一个视频标题.mp4
```

脚本会在输出目录写入 `.douyin-archive.txt` 下载记录。已经成功下载过的视频会
被跳过；需要重新下载时使用 `--force`。

`--info` 会额外生成 `.info.json`，`--thumbnail` 会保存封面 `.jpg`。

> `--template` 只对 `--engine yt-dlp` 生效。浏览器引擎使用固定的
> `{ID}_{标题}.mp4` 命名。

---

## 批量下载一个用户的全部作品

使用 `--all`，脚本会打开该用户的抖音主页，自动向下滚动收集作品链接，然后逐个用
最高画质下载：

```powershell
# 先试跑 20 个
python douyin_downloader.py --all --limit 20 "https://www.douyin.com/user/MS4wLjABAAAA..."

# 确认没问题后下载全部
python douyin_downloader.py --all "https://www.douyin.com/user/MS4wLjABAAAA..."
```

说明：

- `--all` 只支持浏览器引擎，不能和 `--engine yt-dlp`、`--playlist` 同时使用。
- 用户主页必须是公开的；私密账号或需要登录的页面，先登录一次。
- 视频数量多时会滚动较久，下载也会占用较多磁盘空间，建议先用 `--limit` 试跑。
- 每一个视频都会按最高画质下载；已下载过的会自动跳过，中断后重新运行会继续。

### 无窗口运行与“登录一次”

浏览器默认以**新版 headless** 运行：屏幕上不会出现窗口，不抢焦点，可以边下边干活。

- 无头模式被抖音风控拦截时，脚本会**自动降级为屏幕外窗口**（`--window-position=-32000,-32000`），
  日志里会打印 `[降级]` 和具体原因。降级后的窗口仍然在屏幕之外，你同样看不到。
- 浏览器配置默认持久化在脚本所在目录（不是当前工作目录）的 `.browser-profile`，
  所以在哪里启动脚本都会复用同一份登录态。
- 首次运行没有登录态时，脚本会提示“需要登录一次”，临时打开可见窗口让你登录；
  登录成功后自动保存并继续，之后不再提示。
- 登录态失效时会再提示登录一次，而不是直接报错退出。

```powershell
# 默认：无头 + 复用 .browser-profile
python douyin_downloader.py --all --browser-wait 60 "https://www.douyin.com/user/self"

# 指定其它配置目录
python douyin_downloader.py --browser-profile "D:\DouyinProfile" "链接"

# 恢复旧行为：每次都新建一次性临时配置（不保留登录态）
python douyin_downloader.py --temp-profile "链接"

# 调试：强制可见窗口，方便观察页面提示
python douyin_downloader.py --visible "链接"
```

> `.browser-profile` 里包含登录 Cookie，已在 `.gitignore` 中排除，请不要分享或提交到 Git。

`user/self` 页面通常只包含自己的作品/公开内容；“喜欢”列表需要另外切换页面，
当前脚本不自动处理。

---

## 主要参数

| 参数 | 说明 |
| --- | --- |
| `links` | 一个或多个抖音链接/分享文案 |
| `--file FILE` | 从文本文件读取链接 |
| `-o, --output DIR` | 保存目录，默认 `downloads` |
| `--engine {auto,browser,yt-dlp}` | 下载引擎，默认 `auto` |
| `--all` | 下载用户主页全部公开作品（浏览器引擎） |
| `--limit N` | 配合 `--all`，最多下载 N 个 |
| `--browser-profile DIR` | 持久化浏览器配置目录，默认脚本目录下的 `.browser-profile` |
| `--temp-profile` | 使用一次性临时配置（旧行为，不保留登录态） |
| `--visible` | 强制可见浏览器窗口（调试用；默认无头不弹窗） |
| `--format FORMAT` | 清晰度选择，默认 `best` |
| `--prefer-h264` | 优先 H.264，兼容性更好但可能降分辨率 |
| `--list-formats` | 只列出清晰度，不下载 |
| `--browser PATH` | 指定 Edge/Chrome/Chromium 可执行文件 |
| `--browser-headless` | 无头模式（现在已是默认行为，仅为兼容旧命令保留） |
| `--browser-wait SEC` | 打开抖音首页后等待 Cookie 的秒数，默认 8 |
| `--cookies FILE` | Netscape cookies.txt（仅 yt-dlp） |
| `--cookies-from-browser BROWSER[:PROFILE]` | 从浏览器读取 Cookie（仅 yt-dlp） |
| `--no-browser-cookies` | 禁止 yt-dlp 自动读取浏览器 Cookie |
| `--info` | 保存 `.info.json` |
| `--thumbnail` | 保存封面图 |
| `--playlist` | 允许播放列表（使用 yt-dlp） |
| `--force` | 忽略下载记录并覆盖已有文件 |
| `--no-archive` | 不写入下载记录 |
| `--dry-run` | 只解析，不下载 |
| `--no-progress` | 不显示进度 |
| `--limit-rate RATE` | 限速，如 `500K`、`2M` |
| `--proxy URL` | 代理地址 |
| `--timeout SEC` | 网络超时，默认 30 秒 |
| `--retries N` | 重试次数，默认 5 |
| `--folder-by-author` | 按作者建立子目录 |
| `--template TEMPLATE` | yt-dlp 输出模板 |
| `-v, --verbose` | 调试信息 |
| `--version` | 版本 |

查看完整帮助：

```powershell
python douyin_downloader.py --help
```

---

## 关于 Cookie

### 默认浏览器引擎

**不需要手动提供 Cookie。** 脚本会在独立浏览器配置（默认 `.browser-profile`）里
让抖音页面自己生成 Cookie。它不会读取你日常浏览器里的账号 Cookie。

浏览器会话还会把当前的 Cookie 导出成 Netscape 格式的 `cookies.txt`（保存在输出目录），
在浏览器引擎失败、回退到 yt-dlp 时自动复用，不需要你手工导出。

如果遇到 `Uifid Not Found` 或类似风控提示：

- 脚本会自动降级为屏幕外窗口重试，先看日志里的 `[降级]` 原因；
- 仍然失败时加 `--visible` 观察页面提示；
- 确认本机 Edge/Chrome 可以正常打开抖音；
- 用 `--browser` 明确指定浏览器路径。

### yt-dlp 引擎

只有使用 `--engine yt-dlp` 时，`--cookies` / `--cookies-from-browser` 才生效。
如果提示 `Fresh cookies`：

```powershell
# 从 Edge 读取
python douyin_downloader.py --engine yt-dlp --cookies-from-browser edge "链接"

# 或使用扩展导出的 Netscape 格式 cookies.txt
python douyin_downloader.py --engine yt-dlp --cookies cookies.txt "链接"
```

注意：

- 读取浏览器 Cookie 时，最好先**完全退出浏览器**，否则数据库可能被占用。
- 较新版本的 Chrome 可能受 Cookie 加密限制，优先尝试 Edge 或 `cookies.txt`。
- `cookies.txt` 等同于账号凭证，**不要发给别人，也不要提交到 Git**。

---

## 常见问题

### 运行时还是弹出了浏览器窗口

默认不会弹窗。出现窗口只可能是这三种情况，日志里都会写明原因：

1. `[登录] 需要登录一次` —— 首次运行或登录态失效，脚本临时打开可见窗口让你登录，
   登录完成后自动关闭并继续；
2. `[降级] 改用屏幕外窗口重试` —— 无头模式被风控拦截，窗口被放在屏幕外坐标，
   你理论上不会看到；如果它仍然跑到屏幕上了，说明窗口管理器忽略了位置参数；
3. 你加了 `--visible`。

### 每次都提示登录

登录态默认保存在脚本目录下的 `.browser-profile`，正常情况下只需登录一次。如果反复
提示登录：

- 确认没有加 `--temp-profile`（它每次都用一次性配置，等于每次都重新登录）；
- 确认没有用不同的 `--browser-profile` 目录；
- 登录态过期后脚本会再提示一次，重新登录即可，不会直接报错。

```powershell
# 默认就是持久化配置，不需要额外传参
python douyin_downloader.py "链接"
```

### 提示 `浏览器调试端口没有启动`

- 确认安装了 Edge / Chrome / Chromium；
- 用 `--browser` 指定完整路径；
- 检查杀毒软件是否拦截了浏览器启动；
- 如果端口被占用，重新运行即可（脚本每次使用随机端口）。

### 提示 `抖音接口拒绝了浏览器请求（Uifid/风控拦截）`

- 脚本会自动降级为屏幕外窗口重试一次，日志里有 `[降级]` 说明；
- 仍失败时加 `--visible` 打开可见窗口，看页面里的提示（可能要登录或验证）；
- 确认浏览器能正常打开 <https://www.douyin.com>；
- 用 `--browser` 指定一个正常的 Edge/Chrome。

### 提示 `无法获取视频：作品不见了`

视频可能已删除、私密、仅作者可见或受地区限制。脚本不会绕过这些权限。

### 提示 `没有找到 Edge/Chrome/Chromium`

安装 Edge 或 Chrome，或指定路径：

```powershell
python douyin_downloader.py --browser "C:\Program Files\Google\Chrome\Application\chrome.exe" "链接"
```

### `--engine browser` 报错说不支持播放列表

浏览器引擎的批量模式只处理“用户主页全部作品”。用户主页请用 `--all`；
合集/播放列表请使用 `--engine yt-dlp --playlist`。

```powershell
python douyin_downloader.py --all "https://www.douyin.com/user/MS4w..."
```

### 下载成功但没有文件

可能是下载记录里已经有这个视频。使用 `--force` 重新下载，或查看
`downloads\.douyin-archive.txt`。

### 提示 ffmpeg 相关错误

浏览器引擎直接下载 mp4，不依赖 ffmpeg。只有 `--engine yt-dlp` 在合并音视频时
可能需要 ffmpeg。Windows 可以执行：

```powershell
winget install Gyan.FFmpeg
```

### 视频是图文/图集

当前脚本只下载视频。图文作品会提示“这是图文/图集作品”。

---

## 工作原理

1. 从参数、文件或粘贴内容中用正则提取抖音 URL；
2. 对 `v.douyin.com` 短链接发起请求，跟随跳转并提取作品 ID；
3. 把分享页 URL 规范成 `https://www.douyin.com/video/{id}`；
4. **浏览器引擎**：启动独立 Chromium（默认无头，被风控时降级为屏幕外窗口），
   打开抖音首页生成访客 Cookie，
   在页面上下文调用 `/aweme/v1/web/aweme/detail/`，优先用 `play_addr.uri`
   请求 App 的 `play` 接口获取高码率 1080p H.264，失败后回退到网页播放流；
5. **yt-dlp 引擎**：用于播放列表、回退，或用户强制指定时；
6. 使用 `.douyin-archive.txt` 避免重复下载。

脚本不实现抖音签名算法，也不绕过登录、付费或私密权限；它只是用真实浏览器
完成正常页面访问，并把页面已经拿到的视频流保存下来。

---

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖：

- 分享文案/短链接提取
- 作品 ID 和规范 URL 解析
- 浏览器 Cookie 参数解析
- 浏览器窗口模式参数解析（headless / offscreen / visible、`--temp-profile`、`--visible`）
- cookies.txt 导出格式（Netscape）与登录态检测逻辑
- 限速解析
- 浏览器清晰度候选排序、文件名清洗

---

## 目录结构

```text
douyin_downloader.py        抖音主脚本 / CLI
browser_engine.py           真实浏览器 CDP 引擎（两个下载器共用）
bilibili_downloader.py      B站主脚本 / CLI（见 BILIBILI.md）
bilibili.bat                B站 Windows 双击启动
BILIBILI.md                 B站下载器文档
gui_app.py                  图形界面（tkinter，零第三方依赖）
gui.bat                     图形界面 Windows 双击启动
GUI.md                      图形界面文档
requirements.txt            Python 依赖
setup.bat                   Windows 一键安装依赖
douyin.bat                  抖音 Windows 双击启动
links.example.txt           批量链接示例
.browser-profile/           浏览器配置（含登录 Cookie，已 gitignore，首次运行自动创建）
tests/                      单元测试
```

---

## 免责声明

本工具仅用于个人学习、研究和备份自己有权保存的内容。使用者应自行承担因下载、
存储、传播他人作品产生的法律责任。项目不提供任何视频内容，也不保证所有链接
都能下载。
