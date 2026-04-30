from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import json


@dataclass
class ReferenceCanvas:
    image_path: str
    width_mm: float
    height_mm: float
    width_px: int
    height_px: int
    px_per_mm: float = 5.0


@dataclass
class AnswerLayer:
    source_path: str
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation_deg: float = 0.0
    crop_left: int = 0
    crop_top: int = 0
    crop_right: int = 0
    crop_bottom: int = 0
    min_area: int = 3
    already_extracted: bool = False
    enabled: bool = True


@dataclass
class MachineConfig:
    port: str = "COM3"
    baud: int = 115200
    dry_run: bool = True
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    invert_x: bool = False
    invert_y: bool = False
    travel_feed: int = 4000
    write_feed: int = 4000
    pen_down: str = "G1G90 Z4.0F10000"
    pen_up: str = "G1G90 Z0.0F10000"
    dwell_seconds: float = 0.0
    return_home: bool = True
    set_work_zero: bool = True


@dataclass
class SerialProxyConfig:
    virtual_port: str = "COM2"
    real_port: str = "COM13"
    baud: int = 115200
    log_path: str = "logs/serial_log.txt"


@dataclass
class StrokeConfig:
    simplify_epsilon: float = 2.0
    alpha_threshold: int = 20
    min_points: int = 2
    mask_dilation_iterations: int = 0
    mask_erosion_iterations: int = 0
    mask_morphology_order: str = "dilate_then_erode"
    spur_prune_length: int = 8
    min_component_area: int = 2
    min_stroke_length_px: float = 1.0
    svg_sample_step_px: float = 2.5


@dataclass
class TextRenderConfig:
    font_size: int = 84
    char_spacing: int = 2
    line_spacing: int = 18
    line_width: int = 860
    min_area: int = 5
    font_path: str = ""


@dataclass
class ProjectState:
    reference: ReferenceCanvas | None = None
    layers: list[AnswerLayer] = field(default_factory=list)
    machine: MachineConfig = field(default_factory=MachineConfig)
    serial_proxy: SerialProxyConfig = field(default_factory=SerialProxyConfig)
    stroke: StrokeConfig = field(default_factory=StrokeConfig)
    text_render: TextRenderConfig = field(default_factory=TextRenderConfig)
    rotate_trajectory_180: bool = False
    eraser_radius: int = 18
    answer_import_threshold: int = 245

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ProjectState":
        data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        state = cls()
        if data.get("reference"):
            state.reference = ReferenceCanvas(**data["reference"])
        state.layers = [AnswerLayer(**item) for item in data.get("layers", [])]
        if data.get("machine"):
            state.machine = MachineConfig(**data["machine"])
            legacy_grbl_defaults = (
                state.machine.travel_feed == 2500
                and state.machine.write_feed == 1500
                and state.machine.pen_down == "M3S1000"
                and state.machine.pen_up == "M5"
                and abs(state.machine.dwell_seconds - 0.2) < 0.0001
            )
            if legacy_grbl_defaults:
                state.machine.travel_feed = 4000
                state.machine.write_feed = 4000
                state.machine.pen_down = "G1G90 Z4.0F10000"
                state.machine.pen_up = "G1G90 Z0.0F10000"
                state.machine.dwell_seconds = 0.0
        if data.get("serial_proxy"):
            state.serial_proxy = SerialProxyConfig(**data["serial_proxy"])
        if data.get("stroke"):
            state.stroke = StrokeConfig(**data["stroke"])
        if data.get("text_render"):
            state.text_render = TextRenderConfig(**data["text_render"])
        state.rotate_trajectory_180 = bool(data.get("rotate_trajectory_180", False))
        state.eraser_radius = int(data.get("eraser_radius", state.eraser_radius))
        state.answer_import_threshold = int(data.get("answer_import_threshold", state.answer_import_threshold))
        return state
