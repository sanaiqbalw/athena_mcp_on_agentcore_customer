# AgentCore Runtime runs ARM64 containers.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py identity.py aws_context.py athena_mcp_server.py ./

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# AgentCore Runtime expects the MCP server on exactly this host, port and path.
EXPOSE 8000
CMD ["python", "athena_mcp_server.py"]
