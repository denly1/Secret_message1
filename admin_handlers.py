# Admin panel callback handlers
# This file contains all interactive admin panel handlers

async def handle_admin_revenue(callback, bot, db_pool):
    """Show revenue statistics with graphs by period"""
    from bot import get_revenue_by_period
    
    # Get revenue for different periods
    day_stats = await get_revenue_by_period("day")
    week_stats = await get_revenue_by_period("week")
    month_stats = await get_revenue_by_period("month")
    year_stats = await get_revenue_by_period("year")
    
    text = "📊 <b>Статистика прибыли</b>\n\n"
    text += f"📅 <b>За день:</b>\n"
    text += f"💰 {day_stats['total_stars']} ⭐ ({day_stats['total_payments']} платежей)\n\n"
    text += f"📅 <b>За неделю:</b>\n"
    text += f"💰 {week_stats['total_stars']} ⭐ ({week_stats['total_payments']} платежей)\n\n"
    text += f"📅 <b>За месяц:</b>\n"
    text += f"💰 {month_stats['total_stars']} ⭐ ({month_stats['total_payments']} платежей)\n\n"
    text += f"📅 <b>За год:</b>\n"
    text += f"💰 {year_stats['total_stars']} ⭐ ({year_stats['total_payments']} платежей)\n\n"
    
    # Calculate average
    if month_stats['total_payments'] > 0:
        avg_payment = month_stats['total_stars'] / month_stats['total_payments']
        text += f"📈 <b>Средний чек (месяц):</b> {avg_payment:.1f} ⭐\n"
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в админ-панель", callback_data="back_to_admin")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


async def handle_admin_broadcast(callback, state):
    """Start broadcast process"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    text = "📢 <b>Рассылка сообщений</b>\n\n"
    text += "Отправьте сообщение, которое хотите разослать всем пользователям.\n\n"
    text += "Вы можете отправить:\n"
    text += "• Текст\n"
    text += "• Фото с подписью\n"
    text += "• Видео с подписью\n\n"
    text += "После отправки вы увидите предпросмотр и сможете подтвердить рассылку."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_admin")]
    ])
    
    from bot import AdminStates
    await state.set_state(AdminStates.waiting_broadcast_content)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


async def handle_admin_subscriptions(callback):
    """Show subscription management menu"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
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


async def handle_admin_export_csv(callback, bot, db_pool):
    """Export users to CSV"""
    from bot import get_detailed_users_csv
    from aiogram.types import BufferedInputFile
    
    await callback.answer("⏳ Генерирую CSV файл...")
    
    csv_content = await get_detailed_users_csv()
    
    # Send CSV file
    csv_file = BufferedInputFile(
        csv_content.encode('utf-8-sig'),
        filename=f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    
    from datetime import datetime
    await bot.send_document(
        callback.from_user.id,
        csv_file,
        caption="📊 <b>Экспорт пользователей</b>\n\nДетальная статистика по всем пользователям с информацией о подписках и платежах.",
        parse_mode="HTML"
    )
    
    await callback.answer("✅ CSV файл отправлен!")


async def handle_back_to_admin(callback, bot, db_pool):
    """Return to admin panel"""
    from bot import is_super_admin, get_users_stats, get_revenue_stats
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    user_id = callback.from_user.id
    is_super = await is_super_admin(user_id)
    
    # Get stats
    users_stats = await get_users_stats()
    revenue = await get_revenue_stats()
    
    text = "👮 <b>Админ-панель MessageGuardian</b>\n\n"
    text += f"👥 Всего пользователей: <b>{users_stats['total_users']}</b>\n"
    text += f"✅ Активных подписок: <b>{users_stats['active_subscriptions']}</b>\n"
    text += f"🆓 Пробных: <b>{users_stats['trial_users']}</b>\n"
    text += f"💎 Платных: <b>{users_stats['paid_users']}</b>\n\n"
    text += f"💰 Общая прибыль: <b>{revenue['total_stars']} ⭐</b>\n"
    text += f"💳 Всего платежей: <b>{revenue['total_payments']}</b>\n\n"
    text += "Выберите действие:"
    
    # Build keyboard with buttons
    keyboard_buttons = [
        [InlineKeyboardButton(text="📊 Статистика прибыли", callback_data="admin_revenue")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Управление подписками", callback_data="admin_subscriptions")],
        [InlineKeyboardButton(text="📥 Выгрузить CSV", callback_data="admin_export_csv")]
    ]
    
    if is_super:
        keyboard_buttons.append([InlineKeyboardButton(text="👑 Управление админами", callback_data="admin_manage_admins")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
