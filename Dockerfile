# BetMind — imagine de productie (Railway sau orice host de containere).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Utilizator non-root; /data e punctul de montare al volumului persistent.
RUN useradd --create-home betmind \
    && mkdir -p /data \
    && chown -R betmind:betmind /app /data
USER betmind

EXPOSE 8000

# Railway injecteaza $PORT; local ramane 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
