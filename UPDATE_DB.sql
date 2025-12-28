-- SQL скрипт для обновления БД на сервере
-- Применить после git pull на сервере

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

-- Таблица админов
CREATE TABLE IF NOT EXISTS admins (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    added_by BIGINT NOT NULL,
    is_super_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Добавляем главного админа
INSERT INTO admins (user_id, username, first_name, added_by, is_super_admin)
VALUES (825042510, 'admin', 'Super Admin', 825042510, TRUE)
ON CONFLICT (user_id) DO NOTHING;

-- Индекс для админов
CREATE INDEX IF NOT EXISTS idx_admins_user ON admins(user_id);

-- Комментарии
COMMENT ON TABLE business_connections IS 'Подключения к Telegram Business API';
COMMENT ON TABLE subscriptions IS 'Подписки пользователей на бота';
COMMENT ON TABLE payment_history IS 'История платежей пользователей';
COMMENT ON TABLE admins IS 'Администраторы бота';
