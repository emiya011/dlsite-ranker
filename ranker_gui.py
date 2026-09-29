#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLsite 排行榜抓取工具 —— 图形界面（macOS 极简浅色风）

外观：customtkinter + 自定义配色，参照 macOS 应用的观感
    · 浅灰底 #f5f5f7 + 纯白卡片，几乎不用彩色
    · 细线分组、大量留白、克制字重
    · 只有关键处用系统蓝 #0071e3（主按钮／选中态／进度）
    · 左右两栏：左边参数、右边日志，一屏看完不用滚

依赖装在工具目录的 libs/ 下，不影响系统 Python 环境。

启动：python ranker_gui.py  或双击 run_gui.bat

合规提醒：
    只抓公开排行榜页面的元数据，串行低频、不并发、不绕任何防护。
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox

def _app_dir():
    """程序所在目录。

    打包成 exe 之后 __file__ 指向 PyInstaller 的临时解压目录（_MEIPASS），
    settings.json / images / reports 就会跑到临时目录里、关掉就没了。
    所以冻结运行时改用 exe 自己的位置，让这些文件都待在 exe 旁边。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
LIBS_DIR = os.path.join(APP_DIR, "libs")


def icon_search_roots():
    """
    图标可能待的两个地方。

    打包成单文件 exe 后，随包资源解压到 sys._MEIPASS；而 APP_DIR 指的是
    exe 所在目录（settings.json 那些得写在用户看得见的地方）。
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    roots.append(APP_DIR)
    return roots


def window_icon_path(ext=".ico"):
    """在可能的目录里找图标文件（icon.ico / icon.png）。"""
    for root in icon_search_roots():
        path = os.path.join(root, "icon" + ext)
        if os.path.exists(path):
            return path
    return None

for _path in (LIBS_DIR, APP_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

import ranker as core          # noqa: E402

# ===========================================================================
# 配色（照 macOS 取色）
# ===========================================================================

C = {
    "bg":        "#f5f5f7",   # 窗口底
    "panel":     "#ffffff",   # 卡片
    "line":      "#e5e5ea",   # 分隔线
    "field":     "#ffffff",   # 输入框底
    "fieldline": "#d2d2d7",   # 输入框描边
    "hover":     "#f0f0f2",   # 悬停底
    "text":      "#1d1d1f",   # 主文字
    "muted":     "#86868b",   # 次要文字
    "accent":    "#0071e3",   # 系统蓝
    "accent_hi": "#0077ed",
    "danger":    "#ff3b30",   # 系统红
    "ok":        "#34c759",   # 系统绿
}

# 字体：等线（Windows 自带）——字形比微软雅黑秀气，又带系统渲染优化
# 经验：微软雅黑/等线这类系统字体有 ClearType hinting，Tk 下小字号清晰得多；
#       Noto Sans SC 字形虽好看，但 Tk 渲染会发虚（DPI 100% 时也偏模糊）。
FONT_FAMILY = "等线"
MONO_FAMILY = "Consolas"

LAYOUT_CHOICES = (("竖版卡片", "card"), ("竖向单列", "list"), ("多列网格", "grid"),
                  ("榜单分列", "columns"))
PROXY_CHOICES = (("自动", "auto"), ("直连", "none"), ("HTTP", "http"), ("SOCKS5", "socks5"))

SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "version": 1,
    "proxy": {"mode": "auto", "url": ""},
    "pdf": {"enabled": False, "browser": ""},
    "ui": {
        "site": "dlsite",
        "periods": ["week"],
        "categories": ["game"],
        "limit": "50",
        "pages": "2",
        "sleep": "3.0",
        "dir": APP_DIR,
        "layout": "card",
        "download_images": True,
    },
}


def load_settings() -> dict:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for section in ("proxy", "ui", "pdf"):
                if isinstance(data.get(section), dict):
                    settings[section].update(data[section])
    except Exception:
        pass
    return settings


def save_settings(settings: dict) -> bool:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


class DlsiteRankGUI:
    def __init__(self, root):
        self.root = root
        root.title("DLsite 排行榜抓取")
        root.geometry("1120x740")
        root.minsize(980, 660)
        root.configure(fg_color=C["bg"])

        self.f_title = ctk.CTkFont(family=FONT_FAMILY, size=19)
        self.f_body = ctk.CTkFont(family=FONT_FAMILY, size=14)
        self.f_small = ctk.CTkFont(family=FONT_FAMILY, size=12)
        self.f_group = ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")
        self.f_btn = ctk.CTkFont(family=FONT_FAMILY, size=14)
        self.f_mono = ctk.CTkFont(family=FONT_FAMILY, size=13)

        self.queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.running = False
        self.testing_proxy = False
        self.last_report = None
        self.pdf_path = None
        self.pdf_running = False
        self._separators = []

        self._init_vars()
        self._set_window_icon()
        self._build()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ 变量
    def _init_vars(self):
        self.settings = load_settings()
        ui = self.settings.get("ui", {})
        proxy = self.settings.get("proxy", {})
        pdf = self.settings.get("pdf", {})

        # 站点：DLsite 作品榜 / Ci-en 文章榜（两边的周期与分类取值完全不同）
        self.site_var = tk.StringVar(value=ui.get("site") or "dlsite")
        site = self.site_var.get()
        if site not in core.SITES:
            site = "dlsite"
            self.site_var.set(site)
        site_periods, _spl, site_categories, _scl = core.site_options(site)

        # 旧配置里可能存着另一个站点的取值，过滤掉不认的，再保底勾一个
        if site == "cien":
            default_period, default_category = "daily", 9
        else:
            default_period, default_category = "week", "game"
        periods = [p for p in (ui.get("periods") or []) if p in site_periods] or [default_period]
        categories = [c for c in (ui.get("categories") or []) if c in site_categories] or [default_category]

        self.period_vars = {p: tk.BooleanVar(value=(p in periods)) for p in site_periods}
        self.category_vars = {c: tk.BooleanVar(value=(c in categories)) for c in site_categories}
        self.limit_var = tk.StringVar(value=str(ui.get("limit", "50")))
        self.pages_var = tk.StringVar(value=str(ui.get("pages", "2")))
        self.sleep_var = tk.StringVar(value=str(ui.get("sleep", "3.0")))
        self.dir_var = tk.StringVar(value=ui.get("dir") or APP_DIR)
        # 是否下载封面图（不勾就只抓文字信息，报告里用占位块）
        self.image_var = tk.BooleanVar(value=bool(ui.get("download_images", True)))
        # 是否抓 Ci-en 文章正文（一篇一个请求，会明显变慢）
        self.content_var = tk.BooleanVar(value=bool(ui.get("cien_content", False)))
        self.layout_label = tk.StringVar(
            value=next((label for label, code in LAYOUT_CHOICES
                        if code == ui.get("layout", "card")), LAYOUT_CHOICES[0][0]))
        # 榜单排序：人気順 / 販売数順（官网参数，只有 DLsite 有）
        self.sort_label = tk.StringVar(
            value=core.RANKING_SORT_LABEL.get(ui.get("sort", "popular"),
                                              core.RANKING_SORT_LABEL["popular"]))

        self.proxy_mode_var = tk.StringVar(value=proxy.get("mode", "auto"))
        self.proxy_url_var = tk.StringVar(value=proxy.get("url", ""))
        # Ci-en 登录状态提示（登录本身走真实浏览器窗口，见 cien_login_clicked）
        self.login_var = tk.StringVar(value="")
        self.proxy_status_var = tk.StringVar(value="")
        # 导出 PDF 用的浏览器路径：留空表示每次自动探测
        self.pdf_browser_var = tk.StringVar(value=pdf.get("browser", ""))
        # 勾上就表示「抓完自动导出一份 PDF」
        self.pdf_var = tk.BooleanVar(value=bool(pdf.get("enabled", False)))

        self.status_var = tk.StringVar(value="就绪")

    # ------------------------------------------------------- 小工具 / 组件
    def _sep(self, parent, pady=(14, 10)):
        """一条细分隔线（macOS 的分组手法）。"""
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(fill="x", pady=pady)
        line = ctk.CTkFrame(holder, height=1, fg_color=C["line"], corner_radius=0)
        line.pack(fill="x")
        return holder

    def _group_title(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=self.f_group,
                     text_color=C["muted"], anchor="w").pack(fill="x", padx=20, pady=(0, 8))

    def _row(self, parent, pady=5):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=pady)
        return row

    def _tag(self, parent, text, width=46):
        return ctk.CTkLabel(parent, text=text, width=width, anchor="w",
                            font=self.f_body, text_color=C["muted"])

    def _check(self, parent, text, variable, **kw):
        return ctk.CTkCheckBox(parent, text=text, variable=variable, font=self.f_body,
                               checkbox_width=18, checkbox_height=18, corner_radius=5,
                               border_width=2, border_color=C["fieldline"],
                               fg_color=C["accent"], hover_color=C["accent_hi"],
                               text_color=C["text"], text_color_disabled=C["muted"], **kw)

    def _radio(self, parent, text, variable, value):
        return ctk.CTkRadioButton(parent, text=text, variable=variable, value=value,
                                  font=self.f_body, radiobutton_width=18,
                                  radiobutton_height=18, border_width_checked=5,
                                  border_width_unchecked=2,
                                  border_color=C["fieldline"],
                                  fg_color=C["accent"], hover_color=C["accent_hi"],
                                  text_color=C["text"])

    def _field(self, parent, variable, width=110, **kw):
        return ctk.CTkEntry(parent, textvariable=variable, width=width, height=32,
                            font=self.f_body, corner_radius=8,
                            border_width=1, border_color=C["fieldline"],
                            fg_color=C["field"], text_color=C["text"], **kw)

    def _btn(self, parent, text, command, primary=False, width=118, height=36):
        """macOS 风格：主按钮纯蓝，次要按钮白底细描边。"""
        if primary:
            return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                                 font=self.f_btn, corner_radius=9, border_width=0,
                                 fg_color=C["accent"], hover_color=C["accent_hi"],
                                 text_color="#ffffff")
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                             font=self.f_btn, corner_radius=9,
                             border_width=1, border_color=C["fieldline"],
                             fg_color=C["panel"], hover_color=C["hover"],
                             text_color=C["text"])

    def _set_window_icon(self):
        """
        给窗口换上工具图标（标题栏、任务栏、Alt-Tab 都会跟着变）。

        两个 API 一起用是有原因的：
          iconphoto  —— 走 Tk 自己的图像系统，对 PNG 的 alpha 通道支持最好；
          iconbitmap —— Windows 原生接口，任务栏和 Alt-Tab 更认它。
        只用 iconbitmap 的话，某些 Tk 版本会把透明区域画成黑色。
        """
        png = window_icon_path(".png")
        if png:
            try:
                # 必须留着引用：PhotoImage 被回收后图标会跟着消失
                self._icon_image = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_image)
            except Exception:                             # noqa: BLE001
                pass
        ico = window_icon_path(".ico")
        if ico:
            try:
                # default= 让之后弹出的子窗口也自动沿用同一个图标
                self.root.iconbitmap(default=ico)
            except Exception:                             # noqa: BLE001
                pass

    def _build(self):
        # ---------------- 顶部标题 ----------------
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=26, pady=(18, 10))
        ctk.CTkLabel(header, text="排行榜抓取", font=self.f_title,
                     text_color=C["text"]).pack(side="left")
        self.status_label = ctk.CTkLabel(header, textvariable=self.status_var,
                                         font=self.f_small, text_color=C["muted"])
        self.status_label.pack(side="right")

        # ---------------- 主体：左右两栏 ----------------
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=26, pady=(0, 12))

        # ===== 左栏：参数（内容多，可滚轮滚动）=====
        left = ctk.CTkScrollableFrame(body, width=520, fg_color=C["panel"], corner_radius=14,
                                      border_width=1, border_color=C["line"],
                                      scrollbar_button_color="#e0e0e5",
                                      scrollbar_button_hover_color="#cfcfd6")
        left.pack(side="left", fill="y")

        tabs = ctk.CTkTabview(left, corner_radius=10, fg_color=C["panel"],
                              segmented_button_fg_color="#e9e9ee",
                              segmented_button_selected_color=C["panel"],
                              segmented_button_selected_hover_color="#fafafb",
                              segmented_button_unselected_color="#e9e9ee",
                              segmented_button_unselected_hover_color="#dedee3",
                              text_color=C["text"])
        tabs.pack(fill="both", expand=True, padx=12, pady=12)

        # ----- 抓取页 -----
        page = tabs.add("抓取")

        self._group_title(page, "站点")
        row = self._row(page)
        self.site_switch = ctk.CTkSegmentedButton(
            row, values=[core.SITE_LABEL[s] for s in core.SITES],
            font=self.f_body, height=34, corner_radius=8,
            selected_color=C["accent"], selected_hover_color=C["accent_hi"],
            unselected_color="#e9e9ee", unselected_hover_color="#dedee3",
            text_color=C["text"], command=self._on_site_change)
        self.site_switch.set(core.SITE_LABEL.get(self.site_var.get(), core.SITE_LABEL["dlsite"]))
        self.site_switch.pack(side="left")

        self._sep(page)
        self._group_title(page, "榜单")
        self.period_row = self._row(page)
        self._fill_period_row()

        row = self._row(page)
        self._tag(row, "排序", 46).pack(side="left")
        self.sort_menu = ctk.CTkOptionMenu(
            row, variable=self.sort_label,
            values=list(core.RANKING_SORT_LABEL.values()),
            width=150, height=32, font=self.f_body, corner_radius=8,
            fg_color=C["hover"], button_color="#e3e3e8",
            button_hover_color="#d6d6db", text_color=C["text"],
            dropdown_font=self.f_body)
        self.sort_menu.pack(side="left")

        self._sep(page)
        self._group_title(page, "分类")
        self.category_row = self._row(page, pady=2)
        self._fill_category_row()

        self._sep(page)
        self._group_title(page, "条数")
        row = self._row(page)
        self._tag(row, "每个榜取前", 78).pack(side="left")
        self._field(row, self.limit_var, 76).pack(side="left")
        ctk.CTkLabel(row, text="名", font=self.f_body,
                     text_color=C["muted"]).pack(side="left", padx=8)

        # ----- Ci-en 账号 / 关注：与抓取设置分开，各自一组 -----
        self._sep(page)
        self._group_title(page, "Ci-en 账号")
        row = self._row(page, pady=(2, 2))
        self.btn_login = self._btn(row, "登录 Ci-en", self.cien_login_clicked,
                                   width=118, height=36)
        self.btn_login.pack(side="left")
        self.login_state = ctk.CTkLabel(row, textvariable=self.login_var,
                                        font=self.f_small,
                                        text_color=C["muted"])
        self.login_state.pack(side="left", padx=(10, 0))
        self.btn_logout = self._btn(row, "退出登录", self.cien_logout_clicked,
                                    width=88, height=36)

        self._sep(page)
        self._group_title(page, "Ci-en 关注")
        row = self._row(page, pady=(2, 2))
        self._btn(row, "待关注作者", self.open_follow_window,
                  width=118, height=36).pack(side="left")
        ctk.CTkLabel(page, text="列出「关注一下就能读」的作者，可一键跳到作者页。",
                     font=self.f_small, text_color=C["muted"], anchor="w",
                     justify="left", wraplength=320).pack(fill="x", padx=20, pady=(2, 8))

        # ----- 高级设置页 -----
        page = tabs.add("高级")
        self._group_title(page, "抓取")
        row = self._row(page)
        self._tag(row, "最多翻", 60).pack(side="left")
        self._field(row, self.pages_var, 56).pack(side="left")
        ctk.CTkLabel(row, text="页", font=self.f_body,
                     text_color=C["muted"]).pack(side="left", padx=6)
        self._tag(row, "间隔", 44).pack(side="left", padx=(14, 0))
        self._field(row, self.sleep_var, 66).pack(side="left")
        ctk.CTkLabel(row, text="秒", font=self.f_body,
                     text_color=C["muted"]).pack(side="left", padx=6)

        self._sep(page)
        self._group_title(page, "输出")
        row = self._row(page)
        self._tag(row, "报告布局", 60).pack(side="left")
        ctk.CTkOptionMenu(row, variable=self.layout_label,
                          values=[label for label, _ in LAYOUT_CHOICES],
                          width=130, height=32, font=self.f_body, corner_radius=8,
                          fg_color=C["hover"], button_color="#e3e3e8",
                          button_hover_color="#d6d6db", text_color=C["text"],
                          dropdown_font=self.f_body).pack(side="left")

        row = self._row(page)
        self._tag(row, "封面图", 60).pack(side="left")
        self._check(row, "下载封面图", self.image_var).pack(side="left")
        self._check(row, "抓取正文（Ci-en）", self.content_var).pack(side="left", padx=(12, 0))
        self._check(row, "同时导出 PDF", self.pdf_var,
                    command=self._on_pdf_toggle).pack(side="left", padx=(10, 0))

        # 默认自动探测；某些环境下探测不准时可以手动填浏览器路径
        row = self._row(page, pady=(2, 4))
        self._tag(row, "浏览器", 60).pack(side="left")
        self.pdf_browser_entry = self._field(row, self.pdf_browser_var, 10,
                                             placeholder_text="留空 = 自动探测")
        self.pdf_browser_entry.pack(side="left", fill="x", expand=True)
        self._on_pdf_toggle()

        self._sep(page)
        self._group_title(page, "代理")
        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=(0, 6))
        grid.grid_columnconfigure(0, weight=1, uniform="proxy")
        grid.grid_columnconfigure(1, weight=1, uniform="proxy")
        for index, (label, value) in enumerate(PROXY_CHOICES):
            self._radio(grid, label, self.proxy_mode_var, value).grid(
                row=index // 2, column=index % 2, sticky="w", pady=5)

        row = self._row(page)
        self._tag(row, "地址", 46).pack(side="left")
        self.proxy_entry = self._field(row, self.proxy_url_var, 250)
        self.proxy_entry.pack(side="left")
        self.btn_test_proxy = self._btn(row, "测试", self.test_proxy, width=70, height=32)
        self.btn_test_proxy.pack(side="left", padx=8)

        row = self._row(page, pady=(0, 4))
        ctk.CTkLabel(row, textvariable=self.proxy_status_var, font=self.f_small,
                     text_color=C["muted"], anchor="w", wraplength=440).pack(fill="x")

        self._sep(page)
        self._group_title(page, "保存目录")
        row = self._row(page)
        self._field(row, self.dir_var, 10).pack(side="left", fill="x", expand=True)
        self._btn(row, "浏览…", self._pick_dir, width=70, height=32).pack(side="left", padx=8)

        # 这两个操作与「保存目录」分开，避免挤在一起
        self._sep(page)
        row = self._row(page, pady=(2, 12))
        self._btn(row, "重新生成报告", self.regen_report,
                  width=136, height=36).pack(side="left", padx=(0, 16))
        self._btn(row, "打开输出目录", self.open_output_dir,
                  width=136, height=36).pack(side="left")

        self._sep(page)
        self._group_title(page, "清理")
        row = self._row(page, pady=(2, 10))
        self._btn(row, "清理旧报告与图片", self.open_cleanup_window,
                  width=170, height=36).pack(side="left")

        # ===== 右栏：日志 =====
        right = ctk.CTkFrame(body, fg_color=C["panel"], corner_radius=14,
                             border_width=1, border_color=C["line"])
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(head, text="日志", font=self.f_group,
                     text_color=C["muted"]).pack(side="left")
        self._btn(head, "清空", self.clear_log, width=62, height=28).pack(side="right")

        ctk.CTkFrame(right, height=1, fg_color=C["line"], corner_radius=0).pack(fill="x")

        self.log = ctk.CTkTextbox(right, font=self.f_mono, wrap="word",
                                  fg_color=C["panel"], text_color=C["text"],
                                  corner_radius=0, border_width=0, scrollbar_button_color="#e0e0e5",
                                  scrollbar_button_hover_color="#cfcfd6")
        self.log.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        self.log.configure(state="disabled")
        self._setup_log_tags()

        # ---------------- 底部：按钮 + 进度 ----------------
        footer = ctk.CTkFrame(self.root, fg_color="transparent")
        footer.pack(fill="x", padx=26, pady=(0, 18))

        btnbar = ctk.CTkFrame(footer, fg_color="transparent")
        btnbar.pack(fill="x", pady=(0, 12))
        self.btn_start = ctk.CTkButton(btnbar, text="开始抓取", width=146, height=42,
                                       font=ctk.CTkFont(family=FONT_FAMILY, size=15),
                                       corner_radius=10, fg_color=C["accent"],
                                       hover_color=C["accent_hi"], text_color="#ffffff",
                                       command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ctk.CTkButton(btnbar, text="停止", width=98, height=42,
                                      font=ctk.CTkFont(family=FONT_FAMILY, size=15),
                                      corner_radius=10, border_width=1,
                                      border_color=C["fieldline"], fg_color=C["panel"],
                                      hover_color=C["hover"], text_color=C["text"],
                                      state="disabled", command=self.stop)
        self.btn_stop.pack(side="left", padx=10)
        self._btn(btnbar, "打开报告", self.open_report,
                  width=110, height=40).pack(side="left", padx=4)
        # 登录 / 待关注作者 移到左边「抓取」页，清理移到「高级」页，
        # 只保留抓取主流程，避免一行过挤。

        self.progress = ctk.CTkProgressBar(footer, height=6, corner_radius=3,
                                           progress_color=C["accent"],
                                           fg_color="#e5e5ea")
        self.progress.pack(fill="x")
        self.progress.set(0)

        self._on_proxy_mode_change()
        self._refresh_login_state()
        # 启动时若站点是 Ci-en，排序菜单要直接禁用
        if self.site_var.get() != "dlsite":
            try:
                self.sort_menu.configure(state="disabled")
            except AttributeError:
                pass

    # ------------------------------------------------------- 站点联动
    def _fill_period_row(self):
        for widget in self.period_row.winfo_children():
            widget.destroy()
        names = core.site_options(self.site_var.get())[1]
        for period in self.period_vars:
            self._check(self.period_row, names.get(period, str(period)),
                        self.period_vars[period]).pack(side="left", padx=(0, 12))

    def _fill_category_row(self):
        for widget in self.category_row.winfo_children():
            widget.destroy()
        names = core.site_options(self.site_var.get())[3]
        for cat in self.category_vars:
            self._check(self.category_row, names.get(cat, str(cat)),
                        self.category_vars[cat]).pack(side="left", padx=(0, 12))

    def _on_site_change(self, label=None):
        """切换站点。两边的周期/分类取值完全不同，所以重建选项并重置成该站点的默认值。"""
        label = label or self.site_switch.get()
        site = next((s for s in core.SITES if core.SITE_LABEL[s] == label), "dlsite")
        if site == self.site_var.get():
            return
        self.site_var.set(site)

        period_names = core.site_options(site)[1]
        category_names = core.site_options(site)[3]
        default_period = "daily" if site == "cien" else "week"
        default_category = 9 if site == "cien" else "game"

        self.period_vars = {p: tk.BooleanVar(value=(p == default_period)) for p in period_names}
        self.category_vars = {c: tk.BooleanVar(value=(c == default_category))
                              for c in category_names}
        self._fill_period_row()
        self._fill_category_row()
        # 「販売数順」是 DLsite 榜单独有的开关，Ci-en 那边禁掉
        is_dlsite = (site == "dlsite")
        try:
            self.sort_menu.configure(state="normal" if is_dlsite else "disabled")
        except AttributeError:              # 构建过程中控件还没建好
            pass
        if not is_dlsite:
            self.sort_label.set(core.RANKING_SORT_LABEL["popular"])
        self.append_log("已切换到 %s" % core.SITE_LABEL.get(site, site), "dim")

    def _setup_log_tags(self):
        """给日志上色；CTkTextbox 内部是 tk.Text，取法各版本不同，都兜一下。"""
        inner = getattr(self.log, "_textbox", self.log)
        for name, color in (("err", C["danger"]), ("ok", "#1a8f3c"),
                            ("dim", C["muted"]), ("accent", C["accent"])):
            try:
                inner.tag_config(name, foreground=color)
            except Exception:
                pass

    # ------------------------------------------------------------ 界面辅助
    def _pick_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or APP_DIR,
                                         title="选择保存目录")
        if chosen:
            self.dir_var.set(os.path.normpath(chosen))

    def append_log(self, text, tag=None):
        self.log.configure(state="normal")
        try:
            self.log.insert("end", text + "\n", tag or ())
        except Exception:
            self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        # 界面上标红的错误同时留一份到 logs/error.log，窗口关了也查得到
        if tag == "err":
            write_error_log("运行时错误", str(text))

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_status(self, text, color=None):
        self.status_var.set(text)
        self.status_label.configure(text_color=color or C["muted"])

    def set_progress(self, done, total, label):
        if total:
            try:
                self.progress.stop()
            except Exception:
                pass
            self.progress.configure(mode="determinate")
            self.progress.set(max(0.0, min(1.0, done / float(total))))
            self._set_status("%s  %d/%d" % (label, done, total), C["accent"])
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self._set_status(label or "处理中…", C["accent"])

    def _layout_code(self):
        label = self.layout_label.get()
        return next((code for text, code in LAYOUT_CHOICES if text == label), "card")

    def _sort_code(self):
        """排序下拉显示的是中文，这里映射回代码值。"""
        label = self.sort_label.get()
        return next((code for code, text in core.RANKING_SORT_LABEL.items() if text == label),
                    "popular")

    def _workdir(self):
        return self.dir_var.get().strip() or APP_DIR

    def _on_proxy_mode_change(self):
        mode = self.proxy_mode_var.get()
        need_url = mode in ("http", "socks5")
        self.proxy_entry.configure(state="normal" if need_url else "disabled")
        if mode == "socks5" and not core.socks_available():
            self.proxy_status_var.set("SOCKS5 需要 PySocks：pip install PySocks")
        elif mode == "socks5":
            self.proxy_status_var.set("已检测到 PySocks，例：socks5://127.0.0.1:1080")
        elif mode == "http":
            self.proxy_status_var.set("例：127.0.0.1:7890（可省略 http://）")
        elif mode == "none":
            self.proxy_status_var.set("忽略系统代理，强制直连")
        else:
            self.proxy_status_var.set("跟随系统代理／环境变量")

    def _collect_settings(self) -> dict:
        return {
            "version": 1,
            "proxy": {"mode": self.proxy_mode_var.get(),
                      "url": self.proxy_url_var.get().strip()},
            "pdf": {"enabled": self.pdf_var.get(),
                    "browser": self.pdf_browser_var.get().strip()},
            "ui": {
                "site": self.site_var.get(),
                "periods": [p for p in self.period_vars if self.period_vars[p].get()],
                "categories": [c for c in self.category_vars if self.category_vars[c].get()],
                "limit": self.limit_var.get(),
                "pages": self.pages_var.get(),
                "sleep": self.sleep_var.get(),
                "dir": self.dir_var.get().strip(),
                "layout": self._layout_code(),
                "sort": self._sort_code(),
                "download_images": self.image_var.get(),
                "cien_content": self.content_var.get(),
            },
        }

    # ------------------------------------------------------------ 代理测试
    def test_proxy(self):
        if self.testing_proxy:
            return
        mode = self.proxy_mode_var.get()
        url = self.proxy_url_var.get().strip()
        if mode in ("http", "socks5") and not url:
            messagebox.showwarning("缺少地址", "请填写代理地址，例如 127.0.0.1:7890")
            return
        self.testing_proxy = True
        self.btn_test_proxy.configure(state="disabled")
        self.proxy_status_var.set("正在测试…")
        threading.Thread(target=self._test_proxy_worker, args=(mode, url), daemon=True).start()

    def _test_proxy_worker(self, mode, url):
        try:
            result = core.test_proxy(mode, url)
        except Exception as exc:                        # noqa: BLE001
            result = {"ok": False, "mode": mode,
                      "message": "%s: %s" % (type(exc).__name__, exc)}
        self.queue.put(("proxy_test", result))

    def _show_proxy_result(self, result):
        self.testing_proxy = False
        self.btn_test_proxy.configure(state="normal")
        mode = result.get("mode", "")
        message = result.get("message", "")
        if result.get("ok"):
            self.proxy_status_var.set("✓ " + message)
            self.append_log("代理测试通过（%s）：%s" % (mode, message), "ok")
        else:
            self.proxy_status_var.set("✗ " + message[:52])
            self.append_log("代理测试失败（%s）：%s" % (mode, message), "err")

    # ---------------------------------------------------------------- 抓取
    def start(self):
        if self.running:
            return

        periods = [p for p in self.period_vars if self.period_vars[p].get()]
        categories = [c for c in self.category_vars if self.category_vars[c].get()]
        if not periods:
            messagebox.showwarning("缺少参数", "请至少勾选一个榜单周期。")
            return
        if not categories:
            messagebox.showwarning("缺少参数", "请至少勾选一个分类。")
            return
        try:
            limit = int(self.limit_var.get())
            pages = int(self.pages_var.get())
            sleep = float(self.sleep_var.get())
        except ValueError:
            messagebox.showwarning("参数错误", "条数 / 页数 / 请求间隔必须是数字。")
            return
        if limit < 1 or pages < 1 or sleep <= 0:
            messagebox.showwarning("参数错误", "条数、页数、间隔都要大于 0。")
            return

        workdir = self._workdir()
        if not os.path.isdir(workdir):
            messagebox.showwarning("目录不存在", "保存目录不存在：\n%s" % workdir)
            return

        save_settings(self._collect_settings())
        self.clear_log()
        self.stop_event.clear()
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal",
                                fg_color=C["danger"], hover_color="#e0342a",
                                text_color="#ffffff",
                                border_width=0)
        # 下面开始都算「已进入运行态」的操作：一出错必须把界面恢复回来，
        # 否则会卡在“运行中”——按钮禁用、没有任务、看着像死机。
        try:
            _sp, period_names, _sc, category_names = core.site_options(self.site_var.get())
            self.append_log("开始：%s × %s，每榜前 %d 名"
                            % ("/".join(str(period_names.get(p, p)) for p in periods),
                               "/".join(str(category_names.get(c, c)) for c in categories),
                               limit), "accent")
            # Tk 变量不能在工作线程里读，先把要用到的值取出来再传进去
            options = {
                "site": self.site_var.get(),
                "html_layout": self._layout_code(),
                "sort_mode": self._sort_code(),
                "download_images": self.image_var.get(),
                "cien_content": self.content_var.get(),
                "proxy_mode": self.proxy_mode_var.get(),
                "proxy_url": self.proxy_url_var.get().strip() or None,
            }
            self.worker = threading.Thread(
                target=self._run_worker,
                args=(periods, categories, limit, pages, sleep, workdir, options),
                daemon=True)
            self.worker.start()
        except Exception as exc:                        # noqa: BLE001
            self.running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled", fg_color=C["panel"],
                                    text_color=C["text"], border_width=1,
                                    border_color=C["fieldline"])
            messagebox.showerror("启动失败", "%s: %s" % (type(exc).__name__, exc))

    def _run_worker(self, periods, categories, limit, pages, sleep, workdir, options):
        emit = self.queue.put
        try:
            stats = core.run_scrape_with_proxy(
                site=options["site"],
                periods=periods, categories=categories, limit=limit, pages=pages,
                precise_rating=True,
                download_images=options["download_images"],
                fetch_content=options.get("cien_content", False),
                image_dir=os.path.join(workdir, "images"),
                image_size="main",          # 固定主图（缩略图选项已去掉；命令行仍可 --image-size）
                out_csv=os.path.join(workdir, "rank.csv"),
                make_csv=False,          # CSV 不生成，报告直接从内存渲染
                make_html=True,
                html_layout=options["html_layout"],
                sort_mode=options.get("sort_mode", "popular"),
                proxy_mode=options["proxy_mode"],
                proxy_url=options["proxy_url"],
                sleep=sleep,
                log=lambda message: emit(("log", message, None)),
                progress=lambda done, total, label: emit(("progress", done, total, label)),
                should_stop=self.stop_event.is_set,
            )
            emit(("done", stats, workdir))
        except SystemExit as exc:
            emit(("log", str(exc), "err"))
            emit(("done", None, workdir))
        except Exception as exc:                        # noqa: BLE001
            emit(("log", "[x] %s: %s" % (type(exc).__name__, exc), "err"))
            emit(("log", traceback.format_exc(), "dim"))
            emit(("done", None, workdir))

    def stop(self):
        if not self.running:
            return
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self.append_log("已请求停止，当前这次请求结束后会收尾退出…", "dim")

    # -------------------------------------------------------- Ci-en 登录
    def _refresh_login_state(self):
        """
        刷新登录状态：「登录 Ci-en」和「退出登录」同一时刻只出现一个。

        已登录时把登录按钮整个收起来 —— 留着它既占地方，又容易在想重新
        登录的时候误点成退出。两个按钮都插在状态文字前面，位置才不会乱跑。
        """
        try:
            logged, info = core.cien_login_status()
        except Exception:                                 # noqa: BLE001
            logged, info = False, "状态未知"
        self.btn_login.pack_forget()
        self.btn_logout.pack_forget()
        if logged:
            self.login_var.set("已登录")
            self.btn_logout.pack(side="left", padx=4, before=self.login_state)
        else:
            self.btn_login.configure(text="登录 Ci-en")
            self.login_var.set("" if not isinstance(info, str) else info)
            self.btn_login.pack(side="left", padx=4, before=self.login_state)

    def cien_logout_clicked(self):
        """退出登录：清除凭据（连登录用的浏览器数据一起删）。"""
        if self.running:
            self.append_log("正在忙，等当前任务结束再操作。", "dim")
            return
        if not messagebox.askyesno(
                "退出 Ci-en 登录",
                "确定要清除已保存的登录凭据吗？\n\n"
                "清除后需要重新登录，才能继续读取限定内容。\n"
                "（登录用的浏览器数据也会一并删除）",
                parent=self.root):
            return
        try:
            removed = core.cien_logout()
        except Exception as exc:                          # noqa: BLE001
            self.append_log("[x] 退出登录失败：%s: %s"
                            % (type(exc).__name__, exc), "err")
            return
        self.append_log("[√] 已退出 Ci-en 登录" if removed
                        else "[i] 本来就没有保存过凭据", "dim")
        self._set_status("Ci-en 未登录", C["muted"])
        self._refresh_login_state()

    def cien_login_clicked(self):
        """
        唤起浏览器让用户登录 Ci-en。

        登录窗口是真实的浏览器页面：账号密码、验证码、两步验证全部由用户在
        那边操作，本工具不接触密码；登录成功后只通过 Chromium 官方调试接口
        取回 cookie，再用 Windows DPAPI 加密保存。
        """
        if self.running:
            self.append_log("正在抓取中，等这次跑完再登录。", "dim")
            return
        self.running = True
        self.btn_start.configure(state="disabled")
        self.btn_login.configure(state="disabled", text="登录中…")
        self.stop_event.clear()
        self._set_status("等待浏览器登录…", C["accent"])
        self.set_progress(0, 0, "等待登录")
        threading.Thread(target=self._cien_login_worker, daemon=True).start()

    def _cien_login_worker(self):
        emit = self.queue.put
        try:
            ok, message = core.cien_login(
                log=lambda text: emit(("log", text, "dim")),
                should_stop=self.stop_event.is_set,
            )
        except Exception as exc:                          # noqa: BLE001
            ok, message = False, "%s: %s" % (type(exc).__name__, exc)
        emit(("login", ok, message))

    def _login_finished(self, ok, message):
        self.running = False
        self.btn_start.configure(state="normal")
        try:
            self.progress.stop()
        except Exception:                                 # noqa: BLE001
            pass
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.append_log(("[√] " if ok else "[x] ") + message, "ok" if ok else "err")
        self._set_status("Ci-en 登录成功" if ok else "Ci-en 未登录",
                         C["ok"] if ok else C["danger"])
        self._refresh_login_state()

    # ----------------------------------------------------- 待关注作者
    def open_follow_window(self):
        """
        列出「关注一下就能读」的作者。

        工具只负责找出来和跳转；关注按钮指向 robots.txt 明确 Disallow 的
        /mypage，所以最后那一下必须用户自己在浏览器里点。
        """
        if self.site_var.get() != "cien":
            self.append_log("「待关注作者」只对 Ci-en 有效，先切到 Ci-en 站点。", "dim")
            return
        win = getattr(self, "_follow_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        self._follow_win = win
        win.title("待关注作者 · Ci-en")
        win.geometry("680x580")

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 4))
        ctk.CTkLabel(head, text="待关注作者",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
                     text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=("这些作者的文章需要「关注」才能读（免费）。点右边的「打开」跳到作者主页，"
                  "在那里点一下「フォローする」就行。\n"
                  "关注是永久的，之后重新扫描这些条目就会自动消失。"),
            justify="left", wraplength=620,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=C["muted"]).pack(anchor="w", pady=(6, 0))

        self._follow_body = ctk.CTkScrollableFrame(win, fg_color=C["panel"],
                                                   corner_radius=12)
        self._follow_body.pack(fill="both", expand=True, padx=18, pady=10)

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(0, 16))
        self._follow_status = tk.StringVar(value="准备扫描…")
        ctk.CTkLabel(bar, textvariable=self._follow_status, anchor="w",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                     text_color=C["muted"]).pack(side="left", fill="x", expand=True)
        self.btn_follow_scan = self._btn(bar, "重新扫描", self.scan_follow_targets,
                                         width=100, height=34)
        self.btn_follow_scan.pack(side="right")

        self.scan_follow_targets()

    def scan_follow_targets(self):
        """后台扫描当前榜单，找出需要关注的作者。"""
        if getattr(self, "_follow_scanning", False):
            return
        self._follow_scanning = True
        self.btn_follow_scan.configure(state="disabled", text="扫描中…")
        self._follow_status.set("正在抓取榜单并逐篇检查…")
        for child in self._follow_body.winfo_children():
            child.destroy()

        periods = [p for p, var in self.period_vars.items() if var.get()] or ["daily"]
        categories = [c for c, var in self.category_vars.items() if var.get()] or [9]
        try:
            limit = max(1, int(self.limit_var.get() or "50"))
        except ValueError:
            limit = 50
        options = {"proxy_mode": self.proxy_mode_var.get(),
                   "proxy_url": self.proxy_url_var.get().strip() or None}
        threading.Thread(target=self._scan_follow_worker,
                         args=(periods[0], categories[0], min(limit, 60), options),
                         daemon=True).start()

    def _scan_follow_worker(self, period, category, limit, options):
        emit = self.queue.put
        try:
            opener = core.build_opener(options["proxy_mode"], options["proxy_url"])
            entries = core.crawl_cien(opener, period, category, limit, None, 1.0,
                                      log=lambda m: emit(("log", m, "dim")))
            targets, stats = core.scan_cien_follow_targets(
                entries, opener=opener, sleep_base=1.5, max_check=limit,
                log=lambda m: emit(("log", m, "dim")),
                progress=lambda d, t, l: emit(("progress", d, t, l)))
            emit(("follow_targets", targets, stats))
        except Exception as exc:                          # noqa: BLE001
            emit(("log", "[x] 扫描待关注作者失败：%s: %s"
                  % (type(exc).__name__, exc), "err"))
            emit(("follow_targets", [], {"error": str(exc)}))

    def _render_follow_targets(self, targets, stats):
        self._follow_scanning = False
        try:
            self.btn_follow_scan.configure(state="normal", text="重新扫描")
        except Exception:                                 # noqa: BLE001
            pass
        try:
            self.progress.stop()
        except Exception:                                 # noqa: BLE001
            pass
        self.progress.configure(mode="determinate")
        self.progress.set(0)

        # 窗口可能已经被用户关掉了
        body = getattr(self, "_follow_body", None)
        try:
            if body is None or not body.winfo_exists():
                return
        except Exception:                                 # noqa: BLE001
            return
        for child in body.winfo_children():
            child.destroy()

        if stats.get("error"):
            self._follow_status.set("扫描失败，详见日志")
            return

        need = [t for t in targets if t["need_follow"]]
        self._follow_status.set(
            "检查 %d 篇 · 待关注作者 %d 位 · 待赞助 %d 位"
            % (stats.get("checked", 0), len(need), len(targets) - len(need)))

        if not targets:
            ctk.CTkLabel(body, text="没有发现需要关注的作者，当前榜单内容都能直接读。",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=13),
                         text_color=C["muted"]).pack(pady=34)
            return

        for item in targets:
            row = ctk.CTkFrame(body, fg_color=C["hover"], corner_radius=10)
            row.pack(fill="x", pady=4, padx=4)
            flag = "需关注" if item["need_follow"] else "需赞助"
            ctk.CTkLabel(row, text=flag, width=54,
                         font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
                         text_color=C["accent"] if item["need_follow"] else C["muted"]
                         ).pack(side="left", padx=(12, 8), pady=10)
            text = "%s   ·   %d 篇" % (item["creator"], len(item["articles"]))
            ctk.CTkLabel(row, text=text, anchor="w",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=13),
                         text_color=C["text"]).pack(side="left", fill="x", expand=True)
            self._btn(row, "打开", lambda u=item["creator_url"]: webbrowser.open(u),
                      width=66, height=30).pack(side="right", padx=10)

    # ------------------------------------------------------------- 清理
    def open_cleanup_window(self):
        """
        清理旧报告 / 孤儿图片 / 调试样本。

        先扫描、再确认，绝不直接删。孤儿图片的判定依据是「有没有被现存报告
        引用」，所以正在用的封面和正文图不会被误删。
        """
        win = getattr(self, "_cleanup_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        self._cleanup_win = win
        win.title("清理")
        win.geometry("680x560")

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 4))
        ctk.CTkLabel(head, text="清理",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
                     text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=("扫描只统计、不落手；勾选后点「执行清理」才会真的删。\n"
                  "孤儿图片＝没有任何现存报告引用的图片，正在用的封面和正文图不会被删。"),
            justify="left", wraplength=620,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=C["muted"]).pack(anchor="w", pady=(6, 0))

        opt = ctk.CTkFrame(win, fg_color="transparent")
        opt.pack(fill="x", padx=18, pady=(10, 0))
        self._clean_orphans = tk.BooleanVar(value=True)
        self._clean_reports = tk.BooleanVar(value=True)
        self._clean_debug = tk.BooleanVar(value=False)
        self._check(opt, "孤儿图片", self._clean_orphans).pack(side="left")
        self._check(opt, "旧报告", self._clean_reports).pack(side="left", padx=(14, 0))
        self._check(opt, "调试样本", self._clean_debug).pack(side="left", padx=(14, 0))
        ctk.CTkLabel(opt, text="保留最近",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                     text_color=C["muted"]).pack(side="left", padx=(18, 4))
        self._keep_var = tk.StringVar(value="5")
        ctk.CTkEntry(opt, textvariable=self._keep_var, width=52, height=30,
                     corner_radius=8, border_width=1,
                     border_color=C["fieldline"], fg_color=C["panel"],
                     text_color=C["text"]).pack(side="left")
        ctk.CTkLabel(opt, text="份报告",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                     text_color=C["muted"]).pack(side="left", padx=(4, 0))
        self._btn(opt, "重新扫描", self.scan_cleanup_now,
                  width=90, height=30).pack(side="right")

        self._cleanup_body = ctk.CTkScrollableFrame(win, fg_color=C["panel"],
                                                    corner_radius=12)
        self._cleanup_body.pack(fill="both", expand=True, padx=18, pady=10)

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=18, pady=(0, 16))
        self._cleanup_status = tk.StringVar(value="正在扫描…")
        ctk.CTkLabel(bar, textvariable=self._cleanup_status, anchor="w",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                     text_color=C["muted"]).pack(side="left", fill="x", expand=True)
        self.btn_cleanup_go = self._btn(bar, "执行清理", self.run_cleanup,
                                        width=110, height=34)
        self.btn_cleanup_go.pack(side="right")

        self.scan_cleanup_now()

    def scan_cleanup_now(self):
        """扫描工作目录，列出可清理的内容（不删）。"""
        try:
            keep = max(0, int(self._keep_var.get() or "5"))
        except ValueError:
            keep = 5
        body = getattr(self, "_cleanup_body", None)
        try:
            if body is None or not body.winfo_exists():
                return
        except Exception:                                 # noqa: BLE001
            return

        try:
            plan = core.scan_cleanup(self._workdir(), keep_reports=keep,
                                     include_debug=True)
        except Exception as exc:                          # noqa: BLE001
            self._cleanup_status.set("扫描失败：%s" % exc)
            return
        self._cleanup_plan = plan

        for child in body.winfo_children():
            child.destroy()

        counts = plan["counts"]
        self._cleanup_status.set(
            "可释放 %s ｜ 旧报告 %d 个文件 · 孤儿图片 %d 张 · 调试样本 %d 个文件"
            % (core.human_size(plan["freed"]), counts["old_reports"],
               counts["orphan_images"], counts["debug_files"]))

        rows = ([("旧报告", p, s) for p, s in plan["old_reports"]]
                + [("孤儿图", p, s) for p, s in plan["orphan_images"]]
                + [("调试样本", p, s) for p, s in plan["debug_files"]])
        if not rows:
            ctk.CTkLabel(body, text="没有需要清理的东西，很干净。",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=13),
                         text_color=C["muted"]).pack(pady=34)
            return
        for label, path, size in rows[:300]:
            row = ctk.CTkFrame(body, fg_color=C["hover"], corner_radius=8)
            row.pack(fill="x", pady=2, padx=4)
            ctk.CTkLabel(row, text=label, width=68, anchor="w",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                         text_color=C["muted"]).pack(side="left", padx=(10, 4), pady=7)
            ctk.CTkLabel(row, text=os.path.basename(path), anchor="w",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                         text_color=C["text"]).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=core.human_size(size), width=78, anchor="e",
                         font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                         text_color=C["muted"]).pack(side="right", padx=10)

    def run_cleanup(self):
        """确认后执行清理。"""
        plan = getattr(self, "_cleanup_plan", None)
        if not plan or not plan["freed"]:
            self._cleanup_status.set("没有可清理的内容")
            return
        try:
            keep = max(0, int(self._keep_var.get() or "5"))
        except ValueError:
            keep = 5
        if not messagebox.askyesno(
                "确认清理",
                "将要删除：\n\n"
                "  旧报告　　%d 个文件（保留最近 %d 份）\n"
                "  孤儿图片　%d 张\n"
                "  调试样本　%d 个文件\n\n"
                "合计约 %s。\n\n删除后无法恢复，确定继续吗？"
                % (len(plan["old_reports"]), keep, len(plan["orphan_images"]),
                   len(plan["debug_files"]), core.human_size(plan["freed"])),
                parent=self.root):
            return
        try:
            removed, freed = core.cleanup_workspace(
                self._workdir(), keep_reports=keep,
                remove_orphan_images=self._clean_orphans.get(),
                remove_old_reports=self._clean_reports.get(),
                remove_debug=self._clean_debug.get(),
                log=lambda m: self.append_log(m, "dim"))
        except Exception as exc:                          # noqa: BLE001
            messagebox.showerror("清理失败", "%s: %s" % (type(exc).__name__, exc))
            return
        self.append_log("[√] 清理完成：删除 %d 个文件，释放 %s"
                        % (removed, core.human_size(freed)), "ok")
        self.scan_cleanup_now()

    # ------------------------------------------------------------ 队列轮询
    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.append_log(msg[1], msg[2])
                elif kind == "progress":
                    self.set_progress(msg[1], msg[2], msg[3])
                elif kind == "proxy_test":
                    self._show_proxy_result(msg[1])
                elif kind == "pdf":
                    self._pdf_done(msg[1], msg[2], msg[3])
                elif kind == "login":
                    self._login_finished(msg[1], msg[2])
                elif kind == "follow_targets":
                    self._render_follow_targets(msg[1], msg[2])
                elif kind == "done":
                    self._finish(msg[1], msg[2])
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _finish(self, stats, workdir):
        self.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled", fg_color=C["panel"],
                                text_color=C["text"], border_width=1,
                                border_color=C["fieldline"])
        try:
            self.progress.stop()
        except Exception:
            pass

        if stats and stats.get("rows"):
            self.progress.configure(mode="determinate")
            self.progress.set(1.0)
            self._set_status("完成：%d 条" % stats["rows"], C["ok"])
            self.append_log("完成：%d 条作品，精确评分 %d 条，封面 %d 张"
                            % (stats["rows"], stats.get("ratings", 0), stats.get("images", 0)), "ok")
            self.last_report = stats.get("html")
            if stats.get("html"):
                self.append_log("报告：%s" % stats["html"], "dim")
                if self.pdf_var.get():
                    self._start_pdf_export(stats["html"])
        else:
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self._set_status("已停止 / 失败", C["danger"])

    # ------------------------------------------------------------ 报告相关
    def _report_path(self):
        """当前站点的报告路径（reports/ 子目录，两个站点分开存互不覆盖）。"""
        return core.report_path(self._workdir(), self.site_var.get())

    def regen_report(self):
        workdir = self._workdir()
        csv_path = os.path.join(workdir, "rank.csv")
        try:
            path, count = core.build_report(csv_path, self._report_path(),
                                            layout=self._layout_code())
        except Exception as exc:                        # noqa: BLE001
            messagebox.showerror(
                "生成失败",
                "%s\n\n重新生成报告需要一份 CSV；直接点「开始抓取」即可出报告。" % exc)
            return
        self.last_report = path
        self.append_log("已重新生成报告 → %s（%d 条）" % (path, count), "ok")
        if self.pdf_var.get():
            self._start_pdf_export(path)
        self.open_report()

    def _latest_report(self):
        """报告按日期命名，打开的未必是今天那份——挑目录里最新的一份。"""
        folder = core.report_dir(self._workdir())
        try:
            names = [n for n in os.listdir(folder)
                     if n.startswith("report_") and n.endswith(".html")]
        except OSError:
            return None
        if not names:
            return None
        files = [os.path.join(folder, n) for n in names]
        site = self.site_var.get()
        mine = [f for f in files
                if os.path.basename(f).startswith("report_%s_" % site)]
        return max(mine or files, key=os.path.getmtime)

    def open_report(self):
        path = self.last_report
        if not path or not os.path.exists(path):
            path = self._latest_report()
        if not path:
            messagebox.showinfo("还没有报告",
                                "先抓一次数据。\n\n报告目录：\n%s"
                                % core.report_dir(self._workdir()))
            return
        webbrowser.open(pathlib.Path(path).as_uri())

    # ------------------------------------------------------------ 导出 PDF
    def _on_pdf_toggle(self):
        """只有勾选了「同时导出 PDF」，浏览器路径才有意义。"""
        state = "normal" if self.pdf_var.get() else "disabled"
        try:
            self.pdf_browser_entry.configure(state=state)
        except AttributeError:                          # 构建过程中还没建好
            pass

    def _start_pdf_export(self, html_path=None):
        """导出 PDF（丢给后台线程，界面不会卡）。

        勾选框打开时，抓取完成、重新生成报告之后都会自动走这里。
        自动流程不弹窗：没有报告就安静跳过，失败也只写日志。
        """
        if self.pdf_running:
            self.append_log("上一次 PDF 导出还没结束，这次先跳过。", "dim")
            return
        html_path = html_path or self.last_report or self._report_path()
        if not html_path or not os.path.exists(html_path):
            self.append_log("跳过导出 PDF：还没有 HTML 报告。", "dim")
            return
        self.pdf_running = True
        self.append_log("开始导出 PDF…", "dim")
        threading.Thread(target=self._pdf_worker,
                         args=(html_path, self.pdf_browser_var.get().strip()),
                         daemon=True).start()

    def _pdf_worker(self, html_path, browser):
        try:
            ok, message, path = core.html_to_pdf(html_path, browser=browser or None)
        except Exception as exc:                        # noqa: BLE001
            ok, message, path = False, "%s: %s" % (type(exc).__name__, exc), None
        self.queue.put(("pdf", ok, message, path))

    def _pdf_done(self, ok, message, path):
        self.pdf_running = False
        for line in str(message).splitlines():
            self.append_log(line, "ok" if ok else "err")
        if ok:
            self.pdf_path = path
            self.append_log("封面图已经嵌进 PDF，这个文件可以单独发给别人。", "dim")
        else:
            # 自动流程里不弹窗打断，日志里已经是红色的了
            self._set_status("PDF 导出失败（详见日志）", C["danger"])

    def open_output_dir(self):
        path = self._workdir()
        if not os.path.isdir(path):
            messagebox.showinfo("目录不存在", path)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)                      # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:                        # noqa: BLE001
            messagebox.showerror("打开失败", "%s" % exc)

    # ---------------------------------------------------------------- 关闭
    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("正在抓取", "抓取还在进行中，确定要退出吗？"):
                return
            self.stop_event.set()
        save_settings(self._collect_settings())
        self.root.destroy()


def _keep_std_streams():
    """窗口版 exe 没有控制台，sys.stdout / sys.stderr 是 None。

    这时任何一句 print 都会抛 AttributeError 把程序直接搞崩，而且看不到
    任何提示。所以冻结运行时把它们挂到一个日志文件上兜底。
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open(os.path.join(APP_DIR, "gui_stdout.log"),
                      "a", encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


# ------------------------------------------------------------------ 错误日志
LOG_DIR_NAME = "logs"
ERROR_LOG_NAME = "error.log"
LOG_MAX_BYTES = 512 * 1024          # 单个日志文件的上限，超了自动轮转


def error_log_path():
    """错误日志的完整路径：<程序目录>\\logs\\error.log"""
    return os.path.join(APP_DIR, LOG_DIR_NAME, ERROR_LOG_NAME)


def write_error_log(title, detail=""):
    """把一次错误追加进 logs/error.log，返回日志路径（写不进去就返回空串）。

    这主要是给窗口版 exe 准备的：它没有控制台，出错了什么都留不下。
    现在不管是界面回调炸了、还是后台线程抛了异常，都会在这儿留下带完整
    堆栈的记录，事后照着查就行。
    """
    path = error_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 超过上限就把旧日志挪成 error.log.1，别让它无限长大
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write("DLsite 排行榜抓取 —— 错误日志\n")
                fh.write("程序目录 : %s\n" % APP_DIR)
                fh.write("运行方式 : %s\n"
                         % ("打包 exe" if getattr(sys, "frozen", False) else "源码运行"))
                fh.write("Python   : %s\n" % sys.version.split()[0])
                fh.write("系统平台 : %s\n" % sys.platform)
                fh.write("=" * 70 + "\n")
            fh.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), title))
            if detail:
                fh.write(detail.rstrip() + "\n")
            fh.write("\n")
        return path
    except Exception:                                   # noqa: BLE001
        return ""


def _install_error_hooks(root):
    """把平时会「静默消失」的异常全都接到错误日志上。

    最要紧的是 Tkinter 的按钮回调：回调里一旦抛异常，它默认只往 stderr
    打一行，而窗口版 exe 压根没有控制台 —— 结果就是点了没反应、也不留
    任何痕迹。这里统一改成落盘 + 弹窗提示。
    """
    def on_callback_error(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        path = write_error_log("界面操作出错：%s" % exc_type.__name__, detail)
        try:
            messagebox.showerror(
                "操作出错了",
                "%s: %s\n\n详细信息已写入：\n%s"
                % (exc_type.__name__, exc_value, path or "(日志写入失败)"))
        except Exception:                               # noqa: BLE001
            pass

    root.report_callback_exception = on_callback_error

    def on_uncaught(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        write_error_log("未捕获异常：%s" % exc_type.__name__, detail)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = on_uncaught

    def on_thread_error(args):
        if args.exc_type is SystemExit:
            return
        detail = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        write_error_log("后台线程出错（%s）：%s"
                        % (getattr(args.thread, "name", "?"), args.exc_type.__name__), detail)

    threading.excepthook = on_thread_error


def main():
    """启动界面。缺依赖或启动崩溃时明确提示，不静默闪退。"""
    _keep_std_streams()
    if ctk is None:
        holder = tk.Tk()
        holder.withdraw()
        messagebox.showerror(
            "缺少 customtkinter",
            "没找到 customtkinter，界面无法启动。\n\n"
            "请在工具目录执行：\n"
            "python -m pip install customtkinter --target libs\n\n"
            "也可以改用命令行：python ranker.py --help")
        return 1

    error_log = os.path.join(APP_DIR, "gui_error.log")
    try:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
        _install_error_hooks(root)
        DlsiteRankGUI(root)
        root.mainloop()
        return 0
    except Exception:
        detail = traceback.format_exc()
        path = write_error_log("启动失败", detail)
        try:
            messagebox.showerror(
                "启动失败",
                "界面启动时出错，详情已写入：\n%s\n\n%s"
                % (path or error_log, detail[-900:]))
        except Exception:                               # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    sys.exit(main() or 0)
