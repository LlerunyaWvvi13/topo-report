"""Извлечение реквизитов и высотных отметок из распознанного текста."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import median

from . import config
from .loader import Page, TextItem

NUMBER = re.compile(r"[+]?\d{1,4}\.\d{1,3}")
BARE_INTEGER = re.compile(r"\d{4,5}")
LOOKALIKES = str.maketrans({"О": "0", "о": "0", "O": "0", "o": "0", "З": "3", "l": "1", "I": "1"})
MIN_GROUP_SIZE = 3
NON_HEIGHT_CONTEXT = re.compile(
    r"^(s=?|площад\w*|кв\.?м?|м2|м²|га|h=?|масштаб|лист\w*|стр\.?|№|сечени\w*|через)$",
    re.IGNORECASE,
)


@dataclass
class Mark:
    id: int
    value: float
    page: int
    x: float
    y: float
    width: float
    height: float
    confidence: float
    source: str
    status: str
    note: str
    included: bool
    raw: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Range:
    low: float
    high: float

    def __contains__(self, value: float) -> bool:
        return self.low <= value <= self.high


@dataclass
class _Candidate:
    value: float
    decimals: int
    item: TextItem
    page: int
    note: str = ""
    dot_restored: bool = False


def _clean_number(text: str, source: str) -> str:
    cleaned = text.strip(" ;:()[]{}'\"«»|_-~")
    if source == "ocr":
        cleaned = cleaned.translate(LOOKALIKES)
    return cleaned.replace(",", ".").rstrip(".")


def _neighbours_by_line(items: list[TextItem]) -> dict[int, tuple[str, str]]:
    """Находит соседние слова в строке, чтобы отсеять подписи вроде «S = 12.50»."""
    groups: dict[tuple, list[int]] = {}
    for index, item in enumerate(items):
        groups.setdefault(item.line, []).append(index)
    neighbours = {}
    for indices in groups.values():
        ordered = sorted(indices, key=lambda index: items[index].x)
        for position, index in enumerate(ordered):
            before = items[ordered[position - 1]].text if position else ""
            after = items[ordered[position + 1]].text if position + 1 < len(ordered) else ""
            neighbours[index] = (before, after)
    return neighbours


def _candidates(page: Page, items: list[TextItem]) -> tuple[list[_Candidate], list[_Candidate]]:
    """Делит числа на обычные десятичные и целые, где могла потеряться точка."""
    neighbours = _neighbours_by_line(items)
    decimal, integers = [], []
    for index, item in enumerate(items):
        text = _clean_number(item.text, item.source)
        before, after = neighbours.get(index, ("", ""))
        has_context = bool(
            NON_HEIGHT_CONTEXT.match(before.strip(":=,")) or NON_HEIGHT_CONTEXT.match(after.strip(":=,."))
        )
        if NUMBER.fullmatch(text):
            value = float(text)
            if has_context or not config.MIN_ELEVATION <= value <= config.MAX_ELEVATION:
                continue
            decimal.append(_Candidate(value, len(text.split(".")[1]), item, page.number))
        elif item.source == "ocr" and BARE_INTEGER.fullmatch(text) and not has_context:
            integers.append(_Candidate(float(text) / 100, 2, item, page.number, dot_restored=True))
    return decimal, integers


def _groups(ordered: list[float], limit: float) -> list[list[float]]:
    groups = [[ordered[0]]]
    for previous, value in zip(ordered, ordered[1:]):
        if value - previous > limit:
            groups.append([])
        groups[-1].append(value)
    return groups


def _elevation_ranges(values: list[float]) -> tuple[_Range, _Range] | None:
    """Возвращает основную группу отметок и более широкий диапазон для сверки."""
    if not values:
        return None
    ordered = sorted(values)
    gaps = [second - first for first, second in zip(ordered, ordered[1:])]
    typical_gap = 4 * median(gaps) if gaps else 0.0

    core = max(_groups(ordered, max(config.ELEVATION_MIN_GAP_M, typical_gap)), key=len)
    wide_groups = _groups(ordered, max(config.ELEVATION_REVIEW_GAP_M, typical_gap))
    wide = next(group for group in wide_groups if core[0] in group)
    return _Range(core[0], core[-1]), _Range(wide[0], wide[-1])


def _distance_threshold(first: TextItem, second: TextItem) -> float:
    return max(first.height, second.height, 8.0) * 1.5


def _deduplicate(
    candidates: list[_Candidate], allowed: _Range, dominant: int
) -> list[_Candidate]:
    """Объединяет чтения одной подписи: из разных проходов и соседних фрагментов.

    Первым идет чтение, которое лучше согласуется с остальными отметками листа.
    """

    def rank(candidate: _Candidate) -> tuple[bool, bool, bool, float]:
        return (
            candidate.value in allowed,
            candidate.decimals == dominant,
            not candidate.dot_restored,
            candidate.item.confidence,
        )

    kept: list[_Candidate] = []
    for candidate in sorted(candidates, key=rank, reverse=True):
        duplicate = None
        for other in kept:
            limit = _distance_threshold(other.item, candidate.item)
            if (
                other.page == candidate.page
                and abs(other.item.x - candidate.item.x) <= limit * 1.5
                and abs(other.item.y - candidate.item.y) <= limit
            ):
                duplicate = other
                break
        if duplicate is None:
            kept.append(candidate)
        elif abs(duplicate.value - candidate.value) > 1e-6 and not duplicate.note:
            duplicate.note = f"Второе чтение: {candidate.value:g}"
    return kept


def _without_marker_digit(candidates: list[_Candidate], allowed: _Range) -> list[_Candidate]:
    """Проверяет версию числа без первой цифры: кружок точки иногда читается как 1 или 0."""
    fixed = []
    for candidate in candidates:
        if candidate.item.source != "ocr" or candidate.value in allowed:
            continue
        text = _clean_number(candidate.item.text, "ocr").lstrip("+")
        shorter = text[1:]
        if NUMBER.fullmatch(shorter) and float(shorter) in allowed:
            fixed.append(
                _Candidate(
                    float(shorter),
                    candidate.decimals,
                    candidate.item,
                    candidate.page,
                    note=f"Прочитано как {text}, первая цифра отброшена",
                )
            )
    return fixed


def extract_marks(pages: list[tuple[Page, list[TextItem]]]) -> list[Mark]:
    """Разбирает листы по отдельности: высоты на разных листах могут сильно различаться."""
    marks: list[Mark] = []
    for page, items in pages:
        marks.extend(_extract_page_marks(page, items, first_id=len(marks) + 1))
    return marks


def _extract_page_marks(page: Page, items: list[TextItem], first_id: int) -> list[Mark]:
    decimal, integers = _candidates(page, items)
    ranges = _elevation_ranges([candidate.value for candidate in decimal])
    if ranges is None:
        return []
    core, wide = ranges
    margin = max(1.0, (wide.high - wide.low) * 0.1)
    expanded = _Range(wide.low - margin, wide.high + margin)
    restored = [candidate for candidate in integers if candidate.value in expanded]
    in_core = [candidate for candidate in decimal if candidate.value in core]
    dominant = Counter(candidate.decimals for candidate in in_core).most_common(1)[0][0]
    trimmed = _without_marker_digit(decimal, wide)
    few_marks = len(in_core) < MIN_GROUP_SIZE

    marks = []
    ordered = sorted(
        _deduplicate(decimal + restored + trimmed, wide, dominant),
        key=lambda candidate: (candidate.item.y, candidate.item.x),
    )
    for number, candidate in enumerate(ordered, start=first_id):
        status, note, included = _classify(candidate, core, wide, dominant)
        if status == "ok" and few_marks:
            status, note = "check", "На листе мало чисел: возможно, это не отметка"
        marks.append(
            Mark(
                id=number,
                value=round(candidate.value, 3),
                page=candidate.page,
                x=round(candidate.item.x, 1),
                y=round(candidate.item.y, 1),
                width=round(candidate.item.width, 1),
                height=round(candidate.item.height, 1),
                confidence=round(candidate.item.confidence, 3),
                source=candidate.item.source,
                status=status,
                note=note,
                included=included,
                raw=candidate.item.text,
            )
        )
    return marks


def _classify(
    candidate: _Candidate, core: _Range, wide: _Range, dominant: int
) -> tuple[str, str, bool]:
    if candidate.dot_restored:
        return "check", "Точка восстановлена, сверьте с листом", True
    if candidate.value not in wide:
        return "doubtful", "Вне диапазона отметок листа: возможно, длина или площадь", False
    if candidate.decimals != dominant:
        return "doubtful", f"Другая точность: обычно {dominant} знака после запятой", False
    if candidate.value not in core:
        return "check", "Далеко от основной группы отметок, сверьте с листом", True
    if candidate.note:
        return "check", candidate.note, True
    if candidate.item.confidence < config.GOOD_CONFIDENCE:
        return "check", "Низкая уверенность распознавания", True
    return "ok", "", True


METADATA_FIELDS = (
    "title",
    "address",
    "cadastral_numbers",
    "scale",
    "relief_step",
    "height_system",
    "coordinate_system",
    "survey_date",
    "area",
    "contractor",
)

CADASTRAL = re.compile(r"(?<![\d:])\d{2}\s?:\s?\d{2}\s?:\s?\d{6,7}\s?:\s?\d{1,6}(?![\d:])")
SCALE = re.compile(r"(?<![\d.:])1\s?[:/]\s?(\d{3,5})(?![\d.:])")
RELIEF = re.compile(
    r"(?:сечени\w*\s+рельефа|высот\w*\s+сечени\w*|горизонтали\s+проведены\s+через)"
    r"\D{0,30}?(\d+(?:[.,]\d+)?)\s*м",
    re.IGNORECASE,
)
HEIGHT_SYSTEM = re.compile(
    r"(балтийск\w*(?:\s+систем\w*)?(?:\s+высот)?(?:\s+\d{4}(?:\s*г\.?)?)?"
    r"|условн\w*\s+систем\w*\s+высот|бсв[- ]?77)",
    re.IGNORECASE,
)
COORDINATES = re.compile(
    r"(мск[- ]?\d{1,3}(?:[- ]\w{1,3})?|ск[- ]?(?:42|63|95)|гск[- ]?2011|местн\w*\s+систем\w*)",
    re.IGNORECASE,
)
DATE = re.compile(r"(?<![\d.])(\d{2}\.\d{2}\.(?:19|20)\d{2})(?![\d.])")
AREA = re.compile(
    r"(?:площад\w*|(?<![а-яa-z])s)\s*[:=-]?\s*(\d[\d ]*(?:[.,]\d+)?)\s*(кв\.?\s?м\.?|м2|м²|га)",
    re.IGNORECASE,
)
LABELLED = {
    "address": re.compile(r"(?:адрес|местоположение)\s*[:=-]\s*(.+)", re.IGNORECASE),
    "contractor": re.compile(r"(?:исполнитель|подрядчик|организация)\s*[:=-]\s*(.+)", re.IGNORECASE),
}
TITLE = re.compile(r"топографическ\w+\s+(?:план|съ[её]мк\w*)[^\n]*", re.IGNORECASE)


def _reading_lines(items: list[TextItem]) -> list[str]:
    """Собирает слова в строки текста."""
    groups: dict[tuple, list[TextItem]] = {}
    for item in items:
        groups.setdefault(item.line, []).append(item)
    lines = []
    for group in sorted(groups.values(), key=lambda words: (min(w.y for w in words), min(w.x for w in words))):
        ordered = sorted(group, key=lambda word: word.x)
        lines.append(" ".join(word.text for word in ordered))
    return lines


def _capitalized(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .,;:")
    return cleaned[:1].upper() + cleaned[1:]


def extract_metadata(items: list[TextItem]) -> dict[str, str]:
    """Находит реквизиты плана. Если поля нет на листе, значение остается пустым."""
    lines = _reading_lines(items)
    text = "\n".join(lines)
    metadata = {name: "" for name in METADATA_FIELDS}

    numbers = [re.sub(r"\s", "", match) for match in CADASTRAL.findall(text)]
    metadata["cadastral_numbers"] = ", ".join(dict.fromkeys(numbers))

    scales = Counter(SCALE.findall(text))
    allowed = {"200", "500", "1000", "2000", "5000", "10000"}
    labelled = re.search(r"масштаб\D{0,12}1\s?[:/]\s?(\d{3,5})", text, re.IGNORECASE)
    if labelled:
        metadata["scale"] = f"1:{labelled.group(1)}"
    else:
        known = [value for value, _ in scales.most_common() if value in allowed]
        if known:
            metadata["scale"] = f"1:{known[0]}"

    if match := RELIEF.search(text):
        metadata["relief_step"] = f"{match.group(1).replace('.', ',')} м"
    if match := HEIGHT_SYSTEM.search(text):
        metadata["height_system"] = _capitalized(match.group(1))
    if match := COORDINATES.search(text):
        metadata["coordinate_system"] = match.group(1).upper().replace(" ", "-")
    if match := AREA.search(text):
        amount = re.sub(r"\s", "", match.group(1))
        unit = "га" if match.group(2).lower() == "га" else "кв. м"
        metadata["area"] = f"{amount} {unit}"
    if match := DATE.search(text):
        metadata["survey_date"] = match.group(1)
    if match := TITLE.search(text):
        metadata["title"] = _capitalized(match.group(0))
    for name, pattern in LABELLED.items():
        for line in lines:
            if match := pattern.search(line):
                metadata[name] = _capitalized(match.group(1))
                break
    return metadata


def scale_denominator(scale: str) -> int | None:
    match = re.fullmatch(r"\s*1\s*[:/]\s*(\d{2,6})\s*", scale or "")
    return int(match.group(1)) if match else None


def relief_step_metres(value: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", value or "")
    if not match:
        return None
    step = float(match.group(0).replace(",", "."))
    return step if 0 < step <= 100 else None
