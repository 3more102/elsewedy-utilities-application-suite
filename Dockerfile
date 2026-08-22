FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Refresh the pinned Debian base to current security packages and replace two
# vulnerable Python packages inherited from the base image. Package indexes are
# removed in the same layer so they are not retained in the runtime image.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade \
        "setuptools>=78.1.1" \
        "msgpack>=1.2.1"

RUN groupadd --system euas \
    && useradd --system --gid euas --home /app euas

# Production installs only runtime dependencies. Test tooling remains in the
# developer/CI requirements file and is deliberately excluded from the image.
COPY requirements-runtime.txt .
RUN python -m pip install --no-cache-dir -r requirements-runtime.txt

# Copy only the files required by the running FastAPI application instead of the
# repository, CI metadata, tests and engineering documentation.
COPY --chown=euas:euas app ./app
COPY --chown=euas:euas static ./static
RUN mkdir -p /app/data /app/uploads \
    && chown -R euas:euas /app/data /app/uploads

USER euas
EXPOSE 8000

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]
