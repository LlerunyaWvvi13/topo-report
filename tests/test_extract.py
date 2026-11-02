import numpy as np
import pytest

from app.extract import (
    extract_marks,
    extract_metadata,
    relief_step_metres,
    scale_denominator,
)
from app.loader import Page, TextItem


def item(text: str, x: float = 0, y: float = 0, source: str = "pdf", confidence: float = 1.0, line=None):
    return TextItem(text, x, y, 40, 10, confidence, source, line or (x, y))


def page() -> Page:
    return Page(number=1, image=np.zeros((10, 10, 3), np.uint8), dpi=300)


def read(items):
    return extract_marks([(page(), items)])


def spread(values, source="pdf", confidence=1.0):
    return [
        item(text, x=index * 100, y=index * 50, source=source, confidence=confidence)
        for index, text in enumerate(values)
    ]


def test_comma_and_dot_are_both_recognized():
    marks = read(spread(["145,27", "146.31", "147.02"]))
    assert [mark.value for mark in marks] == [145.27, 146.31, 147.02]
    assert all(mark.status == "ok" for mark in marks)


def test_lengths_outside_main_group_are_excluded():
    marks = read(spread(["145.27", "146.31", "147.02", "148.50", "54.33", "12.50"]))
    doubtful = {mark.value: mark for mark in marks if mark.status == "doubtful"}
    assert set(doubtful) == {54.33, 12.5}
    assert not any(mark.included for mark in doubtful.values())


def test_second_group_on_a_slope_stays_for_review():
    marks = read(spread(["100.25", "101.10", "101.80", "110.40", "111.05"]))
    second = [mark for mark in marks if mark.value > 105]
    assert len(second) == 2
    assert all(mark.status == "check" and mark.included for mark in second)


def test_dates_coordinates_and_integers_are_not_heights():
    marks = read(spread(["145.27", "146.31", "12.05.2024", "456000", "1320050", "150"]))
    assert [mark.value for mark in marks] == [145.27, 146.31]


def test_area_context_is_skipped():
    line = ("title",)
    items = spread(["145.27", "146.31"]) + [
        item("S=", 0, 900, line=line),
        item("145.90", 60, 900, line=line),
        item("кв.м", 120, 900, line=line),
    ]
    assert 145.9 not in [mark.value for mark in read(items)]


def test_other_precision_goes_to_doubtful():
    marks = read(spread(["145.27", "146.31", "147.02", "148.5"]))
    assert next(mark for mark in marks if mark.value == 148.5).status == "doubtful"


def test_low_confidence_requires_check():
    marks = read(spread(["145.27", "146.31"]) + [item("147.02", 900, 900, "ocr", 0.4)])
    assert next(mark for mark in marks if mark.value == 147.02).status == "check"


def test_lost_decimal_point_is_restored_for_review():
    marks = read(spread(["145.27", "146.31", "147.02"]) + [item("14652", 900, 900, "ocr", 0.9)])
    restored = next(mark for mark in marks if mark.value == 146.52)
    assert restored.status == "check"
    assert restored.included


def test_duplicate_reading_merges_and_keeps_plausible_value():
    items = spread(["145.27", "146.31", "147.02"], "ocr", 0.9) + [
        item("47.05", 900, 900, "ocr", 0.96),
        item("147.05", 904, 901, "ocr", 0.5),
    ]
    marks = read(items)
    merged = [mark for mark in marks if mark.x > 800]
    assert len(merged) == 1
    assert merged[0].value == 147.05
    assert merged[0].status == "check"


def test_point_marker_read_as_digit_is_dropped():
    marks = read(spread(["145.27", "146.31", "147.02"], "ocr", 0.9) + [item("6146.10", 900, 900, "ocr", 0.9)])
    fixed = next(mark for mark in marks if mark.x > 800 and mark.included)
    assert fixed.value == 146.1
    assert fixed.status == "check"


def test_no_numbers_gives_no_marks():
    assert read([item("Топографический"), item("план")]) == []


def test_metadata_is_found():
    lines = [
        "Топографический план участка",
        "Адрес: Московская обл., дер. Тестово",
        "КН 50:21:0040102:15 и 50:21:0040102:16",
        "Масштаб 1:500",
        "Сечение рельефа 0.5 м",
        "Система высот Балтийская 1977",
        "Система координат МСК-50",
        "Площадь 1 250 кв.м",
        "Дата съемки 12.05.2024",
        "Исполнитель: ООО Пример",
    ]
    items = []
    for row, line in enumerate(lines):
        for column, word in enumerate(line.split()):
            items.append(item(word, column * 60, row * 20, line=(row,)))
    metadata = extract_metadata(items)
    assert metadata["cadastral_numbers"] == "50:21:0040102:15, 50:21:0040102:16"
    assert metadata["scale"] == "1:500"
    assert metadata["relief_step"] == "0,5 м"
    assert metadata["coordinate_system"] == "МСК-50"
    assert metadata["height_system"].startswith("Балтийская")
    assert metadata["area"] == "1250 кв. м"
    assert metadata["survey_date"] == "12.05.2024"
    assert metadata["address"] == "Московская обл., дер. Тестово"
    assert metadata["contractor"] == "ООО Пример"


def test_absent_metadata_stays_empty():
    metadata = extract_metadata([item("Схема")])
    assert metadata["scale"] == ""
    assert metadata["cadastral_numbers"] == ""


@pytest.mark.parametrize(
    ("text", "expected"), [("1:500", 500), (" 1 : 2000 ", 2000), ("1/1000", 1000), ("", None), ("500", None)]
)
def test_scale_denominator(text, expected):
    assert scale_denominator(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("0,5 м", 0.5), ("1 м", 1.0), ("", None), ("0", None)])
def test_relief_step(text, expected):
    assert relief_step_metres(text) == expected
