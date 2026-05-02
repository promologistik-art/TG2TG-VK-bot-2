import logging
import re
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import delete
from database import AsyncSessionLocal
from models import TargetChannel
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message
from .constants import AWAITING_TARGET_PLATFORM, AWAITING_TARGET_FORWARD, AWAITING_VK_TOKEN, AWAITING_VK_GROUP

logger = logging.getLogger(__name__)


async def resolve_vk_group(token: str, query: str) -> tuple:
    """
    Определяет ID группы VK по ссылке или короткому имени.
    Возвращает (group_id, group_name) или (None, error_message).
    """
    # Если прислали чистые цифры — это уже ID
    if query.isdigit():
        group_id = int(query)
        try:
            params = {"access_token": token, "v": "5.199", "group_ids": str(group_id), "fields": "name"}
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.vk.com/method/groups.getById", params=params) as resp:
                    data = await resp.json()
                    if "error" in data:
                        error_msg = data["error"].get("error_msg", "неизвестная ошибка")
                        logger.warning(f"VK API error for ID {group_id}: {error_msg}")
                        return (group_id, f"VK Group {group_id}")
                    if data.get("response"):
                        group_info = data["response"][0]
                        return (group_id, group_info.get("name", f"VK Group {group_id}"))
        except aiohttp.ClientError as e:
            logger.error(f"VK API request failed for ID {group_id}: {e}")
            return (None, f"Ошибка соединения с VK: {str(e)[:80]}")
        except Exception as e:
            logger.error(f"Unexpected error for ID {group_id}: {e}", exc_info=True)
            return (None, f"Ошибка: {str(e)[:80]}")
        return (group_id, f"VK Group {group_id}")
    
    # Извлекаем короткое имя из ссылки
    screen_name = None
    patterns = [
        r'(?:https?://)?vk\.(?:com|ru)/([a-zA-Z0-9_.]+)',
        r'(?:https?://)?vkvideo\.(?:com|ru)/@?([a-zA-Z0-9_.]+)',
        r'@([a-zA-Z0-9_.]+)',
        r'^([a-zA-Z0-9_.]+)$'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, query.strip())
        if match:
            screen_name = match.group(1)
            # Убираем служебные имена VK
            if screen_name.lower() in ['public', 'club', 'event', 'feed', 'im', 'id', 'dev', 'api', 'support', 'help']:
                continue
            break
    
    if not screen_name:
        return (None, "Не удалось распознать ссылку. Отправьте ссылку вида vk.com/mychannel или ID.")
    
    # Ищем группу по короткому имени через API
    try:
        params = {"access_token": token, "v": "5.199", "group_id": screen_name, "fields": "name"}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.vk.com/method/groups.getById", params=params) as resp:
                data = await resp.json()
                
                if "error" in data:
                    error_code = data["error"].get("error_code", 0)
                    if error_code == 100:
                        return (None, f"Сообщество «{screen_name}» не найдено. Проверьте правильность ссылки.")
                    elif error_code == 15:
                        return (None, f"Сообщество «{screen_name}» недоступно (закрыто).")
                    elif error_code == 5:
                        return (None, "Ключ доступа недействителен. Проверьте ключ и попробуйте снова.")
                    elif error_code == 6:
                        return (None, "Слишком много запросов. Попробуйте через минуту.")
                    else:
                        error_msg = data["error"].get("error_msg", "неизвестная ошибка")
                        logger.warning(f"VK API error: [{error_code}] {error_msg}")
                        return (None, f"Ошибка VK: {error_msg[:100]}")
                
                if data.get("response"):
                    group_info = data["response"][0]
                    group_id = group_info.get("id", 0)
                    group_name = group_info.get("name", screen_name)
                    return (group_id, group_name)
                
                return (None, f"Сообщество «{screen_name}» не найдено.")
                
    except aiohttp.ClientError as e:
        logger.error(f"VK API request failed: {e}")
        return (None, "Ошибка соединения с VK API. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Unexpected error resolving VK group: {e}", exc_info=True)
        return (None, f"Ошибка: {str(e)[:100]}")


async def add_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 0: Выбор платформы."""
    project = await require_project(update, context)
    if not project:
        return ConversationHandler.END
    
    target = await get_project_target(project.id)
    if target:
        platform_name = "Telegram" if target.platform == "telegram" else "VK"
        target_name = target.channel_title or target.vk_group_name or "—"
        await update.message.reply_text(
            f"⚠️ В проекте уже есть цель ({platform_name}): {target_name}\n"
            f"Удалите через /my_targets"
        )
        return ConversationHandler.END
    
    context.user_data['temp_project_id'] = project.id
    context.user_data['temp_project_name'] = project.name
    
    keyboard = [
        [InlineKeyboardButton("🟢 Telegram", callback_data="platform_telegram")],
        [InlineKeyboardButton("🔵 VK", callback_data="platform_vk")],
    ]
    
    await update.message.reply_text(
        f"📤 <b>Добавление цели в «{project.name}»</b>\n\n"
        f"Выберите платформу для публикации:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_TARGET_PLATFORM


async def add_target_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора платформы."""
    query = update.callback_query
    await query.answer()
    
    platform = query.data.replace("platform_", "")
    context.user_data['temp_platform'] = platform
    
    if platform == "telegram":
        me = await context.bot.get_me()
        await query.edit_message_text(
            f"🟢 <b>Добавление Telegram-канала</b>\n\n"
            f"1. Добавьте @{me.username} в администраторы канала\n"
            f"2. Выдайте боту права на публикацию сообщений\n"
            f"3. Перешлите сюда любое сообщение из этого канала\n\n"
            f"⚠️ Пересылать нужно именно из канала, не из избранного.",
            parse_mode="HTML"
        )
        return AWAITING_TARGET_FORWARD
    
    elif platform == "vk":
        await query.edit_message_text(
            f"🔵 <b>Добавление VK-сообщества</b>\n\n"
            f"<b>Шаг 1 из 2:</b> Отправьте ключ доступа сообщества VK.\n\n"
            f"<b>Как получить ключ:</b>\n"
            f"1. Создайте сообщество (группу/паблик) в VK\n"
            f"2. Перейдите: Управление → Дополнительно → Работа с API\n"
            f"3. Нажмите «Создать ключ»\n"
            f"4. Отметьте права: <b>wall, photos, video, groups</b>\n"
            f"5. Подтвердите создание ключа\n"
            f"6. Скопируйте ключ и отправьте его сюда\n\n"
            f"🔐 <i>Ключ сохраняется только для этого проекта.\n"
            f"Не передавайте ключ третьим лицам.</i>",
            parse_mode="HTML"
        )
        return AWAITING_VK_TOKEN


async def add_target_vk_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ключа доступа VK."""
    token = update.message.text.strip()
    
    if len(token) < 20:
        await update.message.reply_text(
            "❌ Ключ слишком короткий.\n"
            "Отправьте полный ключ доступа сообщества из настроек VK."
        )
        return AWAITING_VK_TOKEN
    
    context.user_data['temp_vk_token'] = token
    
    await update.message.reply_text(
        f"🔵 <b>Шаг 2 из 2:</b> Отправьте ссылку или ID сообщества VK.\n\n"
        f"<b>Примеры:</b>\n"
        f"• <code>https://vk.com/mychannel</code>\n"
        f"• <code>https://vk.ru/mychannel</code>\n"
        f"• <code>https://vkvideo.ru/@mychannel</code>\n"
        f"• <code>public123456</code>\n"
        f"• <code>123456</code>\n\n"
        f"🤖 Бот сам определит ID сообщества.",
        parse_mode="HTML"
    )
    return AWAITING_VK_GROUP


async def add_target_vk_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение VK-цели с автоопределением ID."""
    query_text = update.message.text.strip()
    
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    vk_token = context.user_data.get('temp_vk_token')
    
    msg = await update.message.reply_text("🔍 Определяю ID сообщества...")
    
    try:
        group_id, result = await resolve_vk_group(vk_token, query_text)
    except Exception as e:
        logger.error(f"resolve_vk_group failed for '{query_text}': {e}", exc_info=True)
        group_id = None
        result = f"Ошибка при определении ID: {str(e)[:150]}"
    
    if group_id is None:
        await msg.edit_text(
            f"❌ {result}\n\n"
            f"Попробуйте:\n"
            f"• Открыть сообщество VK в браузере\n"
            f"• Скопировать ссылку из адресной строки\n"
            f"• Или отправьте ID цифрами: <code>123456</code>\n\n"
            f"<b>Принимаются ссылки:</b>\n"
            f"• <code>vk.com/mychannel</code>\n"
            f"• <code>vk.ru/mychannel</code>\n"
            f"• <code>vkvideo.ru/@mychannel</code>"
        )
        return AWAITING_VK_GROUP
    
    group_name = result
    
    async with AsyncSessionLocal() as session:
        channel = TargetChannel(
            project_id=project_id,
            platform="vk",
            vk_token=vk_token,
            vk_group_id=group_id,
            vk_group_name=group_name
        )
        session.add(channel)
        await session.commit()
        logger.info(f"Added VK target: {group_name} (ID: {group_id}) to project {project_id}")
    
    await msg.edit_text(
        f"✅ <b>VK-сообщество добавлено!</b>\n\n"
        f"📝 Название: <b>{group_name}</b>\n"
        f"🆔 ID: <code>{group_id}</code>\n"
        f"🔐 Ключ доступа сохранён\n\n"
        f"💡 Теперь добавьте источники через /add_source\n"
        f"Бот будет парсить каналы и публиковать посты в это сообщество VK."
    )
    
    for key in ['temp_project_id', 'temp_project_name', 'temp_platform', 'temp_vk_token']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    if sources_count > 0:
        await send_project_ready_message(update, project_name)
    
    return ConversationHandler.END


async def add_target_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление Telegram-канала через пересланное сообщение."""
    msg = update.message
    
    if not msg.forward_from_chat or msg.forward_from_chat.type != 'channel':
        await update.message.reply_text(
            "❌ Это не пересланное сообщение из канала.\n"
            "Перешлите любое сообщение из целевого Telegram-канала."
        )
        return AWAITING_TARGET_FORWARD
    
    chat = msg.forward_from_chat
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    try:
        test_msg = await context.bot.send_message(chat.id, "🔧 Проверка прав доступа...")
        await test_msg.delete()
    except Exception as e:
        logger.error(f"Bot permission check failed: {e}")
        await update.message.reply_text(
            "❌ Бот не имеет прав администратора в этом канале.\n\n"
            "Убедитесь, что:\n"
            "• Бот добавлен в администраторы канала\n"
            "• У бота есть право публиковать сообщения"
        )
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
        logger.info(f"Added Telegram target: {chat.title} (ID: {chat.id})")
    
    await update.message.reply_text(
        f"✅ Канал «{chat.title}» добавлен!\n\n"
        f"Теперь добавьте источники: /add_source"
    )
    
    for key in ['temp_project_id', 'temp_project_name', 'temp_platform']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    if sources_count > 0:
        await send_project_ready_message(update, project_name)
    
    return ConversationHandler.END


async def my_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать цель проекта."""
    project = await require_project(update, context)
    if not project:
        return
    
    target = await get_project_target(project.id)
    
    if not target:
        await update.message.reply_text(
            f"📭 В проекте «{project.name}» нет цели.\n"
            f"Добавьте через /add_target"
        )
        return
    
    text = f"🎯 <b>Цель проекта «{project.name}»</b>\n\n"
    
    if target.platform == "telegram":
        text += f"🟢 <b>Платформа:</b> Telegram\n"
        text += f"📝 <b>Канал:</b> {target.channel_title}\n"
        if target.channel_username:
            text += f"🔗 @{target.channel_username}\n"
        text += f"🆔 <code>{target.channel_id}</code>\n"
        if target.last_posted:
            text += f"📤 Последняя публикация: {target.last_posted.strftime('%d.%m.%Y %H:%M')}\n"
    
    elif target.platform == "vk":
        text += f"🔵 <b>Платформа:</b> VK\n"
        text += f"📝 <b>Сообщество:</b> {target.vk_group_name or 'Группа VK'}\n"
        text += f"🆔 <code>{target.vk_group_id}</code>\n"
        text += f"🔐 Ключ: {'установлен' if target.vk_token else 'не установлен'}\n"
        if target.last_posted:
            text += f"📤 Последняя публикация: {target.last_posted.strftime('%d.%m.%Y %H:%M')}\n"
    
    keyboard = [[InlineKeyboardButton("❌ Удалить цель", callback_data=f"del_target_{target.id}")]]
    
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def delete_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить цель."""
    query = update.callback_query
    await query.answer()
    
    target_id = int(query.data.replace("del_target_", ""))
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TargetChannel).where(TargetChannel.id == target_id))
        await session.commit()
        logger.info(f"Deleted target {target_id}")
    
    await query.edit_message_text("✅ Цель удалена")