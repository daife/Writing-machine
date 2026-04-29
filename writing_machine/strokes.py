from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math

import cv2
import numpy as np
from PIL import Image
from skimage.morphology import skeletonize


Point = tuple[float, float]


@dataclass
class Stroke:
    points: list[Point]
    closed: bool = False


def _neighbors(point: tuple[int, int], pixels: set[tuple[int, int]]) -> list[tuple[int, int]]:
    x, y = point
    found = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            candidate = (x + dx, y + dy)
            if candidate in pixels:
                found.append(candidate)
    return found


def _path_length(points: list[Point]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


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


def _edge_key(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def _trace_from_node(
    start: tuple[int, int],
    nxt: tuple[int, int],
    pixels: set[tuple[int, int]],
    nodes: set[tuple[int, int]],
    visited_edges: set[tuple[tuple[int, int], tuple[int, int]]],
) -> list[tuple[int, int]]:
    path = [start, nxt]
    visited_edges.add(_edge_key(start, nxt))
    prev, cur = start, nxt
    while cur not in nodes:
        candidates = [item for item in _neighbors(cur, pixels) if item != prev]
        candidates = [item for item in candidates if _edge_key(cur, item) not in visited_edges]
        if not candidates:
            break
        chosen = candidates[0]
        visited_edges.add(_edge_key(cur, chosen))
        path.append(chosen)
        prev, cur = cur, chosen
    return path


def _trace_loop(seed: tuple[int, int], pixels: set[tuple[int, int]], visited: set[tuple[int, int]]) -> list[tuple[int, int]]:
    path = [seed]
    visited.add(seed)
    prev = None
    cur = seed
    while True:
        candidates = [item for item in _neighbors(cur, pixels) if item != prev and item not in visited]
        if not candidates:
            break
        nxt = candidates[0]
        path.append(nxt)
        visited.add(nxt)
        prev, cur = cur, nxt
    return path


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
    gray = np.array(rgba.convert("L"))
    return (gray < 240).astype(bool)


def prune_short_terminal_branches(skeleton: np.ndarray, max_branch_length: int, max_passes: int = 20) -> np.ndarray:
    if max_branch_length <= 0:
        return skeleton.astype(bool)
    pixels = set(zip(*np.where(skeleton.astype(bool))[::-1]))
    if not pixels:
        return skeleton.astype(bool)

    for _pass in range(max_passes):
        degree = {pixel: len(_neighbors(pixel, pixels)) for pixel in pixels}
        endpoints = sorted((pixel for pixel, deg in degree.items() if deg == 1), key=lambda item: (item[1], item[0]))
        to_remove: set[tuple[int, int]] = set()

        for endpoint in endpoints:
            if endpoint not in pixels or endpoint in to_remove:
                continue
            path = [endpoint]
            prev: tuple[int, int] | None = None
            cur = endpoint
            terminal_degree = degree[cur]

            while True:
                if cur != endpoint:
                    terminal_degree = degree[cur]
                    if terminal_degree != 2:
                        break
                candidates = [item for item in _neighbors(cur, pixels) if item != prev]
                if not candidates:
                    break
                nxt = candidates[0]
                path.append(nxt)
                prev, cur = cur, nxt
                if len(path) > max_branch_length + 1:
                    break

            branch_length = len(path) - 1
            if branch_length <= max_branch_length and terminal_degree >= 3:
                to_remove.update(path[:-1])

        if not to_remove:
            break
        pixels.difference_update(to_remove)

    pruned = np.zeros_like(skeleton, dtype=bool)
    for x, y in pixels:
        pruned[y, x] = True
    return pruned


def generate_strokes(
    image: Image.Image,
    simplify_epsilon: float = 2.0,
    min_points: int = 2,
    alpha_threshold: int = 20,
    mask_dilation_iterations: int = 0,
    mask_erosion_iterations: int = 0,
    spur_prune_length: int = 8,
    min_component_area: int = 12,
    min_stroke_length_px: float = 6.0,
) -> list[Stroke]:
    mask = image_to_mask(image, threshold=alpha_threshold)
    if not mask.any():
        return []
    if mask_dilation_iterations > 0:
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.dilate(mask.astype(np.uint8) * 255, kernel, iterations=int(mask_dilation_iterations)) > 0
    if mask_erosion_iterations > 0:
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.erode(mask.astype(np.uint8) * 255, kernel, iterations=int(mask_erosion_iterations)) > 0
        if not mask.any():
            return []
    mask = _remove_small_components(mask, min_component_area)
    if not mask.any():
        return []

    skeleton = prune_short_terminal_branches(skeletonize(mask), spur_prune_length)
    count, labels = cv2.connectedComponents(skeleton.astype(np.uint8), 8)
    strokes: list[Stroke] = []

    for label in range(1, count):
        ys, xs = np.where(labels == label)
        pixels = set(zip(xs.tolist(), ys.tolist()))
        if len(pixels) < min_points:
            continue
        degree = {pixel: len(_neighbors(pixel, pixels)) for pixel in pixels}
        nodes = {pixel for pixel, deg in degree.items() if deg != 2}
        visited_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

        if nodes:
            for node in sorted(nodes, key=lambda item: (item[1], item[0])):
                for nxt in _neighbors(node, pixels):
                    if _edge_key(node, nxt) in visited_edges:
                        continue
                    raw = _trace_from_node(node, nxt, pixels, nodes, visited_edges)
                    points = [(float(x), float(y)) for x, y in raw]
                    points = rdp(points, simplify_epsilon)
                    if len(points) >= min_points and _path_length(points) >= min_stroke_length_px:
                        strokes.append(Stroke(points=points, closed=False))
        else:
            visited: set[tuple[int, int]] = set()
            for pixel in pixels:
                if pixel in visited:
                    continue
                raw = _trace_loop(pixel, pixels, visited)
                points = [(float(x), float(y)) for x, y in raw]
                points = rdp(points, simplify_epsilon)
                if len(points) >= min_points and _path_length(points) >= min_stroke_length_px:
                    strokes.append(Stroke(points=points, closed=True))

    return sorted(strokes, key=lambda stroke: (stroke.points[0][1], stroke.points[0][0]))


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
