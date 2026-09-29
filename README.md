# DLsite / Ci-en 排行榜抓取工具

用于抓取 **DLsite**与 **Ci-en**的排行榜，
导出成带封面图的 HTML 报告；另附一个把 PDF 转成一张长图的小工具。

## 功能

**排行榜抓取**

- DLsite：日 / 周 / 月 / 年 × 漫画CG、游戏视频、音声ASMR，支持官网的「人気順」与「販売数順」
- Ci-en：日 / 周 / 月 × 游戏、音声、漫画、插画等分类
- 可选抓取每个作品的详情页，取精确评分（列表页只有星级图标，不是真实分数）
- 可选下载封面图、正文配图

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
