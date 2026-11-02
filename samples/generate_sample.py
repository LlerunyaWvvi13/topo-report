"""Создает тестовый топоплан с вымышленными данными и файл эталонных отметок.

Запуск: python samples/generate_sample.py
Результат: sample_plan.pdf, sample_plan_photo.jpg, sample_truth.json
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import pymupdf
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).parent
MM = 72 / 25.4
PAGE_W, PAGE_H = 420 * MM, 297 * MM
FIELD = pymupdf.Rect(20 * MM, 15 * MM, 400 * MM, 282 * MM)
SCALE = 500
BROWN = (0.61, 0.35, 0.18)
BLACK = (0, 0, 0)
BLUE = (0.1, 0.4, 0.7)
FONT_PATH = str(Path(matplotlib.get_data_path()) / "fonts/ttf/DejaVuSans.ttf")


def terrain(x_m: np.ndarray, y_m: np.ndarray) -> np.ndarray:
    return (
        147.4
        + 2.6 * np.sin(x_m / 34)
        + 1.9 * np.cos(y_m / 27)
        + 0.021 * x_m
        - 0.012 * y_m
    )


def to_page(x_m: float, y_m: float) -> pymupdf.Point:
    return pymupdf.Point(
        FIELD.x0 + x_m * 1000 / SCALE * MM,
        FIELD.y1 - y_m * 1000 / SCALE * MM,
    )


def text(page: pymupdf.Page, point: pymupdf.Point, value: str, size: float = 6.2, color=BLACK, rotate=0):
    page.insert_text(
        point, value, fontsize=size, fontname="plan", fontfile=FONT_PATH, color=color, rotate=rotate
    )


def draw_contours(page: pymupdf.Page, width_m: float, height_m: float) -> None:
    xs = np.linspace(0, width_m, 260)
    ys = np.linspace(0, height_m, 190)
    grid_x, grid_y = np.meshgrid(xs, ys)
    z = terrain(grid_x, grid_y)
    levels = np.arange(np.floor(z.min()), np.ceil(z.max()) + 0.5, 0.5)
    figure = plt.figure()
    contours = plt.contour(grid_x, grid_y, z, levels=levels)
    for level, segments in zip(contours.levels, contours.allsegs):
        major = float(level).is_integer()
        for segment in segments:
            if len(segment) < 3:
                continue
            shape = page.new_shape()
            shape.draw_polyline([to_page(float(px), float(py)) for px, py in segment])
            shape.finish(color=BROWN, width=0.7 if major else 0.3, closePath=False)
            shape.commit()
            if major and len(segment) > 80:
                px, py = segment[len(segment) // 2]
                text(page, to_page(float(px), float(py)), f"{level:.0f}", 5.5, BROWN, rotate=90)
    plt.close(figure)


def draw_grid(page: pymupdf.Page, width_m: float, height_m: float) -> None:
    for x_m in np.arange(0, width_m + 1, 50):
        for y_m in np.arange(0, height_m + 1, 50):
            point = to_page(float(x_m), float(y_m))
            shape = page.new_shape()
            shape.draw_line(point - (6, 0), point + (6, 0))
            shape.draw_line(point - (0, 6), point + (0, 6))
            shape.finish(color=BLACK, width=0.3)
            shape.commit()
    for x_m in np.arange(0, width_m + 1, 50):
        text(page, to_page(float(x_m), 0) + (2, 10), f"{1320000 + int(x_m)}", 5.5)
    for y_m in np.arange(0, height_m + 1, 50):
        text(page, to_page(0, float(y_m)) + (-46, 2), f"{456000 + int(y_m)}", 5.5)


def draw_plot(page: pymupdf.Page) -> None:
    corners = [(92, 42), (168, 48), (172, 104), (86, 96)]
    shape = page.new_shape()
    shape.draw_polyline([to_page(x, y) for x, y in corners + corners[:1]])
    shape.finish(color=(0.75, 0, 0), width=1.1, dashes="[6 3] 0")
    shape.commit()
    for start, end in zip(corners, corners[1:] + corners[:1]):
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        length = float(np.hypot(end[0] - start[0], end[1] - start[1]))
        text(page, to_page(mx, my) + (3, -3), f"{length:.2f}", 5.5, (0.75, 0, 0))

    house = [(112, 62), (132, 63), (131.4, 77), (111.4, 76)]
    shape = page.new_shape()
    shape.draw_polyline([to_page(x, y) for x, y in house + house[:1]])
    shape.finish(color=BLACK, width=0.8, fill=(0.93, 0.93, 0.93))
    shape.commit()
    text(page, to_page(116, 69), "КЖ-2", 6)

    water = [(14, 20 + i * 12 + 4 * np.sin(i)) for i in range(10)]
    shape = page.new_shape()
    shape.draw_polyline([to_page(x, y) for x, y in water])
    shape.finish(color=BLUE, width=1.0, closePath=False)
    shape.commit()


def draw_marks(page: pymupdf.Page, width_m: float, height_m: float) -> list[dict]:
    rng = np.random.default_rng(12)
    marks = []
    for x_m in np.arange(14, width_m - 12, 21):
        for y_m in np.arange(12, height_m - 10, 19):
            x = float(x_m + rng.uniform(-4, 4))
            y = float(y_m + rng.uniform(-4, 4))
            if x > 92 and y < 30:  # зона штампа
                continue
            value = round(float(terrain(x, y)), 2)
            point = to_page(x, y)
            shape = page.new_shape()
            shape.draw_circle(point, 0.9)
            shape.finish(color=BLACK, fill=BLACK)
            shape.commit()
            text(page, point + (3, 2), f"{value:.2f}", 6.4)
            marks.append(
                {
                    "value": value,
                    "x_pt": round(point.x + 14, 2),
                    "y_pt": round(point.y, 2),
                }
            )
    return marks


def draw_stamp(page: pymupdf.Page) -> None:
    box = pymupdf.Rect(PAGE_W - 200 * MM, PAGE_H - 62 * MM, PAGE_W - 20 * MM, PAGE_H - 15 * MM)
    shape = page.new_shape()
    shape.draw_rect(box)
    shape.finish(color=BLACK, fill=(1, 1, 1), width=1.2)
    shape.commit()
    lines = [
        "ТЕСТОВЫЙ ОБРАЗЕЦ. ДАННЫЕ ВЫМЫШЛЕНЫ",
        "Топографический план участка",
        "Адрес: Тестовая область, пос. Примерный, участок 1",
        "Кадастровый номер: 00:00:0000000:101",
        "Площадь: 4210 кв.м",
        "Масштаб 1:500",
        "Система координат: МСК-00",
        "Система высот: Балтийская 1977 г.",
        "Сплошные горизонтали проведены через 0,5 м",
        "Съемка выполнена 12.05.2024",
        "Исполнитель: ООО Пример Геодезия",
    ]
    for index, line in enumerate(lines):
        text(page, pymupdf.Point(box.x0 + 8, box.y0 + 11 + index * 11.2), line, 8)


def make_photo(pdf_path: Path, output: Path) -> None:
    """Имитирует снимок листа на столе: перспектива, тень, шум, сжатие."""
    with pymupdf.open(pdf_path) as document:
        pixmap = document[0].get_pixmap(dpi=220, alpha=False)
    sheet = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    sheet = cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR)
    height, width = sheet.shape[:2]

    frame_w, frame_h = 4200, 3150
    source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    target = np.float32([[210, 240], [3990, 150], [4060, 2950], [130, 3020]])
    matrix = cv2.getPerspectiveTransform(source, target)
    photo = cv2.warpPerspective(
        sheet, matrix, (frame_w, frame_h), flags=cv2.INTER_CUBIC, borderValue=(58, 66, 74)
    )

    rng = np.random.default_rng(3)
    x, y = np.meshgrid(np.linspace(0, 1, frame_w), np.linspace(0, 1, frame_h))
    light = 0.86 + 0.16 * x - 0.08 * y
    photo = np.clip(photo * light[..., None], 0, 255)
    photo = cv2.GaussianBlur(photo.astype(np.uint8), (3, 3), 0.7).astype(np.float32)
    photo += rng.normal(0, 4, photo.shape)
    photo = np.clip(photo, 0, 255).astype(np.uint8)
    cv2.imwrite(str(output), photo, [cv2.IMWRITE_JPEG_QUALITY, 86])


def main() -> None:
    document = pymupdf.open()
    page = document.new_page(width=PAGE_W, height=PAGE_H)
    width_m = FIELD.width / MM * SCALE / 1000
    height_m = FIELD.height / MM * SCALE / 1000

    draw_contours(page, width_m, height_m)
    draw_grid(page, width_m, height_m)
    draw_plot(page)
    marks = draw_marks(page, width_m, height_m)
    draw_stamp(page)

    pdf_path = HERE / "sample_plan.pdf"
    document.subset_fonts()
    document.save(pdf_path, garbage=4, deflate=True)
    document.close()
    make_photo(pdf_path, HERE / "sample_plan_photo.jpg")
    truth = {
        "note": "Эталон для проверки распознавания. Все данные вымышлены.",
        "page_size_pt": [PAGE_W, PAGE_H],
        "marks": marks,
    }
    (HERE / "sample_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Создано отметок: {len(marks)}")


if __name__ == "__main__":
    main()
