# The AutoBrowser stand (`autobrowser serve`) with headless Chromium behind Playwright MCP.
# Guide: docs/development/serve-mode.md. Build: docker build -t autobrowser-serve .
#
# /data is the AutoBrowser home (config, browser profile, recordings, ledger, events) and
# /data/serve.yaml the stand profile (docker/serve.example.yaml is a starting point).

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.21

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM ${PYTHON_IMAGE}
ARG PLAYWRIGHT_MCP_VERSION=0.0.84
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    AUTOBROWSER_HOME=/data \
    AUTOBROWSER_CONFIG_FILE=/data/serve.yaml \
    PYTHONUNBUFFERED=1

# Node.js for the MCP server, tzdata for serve.timezone, then Chromium with its system
# libraries, installed by the Playwright that @playwright/mcp itself depends on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm tzdata ca-certificates \
    && npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
    && cd "$(npm root -g)/@playwright/mcp" \
    && npx --yes playwright install --with-deps chromium \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=build /dist/*.whl /tmp/wheels/
RUN uv tool install /tmp/wheels/*.whl && rm -rf /tmp/wheels /root/.cache

# The MCP SDK starts stdio servers with a reduced environment (no PLAYWRIGHT_BROWSERS_PATH):
# Playwright then looks in the user's default cache, so that is where the browsers are found.
RUN useradd --create-home --uid 10001 autobrowser \
    && mkdir -p /data /home/autobrowser/.cache \
    && ln -s /ms-playwright /home/autobrowser/.cache/ms-playwright \
    && chown -R autobrowser /data /ms-playwright /home/autobrowser/.cache
USER autobrowser
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"

ENTRYPOINT ["autobrowser"]
CMD ["serve"]
