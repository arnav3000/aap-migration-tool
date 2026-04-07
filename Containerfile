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

# Add app-bridge code
WORKDIR /app

RUN wget https://github.com/arnav3000/aap-bridge-fork/archive/refs/heads/main.zip

RUN unzip /app/main.zip

# User setup
RUN useradd appuser

RUN chown appuser:appuser /app -R

USER appuser

RUN pip3 install uv

WORKDIR /app/aap-bridge-fork-main

# Install AAP Bridge
RUN ~/.local/bin/uv venv --seed --python 3.12
RUN ~/.local/bin/uv sync

# Configuration file
RUN cp /app/aap-bridge-fork-main/.env.example /app/aap-bridge-fork-main/.env
