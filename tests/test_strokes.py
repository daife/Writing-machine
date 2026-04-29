from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from writing_machine.strokes import generate_strokes


class StrokeCleanupTests(unittest.TestCase):
    def test_small_components_are_removed_before_skeletonizing(self) -> None:
        image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((8, 20, 52, 20), fill=(0, 0, 0, 255), width=1)
        draw.point((3, 3), fill=(0, 0, 0, 255))
        draw.point((10, 5), fill=(0, 0, 0, 255))

        strokes = generate_strokes(
            image,
            simplify_epsilon=0.5,
            min_component_area=3,
            min_stroke_length_px=4,
            spur_prune_length=0,
        )

        self.assertEqual(len(strokes), 1)
        self.assertGreaterEqual(strokes[0].points[0][0], 8)
        self.assertGreaterEqual(strokes[0].points[-1][0], 8)

    def test_short_traced_paths_are_filtered_after_skeletonizing(self) -> None:
        image = Image.new("RGBA", (30, 20), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((4, 10, 8, 10), fill=(0, 0, 0, 255), width=1)
        draw.line((12, 10, 26, 10), fill=(0, 0, 0, 255), width=1)

        strokes = generate_strokes(
            image,
            simplify_epsilon=0.5,
            min_component_area=1,
            min_stroke_length_px=8,
            spur_prune_length=0,
        )

        self.assertEqual(len(strokes), 1)
        self.assertGreaterEqual(strokes[0].points[-1][0] - strokes[0].points[0][0], 10)


if __name__ == "__main__":
    unittest.main()
