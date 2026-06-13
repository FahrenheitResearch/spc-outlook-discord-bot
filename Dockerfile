FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY spc_outlook_bot.py run_bot.sh ./

RUN chmod +x /app/run_bot.sh

VOLUME ["/app/data"]
CMD ["python", "/app/spc_outlook_bot.py"]
