FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
ARG WITH_LIVE=0
RUN pip install --no-cache-dir . && if [ "$WITH_LIVE" = "1" ]; then pip install --no-cache-dir robin_stocks; fi
RUN useradd -r -u 10001 goblin
USER goblin
EXPOSE 8000
# --no-access-log: the default access log writes every path and query string (search terms, goblin names, token prefixes) to the container log
CMD ["uvicorn", "--factory", "goblinbank.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-level", "warning"]
