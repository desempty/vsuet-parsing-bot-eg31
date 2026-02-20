# Используем официальный образ Python 3.11
FROM python:3.11-slim

# Обновляем pip и устанавливаем зависимости системы для Pillow и BeautifulSoup
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libwebp-dev \
    tcl8.6-dev tk8.6-dev \
    libharfbuzz-dev \
    libfribidi-dev \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы проекта
COPY . /app

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Задаём переменную окружения (можно переопределять при запуске контейнера)
ENV BOT_TOKEN=${BOT_TOKEN}

# Команда запуска бота
CMD ["python", "bot.py"]