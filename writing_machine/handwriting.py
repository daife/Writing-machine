from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .image_ops import extract_black_handwriting


def default_font_path() -> str:
    candidates = [
        r"C:\Windows\Fonts\simkai.ttf",
        r"C:\Windows\Fonts\simfang.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return ""


def render_text(
    text: str,
    output_path: str | Path,
    font_path: str | None = None,
    font_size: int = 42,
    char_spacing: int = 2,
    line_spacing: int = 18,
    line_width: int = 860,
    min_area: int = 1,
    left_margin: int = 20,
    top_margin: int = 20,
    perturb_x_sigma: float = 1.2,
    perturb_y_sigma: float = 1.2,
    perturb_theta_sigma: float = 0.035,
    seed: int | None = 42,
) -> str:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_path = font_path or default_font_path()

    if seed is not None:
        random.seed(seed)
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    scratch = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    scratch_draw = ImageDraw.Draw(scratch)
    max_line_width = max(1, int(line_width))
    char_gap = max(0, int(char_spacing))
    lines: list[list[str]] = []
    for paragraph in text.splitlines() or [""]:
        current: list[str] = []
        current_width = 0
        for char in paragraph:
            bbox = scratch_draw.textbbox((0, 0), char, font=font)
            char_width = max(1, bbox[2] - bbox[0])
            next_width = char_width if not current else current_width + char_gap + char_width
            if current and next_width > max_line_width:
                lines.append(current)
                current = [char]
                current_width = char_width
            else:
                current.append(char)
                current_width = next_width
        lines.append(current)

    bottom_margin = top_margin
    line_step = max(1, int(font_size + line_spacing))
    width = left_margin + max_line_width + left_margin
    height = top_margin + max(1, len(lines)) * line_step - max(0, int(line_spacing)) + bottom_margin
    image = Image.new("RGBA", (width, max(1, height)), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    y = top_margin
    for line in lines:
        x = left_margin
        for char in line:
            jitter_x = random.gauss(0, perturb_x_sigma)
            jitter_y = random.gauss(0, perturb_y_sigma)
            bbox = draw.textbbox((0, 0), char, font=font)
            char_width = max(1, bbox[2] - bbox[0])
            char_height = max(1, bbox[3] - bbox[1])
            char_image = Image.new("RGBA", (char_width + 8, char_height + 8), (255, 255, 255, 0))
            char_draw = ImageDraw.Draw(char_image)
            char_draw.text((4 - bbox[0], 4 - bbox[1]), char, font=font, fill=(0, 0, 0, 255))
            theta = random.gauss(0, perturb_theta_sigma)
            if abs(theta) > 0.001:
                char_image = char_image.rotate(theta * 180 / 3.141592653589793, expand=True, resample=Image.Resampling.BICUBIC)
            image.alpha_composite(char_image, (int(round(x + jitter_x)), int(round(y + jitter_y))))
            x += char_width + char_gap
        y += line_step
    extract_black_handwriting(image, min_area=min_area).save(target)
    return str(target)
