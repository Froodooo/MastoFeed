FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY *.py ./
COPY targets/ targets/

RUN mkdir /data
VOLUME /data

CMD ["python", "main.py"]
