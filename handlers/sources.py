import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select, delete
from database import AsyncSessionLocal
from models import User, SourceChannel
from scrapers import TelegramScraper
from utils import extract_channel_username
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message, check_action_limit, check_user_access
from .constants import (
    AWAITING_SOURCE_USERNAME, AWAITING_CRITERIA, AWAITING_VIEWS, AWAITING_REACTIONS,
    AWAITING_MEDIA_FILTER, AWAITING_REMOVE_TEXT
)

logger = logging.getLogger(__name__)


async def add_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    project = await require_project(update, context)
    
    if not project:
        return ConversationHandler.END
    
    has_access, message, user = await check_user_access(telegram_id)
    if not has_access:
        await update.message.reply_text(message)
        return ConversationHandler.END
    
    can_add, limit_msg = await check_action_limit(user, "add_source", project_id=project.id)
    if not can_add and not user.is_admin:
        await update.message.reply_text(f"❌ {limit_msg}")
        return ConversationHandler.END
    
    context.user_data['temp_project_id'] = project.id
    context.user_data['temp_project_name'] = project.name
    
    await update.message.reply_text(
        f"📥 Добавление источника в «{project.name}»\n\n"
        "Отправьте username канала (@name) или ссылку:\n"
        "• @durov\n"
        "• https://t.me/durov"
    )
    return AWAITING_SOURCE_USERNAME


async def add_source_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = extract_channel_username(update.message.text)
    if not username:
        await update.message.reply_text("❌ Не удалось распознать username.")
        return AWAITING_SOURCE_USERNAME
    
    async with TelegramScraper() as scraper:
        info = await scraper.get_channel_info(username)
    
    if not info:
        await update.message.reply_text("❌ Канал не найден или не является публичным.")
        return AWAITING_SOURCE_USERNAME
    
    context.user_data['temp_source'] = {
        'username': username,
        'title': info['title'],
        'project_id': context.user_data.get('temp_project_id'),
        'project_name': context.user_data.get('temp_project_name')
    }
    
    keyboard = [
        [InlineKeyboardButton("🎯 Свои критерии", callback_data="criteria_custom")],
        [InlineKeyboardButton("👁 1000+ просмотров", callback_data="criteria_views")],
        [InlineKeyboardButton("❤️ 50+ реакций", callback_data="criteria_reactions")],
        [InlineKeyboardButton("👁+❤️ 500+ и 25+", callback_data="criteria_both")],
        [InlineKeyboardButton("⚡ Без критериев", callback_data="criteria_none")],
    ]
    
    await update.message.reply_text(
        f"✅ Канал: @{username}\n📝 Название: {info['title']}\n\nВыберите критерии отбора:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_CRITERIA


async def add_source_criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("criteria_", "")
    temp = context.user_data.get('temp_source')
    
    if not temp:
        await query.edit_message_text("❌ Ошибка: данные не найдены.")
        return ConversationHandler.END
    
    if choice == "custom":
        await query.edit_message_text(
            "📊 <b>Настройка критериев</b>\n\nВведите минимальное количество просмотров (0 = не учитывать):",
            parse_mode="HTML"
        )
        context.user_data['awaiting_criteria'] = 'views'
        return AWAITING_VIEWS
    else:
        criteria = {
            "views": {"min_views": 1000},
            "reactions": {"min_reactions": 50},
            "both": {"min_views": 500, "min_reactions": 25},
            "none": {}
        }.get(choice, {})
        
        context.user_data['temp_criteria'] = criteria
        
        keyboard = [
            [InlineKeyboardButton("📷 Все (фото + видео)", callback_data="media_all")],
            [InlineKeyboardButton("🖼️ Только фото", callback_data="media_photo_only")],
            [InlineKeyboardButton("🎬 Только видео", callback_data="media_video_only")],
        ]
        
        await query.edit_message_text(
            f"✅ Критерии выбраны\n\nТеперь выберите тип контента для @{temp['username']}:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return AWAITING_MEDIA_FILTER


async def criteria_views_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views = int(update.message.text.strip())
        if views < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_VIEWS
    
    context.user_data['temp_criteria_views'] = views
    await update.message.reply_text("📊 Введите минимальное количество реакций (0 = не учитывать):")
    return AWAITING_REACTIONS


async def criteria_reactions_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reactions = int(update.message.text.strip())
        if reactions < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_REACTIONS
    
    views = context.user_data.get('temp_criteria_views', 0)
    criteria = {}
    if views > 0:
        criteria['min_views'] = views
    if reactions > 0:
        criteria['min_reactions'] = reactions
    
    context.user_data['temp_criteria'] = criteria
    
    keyboard = [
        [InlineKeyboardButton("📷 Все (фото + видео)", callback_data="media_all")],
        [InlineKeyboardButton("🖼️ Только фото", callback_data="media_photo_only")],
        [InlineKeyboardButton("🎬 Только видео", callback_data="media_video_only")],
    ]
    
    await update.message.reply_text(
        f"✅ Критерии сохранены\n\nТеперь выберите тип контента:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_MEDIA_FILTER


async def media_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("media_", "")
    context.user_data['temp_media_filter'] = choice
    
    if choice in ("video_only", "all"):
        keyboard = [
            [InlineKeyboardButton("📏 До 1 минуты", callback_data="duration_60")],
            [InlineKeyboardButton("📏 До 3 минут", callback_data="duration_180")],
            [InlineKeyboardButton("📏 Без ограничений", callback_data="duration_0")],
        ]
        
        await query.edit_message_text(
            f"🎬 <b>Ограничение по длительности видео:</b>\n\nВыберите максимальную длительность:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        context.user_data['awaiting_duration'] = True
        return AWAITING_MEDIA_FILTER
    else:
        context.user_data['temp_max_video_duration'] = None
        return await ask_remove_text(query, context)


async def duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("duration_", "")
    duration = int(choice)
    context.user_data['temp_max_video_duration'] = duration if duration > 0 else None
    
    return await ask_remove_text(query, context)


async def ask_remove_text(target, context):
    keyboard = [
        [InlineKeyboardButton("✅ Оставлять текст", callback_data="text_keep")],
        [InlineKeyboardButton("❌ Удалять текст", callback_data="text_remove")],
    ]
    
    text = (
        f"📝 <b>Оригинальный текст поста:</b>\n\n"
        f"Хотите оставлять или удалять текст из источника?\n"
        f"Если удалить — останется только медиа и подпись."
    )
    
    if hasattr(target, 'edit_message_text'):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await target.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    
    context.user_data['awaiting_text_choice'] = True
    return AWAITING_REMOVE_TEXT


async def remove_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("text_", "")
    remove_text = (choice == "remove")
    
    temp = context.user_data.get('temp_source')
    criteria = context.user_data.get('temp_criteria', {})
    media_filter = context.user_data.get('temp_media_filter', 'all')
    max_video_duration = context.user_data.get('temp_max_video_duration')
    
    if not temp:
        await query.edit_message_text("❌ Ошибка: данные не найдены.")
        return ConversationHandler.END
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SourceChannel).where(
                SourceChannel.project_id == temp['project_id'],
                SourceChannel.channel_username == temp['username']
            )
        )
        if result.scalar_one_or_none():
            await query.edit_message_text(f"⚠️ Канал @{temp['username']} уже добавлен в этот проект.")
            return ConversationHandler.END
        
        channel = SourceChannel(
            project_id=temp['project_id'],
            channel_username=temp['username'],
            channel_title=temp['title'],
            criteria=criteria,
            media_filter=media_filter,
            remove_original_text=remove_text,
            max_video_duration=max_video_duration
        )
        session.add(channel)
        await session.commit()
    
    filter_text = {"all": "все", "photo_only": "только фото", "video_only": "только видео"}.get(media_filter, "все")
    
    criteria_parts = []
    if criteria.get('min_views'):
        criteria_parts.append(f"👁 от {criteria['min_views']}")
    if criteria.get('min_reactions'):
        criteria_parts.append(f"❤️ от {criteria['min_reactions']}")
    criteria_display = ", ".join(criteria_parts) if criteria_parts else "без критериев"
    
    text_parts = [f"✅ Канал @{temp['username']} добавлен!"]
    text_parts.append(f"📋 Критерии: {criteria_display}")
    text_parts.append(f"📷 Контент: {filter_text}")
    if max_video_duration:
        text_parts.append(f"🎬 Длительность видео: до {max_video_duration} сек")
    text_parts.append(f"📝 Текст: {'удаляется' if remove_text else 'оставляется'}")
    
    await query.edit_message_text("\n".join(text_parts))
    
    project_id = temp['project_id']
    project_name = temp['project_name']
    
    for key in ['temp_source', 'temp_project_id', 'temp_project_name', 'temp_criteria',
                'temp_criteria_views', 'temp_media_filter', 'temp_max_video_duration',
                'awaiting_criteria', 'awaiting_duration', 'awaiting_text_choice']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    target_channel = await get_project_target(project_id)
    
    if target_channel:
        if sources_count == 1:
            await query.message.reply_text(
                f"✅ <b>Проект «{project_name}» готов к работе!</b>\n\n"
                f"• /set_interval — настроить частоту парсинга\n"
                f"• /set_post_interval — интервал публикаций\n"
                f"• /set_signature — добавить подпись\n"
                f"• /parse — запустить парсинг\n"
                f"• /add_source — добавить ещё источник",
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text(
                f"✅ Источник добавлен! Всего источников: {sources_count}",
                parse_mode="HTML"
            )
    
    return ConversationHandler.END


async def my_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    project = await require_project(update, context)
    
    if not project:
        return
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SourceChannel).where(SourceChannel.project_id == project.id).order_by(SourceChannel.added_at.desc())
        )
        sources = result.scalars().all()
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
    
    if not sources:
        await update.message.reply_text(f"📭 В проекте «{project.name}» нет источников.\nДобавьте: /add_source")
        return
    
    text = f"📥 <b>Источники «{project.name}»</b> ({len(sources)} / {user.max_sources_per_project})\n\n"
    keyboard = []
    
    filter_names = {"all": "все", "photo_only": "только фото", "video_only": "только видео"}
    
    for src in sources:
        criteria_parts = []
        if src.criteria:
            if "min_views" in src.criteria:
                criteria_parts.append(f"👁 ≥{src.criteria['min_views']}")
            if "min_reactions" in src.criteria:
                criteria_parts.append(f"❤️ ≥{src.criteria['min_reactions']}")
        criteria_str = ", ".join(criteria_parts) if criteria_parts else "без критериев"
        
        status_icon = "✅" if src.is_active else "❌"
        text += f"{status_icon} @{src.channel_username}\n"
        text += f"   📊 {criteria_str}\n"
        text += f"   📷 {filter_names.get(src.media_filter, 'все')}"
        if src.max_video_duration:
            text += f" | 🎬 до {src.max_video_duration}с"
        text += f" | 📝 {'без текста' if src.remove_original_text else 'с текстом'}\n"
        if src.last_parsed:
            text += f"   🕐 {src.last_parsed.strftime('%d.%m.%Y %H:%M')}\n"
        text += "\n"
        
        keyboard.append([InlineKeyboardButton(f"❌ Удалить @{src.channel_username}", callback_data=f"del_source_{src.id}")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, parse_mode="HTML")


async def delete_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.replace("del_source_", ""))
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SourceChannel).where(SourceChannel.id == source_id))
        await session.commit()
    await query.edit_message_text("✅ Источник удалён")