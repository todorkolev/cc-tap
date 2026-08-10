# cc-tap over streamable HTTP.
#
# Versioning is hatch-vcs, which derives the version from git metadata. A build
# without .git and its tags fails, so .git is copied into the builder stage.
# For a build from a source tarball (no .git), pass a version explicitly:
#     docker build --build-arg CC_TAP_VERSION=0.1.0 .

FROM python:3.12-slim AS builder

ARG CC_TAP_VERSION=""
ENV HATCH_VCS_PRETEND_VERSION=${CC_TAP_VERSION}

WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2.post1

# .git is required by hatch-vcs unless CC_TAP_VERSION is set (see .dockerignore,
# which deliberately does NOT exclude it).
COPY . .
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim

# Non-root, and matching the UID that owns ~/.claude on the host is what makes a
# read-only bind mount of the credentials directory readable. Override at build
# time to match your host user:  --build-arg APP_UID=1000
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "${APP_GID}" app 2>/dev/null || true \
    && useradd -m -u "${APP_UID}" -g "${APP_GID}" app 2>/dev/null || true

WORKDIR /app

# Pinned transitive dependencies, installed before the wheel so the layer caches.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm /tmp/*.whl

USER ${APP_UID}:${APP_GID}

ENV CC_TAP_TRANSPORT=http \
    CC_TAP_HOST=0.0.0.0 \
    CC_TAP_PORT=8787 \
    CC_TAP_TOKEN_STORE=/data/oauth-tokens.json \
    PYTHONUNBUFFERED=1

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["cc_tap"]
