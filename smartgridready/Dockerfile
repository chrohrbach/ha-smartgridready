ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-time deps for cryptography wheels on armv7 if needed.
RUN apk add --no-cache \
        libffi \
        openssl \
        ca-certificates \
        tzdata \
    && rm -rf /var/cache/apk/*

WORKDIR /opt/smartgridready

COPY requirements.txt /opt/smartgridready/requirements.txt
RUN pip install --no-cache-dir -r /opt/smartgridready/requirements.txt

COPY src/ /opt/smartgridready/src/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
