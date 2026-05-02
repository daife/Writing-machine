from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np
from PIL import Image
from svgpathtools import svg2paths2


Point = tuple[float, float]
Command = str | Sequence[str]
ProgressCallback = Callable[[str], None]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
AUTOTRACE_RUNTIME_DIR = RESOURCE_ROOT / "third_party" / "autotrace-runtime" / "bin"
LOCAL_AUTOTRACE_PATH = AUTOTRACE_RUNTIME_DIR / "autotrace.exe"
DEFAULT_AUTOTRACE_PATHS = (
    LOCAL_AUTOTRACE_PATH,
    PROJECT_ROOT / "third_party" / "autotrace-build-mingw" / ".libs" / "autotrace.exe",
    Path(r"C:\Program Files\AutoTrace\autotrace.exe"),
)
VPYPE_INPROCESS = "__vpype_inprocess__"
FORKCLEAN_PLUGIN_ROOT = RESOURCE_ROOT / "third_party" / "vpype_forkclean_plugin_v2"
MASK_MORPHOLOGY_DILATE_THEN_ERODE = "dilate_then_erode"
MASK_MORPHOLOGY_ERODE_THEN_DILATE = "erode_then_dilate"
MASK_MORPHOLOGY_ORDERS = {
    MASK_MORPHOLOGY_DILATE_THEN_ERODE,
    MASK_MORPHOLOGY_ERODE_THEN_DILATE,
}
HEX_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")


@dataclass
class Stroke:
    points: list[Point]
    closed: bool = False


class TraceToolError(RuntimeError):
    pass


def _remove_small_components(mask: np.ndarray, min_area: int) -> np.ndarray:
    if min_area <= 1:
        return mask.astype(bool)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if count <= 1:
        return mask.astype(bool)
    keep = np.zeros_like(mask, dtype=bool)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            keep |= labels == label
    return keep


def _normalize_mask_morphology_order(order: str | None) -> str:
    if order in MASK_MORPHOLOGY_ORDERS:
        return str(order)
    return MASK_MORPHOLOGY_DILATE_THEN_ERODE


def _apply_mask_morphology(
    mask: np.ndarray,
    mask_dilation_iterations: int,
    mask_erosion_iterations: int,
    mask_morphology_order: str | None,
) -> np.ndarray:
    order = _normalize_mask_morphology_order(mask_morphology_order)
    operations = (
        ("dilate", mask_dilation_iterations),
        ("erode", mask_erosion_iterations),
    )
    if order == MASK_MORPHOLOGY_ERODE_THEN_DILATE:
        operations = tuple(reversed(operations))

    kernel = np.ones((3, 3), dtype=np.uint8)
    for operation, iterations in operations:
        iterations = int(iterations)
        if iterations <= 0:
            continue
        source = mask.astype(np.uint8) * 255
        if operation == "dilate":
            mask = cv2.dilate(source, kernel, iterations=iterations) > 0
        else:
            mask = cv2.erode(source, kernel, iterations=iterations) > 0
        if not mask.any():
            return mask
    return mask


def rdp(points: list[Point], epsilon: float) -> list[Point]:
    if len(points) < 3:
        return points
    start = np.array(points[0], dtype=float)
    end = np.array(points[-1], dtype=float)
    line = end - start
    line_len = np.linalg.norm(line)
    max_dist = -1.0
    index = 0
    for i in range(1, len(points) - 1):
        point = np.array(points[i], dtype=float)
        if line_len == 0:
            dist = np.linalg.norm(point - start)
        else:
            delta = start - point
            dist = abs(line[0] * delta[1] - line[1] * delta[0]) / line_len
        if dist > max_dist:
            max_dist = float(dist)
            index = i
    if max_dist > epsilon:
        left = rdp(points[: index + 1], epsilon)
        right = rdp(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def image_to_mask(image: Image.Image, threshold: int = 20) -> np.ndarray:
    rgba = image.convert("RGBA")
    alpha = np.array(rgba.getchannel("A"))
    if alpha.max() > threshold:
        return (alpha > threshold).astype(bool)
    if alpha.max() == 0:
        return np.zeros(alpha.shape, dtype=bool)
    gray = np.array(rgba.convert("L"))
    return (gray < 240).astype(bool)


def _resolve_executable(command: str) -> str:
    command = command.strip().strip('"')
    if command == "autotrace":
        for autotrace_path in DEFAULT_AUTOTRACE_PATHS:
            if autotrace_path.exists():
                return str(autotrace_path)
    found = shutil.which(command)
    if found:
        return found
    candidate = Path(command)
    if candidate.exists():
        return str(candidate)
    scripts_dir = Path(sys.executable).resolve().parent
    for name in (command, f"{command}.exe", f"{command}.cmd", f"{command}.bat"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    raise TraceToolError(
        f"未找到轨迹工具 `{command}`。请先安装并确保命令可执行；模块 3 需要 AutoTrace 和 vpype。"
    )


def _command_prefix(command: Command) -> list[str]:
    if isinstance(command, str):
        if command.strip().strip('"') == "vpype":
            return [VPYPE_INPROCESS]
        return [_resolve_executable(command)]
    if not command:
        raise TraceToolError("轨迹工具命令不能为空。")
    return [str(part) for part in command]


def _display_command(args: Sequence[str]) -> str:
    if args and args[0] == VPYPE_INPROCESS:
        return "vpype " + subprocess.list2cmdline([str(arg) for arg in args[1:]])
    return "& " + subprocess.list2cmdline([str(arg) for arg in args])


def _notify_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _register_vpype_forkclean_plugin(vpype_cli_module) -> None:
    if "forkclean" in vpype_cli_module.cli.commands:
        return
    plugin_root = str(FORKCLEAN_PLUGIN_ROOT)
    if FORKCLEAN_PLUGIN_ROOT.exists() and plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    try:
        plugin = importlib.import_module("vpype_forkclean")
    except Exception as exc:
        raise TraceToolError(f"加载 vpype forkclean 插件失败：{exc}") from exc
    vpype_cli_module.cli.add_command(plugin.forkclean)


def _run_vpype_inprocess(
    args: list[str],
    cwd: Path,
    progress_callback: ProgressCallback | None = None,
    running_message: str | None = None,
    finished_message: str | None = None,
) -> None:
    stdout = StringIO()
    stderr = StringIO()
    old_cwd = Path.cwd()
    started_at = time.monotonic()
    try:
        import vpype_cli

        _register_vpype_forkclean_plugin(vpype_cli)
        if running_message:
            _notify_progress(progress_callback, f"{running_message}，已运行 0.0 秒...")
        os.chdir(cwd)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            vpype_cli.cli.main(args=args[1:], standalone_mode=False)
        if finished_message:
            elapsed = time.monotonic() - started_at
            _notify_progress(progress_callback, f"{finished_message}，耗时 {elapsed:.1f} 秒。")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0:
            details = (stderr.getvalue() or stdout.getvalue()).strip()
            message = f"轨迹工具执行失败，退出码 {code}：{_display_command(args)}"
            if details:
                message += f"\n{details[-1600:]}"
            raise TraceToolError(message) from exc
    except Exception as exc:
        details = (stderr.getvalue() or stdout.getvalue()).strip()
        message = f"执行 `{_display_command(args)}` 失败：{exc}"
        if details:
            message += f"\n{details[-1600:]}"
        raise TraceToolError(message) from exc
    finally:
        os.chdir(old_cwd)


def _run_trace_command(
    args: list[str],
    cwd: Path,
    progress_callback: ProgressCallback | None = None,
    running_message: str | None = None,
    finished_message: str | None = None,
) -> None:
    if args and args[0] == VPYPE_INPROCESS:
        _run_vpype_inprocess(args, cwd, progress_callback, running_message, finished_message)
        return
    env = None
    executable_dir = Path(args[0]).parent
    if executable_dir.exists():
        env = dict(os.environ)
        path_entries = [executable_dir]
        if AUTOTRACE_RUNTIME_DIR.exists():
            path_entries.append(AUTOTRACE_RUNTIME_DIR)
        local_build_env = PROJECT_ROOT / "third_party" / "autotrace-build-env" / "Library"
        for dependency_dir in (
            local_build_env / "bin",
            local_build_env / "mingw-w64" / "bin",
            local_build_env / "usr" / "bin",
        ):
            if dependency_dir.exists():
                path_entries.append(dependency_dir)
        env["PATH"] = os.pathsep.join(str(path) for path in path_entries) + os.pathsep + env.get("PATH", "")
    try:
        if running_message:
            _notify_progress(progress_callback, f"{running_message}，已运行 0.0 秒...")
        process = subprocess.Popen(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        started_at = time.monotonic()
        last_report = 0.0
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if running_message:
                    elapsed = time.monotonic() - started_at
                    if elapsed - last_report >= 0.5:
                        _notify_progress(progress_callback, f"{running_message}，已运行 {elapsed:.1f} 秒...")
                        last_report = elapsed
    except OSError as exc:
        raise TraceToolError(f"执行 `{_display_command(args)}` 失败：{exc}") from exc
    if process.returncode != 0:
        details = (stderr or stdout or "").strip()
        if len(details) > 1600:
            details = details[-1600:]
        message = f"轨迹工具执行失败，退出码 {process.returncode}：{_display_command(args)}"
        if details:
            message += f"\n{details}"
        raise TraceToolError(message)
    if finished_message:
        elapsed = time.monotonic() - started_at
        _notify_progress(progress_callback, f"{finished_message}，耗时 {elapsed:.1f} 秒。")


def _save_autotrace_input(
    image: Image.Image,
    path: Path,
    alpha_threshold: int,
    mask_dilation_iterations: int,
    mask_erosion_iterations: int,
    min_component_area: int,
    mask_morphology_order: str = MASK_MORPHOLOGY_DILATE_THEN_ERODE,
) -> bool:
    mask = image_to_mask(image, threshold=alpha_threshold)
    if not mask.any():
        return False
    mask = _apply_mask_morphology(
        mask,
        mask_dilation_iterations=mask_dilation_iterations,
        mask_erosion_iterations=mask_erosion_iterations,
        mask_morphology_order=mask_morphology_order,
    )
    if not mask.any():
        return False
    mask = _remove_small_components(mask, min_component_area)
    if not mask.any():
        return False

    bitmap = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), mode="L")
    path.parent.mkdir(parents=True, exist_ok=True)
    bitmap.convert("RGB").save(path, format="BMP")
    return True


def _format_float(value: float) -> str:
    return f"{float(value):g}"


def _build_autotrace_args(
    command: Command,
    input_bitmap: Path,
    output_svg: Path,
    *,
    autotrace_centerline: bool,
    autotrace_background_color: str,
    autotrace_color_count: int,
    autotrace_corner_always_threshold: float,
    autotrace_corner_surround: int,
    autotrace_corner_threshold: float,
    autotrace_despeckle_level: int,
    autotrace_despeckle_tightness: float,
    autotrace_dpi: int,
    autotrace_error_threshold: float,
    autotrace_filter_iterations: int,
    autotrace_line_reversion_threshold: float,
    autotrace_line_threshold: float,
    autotrace_noise_removal: float,
    autotrace_preserve_width: bool,
    autotrace_remove_adjacent_corners: bool,
    autotrace_tangent_surround: int,
    autotrace_width_weight_factor: float,
    autotrace_report_progress: bool,
    autotrace_debug_bitmap: bool,
) -> list[str]:
    args = _command_prefix(command)
    background = autotrace_background_color.strip().lstrip("#")
    if background:
        if not HEX_COLOR_RE.match(background):
            raise TraceToolError("AutoTrace background-color 必须是 6 位十六进制颜色，例如 FFFFFF。")
        args += ["-background-color", background]
    if autotrace_centerline:
        args.append("-centerline")
    if autotrace_color_count > 0:
        args += ["-color-count", str(max(1, min(256, int(autotrace_color_count))))]
    args += [
        "-corner-always-threshold",
        _format_float(autotrace_corner_always_threshold),
        "-corner-surround",
        str(max(0, int(autotrace_corner_surround))),
        "-corner-threshold",
        _format_float(autotrace_corner_threshold),
        "-despeckle-level",
        str(max(0, min(20, int(autotrace_despeckle_level)))),
        "-despeckle-tightness",
        _format_float(max(0.0, min(8.0, float(autotrace_despeckle_tightness)))),
    ]
    if autotrace_dpi > 0:
        args += ["-dpi", str(max(1, int(autotrace_dpi)))]
    args += [
        "-error-threshold",
        _format_float(autotrace_error_threshold),
        "-filter-iterations",
        str(max(0, int(autotrace_filter_iterations))),
        "-line-reversion-threshold",
        _format_float(autotrace_line_reversion_threshold),
        "-line-threshold",
        _format_float(autotrace_line_threshold),
        "-noise-removal",
        _format_float(max(0.0, min(1.0, float(autotrace_noise_removal)))),
    ]
    if autotrace_preserve_width:
        args.append("-preserve-width")
    if autotrace_remove_adjacent_corners:
        args.append("-remove-adjacent-corners")
    args += [
        "-tangent-surround",
        str(max(0, int(autotrace_tangent_surround))),
        "-width-weight-factor",
        _format_float(autotrace_width_weight_factor),
    ]
    if autotrace_report_progress:
        args.append("-report-progress")
    if autotrace_debug_bitmap:
        args.append("-debug-bitmap")
    args += [str(input_bitmap), "-output-format", "svg", "-output-file", str(output_svg)]
    return args


def _svg_path_to_points(path) -> list[Point]:
    points: list[Point] = []
    for segment in path:
        if not points:
            start = segment.start
            points.append((float(start.real), float(start.imag)))
        end = segment.end
        candidate = (float(end.real), float(end.imag))
        if not points or math.hypot(candidate[0] - points[-1][0], candidate[1] - points[-1][1]) > 1e-6:
            points.append(candidate)
    return points


def _svg_to_strokes(svg_path: Path) -> list[Stroke]:
    paths, _attributes, _svg_attributes = svg2paths2(str(svg_path))
    strokes: list[Stroke] = []
    for path in paths:
        for subpath in path.continuous_subpaths():
            points = _svg_path_to_points(subpath)
            if len(points) >= 2:
                strokes.append(Stroke(points=points, closed=bool(subpath.isclosed())))
    return strokes


def trace_image_to_svg(
    image: Image.Image,
    work_dir: str | Path,
    alpha_threshold: int = 20,
    mask_dilation_iterations: int = 0,
    mask_erosion_iterations: int = 0,
    mask_morphology_order: str = MASK_MORPHOLOGY_DILATE_THEN_ERODE,
    min_component_area: int = 12,
    trace_debug_artifacts: bool = False,
    autotrace_centerline: bool = True,
    autotrace_background_color: str = "",
    autotrace_color_count: int = 0,
    autotrace_corner_always_threshold: float = 60.0,
    autotrace_corner_surround: int = 4,
    autotrace_corner_threshold: float = 100.0,
    autotrace_despeckle_level: int = 0,
    autotrace_despeckle_tightness: float = 2.0,
    autotrace_dpi: int = 0,
    autotrace_error_threshold: float = 2.0,
    autotrace_filter_iterations: int = 4,
    autotrace_line_reversion_threshold: float = 0.01,
    autotrace_line_threshold: float = 1.0,
    autotrace_noise_removal: float = 0.99,
    autotrace_preserve_width: bool = False,
    autotrace_remove_adjacent_corners: bool = False,
    autotrace_tangent_surround: int = 3,
    autotrace_width_weight_factor: float = 6.0,
    autotrace_report_progress: bool = False,
    autotrace_debug_bitmap: bool = False,
    vpype_linemerge_tolerance_px: float = 1.0,
    forkclean_max_search_length_px: float = 24.0,
    forkclean_min_short_length_px: float = 3.0,
    forkclean_angle_threshold_deg: float = 110.0,
    forkclean_node_tolerance_px: float = 1.0,
    forkclean_passes: int = 1,
    vpype_linesimplify_tolerance_px: float = 0.3,
    autotrace_command: Command = "autotrace",
    vpype_command: Command = "vpype",
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    work = Path(work_dir)
    input_bitmap = work / "trace_input.bmp"
    raw_svg = work / "trace_raw.svg"
    optimized_svg = work / "trace_optimized.svg"
    _notify_progress(progress_callback, "正在生成 AutoTrace 输入位图 trace_input.bmp...")
    has_pixels = _save_autotrace_input(
        image,
        input_bitmap,
        alpha_threshold=alpha_threshold,
        mask_dilation_iterations=mask_dilation_iterations,
        mask_erosion_iterations=mask_erosion_iterations,
        min_component_area=min_component_area,
        mask_morphology_order=mask_morphology_order,
    )
    if not has_pixels:
        _notify_progress(progress_callback, "没有检测到可生成轨迹的字迹像素。")
        return None
    _notify_progress(progress_callback, f"已生成 AutoTrace 输入位图：{input_bitmap}")

    _run_trace_command(
        _build_autotrace_args(
            autotrace_command,
            input_bitmap,
            raw_svg,
            autotrace_centerline=autotrace_centerline,
            autotrace_background_color=autotrace_background_color,
            autotrace_color_count=autotrace_color_count,
            autotrace_corner_always_threshold=autotrace_corner_always_threshold,
            autotrace_corner_surround=autotrace_corner_surround,
            autotrace_corner_threshold=autotrace_corner_threshold,
            autotrace_despeckle_level=autotrace_despeckle_level,
            autotrace_despeckle_tightness=autotrace_despeckle_tightness,
            autotrace_dpi=autotrace_dpi,
            autotrace_error_threshold=autotrace_error_threshold,
            autotrace_filter_iterations=autotrace_filter_iterations,
            autotrace_line_reversion_threshold=autotrace_line_reversion_threshold,
            autotrace_line_threshold=autotrace_line_threshold,
            autotrace_noise_removal=autotrace_noise_removal,
            autotrace_preserve_width=autotrace_preserve_width,
            autotrace_remove_adjacent_corners=autotrace_remove_adjacent_corners,
            autotrace_tangent_surround=autotrace_tangent_surround,
            autotrace_width_weight_factor=autotrace_width_weight_factor,
            autotrace_report_progress=autotrace_report_progress,
            autotrace_debug_bitmap=autotrace_debug_bitmap,
        ),
        work,
        progress_callback=progress_callback,
        running_message="使用 AutoTrace 将位图转换为中心线 SVG",
        finished_message="AutoTrace 已生成 trace_raw.svg",
    )
    if not raw_svg.exists():
        raise TraceToolError(f"AutoTrace 未生成 SVG：{raw_svg}")

    linemerge_args = [
        "read",
        str(raw_svg),
        "linemerge",
        "-t",
        f"{max(0.0, float(vpype_linemerge_tolerance_px))}px",
    ]
    forkclean_args = [
        "forkclean",
        "--max-search-length",
        f"{max(0.0, float(forkclean_max_search_length_px))}px",
        "--min-short-length",
        f"{max(0.0, float(forkclean_min_short_length_px))}px",
        "--angle-threshold",
        f"{max(0.0, float(forkclean_angle_threshold_deg))}deg",
        "--node-tolerance",
        f"{max(0.0, float(forkclean_node_tolerance_px))}px",
        "--passes",
        str(max(1, int(forkclean_passes))),
    ]
    linesimplify_args = [
        "linesimplify",
        "-t",
        f"{max(0.0, float(vpype_linesimplify_tolerance_px))}px",
    ]
    linesort_args = ["linesort", "--two-opt"]
    if trace_debug_artifacts:
        debug_steps = [
            (raw_svg, "trace_linemerge.svg", ["linemerge", "-t", f"{max(0.0, float(vpype_linemerge_tolerance_px))}px"], "vpype linemerge"),
            (work / "trace_linemerge.svg", "trace_forkclean.svg", forkclean_args, "vpype forkclean"),
            (work / "trace_forkclean.svg", "trace_linesimplify.svg", linesimplify_args, "vpype linesimplify"),
            (work / "trace_linesimplify.svg", optimized_svg.name, linesort_args, "vpype linesort"),
        ]
        for source_svg, output_name, stage_args, label in debug_steps:
            output_svg = work / output_name
            _run_trace_command(
                _command_prefix(vpype_command) + ["read", str(source_svg)] + stage_args + ["write", str(output_svg)],
                work,
                progress_callback=progress_callback,
                running_message=f"使用 {label} 生成 {output_name}",
                finished_message=f"{label} 已生成 {output_name}",
            )
    else:
        vpype_args = _command_prefix(vpype_command) + linemerge_args + forkclean_args + linesimplify_args + linesort_args + [
            "write",
            str(optimized_svg),
        ]
        _run_trace_command(
            vpype_args,
            work,
            progress_callback=progress_callback,
            running_message="使用 vpype 合并、清理、简化并排序 SVG 路径",
            finished_message="vpype 已生成 trace_optimized.svg",
        )
    if not optimized_svg.exists():
        raise TraceToolError(f"vpype 未生成优化 SVG：{optimized_svg}")
    return optimized_svg


def generate_strokes(
    image: Image.Image,
    alpha_threshold: int = 20,
    mask_dilation_iterations: int = 0,
    mask_erosion_iterations: int = 0,
    mask_morphology_order: str = MASK_MORPHOLOGY_DILATE_THEN_ERODE,
    min_component_area: int = 12,
    trace_debug_artifacts: bool = False,
    autotrace_centerline: bool = True,
    autotrace_background_color: str = "",
    autotrace_color_count: int = 0,
    autotrace_corner_always_threshold: float = 60.0,
    autotrace_corner_surround: int = 4,
    autotrace_corner_threshold: float = 100.0,
    autotrace_despeckle_level: int = 0,
    autotrace_despeckle_tightness: float = 2.0,
    autotrace_dpi: int = 0,
    autotrace_error_threshold: float = 2.0,
    autotrace_filter_iterations: int = 4,
    autotrace_line_reversion_threshold: float = 0.01,
    autotrace_line_threshold: float = 1.0,
    autotrace_noise_removal: float = 0.99,
    autotrace_preserve_width: bool = False,
    autotrace_remove_adjacent_corners: bool = False,
    autotrace_tangent_surround: int = 3,
    autotrace_width_weight_factor: float = 6.0,
    autotrace_report_progress: bool = False,
    autotrace_debug_bitmap: bool = False,
    vpype_linemerge_tolerance_px: float = 1.0,
    forkclean_max_search_length_px: float = 24.0,
    forkclean_min_short_length_px: float = 3.0,
    forkclean_angle_threshold_deg: float = 110.0,
    forkclean_node_tolerance_px: float = 1.0,
    forkclean_passes: int = 1,
    vpype_linesimplify_tolerance_px: float = 0.3,
    work_dir: str | Path | None = None,
    autotrace_command: Command = "autotrace",
    vpype_command: Command = "vpype",
    progress_callback: ProgressCallback | None = None,
    **_legacy_kwargs,
) -> list[Stroke]:
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="writing_machine_trace_") as tmp:
            return generate_strokes(
                image,
                alpha_threshold=alpha_threshold,
                mask_dilation_iterations=mask_dilation_iterations,
                mask_erosion_iterations=mask_erosion_iterations,
                mask_morphology_order=mask_morphology_order,
                min_component_area=min_component_area,
                trace_debug_artifacts=trace_debug_artifacts,
                autotrace_centerline=autotrace_centerline,
                autotrace_background_color=autotrace_background_color,
                autotrace_color_count=autotrace_color_count,
                autotrace_corner_always_threshold=autotrace_corner_always_threshold,
                autotrace_corner_surround=autotrace_corner_surround,
                autotrace_corner_threshold=autotrace_corner_threshold,
                autotrace_despeckle_level=autotrace_despeckle_level,
                autotrace_despeckle_tightness=autotrace_despeckle_tightness,
                autotrace_dpi=autotrace_dpi,
                autotrace_error_threshold=autotrace_error_threshold,
                autotrace_filter_iterations=autotrace_filter_iterations,
                autotrace_line_reversion_threshold=autotrace_line_reversion_threshold,
                autotrace_line_threshold=autotrace_line_threshold,
                autotrace_noise_removal=autotrace_noise_removal,
                autotrace_preserve_width=autotrace_preserve_width,
                autotrace_remove_adjacent_corners=autotrace_remove_adjacent_corners,
                autotrace_tangent_surround=autotrace_tangent_surround,
                autotrace_width_weight_factor=autotrace_width_weight_factor,
                autotrace_report_progress=autotrace_report_progress,
                autotrace_debug_bitmap=autotrace_debug_bitmap,
                vpype_linemerge_tolerance_px=vpype_linemerge_tolerance_px,
                forkclean_max_search_length_px=forkclean_max_search_length_px,
                forkclean_min_short_length_px=forkclean_min_short_length_px,
                forkclean_angle_threshold_deg=forkclean_angle_threshold_deg,
                forkclean_node_tolerance_px=forkclean_node_tolerance_px,
                forkclean_passes=forkclean_passes,
                vpype_linesimplify_tolerance_px=vpype_linesimplify_tolerance_px,
                work_dir=tmp,
                autotrace_command=autotrace_command,
                vpype_command=vpype_command,
                progress_callback=progress_callback,
            )

    optimized_svg = trace_image_to_svg(
        image,
        work_dir=work_dir,
        alpha_threshold=alpha_threshold,
        mask_dilation_iterations=mask_dilation_iterations,
        mask_erosion_iterations=mask_erosion_iterations,
        mask_morphology_order=mask_morphology_order,
        min_component_area=min_component_area,
        trace_debug_artifacts=trace_debug_artifacts,
        autotrace_centerline=autotrace_centerline,
        autotrace_background_color=autotrace_background_color,
        autotrace_color_count=autotrace_color_count,
        autotrace_corner_always_threshold=autotrace_corner_always_threshold,
        autotrace_corner_surround=autotrace_corner_surround,
        autotrace_corner_threshold=autotrace_corner_threshold,
        autotrace_despeckle_level=autotrace_despeckle_level,
        autotrace_despeckle_tightness=autotrace_despeckle_tightness,
        autotrace_dpi=autotrace_dpi,
        autotrace_error_threshold=autotrace_error_threshold,
        autotrace_filter_iterations=autotrace_filter_iterations,
        autotrace_line_reversion_threshold=autotrace_line_reversion_threshold,
        autotrace_line_threshold=autotrace_line_threshold,
        autotrace_noise_removal=autotrace_noise_removal,
        autotrace_preserve_width=autotrace_preserve_width,
        autotrace_remove_adjacent_corners=autotrace_remove_adjacent_corners,
        autotrace_tangent_surround=autotrace_tangent_surround,
        autotrace_width_weight_factor=autotrace_width_weight_factor,
        autotrace_report_progress=autotrace_report_progress,
        autotrace_debug_bitmap=autotrace_debug_bitmap,
        vpype_linemerge_tolerance_px=vpype_linemerge_tolerance_px,
        forkclean_max_search_length_px=forkclean_max_search_length_px,
        forkclean_min_short_length_px=forkclean_min_short_length_px,
        forkclean_angle_threshold_deg=forkclean_angle_threshold_deg,
        forkclean_node_tolerance_px=forkclean_node_tolerance_px,
        forkclean_passes=forkclean_passes,
        vpype_linesimplify_tolerance_px=vpype_linesimplify_tolerance_px,
        autotrace_command=autotrace_command,
        vpype_command=vpype_command,
        progress_callback=progress_callback,
    )
    if optimized_svg is None:
        return []
    _notify_progress(progress_callback, "正在解析优化后的 SVG 折线为轨迹点...")
    strokes = _svg_to_strokes(optimized_svg)
    points = sum(len(stroke.points) for stroke in strokes)
    _notify_progress(progress_callback, f"SVG 解析完成：{len(strokes)} 条 stroke，{points} 个点。")
    return strokes


def save_strokes(strokes: list[Stroke], width_px: int, height_px: int, path: str | Path) -> str:
    data = {
        "coordinate_space": "reference_canvas_pixel",
        "width_px": width_px,
        "height_px": height_px,
        "strokes": [
            {"points": [[round(x, 3), round(y, 3)] for x, y in stroke.points], "closed": stroke.closed}
            for stroke in strokes
        ],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def load_strokes(path: str | Path) -> tuple[list[Stroke], int, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    strokes = [
        Stroke(points=[(float(x), float(y)) for x, y in item["points"]], closed=bool(item.get("closed", False)))
        for item in data.get("strokes", [])
    ]
    return strokes, int(data["width_px"]), int(data["height_px"])
