FROM python:3.13-slim

WORKDIR /app

# smbus2 talks to the CP2112 I2C adapter; paho-mqtt publishes to the broker
RUN pip3 install --no-cache-dir paho-mqtt smbus2

COPY fsp2mqtt.py /app/fsp2mqtt.py

CMD ["python3", "-u", "fsp2mqtt.py"]
