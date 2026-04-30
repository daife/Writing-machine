from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

from PIL import Image, ImageDraw

from writing_machine.strokes import _command_prefix, _display_command, generate_strokes


def _write_fake_tools(directory: Path) -> tuple[list[str], list[str]]:
    fake_autotrace = directory / "fake_autotrace.py"
    fake_vpype = directory / "fake_vpype.py"
    fake_autotrace.write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            import sys
            from PIL import Image

            input_path = Path(sys.argv[1])
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
            args = _command_prefix(f'"{tool}"') + ["input.png", "-centerline", "-output-file", "raw.svg"]

        self.assertEqual(args[0], str(tool))
        self.assertTrue(_display_command(args).startswith("& "))
        self.assertIn(f'"{tool}"', _display_command(args))

    def test_empty_transparent_image_returns_no_strokes_without_tools(self) -> None:
        strokes = generate_strokes(Image.new("RGBA", (20, 20), (0, 0, 0, 0)))

        self.assertEqual(strokes, [])

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
                simplify_epsilon=0.5,
                min_component_area=3,
                min_stroke_length_px=4,
                svg_sample_step_px=4,
                work_dir=tmp_path,
                autotrace_command=autotrace,
                vpype_command=vpype,
            )

            self.assertTrue((tmp_path / "trace_input.png").exists())
            self.assertTrue((tmp_path / "trace_raw.svg").exists())
            self.assertTrue((tmp_path / "trace_optimized.svg").exists())

        self.assertEqual(len(strokes), 1)
        self.assertGreaterEqual(strokes[0].points[0][0], 8)
        self.assertGreaterEqual(strokes[0].points[-1][0], 50)

    def test_vpype_pipeline_uses_merge_simplify_and_sort(self) -> None:
        image = Image.new("RGBA", (30, 20), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((4, 10, 26, 10), fill=(0, 0, 0, 255), width=1)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            autotrace, vpype = _write_fake_tools(tmp_path)
            strokes = generate_strokes(
                image,
                simplify_epsilon=1.25,
                min_component_area=1,
                min_stroke_length_px=8,
                svg_sample_step_px=5,
                work_dir=tmp_path,
                autotrace_command=autotrace,
                vpype_command=vpype,
            )
            vpype_args = (tmp_path / "vpype_args.txt").read_text(encoding="utf-8")

        self.assertEqual(len(strokes), 1)
        self.assertIn("linemerge", vpype_args)
        self.assertIn("linesimplify", vpype_args)
        self.assertIn("--tolerance", vpype_args)
        self.assertIn("1.25px", vpype_args)
        self.assertIn("linesort", vpype_args)


if __name__ == "__main__":
    unittest.main()
