FROM python:3.13-alpine3.21

ARG BRANCH=main
ARG REPO_URL=https://github.com/Clowrain/xiaoya_db.git

ENV TZ=Asia/Shanghai \
    CYCLE=86400 \
    BRANCH=${BRANCH} \
    REPO_URL=${REPO_URL} \
    RESTART_AUTO_UPDATE=false

RUN set -ex && \
    apk add --no-cache \
        bash \
        git \
        tini \
        tzdata && \
    apk add --no-cache --virtual=build-dependencies \
        build-base \
        gcc \
        g++ && \
    mkdir /app && \
    git config --global --add safe.directory /app && \
    git clone -b ${BRANCH} ${REPO_URL} /app

# Install and package the checked-out build context, including local changes.
COPY requirements.txt /app/requirements.txt

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    apk del --purge build-dependencies && \
    rm -rf \
        /root/.cache \
        /tmp/*

COPY solid.py emby_refresh.py sync_logging.py /app/

COPY ./pkg/docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ARG VCS_REF=local
ARG BUILD_DATE=unknown
ENV XIAOYA_BUILD_REVISION=${VCS_REF} \
    XIAOYA_BUILD_DATE=${BUILD_DATE} \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["tini", "-g", "--", "/entrypoint.sh"]

CMD ["--media", "/media"]
