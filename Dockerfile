FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
# COPY recalled.db /app/seed/recalled.db

CMD ["sh", "-c", "if [ ! -f /app/data/recalled.db ] && [ -f /app/seed/recalled.db ]; then cp /app/seed/recalled.db /app/data/recalled.db; fi; exec python bot.py"]