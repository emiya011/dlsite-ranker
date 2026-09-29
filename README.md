# DLsite / Ci-en 排行榜抓取工具

抓取 **DLsite**（maniax 男性向 R18 区）与 **Ci-en**（创作者文章榜单）的排行榜，
导出成带封面图的 HTML 报告；另附一个把 PDF 转成一张长图的小工具。

## 功能

**排行榜抓取**

- DLsite：日 / 周 / 月 / 年 × 漫画CG、游戏视频、音声ASMR，支持官网的「人気順」与「販売数順」
- Ci-en：日 / 周 / 月 × 游戏、音声、漫画、插画等分类
- 可选抓取每个作品的详情页，取精确评分（列表页只有星级图标，不是真实分数）
- 可选下载封面图、正文配图

**HTML 报告**

- 四种布局：卡片、单列、网格、按榜单分列
- 支持把 Ci-en 文章正文折叠进报告，点开才展开
- 可导出 PDF

**Ci-en 账号登录**

- 通过真实浏览器窗口登录，密码 / 验证码 / 两步验证全程由用户完成，本工具不接触
- 登录凭据用 Windows DPAPI 加密保存在本地，只有当前 Windows 账户能解开
- 登录后可读取账号有权限的限定内容

**待关注作者**

- 自动扫描榜单，列出「关注一下就能读」的作者，一键跳转到作者主页

**清理**

- 扫描旧报告、无引用的孤儿图片和调试样本，先预览后确认

**PDF 转长图**（`pdf2img.py`）

- 把 PDF 每页渲染后纵向拼成一张长图，自动控制总高度上限

## 环境要求

- Windows（凭据加密依赖 DPAPI；其余功能跨平台，但未在别的系统上验证）
- Python 3.9+
- 可选：Chrome / Edge / Chromium，用于 Ci-en 登录和 PDF 导出

## 安装

```bash
pip install -r requirements.txt
```

依赖也可以装到项目内的 `libs/` 目录，程序会自动把该目录加入模块搜索路径：

```bash
pip install -r requirements.txt --target libs
```

## 使用

### 图形界面

```bash
python ranker_gui.py
```

左侧「抓取」页选择站点、榜单周期、分类和条数，点「开始抓取」即可。
其余选项（翻页数、请求间隔、报告布局、代理、保存目录）在「高级」页。

Ci-en 的登录与「待关注作者」在「抓取」页下方；「清理」在「高级」页。

### 命令行

```bash
# DLsite 游戏区周榜前 50，带封面图和精确评分，导出 HTML
python ranker.py --site dlsite --period week --category game --limit 50 \
                 --precise-rating --html

# 按官网销量排序
python ranker.py --period month --category game --sort sale

# Ci-en 文章榜
python ranker.py --site cien --period daily --category 9 --limit 30

# Ci-en 并抓取每篇文章的正文与配图
python ranker.py --site cien --period weekly --category 9 --cien-content

# 全局参数
python ranker.py --help
```

Ci-en 登录相关的三个子命令：

```bash
python ranker.py --cien-login     # 打开浏览器登录并保存凭据
python ranker.py --cien-status    # 查看登录状态
python ranker.py --cien-logout    # 退出登录，清除凭据
```

清理：

```bash
python ranker.py --cleanup                    # 只扫描预览，不删
python ranker.py --cleanup --cleanup-yes      # 确认后执行
python ranker.py --cleanup --keep-reports 10  # 保留最近 10 份报告
```

### PDF 转长图

```bash
python pdf2img.py input.pdf -o output --dpi 150 --format JPG
```

## 输出位置

程序在自己所在目录下创建这些子目录（已被 `.gitignore` 忽略）：

| 目录 | 内容 |
|---|---|
| `reports/` | HTML 报告与导出的 PDF，文件名带抓取时间戳 |
| `images/` | 封面图与正文配图 |
| `debug/` | 页面样本与访问缓存，页面改版时可用来校准解析规则 |
| `logs/` | 运行日志 |
| `settings.json` | 界面配置（保存目录、代理等） |

## Ci-en 登录是怎么做的

Edge / Chrome 从 127 版起给 cookie 加了 **App-Bound Encryption**，密钥由浏览器的提权服务托管并校验调用方身份。想直接从磁盘解密 cookie，等同于破解浏览器的安全机制 —— 本项目不做这种事。

实际做法完全不碰加密：

1. 唤起一个属于本工具的浏览器 profile（`.cien-profile`），打开 Ci-en
2. 用户在**真实的浏览器窗口**里自己登录，验证码 / 两步验证也由用户完成
3. 登录成功后，通过 Chromium 官方的调试接口（CDP）取回 cookie
4. 用 Windows DPAPI 加密保存到 `cien_login.dat`，只有当前 Windows 账户解得开

顺手会清掉浏览器在这个 profile 里囤的缓存和模型文件 —— Edge 光是「优化指南模型」和「实体抽取模型」就能占掉 60 MB，跟登录毫无关系。

## 合规说明

- 只抓取 **robots.txt 允许**的公开排行榜页面；`/api/`、`/mypage` 等被禁止的路径一律不碰
- 串行请求、低频访问、自动退避，不并发、不伪装 User-Agent
- **不提供**任何绕过付费墙、破解浏览器加密、伪造身份或对抗风控的功能
- 「关注作者」「订阅创作者」这类操作需要用户**自己在浏览器里完成** —— 关注按钮位于 robots 禁止自动访问的路径，而且关注与赞助本质上是对创作者的支持表态，不该由脚本代劳

DLsite 使用条款禁止未经许可的自动获取。请自行控制请求频率，**仅作个人整理使用**，不要二次分发抓取到的内容。

## 项目结构

```
ranker.py         核心：抓取、解析、存储、报告渲染、Ci-en 登录与清理
ranker_gui.py     图形界面（customtkinter）
pdf2img.py        独立的 PDF 转长图工具
build_exe.bat     用 PyInstaller 打包成单文件 exe
run_gui.bat       双击启动图形界面
run_daily.bat     双击跑一次日常抓取
run_pdf2img.bat   双击启动 PDF 转长图工具
```

## 已知限制

- DLsite 列表页的 `star_NN` 是星级图标档位，不是真实评分；要精确评分得开 `--precise-rating` 逐页取
- Ci-en 不公开销量数据，榜单只提供名次与排序
- Ci-en 的正文配图 URL 带签名会过期，必须在抓取当时下载
- 站点改版会导致解析失败 —— 程序会把页面样本存到 `debug/`，可据此校准规则
