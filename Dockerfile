FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    EUAS_DB_PATH=/app/data/euas.db

WORKDIR /app

# Refresh the digest-pinned Debian base to the latest repository security
# packages available at build time, then discard package-manager indexes.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system euas \
    && useradd --system --gid euas --home /app euas

# Production installs only runtime dependencies. Test tooling remains in the
# developer/CI requirements file and is deliberately excluded from the image.
COPY requirements-runtime.txt .
RUN python -m pip install --no-cache-dir -r requirements-runtime.txt \
    # pip and setuptools are build/package-management tools, not EUAS runtime
    # dependencies. Removing them also removes pip's bundled stale CycloneDX
    # document, so scanners inspect the actual installed runtime packages.
    && rm -rf \
        /usr/local/lib/python3.13/site-packages/pip \
        /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.13/site-packages/setuptools \
        /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.13

# Copy only the files required by the running FastAPI application instead of the
# repository, CI metadata, tests and engineering documentation.
COPY --chown=euas:euas app ./app
COPY --chown=euas:euas static ./static
RUN mkdir -p /app/data /app/uploads \
    && chown -R euas:euas /app/data /app/uploads

USER euas
EXPOSE 8000

# Production runs through app.production so EUAS can enforce the deployment-only
# browser policy and resolve forwarded scheme using EUAS_TRUSTED_PROXY_CIDRS.
# Uvicorn proxy-header rewriting stays disabled so the application retains the
# raw socket peer required for spoof-resistant X-Forwarded-For validation.
CMD ["uvicorn","app.production:app","--host","0.0.0.0","--port","8000","--no-proxy-headers"]
