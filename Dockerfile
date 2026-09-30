FROM python:3.12-slim AS builder

RUN python -m venv /opt/venv 
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim
RUN groupadd -r app && useradd -r -g app app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=app:app . .

USER app 
EXPOSE 8000

# --no-access-log : ne pas journaliser les IP ni les URL (coordonnées, recherches)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]