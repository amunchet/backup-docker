FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 DOCKER=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
# Includes post-download-hook.sh, if you made one; rebuild after changing it.
COPY . /app/
CMD ["python", "main.py"]
