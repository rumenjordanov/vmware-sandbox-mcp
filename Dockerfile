# This image exists so MCP directories can start the server and read its tool list.
# It cannot create sandboxes: those need a Windows host with VMware Workstation.
# Without settings the server starts, lists its 15 tools, and every tool call
# answers that it is not set up. For real use, see README.md.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home app
USER app

ENTRYPOINT ["vmware-sandbox-mcp"]
