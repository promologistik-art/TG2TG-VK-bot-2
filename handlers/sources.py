import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select, update as sql_update, delete
from database import AsyncSessionLocal
from models import User, SourceChannel
from scrapers import TelegramScraper
from utils import extract_channel_username
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message, check_action_limit, check_user_access
from .constants import (
    AWAITING_SOURCE_USERNAME, AWAITING_CRITERIA, AWAITING_VIEWS, AWAITING_REACTIONS,
    AWAITING_MEDIA_FILTER, AWAITING_REMOVE_TEXT, CURRENT_PROJECT_KEY,
    AWAITING_EDIT_VIEWS, AWAITING_EDIT_REACTIONS, AWAITING_EDIT_EXCLUDE_PHRASES
)

logger = logging.getLogger(__name__)


# ============ ДОБАВЛЕНИЕ ИСТОЧНИКА ============

async def add_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_project = context.user_data.get(CURRENT_PROJECT_KEY)
    context.user_data.clear()
    if current_project:
        context.user_data[CURRENT_PROJECT_KEY] = current_project
    
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


# ============ РЕДАКТИРОВАНИЕ ИСТОЧНИКА ============

async def edit_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню редактирования источника."""
    query = update.callback_query
    await query.answer()
    
    source_id = int(query.data.replace("edit_source_", ""))
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
        source = result.scalar_one_or_none()
    
    if not source:
        await query.edit_message_text("❌ Источник не найден")
        return
    
    context.user_data['edit_source_id'] = source_id
    
    filter_names = {"all": "все", "photo_only": "только фото", "video_only": "только видео"}
    
    criteria_parts = []
    if source.criteria:
        if "min_views" in source.criteria:
            criteria_parts.append(f"👁 ≥{source.criteria['min_views']}")
        if "min_reactions" in source.criteria:
            criteria_parts.append(f"❤️ ≥{source.criteria['min_reactions']}")
    criteria_str = ", ".join(criteria_parts) if criteria_parts else "без критериев"
    
    text = (
        f"✏️ <b>Редактирование @{source.channel_username}</b>\n\n"
        f"📊 Критерии: {criteria_str}\n"
        f"📷 Контент: {filter_names.get(source.media_filter, 'все')}\n"
        f"🎬 Длительность видео: {'до ' + str(source.max_video_duration) + 'с' if source.max_video_duration else 'без ограничений'}\n"
        f"📝 Текст: {'удаляется' if source.remove_original_text else 'оставляется'}\n"
        f"🚫 Стоп-фразы: {source.exclude_phrases or 'нет'}\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 Изменить критерии", callback_data=f"edit_criteria_{source_id}")],
        [InlineKeyboardButton("📷 Изменить тип контента", callback_data=f"edit_media_{source_id}")],
        [InlineKeyboardButton("📝 Изменить обработку текста", callback_data=f"edit_text_{source_id}")],
        [InlineKeyboardButton("🚫 Изменить стоп-фразы", callback_data=f"edit_phrases_{source_id}")],
        [InlineKeyboardButton("🗑️ Очистить стоп-фразы", callback_data=f"edit_clear_phrases_{source_id}")],
        [InlineKeyboardButton("◀️ Назад к источникам", callback_data="back_to_sources")],
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def edit_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа для ConversationHandler редактирования."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Обработка очистки стоп-фраз (без диалога)
    if data.startswith("edit_clear_phrases_"):
        source_id = int(data.replace("edit_clear_phrases_", ""))
        async with AsyncSessionLocal() as session:
            await session.execute(
                sql_update(SourceChannel)
                .where(SourceChannel.id == source_id)
                .values(exclude_phrases=None)
            )
            await session.commit()
        await query.edit_message_text("✅ Стоп-фразы очищены")
        return ConversationHandler.END
    
    # Остальные действия требуют диалога
    source_id = int(data.split("_")[-1])
    context.user_data['edit_source_id'] = source_id
    
    if data.startswith("edit_criteria_"):
        await query.edit_message_text(
            "📊 Введите новые минимальные просмотры (0 = не учитывать):"
        )
        return AWAITING_EDIT_VIEWS
    
    elif data.startswith("edit_media_"):
        keyboard = [
            [InlineKeyboardButton("📷 Все", callback_data="edit_media_all")],
            [InlineKeyboardButton("🖼️ Только фото", callback_data="edit_media_photo_only")],
            [InlineKeyboardButton("🎬 Только видео", callback_data="edit_media_video_only")],
        ]
        await query.edit_message_text(
            "📷 Выберите новый тип контента:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_MEDIA_FILTER
    
    elif data.startswith("edit_text_"):
        keyboard = [
            [InlineKeyboardButton("✅ Оставлять текст", callback_data="edit_text_keep")],
            [InlineKeyboardButton("❌ Удалять текст", callback_data="edit_text_remove")],
        ]
        await query.edit_message_text(
            "📝 Оставлять или удалять текст из источника?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_REMOVE_TEXT
    
    elif data.startswith("edit_phrases_"):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
            source = result.scalar_one()
        
        current = source.exclude_phrases or "нет"
        
        await query.edit_message_text(
            f"🚫 <b>Стоп-фразы</b>\n\n"
            f"Текущие: {current}\n\n"
            f"Введите новые фразы через запятую.\n"
            f"Например: реклама, спонсор, подпишись\n\n"
            f"Или нажмите кнопку ниже чтобы очистить:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ Очистить стоп-фразы", callback_data=f"edit_clear_phrases_{source_id}")
            ]])
        )
        return AWAITING_EDIT_EXCLUDE_PHRASES
    
    return ConversationHandler.END


async def edit_views_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views = int(update.message.text.strip())
        if views < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_EDIT_VIEWS
    
    context.user_data['edit_views'] = views
    await update.message.reply_text("📊 Введите новые минимальные реакции (0 = не учитывать):")
    return AWAITING_EDIT_REACTIONS


async def edit_reactions_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reactions = int(update.message.text.strip())
        if reactions < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_EDIT_REACTIONS
    
    views = context.user_data.get('edit_views', 0)
    criteria = {}
    if views > 0:
        criteria['min_views'] = views
    if reactions > 0:
        criteria['min_reactions'] = reactions
    
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(criteria=criteria)
        )
        await session.commit()
    
    await update.message.reply_text("✅ Критерии обновлены!")
    
    context.user_data.pop('edit_views', None)
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_media_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_media_", "")
    context.user_data['edit_media_filter'] = choice
    
    if choice in ("video_only", "all"):
        keyboard = [
            [InlineKeyboardButton("📏 До 1 минуты", callback_data="edit_duration_60")],
            [InlineKeyboardButton("📏 До 3 минут", callback_data="edit_duration_180")],
            [InlineKeyboardButton("📏 Без ограничений", callback_data="edit_duration_0")],
        ]
        await query.edit_message_text(
            "🎬 Максимальная длительность видео:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_MEDIA_FILTER
    else:
        source_id = context.user_data.get('edit_source_id')
        async with AsyncSessionLocal() as session:
            await session.execute(
                sql_update(SourceChannel)
                .where(SourceChannel.id == source_id)
                .values(media_filter=choice, max_video_duration=None)
            )
            await session.commit()
        
        await query.edit_message_text(f"✅ Тип контента обновлён: только фото")
        context.user_data.pop('edit_source_id', None)
        context.user_data.pop('edit_media_filter', None)
        return ConversationHandler.END


async def edit_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_duration_", "")
    duration = int(choice) if int(choice) > 0 else None
    media_filter = context.user_data.get('edit_media_filter', 'all')
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(media_filter=media_filter, max_video_duration=duration)
        )
        await session.commit()
    
    dur_text = f"до {duration}с" if duration else "без ограничений"
    filter_text = {"all": "все", "video_only": "только видео"}.get(media_filter, media_filter)
    await query.edit_message_text(f"✅ Обновлено: {filter_text}, {dur_text}")
    
    context.user_data.pop('edit_source_id', None)
    context.user_data.pop('edit_media_filter', None)
    return ConversationHandler.END


async def edit_remove_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_text_", "")
    remove_text = (choice == "remove")
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(remove_original_text=remove_text)
        )
        await session.commit()
    
    await query.edit_message_text(f"✅ Текст: {'удаляется' if remove_text else 'оставляется'}")
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_exclude_phrases_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    source_id = context.user_data.get('edit_source_id')
    
    if text == '-':
        text = None
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(exclude_phrases=text)
        )
        await session.commit()
    
    if text:
        await update.message.reply_text(f"✅ Стоп-фразы обновлены: {text}")
    else:
        await update.message.reply_text("✅ Стоп-фразы очищены")
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


# ============ СПИСОК ИСТОЧНИКОВ ============

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
        text = f"📭 В проекте «{project.name}» нет источников.\nДобавьте: /add_source"
        keyboard = [[InlineKeyboardButton("◀️ Назад к проекту", callback_data=f"project_menu_{project.id}")]]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
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
        text += f" | 📝 {'без текста' if src.remove_original_text else 'с текстом'}"
        if src.exclude_phrases:
            text += f"\n   🚫 Стоп-фразы: {src.exclude_phrases}"
        if src.last_parsed:
            text += f"\n   🕐 {src.last_parsed.strftime('%d.%m.%Y %H:%M')}"
        text += "\n\n"
        
        keyboard.append([
            InlineKeyboardButton(f"✏️ Ред. @{src.channel_username}", callback_data=f"edit_source_{src.id}"),
            InlineKeyboardButton(f"❌ Удалить", callback_data=f"del_source_{src.id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад к проекту", callback_data=f"project_menu_{project.id}")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )


async def delete_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    source_id = int(query.data.replace("del_source_", ""))
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SourceChannel).where(SourceChannel.id == source_id))
        await session.commit()
    await query.edit_message_text("✅ Источник удалён")


# ============ ВОЗВРАТ К ИСТОЧНИКАМ ============

async def back_to_sources_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку источников."""
    query = update.callback_query
    await query.answer()
    await my_sources(update, context)