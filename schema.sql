-- PostgreSQL Database Schema for MessageGuardian Multi-User Bot
-- Database: Secret_message
-- User: postgres
-- Password: 1
-- Port: 5432

-- Таблица пользователей бота
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    is_authenticated BOOLEAN DEFAULT FALSE,
    is_banned BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP
);

-- Таблица неудачных попыток входа
CREATE TABLE IF NOT EXISTS failed_logins (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255),
    attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    attempts_count INTEGER DEFAULT 1
);

-- Таблица заблокированных пользователей
CREATE TABLE IF NOT EXISTS banned_users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    reason VARCHAR(255) DEFAULT 'Too many failed login attempts',
    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица сообщений (временное хранилище до отправки уведомления)
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    user_id BIGINT,
    text TEXT,
    media_type VARCHAR(50),
    file_path TEXT,
    caption TEXT,
    links TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_id, chat_id, message_id)
);

-- Таблица статистики
CREATE TABLE IF NOT EXISTS stats (
    owner_id BIGINT PRIMARY KEY,
    total_messages INTEGER DEFAULT 0,
    total_edits INTEGER DEFAULT 0,
    total_deletes INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_messages_owner_chat ON messages(owner_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_lookup ON messages(owner_id, chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_failed_logins_user ON failed_logins(user_id);
CREATE INDEX IF NOT EXISTS idx_users_auth ON users(user_id, is_authenticated);

-- Функция для автоматической очистки старых неудачных попыток (старше 24 часов)
CREATE OR REPLACE FUNCTION cleanup_old_failed_logins()
RETURNS void AS $$
BEGIN
    DELETE FROM failed_logins WHERE attempt_time < NOW() - INTERVAL '24 hours';
END;
$$ LANGUAGE plpgsql;

COMMENT ON TABLE users IS 'Зарегистрированные пользователи бота';
COMMENT ON TABLE failed_logins IS 'История неудачных попыток входа';
COMMENT ON TABLE banned_users IS 'Заблокированные пользователи';
COMMENT ON TABLE messages IS 'Временное хранилище сообщений (удаляются после отправки уведомления)';
COMMENT ON TABLE stats IS 'Статистика по каждому пользователю';
