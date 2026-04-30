from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from writing_machine.image_ops import prepare_answer_layer_image


class ImageOpsTests(unittest.TestCase):
    def test_prepare_answer_layer_image_binarizes_and_trims_transparent_margin(self) -> None:
        image = Image.new("RGBA", (40, 30), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 8, 29, 19), fill=(0, 0, 0, 255))

        processed = prepare_answer_layer_image(image)

        self.assertLess(processed.width, image.width)
        self.assertLess(processed.height, image.height)
        self.assertEqual(processed.getpixel((0, 0))[3], 255)
        self.assertEqual(processed.getpixel((processed.width - 1, processed.height - 1))[3], 255)
        self.assertEqual(processed.getpixel((0, 0))[:3], (0, 0, 0))

    def test_prepare_answer_layer_image_threshold_filters_faint_gray(self) -> None:
        image = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 5, 14, 14), fill=(235, 235, 235, 255))

        processed = prepare_answer_layer_image(image, threshold=230)

        self.assertEqual(processed.size, (1, 1))
        self.assertEqual(processed.getpixel((0, 0))[3], 0)


if __name__ == "__main__":
    unittest.main()
