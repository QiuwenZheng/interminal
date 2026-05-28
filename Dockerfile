# Use a slim Python base image
FROM python:3.11-slim

WORKDIR /app

# Copy the project files
COPY . .

# Install the package and dependencies
RUN pip install --no-cache-dir .

# Ensure Python output is unbuffered to see logs immediately
ENV PYTHONUNBUFFERED=1

# Run the MCP server
ENTRYPOINT ["interminal"]
