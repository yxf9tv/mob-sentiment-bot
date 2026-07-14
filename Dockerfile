FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Install polyclob (trade execution) from local copy
RUN pip install --no-cache-dir -e . 2>/dev/null || true

CMD ["python", "main.py"]
