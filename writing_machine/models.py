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
    px_per_mm: float = 10.0


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
    threshold: int = 120
    min_area: int = 5
    erosion_iterations: int = 0
    already_extracted: bool = False
    enabled: bool = True


@dataclass
class MachineConfig:
    port: str = "COM3"
    baud: int = 115200
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    invert_x: bool = False
    invert_y: bool = False
    travel_feed: int = 2500
    write_feed: int = 1500
    pen_down: str = "M3S1000"
    pen_up: str = "M5"
    dwell_seconds: float = 0.2
    return_home: bool = True


@dataclass
class StrokeConfig:
    simplify_epsilon: float = 2.0
    alpha_threshold: int = 20
    min_points: int = 2
    mask_erosion_iterations: int = 0
    spur_prune_length: int = 8


@dataclass
class TextRenderConfig:
    width: int = 900
    height: int = 320
    font_size: int = 42
    perturb_x_sigma: float = 1.2
    perturb_y_sigma: float = 1.2
    perturb_theta_sigma: float = 0.035
    seed: int = 42


@dataclass
class ProjectState:
    reference: ReferenceCanvas | None = None
    layers: list[AnswerLayer] = field(default_factory=list)
    machine: MachineConfig = field(default_factory=MachineConfig)
    stroke: StrokeConfig = field(default_factory=StrokeConfig)
    text_render: TextRenderConfig = field(default_factory=TextRenderConfig)
    rotate_trajectory_180: bool = False

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
        if data.get("stroke"):
            state.stroke = StrokeConfig(**data["stroke"])
        if data.get("text_render"):
            state.text_render = TextRenderConfig(**data["text_render"])
        state.rotate_trajectory_180 = bool(data.get("rotate_trajectory_180", False))
        return state
