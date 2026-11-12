FROM python:3.12.14-slim-bookworm

ARG INSTALL_FFMPEG=1
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=https://deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements/base.txt ./requirements/base.txt
RUN pip install --index-url "$PYPI_INDEX_URL" --no-cache-dir -r requirements/base.txt

COPY requirements/media.txt ./requirements/media.txt
RUN if [ "$INSTALL_FFMPEG" = "1" ]; then \
      pip install --index-url "$PYPI_INDEX_URL" --no-cache-dir -r requirements/media.txt; fi

# Share runtime layers; media workers do not need Whisper.
ARG INSTALL_AI_DEPS=0
ARG INSTALL_EMBEDDING_DEPS=0
COPY requirements/ai.txt requirements/embedding.txt ./requirements/
RUN if [ "$INSTALL_AI_DEPS" = "1" ]; then pip install --index-url "$PYPI_INDEX_URL" --no-cache-dir -r requirements/ai.txt; fi \
    && if [ "$INSTALL_EMBEDDING_DEPS" = "1" ]; then pip install --index-url "$PYPI_INDEX_URL" --no-cache-dir -r requirements/embedding.txt; fi

ARG INSTALL_SYSTEM_FFMPEG=0
RUN if [ "$INSTALL_SYSTEM_FFMPEG" = "1" ]; then \
      sed -i \
        -e "s#http://deb.debian.org/debian-security#${DEBIAN_MIRROR}/debian-security#g" \
        -e "s#http://deb.debian.org/debian#${DEBIAN_MIRROR}/debian#g" \
        /etc/apt/sources.list.d/debian.sources \
      && apt-get -o Acquire::Retries=3 update \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg intel-media-va-driver \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY iptv_pilot ./iptv_pilot
COPY scripts ./scripts

CMD ["python", "-m", "iptv_pilot.web.main"]
