"""Веб-приложение: загрузка листа, проверка отметок, выгрузка отчета."""

from __future__ import annotations

import base64
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import cv2
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config
from .extract import (
    METADATA_FIELDS,
    extract_marks,
    extract_metadata,
    relief_step_metres,
    scale_denominator,
)
from .heights import height_map_png, height_stats, metres_per_pixel, slope_stats
from .loader import Page, TextItem, UnsupportedFile, load_document
from .ocr import OcrUnavailable, correct_orientation, read_page
from .report import ReportData, annotated_sheet, build_docx, build_pdf

STATIC_DIR = Path(__file__).parent / "static"
SAMPLE_FILE = Path(__file__).parent.parent / "samples" / "sample_plan.pdf"
PREVIEW_MAX_SIDE = 2800
UPLOAD_CHUNK = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
PAGE_MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}



def _too_large_message() -> str:
    limit = config.MAX_UPLOAD_BYTES // (1024 * 1024)
    return f"Файл больше {limit} МБ. Уменьшите его и загрузите снова."


class SecurityHeaders:
    """Добавляет защитные заголовки ко всем ответам."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.inner(scope, receive, send_with_headers if scope["type"] == "http" else send)


class BodySizeLimit:
    """Останавливает слишком большой запрос до разбора multipart и записи на диск."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        limit = config.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            response = JSONResponse({"detail": _too_large_message()}, status_code=413)
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, _too_large_message())
            return message

        await self.inner(scope, limited_receive, send)


app = FastAPI(title="Топоотчет", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.add_middleware(BodySizeLimit)
app.add_middleware(SecurityHeaders)


@dataclass(frozen=True)
class StoredPage:
    """Данные листа для отчета. Полный растр в памяти не хранится."""

    number: int
    width: int
    height: int
    dpi: float | None
    rectified: bool
    preview: bytes


@dataclass
class Analysis:
    id: str
    filename: str
    pages: dict[int, StoredPage]
    warnings: list[str]


_analyses: OrderedDict[str, Analysis] = OrderedDict()
_analyses_lock = threading.Lock()
_ocr_slots = threading.BoundedSemaphore(config.MAX_PARALLEL_ANALYSES)


class MarkInput(BaseModel):
    id: int
    value: Annotated[float, Field(ge=config.MIN_ELEVATION, le=config.MAX_ELEVATION, allow_inf_nan=False)]
    page: Annotated[int, Field(ge=1)]
    x: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    y: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    height: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    status: Literal["ok", "check", "doubtful", "manual", "edited", "confirmed"]
    included: bool


class ReviewInput(BaseModel):
    marks: Annotated[list[MarkInput], Field(max_length=config.MAX_MARKS)]
    metadata: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=500)]] = {}
    dpi: Annotated[float | None, Field(gt=0, le=4800)] = None


@app.exception_handler(UnsupportedFile)
async def unsupported_file(_: Request, error: UnsupportedFile) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=422)


@app.exception_handler(OcrUnavailable)
async def ocr_unavailable(_: Request, error: OcrUnavailable) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=503)


def _store(analysis: Analysis) -> None:
    with _analyses_lock:
        _analyses[analysis.id] = analysis
        while len(_analyses) > config.ANALYSES_KEPT_IN_MEMORY:
            _analyses.popitem(last=False)


def _get_analysis(analysis_id: str) -> Analysis:
    with _analyses_lock:
        analysis = _analyses.get(analysis_id)
        if analysis is not None:
            _analyses.move_to_end(analysis_id)
    if analysis is None:
        raise HTTPException(404, "Результат разбора устарел. Загрузите файл снова.")
    return analysis


def _safe_filename(name: str | None) -> str:
    cleaned = Path((name or "").replace("\\", "/")).name.strip()
    return cleaned[:160] or "без имени"


def _preview(page: Page) -> bytes:
    scale = min(1.0, PREVIEW_MAX_SIDE / max(page.width, page.height))
    image = cv2.resize(page.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    encoded, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 84])
    if not encoded:
        raise UnsupportedFile("Не удалось подготовить изображение листа.")
    return buffer.tobytes()


def _text_items(page: Page) -> tuple[list[TextItem], bool]:
    """Берет текстовый слой PDF, а если отметок в нем нет, распознает изображение."""
    digital = [item for item in page.items if any(char.isdigit() for char in item.text)]
    if len(digital) >= 3 and extract_marks([(page, page.items)]):
        return page.items, False
    return page.items + read_page(page), True


def _run_analysis(data: bytes, filename: str) -> dict:
    document = load_document(data)
    with _ocr_slots:
        read_pages = []
        all_items: list[TextItem] = []
        used_ocr = False
        for page in document.pages:
            if not page.items and correct_orientation(page):
                document.warnings.append(f"Лист {page.number} повернут автоматически.")
            items, page_used_ocr = _text_items(page)
            used_ocr = used_ocr or page_used_ocr
            read_pages.append((page, items))
            all_items.extend(items)

    marks = extract_marks(read_pages)
    if not marks:
        document.warnings.append(
            "Высотные отметки не найдены. Добавьте их вручную по листу или загрузите скан лучшего качества."
        )

    analysis = Analysis(
        id=uuid.uuid4().hex,
        filename=filename,
        pages={
            page.number: StoredPage(
                page.number, page.width, page.height, page.dpi, page.rectified, _preview(page)
            )
            for page in document.pages
        },
        warnings=document.warnings,
    )
    _store(analysis)
    return {
        "id": analysis.id,
        "filename": filename,
        "kind": document.kind,
        "method": "распознавание изображения" if used_ocr else "текстовый слой PDF",
        "pages": [
            {
                "number": page.number,
                "width": page.width,
                "height": page.height,
                "dpi": round(page.dpi, 1) if page.dpi else None,
                "rectified": page.rectified,
                "preview": f"/api/analyses/{analysis.id}/pages/{page.number}.jpg",
            }
            for page in analysis.pages.values()
        ],
        "marks": [mark.to_dict() for mark in marks],
        "metadata": extract_metadata(all_items),
        "warnings": document.warnings,
    }


async def _read_limited(upload: UploadFile) -> bytes:
    chunks, total = [], 0
    while chunk := await upload.read(UPLOAD_CHUNK):
        total += len(chunk)
        if total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, _too_large_message())
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/analyses")
async def create_analysis(file: Annotated[UploadFile, File()]) -> dict:
    data = await _read_limited(file)
    return await run_in_threadpool(_run_analysis, data, _safe_filename(file.filename))


@app.post("/api/analyses/sample")
async def create_sample_analysis() -> dict:
    if not SAMPLE_FILE.is_file():
        raise HTTPException(404, "Тестовый лист не найден.")
    return await run_in_threadpool(_run_analysis, SAMPLE_FILE.read_bytes(), "Тестовый образец.pdf")


@app.get("/api/analyses/{analysis_id}/pages/{number}.jpg")
def page_preview(analysis_id: str, number: int) -> Response:
    analysis = _get_analysis(analysis_id)
    if number not in analysis.pages:
        raise HTTPException(404, "Такого листа нет.")
    return Response(
        analysis.pages[number].preview,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@dataclass
class Computed:
    stats: object
    slope: object
    map_png: bytes | None
    surface_page: int | None
    included: list[MarkInput]


def _surface_page(included: list[MarkInput]) -> int | None:
    counts: dict[int, int] = {}
    for mark in included:
        counts[mark.page] = counts.get(mark.page, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _compute(analysis: Analysis, review: ReviewInput) -> Computed:
    included = [mark for mark in review.marks if mark.included]
    stats = height_stats([mark.value for mark in included])
    surface_page = _surface_page(included)
    slope, map_png = None, None
    if surface_page is not None:
        page = analysis.pages.get(surface_page)
        points = [mark for mark in included if mark.page == surface_page]
        if page is not None:
            dpi = None if page.rectified else review.dpi or page.dpi
            metres = metres_per_pixel(dpi, scale_denominator(review.metadata.get("scale", "")))
            xs = [mark.x for mark in points]
            ys = [mark.y for mark in points]
            values = [mark.value for mark in points]
            slope = slope_stats(xs, ys, values, metres)
            step = relief_step_metres(review.metadata.get("relief_step", ""))
            map_png = height_map_png(xs, ys, values, metres, step, page.height / page.width)
    return Computed(stats, slope, map_png, surface_page, included)


@app.post("/api/analyses/{analysis_id}/summary")
def summary(analysis_id: str, review: ReviewInput) -> dict:
    computed = _compute(_get_analysis(analysis_id), review)
    return {
        "stats": computed.stats.to_dict() if computed.stats else None,
        "slope": computed.slope.to_dict() if computed.slope else None,
        "map": base64.b64encode(computed.map_png).decode() if computed.map_png else None,
    }


def _clean_metadata(metadata: dict[str, str]) -> dict[str, str]:
    return {name: metadata.get(name, "").strip() for name in METADATA_FIELDS}


@app.post("/api/analyses/{analysis_id}/report")
def report(
    analysis_id: str,
    review: ReviewInput,
    format: Annotated[Literal["docx", "pdf"], Query()] = "docx",
) -> Response:
    analysis = _get_analysis(analysis_id)
    computed = _compute(analysis, review)
    numbered = [
        {**mark.model_dump(), "number": index}
        for index, mark in enumerate(computed.included, start=1)
    ]

    sheet = None
    page = analysis.pages.get(computed.surface_page)
    if page is not None:
        sheet = annotated_sheet(page.preview, page.width, numbered, page.number)

    data = ReportData(
        filename=analysis.filename,
        metadata=_clean_metadata(review.metadata),
        stats=computed.stats,
        slope=computed.slope,
        marks=numbered,
        excluded=sum(1 for mark in review.marks if not mark.included),
        edited=sum(1 for mark in computed.included if mark.status in ("manual", "edited")),
        unchecked=sum(1 for mark in computed.included if mark.status == "check"),
        surface_page=computed.surface_page if len(analysis.pages) > 1 else None,
        sheet_jpeg=sheet,
        map_png=computed.map_png,
        warnings=analysis.warnings,
    )
    content = build_pdf(data) if format == "pdf" else build_docx(data)
    stem = Path(analysis.filename).stem or "план"
    download = quote(f"Отчет {stem}.{format}")
    return Response(
        content,
        media_type=PAGE_MEDIA[format],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{download}"},
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
