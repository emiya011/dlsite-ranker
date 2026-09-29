#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 转长图工具（独立小工具）

把 PDF 的每一页渲染成图片，再纵向拼成一张长图。
适合把多页报告压成一张图发给聊天软件／存档。

依赖：pypdfium2（渲染 PDF）+ Pillow（拼图），都放在同目录的 libs/ 下。
启动：python pdf2img.py   或双击 run_pdf2img.bat
命令行：python pdf2img.py 文件.pdf [更多.pdf ...] [-o 输出目录] [--dpi 150]

说明：
    · 默认 JPEG（长图体积小），可选 PNG（无损但很大）
    · 页数多时若总高度过大，会自动下调 DPI 并在日志里说明
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LIBS_DIR = os.path.join(APP_DIR, "libs")

for _path in (LIBS_DIR, APP_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None        # 长图很容易超过 Pillow 的默认像素上限
except ImportError:
    Image = None

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

# ===========================================================================
# 外观（与 ranker_gui.py 保持一致）
# ===========================================================================

C = {
    "bg":        "#f5f5f7",
    "panel":     "#ffffff",
    "line":      "#e5e5ea",
    "field":     "#ffffff",
    "fieldline": "#d2d2d7",
    "hover":     "#f0f0f2",
    "text":      "#1d1d1f",
    "muted":     "#86868b",
    "accent":    "#0071e3",
    "accent_hi": "#0077ed",
    "danger":    "#ff3b30",
    "ok":        "#34c759",
}

FONT_FAMILY = "等线"

MAX_HEIGHT = 50000          # 长图的像素高度上限，超了就自动降 DPI
SUFFIX = "_长图"

# ===========================================================================
# 核心：渲染 + 拼接
# ===========================================================================

def plan_pages(pdf_path, dpi):
    """
    预估渲染后的总尺寸，必要时把 DPI 降下来。

    返回 (实际使用的 dpi, 页数, 预估宽度, 预估高度, 是否降过 dpi)
    """
    doc = pdfium.PdfDocument(pdf_path)
    try:
        count = len(doc)
        width_pt, height_pt = doc[0].get_size()
    finally:
        doc.close()

    scale = dpi / 72.0
    width_px = int(round(width_pt * scale))
    height_px = int(round(height_pt * scale)) * max(1, count)

    if height_px > MAX_HEIGHT:
        safe_dpi = max(40, int(dpi * MAX_HEIGHT / height_px))
        scale = safe_dpi / 72.0
        width_px = int(round(width_pt * scale))
        height_px = int(round(height_pt * scale)) * max(1, count)
        return safe_dpi, count, width_px, height_px, True
    return dpi, count, width_px, height_px, False


def render_pages(pdf_path, dpi, log=print, progress=None, should_stop=None):
    """把 PDF 每一页渲染成 PIL Image，返回列表。"""
    doc = pdfium.PdfDocument(pdf_path)
    try:
        total = len(doc)
        images = []
        for index in range(total):
            if should_stop and should_stop():
                log("    (已停止)")
                break
            page = doc[index]
            try:
                bitmap = page.render(scale=dpi / 72.0)
                images.append(bitmap.to_pil().convert("RGB"))
            finally:
                page.close()
            if progress:
                progress(index + 1, total, "渲染页面")
            log("    第 %d/%d 页 → %d×%d"
                % (index + 1, total, images[-1].width, images[-1].height))
        return images
    finally:
        doc.close()


def merge_vertical(images, gap=0, bg="#ffffff"):
    """把多张图纵向拼成一张长图。"""
    if not images:
        raise ValueError("没有可拼接的页面")
    width = max(im.width for im in images)
    height = sum(im.height for im in images) + gap * max(0, len(images) - 1)
    canvas = Image.new("RGB", (width, height), bg)
    y = 0
    for im in images:
        canvas.paste(im, (0, y))
        y += im.height + gap
    return canvas


def convert_pdf(pdf_path, out_dir=None, dpi=150, fmt="JPEG", quality=90, gap=0,
                log=print, progress=None, should_stop=None):
    """
    单个 PDF → 一张长图。

    返回 dict(ok, out_path, size, pages, dpi, message)
    """
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(pdf_path))
    os.makedirs(out_dir, exist_ok=True)
    ext = ".png" if fmt.upper() == "PNG" else ".jpg"
    out_path = os.path.join(out_dir, name + SUFFIX + ext)

    log("  %s" % os.path.basename(pdf_path))
    use_dpi, pages, est_w, est_h, shrunk = plan_pages(pdf_path, dpi)
    log("    %d 页，按 %d DPI 渲染 → 预计 %d×%d" % (pages, use_dpi, est_w, est_h))
    if shrunk:
        log("    ! 高度超过 %d 像素，DPI 已自动从 %d 降到 %d" % (MAX_HEIGHT, dpi, use_dpi))

    images = render_pages(pdf_path, use_dpi, log=log, progress=progress,
                          should_stop=should_stop)
    if not images:
        return {"ok": False, "out_path": None, "message": "没有渲染出任何页面"}

    if progress:
        progress(0, 0, "拼接长图")
    canvas = merge_vertical(images, gap=gap)
    log("    拼接完成 → %d×%d" % (canvas.width, canvas.height))

    if progress:
        progress(0, 0, "保存图片")
    final_size = (canvas.width, canvas.height)
    if ext == ".png":
        canvas.save(out_path, "PNG", optimize=True)
    else:
        canvas.save(out_path, "JPEG", quality=quality, optimize=True,
                    progressive=True, subsampling=1)
    canvas.close()
    for im in images:
        im.close()

    size_mb = os.path.getsize(out_path) / 1024.0 / 1024.0
    log("    已保存 → %s（%.1f MB）" % (out_path, size_mb))
    return {"ok": True, "out_path": out_path, "size": final_size,
            "pages": pages, "dpi": use_dpi, "message": "完成"}


# ===========================================================================
# 图形界面
# ===========================================================================

class Pdf2ImgGUI:
    def __init__(self, root):
        self.root = root
        root.title("PDF 转长图")
        root.geometry("900x660")
        root.minsize(780, 560)
        root.configure(fg_color=C["bg"])

        self.f_title = ctk.CTkFont(family=FONT_FAMILY, size=19)
        self.f_body = ctk.CTkFont(family=FONT_FAMILY, size=14)
        self.f_small = ctk.CTkFont(family=FONT_FAMILY, size=12)
        self.f_group = ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")

        self.queue = queue.Queue()
        self.files = []
        self.worker = None
        self.stop_event = threading.Event()
        self.running = False

        self.dpi_var = tk.StringVar(value="150")
        self.fmt_var = tk.StringVar(value="JPEG")
        self.out_dir_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="就绪")

        self._build()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 构件
    def _build(self):
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(16, 8))
        ctk.CTkLabel(header, text="PDF 转长图", font=self.f_title,
                     text_color=C["text"]).pack(side="left")
        self.status_label = ctk.CTkLabel(header, textvariable=self.status_var,
                                         font=self.f_small, text_color=C["muted"])
        self.status_label.pack(side="right")

        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(0, 16))

        # ---------- 左：文件与设置 ----------
        left = ctk.CTkFrame(body, width=430, fg_color=C["panel"], corner_radius=14,
                            border_width=1, border_color=C["line"])
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ctk.CTkLabel(left, text="PDF 文件", font=self.f_group, text_color=C["muted"],
                     anchor="w").pack(fill="x", padx=20, pady=(16, 6))
        self.listbox = tk.Listbox(left, activestyle="none", selectmode="extended",
                                  bg=C["field"], fg=C["text"], relief="flat",
                                  highlightthickness=1, highlightbackground=C["fieldline"],
                                  font=(FONT_FAMILY, 10), selectbackground=C["accent"],
                                  selectforeground="#ffffff", borderwidth=0)
        self.listbox.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 4))
        self._btn(row, "添加 PDF…", self.add_files, width=110, height=34).pack(side="left")
        self._btn(row, "移除选中", self.remove_selected, width=110, height=34).pack(side="left", padx=8)
        self._btn(row, "清空", self.clear_files, width=76, height=34).pack(side="left")

        ctk.CTkFrame(left, height=1, fg_color=C["line"]).pack(fill="x", padx=20, pady=14)

        ctk.CTkLabel(left, text="输出设置", font=self.f_group, text_color=C["muted"],
                     anchor="w").pack(fill="x", padx=20, pady=(0, 6))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row, text="清晰度", width=64, anchor="w", font=self.f_body,
                     text_color=C["muted"]).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.dpi_var, width=70, height=32,
                     font=self.f_body, corner_radius=8, border_width=1,
                     border_color=C["fieldline"], fg_color=C["field"]).pack(side="left")
        ctk.CTkLabel(row, text="DPI（150 够看，300 更清晰但更大）", font=self.f_small,
                     text_color=C["muted"]).pack(side="left", padx=8)

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row, text="格式", width=64, anchor="w", font=self.f_body,
                     text_color=C["muted"]).pack(side="left")
        for label, value in (("JPEG（体积小）", "JPEG"), ("PNG（无损）", "PNG")):
            ctk.CTkRadioButton(row, text=label, variable=self.fmt_var, value=value,
                               font=self.f_body, radiobutton_width=18,
                               radiobutton_height=18, border_width_checked=5,
                               border_width_unchecked=2, border_color=C["fieldline"],
                               fg_color=C["accent"], hover_color=C["accent_hi"],
                               text_color=C["text"]).pack(side="left", padx=(0, 14))

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(10, 16))
        ctk.CTkLabel(row, text="保存到", width=64, anchor="w", font=self.f_body,
                     text_color=C["muted"]).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.out_dir_var, height=32, font=self.f_body,
                     corner_radius=8, border_width=1, border_color=C["fieldline"],
                     fg_color=C["field"], placeholder_text="留空 = 与 PDF 同目录"
                     ).pack(side="left", fill="x", expand=True)
        self._btn(row, "浏览…", self.pick_dir, width=70, height=32).pack(side="left", padx=8)

        # ---------- 右：日志 ----------
        right = ctk.CTkFrame(body, fg_color=C["panel"], corner_radius=14,
                             border_width=1, border_color=C["line"])
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 4))
        ctk.CTkLabel(head, text="日志", font=self.f_group,
                     text_color=C["muted"]).pack(side="left")
        self._btn(head, "清空", self.clear_log, width=62, height=28).pack(side="right")

        ctk.CTkFrame(right, height=1, fg_color=C["line"]).pack(fill="x")

        self.log = ctk.CTkTextbox(right, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                                  wrap="word", fg_color=C["panel"], text_color=C["text"],
                                  corner_radius=0, border_width=0,
                                  scrollbar_button_color="#e0e0e5",
                                  scrollbar_button_hover_color="#cfcfd6")
        self.log.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        self.log.configure(state="disabled")
        inner = getattr(self.log, "_textbox", self.log)
        for name, color in (("err", C["danger"]), ("ok", "#1a8f3c"), ("dim", C["muted"])):
            try:
                inner.tag_config(name, foreground=color)
            except Exception:
                pass

        # ---------- 底部 ----------
        footer = ctk.CTkFrame(self.root, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(0, 16))
        self.btn_start = ctk.CTkButton(footer, text="开始转换", width=140, height=42,
                                       font=ctk.CTkFont(family=FONT_FAMILY, size=15),
                                       corner_radius=10, fg_color=C["accent"],
                                       hover_color=C["accent_hi"], text_color="#ffffff",
                                       command=self.start)
        self.btn_start.pack(side="left")
        self.progress = ctk.CTkProgressBar(footer, height=8, corner_radius=4,
                                           progress_color=C["accent"], fg_color="#e3e5e9")
        self.progress.pack(side="left", fill="x", expand=True, padx=16)
        self.progress.set(0)

        self.append_log("选好 PDF 后点「开始转换」，多页会拼成一张长图。", "dim")

    def _btn(self, parent, text, command, width=110, height=32):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                             font=self.f_body, corner_radius=8, border_width=1,
                             border_color=C["fieldline"], fg_color=C["panel"],
                             hover_color=C["hover"], text_color=C["text"])

    # ------------------------------------------------------------ 列表操作
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择 PDF", filetypes=[("PDF 文件", "*.pdf")])
        added = 0
        for path in paths:
            path = os.path.normpath(path)
            if path not in self.files:
                self.files.append(path)
                self.listbox.insert("end", os.path.basename(path))
                added += 1
        if added:
            self.append_log("添加 %d 个文件" % added, "dim")
            if not self.out_dir_var.get():
                self.out_dir_var.set(os.path.dirname(self.files[0]))

    def remove_selected(self):
        for index in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(index)
            del self.files[index]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()

    def pick_dir(self):
        chosen = filedialog.askdirectory(title="选择输出目录")
        if chosen:
            self.out_dir_var.set(os.path.normpath(chosen))

    # ------------------------------------------------------------ 日志 / 进度
    def append_log(self, text, tag=None):
        self.log.configure(state="normal")
        try:
            self.log.insert("end", text + "\n", tag or ())
        except Exception:
            self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_status(self, text, color=None):
        self.status_var.set(text)
        self.status_label.configure(text_color=color or C["muted"])

    def set_progress(self, done, total, label):
        if total:
            self.progress.configure(mode="determinate")
            self.progress.set(max(0.0, min(1.0, done / float(total))))
            self._set_status("%s %d/%d" % (label, done, total), C["accent"])
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self._set_status(label or "处理中…", C["accent"])

    # ------------------------------------------------------------ 转换
    def start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showwarning("还没选文件", "先添加至少一个 PDF。")
            return
        try:
            dpi = int(self.dpi_var.get())
        except ValueError:
            messagebox.showwarning("参数错误", "DPI 必须是数字，例如 150。")
            return
        if not 40 <= dpi <= 600:
            messagebox.showwarning("参数错误", "DPI 建议在 40～600 之间。")
            return

        out_dir = self.out_dir_var.get().strip() or ""
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("目录不可用", "%s" % exc)
                return

        self.clear_log()
        self.stop_event.clear()
        self.running = True
        self.btn_start.configure(state="disabled")
        fmt = self.fmt_var.get()
        self.append_log("开始转换 %d 个 PDF（%d DPI，%s）" % (len(self.files), dpi, fmt), "ok")
        self.worker = threading.Thread(
            target=self._worker,
            args=(list(self.files), out_dir, dpi, fmt), daemon=True)
        self.worker.start()

    def _worker(self, files, out_dir, dpi, fmt):
        emit = self.queue.put
        done = 0
        for path in files:
            if self.stop_event.is_set():
                break
            try:
                result = convert_pdf(
                    path, out_dir=out_dir or None, dpi=dpi, fmt=fmt,
                    log=lambda m: emit(("log", m, None)),
                    progress=lambda d, t, l: emit(("progress", d, t, l)),
                    should_stop=self.stop_event.is_set)
                if result.get("ok"):
                    done += 1
                    emit(("log", "  ✓ %s" % os.path.basename(result["out_path"]), "ok"))
                else:
                    emit(("log", "  ✗ %s：%s" % (os.path.basename(path), result.get("message")), "err"))
            except Exception as exc:                    # noqa: BLE001
                emit(("log", "  ✗ %s：%s: %s" % (os.path.basename(path),
                                                 type(exc).__name__, exc), "err"))
                emit(("log", traceback.format_exc(), "dim"))
        emit(("finish", done, len(files)))

    # ------------------------------------------------------------ 轮询
    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg[0] == "log":
                    self.append_log(msg[1], msg[2])
                elif msg[0] == "progress":
                    self.set_progress(msg[1], msg[2], msg[3])
                elif msg[0] == "finish":
                    self._finish(msg[1], msg[2])
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _finish(self, done, total):
        self.running = False
        self.btn_start.configure(state="normal")
        try:
            self.progress.stop()
        except Exception:
            pass
        if done:
            self.progress.configure(mode="determinate")
            self.progress.set(1.0)
            self._set_status("完成 %d/%d" % (done, total), C["ok"])
            self.append_log("全部完成：成功 %d / %d" % (done, total), "ok")
        else:
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self._set_status("没有转换成功", C["danger"])

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("正在转换", "转换还没结束，确定退出吗？"):
                return
            self.stop_event.set()
        self.root.destroy()


# ===========================================================================
# 入口
# ===========================================================================

def cli(argv=None):
    parser = argparse.ArgumentParser(description="把 PDF 拼成一张长图")
    parser.add_argument("pdfs", nargs="+", help="一个或多个 PDF 文件")
    parser.add_argument("-o", "--out-dir", default=None, help="输出目录（默认与 PDF 同目录）")
    parser.add_argument("--dpi", type=int, default=150, help="清晰度，默认 150")
    parser.add_argument("--format", default="JPEG", choices=["JPEG", "PNG"], help="输出格式")
    parser.add_argument("--quality", type=int, default=90, help="JPEG 质量（1-100）")
    args = parser.parse_args(argv)

    ok = 0
    for path in args.pdfs:
        if not os.path.isfile(path):
            print("  ! 找不到文件：%s" % path)
            continue
        try:
            result = convert_pdf(path, out_dir=args.out_dir, dpi=args.dpi,
                                 fmt=args.format, quality=args.quality, log=print)
            ok += 1 if result.get("ok") else 0
        except Exception as exc:                        # noqa: BLE001
            print("  ✗ %s：%s: %s" % (os.path.basename(path), type(exc).__name__, exc))
    print("\n完成 %d / %d" % (ok, len(args.pdfs)))
    return 0 if ok else 1


def main():
    if pdfium is None or Image is None:
        holder = tk.Tk()
        holder.withdraw()
        messagebox.showerror(
            "缺少组件",
            "需要 pypdfium2 和 Pillow。请在工具目录执行：\n\n"
            "python -m pip install pypdfium2 pillow --target libs")
        return 1

    if len(sys.argv) > 1 and not sys.argv[1].lower().endswith(".py"):
        return cli()

    if ctk is None:
        holder = tk.Tk()
        holder.withdraw()
        messagebox.showerror(
            "缺少 customtkinter",
            "界面需要 customtkinter。请在工具目录执行：\n\n"
            "python -m pip install customtkinter --target libs\n\n"
            "也可以直接用命令行：python pdf2img.py 文件.pdf")
        return 1

    error_log = os.path.join(APP_DIR, "pdf2img_error.log")
    try:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
        Pdf2ImgGUI(root)
        root.mainloop()
        return 0
    except Exception:
        detail = traceback.format_exc()
        try:
            with open(error_log, "w", encoding="utf-8") as fh:
                fh.write(detail)
        except Exception:
            pass
        try:
            messagebox.showerror("启动失败",
                                 "出错了，详情写入：\n%s\n\n%s" % (error_log, detail[-800:]))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main() or 0)
