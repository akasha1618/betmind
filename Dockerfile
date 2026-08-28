# BetMind — imagine de productie (Railway sau orice host de containere).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MAX_TOKENS=128000

# gosu: coborarea privilegiilor din entrypoint (root -> betmind), pastrand PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Utilizatorul non-root sub care ruleaza aplicatia. /data primeste permisiunile
# corecte abia in entrypoint: volumul Railway se monteaza peste el la runtime,
# iar un chown facut aici ar fi acoperit de mount.
RUN useradd --create-home betmind \
    && mkdir -p /data \
    && chown -R betmind:betmind /app /data

# sed: elimina eventualele CRLF (repo editat pe Windows) — altfel /bin/sh nu
# gaseste interpretorul si containerul pica la pornire.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh \
    && chmod +x /docker-entrypoint.sh

EXPOSE 8000

# Fara USER aici: entrypoint-ul porneste ca root, pregateste volumul si abia
# apoi trece la betmind (gosu).
ENTRYPOINT ["/docker-entrypoint.sh"]

# Railway injecteaza $PORT; local ramane 8000.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
