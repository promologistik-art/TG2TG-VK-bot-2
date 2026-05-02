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
    """Определяет ID группы VK по ссылке или короткому имени."""
    
    if query.isdigit():
        group_id = int(query)
        try:
            params = {"access_token": token, "v": "5.199", "group_ids": str(group_id), "fields": "name"}
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.vk.com/method/groups.getById", params=params) as resp:
                    data = await resp.json()
                    if "error" in data:
                        return (group_id, f"VK Group {group_id}")
                    
                    resp_data = data.get("response", {})
                    if isinstance(resp_data, list) and len(resp_data) > 0:
                        group_info = resp_data[0]
                        return (group_id, group_info.get("name", f"VK Group {group_id}"))
                    elif isinstance(resp_data, dict) and "groups" in resp_data and len(resp_data["groups"]) > 0:
                        group_info = resp_data["groups"][0]
                        return (group_id, group_info.get("name", f"VK Group {group_id}"))
        except aiohttp.ClientError as e:
            logger.error(f"VK API request failed: {e}")
            return (None, f"Ошибка соединения: {str(e)[:80]}")
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return (None, f"Ошибка: {str(e)[:80]}")
        return (group_id, f"VK Group {group_id}")
    
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
            if screen_name.lower() in ['public', 'club', 'event', 'feed', 'im', 'id', 'dev', 'api', 'support', 'help']:
                continue
            break
    
    if not screen_name:
        return (None, "Не удалось распознать ссылку. Отправьте ссылку вида vk.com/mychannel или ID.")
    
    try:
        params = {"access_token": token, "v": "5.199", "group_id": screen_name, "fields": "name"}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.vk.com/method/groups.getById", params=params) as resp:
                data = await resp.json()
                
                if "error" in data:
                    error_code = data["error"].get("error_code", 0)
                    if error_code == 100:
                        return (None, f"Сообщество «{screen_name}» не найдено.")
                    elif error_code == 15:
                        return (None, f"Сообщество «{screen_name}» недоступно.")
                    elif error_code == 5:
                        return (None, "Ключ доступа недействителен.")
                    else:
                        error_msg = data["error"].get("error_msg", "неизвестная ошибка")
                        return (None, f"Ошибка VK: {error_msg[:100]}")
                
                # Ответ может быть списком или объектом с ключом groups
                resp_data = data.get("response", {})
                if isinstance(resp_data, list) and len(resp_data) > 0:
                    group_info = resp_data[0]
                elif isinstance(resp_data, dict) and "groups" in resp_data and len(resp_data["groups"]) > 0:
                    group_info = resp_data["groups"][0]
                else:
                    return (None, f"Сообщество «{screen_name}» не найдено.")
                
                group_id = group_info.get("id", 0)
                group_name = group_info.get("name", screen_name)
                return (group_id, group_name)
                
    except aiohttp.ClientError as e:
        logger.error(f"VK API request failed: {e}")
        return (None, "Ошибка соединения с VK API.")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return (None, f"Ошибка: {str(e)[:100]}")


async def add_target_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    
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
        f"Выберите платформу:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_TARGET_PLATFORM


async def add_target_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    platform = query.data.replace("platform_", "")
    context.user_data['temp_platform'] = platform
    
    if platform == "telegram":
        me = await context.bot.get_me()
        await query.edit_message_text(
            f"🟢 <b>Добавление Telegram-канала</b>\n\n"
            f"1. Добавьте @{me.username} в администраторы канала\n"
            f"2. Выдайте боту права на публикацию\n"
            f"3. Перешлите сюда любое сообщение из этого канала",
            parse_mode="HTML"
        )
        return AWAITING_TARGET_FORWARD
    
    elif platform == "vk":
        await query.edit_message_text(
            f"🔵 <b>Добавление VK-сообщества</b>\n\n"
            f"<b>Шаг 1 из 2:</b> Отправьте ссылку или ID сообщества VK.\n\n"
            f"<b>Примеры:</b>\n"
            f"• <code>https://vk.com/mychannel</code>\n"
            f"• <code>https://vk.ru/mychannel</code>\n"
            f"• <code>https://vkvideo.ru/@mychannel</code>\n"
            f"• <code>public123456</code>\n"
            f"• <code>123456</code>",
            parse_mode="HTML"
        )
        return AWAITING_VK_GROUP


async def add_target_vk_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    context.user_data['temp_vk_group'] = query_text
    
    await update.message.reply_text(
        f"🔵 <b>Шаг 2 из 2:</b> Отправьте ключ доступа сообщества VK.\n\n"
        f"<b>Как получить ключ:</b>\n"
        f"1. Создайте сообщество в VK\n"
        f"2. Перейдите: Управление → Дополнительно → Работа с API\n"
        f"3. Нажмите «Создать ключ»\n"
        f"4. Отметьте права: <b>wall, photos, video, groups</b>\n"
        f"5. Скопируйте ключ и отправьте его сюда\n\n"
        f"🔐 <i>Ключ сохраняется только для этого проекта.</i>",
        parse_mode="HTML"
    )
    return AWAITING_VK_TOKEN


async def add_target_vk_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    
    if len(token) < 20:
        await update.message.reply_text("❌ Ключ слишком короткий.")
        return AWAITING_VK_TOKEN
    
    query_text = context.user_data.get('temp_vk_group')
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    msg = await update.message.reply_text("🔍 Проверяю доступ к сообществу...")
    
    try:
        group_id, result = await resolve_vk_group(token, query_text)
    except Exception as e:
        logger.error(f"resolve_vk_group failed: {e}", exc_info=True)
        group_id = None
        result = f"Ошибка: {str(e)[:150]}"
    
    if group_id is None:
        await msg.edit_text(
            f"❌ {result}\n\nПроверьте ссылку и ключ, попробуйте снова.\nОтправьте ключ ещё раз:"
        )
        return AWAITING_VK_TOKEN
    
    group_name = result
    
    async with AsyncSessionLocal() as session:
        channel = TargetChannel(
            project_id=project_id, platform="vk",
            vk_token=token, vk_group_id=group_id, vk_group_name=group_name
        )
        session.add(channel)
        await session.commit()
        logger.info(f"Added VK target: {group_name} (ID: {group_id})")
    
    await msg.edit_text(
        f"✅ <b>VK-сообщество добавлено!</b>\n\n"
        f"📝 Название: <b>{group_name}</b>\n"
        f"🆔 ID: <code>{group_id}</code>\n"
        f"🔐 Ключ сохранён\n\n"
        f"💡 Теперь добавьте источники через /add_source"
    )
    
    for key in ['temp_project_id', 'temp_project_name', 'temp_platform', 'temp_vk_group']:
        context.user_data.pop(key, None)
    
    sources_count = await get_sources_count(project_id)
    if sources_count > 0:
        await send_project_ready_message(update, project_name)
    
    return ConversationHandler.END


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
            project_id=project_id, platform="telegram",
            channel_id=chat.id, channel_username=chat.username, channel_title=chat.title
        )
        session.add(channel)
        await session.commit()
    
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
    project = await require_project(update, context)
    if not project:
        return
    
    target = await get_project_target(project.id)
    if not target:
        await update.message.reply_text(f"📭 В проекте «{project.name}» нет цели.\nДобавьте: /add_target")
        return
    
    text = f"🎯 <b>Цель проекта «{project.name}»</b>\n\n"
    
    if target.platform == "telegram":
        text += f"🟢 <b>Telegram</b>\n📝 {target.channel_title}\n"
    elif target.platform == "vk":
        text += f"🔵 <b>VK</b>\n📝 {target.vk_group_name or 'Группа VK'}\n"
    
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