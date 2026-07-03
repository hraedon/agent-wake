# agent-waked daemon — container image
#
# Per Plan 004 WI-3.1 and blueprint §3 (deployment substrate: Linux/Docker/Windows).
# The daemon reads only config.json + suite.env + secrets; no host filesystem
# access beyond the mounted config volume and the unix socket.
#
# Build:  docker build -t agent-waked .
# Run:    docker run -d --name agent-waked \
#           -p 127.0.0.1:8788:8788 \
#           -v ~/.config/agent-wake:/config:ro \
#           -e AGENT_WAKE_CONFIG=/config/config.json \
#           -e AGENT_WAKE_LOG_FORMAT=json \
#           agent-waked
#
# For Vault-backed sources, mount the AppRole secret_id file and set VAULT_*.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="agent-waked"
LABEL org.opencontainers.image.description="agent-wake daemon — external event signaling"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/hraedon/agent-wake"

RUN useradd --create-home --shell /bin/bash agentwake

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY README.md ./

RUN pip install --no-cache-dir . && \
    pip cache purge

USER agentwake

ENV AGENT_WAKE_CONFIG=/config/config.json
ENV AGENT_WAKE_LOG_FORMAT=json
ENV PYTHONUNBUFFERED=1

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, json; urllib.request.urlopen('http://127.0.0.1:8788/', timeout=3)"

ENTRYPOINT ["agent-waked"]
