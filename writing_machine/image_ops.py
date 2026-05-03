from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .models import AnswerLayer, ReferenceCanvas


def ensure_artifacts_dir(root: str | Path = ".") -> Path:
    path = Path(root) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_rgba(path: str | Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def validate_quad(points: list[tuple[float, float]]) -> None:
    if len(points) != 4:
        raise ValueError("需要选择 4 个角点。")
    if abs(polygon_area(points)) < 100:
        raise ValueError("角点围成的区域太小，请重新选择。")


def perspective_transform(
    image: Image.Image,
    points: list[tuple[float, float]],
    width_mm: float,
    height_mm: float,
    px_per_mm: float = 10.0,
) -> tuple[Image.Image, ReferenceCanvas]:
    validate_quad(points)
    width_px = max(1, int(round(width_mm * px_per_mm)))
    height_px = max(1, int(round(height_mm * px_per_mm)))
    src = np.array(points, dtype=np.float32)
    dst = np.array(
        [[0, 0], [width_px - 1, 0], [width_px - 1, height_px - 1], [0, height_px - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    cv_image = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    warped = cv2.warpPerspective(cv_image, matrix, (width_px, height_px), flags=cv2.INTER_CUBIC)
    result = Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)).convert("RGBA")
    ref = ReferenceCanvas(
        image_path="",
        width_mm=width_mm,
        height_mm=height_mm,
        width_px=width_px,
        height_px=height_px,
        px_per_mm=px_per_mm,
    )
    return result, ref


def transform_points_rotate_cw(points: list[tuple[float, float]], _width: int, height: int) -> list[tuple[float, float]]:
    return [(height - 1 - y, x) for x, y in points]


def transform_points_flip_h(points: list[tuple[float, float]], width: int) -> list[tuple[float, float]]:
    return [(width - 1 - x, y) for x, y in points]


def transform_points_flip_v(points: list[tuple[float, float]], height: int) -> list[tuple[float, float]]:
    return [(x, height - 1 - y) for x, y in points]


def _remove_small_components(mask: np.ndarray, min_area: int) -> np.ndarray:
    if min_area <= 1:
        return mask
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
    cleaned = np.zeros_like(mask, dtype=np.uint8)
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            cleaned[labels == label] = 255
    return cleaned


def extract_black_handwriting(
    image: Image.Image,
    threshold: int = 245,
    min_area: int = 5,
) -> Image.Image:
    rgba = image.convert("RGBA")
    rgb = np.array(rgba.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    alpha_source = np.array(rgba.getchannel("A"))
    mask = ((gray < threshold) & (alpha_source > 0)).astype(np.uint8) * 255
    mask = _remove_small_components(mask, min_area)
    alpha = Image.fromarray(mask, "L")
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    black.putalpha(alpha)
    return black


def binarize_black_on_transparent(image: Image.Image, threshold: int = 245) -> Image.Image:
    rgba = image.convert("RGBA")
    rgb = np.array(rgba.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    source_alpha = np.array(rgba.getchannel("A"))
    alpha = ((gray < threshold) & (source_alpha > 0)).astype(np.uint8) * 255
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    black.putalpha(Image.fromarray(alpha, "L"))
    return black


def prepare_answer_layer_image(image: Image.Image, threshold: int = 245, min_area: int = 5) -> Image.Image:
    processed = extract_black_handwriting(image, threshold=threshold, min_area=min_area)
    alpha_bbox = processed.getchannel("A").getbbox()
    if alpha_bbox is None:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    return processed.crop(alpha_bbox)


def render_layer(layer: AnswerLayer) -> Image.Image:
    image = load_rgba(layer.source_path)
    width, height = image.size
    left = max(0, min(width - 1, layer.crop_left))
    top = max(0, min(height - 1, layer.crop_top))
    right_crop = max(0, min(width - left - 1, layer.crop_right))
    bottom_crop = max(0, min(height - top - 1, layer.crop_bottom))
    image = image.crop((left, top, width - right_crop, height - bottom_crop))
    if layer.scale != 1.0:
        new_size = (
            max(1, int(round(image.width * layer.scale))),
            max(1, int(round(image.height * layer.scale))),
        )
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    if layer.rotation_deg:
        image = image.rotate(-layer.rotation_deg, expand=True, resample=Image.Resampling.BICUBIC)
    return image


def composite_layers(reference: ReferenceCanvas, layers: list[AnswerLayer]) -> Image.Image:
    canvas = Image.new("RGBA", (reference.width_px, reference.height_px), (0, 0, 0, 0))
    for layer in layers:
        if not layer.enabled:
            continue
        rendered = render_layer(layer)
        canvas.alpha_composite(rendered, (int(round(layer.x)), int(round(layer.y))))
    return canvas


def make_preview(reference: ReferenceCanvas, layers: list[AnswerLayer], show_background: bool = True) -> Image.Image:
    if show_background and reference.image_path:
        base = load_rgba(reference.image_path).resize((reference.width_px, reference.height_px))
        overlay = Image.new("RGBA", base.size, (255, 255, 255, 80))
        base = Image.alpha_composite(base, overlay)
    else:
        base = Image.new("RGBA", (reference.width_px, reference.height_px), (255, 255, 255, 255))
    handwriting = composite_layers(reference, layers)
    return Image.alpha_composite(base, handwriting)


def save_png(image: Image.Image, path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return str(target)
