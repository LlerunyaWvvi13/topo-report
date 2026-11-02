FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/topo-report
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY samples samples

RUN useradd --create-home topo
USER topo
ENV OMP_THREAD_LIMIT=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
