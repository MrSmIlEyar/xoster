FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# (Опционально) системные зависимости. Для большинства случаев можно убрать,
# но curl полезен для диагностики, а ca-certificates — для https.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — для кеша слоёв (быстрее пересборки). [web:10]
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Код
COPY . /app

# Папка под временные файлы и стейт (можешь переопределить WORKDIR/MAP_FILE env-ами).
RUN mkdir -p /app/_mirror_tmp

# Вынеси сессию/карту в volume (чтобы переживали перезапуск контейнера)
VOLUME ["/app"]

CMD ["python", "main.py"]
