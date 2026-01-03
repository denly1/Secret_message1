import os
import asyncio
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BusinessMessagesDeleted, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, CallbackQuery, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, KeyboardButtonRequestUsers, UsersShared
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncpg
import io
import csv
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import defaultdict

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

# Track recent deletions for chat clear detection
recent_deletions = {}  # {chat_id: [(timestamp, count), ...]}

# FSM States for admin panel
class AdminStates(StatesGroup):
    waiting_broadcast_content = State()
    waiting_broadcast_confirm = State()
    waiting_grant_user_id = State()
    waiting_grant_days = State()
    waiting_revoke_user_id = State()
    waiting_check_user_id = State()
    waiting_add_admin_id = State()
    waiting_remove_admin_id = State()

# FSM States for duplicate command
class DuplicateStates(StatesGroup):
    waiting_contact = State()


async def init_db():
    """Initialize database connection pool"""
    global db_pool
    # Increased pool size for scalability (15000+ users)
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=10,  # Minimum connections
        max_size=50   # Maximum connections for high load
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


async def get_revenue_by_period(period: str) -> dict:
    """Get revenue statistics by period (day/week/month/year)"""
    async with db_pool.acquire() as conn:
        if period == "day":
            date_filter = "created_at >= NOW() - INTERVAL '1 day'"
        elif period == "week":
            date_filter = "created_at >= NOW() - INTERVAL '7 days'"
        elif period == "month":
            date_filter = "created_at >= NOW() - INTERVAL '30 days'"
        elif period == "year":
            date_filter = "created_at >= NOW() - INTERVAL '365 days'"
        else:
            date_filter = "TRUE"
        
        total = await conn.fetchval(
            f"SELECT COALESCE(SUM(amount), 0) FROM payment_history WHERE status = 'completed' AND {date_filter}"
        ) or 0
        
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM payment_history WHERE status = 'completed' AND {date_filter}"
        ) or 0
        
        return {"total_stars": total, "total_payments": count, "period": period}


async def get_users_stats() -> dict:
    """Get detailed users statistics"""
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        active_subs = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE is_active = TRUE"
        ) or 0
        trial_users = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE subscription_type = 'trial' AND is_active = TRUE"
        ) or 0
        paid_users = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE subscription_type != 'trial' AND is_active = TRUE"
        ) or 0
        
        return {
            "total_users": total_users,
            "active_subscriptions": active_subs,
            "trial_users": trial_users,
            "paid_users": paid_users
        }


async def get_detailed_users_csv() -> str:
    """Generate compact CSV optimized for mobile viewing"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                u.user_id,
                u.username,
                u.first_name,
                u.created_at as registered_at,
                s.subscription_type,
                s.is_active,
                s.end_date,
                COALESCE(SUM(ph.amount), 0) as total_spent,
                COUNT(ph.payment_id) as payments_count,
                EXISTS(SELECT 1 FROM business_connections bc WHERE bc.user_id = u.user_id) as has_business_connection
            FROM users u
            LEFT JOIN subscriptions s ON u.user_id = s.user_id
            LEFT JOIN payment_history ph ON u.user_id = ph.user_id AND ph.status = 'completed'
            GROUP BY u.user_id, u.username, u.first_name, u.created_at, s.subscription_type, s.is_active, s.end_date
            ORDER BY total_spent DESC, u.created_at DESC
        """)
        
        # Calculate totals
        total_users = len(rows)
        total_revenue = sum(row['total_spent'] for row in rows)
        total_payments = sum(row['payments_count'] for row in rows)
        active_subs = sum(1 for row in rows if row['is_active'])
        connected_bots = sum(1 for row in rows if row['has_business_connection'])
        
        output = io.StringIO()
        writer = csv.writer(output, delimiter=',')  # Comma for mobile compatibility
        
        # Compact header
        writer.writerow(['MessageAssistant - Отчет', datetime.now().strftime("%d.%m.%Y %H:%M")])
        writer.writerow([])
        
        # Summary (compact)
        writer.writerow(['СТАТИСТИКА'])
        writer.writerow(['Пользователей', total_users])
        writer.writerow(['Активных', active_subs])
        writer.writerow(['Бот подключен', connected_bots])
        writer.writerow(['Прибыль ⭐', total_revenue])
        writer.writerow(['Платежей', total_payments])
        writer.writerow(['Средний чек', f'{total_revenue/total_payments:.1f}' if total_payments > 0 else '0'])
        writer.writerow([])
        
        # Compact user table (mobile-friendly columns)
        writer.writerow(['ID', 'Имя', 'Username', 'Подписка', 'Активна', 'Потрачено ', 'Платежей', 'Бот подключен'])
        
        for row in rows:
            writer.writerow([
                row['user_id'],
                row['first_name'] or 'N/A',
                f"@{row['username']}" if row['username'] else '-',
                row['subscription_type'] or 'trial',
                '✓' if row['is_active'] else '✗',
                row['total_spent'],
                row['payments_count'],
                '✅ Да' if row['has_business_connection'] else '❌ Нет'
            ])
        
        writer.writerow([])
        writer.writerow(['Всего записей:', total_users])
        
        return output.getvalue()


async def generate_revenue_chart() -> io.BytesIO:
    """Generate beautiful revenue chart with daily statistics"""
    async with db_pool.acquire() as conn:
        # Get revenue by day for last 30 days
        rows = await conn.fetch("""
            SELECT 
                DATE(created_at) as date,
                SUM(amount) as total,
                COUNT(*) as count
            FROM payment_history
            WHERE status = 'completed' AND created_at >= NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date
        """)
    
    if not rows:
        # Create empty chart
        fig, ax = plt.subplots(figsize=(14, 8), facecolor='#1a1a2e')
        ax.set_facecolor('#16213e')
        ax.text(0.5, 0.5, '📊 Нет данных за последние 30 дней\n\nКогда появятся платежи, здесь будут графики', 
                ha='center', va='center', fontsize=18, color='white', fontweight='bold')
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        dates = [row['date'] for row in rows]
        totals = [row['total'] for row in rows]
        counts = [row['count'] for row in rows]
        
        # Calculate totals for info
        total_revenue = sum(totals)
        total_payments = sum(counts)
        avg_payment = total_revenue / total_payments if total_payments > 0 else 0
        
        # Create figure with dark theme
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 12), facecolor='#1a1a2e')
        
        # Revenue chart
        ax1.set_facecolor('#16213e')
        bars = ax1.bar(dates, totals, color='#ffd700', alpha=0.9, edgecolor='#ffed4e', linewidth=2.5)
        
        # Add value labels on bars
        for bar in bars:
            height = bar.get_height()
            if height > 0:
                ax1.text(bar.get_x() + bar.get_width()/2., height,
                        f'{int(height)}⭐',
                        ha='center', va='bottom', color='#ffd700', fontsize=11, fontweight='bold')
        
        ax1.set_title(f'💰 ПРИБЫЛЬ ПО ДНЯМ (Всего: {total_revenue}⭐ за {len(dates)} дней)', 
                     fontsize=20, color='#ffd700', pad=25, fontweight='bold')
        ax1.set_xlabel('Дата', fontsize=14, color='white', fontweight='bold')
        ax1.set_ylabel('Звезды ⭐', fontsize=14, color='white', fontweight='bold')
        ax1.tick_params(colors='white', labelsize=11)
        ax1.grid(True, alpha=0.3, color='white', linestyle='--', linewidth=0.8)
        ax1.spines['bottom'].set_color('white')
        ax1.spines['left'].set_color('white')
        ax1.spines['top'].set_visible(False)
        ax1.spines['right'].set_visible(False)
        
        # Payments count chart
        ax2.set_facecolor('#16213e')
        line = ax2.plot(dates, counts, color='#00d4ff', marker='o', linewidth=4, 
                       markersize=10, markerfacecolor='#00d4ff', markeredgecolor='white', 
                       markeredgewidth=2.5, label=f'Платежей: {total_payments}')[0]
        ax2.fill_between(dates, counts, alpha=0.4, color='#00d4ff')
        
        # Add value labels on points
        for i, (date, count) in enumerate(zip(dates, counts)):
            if count > 0:
                ax2.text(date, count, f'{int(count)}',
                        ha='center', va='bottom', color='#00d4ff', fontsize=11, fontweight='bold')
        
        ax2.set_title(f'💳 КОЛИЧЕСТВО ПЛАТЕЖЕЙ (Средний чек: {avg_payment:.1f}⭐)', 
                     fontsize=20, color='#00d4ff', pad=25, fontweight='bold')
        ax2.set_xlabel('Дата', fontsize=14, color='white', fontweight='bold')
        ax2.set_ylabel('Количество платежей', fontsize=14, color='white', fontweight='bold')
        ax2.tick_params(colors='white', labelsize=11)
        ax2.grid(True, alpha=0.3, color='white', linestyle='--', linewidth=0.8)
        ax2.spines['bottom'].set_color('white')
        ax2.spines['left'].set_color('white')
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)
        ax2.legend(loc='upper left', fontsize=12, facecolor='#16213e', edgecolor='white', labelcolor='white')
        
        # Format dates
        for ax in [ax1, ax2]:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=11)
    
    plt.tight_layout()
    
    # Save to bytes
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#1a1a2e', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    return buf


async def generate_users_chart() -> io.BytesIO:
    """Generate beautiful users statistics chart"""
    async with db_pool.acquire() as conn:
        # Get user registrations by day for last 30 days
        reg_rows = await conn.fetch("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as count
            FROM users
            WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date
        """)
        
        # Get subscription types distribution
        sub_rows = await conn.fetch("""
            SELECT 
                subscription_type,
                COUNT(*) as count
            FROM subscriptions
            WHERE is_active = TRUE
            GROUP BY subscription_type
        """)
        
        # Get active/inactive counts
        active = await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE is_active = TRUE") or 0
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        inactive = total_users - active
    
    fig = plt.figure(figsize=(16, 12), facecolor='#1a1a2e')
    
    # Registration chart
    ax1 = plt.subplot(2, 2, (1, 2))
    ax1.set_facecolor('#16213e')
    
    if reg_rows:
        dates = [row['date'] for row in reg_rows]
        counts = [row['count'] for row in reg_rows]
        total_new = sum(counts)
        
        line = ax1.plot(dates, counts, color='#00ff88', marker='o', linewidth=4, 
                       markersize=10, markerfacecolor='#00ff88', markeredgecolor='white', 
                       markeredgewidth=2.5, label=f'Всего новых: {total_new}')[0]
        ax1.fill_between(dates, counts, alpha=0.4, color='#00ff88')
        
        # Add value labels on points
        for date, count in zip(dates, counts):
            if count > 0:
                ax1.text(date, count, f'{int(count)}',
                        ha='center', va='bottom', color='#00ff88', fontsize=11, fontweight='bold')
        
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=11)
        ax1.legend(loc='upper left', fontsize=12, facecolor='#16213e', edgecolor='white', labelcolor='white')
    
    ax1.set_title(f'👥 РЕГИСТРАЦИИ ПОЛЬЗОВАТЕЛЕЙ (Всего: {total_users} пользователей)', 
                 fontsize=18, color='#00ff88', pad=20, fontweight='bold')
    ax1.set_xlabel('Дата', fontsize=13, color='white', fontweight='bold')
    ax1.set_ylabel('Новых пользователей', fontsize=13, color='white', fontweight='bold')
    ax1.tick_params(colors='white', labelsize=11)
    ax1.grid(True, alpha=0.3, color='white', linestyle='--', linewidth=0.8)
    ax1.spines['bottom'].set_color('white')
    ax1.spines['left'].set_color('white')
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)
    
    # Subscription types pie chart
    ax2 = plt.subplot(2, 2, 3)
    ax2.set_facecolor('#16213e')
    
    if sub_rows:
        labels = []
        sizes = []
        for row in sub_rows:
            sub_type = row['subscription_type']
            count = row['count']
            labels.append(f"{sub_type}\n({count} чел.)")
            sizes.append(count)
        
        colors = ['#ffd700', '#00d4ff', '#ff6b6b', '#4ecdc4', '#95e1d3']
        
        wedges, texts, autotexts = ax2.pie(sizes, labels=labels, autopct='%1.1f%%', 
                                            colors=colors, startangle=90,
                                            textprops={'color': 'white', 'fontsize': 12, 'fontweight': 'bold'},
                                            explode=[0.05] * len(sizes))
        for autotext in autotexts:
            autotext.set_color('black')
            autotext.set_fontweight('bold')
            autotext.set_fontsize(13)
    else:
        ax2.text(0.5, 0.5, 'Нет активных\nподписок', 
                ha='center', va='center', fontsize=14, color='white', fontweight='bold')
    
    ax2.set_title('📊 ТИПЫ ПОДПИСОК', fontsize=16, color='white', pad=15, fontweight='bold')
    
    # Active vs Inactive users
    ax3 = plt.subplot(2, 2, 4)
    ax3.set_facecolor('#16213e')
    
    categories = ['✅ Активные\nподписки', '❌ Без\nподписки']
    values = [active, inactive]
    colors_bar = ['#00ff88', '#ff6b6b']
    
    bars = ax3.bar(categories, values, color=colors_bar, alpha=0.9, edgecolor='white', linewidth=2.5, width=0.6)
    ax3.set_title('✅ СТАТУС ПОЛЬЗОВАТЕЛЕЙ', fontsize=16, color='white', pad=15, fontweight='bold')
    ax3.set_ylabel('Количество пользователей', fontsize=13, color='white', fontweight='bold')
    ax3.tick_params(colors='white', labelsize=11)
    ax3.grid(True, alpha=0.3, color='white', axis='y', linestyle='--', linewidth=0.8)
    ax3.spines['bottom'].set_color('white')
    ax3.spines['left'].set_color('white')
    ax3.spines['top'].set_visible(False)
    ax3.spines['right'].set_visible(False)
    
    # Add value labels on bars with percentage
    for bar, val in zip(bars, values):
        height = bar.get_height()
        percentage = (val / total_users * 100) if total_users > 0 else 0
        ax3.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(height)}\n({percentage:.1f}%)',
                ha='center', va='bottom', color='white', fontsize=13, fontweight='bold')
    
    plt.tight_layout()
    
    # Save to bytes
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#1a1a2e', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    return buf


# ==================== END ADMIN FUNCTIONS ====================


# ==================== REFERRAL FUNCTIONS ====================

async def create_referral(referrer_id: int, referred_id: int) -> bool:
    """Create referral link between users"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO referrals (referrer_id, referred_id, used)
                VALUES ($1, $2, FALSE)
                ON CONFLICT (referred_id) DO NOTHING
                """,
                referrer_id, referred_id
            )
            return True
    except:
        return False


async def check_referral_used(user_id: int) -> bool:
    """Check if user already used referral bonus"""
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM referrals WHERE referred_id = $1)",
            user_id
        )
        return result or False


async def mark_referral_used(referred_id: int) -> None:
    """Mark referral as used"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE referrals SET used = TRUE WHERE referred_id = $1",
            referred_id
        )


async def get_referral_count(user_id: int) -> int:
    """Get count of successful referrals"""
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = $1 AND used = TRUE",
            user_id
        )
        return count or 0


# ==================== END REFERRAL FUNCTIONS ====================


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


async def export_chat_via_api(owner_id: int, target_user_id: int, chat_name: str) -> str:
    """Export chat history by fetching messages from Telegram API (not from DB)"""
    print(f"📦 Начинаю экспорт чата через API для owner={owner_id}, target_user={target_user_id}")
    
    # Find chat_id where target_user_id is the chat_id itself (private chat)
    # In Telegram, private chat_id equals user_id
    chat_id = target_user_id
    
    async with db_pool.acquire() as conn:
        # Check if we have any messages from this chat
        message_count = await conn.fetchval(
            """
            SELECT COUNT(*) 
            FROM messages 
            WHERE owner_id = $1 AND chat_id = $2
            """,
            owner_id, chat_id
        )
        
        if message_count == 0:
            print(f"⚠️ Нет сообщений в БД для owner={owner_id}, chat_id={chat_id}")
            return None
        
        print(f"📦 Найдено {message_count} сообщений для chat_id={chat_id}")
        
        # Get ALL messages from DB (includes deleted and edited)
        messages = await conn.fetch(
            """
            SELECT message_id, user_id, text, caption, media_type, file_path, created_at
            FROM messages
            WHERE owner_id = $1 AND chat_id = $2
            ORDER BY created_at DESC
            """,
            owner_id, chat_id
        )
        
        # Reverse to show oldest first
        messages = list(reversed(messages))
    
    print(f"📦 Найдено сообщений в БД: {len(messages)}")
    
    if not messages:
        print(f"⚠️ Нет сообщений для экспорта")
        return None
    
    # Create HTML file
    html_content = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Экспорт чата - {chat_name}</title>
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
            padding: 20px;
            text-align: center;
            border-bottom: 2px solid #30363d;
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .chat-header h1 {{
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 8px;
        }}
        .chat-header p {{
            color: #8b949e;
            font-size: 14px;
        }}
        .messages {{
            padding: 20px;
        }}
        .message {{
            margin-bottom: 16px;
            padding: 12px 16px;
            background: #161b22;
            border-radius: 8px;
            border-left: 3px solid #58a6ff;
        }}
        .message-header {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            font-size: 13px;
        }}
        .message-sender {{
            color: #58a6ff;
            font-weight: 600;
        }}
        .message-time {{
            color: #8b949e;
        }}
        .message-content {{
            color: #c9d1d9;
            line-height: 1.5;
            word-wrap: break-word;
        }}
        .message-media {{
            margin-top: 8px;
            padding: 8px;
            background: #0d1117;
            border-radius: 4px;
            color: #58a6ff;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="chat-header">
            <h1>💬 Экспорт чата</h1>
            <p>{chat_name} • {len(messages)} сообщений • {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
        </div>
        <div class="messages">
"""
    
    for msg in messages:
        sender = "Вы" if msg['user_id'] == owner_id else chat_name
        timestamp = msg['created_at'].strftime('%d.%m.%Y %H:%M')
        text = msg['text'] or msg['caption'] or ""
        media_info = ""
        
        if msg['media_type']:
            media_types = {
                'photo': '📷 Фото',
                'video': '🎥 Видео',
                'document': '📄 Документ',
                'sticker': '🎭 Стикер',
                'voice': '🎤 Голосовое',
                'video_note': '🎬 Видеосообщение',
                'animation': '🎞 GIF'
            }
            media_info = f'<div class="message-media">{media_types.get(msg["media_type"], "📎 Медиа")}</div>'
        
        html_content += f"""
            <div class="message">
                <div class="message-header">
                    <span class="message-sender">{sender}</span>
                    <span class="message-time">{timestamp}</span>
                </div>
                <div class="message-content">{text if text else '<i>Медиа без подписи</i>'}</div>
                {media_info}
            </div>
"""
    
    html_content += """
        </div>
    </div>
</body>
</html>
"""
    
    # Save to file
    filename = f"chat_export_{owner_id}_{target_user_id}_{int(datetime.now().timestamp())}.html"
    filepath = Path("saved_media") / filename
    filepath.parent.mkdir(exist_ok=True)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"✅ HTML-файл создан: {filepath}")
    return str(filepath)


async def create_chat_html_backup(owner_id: int, chat_id: int, chat_name: str, limit: int = None) -> str:
    """Create HTML backup of chat history with optional message limit"""
    print(f"📦 Начинаю создание HTML-копии для чата {chat_id}, owner {owner_id}, limit={limit}")
    
    async with db_pool.acquire() as conn:
        if limit:
            # Get last N messages
            messages = await conn.fetch(
                """
                SELECT message_id, user_id, text, caption, media_type, file_path, created_at
                FROM messages
                WHERE owner_id = $1 AND chat_id = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                owner_id, chat_id, limit
            )
            # Reverse to show oldest first
            messages = list(reversed(messages))
        else:
            # Get all messages
            messages = await conn.fetch(
                """
                SELECT message_id, user_id, text, caption, media_type, file_path, created_at
                FROM messages
                WHERE owner_id = $1 AND chat_id = $2
                ORDER BY created_at ASC
                """,
                owner_id, chat_id
            )
    
    print(f"📦 Найдено сообщений в БД: {len(messages)}")
    
    if not messages:
        print(f"⚠️ Нет сообщений для создания HTML-копии")
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
            <div>MessageAssistant • Резервная копия чата</div>
            <div class="stats-badge">Всего сообщений: {len(messages)}</div>
        </div>
    </div>
</body>
</html>
"""
    
    # Save HTML file
    # Create saved_media directory if it doesn't exist
    import os
    os.makedirs("saved_media", exist_ok=True)
    
    filename = f"saved_media/chat_backup_{chat_id}_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"✅ HTML файл создан: {filename}")
        return filename
    except Exception as e:
        print(f"❌ Ошибка создания HTML файла: {e}")
        return None


async def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not bot_token:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не указан в .env")
        return
    
    await init_db()
    bot = Bot(token=bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        user_id = message.from_user.id
        username = message.from_user.username or "Unknown"
        first_name = message.from_user.first_name or "User"
        
        # Check for referral code in /start command
        referrer_id = None
        if len(message.text.split()) > 1:
            try:
                referrer_id = int(message.text.split()[1])
            except:
                pass
        
        # Auto-authenticate user
        is_new_user = not await is_user_authenticated(user_id)
        if is_new_user:
            await authenticate_user(user_id, username, first_name)
            # Create trial subscription for new user
            await create_trial_subscription(user_id)
            
            # Process referral if exists
            if referrer_id and referrer_id != user_id:
                # Check if this user hasn't used referral before
                if not await check_referral_used(user_id):
                    await create_referral(referrer_id, user_id)
                    # Give bonus to new user
                    await extend_subscription(user_id, "referral_bonus", 7)
                    await mark_referral_used(user_id)
                    
                    # Notify referrer
                    try:
                        await bot.send_message(
                            referrer_id,
                            "🎉 <b>Поздравляем!</b>\n\n"
                            f"По вашей реферальной ссылке зарегистрировался новый пользователь!\n"
                            "✅ Вам начислено +7 дней подписки",
                            parse_mode="HTML"
                        )
                    except:
                        pass
        
        # Check subscription status
        sub_status = await check_subscription(user_id)
        stats = await get_stats(user_id)
        
        # Build keyboard
        keyboard_buttons = [
            [InlineKeyboardButton(text="📚 Инструкция по подключению", url="https://t.me/MessageAssistant/4")],
            [InlineKeyboardButton(text="📖 Инструкция по использованию", url="https://t.me/MessageAssistant/5")]
        ]
        
        # Only show subscription button if trial expired
        if not sub_status['active']:
            keyboard_buttons.append([InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        # Build message text - hide subscription info during trial
        caption_text = "<b>👋 Добро пожаловать!</b>\n\n"
        caption_text += "Этот бот создан для сохранения всех деталей переписки, "
        caption_text += "даже в случае их изменения или удаления 🤫\n\n"
        
        # Show subscription info only if NOT in trial OR if expired
        if sub_status['type'] != 'trial' or not sub_status['active']:
            if sub_status['active']:
                caption_text += f"✅ <b>Подписка активна</b>\n📅 Осталось дней: <b>{sub_status['days_left']}</b>\n\n"
            else:
                # Trial expired - show subscription offer with referral link
                bot_username = (await bot.get_me()).username
                ref_link = f"https://t.me/{bot_username}?start={user_id}"
                caption_text += "😢 <b>Пробный период закончился</b>\n\n"
                caption_text += "💳 Можете приобрести подписку\n"
                caption_text += f"🎁 Или пригласите друга и получите +7 дней бесплатно!\n\n"
                caption_text += f"🔗 Ваша реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        
        caption_text += f"📊 <b>Статистика:</b>\n"
        caption_text += f"📨 Сообщений: <b>{stats['messages']}</b>\n"
        caption_text += f"✏️ Изменений: <b>{stats['edits']}</b>\n"
        caption_text += f"🗑 Удалений: <b>{stats['deletes']}</b>\n\n"
        caption_text += f"<b>Доступные команды:</b>\n"
        caption_text += f"/stats - показать статистику\n"
        caption_text += f"/help - справка\n"
        caption_text += f"/duplicate - дубликат чата"
        
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
    
    
    @dp.message(Command("premium"))
    async def cmd_premium(message: Message):
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        # Check current subscription
        sub_status = await check_subscription(user_id)
        
        if sub_status['active']:
            await message.answer(
                f"✅ <b>У вас уже есть активная подписка!</b>\n\n"
                f"📅 Тип: <b>{sub_status['type']}</b>\n"
                f"⏰ Осталось дней: <b>{sub_status['days_left']}</b>",
                parse_mode="HTML"
            )
            return
        
        # Show subscription offer
        bot_username = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        
        text = (
            "😔 <b>Ваш пробный период использования бота подошел к концу</b>\n\n"
            "😊 Пожалуйста, подключите Premium-статус, либо пригласите хотя бы 1 пользователя с Telegram Premium для продления пробного периода\n\n"
            "👑 <b>Подключить Premium-статус:</b>\n"
            "➡️ Нажмите кнопку ниже\n\n"
            "🎁 <b>Для приглашения:</b>\n"
            "➡️ Отправьте эту ссылку своим друзьям и знакомым:\n"
            f"👉 <code>{ref_link}</code>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Подключить Premium", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    
    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        stats = await get_stats(user_id)
        await message.answer(
            f"📊 <b>Ваша статистика MessageAssistant</b>\n\n"
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
            "📖 <b>Инструкция MessageAssistant</b>\n\n"
            "🤖 <b>Что делает бот:</b>\n"
            "• Сохраняет все удалённые сообщения\n"
            "• Отслеживает изменения в сообщениях\n"
            "• Сохраняет View Once фото/видео\n"
            "• Создаёт HTML-копию при очистке чата\n\n"
            "🔧 <b>Как подключить:</b>\n"
            "1. Откройте Настройки → Telegram Business\n"
            "2. Раздел 'Чаты' → 'Подключить бота'\n"
            "3. Найдите @MessageAssistantBot_bot\n"
            "4. Выберите 'Все личные чаты'\n\n"
            "💡 <b>Как сохранить View Once медиа:</b>\n"
            "• Ответьте на исчезающее фото/видео\n"
            "• Бот автоматически сохранит его\n"
            "• Вы получите уведомление с медиа\n\n"
            "📊 <b>Команды:</b>\n"
            "/start - главное меню\n"
            "/stats - статистика сообщений\n"
            "/help - эта инструкция\n"
            "/duplicate - экспорт полной переписки с пользователем\n\n"
            "⚠️ <b>Важно:</b>\n"
            "Бот работает только с вашими бизнес-чатами и автоматически удаляет данные из БД после отправки уведомления.",
            parse_mode="HTML"
        )
    
    @dp.message(Command("duplicate"))
    async def cmd_duplicate(message: Message, state: FSMContext):
        user_id = message.from_user.id
        
        if not await is_user_authenticated(user_id):
            await message.answer("🔐 Сначала авторизуйтесь: /start")
            return
        
        # Create keyboard with user selection button
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Выбрать чат с пользователем", request_users=KeyboardButtonRequestUsers(request_id=1, user_is_bot=False))]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        
        await state.set_state(DuplicateStates.waiting_contact)
        await message.answer(
            "📋 <b>Экспорт переписки</b>\n\n"
            "Нажмите кнопку ниже и выберите пользователя, чью переписку вы хотите экспортировать.\n\n"
            "📄 Бот выгрузит ВСЕ сохранённые сообщения из чата (включая удалённые и изменённые) и создаст HTML-файл.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.message(DuplicateStates.waiting_contact, F.users_shared)
    async def process_duplicate_user_shared(message: Message, state: FSMContext):
        print(f"🔍 DUPLICATE: Получено users_shared событие")
        print(f"🔍 DUPLICATE: message.users_shared = {message.users_shared}")
        print(f"🔍 DUPLICATE: Тип message = {type(message)}")
        
        user_id = message.from_user.id
        
        # Get selected user ID
        if not message.users_shared or not message.users_shared.user_ids:
            print(f"❌ DUPLICATE: users_shared пустой или нет user_ids")
            await message.answer(
                "❌ Не удалось получить информацию о пользователе. Попробуйте снова.",
                reply_markup=ReplyKeyboardRemove()
            )
            await state.clear()
            return
        
        selected_user_id = message.users_shared.user_ids[0]
        print(f"✅ DUPLICATE: Выбран пользователь {selected_user_id}")
        await state.clear()
        
        # Remove keyboard
        status_msg = await message.answer(
            "⏳ <b>Получаю информацию о пользователе...</b>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
        
        # Get user info
        try:
            print(f"🔍 DUPLICATE: Получаю информацию о пользователе {selected_user_id}")
            user_info = await bot.get_chat(selected_user_id)
            chat_name = user_info.first_name or "Unknown"
            if user_info.last_name:
                chat_name += f" {user_info.last_name}"
            print(f"✅ DUPLICATE: Имя пользователя: {chat_name}")
        except Exception as e:
            print(f"❌ DUPLICATE: Ошибка получения инфо: {e}")
            import traceback
            traceback.print_exc()
            chat_name = f"User {selected_user_id}"
        
        # Delete status message before long operation to avoid timeout
        try:
            print(f"🔍 DUPLICATE: Удаляю статусное сообщение перед экспортом")
            await status_msg.delete()
        except Exception as e:
            print(f"⚠️ DUPLICATE: Не удалось удалить статусное сообщение: {e}")
        
        # Send new message about export
        try:
            export_msg = await message.answer(
                f"⏳ <b>Экспортирую переписку с {chat_name}...</b>\n\n"
                "🔍 Выгружаю ВСЕ сохранённые сообщения из чата...\n"
                "⏳ Это может занять несколько минут...",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"⚠️ DUPLICATE: Не удалось отправить сообщение об экспорте: {e}")
            export_msg = None
        
        # Export chat history via Telegram API
        try:
            print(f"🔍 DUPLICATE: Вызываю export_chat_via_api для owner={user_id}, target={selected_user_id}")
            html_file = await export_chat_via_api(user_id, selected_user_id, chat_name)
            print(f"🔍 DUPLICATE: export_chat_via_api вернул: {html_file}")
            
            if not html_file:
                error_text = (
                    f"❌ <b>Чат с {chat_name} не найден</b>\n\n"
                    "📭 В базе данных нет сохранённых сообщений с этим пользователем.\n\n"
                    "💡 Возможно, бот ещё не начал сохранять сообщения из этого чата."
                )
                if export_msg:
                    try:
                        await export_msg.edit_text(error_text, parse_mode="HTML")
                    except:
                        await message.answer(error_text, parse_mode="HTML")
                else:
                    await message.answer(error_text, parse_mode="HTML")
                return
            
            if html_file and Path(html_file).exists():
                await bot.send_document(
                    user_id,
                    FSInputFile(html_file),
                    caption=f"📋 <b>Полная переписка с {chat_name}</b>\n\n"
                            f"📄 ВСЕ сохранённые сообщения из чата\n"
                            f"Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="HTML"
                )
                
                # Delete export message
                if export_msg:
                    try:
                        await export_msg.delete()
                    except:
                        pass
                
                # Clean up file
                try:
                    Path(html_file).unlink()
                except:
                    pass
            else:
                error_text = "❌ Не удалось создать HTML-файл. Попробуйте позже."
                if export_msg:
                    try:
                        await export_msg.edit_text(error_text, parse_mode="HTML")
                    except:
                        await message.answer(error_text, parse_mode="HTML")
                else:
                    await message.answer(error_text, parse_mode="HTML")
        except Exception as e:
            print(f"❌ DUPLICATE: Ошибка экспорта: {e}")
            import traceback
            traceback.print_exc()
            error_text = (
                f"❌ <b>Ошибка при экспорте</b>\n\n"
                f"Произошла ошибка: {str(e)}\n\n"
                "Попробуйте позже."
            )
            try:
                if export_msg:
                    await export_msg.edit_text(error_text, parse_mode="HTML")
                else:
                    await message.answer(error_text, parse_mode="HTML")
            except:
                pass
    
    @dp.message(Command("admin"))
    async def cmd_admin(message: Message):
        user_id = message.from_user.id
        
        if not await is_admin(user_id):
            return
        
        is_super = await is_super_admin(user_id)
        
        # Get stats
        users_stats = await get_users_stats()
        revenue = await get_revenue_stats()
        
        text = "👮 <b>Админ-панель MessageAssistant</b>\n\n"
        text += f"👥 Всего пользователей: <b>{users_stats['total_users']}</b>\n"
        text += f"✅ Активных подписок: <b>{users_stats['active_subscriptions']}</b>\n"
        text += f"🆓 Пробных: <b>{users_stats['trial_users']}</b>\n"
        text += f"💰 Платных: <b>{users_stats['paid_users']}</b>\n\n"
        text += f"💸 Общая прибыль: <b>{revenue['total_stars']} ⭐</b>\n"
        text += f"💳 Всего платежей: <b>{revenue['total_payments']}</b>\n\n"
        text += "Выберите действие:"
        
        # Build keyboard with buttons
        keyboard_buttons = [
            [InlineKeyboardButton(text="📊 Статистика прибыли", callback_data="admin_revenue")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="👥 Управление подписками", callback_data="admin_subscriptions")],
            [InlineKeyboardButton(text="📥 Выгрузить CSV", callback_data="admin_export_csv")],
            [InlineKeyboardButton(text="💬 Выгрузка переписок", callback_data="admin_export_chats")],
            [InlineKeyboardButton(text="💾 ПАМЯТЬ БОТА", callback_data="admin_db_memory")]
        ]
        
        if is_super:
            keyboard_buttons.append([InlineKeyboardButton(text="👑 Управление админами", callback_data="admin_manage_admins")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    
    # ==================== SUBSCRIPTION CALLBACKS ====================
    
    @dp.callback_query(F.data == "show_instructions")
    async def callback_show_instructions(callback):
        """Show usage instructions"""
        text = (
            "📖 <b>Инструкция по использованию MessageAssistant</b>\n\n"
            
            "<b>🔔 Уведомления об удалённых сообщениях:</b>\n"
            "• Когда собеседник удаляет сообщение, вы получите уведомление с текстом и медиа\n"
            "• Поддерживаются: фото, видео, документы, стикеры, голосовые, видео-кружки\n\n"
            
            "<b>✏️ Уведомления об изменённых сообщениях:</b>\n"
            "• Видите старую и новую версию сообщения\n"
            "• Отслеживаются все правки текста\n\n"
            
            "<b>🔒 Исчезающие фото и видео:</b>\n"
            "• View Once медиа автоматически сохраняются\n"
            "• Вы получите копию даже после просмотра\n\n"
            
            "<b>📦 Очистка чата:</b>\n"
            "• При массовом удалении создаётся HTML-архив переписки\n"
            "• Все медиа встроены в файл\n\n"
            
            "<b>🎁 Реферальная программа:</b>\n"
            "• Пригласите друга - получите +7 дней подписки\n"
            "• Ваша ссылка доступна в /start\n\n"
            
            "<b>📊 Команды:</b>\n"
            "/start - главное меню\n"
            "/stats - статистика\n"
            "/help - справка\n"
            "/admin - панель администратора (для админов)\n\n"
            
            "💡 <b>Важно:</b> Бот работает только с Telegram Business аккаунтами"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data == "buy_subscription")
    async def callback_buy_subscription(callback):
        """Show subscription options"""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Неделя - 50 звёзд", callback_data="sub_week")],
            [InlineKeyboardButton(text="⭐ Месяц - 100 звёзд", callback_data="sub_month")],
            [InlineKeyboardButton(text="⭐ Год - 550 звёзд", callback_data="sub_year")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        text = (
            "💳 <b>Выберите подписку:</b>\n\n"
            "⭐ <b>Неделя</b> - 50 звёзд (7 дней)\n"
            "⭐ <b>Месяц</b> - 100 звёзд (30 дней)\n"
            "⭐ <b>Год</b> - 550 звёзд (365 дней)\n\n"
            "💡 Оплата через Telegram Stars\n"
            "💰 При повторной оплате дни прибавляются к текущей подписке"
        )
        
        # Delete original message and send new one
        try:
            await callback.message.delete()
        except:
            pass
        
        await bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data.startswith("view_edit_"))
    async def callback_view_edit(callback: CallbackQuery):
        """Show subscription offer when trying to view edited message"""
        bot_username = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
        
        text = (
            "😔 <b>Ваш пробный период использования бота подошел к концу</b>\n\n"
            "😊 Пожалуйста, подключите Premium-статус, либо пригласите хотя бы 1 пользователя с Telegram Premium для продления пробного периода\n\n"
            "👑 <b>Подключить Premium-статус:</b>\n"
            "➡️ Нажмите кнопку ниже\n\n"
            "🎁 <b>Для приглашения:</b>\n"
            "➡️ Отправьте эту ссылку своим друзьям и знакомым:\n"
            f"👉 <code>{ref_link}</code>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Подключить Premium", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data.startswith("view_delete_"))
    async def callback_view_delete(callback: CallbackQuery):
        """Show subscription offer when trying to view deleted message"""
        bot_username = (await bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
        
        text = (
            "😔 <b>Ваш пробный период использования бота подошел к концу</b>\n\n"
            "😊 Пожалуйста, подключите Premium-статус, либо пригласите хотя бы 1 пользователя с Telegram Premium для продления пробного периода\n\n"
            "👑 <b>Подключить Premium-статус:</b>\n"
            "➡️ Нажмите кнопку ниже\n\n"
            "🎁 <b>Для приглашения:</b>\n"
            "➡️ Отправьте эту ссылку своим друзьям и знакомым:\n"
            f"👉 <code>{ref_link}</code>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Подключить Premium", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
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
    async def callback_process_subscription(callback: CallbackQuery):
        """Process subscription purchase"""
        sub_type = callback.data.split("_")[1]
        user_id = callback.from_user.id
        
        # Define prices and names
        prices = {"week": 50, "month": 100, "year": 550}
        names = {"week": "Неделя", "month": "Месяц", "year": "Год"}
        
        if sub_type not in prices:
            await callback.answer("❌ Неверный тип подписки")
            return
        
        amount = prices[sub_type]
        name = names[sub_type]
        
        # Create invoice
        await bot.send_invoice(
            chat_id=user_id,
            title=f"Подписка MessageAssistant - {name}",
            description=f"Подписка на бота",
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
            days_map = {"week": 7, "month": 30, "year": 365}
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
    
    # ==================== ADMIN PANEL CALLBACKS ====================
    
    @dp.callback_query(F.data == "admin_revenue")
    async def callback_admin_revenue(callback: CallbackQuery):
        """Show revenue statistics with beautiful charts"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        await callback.answer("⏳ Генерирую графики...")
        
        # Get statistics
        day_stats = await get_revenue_by_period("day")
        week_stats = await get_revenue_by_period("week")
        month_stats = await get_revenue_by_period("month")
        year_stats = await get_revenue_by_period("year")
        
        text = "📊 <b>Статистика прибыли</b>\n\n"
        text += f"📅 <b>За день:</b> {day_stats['total_stars']} ⭐ ({day_stats['total_payments']} платежей)\n"
        text += f"📅 <b>За неделю:</b> {week_stats['total_stars']} ⭐ ({week_stats['total_payments']} платежей)\n"
        text += f"📅 <b>За месяц:</b> {month_stats['total_stars']} ⭐ ({month_stats['total_payments']} платежей)\n"
        text += f"📅 <b>За год:</b> {year_stats['total_stars']} ⭐ ({year_stats['total_payments']} платежей)\n\n"
        
        if month_stats['total_payments'] > 0:
            avg = month_stats['total_stars'] / month_stats['total_payments']
            text += f"📈 <b>Средний чек (месяц):</b> {avg:.1f} ⭐\n\n"
        
        text += "📈 Графики прибыли отправлены ниже"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Статистика пользователей", callback_data="admin_users_stats")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
        
        # Generate and send revenue chart
        revenue_chart = await generate_revenue_chart()
        revenue_photo = BufferedInputFile(revenue_chart.read(), filename="revenue_chart.png")
        
        await callback.message.delete()
        await bot.send_photo(
            callback.from_user.id,
            revenue_photo,
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.callback_query(F.data == "admin_users_stats")
    async def callback_admin_users_stats(callback: CallbackQuery):
        """Show users statistics with beautiful charts"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        await callback.answer("⏳ Генерирую графики...")
        
        # Get statistics
        users_stats = await get_users_stats()
        
        text = "👥 <b>Статистика пользователей</b>\n\n"
        text += f"👤 Всего пользователей: <b>{users_stats['total_users']}</b>\n"
        text += f"✅ Активных подписок: <b>{users_stats['active_subscriptions']}</b>\n"
        text += f"🆓 Пробных: <b>{users_stats['trial_users']}</b>\n"
        text += f"💎 Платных: <b>{users_stats['paid_users']}</b>\n\n"
        text += "📊 Детальные графики отправлены ниже"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Статистика прибыли", callback_data="admin_revenue")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
        
        # Generate and send users chart
        users_chart = await generate_users_chart()
        users_photo = BufferedInputFile(users_chart.read(), filename="users_chart.png")
        
        await callback.message.delete()
        await bot.send_photo(
            callback.from_user.id,
            users_photo,
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.callback_query(F.data == "admin_broadcast")
    async def callback_admin_broadcast(callback: CallbackQuery, state: FSMContext):
        """Start broadcast process"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "📢 <b>Рассылка сообщений</b>\n\n"
        text += "Отправьте сообщение для рассылки.\n"
        text += "Можно отправить текст, фото или видео с подписью.\n\n"
        text += "После отправки вы увидите предпросмотр."
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_admin")]
        ])
        
        await state.set_state(AdminStates.waiting_broadcast_content)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data == "admin_subscriptions")
    async def callback_admin_subscriptions(callback: CallbackQuery):
        """Show subscription management menu"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "👥 <b>Управление подписками</b>\n\n"
        text += "Выберите действие:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выдать подписку", callback_data="admin_grant_sub")],
            [InlineKeyboardButton(text="❌ Забрать подписку", callback_data="admin_revoke_sub")],
            [InlineKeyboardButton(text="🔍 Проверить подписку", callback_data="admin_check_sub")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data == "admin_grant_sub")
    async def callback_admin_grant_sub(callback: CallbackQuery, state: FSMContext):
        """Start grant subscription process"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "✅ <b>Выдать подписку</b>\n\n"
        text += "Отправьте User ID пользователя:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_subscriptions")]
        ])
        
        await state.set_state(AdminStates.waiting_grant_user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.message(AdminStates.waiting_grant_user_id)
    async def process_grant_user_id(message: Message, state: FSMContext):
        """Process user ID for grant subscription"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            user_id = int(message.text.strip())
            await state.update_data(target_user_id=user_id)
            await state.set_state(AdminStates.waiting_grant_days)
            
            await message.answer(
                f"✅ User ID: <code>{user_id}</code>\n\n"
                "Теперь отправьте количество дней подписки:",
                parse_mode="HTML"
            )
        except:
            await message.answer("❌ Неверный формат. Отправьте числовой User ID.")
    
    @dp.message(AdminStates.waiting_grant_days)
    async def process_grant_days(message: Message, state: FSMContext):
        """Process days and grant subscription"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            days = int(message.text.strip())
            data = await state.get_data()
            target_user_id = data['target_user_id']
            
            await grant_subscription(target_user_id, "admin_grant", days)
            await state.clear()
            
            await message.answer(
                f"✅ <b>Подписка выдана!</b>\n\n"
                f"👤 User ID: <code>{target_user_id}</code>\n"
                f"📅 Дней: <b>{days}</b>",
                parse_mode="HTML"
            )
        except:
            await message.answer("❌ Неверный формат. Отправьте число дней.")
    
    @dp.callback_query(F.data == "admin_revoke_sub")
    async def callback_admin_revoke_sub(callback: CallbackQuery, state: FSMContext):
        """Start revoke subscription process"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "❌ <b>Забрать подписку</b>\n\n"
        text += "Отправьте User ID пользователя:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_subscriptions")]
        ])
        
        await state.set_state(AdminStates.waiting_revoke_user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.message(AdminStates.waiting_revoke_user_id)
    async def process_revoke_user_id(message: Message, state: FSMContext):
        """Process user ID for revoke subscription"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            user_id = int(message.text.strip())
            await revoke_subscription(user_id)
            await state.clear()
            
            await message.answer(
                f"❌ <b>Подписка отозвана!</b>\n\n"
                f"👤 User ID: <code>{user_id}</code>",
                parse_mode="HTML"
            )
        except:
            await message.answer("❌ Неверный формат. Отправьте числовой User ID.")
    
    @dp.callback_query(F.data == "admin_check_sub")
    async def callback_admin_check_sub(callback: CallbackQuery, state: FSMContext):
        """Check user subscription"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "🔍 <b>Проверить подписку</b>\n\n"
        text += "Отправьте User ID пользователя:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_subscriptions")]
        ])
        
        await state.set_state(AdminStates.waiting_check_user_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.message(AdminStates.waiting_check_user_id)
    async def process_check_user_id(message: Message, state: FSMContext):
        """Process user ID for check subscription"""
        if not await is_admin(message.from_user.id):
            return
        
        try:
            user_id = int(message.text.strip())
            sub_status = await check_subscription(user_id)
            await state.clear()
            
            if sub_status['active']:
                text = (
                    f"✅ <b>ПОДПИСКА АКТИВНА</b>\n\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"📦 Тип подписки: <b>{sub_status['type']}</b>\n"
                    f"📅 Осталось дней: <b>{sub_status['days_left']}</b>\n"
                    f"🗓 Дата окончания: <b>{sub_status['end_date'].strftime('%d.%m.%Y')}</b>\n\n"
                    f"✨ Подписка действует"
                )
            else:
                text = (
                    f"❌ <b>ПОДПИСКА НЕАКТИВНА</b>\n\n"
                    f"👤 User ID: <code>{user_id}</code>\n\n"
                    f"⚠️ У пользователя нет активной подписки"
                )
            
            await message.answer(text, parse_mode="HTML")
        except ValueError:
            await message.answer("❌ Неверный формат. Отправьте числовой User ID.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    
    @dp.callback_query(F.data == "admin_export_csv")
    async def callback_admin_export_csv(callback: CallbackQuery):
        """Export users to detailed CSV"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        await callback.answer("⏳ Генерирую CSV...")
        
        csv_content = await get_detailed_users_csv()
        csv_file = BufferedInputFile(
            csv_content.encode('utf-8-sig'),
            filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        
        await bot.send_document(
            callback.from_user.id,
            csv_file,
            caption="📊 <b>Детальный экспорт пользователей</b>",
            parse_mode="HTML"
        )
    
    @dp.callback_query(F.data == "admin_db_memory")
    async def callback_admin_db_memory(callback: CallbackQuery):
        """Show database memory usage statistics"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        await callback.answer("⏳ Получаю статистику БД...")
        
        async with db_pool.acquire() as conn:
            # Get database size
            db_size = await conn.fetchval(
                "SELECT pg_database_size(current_database())"
            )
            
            # Get table sizes
            tables_info = await conn.fetch(
                """
                SELECT 
                    schemaname,
                    tablename,
                    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size,
                    pg_total_relation_size(schemaname||'.'||tablename) AS size_bytes
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY size_bytes DESC
                """
            )
            
            # Get row counts
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            messages_count = await conn.fetchval("SELECT COUNT(*) FROM messages")
            subscriptions_count = await conn.fetchval("SELECT COUNT(*) FROM subscriptions")
            
            # Check if payments table exists
            payments_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'payments'
                )
                """
            )
            payments_count = await conn.fetchval("SELECT COUNT(*) FROM payments") if payments_exists else 0
            
            # Get media files size
            media_dir = Path("saved_media")
            media_size = 0
            media_files_count = 0
            if media_dir.exists():
                for file in media_dir.rglob("*"):
                    if file.is_file():
                        media_size += file.stat().st_size
                        media_files_count += 1
            
            # Get disk space
            import shutil
            disk_usage = shutil.disk_usage("/")
            disk_total = disk_usage.total
            disk_used = disk_usage.used
            disk_free = disk_usage.free
        
        # Format sizes
        def format_size(bytes_size):
            for unit in ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']:
                if bytes_size < 1024.0:
                    return f"{bytes_size:.2f} {unit}"
                bytes_size /= 1024.0
            return f"{bytes_size:.2f} ПБ"
        
        db_size_formatted = format_size(db_size)
        media_size_formatted = format_size(media_size)
        total_size = db_size + media_size
        total_size_formatted = format_size(total_size)
        
        disk_total_formatted = format_size(disk_total)
        disk_used_formatted = format_size(disk_used)
        disk_free_formatted = format_size(disk_free)
        disk_used_percent = (disk_used / disk_total) * 100
        
        text = "💾 <b>ПАМЯТЬ БОТА</b>\n\n"
        text += "🖥 <b>Диск сервера:</b>\n"
        text += f"💿 Всего: <b>{disk_total_formatted}</b>\n"
        text += f"📊 Занято: <b>{disk_used_formatted}</b> ({disk_used_percent:.1f}%)\n"
        text += f"✅ Свободно: <b>{disk_free_formatted}</b>\n\n"
        
        text += "📊 <b>Данные бота:</b>\n"
        text += f"💿 База данных: <b>{db_size_formatted}</b>\n"
        text += f"📁 Медиа файлы: <b>{media_size_formatted}</b> ({media_files_count} файлов)\n"
        text += f"📦 Всего занято ботом: <b>{total_size_formatted}</b>\n\n"
        
        text += "📋 <b>Записи в таблицах:</b>\n"
        text += f"👥 Пользователи: <b>{users_count:,}</b>\n"
        text += f"💬 Сообщения: <b>{messages_count:,}</b>\n"
        text += f"🎫 Подписки: <b>{subscriptions_count:,}</b>\n"
        text += f"💳 Платежи: <b>{payments_count:,}</b>\n\n"
        
        text += "📂 <b>Размеры таблиц:</b>\n"
        for table in tables_info[:5]:  # Show top 5 tables
            text += f"• {table['tablename']}: <b>{table['size']}</b>\n"
        
        text += f"\n⚙️ <b>Статус:</b> "
        if total_size < 1024**3:  # Less than 1 GB
            text += "✅ Отлично"
        elif total_size < 5 * 1024**3:  # Less than 5 GB
            text += "⚠️ Нормально"
        else:
            text += "🔴 Требуется внимание"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_db_memory")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    
    @dp.callback_query(F.data == "admin_export_chats")
    async def callback_admin_export_chats(callback: CallbackQuery):
        """Admin function to export other users' chats - page 1"""
        await callback_admin_export_chats_page(callback, page=0)
    
    @dp.callback_query(F.data.startswith("admin_export_chats_page_"))
    async def callback_admin_export_chats_paginated(callback: CallbackQuery):
        """Handle pagination for admin export chats"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        page = int(callback.data.split("_")[-1])
        await callback_admin_export_chats_page(callback, page)
    
    async def callback_admin_export_chats_page(callback: CallbackQuery, page: int = 0):
        """Show paginated list of users for chat export"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        await callback.answer()
        
        # Get list of all users with chats (excluding protected IDs)
        PROTECTED_IDS = [1812256281, 808581806, 825042510]
        USERS_PER_PAGE = 10
        offset = page * USERS_PER_PAGE
        
        async with db_pool.acquire() as conn:
            # Get total count
            total_users = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT u.user_id)
                FROM users u
                INNER JOIN messages m ON u.user_id = m.owner_id
                WHERE u.user_id != ALL($1)
                """,
                PROTECTED_IDS
            )
            
            # Get users for current page
            users = await conn.fetch(
                """
                SELECT DISTINCT u.user_id, u.first_name, u.username, COUNT(DISTINCT m.chat_id) as chats_count
                FROM users u
                INNER JOIN messages m ON u.user_id = m.owner_id
                WHERE u.user_id != ALL($1)
                GROUP BY u.user_id, u.first_name, u.username
                ORDER BY chats_count DESC
                LIMIT $2 OFFSET $3
                """,
                PROTECTED_IDS, USERS_PER_PAGE, offset
            )
        
        if not users:
            await callback.message.edit_text(
                "❌ Нет доступных пользователей для выгрузки.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
                ])
            )
            return
        
        # Create keyboard with user list
        keyboard_buttons = []
        for user in users:
            user_name = user['first_name'] or "Unknown"
            username = f"@{user['username']}" if user['username'] else ""
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"👤 {user_name} {username} ({user['chats_count']} чатов)",
                    callback_data=f"admin_export_user_{user['user_id']}"
                )
            ])
        
        # Add pagination buttons
        total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE
        nav_buttons = []
        
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_export_chats_page_{page-1}"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_export_chats_page_{page+1}"))
        
        if nav_buttons:
            keyboard_buttons.append(nav_buttons)
        
        keyboard_buttons.append([InlineKeyboardButton(text="◀️ В админ панель", callback_data="back_to_admin")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(
            f"💬 <b>Выгрузка переписок пользователей</b>\n\n"
            f"Страница {page + 1} из {total_pages}\n"
            f"Всего пользователей: {total_users}\n\n"
            "Выберите пользователя, чьи переписки хотите выгрузить:\n\n"
            "⚠️ <i>Защищённые аккаунты не отображаются</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.callback_query(F.data.startswith("admin_export_user_"))
    async def callback_admin_export_user(callback: CallbackQuery):
        """Export specific user's chats - page 1"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        user_id = int(callback.data.split("_")[3])
        await callback_admin_export_user_chats_page(callback, user_id, page=0)
    
    @dp.callback_query(F.data.startswith("admin_user_chats_"))
    async def callback_admin_user_chats_paginated(callback: CallbackQuery):
        """Handle pagination for user's chats"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        parts = callback.data.split("_")
        user_id = int(parts[3])
        page = int(parts[4])
        await callback_admin_export_user_chats_page(callback, user_id, page)
    
    async def callback_admin_export_user_chats_page(callback: CallbackQuery, user_id: int, page: int = 0):
        """Show paginated list of user's chats"""
        PROTECTED_IDS = [1812256281, 808581806, 825042510]
        
        # Double check protection
        if user_id in PROTECTED_IDS:
            await callback.answer("❌ Этот аккаунт защищён от выгрузки", show_alert=True)
            return
        
        await callback.answer("⏳ Получаю список чатов...")
        
        CHATS_PER_PAGE = 10
        offset = page * CHATS_PER_PAGE
        
        # Get total count and chats for this user
        async with db_pool.acquire() as conn:
            total_chats = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT m.chat_id)
                FROM messages m
                WHERE m.owner_id = $1 AND m.user_id != $1
                """,
                user_id
            )
            
            chats = await conn.fetch(
                """
                SELECT DISTINCT m.chat_id, m.user_id, COUNT(*) as msg_count
                FROM messages m
                WHERE m.owner_id = $1 AND m.user_id != $1
                GROUP BY m.chat_id, m.user_id
                ORDER BY msg_count DESC
                LIMIT $2 OFFSET $3
                """,
                user_id, CHATS_PER_PAGE, offset
            )
        
        if not chats:
            await callback.message.edit_text(
                "❌ У этого пользователя нет сохранённых чатов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_export_chats")]
                ])
            )
            return
        
        # Create keyboard with chat list
        keyboard_buttons = []
        for chat in chats:
            try:
                chat_info = await bot.get_chat(chat['chat_id'])
                chat_name = chat_info.first_name or "Unknown"
                if chat_info.last_name:
                    chat_name += f" {chat_info.last_name}"
            except:
                chat_name = f"Chat {chat['chat_id']}"
            
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"💬 {chat_name} ({chat['msg_count']} сооб.)",
                    callback_data=f"admin_dl_{user_id}_{chat['chat_id']}"
                )
            ])
        
        # Add pagination buttons
        total_pages = (total_chats + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE
        nav_buttons = []
        
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user_chats_{user_id}_{page-1}"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_user_chats_{user_id}_{page+1}"))
        
        if nav_buttons:
            keyboard_buttons.append(nav_buttons)
        
        keyboard_buttons.append([InlineKeyboardButton(text="◀️ К списку пользователей", callback_data="admin_export_chats")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(
            f"💬 <b>Чаты пользователя {user_id}</b>\n\n"
            f"Страница {page + 1} из {total_pages}\n"
            f"Всего чатов: {total_chats}\n\n"
            "Выберите чат для выгрузки:",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.callback_query(F.data.startswith("admin_dl_"))
    async def callback_admin_download_chat(callback: CallbackQuery):
        """Download specific chat as HTML"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        parts = callback.data.split("_")
        owner_id = int(parts[2])
        chat_id = int(parts[3])
        
        PROTECTED_IDS = [1812256281, 808581806, 825042510]
        
        # Triple check protection
        if owner_id in PROTECTED_IDS:
            await callback.answer("❌ Этот аккаунт защищён от выгрузки", show_alert=True)
            return
        
        await callback.answer("⏳ Создаю HTML-файл...")
        await callback.message.edit_text("⏳ <b>Создаю HTML-файл...</b>", parse_mode="HTML")
        
        # Get chat name
        try:
            chat_info = await bot.get_chat(chat_id)
            chat_name = chat_info.first_name or "Unknown"
            if chat_info.last_name:
                chat_name += f" {chat_info.last_name}"
        except:
            chat_name = f"Chat {chat_id}"
        
        # Create HTML backup
        try:
            html_file = await create_chat_html_backup(owner_id, chat_id, chat_name)
            
            if html_file and Path(html_file).exists():
                await bot.send_document(
                    callback.from_user.id,
                    FSInputFile(html_file),
                    caption=f"📋 <b>Переписка пользователя {owner_id}</b>\n\n"
                            f"💬 Чат: {chat_name}\n"
                            f"📄 ВСЕ сохранённые сообщения\n"
                            f"Экспортировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="HTML"
                )
                
                await callback.message.edit_text(
                    "✅ <b>HTML-файл успешно создан!</b>\n\n"
                    "📄 Файл отправлен вам в чат.",
                    parse_mode="HTML"
                )
                
                # Delete temp file
                try:
                    Path(html_file).unlink()
                except:
                    pass
            else:
                await callback.message.edit_text("❌ Ошибка при создании HTML-файла.")
        except Exception as e:
            print(f"❌ Ошибка экспорта чата: {e}")
            import traceback
            traceback.print_exc()
            await callback.message.edit_text(f"❌ Ошибка: {e}")
    
    @dp.callback_query(F.data == "back_to_admin")
    async def callback_back_to_admin(callback: CallbackQuery, state: FSMContext):
        """Return to admin panel"""
        await state.clear()
        
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        is_super = await is_super_admin(callback.from_user.id)
        users_stats = await get_users_stats()
        revenue = await get_revenue_stats()
        
        text = "👮 <b>Админ-панель MessageAssistant</b>\n\n"
        text += f"👥 Всего пользователей: <b>{users_stats['total_users']}</b>\n"
        text += f"✅ Активных подписок: <b>{users_stats['active_subscriptions']}</b>\n"
        text += f"🆓 Пробных: <b>{users_stats['trial_users']}</b>\n"
        text += f"💎 Платных: <b>{users_stats['paid_users']}</b>\n\n"
        text += f"💰 Общая прибыль: <b>{revenue['total_stars']} ⭐</b>\n"
        text += f"💳 Всего платежей: <b>{revenue['total_payments']}</b>\n\n"
        text += "Выберите действие:"
        
        keyboard_buttons = [
            [InlineKeyboardButton(text="📊 Статистика прибыли", callback_data="admin_revenue")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="👥 Управление подписками", callback_data="admin_subscriptions")],
            [InlineKeyboardButton(text="📥 Выгрузить CSV", callback_data="admin_export_csv")],
            [InlineKeyboardButton(text="💬 Выгрузка переписок", callback_data="admin_export_chats")],
            [InlineKeyboardButton(text="💾 ПАМЯТЬ БОТА", callback_data="admin_db_memory")]
        ]
        
        if is_super:
            keyboard_buttons.append([InlineKeyboardButton(text="👑 Управление админами", callback_data="admin_manage_admins")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        # Check if message has photo (from revenue stats)
        if callback.message.photo:
            # Delete photo message and send new text message
            await callback.message.delete()
            await bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=keyboard)
        else:
            # Edit text message normally
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        
        await callback.answer()
    
    @dp.callback_query(F.data == "admin_manage_admins")
    async def callback_admin_manage_admins(callback: CallbackQuery):
        """Manage admins (super admin only)"""
        if not await is_super_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        async with db_pool.acquire() as conn:
            admins = await conn.fetch(
                "SELECT user_id, username, first_name, is_super_admin, created_at FROM admins ORDER BY created_at DESC"
            )
        
        text = "👑 <b>Управление администраторами</b>\n\n"
        
        if admins:
            for admin in admins:
                super_badge = "👑" if admin['is_super_admin'] else "👮"
                text += f"{super_badge} <b>{admin['first_name']}</b> (@{admin['username'] or 'N/A'})\n"
                text += f"   ID: <code>{admin['user_id']}</code>\n\n"
        else:
            text += "<i>Нет администраторов</i>\n"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add_admin")],
            [InlineKeyboardButton(text="🗑 Удалить админа", callback_data="admin_remove_admin")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.callback_query(F.data == "admin_add_admin")
    async def callback_admin_add_admin(callback: CallbackQuery, state: FSMContext):
        """Start add admin process"""
        if not await is_super_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "➕ <b>Добавить администратора</b>\n\n"
        text += "Отправьте User ID пользователя, которого хотите сделать админом:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_manage_admins")]
        ])
        
        await state.set_state(AdminStates.waiting_add_admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.message(AdminStates.waiting_add_admin_id)
    async def process_add_admin_id(message: Message, state: FSMContext):
        """Process admin ID and add to database"""
        if not await is_super_admin(message.from_user.id):
            return
        
        try:
            admin_id = int(message.text.strip())
            
            # Check if already admin
            async with db_pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT user_id FROM admins WHERE user_id = $1",
                    admin_id
                )
                
                if existing:
                    await message.answer(
                        "⚠️ <b>Этот пользователь уже является админом!</b>",
                        parse_mode="HTML"
                    )
                    await state.clear()
                    return
                
                # Get user info if exists
                user_info = await conn.fetchrow(
                    "SELECT username, first_name FROM users WHERE user_id = $1",
                    admin_id
                )
                
                username = user_info['username'] if user_info else 'unknown'
                first_name = user_info['first_name'] if user_info else 'New Admin'
                
                # Add admin
                await conn.execute(
                    """INSERT INTO admins (user_id, username, first_name, added_by, is_super_admin)
                       VALUES ($1, $2, $3, $4, FALSE)""",
                    admin_id, username, first_name, message.from_user.id
                )
            
            await message.answer(
                f"✅ <b>Админ добавлен!</b>\n\n"
                f"👤 User ID: <code>{admin_id}</code>\n"
                f"📝 Имя: {first_name}\n"
                f"🔗 Username: @{username}",
                parse_mode="HTML"
            )
            await state.clear()
            
        except ValueError:
            await message.answer(
                "❌ <b>Ошибка!</b>\n\n"
                "Отправьте корректный User ID (число)",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"❌ Ошибка добавления админа: {e}")
            await message.answer(
                "❌ <b>Ошибка при добавлении админа</b>\n\n"
                f"Попробуйте еще раз",
                parse_mode="HTML"
            )
            await state.clear()
    
    @dp.callback_query(F.data == "admin_remove_admin")
    async def callback_admin_remove_admin(callback: CallbackQuery, state: FSMContext):
        """Start remove admin process"""
        if not await is_super_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        text = "🗑 <b>Удалить администратора</b>\n\n"
        text += "Отправьте User ID администратора, которого хотите удалить:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_manage_admins")]
        ])
        
        await state.set_state(AdminStates.waiting_remove_admin_id)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
    
    @dp.message(AdminStates.waiting_remove_admin_id)
    async def process_remove_admin_id(message: Message, state: FSMContext):
        """Process admin ID and remove from database"""
        if not await is_super_admin(message.from_user.id):
            return
        
        try:
            admin_id = int(message.text.strip())
            
            # Проверка: нельзя удалить себя
            if admin_id == message.from_user.id:
                await message.answer(
                    "⚠️ <b>Нельзя удалить самого себя!</b>",
                    parse_mode="HTML"
                )
                await state.clear()
                return
            
            # Check if admin exists
            async with db_pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT user_id, first_name, username, is_super_admin FROM admins WHERE user_id = $1",
                    admin_id
                )
                
                if not existing:
                    await message.answer(
                        "⚠️ <b>Этот пользователь не является админом!</b>",
                        parse_mode="HTML"
                    )
                    await state.clear()
                    return
                
                # Проверка: нельзя удалить супер-админа
                if existing['is_super_admin']:
                    await message.answer(
                        "⚠️ <b>Нельзя удалить супер-администратора!</b>",
                        parse_mode="HTML"
                    )
                    await state.clear()
                    return
                
                # Remove admin
                await conn.execute(
                    "DELETE FROM admins WHERE user_id = $1",
                    admin_id
                )
            
            await message.answer(
                f"✅ <b>Админ удален!</b>\n\n"
                f"👤 User ID: <code>{admin_id}</code>\n"
                f"📝 Имя: {existing['first_name']}\n"
                f"🔗 Username: @{existing['username'] or 'N/A'}",
                parse_mode="HTML"
            )
            await state.clear()
            
        except ValueError:
            await message.answer(
                "❌ <b>Ошибка!</b>\n\n"
                "Отправьте корректный User ID (число)",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"❌ Ошибка удаления админа: {e}")
            await message.answer(
                "❌ <b>Ошибка при удалении админа</b>\n\n"
                f"Попробуйте еще раз",
                parse_mode="HTML"
            )
            await state.clear()
    
    @dp.message(AdminStates.waiting_broadcast_content)
    async def process_broadcast_content(message: Message, state: FSMContext):
        """Process broadcast message content"""
        if not await is_admin(message.from_user.id):
            return
        
        # Save message data
        await state.update_data(
            text=message.text or message.caption,
            photo=message.photo[-1].file_id if message.photo else None,
            video=message.video.file_id if message.video else None
        )
        
        # Show preview
        text = "📢 <b>Предпросмотр рассылки</b>\n\n"
        if message.photo:
            text += "📸 Фото с подписью\n"
        elif message.video:
            text += "🎥 Видео с подписью\n"
        else:
            text += "📝 Текстовое сообщение\n"
        
        users = await get_all_users()
        text += f"\n👥 Будет отправлено: <b>{len(users)}</b> пользователям\n\n"
        text += "Подтвердите рассылку:"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_broadcast")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_admin")]
        ])
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    
    @dp.callback_query(F.data == "confirm_broadcast")
    async def callback_confirm_broadcast(callback: CallbackQuery, state: FSMContext):
        """Confirm and send broadcast"""
        if not await is_admin(callback.from_user.id):
            await callback.answer("❌ Доступ запрещен")
            return
        
        data = await state.get_data()
        users = await get_all_users()
        
        await callback.message.edit_text("📤 Рассылка началась...", parse_mode="HTML")
        
        success = 0
        failed = 0
        
        for user in users:
            try:
                if data.get('photo'):
                    await bot.send_photo(user['user_id'], data['photo'], caption=data.get('text'))
                elif data.get('video'):
                    await bot.send_video(user['user_id'], data['video'], caption=data.get('text'))
                else:
                    await bot.send_message(user['user_id'], data.get('text'))
                success += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1
        
        await state.clear()
        await callback.message.edit_text(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"✅ Успешно: {success}\n"
            f"❌ Ошибок: {failed}",
            parse_mode="HTML"
        )
        await callback.answer()
    
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
                    "✅ <b>Бот успешно добавлен</b>",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"❌ Ошибка отправки уведомления о подключении: {e}")
        else:
            print(f"❌ Отключено: {connection_id}")
    
    @dp.business_message()
    async def handle_business_message(message: Message):
        print("\n" + "="*80)
        print("📨 BUSINESS MESSAGE EVENT")
        print("="*80)
        print(f"📊 Chat ID: {message.chat.id}")
        print(f"📊 Message ID: {message.message_id}")
        print(f"📊 From user: {message.from_user.id if message.from_user else 'N/A'} ({message.from_user.first_name if message.from_user else 'N/A'})")
        print(f"📊 Text: {message.text[:50] if message.text else 'N/A'}...")
        print(f"📊 Caption: {message.caption[:50] if message.caption else 'N/A'}...")
        
        # МЕГА ЛОГИРОВАНИЕ МЕДИА
        print(f"\n📷 PHOTO: {bool(message.photo)}")
        if message.photo:
            print(f"   - Количество размеров: {len(message.photo)}")
            print(f"   - Последний размер file_id: {message.photo[-1].file_id}")
            print(f"   - has_media_spoiler: {getattr(message, 'has_media_spoiler', 'N/A')}")
        
        print(f"\n🎥 VIDEO: {bool(message.video)}")
        if message.video:
            print(f"   - file_id: {message.video.file_id}")
            print(f"   - has_media_spoiler: {getattr(message, 'has_media_spoiler', 'N/A')}")
        
        print(f"\n💬 REPLY_TO_MESSAGE: {bool(message.reply_to_message)}")
        if message.reply_to_message:
            print(f"   - Reply message_id: {message.reply_to_message.message_id}")
            print(f"   - Reply from: {message.reply_to_message.from_user.id if message.reply_to_message.from_user else 'N/A'}")
            print(f"   - Reply has photo: {bool(message.reply_to_message.photo)}")
            if message.reply_to_message.photo:
                print(f"   - Reply photo file_id: {message.reply_to_message.photo[-1].file_id}")
                print(f"   - Reply has_media_spoiler: {getattr(message.reply_to_message, 'has_media_spoiler', 'N/A')}")
            print(f"   - Reply has video: {bool(message.reply_to_message.video)}")
            if message.reply_to_message.video:
                print(f"   - Reply video file_id: {message.reply_to_message.video.file_id}")
                print(f"   - Reply has_media_spoiler: {getattr(message.reply_to_message, 'has_media_spoiler', 'N/A')}")
        
        print(f"\n📄 Все атрибуты message:")
        for attr in ['document', 'sticker', 'voice', 'video_note', 'animation', 'audio', 'contact', 'location']:
            if hasattr(message, attr) and getattr(message, attr):
                print(f"   - {attr}: {bool(getattr(message, attr))}")
        
        print("="*80 + "\n")
        
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
        
        # View Once photo via reply - Business API doesn't set has_media_spoiler, so check just for photo
        if message.reply_to_message and message.reply_to_message.photo:
            # Отправлять View Once фото от СОБЕСЕДНИКА (не от владельца в исходном сообщении)
            # Владелец МОЖЕТ отвечать на исчезающие фото - это нормально
            if message.reply_to_message.from_user and message.reply_to_message.from_user.id == owner_id:
                print(f"ℹ️ Это ответ на фото владельца - пропускаю (не исчезающее)")
            else:
                try:
                    orig_msg_id = message.reply_to_message.message_id
                    file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_photo_reply.jpg"
                    
                    print(f"📸 ОБНАРУЖЕНО исчезающее фото от собеседника! Скачиваю: {file_path}")
                    await bot.download(message.reply_to_message.photo[-1], destination=file_path)
                    
                    if not Path(file_path).exists():
                        print(f"❌ Файл не был создан: {file_path}")
                        return
                    
                    print(f"✅ Файл сохранён: {file_path}, размер: {Path(file_path).stat().st_size} байт")
                    
                    user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                    user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                    fancy_name = to_fancy(user_name)
                    header = f"🔒 <b>Исчезающее фото сохранено!</b>\n\n{fancy_name}{user_username} отправил(а) исчезающее фото\n\n@MessageAssistantBot_bot"
                    
                    print(f"📤 Отправляю View Once фото владельцу {owner_id}")
                    await bot.send_photo(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                    print(f"✅ Исчезающее фото успешно отправлено {owner_id}")
                    
                    # Save to DB after successful send
                    await save_message(owner_id, message.chat.id, orig_msg_id,
                               message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                               "", media_type="photo_reply", file_path=file_path,
                               caption=message.reply_to_message.caption)
                except Exception as e:
                    print(f"❌ Ошибка исчезающего фото: {e}")
                    import traceback
                    traceback.print_exc()
        
        # View Once video via reply - Business API doesn't set has_media_spoiler, so check just for video
        if message.reply_to_message and message.reply_to_message.video:
            # Отправлять View Once видео от СОБЕСЕДНИКА (не от владельца в исходном сообщении)
            if message.reply_to_message.from_user and message.reply_to_message.from_user.id == owner_id:
                print(f"ℹ️ Это ответ на видео владельца - пропускаю (не исчезающее)")
            else:
                try:
                    orig_msg_id = message.reply_to_message.message_id
                    file_path = f"saved_media/{message.chat.id}_{orig_msg_id}_video_reply.mp4"
                    
                    print(f"🎥 ОБНАРУЖЕНО исчезающее видео от собеседника! Скачиваю: {file_path}")
                    await bot.download(message.reply_to_message.video, destination=file_path)
                    
                    if not Path(file_path).exists():
                        print(f"❌ Файл не был создан: {file_path}")
                        return
                    
                    print(f"✅ Файл сохранён: {file_path}, размер: {Path(file_path).stat().st_size} байт")
                    
                    user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Unknown"
                    user_username = f" (@{message.reply_to_message.from_user.username})" if message.reply_to_message.from_user and message.reply_to_message.from_user.username else ""
                    fancy_name = to_fancy(user_name)
                    header = f"🔒 <b>Исчезающее видео сохранено!</b>\n\n{fancy_name}{user_username} отправил(а) исчезающее видео\n\n@MessageAssistantBot_bot"
                    
                    print(f"📤 Отправляю View Once видео владельцу {owner_id}")
                    await bot.send_video(owner_id, FSInputFile(file_path), caption=header, parse_mode="HTML")
                    print(f"✅ Исчезающее видео успешно отправлено {owner_id}")
                    
                    # Save to DB after successful send
                    await save_message(owner_id, message.chat.id, orig_msg_id,
                               message.reply_to_message.from_user.id if message.reply_to_message.from_user else None,
                               "", media_type="video_reply", file_path=file_path,
                               caption=message.reply_to_message.caption)
                except Exception as e:
                    print(f"❌ Ошибка исчезающего видео: {e}")
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
        
        # Check subscription status
        sub_status = await check_subscription(owner_id)
        print(f"📊 EDIT: Проверка подписки для owner_id={owner_id}: active={sub_status['active']}, type={sub_status.get('type')}, days_left={sub_status.get('days_left')}")
        
        if sub_status['active']:
            # Full notification for active subscribers - apply fancy to message text only
            print(f"✅ EDIT: Подписка активна - отправляю полное уведомление")
            old_formatted = to_fancy(old) if old else '<i>Не найдено</i>'
            new_formatted = to_fancy(new) if new else '<i>Пусто</i>'
            
            text = (
                f"{user_name}{user_username} изменил(а) сообщение:\n\n"
                f"<blockquote>Old:\n{old_formatted}</blockquote>\n\n"
                f"<blockquote>New:\n{new_formatted}</blockquote>\n\n"
                f"@MessageAssistantBot_bot"
            )
            
            try:
                await bot.send_message(owner_id, text, parse_mode="HTML")
                print(f"✅ EDIT: Полное уведомление отправлено")
            except Exception as e:
                print(f"❌ EDIT: Ошибка отправки полного уведомления: {e}")
        else:
            # Limited notification for expired subscription
            print(f"⚠️ EDIT: Подписка НЕактивна - отправляю краткое уведомление")
            text = f"{user_name}{user_username} изменил(а) сообщение:"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁 Посмотреть", callback_data=f"view_edit_{message.chat.id}_{message.message_id}")]
            ])
            
            try:
                await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=keyboard)
                print(f"✅ EDIT: Краткое уведомление отправлено")
            except Exception as e:
                print(f"❌ EDIT: Ошибка отправки краткого уведомления: {e}")
    
    @dp.deleted_business_messages()
    async def handle_deleted_business_messages(event: BusinessMessagesDeleted):
        print("\n" + "="*80)
        print("🗑 DELETED_BUSINESS_MESSAGES EVENT")
        print("="*80)
        print(f"📊 Количество удаленных сообщений: {len(event.message_ids)}")
        print(f"📊 Chat ID: {event.chat.id}")
        print(f"📊 Message IDs: {event.message_ids}")
        print(f"📊 Event type: {type(event).__name__}")
        print(f"📊 Event chat: {event.chat}")
        print(f"📊 Event chat.type: {event.chat.type if event.chat else 'N/A'}")
        print(f"📊 Event chat.first_name: {event.chat.first_name if event.chat else 'N/A'}")
        print(f"📊 Event chat.username: {event.chat.username if event.chat else 'N/A'}")
        
        # Логируем все атрибуты event
        print(f"📊 Все атрибуты event:")
        for attr in dir(event):
            if not attr.startswith('_'):
                try:
                    value = getattr(event, attr)
                    if not callable(value):
                        print(f"   - {attr}: {value}")
                except:
                    pass
        print("="*80)
        
        # Get owner_id and total messages in this chat
        async with db_pool.acquire() as conn:
            first_row = await conn.fetchrow(
                "SELECT owner_id FROM messages WHERE chat_id = $1 AND message_id = ANY($2) LIMIT 1",
                event.chat.id, event.message_ids
            )
            
            if not first_row:
                print(f"⚠️ Не найден owner_id для удаленных сообщений в чате {event.chat.id}")
                print(f"⚠️ Проверяю БД: есть ли вообще сообщения для этого чата...")
                total_in_db = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE chat_id = $1", event.chat.id)
                print(f"⚠️ Всего сообщений в БД для чата {event.chat.id}: {total_in_db}")
                return
            
            owner_id = first_row['owner_id']
            print(f"✅ Owner ID найден: {owner_id}")
            
            # Count total messages in this chat
            total_messages = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND owner_id = $2",
                event.chat.id, owner_id
            )
        
        print(f"📊 Всего сообщений в БД для чата {event.chat.id}: {total_messages}")
        print(f"📊 Удаляется сообщений: {len(event.message_ids)}")
        
        # Track deletions for this chat
        import time
        current_time = time.time()
        chat_id = event.chat.id
        
        if chat_id not in recent_deletions:
            recent_deletions[chat_id] = []
        
        # Clean old deletions (older than 10 seconds)
        recent_deletions[chat_id] = [(t, c) for t, c in recent_deletions[chat_id] if current_time - t < 10]
        
        # Add current deletion
        recent_deletions[chat_id].append((current_time, len(event.message_ids)))
        
        # Calculate total deletions in last 10 seconds
        total_recent_deletions = sum(c for _, c in recent_deletions[chat_id])
        
        # Check if this is a full chat clear
        # Conditions:
        # 1. Deleting >=2 messages at once OR
        # 2. >20% of messages deleted OR
        # 3. Multiple deletions in 10 seconds totaling >=3 messages
        percentage = (len(event.message_ids) / total_messages * 100) if total_messages > 0 else 0
        is_chat_clear = (
            (len(event.message_ids) >= 2) or 
            (percentage > 20) or 
            (total_recent_deletions >= 3)
        )
        
        print(f"📊 Процент удаляемых сообщений: {percentage:.1f}%")
        print(f"📊 Удалений за последние 10 сек: {total_recent_deletions}")
        print(f"📊 Определено как очистка чата: {is_chat_clear}")
        
        if is_chat_clear:
            chat_name = event.chat.first_name or "Unknown" if event.chat else "Unknown"
            
            # Create HTML backup before deleting
            print(f"📦 Создаю HTML-копию чата {event.chat.id}...")
            html_file = await create_chat_html_backup(owner_id, event.chat.id, chat_name)
            
            if html_file:
                print(f"✅ HTML файл получен: {html_file}")
                try:
                    print(f"📤 Отправляю HTML файл владельцу {owner_id}...")
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
            else:
                print(f"❌ HTML файл не был создан (вернулся None)")
        
        for msg_id in event.message_ids:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM messages WHERE chat_id = $1 AND message_id = $2", event.chat.id, msg_id)
                
                if not row:
                    print(f"⚠️ Сообщение {msg_id} не найдено в БД")
                    continue
                
                owner_id = row["owner_id"]
                msg_data = dict(row)
                
                print(f"📝 Обрабатываю удаление сообщения {msg_id}")
                print(f"📝 user_id сообщения: {msg_data.get('user_id')}, owner_id: {owner_id}")
                
                if msg_data.get("user_id") == owner_id:
                    print(f"ℹ️ Это твое сообщение - просто удаляю из БД без уведомления")
                    await delete_message_from_db(owner_id, event.chat.id, msg_id)
                    continue
                
                print(f"🔔 Это сообщение собеседника - отправляю уведомление!")
                
                await increment_stat(owner_id, "total_deletes")
                
                user_name = event.chat.first_name or "User" if event.chat else "Unknown"
                user_username = f" (@{event.chat.username})" if event.chat and event.chat.username else ""
                
                # Check subscription status
                sub_status = await check_subscription(owner_id)
                print(f"📊 DELETE: Проверка подписки для owner_id={owner_id}: active={sub_status['active']}, type={sub_status.get('type')}, days_left={sub_status.get('days_left')}")
                
                if not sub_status['active']:
                    # Limited notification for expired subscription
                    print(f"⚠️ DELETE: Подписка НЕактивна - отправляю краткое уведомление")
                    text = f"{user_name}{user_username} удалил(а) сообщение:"
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="👁 Посмотреть", callback_data=f"view_delete_{event.chat.id}_{msg_id}")]
                    ])
                    
                    try:
                        await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=keyboard)
                        print(f"✅ DELETE: Краткое уведомление отправлено")
                    except Exception as e:
                        print(f"❌ DELETE: Ошибка отправки краткого уведомления: {e}")
                    
                    await delete_message_from_db(owner_id, event.chat.id, msg_id)
                    print(f"🗑️ DELETE: Сообщение {msg_id} удалено из БД")
                    continue
                
                # Full notification for active subscribers
                print(f"✅ DELETE: Подписка активна - отправляю полное уведомление")
                
                # Full notification for active subscribers - apply fancy to message content only, not labels
                caption_parts = []
                if msg_data.get("text") and msg_data["text"].strip():
                    fancy_text = to_fancy(msg_data['text'])
                    caption_parts.append(f"📝 Текст: {fancy_text}")
                elif msg_data.get("caption") and msg_data["caption"].strip():
                    fancy_caption = to_fancy(msg_data['caption'])
                    caption_parts.append(f"📝 Подпись: {fancy_caption}")
                
                if msg_data.get("links"):
                    caption_parts.append(f"🔗 Ссылки: {msg_data['links']}")
                
                header = f"{user_name}{user_username} удалил(а) сообщение:\n\n"
                if caption_parts:
                    header += "<blockquote>" + "\n".join(caption_parts) + "</blockquote>\n\n"
                header += "@MessageAssistantBot_bot"
                
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
                            await bot.send_sticker(owner_id, FSInputFile(msg_data["file_path"]))
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
    print("MessageAssistant Multi-User Bot (PostgreSQL)")
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
