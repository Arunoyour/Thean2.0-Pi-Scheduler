FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 scheduler

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py jobs.json ./
RUN mkdir -p /app/logs /app/state \
    && chown -R scheduler:scheduler /app

USER scheduler

EXPOSE 8090

CMD ["python", "main.py"]
