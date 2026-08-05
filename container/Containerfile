# AAP Bridge
# Base: Universal Base Image (UBI) 9
FROM ubi9/ubi

# Labels (OCI standard)
LABEL org.opencontainers.image.title="aap-bridge" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.description="Production-grade migration tool for Ansible Automation Platform" \
      org.opencontainers.image.source="https://github.com/antonysallas/aap-bridge" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.vendor="Antony Sallas"

MAINTAINER Magnus Glantz <sudo@redhat.com>

# Prereqs
RUN dnf update -y && \
    dnf install python3 python3-pip wget unzip openssh-clients ncurses -y && \
    dnf remove vim-minimal -y && \
    pip3 install --upgrade "setuptools>=78.1.1" && \
    dnf clean all && \
    rm -rf /var/cache/dnf

RUN mkdir /app

# Working directory
WORKDIR /app

# Download variable, if you are building your own image, simply pass your own repository like such:
# podman build --build-arg AAP_BRIDGE_ZIP=https://github.com/myuser/aap-bridge-fork/archive/refs/heads/main.zip -t stuff .
ARG AAP_BRIDGE_ZIP
RUN echo "Downloading from: $AAP_BRIDGE_ZIP"
ENV AAP_BRIDGE_ZIP="${AAP_BRIDGE_ZIP:-https://github.com/arnav3000/aap-bridge-fork/archive/refs/heads/main.zip}" 

# Download and unzip of aap-bridge code
RUN wget -q "$AAP_BRIDGE_ZIP" -O /tmp/aap-bridge.zip && \
    unzip -q /tmp/aap-bridge.zip -d /app/ && \
    mv /app/$(ls /app | head -1) /app/aap-bridge && \
    rm -f /tmp/aap-bridge.zip

WORKDIR /app/aap-bridge

RUN mkdir -p /app/aap-bridge/{logs,exports,xformed,database}

# User setup
RUN useradd appuser && \
    chown appuser:appuser /app -R

USER appuser

RUN pip3 install --no-cache-dir "uv>=0.7.0"

# Single RUN — old seeded versions never persist as a separate layer
RUN ~/.local/bin/uv venv --seed --python 3.12 && \
    ~/.local/bin/uv pip install \
        "h11>=0.16.0" \
        "setuptools>=78.1.1" \
        "msgpack>=1.2.1" && \
    ~/.local/bin/uv sync --upgrade

# Create an alias for aap-bridge when someone enters a shell
RUN echo "alias aap-bridge=/app/aap-bridge/.venv/bin/aap-bridge" >> ~/.bashrc

# Show MOTD banner on container shell login
RUN echo 'python3 -c "import sys; sys.path.insert(0, \"/app/aap-bridge/src\"); from aap_migration.banner import get_container_motd; print(get_container_motd())" 2>/dev/null || true' >> ~/.bashrc

# Note: .env will be mounted at runtime - do not copy .env.example here
