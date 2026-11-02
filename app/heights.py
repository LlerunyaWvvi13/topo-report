"""Расчет высотных показателей и схема рельефа."""

from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from scipy.interpolate import griddata  # noqa: E402
from scipy.spatial import Delaunay, QhullError  # noqa: E402

MIN_POINTS_FOR_SURFACE = 3
MIN_TRIANGLE_QUALITY = 0.35
EARTH_RAMP = LinearSegmentedColormap.from_list(
    "earth", ["#f4ece1", "#dcbf9d", "#bd8f62", "#93603a", "#5e3820"]
)
INK = "#14303d"
INK_MUTED = "#55686f"
GRID = "#dfe5e6"
DIRECTIONS = (
    "север",
    "северо-восток",
    "восток",
    "юго-восток",
    "юг",
    "юго-запад",
    "запад",
    "северо-запад",
)


@dataclass
class HeightStats:
    count: int
    minimum: float
    maximum: float
    mean: float
    median: float
    spread: float
    deviation: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SlopeStats:
    mean_percent: float
    mean_degrees: float
    max_percent: float
    direction: str

    def to_dict(self) -> dict:
        return asdict(self)


def height_stats(values: list[float]) -> HeightStats | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    return HeightStats(
        count=int(array.size),
        minimum=round(float(array.min()), 3),
        maximum=round(float(array.max()), 3),
        mean=round(float(array.mean()), 3),
        median=round(float(np.median(array)), 3),
        spread=round(float(array.max() - array.min()), 3),
        deviation=round(float(array.std()), 3),
    )


def metres_per_pixel(dpi: float | None, scale: int | None) -> float | None:
    """Переводит пиксели листа в метры на местности."""
    if not dpi or not scale or dpi <= 0 or scale <= 0:
        return None
    return 0.0254 / dpi * scale


def slope_stats(
    x_px: list[float], y_px: list[float], values: list[float], metres: float | None
) -> SlopeStats | None:
    """Считает общий уклон по плоскости и наибольший по соседним точкам.

    Ось Y листа направлена вниз, поэтому восток — вправо, север — вверх листа.
    """
    if not metres or len(values) < MIN_POINTS_FOR_SURFACE:
        return None
    east = np.asarray(x_px, dtype=float) * metres
    north = -np.asarray(y_px, dtype=float) * metres
    heights = np.asarray(values, dtype=float)
    design = np.column_stack([east, north, np.ones_like(east)])
    if np.linalg.matrix_rank(design) < 3:
        return None

    (gradient_east, gradient_north, _), *_ = np.linalg.lstsq(design, heights, rcond=None)
    slope = math.hypot(gradient_east, gradient_north)
    bearing = math.degrees(math.atan2(-gradient_east, -gradient_north)) % 360
    direction = DIRECTIONS[int((bearing + 22.5) // 45) % 8]
    return SlopeStats(
        mean_percent=round(slope * 100, 2),
        mean_degrees=round(math.degrees(math.atan(slope)), 2),
        max_percent=round(_steepest_triangle(east, north, heights) * 100, 2),
        direction=direction,
    )


def _triangle_quality(corners: np.ndarray) -> float:
    """Возвращает 1 для равностороннего треугольника и около 0 для вытянутого."""
    first, second, third = corners
    sides = (
        np.sum((second - first) ** 2) + np.sum((third - second) ** 2) + np.sum((first - third) ** 2)
    )
    if sides == 0:
        return 0.0
    edge_a, edge_b = second - first, third - first
    area = abs(edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0]) / 2
    return float(4 * math.sqrt(3) * area / sides)


def _steepest_triangle(east: np.ndarray, north: np.ndarray, heights: np.ndarray) -> float:
    """Ищет наибольший уклон по триангуляции.

    Узкие треугольники на краю участка соединяют далекие точки почти по прямой и дают
    ложные значения, поэтому в расчет не входят.
    """
    points = np.column_stack([east, north])
    try:
        triangulation = Delaunay(points)
    except QhullError:
        return 0.0
    steepest = 0.0
    for simplex in triangulation.simplices:
        corners = points[simplex]
        if _triangle_quality(corners) < MIN_TRIANGLE_QUALITY:
            continue
        design = np.column_stack([corners, np.ones(3)])
        gradient = np.linalg.solve(design, heights[simplex])
        steepest = max(steepest, math.hypot(gradient[0], gradient[1]))
    return steepest


def _contour_levels(low: float, high: float, step: float | None) -> np.ndarray:
    span = max(high - low, 1e-6)
    if not step or span / step > 60:
        step = float(np.round(span / 10, 2)) or span / 10
    first = math.floor(low / step) * step
    last = math.ceil(high / step) * step
    return np.arange(first, last + step / 2, step)


def height_map_png(
    x_px: list[float],
    y_px: list[float],
    values: list[float],
    metres: float | None,
    step: float | None,
    aspect: float,
) -> bytes | None:
    """Рисует схему высот. Вне контура точек поверхность не экстраполируется."""
    if len(values) < MIN_POINTS_FOR_SURFACE:
        return None
    unit = metres or 1.0
    east = np.asarray(x_px, dtype=float) * unit
    north = -np.asarray(y_px, dtype=float) * unit
    east -= east.min()
    north -= north.min()
    heights = np.asarray(values, dtype=float)
    if np.linalg.matrix_rank(np.column_stack([east, north, np.ones_like(east)])) < 3:
        return None

    margin_x = (east.max() - east.min()) * 0.04
    margin_y = (north.max() - north.min()) * 0.05
    grid_x, grid_y = np.meshgrid(
        np.linspace(east.min(), east.max(), 420), np.linspace(north.min(), north.max(), 320)
    )
    surface = griddata((east, north), heights, (grid_x, grid_y), method="linear")
    levels = _contour_levels(float(heights.min()), float(heights.max()), step)

    width = 9.0
    height = float(np.clip(width * aspect, 4.5, 8.5))
    figure, axes = plt.subplots(figsize=(width, height), dpi=170, layout="constrained")
    figure.patch.set_facecolor("white")
    filled = axes.contourf(grid_x, grid_y, surface, levels=levels, cmap=EARTH_RAMP)
    lines = axes.contour(grid_x, grid_y, surface, levels=levels, colors=INK, linewidths=0.45, alpha=0.65)
    axes.clabel(lines, inline=True, fontsize=6.5, fmt=lambda level: f"{level:g}".replace(".", ","))
    axes.scatter(east, north, s=11, c="white", edgecolors=INK, linewidths=0.7, zorder=3)

    for label, index, offset in (
        ("наименьшая", int(heights.argmin()), (7, -11)),
        ("наибольшая", int(heights.argmax()), (7, 7)),
    ):
        axes.scatter(east[index], north[index], s=46, c=INK, edgecolors="white", linewidths=1.2, zorder=4)
        axes.annotate(
            f"{label} {heights[index]:.2f}".replace(".", ","),
            (east[index], north[index]),
            xytext=offset,
            textcoords="offset points",
            fontsize=8,
            color=INK,
            fontweight="bold",
            bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": "none", "alpha": 0.86},
            zorder=5,
        )

    axes.set_aspect("equal")
    axes.set_xlim(east.min() - margin_x, east.max() + margin_x)
    axes.set_ylim(north.min() - margin_y, north.max() + margin_y)
    axes.set_title("Схема высот, м", loc="left", color=INK, fontsize=11, fontweight="bold")
    for spine in axes.spines.values():
        spine.set_color(GRID)
    if metres:
        axes.tick_params(colors=INK_MUTED, labelsize=7, length=2)
        axes.set_xlabel("метры на местности", color=INK_MUTED, fontsize=8)
    else:
        axes.set_xticks([])
        axes.set_yticks([])
    legend = figure.colorbar(filled, ax=axes, shrink=0.72, pad=0.015)
    legend.ax.tick_params(colors=INK_MUTED, labelsize=7, length=2)
    legend.outline.set_edgecolor(GRID)

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    plt.close(figure)
    return buffer.getvalue()
