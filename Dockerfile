FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 adrobot \
    && mkdir -p /app/data && chown -R adrobot:adrobot /app
COPY --chown=adrobot:adrobot . .
USER adrobot
ENV DATABASE_URL=sqlite:////app/data/adrobot.db \
    DEMO_DATABASE_URL=sqlite:////app/data/demo-keitaro.db
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["python", "-m", "app.run"]
