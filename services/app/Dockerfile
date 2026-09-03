FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/code
WORKDIR /code
COPY services/app/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt
COPY . /code
RUN useradd -m -u 1000 app && chown -R app:app /code
USER app
EXPOSE 8000
CMD ["uvicorn", "services.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
