from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

from PIL import Image, ImageDraw

from writing_machine import strokes as strokes_module
from writing_machine.strokes import (
    MASK_MORPHOLOGY_DILATE_THEN_ERODE,
    MASK_MORPHOLOGY_ERODE_THEN_DILATE,
    VPYPE_INPROCESS,
    _apply_mask_morphology,
    _command_prefix,
    _display_command,
    generate_strokes,
)


def _write_fake_tools(directory: Path) -> tuple[list[str], list[str]]:
    fake_autotrace = directory / "fake_autotrace.py"
    fake_vpype = directory / "fake_vpype.py"
    fake_autotrace.write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            import sys
            from PIL import Image

            (Path.cwd() / "autotrace_args.txt").write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
            input_path = Path(sys.argv[sys.argv.index("-output-format") - 1])
            output_path = Path(sys.argv[sys.argv.index("-output-file") + 1])
            image = Image.open(input_path).convert("L")
            black = [(x, y) for y in range(image.height) for x in range(image.width) if image.getpixel((x, y)) < 128]
            if black:
                xs = [x for x, _y in black]
                ys = [y for _x, y in black]
                y = (min(ys) + max(ys)) / 2
                d = f"M {min(xs)} {y:.3f} L {max(xs)} {y:.3f}"
            else:
                d = ""
            output_path.write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{image.width}" height="{image.height}">'
                f'<path d="{d}" fill="none" stroke="black"/></svg>',
                encoding="utf-8",
            )
            """
        ).strip(),
        encoding="utf-8",
    )
    fake_vpype.write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            import shutil
            import sys

            args_path = Path.cwd() / "vpype_args.txt"
            args_path.write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
            input_path = Path(sys.argv[sys.argv.index("read") + 1])
            output_path = Path(sys.argv[-1])
            shutil.copyfile(input_path, output_path)
            """
        ).strip(),
        encoding="utf-8",
    )
    return [sys.executable, str(fake_autotrace)], [sys.executable, str(fake_vpype)]


class StrokeTracePipelineTests(unittest.TestCase):
    def test_quoted_tool_path_is_normalized_and_displayed_for_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tool = tmp_path / "fake tool.exe"
            tool.write_text("", encoding="utf-8")
            args = _command_prefix(f'"{tool}"') + ["input.bmp", "-centerline", "-output-file", "raw.svg"]

        self.assertEqual(args[0], str(tool))
        self.assertTrue(_display_command(args).startswith("& "))
        self.assertIn(f'"{tool}"', _display_command(args))

    def test_default_vpype_uses_inprocess_runner_for_plugin_registration(self) -> None:
        original = getattr(strokes_module.sys, "frozen", None)
        strokes_module.sys.frozen = True
        try:
            args = _command_prefix("vpype") + ["read", "raw.svg", "write", "optimized.svg"]
        finally:
            if original is None:
                delattr(strokes_module.sys, "frozen")
            else:
                strokes_module.sys.frozen = original

        self.assertEqual(args[0], VPYPE_INPROCESS)
        self.assertEqual(_display_command(args), "vpype read raw.svg write optimized.svg")

    def test_empty_transparent_image_returns_no_strokes_without_tools(self) -> None:
        strokes = generate_strokes(Image.new("RGBA", (20, 20), (0, 0, 0, 0)))

        self.assertEqual(strokes, [])

    def test_mask_morphology_order_controls_erosion_and_dilation_sequence(self) -> None:
        image = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((1, 1, 5, 5), fill=(0, 0, 0, 255))
        draw.point((7, 7), fill=(0, 0, 0, 255))
        mask = strokes_module.image_to_mask(image)

        closed = _apply_mask_morphology(mask, 1, 1, MASK_MORPHOLOGY_DILATE_THEN_ERODE)
        opened = _apply_mask_morphology(mask, 1, 1, MASK_MORPHOLOGY_ERODE_THEN_DILATE)

        self.assertTrue(closed[7, 7])
        self.assertFalse(opened[7, 7])
        self.assertGreater(int(closed.sum()), int(opened.sum()))

    def test_small_components_are_removed_before_centerline_trace(self) -> None:
        image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((8, 20, 52, 20), fill=(0, 0, 0, 255), width=1)
        draw.point((3, 3), fill=(0, 0, 0, 255))
        draw.point((10, 5), fill=(0, 0, 0, 255))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            autotrace, vpype = _write_fake_tools(tmp_path)
            strokes = generate_strokes(
                image,
                min_component_area=3,
                trace_debug_artifacts=True,
                autotrace_background_color="FFFFFF",
                autotrace_color_count=2,
                autotrace_error_threshold=1.5,
                autotrace_filter_iterations=0,
                work_dir=tmp_path,
                autotrace_command=autotrace,
                vpype_command=vpype,
            )
            autotrace_args = (tmp_path / "autotrace_args.txt").read_text(encoding="utf-8")

            self.assertTrue((tmp_path / "trace_input.bmp").exists())
            self.assertTrue((tmp_path / "trace_raw.svg").exists())
            self.assertTrue((tmp_path / "trace_linemerge.svg").exists())
            self.assertTrue((tmp_path / "trace_forkclean.svg").exists())
            self.assertTrue((tmp_path / "trace_linesimplify.svg").exists())
            self.assertTrue((tmp_path / "trace_optimized.svg").exists())

        self.assertEqual(len(strokes), 1)
        self.assertEqual(len(strokes[0].points), 2)
        self.assertGreaterEqual(strokes[0].points[0][0], 8)
        self.assertGreaterEqual(strokes[0].points[-1][0], 50)
        self.assertIn("-centerline", autotrace_args)
        self.assertIn("-background-color", autotrace_args)
        self.assertIn("FFFFFF", autotrace_args)
        self.assertIn("-color-count", autotrace_args)
        self.assertIn("2", autotrace_args)
        self.assertIn("-error-threshold", autotrace_args)
        self.assertIn("1.5", autotrace_args)
        self.assertIn("-filter-iterations", autotrace_args)

    def test_vpype_pipeline_uses_merge_simplify_and_sort(self) -> None:
        image = Image.new("RGBA", (30, 20), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((4, 10, 26, 10), fill=(0, 0, 0, 255), width=1)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            autotrace, vpype = _write_fake_tools(tmp_path)
            strokes = generate_strokes(
                image,
                min_component_area=1,
                vpype_linemerge_tolerance_px=1.5,
                forkclean_max_search_length_px=25,
                forkclean_min_short_length_px=4,
                forkclean_angle_threshold_deg=100,
                forkclean_node_tolerance_px=0.75,
                forkclean_passes=2,
                vpype_linesimplify_tolerance_px=0.4,
                work_dir=tmp_path,
                autotrace_command=autotrace,
                vpype_command=vpype,
            )
            vpype_args = (tmp_path / "vpype_args.txt").read_text(encoding="utf-8")

        self.assertEqual(len(strokes), 1)
        self.assertIn("linemerge", vpype_args)
        self.assertIn("1.5px", vpype_args)
        self.assertIn("forkclean", vpype_args)
        self.assertIn("--max-search-length", vpype_args)
        self.assertIn("25.0px", vpype_args)
        self.assertIn("--min-short-length", vpype_args)
        self.assertIn("4.0px", vpype_args)
        self.assertIn("--angle-threshold", vpype_args)
        self.assertIn("100.0deg", vpype_args)
        self.assertIn("--node-tolerance", vpype_args)
        self.assertIn("0.75px", vpype_args)
        self.assertIn("--passes", vpype_args)
        self.assertIn("2", vpype_args)
        self.assertIn("linesimplify", vpype_args)
        self.assertIn("0.4px", vpype_args)
        self.assertIn("linesort", vpype_args)
        self.assertIn("--two-opt", vpype_args)


if __name__ == "__main__":
    unittest.main()
