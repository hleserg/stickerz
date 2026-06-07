FROM ghcr.io/astral-sh/uv:python3.13-bookworm

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

ARG USERNAME=vscode
RUN useradd --create-home --shell /bin/bash ${USERNAME}
USER ${USERNAME}
WORKDIR /workspace
