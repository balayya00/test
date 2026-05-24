FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN playwright install chromium && \
    playwright install-deps chromium

EXPOSE 10000

CMD ["python", "server.py"]
