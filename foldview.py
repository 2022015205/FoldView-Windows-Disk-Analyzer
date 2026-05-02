import csv
import json
import math
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk


APP_NAME = "FoldView"
WINDOW_TITLE = "FoldView - Windows 磁盘空间可视化分析工具"
BG = "#F3F6FB"
CARD = "#FFFFFF"
BORDER = "#E5E7EB"
TEXT = "#111827"
MUTED = "#64748B"
BLUE = "#2563EB"
PALE_BLUE = "#EAF2FF"
HOVER = "#F3F6FB"
GREEN = "#22C55E"
FONT = "Microsoft YaHei UI"
NUM_FONT = "Segoe UI"
COLORS = ["#3B82F6", "#22C55E", "#F59E0B", "#EF476F", "#8B5CF6", "#06B6D4", "#9CA3AF", "#F97316"]
CONFIG_PATH = Path.home() / ".foldview_config.json"
CONFIG_VERSION = 3
DEFAULT_WINDOW = (1440, 860)
MIN_WINDOW = (1280, 760)
TOPBAR_H = 80
STATUSBAR_H = 36
SUMMARY_DEFAULT_H = 165
SUMMARY_MIN_H = 145
SUMMARY_MAX_H = 190
LEFT_MIN_W = 260
LEFT_MAX_W = 440
CENTER_MIN_W = 540
RIGHT_MIN_W = 420


class ScanCancelled(Exception):
    pass


@dataclass
class LargeFile:
    path: Path
    size: int
    modified: float


@dataclass
class FolderNode:
    path: Path
    size: int = 0
    file_count: int = 0
    folder_count: int = 0
    modified: float = 0
    children: list["FolderNode"] = field(default_factory=list)
    denied_count: int = 0
    large_files: list[LargeFile] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def split_size(size_text: str):
    parts = size_text.split()
    if len(parts) == 2:
        return parts[0], parts[1]
    return size_text, ""


def format_time(timestamp: float) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y/%m/%d %H:%M")


def ellipsize(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def scan_folder(folder_path: str, cancel_event=None, progress_callback=None) -> FolderNode:
    root_path = Path(folder_path).expanduser().resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"路径不存在: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"不是文件夹: {root_path}")

    scanned_files = 0
    scanned_dirs = 0
    last_emit = 0.0

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def emit(path: Path, force=False):
        nonlocal last_emit
        now = time.time()
        if progress_callback and (force or now - last_emit > 0.12):
            last_emit = now
            progress_callback(path, scanned_files, scanned_dirs)

    def keep_large(current, incoming):
        merged = current + incoming
        merged.sort(key=lambda item: item.size, reverse=True)
        return merged[:80]

    def scan_node(path: Path) -> FolderNode:
        nonlocal scanned_files, scanned_dirs
        if cancelled():
            raise ScanCancelled()

        node = FolderNode(path=path)
        latest_modified = 0.0
        try:
            entries = list(os.scandir(path))
        except (PermissionError, OSError):
            node.denied_count += 1
            return node

        folders = []
        for entry in entries:
            if cancelled():
                raise ScanCancelled()
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = scan_node(Path(entry.path))
                    folders.append(child)
                    node.size += child.size
                    node.file_count += child.file_count
                    node.folder_count += child.folder_count + 1
                    node.denied_count += child.denied_count
                    node.large_files = keep_large(node.large_files, child.large_files)
                    latest_modified = max(latest_modified, child.modified)
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    node.size += stat.st_size
                    node.file_count += 1
                    scanned_files += 1
                    latest_modified = max(latest_modified, stat.st_mtime)
                    node.large_files = keep_large(node.large_files, [LargeFile(Path(entry.path), stat.st_size, stat.st_mtime)])
                    if scanned_files % 96 == 0:
                        emit(path)
            except (PermissionError, OSError):
                node.denied_count += 1

        scanned_dirs += 1
        node.modified = latest_modified or safe_mtime(path)
        node.children = sorted(folders, key=lambda child: child.size, reverse=True)
        emit(path)
        return node

    result = scan_node(root_path)
    emit(root_path, force=True)
    return result


def aggregate_for_charts(children: list[FolderNode], max_items=7, min_ratio=0.018):
    total = sum(child.size for child in children)
    if total <= 0:
        return []
    visible = []
    other = FolderNode(path=Path("其他文件"))
    for index, child in enumerate(children):
        if index < max_items and child.size / total >= min_ratio:
            visible.append(child)
        else:
            other.size += child.size
            other.file_count += child.file_count
            other.folder_count += child.folder_count
    if other.size:
        visible.append(other)
    return visible


def round_rect(canvas, x1, y1, x2, y2, radius=12, **kwargs):
    radius = max(0, min(float(radius), abs(x2 - x1) / 2, abs(y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def make_folder_photo(size=56):
    image = tk.PhotoImage(width=size, height=size)
    scale = size / 56

    def rect(x1, y1, x2, y2, color):
        image.put(color, to=(round(x1 * scale), round(y1 * scale), round(x2 * scale), round(y2 * scale)))

    rect(9, 19, 49, 45, "#1D4ED8")
    rect(12, 15, 28, 24, "#60A5FA")
    rect(12, 21, 51, 47, "#2563EB")
    rect(14, 24, 52, 48, "#3B82F6")
    rect(40, 38, 44, 42, "#BFDBFE")
    rect(46, 38, 50, 42, "#DBEAFE")
    return image


class RoundedCard(tk.Frame):
    def __init__(self, parent, radius=14, padding=0, bg=BG):
        super().__init__(parent, bg=bg, highlightthickness=0)
        self.radius = radius
        self.padding = padding
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=CARD)
        self.window = self.canvas.create_window(padding, padding, anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", self.redraw)

    def redraw(self, _event=None):
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        self.canvas.delete("card")
        round_rect(self.canvas, 1, 1, width - 1, height - 1, self.radius, fill=CARD, outline=BORDER, width=1, tags="card")
        self.canvas.tag_lower("card")
        self.canvas.coords(self.window, self.padding, self.padding)
        self.canvas.itemconfigure(self.window, width=max(1, width - self.padding * 2), height=max(1, height - self.padding * 2))


class Tooltip:
    def __init__(self, root):
        self.root = root
        self.tip = None

    def show(self, x, y, text):
        self.hide()
        self.tip = tk.Toplevel(self.root)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg="#111827")
        tk.Label(self.tip, text=text, bg="#111827", fg="#FFFFFF", justify="left", font=(FONT, 9), padx=10, pady=7).pack()
        self.tip.wm_geometry(f"+{x + 14}+{y + 14}")

    def hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class FoldViewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.apply_window_bounds()
        self.configure(bg=BG)

        self.config_data = self.load_config()
        self.cache = {}
        self.result_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.current_root = None
        self.result_rows = []
        self.scanning = False
        self.scan_start = 0.0
        self.sort_key = "size"
        self.sort_reverse = True
        self.redraw_job = None
        self.hover_item = None
        self.item_paths = {}
        self.chart_items = {}
        self.table_rows = []
        self.images = {
            "app": make_folder_photo(40),
            "summary": make_folder_photo(82),
            "folder16": make_folder_photo(16),
            "folder18": make_folder_photo(18),
        }
        self.tooltip = Tooltip(self)
        self.iconphoto(True, self.images["app"])

        self.configure_styles()
        self.build_layout()
        self.load_drives()
        self.restore_layout()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_welcome_state()

    def apply_window_bounds(self):
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width, height = DEFAULT_WINDOW
        if screen_w < width or screen_h < height:
            width = max(900, int(screen_w * 0.90))
            height = max(650, int(screen_h * 0.85))
        min_w = min(MIN_WINDOW[0], width)
        min_h = min(MIN_WINDOW[1], height)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min_w, min_h)

    def configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Nav.Treeview", rowheight=34, font=(FONT, 10), background=CARD, fieldbackground=CARD, borderwidth=0, relief="flat")
        style.configure("Nav.Treeview.Heading", background=CARD, foreground=CARD, borderwidth=0, relief="flat")
        style.map("Nav.Treeview", background=[("selected", PALE_BLUE)], foreground=[("selected", "#1D4ED8")])

    def make_pane(self, parent, orient):
        return tk.PanedWindow(
            parent,
            orient=orient,
            bg=BORDER,
            bd=0,
            relief="flat",
            sashwidth=5,
            sashpad=1,
            sashrelief="flat",
            showhandle=False,
            handlesize=8,
            handlepad=4,
            opaqueresize=True,
        )

    def build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.build_topbar()

        self.main_pane = self.make_pane(self, tk.HORIZONTAL)
        self.main_pane.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        self.main_pane.bind("<ButtonRelease-1>", lambda _event: self.on_splitter_changed())
        self.main_pane.bind("<B1-Motion>", lambda _event: self.after_idle(self.constrain_main_pane))
        self.main_pane.bind("<Configure>", self.on_main_configure)

        self.sidebar_card = RoundedCard(self.main_pane, padding=10)
        self.sidebar_card.body.grid_rowconfigure(1, weight=1)
        self.sidebar_card.body.grid_columnconfigure(0, weight=1)
        self.main_pane.add(self.sidebar_card, minsize=260, stretch="never")
        self.build_sidebar(self.sidebar_card.body)

        self.center_pane = self.make_pane(self.main_pane, tk.VERTICAL)
        self.center_pane.bind("<ButtonRelease-1>", lambda _event: self.on_splitter_changed())
        self.center_pane.bind("<B1-Motion>", lambda _event: self.after_idle(self.constrain_center_pane))
        self.center_pane.bind("<Configure>", lambda _event: self.after_idle(self.constrain_center_pane))
        self.main_pane.add(self.center_pane, minsize=CENTER_MIN_W, stretch="always")
        self.build_center()

        self.right_pane = self.make_pane(self.main_pane, tk.VERTICAL)
        self.right_pane.bind("<ButtonRelease-1>", lambda _event: self.on_splitter_changed())
        self.right_pane.bind("<Configure>", lambda _event: self.schedule_chart_redraw())
        self.main_pane.add(self.right_pane, minsize=RIGHT_MIN_W, stretch="always")
        self.build_right()

        self.build_statusbar()
        self.build_context_menu()

    def on_splitter_changed(self):
        self.constrain_main_pane()
        self.constrain_center_pane()
        self.save_layout()
        self.schedule_chart_redraw()

    def on_main_configure(self, _event=None):
        if not getattr(self, "_restored_layout", False):
            return
        self.after_idle(self.constrain_main_pane)

    def set_main_sashes(self, left, center):
        try:
            self.main_pane.sash_place(0, int(left), 1)
            self.main_pane.sash_place(1, int(left + center), 1)
        except tk.TclError:
            pass

    def default_main_sizes(self):
        width = max(self.main_pane.winfo_width(), sum([LEFT_MIN_W, CENTER_MIN_W, RIGHT_MIN_W]) + 16)
        left = max(LEFT_MIN_W, min(LEFT_MAX_W, int(width * 0.22)))
        center = max(CENTER_MIN_W, int(width * 0.42))
        right = max(RIGHT_MIN_W, width - left - center - 12)
        if right < RIGHT_MIN_W:
            center = max(CENTER_MIN_W, width - left - RIGHT_MIN_W - 12)
        return left, center, right

    def constrain_main_pane(self):
        try:
            width = self.main_pane.winfo_width()
            s0 = self.main_pane.sash_coord(0)[0]
            s1 = self.main_pane.sash_coord(1)[0]
        except tk.TclError:
            return
        left = max(LEFT_MIN_W, min(LEFT_MAX_W, s0))
        center = max(CENTER_MIN_W, s1 - left)
        max_center = max(CENTER_MIN_W, width - left - RIGHT_MIN_W - 10)
        center = min(center, max_center)
        if width - left - center < RIGHT_MIN_W:
            left = max(LEFT_MIN_W, min(left, width - CENTER_MIN_W - RIGHT_MIN_W - 10))
            center = max(CENTER_MIN_W, width - left - RIGHT_MIN_W - 10)
        self.set_main_sashes(left, center)

    def constrain_center_pane(self):
        try:
            height = self.center_pane.winfo_height()
            y = self.center_pane.sash_coord(0)[1]
        except tk.TclError:
            return
        summary_h = max(SUMMARY_MIN_H, min(SUMMARY_MAX_H, y or SUMMARY_DEFAULT_H))
        summary_h = min(summary_h, max(SUMMARY_MIN_H, height - 260))
        try:
            self.center_pane.sash_place(0, 1, int(summary_h))
        except tk.TclError:
            pass

    def build_topbar(self):
        top = tk.Frame(self, bg=BG, height=TOPBAR_H)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(8, 8))
        top.grid_propagate(False)
        top.grid_columnconfigure(1, weight=1)

        brand = tk.Frame(top, bg=BG, width=280, height=64)
        brand.grid(row=0, column=0, sticky="w")
        brand.grid_propagate(False)
        tk.Label(brand, image=self.images["app"], bg=BG).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        tk.Label(brand, text="存储感知助手", bg=BG, fg=TEXT, font=(FONT, 18, "bold")).grid(row=0, column=1, sticky="sw")
        tk.Label(brand, text="Folder Size Analyzer", bg=BG, fg=MUTED, font=(FONT, 10)).grid(row=1, column=1, sticky="nw")

        path_box = tk.Frame(top, bg=CARD, height=40, highlightthickness=1, highlightbackground=BORDER)
        path_box.grid(row=0, column=1, sticky="ew", padx=(12, 12))
        path_box.grid_propagate(False)
        path_box.grid_columnconfigure(0, weight=1)
        self.path_var = tk.StringVar()
        entry = tk.Entry(path_box, textvariable=self.path_var, relief="flat", bg=CARD, fg=TEXT, insertbackground=TEXT, font=(FONT, 11))
        entry.grid(row=0, column=0, sticky="ew", padx=14, pady=8)
        entry.bind("<Return>", lambda _event: self.scan_from_input())
        tk.Label(path_box, text="📁", bg=CARD, fg=MUTED, font=(FONT, 13)).grid(row=0, column=1, padx=(0, 12), pady=7)

        self.scan_button = self.top_button(top, "▶ 扫描", self.scan_from_input, BLUE, "#FFFFFF", 96)
        self.scan_button.grid(row=0, column=2, padx=(0, 10))
        self.top_button(top, "选择", self.choose_folder, CARD, TEXT, 88).grid(row=0, column=3, padx=(0, 10))
        self.top_button(top, "⟳ 刷新", self.refresh_all, CARD, TEXT, 88).grid(row=0, column=4, padx=(0, 10))
        self.top_button(top, "导出报告", self.export_report, CARD, TEXT, 112).grid(row=0, column=5, padx=(0, 10))
        self.cancel_button = self.top_button(top, "取消", self.cancel_scan, "#E5E2DC", "#8B8A86", 88)
        self.cancel_button.grid(row=0, column=6, padx=(0, 10))
        tk.Label(top, text="⋯", bg=BG, fg=TEXT, font=(FONT, 18, "bold"), width=3).grid(row=0, column=7)

    def top_button(self, parent, text, command, bg, fg, pixel_width):
        frame = tk.Frame(parent, bg=bg, width=pixel_width, height=40, highlightthickness=1, highlightbackground=BORDER)
        frame.grid_propagate(False)
        button = tk.Button(frame, text=text, command=command, bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief="flat", bd=0, font=(FONT, 10, "bold"), cursor="hand2")
        button.pack(fill="both", expand=True)
        frame.button = button
        return frame

    def set_scan_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.scan_button.button.configure(state=state)
        self.cancel_button.button.configure(state="normal" if not enabled else "disabled")

    def build_sidebar(self, parent):
        tk.Label(parent, text="文件夹目录", bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).grid(row=0, column=0, sticky="w", padx=4, pady=(4, 10))
        self.folder_tree = ttk.Treeview(parent, show="tree", style="Nav.Treeview", selectmode="browse")
        self.folder_tree.column("#0", width=320, stretch=True)
        self.folder_tree.grid(row=1, column=0, sticky="nsew")
        self.folder_tree.tag_configure("hover", background=HOVER)
        self.folder_tree.bind("<<TreeviewOpen>>", self.on_tree_open)
        self.folder_tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.folder_tree.bind("<Motion>", self.on_tree_motion)
        self.folder_tree.bind("<Leave>", lambda _event: self.clear_tree_hover())
        settings = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground="#EEF2F7")
        settings.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tk.Label(settings, text="⚙ 设置", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=10, pady=10)

    def build_center(self):
        self.summary_card = RoundedCard(self.center_pane, padding=0)
        self.summary_card.configure(height=SUMMARY_DEFAULT_H)
        self.center_pane.add(self.summary_card, minsize=SUMMARY_MIN_H, stretch="never")
        body = self.summary_card.body
        body.grid_columnconfigure(0, minsize=80)
        body.grid_columnconfigure(1, weight=1, minsize=240)
        body.grid_columnconfigure(2, minsize=280)
        body.bind("<Configure>", lambda _event: self.fit_summary_text())
        tk.Label(body, image=self.images["summary"], bg=CARD).grid(row=0, column=0, rowspan=3, padx=(18, 10), pady=20)
        tk.Label(body, text="当前文件夹总大小", bg=CARD, fg=MUTED, font=(FONT, 10, "bold")).grid(row=0, column=1, sticky="w", pady=(26, 0))
        self.total_size_value = tk.StringVar(value="请选择目录")
        self.total_size_unit = tk.StringVar(value="")
        number = tk.Frame(body, bg=CARD)
        number.grid(row=1, column=1, sticky="w")
        self.total_number_label = tk.Label(number, textvariable=self.total_size_value, bg=CARD, fg=TEXT, font=(NUM_FONT, 36, "bold"))
        self.total_number_label.pack(side="left")
        self.total_unit_label = tk.Label(number, textvariable=self.total_size_unit, bg=CARD, fg=TEXT, font=(NUM_FONT, 25, "bold"))
        self.total_unit_label.pack(side="left", padx=(8, 0), pady=(8, 0))
        self.summary_var = tk.StringVar(value="左侧选择目录后点击扫描")
        tk.Label(body, textvariable=self.summary_var, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(row=2, column=1, sticky="w", pady=(0, 18))

        disk = tk.Frame(body, bg=CARD, width=280)
        disk.grid(row=0, column=2, rowspan=3, sticky="nsew", padx=(8, 18), pady=20)
        disk.grid_propagate(False)
        tk.Label(disk, text="占用磁盘空间", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        self.disk_percent_var = tk.StringVar(value="--")
        tk.Label(disk, textvariable=self.disk_percent_var, bg=CARD, fg=TEXT, font=(NUM_FONT, 15, "bold")).pack(anchor="w", pady=(6, 6))
        self.disk_bar = tk.Canvas(disk, height=12, bg=CARD, highlightthickness=0)
        self.disk_bar.pack(fill="x")
        tk.Label(disk, text="可用空间", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(14, 0))
        self.disk_free_var = tk.StringVar(value="--")
        tk.Label(disk, textvariable=self.disk_free_var, bg=CARD, fg=TEXT, font=(NUM_FONT, 13, "bold")).pack(anchor="w", pady=(4, 0))

        self.table_card = RoundedCard(self.center_pane, padding=0)
        self.center_pane.add(self.table_card, minsize=260, stretch="always")
        table_body = self.table_card.body
        table_body.grid_rowconfigure(1, weight=1)
        table_body.grid_columnconfigure(0, weight=1)
        self.table_header = tk.Canvas(table_body, height=38, bg="#F8FAFC", highlightthickness=0)
        self.table_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        self.table_canvas = tk.Canvas(table_body, bg=CARD, highlightthickness=0)
        self.table_canvas.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        table_scroll = ttk.Scrollbar(table_body, orient="vertical", command=self.table_canvas.yview)
        table_scroll.grid(row=1, column=1, sticky="ns", pady=(0, 12))
        self.table_canvas.configure(yscrollcommand=table_scroll.set)
        table_xscroll = ttk.Scrollbar(table_body, orient="horizontal", command=self.table_xview)
        table_xscroll.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.table_canvas.configure(xscrollcommand=table_xscroll.set)
        self.table_canvas.bind("<Configure>", lambda _event: self.draw_table())
        self.table_canvas.bind("<Button-3>", self.show_table_menu)

    def build_right(self):
        self.donut_card = RoundedCard(self.right_pane, padding=0)
        self.right_pane.add(self.donut_card, minsize=260, stretch="always")
        body = self.donut_card.body
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)
        tk.Label(body, text="按文件夹大小占比", bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).grid(row=0, column=0, sticky="w", padx=16, pady=14)
        self.donut_canvas = tk.Canvas(body, bg=CARD, highlightthickness=0)
        self.donut_canvas.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.donut_canvas.bind("<Configure>", self.schedule_chart_redraw)
        self.donut_canvas.bind("<Motion>", self.on_chart_motion)
        self.donut_canvas.bind("<Leave>", lambda _event: self.tooltip.hide())

        self.rank_card = RoundedCard(self.right_pane, padding=0)
        self.right_pane.add(self.rank_card, minsize=260, stretch="always")
        body = self.rank_card.body
        body.grid_rowconfigure(1, weight=1)
        body.grid_columnconfigure(0, weight=1)
        tk.Label(body, text="文件夹大小分布（排行图）", bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).grid(row=0, column=0, sticky="w", padx=16, pady=14)
        self.rank_canvas = tk.Canvas(body, bg=CARD, highlightthickness=0)
        self.rank_canvas.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        rank_scroll = ttk.Scrollbar(body, orient="vertical", command=self.rank_canvas.yview)
        rank_scroll.grid(row=1, column=1, sticky="ns", pady=(0, 12))
        self.rank_canvas.configure(yscrollcommand=rank_scroll.set)
        self.rank_canvas.bind("<Configure>", self.schedule_chart_redraw)
        self.rank_canvas.bind("<Motion>", self.on_chart_motion)
        self.rank_canvas.bind("<Leave>", lambda _event: self.tooltip.hide())

    def build_statusbar(self):
        self.status = tk.Frame(self, bg=BG, height=36)
        self.status.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 8))
        self.status.grid_propagate(False)
        self.status.grid_columnconfigure(3, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        self.elapsed_var = tk.StringVar(value="扫描用时: --")
        self.done_time_var = tk.StringVar(value="完成时间: --")
        self.path_status_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="共扫描 0 个文件，0 个文件夹")
        tk.Label(self.status, textvariable=self.status_var, bg=BG, fg=GREEN, font=(FONT, 10, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(self.status, textvariable=self.elapsed_var, bg=BG, fg="#475569", font=(FONT, 10)).grid(row=0, column=1, sticky="w", padx=(24, 0))
        tk.Label(self.status, textvariable=self.done_time_var, bg=BG, fg="#475569", font=(FONT, 10)).grid(row=0, column=2, sticky="w", padx=(24, 0))
        tk.Label(self.status, textvariable=self.path_status_var, bg=BG, fg=MUTED, font=(FONT, 9)).grid(row=0, column=3, sticky="w", padx=(24, 0))
        tk.Label(self.status, textvariable=self.count_var, bg=BG, fg="#475569", font=(FONT, 10)).grid(row=0, column=4, sticky="e")

    def build_context_menu(self):
        self.table_menu = tk.Menu(self, tearoff=0)
        self.table_menu.add_command(label="在资源管理器中打开", command=self.open_selected_path)
        self.table_menu.add_command(label="扫描该目录", command=self.scan_selected_result)

    def get_drives(self):
        if os.name != "nt":
            return ["/"]
        return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]

    def disk_value_text(self, path):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return ""
        used = usage.total - usage.free
        return f"{human_size(used)} / {human_size(usage.total)}"

    def load_drives(self):
        self.folder_tree.delete(*self.folder_tree.get_children())
        self.item_paths.clear()
        for drive in self.get_drives():
            text = f"{drive}    {self.disk_value_text(drive)}"
            item = self.folder_tree.insert("", "end", text=text, image=self.images["folder18"], tags=(drive,))
            self.item_paths[item] = drive
            self.folder_tree.insert(item, "end", text="加载中...")
        drives = self.get_drives()
        if drives and not self.path_var.get():
            self.set_selected_path(drives[0])

    def show_welcome_state(self):
        drives = self.get_drives()
        self.set_total_size_text("整机存储概览")
        self.summary_var.set(f"已发现 {len(drives)} 个磁盘；点击目录只选择，点击扫描才统计")
        self.draw_disk_bar(0)
        self.draw_empty_charts()

    def refresh_all(self):
        self.load_drives()
        if self.current_root:
            self.draw_charts(self.current_root)
        else:
            self.show_welcome_state()

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if selected:
            self.set_selected_path(selected)

    def set_selected_path(self, path):
        self.path_var.set(path)
        self.path_status_var.set(f"当前路径: {ellipsize(path, 48)}")

    def scan_signature(self, path):
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return 0

    def scan_from_input(self):
        self.start_scan(self.path_var.get().strip())

    def start_scan(self, path: str):
        if self.scanning:
            messagebox.showinfo(APP_NAME, "正在扫描，请先取消或等待完成。")
            return
        if not path or not os.path.isdir(path):
            messagebox.showwarning(APP_NAME, "请选择有效文件夹路径。")
            return
        sig = self.scan_signature(path)
        cached = self.cache.get(path)
        if cached and cached[0] == sig:
            self.scan_start = time.time()
            self.scan_complete(cached[1], from_cache=True)
            return
        self.cancel_event.clear()
        self.scanning = True
        self.scan_start = time.time()
        self.set_scan_enabled(False)
        self.status_var.set("正在扫描，可取消")
        self.elapsed_var.set("扫描用时: 0.0 秒")
        self.done_time_var.set("完成时间: --")
        self.count_var.set("已扫描 0 个文件，0 个文件夹")
        self.path_status_var.set(f"当前扫描: {ellipsize(path, 48)}")
        self.set_total_size_text("扫描中")
        self.summary_var.set("后台正在遍历文件夹，请稍等")
        self.clear_results()
        threading.Thread(target=self.scan_worker, args=(path, sig), daemon=True).start()
        self.after(100, self.poll_queue)

    def cancel_scan(self):
        if self.scanning:
            self.cancel_event.set()
            self.status_var.set("正在取消")

    def scan_worker(self, path, sig):
        def progress(path_obj, files, dirs):
            self.result_queue.put(("progress", str(path_obj), files, dirs))
        try:
            result = scan_folder(path, self.cancel_event, progress)
            self.cache[path] = (sig, result)
            self.result_queue.put(("done", result, False))
        except ScanCancelled:
            self.result_queue.put(("cancelled",))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))

    def poll_queue(self):
        try:
            while True:
                kind, *payload = self.result_queue.get_nowait()
                if kind == "progress":
                    path, files, dirs = payload
                    elapsed = time.time() - self.scan_start
                    self.elapsed_var.set(f"扫描用时: {elapsed:.1f} 秒")
                    self.count_var.set(f"已扫描 {files:,} 个文件，{dirs:,} 个文件夹")
                    self.path_status_var.set(f"当前扫描: {ellipsize(path, 48)}")
                elif kind == "done":
                    self.scan_complete(payload[0], from_cache=payload[1])
                    return
                elif kind == "cancelled":
                    self.scan_cancelled()
                    return
                elif kind == "error":
                    self.scan_failed(payload[0])
                    return
        except queue.Empty:
            pass
        if self.scanning:
            self.after(100, self.poll_queue)

    def finish_scan_ui(self):
        self.scanning = False
        self.set_scan_enabled(True)

    def scan_complete(self, node: FolderNode, from_cache=False):
        self.finish_scan_ui()
        self.current_root = node
        self.result_rows = node.children[:]
        elapsed = time.time() - self.scan_start
        self.set_total_size_text(human_size(node.size))
        denied = f"，跳过 {node.denied_count} 个无权限目录" if node.denied_count else ""
        self.summary_var.set(f"共 {node.file_count:,} 个文件，{node.folder_count:,} 个文件夹{denied}")
        self.status_var.set("✓ 扫描完成" + ("（缓存）" if from_cache else ""))
        self.elapsed_var.set(f"扫描用时: {elapsed:.1f} 秒")
        self.done_time_var.set(f"完成时间: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}")
        self.count_var.set(f"共扫描 {node.file_count:,} 个文件，{node.folder_count:,} 个文件夹")
        self.path_status_var.set(f"当前路径: {ellipsize(str(node.path), 48)}")
        self.update_disk_usage(node.path, node.size)
        self.populate_table()
        self.populate_scanned_tree(node)
        self.draw_charts(node)

    def scan_cancelled(self):
        self.finish_scan_ui()
        self.status_var.set("已取消")
        self.summary_var.set("扫描已取消，可重新选择目录后扫描")

    def scan_failed(self, error):
        self.finish_scan_ui()
        self.status_var.set("扫描失败")
        messagebox.showerror(APP_NAME, error)

    def set_total_size_text(self, text):
        number, unit = split_size(text)
        self.total_size_value.set(number)
        self.total_size_unit.set(unit)
        self.fit_summary_text()

    def fit_summary_text(self):
        value = self.total_size_value.get()
        unit = self.total_size_unit.get()
        available = max(170, self.summary_card.body.winfo_width() - 380)
        full_len = len(value) + len(unit)
        font_size = 38 if full_len <= 8 else 34 if full_len <= 10 else 30
        while font_size > 24 and full_len * font_size * 0.62 > available:
            font_size -= 2
        self.total_number_label.configure(font=(NUM_FONT, font_size, "bold"))
        self.total_unit_label.configure(font=(NUM_FONT, max(18, int(font_size * 0.68)), "bold"))

    def update_disk_usage(self, path: Path, folder_size: int):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return
        percent = folder_size / usage.total * 100 if usage.total else 0
        self.disk_percent_var.set(f"{percent:.1f}%")
        self.disk_free_var.set(f"{human_size(usage.free)} / {human_size(usage.total)}")
        self.draw_disk_bar(percent)

    def draw_disk_bar(self, percent):
        self.disk_bar.delete("all")
        self.disk_bar.update_idletasks()
        width = max(100, self.disk_bar.winfo_width())
        round_rect(self.disk_bar, 0, 2, width, 11, 6, fill="#E5E7EB", outline="")
        round_rect(self.disk_bar, 0, 2, max(8, width * min(percent, 100) / 100), 11, 6, fill=BLUE, outline="")

    def clear_results(self):
        self.table_canvas.delete("all")
        self.donut_canvas.delete("all")
        self.rank_canvas.delete("all")
        self.table_rows.clear()

    def sort_table(self, key):
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = key != "name"
        self.populate_table()

    def node_sort_value(self, node):
        return {
            "name": node.name.lower(),
            "size": node.size,
            "ratio": node.size,
            "files": node.file_count,
            "modified": node.modified,
        }.get(self.sort_key, node.size)

    def populate_table(self):
        self.draw_table()

    def table_xview(self, *args):
        self.table_canvas.xview(*args)
        self.table_header.xview(*args)

    def draw_table(self):
        self.table_canvas.delete("all")
        self.table_header.delete("all")
        self.table_rows.clear()
        rows = sorted(self.result_rows, key=self.node_sort_value, reverse=self.sort_reverse)
        width = max(self.table_canvas.winfo_width(), 742)
        row_h = 40
        pad = 16
        size_w, ratio_w, files_w, mod_w = 110, 150, 110, 160
        name_w = max(180, width - pad * 2 - size_w - ratio_w - files_w - mod_w)
        x_name = pad
        x_size = x_name + name_w
        x_ratio = x_size + size_w
        x_files = x_ratio + ratio_w
        x_mod = x_files + files_w
        headers = [("名称", x_name), ("大小", x_size), ("占比", x_ratio), ("文件数", x_files), ("修改日期", x_mod)]
        self.table_header.create_rectangle(0, 0, width, 38, fill="#F8FAFC", outline="")
        self.table_header.configure(scrollregion=(0, 0, width, 38))
        for text, x in headers:
            self.table_header.create_text(x, 19, anchor="w", text=text, fill=TEXT, font=(FONT, 10, "bold"))
        total = max(self.current_root.size if self.current_root else 1, 1)
        for index, node in enumerate(rows):
            y = index * row_h
            if index:
                self.table_canvas.create_line(0, y, width, y, fill="#EEF2F7")
            ratio = node.size / total * 100
            tag = f"row_{index}"
            self.table_rows.append((tag, str(node.path)))
            self.table_canvas.create_image(x_name, y + 21, anchor="w", image=self.images["folder16"], tags=(tag,))
            name_chars = max(8, int((name_w - 34) / 8.5))
            self.table_canvas.create_text(x_name + 24, y + 21, anchor="w", text=ellipsize(node.name, name_chars), fill=TEXT, font=(FONT, 10), tags=(tag,))
            self.table_canvas.create_text(x_size, y + 21, anchor="w", text=human_size(node.size), fill=TEXT, font=(NUM_FONT, 10), tags=(tag,))
            self.table_canvas.create_text(x_ratio, y + 14, anchor="w", text=f"{ratio:.2f}%", fill=TEXT, font=(NUM_FONT, 10), tags=(tag,))
            bar_x = x_ratio
            bar_y = y + 25
            bar_w = ratio_w - 26
            round_rect(self.table_canvas, bar_x, bar_y, bar_x + bar_w, bar_y + 6, 999, fill="#E5E7EB", outline="", tags=(tag,))
            round_rect(self.table_canvas, bar_x, bar_y, bar_x + max(6, bar_w * min(ratio, 100) / 100), bar_y + 6, 999, fill=BLUE, outline="", tags=(tag,))
            self.table_canvas.create_text(x_files, y + 21, anchor="w", text=f"{node.file_count:,}", fill=TEXT, font=(NUM_FONT, 10), tags=(tag,))
            self.table_canvas.create_text(x_mod, y + 21, anchor="w", text=format_time(node.modified), fill="#475569", font=(FONT, 9), tags=(tag,))
            self.table_canvas.tag_bind(tag, "<Button-3>", self.show_table_menu)
        self.table_canvas.configure(scrollregion=(0, 0, width, max(1, len(rows) * row_h)))

    def populate_scanned_tree(self, root_node: FolderNode):
        self.folder_tree.delete(*self.folder_tree.get_children())
        root = self.folder_tree.insert("", "end", text=f"{root_node.path}    {human_size(root_node.size)}", image=self.images["folder18"], tags=(str(root_node.path),), open=True)
        self.item_paths[root] = str(root_node.path)
        for child in root_node.children[:160]:
            item = self.folder_tree.insert(root, "end", text=f"{child.name}    {human_size(child.size)}", image=self.images["folder18"], tags=(str(child.path),))
            self.item_paths[item] = str(child.path)
            if child.children:
                self.folder_tree.insert(item, "end", text="加载中...")
        for drive in self.get_drives():
            if not str(root_node.path).lower().startswith(drive.lower()):
                item = self.folder_tree.insert("", "end", text=f"{drive}    {self.disk_value_text(drive)}", image=self.images["folder18"], tags=(drive,))
                self.item_paths[item] = drive
                self.folder_tree.insert(item, "end", text="加载中...")

    def on_tree_open(self, _event):
        item = self.folder_tree.focus()
        children = self.folder_tree.get_children(item)
        if not children or self.folder_tree.item(children[0], "text") != "加载中...":
            return
        self.folder_tree.delete(*children)
        path = self.tree_item_path(item)
        if not path:
            return
        try:
            folders = sorted([entry for entry in os.scandir(path) if entry.is_dir(follow_symlinks=False)], key=lambda entry: entry.name.lower())
        except (PermissionError, OSError):
            return
        for entry in folders[:500]:
            child = self.folder_tree.insert(item, "end", text=entry.name, image=self.images["folder18"], tags=(entry.path,))
            self.item_paths[child] = entry.path
            self.folder_tree.insert(child, "end", text="加载中...")

    def on_tree_select(self, _event):
        path = self.tree_item_path(self.folder_tree.focus())
        if path and os.path.isdir(path):
            self.set_selected_path(path)

    def on_tree_motion(self, event):
        item = self.folder_tree.identify_row(event.y)
        if item == self.hover_item:
            return
        self.clear_tree_hover()
        self.hover_item = item
        if item:
            tags = list(self.folder_tree.item(item, "tags"))
            if "hover" not in tags:
                tags.append("hover")
            self.folder_tree.item(item, tags=tags)

    def clear_tree_hover(self):
        if self.hover_item:
            tags = [tag for tag in self.folder_tree.item(self.hover_item, "tags") if tag != "hover"]
            self.folder_tree.item(self.hover_item, tags=tags)
            self.hover_item = None

    def tree_item_path(self, item):
        if not item:
            return None
        tags = [tag for tag in self.folder_tree.item(item, "tags") if tag != "hover"]
        return tags[0] if tags else self.item_paths.get(item)

    def schedule_chart_redraw(self, _event=None):
        if not self.current_root:
            return
        if self.redraw_job:
            self.after_cancel(self.redraw_job)
        self.redraw_job = self.after(100, lambda: self.draw_charts(self.current_root))

    def draw_empty_charts(self):
        self.donut_canvas.delete("all")
        self.rank_canvas.delete("all")
        self.donut_canvas.create_text(220, 140, text="扫描后显示占比图", fill="#94A3B8", font=(FONT, 12, "bold"))
        self.rank_canvas.create_text(220, 140, text="扫描后显示排行图", fill="#94A3B8", font=(FONT, 12, "bold"))

    def draw_charts(self, node: FolderNode):
        self.redraw_job = None
        data = aggregate_for_charts(node.children)
        self.draw_donut(data, node.size)
        self.draw_rank(data, node.size)

    def draw_donut(self, items, total):
        canvas = self.donut_canvas
        canvas.delete("all")
        self.chart_items.clear()
        canvas.update_idletasks()
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        if not items or total <= 0:
            return
        legend_w = min(210, max(160, int(width * 0.42)))
        chart_area_w = max(180, width - legend_w - 24)
        diameter = max(180, min(260, chart_area_w - 20, height - 20))
        radius = diameter / 2
        cx, cy = 16 + radius, height / 2
        start = 90
        for idx, item in enumerate(items):
            extent = -360 * item.size / total
            tag = f"chart_{idx}"
            canvas.create_arc(cx - radius, cy - radius, cx + radius, cy + radius, start=start, extent=extent, fill=COLORS[idx % len(COLORS)], outline="#FFFFFF", width=4, tags=(tag,))
            self.chart_items[tag] = (item, item.size / total * 100)
            start += extent
        inner = radius * 0.56
        canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner, fill=CARD, outline=CARD)
        canvas.create_text(cx, cy - 7, text=human_size(total), fill=TEXT, font=(NUM_FONT, 16, "bold"))
        canvas.create_text(cx, cy + 22, text="总计", fill=MUTED, font=(FONT, 10))
        legend_x = min(width - legend_w, cx + radius + 16)
        row_h = min(28, max(22, int((height - 22) / max(len(items), 1))))
        legend_y = max(22, (height - row_h * len(items)) / 2)
        for idx, item in enumerate(items):
            y = legend_y + idx * row_h
            ratio = item.size / max(total, 1) * 100
            tag = f"chart_{idx}"
            canvas.create_oval(legend_x, y - 6, legend_x + 12, y + 6, fill=COLORS[idx % len(COLORS)], outline="", tags=(tag,))
            name_chars = max(6, int((width - legend_x - 92) / 8))
            canvas.create_text(legend_x + 22, y, anchor="w", text=ellipsize(item.name, name_chars), fill="#334155", font=(FONT, 9), tags=(tag,))
            canvas.create_text(width - 12, y, anchor="e", text=f"{ratio:.2f}%", fill=MUTED, font=(NUM_FONT, 9, "bold"), tags=(tag,))

    def draw_rank(self, items, total):
        canvas = self.rank_canvas
        canvas.delete("all")
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 1)
        row_h = 52
        max_size = max([item.size for item in items], default=1)
        for idx, item in enumerate(items):
            y = 10 + idx * row_h
            ratio = item.size / max(total, 1) * 100
            bar_w = max(6, (width - 20) * item.size / max_size)
            tag = f"rank_{idx}"
            self.chart_items[tag] = (item, ratio)
            round_rect(canvas, 10, y + 24, 10 + bar_w, y + 46, 6, fill=COLORS[idx % len(COLORS)], outline="", tags=(tag,))
            name_chars = max(8, int((width - 30) / 8))
            canvas.create_text(18, y + 10, anchor="w", text=ellipsize(item.name, name_chars), fill=TEXT, font=(FONT, 10, "bold"), tags=(tag,))
            canvas.create_text(18, y + 35, anchor="w", text=f"{human_size(item.size)}   {ratio:.2f}%", fill="#FFFFFF", font=(NUM_FONT, 10, "bold"), tags=(tag,))
        canvas.configure(scrollregion=(0, 0, width, max(1, 20 + len(items) * row_h)))

    def on_chart_motion(self, event):
        found = event.widget.find_withtag("current")
        if not found:
            self.tooltip.hide()
            return
        for tag in event.widget.gettags(found[0]):
            if tag in self.chart_items:
                item, ratio = self.chart_items[tag]
                self.tooltip.show(event.x_root, event.y_root, f"{item.name}\n大小: {human_size(item.size)}\n占比: {ratio:.2f}%")
                return
        self.tooltip.hide()

    def show_table_menu(self, event):
        row_path = None
        y = self.table_canvas.canvasy(event.y)
        for tag, path in self.table_rows:
            items = self.table_canvas.find_withtag(tag)
            if items:
                bbox = self.table_canvas.bbox(tag)
                if bbox and bbox[1] <= y <= bbox[3]:
                    row_path = path
                    break
        if row_path:
            self.context_path = row_path
            self.table_menu.tk_popup(event.x_root, event.y_root)

    def selected_table_path(self):
        return getattr(self, "context_path", None)

    def open_selected_path(self):
        path = self.selected_table_path()
        if path and os.path.exists(path):
            os.startfile(path)

    def scan_selected_result(self):
        path = self.selected_table_path()
        if path and os.path.isdir(path):
            self.set_selected_path(path)
            self.start_scan(path)

    def export_report(self):
        if not self.current_root:
            messagebox.showinfo(APP_NAME, "请先扫描一个目录。")
            return
        target = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF", "*.pdf")], initialfile="FoldView_Report.pdf")
        if not target:
            return
        try:
            self.export_pdf(target)
            messagebox.showinfo(APP_NAME, "PDF 报告已导出。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败: {exc}")

    def export_pdf(self, target):
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        except ModuleNotFoundError:
            self.export_basic_pdf(target)
            return
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        styles = getSampleStyleSheet()
        for style_name in styles.byName:
            styles[style_name].fontName = "STSong-Light"
        doc = SimpleDocTemplate(target, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
        story = [
            Paragraph("FoldView 存储分析报告", styles["Title"]),
            Paragraph(f"扫描路径：{self.current_root.path}", styles["Normal"]),
            Paragraph(f"总大小：{human_size(self.current_root.size)}；文件：{self.current_root.file_count:,}；文件夹：{self.current_root.folder_count:,}；跳过：{self.current_root.denied_count}", styles["Normal"]),
            Spacer(1, 8),
        ]
        data = [["名称", "大小", "占比", "文件数", "文件夹数", "修改日期"]]
        total = max(self.current_root.size, 1)
        for node in self.current_root.children[:80]:
            data.append([node.name, human_size(node.size), f"{node.size / total * 100:.2f}%", f"{node.file_count:,}", f"{node.folder_count:,}", format_time(node.modified)])
        table = Table(data, colWidths=[54 * mm, 26 * mm, 22 * mm, 25 * mm, 25 * mm, 34 * mm])
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2F7")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.append(table)
        doc.build(story)

    def export_basic_pdf(self, target):
        lines = [
            "FoldView Storage Analysis Report",
            f"Path: {self.current_root.path}",
            f"Total: {human_size(self.current_root.size)}",
            f"Files: {self.current_root.file_count:,}    Folders: {self.current_root.folder_count:,}    Skipped: {self.current_root.denied_count}",
            "",
            "Name | Size | Ratio | Files | Folders | Modified",
        ]
        total = max(self.current_root.size, 1)
        for node in self.current_root.children[:70]:
            lines.append(f"{node.name} | {human_size(node.size)} | {node.size / total * 100:.2f}% | {node.file_count:,} | {node.folder_count:,} | {format_time(node.modified)}")
        escaped = []
        for line in lines:
            text = line.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            escaped.append(text)
        stream_lines = ["BT", "/F1 10 Tf", "50 790 Td", "14 TL"]
        for line in escaped:
            stream_lines.append(f"({line}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        ]
        output = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(output))
            output.extend(f"{index} 0 obj\n".encode("ascii"))
            output.extend(obj)
            output.extend(b"\nendobj\n")
        xref = len(output)
        output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii"))
        Path(target).write_bytes(output)

    def load_config(self):
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data if data.get("version") == CONFIG_VERSION else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_layout(self):
        def sash(pane, index=0):
            try:
                return pane.sash_coord(index)[0 if pane.cget("orient") == tk.HORIZONTAL else 1]
            except tk.TclError:
                return None
        self.config_data["layout"] = {
            "main": [sash(self.main_pane, 0), sash(self.main_pane, 1)],
            "center": sash(self.center_pane),
            "right": sash(self.right_pane),
            "geometry": self.geometry(),
        }
        self.config_data["version"] = CONFIG_VERSION
        try:
            CONFIG_PATH.write_text(json.dumps(self.config_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def restore_layout(self):
        layout = self.config_data.get("layout", {})
        if layout.get("geometry"):
            self.geometry(layout["geometry"])
        def restore():
            main = layout.get("main")
            if isinstance(main, list) and len(main) == 2 and all(isinstance(v, int) for v in main):
                self.set_main_sashes(main[0], main[1] - main[0])
            else:
                left, center, _right = self.default_main_sizes()
                self.set_main_sashes(left, center)
            right_default = int(max(540, self.right_pane.winfo_height()) * 0.48)
            has_center_layout = isinstance(layout.get("center"), int) and layout.get("center") > 40
            for pane, key, default_value in [(self.center_pane, "center", SUMMARY_DEFAULT_H), (self.right_pane, "right", right_default)]:
                value = layout.get(key)
                if not isinstance(value, int) or value <= 40:
                    value = default_value
                if value:
                    try:
                        pane.sash_place(0, 1, value)
                    except tk.TclError:
                        pass
            self.constrain_main_pane()
            if has_center_layout:
                self.constrain_center_pane()
            else:
                try:
                    self.center_pane.sash_place(0, 1, SUMMARY_DEFAULT_H)
                except tk.TclError:
                    pass
            self._restored_layout = True
            self.schedule_chart_redraw()
        self.after(300, restore)

    def on_close(self):
        self.save_layout()
        self.destroy()


import foldview_modern
foldview_modern.install(globals())

def main():
    app = FoldViewApp()
    app.mainloop()


if __name__ == "__main__":
    main()
