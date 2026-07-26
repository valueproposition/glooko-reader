FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    requests \
    git+https://github.com/spamsch/glooko-reader.git

COPY sync.py .

CMD ["python", "sync.py"]
