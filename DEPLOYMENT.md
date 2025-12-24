# 🚀 Deployment Guide - MessageGuardian Bot

Инструкция по развёртыванию бота на production сервере.

## 📋 Информация о сервере

- **IP:** 148.253.213.55
- **User:** root
- **OS:** Linux (предположительно Ubuntu/Debian)

## ⚠️ ВАЖНО: На сервере уже есть другой проект!

Будем работать аккуратно, создадим отдельную директорию для нового проекта.

---

## 🔧 Шаг 1: Подключение к серверу

```bash
ssh root@148.253.213.55
```

## 📂 Шаг 2: Проверка существующих проектов

```bash
ls -la /root
ls -la /opt
ls -la /var/www
```

## 📁 Шаг 3: Создание директории для проекта

```bash
mkdir -p /opt/messageguardian
cd /opt/messageguardian
```

## 🐍 Шаг 4: Установка Python и зависимостей

```bash
# Обновить систему
apt update && apt upgrade -y

# Установить Python 3.10+ если нужно
apt install python3 python3-pip python3-venv -y

# Проверить версию
python3 --version
```

## 🗄️ Шаг 5: Установка PostgreSQL

```bash
# Установить PostgreSQL
apt install postgresql postgresql-contrib -y

# Запустить PostgreSQL
systemctl start postgresql
systemctl enable postgresql

# Проверить статус
systemctl status postgresql
```

## 🔐 Шаг 6: Создание БД и пользователя

```bash
sudo -u postgres psql
```

В PostgreSQL консоли:

```sql
CREATE DATABASE Secret_message;
CREATE USER botuser WITH PASSWORD 'SecurePassword123!';
GRANT ALL PRIVILEGES ON DATABASE Secret_message TO botuser;
\q
```

## 📥 Шаг 7: Клонирование проекта

```bash
cd /opt/messageguardian
git clone https://github.com/denly1/Secret_message1.git .
```

## 🔧 Шаг 8: Настройка виртуального окружения

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## ⚙️ Шаг 9: Создание .env файла

```bash
nano .env
```

Содержимое:

```env
TELEGRAM_BOT_TOKEN=8578409666:AAF32MDqhOvA_656X6XelYURy5Ok-K3RCLG_Q
BOT_PASSWORD=12391
ADMIN_ID=825042510

# PostgreSQL Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=Secret_message
DB_USER=botuser
DB_PASSWORD=SecurePassword123!
```

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`

## 🧪 Шаг 10: Тестовый запуск

```bash
python3 bot.py
```

Проверить что бот запустился без ошибок. Остановить: `Ctrl+C`

## 🔄 Шаг 11: Создание systemd сервиса

```bash
nano /etc/systemd/system/messageguardian.service
```

Содержимое:

```ini
[Unit]
Description=MessageGuardian Telegram Bot
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/messageguardian
Environment="PATH=/opt/messageguardian/venv/bin"
ExecStart=/opt/messageguardian/venv/bin/python3 /opt/messageguardian/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`

## ▶️ Шаг 12: Запуск сервиса

```bash
# Перезагрузить systemd
systemctl daemon-reload

# Запустить бота
systemctl start messageguardian

# Включить автозапуск
systemctl enable messageguardian

# Проверить статус
systemctl status messageguardian

# Посмотреть логи
journalctl -u messageguardian -f
```

## 📊 Управление сервисом

```bash
# Остановить
systemctl stop messageguardian

# Перезапустить
systemctl restart messageguardian

# Посмотреть логи
journalctl -u messageguardian -n 100 --no-pager

# Следить за логами в реальном времени
journalctl -u messageguardian -f
```

## 🔒 Безопасность

### 1. Настроить файрвол

```bash
# Установить UFW если нужно
apt install ufw -y

# Разрешить SSH
ufw allow 22/tcp

# Включить файрвол
ufw enable

# Проверить статус
ufw status
```

### 2. Защитить .env файл

```bash
chmod 600 /opt/messageguardian/.env
```

### 3. Создать отдельного пользователя (опционально)

```bash
# Создать пользователя
useradd -m -s /bin/bash botuser

# Передать права
chown -R botuser:botuser /opt/messageguardian

# Обновить сервис (User=botuser)
```

## 🔄 Обновление бота

```bash
cd /opt/messageguardian
git pull
systemctl restart messageguardian
```

## 📝 Резервное копирование БД

```bash
# Создать бэкап
sudo -u postgres pg_dump Secret_message > /opt/backups/messageguardian_$(date +%Y%m%d).sql

# Восстановить из бэкапа
sudo -u postgres psql Secret_message < /opt/backups/messageguardian_20241224.sql
```

## 🐛 Troubleshooting

### Бот не запускается

```bash
# Проверить логи
journalctl -u messageguardian -n 50

# Проверить права
ls -la /opt/messageguardian

# Проверить .env
cat /opt/messageguardian/.env

# Проверить PostgreSQL
systemctl status postgresql
sudo -u postgres psql -c "\l"
```

### PostgreSQL ошибки

```bash
# Перезапустить PostgreSQL
systemctl restart postgresql

# Проверить подключение
sudo -u postgres psql -d Secret_message -c "SELECT 1;"
```

### Проблемы с правами

```bash
# Дать права на директорию
chown -R root:root /opt/messageguardian
chmod -R 755 /opt/messageguardian
chmod 600 /opt/messageguardian/.env
```

## ✅ Проверка работы

1. Проверить статус: `systemctl status messageguardian`
2. Проверить логи: `journalctl -u messageguardian -f`
3. Написать боту `/start` в Telegram
4. Ввести пароль `12391`
5. Отправить тестовое сообщение и удалить его

---

**Готово! Бот работает на production сервере!** 🎉
