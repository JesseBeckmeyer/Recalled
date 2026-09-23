FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
<<<<<<< HEAD
COPY /data/recalled.db /app/seed/recalled.db
=======
# COPY recalled.db /app/seed/recalled.db
>>>>>>> 0941b66c9f217dd1ef226d5dfe0e7b2d3814325e

CMD ["sh", "-c", "if [ ! -f /app/data/recalled.db ] && [ -f /app/seed/recalled.db ]; then cp /app/seed/recalled.db /app/data/recalled.db; fi; exec python bot.py"]