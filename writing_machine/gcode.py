from __future__ import annotations

from pathlib import Path

from .models import MachineConfig, ReferenceCanvas
from .strokes import Stroke


def pixel_to_mm(
    x_px: float,
    y_px: float,
    reference: ReferenceCanvas,
    config: MachineConfig,
    diagonal_origin_180: bool = False,
) -> tuple[float, float]:
    x_mm = x_px / reference.px_per_mm
    y_mm = y_px / reference.px_per_mm
    if diagonal_origin_180:
        x_mm = reference.width_mm - x_mm
        y_mm = reference.height_mm - y_mm
    if config.invert_x:
        x_mm = reference.width_mm - x_mm
    if config.invert_y:
        y_mm = reference.height_mm - y_mm
    return x_mm + config.origin_x_mm, config.origin_y_mm - y_mm


def strokes_to_gcode(
    strokes: list[Stroke],
    reference: ReferenceCanvas,
    config: MachineConfig,
    include_header: bool = True,
    include_footer: bool = True,
    diagonal_origin_180: bool = False,
) -> list[str]:
    lines: list[str] = []
    if include_header:
        if config.set_work_zero:
            lines.append("G10 P0 L20 X0.0Y0.0Z0")
        lines.extend(["G21", "G90", f"F{config.travel_feed}", config.pen_up, config.pen_up])

    for stroke in strokes:
        if len(stroke.points) < 2:
            continue
        start_x, start_y = pixel_to_mm(
            stroke.points[0][0],
            stroke.points[0][1],
            reference,
            config,
            diagonal_origin_180=diagonal_origin_180,
        )
        lines.append(f"G0 X{start_x:.3f}Y{start_y:.3f}F{config.travel_feed}")
        lines.append(config.pen_down)
        if config.dwell_seconds > 0:
            lines.append(f"G4 P{config.dwell_seconds:.3f}")
        for point in stroke.points[1:]:
            x_mm, y_mm = pixel_to_mm(point[0], point[1], reference, config, diagonal_origin_180=diagonal_origin_180)
            lines.append(f"G1 X{x_mm:.3f}Y{y_mm:.3f}F{config.write_feed}")
        lines.append(config.pen_up)
        if config.dwell_seconds > 0:
            lines.append(f"G4 P{config.dwell_seconds:.3f}")

    if include_footer:
        lines.append(config.pen_up)
        if config.return_home:
            lines.append("G90G0 X0Y0")
    return lines


def save_gcode(lines: list[str], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)
