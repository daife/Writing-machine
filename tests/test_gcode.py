from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from writing_machine.gcode import pixel_to_mm, strokes_to_gcode
from writing_machine.models import MachineConfig, ProjectState, ReferenceCanvas, StrokeConfig
from writing_machine.strokes import Stroke


class GCodeCoordinateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = ReferenceCanvas(
            image_path="",
            width_mm=210.0,
            height_mm=297.0,
            width_px=2100,
            height_px=2970,
            px_per_mm=10.0,
        )
        self.config = MachineConfig()

    def test_normal_mode_matches_official_top_left_mapping(self) -> None:
        x_mm, y_mm = pixel_to_mm(204.82, 119.47, self.reference, self.config)

        self.assertAlmostEqual(x_mm, 20.482, places=3)
        self.assertAlmostEqual(y_mm, -11.947, places=3)

    def test_diagonal_mode_matches_official_180_mapping(self) -> None:
        x_mm, y_mm = pixel_to_mm(204.82, 119.47, self.reference, self.config, diagonal_origin_180=True)

        self.assertAlmostEqual(x_mm, 189.518, places=3)
        self.assertAlmostEqual(y_mm, -285.053, places=3)

    def test_diagonal_mode_has_no_extra_corner_move(self) -> None:
        lines = strokes_to_gcode(
            [Stroke(points=[(204.82, 119.47), (203.73, 123.86)])],
            self.reference,
            self.config,
            diagonal_origin_180=True,
        )

        self.assertNotIn("G0 X210.000Y297.000F4000", lines)
        self.assertNotIn("G0 X210.000Y-297.000F4000", lines)
        self.assertIn("G0 X189.518Y-285.053F4000", lines)
        self.assertIn("G1 X189.627Y-284.614F4000", lines)


class MachineConfigMigrationTests(unittest.TestCase):
    def test_legacy_defaults_migrate_to_captured_z_axis_protocol(self) -> None:
        state = ProjectState()
        state.machine = MachineConfig(travel_feed=2500, write_feed=1500, pen_down="M3S1000", pen_up="M5", dwell_seconds=0.2)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            state.save(path)
            loaded = ProjectState.load(path)

        self.assertEqual(loaded.machine.travel_feed, 4000)
        self.assertEqual(loaded.machine.write_feed, 4000)
        self.assertEqual(loaded.machine.pen_down, "G1G90 Z4.0F10000")
        self.assertEqual(loaded.machine.pen_up, "G1G90 Z0.0F10000")
        self.assertEqual(loaded.machine.dwell_seconds, 0.0)

    def test_stroke_morphology_order_is_saved_and_legacy_configs_default(self) -> None:
        state = ProjectState()
        state.stroke = StrokeConfig(
            mask_dilation_iterations=1,
            mask_erosion_iterations=2,
            mask_morphology_order="erode_then_dilate",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            state.save(path)
            loaded = ProjectState.load(path)

            legacy_path = Path(tmp) / "legacy_project.json"
            legacy_path.write_text(
                '{"stroke": {"mask_dilation_iterations": 1, "mask_erosion_iterations": 2}}',
                encoding="utf-8",
            )
            legacy_loaded = ProjectState.load(legacy_path)

        self.assertEqual(loaded.stroke.mask_morphology_order, "erode_then_dilate")
        self.assertEqual(legacy_loaded.stroke.mask_morphology_order, "dilate_then_erode")


if __name__ == "__main__":
    unittest.main()
