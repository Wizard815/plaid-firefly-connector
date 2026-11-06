FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
ARG WITH_LIVE=0
RUN pip install --no-cache-dir . && if [ "$WITH_LIVE" = "1" ]; then pip install --no-cache-dir robin_stocks; fi
RUN useradd -r -u 10001 goblin
USER goblin
EXPOSE 8000
CMD ["uvicorn", "--factory", "goblinbank.main:app", "--host", "0.0.0.0", "--port", "8000"]
