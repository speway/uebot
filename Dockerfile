FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY bot.py /app/bot.py
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
USER bot
VOLUME ["/data"]
CMD ["python", "bot.py"]
