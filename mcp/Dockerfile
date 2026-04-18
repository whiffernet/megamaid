FROM python:3.12-slim

# Non-root user for security
RUN groupadd -r mcp && useradd -r -g mcp -s /bin/false mcp

WORKDIR /app

# Install FastMCP and httpx (megamaid's recon module needs httpx)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "fastmcp>=3.0.0" "httpx>=0.27.0" "pydantic>=2.5.0"

# Install megamaid templates (provides the recon module used in-process).
# Build context is the repo root so templates/ is accessible from mcp/Dockerfile.
COPY templates/ /app/megamaid-templates/
RUN pip install --no-cache-dir /app/megamaid-templates/

# Copy MCP server (self-contained — no shared/ dependency)
COPY mcp/server.py /app/server.py

USER mcp

EXPOSE 8000

CMD ["python", "/app/server.py"]
