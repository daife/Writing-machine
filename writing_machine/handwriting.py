from __future__ import annotations

from pathlib import Path
import random

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
    width: int = 900,
    height: int = 300,
    font_path: str | None = None,
    font_size: int = 42,
    line_spacing: int = 18,
    left_margin: int = 20,
    top_margin: int = 20,
    use_handright: bool = True,
    perturb_x_sigma: float = 1.2,
    perturb_y_sigma: float = 1.2,
    perturb_theta_sigma: float = 0.035,
    seed: int | None = 42,
) -> str:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_path = font_path or default_font_path()

    if use_handright and font_path:
        try:
            from handright import Template, handwrite

            background = Image.new("RGB", (width, height), "white")
            font = ImageFont.truetype(font_path, font_size)
            template = Template(
                background=background,
                font=font,
                fill=(0, 0, 0),
                left_margin=left_margin,
                top_margin=top_margin,
                right_margin=20,
                bottom_margin=20,
                line_spacing=font_size + line_spacing,
                perturb_x_sigma=perturb_x_sigma,
                perturb_y_sigma=perturb_y_sigma,
                perturb_theta_sigma=perturb_theta_sigma,
            )
            image = next(iter(handwrite(text, template, seed=seed))).convert("RGBA")
            extract_black_handwriting(image, threshold=210, min_area=2).save(target)
            return str(target)
        except Exception as exc:
            raise RuntimeError(f"Handright 生成失败：{exc}") from exc

    if seed is not None:
        random.seed(seed)
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    if font_path:
        font = ImageFont.truetype(font_path, font_size)
    else:
        font = ImageFont.load_default()

    x = left_margin
    y = top_margin
    for line in text.splitlines() or [""]:
        for char in line:
            jitter_x = random.gauss(0, perturb_x_sigma)
            jitter_y = random.gauss(0, perturb_y_sigma)
            draw.text((x + jitter_x, y + jitter_y), char, font=font, fill=(0, 0, 0, 255))
            bbox = draw.textbbox((0, 0), char, font=font)
            x += max(1, bbox[2] - bbox[0]) + random.gauss(0, max(0.0, perturb_x_sigma) / 2)
            if x > width - 40:
                x = left_margin
                y += font_size + line_spacing
        x = left_margin
        y += font_size + line_spacing
    image.save(target)
    return str(target)
