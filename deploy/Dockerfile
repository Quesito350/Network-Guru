# Network Guru app image.
# Built ON THE SITE SERVER (install.sh / ng_updater.py) from an unpacked release folder, with
# the copy of this file in /opt/networkguru - never a release zip's own. Works offline:
#   - the base image is loaded from a tar made by tools/fetch_images.py (docker load)
#   - the Python packages come from deploy/wheels (tools/fetch_wheels.py), checked against
#     the sha256 hashes in deploy/requirements.lock
# No "# syntax=" line and no RUN --mount on purpose: both make the builder go online.
# deploy/ng_updater.py has the same steps as a builder-free fallback (FALLBACK_SCRIPT) - keep
# them in step.
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore \
    NETWORKGURU_DATA=/data NETWORKGURU_DEPLOY=docker

COPY deploy/wheels /tmp/wheels
COPY deploy/requirements.lock /tmp/requirements.lock
RUN pip install --no-index --find-links /tmp/wheels --require-hashes -r /tmp/requirements.lock \
 && rm -rf /tmp/wheels /tmp/requirements.lock \
 && groupadd --gid 10001 networkguru \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin networkguru \
 && mkdir -p /data/updates /app \
 && chown -R 10001:10001 /data

COPY networkguru /app/networkguru
COPY VERSION.txt /app/VERSION.txt
WORKDIR /app
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "networkguru", "serve", "--inventory", "/site/inventory.yaml"]
