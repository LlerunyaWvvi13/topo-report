"""Чтение загруженных файлов: PDF, JPEG, PNG, TIFF."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import cv2
import pymupdf
import numpy as np
from PIL import Image, ImageOps

from . import config


class UnsupportedFile(ValueError):
    """Файл нельзя обработать; текст ошибки показывается пользователю."""


@dataclass
class TextItem:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float
    source: str
    line: tuple = ()


@dataclass
class Page:
    number: int
    image: np.ndarray
    dpi: float | None
    items: list[TextItem] = field(default_factory=list)

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def width(self) -> int:
        return int(self.image.shape[1])


@dataclass
class Document:
    kind: str
    pages: list[Page]
    warnings: list[str] = field(default_factory=list)


def detect_kind(data: bytes) -> str:
    """Определяет тип по содержимому: расширению файла доверять нельзя."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    raise UnsupportedFile("Поддерживаются PDF, JPEG, PNG и TIFF. Этот файл другого формата.")


def load_document(data: bytes) -> Document:
    if not data:
        raise UnsupportedFile("Файл пустой.")
    kind = detect_kind(data)
    return _load_pdf(data) if kind == "pdf" else _load_image(data, kind)


def _render_dpi(width_pt: float, height_pt: float) -> float:
    """Подбирает разрешение так, чтобы лист не превысил лимит по пикселям."""
    area_in2 = max(width_pt * height_pt / (72 * 72), 1e-6)
    return min(config.TARGET_PDF_DPI, math.sqrt(config.MAX_PIXELS / area_in2))


def _load_pdf(data: bytes) -> Document:
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise UnsupportedFile("PDF поврежден: не удалось открыть файл.") from error

    with pdf:
        if pdf.needs_pass:
            raise UnsupportedFile("PDF защищен паролем. Снимите защиту и загрузите файл снова.")
        if pdf.page_count == 0:
            raise UnsupportedFile("В PDF нет страниц.")

        warnings = []
        if pdf.page_count > config.MAX_PDF_PAGES:
            warnings.append(
                f"В файле {pdf.page_count} стр. Обработаны первые {config.MAX_PDF_PAGES}."
            )

        pages = []
        for index in range(min(pdf.page_count, config.MAX_PDF_PAGES)):
            source = pdf[index]
            dpi = _render_dpi(source.rect.width, source.rect.height)
            if dpi < config.MIN_PDF_DPI:
                warnings.append(
                    f"Лист {index + 1} очень большой: разрешение снижено до {dpi:.0f} dpi."
                )
            pixmap = source.get_pixmap(dpi=round(dpi), alpha=False)
            rgb = np.frombuffer(pixmap.samples, dtype=np.uint8)
            rgb = rgb.reshape(pixmap.height, pixmap.width, pixmap.n)
            if pixmap.n == 1:
                image = cv2.cvtColor(rgb, cv2.COLOR_GRAY2BGR)
            else:
                image = cv2.cvtColor(rgb[:, :, :3], cv2.COLOR_RGB2BGR)
            actual_dpi = pixmap.width / max(source.rect.width / 72, 1e-6)
            pages.append(
                Page(
                    number=index + 1,
                    image=image,
                    dpi=actual_dpi,
                    items=_text_layer(source, actual_dpi / 72),
                )
            )
    return Document("pdf", pages, warnings)


def _text_layer(page: pymupdf.Page, zoom: float) -> list[TextItem]:
    """Читает слова PDF. Их координаты приводятся к развернутой странице, как и растр."""
    items = []
    rotation = page.rotation_matrix
    for x0, y0, x1, y1, word, block, line, _ in page.get_text("words"):
        text = word.strip()
        if text:
            box = pymupdf.Rect(x0, y0, x1, y1) * rotation
            items.append(
                TextItem(
                    text=text,
                    x=(box.x0 + box.x1) / 2 * zoom,
                    y=(box.y0 + box.y1) / 2 * zoom,
                    width=box.width * zoom,
                    height=box.height * zoom,
                    confidence=1.0,
                    source="pdf",
                    line=(block, line),
                )
            )
    return items


def _load_image(data: bytes, kind: str) -> Document:
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = config.MAX_PIXELS
    warnings = []
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "TIFF")) as source:
            if source.width * source.height > config.MAX_PIXELS:
                raise UnsupportedFile(_too_large_message())
            if getattr(source, "n_frames", 1) > 1:
                warnings.append("В TIFF несколько страниц. Обработана первая.")
            dpi = _trusted_dpi(source)
            image = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise UnsupportedFile(_too_large_message()) from error
    except (OSError, ValueError, SyntaxError) as error:
        if isinstance(error, UnsupportedFile):
            raise
        raise UnsupportedFile("Изображение повреждено: не удалось его прочитать.") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    bgr, rectified = rectify_sheet(bgr)
    if rectified:
        dpi = None
        warnings.append("Лист на фото выровнен автоматически. Проверьте положение отметок.")
    return Document(kind, [Page(number=1, image=bgr, dpi=dpi)], warnings)


def _too_large_message() -> str:
    limit = config.MAX_PIXELS // 1_000_000
    return f"Изображение больше {limit} Мп. Уменьшите разрешение и загрузите снова."


def _trusted_dpi(image: Image.Image) -> float | None:
    """Возвращает dpi только для сканов: у фото это поле ничего не значит."""
    exif_make_tag = 271
    if image.getexif().get(exif_make_tag):
        return None
    dpi = image.info.get("dpi")
    if not dpi:
        return None
    value = float(dpi[0])
    return value if 100 <= value <= 2400 else None


def _order_corners(points: np.ndarray) -> np.ndarray:
    total = points.sum(axis=1)
    difference = np.diff(points, axis=1).ravel()
    return np.array(
        [
            points[np.argmin(total)],
            points[np.argmin(difference)],
            points[np.argmax(total)],
            points[np.argmax(difference)],
        ],
        dtype=np.float32,
    )


def rectify_sheet(image: np.ndarray) -> tuple[np.ndarray, bool]:
    """Находит лист на фото и убирает перспективу.

    Если четкого четырехугольника нет (скан, лист обрезан кадром), изображение
    возвращается без изменений.
    """
    height, width = image.shape[:2]
    scale = 1200 / max(height, width)
    if scale >= 1:
        scale = 1.0
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return image, False

    largest = max(contours, key=cv2.contourArea)
    area_ratio = cv2.contourArea(largest) / float(small.shape[0] * small.shape[1])
    if not 0.35 <= area_ratio <= 0.97:
        return image, False
    quad = cv2.approxPolyDP(largest, 0.02 * cv2.arcLength(largest, True), True)
    if len(quad) != 4 or not cv2.isContourConvex(quad):
        return image, False

    corners = _order_corners(quad.reshape(4, 2).astype(np.float32) / scale)
    top_left, top_right, bottom_right, bottom_left = corners
    target_width = int(
        max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))
    )
    target_height = int(
        max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))
    )
    destination = np.array(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(corners, destination)
    warped = cv2.warpPerspective(image, matrix, (target_width, target_height), flags=cv2.INTER_CUBIC)
    return warped, True
