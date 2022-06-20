FROM python:3.9-slim
LABEL author="Lennart Weller <lennart.weller@hansemerkur.de"

WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt

ENV PYTHONPATH=$PYTHONPATH:/app

CMD ["kopf","run", "-A", "-v", "--liveness=http://0.0.0.0:8080/healthz", "/app/rgwoperator/s3users.py", "/app/rgwoperator/s3accesskeys.py", "/app/rgwoperator/s3buckets.py"]
