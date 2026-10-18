FROM python:3.12-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY everlights.py scheduler.py server.py ./
COPY static ./static
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/schedules',timeout=4)"
CMD ["python", "server.py"]
