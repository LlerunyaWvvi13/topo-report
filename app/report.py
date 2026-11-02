"""Формирование отчета в DOCX и PDF."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import matplotlib
import numpy as np
from docx import Document as WordDocument
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as PdfImage,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .heights import HeightStats, SlopeStats

NOT_FOUND = "не определено"
SHEET_MAX_SIDE = 2000
INK = colors.HexColor("#14303d")
MUTED = colors.HexColor("#55686f")
RULE = colors.HexColor("#c4cfd2")
METADATA_LABELS = {
    "title": "Документ",
    "address": "Адрес или местоположение",
    "cadastral_numbers": "Кадастровые номера",
    "scale": "Масштаб",
    "relief_step": "Сечение рельефа",
    "height_system": "Система высот",
    "coordinate_system": "Система координат",
    "survey_date": "Дата съемки",
    "area": "Площадь",
    "contractor": "Исполнитель",
}
STATUS_LABELS = {
    "ok": "распознана",
    "check": "требует сверки",
    "doubtful": "сомнительная",
    "manual": "внесена вручную",
    "edited": "исправлена вручную",
    "confirmed": "сверена",
}


@dataclass
class ReportData:
    filename: str
    metadata: dict[str, str]
    stats: HeightStats | None
    slope: SlopeStats | None
    marks: list[dict]
    excluded: int
    edited: int
    unchecked: int
    surface_page: int | None
    sheet_jpeg: bytes | None = None
    map_png: bytes | None = None
    warnings: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)


def number(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def annotated_sheet(preview_jpeg: bytes, page_width: int, marks: list[dict], page: int) -> bytes:
    """Готовит изображение листа с номерами отметок из ведомости."""
    image = cv2.imdecode(np.frombuffer(preview_jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return b""
    limit = min(1.0, SHEET_MAX_SIDE / max(image.shape[:2]))
    image = cv2.resize(image, None, fx=limit, fy=limit, interpolation=cv2.INTER_AREA)
    scale = image.shape[1] / page_width
    font_scale = max(0.42, max(image.shape[:2]) / 3000)
    for mark in marks:
        if mark["page"] != page:
            continue
        center = (round(mark["x"] * scale), round(mark["y"] * scale))
        label = str(mark["number"])
        (text_width, text_height), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
        lift = round(mark.get("height", 0) * scale / 2) + text_height + 8
        corner = (center[0] - text_width // 2 - 3, max(center[1] - lift, 0))
        cv2.rectangle(
            image,
            corner,
            (corner[0] + text_width + 6, corner[1] + text_height + 6),
            (122, 82, 18),
            -1,
        )
        cv2.putText(
            image,
            label,
            (corner[0] + 3, corner[1] + text_height + 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    encoded, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buffer.tobytes() if encoded else b""


def _metadata_rows(data: ReportData) -> list[tuple[str, str]]:
    rows = [("Исходный файл", data.filename)]
    rows += [
        (label, data.metadata.get(name, "").strip() or NOT_FOUND)
        for name, label in METADATA_LABELS.items()
    ]
    return rows


def _direction_text(slope: SlopeStats) -> str:
    if slope.direction is None:
        return "не выражено: рельеф ровный"
    return f"на {slope.direction}, если верх листа — север"


def _height_rows(data: ReportData) -> list[tuple[str, str]]:
    if data.stats is None:
        return [("Отметки", "в отчет не включено ни одной отметки")]
    stats = data.stats
    rows = [
        ("Количество отметок", str(stats.count)),
        ("Наименьшая отметка, м", number(stats.minimum)),
        ("Наибольшая отметка, м", number(stats.maximum)),
        ("Перепад высот, м", number(stats.spread)),
        ("Средняя отметка, м", number(stats.mean)),
        ("Медиана, м", number(stats.median)),
        ("Стандартное отклонение, м", number(stats.deviation)),
    ]
    if data.slope:
        rows += [
            ("Средний уклон участка", f"{number(data.slope.mean_percent)} % ({number(data.slope.mean_degrees)}°)"),
            ("Наибольший уклон между соседними точками", f"{number(data.slope.max_percent)} %"),
            ("Понижение рельефа", _direction_text(data.slope)),
        ]
    else:
        rows.append(("Уклон", "не рассчитан: нет достоверного масштаба листа"))
    return rows


def _notes(data: ReportData) -> list[str]:
    notes = [
        "Отчет составлен автоматически по изображению документа. Значения нужно сверить "
        "с исходным топографическим планом; отчет не заменяет материалы инженерных изысканий.",
        f"Исключено из расчета: {data.excluded}. Исправлено или добавлено вручную: {data.edited}. "
        f"Включено без сверки: {data.unchecked}.",
    ]
    if data.surface_page is not None:
        notes.append(f"Уклон и схема высот рассчитаны по листу {data.surface_page}.")
    return notes + data.warnings


def _ledger_rows(data: ReportData) -> list[list[str]]:
    return [
        [
            str(mark["number"]),
            number(mark["value"]),
            str(mark["page"]),
            STATUS_LABELS.get(mark["status"], mark["status"]),
            f"{mark['confidence'] * 100:.0f} %" if mark["status"] in ("ok", "check", "doubtful") else "—",
        ]
        for mark in data.marks
    ]


LEDGER_HEADER = ["№", "Отметка, м", "Лист", "Статус", "Уверенность"]


def _font_path(name: str) -> str:
    return str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / name)


def _register_fonts() -> None:
    if "ReportFont" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("ReportFont", _font_path("DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("ReportFont-Bold", _font_path("DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("ReportFont", normal="ReportFont", bold="ReportFont-Bold")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_pdf(data: ReportData) -> bytes:
    _register_fonts()
    body = ParagraphStyle("body", fontName="ReportFont", fontSize=9, leading=13, textColor=INK, alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=11, textColor=MUTED)
    heading = ParagraphStyle(
        "heading", parent=body, fontName="ReportFont-Bold", fontSize=11.5, leading=15, spaceBefore=12, spaceAfter=5, keepWithNext=1
    )
    title = ParagraphStyle("title", parent=heading, fontSize=17, leading=21, spaceBefore=0, spaceAfter=3)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=11)
    width = A4[0] - 3.6 * cm

    def table(rows: list[tuple[str, str]]) -> Table:
        content = [[Paragraph(_escape(left), cell), Paragraph(_escape(right), cell)] for left, right in rows]
        result = Table(content, colWidths=[width * 0.42, width * 0.58])
        result.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return result

    def image(content: bytes, caption: str) -> KeepTogether:
        reader = ImageReader(io.BytesIO(content))
        image_width, image_height = reader.getSize()
        display_height = min(width * image_height / image_width, 11.4 * cm)
        display_width = display_height * image_width / image_height
        return KeepTogether(
            [PdfImage(io.BytesIO(content), display_width, display_height), Spacer(1, 3), Paragraph(caption, small)]
        )

    story = [
        Paragraph("Отчет по топографической съемке", title),
        Paragraph(f"Сформирован {data.generated_at:%d.%m.%Y %H:%M}", small),
        Paragraph("Реквизиты документа", heading),
        table(_metadata_rows(data)),
        Paragraph("Высотные показатели", heading),
        table(_height_rows(data)),
        Paragraph("Примечания", heading),
    ]
    story += [Paragraph(_escape(note), body) for note in _notes(data)]
    if data.map_png:
        story += [Spacer(1, 10), image(data.map_png, "Интерполяция по отметкам; вне контура точек рельеф не показан.")]
    if data.sheet_jpeg:
        story += [Spacer(1, 10), image(data.sheet_jpeg, "Исходный лист. Номера соответствуют ведомости отметок.")]
    if data.marks:
        rows = [LEDGER_HEADER] + _ledger_rows(data)
        ledger = Table(rows, colWidths=[width * share for share in (0.1, 0.2, 0.1, 0.38, 0.22)], repeatRows=1)
        ledger.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, 0), "ReportFont-Bold", 8.5),
                    ("FONT", (0, 1), (-1, -1), "ReportFont", 8.5),
                    ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("RIGHTPADDING", (1, 0), (1, -1), 16),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                ]
            )
        )
        story += [Paragraph("Ведомость высотных отметок", heading), ledger]

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont("ReportFont", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 1.8 * cm, 1 * cm, f"Страница {document.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    template = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.8 * cm,
        rightMargin=1.8 * cm,
        topMargin=1.7 * cm,
        bottomMargin=1.7 * cm,
        title="Отчет по топографической съемке",
        author="Топоотчет",
    )
    template.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _set_cell(cell, text: str, bold: bool = False, size: float = 9.5) -> None:
    cell.text = ""
    run = cell.paragraphs[0].add_run(text)
    run.bold = bold
    run.font.size = Pt(size)


def _word_table(document, rows: list[tuple[str, str]]) -> None:
    table = document.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row, (left, right) in zip(table.rows, rows):
        row.cells[0].width = Cm(7.2)
        row.cells[1].width = Cm(10)
        _set_cell(row.cells[0], left)
        _set_cell(row.cells[1], right)


def build_docx(data: ReportData) -> bytes:
    document = WordDocument()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(1.9)
    section.top_margin = section.bottom_margin = Cm(1.8)

    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    normal.font.size = Pt(10.5)
    for name, size in (("Title", 20), ("Heading 1", 13)):
        style = document.styles[name]
        style.font.name = "Arial"
        style.element.rPr.rFonts.set(qn("w:asciiTheme"), "")
        style.element.rPr.rFonts.set(qn("w:ascii"), "Arial")
        style.element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0x14, 0x30, 0x3D)

    document.core_properties.title = "Отчет по топографической съемке"
    document.core_properties.author = "Топоотчет"
    document.add_paragraph("Отчет по топографической съемке", style="Title")
    document.add_paragraph(f"Сформирован {data.generated_at:%d.%m.%Y %H:%M}")

    document.add_heading("Реквизиты документа", level=1)
    _word_table(document, _metadata_rows(data))
    document.add_heading("Высотные показатели", level=1)
    _word_table(document, _height_rows(data))
    document.add_heading("Примечания", level=1)
    for note in _notes(data):
        document.add_paragraph(note)

    if data.map_png:
        document.add_heading("Схема высот", level=1)
        document.add_picture(io.BytesIO(data.map_png), width=Cm(17.2))
        document.add_paragraph("Интерполяция по отметкам; вне контура точек рельеф не показан.")
    if data.sheet_jpeg:
        document.add_heading("Исходный лист", level=1)
        document.add_picture(io.BytesIO(data.sheet_jpeg), width=Cm(17.2))
        document.add_paragraph("Номера соответствуют ведомости отметок.")

    if data.marks:
        document.add_heading("Ведомость высотных отметок", level=1)
        table = document.add_table(rows=1, cols=len(LEDGER_HEADER))
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, LEDGER_HEADER):
            _set_cell(cell, title, bold=True)
        header_properties = table.rows[0]._tr.get_or_add_trPr()
        repeat = OxmlElement("w:tblHeader")
        repeat.set(qn("w:val"), "true")
        header_properties.append(repeat)
        for values in _ledger_rows(data):
            for cell, value in zip(table.add_row().cells, values):
                _set_cell(cell, value)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
