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
