FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --system euas && useradd --system --gid euas --home /app euas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=euas:euas . .
RUN mkdir -p /app/data /app/uploads && chown -R euas:euas /app/data /app/uploads
USER euas
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]
