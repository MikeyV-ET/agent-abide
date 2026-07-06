# agent-abide smoke test
# Verifies a clean install: deps, setup, config, script path resolution,
# and asdaaas startup (up to backend session load).
#
# Usage:
#   docker build -t agent-abide-test .
#   docker run --rm -e XAI_API_KEY="xai-..." agent-abide-test
#
# Without XAI_API_KEY, the smoke test verifies everything up to the point
# where the grok binary tries to authenticate.

FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git bash procps \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (for grok binary)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install grok binary
RUN npm install -g @xai-official/grok

# Python deps
RUN pip install --no-cache-dir \
    requests \
    websockets \
    websocket-client \
    textual \
    rich

# Create a non-root user (matches typical setup)
RUN useradd -m -s /bin/bash testuser
USER testuser
WORKDIR /home/testuser

# Copy repo
COPY --chown=testuser:testuser . /home/testuser/agent-abide

# Setup: copy example config and create an agent
RUN cd /home/testuser/agent-abide \
    && cp agents.json.example agents.json \
    && sed -i "s|/home/YOURUSER|/home/testuser|g" agents.json \
    && sed -i "s|ExampleAgent|SmokeTestAgent|g" agents.json \
    && mkdir -p /home/testuser/agents/SmokeTestAgent \
    && mkdir -p /tmp/asdaaas_logs

# Run smoke test
COPY --chown=testuser:testuser scripts/smoke_test.sh /home/testuser/smoke_test.sh
RUN chmod +x /home/testuser/smoke_test.sh

CMD ["/home/testuser/smoke_test.sh"]
