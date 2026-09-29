#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLsite 排行榜抓取工具 —— 核心模块（抓取 + 解析 + 存储 + 报告）

抓什么：
    男性向 R18 区（maniax）排行榜，日/周/月/年 × 漫画CG、游戏视频、音声ASMR，
    取前 N 名，记录：
      名次 / 作品号 / 标题 / 发售日 / 精确评分 / 评价人数 /
      期间销量 / 累计销量 / 社团 / 作品链接 / 封面图

可选的额外动作：
    --precise-rating   逐个打开作品详情页取精确评分（4.51 这种，非图标档位）
    --download-images  下载每个作品的一张封面图
    --html             生成带封面的 HTML 报告（report.html）

命令行用法：
    python ranker.py --limit 50 --precise-rating --download-images --html
    python ranker.py --period day week --category game --limit 20
    python ranker.py --only-report            # 只重新生成报告，不抓取

图形界面：
    python ranker_gui.py

合规说明：
    只抓公开排行榜页面上的元数据，串行低频、不并发、不绕任何防护。
    DLsite 使用条款禁止未经许可的自动获取，请自行控制频率并仅作个人整理使用。

注意事项
  1. 列表页的 star_NN（如 star_50）是「星级图标档位」，不是评分！
     例：RJ01676425 列表页显示 star_50，实际评分 4.51。规律是「向上取整到最近的 0.5 星」，
     所以榜单里 75% 都显示 star_50。精确评分只能去详情页取 schema.org 的 ratingValue。
  2. `<li class="sales_date">` 里装的是「販売日」＝发售日，不是销量，别被类名骗了。
  3. 销量有两个口径：左栏 dl_count 随榜单周期变（期间值），右栏 _dl_count_ 是累计值。
  4. 写入 CSV 前先确认文件没被 Excel 打开，否则 PermissionError。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html as html_mod
import json
import os
import pathlib
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

# ===========================================================================
# 一、站点常量与解析规则（全部按真实页面校准过）
# ===========================================================================

# ===========================================================================
# 站点配置
# ===========================================================================

SITES = ("dlsite", "cien")
SITE_LABEL = {"dlsite": "DLsite 作品榜", "cien": "Ci-en 文章榜"}

# ---------- DLsite（maniax 男性向 R18 作品榜）----------
BASE = "https://www.dlsite.com"
DLSITE_SITE = "maniax"

PERIODS = ("day", "week", "month", "year")      # 24小时 / 7日 / 30日 / 年间
PERIOD_LABEL = {"day": "日榜", "week": "周榜", "month": "月榜", "year": "年榜"}

# maniax 排行榜只有三个分类；「游戏」和「视频」同属 game（ゲーム・動画）
CATEGORIES = ("comic", "game", "voice")
CATEGORY_LABEL = {"comic": "漫画・CG", "game": "游戏・视频", "voice": "音声・ASMR"}

# ---------- Ci-en（创作者文章榜）----------
# 页面 https://ci-en.dlsite.com/ranking/articles/{period}?categoryId=N&page=K
# 数据在页内 <script id="__NUXT_DATA__"> 的 Nuxt payload 里（devalue 扁平数组）。
# 注意 robots.txt 禁止 /api/，所以只抓网页版，不碰它暴露出来的那个 JSON 接口。
CIEN_BASE = "https://ci-en.dlsite.com"
CIEN_PERIODS = ("daily", "weekly", "monthly")   # 三档都有效
CIEN_PERIOD_LABEL = {"daily": "日榜", "weekly": "周榜", "monthly": "月榜"}
CIEN_CATEGORIES = (9, 8, 2, 1, 7, 18, 10)       # 对应的 categoryId
CIEN_CATEGORY_LABEL = {
    9: "游戏", 8: "音声作品", 2: "漫画", 1: "插画",
    7: "声优・歌手", 18: "VTuber", 10: "YouTuber・实况",
}

# R18 分类要先过年龄确认：在页面上点一次「はい」之后，站点会写下这个 cookie，
# 服务端凭它决定要不要把榜单渲染进 HTML —— 缺了它整页只剩一张年龄确认门，一条数据都没有。
# 下面这行等价于「在网页上确认一次年龄」：不改 UA、不碰 robots 禁止的 /api/。
CIEN_ACCEPT_COOKIE = "accepted_rating=r18"


def cien_cookie(cookie=None, use_login=True):
    """
    Ci-en 请求用的 Cookie。

    依次补上：年龄确认项 → 工具里保存的登录凭据（如果有）→ 用户额外给的 Cookie。
    同名字段只保留最先出现的那一份。
    """
    merged, seen = [], set()

    def add(text):
        for part in (text or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name = part.split("=", 1)[0].strip().lower()
            if name in seen:
                continue
            seen.add(name)
            merged.append(part)

    add(CIEN_ACCEPT_COOKIE)
    if use_login:
        credential, _ = load_cien_credential()
        if credential:
            add(credential)
    add(cookie)
    return "; ".join(merged)


def site_options(site):
    """按站点返回 (周期元组, 周期标签, 分类元组, 分类标签)。GUI 用它做联动。"""
    if (site or "dlsite").lower() == "cien":
        return CIEN_PERIODS, CIEN_PERIOD_LABEL, CIEN_CATEGORIES, CIEN_CATEGORY_LABEL
    return PERIODS, PERIOD_LABEL, CATEGORIES, CATEGORY_LABEL


REPORT_DIR_NAME = "reports"      # 报告（HTML 和导出的 PDF）统一放这个子目录


def report_filename(site, when=None):
    """
    报告文件名：站点 + 日期 + 时间。

    形如 report_cien_2026.09.14_1430.html —— 精确到「时:分」，
    同一分钟内的多次抓取会覆盖（时间用紧凑写法，避免与日期的点混淆）。
    """
    site = (site or "dlsite").lower()
    if site not in SITES:
        site = "dlsite"
    stamp = (when or datetime.now()).strftime("%Y.%m.%d_%H%M")
    return "report_%s_%s.html" % (site, stamp)


def report_dir(base_dir):
    """报告目录：工作目录下的 reports/。"""
    return os.path.join(base_dir, REPORT_DIR_NAME)


def report_path(base_dir, site):
    """报告的完整路径（HTML 与 PDF 同目录，导出 PDF 时按同名换后缀）。"""
    return os.path.join(report_dir(base_dir), report_filename(site))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6",
    "Accept-Encoding": "gzip",
    "Connection": "close",
}

# 命中这些片段说明拿到的不是正常页面（Cloudflare 拦截 / 地区限制）
SUSPECT_MARKERS = (
    "just a moment", "cf-chl", "cf_chl", "attention required",
    "enable javascript and cookies", "checking your browser",
    "access denied", "not available in your region",
)

# --- 条目切块：每个作品以 <div class="rank_no ..."> 开头，一页 100 个 ---
RANK_MARK_RE = re.compile(r'<div class="rank_no[^"]*">', re.I)
RANK_RE = re.compile(r'class="rank_no[^"]*">\s*(\d{1,4})', re.I)
PRODUCT_ID_RE = re.compile(r"product_id[/=]?((?:RJ|VJ|BJ)\d+)", re.I)
PRODUCT_LINK_RE = re.compile(r'product_id/((?:RJ|VJ|BJ)\d+)\.html', re.I)

# 标题：work_name 块里指向作品页的那个 <a>（前面还有 period_date 促销文字干扰）
TITLE_RE = re.compile(
    r'class="work_name".{0,2000}?<a[^>]{0,400}?product_id/(?:RJ|VJ|BJ)\d+\.html"[^>]{0,300}?>(.{1,300}?)</a>',
    re.I | re.S)

# 评价人数：(410) / (1,158)，带千位分隔符
RATING_COUNT_RE = re.compile(r'class="star_rating\s+star_\d{1,3}"[^>]*>\s*\(([\d,]{1,11})\)', re.I)

# 销量两口径
SALES_PERIOD_RE = re.compile(r'class="dl_count"[^>]*>\s*<span class="dl_count_label">販売数</span>\s*([\d,]+)', re.I)
SALES_TOTAL_RE = re.compile(r'class="_dl_count_[A-Z]{2}\d+"[^>]*>([\d,]+)<', re.I)

# 发售日（class 名叫 sales_date，内容是販売日）
RELEASE_BLOCK_RE = re.compile(r'class="sales_date"[^>]*>([^<]{1,80})<', re.I)
RELEASE_DATE_RE = re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日')

# 社团
CIRCLE_RE = re.compile(r'class="maker_name"[^>]*>\s*<a[^>]{0,400}?>([^<]{1,200})</a>', re.I | re.S)

# 封面图：主图（~150KB）与缩略图（~35KB），都是协议相对地址
IMAGE_MAIN_RE = re.compile(r'//img\.dlsite\.jp/modpub/images2/work/[^"\'\s\\]+?_img_main\.jpg')
IMAGE_THUMB_RE = re.compile(r'//img\.dlsite\.jp/resize/images2/work/[^"\'\s\\]+?_img_main_\d+x\d+\.jpg')

# 详情页的精确评分（服务端渲染的 schema.org 微数据）
PRECISE_VALUE_RE = re.compile(r'itemprop=["\']ratingValue["\'][^>]{0,200}?content=["\']([\d.]+)["\']', re.I)
PRECISE_COUNT_RE = re.compile(r'itemprop=["\']ratingCount["\'][^>]{0,200}?content=["\'](\d+)["\']', re.I)

# 排行榜链接 / 分类链接（供结构探测用）
RANKING_LINK_RE = re.compile(r'href="([^"]{0,200}?/ranking/[^"]{0,120})"', re.I)
CATEGORY_LINK_RE = re.compile(
    r'href="[^"]{0,200}?[?&]category=([A-Za-z0-9_\-]{1,30})[^"]{0,200}"[^>]{0,200}>(.{0,60}?)</a>',
    re.I | re.S)

FIELDS = ["fetched_at", "site", "period", "category", "rank", "product_id", "title",
          "release_date", "rating", "rating_count", "sales_period", "sales_total",
          "circle", "url", "image_url", "image_file", "sort_mode"]

# ---------------------------------------------------------------------------
# 榜单排序
# 官网页面上就是两个标签：人気順（默认）和 販売数順（URL 追加 sort=sale）。
# 这是服务器端排序，口径和官网上看到的一致 —— 不要自己按字段重排，那样对不上。
# 只对 DLsite 有效：Ci-en 的文章榜没有这个开关。
# ---------------------------------------------------------------------------
RANKING_SORTS = ("popular", "sale")
RANKING_SORT_LABEL = {
    "popular": "人気順（默认）",
    "sale": "販売数順",
}


# ===========================================================================
# 二、网络层
# ===========================================================================

# --- 代理模式 ---------------------------------------------------------------
PROXY_MODES = ("auto", "none", "http", "socks5")
PROXY_MODE_LABEL = {
    "auto": "自动（跟随系统代理／环境变量）",
    "none": "直连（忽略所有代理设置）",
    "http": "HTTP 代理",
    "socks5": "SOCKS5 代理（需要 PySocks）",
}

_ORIGINAL_SOCKET = socket.socket        # 打 SOCKS 补丁前留一份原始函数


def parse_proxy_url(proxy_url, default_port=7890):
    """
    '127.0.0.1:7890' ／ 'http://127.0.0.1:7890' ／ 'socks5://127.0.0.1:1080'
        → ('127.0.0.1', 7890)
    只写主机名时用 default_port 兜底。
    """
    text = (proxy_url or "").strip()
    for prefix in ("socks5h://", "socks5://", "socks4://", "http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip().rstrip("/")
    if not text:
        return "", default_port
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        try:
            return host, int(port_text)
        except ValueError:
            pass
    return text, default_port


def socks_available() -> bool:
    """PySocks 是否已安装。"""
    try:
        import socks                    # noqa: F401
        return True
    except ImportError:
        return False


def apply_socks(proxy_url):
    """
    给当前进程打 SOCKS5 补丁（urllib 原生不支持 SOCKS，只能这样）。

    返回 (ok, message)。调用方用完必须调用 restore_socket()。
    """
    try:
        import socks
    except ImportError:
        return False, "SOCKS5 需要 PySocks，请先运行：pip install PySocks"
    host, port = parse_proxy_url(proxy_url, default_port=1080)
    if not host:
        return False, "SOCKS5 地址为空"
    socks.set_default_proxy(socks.SOCKS5, host, port, rdns=True)
    socket.socket = socks.socksocket
    return True, "%s:%d" % (host, port)


def restore_socket():
    """还原 socket，别让补丁影响进程里其他网络调用。"""
    socket.socket = _ORIGINAL_SOCKET


def build_opener(proxy_mode="auto", proxy_url=None):
    """
    按代理模式构造 opener。

      auto   跟随系统设置／环境变量（默认，与以前行为一致）
      none   强制直连
      http   手动 HTTP 代理（proxy_url 可省略 http:// 前缀）
      socks5 socket 已由 apply_socks() 打过补丁，这里按「不走 HTTP 代理」构造
    """
    mode = (proxy_mode or "auto").lower()
    if mode in ("none", "socks5"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if mode == "http" and proxy_url:
        url = proxy_url if "://" in proxy_url else "http://" + proxy_url
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url}))
    return urllib.request.build_opener(urllib.request.ProxyHandler())


def fetch(url, opener=None, cookie=None, timeout=30, retries=3, backoff=3.0):
    """
    抓一个 URL，返回 (status, body_text, note)。

    只有普通请求头、低频、失败退避——不做任何防护绕过。
    网络层最终失败时抛 SystemExit（CLI 场景足够，GUI 会自己捕获）。
    """
    opener = opener or build_opener()
    last_err = ""

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=dict(HEADERS))
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                status = getattr(resp, "status", 200)
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return status, raw.decode("utf-8", errors="replace"), ("gzip" if "gzip" in enc else "")

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = "HTTP %s %s" % (exc.code, exc.reason)
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            return exc.code, body, last_err

        except Exception as exc:                            # noqa: BLE001
            last_err = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue

    raise SystemExit(
        "[x] 请求失败：%s\n"
        "    排查：1) 这台机器能否访问 %s；2) 走了代理吗（HTTPS_PROXY）；\n"
        "          3) 若返回 403/验证页说明触发了站点防护，请降低频率，不要绕过。"
        % (last_err, BASE))


def looks_like_block(body: str):
    """返回命中的拦截特征；正常页面返回 None。"""
    low = body[:4000].lower()
    for marker in SUSPECT_MARKERS:
        if marker in low:
            return marker
    if len(body) < 2000:
        return "页面过短（%d 字节）" % len(body)
    return None


def test_proxy(proxy_mode="auto", proxy_url=None, timeout=20) -> dict:
    """
    用指定的代理设置访问一个轻量地址，返回结果字典（不抛异常，失败信息在 message 里）。

    给 GUI 的「测试代理」按钮和 CLI 的 --test-proxy 用。
    """
    target = BASE + "/robots.txt"
    mode = (proxy_mode or "auto").lower()
    applied = False
    try:
        if mode == "socks5":
            ok, message = apply_socks(proxy_url)
            if not ok:
                return {"ok": False, "status": None, "seconds": 0.0,
                        "message": message, "mode": mode}
            applied = True

        opener = build_opener(mode, proxy_url)
        start = time.time()
        try:
            status, body, note = fetch(target, opener=opener, timeout=timeout, retries=1)
        except SystemExit as exc:
            # fetch 失败时抛的 SystemExit 带一大段排查提示，界面里只留第一行
            first_line = (str(exc) or "请求失败").splitlines()[0]
            return {"ok": False, "status": None, "seconds": 0.0,
                    "message": first_line, "mode": mode}
        elapsed = time.time() - start

        blocked = looks_like_block(body)
        if status == 200 and not blocked:
            return {"ok": True, "status": status, "seconds": elapsed, "mode": mode,
                    "message": "连通正常（%d 字节，%.1f 秒）" % (len(body), elapsed)}
        return {"ok": False, "status": status, "seconds": elapsed, "mode": mode,
                "message": "异常响应：HTTP %s%s" % (status, (" / " + blocked) if blocked else "")}

    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "status": None, "seconds": 0.0, "mode": mode,
                "message": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if applied:
            restore_socket()


# ===========================================================================
# 三、解析层
# ===========================================================================

def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]{1,300}>", " ", value)
    value = html_mod.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def iter_item_blocks(body: str, max_block: int = 20000):
    """把排行榜 HTML 切成「每个作品一块」，以 rank_no 为界（没有则退化为 product_id）。"""
    marks = list(RANK_MARK_RE.finditer(body))
    if not marks:
        marks = list(PRODUCT_ID_RE.finditer(body))
    if not marks:
        return
    for idx, mark in enumerate(marks):
        start = mark.start()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else min(len(body), start + max_block)
        if end <= start:
            end = min(len(body), start + max_block)
        yield body[start:end]


def _abs_image_url(match) -> str:
    url = match.group(0).replace("\\/", "/")
    return "https:" + url if url.startswith("//") else url


def extract_image_urls(block: str, prefer: str = "main"):
    """同时取出两种尺寸的封面地址，返回 (首选, 备选)；取不到的是空串。

    列表页里主图和缩略图一般都在。主图是 CDN 上的大图，偶尔会 404，
    所以把另一个尺寸也留着——下载失败时可以拿它顶上。
    """
    main_m = IMAGE_MAIN_RE.search(block)
    thumb_m = IMAGE_THUMB_RE.search(block)
    main = _abs_image_url(main_m) if main_m else ""
    thumb = _abs_image_url(thumb_m) if thumb_m else ""
    return (thumb, main) if prefer == "thumb" else (main, thumb)


def extract_image_url(block: str, prefer: str = "main") -> str:
    """取封面图地址（只要首选那个，留着重给旧调用方）。"""
    return extract_image_urls(block, prefer)[0]


def extract_entries(body: str, image_prefer: str = "main"):
    """
    解析排行榜页面 → 条目列表。

    rating 恒为空（列表页没有评分），要用 fetch_precise_rating() 补。
    """
    entries = []
    for order, block in enumerate(iter_item_blocks(body), start=1):
        link = PRODUCT_LINK_RE.search(block)
        if not link:
            continue
        pid = link.group(1).upper()

        rank_m = RANK_RE.search(block)
        try:
            rank = int(rank_m.group(1)) if rank_m else order
        except (TypeError, ValueError):
            rank = order

        title = ""
        tm = TITLE_RE.search(block)
        if tm:
            title = _clean_text(tm.group(1))
        else:
            wm = re.search(r'class="work_name".{0,2000}?</dt>', block, re.I | re.S)
            if wm:
                am = re.search(r"<a[^>]{0,400}?>([^<]{2,300})</a>", wm.group(0))
                if am:
                    title = _clean_text(am.group(1))

        rating_count = ""
        rcm = RATING_COUNT_RE.search(block)
        if rcm:
            rating_count = rcm.group(1).replace(",", "")

        circle = ""
        cm = CIRCLE_RE.search(block)
        if cm:
            circle = _clean_text(cm.group(1))

        release_date = ""
        rbm = RELEASE_BLOCK_RE.search(block)
        if rbm:
            dm = RELEASE_DATE_RE.search(html_mod.unescape(rbm.group(1)))
            if dm:
                release_date = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))

        sales_period = ""
        spm = SALES_PERIOD_RE.search(block)
        if spm:
            sales_period = spm.group(1).replace(",", "")
        sales_total = ""
        stm = SALES_TOTAL_RE.search(block)
        if stm:
            sales_total = stm.group(1).replace(",", "")
        image_url, image_fallback = extract_image_urls(block, image_prefer)

        entries.append({
            "rank": rank,
            "product_id": pid,
            "title": title,
            "release_date": release_date,
            "rating": "",
            "rating_count": rating_count,
            "sales_period": sales_period,
            "sales_total": sales_total,
            "circle": circle,
            "url": "%s/maniax/work/=/product_id/%s.html" % (BASE, pid),
            "image_url": image_url,
            "image_url_fallback": image_fallback,
            "image_file": "",
        })

    entries.sort(key=lambda item: item["rank"])
    return entries


def fetch_precise_rating(product_url, opener=None, cookie=None, timeout=30):
    """打开作品详情页取精确评分，返回 (rating, rating_count)，取不到给空串。"""
    status, body, _ = fetch(product_url, opener=opener, cookie=cookie, timeout=timeout, retries=2)
    if status != 200:
        return "", ""
    rv = PRECISE_VALUE_RE.search(body)
    rc = PRECISE_COUNT_RE.search(body)
    return (rv.group(1) if rv else "", rc.group(1) if rc else "")


def download_image(url: str, dest_path: str, opener=None, timeout=30, referer=None):
    """下载一张图，返回 (ok, size)。

    CDN 会看 Referer：DLsite 的图用 dlsite.com，Ci-en 的图用自己的域名，
    所以这里留了 referer 参数（不传就按 DLsite 处理）。
    """
    if not url:
        return False, 0
    opener = opener or build_opener()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer or (BASE + "/"),
        "Accept": "image/avif,image/webp,image/jpeg,image/*,*/*;q=0.8",
    })
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        return False, 0
    parent = os.path.dirname(os.path.abspath(dest_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return True, len(data)


def build_ranking_url(period, category=None, page=None, site=DLSITE_SITE, sort=None):
    url = "%s/%s/ranking/%s" % (BASE, site, period)
    query = {}
    if category:
        query["category"] = category
    if page and page > 1:
        query["page"] = str(page)
    # 官网的「販売数順」靠这个参数实现；人気順（默认）不加
    if sort and sort != "popular":
        query["sort"] = sort
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


# ---------------------------------------------------------------------------
# Ci-en：页面是 Nuxt 3，数据全在 <script id="__NUXT_DATA__"> 里，
# 格式是 devalue 的扁平数组 —— 每个元素是数据，对象/数组的成员用「整数索引」互相引用，
# 所以必须先解引用才能还原成正常结构。
# 另：robots.txt 禁止 /api/，所以只抓网页版（内容与之等价，因为页面是 SSR 的）。
# ---------------------------------------------------------------------------

NUXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)


def build_cien_url(period, category_id, page=1):
    return "%s/ranking/articles/%s?categoryId=%s&page=%d" % (
        CIEN_BASE, period, category_id, page)


def devalue_resolve(payload, index, depth=0):
    """把 devalue 扁平数组里 index 指向的值还原成普通 Python 结构。"""
    if depth > 14:
        return None
    if not isinstance(index, int) or index < 0 or index >= len(payload):
        return index
    value = payload[index]
    if isinstance(value, dict):
        return {key: devalue_resolve(payload, item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list):
        # ["ShallowReactive", n] / ["Date", "..."] 之类特殊标记
        if value and isinstance(value[0], str) and value[0][:1].isupper():
            return devalue_resolve(payload, value[1], depth + 1) if len(value) > 1 else None
        return [devalue_resolve(payload, item, depth + 1) for item in value]
    return value


def extract_cien_entries(body):
    """
    解析 Ci-en 榜单页 → (entries, meta)。

    meta 含 total / last_page / per_page。
    条目字段映射成与 DLsite 一致的结构（缺的评分/销量留空），
    另附 Ci-en 专有字段：article_id / creator_url / creator_icon / published_at / category。
    """
    match = NUXT_DATA_RE.search(body)
    if not match:
        return [], {}
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return [], {}

    raw_items, meta = [], {}
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            continue
        if "current_page" in raw and "last_page" in raw:
            page_info = devalue_resolve(payload, index) or {}
            meta = {"total": page_info.get("total"),
                    "last_page": page_info.get("last_page"),
                    "per_page": page_info.get("per_page")}
        elif ("replaced_title" in raw or "title" in raw) and "creator" in raw:
            raw_items.append(devalue_resolve(payload, index) or {})

    entries = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        creator = item.get("creator") or {}
        published = (item.get("released_at") or "").strip()
        entries.append({
            "rank": 0,
            "product_id": "A" + str(item.get("id") or ""),   # 借这一列放文章 ID
            # 未登录时字段叫 replaced_title（站点处理过的标题），登录后是真 title
            "title": item.get("replaced_title") or item.get("title") or "",
            "release_date": published[:10],                  # "2026-09-13 01:38:00" → 日期
            "rating": "",                                    # Ci-en 没有评分
            "rating_count": "",
            "sales_period": "",                              # 也没有销量
            "sales_total": "",
            "circle": creator.get("name") or "",             # 「社团」这一列放作者名
            "url": item.get("path") or "",
            "image_url": item.get("image_src") or "",
            "image_url_fallback": "",                        # Ci-en 只提供一种尺寸
            "image_file": "",
            # ---- Ci-en 专有 ----
            "article_id": str(item.get("id") or ""),
            "creator_url": creator.get("path") or "",
            "creator_icon": creator.get("image_path") or "",
            "published_at": published,
            "category": item.get("creator_category_label") or "",
        })
    return entries, meta


def probe_page(url, opener=None, cookie=None, save_html=None):
    """
    结构探测：抓一个页面，返回诊断信息字典（给 GUI 的「检查页面」用）。
    已校准过规则，一般不需要；页面改版时可以拿来对结构。
    """
    status, body, note = fetch(url, opener=opener, cookie=cookie)
    blocked = looks_like_block(body)
    info = {
        "url": url, "status": status, "bytes": len(body), "blocked": blocked,
        "ranking_links": [], "categories": {}, "entries": [], "saved": None,
    }
    links = []
    for m in RANKING_LINK_RE.finditer(body):
        href = html_mod.unescape(m.group(1))
        if href.startswith("/"):
            href = BASE + href
        if href not in links:
            links.append(href)
    info["ranking_links"] = links[:20]

    cats = {}
    for m in CATEGORY_LINK_RE.finditer(body):
        cats.setdefault(m.group(1), _clean_text(m.group(2)))
    info["categories"] = cats

    info["entries"] = extract_entries(body)[:10]

    if save_html:
        os.makedirs(os.path.dirname(os.path.abspath(save_html)), exist_ok=True)
        with open(save_html, "w", encoding="utf-8") as fh:
            fh.write(body)
        info["saved"] = os.path.abspath(save_html)
    return info


# ===========================================================================
# 四、存储层
# ===========================================================================

def write_csv(path: str, rows):
    new_file = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in FIELDS})
    except PermissionError:
        raise SystemExit(
            "[x] 写不进 %s：文件正被其他程序占用。\n"
            "    最常见的原因是用 Excel 打开了它。关掉 Excel 再跑一次即可，\n"
            "    或者换一个输出文件名。\n" % os.path.abspath(path))


def write_sqlite(path: str, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ranking (
                   fetched_at   TEXT, period TEXT, category TEXT, rank INTEGER,
                   product_id   TEXT, title TEXT, release_date TEXT,
                   rating REAL, rating_count INTEGER,
                   sales_period INTEGER, sales_total INTEGER,
                   circle TEXT, url TEXT, image_url TEXT, image_file TEXT,
                   PRIMARY KEY (fetched_at, period, category, rank))"""
        )
        conn.executemany(
            "INSERT OR REPLACE INTO ranking VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(
                row["fetched_at"], row["period"], row["category"], row["rank"],
                row["product_id"], row["title"], row.get("release_date", ""),
                float(row["rating"]) if row.get("rating") else None,
                int(row["rating_count"]) if row.get("rating_count") else None,
                int(row["sales_period"]) if row.get("sales_period") else None,
                int(row["sales_total"]) if row.get("sales_total") else None,
                row.get("circle", ""), row["url"],
                row.get("image_url", ""), row.get("image_file", ""),
            ) for row in rows])
        conn.commit()
    finally:
        conn.close()


def load_previous(csv_path: str):
    """读 CSV，返回 {(period, category): {product_id: rank}}（上一次抓取的快照）。"""
    if not os.path.exists(csv_path):
        return {}
    latest, buckets = {}, {}
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("period", ""), row.get("category", ""))
            stamp = row.get("fetched_at", "")
            if key not in latest or stamp > latest[key]:
                latest[key] = stamp
                buckets[key] = []
            if stamp == latest[key]:
                buckets[key].append(row)
    return {k: {r["product_id"]: int(r["rank"]) for r in v if r.get("product_id")}
            for k, v in buckets.items()}


# ===========================================================================
# 五、报告层（HTML）
# ===========================================================================

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 28px 60px;
  font-family: "Segoe UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
  background: #f5f6f8; color: #222;
}
h1 { font-size: 20px; margin: 0 0 6px; }
.meta { color: #667; font-size: 13px; margin-bottom: 8px; }
.meta b { color: #334; }
.section-title {
  font-size: 15px; margin: 26px 0 12px; color: #445;
  border-left: 3px solid #4a7d5f; padding-left: 9px;
}
/* 布局 card：竖版卡片，图上文下，多列（默认） */
.layout-card { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 14px; }
.layout-card .card { flex-direction: column; }
.layout-card .thumb { flex: 0 0 auto; }
.layout-card .thumb img, .layout-card .thumb.noimg { width: 100%; height: 160px; }
/* 布局 list：竖向单列，一行一个作品 */
.layout-list { display: flex; flex-direction: column; gap: 12px; }
.layout-list .thumb, .layout-list .thumb img, .layout-list .thumb.noimg {
  flex: 0 0 170px; width: 170px; height: 170px;
}
/* 布局 grid：多列网格，图左文右 */
.layout-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(370px, 1fr)); gap: 14px; }
/* 布局 columns：每个榜单（日/周/月）各占一列，列内竖着排 */
.layout-columns { display: flex; gap: 12px; align-items: flex-start; }
.layout-columns > .col {
  flex: 1 1 0; min-width: 0;
  display: flex; flex-direction: column; gap: 10px;
}
.layout-columns .col-title {
  font-size: 14px; font-weight: 600; color: #3a4250;
  padding: 4px 2px 8px; border-bottom: 1px solid #e5e5ea; margin-bottom: 2px;
}
.layout-columns .col-title span { color: #8a8f98; font-weight: 400; font-size: 12px; }
.layout-columns .card { flex-direction: column; gap: 8px; }
.layout-columns .thumb { flex: 0 0 auto; }
.layout-columns .thumb img, .layout-columns .thumb.noimg { width: 100%; height: 132px; }
.layout-columns .title { font-size: 13px; }
.card {
  display: flex; gap: 12px; background: #fff; border-radius: 10px;
  padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.09);
  transition: box-shadow .15s ease;
}
.card:hover { box-shadow: 0 3px 12px rgba(0,0,0,.14); }
.thumb { flex: 0 0 132px; }
.thumb img {
  width: 132px; height: 132px; object-fit: cover; display: block;
  border-radius: 8px; background: #eceef1;
}
.thumb.noimg {
  width: 132px; height: 132px; border-radius: 8px; background: #eceef1;
  color: #9aa; font-size: 12px; display: flex; align-items: center; justify-content: center;
}
.info { flex: 1 1 auto; min-width: 0; }
.rank { font-weight: 700; font-size: 13px; color: #c0392b; }
.title { font-size: 14px; line-height: 1.38; margin: 3px 0 9px; }
.title a { color: #1a3d7c; text-decoration: none; }
.title a:hover { text-decoration: underline; }
.stats { display: flex; flex-wrap: wrap; gap: 3px 12px; font-size: 12px; color: #667; }
.stats span b { color: #222; font-weight: 600; }
.score { color: #b8860b; font-weight: 700; font-size: 13px; }
.pid { font-family: Consolas, monospace; font-size: 11px; color: #99a; }
footer { margin-top: 34px; color: #99a; font-size: 12px; }

/* ===== 导出 PDF / 浏览器打印 =====
   走的是 Chromium 的打印通道，所以分页、背景色这些必须靠 @media print 控制，
   否则导出的 PDF 会丢掉灰底、卡片被从中间劈开。 */
@media print {
  @page { size: A4; margin: 10mm; }
  /* 不加这句，Chromium 会为了省墨把背景色和阴影全部丢掉 */
  html, body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { padding: 0 0 8mm; }
  /* 卡片／封面／分节标题尽量别被分页线切开 */
  .card, .thumb, .thumb img, .section-title, footer {
    break-inside: avoid; page-break-inside: avoid;
  }
  .card, .card:hover { box-shadow: none; border: 1px solid #e5e5ea; }
  /* 纸上不用留这么多列间距，一页能多放几个作品 */
  .layout-card { grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
  .layout-grid { grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; }
  a[href]::after { content: none; }
}

/* 正文区：默认折叠，点开才看 —— 50 条正文铺开会变成几万字的流水账 */
.content { margin-top: 10px; border-top: 1px dashed #e3e3ea; }
.content summary { cursor: pointer; padding: 7px 0 3px; font-size: 12px;
  color: #5b6b8c; list-style: none; display: block; }
.content summary::-webkit-details-marker { display: none; }
.content summary::marker { content: ""; }
.content summary .excerpt { color: #8b93a6; }
.content summary .more { color: #3b6ea5; margin-left: 6px; white-space: nowrap; }
.content summary:hover .more { text-decoration: underline; }
.content[open] summary .more { color: #8b93a6; }
.content-text { white-space: pre-wrap; word-break: break-word; line-height: 1.8;
  font-size: 13.5px; color: #333; padding: 6px 2px 2px; }
/* 正文配图：缩略图排一行，点开看原图 */
.content-images { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.content-images a { display: block; line-height: 0; }
.content-images img { max-width: 240px; max-height: 240px; border-radius: 8px;
  border: 1px solid #e5e5ea; object-fit: cover; }
.content-images img:hover { border-color: #b9c6da; }
.content.locked { color: #96909a; font-size: 12.5px; padding: 7px 0 2px; }
.content.locked .hint { color: #b0b6c2; }
/* 多列布局里正文会被挤得很窄，限制高度，避免整页失衡 */
.layout-grid .content-body, .layout-columns .content-body {
  max-height: 240px; overflow: auto;
}
@media print { .content-body { max-height: none !important; overflow: visible !important; } }
"""

HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{meta}</div>
"""

HTML_TAIL = """<footer>由 ranker.py 生成 ｜ 数据来源：DLsite／Ci-en 公开排行榜页面（均在 robots.txt 允许范围内）｜ 仅供个人整理使用</footer>
</body>
</html>
"""


def esc(value) -> str:
    return html_mod.escape("" if value is None else str(value), quote=True)


def thousands(value) -> str:
    try:
        return "{:,}".format(int(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return esc(value) if value else "-"


def _image_prefix(image_base, out_dir):
    """算出「报告所在目录 → 图片目录」的相对前缀，例如 '../'。

    报告挪进 reports/ 子目录后，HTML 里就不能再写 images/xxx.jpg 了——浏览器
    会去找 reports/images/xxx.jpg，封面全部失效。跨盘符时相对路径算不出来，
    返回 None，调用方改用绝对的 file:// 路径。
    """
    try:
        rel = os.path.relpath(image_base, out_dir)
    except ValueError:                                 # Windows 上跨盘符
        return None
    return "" if rel == "." else rel.replace("\\", "/") + "/"


def _render_content_block(row, base_dir="", image_prefix="") -> str:
    """
    条目下方的正文区。

    有正文就折起来（默认收起，点开才看）—— 50 条正文直接铺开，
    报告会变成几万字的流水账。用原生 <details>，不需要 JavaScript。
    正文配图也折在同一块里。
    """
    text = (row.get("content") or "").strip()
    files = [f for f in (row.get("content_files") or []) if f]

    if text or files:
        chars = row.get("content_chars") or len(text)
        blocks = []
        if text:
            blocks.append('<div class="content-text">%s</div>' % esc(text))
        if files:
            tiles = []
            for name in files:
                rel = "images/" + name
                if image_prefix is None:                  # 跨盘符：退回绝对路径
                    src = pathlib.Path(os.path.join(base_dir, rel)).as_uri()
                else:
                    src = image_prefix + rel
                tiles.append('<a href="%s" target="_blank" rel="noreferrer">'
                             '<img src="%s" alt="" loading="lazy"></a>'
                             % (esc(src), esc(src)))
            blocks.append('<div class="content-images">%s</div>' % "".join(tiles))

        if text and files:
            label = "展开全文（%s 字，%d 图）" % (chars, len(files))
        elif text:
            label = "展开全文（%s 字）" % chars
        else:
            label = "展开图片（%d 张）" % len(files)

        excerpt = re.sub(r"\s+", " ", text)[:68] if text else ""
        excerpt_html = ('<span class="excerpt">%s…</span>' % esc(excerpt)
                        if excerpt else "")
        return ('    <details class="content">\n'
                '      <summary>%s<span class="more">%s</span></summary>\n'
                '      <div class="content-body">%s</div>\n'
                '    </details>\n'
                % (excerpt_html, esc(label), "\n".join(blocks)))

    state = (row.get("access_state") or "").strip()
    if state == "follow":
        return ('    <div class="content locked">🔒 需要关注作者才能阅读 '
                '<span class="hint">（工具里的「待关注作者」能一键跳过去）</span></div>\n')
    if state == "plan":
        plans = [p for p in (row.get("plans") or []) if p][:2]
        detail = ("　" + "、".join(plans)) if plans else ""
        return ('    <div class="content locked">🔒 需要赞助作者才能阅读%s</div>\n'
                % esc(detail))
    if state:
        return '    <div class="content locked">这一篇没抓到正文</div>\n'
    return ""


def render_card(row: dict, base_dir: str, image_prefix="") -> str:
    image = (row.get("image_file") or "").replace("\\", "/").strip()
    has_image = bool(image) and os.path.exists(os.path.join(base_dir, image))
    url = (row.get("url") or "").strip()

    if has_image:
        if image_prefix is None:                       # 跨盘符：退回绝对路径
            src = pathlib.Path(os.path.join(base_dir, image)).as_uri()
        else:
            src = image_prefix + image
        thumb = ('<a class="thumb" href="%s" target="_blank" rel="noreferrer">'
                 '<img src="%s" alt="" loading="lazy"></a>' % (esc(src), esc(src)))
    else:
        thumb = '<div class="thumb noimg">无封面图</div>'

    title = esc(row.get("title") or "(标题缺失)")
    title_html = ('<a href="%s" target="_blank" rel="noreferrer">%s</a>' % (esc(url), title)
                  if url else title)

    rating = (row.get("rating") or "").strip()
    rating_count = (row.get("rating_count") or "").strip()
    score_html = ""
    if rating:
        score_html = '<span class="score">★ %s</span>' % esc(rating)
        if rating_count:
            score_html += '<span style="font-size:11px;color:#99a"> (%s 人)</span>' % thousands(rating_count)

    is_cien = (row.get("site") or "dlsite").lower() == "cien"

    parts = []
    if score_html:
        parts.append('<span>%s</span>' % score_html)
    parts.append('<span>%s <b>%s</b></span>'
                 % ("发布" if is_cien else "发售", esc(row.get("release_date") or "-")))
    if not is_cien:
        # Ci-en 没有销量，缺就整行不显示，别渲染成 "-"
        if row.get("sales_period"):
            parts.append('<span>期间销量 <b>%s</b></span>' % thousands(row.get("sales_period")))
        if row.get("sales_total"):
            parts.append('<span>累计 <b>%s</b></span>' % thousands(row.get("sales_total")))
    if row.get("circle"):
        parts.append('<span>%s <b>%s</b></span>'
                     % ("作者" if is_cien else "社团", esc(row["circle"])))

    content_html = _render_content_block(row, base_dir, image_prefix)

    return ('<article class="card">\n  %s\n  <div class="info">\n'
            '    <div class="rank">#%s <span class="pid">%s</span></div>\n'
            '    <div class="title">%s</div>\n'
            '    <div class="stats">%s</div>\n%s  </div>\n</article>'
            % (thumb, esc(row.get("rank") or "-"), esc(row.get("product_id") or ""),
               title_html, "\n      ".join(parts), content_html))


def _render_report(rows, out_path, layout="card", base_dir=None, all_batches=False):
    """把条目列表渲染成 HTML 报告（build_report / build_report_from_rows 共用）。"""
    if not rows:
        raise ValueError("没有可生成报告的数据。")
    out_path = os.path.abspath(out_path)
    if base_dir is None:
        base_dir = os.path.dirname(out_path)
    # 报告在 reports/ 子目录里，图片引用要按相对位置换算前缀
    image_prefix = _image_prefix(base_dir, os.path.dirname(out_path))

    if all_batches:
        selected = rows
    else:
        latest = max((r.get("fetched_at") or "") for r in rows)
        selected = [r for r in rows if (r.get("fetched_at") or "") == latest]

    sections = {}
    for row in selected:
        sections.setdefault((row.get("period") or "", row.get("category") or ""), []).append(row)
    # 不按 rank 重排：抓取顺序即最终顺序
    # （用户可能选了「累计销量」「评价人数」之类的口径）。

    with_image = sum(1 for r in selected
                     if (r.get("image_file") or "")
                     and os.path.exists(os.path.join(base_dir, r["image_file"].replace("\\", "/"))))
    stamps = sorted({(r.get("fetched_at") or "") for r in selected})
    title = "DLsite 排行榜快照"
    meta = ("抓取时间 <b>%s</b> ／ 共 <b>%d</b> 条作品 ／ 含封面 <b>%d</b> 张"
            % (esc(stamps[-1] if stamps else "-"), len(selected), with_image))

    out = [HTML_HEAD.format(title=title, css=CSS, meta=meta)]

    if layout == "columns":
        # 每个榜单周期各占一列，列内从上往下排。
        # 列顺序按站点定义的周期顺序：日榜 → 周榜 → 月榜（→ 年榜），
        # 不能用字母序，否则会变成 日/月/周。
        first_site = next((r.get("site") for r in selected if r.get("site")), "dlsite")
        period_rank = {p: i for i, p in enumerate(site_options(first_site)[0])}

        by_period = {}
        for (period, _category), items in sorted(sections.items()):
            by_period.setdefault(period, []).extend(items)

        out.append('<div class="layout-columns">')
        for period in sorted(by_period, key=lambda p: period_rank.get(p, 99)):
            items = by_period[period]
            row_site = next((r.get("site") for r in items if r.get("site")), "dlsite")
            _p, p_names, _c, c_names = site_options(row_site)
            period_label = p_names.get(period, period or "-")

            # 同一列里可能混了多个分类，标题上用逗号点出来
            cats = []
            for row in items:
                cat = c_names.get(row.get("category"), row.get("category") or "")
                if cat and cat not in cats:
                    cats.append(cat)
            subtitle = "%d 条" % len(items)
            if cats:
                subtitle += "，" + "、".join(str(c) for c in cats)

            out.append('<div class="col">')
            out.append('<div class="col-title">%s <span>%s</span></div>'
                       % (esc(period_label), esc(subtitle)))
            for row in items:
                out.append(render_card(row, base_dir, image_prefix))
            out.append('</div>')
        out.append('</div>')
    else:
        for (period, category), items in sorted(sections.items()):
            # 分节标题按这批数据所属站点取叫法（两边周期/分类的名字不一样）
            row_site = next((r.get("site") for r in items if r.get("site")), "dlsite")
            _p, p_names, _c, c_names = site_options(row_site)
            period_label = p_names.get(period, period or "-")
            category_label = c_names.get(category, category or "全部")
            site_tag = "" if row_site == "dlsite" else "Ci-en "
            order = next((r.get("sort_mode") for r in items if r.get("sort_mode")), "popular")
            order_note = ""
            if order and order != "popular":
                order_note = "，按%s排序" % RANKING_SORT_LABEL.get(order, order)
            out.append('<div class="section-title">%s%s ／ %s（%d 条%s）</div>'
                       % (site_tag, esc(period_label), esc(category_label), len(items),
                          esc(order_note)))
            out.append('<div class="layout-%s">' % esc(layout))
            for row in items:
                out.append(render_card(row, base_dir, image_prefix))
            out.append('</div>')
    out.append(HTML_TAIL)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)   # reports/ 可能还不存在
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return out_path, len(selected)


def build_report(csv_path: str, out_path: str, all_batches: bool = False, layout: str = "card"):
    """读 CSV → 生成 HTML 报告，返回 (绝对输出路径, 条目数)。"""
    if not os.path.exists(csv_path):
        raise FileNotFoundError("找不到 %s" % csv_path)
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as fh:
        rows = [row for row in csv.DictReader(fh)]
    if not rows:
        raise ValueError("%s 里没有数据行。" % csv_path)
    return _render_report(rows, out_path, layout=layout,
                          base_dir=os.path.dirname(os.path.abspath(csv_path)),
                          all_batches=all_batches)


def build_report_from_rows(rows, out_path, layout="card", base_dir=None):
    """直接用内存里的条目生成报告（不写 CSV 时走这条路）。"""
    return _render_report(rows, out_path, layout=layout, base_dir=base_dir, all_batches=False)


# ===========================================================================
# 五之二、HTML → PDF（GUI「导出 PDF」按钮 / CLI --pdf）
#
# 三条设计原则：
#   1. 零新依赖——只用标准库 + 系统里已有的 Chromium 系浏览器；
#   2. 纯增强功能——失败只回传一句说明，绝不抛异常打断抓取主流程；
#   3. 跨平台——Windows（注册表 + 常见安装路径）、macOS（应用包）、
#      Linux（扫 PATH + 容器必需参数），一条降级链走到底。
#
# 为什么只认 Chromium 系：报告用了 flex + grid 布局，只有 Chromium 内核能
# 原样还原；Firefox 的无头模式至今没有实现打印到 PDF，做不了备选。
# ===========================================================================

BROWSER_ENV_VAR = "RANKER_BROWSER"

# Windows：先查注册表 App Paths（比猜安装目录可靠得多）
_WIN_APP_PATHS = ("chrome.exe", "msedge.exe", "brave.exe")
_WIN_RELATIVE = (
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome Beta\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Chromium\Application\chrome.exe",
    r"Vivaldi\Application\vivaldi.exe",
)
_WIN_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA")

# Linux：按可执行名扫 PATH
_LINUX_NAMES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi-stable",
)

# macOS：应用包里的真正可执行文件（不是 .app 目录本身）
_MAC_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
)

NO_BROWSER_HINT = (
    "没找到能导出 PDF 的浏览器（需要 Chrome / Edge / Chromium 等 Chromium 系）。\n"
    "可以手动指定：界面上填「浏览器」路径、设环境变量 %s、"
    "或命令行加 --pdf-browser <路径>。" % BROWSER_ENV_VAR
)

# ===========================================================================
# 清理：孤儿图片 / 旧报告 / 调试样本
#
# 原则是先看后删：scan_cleanup() 只统计、不碰任何文件；
# cleanup_workspace() 才真正删除。判定孤儿图片的依据是「有没有被现存的报告
# 引用」，所以正在用的封面和正文图绝不会被误删。
# ===========================================================================

def report_image_refs(report_dir_path):
    """扫描 reports/*.html，收集它们引用到的图片文件名。"""
    used = set()
    if not os.path.isdir(report_dir_path):
        return used
    for name in sorted(os.listdir(report_dir_path)):
        if not name.lower().endswith(".html"):
            continue
        try:
            with open(os.path.join(report_dir_path, name), encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in re.finditer(r'(?:\.\./)?images/([^"\'\s>]+)', text):
            used.add(os.path.basename(m.group(1)))
    return used


def scan_cleanup(workdir, keep_reports=5, include_debug=True):
    """
    看看能清掉些什么（只统计，不删任何东西）→ dict。

    - orphan_images：images/ 里没被任何现存报告引用的图片
    - old_reports ：超出 keep_reports 份的旧报告（html 连同同名 pdf）
    - debug_files ：debug/ 下的页面样本与缓存
    """
    workdir = os.path.abspath(workdir)
    rep_dir = os.path.join(workdir, "reports")
    img_dir = os.path.join(workdir, "images")

    result = {"workdir": workdir, "reports_dir": rep_dir, "images_dir": img_dir,
              "orphan_images": [], "old_reports": [], "debug_files": [],
              "kept_reports": [], "freed": 0, "counts": {}}

    # --- 孤儿图片 ---
    used = report_image_refs(rep_dir)
    if os.path.isdir(img_dir):
        for name in sorted(os.listdir(img_dir)):
            path = os.path.join(img_dir, name)
            if os.path.isfile(path) and name not in used:
                result["orphan_images"].append((path, os.path.getsize(path)))

    # --- 旧报告 ---
    # 文件名里就带抓取时间戳（report_站点_YYYY.MM.DD_HHMM），按名字排即按时间排
    names = []
    if os.path.isdir(rep_dir):
        for name in os.listdir(rep_dir):
            if name.startswith("report_") and name.lower().endswith((".html", ".pdf")):
                if os.path.isfile(os.path.join(rep_dir, name)):
                    names.append(name)
    stamps = []
    for name in sorted(names, reverse=True):
        base = os.path.splitext(name)[0]
        if base not in stamps:
            stamps.append(base)
    result["kept_reports"] = stamps[:max(0, int(keep_reports))]
    for base in stamps[max(0, int(keep_reports)):]:
        for path in (os.path.join(rep_dir, base + ".html"),
                     os.path.join(rep_dir, base + ".pdf")):
            if os.path.isfile(path):
                result["old_reports"].append((path, os.path.getsize(path)))

    # --- debug 样本 / 缓存 ---
    if include_debug:
        dbg = os.path.join(workdir, "debug")
        if os.path.isdir(dbg):
            for name in sorted(os.listdir(dbg)):
                path = os.path.join(dbg, name)
                if os.path.isfile(path):
                    result["debug_files"].append((path, os.path.getsize(path)))

    result["freed"] = sum(size for _p, size in
                          result["orphan_images"] + result["old_reports"]
                          + result["debug_files"])
    result["counts"] = {
        "orphan_images": len(result["orphan_images"]),
        "old_reports": len(result["old_reports"]),
        "debug_files": len(result["debug_files"]),
        "kept_reports": len(result["kept_reports"]),
    }
    return result


def cleanup_workspace(workdir, keep_reports=5, remove_orphan_images=True,
                      remove_old_reports=True, remove_debug=True, log=print):
    """
    按扫描结果实际删除，返回 (删掉的文件数, 释放的字节数)。

    顺序有讲究：先删旧报告，再算孤儿图片 —— 否则刚刚被删掉的那份报告
    引用过的图片会被当成「还有人用」而留下来，白清一遍。
    """
    removed = freed = 0

    def drop(items):
        nonlocal removed, freed
        for path, size in items:
            try:
                os.remove(path)
                removed += 1
                freed += size
            except OSError as exc:
                log("    ! 删不掉 %s：%s" % (os.path.basename(path), exc))

    if remove_old_reports:
        drop(scan_cleanup(workdir, keep_reports=keep_reports,
                          include_debug=False)["old_reports"])
    if remove_orphan_images:
        drop(scan_cleanup(workdir, keep_reports=keep_reports,
                          include_debug=False)["orphan_images"])
    if remove_debug:
        drop(scan_cleanup(workdir, keep_reports=keep_reports,
                          include_debug=True)["debug_files"])
    return removed, freed


def human_size(num_bytes):
    """把字节数写得好看一点。"""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % size
        size /= 1024
    return "%.1f GB" % size


_BROWSER_CACHE = {"list": None, "done": False}
_CJK_FONT_CACHE = {}


def _windows_browser_candidates():
    """Windows 上的候选浏览器：注册表 App Paths 优先，其次常见安装目录。"""
    found = []
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s"
        for exe in _WIN_APP_PATHS:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, key_path % exe) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                if path:
                    found.append(path)
    for root in _WIN_ROOTS:
        base = os.environ.get(root)
        if not base:
            continue
        found.extend(os.path.join(base, rel) for rel in _WIN_RELATIVE)
    return found


def find_browser(explicit=None, prefer=None):
    """
    找一个能驱动页面 / 打印 PDF 的浏览器，返回可执行文件路径；找不到返回 None。

    探测顺序：显式指定 → 环境变量 RANKER_BROWSER → 系统已装的 Chromium 系。
    prefer 可以指定偏好："edge" / "chrome" / "brave"（不指定就按默认顺序）。
    自动探测结果缓存在进程内，避免每次点击都重扫磁盘。
    """
    if explicit is None:
        explicit = os.environ.get(BROWSER_ENV_VAR)
    explicit = (explicit or "").strip().strip('"')
    if explicit:
        return explicit if os.path.exists(explicit) else None

    if not _BROWSER_CACHE["done"]:
        if os.name == "nt":
            candidates = _windows_browser_candidates()
        elif sys.platform == "darwin":
            candidates = list(_MAC_PATHS)
        else:
            candidates = [shutil.which(name) for name in _LINUX_NAMES]
            candidates += ["/usr/bin/google-chrome", "/usr/bin/chromium",
                           "/snap/bin/chromium", "/usr/bin/microsoft-edge"]
        seen, uniq = set(), []
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(path)
        _BROWSER_CACHE.update({"list": uniq, "done": True})

    found = list(_BROWSER_CACHE["list"] or [])
    if not found:
        return None
    if prefer:
        alias = {"edge": ("msedge",), "chrome": ("chrome",),
                 "brave": ("brave",), "chromium": ("chromium",)}
        names = alias.get(prefer.strip().lower(), (prefer.strip().lower(),))
        found.sort(key=lambda p: 0 if any(
            os.path.basename(p).lower().startswith(n) for n in names) else 1)
    return found[0]


def fetch_rendered(url, browser=None, wait_ms=8000, timeout=180,
                   save_html=None, log=print):
    """
    用无头浏览器打开页面，等 JS 执行完再取回渲染后的 HTML。

    目前抓取并不需要它：Ci-en 现在这版页面是服务端渲染的，带上年龄确认
    cookie 直接 HTTP 就能拿到全部内容（见 crawl_cien）。这个函数留着备用 ——
    万一哪天页面又变回「首屏没数据、必须等 JS 跑完」的形态。
    注意：输出走文件重定向而不是管道 —— 受限环境里管道会让浏览器起不来。
    """
    browser = browser or find_browser()
    if not browser:
        raise SystemExit(
            "这个功能需要 Chromium 系浏览器（Chrome/Edge）来打开页面。\n"
            + NO_BROWSER_HINT)

    import tempfile

    body = ""
    # 浏览器退出后可能还占着临时文件，清理失败不能影响抓取结果
    with tempfile.TemporaryDirectory(prefix="ranker_render_",
                                     ignore_cleanup_errors=True) as tmp:
        dom_path = os.path.join(tmp, "dom.html")
        err_path = os.path.join(tmp, "err.txt")
        profile = os.path.join(tmp, "profile")
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--user-data-dir=" + profile,
            "--virtual-time-budget=%d" % int(wait_ms),
            "--dump-dom",
            url,
        ]
        log("    （用 %s 渲染页面…）" % os.path.basename(browser))
        try:
            with open(dom_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
                subprocess.run(cmd, stdout=out_fh, stderr=err_fh, timeout=timeout)
        except subprocess.TimeoutExpired:
            log("    ! 浏览器渲染超时（%d 秒）" % timeout)
        except Exception as exc:                        # noqa: BLE001
            log("    ! 浏览器调用失败：%s: %s" % (type(exc).__name__, exc))

        if os.path.exists(dom_path):
            with open(dom_path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()

        if not body:
            tail = ""
            try:
                with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
                    tail = " ".join(fh.read().split())
            except Exception:                           # noqa: BLE001
                pass
            if tail:
                log("    ! 浏览器没返回内容：%s" % tail[-220:])
            # 失败原因落盘，便于事后排查
            try:
                err_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "debug", "render_last_error.log")
                os.makedirs(os.path.dirname(err_log), exist_ok=True)
                with open(err_log, "w", encoding="utf-8") as fh:
                    fh.write("time   : %s\nurl    : %s\nbrowser: %s\n\n%s\n"
                             % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                url, browser, tail or "(没有 stderr 输出)"))
                log("    失败详情已存 → debug/render_last_error.log")
            except Exception:                           # noqa: BLE001
                pass

    if save_html and body:
        os.makedirs(os.path.dirname(os.path.abspath(save_html)), exist_ok=True)
        with open(save_html, "w", encoding="utf-8") as fh:
            fh.write(body)
        log("    渲染结果已存 → %s" % save_html)

    return body


# ===========================================================================
# Ci-en 登录：浏览器代登录 + 官方调试接口取凭据 + DPAPI 加密落盘
#
# 为什么绕这一圈：Edge/Chrome 从 127 版起给 cookie 上了 App-Bound Encryption
# （Local State 里的 app_bound_encrypted_key），密钥由浏览器的提权服务托管并校验
# 调用方身份。想直接从磁盘把 cookie 解出来，等同于破解浏览器的安全机制 —— 本工具
# 不做这种事，也正因为不做，它在任何环节都没有「隐身」能力。
#
# 这里的做法完全不碰加密：唤起一个属于本工具的浏览器 profile，用户在**真实的浏览器
# 窗口**里自己登录（密码、验证码、两步验证全部由用户完成，永不经过本工具），
# 登录成功后通过 Chromium 官方的调试接口（CDP）把 cookie 取回来，再用 Windows
# DPAPI 加密保存 —— 只有当前 Windows 账户解得开。
# ===========================================================================

CIEN_CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "cien_login.dat")
CIEN_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".cien-profile")
CIEN_LOGIN_URL = CIEN_BASE + "/"
CIEN_LOGIN_HINT = ("登录窗口已打开：请在浏览器里完成 Ci-en 登录（注册、验证码、"
                   "两步验证都在那边操作），登录成功后窗口会自己关掉。")


def _load_websocket():
    """惰性加载 libs/ 下的 websocket-client —— 只有登录功能需要它。"""
    libs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
    if os.path.isdir(libs) and libs not in sys.path:
        sys.path.insert(0, libs)
    try:
        import websocket                                  # noqa: WPS433
        return websocket
    except ImportError:
        return None


def _dpapi(data: bytes, protect: bool = True) -> bytes:
    """Windows DPAPI：加解密只对当前登录的 Windows 账户有效。"""
    if os.name != "nt":
        raise OSError("DPAPI 只在 Windows 上可用")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data),
                        ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if protect
          else ctypes.windll.crypt32.CryptUnprotectData)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0,
              ctypes.byref(blob_out)):
        raise OSError("DPAPI 调用失败（error %d）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_cien_credential(cookie: str, account: str = "") -> None:
    """把登录 Cookie 加密后存下来（明文不落盘）。"""
    payload = json.dumps({
        "cookie": cookie,
        "account": account,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False).encode("utf-8")
    with open(CIEN_CRED_FILE, "wb") as fh:
        fh.write(_dpapi(payload, protect=True))


def load_cien_credential():
    """读回登录凭据 → (cookie 或 None, 信息字典或原因字符串)。"""
    if not os.path.exists(CIEN_CRED_FILE):
        return None, "尚未登录"
    try:
        with open(CIEN_CRED_FILE, "rb") as fh:
            blob = fh.read()
        info = json.loads(_dpapi(blob, protect=False).decode("utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return None, ("凭据读不出来（%s）—— 可能换了 Windows 账户，"
                      "或者文件损坏，重新登录一次即可" % type(exc).__name__)
    credential = (info.get("cookie") or "").strip()
    if not credential:
        return None, "凭据是空的"
    return credential, info


def clear_cien_credential() -> bool:
    """删掉保存的登录凭据。"""
    try:
        os.remove(CIEN_CRED_FILE)
        return True
    except OSError:
        return False


def cien_logout(remove_profile=True):
    """
    退出登录：清除保存的登录凭据。

    remove_profile=True 时把登录用的浏览器数据也一起删掉 —— 那边是浏览器
    自己的 profile（几十 MB，而且里面也有登录态），留着没有意义。
    """
    removed = clear_cien_credential()
    if remove_profile:
        shutil.rmtree(CIEN_PROFILE_DIR, ignore_errors=True)
    return removed


class CdpSession:
    """极简 CDP 客户端：只用到「求值」和「取 cookie」两个命令。"""

    def __init__(self, ws_url, timeout=90):
        websocket = _load_websocket()
        if websocket is None:
            raise RuntimeError("缺少 websocket-client")
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self.mid = 0
        self.timeout = timeout

    def cmd(self, method, **params):
        self.mid += 1
        mid = self.mid
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
        raise TimeoutError("CDP 命令超时：%s" % method)

    def ev(self, expression):
        res = self.cmd("Runtime.evaluate", expression=expression,
                       returnByValue=True, awaitPromise=True).get("result", {})
        return res.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:                                 # noqa: BLE001
            pass


def _cien_logged_in(cdp) -> bool:
    """页面上出现「マイページ」链接或「ログアウト」，即视为登录成功。"""
    return bool(cdp.ev(
        "!!document.querySelector('a[href*=\"mypage\"]')"
        " || document.body.innerText.includes('ログアウト')"))


def _trim_browser_profile(profile_dir):
    """
    登录结束后清掉浏览器的缓存目录，只留登录态本身。

    不清的话，一次登录就能在这个 profile 里堆上百 MB —— 而它平时根本用不到，
    只有用户点「重新登录」时才需要。Edge 尤其明显：光是「优化指南模型」
    和「实体抽取模型」两项就占了 60 MB，都跟登录毫无关系。

    只删缓存和浏览器自己下的模型，不碰 Cookies / 登录相关的东西。
    """
    if not os.path.isdir(profile_dir):
        return
    junk = (
        # 常规缓存
        "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
        "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker",
        "Application Cache", "Media Cache",
        # Edge / Chrome 自己下的模型与上报数据，跟登录无关，删了会自动重建
        "optimization_guide_model_store", "OptimizationGuideModelDownloading",
        "OptimizationHints", "Edge Entity Extraction", "BrowserMetrics",
        "EdgeLanguageDetectionModel", "Safe Browsing", "DualEngine",
        "AutofillAiModelCache", "component_crx_cache", "extensions_crx_cache",
    )
    for base in (os.path.join(profile_dir, "Default"), profile_dir):
        for name in junk:
            target = os.path.join(base, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
        # 带序号的历史缓存目录，例如 old_GrShaderCache_000
        try:
            for entry in os.listdir(base):
                if entry.startswith(("old_GrShaderCache_", "old_ShaderCache_")):
                    shutil.rmtree(os.path.join(base, entry), ignore_errors=True)
        except OSError:
            pass


def cien_login(browser=None, log=print, should_stop=None, login_timeout=420,
               browser_prefer="edge"):
    """
    唤起浏览器让用户自己登录 Ci-en，成功后取回凭据并加密保存。

    返回 (是否成功, 说明文字)。

    - 用工具专属 profile（.cien-profile），不碰用户日常浏览器的数据
    - 默认用 Edge（browser_prefer），装了别的也能换
    - 不使用无头模式：用户需要看到页面并手动操作
    - 密码 / 验证码 / 两步验证全程由用户在浏览器里完成，本工具不接触
    - 凭据由浏览器经 CDP 主动交出，不涉及解密浏览器自身的加密
    """
    if os.name != "nt":
        return False, "登录凭据加密依赖 Windows DPAPI，当前系统不支持"
    browser = browser or find_browser(prefer=browser_prefer)
    if not browser:
        return False, NO_BROWSER_HINT
    log("    使用的浏览器：%s" % browser)
    if _load_websocket() is None:
        return False, ("缺少 websocket-client，无法和浏览器通信。\n"
                       "装一下：python -m pip install websocket-client --target libs")

    os.makedirs(CIEN_PROFILE_DIR, exist_ok=True)
    port_file = os.path.join(CIEN_PROFILE_DIR, "DevToolsActivePort")
    try:
        os.remove(port_file)
    except OSError:
        pass

    log("    正在打开登录窗口…")
    cmd = [
        browser,
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--user-data-dir=" + CIEN_PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        # 这个 profile 只为登录一次而存在，别让它囤缓存
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--disable-application-cache",
        # 不用 --headless：需要用户可见、可交互
        CIEN_LOGIN_URL,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, "启动浏览器失败：%s" % exc

    port = None
    for _ in range(200):
        if os.path.exists(port_file):
            try:
                text = open(port_file, encoding="utf-8",
                            errors="replace").read().split()
            except OSError:
                text = []
            if text:
                port = text[0]
                break
        if proc.poll() is not None:
            return False, "浏览器启动后立刻退出（可能被安全软件拦了）"
        time.sleep(0.2)

    if not port:
        try:
            proc.kill()
        except Exception:                                 # noqa: BLE001
            pass
        return False, "等不到浏览器的调试端口，登录没有开始"

    cdp = None
    try:
        targets = json.load(urllib.request.urlopen(
            "http://127.0.0.1:%s/json/list" % port, timeout=20))
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            return False, "浏览器里没有可用页面"
        cdp = CdpSession(pages[0]["webSocketDebuggerUrl"])
        cdp.cmd("Network.enable")

        log("    %s" % CIEN_LOGIN_HINT)
        deadline = time.time() + login_timeout
        logged = False
        while time.time() < deadline:
            if should_stop and should_stop():
                return False, "已取消登录"
            if proc.poll() is not None:
                break
            try:
                if _cien_logged_in(cdp):
                    logged = True
                    break
            except Exception:                             # noqa: BLE001
                pass
            time.sleep(2)

        if not logged:
            return False, ("没检测到登录成功（超时，或窗口被提前关掉了）。\n"
                           "若用户确实已经登录，请再试一次，"
                           "并在登录成功后稍等两秒再关窗口。")

        cookies = cdp.cmd("Network.getAllCookies").get("cookies", [])
    except Exception as exc:                              # noqa: BLE001
        return False, "取凭据失败：%s: %s" % (type(exc).__name__, exc)
    finally:
        if cdp:
            cdp.close()
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:                                 # noqa: BLE001
            try:
                proc.kill()
            except Exception:                             # noqa: BLE001
                pass

    # 浏览器已经关了，把它的缓存清掉，别白占空间
    _trim_browser_profile(CIEN_PROFILE_DIR)

    # 只保留 Ci-en / DLsite 用得到的域，其它站点的 cookie 一概不要
    wanted = []
    for item in cookies:
        domain = (item.get("domain") or "").lstrip(".")
        if "ci-en" in domain or domain.endswith("dlsite.com"):
            wanted.append("%s=%s" % (item.get("name"), item.get("value")))
    if not wanted:
        return False, "没取到 Ci-en 的 Cookie，登录大概没有真正完成"

    try:
        save_cien_credential("; ".join(wanted))
    except Exception as exc:                              # noqa: BLE001
        return False, "凭据保存失败：%s: %s" % (type(exc).__name__, exc)

    # 拿这份凭据实际请求一次，确认它真的好使
    note = ""
    try:
        status, body, _ = fetch(CIEN_BASE + "/", cookie=cien_cookie(),
                                retries=1, timeout=30)
        if status == 200 and "ログアウト" not in body and "mypage" not in body:
            note = "（不过随后的验证请求没看到登录态，可能已经过期）"
    except Exception:                                     # noqa: BLE001
        pass                                              # 网络抖动不算登录失败

    return True, "登录成功，已加密保存 %d 个 cookie。%s" % (len(wanted), note)


def cien_login_status():
    """给 GUI 用：返回 (是否已登录, 状态文字)。"""
    credential, info = load_cien_credential()
    if not credential:
        return False, str(info)
    if isinstance(info, dict):
        return True, "已登录（保存于 %s）" % info.get("saved_at", "未知时间")
    return True, "已登录"


def _missing_cjk_fonts():
    """Linux 上判断系统有没有中文字体（缺了 PDF 里的中文会变成方块）。"""
    if os.name == "nt" or sys.platform == "darwin":
        return False
    if "cjk" in _CJK_FONT_CACHE:
        return _CJK_FONT_CACHE["cjk"]
    result = False
    fc_list = shutil.which("fc-list")
    if fc_list:
        try:
            proc = subprocess.run([fc_list, ":lang=zh", "-f", "%{family}\n"],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=10)
            result = not (proc.stdout or b"").strip()
        except Exception:                              # noqa: BLE001
            result = False
    _CJK_FONT_CACHE["cjk"] = result
    return result


def html_to_pdf(html_path, pdf_path=None, browser=None, timeout=120,
                wait_ms=8000, log=None):
    """把 HTML 报告导出成 PDF，返回 (是否成功, 说明文字, PDF 路径或 None)。

    这个函数不抛异常——失败原因写在说明文字里，调用方（GUI 线程 / CLI）
    记一下日志就行。导出 PDF 是增强功能，绝不能影响抓取和 HTML 报告。
    """
    say = log or (lambda message: None)
    try:
        html_path = os.path.abspath(html_path)
        if not os.path.exists(html_path):
            return False, "找不到 HTML 报告：%s" % html_path, None
        if not pdf_path:
            pdf_path = os.path.splitext(html_path)[0] + ".pdf"
        pdf_path = os.path.abspath(pdf_path)

        explicit = (browser or os.environ.get(BROWSER_ENV_VAR) or "").strip().strip('"')
        if explicit and not os.path.exists(explicit):
            return False, "指定的浏览器路径不存在：%s" % explicit, None
        exe = find_browser(browser)
        if not exe:
            return False, NO_BROWSER_HINT, None
        say("用于导出的浏览器：%s" % exe)

        # 目标文件被占用时先探测，避免白跑一趟
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                return False, ("目标 PDF 正被其他程序占用，请先关掉阅读器再试：%s"
                               % pdf_path), None

        # 用独立的临时 profile：既不会碰用户正在用的浏览器，也躲开单例冲突。
        # （不隔离的话，桌面浏览器开着时新进程会把命令转交给已有实例然后立刻
        #   退出，退出码还是 0，PDF 却根本没生成——最难查的一种失败。）
        profile = tempfile.mkdtemp(prefix="ranker_pdf_")
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--run-all-compositor-stages-before-draw",
            "--user-data-dir=" + profile,
            "--virtual-time-budget=%d" % int(wait_ms),
            "--print-to-pdf=" + pdf_path,
            # 新旧 Chromium 的参数名不一样，两个都传，不认识的会被忽略
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
        ]
        if os.name != "nt":
            # 容器里跑不了 Chromium 沙箱，而且 /dev/shm 默认只有 64MB
            cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
        cmd.append(pathlib.Path(html_path).as_uri())   # 跨平台，中文/空格自动转义

        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "转换超时（超过 %d 秒），已中止。" % timeout, None
        except OSError as exc:
            return False, "启动浏览器失败：%s" % exc, None
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        # 只看文件、不看退出码：Chromium 成功时也可能返回非 0，反过来返回 0
        # 也未必真写了文件，用文件本身判断才靠谱。
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            tail = "\n".join(detail.splitlines()[-4:]) if detail else "（浏览器没有输出）"
            return False, "浏览器没有生成 PDF。\n%s" % tail, None

        with open(pdf_path, "rb") as fh:
            if fh.read(4) != b"%PDF":
                return False, "生成的文件不是有效的 PDF：%s" % pdf_path, None

        note = ""
        if _missing_cjk_fonts():
            note = ("\n（提示：系统里没装中文字体，PDF 的中文可能显示成方块，"
                    "Linux 上装一个即可：apt install fonts-noto-cjk）")
        return True, "已导出 PDF：%s（%.1f KB）%s" % (
            pdf_path, os.path.getsize(pdf_path) / 1024.0, note), pdf_path
    except Exception as exc:                           # noqa: BLE001
        return False, "导出 PDF 时出错：%s: %s" % (type(exc).__name__, exc), None


# ===========================================================================
# 六、抓取流程（CLI 与 GUI 共用）
# ===========================================================================

def crawl_ranking(opener, period, category, limit, pages, cookie, sleep_base,
                  image_prefer="main", log=print, should_stop=None, sort=None):
    """抓一个「周期 + 分类」，返回条目列表（最多 limit 条）。"""
    collected, seen = [], set()
    for page in range(1, pages + 1):
        if should_stop and should_stop():
            log("      (已收到停止请求)")
            break
        url = build_ranking_url(period, category or None, page, sort=sort)
        log("    GET %s" % url)
        status, body, _ = fetch(url, opener=opener, cookie=cookie)
        blocked = looks_like_block(body)
        if status != 200 or blocked:
            log("      !! 异常：HTTP %s / %s —— 跳过这一页" % (status, blocked or "-"))
            break
        entries = extract_entries(body, image_prefer)
        new_count = 0
        for item in entries:
            if item["product_id"] in seen:
                continue
            seen.add(item["product_id"])
            collected.append(item)
            new_count += 1
        log("      解析 %d 条，新增 %d 条（累计 %d）" % (len(entries), new_count, len(collected)))
        if len(collected) >= limit or new_count == 0:
            break
        if page < pages:
            time.sleep(sleep_base + random.uniform(0, 2))
    return collected[:limit]


def crawl_cien(opener, period, category_id, limit, cookie, sleep_base,
               log=print, should_stop=None, max_pages=10, save_html=None,
               fetch_content=False, content_sleep=None, progress=None,
               download_images=False, image_dir=None):
    """
    抓 Ci-en 一个「周期 + 分类」的文章榜，返回条目（最多 limit 条）。

    每页 20 条；翻页直到够数或到最后一页。名次按抓到的顺序编。

    页面是 Nuxt 3 的 SSR，榜单数据落在 <script id="__NUXT_DATA__"> 里；
    但服务端要先确认年龄才肯把它渲染出来 —— 所以请求必须带上
    accepted_rating cookie（见 cien_cookie），否则拿回来的只是一张年龄门的空壳。
    """
    collected = []
    last_page = 1
    page = 1
    req_cookie = cien_cookie(cookie)
    if load_cien_credential()[0]:
        log("      （已带上登录凭据：账号有权访问的限定内容也能读到）")

    # 原样页面默认留一份在 debug/ 下，方便日后页面又改版时对结构
    if save_html is None:
        save_html = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "debug", "cien_last.html")

    while page <= last_page and page <= max_pages and len(collected) < limit:
        if should_stop and should_stop():
            log("      (已收到停止请求)")
            break
        url = build_cien_url(period, category_id, page)
        log("    GET %s" % url)

        status, body, note = fetch(url, opener=opener, cookie=req_cookie)
        if not body:
            log("      !! 拿不到页面内容")
            break
        if page == 1 and save_html:
            try:
                os.makedirs(os.path.dirname(save_html), exist_ok=True)
                with open(save_html, "w", encoding="utf-8") as fh:
                    fh.write(body)
            except OSError:
                pass

        entries, meta = extract_cien_entries(body)
        if not entries:
            if "18歳以上" in body and len(body) < 60000:
                log("      !! 被年龄确认拦住了（请求没带上 accepted_rating cookie）")
            else:
                log("      !! 页面里解析不到条目，结构可能又变了")
                log("         （原样页面已存到 debug/cien_last.html，可以据此校准规则）")
            break
        try:
            last_page = int(meta.get("last_page") or page)
        except (TypeError, ValueError):
            last_page = page

        log("      第 %d/%d 页，解析 %d 条（榜单共 %s 条）"
            % (page, last_page, len(entries), meta.get("total", "?")))
        collected.extend(entries)

        page += 1
        if page <= last_page and len(collected) < limit:
            time.sleep(sleep_base + random.uniform(0, 2))

    result = collected[:limit]
    for index, item in enumerate(result, start=1):
        item["rank"] = index

    # 逐篇抓正文（可选）：一篇一个请求，所以默认关闭
    if fetch_content and result:
        gap = content_sleep if content_sleep is not None else max(1.5, sleep_base)
        log("    正在抓正文（共 %d 篇，每篇一个请求，预计 %.1f 分钟）"
            % (len(result), len(result) * (gap + 0.5) / 60.0))
        ok = sealed = other = img_total = 0
        for idx, item in enumerate(result, start=1):
            if should_stop and should_stop():
                log("    (已收到停止请求，正文抓到 %d 篇)" % (ok + sealed + other))
                break
            if progress:
                progress(idx, len(result), "抓正文")
            item.setdefault("content", "")
            try:
                content, access = fetch_cien_content(item["url"], opener=opener,
                                                     cookie=cookie)
            except Exception as exc:                      # noqa: BLE001
                content = {"text": "", "chars": 0, "images": [], "state": "none"}
                access = {}
                log("      ! %s 正文抓取失败：%s" % (item.get("product_id"), exc))
            item["content"] = content["text"]
            item["content_chars"] = content["chars"]
            item["content_state"] = content["state"]
            item["content_images"] = content["images"]
            item["access_state"] = (access or {}).get("state") or ""
            item["content_files"] = []

            # 正文配图当场下载：URL 带 px-time / px-hash 签名，过后就失效了
            if download_images and image_dir and content["images"]:
                os.makedirs(image_dir, exist_ok=True)
                saved = []
                for n, img_url in enumerate(content["images"], start=1):
                    ext = ".jpg"
                    try:
                        cand = os.path.splitext(
                            urllib.parse.urlparse(img_url).path)[1].lower()
                        if cand in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            ext = cand
                    except Exception:                     # noqa: BLE001
                        pass
                    name = "%s_p%d%s" % (item.get("product_id") or "art", n, ext)
                    dest = os.path.join(image_dir, name)
                    if os.path.exists(dest) and os.path.getsize(dest) > 0:
                        saved.append(name)
                        continue
                    try:
                        ok_img, _size = download_image(
                            img_url, dest, opener=opener,
                            referer=CIEN_BASE + "/")
                        if ok_img:
                            saved.append(name)
                    except Exception as exc:              # noqa: BLE001
                        log("      ! %s 的第 %d 张正文图下载失败：%s"
                            % (item.get("product_id"), n, exc))
                    time.sleep(max(0.15, gap * 0.2) + random.uniform(0, 0.25))
                item["content_files"] = saved
                img_total += len(saved)
            item["need_follow"] = bool((access or {}).get("need_follow"))
            item["plans"] = (access or {}).get("plans") or []

            if content["chars"] > 0:
                ok += 1
            elif item["access_state"] in ("follow", "plan"):
                sealed += 1
            else:
                other += 1
            time.sleep(gap + random.uniform(0, 1.0))
        log("    正文：拿到 %d 篇，权限不足 %d 篇，其它 %d 篇；正文配图 %d 张"
            % (ok, sealed, other, img_total))

    return result


# ---------------------------------------------------------------------------
# Ci-en 可见范围检测：哪些文章要「关注」才能看
#
# 只做读取判断，不发送任何关注请求 —— 关注按钮指向 robots.txt 里明确
# Disallow 的 /mypage，本工具不碰那条路径。用户在浏览器里点一下，
# 之后抓取就自动能读到那些内容了。
#
# 页面上权限块的形态：
#   <div class="c-rewardBox is-sealed is-follow">  → 关注可见（免费）
#   <div class="c-rewardBox is-sealed">            → 需要付费プラン
#   没有 is-sealed 的                              → 公开
# ---------------------------------------------------------------------------

CIEN_ACCESS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "debug", "cien_access_cache.json")
CIEN_REWARD_RE = re.compile(
    r'<div class="c-rewardBox([^"]*)"[^>]*>\s*'
    r'<h3 class="c-rewardBox-heading">(.*?)</h3>', re.S)
CIEN_STATE_LABEL = {"public": "公开", "open": "已解锁", "follow": "需关注",
                    "plan": "需赞助", "unknown": "未知"}


def _clean_heading(text):
    """把权限块标题里的标签和多余空白去掉。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_cien_access_body(body, info=None):
    """从文章页 HTML 里解析可见范围（check / fetch 共用）。"""
    info = dict(info or {})
    info.setdefault("state", "unknown")
    info.setdefault("need_follow", False)
    info.setdefault("plans", [])
    info.setdefault("unlocked", False)
    info.setdefault("preview_chars", 0)
    if not body:
        return info

    # 公开预览的字数（未解锁时 articleBody 一般是空的，仅供参考）
    m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                  body, re.S)
    if m:
        try:
            payload = json.loads(m.group(1))
            article = payload[0] if isinstance(payload, list) and payload else {}
            raw = article.get("articleBody") or ""
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"&[a-z]+;", " ", text)
            info["preview_chars"] = len(re.sub(r"\s+", " ", text).strip())
        except Exception:                                 # noqa: BLE001
            pass

    for classes, heading in CIEN_REWARD_RE.findall(body):
        label = _clean_heading(heading)
        if "is-open" in classes:
            info["unlocked"] = True                       # 当前账号读得到
        elif "is-sealed" in classes:
            if "フォロワー" in label:
                info["need_follow"] = True
            elif "プラン以上限定" in label:
                info["plans"].append(label)

    if info["need_follow"]:
        info["state"] = "follow"
    elif info["plans"]:
        info["state"] = "plan"
    elif info["unlocked"]:
        info["state"] = "open"
    else:
        info["state"] = "public"
    return info


def check_cien_article_access(article_url, opener=None, cookie=None, timeout=30):
    """
    判断一篇文章对当前账号的可见范围 → dict。

    state：
      public  没有权限块（完全公开）
      open    有权限块，但当前账号已经解锁（正文就在页面里）
      follow  还有「フォロワー以上限定」没解锁 —— 关注即可看
      plan    只剩「プラン以上限定」没解锁 —— 需要赞助
    """
    info = {"state": "unknown", "need_follow": False, "plans": [],
            "unlocked": False, "preview_chars": 0, "status": None}
    try:
        status, body, _ = fetch(article_url, opener=opener, cookie=cien_cookie(cookie),
                                timeout=timeout, retries=2)
    except SystemExit:
        return info
    info = _parse_cien_access_body(body, info)
    info["status"] = status
    return info


# 正文块的边界：每个权限块都以 <div class="c-rewardBox 开头
CIEN_REWARD_SPLIT = re.compile(r'<div class="c-rewardBox')


def html_fragment_to_text(fragment):
    """
    把 Ci-en 正文的 HTML 片段转成保留段落的纯文本。

    <br> 当换行、块级标签当段落分隔、图片先占个位 —— 否则整段文字会糊成一坨。
    """
    text = fragment or ""
    text = re.sub(r"<img[^>]*?src=\"([^\"]+)\"[^>]*>", "\n[图片]\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|h[1-6]|div|li|figure|blockquote)\s*>", "\n\n", text,
                  flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_cien_content(body):
    """
    从文章页 HTML 里取出正文 → dict。

    正文有两个来源，取更长的那个：
      1. <div class="c-rewardBox is-open"> 里的 c-inner-rewardBox
         —— 限定内容解锁后走这里（站点不会把它塞进 ld+json）
      2. <script type="application/ld+json"> 的 articleBody
         —— 完全公开的文章走这里（站点把它喂给搜索引擎）

    返回 {"text", "chars", "images", "state"}
    state：ok（拿到正文）/ sealed（有内容但当前账号无权）/ none（没有正文块）
    """
    result = {"text": "", "chars": 0, "images": [], "state": "none"}
    if not body:
        return result

    opened, has_sealed = [], False
    for part in CIEN_REWARD_SPLIT.split(body)[1:]:
        head = part[:80].split(">", 1)[0]        # 只看标签上的 class
        if "is-sealed" in head:
            has_sealed = True
            continue
        if "is-open" not in head:
            continue
        inner = re.search(r'<div class="c-inner-rewardBox">(.*)', part, re.S)
        if inner:
            opened.append(inner.group(1))

    candidates = []
    if opened:
        candidates.append("\n\n".join(opened))

    m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                  body, re.S)
    if m:
        try:
            payload = json.loads(m.group(1))
            article = payload[0] if isinstance(payload, list) and payload else {}
            raw = article.get("articleBody") or ""
            if raw:
                candidates.append(raw)
        except Exception:                                 # noqa: BLE001
            pass

    if not candidates:
        result["state"] = "sealed" if has_sealed else "none"
        return result

    best, best_images = "", []
    for raw in candidates:
        text = html_fragment_to_text(raw)
        if len(text) > len(best):
            best = text
            # Ci-en 的正文图是 <vue-l-image src="..."> 自定义元素，不是 <img>
            best_images = []
            for url in re.findall(r'src="(https://media\.ci-en\.jp/[^"]+)"', raw, re.I):
                url = html_mod.unescape(url)
                if url not in best_images:
                    best_images.append(url)
    result["text"] = best
    result["chars"] = len(best)
    result["images"] = best_images
    if best:
        result["state"] = "ok"
    else:
        result["state"] = "sealed" if has_sealed else "none"
    return result


def fetch_cien_content(article_url, opener=None, cookie=None, timeout=30):
    """
    抓一篇文章页并取出正文 → (content_dict, access_dict)。

    一次请求同时拿到正文和可见范围，省得同一页抓两遍。
    """
    content = {"text": "", "chars": 0, "images": [], "state": "none"}
    access = {"state": "unknown", "need_follow": False, "plans": [],
              "unlocked": False, "status": None}
    try:
        status, body, _ = fetch(article_url, opener=opener, cookie=cien_cookie(cookie),
                                timeout=timeout, retries=2)
    except SystemExit:
        return content, access
    access = _parse_cien_access_body(body, access)
    access["status"] = status
    content = extract_cien_content(body)
    return content, access


def load_cien_access_cache():
    try:
        with open(CIEN_ACCESS_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                     # noqa: BLE001
        return {}


def save_cien_access_cache(cache):
    try:
        os.makedirs(os.path.dirname(CIEN_ACCESS_CACHE), exist_ok=True)
        with open(CIEN_ACCESS_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def scan_cien_follow_targets(entries, opener=None, cookie=None, sleep_base=1.5,
                             log=print, should_stop=None, max_check=60,
                             use_cache=True, progress=None):
    """
    逐篇检查可见范围，汇总出「要关注才能看」的作者。

    只发 GET，不发任何关注请求。结果写进 debug/cien_access_cache.json，
    同一篇不会反复请求 —— 用户关注之后重新扫一次，权限够了就会自动消失。

    返回 (targets, stats)：targets 是作者列表，stats 是统计信息。
    """
    cache = load_cien_access_cache() if use_cache else {}
    targets, fresh, checked = {}, 0, 0

    todo = [item for item in entries[:max_check] if item.get("url")]
    for index, item in enumerate(todo, start=1):
        if should_stop and should_stop():
            break
        key = item.get("product_id") or item["url"]
        info = cache.get(key)
        if not info:
            if progress:
                progress(index, len(todo), "检查可见范围")
            try:
                info = check_cien_article_access(item["url"], opener=opener,
                                                 cookie=cookie)
            except Exception as exc:                      # noqa: BLE001
                log("      ! %s 检查失败：%s" % (item.get("product_id"), exc))
                continue
            cache[key] = info
            fresh += 1
            time.sleep(sleep_base + random.uniform(0, 1.0))

        checked += 1
        creator_url = item.get("creator_url") or ""
        if not creator_url:
            continue
        state = info.get("state")
        if state in ("follow", "plan"):
            record = targets.setdefault(creator_url, {
                "creator": item.get("circle") or "",
                "creator_url": creator_url,
                "creator_icon": item.get("creator_icon") or "",
                "need_follow": False,
                "plans": [],
                "articles": [],
            })
            record["articles"].append(item.get("title") or item.get("product_id") or "")
            if state == "follow":
                record["need_follow"] = True
            for plan in info.get("plans") or []:
                if plan not in record["plans"]:
                    record["plans"].append(plan)

    if use_cache and fresh:
        save_cien_access_cache(cache)

    ordered = sorted(targets.values(),
                     key=lambda r: (not r["need_follow"], -len(r["articles"]),
                                    r["creator"]))
    stats = {"checked": checked, "fresh": fresh,
             "follow": sum(1 for r in ordered if r["need_follow"]),
             "plan": sum(1 for r in ordered if r["plans"]),
             "targets": len(ordered)}
    return ordered, stats


def normalize_ranks(entries):
    """名次校准：页面名次严格递增就用它，否则按抓取顺序编号。"""
    ranks = [e["rank"] for e in entries]
    ok = len(ranks) == len(set(ranks)) and all(ranks[i] < ranks[i + 1] for i in range(len(ranks) - 1))
    if not ok:
        for idx, item in enumerate(entries, start=1):
            item["rank"] = idx
    return entries


def print_diff(current, previous, log=print):
    """与上一次快照对比，打印新进榜／掉榜／升降。"""
    if not previous:
        log("    （没有历史快照，本次作为基线）")
        return
    cur = {item["product_id"]: item for item in current}
    new_in = [pid for pid in cur if pid not in previous]
    dropped = [pid for pid in previous if pid not in cur]
    moved = sorted(((pid, previous[pid] - cur[pid]["rank"])
                    for pid in cur if pid in previous and previous[pid] != cur[pid]["rank"]),
                   key=lambda pair: -abs(pair[1]))
    log("    新进榜 %d 个 / 掉榜 %d 个 / 名次变动 %d 个" % (len(new_in), len(dropped), len(moved)))
    for pid in new_in[:5]:
        log("      + %s  #%s %s" % (pid, cur[pid]["rank"], cur[pid]["title"][:40]))
    for pid in dropped[:5]:
        log("      - %s  (上次 #%s)" % (pid, previous[pid]))
    for pid, delta in moved[:5]:
        log("      %s %s %s%d → #%s %s" % ("↑" if delta > 0 else "↓", pid,
                                           "+" if delta > 0 else "", abs(delta),
                                           cur[pid]["rank"], cur[pid]["title"][:40]))


def run_scrape(*, site="dlsite", periods=("week",), categories=("game",), limit=50, pages=2,
               precise_rating=True, download_images=True, fetch_content=False,
               image_dir="images", image_size="main",
               out_csv="rank.csv", out_db=None,
               make_csv=False, make_html=True, html_layout="card",
               sort_mode="popular",
               proxy_mode="auto", proxy_url=None, cookie=None, sleep=3.0, compare=True,
               dry_run=False, workdir=None, log=None, progress=None,
               should_stop=None):
    """
    完整跑一次抓取。GUI 和 CLI 都用这个函数。

    site：dlsite = DLsite 作品榜（maniax）；cien = Ci-en 文章榜。

    默认行为（默认都开）：下载封面图 + 生成 HTML 报告；
    DLsite 另外逐个取精确评分（Ci-en 榜单没有评分字段，会自动跳过）。
    CSV 默认不生成 —— 它只是可选的额外输出，报告不依赖它（直接从内存渲染）。

    这些开关用于按需关闭：
        precise_rating   逐个打开详情页取精确评分（最耗时；对 Ci-en 无效）
        download_images  下载每个条目的一张封面图
        make_html        生成 HTML 报告
        make_csv         额外写出 CSV 文件（默认关）

    参数：
        log       callable(str)                —— 日志输出
        progress  callable(done, total, label) —— 进度回调（total 为 0 表示不确定）
    返回统计 dict。
    """
    log = log or (lambda message: print(message))
    progress = progress or (lambda done, total, label: None)

    if workdir:
        os.chdir(workdir)

    site = (site or "dlsite").lower()
    if site not in SITES:
        site = "dlsite"
    # 周期/分类的叫法两个站点不同，取当前站点这一套
    _periods, period_names, _categories, category_names = site_options(site)
    if site == "cien":
        precise_rating = False          # Ci-en 榜单没有评分可拿
        sort_mode = "popular"           # 文章榜没有「販売数順」这个开关

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    opener = build_opener(proxy_mode, proxy_url)
    previous = {} if (dry_run or not compare) else load_previous(out_csv)

    log("=" * 62)
    log("排行榜抓取 —— %s" % SITE_LABEL.get(site, site))
    log("时间     : %s" % fetched_at)
    log("周期     : %s" % " ".join(str(period_names.get(p, p)) for p in periods))
    log("分类     : %s" % " ".join(str(category_names.get(c, c)) for c in categories))
    log("每榜取   : 前 %d 名" % limit)
    if site == "dlsite":
        log("排序     : %s" % RANKING_SORT_LABEL.get(sort_mode, sort_mode))
    if site == "cien":
        log("封面图   : %s（Ci-en 无评分字段）" % ("是" if download_images else "否"))
    else:
        log("精确评分 : %s   封面图 : %s"
            % ("是" if precise_rating else "否", "是" if download_images else "否"))
    proxy_desc = PROXY_MODE_LABEL.get(proxy_mode, proxy_mode)
    if proxy_url and proxy_mode in ("http", "socks5"):
        proxy_desc += "  " + proxy_url
    log("代理     : %s" % proxy_desc)
    log("=" * 62)

    all_rows = []
    total_ratings = 0
    total_images = 0

    for period in periods:
        for category in categories:
            if should_stop and should_stop():
                log("已收到停止请求，提前结束。")
                break
            log("")
            log("[%s / %s]" % (period_names.get(period, period),
                               category_names.get(category, category)))
            progress(0, 0, "抓取榜单")

            if site == "cien":
                entries = crawl_cien(opener, period, category, limit, cookie, sleep,
                                     log=log, should_stop=should_stop,
                                     fetch_content=fetch_content, progress=progress,
                                     download_images=download_images and not dry_run,
                                     image_dir=image_dir)
            else:
                entries = crawl_ranking(opener, period, category, limit, pages,
                                        cookie, sleep, image_prefer=image_size, log=log,
                                        should_stop=should_stop, sort=sort_mode)
                entries = normalize_ranks(entries)
            if not entries:
                log("    !! 没抓到任何条目：页面结构可能变了，可用「检查页面」功能诊断")
                continue

            missing_title = sum(1 for e in entries if not e["title"])
            if missing_title:
                log("    !! 标题缺失 %d 条（解析规则需要校准）" % missing_title)

            # --- 精确评分 ---
            if precise_rating:
                log("    正在逐个取详情页精确评分（共 %d 条，预计 %.1f 分钟）"
                    % (len(entries), len(entries) * (sleep + 1) / 60.0))
                got = 0
                for idx, item in enumerate(entries, start=1):
                    if should_stop and should_stop():
                        log("    (已停止，评分取到 %d 条)" % got)
                        break
                    progress(idx, len(entries), "取精确评分")
                    try:
                        value, count = fetch_precise_rating(item["url"], opener=opener, cookie=cookie)
                    except Exception as exc:             # noqa: BLE001
                        value, count = "", ""
                        log("      ! %s 取评分失败：%s" % (item["product_id"], exc))
                    if value:
                        item["rating"] = value
                        item["rating_count"] = count or item["rating_count"]
                        got += 1
                    time.sleep(sleep + random.uniform(0, 1.5))
                total_ratings += got
                log("    精确评分取到 %d / %d 条" % (got, len(entries)))

            # --- 封面图 ---
            if download_images and not dry_run:
                img_dir = os.path.abspath(image_dir)
                os.makedirs(img_dir, exist_ok=True)
                log("    正在下载封面图（%s，共 %d 张）" % (image_size, len(entries)))
                got_img = failed = fell_back = 0
                for idx, item in enumerate(entries, start=1):
                    if should_stop and should_stop():
                        log("    (已停止，图片下到 %d 张)" % got_img)
                        break
                    progress(idx, len(entries), "下载封面图")
                    # 首选尺寸下不到就换另一个尺寸顶上（主图 → 缩略图）。
                    # 两个地址在解析列表页时就一起留下了，这里不额外请求页面。
                    candidates = [u for u in (item.get("image_url") or "",
                                              item.get("image_url_fallback") or "") if u]
                    if not candidates:
                        failed += 1
                        continue
                    ok = False
                    used_fallback = False
                    for pos, candidate in enumerate(candidates):
                        # 换了尺寸扩展名可能不一样，每轮重新算文件名
                        ext = os.path.splitext(candidate.split("?")[0])[1] or ".jpg"
                        fname = item["product_id"] + ext
                        dest = os.path.join(img_dir, fname)
                        try:
                            ok, _size = download_image(candidate, dest, opener=opener)
                        except Exception:                # noqa: BLE001
                            ok = False
                        if ok:
                            item["image_file"] = os.path.basename(img_dir) + "/" + fname
                            used_fallback = pos > 0
                            break
                        # 失败可能留下空文件，先清掉以免报告显示坏图
                        try:
                            if os.path.exists(dest) and os.path.getsize(dest) == 0:
                                os.remove(dest)
                        except OSError:
                            pass
                    if ok:
                        got_img += 1
                        if used_fallback:
                            fell_back += 1
                    else:
                        failed += 1
                    # 封面图是 CDN 上的静态资源，不必按页面请求的节奏等；
                    # 取 sleep 的 15% 打底（最少 0.15 秒），依旧串行、不并发。
                    time.sleep(max(0.15, sleep * 0.15) + random.uniform(0, 0.25))
                total_images += got_img
                if fell_back:
                    log("    封面图成功 %d / %d 张（其中 %d 张主图下不到，已改用缩略图；失败 %d）"
                        % (got_img, len(entries), fell_back, failed))
                else:
                    log("    封面图成功 %d / %d 张（失败 %d）" % (got_img, len(entries), failed))
            elif download_images and dry_run:
                log("    [dry-run] 跳过封面图下载（共 %d 张）" % len(entries))

            log("    预览：")
            for item in entries[:5]:
                if site == "cien":
                    log("      #%-3s %-10s 发布 %-11s 作者 %-16s %s"
                        % (item["rank"], item["product_id"], item["release_date"] or "-",
                           (item.get("circle") or "-")[:16], item["title"][:26]))
                else:
                    log("      #%-3s %-10s 发售 %-11s 评分 %-5s (%-6s人) 期间 %-7s 累计 %-8s %s"
                        % (item["rank"], item["product_id"], item["release_date"] or "-",
                           item["rating"] or "-", item["rating_count"] or "-",
                           item["sales_period"] or "-", item["sales_total"] or "-",
                           item["title"][:26]))
            if len(entries) > 5:
                log("      ...（共 %d 条）" % len(entries))

            # 历史快照只存在 CSV 里，不写 CSV 时没有可比对象，直接跳过
            if compare and make_csv:
                log("    与上次对比：")
                print_diff(entries, previous.get((period, category), {}), log=log)

            for item in entries:
                row = dict(item)
                row.update({"fetched_at": fetched_at, "site": site,
                            "period": period, "category": category,
                            "sort_mode": sort_mode})
                all_rows.append(row)
            time.sleep(sleep + random.uniform(0, 2))

    stats = {"rows": len(all_rows), "ratings": total_ratings, "images": total_images,
             "csv": None, "db": None, "html": None, "html_count": 0}

    if not all_rows:
        log("")
        log("[x] 没有抓到任何数据。")
        return stats

    if dry_run:
        log("")
        log("[dry-run] 共 %d 条，未写入任何文件。" % len(all_rows))
        stats["dry_run"] = True
        return stats

    progress(0, 0, "写入文件")
    if make_csv:
        write_csv(out_csv, all_rows)
        stats["csv"] = os.path.abspath(out_csv)
        log("")
        log("已追加 %d 条 → %s" % (len(all_rows), stats["csv"]))
    else:
        log("")
        log("已获取 %d 条（CSV 未生成，报告直接由本次数据渲染）" % len(all_rows))

    if out_db:
        write_sqlite(out_db, all_rows)
        stats["db"] = os.path.abspath(out_db)
        log("已写入 SQLite → %s" % stats["db"])

    if make_html:
        try:
            # 报告固定放在 CSV 同目录，避免受当前工作目录影响；文件名按站点区分
            base_dir = os.path.dirname(os.path.abspath(out_csv))
            html_target = report_path(base_dir, site)
            if make_csv:
                html_path, count = build_report(out_csv, html_target, layout=html_layout)
            else:
                html_path, count = build_report_from_rows(all_rows, html_target,
                                                          layout=html_layout, base_dir=base_dir)
            stats["html"] = html_path
            stats["html_count"] = count
            log("已生成带封面的 HTML 报告 → %s（%d 条）" % (html_path, count))
        except Exception as exc:                         # noqa: BLE001
            log("[!] 生成 HTML 报告失败：%s" % exc)

    return stats


def run_scrape_with_proxy(**kwargs) -> dict:
    """
    抓取入口：按需准备 SOCKS5 环境 → 调用 run_scrape → 无论如何都还原 socket。

    GUI 和 CLI 都走这个函数，别在别处直接调 run_scrape。
    """
    proxy_mode = (kwargs.get("proxy_mode") or "auto").lower()
    proxy_url = kwargs.get("proxy_url")
    log = kwargs.get("log") or print

    applied = False
    if proxy_mode == "socks5":
        ok, message = apply_socks(proxy_url)
        if not ok:
            log("[x] " + message)
            return {"rows": 0, "ratings": 0, "images": 0, "csv": None, "db": None,
                    "html": None, "html_count": 0, "error": message}
        applied = True

    try:
        return run_scrape(**kwargs)
    finally:
        if applied:
            restore_socket()


# ===========================================================================
# 七、命令行入口
# ===========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="DLsite 男性向 R18 排行榜抓取（标题／评分／销量／封面图）")
    parser.add_argument("--site", default="dlsite", choices=list(SITES),
                        help="站点：dlsite = DLsite 作品榜（默认）／cien = Ci-en 文章榜")
    parser.add_argument("--period", nargs="+", default=None,
                        help="榜单周期（DLsite：day/week/month/year；Ci-en：daily/weekly/monthly）")
    parser.add_argument("--category", nargs="+", default=None,
                        help="分类（DLsite：comic/game/voice；Ci-en：9=游戏 8=音声 2=漫画 1=插画 …）")
    parser.add_argument("--limit", type=int, default=50, help="每个榜取前 N 名（默认 50）")
    parser.add_argument("--pages", type=int, default=2, help="最多翻几页凑够 N 条（默认 2）")
    parser.add_argument("--no-precise-rating", action="store_true",
                        help="跳过精确评分（默认会逐个打开详情页取，这一步最耗时）")
    parser.add_argument("--no-images", action="store_true",
                        help="跳过封面图下载（默认会下载）")
    parser.add_argument("--no-html", action="store_true",
                        help="跳过 HTML 报告（默认会生成）")
    parser.add_argument("--csv", action="store_true",
                        help="额外写出 CSV 文件（默认只出 HTML 报告）")
    parser.add_argument("--image-dir", default="images", help="封面图保存目录（默认 images/）")
    parser.add_argument("--image-size", default="main", choices=["main", "thumb"],
                        help="main=主图（约 150KB）／thumb=缩略图（约 35KB）；"
                             "首选尺寸下不到时会自动改用另一种")
    parser.add_argument("--out", default="rank.csv",
                        help="CSV 路径（配合 --csv 使用，同时也决定报告输出目录）")
    parser.add_argument("--db", default=None, help="可选：SQLite 输出路径")
    parser.add_argument("--html-layout", default="card",
                        choices=["card", "list", "grid", "columns"],
                        help="报告布局：card 竖版卡片（默认）／list 单列／grid 网格／"
                             "columns 按榜单分列（日/周/月各占一列）")
    parser.add_argument("--sort", default="popular", choices=list(RANKING_SORTS),
                        help="榜单排序（仅 DLsite）：popular 人気順（默认）／sale 販売数順")
    parser.add_argument("--proxy", default=None,
                        help="HTTP 代理，如 http://127.0.0.1:7890")
    parser.add_argument("--socks5", default=None,
                        help="SOCKS5 代理，如 socks5://127.0.0.1:1080（需 pip install PySocks）")
    parser.add_argument("--no-proxy", action="store_true",
                        help="强制直连，忽略系统代理与环境变量")
    parser.add_argument("--proxy-mode", default=None, choices=list(PROXY_MODES),
                        help="直接指定代理模式（默认 auto）")
    parser.add_argument("--test-proxy", action="store_true",
                        help="只测试代理连通性然后退出")
    parser.add_argument("--cookie", default=None,
                        help="额外附带的 Cookie（Ci-en 的年龄确认已自动处理，一般不用给）")
    parser.add_argument("--cien-login", action="store_true",
                        help="打开浏览器登录 Ci-en，把凭据加密保存到本地，然后退出")
    parser.add_argument("--cien-logout", action="store_true",
                        help="清除已保存的 Ci-en 登录凭据，然后退出")
    parser.add_argument("--cien-status", action="store_true",
                        help="查看 Ci-en 登录状态，然后退出")
    parser.add_argument("--cien-content", action="store_true",
                        help="Ci-en：额外抓每篇文章的正文（一篇一个请求，会明显变慢）")
    parser.add_argument("--cien-browser", default="edge",
                        help="登录 Ci-en 用哪个浏览器：edge（默认）／chrome／brave，"
                             "也可以直接写完整路径")
    parser.add_argument("--cleanup", action="store_true",
                        help="扫描可清理的旧报告 / 孤儿图片 / 调试样本（默认只看不删）")
    parser.add_argument("--cleanup-yes", action="store_true",
                        help="配合 --cleanup：真正执行删除")
    parser.add_argument("--cleanup-dir", default=None,
                        help="要清理哪个目录（默认是工具所在目录）")
    parser.add_argument("--keep-reports", type=int, default=5,
                        help="清理时保留最近几份报告（默认 5）")
    parser.add_argument("--sleep", type=float, default=3.0, help="请求间隔基准秒（默认 3）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件、不下图")
    parser.add_argument("--only-report", action="store_true",
                        help="不抓取，只用现有 CSV 重新生成报告")
    parser.add_argument("--open-report", action="store_true", help="生成报告后用浏览器打开")
    parser.add_argument("--pdf", action="store_true",
                        help="额外把 HTML 报告导出成 PDF（需要系统里有 Chrome/Edge 等）")
    parser.add_argument("--pdf-browser", default=None,
                        help="导出 PDF 用的浏览器路径（默认自动探测，也可设环境变量 %s）"
                             % BROWSER_ENV_VAR)
    args = parser.parse_args()

    # --- Ci-en 登录相关：干完就退出，不进入抓取流程 ---
    if args.cien_status:
        logged, message = cien_login_status()
        print(("[√] " if logged else "[ ] ") + message)
        return 0
    if args.cien_logout:
        removed = cien_logout()
        print("[√] 已退出 Ci-en 登录，凭据与登录用的浏览器数据都已清理"
              if removed else
              "[i] 本来就没有登录过（登录用的浏览器数据已清理）")
        return 0
    if args.cien_login:
        print("=" * 62)
        print("Ci-en 登录（在弹出浏览器窗口里完成，本工具不接触密码）")
        print("=" * 62)
        # 允许直接给浏览器完整路径，否则当成名称（edge / chrome / …）
        pick = (args.cien_browser or "edge").strip().strip('"')
        browser_path = pick if os.path.exists(pick) else None
        ok, message = cien_login(log=print, browser=browser_path,
                                 browser_prefer=None if browser_path else pick)
        print()
        print(("[√] " if ok else "[x] ") + message)
        return 0 if ok else 1

    # --- 清理：先预览，加 --cleanup-yes 才真删 ---
    if args.cleanup:
        workdir = os.path.abspath(args.cleanup_dir
                                  or os.path.dirname(os.path.abspath(__file__)))
        print("=" * 62)
        print("清理预览 —— %s" % workdir)
        print("=" * 62)
        plan = scan_cleanup(workdir, keep_reports=args.keep_reports)
        counts = plan["counts"]
        print("  旧报告    : %d 个文件" % counts["old_reports"])
        print("  孤儿图片  : %d 张" % counts["orphan_images"])
        print("  调试样本  : %d 个文件" % counts["debug_files"])
        print("  可释放    : %s" % human_size(plan["freed"]))
        print("  保留报告  : 最近 %d 份" % counts["kept_reports"])
        for label, items in (("旧", plan["old_reports"]),
                             ("孤", plan["orphan_images"]),
                             ("调", plan["debug_files"])):
            for path, size in items[:12]:
                print("    [%s] %-44s %s"
                      % (label, os.path.basename(path), human_size(size)))
            if len(items) > 12:
                print("    [%s] …还有 %d 个" % (label, len(items) - 12))

        if not args.cleanup_yes:
            print()
            print("以上只是预览，一个都没删。确认要清就加 --cleanup-yes 再跑一次。")
            return 0

        removed, freed = cleanup_workspace(workdir,
                                           keep_reports=args.keep_reports,
                                           log=print)
        print()
        print("[√] 已删除 %d 个文件，释放 %s" % (removed, human_size(freed)))
        return 0

    # 代理模式优先级：--no-proxy > --socks5 > --proxy > --proxy-mode > 默认 auto
    if args.no_proxy:
        proxy_mode, proxy_url = "none", None
    elif args.socks5:
        proxy_mode, proxy_url = "socks5", args.socks5
    elif args.proxy:
        proxy_mode, proxy_url = "http", args.proxy
    else:
        proxy_mode, proxy_url = (args.proxy_mode or "auto"), None

    # 站点归一：两个站点的周期/分类取值不同，这里统一成当前站点的一套并做校验
    site = (args.site or "dlsite").lower()
    _sp, _spl, _sc, _scl = site_options(site)
    if site == "cien":
        periods = args.period or ["daily"]
        try:
            categories = [int(c) for c in (args.category or ["9"])]
        except (TypeError, ValueError):
            print("[x] Ci-en 的分类要写数字 ID，例如 --category 9 8")
            return 1
    else:
        periods = args.period or ["week"]
        categories = args.category or ["game"]

    bad_periods = [p for p in periods if p not in _sp]
    if bad_periods:
        print("[x] %s 不支持的周期：%s（可选：%s）"
              % (SITE_LABEL.get(site, site), ", ".join(map(str, bad_periods)),
                 ", ".join(map(str, _sp))))
        return 1
    bad_categories = [c for c in categories if c not in _sc]
    if bad_categories:
        print("[x] %s 不支持的分类：%s（可选：%s）"
              % (SITE_LABEL.get(site, site), ", ".join(map(str, bad_categories)),
                 ", ".join(map(str, _sc))))
        return 1

    if args.test_proxy:
        print("正在测试代理（%s）…" % PROXY_MODE_LABEL.get(proxy_mode, proxy_mode))
        result = test_proxy(proxy_mode, proxy_url)
        print("结果：%s" % result.get("message"))
        return 0 if result.get("ok") else 1

    if args.only_report:
        try:
            report_target = report_path(
                os.path.dirname(os.path.abspath(args.out)), site)
            path, count = build_report(args.out, report_target, layout=args.html_layout)
        except (FileNotFoundError, ValueError) as exc:
            print("[x] %s" % exc)
            return 1
        print("已生成报告 → %s（%d 条作品）" % (path, count))
        if args.open_report:
            webbrowser.open(pathlib.Path(path).as_uri())
        if args.pdf:
            ok, message, _pdf = html_to_pdf(path, browser=args.pdf_browser, log=print)
            print(message)
            if not ok:
                return 1
        return 0

    def on_progress(done, total, label):
        if total:
            sys.stdout.write("\r    [%s] %d/%d  " % (label, done, total))
            if done >= total:
                sys.stdout.write("\n")
            sys.stdout.flush()

    try:
        stats = run_scrape_with_proxy(
            site=site, periods=periods, categories=categories,
            limit=args.limit, pages=args.pages,
            precise_rating=not args.no_precise_rating,
            download_images=not args.no_images,
            fetch_content=args.cien_content,
            image_dir=args.image_dir, image_size=args.image_size,
            out_csv=args.out, out_db=args.db,
            make_csv=args.csv,
            make_html=not args.no_html, html_layout=args.html_layout,
            sort_mode=args.sort,
            proxy_mode=proxy_mode, proxy_url=proxy_url,
            cookie=args.cookie, sleep=args.sleep,
            dry_run=args.dry_run, progress=on_progress,
        )
    except SystemExit as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130

    if stats.get("html") and args.open_report:
        webbrowser.open(pathlib.Path(stats["html"]).as_uri())
    if args.pdf:
        if stats.get("html"):
            ok, message, _pdf = html_to_pdf(stats["html"], browser=args.pdf_browser, log=print)
            print(message)
            if not ok:
                return 1
        else:
            print("[!] 这次没有生成 HTML 报告，跳过 PDF 导出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
