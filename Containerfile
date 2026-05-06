# AAP Bridge
# Base: Universal Base Image (UBI) 9
FROM ubi9/ubi

# Labels (OCI standard)
LABEL org.opencontainers.image.title="aap-bridge" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.description="Production-grade migration tool for Ansible Automation Platform" \
      org.opencontainers.image.source="https://github.com/antonysallas/aap-bridge" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.vendor="Antony Sallas"

MAINTAINER Magnus Glantz <sudo@redhat.com>

# Prereqs
RUN dnf install python3 python3-pip wget unzip -y

RUN mkdir /app

# Working directory
WORKDIR /app

# Download variable, if you are building your own image, simply pass your own repository like such:
# podman build --build-arg AAP_BRIDGE_ZIP=https://github.com/myuser/aap-bridge-fork/archive/refs/heads/main.zip -t stuff .
ARG AAP_BRIDGE_ZIP
RUN echo "Downloading from: $AAP_BRIDGE_ZIP"
ENV AAP_BRIDGE_ZIP="${AAP_BRIDGE_ZIP:-https://github.com/arnav3000/aap-bridge-fork/archive/refs/heads/main.zip}" 

# Download and unzip of aap-bridge code
RUN wget $AAP_BRIDGE_ZIP
RUN unzip /app/"${AAP_BRIDGE_ZIP##*/}"

RUN mv /app/$(ls /app | head -1) /app/aap-bridge

WORKDIR /app/aap-bridge

RUN mkdir -p /app/aap-bridge/logs
RUN mkdir -p /app/aap-bridge/exports
RUN mkdir -p /app/aap-bridge/xformed
RUN mkdir -p /app/aap-bridge/database

# User setup
RUN useradd appuser

RUN chown appuser:appuser /app -R

USER appuser

RUN pip3 install uv

# Install AAP Bridge
RUN ~/.local/bin/uv venv --seed --python 3.12
RUN ~/.local/bin/uv sync

# Create an alias for aap-bridge when someone enters a shell
RUN echo "alias aap-bridge=/app/aap-bridge/.venv/bin/aap-bridge" >> ~/.bashrc

# Note: .env will be mounted at runtime - do not copy .env.example here
