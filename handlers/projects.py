import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, delete
from config import Config
from database import AsyncSessionLocal
from models import User, Project, SourceChannel, TargetChannel, PostQueue
from .utils import (
    get_current_project, get_sources_count, get_project_target,
    get_user_projects_count, check_user_access, check_action_limit
)
from .constants import CURRENT_PROJECT_KEY

logger = logging.getLogger(__name__)


async def my_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    has_access, message, user = await check_user_access(telegram_id)
    current_project = await get_current_project(telegram_id, context)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
        result = await session.execute(select(Project).where(Project.user_id == telegram_id).order_by(Project.id))
        projects = result.scalars().all()
    
    if not projects:
        can_create, limit_msg = await check_action_limit(user, "create_project")
        keyboard = None
        create_text = ""
        if can_create or user.is_admin:
            keyboard = [[InlineKeyboardButton("➕ Создать проект", callback_data="create_project")]]
        else:
            create_text = f"\n\n{limit_msg}"
        
        text = "📭 У вас пока нет проектов.\n\nПроект — это связка из источников и целевого канала.\nНапример: «Мемасы», «Книги», «Кино»" + create_text
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        return
    
    text = f"📁 <b>Ваши проекты</b> ({len(projects)} / {user.max_projects})\n\n"
    if not has_access:
        text += f"⚠️ {message}\n\n"
    
    keyboard = []
    
    for p in projects:
        sources_count = await get_sources_count(p.id)
        target = await get_project_target(p.id)
        
        current_icon = "👉 " if current_project and p.id == current_project.id else ""
        status_icon = "✅" if p.is_active else "❌"
        
        target_name = 'не задана'
        if target:
            if target.platform == 'telegram':
                target_name = target.channel_title or 'Канал Telegram'
            elif target.platform == 'vk':
                target_name = target.vk_group_name or 'Группа VK'
        
        text += f"{current_icon}{status_icon} <b>{p.name}</b>\n"
        text += f"   📥 Источников: {sources_count}\n"
        text += f"   📤 Цель: {target_name}\n"
        text += f"   📊 Сегодня: {p.posts_parsed_today} / {p.posts_posted_today}\n\n"
        
        if not current_project or p.id != current_project.id:
            keyboard.append([InlineKeyboardButton(f"✅ Выбрать «{p.name}»", callback_data=f"select_project_{p.id}")])
        
        keyboard.append([
            InlineKeyboardButton(f"📊 Статистика", callback_data=f"stats_project_{p.id}"),
            InlineKeyboardButton(f"❌ Удалить", callback_data=f"delete_project_{p.id}")
        ])
    
    can_create, _ = await check_action_limit(user, "create_project")
    if len(projects) < user.max_projects and (can_create or user.is_admin):
        keyboard.append([InlineKeyboardButton("➕ Создать новый проект", callback_data="create_project")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def projects_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    data = query.data
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
    
    if data == "create_project":
        can_create, limit_msg = await check_action_limit(user, "create_project")
        if not can_create and not user.is_admin:
            await query.edit_message_text(f"❌ {limit_msg}")
            return
        await query.edit_message_text("📁 Введите название нового проекта:")
        context.user_data['awaiting_project_name'] = True
        return
    
    if data.startswith("select_project_"):
        project_id = int(data.replace("select_project_", ""))
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Project).where(Project.id == project_id, Project.user_id == telegram_id))
            project = result.scalar_one_or_none()
        if project:
            context.user_data[CURRENT_PROJECT_KEY] = project.id
            await query.edit_message_text(f"✅ Выбран проект «{project.name}»")
    
    elif data.startswith("delete_project_"):
        project_id = int(data.replace("delete_project_", ""))
        keyboard = [
            [InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{project_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete")],
        ]
        await query.edit_message_text(
            "⚠️ Удалить проект? Все источники и настройки будут потеряны.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("confirm_delete_"):
        project_id = int(data.replace("confirm_delete_", ""))
        async with AsyncSessionLocal() as session:
            await session.execute(delete(SourceChannel).where(SourceChannel.project_id == project_id))
            await session.execute(delete(TargetChannel).where(TargetChannel.project_id == project_id))
            await session.execute(delete(PostQueue).where(PostQueue.project_id == project_id))
            await session.execute(delete(Project).where(Project.id == project_id))
            await session.commit()
        if context.user_data.get(CURRENT_PROJECT_KEY) == project_id:
            context.user_data.pop(CURRENT_PROJECT_KEY, None)
        await query.edit_message_text("✅ Проект удалён")
    
    elif data == "cancel_delete":
        await query.edit_message_text("❌ Удаление отменено")
    
    elif data.startswith("stats_project_"):
        project_id = int(data.replace("stats_project_", ""))
        await show_project_stats(query, project_id)


async def handle_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_project_name'):
        return
    
    name = update.message.text.strip()
    telegram_id = update.effective_user.id
    
    if len(name) < 2 or len(name) > 50:
        await update.message.reply_text("❌ Название должно быть от 2 до 50 символов.")
        return
    
    has_access, access_msg, user = await check_user_access(telegram_id)
    if not has_access:
        await update.message.reply_text(access_msg)
        context.user_data['awaiting_project_name'] = False
        return
    
    can_create, limit_msg = await check_action_limit(user, "create_project")
    if not can_create and not user.is_admin:
        await update.message.reply_text(f"❌ {limit_msg}")
        context.user_data['awaiting_project_name'] = False
        return
    
    async with AsyncSessionLocal() as session:
        project = Project(
            user_id=telegram_id,
            name=name,
            check_interval_minutes=user.min_check_interval_minutes,
            post_interval_hours=max(user.min_post_interval_minutes // 60, 1),
            active_hours_start=Config.DEFAULT_ACTIVE_HOURS_START,
            active_hours_end=Config.DEFAULT_ACTIVE_HOURS_END
        )
        session.add(project)
        await session.commit()
        context.user_data[CURRENT_PROJECT_KEY] = project.id
    
    context.user_data['awaiting_project_name'] = False
    
    await update.message.reply_text(
        f"✅ Проект «{name}» создан!\n\n"
        f"Теперь добавьте:\n"
        f"• /add_target — целевой канал\n"
        f"• /add_source — каналы-источники\n\n"
        f"💡 После добавления источников проект начнёт работу автоматически."
    )


async def show_project_stats(query, project_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one()
    
    sources_count = await get_sources_count(project_id)
    target = await get_project_target(project_id)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PostQueue).where(PostQueue.project_id == project_id, PostQueue.status == "pending")
        )
        pending = len(result.scalars().all())
    
    target_name = 'не задана'
    platform_name = '—'
    if target:
        if target.platform == 'telegram':
            target_name = target.channel_title or 'Канал Telegram'
            platform_name = 'Telegram'
        elif target.platform == 'vk':
            target_name = target.vk_group_name or 'Группа VK'
            platform_name = 'VK'
    
    text = (
        f"📊 <b>Статистика «{project.name}»</b>\n\n"
        f"📥 Источников: {sources_count}\n"
        f"📤 Цель ({platform_name}): {target_name}\n"
        f"⏰ Интервал парсинга: {project.check_interval_minutes} мин\n"
        f"📅 Интервал публикации: {project.post_interval_hours} ч\n"
        f"📈 Сегодня: спарсено {project.posts_parsed_today}, опубликовано {project.posts_posted_today}\n"
        f"📬 В очереди: {pending}"
    )
    
    keyboard = [[InlineKeyboardButton("◀️ Назад к проектам", callback_data="back_to_projects")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def back_to_projects_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await my_projects(update, context)