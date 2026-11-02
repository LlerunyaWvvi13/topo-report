from pathlib import Path

import pytest

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def sample_pdf() -> bytes:
    return (SAMPLES / "sample_plan.pdf").read_bytes()


@pytest.fixture
def sample_photo() -> bytes:
    return (SAMPLES / "sample_plan_photo.jpg").read_bytes()
