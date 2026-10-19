# Optional. AgentLite runs fine as a plain Python process - this image exists so
# that people who want isolation get a good one, not a decorative one.
#
#   docker build -t agentlite .
#   docker run --rm \
#     --read-only --cap-drop ALL --security-opt no-new-privileges \
#     --tmpfs /tmp:rw,noexec,nosuid,size=64m \
#     -p 127.0.0.1:8765:8765 \
#     -e OPENAI_API_KEY -e AGENTLITE_API_TOKEN \
#     -v "$PWD/workspace:/app/workspace" \
#     agentlite
#
# Notes that matter:
#   * Bind to 127.0.0.1 on the host (as above) unless you have a TLS reverse
#     proxy in front of it. The container listens on 0.0.0.0 by necessity.
#   * AGENTLITE_API_TOKEN is required by `agentlite start` for a non-loopback
#     bind; without it the container exits with an explanation.
#   * --read-only + --cap-drop ALL + no-new-privileges are the point of using a
#     container at all. The image is built so that they work: nothing needs to
#     write outside /tmp and /app/workspace.
#   * This is defence in depth, not a sandbox on its own. An allowed command
#     still runs as uid 10001 inside the container.
#
# With the browser tools:
#   docker build -t agentlite --build-arg INSTALL_BROWSER=true .

FROM python:3.11-slim

ARG INSTALL_BROWSER=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTLITE__LOGGING__DIR=/app/workspace/.logs

WORKDIR /app

# Unprivileged user: AgentLite never needs root, and "an allowed command runs as
# your user" is much less scary when that user is not root.
RUN useradd --create-home --uid 10001 --gid 0 agentlite

COPY pyproject.toml README.md LICENSE ./
COPY agentlite ./agentlite

RUN pip install --no-cache-dir . \
    && if [ "$INSTALL_BROWSER" = "true" ]; then \
           pip install --no-cache-dir "playwright>=1.40" \
           && playwright install chromium; \
       fi \
    && python -c "import agentlite, sys; sys.exit(0 if agentlite.__version__ else 1)"

# Writable paths only: the workspace (mounted) and a tmpfs for Chromium.
RUN mkdir -p /app/workspace /tmp && chown -R agentlite:0 /app && chmod -R g=u /app

# The API binds inside the container; expose it deliberately.
ENV AGENTLITE__SERVER__HOST=0.0.0.0

USER 10001

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"

CMD ["agentlite", "start"]
