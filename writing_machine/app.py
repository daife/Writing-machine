from __future__ import annotations

import copy
from pathlib import Path
import math
import queue
import shutil
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
    composite_layers,
    ensure_artifacts_dir,
    load_rgba,
    make_preview,
    perspective_transform,
    render_layer,
    save_png,
    transform_points_flip_h,
    transform_points_flip_v,
    transform_points_rotate_cw,
)
from .models import AnswerLayer, MachineConfig, ProjectState, ReferenceCanvas
from .serial_exec import GCodeRunner, available_ports
from .strokes import Stroke, generate_strokes, load_strokes, save_strokes


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
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._erase_points: list[tuple[float, float]] = []
        self.undo_stack: list[tuple[ProjectState, list[Stroke], list[str], int | None]] = []
        self.redo_stack: list[tuple[ProjectState, list[Stroke], list[str], int | None]] = []

        self._build_ui()
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        self.root.bind_all("<Control-v>", self.on_global_paste)
        self.root.bind_all("<Control-V>", self.on_global_paste)
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

        ttk.Label(left, text="真实宽度 mm").pack(anchor="w", pady=(16, 2))
        self.width_mm_var = tk.StringVar(value="210")
        ttk.Entry(left, textvariable=self.width_mm_var).pack(fill="x")
        ttk.Label(left, text="真实高度 mm").pack(anchor="w", pady=(8, 2))
        self.height_mm_var = tk.StringVar(value="297")
        ttk.Entry(left, textvariable=self.height_mm_var).pack(fill="x")
        ttk.Button(left, text="确认透视变换", command=self.confirm_reference).pack(fill="x", pady=16)
        ttk.Button(left, text="保存当前底图配置", command=self.save_reference_config).pack(fill="x", pady=3)
        ttk.Button(left, text="加载底图配置", command=self.load_reference_config).pack(fill="x", pady=3)
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
        left_outer = ttk.Frame(tab)
        left_outer.pack(side="left", fill="y", padx=8, pady=8)
        left_canvas = tk.Canvas(left_outer, width=260, highlightthickness=0)
        left_scrollbar = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left = ttk.Frame(left_canvas)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_canvas.pack(side="left", fill="y", expand=False)
        left_scrollbar.pack(side="right", fill="y")

        def update_left_scroll(_event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
            left_canvas.itemconfigure(left_window, width=left_canvas.winfo_width())

        left.bind("<Configure>", update_left_scroll)
        left_canvas.bind("<Configure>", update_left_scroll)
        left_canvas.bind_all("<MouseWheel>", lambda event: left_canvas.yview_scroll(int(-event.delta / 120), "units"))
        right = ttk.Frame(tab)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=8)

        ttk.Button(left, text="导入答案图片图层", command=self.add_answer_layer).pack(fill="x", pady=3)
        ttk.Button(left, text="删除当前图层", command=self.delete_current_layer).pack(fill="x", pady=3)
        ttk.Button(left, text="刷新预览", command=self.refresh_layers_preview).pack(fill="x", pady=3)
        ttk.Button(left, text="融合并导出透明字迹图", command=self.export_merged_handwriting).pack(fill="x", pady=12)

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
                "左键按住顶部圆形标识：旋转\n"
                "左键按住右下蓝色方块：缩放\n"
                "左键按住红色角点：裁切"
            ),
            wraplength=230,
        ).pack(anchor="w", pady=(8, 6))

        self.layer_vars = {
            "x": tk.DoubleVar(value=0),
            "y": tk.DoubleVar(value=0),
            "scale": tk.DoubleVar(value=1),
            "rotation_deg": tk.DoubleVar(value=0),
            "threshold": tk.IntVar(value=120),
            "crop_left": tk.IntVar(value=0),
            "crop_top": tk.IntVar(value=0),
            "crop_right": tk.IntVar(value=0),
            "crop_bottom": tk.IntVar(value=0),
            "erosion_iterations": tk.IntVar(value=0),
        }
        self._add_layer_control(left, "X", "x", -2000, 4000, 1)
        self._add_layer_control(left, "Y", "y", -2000, 4000, 1)
        self._add_layer_control(left, "缩放", "scale", 0.05, 5.0, 0.01)
        self._add_layer_control(left, "旋转角度", "rotation_deg", -180, 180, 0.1)
        self._add_layer_control(left, "黑色阈值", "threshold", 0, 255, 1)
        self._add_layer_control(left, "裁左", "crop_left", 0, 2000, 1)
        self._add_layer_control(left, "裁上", "crop_top", 0, 2000, 1)
        self._add_layer_control(left, "裁右", "crop_right", 0, 2000, 1)
        self._add_layer_control(left, "裁下", "crop_bottom", 0, 2000, 1)
        self._add_layer_control(left, "腐蚀次数", "erosion_iterations", 0, 5, 1)
        ttk.Label(left, text="橡皮擦半径 px").pack(anchor="w", pady=(7, 0))
        self.eraser_radius_var = tk.IntVar(value=18)
        ttk.Spinbox(left, from_=2, to=200, increment=1, textvariable=self.eraser_radius_var).pack(fill="x")

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
        ttk.Label(parent, text=label).pack(anchor="w", pady=(7, 0))
        spin = ttk.Spinbox(parent, from_=min_v, to=max_v, increment=increment, textvariable=self.layer_vars[key], command=self.update_current_layer)
        spin.pack(fill="x")
        spin.bind("<Return>", lambda _event: self.update_current_layer())
        spin.bind("<FocusOut>", lambda _event: self.update_current_layer())

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
        if hasattr(self, "epsilon_var"):
            self.epsilon_var.set(self.state.stroke.simplify_epsilon)
            self.stroke_alpha_threshold_var.set(self.state.stroke.alpha_threshold)
            self.stroke_min_points_var.set(self.state.stroke.min_points)
            self.mask_erosion_iterations_var.set(self.state.stroke.mask_erosion_iterations)
            self.spur_prune_length_var.set(self.state.stroke.spur_prune_length)
        if hasattr(self, "port_var"):
            self.port_var.set(self.state.machine.port)
            self.baud_var.set(self.state.machine.baud)
            self.invert_x_var.set(self.state.machine.invert_x)
            self.invert_y_var.set(self.state.machine.invert_y)
        if hasattr(self, "text_width_var"):
            self.text_width_var.set(self.state.text_render.width)
            self.text_height_var.set(self.state.text_render.height)
            self.font_size_var.set(self.state.text_render.font_size)
            self.perturb_x_var.set(self.state.text_render.perturb_x_sigma)
            self.perturb_y_var.set(self.state.text_render.perturb_y_sigma)
            self.perturb_theta_var.set(self.state.text_render.perturb_theta_sigma)
            self.text_seed_var.set(self.state.text_render.seed)
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

        ttk.Label(left, text="路径简化 epsilon(px)").pack(anchor="w")
        self.epsilon_var = tk.DoubleVar(value=self.state.stroke.simplify_epsilon)
        ttk.Spinbox(left, from_=0.1, to=20.0, increment=0.1, textvariable=self.epsilon_var).pack(fill="x")
        ttk.Label(left, text="Alpha 阈值").pack(anchor="w", pady=(7, 0))
        self.stroke_alpha_threshold_var = tk.IntVar(value=self.state.stroke.alpha_threshold)
        ttk.Spinbox(left, from_=0, to=255, increment=1, textvariable=self.stroke_alpha_threshold_var).pack(fill="x")
        ttk.Label(left, text="每笔最少点数").pack(anchor="w", pady=(7, 0))
        self.stroke_min_points_var = tk.IntVar(value=self.state.stroke.min_points)
        ttk.Spinbox(left, from_=2, to=20, increment=1, textvariable=self.stroke_min_points_var).pack(fill="x")
        ttk.Label(left, text="轨迹腐蚀次数").pack(anchor="w", pady=(7, 0))
        self.mask_erosion_iterations_var = tk.IntVar(value=self.state.stroke.mask_erosion_iterations)
        ttk.Spinbox(left, from_=0, to=5, increment=1, textvariable=self.mask_erosion_iterations_var).pack(fill="x")
        ttk.Label(left, text="毛刺剪枝长度(px)").pack(anchor="w", pady=(7, 0))
        self.spur_prune_length_var = tk.IntVar(value=self.state.stroke.spur_prune_length)
        ttk.Spinbox(left, from_=0, to=80, increment=1, textvariable=self.spur_prune_length_var).pack(fill="x")
        self.rotate_trajectory_180_var = tk.BooleanVar(value=self.state.rotate_trajectory_180)
        ttk.Checkbutton(
            left,
            text="执行时使用对角原点/XY反向",
            variable=self.rotate_trajectory_180_var,
            command=self.on_trajectory_mode_toggle,
        ).pack(anchor="w", pady=8)
        ttk.Button(left, text="预览轨迹输入图", command=self.preview_rotated_handwriting).pack(fill="x", pady=3)
        ttk.Button(left, text="从透明字迹图生成轨迹", command=self.generate_stroke_paths).pack(fill="x", pady=8)
        ttk.Button(left, text="加载 自定义轨迹strokes.json", command=self.load_strokes_json).pack(fill="x", pady=3)
        ttk.Button(left, text="由轨迹生成 G-code", command=self.export_gcode).pack(fill="x", pady=3)
        self.stroke_info_var = tk.StringVar(value="尚未生成轨迹")
        ttk.Label(left, textvariable=self.stroke_info_var, wraplength=240).pack(anchor="w", pady=12)

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
        self.text_width_var = tk.IntVar(value=self.state.text_render.width)
        self.text_height_var = tk.IntVar(value=self.state.text_render.height)
        self.font_size_var = tk.IntVar(value=self.state.text_render.font_size)
        self.perturb_x_var = tk.DoubleVar(value=self.state.text_render.perturb_x_sigma)
        self.perturb_y_var = tk.DoubleVar(value=self.state.text_render.perturb_y_sigma)
        self.perturb_theta_var = tk.DoubleVar(value=self.state.text_render.perturb_theta_sigma)
        self.text_seed_var = tk.IntVar(value=self.state.text_render.seed)
        self.font_path_var = tk.StringVar(value=default_font_path())
        for var in (
            self.text_width_var,
            self.text_height_var,
            self.font_size_var,
            self.perturb_x_var,
            self.perturb_y_var,
            self.perturb_theta_var,
            self.text_seed_var,
            self.font_path_var,
        ):
            var.trace_add("write", lambda *_args: self.invalidate_generated_text())
        for label, var in [("宽 px", self.text_width_var), ("高 px", self.text_height_var), ("字号", self.font_size_var)]:
            ttk.Label(options, text=label).pack(anchor="w")
            ttk.Entry(options, textvariable=var, width=12).pack(fill="x", pady=(0, 6))
        for label, var in [
            ("横向随机偏移", self.perturb_x_var),
            ("纵向随机偏移", self.perturb_y_var),
            ("旋转随机扰动", self.perturb_theta_var),
        ]:
            ttk.Label(options, text=label).pack(anchor="w")
            ttk.Spinbox(options, from_=0, to=20, increment=0.1, textvariable=var, width=12).pack(fill="x", pady=(0, 6))
        ttk.Label(options, text="随机种子").pack(anchor="w")
        ttk.Entry(options, textvariable=self.text_seed_var, width=12).pack(fill="x", pady=(0, 6))
        ttk.Button(options, text="选择字体", command=self.choose_font).pack(fill="x", pady=3)
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
        self.dry_run_var = tk.BooleanVar(value=True)

        ttk.Button(left, text="刷新串口", command=self.refresh_ports).pack(fill="x", pady=3)
        self.port_combo = ttk.Combobox(left, textvariable=self.port_var, values=available_ports(), width=18)
        self.port_combo.pack(fill="x", pady=3)
        ttk.Label(left, text="波特率").pack(anchor="w")
        ttk.Entry(left, textvariable=self.baud_var).pack(fill="x")
        ttk.Checkbutton(left, text="X 轴反向", variable=self.invert_x_var).pack(anchor="w", pady=3)
        ttk.Checkbutton(left, text="Y 轴反向", variable=self.invert_y_var).pack(anchor="w", pady=3)
        ttk.Checkbutton(
            left,
            text="对角原点/XY反向",
            variable=self.rotate_trajectory_180_var,
            command=self.on_trajectory_mode_toggle,
        ).pack(anchor="w", pady=3)
        ttk.Checkbutton(left, text="dry-run 理论验证", variable=self.dry_run_var).pack(anchor="w", pady=8)
        ttk.Button(left, text="由轨迹生成 G-code", command=self.export_gcode).pack(fill="x", pady=3)
        ttk.Button(left, text="开始", command=self.start_execution).pack(fill="x", pady=10)
        ttk.Button(left, text="暂停", command=self.pause_execution).pack(fill="x", pady=3)
        ttk.Button(left, text="继续", command=self.resume_execution).pack(fill="x", pady=3)
        ttk.Button(left, text="终止", command=self.stop_execution).pack(fill="x", pady=3)

        self.log_text = tk.Text(right, height=30, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def require_reference(self) -> ReferenceCanvas | None:
        if self.state.reference is None:
            messagebox.showwarning("缺少底图", "请先在模块 1 生成参考底图。")
            return None
        return self.state.reference

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

    def add_answer_layer(self, path: str | None = None, preserve_transparency: bool = False) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        path = path or filedialog.askopenfilename(filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff")])
        if not path:
            return
        layer = AnswerLayer(source_path=path, x=ref.width_px * 0.05, y=ref.height_px * 0.05)
        if preserve_transparency:
            layer.already_extracted = True
            layer.threshold = 255
            layer.min_area = 1
            layer.erosion_iterations = 0
        self.push_undo_state()
        self.state.layers.append(layer)
        self.refresh_layer_list()
        self.layer_list.selection_clear(0, tk.END)
        self.layer_list.selection_set(len(self.state.layers) - 1)
        self.load_layer_to_controls()
        self.refresh_layers_preview()

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
            if key.startswith("crop") or key in {"threshold", "erosion_iterations"}:
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
        layer.threshold = 255
        layer.erosion_iterations = 0
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

    def commit_layer_erase(self) -> None:
        ref = self.state.reference
        layer = self.current_layer()
        index = self.current_layer_index()
        if ref is None or layer is None or index is None or not self._erase_points:
            return
        full = Image.new("RGBA", (ref.width_px, ref.height_px), (0, 0, 0, 0))
        rendered = render_layer(layer)
        full.alpha_composite(rendered, (int(round(layer.x)), int(round(layer.y))))
        alpha = full.getchannel("A")
        draw = ImageDraw.Draw(alpha)
        radius = max(1, int(self.eraser_radius_var.get()))
        for x, y in self._erase_points:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=0)
        full.putalpha(alpha)
        bbox = alpha.getbbox()
        if bbox:
            full = full.crop(bbox)
            new_x, new_y = bbox[0], bbox[1]
        else:
            full = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            new_x, new_y = layer.x, layer.y
        path = self.artifacts / f"erased_layer_{index + 1}.png"
        full.save(path)
        layer.source_path = str(path)
        layer.x = float(new_x)
        layer.y = float(new_y)
        layer.scale = 1.0
        layer.rotation_deg = 0.0
        layer.crop_left = layer.crop_top = layer.crop_right = layer.crop_bottom = 0
        layer.threshold = 255
        layer.erosion_iterations = 0
        layer.already_extracted = True
        self.sync_layer_controls(layer)

    def export_merged_handwriting(self) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        try:
            image = composite_layers(ref, self.state.layers)
            path = save_png(image, self.artifacts / "handwriting_merged.png")
            messagebox.showinfo("完成", f"透明字迹图已保存：{path}")
        except Exception as exc:
            messagebox.showerror("融合失败", str(exc))

    def generate_stroke_paths(self) -> None:
        ref = self.require_reference()
        if ref is None:
            return
        path = self.artifacts / "handwriting_merged.png"
        if not path.exists():
            self.export_merged_handwriting()
        if not path.exists():
            return
        try:
            image = load_rgba(path)
            self.push_undo_state()
            self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            self.state.stroke.simplify_epsilon = float(self.epsilon_var.get())
            self.state.stroke.alpha_threshold = int(self.stroke_alpha_threshold_var.get())
            self.state.stroke.min_points = int(self.stroke_min_points_var.get())
            self.state.stroke.mask_erosion_iterations = int(self.mask_erosion_iterations_var.get())
            self.state.stroke.spur_prune_length = int(self.spur_prune_length_var.get())
            self.strokes = generate_strokes(
                image,
                simplify_epsilon=self.state.stroke.simplify_epsilon,
                alpha_threshold=self.state.stroke.alpha_threshold,
                min_points=self.state.stroke.min_points,
                mask_erosion_iterations=self.state.stroke.mask_erosion_iterations,
                spur_prune_length=self.state.stroke.spur_prune_length,
            )
            save_strokes(self.strokes, ref.width_px, ref.height_px, self.artifacts / "strokes.json")
            self.state.save(self.artifacts / "project.json")
            points = sum(len(stroke.points) for stroke in self.strokes)
            self.stroke_info_var.set(f"已生成 {len(self.strokes)} 条 stroke，共 {points} 个点。")
            self.draw_strokes_preview()
            messagebox.showinfo("完成", "轨迹已保存到 artifacts/strokes.json")
        except Exception as exc:
            messagebox.showerror("轨迹生成失败", str(exc))

    def preview_rotated_handwriting(self) -> None:
        ref = self.state.reference
        if ref is None:
            self.stroke_canvas.set_image(None)
            return
        path = self.artifacts / "handwriting_merged.png"
        if not path.exists():
            if self.state.layers:
                self.export_merged_handwriting()
            else:
                self.stroke_canvas.set_image(load_rgba(ref.image_path) if ref.image_path and Path(ref.image_path).exists() else None)
                return
        try:
            handwriting = load_rgba(path)
            self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            if ref.image_path and Path(ref.image_path).exists():
                base = load_rgba(ref.image_path)
            else:
                base = Image.new("RGBA", (ref.width_px, ref.height_px), (255, 255, 255, 255))
            overlay = Image.new("RGBA", base.size, (255, 255, 255, 80))
            base = Image.alpha_composite(base, overlay)
            preview = Image.alpha_composite(base, handwriting)
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

    def invalidate_generated_text(self, _event=None) -> None:
        self.generated_text_path = None
        if hasattr(self, "add_generated_text_button"):
            self.add_generated_text_button.configure(state="disabled")

    def generate_text_image(self) -> None:
        text = self.text_input.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("缺少文本", "请输入要生成的文本。")
            return
        try:
            self.state.text_render.width = int(self.text_width_var.get())
            self.state.text_render.height = int(self.text_height_var.get())
            self.state.text_render.font_size = int(self.font_size_var.get())
            self.state.text_render.perturb_x_sigma = float(self.perturb_x_var.get())
            self.state.text_render.perturb_y_sigma = float(self.perturb_y_var.get())
            self.state.text_render.perturb_theta_sigma = float(self.perturb_theta_var.get())
            self.state.text_render.seed = int(self.text_seed_var.get())
            filename = f"handright_output_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
            path = render_text(
                text=text,
                output_path=self.artifacts / filename,
                width=self.state.text_render.width,
                height=self.state.text_render.height,
                font_path=self.font_path_var.get(),
                font_size=self.state.text_render.font_size,
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
        config.invert_x = bool(self.invert_x_var.get())
        config.invert_y = bool(self.invert_y_var.get())
        self.state.machine = config
        self.state.save(self.artifacts / "project.json")
        return config

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
        self.root.after(100, self._poll_log_queue)

    def on_close(self) -> None:
        try:
            if hasattr(self, "port_var"):
                self._machine_config_from_ui()
            if hasattr(self, "rotate_trajectory_180_var"):
                self.state.rotate_trajectory_180 = bool(self.rotate_trajectory_180_var.get())
            if hasattr(self, "epsilon_var"):
                self.state.stroke.simplify_epsilon = float(self.epsilon_var.get())
            if hasattr(self, "stroke_alpha_threshold_var"):
                self.state.stroke.alpha_threshold = int(self.stroke_alpha_threshold_var.get())
            if hasattr(self, "stroke_min_points_var"):
                self.state.stroke.min_points = int(self.stroke_min_points_var.get())
            if hasattr(self, "mask_erosion_iterations_var"):
                self.state.stroke.mask_erosion_iterations = int(self.mask_erosion_iterations_var.get())
            if hasattr(self, "text_width_var"):
                self.state.text_render.width = int(self.text_width_var.get())
            if hasattr(self, "text_height_var"):
                self.state.text_render.height = int(self.text_height_var.get())
            if hasattr(self, "font_size_var"):
                self.state.text_render.font_size = int(self.font_size_var.get())
            if hasattr(self, "perturb_x_var"):
                self.state.text_render.perturb_x_sigma = float(self.perturb_x_var.get())
            if hasattr(self, "perturb_y_var"):
                self.state.text_render.perturb_y_sigma = float(self.perturb_y_var.get())
            if hasattr(self, "perturb_theta_var"):
                self.state.text_render.perturb_theta_sigma = float(self.perturb_theta_var.get())
            if hasattr(self, "text_seed_var"):
                self.state.text_render.seed = int(self.text_seed_var.get())
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
