import os
import asyncio
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BusinessMessagesDeleted, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode
import asyncpg

load_dotenv()

MEDIA_DIR = Path("saved_media")
MEDIA_DIR.mkdir(exist_ok=True)

BOT_PASSWORD = os.getenv("BOT_PASSWORD", "12391")
ADMIN_ID = int(os.getenv("ADMIN_ID", "825042510"))
SUPER_ADMIN_ID = 825042510  # Главный админ

# PostgreSQL connection
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "Secret_message")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1")

# Global database pool
db_pool = None


async def init_db():
    """Initialize database connection pool"""
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=5,
        max_size=20
    )
    print("✅ PostgreSQL connection pool created")
    
    # Create business_connections table if not exists
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id VARCHAR(255) PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    print("✅ Business connections table ready")


async def close_db():
    """Close database connection pool"""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("✅ PostgreSQL connection pool closed")


# ==================== SUBSCRIPTION FUNCTIONS ====================

async def create_trial_subscription(user_id: int) -> None:
    """Create 7-day trial subscription for new user"""
    async with db_pool.acquire() as conn:
        end_date = datetime.now() + timedelta(days=7)
        await conn.execute(
            """
            INSERT INTO subscriptions (user_id, subscription_type, start_date, end_date, is_active)
            VALUES ($1, 'trial', NOW(), $2, TRUE)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, end_date
        )


async def check_subscription(user_id: int) -> dict:
    """Check if user has active subscription"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT subscription_type, end_date, is_active
            FROM subscriptions
            WHERE user_id = $1
            """,
            user_id
        )
        
        if not row:
            return {"active": False, "type": None, "days_left": 0}
        
        if not row['is_active']:
            return {"active": False, "type": row['subscription_type'], "days_left": 0}
        
        days_left = (row['end_date'] - datetime.now()).days
        
        if days_left < 0:
            # Subscription expired
            await conn.execute(
                "UPDATE subscriptions SET is_active = FALSE WHERE user_id = $1",
                user_id
            )
            return {"active": False, "type": row['subscription_type'], "days_left": 0}
        
        return {
            "active": True,
            "type": row['subscription_type'],
            "days_left": days_left,
            "end_date": row['end_date']
        }


async def grant_subscription(user_id: int, sub_type: str, days: int) -> None:
    """Grant subscription to user (admin function)"""
    async with db_pool.acquire() as conn:
        end_date = datetime.now() + timedelta(days=days)
        await conn.execute(
            """
            INSERT INTO subscriptions (user_id, subscription_type, start_date, end_date, is_active)
            VALUES ($1, $2, NOW(), $3, TRUE)
            ON CONFLICT (user_id) DO UPDATE
            SET subscription_type = $2, start_date = NOW(), end_date = $3, is_active = TRUE, updated_at = NOW()
            """,
            user_id, sub_type, end_date
        )


async def revoke_subscription(user_id: int) -> None:
    """Revoke user subscription (admin function)"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE subscriptions SET is_active = FALSE, updated_at = NOW() WHERE user_id = $1",
            user_id
        )


async def extend_subscription(user_id: int, sub_type: str, days: int) -> None:
    """Extend or create subscription after payment"""
    async with db_pool.acquire() as conn:
        # Check if user has active subscription
        row = await conn.fetchrow(
            "SELECT end_date, is_active FROM subscriptions WHERE user_id = $1",
            user_id
        )
        
        if row and row['is_active']:
            # Extend existing subscription
            new_end_date = row['end_date'] + timedelta(days=days)
        else:
            # Create new subscription
            new_end_date = datetime.now() + timedelta(days=days)
        
        await conn.execute(
            """
            INSERT INTO subscriptions (user_id, subscription_type, start_date, end_date, is_active)
            VALUES ($1, $2, NOW(), $3, TRUE)
            ON CONFLICT (user_id) DO UPDATE
            SET subscription_type = $2, end_date = $3, is_active = TRUE, updated_at = NOW()
            """,
            user_id, sub_type, new_end_date
        )


async def save_payment(user_id: int, sub_type: str, amount: int, payment_id: str, status: str = 'completed') -> None:
    """Save payment to history"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payment_history (user_id, subscription_type, amount, payment_id, status)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id, sub_type, amount, payment_id, status
        )


async def get_all_users() -> list:
    """Get all authenticated users for broadcast"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, first_name FROM users WHERE is_authenticated = TRUE"
        )
        return [dict(row) for row in rows]


# ==================== ADMIN FUNCTIONS ====================

async def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM admins WHERE user_id = $1)",
            user_id
        )
        return result or False


async def is_super_admin(user_id: int) -> bool:
    """Check if user is super admin"""
    return user_id == SUPER_ADMIN_ID


async def add_admin(user_id: int, username: str, first_name: str, added_by: int) -> None:
    """Add new admin"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admins (user_id, username, first_name, added_by, is_super_admin)
            VALUES ($1, $2, $3, $4, FALSE)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, username, first_name, added_by
        )


async def remove_admin(user_id: int) -> None:
    """Remove admin (except super admin)"""
    if user_id == SUPER_ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM admins WHERE user_id = $1 AND is_super_admin = FALSE",
            user_id
        )


async def get_all_admins() -> list:
    """Get all admins"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, first_name, is_super_admin, created_at FROM admins ORDER BY created_at"
        )
        return [dict(row) for row in rows]


async def get_revenue_stats() -> dict:
    """Get revenue statistics"""
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM payment_history WHERE status = 'completed'"
        ) or 0
        
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM payment_history WHERE status = 'completed'"
        ) or 0
        
        return {"total_stars": total, "total_payments": count}


# ==================== END ADMIN FUNCTIONS ====================


async def save_message(owner_id: int, chat_id: int, message_id: int, user_id: int | None, text: str | None,
                 media_type: str | None = None, file_path: str | None = None,
                 caption: str | None = None, links: str | None = None) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (owner_id, chat_id, message_id, user_id, text, media_type, file_path, caption, links)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (owner_id, chat_id, message_id) DO UPDATE
            SET text = $5, media_type = $6, file_path = $7, caption = $8, links = $9
            """,
            owner_id, chat_id, message_id, user_id, text or "", media_type, file_path, caption, links
        )


async def get_message_full(owner_id: int, chat_id: int, message_id: int) -> Optional[dict]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, text, media_type, file_path, caption, links FROM messages WHERE owner_id = $1 AND chat_id = $2 AND message_id = $3",
            owner_id, chat_id, message_id
        )
        if row:
            return dict(row)
        return None


async def delete_message_from_db(owner_id: int, chat_id: int, message_id: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE owner_id = $1 AND chat_id = $2 AND message_id = $3",
            owner_id, chat_id, message_id
        )


async def increment_stat(owner_id: int, stat_type: str) -> None:
    async with db_pool.acquire() as conn:
        if stat_type == "total_messages":
            await conn.execute(
                """
                INSERT INTO stats (owner_id, total_messages, updated_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (owner_id) DO UPDATE
                SET total_messages = stats.total_messages + 1, updated_at = NOW()
                """,
                owner_id
            )
        elif stat_type == "total_edits":
            await conn.execute(
                """
                INSERT INTO stats (owner_id, total_edits, updated_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (owner_id) DO UPDATE
                SET total_edits = stats.total_edits + 1, updated_at = NOW()
                """,
                owner_id
            )
        elif stat_type == "total_deletes":
            await conn.execute(
                """
                INSERT INTO stats (owner_id, total_deletes, updated_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (owner_id) DO UPDATE
                SET total_deletes = stats.total_deletes + 1, updated_at = NOW()
                """,
                owner_id
            )


async def get_stats(owner_id: int) -> dict:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_messages, total_edits, total_deletes FROM stats WHERE owner_id = $1",
            owner_id
        )
        if row:
            return {
                "messages": row["total_messages"],
                "edits": row["total_edits"],
                "deletes": row["total_deletes"]
            }
        return {"messages": 0, "edits": 0, "deletes": 0}


async def is_user_authenticated(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_authenticated FROM users WHERE user_id = $1 AND is_banned = FALSE",
            user_id
        )
        return result is True


async def is_user_banned(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_banned FROM users WHERE user_id = $1",
            user_id
        )
        return result is True


async def authenticate_user(user_id: int, username: str, first_name: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, is_authenticated, last_login)
            VALUES ($1, $2, $3, TRUE, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET is_authenticated = TRUE, last_login = NOW(), username = $2, first_name = $3
            """,
            user_id, username, first_name
        )


async def record_failed_login(user_id: int, username: str, first_name: str) -> int:
    async with db_pool.acquire() as conn:
        attempts = await conn.fetchval(
            "SELECT attempts_count FROM failed_logins WHERE user_id = $1 ORDER BY attempt_time DESC LIMIT 1",
            user_id
        )
        
        if attempts is None:
            attempts = 0
        
        new_attempts = attempts + 1
        
        await conn.execute(
            "INSERT INTO failed_logins (user_id, username, first_name, attempts_count) VALUES ($1, $2, $3, $4)",
            user_id, username, first_name, new_attempts
        )
        
        return new_attempts


async def ban_user(user_id: int, username: str, first_name: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO banned_users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, username, first_name
        )
        
        await conn.execute(
            "UPDATE users SET is_banned = TRUE WHERE user_id = $1",
            user_id
        )


async def get_banned_users() -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, first_name, reason, banned_at FROM banned_users ORDER BY banned_at DESC"
        )
        return [dict(row) for row in rows]


async def get_failed_logins() -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, username, first_name, MAX(attempts_count) as attempts, MAX(attempt_time) as last_attempt
            FROM failed_logins
            GROUP BY user_id, username, first_name
            ORDER BY last_attempt DESC
            LIMIT 50
            """
        )
        return [dict(row) for row in rows]


async def save_business_connection(connection_id: str, user_id: int, username: str, first_name: str) -> None:
    """Save business connection mapping"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO business_connections (connection_id, user_id, username, first_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (connection_id) DO UPDATE
            SET user_id = $2, username = $3, first_name = $4, connected_at = NOW()
            """,
            connection_id, user_id, username, first_name
        )


async def get_user_by_connection(connection_id: str) -> Optional[int]:
    """Get user_id by business_connection_id"""
    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval(
            "SELECT user_id FROM business_connections WHERE connection_id = $1",
            connection_id
        )
        return user_id


def to_fancy(text: str) -> str:
    fancy_map = {
        'A': '𝓐', 'B': '𝓑', 'C': '𝓒', 'D': '𝓓', 'E': '𝓔', 'F': '𝓕', 'G': '𝓖', 'H': '𝓗', 'I': '𝓘', 'J': '𝓙',
        'K': '𝓚', 'L': '𝓛', 'M': '𝓜', 'N': '𝓝', 'O': '𝓞', 'P': '𝓟', 'Q': '𝓠', 'R': '𝓡', 'S': '𝓢', 'T': '𝓣',
        'U': '𝓤', 'V': '𝓥', 'W': '𝓦', 'X': '𝓧', 'Y': '𝓨', 'Z': '𝓩',
        'a': '𝓪', 'b': '𝓫', 'c': '𝓬', 'd': '𝓭', 'e': '𝓮', 'f': '𝓯', 'g': '𝓰', 'h': '𝓱', 'i': '𝓲', 'j': '𝓳',
        'k': '𝓴', 'l': '𝓵', 'm': '𝓶', 'n': '𝓷', 'o': '𝓸', 'p': '𝓹', 'q': '𝓺', 'r': '𝓻', 's': '𝓼', 't': '𝓽',
        'u': '𝓾', 'v': '𝓿', 'w': '𝔀', 'x': '𝔁', 'y': '𝔂', 'z': '𝔃'
    }
    return ''.join(fancy_map.get(c, c) for c in text)


async def create_chat_html_backup(owner_id: int, chat_id: int, chat_name: str) -> str:
    """Create HTML backup of entire chat history"""
    async with db_pool.acquire() as conn:
        messages = await conn.fetch(
            """
            SELECT message_id, user_id, text, caption, media_type, file_path, created_at
            FROM messages
            WHERE owner_id = $1 AND chat_id = $2
            ORDER BY created_at ASC
            """,
            owner_id, chat_id
        )
    
    if not messages:
        return None
    
    html_content = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Резервная копия чата - {chat_name}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #0f1419 0%, #1a1f2e 100%);
            color: #ffffff;
            min-height: 100vh;
            padding: 0;
        }}
        .chat-container {{
            max-width: 680px;
            margin: 0 auto;
            background: #0d1117;
            min-height: 100vh;
            box-shadow: 0 0 40px rgba(0,0,0,0.5);
        }}
        .chat-header {{
            background: linear-gradient(90deg, #1e2936 0%, #2d3748 100%);
            padding: 18px 20px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            display: flex;
            align-items: center;
            gap: 15px;
            position: sticky;
            top: 0;
            z-index: 100;
            backdrop-filter: blur(10px);
        }}
        .chat-avatar {{
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            font-weight: 600;
            color: white;
            flex-shrink: 0;
        }}
        .chat-info {{
            flex: 1;
        }}
        .chat-name {{
            font-size: 16px;
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 2px;
        }}
        .chat-status {{
            font-size: 13px;
            color: #8b949e;
        }}
        .messages-container {{
            padding: 20px 15px;
            background: #0d1117;
        }}
        .message-wrapper {{
            display: flex;
            margin-bottom: 12px;
            align-items: flex-end;
            gap: 8px;
        }}
        .message-wrapper.outgoing {{
            flex-direction: row-reverse;
        }}
        .message-avatar {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            font-weight: 600;
            color: white;
            flex-shrink: 0;
        }}
        .message-wrapper.outgoing .message-avatar {{
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }}
        .message-bubble {{
            max-width: 65%;
            padding: 10px 14px;
            border-radius: 18px;
            position: relative;
            word-wrap: break-word;
            box-shadow: 0 1px 2px rgba(0,0,0,0.3);
        }}
        .message-wrapper.incoming .message-bubble {{
            background: linear-gradient(135deg, #2d3748 0%, #1e2936 100%);
            border-bottom-left-radius: 4px;
        }}
        .message-wrapper.outgoing .message-bubble {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-bottom-right-radius: 4px;
        }}
        .message-sender {{
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 4px;
            opacity: 0.9;
        }}
        .message-wrapper.incoming .message-sender {{
            color: #58a6ff;
        }}
        .message-wrapper.outgoing .message-sender {{
            color: #ffffff;
        }}
        .message-text {{
            font-size: 15px;
            line-height: 1.4;
            color: #ffffff;
            margin-bottom: 4px;
        }}
        .message-media {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 10px;
            background: rgba(255,255,255,0.1);
            border-radius: 12px;
            font-size: 13px;
            margin-top: 6px;
            color: #58a6ff;
        }}
        .message-time {{
            font-size: 11px;
            color: rgba(255,255,255,0.5);
            text-align: right;
            margin-top: 2px;
        }}
        .date-divider {{
            text-align: center;
            margin: 20px 0;
            position: relative;
        }}
        .date-divider span {{
            background: rgba(255,255,255,0.1);
            padding: 6px 16px;
            border-radius: 12px;
            font-size: 13px;
            color: #8b949e;
            display: inline-block;
        }}
        .chat-footer {{
            background: linear-gradient(90deg, #1e2936 0%, #2d3748 100%);
            padding: 15px 20px;
            border-top: 1px solid rgba(255,255,255,0.1);
            text-align: center;
            color: #8b949e;
            font-size: 13px;
        }}
        .stats-badge {{
            display: inline-block;
            background: rgba(102, 126, 234, 0.2);
            color: #667eea;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            margin-top: 8px;
        }}
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="chat-header">
            <div class="chat-avatar">{chat_name[0].upper()}</div>
            <div class="chat-info">
                <div class="chat-name">{chat_name}</div>
                <div class="chat-status">Резервная копия • {__import__('datetime').datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
            </div>
        </div>
        <div class="messages-container">
"""
    
    last_date = None
    for msg in messages:
        is_owner = msg['user_id'] == owner_id
        sender_name = "Вы" if is_owner else chat_name
        wrapper_class = "message-wrapper outgoing" if is_owner else "message-wrapper incoming"
        text = msg['text'] or msg['caption'] or ""
        media_content = ""
        
        # Date divider
        msg_date = msg['created_at'].strftime('%d.%m.%Y')
        if msg_date != last_date:
            html_content += f'<div class="date-divider"><span>{msg_date}</span></div>\n'
            last_date = msg_date
        
        # Handle media with actual files
        if msg['media_type'] and msg['file_path']:
            file_path = Path(msg['file_path'])
            if file_path.exists():
                if msg['media_type'] in ('photo', 'photo_reply'):
                    # Embed image as base64
                    import base64
                    try:
                        with open(file_path, 'rb') as img_file:
                            img_data = base64.b64encode(img_file.read()).decode('utf-8')
                            media_content = f'<img src="data:image/jpeg;base64,{img_data}" style="max-width: 100%; border-radius: 12px; margin-bottom: 8px;" />'
                    except:
                        media_content = '<div class="message-media">📷 Фото</div>'
                elif msg['media_type'] in ('video', 'video_reply'):
                    media_content = '<div class="message-media">🎥 Видео</div>'
                elif msg['media_type'] == 'sticker':
                    media_content = '<div class="message-media">🎭 Стикер</div>'
                elif msg['media_type'] == 'voice':
                    media_content = '<div class="message-media">🎤 Голосовое сообщение</div>'
                elif msg['media_type'] == 'video_note':
                    media_content = '<div class="message-media">🎬 Видеосообщение</div>'
                elif msg['media_type'] == 'animation':
                    media_content = '<div class="message-media">🎬 GIF</div>'
                elif msg['media_type'] == 'document':
                    media_content = '<div class="message-media">📄 Документ</div>'
            else:
                # File doesn't exist, show placeholder
                media_types = {
                    'photo': '📷 Фото', 'photo_reply': '📷 Фото',
                    'video': '🎥 Видео', 'video_reply': '🎥 Видео',
                    'document': '📄 Документ', 'sticker': '🎭 Стикер',
                    'voice': '🎤 Голосовое', 'video_note': '🎬 Видеосообщение',
                    'animation': '🎬 GIF'
                }
                media_content = f'<div class="message-media">{media_types.get(msg["media_type"], "📎 Медиа")}</div>'
        
        time_str = msg['created_at'].strftime('%H:%M')
        avatar_letter = sender_name[0].upper()
        
        html_content += f"""
            <div class="{wrapper_class}">
                <div class="message-avatar">{avatar_letter}</div>
                <div class="message-bubble">
                    {media_content}
                    <div class="message-text">{text if text else ('<i>Медиа без текста</i>' if media_content else '')}</div>
                    <div class="message-time">{time_str}</div>
                </div>
            </div>
"""
    
    html_content += f"""
        </div>
        <div class="chat-footer">
            <div>MessageGuardian • Резервная копия чата</div>
            <div class="stats-badge">Всего сообщений: {len(messages)}</div>
        </div>
    </div>
</body>
</html>
"""
    
    # Save HTML file
    filename = f"saved_media/chat_backup_{chat_id}_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    return filename


async def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not bot_token:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не указан в .env")
        return
    
    await init_db()
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        user_id = message.from_user.id
        username = message.from_user.username or "Unknown"
        first_name = message.from_user.first_name or "User"
        
        # Auto-authenticate user
        if not await is_user_authenticated(user_id):
            await authenticate_user(user_id, username, first_name)
            # Create trial subscription for new user
            await create_trial_subscription(user_id)
        
        # Check subscription status
        sub_status = await check_subscription(user_id)
        stats = await get_stats(user_id)
        
        # Build keyboard based on subscription status
        keyboard_buttons = [
            [InlineKeyboardButton(text="📚 Инструкция по подключению", url="https://t.me/MessageAssistant/4")]
        ]
        
        if not sub_status['active']:
            # Add subscription button if expired
            keyboard_buttons.append([InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        # Build subscription info
        if sub_status['active']:
            sub_info = f"✅ <b>Подписка активна</b>\n📅 Осталось дней: <b>{sub_status['days_left']}</b>\n"
        else:
            sub_info = "😢 <b>Пробный период закончился</b>\n💳 Можете приобрести подписку\n"
        
        caption_text = (
            "<b>👋 Добро пожаловать!</b>\n\n"
            "Этот бот создан для сохранения всех деталей переписки, "
            "даже в случае их изменения или удаления 🤫\n\n"
            f"{sub_info}\n"
            f"📊 <b>Статистика:</b>\n"
            f"📨 Сообщений: <b>{stats['messages']}</b>\n"
            f"✏️ Изменений: <b>{stats['edits']}</b>\n"
            f"🗑 Удалений: <b>{stats['deletes']}</b>\n\n"
            f"<b>Доступные команды:</b>\n"
            f"/stats - показать статистику\n"
            f"/help - справка"
        )
        
        # Send photo with caption and inline button
        try:
            await bot.send_photo(
                user_id,
                FSInputFile("photo_2025-12-29_00-18-36.jpg"),
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            print(f"❌ Ошибка отправки фото: {e}")
            # Fallback to text message if photo fails
            await message.answer(caption_text, parse_mode="HTML", reply_markup=keyboard)
    
    
    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        stats = await get_stats(user_id)
        await message.answer(
            f"📊 <b>Ваша статистика MessageGuardian</b>\n\n"
            f"📨 Всего сообщений: <b>{stats['messages']}</b>\n"
            f"✏️ Изменений: <b>{stats['edits']}</b>\n"
            f"🗑 Удалений: <b>{stats['deletes']}</b>",
            parse_mode="HTML"
        )
    
    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        await message.answer(
            "📖 <b>Инструкция MessageGuardian</b>\n\n"
            "🤖 <b>Что делает бот:</b>\n"
            "• Сохраняет все удалённые сообщения\n"
            "• Отслеживает изменения в сообщениях\n"
            "• Сохраняет View Once фото/видео\n"
            "• Создаёт HTML-копию при очистке чата\n\n"
            "🔧 <b>Как подключить:</b>\n"
            "1. Откройте Настройки → Telegram Business\n"
            "2. Раздел 'Чаты' → 'Подключить бота'\n"
            "3. Найдите @MessageGuardianBot\n"
            "4. Выберите 'Все личные чаты'\n\n"
            "💡 <b>Как сохранить View Once медиа:</b>\n"
            "• Ответьте на исчезающее фото/видео\n"
            "• Бот автоматически сохранит его\n"
            "• Вы получите уведомление с медиа\n\n"
            "📊 <b>Команды:</b>\n"
            "/start - главное меню\n"
            "/stats - статистика сообщений\n"
            "/help - эта инструкция\n\n"
            "⚠️ <b>Важно:</b>\n"
            "Бот работает только с вашими бизнес-чатами и автоматически удаляет данные из БД после отправки уведомления.",
            parse_mode="HTML"
        )
    
    @dp.message(Command("admin"))
    async def cmd_admin(message: Message):
        user_id = message.from_user.id
        
        if not await is_admin(user_id):
            return
        
        is_super = await is_super_admin(user_id)
        
        # Get stats
        users = await get_all_users()
        revenue = await get_revenue_stats()
        active_subs = 0
        async with db_pool.acquire() as conn:
            active_subs = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE is_active = TRUE"
            )
        
        text = "👮 <b>Админ-панель MessageGuardian</b>\n\n"
        text += f"👥 Пользователей: <b>{len(users)}</b>\n"
        text += f"✅ Активных подписок: <b>{active_subs}</b>\n"
        text += f"💰 Прибыль: <b>{revenue['total_stars']} ⭐</b>\n"
        text += f"💳 Платежей: <b>{revenue['total_payments']}</b>\n\n"
        
        text += "<b>📝 Команды:</b>\n"
        text += "<code>/grant USER_ID DAYS</code> - выдать подписку\n"
        text += "<code>/revoke USER_ID</code> - забрать подписку\n"
        text += "<code>/check USER_ID</code> - проверить подписку\n"
        text += "<code>/users</code> - выгрузить пользователей в CSV\n"
        text += "<code>/broadcast</code> + ответ на сообщение - рассылка\n\n"
        
        if is_super:
            text += "<b>� Команды супер-админа:</b>\n"
            text += "<code>/addadmin USER_ID</code> - добавить админа\n"
            text += "<code>/deladmin USER_ID</code> - удалить админа\n"
            text += "<code>/admins</code> - список админов\n"
        
        await message.answer(text, parse_mode="HTML")
    
    # ==================== SUBSCRIPTION CALLBACKS ====================
    
    @dp.callback_query(F.data == "buy_subscription")
    async def callback_buy_subscription(callback):
        """Show subscription options"""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 ТЕСТ - 1 звезда (7 дней)", callback_data="sub_test")],
            [InlineKeyboardButton(text="⭐ Неделя - 50 звёзд", callback_data="sub_week")],
            [InlineKeyboardButton(text="⭐ Месяц - 100 звёзд", callback_data="sub_month")],
            [InlineKeyboardButton(text="⭐ Год - 550 звёзд", callback_data="sub_year")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        text = (
            "💳 <b>Выберите подписку:</b>\n\n"
            "🧪 <b>ТЕСТ</b> - 1 звезда (7 дней)\n"
            "⭐ <b>Неделя</b> - 50 звёзд (7 дней)\n"
            "⭐ <b>Месяц</b> - 100 звёзд (30 дней)\n"
            "⭐ <b>Год</b> - 550 звёзд (365 дней)\n\n"
            "💡 Оплата через Telegram Stars\n"
            "💰 Звезды поступают на счет бота"
        )
        
        # Delete original message and send new one
        try:
            await callback.message.delete()
        except:
            pass
        
        await bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data == "back_to_start")
    async def callback_back_to_start(callback):
        """Return to start menu"""
        try:
            await callback.message.delete()
        except:
            pass
        await callback.answer("Возвращаемся в главное меню...")
        
        # Get subscription and stats
        user_id = callback.from_user.id
        sub_status = await check_subscription(user_id)
        stats = await get_stats(user_id)
        
        # Build keyboard
        keyboard_buttons = [
            [InlineKeyboardButton(text="📚 Инструкция по подключению", url="https://t.me/MessageAssistant/4")]
        ]
        if not sub_status['active']:
            keyboard_buttons.append([InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        # Build text
        if sub_status['active']:
            sub_info = f"✅ <b>Подписка активна</b>\n📅 Осталось дней: <b>{sub_status['days_left']}</b>\n"
        else:
            sub_info = "😢 <b>Пробный период закончился</b>\n💳 Можете приобрести подписку\n"
        
        caption_text = (
            "<b>👋 Добро пожаловать!</b>\n\n"
            "Этот бот создан для сохранения всех деталей переписки, "
            "даже в случае их изменения или удаления 🤫\n\n"
            f"{sub_info}\n"
            f"📊 <b>Статистика:</b>\n"
            f"📨 Сообщений: <b>{stats['messages']}</b>\n"
            f"✏️ Изменений: <b>{stats['edits']}</b>\n"
            f"🗑 Удалений: <b>{stats['deletes']}</b>\n\n"
            f"<b>Доступные команды:</b>\n"
            f"/stats - показать статистику\n"
            f"/help - справка"
        )
        
        # Send photo
        try:
            await bot.send_photo(
                user_id,
                FSInputFile("photo_2025-12-29_00-18-36.jpg"),
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except:
            await bot.send_message(user_id, caption_text, parse_mode="HTML", reply_markup=keyboard)
    
    @dp.callback_query(F.data.startswith("sub_"))
    async def callback_subscribe(callback):
        """Process subscription purchase"""
        user_id = callback.from_user.id
        sub_type = callback.data.split("_")[1]
        
        # Define subscription parameters
        prices = {
            "test": (1, 7, "Тест"),
            "week": (50, 7, "Неделя"),
            "month": (100, 30, "Месяц"),
            "year": (550, 365, "Год")
        }
        
        if sub_type not in prices:
            await callback.answer("❌ Неверный тип подписки")
            return
        
        amount, days, name = prices[sub_type]
        
        # Create invoice
        await bot.send_invoice(
            chat_id=user_id,
            title=f"Подписка MessageGuardian - {name}",
            description=f"Подписка на {days} дней",
            payload=f"subscription_{sub_type}_{user_id}",
            provider_token="",  # Empty for Stars
            currency="XTR",  # Telegram Stars
            prices=[LabeledPrice(label=f"Подписка {name}", amount=amount)]
        )
        
        await callback.answer()
    
    @dp.pre_checkout_query()
    async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        """Approve payment"""
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    
    @dp.message(F.successful_payment)
    async def process_successful_payment(message: Message):
        """Handle successful payment"""
        user_id = message.from_user.id
        payment = message.successful_payment
        
        # Parse payload
        payload_parts = payment.invoice_payload.split("_")
        if len(payload_parts) >= 2:
            sub_type = payload_parts[1]
            
            # Define days
            days_map = {"test": 7, "week": 7, "month": 30, "year": 365}
            days = days_map.get(sub_type, 7)
            
            # Extend subscription
            await extend_subscription(user_id, sub_type, days)
            
            # Save payment
            await save_payment(user_id, sub_type, payment.total_amount, payment.telegram_payment_charge_id)
            
            # Send confirmation
            await message.answer(
                f"✅ <b>Оплата успешна!</b>\n\n"
                f"💳 Подписка активирована на {days} дней\n"
                f"🎉 Спасибо за поддержку!",
                parse_mode="HTML"
            )
    
    # ==================== ADMIN COMMANDS ====================
    
    @dp.message(Command("grant"))
    async def admin_grant_subscription(message: Message):
        """Admin command: /grant USER_ID DAYS"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            parts = message.text.split()
            if len(parts) < 3:
                await message.answer("❌ Формат: <code>/grant USER_ID DAYS</code>", parse_mode="HTML")
                return
            
            target_user_id = int(parts[1])
            days = int(parts[2])
            
            await grant_subscription(target_user_id, "admin_grant", days)
            
            await message.answer(
                f"✅ Подписка выдана пользователю <code>{target_user_id}</code> на {days} дней",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("revoke"))
    async def admin_revoke_subscription(message: Message):
        """Admin command: /revoke USER_ID"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Формат: <code>/revoke USER_ID</code>", parse_mode="HTML")
                return
            
            target_user_id = int(parts[1])
            
            await revoke_subscription(target_user_id)
            
            await message.answer(
                f"❌ Подписка отозвана у пользователя <code>{target_user_id}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("check"))
    async def admin_check_subscription(message: Message):
        """Admin command: /check USER_ID"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Формат: <code>/check USER_ID</code>", parse_mode="HTML")
                return
            
            target_user_id = int(parts[1])
            sub_status = await check_subscription(target_user_id)
            
            if sub_status['active']:
                text = (
                    f"✅ <b>Подписка активна</b>\n\n"
                    f"👤 User ID: <code>{target_user_id}</code>\n"
                    f"📦 Тип: <b>{sub_status['type']}</b>\n"
                    f"📅 Осталось дней: <b>{sub_status['days_left']}</b>\n"
                    f"🗓 Истекает: <b>{sub_status['end_date'].strftime('%d.%m.%Y')}</b>"
                )
            else:
                text = (
                    f"❌ <b>Подписка неактивна</b>\n\n"
                    f"👤 User ID: <code>{target_user_id}</code>"
                )
            
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("broadcast"))
    async def admin_broadcast_message(message: Message):
        """Admin command: /broadcast (reply to message)"""
        if not await is_admin(message.from_user.id):
            return
        
        if not message.reply_to_message:
            await message.answer("❌ Ответьте на сообщение которое хотите разослать", parse_mode="HTML")
            return
        
        users = await get_all_users()
        replied_msg = message.reply_to_message
        
        success = 0
        failed = 0
        
        for user in users:
            try:
                if replied_msg.photo:
                    # Send photo with caption
                    await bot.send_photo(
                        user['user_id'],
                        replied_msg.photo[-1].file_id,
                        caption=replied_msg.caption or replied_msg.text,
                        parse_mode="HTML"
                    )
                elif replied_msg.text:
                    # Send text
                    await bot.send_message(
                        user['user_id'],
                        replied_msg.text,
                        parse_mode="HTML"
                    )
                success += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except Exception as e:
                failed += 1
                print(f"Failed to send to {user['user_id']}: {e}")
        
        await message.answer(
            f"📢 Рассылка завершена\n\n"
            f"✅ Успешно: {success}\n"
            f"❌ Ошибок: {failed}",
            parse_mode="HTML"
        )
    
    @dp.message(Command("users"))
    async def admin_export_users(message: Message):
        """Admin command: /users - Export users to CSV"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            users = await get_all_users()
            
            # Create CSV content
            csv_content = "user_id,username,first_name,subscription_status,days_left\n"
            
            for user in users:
                sub_status = await check_subscription(user['user_id'])
                status = "active" if sub_status['active'] else "inactive"
                days = sub_status['days_left'] if sub_status['active'] else 0
                
                csv_content += f"{user['user_id']},{user['username']},{user['first_name']},{status},{days}\n"
            
            # Save to file
            csv_file = Path("users_export.csv")
            csv_file.write_text(csv_content, encoding='utf-8')
            
            # Send file
            await bot.send_document(
                message.from_user.id,
                FSInputFile(csv_file),
                caption=f"📊 Экспорт пользователей\n\nВсего: {len(users)}"
            )
            
            # Delete file
            csv_file.unlink()
            
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("addadmin"))
    async def super_admin_add_admin(message: Message):
        """Super admin command: /addadmin USER_ID"""
        if not await is_super_admin(message.from_user.id):
            return
        
        try:
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Формат: <code>/addadmin USER_ID</code>", parse_mode="HTML")
                return
            
            target_user_id = int(parts[1])
            
            # Get user info
            try:
                chat = await bot.get_chat(target_user_id)
                username = chat.username or "unknown"
                first_name = chat.first_name or "User"
            except:
                username = "unknown"
                first_name = "User"
            
            await add_admin(target_user_id, username, first_name, message.from_user.id)
            
            await message.answer(
                f"✅ Админ добавлен\n\n"
                f"👤 User ID: <code>{target_user_id}</code>\n"
                f"👤 Username: @{username}",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("deladmin"))
    async def super_admin_remove_admin(message: Message):
        """Super admin command: /deladmin USER_ID"""
        if not await is_super_admin(message.from_user.id):
            return
        
        try:
            parts = message.text.split()
            if len(parts) < 2:
                await message.answer("❌ Формат: <code>/deladmin USER_ID</code>", parse_mode="HTML")
                return
            
            target_user_id = int(parts[1])
            
            if target_user_id == SUPER_ADMIN_ID:
                await message.answer("❌ Нельзя удалить главного админа")
                return
            
            await remove_admin(target_user_id)
            
            await message.answer(
                f"❌ Админ удалён\n\n"
                f"👤 User ID: <code>{target_user_id}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.message(Command("admins"))
    async def super_admin_list_admins(message: Message):
        """Super admin command: /admins - List all admins"""
        if not await is_super_admin(message.from_user.id):
            return
        
        try:
            admins = await get_all_admins()
            
            text = "👮 <b>Список админов</b>\n\n"
            
            for admin in admins:
                role = "👑 Супер-админ" if admin['is_super_admin'] else "👮 Админ"
                text += f"{role}\n"
                text += f"├ ID: <code>{admin['user_id']}</code>\n"
                text += f"├ Username: @{admin['username']}\n"
                text += f"└ Добавлен: {admin['created_at'].strftime('%d.%m.%Y')}\n\n"
            
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    
    
    @dp.business_connection()
    async def handle_business_connection(connection):
        """Handle business connection events"""
        user_id = connection.user.id
        username = connection.user.username or "Unknown"
        first_name = connection.user.first_name or "User"
        connection_id = connection.id
        
        print(f"🔗 Business connection: user_id={user_id}, connection_id={connection_id}")
        
        if connection.is_enabled:
            await save_business_connection(connection_id, user_id, username, first_name)
            print(f"✅ Сохранена связь: {connection_id} → {user_id}")
            
            # Send success notification to user
            try:
                await bot.send_message(
                    user_id,
                    "✅ <b>Бот успешно подключен!</b>\n\n"
                    "🤖 MessageGuardian теперь отслеживает ваши бизнес-чаты.\n"
                    "Все удаленные и измененные сообщения будут сохранены.\n\n"
                    "💡 <b>Для View Once медиа:</b> ответьте на сообщение, чтобы сохранить его.",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"❌ Ошибка отправки уведомления о подключении: {e}")
        else:
            print(f"❌ Отключено: {connection_id}")
    
    @dp.business_message()
    async def handle_business_message(message: Message):
        print(f"📨 Получено business сообщение: chat_id={message.chat.id}, msg_id={message.message_id}")
        
        # Get owner from business_connection
        owner_id = None
        if hasattr(message, 'business_connection_id') and message.business_connection_id:
            owner_id = await get_user_by_connection(message.business_connection_id)
            print(f"🔗 Connection ID: {message.business_connection_id} → Owner: {owner_id}")
        
        if not owner_id:
            print(f"⚠️ Owner ID не найден для connection {message.business_connection_id if hasattr(message, 'business_connection_id') else 'N/A'}")
            return
            
        is_auth = await is_user_authenticated(owner_id)
        print(f"🔐 Авторизован: {is_auth}")
        
        if not is_auth:
            print(f"⚠️ Пользователь {owner_id} не авторизован, пропускаю сообщение")
            return
        
        # ===== PRIORITY: View Once media - process BEFORE subscription check =====
        # View Once photo via reply (only if has_media_spoiler)
        if message.reply_to_message and message.reply_to_message.photo and message.reply_to_message.has_media_spoiler:
            try:
                orig_msg_id = message.reply_to_message.message_id
                file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_photo_reply.jpg"
                
                print(f"📸 Скачиваю View Once фото: {file_path}")
                await bot.download(message.reply_to_message.photo[-1], destination=file_path)
                
                if not Path(file_path).exists():
                    print(f"❌ Файл не был создан: {file_path}")
                    return
                
                print(f"✅ Файл сохранён: {file_path}, размер: {Path(file_path).stat().st_size} байт")
                
                user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                fancy_name = to_fancy(user_name)
                header = f"🔒 <b>View Once фото сохранено!</b>\n\n{fancy_name}{user_username} отправил(а) исчезающее фото"
                
                print(f"📤 Отправляю View Once фото владельцу {owner_id}")
                await bot.send_photo(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                print(f"✅ View Once фото успешно отправлено {owner_id}")
                
                # Save to DB after successful send
                await save_message(owner_id, message.chat.id, orig_msg_id,
                           message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                           "", media_type="photo_reply", file_path=file_path,
                           caption=message.reply_to_message.caption)
            except Exception as e:
                print(f"❌ Ошибка View Once фото: {e}")
                import traceback
                traceback.print_exc()
        
        # View Once video via reply (only if has_media_spoiler)
        if message.reply_to_message and message.reply_to_message.video and message.reply_to_message.has_media_spoiler:
            try:
                orig_msg_id = message.reply_to_message.message_id
                file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_video_reply.mp4"
                
                print(f"🎥 Скачиваю View Once видео: {file_path}")
                await bot.download(message.reply_to_message.video, destination=file_path)
                
                if not Path(file_path).exists():
                    print(f"❌ Файл не был создан: {file_path}")
                    return
                
                print(f"✅ Файл сохранён: {file_path}, размер: {Path(file_path).stat().st_size} байт")
                
                user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                fancy_name = to_fancy(user_name)
                header = f"🔒 <b>View Once видео сохранено!</b>\n\n{fancy_name}{user_username} отправил(а) исчезающее видео"
                
                print(f"📤 Отправляю View Once видео владельцу {owner_id}")
                await bot.send_video(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                print(f"✅ View Once видео успешно отправлено {owner_id}")
                
                # Save to DB after successful send
                await save_message(owner_id, message.chat.id, orig_msg_id,
                           message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                           "", media_type="video_reply", file_path=file_path,
                           caption=message.reply_to_message.caption)
            except Exception as e:
                print(f"❌ Ошибка View Once видео: {e}")
                import traceback
                traceback.print_exc()
        
        # ===== NOW check subscription for regular message processing =====
        sub_status = await check_subscription(owner_id)
        if not sub_status['active']:
            print(f"⚠️ У пользователя {owner_id} истекла подписка")
            # Don't process regular messages, but View Once already processed above
            return
        
        media_type = None
        file_path = None
        
        try:
            if message.photo:
                media_type = "photo"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_photo.jpg"
                await bot.download(message.photo[-1], destination=file_path)
            elif message.video:
                media_type = "video"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_video.mp4"
                await bot.download(message.video, destination=file_path)
            elif message.document:
                media_type = "document"
                ext = message.document.file_name.split('.')[-1] if message.document.file_name else "file"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_doc.{ext}"
                await bot.download(message.document, destination=file_path)
            elif message.sticker:
                media_type = "sticker"
                if message.sticker.is_video:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.webm"
                elif message.sticker.is_animated:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.tgs"
                else:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.webp"
                await bot.download(message.sticker, destination=file_path)
            elif message.voice:
                media_type = "voice"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_voice.ogg"
                await bot.download(message.voice, destination=file_path)
            elif message.video_note:
                media_type = "video_note"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_videonote.mp4"
                await bot.download(message.video_note, destination=file_path)
            elif message.animation:
                media_type = "animation"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_animation.mp4"
                await bot.download(message.animation, destination=file_path)
        except Exception as e:
            print(f"❌ Ошибка скачивания медиа: {e}")
        
        links = []
        if message.entities:
            for entity in message.entities:
                if entity.type in ("url", "text_link"):
                    if entity.type == "url" and message.text:
                        links.append(message.text[entity.offset:entity.offset + entity.length])
                    elif entity.type == "text_link" and entity.url:
                        links.append(entity.url)
        
        await save_message(owner_id, message.chat.id, message.message_id,
                    message.from_user.id if message.from_user else None,
                    message.text or "", media_type=media_type, file_path=file_path,
                    caption=message.caption, links=", ".join(links) if links else None)
        await increment_stat(owner_id, "total_messages")
    
    @dp.edited_business_message()
    async def handle_edited_business_message(message: Message):
        print(f"✏️ Получено изменение сообщения: chat_id={message.chat.id}, msg_id={message.message_id}")
        
        # Get owner from business_connection
        owner_id = None
        if hasattr(message, 'business_connection_id') and message.business_connection_id:
            owner_id = await get_user_by_connection(message.business_connection_id)
        
        if not owner_id or not await is_user_authenticated(owner_id):
            print(f"⚠️ Пропускаю изменение: owner_id={owner_id}")
            return
        
        if message.from_user and message.from_user.id == owner_id:
            return
        
        old_data = await get_message_full(owner_id, message.chat.id, message.message_id)
        old = old_data["text"] if old_data else None
        new = message.text or message.caption or ""
        
        await save_message(owner_id, message.chat.id, message.message_id,
                    message.from_user.id if message.from_user else None,
                    new, caption=message.caption)
        await increment_stat(owner_id, "total_edits")
        
        user_name = message.from_user.first_name if message.from_user else "Unknown"
        user_username = f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
        fancy_name = to_fancy(user_name)
        
        # Monospace font for Old/New text
        def to_monospace(text: str) -> str:
            mono_map = {
                'A': '𝙰', 'B': '𝙱', 'C': '𝙲', 'D': '𝙳', 'E': '𝙴', 'F': '𝙵', 'G': '𝙶', 'H': '𝙷', 'I': '𝙸', 'J': '𝙹',
                'K': '𝙺', 'L': '𝙻', 'M': '𝙼', 'N': '𝙽', 'O': '𝙾', 'P': '𝙿', 'Q': '𝚀', 'R': '𝚁', 'S': '𝚂', 'T': '𝚃',
                'U': '𝚄', 'V': '𝚅', 'W': '𝚆', 'X': '𝚇', 'Y': '𝚈', 'Z': '𝚉',
                'a': '𝚊', 'b': '𝚋', 'c': '𝚌', 'd': '𝚍', 'e': '𝚎', 'f': '𝚏', 'g': '𝚐', 'h': '𝚑', 'i': '𝚒', 'j': '𝚓',
                'k': '𝚔', 'l': '𝚕', 'm': '𝚖', 'n': '𝚗', 'o': '𝚘', 'p': '𝚙', 'q': '𝚚', 'r': '𝚛', 's': '𝚜', 't': '𝚝',
                'u': '𝚞', 'v': '𝚟', 'w': '𝚠', 'x': '𝚡', 'y': '𝚢', 'z': '𝚣',
                '0': '𝟶', '1': '𝟷', '2': '𝟸', '3': '𝟹', '4': '𝟺', '5': '𝟻', '6': '𝟼', '7': '𝟽', '8': '𝟾', '9': '𝟿'
            }
            return ''.join(mono_map.get(c, c) for c in text)
        
        old_formatted = to_monospace(old) if old else '<i>Не найдено</i>'
        new_formatted = to_monospace(new) if new else '<i>Пусто</i>'
        
        text = f"{fancy_name}{user_username} изменил(а) сообщение:\n\nOld:\n{old_formatted}\n\nNew:\n{new_formatted}"
        
        try:
            await bot.send_message(owner_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"❌ Ошибка отправки изменения: {e}")
    
    @dp.deleted_business_messages()
    async def handle_deleted_business_messages(event: BusinessMessagesDeleted):
        print(f"🗑 Получено удаление {len(event.message_ids)} сообщений в чате {event.chat.id}")
        
        # Get owner_id and total messages in this chat
        async with db_pool.acquire() as conn:
            first_row = await conn.fetchrow(
                "SELECT owner_id FROM messages WHERE chat_id = $1 AND message_id = ANY($2) LIMIT 1",
                event.chat.id, event.message_ids
            )
            
            if not first_row:
                print("⚠️ Не найден owner_id для удаленных сообщений")
                return
            
            owner_id = first_row['owner_id']
            
            # Count total messages in this chat
            total_messages = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND owner_id = $2",
                event.chat.id, owner_id
            )
        
        print(f"📊 Всего сообщений в БД для чата {event.chat.id}: {total_messages}")
        print(f"📊 Удаляется сообщений: {len(event.message_ids)}")
        
        # Check if this is a full chat clear
        # If deleting >50% of messages OR >10 messages at once, consider it a chat clear
        is_chat_clear = (len(event.message_ids) > 10) or (total_messages > 0 and len(event.message_ids) / total_messages > 0.5)
        
        if is_chat_clear:
            chat_name = event.chat.first_name or "Unknown" if event.chat else "Unknown"
            
            # Create HTML backup before deleting
            print(f"📦 Создаю HTML-копию чата {event.chat.id}...")
            html_file = await create_chat_html_backup(owner_id, event.chat.id, chat_name)
            
            if html_file:
                try:
                    await bot.send_document(
                        owner_id,
                        FSInputFile(html_file),
                        caption=f"🗑 <b>Весь чат был очищен!</b>\n\n"
                                f"👤 Чат: {chat_name}\n"
                                f"📊 Удалено сообщений: {len(event.message_ids)}\n\n"
                                f"📄 HTML-копия чата прикреплена",
                        parse_mode="HTML"
                    )
                    print(f"✅ HTML-копия отправлена владельцу {owner_id}")
                except Exception as e:
                    print(f"❌ Ошибка отправки HTML: {e}")
        
        for msg_id in event.message_ids:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM messages WHERE chat_id = $1 AND message_id = $2", event.chat.id, msg_id)
                
                if not row:
                    continue
                
                owner_id = row["owner_id"]
                msg_data = dict(row)
                
                if msg_data.get("user_id") == owner_id:
                    await delete_message_from_db(owner_id, event.chat.id, msg_id)
                    continue
                
                await increment_stat(owner_id, "total_deletes")
                
                user_name = event.chat.first_name or "User" if event.chat else "Unknown"
                user_username = f" (@{event.chat.username})" if event.chat and event.chat.username else ""
                fancy_name = to_fancy(user_name)
                
                caption_parts = []
                if msg_data.get("text") and msg_data["text"].strip():
                    caption_parts.append(f"📝 Текст: {msg_data['text']}")
                elif msg_data.get("caption") and msg_data["caption"].strip():
                    caption_parts.append(f"📝 Подпись: {msg_data['caption']}")
                
                if msg_data.get("links"):
                    caption_parts.append(f"🔗 Ссылки: {msg_data['links']}")
                
                header = f"{fancy_name}{user_username} удалил(а) сообщение:\n\n"
                if caption_parts:
                    header += "\n".join(caption_parts) + "\n\n"
                
                if msg_data.get("file_path") and Path(msg_data["file_path"]).exists():
                    try:
                        if msg_data["media_type"] in ("photo", "photo_reply"):
                            prefix = "💬 Фото (через ответ)\n" if msg_data["media_type"] == "photo_reply" else ""
                            await bot.send_photo(owner_id, FSInputFile(msg_data["file_path"]), caption=prefix + header, parse_mode="HTML")
                        elif msg_data["media_type"] in ("video", "video_reply"):
                            prefix = "💬 Видео (через ответ)\n" if msg_data["media_type"] == "video_reply" else ""
                            await bot.send_video(owner_id, FSInputFile(msg_data["file_path"]), caption=prefix + header, parse_mode="HTML")
                        elif msg_data["media_type"] == "document":
                            await bot.send_document(owner_id, FSInputFile(msg_data["file_path"]), caption=header, parse_mode="HTML")
                        elif msg_data["media_type"] == "sticker":
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                            await bot.send_document(owner_id, FSInputFile(msg_data["file_path"]))
                        elif msg_data["media_type"] == "voice":
                            await bot.send_voice(owner_id, FSInputFile(msg_data["file_path"]), caption=header, parse_mode="HTML")
                        elif msg_data["media_type"] == "video_note":
                            await bot.send_video_note(owner_id, FSInputFile(msg_data["file_path"]))
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                        elif msg_data["media_type"] == "animation":
                            await bot.send_animation(owner_id, FSInputFile(msg_data["file_path"]), caption=header, parse_mode="HTML")
                    except Exception as e:
                        print(f"❌ Ошибка отправки медиа: {e}")
                        try:
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                        except:
                            pass
                else:
                    if caption_parts:
                        try:
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                        except:
                            pass
                
                await delete_message_from_db(owner_id, event.chat.id, msg_id)
                print(f"🗑️ Сообщение {msg_id} удалено из БД")
    
    print("=" * 60)
    print("MessageGuardian Multi-User Bot (PostgreSQL)")
    print("=" * 60)
    print(f"🔐 Пароль: {BOT_PASSWORD}")
    print(f"👮 Admin ID: {ADMIN_ID}")
    print(f"🗄️  База данных: {DB_NAME} @ {DB_HOST}:{DB_PORT}")
    print("=" * 60)
    print("Бот готов! Напишите /start для авторизации")
    print("=" * 60)
    
    try:
        await dp.start_polling(bot)
    finally:
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nБот остановлен.")
