FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY main.py .

RUN mkdir /data
VOLUME /data

CMD ["python", "main.py"]
