
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    flex \
    bison \
    libreadline-dev \
    zlib1g-dev \
    pkg-config \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    libedit-dev \
    libicu-dev \
    git \
    curl \
    ca-certificates \
    gdb \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Backend of the recovery visualizer (recovery_app/)
RUN pip3 install --no-cache-dir "Django>=5.2,<5.3"

ENV CC=gcc
ENV CXX=g++

# Non-root user: initdb and postgres refuse to run as root
RUN useradd -m -s /bin/bash dev
USER dev
