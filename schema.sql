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

-- Таблица подключений Telegram Business
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id VARCHAR(255) PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица подписок пользователей
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id BIGINT PRIMARY KEY,
    subscription_type VARCHAR(50) NOT NULL, -- 'trial', 'week', 'month', 'year', 'lifetime'
    start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    end_date TIMESTAMP NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    auto_renew BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица истории платежей
CREATE TABLE IF NOT EXISTS payment_history (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    subscription_type VARCHAR(50) NOT NULL,
    amount INTEGER NOT NULL, -- в звездах
    payment_id VARCHAR(255),
    status VARCHAR(50) DEFAULT 'pending', -- 'pending', 'completed', 'failed'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_business_connections_user ON business_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_active ON subscriptions(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_payment_history_user ON payment_history(user_id);

COMMENT ON TABLE users IS 'Зарегистрированные пользователи бота';
COMMENT ON TABLE failed_logins IS 'История неудачных попыток входа';
COMMENT ON TABLE banned_users IS 'Заблокированные пользователи';
COMMENT ON TABLE messages IS 'Временное хранилище сообщений (удаляются после отправки уведомления)';
COMMENT ON TABLE stats IS 'Статистика по каждому пользователю';
-- Таблица админов
CREATE TABLE IF NOT EXISTS admins (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    added_by BIGINT NOT NULL, -- ID админа который добавил
    is_super_admin BOOLEAN DEFAULT FALSE, -- только 825042510
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Добавляем главного админа
INSERT INTO admins (user_id, username, first_name, added_by, is_super_admin)
VALUES (825042510, 'admin', 'Super Admin', 825042510, TRUE)
ON CONFLICT (user_id) DO NOTHING;

-- Индекс
CREATE INDEX IF NOT EXISTS idx_admins_user ON admins(user_id);

-- Таблица реферальной системы
CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    referrer_id BIGINT NOT NULL, -- Кто пригласил
    referred_id BIGINT NOT NULL, -- Кого пригласили
    used BOOLEAN DEFAULT FALSE, -- Использована ли реферальная награда
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(referred_id) -- Один пользователь может быть приглашен только один раз
);

-- Индекс для реферальной системы
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals(referred_id);

COMMENT ON TABLE business_connections IS 'Подключения к Telegram Business API';
COMMENT ON TABLE subscriptions IS 'Подписки пользователей на бота';
COMMENT ON TABLE payment_history IS 'История платежей пользователей';
COMMENT ON TABLE admins IS 'Администраторы бота';
COMMENT ON TABLE referrals IS 'Реферальная система - приглашения пользователей';
