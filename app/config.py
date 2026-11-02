"""Ограничения и настройки приложения."""

from __future__ import annotations

import os

MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_PIXELS = 60_000_000
MAX_PDF_PAGES = 5
MAX_MARKS = 5000
MIN_PDF_DPI = 150
TARGET_PDF_DPI = 300

OCR_TILE_SIZE = 2200
OCR_TILE_OVERLAP = 220
OCR_TIMEOUT_SECONDS = 180
OCR_LANGUAGES = "rus+eng"
MAX_REFINED_BLOBS = 1500

# Отметки одного листа образуют плотную группу. Разрыв больше ELEVATION_MIN_GAP_M отделяет
# ее от других чисел. Значения в пределах ELEVATION_REVIEW_GAP_M могут быть верными
# отметками на крутом склоне, поэтому остаются в расчете и идут на сверку. Числа дальше
# считаются посторонними: длинами линий, площадями, номерами.
ELEVATION_MIN_GAP_M = 5.0
ELEVATION_REVIEW_GAP_M = 20.0
MIN_ELEVATION = -450.0
MAX_ELEVATION = 9000.0
GOOD_CONFIDENCE = 0.75

ANALYSES_KEPT_IN_MEMORY = 8
MAX_PARALLEL_ANALYSES = 2

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("TOPO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if host.strip()
]
