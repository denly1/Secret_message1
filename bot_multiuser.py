"""
MessageGuardian Multi-User Business Bot
Supports multiple users with password authentication
PostgreSQL database for scalability
"""

import asyncio
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, BusinessMessagesDeleted
import asyncpg

load_dotenv()

# Environment variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "12391")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# PostgreSQL connection
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "Secret_message")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1")

# Create media directory
Path("saved_media").mkdir(exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Global database pool
db_pool = None


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

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


async def close_db():
    """Close database connection pool"""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("✅ PostgreSQL connection pool closed")


async def is_user_authenticated(user_id: int) -> bool:
    """Check if user is authenticated"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_authenticated FROM users WHERE user_id = $1 AND is_banned = FALSE",
            user_id
        )
        return result is True


async def is_user_banned(user_id: int) -> bool:
    """Check if user is banned"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_banned FROM users WHERE user_id = $1",
            user_id
        )
        return result is True


async def authenticate_user(user_id: int, username: str, first_name: str):
    """Authenticate user after successful password"""
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
    """Record failed login attempt and return total attempts"""
    async with db_pool.acquire() as conn:
        # Get current attempts count
        attempts = await conn.fetchval(
            "SELECT attempts_count FROM failed_logins WHERE user_id = $1 ORDER BY attempt_time DESC LIMIT 1",
            user_id
        )
        
        if attempts is None:
            attempts = 0
        
        new_attempts = attempts + 1
        
        # Record new attempt
        await conn.execute(
            "INSERT INTO failed_logins (user_id, username, first_name, attempts_count) VALUES ($1, $2, $3, $4)",
            user_id, username, first_name, new_attempts
        )
        
        return new_attempts


async def ban_user(user_id: int, username: str, first_name: str):
    """Ban user after too many failed attempts"""
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


async def save_message(owner_id: int, chat_id: int, message_id: int, user_id: int, text: str,
                      media_type: str = None, file_path: str = None, caption: str = None, links: str = None):
    """Save message to database"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (owner_id, chat_id, message_id, user_id, text, media_type, file_path, caption, links)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (owner_id, chat_id, message_id) DO UPDATE
            SET text = $5, media_type = $6, file_path = $7, caption = $8, links = $9
            """,
            owner_id, chat_id, message_id, user_id, text, media_type, file_path, caption, links
        )


async def get_message_full(owner_id: int, chat_id: int, message_id: int):
    """Get full message data"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM messages WHERE owner_id = $1 AND chat_id = $2 AND message_id = $3",
            owner_id, chat_id, message_id
        )
        if row:
            return dict(row)
        return None


async def delete_message_from_db(owner_id: int, chat_id: int, message_id: int):
    """Delete message from database after sending notification"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE owner_id = $1 AND chat_id = $2 AND message_id = $3",
            owner_id, chat_id, message_id
        )


async def increment_stat(owner_id: int, stat_type: str):
    """Increment statistics"""
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


async def get_stats(owner_id: int):
    """Get user statistics"""
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


async def get_banned_users():
    """Get list of banned users (admin only)"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, first_name, reason, banned_at FROM banned_users ORDER BY banned_at DESC"
        )
        return [dict(row) for row in rows]


async def get_failed_logins():
    """Get list of failed login attempts (admin only)"""
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


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def to_fancy(text: str) -> str:
    """Convert text to fancy Unicode font"""
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    fancy = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇"
    trans = str.maketrans(normal, fancy)
    return text.translate(trans)


# ============================================================
# BOT HANDLERS
# ============================================================

async def main():
    """Main bot function"""
    
    # Initialize database
    await init_db()
    
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        """Handle /start command - password authentication"""
        user_id = message.from_user.id
        username = message.from_user.username or "Unknown"
        first_name = message.from_user.first_name or "User"
        
        # Check if banned
        if await is_user_banned(user_id):
            await message.answer(
                "🚫 <b>Доступ запрещён</b>\n\n"
                "Вы заблокированы за превышение лимита попыток входа.\n"
                "Обратитесь к администратору.",
                parse_mode="HTML"
            )
            return
        
        # Check if already authenticated
        if await is_user_authenticated(user_id):
            stats = await get_stats(user_id)
            await message.answer(
                f"✅ <b>Вы уже авторизованы!</b>\n\n"
                f"🤖 <b>MessageGuardian Multi-User Bot</b>\n\n"
                f"📊 <b>Ваша статистика:</b>\n"
                f"📨 Сообщений: <b>{stats['messages']}</b>\n"
                f"✏️ Изменений: <b>{stats['edits']}</b>\n"
                f"🗑 Удалений: <b>{stats['deletes']}</b>\n\n"
                f"Команды:\n"
                f"/stats - статистика\n"
                f"/help - помощь",
                parse_mode="HTML"
            )
            return
        
        await message.answer(
            "🔐 <b>Добро пожаловать в MessageGuardian!</b>\n\n"
            "Для доступа к боту введите пароль:",
            parse_mode="HTML"
        )
    
    @dp.message(F.text)
    async def handle_password(message: Message):
        """Handle password authentication"""
        user_id = message.from_user.id
        username = message.from_user.username or "Unknown"
        first_name = message.from_user.first_name or "User"
        
        # Check if banned
        if await is_user_banned(user_id):
            await message.answer(
                "🚫 <b>Доступ запрещён</b>\n\n"
                "Вы заблокированы.",
                parse_mode="HTML"
            )
            return
        
        # Check if already authenticated
        if await is_user_authenticated(user_id):
            return
        
        # Check password
        if message.text == BOT_PASSWORD:
            await authenticate_user(user_id, username, first_name)
            await message.answer(
                "✅ <b>Авторизация успешна!</b>\n\n"
                "🤖 <b>MessageGuardian Multi-User Bot</b>\n\n"
                "Теперь подключите меня к бизнес-аккаунту:\n"
                "1. Настройки → Telegram для бизнеса\n"
                "2. Раздел 'Бот' → укажите мой @username\n"
                "3. Выберите 'Все личные чаты'\n"
                "4. Включите 'Сообщения 5/5'\n\n"
                "Я буду сохранять ВСЁ:\n"
                "🖼 Фото (включая View Once)\n"
                "🎥 Видео (включая исчезающие)\n"
                "🎭 Стикеры\n"
                "📄 Документы\n"
                "🎤 Голосовые\n"
                "🎬 GIF/Анимации\n\n"
                "💡 <b>Для View Once фото:</b>\n"
                "Ответьте на фото любым сообщением — я сохраню его!\n\n"
                "Команды:\n"
                "/stats - статистика\n"
                "/help - помощь",
                parse_mode="HTML"
            )
            print(f"✅ Пользователь {first_name} (@{username}, ID: {user_id}) авторизован")
        else:
            # Wrong password
            attempts = await record_failed_login(user_id, username, first_name)
            
            if attempts >= 3:
                await ban_user(user_id, username, first_name)
                await message.answer(
                    "🚫 <b>Доступ заблокирован!</b>\n\n"
                    "Превышен лимит попыток входа (3).\n"
                    "Обратитесь к администратору.",
                    parse_mode="HTML"
                )
                print(f"🚫 Пользователь {first_name} (@{username}, ID: {user_id}) ЗАБЛОКИРОВАН после {attempts} попыток")
                
                # Notify admin
                if ADMIN_ID:
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🚫 <b>Пользователь заблокирован</b>\n\n"
                            f"👤 {first_name} (@{username})\n"
                            f"🆔 ID: <code>{user_id}</code>\n"
                            f"❌ Неудачных попыток: {attempts}",
                            parse_mode="HTML"
                        )
                    except:
                        pass
            else:
                remaining = 3 - attempts
                await message.answer(
                    f"❌ <b>Неверный пароль!</b>\n\n"
                    f"Осталось попыток: <b>{remaining}</b>",
                    parse_mode="HTML"
                )
                print(f"❌ Неудачная попытка входа: {first_name} (@{username}, ID: {user_id}), попытка {attempts}/3")
    
    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        """Show user statistics"""
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        stats = await get_stats(user_id)
        await message.answer(
            f"📊 <b>Ваша статистика MessageGuardian</b>\n\n"
            f"📨 Всего сообщений: <b>{stats['messages']}</b>\n"
            f"✏️ Изменений: <b>{stats['edits']}</b>\n"
            f"🗑 Удалений: <b>{stats['deletes']}</b>\n\n"
            f"<i>Статистика ведется с момента регистрации</i>",
            parse_mode="HTML"
        )
    
    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        """Show help"""
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        await message.answer(
            "📖 <b>Помощь MessageGuardian</b>\n\n"
            "<b>Команды:</b>\n"
            "/start - авторизация\n"
            "/stats - статистика\n"
            "/help - эта справка\n\n"
            "<b>Как работает бот:</b>\n"
            "• Сохраняет все сообщения в ваших чатах\n"
            "• Уведомляет об удалениях и изменениях\n"
            "• Работает только с вашими чатами\n"
            "• Автоматически удаляет данные после уведомления\n\n"
            "<b>View Once фото:</b>\n"
            "Ответьте на фото — бот сохранит его сразу",
            parse_mode="HTML"
        )
    
    @dp.message(Command("admin"))
    async def cmd_admin(message: Message):
        """Admin panel - show banned users and failed logins"""
        user_id = message.from_user.id
        
        if user_id != ADMIN_ID:
            return
        
        banned = await get_banned_users()
        failed = await get_failed_logins()
        
        text = "👮 <b>Админ-панель MessageGuardian</b>\n\n"
        
        text += f"🚫 <b>Заблокированные пользователи ({len(banned)}):</b>\n"
        if banned:
            for user in banned[:10]:
                text += f"• {user['first_name']} (@{user['username']}) - ID: {user['user_id']}\n"
                text += f"  Причина: {user['reason']}\n"
                text += f"  Дата: {user['banned_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
        else:
            text += "<i>Нет заблокированных пользователей</i>\n\n"
        
        text += f"❌ <b>Неудачные попытки входа ({len(failed)}):</b>\n"
        if failed:
            for attempt in failed[:10]:
                text += f"• {attempt['first_name']} (@{attempt['username']}) - ID: {attempt['user_id']}\n"
                text += f"  Попыток: {attempt['attempts']}\n"
                text += f"  Последняя: {attempt['last_attempt'].strftime('%Y-%m-%d %H:%M')}\n\n"
        else:
            text += "<i>Нет неудачных попыток</i>\n\n"
        
        await message.answer(text, parse_mode="HTML")
    
    # ============================================================
    # BUSINESS MESSAGE HANDLERS
    # ============================================================
    
    @dp.business_message()
    async def handle_business_message(message: Message):
        """Handle incoming business messages - save all media"""
        # Get owner_id from business_connection_id
        # In Business API, the owner is the one who connected the bot
        owner_id = message.from_user.id if message.from_user else None
        
        if not owner_id:
            return
        
        # Check if user is authenticated
        if not await is_user_authenticated(owner_id):
            return
        
        media_type = None
        file_path = None
        
        # Check for View Once photo via reply
        if message.reply_to_message and message.reply_to_message.photo:
            try:
                print(f"🔍 View Once фото обнаружено (ID: {message.reply_to_message.message_id})")
                orig_msg_id = message.reply_to_message.message_id
                file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_photo_reply.jpg"
                await bot.download(message.reply_to_message.photo[-1], destination=file_path)
                print(f"✅ View Once фото сохранено: {file_path}")
                
                await save_message(
                    owner_id,
                    message.chat.id,
                    orig_msg_id,
                    message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                    "",
                    media_type="photo_reply",
                    file_path=file_path,
                    caption=message.reply_to_message.caption
                )
                
                # Send immediately to owner
                user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                fancy_name = to_fancy(user_name)
                header = f"💬 View Once фото\n{fancy_name}{user_username} отправил(а) исчезающее фото:\n\n"
                
                try:
                    await bot.send_photo(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                    print(f"✅ View Once фото отправлено владельцу {owner_id}")
                except Exception as e:
                    print(f"❌ Ошибка отправки View Once фото: {e}")
            except Exception as e:
                print(f"❌ Ошибка сохранения View Once фото: {e}")
        
        # Check for View Once video via reply
        if message.reply_to_message and message.reply_to_message.video:
            try:
                print(f"🔍 View Once видео обнаружено (ID: {message.reply_to_message.message_id})")
                orig_msg_id = message.reply_to_message.message_id
                file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_video_reply.mp4"
                await bot.download(message.reply_to_message.video, destination=file_path)
                print(f"✅ View Once видео сохранено: {file_path}")
                
                await save_message(
                    owner_id,
                    message.chat.id,
                    orig_msg_id,
                    message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                    "",
                    media_type="video_reply",
                    file_path=file_path,
                    caption=message.reply_to_message.caption
                )
                
                # Send immediately to owner
                user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                fancy_name = to_fancy(user_name)
                header = f"💬 View Once видео\n{fancy_name}{user_username} отправил(а) исчезающее видео:\n\n"
                
                try:
                    await bot.send_video(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                    print(f"✅ View Once видео отправлено владельцу {owner_id}")
                except Exception as e:
                    print(f"❌ Ошибка отправки View Once видео: {e}")
            except Exception as e:
                print(f"❌ Ошибка сохранения View Once видео: {e}")
        
        try:
            # Download media
            if message.photo:
                media_type = "photo"
                is_protected = hasattr(message, 'has_protected_content') and message.has_protected_content
                is_ttl = hasattr(message.photo[-1], 'ttl_seconds') and message.photo[-1].ttl_seconds
                
                if is_ttl:
                    media_type = "photo_ttl"
                    print(f"⏱️ Истекающее фото (TTL: {message.photo[-1].ttl_seconds}s)")
                if is_protected:
                    media_type = "photo_protected"
                    print(f"🔒 Защищённое фото (View Once)")
                
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_photo.jpg"
                await bot.download(message.photo[-1], destination=file_path)
                print(f"✅ Фото сохранено: {file_path}")
            
            elif message.video:
                media_type = "video"
                if hasattr(message.video, 'ttl_seconds') and message.video.ttl_seconds:
                    media_type = "video_ttl"
                    print(f"⏱️ Истекающее видео (TTL: {message.video.ttl_seconds}s)")
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_video.mp4"
                await bot.download(message.video, destination=file_path)
                print(f"✅ Видео сохранено: {file_path}")
            
            elif message.document:
                media_type = "document"
                ext = message.document.file_name.split('.')[-1] if message.document.file_name else "file"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_doc.{ext}"
                await bot.download(message.document, destination=file_path)
                print(f"✅ Документ сохранен: {file_path}")
            
            elif message.sticker:
                media_type = "sticker"
                if message.sticker.is_video:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.webm"
                elif message.sticker.is_animated:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.tgs"
                else:
                    file_path = f"saved_media/{message.chat.id}_{message.message_id}_sticker.webp"
                await bot.download(message.sticker, destination=file_path)
                print(f"✅ Стикер сохранен: {file_path}")
            
            elif message.voice:
                media_type = "voice"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_voice.ogg"
                await bot.download(message.voice, destination=file_path)
                print(f"✅ Голосовое сохранено: {file_path}")
            
            elif message.video_note:
                media_type = "video_note"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_videonote.mp4"
                await bot.download(message.video_note, destination=file_path)
                print(f"✅ Кружочек сохранен: {file_path}")
            
            elif message.animation:
                media_type = "animation"
                file_path = f"saved_media/{message.chat.id}_{message.message_id}_animation.mp4"
                await bot.download(message.animation, destination=file_path)
                print(f"✅ GIF сохранен: {file_path}")
        
        except Exception as e:
            print(f"❌ Ошибка скачивания медиа ({media_type}): {e}")
            import traceback
            traceback.print_exc()
        
        # Extract links
        links = []
        if message.entities:
            for entity in message.entities:
                if entity.type in ("url", "text_link"):
                    if entity.type == "url" and message.text:
                        links.append(message.text[entity.offset:entity.offset + entity.length])
                    elif entity.type == "text_link" and entity.url:
                        links.append(entity.url)
        
        await save_message(
            owner_id,
            message.chat.id,
            message.message_id,
            message.from_user.id if message.from_user else None,
            message.text or "",
            media_type=media_type,
            file_path=file_path,
            caption=message.caption,
            links=", ".join(links) if links else None
        )
        await increment_stat(owner_id, "total_messages")
    
    @dp.edited_business_message()
    async def handle_edited_business_message(message: Message):
        """Handle edited business messages"""
        owner_id = message.from_user.id if message.from_user else None
        
        if not owner_id or not await is_user_authenticated(owner_id):
            return
        
        # Skip own edits
        if message.from_user and message.from_user.id == owner_id:
            print(f"⏭ Пропускаю изменение своего сообщения (owner {owner_id})")
            return
        
        old_data = await get_message_full(owner_id, message.chat.id, message.message_id)
        old = old_data["text"] if old_data else None
        new = message.text or message.caption or ""
        
        await save_message(
            owner_id,
            message.chat.id,
            message.message_id,
            message.from_user.id if message.from_user else None,
            new,
            caption=message.caption
        )
        await increment_stat(owner_id, "total_edits")
        
        user_name = message.from_user.first_name if message.from_user else "Unknown"
        user_username = f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
        fancy_name = to_fancy(user_name)
        
        if old is None or not old.strip():
            text = (
                f"{fancy_name}{user_username} изменил(а) сообщение:\n\n"
                f"<b>Old:</b>\n<i>Текст не найден в кэше</i>\n\n"
                f"<b>New:</b>\n{new}"
            )
        else:
            text = (
                f"{fancy_name}{user_username} изменил(а) сообщение:\n\n"
                f"<b>Old:</b>\n{old}\n\n"
                f"<b>New:</b>\n{new}"
            )
        
        try:
            await bot.send_message(owner_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"❌ Ошибка отправки уведомления об изменении: {e}")
    
    @dp.deleted_business_messages()
    async def handle_deleted_business_messages(event: BusinessMessagesDeleted):
        """Handle deleted business messages"""
        # Get owner from chat - in Business API, we need to find who owns this chat
        # For now, we'll iterate through all message IDs and find the owner
        
        print(f"🗑 Обнаружено удаление {len(event.message_ids)} сообщений в чате {event.chat.id}")
        
        for msg_id in event.message_ids:
            # Try to find message in DB for any owner
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM messages WHERE chat_id = $1 AND message_id = $2",
                    event.chat.id, msg_id
                )
                
                if not row:
                    continue
                
                msg_data = dict(row)
                owner_id = msg_data["owner_id"]
                
                print(f"📦 Данные сообщения {msg_id}: owner={owner_id}, media={msg_data.get('media_type')}")
                
                # Skip own deletions
                if msg_data.get("user_id") == owner_id:
                    print(f"⏭ Пропускаю удаление своего сообщения (owner {owner_id})")
                    await delete_message_from_db(owner_id, event.chat.id, msg_id)
                    continue
                
                await increment_stat(owner_id, "total_deletes")
                
                user_name = "Unknown"
                user_username = ""
                if event.chat:
                    user_name = event.chat.first_name or "User"
                    if event.chat.username:
                        user_username = f" (@{event.chat.username})"
                
                fancy_name = to_fancy(user_name)
                
                # Form caption
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
                
                # Send media if exists
                if msg_data.get("file_path") and Path(msg_data["file_path"]).exists():
                    try:
                        if msg_data["media_type"] in ("photo", "photo_ttl", "photo_protected", "photo_reply"):
                            prefix = ""
                            if msg_data["media_type"] == "photo_ttl":
                                prefix = "⏱ Истекающее фото\n"
                            elif msg_data["media_type"] == "photo_protected":
                                prefix = "🔒 View Once фото\n"
                            elif msg_data["media_type"] == "photo_reply":
                                prefix = "💬 Фото (сохранено через ответ)\n"
                            
                            await bot.send_photo(owner_id, FSInputFile(msg_data["file_path"]), caption=prefix + header, parse_mode="HTML")
                            print(f"✅ Фото отправлено владельцу {owner_id}")
                        
                        elif msg_data["media_type"] in ("video", "video_ttl", "video_reply"):
                            prefix = ""
                            if msg_data["media_type"] == "video_ttl":
                                prefix = "⏱ Истекающее видео\n"
                            elif msg_data["media_type"] == "video_reply":
                                prefix = "💬 Видео (сохранено через ответ)\n"
                            
                            await bot.send_video(owner_id, FSInputFile(msg_data["file_path"]), caption=prefix + header, parse_mode="HTML")
                            print(f"✅ Видео отправлено владельцу {owner_id}")
                        
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
                        import traceback
                        traceback.print_exc()
                        try:
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                        except:
                            pass
                else:
                    # Only text
                    if caption_parts:
                        try:
                            await bot.send_message(owner_id, header, parse_mode="HTML")
                        except Exception as e:
                            print(f"❌ Ошибка отправки текста: {e}")
                
                # Delete message from DB after sending notification
                await delete_message_from_db(owner_id, event.chat.id, msg_id)
                print(f"🗑️ Сообщение {msg_id} удалено из БД после отправки уведомления")
    
    print("=" * 60)
    print("MessageGuardian Multi-User Business Bot запущен")
    print("=" * 60)
    print(f"🔐 Пароль для доступа: {BOT_PASSWORD}")
    print(f"👮 Admin ID: {ADMIN_ID}")
    print(f"🗄️  База данных: {DB_NAME} @ {DB_HOST}:{DB_PORT}")
    print("=" * 60)
    print("Бот готов к работе!")
    print("Нажмите Ctrl+C для остановки")
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
