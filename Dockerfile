# TamaBot — production image (long polling, webhook-порты не нужны)
# Сборка из корня репозитория:  docker build -t tamabot .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl/ps нужны для HEALTHCHECK внутри контейнера
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl procps tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала только requirements — слой кэшируется и не пересобирается при правке кода
COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/app ./app
COPY bot/alembic.ini ./alembic.ini
COPY bot/alembic ./alembic

# Непривилегированный пользователь; /app/data — volume для sqlite/сессий/логов
RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser
VOLUME ["/app/data"]

# Healthcheck: процесс бота жив и отвечает на SIG 0. При зависании/poll-ошибках
# aiogram завершает процесс, Docker отметит контейнер unhealthy и (с restart:
# unless-stopped в compose) поднимет новый.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "app.main" >/dev/null 2>&1 || exit 1

CMD ["python", "-m", "app.main"]
