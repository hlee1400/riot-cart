FROM python:3.12-slim
WORKDIR /home/sprite/app
# mcp_server is required because pyproject's wheel target packages both worker and mcp_server
COPY pyproject.toml ./
COPY worker/ worker/
COPY mcp_server/ mcp_server/
COPY deploy/install_worker.sh deploy/install_worker.sh
RUN bash deploy/install_worker.sh /home/sprite/app && rm -rf /var/lib/apt/lists/*
ENV PATH=/home/sprite/app/.venv/bin:$PATH
