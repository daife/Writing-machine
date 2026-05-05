from __future__ import annotations

import copy
from pathlib import Path
import math
import queue
import shutil
import threading
import tkinter as tk
import time
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageGrab, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None

from .gcode import save_gcode, strokes_to_gcode
from .handwriting import default_font_path, render_text
from .image_ops import (
    ensure_artifacts_dir,
    load_rgba,
    make_preview,
    perspective_transform,
    prepare_answer_layer_image,
    render_layer,
    save_png,
    transform_points_flip_h,
    transform_points_flip_v,
    transform_points_rotate_cw,
)
from .models import AnswerLayer, MachineConfig, ProjectState, ReferenceCanvas, SerialProxyConfig
from .serial_exec import GCodeRunner, SerialProxyRunner, available_ports
from .strokes import (
    MASK_MORPHOLOGY_DILATE_THEN_ERODE,
    MASK_MORPHOLOGY_ERODE_THEN_DILATE,
    Stroke,
    generate_strokes,
    load_strokes,
    save_strokes,
    save_strokes_svg,
    sort_svg_paths,
    svg_to_strokes,
)


MASK_MORPHOLOGY_ORDER_LABELS = {
    MASK_MORPHOLOGY_DILATE_THEN_ERODE: "先膨胀后腐蚀",
    MASK_MORPHOLOGY_ERODE_THEN_DILATE: "先腐蚀后膨胀",
}
MASK_MORPHOLOGY_ORDER_VALUES = {label: value for value, label in MASK_MORPHOLOGY_ORDER_LABELS.items()}


def mask_morphology_order_label(value: str) -> str:
    return MASK_MORPHOLOGY_ORDER_LABELS.get(value, MASK_MORPHOLOGY_ORDER_LABELS[MASK_MORPHOLOGY_DILATE_THEN_ERODE])


def mask_morphology_order_value(label_or_value: str) -> str:
    return MASK_MORPHOLOGY_ORDER_VALUES.get(label_or_value, label_or_value)


class PreviewCanvas(tk.Canvas):
    def __init__(self, master, **kwargs):
        super().__init__(master, bg="#f5f5f5", highlightthickness=1, highlightbackground="#ccc", **kwargs)
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

    def set_image(self, image: Image.Image | None) -> None:
        self.image = image
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        if self.image is None:
            self.create_text(20, 20, anchor="nw", text="请先导入图片", fill="#666")
            return
        canvas_w = max(10, self.winfo_width() or int(self["width"]))
        canvas_h = max(10, self.winfo_height() or int(self["height"]))
        img_w, img_h = self.image.size
        self.scale = min(canvas_w / img_w, canvas_h / img_h, 1.0)
        show_w = max(1, int(img_w * self.scale))
        show_h = max(1, int(img_h * self.scale))
        self.offset_x = (canvas_w - show_w) / 2
        self.offset_y = (canvas_h - show_h) / 2
        preview = self.image.resize((show_w, show_h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview)
        self.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.photo)
        self.create_rectangle(self.offset_x, self.offset_y, self.offset_x + show_w, self.offset_y + show_h, outline="#999")

    def canvas_to_image(self, x: float, y: float, allow_outside: bool = False) -> tuple[float, float] | None:
        if self.image is None:
            return None
        ix = (x - self.offset_x) / self.scale
        iy = (y - self.offset_y) / self.scale
        if not allow_outside and (ix < 0 or iy < 0 or ix >= self.image.width or iy >= self.image.height):
            return None
        return ix, iy

    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return self.offset_x + x * self.scale, self.offset_y + y * self.scale


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None) -> None:
        self.cancel()
        self._after_id = self.widget.after(self.delay_ms, self.show)

    def cancel(self) -> None:
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def show(self) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self._tip,
            text=self.text,
            justify="left",
            background="#ffffe8",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            wraplength=320,
        )
        label.pack()

    def hide(self, _event=None) -> None:
        self.cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class WritingMachineApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Writing Machine 控制台")
        self.root.geometry("1180x780")
        self.artifacts = ensure_artifacts_dir(Path.cwd())
        project_path = self.artifacts / "project.json"
        self.state = ProjectState.load(project_path) if project_path.exists() else ProjectState()
        self.ref_edit_image: Image.Image | None = None
        self.ref_points: list[tuple[float, float]] = []
        self.strokes: list[Stroke] = []
        self.gcode_lines: list[str] = []
        self.runner: GCodeRunner | None = None
        self.proxy_runner: SerialProxyRunner | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.proxy_log_queue: queue.Queue[str] = queue.Queue()
        self.layer_task_queue: queue.Queue[tuple] = queue.Queue()
        self.stroke_task_queue: queue.Queue[tuple] = queue.Queue()
        self.layer_import_running = False
        self.stroke_generation_running = False
        self._erase_points: list[tuple[float, float]] = []
        self.undo_stack: list[tuple[ProjectState, list[Stroke], list[str], int | None]] = []
        self.redo_stack: list[tuple[ProjectState, list[Stroke], list[str], int | None]] = []
        self.tooltips: list[ToolTip] = []

        self._build_ui()
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        self.root.bind_all("<Control-v>", self.on_global_paste)
        self.root.bind_all("<Control-V>", self.on_global_paste)
        self.sync_ui_from_state()
        self.refresh_layer_list()
        self.refresh_layers_preview()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_log_queue()

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_reference_tab()
        self._build_layers_tab()
        self._build_strokes_tab()
        self._build_text_tab()
        self._build_executor_tab()
        self._build_serial_proxy_tab()

    def _build_reference_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="1 底图设置")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        ttk.Button(left, text="导入作业纸照片", command=self.load_reference_photo).pack(fill="x", pady=3)
        ttk.Button(left, text="顺时针旋转 90°", command=self.rotate_reference).pack(fill="x", pady=3)
        ttk.Button(left, text="水平翻转", command=self.flip_reference_h).pack(fill="x", pady=3)
        ttk.Button(left, text="垂直翻转", command=self.flip_reference_v).pack(fill="x", pady=3)
        ttk.Button(left, text="清空角点", command=self.clear_reference_points).pack(fill="x", pady=3)

        width_label = ttk.Label(left, text="真实宽度 mm")
        width_label.pack(anchor="w", pady=(16, 2))
        self.width_mm_var = tk.StringVar(value="210")
        width_entry = ttk.Entry(left, textvariable=self.width_mm_var)
        width_entry.pack(fill="x")
        width_tip = "在点击“确认透视变换”时生效。用于把底图透视校正成真实纸张比例，并决定后续像素到毫米的换算。"
        self.add_tooltip(width_label, width_tip)
        self.add_tooltip(width_entry, width_tip)
        height_label = ttk.Label(left, text="真实高度 mm")
        height_label.pack(anchor="w", pady=(8, 2))
        self.height_mm_var = tk.StringVar(value="297")
        height_entry = ttk.Entry(left, textvariable=self.height_mm_var)
        height_entry.pack(fill="x")
        height_tip = "在点击“确认透视变换”时生效。用于把底图透视校正成真实纸张比例，并决定后续写字坐标的毫米尺寸。"
        self.add_tooltip(height_label, height_tip)
        self.add_tooltip(height_entry, height_tip)
        ttk.Button(left, text="确认透视变换", command=self.confirm_reference).pack(fill="x", pady=16)
        ttk.Button(left, text="保存当前底图配置", command=self.save_reference_config).pack(fill="x", pady=3)
        ttk.Button(left, text="加载底图配置", command=self.load_reference_config).pack(fill="x", pady=3)
        clear_cache_button = ttk.Button(left, text="清理缓存文件", command=self.clear_artifact_cache)
        clear_cache_button.pack(fill="x", pady=(12, 3))
        self.add_tooltip(
            clear_cache_button,
            "删除 artifacts 根目录中未被当前项目引用的临时图片、融合图、轨迹和 G-code。不会删除 project.json、底图配置目录、当前底图和当前图层图片。",
        )
        ttk.Label(
            left,
            text=(
                "画布操作：\n"
                "Ctrl+V：粘贴剪切板图片作为底图\n"
                "拖入图片文件：导入为底图\n"
                "点击画布：依次选择左上、右上、右下、左下角点"
            ),
            wraplength=220,
        ).pack(anchor="w")

        self.ref_canvas = PreviewCanvas(right, width=880, height=680)
        self.ref_canvas.pack(fill="both", expand=True)
        self.ref_canvas.bind("<Button-1>", self.on_reference_click)
        self.ref_canvas.bind("<Control-v>", self.paste_reference_from_clipboard)
        self.ref_canvas.bind("<Control-V>", self.paste_reference_from_clipboard)
        self.ref_canvas.bind("<Configure>", lambda _event: self.draw_reference_overlay())
        self.register_file_drop(self.ref_canvas, self.on_reference_drop)

    def _build_layers_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="2 字迹定位")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        self.add_layer_button = ttk.Button(left, text="导入答案图片图层", command=self.add_answer_layer)
        self.add_layer_button.pack(fill="x", pady=3)
        ttk.Button(left, text="删除当前图层", command=self.delete_current_layer).pack(fill="x", pady=3)

        self.layer_list = tk.Listbox(left, height=7, exportselection=False)
        self.layer_list.pack(fill="x", pady=4)
        self.layer_list.bind("<<ListboxSelect>>", lambda _event: self.load_layer_to_controls())
        ttk.Label(
            left,
            text=(
                "画布操作：\n"
                "Ctrl+V或win+V：粘贴剪切板图片为答案图层\n"
                "拖入图片文件：导入为答案图层\n"
                "Ctrl+Z/Ctrl+Y：撤销/重做\n"
                "左键未选中图层：选择图层\n"
                "右键按住图框内部：平移\n"
                "左键按住图框内部：橡皮擦\n"
                "左键按住顶部圆形标识：旋转（尽量不要旋转，可能破坏图像结构）\n"
                "左键按住右下蓝色方块：缩放\n"
                "左键按住红色角点：裁切"
            ),
            wraplength=230,
        ).pack(anchor="w", pady=(8, 6))

        self.layer_import_threshold_var = tk.IntVar(value=self.state.answer_import_threshold)
        self.layer_import_threshold_var.trace_add("write", lambda *_args: self.save_layer_import_threshold_from_ui())
        threshold_label = ttk.Label(left, text="二值化阈值")
        threshold_label.pack(anchor="w", pady=(7, 0))
        threshold_spin = ttk.Spinbox(left, from_=0, to=255, increment=1, textvariable=self.layer_import_threshold_var)
        threshold_spin.pack(fill="x")
        threshold_tip = "仅在导入答案图片图层时生效。灰度值低于该阈值的像素会被视为黑色字迹；提高它会过滤更浅的黑灰区域。"
        self.add_tooltip(threshold_label, threshold_tip)
        self.add_tooltip(threshold_spin, threshold_tip)
        ttk.Label(
            left,
            text="若为 AI 生成图片，建议先前往 https://bigjpg.com/ 使用 4x 和低降噪放大后再导入。",
            wraplength=230,
        ).pack(anchor="w", pady=(7, 0))

        self.layer_vars = {}
        self.layer_import_min_area_var = tk.IntVar(value=self.state.answer_import_min_area)
        self.layer_import_min_area_var.trace_add("write", lambda *_args: self.save_layer_import_min_area_from_ui())
        min_area_label = ttk.Label(left, text="导入去噪最小连通域")
        min_area_label.pack(anchor="w", pady=(7, 0))
        min_area_spin = ttk.Spinbox(left, from_=1, to=5000, increment=1, textvariable=self.layer_import_min_area_var)
        min_area_spin.pack(fill="x")
        min_area_tip = "仅在导入或粘贴答案图片图层时生效。二值化白底透明后，会一次性删除面积小于该值的黑色连通域，并保存为新的透明 PNG。"
        self.add_tooltip(min_area_label, min_area_tip)
        self.add_tooltip(min_area_spin, min_area_tip)

        eraser_label = ttk.Label(left, text="橡皮擦半径 px")
        eraser_label.pack(anchor="w", pady=(7, 0))
        self.eraser_radius_var = tk.IntVar(value=self.state.eraser_radius)
        self.eraser_radius_var.trace_add("write", lambda *_args: self.save_eraser_radius())
        eraser_spin = ttk.Spinbox(left, from_=2, to=200, increment=1, textvariable=self.eraser_radius_var)
        eraser_spin.pack(fill="x")
        eraser_tip = "在模块2画布左键拖动橡皮擦时生效。数值越大，一次擦除的透明圆形范围越大；会自动保存到项目配置。"
        self.add_tooltip(eraser_label, eraser_tip)
        self.add_tooltip(eraser_spin, eraser_tip)
        layer_progress_outer = ttk.Frame(left)
        layer_progress_outer.pack(side="bottom", fill="both", expand=True, pady=(8, 0))
        ttk.Label(layer_progress_outer, text="字迹定位输出").pack(anchor="w")
        layer_progress_frame = ttk.Frame(layer_progress_outer)
        layer_progress_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.layer_progress_text = tk.Text(layer_progress_frame, height=8, width=34, wrap="word", state="disabled")
        layer_progress_scroll = ttk.Scrollbar(layer_progress_frame, orient="vertical", command=self.layer_progress_text.yview)
        self.layer_progress_text.configure(yscrollcommand=layer_progress_scroll.set)
        self.layer_progress_text.pack(side="left", fill="both", expand=True)
        layer_progress_scroll.pack(side="right", fill="y")

        self.layers_canvas = PreviewCanvas(right, width=880, height=680)
        self.layers_canvas.pack(fill="both", expand=True)
        self.layers_canvas.bind("<Configure>", lambda _event: self.refresh_layers_preview())
        self.layers_canvas.bind("<ButtonPress-1>", self.on_layer_mouse_down)
        self.layers_canvas.bind("<B1-Motion>", self.on_layer_mouse_drag)
        self.layers_canvas.bind("<ButtonRelease-1>", self.on_layer_mouse_up)
        self.layers_canvas.bind("<ButtonPress-3>", self.on_layer_right_down)
        self.layers_canvas.bind("<B3-Motion>", self.on_layer_mouse_drag)
        self.layers_canvas.bind("<ButtonRelease-3>", self.on_layer_mouse_up)
        self.layers_canvas.bind("<Motion>", self.on_layer_mouse_move)
        self.layers_canvas.bind("<Control-v>", self.paste_layers_from_clipboard)
        self.layers_canvas.bind("<Control-V>", self.paste_layers_from_clipboard)
        self.register_file_drop(self.layers_canvas, self.on_layers_drop)
        self._layer_mouse_start: dict[str, object] | None = None

    def _add_layer_control(self, parent: ttk.Frame, label: str, key: str, min_v: float, max_v: float, increment: float) -> None:
        tooltip = ""
        if key == "min_area":
            tooltip = "仅对手动导入的答案图片图层生效。白底透明化后，会删除面积小于该值的黑色连通域，用来过滤扫描噪点和小黑点；模块4生成的字迹图不受影响。"
        label_widget = ttk.Label(parent, text=label)
        label_widget.pack(anchor="w", pady=(7, 0))
        spin = ttk.Spinbox(parent, from_=min_v, to=max_v, increment=increment, textvariable=self.layer_vars[key], command=self.update_current_layer)
        spin.pack(fill="x")
        spin.bind("<Return>", lambda _event: self.update_current_layer())
        spin.bind("<FocusOut>", lambda _event: self.update_current_layer())
        if tooltip:
            self.add_tooltip(label_widget, tooltip)
            self.add_tooltip(spin, tooltip)

    def add_tooltip(self, widget: tk.Widget, text: str) -> None:
        self.tooltips.append(ToolTip(widget, text))

    def save_eraser_radius(self) -> None:
        if not hasattr(self, "eraser_radius_var"):
            return
        try:
            self.state.eraser_radius = max(1, int(self.eraser_radius_var.get()))
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def save_layer_import_threshold_from_ui(self) -> None:
        if not hasattr(self, "layer_import_threshold_var"):
            return
        try:
            self.state.answer_import_threshold = max(0, min(255, int(self.layer_import_threshold_var.get())))
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def save_layer_import_min_area_from_ui(self) -> None:
        if not hasattr(self, "layer_import_min_area_var"):
            return
        try:
            self.state.answer_import_min_area = max(1, int(self.layer_import_min_area_var.get()))
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def save_stroke_config_from_ui(self) -> None:
        required = (
            "stroke_alpha_threshold_var",
            "trace_debug_artifacts_var",
            "mask_dilation_iterations_var",
            "mask_erosion_iterations_var",
            "mask_morphology_order_var",
            "min_component_area_var",
            "autotrace_centerline_var",
            "autotrace_background_color_var",
            "autotrace_color_count_var",
            "autotrace_corner_always_threshold_var",
            "autotrace_corner_surround_var",
            "autotrace_corner_threshold_var",
            "autotrace_despeckle_level_var",
            "autotrace_despeckle_tightness_var",
            "autotrace_dpi_var",
            "autotrace_error_threshold_var",
            "autotrace_filter_iterations_var",
            "autotrace_line_reversion_threshold_var",
            "autotrace_line_threshold_var",
            "autotrace_noise_removal_var",
            "autotrace_preserve_width_var",
            "autotrace_remove_adjacent_corners_var",
            "autotrace_tangent_surround_var",
            "autotrace_width_weight_factor_var",
            "autotrace_report_progress_var",
            "autotrace_debug_bitmap_var",
            "vpype_linemerge_tolerance_var",
            "forkclean_enabled_var",
            "forkclean_max_search_length_var",
            "forkclean_min_short_length_var",
            "forkclean_angle_threshold_var",
            "forkclean_node_tolerance_var",
            "forkclean_passes_var",
            "vpype_linesimplify_tolerance_var",
        )
        if not all(hasattr(self, name) for name in required):
            return
        try:
            self.state.stroke.trace_debug_artifacts = bool(self.trace_debug_artifacts_var.get())
            self.state.stroke.alpha_threshold = int(self.stroke_alpha_threshold_var.get())
            self.state.stroke.mask_dilation_iterations = int(self.mask_dilation_iterations_var.get())
            self.state.stroke.mask_erosion_iterations = int(self.mask_erosion_iterations_var.get())
            self.state.stroke.mask_morphology_order = mask_morphology_order_value(
                str(self.mask_morphology_order_var.get())
            )
            self.state.stroke.min_component_area = int(self.min_component_area_var.get())
            self.state.stroke.autotrace_centerline = bool(self.autotrace_centerline_var.get())
            self.state.stroke.autotrace_background_color = self.autotrace_background_color_var.get().strip()
            self.state.stroke.autotrace_color_count = int(self.autotrace_color_count_var.get())
            self.state.stroke.autotrace_corner_always_threshold = float(self.autotrace_corner_always_threshold_var.get())
            self.state.stroke.autotrace_corner_surround = int(self.autotrace_corner_surround_var.get())
            self.state.stroke.autotrace_corner_threshold = float(self.autotrace_corner_threshold_var.get())
            self.state.stroke.autotrace_despeckle_level = int(self.autotrace_despeckle_level_var.get())
            self.state.stroke.autotrace_despeckle_tightness = float(self.autotrace_despeckle_tightness_var.get())
            self.state.stroke.autotrace_dpi = int(self.autotrace_dpi_var.get())
            self.state.stroke.autotrace_error_threshold = float(self.autotrace_error_threshold_var.get())
            self.state.stroke.autotrace_filter_iterations = int(self.autotrace_filter_iterations_var.get())
            self.state.stroke.autotrace_line_reversion_threshold = float(
                self.autotrace_line_reversion_threshold_var.get()
            )
            self.state.stroke.autotrace_line_threshold = float(self.autotrace_line_threshold_var.get())
            self.state.stroke.autotrace_noise_removal = float(self.autotrace_noise_removal_var.get())
            self.state.stroke.autotrace_preserve_width = bool(self.autotrace_preserve_width_var.get())
            self.state.stroke.autotrace_remove_adjacent_corners = bool(
                self.autotrace_remove_adjacent_corners_var.get()
            )
            self.state.stroke.autotrace_tangent_surround = int(self.autotrace_tangent_surround_var.get())
            self.state.stroke.autotrace_width_weight_factor = float(self.autotrace_width_weight_factor_var.get())
            self.state.stroke.autotrace_report_progress = bool(self.autotrace_report_progress_var.get())
            self.state.stroke.autotrace_debug_bitmap = bool(self.autotrace_debug_bitmap_var.get())
            self.state.stroke.vpype_linemerge_tolerance_px = float(self.vpype_linemerge_tolerance_var.get())
            self.state.stroke.forkclean_enabled = bool(self.forkclean_enabled_var.get())
            self.state.stroke.forkclean_max_search_length_px = float(self.forkclean_max_search_length_var.get())
            self.state.stroke.forkclean_min_short_length_px = float(self.forkclean_min_short_length_var.get())
            self.state.stroke.forkclean_angle_threshold_deg = float(self.forkclean_angle_threshold_var.get())
            self.state.stroke.forkclean_node_tolerance_px = float(self.forkclean_node_tolerance_var.get())
            self.state.stroke.forkclean_passes = int(self.forkclean_passes_var.get())
            self.state.stroke.vpype_linesimplify_tolerance_px = float(self.vpype_linesimplify_tolerance_var.get())
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def save_trajectory_mode_from_ui(self) -> None:
        if not hasattr(self, "rotate_trajectory_180_var"):
            return
        try:
            self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def clear_layer_progress(self) -> None:
        if not hasattr(self, "layer_progress_text"):
            return
        self.layer_progress_text.configure(state="normal")
        self.layer_progress_text.delete("1.0", tk.END)
        self.layer_progress_text.configure(state="disabled")

    def append_layer_progress(self, message: str) -> None:
        if not hasattr(self, "layer_progress_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.layer_progress_text.configure(state="normal")
        self.layer_progress_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.layer_progress_text.see(tk.END)
        self.layer_progress_text.configure(state="disabled")

    def clear_stroke_progress(self) -> None:
        if not hasattr(self, "stroke_progress_text"):
            return
        self.stroke_progress_text.configure(state="normal")
        self.stroke_progress_text.delete("1.0", tk.END)
        self.stroke_progress_text.configure(state="disabled")

    def append_stroke_progress(self, message: str) -> None:
        if not hasattr(self, "stroke_progress_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.stroke_progress_text.configure(state="normal")
        self.stroke_progress_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.stroke_progress_text.see(tk.END)
        self.stroke_progress_text.configure(state="disabled")

    def make_undo_snapshot(self) -> tuple[ProjectState, list[Stroke], list[str], int | None]:
        return (
            copy.deepcopy(self.state),
            copy.deepcopy(self.strokes),
            list(self.gcode_lines),
            self.current_layer_index() if hasattr(self, "layer_list") else None,
        )

    def push_undo_state(self) -> None:
        self.undo_stack.append(self.make_undo_snapshot())
        if len(self.undo_stack) > 80:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def restore_undo_snapshot(self, snapshot: tuple[ProjectState, list[Stroke], list[str], int | None]) -> None:
        state, strokes, gcode_lines, selected_index = snapshot
        self.state = copy.deepcopy(state)
        self.strokes = copy.deepcopy(strokes)
        self.gcode_lines = list(gcode_lines)
        self.sync_ui_from_state(selected_index)
        self.state.save(self.artifacts / "project.json")

    def sync_ui_from_state(self, selected_index: int | None = None) -> None:
        if self.state.reference is not None:
            self.width_mm_var.set(str(self.state.reference.width_mm))
            self.height_mm_var.set(str(self.state.reference.height_mm))
            if self.state.reference.image_path and Path(self.state.reference.image_path).exists():
                self.ref_edit_image = load_rgba(self.state.reference.image_path)
                self.ref_canvas.set_image(self.ref_edit_image)
        if hasattr(self, "rotate_trajectory_180_var"):
            self.rotate_trajectory_180_var.set(self.state.rotate_trajectory_180)
        if hasattr(self, "vpype_linemerge_tolerance_var"):
            self.trace_debug_artifacts_var.set(self.state.stroke.trace_debug_artifacts)
            self.stroke_alpha_threshold_var.set(self.state.stroke.alpha_threshold)
            self.mask_dilation_iterations_var.set(self.state.stroke.mask_dilation_iterations)
            self.mask_erosion_iterations_var.set(self.state.stroke.mask_erosion_iterations)
            self.mask_morphology_order_var.set(mask_morphology_order_label(self.state.stroke.mask_morphology_order))
            self.min_component_area_var.set(self.state.stroke.min_component_area)
            self.autotrace_centerline_var.set(self.state.stroke.autotrace_centerline)
            self.autotrace_background_color_var.set(self.state.stroke.autotrace_background_color)
            self.autotrace_color_count_var.set(self.state.stroke.autotrace_color_count)
            self.autotrace_corner_always_threshold_var.set(self.state.stroke.autotrace_corner_always_threshold)
            self.autotrace_corner_surround_var.set(self.state.stroke.autotrace_corner_surround)
            self.autotrace_corner_threshold_var.set(self.state.stroke.autotrace_corner_threshold)
            self.autotrace_despeckle_level_var.set(self.state.stroke.autotrace_despeckle_level)
            self.autotrace_despeckle_tightness_var.set(self.state.stroke.autotrace_despeckle_tightness)
            self.autotrace_dpi_var.set(self.state.stroke.autotrace_dpi)
            self.autotrace_error_threshold_var.set(self.state.stroke.autotrace_error_threshold)
            self.autotrace_filter_iterations_var.set(self.state.stroke.autotrace_filter_iterations)
            self.autotrace_line_reversion_threshold_var.set(self.state.stroke.autotrace_line_reversion_threshold)
            self.autotrace_line_threshold_var.set(self.state.stroke.autotrace_line_threshold)
            self.autotrace_noise_removal_var.set(self.state.stroke.autotrace_noise_removal)
            self.autotrace_preserve_width_var.set(self.state.stroke.autotrace_preserve_width)
            self.autotrace_remove_adjacent_corners_var.set(self.state.stroke.autotrace_remove_adjacent_corners)
            self.autotrace_tangent_surround_var.set(self.state.stroke.autotrace_tangent_surround)
            self.autotrace_width_weight_factor_var.set(self.state.stroke.autotrace_width_weight_factor)
            self.autotrace_report_progress_var.set(self.state.stroke.autotrace_report_progress)
            self.autotrace_debug_bitmap_var.set(self.state.stroke.autotrace_debug_bitmap)
            self.vpype_linemerge_tolerance_var.set(self.state.stroke.vpype_linemerge_tolerance_px)
            self.forkclean_enabled_var.set(self.state.stroke.forkclean_enabled)
            self.forkclean_max_search_length_var.set(self.state.stroke.forkclean_max_search_length_px)
            self.forkclean_min_short_length_var.set(self.state.stroke.forkclean_min_short_length_px)
            self.forkclean_angle_threshold_var.set(self.state.stroke.forkclean_angle_threshold_deg)
            self.forkclean_node_tolerance_var.set(self.state.stroke.forkclean_node_tolerance_px)
            self.forkclean_passes_var.set(self.state.stroke.forkclean_passes)
            self.vpype_linesimplify_tolerance_var.set(self.state.stroke.vpype_linesimplify_tolerance_px)
        if hasattr(self, "eraser_radius_var"):
            self.eraser_radius_var.set(self.state.eraser_radius)
        if hasattr(self, "layer_import_threshold_var"):
            self.layer_import_threshold_var.set(self.state.answer_import_threshold)
        if hasattr(self, "layer_import_min_area_var"):
            self.layer_import_min_area_var.set(self.state.answer_import_min_area)
        if hasattr(self, "port_var"):
            self.port_var.set(self.state.machine.port)
            self.baud_var.set(self.state.machine.baud)
            self.invert_x_var.set(self.state.machine.invert_x)
            self.invert_y_var.set(self.state.machine.invert_y)
            self.dry_run_var.set(self.state.machine.dry_run)
        if hasattr(self, "proxy_virtual_port_var"):
            self.proxy_virtual_port_var.set(self.state.serial_proxy.virtual_port)
            self.proxy_real_port_var.set(self.state.serial_proxy.real_port)
            self.proxy_baud_var.set(self.state.serial_proxy.baud)
            self.proxy_log_path_var.set(self.state.serial_proxy.log_path)
        if hasattr(self, "text_line_width_var"):
            self.text_line_width_var.set(self.state.text_render.line_width)
            self.font_size_var.set(self.state.text_render.font_size)
            self.char_spacing_var.set(self.state.text_render.char_spacing)
            self.text_line_spacing_var.set(self.state.text_render.line_spacing)
            self.font_path_var.set(self.state.text_render.font_path or default_font_path())
            self.perturb_x_sigma_var.set(self.state.text_render.perturb_x_sigma)
            self.perturb_y_sigma_var.set(self.state.text_render.perturb_y_sigma)
            self.perturb_theta_sigma_var.set(self.state.text_render.perturb_theta_sigma)
            self.seed_var.set(self.state.text_render.seed)
        self.refresh_layer_list()
        if self.state.layers:
            index = min(max(selected_index or 0, 0), len(self.state.layers) - 1)
            self.layer_list.selection_clear(0, tk.END)
            self.layer_list.selection_set(index)
            self.layer_list.see(index)
            self.load_layer_to_controls()
        else:
            self.layer_list.selection_clear(0, tk.END)
            self.refresh_layers_preview()
        self.draw_strokes_preview()

    def undo(self, _event=None) -> str:
        if not self.undo_stack:
            return "break"
        self.redo_stack.append(self.make_undo_snapshot())
        self.restore_undo_snapshot(self.undo_stack.pop())
        return "break"

    def redo(self, _event=None) -> str:
        if not self.redo_stack:
            return "break"
        self.undo_stack.append(self.make_undo_snapshot())
        self.restore_undo_snapshot(self.redo_stack.pop())
        return "break"

    def _build_strokes_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="3 轨迹生成")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        params_frame = ttk.Frame(left)
        params_frame.pack(fill="x")
        for column in (1, 3, 5):
            params_frame.columnconfigure(column, weight=1)
        grid_row = 0

        def add_param_spin(
            label: str,
            var: tk.Variable,
            from_: float,
            to: float,
            increment: float,
            tip: str,
            width: int = 8,
        ) -> None:
            nonlocal grid_row
            row = grid_row // 3
            column = (grid_row % 3) * 2
            label_widget = ttk.Label(params_frame, text=label)
            label_widget.grid(row=row, column=column, sticky="w", padx=(0, 4), pady=2)
            spin = ttk.Spinbox(params_frame, from_=from_, to=to, increment=increment, textvariable=var, width=width)
            spin.grid(row=row, column=column + 1, sticky="ew", padx=(0, 10), pady=2)
            self.add_tooltip(label_widget, tip)
            self.add_tooltip(spin, tip)
            grid_row += 1

        def add_param_entry(label: str, var: tk.Variable, tip: str, width: int = 8) -> None:
            nonlocal grid_row
            row = grid_row // 3
            column = (grid_row % 3) * 2
            label_widget = ttk.Label(params_frame, text=label)
            label_widget.grid(row=row, column=column, sticky="w", padx=(0, 4), pady=2)
            entry = ttk.Entry(params_frame, textvariable=var, width=width)
            entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 10), pady=2)
            self.add_tooltip(label_widget, tip)
            self.add_tooltip(entry, tip)
            grid_row += 1

        def add_param_check(label: str, var: tk.BooleanVar, tip: str) -> ttk.Checkbutton:
            nonlocal grid_row
            row = grid_row // 3
            column = (grid_row % 3) * 2
            check = ttk.Checkbutton(params_frame, text=label, variable=var)
            check.grid(row=row, column=column, columnspan=2, sticky="w", padx=(0, 10), pady=2)
            self.add_tooltip(check, tip)
            grid_row += 1
            return check

        self.trace_debug_artifacts_var = tk.BooleanVar(value=self.state.stroke.trace_debug_artifacts)
        self.stroke_alpha_threshold_var = tk.IntVar(value=self.state.stroke.alpha_threshold)
        self.mask_dilation_iterations_var = tk.IntVar(value=self.state.stroke.mask_dilation_iterations)
        self.mask_erosion_iterations_var = tk.IntVar(value=self.state.stroke.mask_erosion_iterations)
        self.mask_morphology_order_var = tk.StringVar(
            value=mask_morphology_order_label(self.state.stroke.mask_morphology_order)
        )
        self.min_component_area_var = tk.IntVar(value=self.state.stroke.min_component_area)
        self.autotrace_centerline_var = tk.BooleanVar(value=self.state.stroke.autotrace_centerline)
        self.autotrace_background_color_var = tk.StringVar(value=self.state.stroke.autotrace_background_color)
        self.autotrace_color_count_var = tk.IntVar(value=self.state.stroke.autotrace_color_count)
        self.autotrace_corner_always_threshold_var = tk.DoubleVar(
            value=self.state.stroke.autotrace_corner_always_threshold
        )
        self.autotrace_corner_surround_var = tk.IntVar(value=self.state.stroke.autotrace_corner_surround)
        self.autotrace_corner_threshold_var = tk.DoubleVar(value=self.state.stroke.autotrace_corner_threshold)
        self.autotrace_despeckle_level_var = tk.IntVar(value=self.state.stroke.autotrace_despeckle_level)
        self.autotrace_despeckle_tightness_var = tk.DoubleVar(value=self.state.stroke.autotrace_despeckle_tightness)
        self.autotrace_dpi_var = tk.IntVar(value=self.state.stroke.autotrace_dpi)
        self.autotrace_error_threshold_var = tk.DoubleVar(value=self.state.stroke.autotrace_error_threshold)
        self.autotrace_filter_iterations_var = tk.IntVar(value=self.state.stroke.autotrace_filter_iterations)
        self.autotrace_line_reversion_threshold_var = tk.DoubleVar(
            value=self.state.stroke.autotrace_line_reversion_threshold
        )
        self.autotrace_line_threshold_var = tk.DoubleVar(value=self.state.stroke.autotrace_line_threshold)
        self.autotrace_noise_removal_var = tk.DoubleVar(value=self.state.stroke.autotrace_noise_removal)
        self.autotrace_preserve_width_var = tk.BooleanVar(value=self.state.stroke.autotrace_preserve_width)
        self.autotrace_remove_adjacent_corners_var = tk.BooleanVar(
            value=self.state.stroke.autotrace_remove_adjacent_corners
        )
        self.autotrace_tangent_surround_var = tk.IntVar(value=self.state.stroke.autotrace_tangent_surround)
        self.autotrace_width_weight_factor_var = tk.DoubleVar(value=self.state.stroke.autotrace_width_weight_factor)
        self.autotrace_report_progress_var = tk.BooleanVar(value=self.state.stroke.autotrace_report_progress)
        self.autotrace_debug_bitmap_var = tk.BooleanVar(value=self.state.stroke.autotrace_debug_bitmap)
        self.vpype_linemerge_tolerance_var = tk.DoubleVar(value=self.state.stroke.vpype_linemerge_tolerance_px)
        self.forkclean_enabled_var = tk.BooleanVar(value=self.state.stroke.forkclean_enabled)
        self.forkclean_max_search_length_var = tk.DoubleVar(value=self.state.stroke.forkclean_max_search_length_px)
        self.forkclean_min_short_length_var = tk.DoubleVar(value=self.state.stroke.forkclean_min_short_length_px)
        self.forkclean_angle_threshold_var = tk.DoubleVar(value=self.state.stroke.forkclean_angle_threshold_deg)
        self.forkclean_node_tolerance_var = tk.DoubleVar(value=self.state.stroke.forkclean_node_tolerance_px)
        self.forkclean_passes_var = tk.IntVar(value=self.state.stroke.forkclean_passes)
        self.vpype_linesimplify_tolerance_var = tk.DoubleVar(value=self.state.stroke.vpype_linesimplify_tolerance_px)
        add_param_check(
            "Debug 中间 SVG",
            self.trace_debug_artifacts_var,
            "打开后轨迹生成会在每个图层的 trace_layers 子目录中额外输出 trace_linemerge.svg、trace_forkclean.svg、trace_linesimplify.svg 等中间态文件。",
        )
        add_param_check(
            "AutoTrace centerline",
            self.autotrace_centerline_var,
            "传给 AutoTrace -centerline。关闭后会按轮廓追踪，通常不适合书写机中心线轨迹。",
        )
        add_param_spin(
            "Alpha 阈值",
            self.stroke_alpha_threshold_var,
            0,
            255,
            1,
            "在按图层生成轨迹时生效。每个 source_path 图层中 alpha 大于该值的像素会被视为要写的字迹区域；提高它会忽略半透明边缘。",
        )
        add_param_spin(
            "轨迹膨胀次数",
            self.mask_dilation_iterations_var,
            0,
            5,
            1,
            "在轨迹生成时生效。会把字迹 mask 向外扩一圈，可连接断裂笔画或加粗细线；次数过大会粘连相邻笔画。",
        )
        add_param_spin(
            "轨迹腐蚀次数",
            self.mask_erosion_iterations_var,
            0,
            5,
            1,
            "在轨迹生成时生效。会把字迹 mask 向内收缩，可抵消膨胀或去掉细小毛边；次数过大会吃掉细笔画。",
        )
        order_label = ttk.Label(params_frame, text="腐蚀/膨胀顺序")
        row = grid_row // 3
        column = (grid_row % 3) * 2
        order_label.grid(row=row, column=column, sticky="w", padx=(0, 4), pady=2)
        order_combo = ttk.Combobox(
            params_frame,
            values=tuple(MASK_MORPHOLOGY_ORDER_VALUES.keys()),
            textvariable=self.mask_morphology_order_var,
            state="readonly",
            width=12,
        )
        order_combo.grid(row=row, column=column + 1, sticky="ew", padx=(0, 10), pady=2)
        grid_row += 1
        order_tip = "在轨迹生成时生效。选择先膨胀后腐蚀可补小断口；选择先腐蚀后膨胀可先去毛边和孤立噪点。"
        self.add_tooltip(order_label, order_tip)
        self.add_tooltip(order_combo, order_tip)
        add_param_spin(
            "最小噪点面积(px)",
            self.min_component_area_var,
            1,
            200,
            1,
            "在送入 AutoTrace 之前生效。连通域面积小于该值的透明字迹碎点会被删除；孤立点多时调大，标点丢失时调小。",
        )
        add_param_entry(
            "AT 背景色",
            self.autotrace_background_color_var,
            "传给 AutoTrace -background-color。留空表示不指定；可填 FFFFFF 这类 6 位十六进制颜色。",
        )
        add_param_spin("AT 色彩数", self.autotrace_color_count_var, 0, 256, 1, "传给 AutoTrace -color-count。0 表示不做颜色量化。")
        add_param_spin("AT 角点必保", self.autotrace_corner_always_threshold_var, 0, 180, 1, "传给 -corner-always-threshold。")
        add_param_spin("AT 角点范围", self.autotrace_corner_surround_var, 0, 20, 1, "传给 -corner-surround。")
        add_param_spin("AT 角点阈值", self.autotrace_corner_threshold_var, 0, 180, 1, "传给 -corner-threshold。")
        add_param_spin("AT 去斑等级", self.autotrace_despeckle_level_var, 0, 20, 1, "传给 -despeckle-level。")
        add_param_spin("AT 去斑强度", self.autotrace_despeckle_tightness_var, 0, 8, 0.1, "传给 -despeckle-tightness。")
        add_param_spin("AT DPI", self.autotrace_dpi_var, 0, 1200, 1, "传给 -dpi。0 表示不传该参数。")
        add_param_spin("AT 误差阈值", self.autotrace_error_threshold_var, 0, 20, 0.1, "传给 -error-threshold。")
        add_param_spin("AT 平滑轮数", self.autotrace_filter_iterations_var, 0, 20, 1, "传给 -filter-iterations。")
        add_param_spin("AT 直线回退", self.autotrace_line_reversion_threshold_var, 0, 1, 0.01, "传给 -line-reversion-threshold。")
        add_param_spin("AT 直线阈值", self.autotrace_line_threshold_var, 0, 20, 0.1, "传给 -line-threshold。")
        add_param_spin("AT 去噪比例", self.autotrace_noise_removal_var, 0, 1, 0.01, "传给 -noise-removal。")
        add_param_spin("AT 切线范围", self.autotrace_tangent_surround_var, 0, 20, 1, "传给 -tangent-surround。")
        add_param_spin("AT 线宽权重", self.autotrace_width_weight_factor_var, 0, 30, 0.1, "传给 -width-weight-factor。")
        add_param_check("AT 保留线宽", self.autotrace_preserve_width_var, "传给 AutoTrace -preserve-width。")
        add_param_check("AT 移除相邻角", self.autotrace_remove_adjacent_corners_var, "传给 AutoTrace -remove-adjacent-corners。")
        add_param_check("AT 进度输出", self.autotrace_report_progress_var, "传给 AutoTrace -report-progress。")
        add_param_check("AT 调试位图", self.autotrace_debug_bitmap_var, "传给 AutoTrace -debug-bitmap，会输出 bitmap dump。")
        add_param_spin(
            "linemerge 容差(px)",
            self.vpype_linemerge_tolerance_var,
            0.0,
            10.0,
            0.1,
            "传给 vpype linemerge -t。用于合并距离足够近的线段端点；过大会误合并相邻笔画。",
        )
        add_param_check(
            "执行 forkclean",
            self.forkclean_enabled_var,
            "开启后在 linesimplify 前执行 forkclean 清理分叉短枝；默认关闭，避免误删细小笔画。",
        )
        add_param_spin(
            "forkclean 最大搜索(px)",
            self.forkclean_max_search_length_var,
            0.0,
            200.0,
            1.0,
            "传给 forkclean --max-search-length。达到该距离的分支会被视为长分支，用于判断岔路结构。",
        )
        add_param_spin(
            "forkclean 短枝阈值(px)",
            self.forkclean_min_short_length_var,
            0.0,
            80.0,
            0.5,
            "传给 forkclean --min-short-length。短于该值的末端短枝在满足角度条件时会被删除。",
        )
        add_param_spin(
            "forkclean 角度阈值(deg)",
            self.forkclean_angle_threshold_var,
            0.0,
            180.0,
            1.0,
            "传给 forkclean --angle-threshold。只有长分支之间的最小转角超过该值时，才删除末端短枝。",
        )
        add_param_spin(
            "forkclean 节点容差(px)",
            self.forkclean_node_tolerance_var,
            0.0,
            10.0,
            0.1,
            "传给 forkclean --node-tolerance。用于把近似重合的端点聚类成同一图节点。",
        )
        add_param_spin(
            "forkclean 执行轮数",
            self.forkclean_passes_var,
            1,
            10,
            1,
            "传给 forkclean --passes。重复清理轮数，删除一批短枝后可能暴露新的短枝。",
        )
        add_param_spin(
            "linesimplify 容差(px)",
            self.vpype_linesimplify_tolerance_var,
            0.0,
            20.0,
            0.1,
            "传给 vpype linesimplify -t。数值越大点越少、线条越简化，但可能损失笔画细节。",
        )
        for var in (
            self.trace_debug_artifacts_var,
            self.stroke_alpha_threshold_var,
            self.mask_dilation_iterations_var,
            self.mask_erosion_iterations_var,
            self.mask_morphology_order_var,
            self.min_component_area_var,
            self.autotrace_centerline_var,
            self.autotrace_background_color_var,
            self.autotrace_color_count_var,
            self.autotrace_corner_always_threshold_var,
            self.autotrace_corner_surround_var,
            self.autotrace_corner_threshold_var,
            self.autotrace_despeckle_level_var,
            self.autotrace_despeckle_tightness_var,
            self.autotrace_dpi_var,
            self.autotrace_error_threshold_var,
            self.autotrace_filter_iterations_var,
            self.autotrace_line_reversion_threshold_var,
            self.autotrace_line_threshold_var,
            self.autotrace_noise_removal_var,
            self.autotrace_preserve_width_var,
            self.autotrace_remove_adjacent_corners_var,
            self.autotrace_tangent_surround_var,
            self.autotrace_width_weight_factor_var,
            self.autotrace_report_progress_var,
            self.autotrace_debug_bitmap_var,
            self.vpype_linemerge_tolerance_var,
            self.forkclean_enabled_var,
            self.forkclean_max_search_length_var,
            self.forkclean_min_short_length_var,
            self.forkclean_angle_threshold_var,
            self.forkclean_node_tolerance_var,
            self.forkclean_passes_var,
            self.vpype_linesimplify_tolerance_var,
        ):
            var.trace_add("write", lambda *_args: self.save_stroke_config_from_ui())
        self.rotate_trajectory_180_var = tk.BooleanVar(value=self.state.rotate_trajectory_180)
        self.rotate_trajectory_180_var.trace_add("write", lambda *_args: self.save_trajectory_mode_from_ui())
        rotate_check = ttk.Checkbutton(
            left,
            text="执行时使用对角原点/XY反向",
            variable=self.rotate_trajectory_180_var,
            command=self.on_trajectory_mode_toggle,
        )
        rotate_check.pack(anchor="w", pady=8)
        self.add_tooltip(
            rotate_check,
            "在轨迹预览、G-code 生成和执行时生效。用于纸张实际旋转180度、机器从对角原点书写的场景，会同步旋转预览并镜像输出坐标。",
        )
        ttk.Button(left, text="预览轨迹输入图", command=self.preview_rotated_handwriting).pack(fill="x", pady=3)
        self.generate_strokes_button = ttk.Button(left, text="按图层生成轨迹", command=self.generate_stroke_paths)
        self.generate_strokes_button.pack(fill="x", pady=8)
        ttk.Button(left, text="加载 自定义轨迹strokes.json", command=self.load_strokes_json).pack(fill="x", pady=3)
        ttk.Button(left, text="由轨迹生成 G-code", command=self.export_gcode).pack(fill="x", pady=3)
        self.stroke_info_var = tk.StringVar(value="尚未生成轨迹")
        ttk.Label(left, textvariable=self.stroke_info_var, wraplength=240).pack(anchor="w", pady=12)
        progress_outer = ttk.Frame(left)
        progress_outer.pack(side="bottom", fill="both", expand=True, pady=(8, 0))
        ttk.Label(progress_outer, text="轨迹生成输出").pack(anchor="w")
        progress_frame = ttk.Frame(progress_outer)
        progress_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.stroke_progress_text = tk.Text(progress_frame, height=9, width=34, wrap="word", state="disabled")
        progress_scroll = ttk.Scrollbar(progress_frame, orient="vertical", command=self.stroke_progress_text.yview)
        self.stroke_progress_text.configure(yscrollcommand=progress_scroll.set)
        self.stroke_progress_text.pack(side="left", fill="both", expand=True)
        progress_scroll.pack(side="right", fill="y")

        self.stroke_canvas = PreviewCanvas(right, width=880, height=680)
        self.stroke_canvas.pack(fill="both", expand=True)
        self.stroke_canvas.bind("<Configure>", lambda _event: self.draw_strokes_preview())

    def _build_text_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="4 文本转字迹")
        top = ttk.Frame(tab)
        top.pack(side="top", fill="x", padx=8, pady=8)
        bottom = ttk.Frame(tab)
        bottom.pack(side="bottom", fill="both", expand=True, padx=8, pady=8)

        self.text_input = tk.Text(top, height=8)
        self.text_input.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.text_input.bind("<KeyRelease>", self.invalidate_generated_text)
        options = ttk.Frame(top)
        options.pack(side="right", fill="y")
        self.text_line_width_var = tk.IntVar(value=self.state.text_render.line_width)
        self.font_size_var = tk.IntVar(value=self.state.text_render.font_size)
        self.char_spacing_var = tk.IntVar(value=self.state.text_render.char_spacing)
        self.text_line_spacing_var = tk.IntVar(value=self.state.text_render.line_spacing)
        self.font_path_var = tk.StringVar(value=self.state.text_render.font_path or default_font_path())
        self.perturb_x_sigma_var = tk.DoubleVar(value=self.state.text_render.perturb_x_sigma)
        self.perturb_y_sigma_var = tk.DoubleVar(value=self.state.text_render.perturb_y_sigma)
        self.perturb_theta_sigma_var = tk.DoubleVar(value=self.state.text_render.perturb_theta_sigma)
        self.seed_var = tk.IntVar(value=self.state.text_render.seed)
        for var in (
            self.text_line_width_var,
            self.font_size_var,
            self.char_spacing_var,
            self.text_line_spacing_var,
            self.font_path_var,
            self.perturb_x_sigma_var,
            self.perturb_y_sigma_var,
            self.perturb_theta_sigma_var,
            self.seed_var,
        ):
            var.trace_add("write", lambda *_args: self.on_text_render_config_change())
        text_entry_tips = {
            "字号": "单位为像素(px)，表示字体的高度。在点击“生成手写图片”时生效。控制生成字迹的字体大小，数值越大字越大。",
            "字距 px": "在点击“生成手写图片”时生效。控制同一行相邻字符之间的额外距离，数值越大字越疏。",
            "行距 px": "在点击“生成手写图片”时生效。控制相邻两行之间的额外距离，数值越大行越疏。",
            "行宽 px": "在点击“生成手写图片”时生效。控制自动换行的最大行宽，最终图片宽度会按该值和边距自动生成。",
            "扰动 X (sigma)": "字符在水平方向上的随机偏移程度（标准差）。",
            "扰动 Y (sigma)": "字符在垂直方向上的随机偏移程度（标准差）。",
            "扰动 旋转 (sigma)": "字符旋转角度的随机偏移程度（弧度，标准差）。",
            "随机种子": "随机数生成器的种子。固定种子可以保证每次生成的字迹抖动效果一致。",
        }
        for label, var in [
            ("字号", self.font_size_var),
            ("字距 px", self.char_spacing_var),
            ("行距 px", self.text_line_spacing_var),
            ("行宽 px", self.text_line_width_var),
            ("扰动 X (sigma)", self.perturb_x_sigma_var),
            ("扰动 Y (sigma)", self.perturb_y_sigma_var),
            ("扰动 旋转 (sigma)", self.perturb_theta_sigma_var),
            ("随机种子", self.seed_var),
        ]:
            label_widget = ttk.Label(options, text=label)
            label_widget.pack(anchor="w")
            entry = ttk.Entry(options, textvariable=var, width=12)
            entry.pack(fill="x", pady=(0, 6))
            self.add_tooltip(label_widget, text_entry_tips[label])
            self.add_tooltip(entry, text_entry_tips[label])
        font_button = ttk.Button(options, text="选择字体", command=self.choose_font)
        font_button.pack(fill="x", pady=3)
        self.add_tooltip(font_button, "在点击“生成手写图片”时生效。选择用于模块4文字渲染的字体文件，不选择则使用系统默认候选字体。")
        default_font_button = ttk.Button(options, text="使用系统默认字体", command=self.use_default_font)
        default_font_button.pack(fill="x", pady=3)
        self.add_tooltip(default_font_button, "把模块4字体切回系统默认候选字体。适合在选择过自己的字体后恢复默认字体。")
        ttk.Button(options, text="生成手写图片", command=self.generate_text_image).pack(fill="x", pady=8)
        self.add_generated_text_button = ttk.Button(options, text="加入模块2", command=self.add_generated_text_to_layers, state="disabled")
        self.add_generated_text_button.pack(fill="x", pady=3)

        self.text_canvas = PreviewCanvas(bottom, width=900, height=430)
        self.text_canvas.pack(fill="both", expand=True)
        self.generated_text_path: str | None = None

    def _build_executor_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="5 执行器")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        self.port_var = tk.StringVar(value=self.state.machine.port)
        self.baud_var = tk.IntVar(value=self.state.machine.baud)
        self.invert_x_var = tk.BooleanVar(value=self.state.machine.invert_x)
        self.invert_y_var = tk.BooleanVar(value=self.state.machine.invert_y)
        self.dry_run_var = tk.BooleanVar(value=self.state.machine.dry_run)
        for var in (self.port_var, self.baud_var, self.invert_x_var, self.invert_y_var, self.dry_run_var):
            var.trace_add("write", lambda *_args: self.save_machine_config_from_ui())

        ttk.Button(left, text="刷新串口", command=self.refresh_ports).pack(fill="x", pady=3)
        self.port_combo = ttk.Combobox(left, textvariable=self.port_var, values=available_ports(), width=18)
        self.port_combo.pack(fill="x", pady=3)
        self.add_tooltip(
            self.port_combo,
            "在点击“开始”连接执行器时生效。选择写字机所在串口；点“刷新串口”可重新扫描可用端口。",
        )
        baud_label = ttk.Label(left, text="波特率")
        baud_label.pack(anchor="w")
        baud_entry = ttk.Entry(left, textvariable=self.baud_var)
        baud_entry.pack(fill="x")
        baud_tip = "在点击“开始”连接执行器时生效。必须与控制板固件串口波特率一致，常见值是115200。"
        self.add_tooltip(baud_label, baud_tip)
        self.add_tooltip(baud_entry, baud_tip)
        invert_x_check = ttk.Checkbutton(left, text="X 轴反向", variable=self.invert_x_var)
        invert_x_check.pack(anchor="w", pady=3)
        self.add_tooltip(invert_x_check, "在生成 G-code 时生效。用于机器 X 方向与预览相反的情况，会镜像 X 坐标。")
        invert_y_check = ttk.Checkbutton(left, text="Y 轴反向", variable=self.invert_y_var)
        invert_y_check.pack(anchor="w", pady=3)
        self.add_tooltip(invert_y_check, "在生成 G-code 时生效。用于机器 Y 方向与预览相反的情况，会镜像 Y 坐标。")
        diagonal_check = ttk.Checkbutton(
            left,
            text="对角原点/XY反向",
            variable=self.rotate_trajectory_180_var,
            command=self.on_trajectory_mode_toggle,
        )
        diagonal_check.pack(anchor="w", pady=3)
        self.add_tooltip(
            diagonal_check,
            "在轨迹预览、G-code 生成和执行时生效。用于纸张或机器坐标以对角为原点的场景，会让输出坐标等效旋转180度。",
        )
        dry_run_check = ttk.Checkbutton(left, text="dry-run 理论验证", variable=self.dry_run_var)
        dry_run_check.pack(anchor="w", pady=8)
        self.add_tooltip(dry_run_check, "在点击“开始”时生效。开启时只模拟发送流程并打印日志，不会真正向串口写入 G-code。")
        ttk.Button(left, text="由轨迹生成 G-code", command=self.export_gcode).pack(fill="x", pady=3)
        ttk.Button(left, text="开始", command=self.start_execution).pack(fill="x", pady=10)
        ttk.Button(left, text="暂停", command=self.pause_execution).pack(fill="x", pady=3)
        ttk.Button(left, text="继续", command=self.resume_execution).pack(fill="x", pady=3)
        ttk.Button(left, text="终止", command=self.stop_execution).pack(fill="x", pady=3)

        self.log_text = tk.Text(right, height=30, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _build_serial_proxy_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="6 串口抓包")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y", padx=8, pady=8)
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        self.proxy_virtual_port_var = tk.StringVar(value=self.state.serial_proxy.virtual_port)
        self.proxy_real_port_var = tk.StringVar(value=self.state.serial_proxy.real_port)
        self.proxy_baud_var = tk.IntVar(value=self.state.serial_proxy.baud)
        self.proxy_log_path_var = tk.StringVar(value=self.state.serial_proxy.log_path)
        for var in (
            self.proxy_virtual_port_var,
            self.proxy_real_port_var,
            self.proxy_baud_var,
            self.proxy_log_path_var,
        ):
            var.trace_add("write", lambda *_args: self.save_serial_proxy_config_from_ui())

        ttk.Button(left, text="刷新串口", command=self.refresh_proxy_ports).pack(fill="x", pady=3)
        ttk.Label(left, text="虚拟端口（官方软件连接）").pack(anchor="w", pady=(8, 0))
        self.proxy_virtual_combo = ttk.Combobox(left, textvariable=self.proxy_virtual_port_var, values=available_ports(), width=18)
        self.proxy_virtual_combo.pack(fill="x")
        self.add_tooltip(self.proxy_virtual_combo, "官方写字机软件连接的虚拟串口端。例如虚拟串口对 COM1 <-> COM2 中，官方软件连 COM1，则这里填 COM2。")

        ttk.Label(left, text="真实设备端口").pack(anchor="w", pady=(8, 0))
        self.proxy_real_combo = ttk.Combobox(left, textvariable=self.proxy_real_port_var, values=available_ports(), width=18)
        self.proxy_real_combo.pack(fill="x")
        self.add_tooltip(self.proxy_real_combo, "真实写字机所在串口。代理会把虚拟端收到的数据转发到这里，并反向转发设备响应。")

        ttk.Label(left, text="波特率").pack(anchor="w", pady=(8, 0))
        proxy_baud_entry = ttk.Entry(left, textvariable=self.proxy_baud_var)
        proxy_baud_entry.pack(fill="x")
        self.add_tooltip(proxy_baud_entry, "串口代理两端使用同一个波特率，通常要与官方软件和设备设置一致。")

        ttk.Label(left, text="日志文件").pack(anchor="w", pady=(8, 0))
        proxy_log_entry = ttk.Entry(left, textvariable=self.proxy_log_path_var)
        proxy_log_entry.pack(fill="x")
        self.add_tooltip(proxy_log_entry, "抓包日志追加写入的位置。每行包含方向、字节数、十六进制和 ASCII 预览。")
        ttk.Button(left, text="选择日志文件", command=self.choose_proxy_log_path).pack(fill="x", pady=3)

        ttk.Button(left, text="开始抓包代理", command=self.start_serial_proxy).pack(fill="x", pady=(16, 3))
        ttk.Button(left, text="停止抓包代理", command=self.stop_serial_proxy).pack(fill="x", pady=3)
        self.proxy_status_var = tk.StringVar(value="串口代理未启动")
        ttk.Label(left, textvariable=self.proxy_status_var, wraplength=240).pack(anchor="w", pady=12)

        self.proxy_log_text = tk.Text(right, height=30, wrap="word")
        self.proxy_log_text.pack(fill="both", expand=True)

    def require_reference(self) -> ReferenceCanvas | None:
        if self.state.reference is None:
            messagebox.showwarning("缺少底图", "请先在模块 1 生成参考底图。")
            return None
        return self.state.reference

    def clear_artifact_cache(self) -> None:
        if not messagebox.askyesno("清理缓存", "将删除未被当前项目引用的临时文件，但保留配置文件、底图配置、当前底图和当前图层图片。是否继续？"):
            return
        protected: set[Path] = {(self.artifacts / "project.json").resolve()}
        if self.state.reference and self.state.reference.image_path:
            protected.add(Path(self.state.reference.image_path).resolve())
        for layer in self.state.layers:
            if layer.source_path:
                protected.add(Path(layer.source_path).resolve())
        if self.generated_text_path:
            protected.add(Path(self.generated_text_path).resolve())

        deleted = 0
        skipped = 0
        deleted_names: set[str] = set()
        for path in self.artifacts.iterdir():
            try:
                if path.name == "references":
                    skipped += 1
                    continue
                if path.resolve() in protected:
                    skipped += 1
                    continue
                if path.is_file() and path.suffix.lower() in {".png", ".bmp", ".svg", ".json", ".gcode"}:
                    deleted_names.add(path.name)
                    path.unlink()
                    deleted += 1
                else:
                    skipped += 1
            except Exception as exc:
                self.log(f"缓存文件删除失败：{path}，{exc}")
        if "strokes.json" in deleted_names:
            self.strokes = []
            if hasattr(self, "stroke_info_var"):
                self.stroke_info_var.set("尚未生成轨迹")
            self.draw_strokes_preview()
        if "job.gcode" in deleted_names:
            self.gcode_lines = []
        messagebox.showinfo("清理完成", f"已删除 {deleted} 个缓存文件，保留 {skipped} 项。")

    def register_file_drop(self, widget: tk.Widget, callback) -> None:
        if DND_FILES is None or not hasattr(widget, "drop_target_register"):
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", callback)
        except Exception as exc:
            self.log(f"拖拽注册失败：{exc}")

    def dropped_files(self, event) -> list[str]:
        try:
            files = list(self.root.tk.splitlist(event.data))
        except Exception:
            files = [event.data]
        return [str(Path(item)) for item in files if str(item).strip()]

    def clipboard_image_paths(self, prefix: str) -> list[str]:
        data = ImageGrab.grabclipboard()
        if isinstance(data, Image.Image):
            filename = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
            path = self.artifacts / filename
            data.convert("RGBA").save(path)
            return [str(path)]
        if isinstance(data, (list, tuple)):
            image_paths = []
            for item in data:
                path = Path(str(item))
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"} and path.exists():
                    image_paths.append(str(path))
            return image_paths
        return []

    def paste_reference_from_clipboard(self, _event=None) -> str:
        try:
            paths = self.clipboard_image_paths("clipboard_reference")
            if not paths:
                messagebox.showwarning("剪切板无图片", "请先复制一张图片，或复制一个图片文件。")
                return "break"
            self.load_reference_photo(paths[0])
        except Exception as exc:
            messagebox.showerror("粘贴失败", str(exc))
        return "break"

    def paste_layers_from_clipboard(self, _event=None) -> str:
        try:
            paths = self.clipboard_image_paths("clipboard_layer")
            if not paths:
                messagebox.showwarning("剪切板无图片", "请先复制一张图片，或复制一个图片文件。")
                return "break"
            for path in paths:
                self.add_answer_layer(path)
        except Exception as exc:
            messagebox.showerror("粘贴失败", str(exc))
        return "break"

    def on_global_paste(self, event) -> str | None:
        focus = self.root.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
            return None
        try:
            current_tab = self.notebook.index(self.notebook.select())
        except Exception:
            return None
        if current_tab == 0:
            return self.paste_reference_from_clipboard(event)
        if current_tab == 1:
            return self.paste_layers_from_clipboard(event)
        return None

    def on_reference_drop(self, event) -> None:
        files = self.dropped_files(event)
        if files:
            self.load_reference_photo(files[0])

    def on_layers_drop(self, event) -> None:
        for path in self.dropped_files(event):
            self.add_answer_layer(path)

    def load_reference_photo(self, path: str | None = None) -> None:
        path = path or filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff")])
        if not path:
            return
        self.ref_edit_image = load_rgba(path)
        self.ref_points = []
        self.ref_canvas.set_image(self.ref_edit_image)
        self.draw_reference_overlay()

    def rotate_reference(self) -> None:
        if self.ref_edit_image is None:
            return
        width, height = self.ref_edit_image.size
        self.ref_edit_image = self.ref_edit_image.rotate(-90, expand=True)
        self.ref_points = transform_points_rotate_cw(self.ref_points, width, height)
        self.ref_canvas.set_image(self.ref_edit_image)
        self.draw_reference_overlay()

    def flip_reference_h(self) -> None:
        if self.ref_edit_image is None:
            return
        self.ref_edit_image = self.ref_edit_image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        self.ref_points = transform_points_flip_h(self.ref_points, self.ref_edit_image.width)
        self.ref_canvas.set_image(self.ref_edit_image)
        self.draw_reference_overlay()

    def flip_reference_v(self) -> None:
        if self.ref_edit_image is None:
            return
        self.ref_edit_image = self.ref_edit_image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self.ref_points = transform_points_flip_v(self.ref_points, self.ref_edit_image.height)
        self.ref_canvas.set_image(self.ref_edit_image)
        self.draw_reference_overlay()

    def clear_reference_points(self) -> None:
        self.ref_points = []
        self.draw_reference_overlay()

    def on_reference_click(self, event) -> None:
        point = self.ref_canvas.canvas_to_image(event.x, event.y)
        if point is None:
            return
        if len(self.ref_points) >= 4:
            self.ref_points = []
        self.ref_points.append(point)
        self.draw_reference_overlay()

    def draw_reference_overlay(self) -> None:
        self.ref_canvas.redraw()
        names = ["左上", "右上", "右下", "左下"]
        points_canvas = []
        for idx, point in enumerate(self.ref_points):
            cx, cy = self.ref_canvas.image_to_canvas(*point)
            points_canvas.append((cx, cy))
            self.ref_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill="#e53935", outline="")
            self.ref_canvas.create_text(cx + 8, cy - 8, text=names[idx], anchor="w", fill="#e53935")
        if len(points_canvas) > 1:
            for a, b in zip(points_canvas, points_canvas[1:]):
                self.ref_canvas.create_line(*a, *b, fill="#e53935", width=2)
        if len(points_canvas) == 4:
            self.ref_canvas.create_line(*points_canvas[-1], *points_canvas[0], fill="#e53935", width=2)

    def confirm_reference(self) -> None:
        if self.ref_edit_image is None:
            messagebox.showwarning("缺少图片", "请先导入底图照片。")
            return
        try:
            width_mm = float(self.width_mm_var.get())
            height_mm = float(self.height_mm_var.get())
            image, reference = perspective_transform(self.ref_edit_image, self.ref_points, width_mm, height_mm)
            path = self.artifacts / "reference_canvas.png"
            image.save(path)
            reference.image_path = str(path)
            self.push_undo_state()
            self.state.reference = reference
            self.state.save(self.artifacts / "project.json")
            self.refresh_layers_preview()
            messagebox.showinfo("完成", f"参考底图已保存：{path}")
        except Exception as exc:
            messagebox.showerror("透视变换失败", str(exc))

    def save_reference_config(self) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        name = simpledialog.askstring("保存底图配置", "请输入配置名称：", initialvalue="A4_作业纸")
        if not name:
            return
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name.strip())
        if not safe_name:
            messagebox.showwarning("名称无效", "配置名称不能为空。")
            return
        target_dir = self.artifacts / "references" / safe_name
        if target_dir.exists():
            if not messagebox.askyesno("配置已存在", f"`{safe_name}` 已存在，是否覆盖？\n选择“否”会自动另存为带序号的新配置。"):
                base = safe_name
                suffix = 2
                while target_dir.exists():
                    safe_name = f"{base}_{suffix}"
                    target_dir = self.artifacts / "references" / safe_name
                    suffix += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        image_target = target_dir / "reference_canvas.png"
        json_target = target_dir / "reference.json"
        try:
            if ref.image_path and Path(ref.image_path).exists():
                shutil.copy2(ref.image_path, image_target)
            saved_ref = ReferenceCanvas(
                image_path=str(image_target),
                width_mm=ref.width_mm,
                height_mm=ref.height_mm,
                width_px=ref.width_px,
                height_px=ref.height_px,
                px_per_mm=ref.px_per_mm,
            )
            temp_state = ProjectState(reference=saved_ref)
            temp_state.save(json_target)
            self.push_undo_state()
            self.state.reference = saved_ref
            self.state.save(self.artifacts / "project.json")
            if saved_ref.image_path and Path(saved_ref.image_path).exists():
                self.ref_edit_image = load_rgba(saved_ref.image_path)
                self.ref_points = []
                self.ref_canvas.set_image(self.ref_edit_image)
            self.refresh_layers_preview()
            messagebox.showinfo("完成", f"底图配置已保存并切换为当前配置：{json_target}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def load_reference_config(self) -> None:
        refs_dir = self.artifacts / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        path = filedialog.askopenfilename(
            initialdir=refs_dir,
            filetypes=[("Reference config", "*.json")],
            title="选择底图配置 JSON",
        )
        if not path:
            return
        try:
            loaded = ProjectState.load(path)
            if loaded.reference is None:
                raise ValueError("配置文件中没有 reference 信息。")
            self.push_undo_state()
            self.state.reference = loaded.reference
            if self.state.layers and messagebox.askyesno("切换底图", "是否清空当前答案图层？不同底图尺寸可能导致旧图层错位。"):
                self.state.layers.clear()
                self.refresh_layer_list()
            self.state.save(self.artifacts / "project.json")
            if self.state.reference.image_path and Path(self.state.reference.image_path).exists():
                self.ref_edit_image = load_rgba(self.state.reference.image_path)
                self.ref_points = []
                self.ref_canvas.set_image(self.ref_edit_image)
            self.refresh_layers_preview()
            messagebox.showinfo("完成", f"已加载底图配置：{path}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def initial_import_scale(self, image: Image.Image, ref: ReferenceCanvas) -> float:
        max_width = max(1.0, ref.width_px * 0.85)
        max_height = max(1.0, ref.height_px * 0.85)
        if image.width <= max_width and image.height <= max_height:
            return 1.0
        return max(0.05, min(max_width / max(1, image.width), max_height / max(1, image.height)))

    def set_layer_import_running(self, running: bool) -> None:
        self.layer_import_running = running
        if hasattr(self, "add_layer_button"):
            self.add_layer_button.configure(state="disabled" if running else "normal")

    def import_answer_layer_worker(
        self,
        path: str,
        ref: ReferenceCanvas,
        threshold: int,
        min_area: int,
    ) -> None:
        try:
            self.layer_task_queue.put(("progress", f"开始导入答案图层：{path}"))
            image = load_rgba(path)
            self.layer_task_queue.put(("progress", f"已读取图片：{image.width} x {image.height}px,正在进一步处理..."))
            processed = prepare_answer_layer_image(image, threshold=threshold, min_area=min_area)
            self.layer_task_queue.put(("progress", f"已二值化、去噪并裁边：{processed.width} x {processed.height}px"))
            stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in Path(path).stem.strip()) or "layer"
            filename = f"imported_layer_{time.strftime('%Y%m%d_%H%M%S')}_{stem}.png"
            source_path = save_png(processed, self.artifacts / filename)
            scale = self.initial_import_scale(processed, ref)
            layer = AnswerLayer(source_path=source_path, x=ref.width_px * 0.05, y=ref.height_px * 0.05)
            layer.scale = scale
            layer.already_extracted = True
            layer.min_area = min_area
            self.layer_task_queue.put(("done", layer, f"导入完成：{Path(source_path).name}，初始缩放 {scale:.3f}x"))
        except Exception as exc:
            self.layer_task_queue.put(("error", str(exc)))

    def add_answer_layer(self, path: str | None = None, preserve_transparency: bool = False) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        path = path or filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff")])
        if not path:
            return
        source_path = path
        if preserve_transparency:
            layer = AnswerLayer(source_path=source_path, x=ref.width_px * 0.05, y=ref.height_px * 0.05)
            layer.scale = self.initial_import_scale(load_rgba(source_path), ref)
            layer.already_extracted = True
            layer.min_area = 1
            self.push_undo_state()
            self.state.layers.append(layer)
            self.refresh_layer_list()
            self.layer_list.selection_clear(0, tk.END)
            self.layer_list.selection_set(len(self.state.layers) - 1)
            self.load_layer_to_controls()
            self.refresh_layers_preview()
            return
        if self.layer_import_running:
            self.append_layer_progress("已有答案图层正在导入，请等待完成后再导入下一张。")
            return
        try:
            threshold = int(self.layer_import_threshold_var.get()) if hasattr(self, "layer_import_threshold_var") else 245
            min_area = int(self.layer_import_min_area_var.get()) if hasattr(self, "layer_import_min_area_var") else 5
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("导入失败", f"导入参数无效：{exc}")
            return
        self.clear_layer_progress()
        self.set_layer_import_running(True)
        thread = threading.Thread(
            target=self.import_answer_layer_worker,
            args=(path, copy.deepcopy(ref), threshold, min_area),
            daemon=True,
        )
        thread.start()

    def refresh_layer_list(self) -> None:
        self.layer_list.delete(0, tk.END)
        for index, layer in enumerate(self.state.layers, start=1):
            self.layer_list.insert(tk.END, f"{index}. {Path(layer.source_path).name}")

    def current_layer_index(self) -> int | None:
        selection = self.layer_list.curselection()
        if not selection:
            return None
        return int(selection[0])

    def current_layer(self) -> AnswerLayer | None:
        index = self.current_layer_index()
        if index is None or index >= len(self.state.layers):
            return None
        return self.state.layers[index]

    def load_layer_to_controls(self) -> None:
        layer = self.current_layer()
        if layer is None:
            return
        for key, var in self.layer_vars.items():
            var.set(getattr(layer, key))
        self.refresh_layers_preview()

    def update_current_layer(self) -> None:
        layer = self.current_layer()
        if layer is None:
            return
        changed = False
        new_values: dict[str, float | int] = {}
        for key, var in self.layer_vars.items():
            value = var.get()
            if key.startswith("crop") or key in {"min_area"}:
                value = int(value)
            new_values[key] = value
            if getattr(layer, key) != value:
                changed = True
        if not changed:
            return
        self.push_undo_state()
        for key, value in new_values.items():
            setattr(layer, key, value)
        self.state.save(self.artifacts / "project.json")
        self.refresh_layers_preview()

    def sync_layer_controls(self, layer: AnswerLayer) -> None:
        for key, var in self.layer_vars.items():
            value = getattr(layer, key)
            if isinstance(var, tk.IntVar):
                var.set(int(round(value)))
            else:
                var.set(round(float(value), 3))

    def delete_current_layer(self) -> None:
        index = self.current_layer_index()
        if index is None:
            return
        self.push_undo_state()
        del self.state.layers[index]
        self.refresh_layer_list()
        self.refresh_layers_preview()

    def refresh_layers_preview(self) -> None:
        ref = self.state.reference
        if ref is None:
            self.layers_canvas.set_image(None)
            return
        try:
            self.layers_canvas.set_image(make_preview(ref, self.state.layers))
            self.draw_layer_selection_overlay()
        except Exception as exc:
            self.log(f"图层预览失败：{exc}")

    def selected_layer_bbox(self) -> tuple[float, float, float, float] | None:
        layer = self.current_layer()
        if layer is None or not layer.enabled:
            return None
        try:
            rendered = render_layer(layer)
        except Exception:
            return None
        return (layer.x, layer.y, layer.x + rendered.width, layer.y + rendered.height)

    def layer_bbox(self, layer: AnswerLayer) -> tuple[float, float, float, float] | None:
        if not layer.enabled:
            return None
        try:
            rendered = render_layer(layer)
        except Exception:
            return None
        return (layer.x, layer.y, layer.x + rendered.width, layer.y + rendered.height)

    def layer_needs_crop_bake(self, layer: AnswerLayer) -> bool:
        return abs(layer.rotation_deg) > 0.001

    def bake_current_layer_for_crop(self, layer: AnswerLayer) -> None:
        index = self.current_layer_index()
        if index is None:
            return
        rendered = render_layer(layer)
        target = self.artifacts / f"baked_layer_{index + 1}_{int(time.time() * 1000)}.png"
        rendered.save(target)
        layer.source_path = str(target)
        layer.scale = 1.0
        layer.rotation_deg = 0.0
        layer.crop_left = 0
        layer.crop_top = 0
        layer.crop_right = 0
        layer.crop_bottom = 0
        layer.already_extracted = True

    def draw_layer_selection_overlay(self) -> None:
        self.layers_canvas.delete("layer_overlay")
        bbox = self.selected_layer_bbox()
        if bbox is None:
            return
        x1, y1, x2, y2 = bbox
        c1 = self.layers_canvas.image_to_canvas(x1, y1)
        c2 = self.layers_canvas.image_to_canvas(x2, y2)
        self.layers_canvas.create_rectangle(*c1, *c2, outline="#e53935", width=2, dash=(5, 3), tags="layer_overlay")
        handles = [
            c1,
            (c2[0], c1[1]),
            c2,
            (c1[0], c2[1]),
            ((c1[0] + c2[0]) / 2, c1[1]),
            (c2[0], (c1[1] + c2[1]) / 2),
            ((c1[0] + c2[0]) / 2, c2[1]),
            (c1[0], (c1[1] + c2[1]) / 2),
        ]
        for hx, hy in handles:
            self.layers_canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="#e53935", outline="", tags="layer_overlay")
        top_mid = ((c1[0] + c2[0]) / 2, c1[1])
        rotate_handle = (top_mid[0], top_mid[1] - 28)
        self.layers_canvas.create_line(*top_mid, *rotate_handle, fill="#e53935", width=2, tags="layer_overlay")
        self.layers_canvas.create_oval(
            rotate_handle[0] - 7,
            rotate_handle[1] - 7,
            rotate_handle[0] + 7,
            rotate_handle[1] + 7,
            outline="#e53935",
            width=2,
            tags="layer_overlay",
        )
        scale_handle = (c2[0] + 18, c2[1] + 18)
        self.layers_canvas.create_line(c2[0], c2[1], *scale_handle, fill="#1565c0", width=2, tags="layer_overlay")
        self.layers_canvas.create_rectangle(
            scale_handle[0] - 8,
            scale_handle[1] - 8,
            scale_handle[0] + 8,
            scale_handle[1] + 8,
            fill="#1565c0",
            outline="#ffffff",
            width=2,
            tags="layer_overlay",
        )
        self.layers_canvas.create_text(
            c1[0],
            c1[1] - 18,
            text="右键移动 / 左键擦除 / 拖动角点裁切",
            anchor="sw",
            fill="#e53935",
            tags="layer_overlay",
        )

    def draw_live_box(self, corners: list[tuple[float, float]], label: str = "") -> None:
        self.layers_canvas.delete("live_overlay")
        canvas_points = [self.layers_canvas.image_to_canvas(x, y) for x, y in corners]
        flat = [coord for point in canvas_points for coord in point]
        self.layers_canvas.create_polygon(*flat, outline="#ff1744", fill="", width=2, dash=(4, 2), tags="live_overlay")
        for x, y in canvas_points:
            self.layers_canvas.create_rectangle(x - 4, y - 4, x + 4, y + 4, fill="#ff1744", outline="", tags="live_overlay")
        if label:
            self.layers_canvas.create_text(canvas_points[0][0], canvas_points[0][1] - 12, anchor="sw", text=label, fill="#ff1744", tags="live_overlay")

    def bbox_corners(self, bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
        x1, y1, x2, y2 = bbox
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    def rotate_corners(
        self,
        corners: list[tuple[float, float]],
        center: tuple[float, float],
        angle_deg: float,
    ) -> list[tuple[float, float]]:
        angle = math.radians(angle_deg)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        cx, cy = center
        rotated = []
        for x, y in corners:
            dx = x - cx
            dy = y - cy
            rotated.append((cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a))
        return rotated

    def scaled_corners(
        self,
        bbox: tuple[float, float, float, float],
        factor: float,
    ) -> list[tuple[float, float]]:
        x1, y1, x2, y2 = bbox
        return [(x1, y1), (x1 + (x2 - x1) * factor, y1), (x1 + (x2 - x1) * factor, y1 + (y2 - y1) * factor), (x1, y1 + (y2 - y1) * factor)]

    def hit_test_layer(self, x: float, y: float) -> int | None:
        for index in range(len(self.state.layers) - 1, -1, -1):
            bbox = self.layer_bbox(self.state.layers[index])
            if bbox is None:
                continue
            x1, y1, x2, y2 = bbox
            if x1 <= x <= x2 and y1 <= y <= y2:
                return index
        return None

    def select_layer_index(self, index: int) -> None:
        self.layer_list.selection_clear(0, tk.END)
        self.layer_list.selection_set(index)
        self.layer_list.see(index)
        self.load_layer_to_controls()

    def nearest_crop_corner(self, point: tuple[float, float], bbox: tuple[float, float, float, float]) -> str:
        x, y = point
        x1, y1, x2, y2 = bbox
        distances = {
            "tl": math.hypot(x - x1, y - y1),
            "tr": math.hypot(x - x2, y - y1),
            "br": math.hypot(x - x2, y - y2),
            "bl": math.hypot(x - x1, y - y2),
        }
        return min(distances, key=distances.get)

    def is_near_canvas_point(self, event_x: float, event_y: float, image_point: tuple[float, float], radius: float = 18) -> bool:
        cx, cy = self.layers_canvas.image_to_canvas(*image_point)
        return math.hypot(event_x - cx, event_y - cy) <= radius

    def rotate_handle_point(self, bbox: tuple[float, float, float, float]) -> tuple[float, float]:
        x1, y1, x2, _y2 = bbox
        return ((x1 + x2) / 2, y1 - 28 / max(self.layers_canvas.scale, 0.001))

    def scale_handle_point(self, bbox: tuple[float, float, float, float]) -> tuple[float, float]:
        _x1, _y1, x2, y2 = bbox
        return (x2 + 18 / max(self.layers_canvas.scale, 0.001), y2 + 18 / max(self.layers_canvas.scale, 0.001))

    def bbox_contains(self, bbox: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
        x1, y1, x2, y2 = bbox
        return x1 <= point[0] <= x2 and y1 <= point[1] <= y2

    def near_crop_corner(self, event_x: float, event_y: float, bbox: tuple[float, float, float, float], radius: float = 14) -> str | None:
        x1, y1, x2, y2 = bbox
        corners = {"tl": (x1, y1), "tr": (x2, y1), "br": (x2, y2), "bl": (x1, y2)}
        for name, point in corners.items():
            if self.is_near_canvas_point(event_x, event_y, point, radius):
                return name
        return None

    def on_layer_mouse_down(self, event) -> None:
        point = self.layers_canvas.canvas_to_image(event.x, event.y, allow_outside=True)
        if point is None:
            return
        hit = self.hit_test_layer(point[0], point[1])
        current_index = self.current_layer_index()
        if hit is not None and hit != current_index:
            self.select_layer_index(hit)
            self._layer_mouse_start = None
            return
        layer = self.current_layer()
        if layer is None:
            self._layer_mouse_start = None
            return
        bbox = self.selected_layer_bbox()
        if bbox is None:
            self._layer_mouse_start = None
            return
        center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        mode = None
        if self.is_near_canvas_point(event.x, event.y, self.rotate_handle_point(bbox), radius=22):
            mode = "rotate"
        elif self.is_near_canvas_point(event.x, event.y, self.scale_handle_point(bbox), radius=18):
            mode = "scale"
        else:
            crop_corner = self.near_crop_corner(event.x, event.y, bbox)
            if crop_corner:
                mode = "crop"
            elif self.bbox_contains(bbox, point):
                mode = "erase"

        if mode is None:
            self._layer_mouse_start = None
            return
        undo_pushed = False
        if mode == "crop" and self.layer_needs_crop_bake(layer):
            self.push_undo_state()
            undo_pushed = True
            self.bake_current_layer_for_crop(layer)
            self.sync_layer_controls(layer)
            bbox = self.selected_layer_bbox()
            point = self.layers_canvas.canvas_to_image(event.x, event.y, allow_outside=True)
            if bbox is None or point is None:
                self._layer_mouse_start = None
                return
        dx = point[0] - center[0]
        dy = point[1] - center[1]
        mouse_angle = math.degrees(math.atan2(dy, dx))
        mouse_distance = max(1.0, math.hypot(dx, dy))
        self._erase_points = [point] if mode == "erase" else []
        self._layer_mouse_start = {
            "mode": mode,
            "point": point,
            "bbox": bbox,
            "corners": self.bbox_corners(bbox),
            "x": layer.x,
            "y": layer.y,
            "scale": layer.scale,
            "rotation_deg": layer.rotation_deg,
            "crop_left": layer.crop_left,
            "crop_top": layer.crop_top,
            "crop_right": layer.crop_right,
            "crop_bottom": layer.crop_bottom,
            "center": center,
            "angle": mouse_angle,
            "distance": mouse_distance,
            "corner": self.near_crop_corner(event.x, event.y, bbox) or self.nearest_crop_corner(point, bbox),
        }
        if not undo_pushed:
            self.push_undo_state()
        if mode == "erase":
            self.draw_eraser_preview(point)

    def on_layer_right_down(self, event) -> None:
        point = self.layers_canvas.canvas_to_image(event.x, event.y, allow_outside=True)
        if point is None:
            return
        layer = self.current_layer()
        bbox = self.selected_layer_bbox()
        if layer is None or bbox is None or not self.bbox_contains(bbox, point):
            self._layer_mouse_start = None
            return
        center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        self._layer_mouse_start = {
            "mode": "drag",
            "point": point,
            "bbox": bbox,
            "corners": self.bbox_corners(bbox),
            "x": layer.x,
            "y": layer.y,
            "scale": layer.scale,
            "rotation_deg": layer.rotation_deg,
            "crop_left": layer.crop_left,
            "crop_top": layer.crop_top,
            "crop_right": layer.crop_right,
            "crop_bottom": layer.crop_bottom,
            "center": center,
            "angle": 0.0,
            "distance": 1.0,
            "corner": "br",
        }
        self.push_undo_state()

    def on_layer_mouse_drag(self, event) -> None:
        layer = self.current_layer()
        if layer is None or self._layer_mouse_start is None:
            return
        point = self.layers_canvas.canvas_to_image(event.x, event.y, allow_outside=True)
        if point is None:
            return
        start = self._layer_mouse_start
        mode = str(start["mode"])
        start_point = start["point"]  # type: ignore[assignment]
        sx, sy = start_point  # type: ignore[misc]
        dx = point[0] - sx
        dy = point[1] - sy
        bbox = start["bbox"]  # type: ignore[assignment]
        x1, y1, x2, y2 = bbox  # type: ignore[misc]
        center = start["center"]  # type: ignore[assignment]
        cx, cy = center  # type: ignore[misc]

        if mode == "drag":
            layer.x = float(start["x"]) + dx
            layer.y = float(start["y"]) + dy
            self.draw_live_box([(x + dx, y + dy) for x, y in start["corners"]], "平移")  # type: ignore[index]
        elif mode == "rotate":
            current_angle = math.degrees(math.atan2(point[1] - cy, point[0] - cx))
            delta_angle = current_angle - float(start["angle"])
            layer.rotation_deg = float(start["rotation_deg"]) + delta_angle
            while layer.rotation_deg > 180:
                layer.rotation_deg -= 360
            while layer.rotation_deg < -180:
                layer.rotation_deg += 360
            self.draw_live_box(self.rotate_corners(start["corners"], (cx, cy), delta_angle), f"{layer.rotation_deg:.1f}°")  # type: ignore[arg-type,index]
        elif mode == "scale":
            original_width = max(1.0, float(bbox[2]) - float(bbox[0]))  # type: ignore[index]
            original_height = max(1.0, float(bbox[3]) - float(bbox[1]))  # type: ignore[index]
            factor_x = (point[0] - float(bbox[0])) / original_width  # type: ignore[index]
            factor_y = (point[1] - float(bbox[1])) / original_height  # type: ignore[index]
            factor = max(0.05, min(5.0, (factor_x + factor_y) / 2))
            layer.scale = max(0.05, min(5.0, float(start["scale"]) * factor))
            actual_factor = layer.scale / max(0.001, float(start["scale"]))
            self.draw_live_box(self.scaled_corners(bbox, actual_factor), f"{layer.scale:.2f}x")  # type: ignore[arg-type]
        elif mode == "crop":
            corner = str(start["corner"])
            factor = max(0.05, float(start["scale"]))
            start_crop_left = int(start["crop_left"])
            start_crop_top = int(start["crop_top"])
            start_crop_right = int(start["crop_right"])
            start_crop_bottom = int(start["crop_bottom"])
            if "l" in corner:
                layer.crop_left = max(0, int(round(start_crop_left + dx / factor)))
            if "r" in corner:
                layer.crop_right = max(0, int(round(start_crop_right - dx / factor)))
            if "t" in corner:
                layer.crop_top = max(0, int(round(start_crop_top + dy / factor)))
            if "b" in corner:
                layer.crop_bottom = max(0, int(round(start_crop_bottom - dy / factor)))
            delta_left = layer.crop_left - start_crop_left
            delta_top = layer.crop_top - start_crop_top
            delta_right = layer.crop_right - start_crop_right
            delta_bottom = layer.crop_bottom - start_crop_bottom
            layer.x = float(start["x"]) + delta_left * factor
            layer.y = float(start["y"]) + delta_top * factor
            x1 = float(bbox[0]) + delta_left * factor  # type: ignore[index]
            y1 = float(bbox[1]) + delta_top * factor  # type: ignore[index]
            x2 = float(bbox[2]) - delta_right * factor  # type: ignore[index]
            y2 = float(bbox[3]) - delta_bottom * factor  # type: ignore[index]
            self.draw_live_box(self.bbox_corners((x1, y1, x2, y2)), "裁切")
        elif mode == "erase":
            self._erase_points.append(point)
            self.draw_eraser_preview(point)

        self.sync_layer_controls(layer)

    def on_layer_mouse_up(self, _event) -> None:
        if self._layer_mouse_start is not None:
            if self._layer_mouse_start.get("mode") == "erase":
                self.commit_layer_erase()
            self.state.save(self.artifacts / "project.json")
        self._layer_mouse_start = None
        self._erase_points = []
        self.layers_canvas.delete("live_overlay")
        self.refresh_layers_preview()

    def on_layer_mouse_move(self, event) -> None:
        point = self.layers_canvas.canvas_to_image(event.x, event.y, allow_outside=True)
        bbox = self.selected_layer_bbox()
        if point is not None and bbox is not None and self.bbox_contains(bbox, point):
            self.draw_eraser_preview(point, preview_only=True)
        else:
            self.layers_canvas.delete("eraser_preview")

    def draw_eraser_preview(self, point: tuple[float, float], preview_only: bool = False) -> None:
        if preview_only:
            self.layers_canvas.delete("eraser_preview")
        radius = max(1, int(self.eraser_radius_var.get()))
        cx, cy = self.layers_canvas.image_to_canvas(*point)
        scaled_radius = radius * self.layers_canvas.scale
        tag = "eraser_preview" if preview_only else "live_overlay"
        self.layers_canvas.create_oval(
            cx - scaled_radius,
            cy - scaled_radius,
            cx + scaled_radius,
            cy + scaled_radius,
            outline="#00a6ff",
            width=2,
            tags=tag,
        )

    def current_layer_source_image(self, layer: AnswerLayer) -> tuple[Image.Image, tuple[int, int, int, int]]:
        image = load_rgba(layer.source_path)
        width, height = image.size
        left = max(0, min(width - 1, layer.crop_left))
        top = max(0, min(height - 1, layer.crop_top))
        right_crop = max(0, min(width - left - 1, layer.crop_right))
        bottom_crop = max(0, min(height - top - 1, layer.crop_bottom))
        crop_box = (left, top, width - right_crop, height - bottom_crop)
        return image.crop(crop_box), crop_box

    def transformed_layer_offset(
        self,
        layer: AnswerLayer,
        source_size: tuple[int, int],
        bbox: tuple[int, int, int, int],
    ) -> tuple[float, float]:
        width, height = source_size
        scale = max(0.001, float(layer.scale))
        scaled_width = width * scale
        scaled_height = height * scale
        angle = math.radians(-float(layer.rotation_deg))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        center = (scaled_width / 2.0, scaled_height / 2.0)

        def rotate_scaled(x: float, y: float) -> tuple[float, float]:
            sx = x * scale
            sy = y * scale
            dx = sx - center[0]
            dy = sy - center[1]
            return (
                center[0] + dx * cos_a - dy * sin_a,
                center[1] + dx * sin_a + dy * cos_a,
            )

        image_corners = [
            rotate_scaled(0, 0),
            rotate_scaled(width, 0),
            rotate_scaled(width, height),
            rotate_scaled(0, height),
        ]
        min_image_x = min(x for x, _y in image_corners)
        min_image_y = min(y for _x, y in image_corners)
        left, top, right, bottom = bbox
        bbox_corners = [
            rotate_scaled(left, top),
            rotate_scaled(right, top),
            rotate_scaled(right, bottom),
            rotate_scaled(left, bottom),
        ]
        return (
            min(x for x, _y in bbox_corners) - min_image_x,
            min(y for _x, y in bbox_corners) - min_image_y,
        )

    def reference_point_to_layer_source(
        self,
        layer: AnswerLayer,
        point: tuple[float, float],
        source_size: tuple[int, int],
    ) -> tuple[float, float]:
        width, height = source_size
        scale = max(0.001, float(layer.scale))
        scaled_width = width * scale
        scaled_height = height * scale
        angle = math.radians(-float(layer.rotation_deg))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        center = (scaled_width / 2.0, scaled_height / 2.0)

        def rotate_scaled(x: float, y: float) -> tuple[float, float]:
            dx = x - center[0]
            dy = y - center[1]
            return (
                center[0] + dx * cos_a - dy * sin_a,
                center[1] + dx * sin_a + dy * cos_a,
            )

        image_corners = [
            rotate_scaled(0, 0),
            rotate_scaled(scaled_width, 0),
            rotate_scaled(scaled_width, scaled_height),
            rotate_scaled(0, scaled_height),
        ]
        min_image_x = min(x for x, _y in image_corners)
        min_image_y = min(y for _x, y in image_corners)
        sx = point[0] - float(layer.x) + min_image_x
        sy = point[1] - float(layer.y) + min_image_y
        dx = sx - center[0]
        dy = sy - center[1]
        unrotated_x = center[0] + dx * cos_a + dy * sin_a
        unrotated_y = center[1] - dx * sin_a + dy * cos_a
        return (unrotated_x / scale, unrotated_y / scale)

    def commit_layer_erase(self) -> None:
        layer = self.current_layer()
        index = self.current_layer_index()
        if layer is None or index is None or not self._erase_points:
            return
        source, _crop_box = self.current_layer_source_image(layer)
        alpha = source.getchannel("A")
        draw = ImageDraw.Draw(alpha)
        radius = max(1.0, float(self.eraser_radius_var.get()) / max(0.001, float(layer.scale)))
        for point in self._erase_points:
            x, y = self.reference_point_to_layer_source(layer, point, source.size)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        source.putalpha(alpha)
        bbox = alpha.getbbox()
        if bbox:
            offset_x, offset_y = self.transformed_layer_offset(layer, source.size, bbox)
            source = source.crop(bbox)
            new_x = float(layer.x) + offset_x
            new_y = float(layer.y) + offset_y
        else:
            source = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            new_x, new_y = layer.x, layer.y
        path = self.artifacts / f"erased_layer_{index + 1}.png"
        source.save(path)
        layer.source_path = str(path)
        layer.x = float(new_x)
        layer.y = float(new_y)
        layer.crop_left = layer.crop_top = layer.crop_right = layer.crop_bottom = 0
        layer.already_extracted = True
        self.sync_layer_controls(layer)

    def transform_layer_strokes(
        self,
        layer: AnswerLayer,
        strokes: list[Stroke],
        source_size: tuple[int, int],
    ) -> list[Stroke]:
        width, height = source_size
        scale = max(0.001, float(layer.scale))
        scaled_width = width * scale
        scaled_height = height * scale
        angle = math.radians(-float(layer.rotation_deg))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        center = (scaled_width / 2.0, scaled_height / 2.0)

        def rotate_scaled(x: float, y: float) -> tuple[float, float]:
            sx = x * scale
            sy = y * scale
            dx = sx - center[0]
            dy = sy - center[1]
            return (
                center[0] + dx * cos_a - dy * sin_a,
                center[1] + dx * sin_a + dy * cos_a,
            )

        image_corners = [
            rotate_scaled(0, 0),
            rotate_scaled(width, 0),
            rotate_scaled(width, height),
            rotate_scaled(0, height),
        ]
        min_image_x = min(x for x, _y in image_corners)
        min_image_y = min(y for _x, y in image_corners)

        transformed: list[Stroke] = []
        for stroke in strokes:
            points = []
            for x, y in stroke.points:
                rx, ry = rotate_scaled(x, y)
                points.append((float(layer.x) + rx - min_image_x, float(layer.y) + ry - min_image_y))
            transformed.append(Stroke(points=points, closed=stroke.closed))
        return transformed

    def set_stroke_generation_running(self, running: bool) -> None:
        self.stroke_generation_running = running
        if hasattr(self, "generate_strokes_button"):
            self.generate_strokes_button.configure(state="disabled" if running else "normal")

    def generate_stroke_paths_worker(
        self,
        ref: ReferenceCanvas,
        layers: list[AnswerLayer],
        stroke_config,
    ) -> None:
        def progress(message: str) -> None:
            self.stroke_task_queue.put(("progress", message))

        try:
            merged_strokes: list[Stroke] = []
            trace_layers_dir = self.artifacts / "trace_layers"
            trace_layers_dir.mkdir(parents=True, exist_ok=True)
            for layer_index, layer in enumerate(layers, start=1):
                image, _crop_box = self.current_layer_source_image(layer)
                progress(f"图层 {layer_index}：正在读取 source_path：{layer.source_path}")
                layer_strokes = generate_strokes(
                    image,
                    alpha_threshold=stroke_config.alpha_threshold,
                    mask_dilation_iterations=stroke_config.mask_dilation_iterations,
                    mask_erosion_iterations=stroke_config.mask_erosion_iterations,
                    mask_morphology_order=stroke_config.mask_morphology_order,
                    min_component_area=stroke_config.min_component_area,
                    trace_debug_artifacts=stroke_config.trace_debug_artifacts,
                    autotrace_centerline=stroke_config.autotrace_centerline,
                    autotrace_background_color=stroke_config.autotrace_background_color,
                    autotrace_color_count=stroke_config.autotrace_color_count,
                    autotrace_corner_always_threshold=stroke_config.autotrace_corner_always_threshold,
                    autotrace_corner_surround=stroke_config.autotrace_corner_surround,
                    autotrace_corner_threshold=stroke_config.autotrace_corner_threshold,
                    autotrace_despeckle_level=stroke_config.autotrace_despeckle_level,
                    autotrace_despeckle_tightness=stroke_config.autotrace_despeckle_tightness,
                    autotrace_dpi=stroke_config.autotrace_dpi,
                    autotrace_error_threshold=stroke_config.autotrace_error_threshold,
                    autotrace_filter_iterations=stroke_config.autotrace_filter_iterations,
                    autotrace_line_reversion_threshold=stroke_config.autotrace_line_reversion_threshold,
                    autotrace_line_threshold=stroke_config.autotrace_line_threshold,
                    autotrace_noise_removal=stroke_config.autotrace_noise_removal,
                    autotrace_preserve_width=stroke_config.autotrace_preserve_width,
                    autotrace_remove_adjacent_corners=stroke_config.autotrace_remove_adjacent_corners,
                    autotrace_tangent_surround=stroke_config.autotrace_tangent_surround,
                    autotrace_width_weight_factor=stroke_config.autotrace_width_weight_factor,
                    autotrace_report_progress=stroke_config.autotrace_report_progress,
                    autotrace_debug_bitmap=stroke_config.autotrace_debug_bitmap,
                    vpype_linemerge_tolerance_px=stroke_config.vpype_linemerge_tolerance_px,
                    forkclean_enabled=stroke_config.forkclean_enabled,
                    forkclean_max_search_length_px=stroke_config.forkclean_max_search_length_px,
                    forkclean_min_short_length_px=stroke_config.forkclean_min_short_length_px,
                    forkclean_angle_threshold_deg=stroke_config.forkclean_angle_threshold_deg,
                    forkclean_node_tolerance_px=stroke_config.forkclean_node_tolerance_px,
                    forkclean_passes=stroke_config.forkclean_passes,
                    vpype_linesimplify_tolerance_px=stroke_config.vpype_linesimplify_tolerance_px,
                    sort_paths=False,
                    work_dir=trace_layers_dir / f"layer_{layer_index:03d}",
                    progress_callback=lambda message, index=layer_index: progress(f"图层 {index}：{message}"),
                )
                transformed = self.transform_layer_strokes(layer, layer_strokes, image.size)
                merged_strokes.extend(transformed)
                progress(f"图层 {layer_index}：已生成并变换 {len(transformed)} 条 stroke。")

            if not merged_strokes:
                self.stroke_task_queue.put(("empty", "没有生成任何轨迹，流程已停止。"))
                return
            merged_svg = self.artifacts / "trace_merged_unsorted.svg"
            sorted_svg = self.artifacts / "trace_optimized.svg"
            progress("正在合并所有图层 SVG，尚未 linesort...")
            save_strokes_svg(merged_strokes, ref.width_px, ref.height_px, merged_svg)
            sort_svg_paths(merged_svg, sorted_svg, progress_callback=progress)
            progress("正在解析 linesort 后的 SVG 折线为轨迹点...")
            strokes = svg_to_strokes(sorted_svg)
            progress("正在保存 strokes.json...")
            save_strokes(strokes, ref.width_px, ref.height_px, self.artifacts / "strokes.json")
            points = sum(len(stroke.points) for stroke in strokes)
            self.stroke_task_queue.put(
                ("done", strokes, points, f"轨迹生成完成：{len(strokes)} 条 stroke，共 {points} 个点。")
            )
        except Exception as exc:
            self.stroke_task_queue.put(("error", str(exc)))

    def generate_stroke_paths(self) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        if self.stroke_generation_running:
            self.append_stroke_progress("轨迹正在生成中，请等待当前任务完成。")
            return
        self.clear_stroke_progress()
        enabled_layers = [layer for layer in self.state.layers if layer.enabled]
        self.append_stroke_progress("开始按图层从 source_path 生成轨迹。")
        if not enabled_layers:
            self.append_stroke_progress("没有启用的答案图层，轨迹生成已停止。")
            return
        try:
            self.push_undo_state()
            self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            self.state.stroke.trace_debug_artifacts = bool(self.trace_debug_artifacts_var.get())
            self.state.stroke.alpha_threshold = int(self.stroke_alpha_threshold_var.get())
            self.state.stroke.mask_dilation_iterations = int(self.mask_dilation_iterations_var.get())
            self.state.stroke.mask_erosion_iterations = int(self.mask_erosion_iterations_var.get())
            self.state.stroke.mask_morphology_order = mask_morphology_order_value(
                str(self.mask_morphology_order_var.get())
            )
            self.state.stroke.min_component_area = int(self.min_component_area_var.get())
            self.state.stroke.autotrace_centerline = bool(self.autotrace_centerline_var.get())
            self.state.stroke.autotrace_background_color = self.autotrace_background_color_var.get().strip()
            self.state.stroke.autotrace_color_count = int(self.autotrace_color_count_var.get())
            self.state.stroke.autotrace_corner_always_threshold = float(self.autotrace_corner_always_threshold_var.get())
            self.state.stroke.autotrace_corner_surround = int(self.autotrace_corner_surround_var.get())
            self.state.stroke.autotrace_corner_threshold = float(self.autotrace_corner_threshold_var.get())
            self.state.stroke.autotrace_despeckle_level = int(self.autotrace_despeckle_level_var.get())
            self.state.stroke.autotrace_despeckle_tightness = float(self.autotrace_despeckle_tightness_var.get())
            self.state.stroke.autotrace_dpi = int(self.autotrace_dpi_var.get())
            self.state.stroke.autotrace_error_threshold = float(self.autotrace_error_threshold_var.get())
            self.state.stroke.autotrace_filter_iterations = int(self.autotrace_filter_iterations_var.get())
            self.state.stroke.autotrace_line_reversion_threshold = float(
                self.autotrace_line_reversion_threshold_var.get()
            )
            self.state.stroke.autotrace_line_threshold = float(self.autotrace_line_threshold_var.get())
            self.state.stroke.autotrace_noise_removal = float(self.autotrace_noise_removal_var.get())
            self.state.stroke.autotrace_preserve_width = bool(self.autotrace_preserve_width_var.get())
            self.state.stroke.autotrace_remove_adjacent_corners = bool(
                self.autotrace_remove_adjacent_corners_var.get()
            )
            self.state.stroke.autotrace_tangent_surround = int(self.autotrace_tangent_surround_var.get())
            self.state.stroke.autotrace_width_weight_factor = float(self.autotrace_width_weight_factor_var.get())
            self.state.stroke.autotrace_report_progress = bool(self.autotrace_report_progress_var.get())
            self.state.stroke.autotrace_debug_bitmap = bool(self.autotrace_debug_bitmap_var.get())
            self.state.stroke.vpype_linemerge_tolerance_px = float(self.vpype_linemerge_tolerance_var.get())
            self.state.stroke.forkclean_enabled = bool(self.forkclean_enabled_var.get())
            self.state.stroke.forkclean_max_search_length_px = float(self.forkclean_max_search_length_var.get())
            self.state.stroke.forkclean_min_short_length_px = float(self.forkclean_min_short_length_var.get())
            self.state.stroke.forkclean_angle_threshold_deg = float(self.forkclean_angle_threshold_var.get())
            self.state.stroke.forkclean_node_tolerance_px = float(self.forkclean_node_tolerance_var.get())
            self.state.stroke.forkclean_passes = int(self.forkclean_passes_var.get())
            self.state.stroke.vpype_linesimplify_tolerance_px = float(self.vpype_linesimplify_tolerance_var.get())
            self.state.save(self.artifacts / "project.json")
            self.set_stroke_generation_running(True)
            thread = threading.Thread(
                target=self.generate_stroke_paths_worker,
                args=(copy.deepcopy(ref), copy.deepcopy(enabled_layers), copy.deepcopy(self.state.stroke)),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            self.append_stroke_progress(f"轨迹生成失败：{exc}")
            self.set_stroke_generation_running(False)
            messagebox.showerror("轨迹生成失败", str(exc))

    def preview_rotated_handwriting(self) -> None:
        ref = self.state.reference
        if ref is None:
            self.stroke_canvas.set_image(None)
            return
        try:
            self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            preview = make_preview(ref, self.state.layers)
            if self.state.rotate_trajectory_180:
                preview = preview.rotate(180, expand=False)
                draw = ImageDraw.Draw(preview)
                draw.rectangle((8, 8, 450, 42), fill=(255, 255, 255, 220), outline=(220, 0, 0, 255))
                draw.text((16, 16), "Diagonal origin mode preview: paper view rotated 180 deg.", fill=(220, 0, 0, 255))
            self.stroke_canvas.set_image(preview)
            self.state.save(self.artifacts / "project.json")
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc))

    def refresh_trajectory_preview(self) -> None:
        if self.strokes:
            self.draw_strokes_preview()
            self.state.save(self.artifacts / "project.json")
        else:
            self.preview_rotated_handwriting()

    def on_trajectory_mode_toggle(self) -> None:
        self.push_undo_state()
        self.refresh_trajectory_preview()

    def load_strokes_json(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Stroke JSON", "*.json")])
        if not path:
            return
        try:
            self.strokes, _width, _height = load_strokes(path)
            self.stroke_info_var.set(f"已加载 {len(self.strokes)} 条 stroke。")
            self.draw_strokes_preview()
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def draw_strokes_preview(self) -> None:
        ref = self.state.reference
        if ref is None:
            self.stroke_canvas.set_image(None)
            return
        if ref.image_path and Path(ref.image_path).exists():
            image = load_rgba(ref.image_path)
        else:
            image = Image.new("RGBA", (ref.width_px, ref.height_px), (255, 255, 255, 255))
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for stroke in self.strokes:
            if len(stroke.points) >= 2:
                draw.line(stroke.points, fill=(220, 0, 0, 220), width=2)
        preview = Image.alpha_composite(image, overlay)
        self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
        if self.state.rotate_trajectory_180:
            preview = preview.rotate(180, expand=False)
            draw = ImageDraw.Draw(preview)
            draw.rectangle((8, 8, 470, 42), fill=(255, 255, 255, 220), outline=(220, 0, 0, 255))
            draw.text((16, 16), "Diagonal origin mode result: paper and strokes rotated 180 deg.", fill=(220, 0, 0, 255))
        self.stroke_canvas.set_image(preview)

    def choose_font(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Fonts", "*.ttf;*.ttc;*.otf")])
        if path:
            self.font_path_var.set(path)

    def use_default_font(self) -> None:
        self.font_path_var.set(default_font_path())

    def invalidate_generated_text(self, _event=None) -> None:
        self.generated_text_path = None
        if hasattr(self, "add_generated_text_button"):
            self.add_generated_text_button.configure(state="disabled")

    def on_text_render_config_change(self) -> None:
        self.invalidate_generated_text()
        if not hasattr(self, "text_line_width_var"):
            return
        try:
            self.state.text_render.line_width = max(1, int(self.text_line_width_var.get()))
            self.state.text_render.font_size = max(1, int(self.font_size_var.get()))
            self.state.text_render.char_spacing = max(0, int(self.char_spacing_var.get()))
            self.state.text_render.line_spacing = max(0, int(self.text_line_spacing_var.get()))
            self.state.text_render.font_path = self.font_path_var.get()
            self.state.text_render.perturb_x_sigma = float(self.perturb_x_sigma_var.get())
            self.state.text_render.perturb_y_sigma = float(self.perturb_y_sigma_var.get())
            self.state.text_render.perturb_theta_sigma = float(self.perturb_theta_sigma_var.get())
            self.state.text_render.seed = int(self.seed_var.get())
            self.state.save(self.artifacts / "project.json")
        except (tk.TclError, ValueError):
            return

    def generate_text_image(self) -> None:
        text = self.text_input.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("缺少文本", "请输入要生成的文本。")
            return
        try:
            self.state.text_render.line_width = max(1, int(self.text_line_width_var.get()))
            self.state.text_render.font_size = max(1, int(self.font_size_var.get()))
            self.state.text_render.char_spacing = max(0, int(self.char_spacing_var.get()))
            self.state.text_render.line_spacing = max(0, int(self.text_line_spacing_var.get()))
            self.state.text_render.font_path = self.font_path_var.get()
            self.state.text_render.perturb_x_sigma = float(self.perturb_x_sigma_var.get())
            self.state.text_render.perturb_y_sigma = float(self.perturb_y_sigma_var.get())
            self.state.text_render.perturb_theta_sigma = float(self.perturb_theta_sigma_var.get())
            self.state.text_render.seed = int(self.seed_var.get())

            filename = f"handright_output_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
            path = render_text(
                text=text,
                output_path=self.artifacts / filename,
                font_path=self.font_path_var.get(),
                font_size=self.state.text_render.font_size,
                char_spacing=self.state.text_render.char_spacing,
                line_spacing=self.state.text_render.line_spacing,
                line_width=self.state.text_render.line_width,
                perturb_x_sigma=self.state.text_render.perturb_x_sigma,
                perturb_y_sigma=self.state.text_render.perturb_y_sigma,
                perturb_theta_sigma=self.state.text_render.perturb_theta_sigma,
                seed=self.state.text_render.seed,
            )
            self.generated_text_path = path
            self.text_canvas.set_image(load_rgba(path))
            self.add_generated_text_button.configure(state="normal")
            self.state.save(self.artifacts / "project.json")
            messagebox.showinfo("完成", f"手写图片已保存：{path}")
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))

    def add_generated_text_to_layers(self) -> None:
        if not self.generated_text_path or not Path(self.generated_text_path).exists():
            messagebox.showwarning("缺少图片", "请先点击“生成手写图片”。")
            return
        self.add_answer_layer(self.generated_text_path, preserve_transparency=True)
        self.notebook.select(1)

    def _machine_config_from_ui(self) -> MachineConfig:
        config = self.state.machine
        config.port = self.port_var.get()
        config.baud = int(self.baud_var.get())
        config.dry_run = bool(self.dry_run_var.get())
        config.invert_x = bool(self.invert_x_var.get())
        config.invert_y = bool(self.invert_y_var.get())
        self.state.machine = config
        self.state.save(self.artifacts / "project.json")
        return config

    def save_machine_config_from_ui(self) -> None:
        if not hasattr(self, "port_var"):
            return
        try:
            self._machine_config_from_ui()
        except (tk.TclError, ValueError):
            return

    def serial_proxy_config_from_ui(self) -> SerialProxyConfig:
        config = self.state.serial_proxy
        config.virtual_port = self.proxy_virtual_port_var.get()
        config.real_port = self.proxy_real_port_var.get()
        config.baud = int(self.proxy_baud_var.get())
        config.log_path = self.proxy_log_path_var.get().strip() or "logs/serial_log.txt"
        self.state.serial_proxy = config
        self.state.save(self.artifacts / "project.json")
        return config

    def save_serial_proxy_config_from_ui(self) -> None:
        if not hasattr(self, "proxy_virtual_port_var"):
            return
        try:
            self.serial_proxy_config_from_ui()
        except (tk.TclError, ValueError):
            return

    def refresh_proxy_ports(self) -> None:
        ports = available_ports()
        self.proxy_virtual_combo["values"] = ports
        self.proxy_real_combo["values"] = ports

    def choose_proxy_log_path(self) -> None:
        path = filedialog.asksaveasfilename(
            initialfile=Path(self.proxy_log_path_var.get() or "logs/serial_log.txt").name,
            defaultextension=".txt",
            filetypes=[("Text log", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.proxy_log_path_var.set(path)

    def proxy_log(self, message: str) -> None:
        self.proxy_log_text.insert("end", message + "\n")
        self.proxy_log_text.see("end")

    def start_serial_proxy(self) -> None:
        if self.proxy_runner and self.proxy_runner.is_running():
            messagebox.showinfo("串口代理", "串口代理已经在运行。")
            return
        try:
            config = self.serial_proxy_config_from_ui()
            self.proxy_runner = SerialProxyRunner(
                config,
                on_log=lambda msg: self.proxy_log_queue.put(msg),
                on_done=lambda ok, msg: self.proxy_log_queue.put(("完成：" if ok else "失败：") + msg),
            )
            self.proxy_runner.start()
            self.proxy_status_var.set("串口代理运行中")
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def stop_serial_proxy(self) -> None:
        if self.proxy_runner:
            self.proxy_runner.stop()
        self.proxy_status_var.set("正在停止串口代理...")

    def on_serial_proxy_done(self, ok: bool, message: str) -> None:
        self.proxy_log(message)
        self.proxy_status_var.set("串口代理已停止" if ok else "串口代理失败")

    def refresh_ports(self) -> None:
        self.port_combo["values"] = available_ports()

    def export_gcode(self) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        if not self.strokes:
            strokes_path = self.artifacts / "strokes.json"
            if strokes_path.exists():
                self.strokes, _width, _height = load_strokes(strokes_path)
            else:
                messagebox.showwarning("缺少轨迹", "请先在模块 3 生成轨迹。")
                return
        config = self._machine_config_from_ui()
        self.gcode_lines = strokes_to_gcode(
            self.strokes,
            ref,
            config,
            diagonal_origin_180=bool(self.rotate_trajectory_180_var.get()),
        )
        path = save_gcode(self.gcode_lines, self.artifacts / "job.gcode")
        self.log(f"G-code 已生成：{path}，共 {len(self.gcode_lines)} 行。")

    def start_execution(self) -> None:
        if not self.gcode_lines:
            self.export_gcode()
        if not self.gcode_lines:
            return
        config = self._machine_config_from_ui()
        self.runner = GCodeRunner(
            self.gcode_lines,
            config,
            on_log=lambda msg: self.log_queue.put(msg),
            on_done=lambda ok, msg: self.log_queue.put(("完成：" if ok else "失败：") + msg),
            dry_run=bool(self.dry_run_var.get()),
        )
        self.runner.start()

    def pause_execution(self) -> None:
        if self.runner:
            self.runner.pause()

    def resume_execution(self) -> None:
        if self.runner:
            self.runner.resume()

    def stop_execution(self) -> None:
        if self.runner:
            self.runner.stop()

    def log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                self.log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                message = self.proxy_log_queue.get_nowait()
                self.proxy_log(message)
                if message.startswith("完成：") or message.startswith("失败："):
                    self.proxy_status_var.set("串口代理已停止" if message.startswith("完成：") else "串口代理失败")
        except queue.Empty:
            pass
        self._poll_layer_task_queue()
        self._poll_stroke_task_queue()
        self.root.after(100, self._poll_log_queue)

    def _poll_layer_task_queue(self) -> None:
        try:
            while True:
                item = self.layer_task_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self.append_layer_progress(item[1])
                elif kind == "done":
                    _kind, layer, message = item
                    self.push_undo_state()
                    self.state.layers.append(layer)
                    self.state.save(self.artifacts / "project.json")
                    self.refresh_layer_list()
                    self.layer_list.selection_clear(0, tk.END)
                    self.layer_list.selection_set(len(self.state.layers) - 1)
                    self.load_layer_to_controls()
                    self.refresh_layers_preview()
                    self.append_layer_progress(message)
                    self.set_layer_import_running(False)
                elif kind == "error":
                    self.append_layer_progress(f"导入失败：{item[1]}")
                    self.set_layer_import_running(False)
                    messagebox.showerror("导入失败", item[1])
        except queue.Empty:
            pass

    def _poll_stroke_task_queue(self) -> None:
        try:
            while True:
                item = self.stroke_task_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self.append_stroke_progress(item[1])
                elif kind == "done":
                    _kind, strokes, points, message = item
                    self.strokes = strokes
                    self.stroke_info_var.set(f"已生成 {len(self.strokes)} 条 stroke，共 {points} 个点。")
                    self.draw_strokes_preview()
                    self.append_stroke_progress(message)
                    self.set_stroke_generation_running(False)
                    messagebox.showinfo("完成", "轨迹已保存到 artifacts/strokes.json")
                elif kind == "empty":
                    self.append_stroke_progress(item[1])
                    self.set_stroke_generation_running(False)
                elif kind == "error":
                    self.append_stroke_progress(f"轨迹生成失败：{item[1]}")
                    self.set_stroke_generation_running(False)
                    messagebox.showerror("轨迹生成失败", item[1])
        except queue.Empty:
            pass

    def on_close(self) -> None:
        try:
            if self.proxy_runner:
                self.proxy_runner.stop()
            if hasattr(self, "port_var"):
                self._machine_config_from_ui()
            if hasattr(self, "proxy_virtual_port_var"):
                self.serial_proxy_config_from_ui()
            if hasattr(self, "rotate_trajectory_180_var"):
                self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            if hasattr(self, "trace_debug_artifacts_var"):
                self.state.stroke.trace_debug_artifacts = bool(self.trace_debug_artifacts_var.get())
            if hasattr(self, "stroke_alpha_threshold_var"):
                self.state.stroke.alpha_threshold = int(self.stroke_alpha_threshold_var.get())
            if hasattr(self, "mask_dilation_iterations_var"):
                self.state.stroke.mask_dilation_iterations = int(self.mask_dilation_iterations_var.get())
            if hasattr(self, "mask_erosion_iterations_var"):
                self.state.stroke.mask_erosion_iterations = int(self.mask_erosion_iterations_var.get())
            if hasattr(self, "mask_morphology_order_var"):
                self.state.stroke.mask_morphology_order = mask_morphology_order_value(
                    str(self.mask_morphology_order_var.get())
                )
            if hasattr(self, "min_component_area_var"):
                self.state.stroke.min_component_area = int(self.min_component_area_var.get())
            if hasattr(self, "autotrace_centerline_var"):
                self.state.stroke.autotrace_centerline = bool(self.autotrace_centerline_var.get())
            if hasattr(self, "autotrace_background_color_var"):
                self.state.stroke.autotrace_background_color = self.autotrace_background_color_var.get().strip()
            if hasattr(self, "autotrace_color_count_var"):
                self.state.stroke.autotrace_color_count = int(self.autotrace_color_count_var.get())
            if hasattr(self, "autotrace_corner_always_threshold_var"):
                self.state.stroke.autotrace_corner_always_threshold = float(
                    self.autotrace_corner_always_threshold_var.get()
                )
            if hasattr(self, "autotrace_corner_surround_var"):
                self.state.stroke.autotrace_corner_surround = int(self.autotrace_corner_surround_var.get())
            if hasattr(self, "autotrace_corner_threshold_var"):
                self.state.stroke.autotrace_corner_threshold = float(self.autotrace_corner_threshold_var.get())
            if hasattr(self, "autotrace_despeckle_level_var"):
                self.state.stroke.autotrace_despeckle_level = int(self.autotrace_despeckle_level_var.get())
            if hasattr(self, "autotrace_despeckle_tightness_var"):
                self.state.stroke.autotrace_despeckle_tightness = float(self.autotrace_despeckle_tightness_var.get())
            if hasattr(self, "autotrace_dpi_var"):
                self.state.stroke.autotrace_dpi = int(self.autotrace_dpi_var.get())
            if hasattr(self, "autotrace_error_threshold_var"):
                self.state.stroke.autotrace_error_threshold = float(self.autotrace_error_threshold_var.get())
            if hasattr(self, "autotrace_filter_iterations_var"):
                self.state.stroke.autotrace_filter_iterations = int(self.autotrace_filter_iterations_var.get())
            if hasattr(self, "autotrace_line_reversion_threshold_var"):
                self.state.stroke.autotrace_line_reversion_threshold = float(
                    self.autotrace_line_reversion_threshold_var.get()
                )
            if hasattr(self, "autotrace_line_threshold_var"):
                self.state.stroke.autotrace_line_threshold = float(self.autotrace_line_threshold_var.get())
            if hasattr(self, "autotrace_noise_removal_var"):
                self.state.stroke.autotrace_noise_removal = float(self.autotrace_noise_removal_var.get())
            if hasattr(self, "autotrace_preserve_width_var"):
                self.state.stroke.autotrace_preserve_width = bool(self.autotrace_preserve_width_var.get())
            if hasattr(self, "autotrace_remove_adjacent_corners_var"):
                self.state.stroke.autotrace_remove_adjacent_corners = bool(
                    self.autotrace_remove_adjacent_corners_var.get()
                )
            if hasattr(self, "autotrace_tangent_surround_var"):
                self.state.stroke.autotrace_tangent_surround = int(self.autotrace_tangent_surround_var.get())
            if hasattr(self, "autotrace_width_weight_factor_var"):
                self.state.stroke.autotrace_width_weight_factor = float(self.autotrace_width_weight_factor_var.get())
            if hasattr(self, "autotrace_report_progress_var"):
                self.state.stroke.autotrace_report_progress = bool(self.autotrace_report_progress_var.get())
            if hasattr(self, "autotrace_debug_bitmap_var"):
                self.state.stroke.autotrace_debug_bitmap = bool(self.autotrace_debug_bitmap_var.get())
            if hasattr(self, "vpype_linemerge_tolerance_var"):
                self.state.stroke.vpype_linemerge_tolerance_px = float(self.vpype_linemerge_tolerance_var.get())
            if hasattr(self, "forkclean_enabled_var"):
                self.state.stroke.forkclean_enabled = bool(self.forkclean_enabled_var.get())
            if hasattr(self, "forkclean_max_search_length_var"):
                self.state.stroke.forkclean_max_search_length_px = float(self.forkclean_max_search_length_var.get())
            if hasattr(self, "forkclean_min_short_length_var"):
                self.state.stroke.forkclean_min_short_length_px = float(self.forkclean_min_short_length_var.get())
            if hasattr(self, "forkclean_angle_threshold_var"):
                self.state.stroke.forkclean_angle_threshold_deg = float(self.forkclean_angle_threshold_var.get())
            if hasattr(self, "forkclean_node_tolerance_var"):
                self.state.stroke.forkclean_node_tolerance_px = float(self.forkclean_node_tolerance_var.get())
            if hasattr(self, "forkclean_passes_var"):
                self.state.stroke.forkclean_passes = int(self.forkclean_passes_var.get())
            if hasattr(self, "vpype_linesimplify_tolerance_var"):
                self.state.stroke.vpype_linesimplify_tolerance_px = float(self.vpype_linesimplify_tolerance_var.get())
            if hasattr(self, "eraser_radius_var"):
                self.state.eraser_radius = max(1, int(self.eraser_radius_var.get()))
            if hasattr(self, "layer_import_min_area_var"):
                self.state.answer_import_min_area = max(1, int(self.layer_import_min_area_var.get()))
            if hasattr(self, "text_line_width_var"):
                self.state.text_render.line_width = max(1, int(self.text_line_width_var.get()))
            if hasattr(self, "font_size_var"):
                self.state.text_render.font_size = max(1, int(self.font_size_var.get()))
            if hasattr(self, "char_spacing_var"):
                self.state.text_render.char_spacing = max(0, int(self.char_spacing_var.get()))
            if hasattr(self, "text_line_spacing_var"):
                self.state.text_render.line_spacing = max(0, int(self.text_line_spacing_var.get()))
            if hasattr(self, "font_path_var"):
                self.state.text_render.font_path = self.font_path_var.get()
            if hasattr(self, "perturb_x_sigma_var"):
                self.state.text_render.perturb_x_sigma = float(self.perturb_x_sigma_var.get())
            if hasattr(self, "perturb_y_sigma_var"):
                self.state.text_render.perturb_y_sigma = float(self.perturb_y_sigma_var.get())
            if hasattr(self, "perturb_theta_sigma_var"):
                self.state.text_render.perturb_theta_sigma = float(self.perturb_theta_sigma_var.get())
            if hasattr(self, "seed_var"):
                self.state.text_render.seed = int(self.seed_var.get())
            self.state.save(self.artifacts / "project.json")
        finally:
            self.root.destroy()


def run() -> None:
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = WritingMachineApp(root)
    root.mainloop()
