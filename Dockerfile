FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV POETRY_VERSION=2.2.0
# The process default is 127.0.0.1. A published container port only works
# when the server listens on every interface inside the container.
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

WORKDIR /app

RUN pip install --upgrade pip \
 && pip install poetry==$POETRY_VERSION --no-cache-dir \
 && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock README.md ./

# Install dependencies only (no root package yet) for better layer caching
RUN poetry install --only=main --no-root

COPY src ./src

# Install the root package now that src/ exists
RUN poetry install --only=main --no-interaction

RUN groupadd -r mcpuser && useradd -r -g mcpuser mcpuser

RUN chown -R mcpuser:mcpuser /app

USER mcpuser

CMD ["python", "src/main.py"]
