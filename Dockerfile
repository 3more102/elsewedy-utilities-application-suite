FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Refresh the pinned Debian base to current security packages and remove two
# vulnerable Python packages inherited from the base image before application
# dependencies are installed. Keep package-manager indexes out of the layer.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade \
        "setuptools>=78.1.1" \
        "msgpack>=1.2.1"

RUN groupadd --system euas \
    && useradd --system --gid euas --home /app euas

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=euas:euas . .
RUN mkdir -p /app/data /app/uploads \
    && chown -R euas:euas /app/data /app/uploads

USER euas
EXPOSE 8000

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]
