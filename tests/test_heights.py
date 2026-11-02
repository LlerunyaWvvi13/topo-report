import math

import pytest

from app.heights import height_map_png, height_stats, metres_per_pixel, slope_stats


def test_stats_on_known_values():
    stats = height_stats([100.0, 101.0, 103.0])
    assert stats.minimum == 100
    assert stats.maximum == 103
    assert stats.spread == 3
    assert stats.mean == pytest.approx(101.333, abs=0.001)
    assert stats.median == 101


def test_stats_empty():
    assert height_stats([]) is None


def test_metres_per_pixel_for_300_dpi_and_1_500():
    assert metres_per_pixel(300, 500) == pytest.approx(0.04233, abs=1e-5)


def test_metres_per_pixel_needs_both_values():
    assert metres_per_pixel(None, 500) is None
    assert metres_per_pixel(300, None) is None


def test_slope_of_known_plane_falls_to_east():
    # Высота падает на 1 м на каждые 100 м к востоку. Один пиксель равен 1 м.
    xs = [0.0, 100.0, 0.0, 100.0, 50.0]
    ys = [0.0, 0.0, 100.0, 100.0, 50.0]
    values = [150 - x / 100 for x in xs]
    slope = slope_stats(xs, ys, values, 1.0)
    assert slope.mean_percent == pytest.approx(1.0, abs=0.001)
    assert slope.max_percent == pytest.approx(1.0, abs=0.001)
    assert slope.mean_degrees == pytest.approx(math.degrees(math.atan(0.01)), abs=0.01)
    assert slope.direction == "восток"


def test_slope_direction_uses_top_of_sheet_as_north():
    xs = [0.0, 100.0, 0.0, 100.0]
    ys = [0.0, 0.0, 100.0, 100.0]
    # Ниже по листу (к югу) высота растет, значит склон падает на север.
    values = [150 + y / 100 for y in ys]
    assert slope_stats(xs, ys, values, 1.0).direction == "север"


def test_narrow_edge_triangles_do_not_inflate_max_slope():
    # Две точки почти рядом вдоль края дают узкий треугольник с огромным ложным уклоном.
    xs = [0.0, 100.0, 200.0, 0.0, 100.0, 200.0, 100.0]
    ys = [0.0, 0.0, 0.0, 100.0, 100.0, 100.0, 0.4]
    values = [100.0, 101.0, 102.0, 100.0, 101.0, 102.0, 103.0]
    assert slope_stats(xs, ys, values, 1.0).max_percent < 60


def test_slope_needs_scale_and_non_collinear_points():
    assert slope_stats([0, 1, 2], [0, 1, 3], [1, 2, 3], None) is None
    assert slope_stats([0, 1, 2], [0, 1, 2], [1, 2, 3], 1.0) is None


def test_height_map_is_png():
    png = height_map_png([0, 100, 0, 100], [0, 0, 80, 80], [10.0, 11.0, 12.0, 13.5], 0.5, 0.5, 0.8)
    assert png.startswith(b"\x89PNG")


def test_height_map_needs_surface():
    assert height_map_png([0, 1], [0, 1], [1.0, 2.0], 1.0, 0.5, 1.0) is None
    assert height_map_png([0, 1, 2], [0, 1, 2], [1.0, 2.0, 3.0], 1.0, 0.5, 1.0) is None
