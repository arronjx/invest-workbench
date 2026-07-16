FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cli.py .
COPY src/ src/
COPY panels/ panels/
COPY prompts/ prompts/

RUN mkdir -p data reports

EXPOSE 8787

# Railway 注入 PORT；本地默认 8787
CMD ["sh", "-c", "python cli.py serve --host 0.0.0.0 --port ${PORT:-8787}"]
