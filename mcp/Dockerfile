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

# Install Playwright + Chromium so megamaid_run can launch project scrapers.
# PLAYWRIGHT_BROWSERS_PATH puts the binaries in /ms-playwright so they are
# readable by the non-root mcp user at runtime. Without this, playwright
# installs to /root/.cache which the mcp user cannot access.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && \
    chmod -R 755 /ms-playwright

# Copy MCP server (self-contained — no shared/ dependency)
COPY mcp/server.py /app/server.py

USER mcp

EXPOSE 8000

CMD ["python", "/app/server.py"]
