FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    dnsutils \
    file \
    ffmpeg \
    git \
    imagemagick \
    iputils-ping \
    jq \
    make \
    netcat-openbsd \
    nodejs \
    npm \
    openssh-client \
    openssl \
    pkg-config \
    procps \
    tini \
    traceroute \
    unzip \
    wget \
    whois \
    xz-utils \
    zip \
    && rm -rf /var/lib/apt/lists/*

# Keep browser and font payloads in separate layers for faster rebuilds.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei \
    fonts-noto-color-emoji \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache/fontconfig

RUN pip install --no-cache-dir uv

RUN npm install --prefix /app --no-audit --no-fund \
    puppeteer-core@24 \
    puppeteer-extra@3 \
    puppeteer-extra-plugin-stealth@2

COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --system -r /tmp/requirements.txt \
    && rm -rf /root/.cache/uv

RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox \
    && mkdir -p /app /data/sessions /tmp/sandbox-cache \
    && chown -R sandbox:sandbox \
        /app /data/sessions /tmp/sandbox-cache /home/sandbox

COPY --chown=sandbox:sandbox app /app/app
COPY --chown=sandbox:sandbox tools /app/tools
USER sandbox
WORKDIR /app

EXPOSE 7860

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
