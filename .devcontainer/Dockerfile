FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv curl ca-certificates git build-essential \
    && rm -rf /var/lib/apt/lists/*

USER vscode
RUN curl -L https://foundry.paradigm.xyz | bash
ENV PATH="/home/vscode/.foundry/bin:${PATH}"
RUN foundryup -v 1.8.6

USER root
