FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SITARR_DATA=/data
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY sitarr ./sitarr
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home sitarr \
    && mkdir /data && chown sitarr:sitarr /data
USER sitarr
EXPOSE 7171
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7171/health',timeout=3)"
CMD ["uvicorn", "sitarr.app:app", "--host", "0.0.0.0", "--port", "7171", "--no-access-log"]

