import io
import json
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image

from app import config
from app.main import _analyses, app

client = TestClient(app)
TRUTH = json.loads((Path(__file__).parent.parent / "samples" / "sample_truth.json").read_text("utf-8"))
TRUE_VALUES = sorted(mark["value"] for mark in TRUTH["marks"])


def upload(data: bytes, name: str = "план.pdf"):
    return client.post("/api/analyses", files={"file": (name, data)})


def review_body(result: dict) -> dict:
    return {"marks": result["marks"], "metadata": result["metadata"]}


def included(result: dict) -> list[float]:
    return sorted(mark["value"] for mark in result["marks"] if mark["included"])


def test_index_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "Топоотчет" in response.text


def test_security_headers_are_set():
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_foreign_host_is_rejected():
    response = client.get("/", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_digital_pdf_reads_every_mark_exactly(sample_pdf):
    response = upload(sample_pdf)
    assert response.status_code == 200
    result = response.json()
    assert result["method"] == "текстовый слой PDF"
    assert included(result) == TRUE_VALUES
    metadata = result["metadata"]
    assert metadata["scale"] == "1:500"
    assert metadata["cadastral_numbers"] == "00:00:0000000:101"


def test_photo_finds_most_marks_without_inventing_values(sample_photo):
    result = upload(sample_photo, "фото.jpg").json()
    found = included(result)
    remaining = list(TRUE_VALUES)
    correct = 0
    for value in found:
        if value in remaining:
            remaining.remove(value)
            correct += 1
    assert correct / len(TRUE_VALUES) >= 0.85
    assert correct / len(found) >= 0.9
    assert result["metadata"]["scale"] == "1:500"


def test_summary_matches_known_surface(sample_pdf):
    result = upload(sample_pdf).json()
    summary = client.post(f"/api/analyses/{result['id']}/summary", json=review_body(result)).json()
    assert summary["stats"]["count"] == len(TRUE_VALUES)
    assert summary["stats"]["minimum"] == min(TRUE_VALUES)
    assert summary["stats"]["maximum"] == max(TRUE_VALUES)
    # Наибольший уклон тестовой поверхности около 13 %, между точками он не больше.
    assert 0 < summary["slope"]["mean_percent"] < summary["slope"]["max_percent"] <= 14
    assert summary["map"]


def test_excluding_a_mark_changes_the_result(sample_pdf):
    result = upload(sample_pdf).json()
    highest = max(TRUE_VALUES)
    for mark in result["marks"]:
        if mark["value"] == highest:
            mark["included"] = False
    summary = client.post(f"/api/analyses/{result['id']}/summary", json=review_body(result)).json()
    assert summary["stats"]["maximum"] < highest


def test_docx_report_contains_values(sample_pdf):
    result = upload(sample_pdf).json()
    response = client.post(f"/api/analyses/{result['id']}/report?format=docx", json=review_body(result))
    assert response.status_code == 200
    assert "filename*=UTF-8" in response.headers["content-disposition"]
    document = Document(io.BytesIO(response.content))
    text = "\n".join(cell.text for table in document.tables for row in table.rows for cell in row.cells)
    assert f"{min(TRUE_VALUES):.2f}".replace(".", ",") in text
    assert "1:500" in text
    assert len(document.inline_shapes) == 2


def test_pdf_report_is_readable(sample_pdf):
    result = upload(sample_pdf).json()
    response = client.post(f"/api/analyses/{result['id']}/report?format=pdf", json=review_body(result))
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
        text = "\n".join(page.get_text() for page in pdf)
    assert "Отчет по топографической съемке" in text
    assert "Ведомость высотных отметок" in text


def test_report_without_marks_still_builds(sample_pdf):
    result = upload(sample_pdf).json()
    for mark in result["marks"]:
        mark["included"] = False
    response = client.post(f"/api/analyses/{result['id']}/report?format=pdf", json=review_body(result))
    assert response.status_code == 200


def test_unknown_analysis_returns_clear_message():
    response = client.post("/api/analyses/missing/summary", json={"marks": [], "metadata": {}})
    assert response.status_code == 404
    assert "Загрузите файл снова" in response.json()["detail"]


def test_old_analyses_are_evicted(sample_pdf, monkeypatch):
    monkeypatch.setattr(config, "ANALYSES_KEPT_IN_MEMORY", 1)
    first = upload(sample_pdf).json()
    upload(sample_pdf)
    response = client.get(f"/api/analyses/{first['id']}/pages/1.jpg")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"", "пустой"),
        (b"hello world", "Поддерживаются"),
        (b"%PDF-1.7 broken", "поврежден"),
        (b"\xff\xd8\xff\xe0 broken", "повреждено"),
    ],
)
def test_bad_files_have_clear_errors(data, fragment):
    response = upload(data, "x.bin")
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


def test_password_protected_pdf_is_rejected():
    document = pymupdf.open()
    document.new_page()
    buffer = io.BytesIO()
    document.save(buffer, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1", owner_pw="2")
    response = upload(buffer.getvalue())
    assert response.status_code == 422
    assert "паролем" in response.json()["detail"]


def test_upload_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = upload(b"%PDF-" + b"0" * 100)
    assert response.status_code == 413


def test_oversized_body_is_stopped_before_parsing(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        "/api/analyses",
        content=b"0" * 200_000,
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 413
    assert "МБ" in response.json()["detail"]


def test_chunked_body_without_length_is_stopped(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)

    def chunks():
        for _ in range(30):
            yield b"0" * 8192

    response = client.post(
        "/api/analyses",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 413


def test_full_size_page_is_not_kept_after_analysis(sample_pdf):
    result = upload(sample_pdf).json()
    analysis = _analyses[result["id"]]
    assert not hasattr(analysis, "document")
    assert all(len(page.preview) < 3_000_000 for page in analysis.pages.values())


def test_rectified_photo_has_no_slope_even_with_manual_dpi(sample_photo):
    result = upload(sample_photo, "фото.jpg").json()
    assert result["pages"][0]["rectified"] is True
    body = {**review_body(result), "dpi": 200}
    summary = client.post(f"/api/analyses/{result['id']}/summary", json=body).json()
    assert summary["slope"] is None
    assert summary["stats"]["count"] > 0


def test_flat_ground_builds_summary_and_report(sample_pdf):
    result = upload(sample_pdf).json()
    for mark in result["marks"]:
        mark["value"] = 100.0
        mark["included"] = True
    body = review_body(result)
    summary = client.post(f"/api/analyses/{result['id']}/summary", json=body).json()
    assert summary["stats"]["spread"] == 0
    assert summary["slope"]["direction"] is None
    assert summary["map"]
    response = client.post(f"/api/analyses/{result['id']}/report?format=pdf", json=body)
    assert response.status_code == 200


def test_huge_image_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "MAX_PIXELS", 100)
    buffer = io.BytesIO()
    Image.fromarray(np.zeros((50, 50, 3), np.uint8)).save(buffer, format="PNG")
    response = upload(buffer.getvalue(), "x.png")
    assert response.status_code == 422
    assert "Мп" in response.json()["detail"]


def test_blank_image_reports_no_marks():
    buffer = io.BytesIO()
    Image.fromarray(np.full((400, 400, 3), 255, np.uint8)).save(buffer, format="PNG")
    result = upload(buffer.getvalue(), "x.png").json()
    assert result["marks"] == []
    assert any("не найдены" in warning for warning in result["warnings"])


def test_invalid_mark_value_is_rejected(sample_pdf):
    result = upload(sample_pdf).json()
    result["marks"][0]["value"] = 1e9
    response = client.post(f"/api/analyses/{result['id']}/summary", json=review_body(result))
    assert response.status_code == 422


def test_path_is_removed_from_filename(sample_pdf):
    result = upload(sample_pdf, "..\\..\\secret\\план.pdf").json()
    assert result["filename"] == "план.pdf"


def scanned_pdf(source: bytes) -> bytes:
    """Превращает цифровой PDF в скан: страница есть, текстового слоя нет."""
    with pymupdf.open(stream=source, filetype="pdf") as original:
        page = original[0]
        pixmap = page.get_pixmap(dpi=300)
        result = pymupdf.open()
        target = result.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_image(target.rect, pixmap=pixmap)
        return result.tobytes()


def test_scanned_pdf_uses_ocr_and_keeps_scale(sample_pdf):
    result = upload(scanned_pdf(sample_pdf)).json()
    assert result["method"] == "распознавание изображения"
    assert result["pages"][0]["dpi"] == pytest.approx(300, abs=1)
    correct = set(included(result)) & set(TRUE_VALUES)
    assert len(correct) / len(TRUE_VALUES) >= 0.95
    summary = client.post(f"/api/analyses/{result['id']}/summary", json=review_body(result)).json()
    assert summary["slope"] is not None


def test_sideways_scan_is_turned_upright(sample_pdf):
    with pymupdf.open(stream=sample_pdf, filetype="pdf") as original:
        pixmap = original[0].get_pixmap(dpi=200)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).rotate(90, expand=True)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(200, 200))
    result = upload(buffer.getvalue(), "скан.png").json()
    assert any("повернут" in warning for warning in result["warnings"])
    assert len(set(included(result)) & set(TRUE_VALUES)) / len(TRUE_VALUES) >= 0.85


def test_rotated_pdf_page_keeps_marks_on_the_right_spots(sample_pdf):
    with pymupdf.open(stream=sample_pdf, filetype="pdf") as document:
        document[0].set_rotation(90)
        rotated = document.tobytes()
    result = upload(rotated).json()
    assert included(result) == TRUE_VALUES
    page = result["pages"][0]
    assert page["height"] > page["width"]
    assert all(mark["x"] <= page["width"] and mark["y"] <= page["height"] for mark in result["marks"])
