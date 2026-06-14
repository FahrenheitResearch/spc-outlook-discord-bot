FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY spc_outlook_bot.py run_bot.sh ./
COPY assets ./assets

RUN chmod +x /app/run_bot.sh

VOLUME ["/app/data"]
CMD ["python", "/app/spc_outlook_bot.py"]
