FROM ghcr.io/openclaw/openclaw:latest

USER root

# Install jq system-wide
RUN apt-get update -qq && apt-get install -y jq python3-dateutil && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python 3.12 standalone (no system deps, self-contained)
RUN curl -fsSL https://github.com/astral-sh/python-build-standalone/releases/download/20250317/cpython-3.12.9+20250317-x86_64-unknown-linux-gnu-install_only.tar.gz \
    | tar -xz -C /opt \
    && ln -sf /opt/python/bin/python3.12 /usr/local/bin/python3.12 \
    && /opt/python/bin/pip3 install --no-cache-dir cookidoo-api openpyxl python-dateutil

USER node
