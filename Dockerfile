FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY __init__.py server.py server_http.py terminal_ui.py ./
COPY tools ./tools

RUN mkdir -p /app/data

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV MCP_PATH=/mcp
ENV RESEARCH_SESSION_DB=/app/data/research_sessions.sqlite3

EXPOSE 8000

CMD ["python", "server_http.py"]
