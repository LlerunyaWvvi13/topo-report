"""Распознавание текста на растре с помощью Tesseract."""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pytesseract

from . import config
from .loader import Page, TextItem

os.environ.setdefault("OMP_THREAD_LIMIT", "1")

WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
NOT_INSTALLED = "Не найден Tesseract OCR. Установите его с русским языком, инструкция в README."

DIGIT_CONFIG = "--oem 1 --psm 11 -c tessedit_char_whitelist=0123456789.,"
BLOB_CONFIG = "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789.,"
TEXT_CONFIG = "--oem 1 --psm 11"
LONG_LINE_PX = 70
BLOB_TARGET_HEIGHT = 44
NUMBER_LIKE = re.compile(r"\d{1,4}[.,]\d{1,3}")
ORIENTATION_MAX_SIDE = 5000
ORIENTATION_MIN_NUMBERS = 4
ORIENTATION_ENOUGH_NUMBERS = 12
ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def _locate_tesseract() -> None:
    custom = os.environ.get("TESSERACT_CMD")
    if custom:
        pytesseract.pytesseract.tesseract_cmd = custom
    elif shutil.which("tesseract") is None and WINDOWS_TESSERACT.is_file():
        pytesseract.pytesseract.tesseract_cmd = str(WINDOWS_TESSERACT)


_locate_tesseract()


class OcrUnavailable(RuntimeError):
    """На компьютере не установлен Tesseract или нужный языковой пакет."""


def ink_layer(image: np.ndarray) -> np.ndarray:
    """Оставляет темную подпись: убирает неравномерный свет, цветные линии и сетку."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), 15)
    flattened = cv2.divide(gray, background, scale=255)

    colored = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 70)
    flattened[colored] = 255

    _, ink = cv2.threshold(255 - flattened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, LONG_LINE_PX), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((LONG_LINE_PX, 1), np.uint8))
    lines = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    flattened[lines > 0] = 255
    return flattened


def _tiles(height: int, width: int) -> list[tuple[int, int]]:
    step = config.OCR_TILE_SIZE - config.OCR_TILE_OVERLAP
    rows = range(0, max(height - config.OCR_TILE_OVERLAP, 1), step)
    columns = range(0, max(width - config.OCR_TILE_OVERLAP, 1), step)
    return [(top, left) for top in rows for left in columns]


def _read_tile(
    gray: np.ndarray, top: int, left: int, scale: float, lang: str, ocr_config: str, tag: str
) -> list[TextItem]:
    tile = gray[top : top + config.OCR_TILE_SIZE, left : left + config.OCR_TILE_SIZE]
    if tile.size == 0 or tile.min() > 200:
        return []
    if scale != 1:
        tile = cv2.resize(tile, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(
        tile,
        lang=lang,
        config=ocr_config,
        output_type=pytesseract.Output.DICT,
        timeout=config.OCR_TIMEOUT_SECONDS,
    )

    items = []
    for index, raw in enumerate(data["text"]):
        text = raw.strip()
        confidence = float(data["conf"][index])
        if not text or confidence < 0:
            continue
        width = data["width"][index] / scale
        height = data["height"][index] / scale
        items.append(
            TextItem(
                text=text,
                x=left + data["left"][index] / scale + width / 2,
                y=top + data["top"][index] / scale + height / 2,
                width=width,
                height=height,
                confidence=confidence / 100,
                source="ocr",
                line=(
                    tag,
                    top,
                    left,
                    data["block_num"][index],
                    data["par_num"][index],
                    data["line_num"][index],
                ),
            )
        )
    return items


def _digit_scale(page: Page) -> float:
    """Увеличивает мелкие подписи: Tesseract лучше читает символы от 25 пикселей."""
    return 2.0 if page.dpi is None or page.dpi < 280 else 1.5


def _count_numbers(gray: np.ndarray) -> int:
    """Считает числа на изображении. Без белого списка перевернутый текст дает мусор."""
    text = pytesseract.image_to_string(
        gray, lang="eng", config=TEXT_CONFIG, timeout=config.OCR_TIMEOUT_SECONDS
    )
    return sum(1 for word in text.split() if NUMBER_LIKE.fullmatch(word.strip(".,;:")))


def correct_orientation(page: Page) -> bool:
    """Поворачивает лист, снятый боком или вверх ногами.

    На топоплане мало связного текста, поэтому стандартное определение ориентации
    ненадежно. Отметки подписаны горизонтально, и верным считается положение, в котором
    читается больше всего чисел. Обычный лист отсеивается после первой проверки.
    """
    height, width = page.image.shape[:2]
    scale = min(1.0, ORIENTATION_MAX_SIDE / max(height, width))
    small = cv2.resize(ink_layer(page.image), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    try:
        upright = _count_numbers(small)
        if upright >= ORIENTATION_ENOUGH_NUMBERS:
            return False
        counts = {
            rotation: _count_numbers(cv2.rotate(small, code)) for rotation, code in ROTATIONS.items()
        }
    except pytesseract.TesseractNotFoundError as error:
        raise OcrUnavailable(NOT_INSTALLED) from error
    except (pytesseract.TesseractError, RuntimeError):
        return False

    best = max(counts, key=counts.get)
    if counts[best] < ORIENTATION_MIN_NUMBERS or counts[best] < upright * 2:
        return False
    page.image = cv2.rotate(page.image, ROTATIONS[best])
    return True


def _find_number_blobs(gray: np.ndarray, glyph_height: float) -> list[tuple[int, int, int, int]]:
    """Ищет на листе компактные строки символов высотой примерно как у отметок."""
    _, ink = cv2.threshold(255 - gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bridge = max(3, round(glyph_height * 0.7))
    joined = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((1, bridge), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    blobs = []
    for label in range(1, count):
        x, y, width, height, _ = stats[label]
        aspect = width / max(height, 1)
        if 0.6 * glyph_height <= height <= 1.7 * glyph_height and 1.8 <= aspect <= 9:
            blobs.append((int(x), int(y), int(width), int(height)))
    return blobs


def _erase_point_marker(crop: np.ndarray, glyph_height: int) -> None:
    """Стирает кружок точки перед подписью: иначе он читается как лишняя цифра."""
    _, ink = cv2.threshold(255 - crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if count < 3:
        return
    leftmost = 1 + int(np.argmin(stats[1:, cv2.CC_STAT_LEFT]))
    if stats[leftmost, cv2.CC_STAT_HEIGHT] < 0.55 * glyph_height:
        crop[labels == leftmost] = 255


def _read_blob(gray: np.ndarray, blob: tuple[int, int, int, int]) -> list[TextItem]:
    x, y, width, height = blob
    pad = max(4, height // 2)
    crop = gray[max(y - pad, 0) : y + height + pad, max(x - pad, 0) : x + width + pad].copy()
    _erase_point_marker(crop, height)
    scale = BLOB_TARGET_HEIGHT / max(height, 1)
    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(
        crop,
        lang="eng",
        config=BLOB_CONFIG,
        output_type=pytesseract.Output.DICT,
        timeout=config.OCR_TIMEOUT_SECONDS,
    )
    words = [
        (text.strip(), float(conf))
        for text, conf in zip(data["text"], data["conf"])
        if text.strip() and float(conf) >= 0
    ]
    if not words:
        return []
    text = "".join(word for word, _ in words)
    confidence = min(conf for _, conf in words) / 100
    return [
        TextItem(
            text=text,
            x=x + width / 2,
            y=y + height / 2,
            width=float(width),
            height=float(height),
            confidence=confidence,
            source="ocr",
            line=("blob", x, y),
        )
    ]


def _number_shape(text: str) -> tuple[int, int]:
    """Возвращает количество цифр до и после разделителя."""
    whole, fraction = re.split(r"[.,]", text)
    return len(whole), len(fraction)


def refine_numbers(gray: np.ndarray, items: list[TextItem]) -> list[TextItem]:
    """Перечитывает крупным планом числа, пропущенные при чтении всего листа."""
    readings = [item for item in items if NUMBER_LIKE.fullmatch(item.text)]
    if len(readings) < 3:
        return []
    glyph_height = float(np.median([item.height for item in readings]))
    usual_shape = Counter(_number_shape(item.text) for item in readings).most_common(1)[0][0]
    trusted = [
        item
        for item in readings
        if item.confidence >= config.GOOD_CONFIDENCE and _number_shape(item.text) == usual_shape
    ]

    def already_read(blob: tuple[int, int, int, int]) -> bool:
        x, y, width, height = blob
        center_x, center_y = x + width / 2, y + height / 2
        return any(
            abs(item.x - center_x) < max(item.width, width) / 2
            and abs(item.y - center_y) < max(item.height, height)
            for item in trusted
        )

    blobs = [blob for blob in _find_number_blobs(gray, glyph_height) if not already_read(blob)]
    blobs = blobs[: config.MAX_REFINED_BLOBS]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        return [item for chunk in pool.map(lambda blob: _read_blob(gray, blob), blobs) for item in chunk]


def read_page(page: Page) -> list[TextItem]:
    """Распознает страницу: весь текст, числа и уточнение пропущенных чисел."""
    gray = ink_layer(page.image)
    tiles = _tiles(*gray.shape[:2])
    scale = _digit_scale(page)

    def text_pass(tile: tuple[int, int]) -> list[TextItem]:
        return _read_tile(gray, *tile, 1.5, config.OCR_LANGUAGES, TEXT_CONFIG, "text")

    def digit_pass(tile: tuple[int, int]) -> list[TextItem]:
        return _read_tile(gray, *tile, scale, "eng", DIGIT_CONFIG, "digits")

    try:
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
            text_items = [item for chunk in pool.map(text_pass, tiles) for item in chunk]
            digit_items = [item for chunk in pool.map(digit_pass, tiles) for item in chunk]
        refined = refine_numbers(gray, text_items + digit_items)
    except pytesseract.TesseractNotFoundError as error:
        raise OcrUnavailable(NOT_INSTALLED) from error
    except pytesseract.TesseractError as error:
        if re.search(r"Failed loading language|Error opening data file", str(error)):
            raise OcrUnavailable(
                "В Tesseract нет русского языка. Установите пакет rus, инструкция в README."
            ) from error
        raise
    return text_items + digit_items + refined
