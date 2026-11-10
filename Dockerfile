FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
ARG WITH_LIVE=0
RUN pip install --no-cache-dir . && if [ "$WITH_LIVE" = "1" ]; then pip install --no-cache-dir robin_stocks; fi
RUN useradd -r -u 10001 goblin
USER goblin
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# --no-access-log: the default access log writes every path and query string (search terms, goblin names, token prefixes) to the container log.
# The app logs its own operational lines instead (started, connected, sync counts, failed sign-in counts), never anything personal: see src/goblinbank/oplog.py.
CMD ["uvicorn", "--factory", "goblinbank.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-level", "info"]
