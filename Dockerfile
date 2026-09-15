FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY bot.py bot_copy.py ai_responder.py schedule_image.py /app/
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
USER bot
VOLUME ["/data"]
CMD ["python", "bot.py"]
