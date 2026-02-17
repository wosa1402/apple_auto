FROM python:3.11-slim

# Install Chrome dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 \
    libnss3 libgconf-2-4 libfontconfig1 libx11-6 libx11-xcb1 \
    libxcb1 libxcomposite1 libxcursor1 libxdamage1 libxext6 \
    libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
    libpango-1.0-0 libpangocairo-1.0-0 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libasound2 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome stable
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && dpkg -i /tmp/chrome.deb || apt-get -f install -y \
    && rm /tmp/chrome.deb

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV CHROME_BINARY=/usr/bin/google-chrome
ENV HEADLESS=true
ENV HOST=0.0.0.0
ENV PORT=5000

EXPOSE 5000

VOLUME ["/app/data"]

CMD ["python", "-u", "app.py"]
