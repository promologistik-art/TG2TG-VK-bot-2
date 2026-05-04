import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import delete
from database import AsyncSessionLocal
from models import TargetChannel
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message
from .constants import AWAITING_TARGET_FORWARD

logger = logging.getLogger(__name__)


async def add_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    
    project = await require_project(update, context)
    if not project:
        return ConversationHandler.END
    
    target = await get_project_target(project.id)
    if target:
        await update.message.reply_text(
            f"⚠️ В проекте уже есть цель: {target.channel_title or 'Канал'}\n"
            f"Удалите через /my_targets"
        )
        return ConversationHandler.END
    
    context.user_data['temp_project_id'] = project.id
    context.user_data['temp_project_name'] = project.name
    
    me = await context.bot.get_me()
    await update.message.reply_text(
        f"📤 Добавление целевого канала в «{project.name}»\n\n"
        f"1. Добавьте @{me.username} в администраторы канала\n"
        f"2. Выдайте боту права на публикацию сообщений\n"
        f"3. Перешлите сюда любое сообщение из этого канала\n\n"
        f"⚠️ Пересылать нужно именно из канала, не из избранного."
    )
    return AWAITING_TARGET_FORWARD


async def add_target_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    if not msg.forward_from_chat or msg.forward_from_chat.type != 'channel':
        await update.message.reply_text("❌ Перешлите сообщение из канала.")
        return AWAITING_TARGET_FORWARD
    
    chat = msg.forward_from_chat
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    try:
        test_msg = await context.bot.send_message(chat.id, "🔧 Проверка прав...")
        await test_msg.delete()
    except:
        await update.message.reply_text("❌ Бот не имеет прав администратора.")
        return AWAITING_TARGET_FORWARD
    
    async with AsyncSessionLocal() as session:
        channel = TargetChannel(
            project_id=project_id,
            platform="telegram",
            channel_id=chat.id,
            channel_username=chat.username,
            channel_title=chat.title
        )
        session.add(channel)
        await session.commit()
    
    await update.message.reply_text(
        f"✅ Канал «{chat.title}» добавлен!\n\n"
        f"Теперь добавьте источники: /add_source"
    )
    
    for key in ['temp_project_id', 'temp_project_name']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    if sources_count > 0:
        await send_project_ready_message(update, project_name)
    
    return ConversationHandler.END


async def my_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    project = await require_project(update, context)
    if not project:
        return
    
    target = await get_project_target(project.id)
    if not target:
        await update.message.reply_text(f"📭 В проекте «{project.name}» нет цели.\nДобавьте: /add_target")
        return
    
    text = f"🎯 Цель проекта «{project.name}»\n\n"
    text += f"📝 {target.channel_title}\n"
    if target.channel_username:
        text += f"🔗 @{target.channel_username}\n"
    
    keyboard = [[InlineKeyboardButton("❌ Удалить цель", callback_data=f"del_target_{target.id}")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def delete_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target_id = int(query.data.replace("del_target_", ""))
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TargetChannel).where(TargetChannel.id == target_id))
        await session.commit()
    await query.edit_message_text("✅ Цель удалена")