# Optional. AgentLite runs fine as a plain Python process - this image only
# exists for people who want isolation or reproducible deployments.
#
#   docker build -t agentlite .
#   docker run --rm -p 8765:8765 -e OPENAI_API_KEY -e AGENTLITE_API_TOKEN \
#     -v "$PWD/workspace:/app/workspace" agentlite
#
# With the browser tools:
#   docker build -t agentlite --build-arg INSTALL_BROWSER=true .

FROM python:3.11-slim

ARG INSTALL_BROWSER=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 agentlite

COPY pyproject.toml README.md LICENSE ./
COPY agentlite ./agentlite

RUN pip install --no-cache-dir . \
    && if [ "$INSTALL_BROWSER" = "true" ]; then \
           pip install --no-cache-dir "playwright>=1.40" \
           && playwright install --with-deps chromium; \
       fi

# The API binds inside the container; expose it deliberately.
ENV AGENTLITE__SERVER__HOST=0.0.0.0

RUN mkdir -p /app/workspace && chown -R agentlite:agentlite /app
USER agentlite

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"

CMD ["agentlite", "start"]
