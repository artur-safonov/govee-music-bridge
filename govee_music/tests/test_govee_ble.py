import functools
import operator
import unittest

from govee_ble import (
    APPLY_PALETTE_FRAME,
    CAPTURED_RED_PALETTE,
    fixed_music_frame,
    palette_frames,
    parse_rgb,
)


class GoveeBLEFrameTests(unittest.TestCase):
    def test_rebuilds_exact_captured_red_frames(self):
        first, second = palette_frames(CAPTURED_RED_PALETTE)
        self.assertEqual(
            first.hex(" "),
            "a3 00 01 02 51 08 ff 47 23 ff 3c 1e ff 32 19 ff 28 14 ff 57",
        )
        self.assertEqual(
            second.hex(" "),
            "a3 ff 1e 0f ff 14 0a ff 0a 05 ff 00 00 00 00 00 00 00 00 a3",
        )

    def test_all_generated_frames_have_valid_xor_checksum(self):
        frames = (
            *palette_frames(((0, 0, 255),) * 8),
            APPLY_PALETTE_FRAME,
            fixed_music_frame(99),
        )
        for frame in frames:
            self.assertEqual(len(frame), 20)
            self.assertEqual(functools.reduce(operator.xor, frame, 0), 0)

    def test_parses_hex_and_decimal_colors(self):
        self.assertEqual(parse_rgb("#12abEF"), (0x12, 0xAB, 0xEF))
        self.assertEqual(parse_rgb("1, 2, 255"), (1, 2, 255))


if __name__ == "__main__":
    unittest.main()
