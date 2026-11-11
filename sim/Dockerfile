# One image, two roles, no per-site rebuilds (D10): the Modbus TCP device simulator and the
# Modbus -> OTLP poller. They share sunspec.py so the register encoder and decoder cannot
# drift apart, and both are parameterised entirely by environment.
#
# arm64: python:3.12-slim publishes arm64; pymodbus and the OTel SDK are pure Python, and the
# OTLP/HTTP exporter pulls in requests, which is pure Python too. There is deliberately no
# grpcio in this image - see requirements.txt and plan.md D26. Verified by running it, not by
# assuming: `docker exec ... grep VmRSS /proc/1/status`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sunspec.py ./
COPY site/ ./site/
COPY poller/ ./poller/
COPY ocpp/ ./ocpp/
# The market feed is an *application*, not a device (M4, #22): it replays a recorded price file
# and emits semantic names directly, like the charge point does. Same image, fourth role.
COPY market/ ./market/
