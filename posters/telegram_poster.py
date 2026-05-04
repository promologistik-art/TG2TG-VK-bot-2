import os
import logging
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from sqlalchemy import select, update
from database import AsyncSessionLocal
from models import PostQueue, PublishedPost, TargetChannel, Project
from utils import clean_caption
from config import Config

logger = logging.getLogger(__name__)


class TelegramPoster:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def add_to_queue(self, project_id: int, target_channel_id: int, post_data: dict, scheduled_time: datetime, platform: str = "telegram"):
        async with AsyncSessionLocal() as session:
            queue_item = PostQueue(
                project_id=project_id,
                target_channel_id=target_channel_id,
                platform=platform,
                post_data=post_data,
                scheduled_time=scheduled_time,
                status="pending"
            )
            session.add(queue_item)
            await session.commit()
            logger.info(f"📨 Post queued for project {project_id} ({platform})")

    async def publish_post(self, queue_item: PostQueue) -> bool:
        real_chat_id = None
        signature = None
        
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TargetChannel).where(TargetChannel.id == queue_item.target_channel_id)
                )
                target = result.scalar_one_or_none()
                if not target:
                    await self._mark_failed(queue_item, "Целевой канал не найден")
                    return False
                
                if target.platform == "vk":
                    await self._mark_failed(queue_item, "Пост для VK, используйте VK постер")
                    return False
                
                if not target.channel_id:
                    await self._mark_failed(queue_item, "Chat ID не указан")
                    return False
                
                real_chat_id = target.channel_id
                
                result = await session.execute(
                    select(Project).where(Project.id == queue_item.project_id)
                )
                project = result.scalar_one_or_none()
                signature = project.signature if project else None
        except Exception as e:
            logger.error(f"Failed to get target info: {e}")
            await self._mark_failed(queue_item, "Ошибка получения данных канала")
            return False
        
        post_data = queue_item.post_data
        
        remove_text = post_data.get("remove_original_text", False)
        if remove_text:
            caption = ""
        else:
            caption = clean_caption(post_data.get("text", ""))
        
        if signature:
            if caption:
                caption += f"\n\n{signature}"
            else:
                caption = signature
        
        if Config.SHOW_SOURCE_SIGNATURE:
            source = post_data.get("source_username", "")
            if source:
                if caption:
                    caption += f"\n\n📡 @{source}"
                else:
                    caption = f"📡 @{source}"
        
        media_path = post_data.get("media_path")
        media_type = post_data.get("media_type")
        
        if not caption and not (media_path and os.path.exists(media_path)):
            if remove_text:
                await self._mark_failed(queue_item, "Текст удалён, медиа нет")
            else:
                await self._mark_failed(queue_item, "Нет текста и медиа")
            return False
        
        parse_mode = None
        if caption and ("<a href=" in caption or "<b>" in caption or "<i>" in caption):
            parse_mode = "HTML"
        
        if media_path and os.path.exists(media_path):
            try:
                with open(media_path, "rb") as f:
                    if media_type == "photo":
                        await self.bot.send_photo(chat_id=real_chat_id, photo=f, caption=caption if caption else None, parse_mode=parse_mode)
                    elif media_type == "video":
                        await self.bot.send_video(chat_id=real_chat_id, video=f, caption=caption if caption else None, parse_mode=parse_mode)
                    else:
                        await self.bot.send_document(chat_id=real_chat_id, document=f, caption=caption if caption else None, parse_mode=parse_mode)
                
                try:
                    os.remove(media_path)
                except:
                    pass
                
                await self._mark_published(queue_item)
                logger.info(f"✅ Published post {queue_item.id} with media")
                return True
                
            except TelegramError as e:
                logger.error(f"Failed to send media: {e}")
                
                if parse_mode and "parse" in str(e).lower():
                    try:
                        with open(media_path, "rb") as f:
                            if media_type == "photo":
                                await self.bot.send_photo(chat_id=real_chat_id, photo=f, caption=caption if caption else None)
                            elif media_type == "video":
                                await self.bot.send_video(chat_id=real_chat_id, video=f, caption=caption if caption else None)
                            else:
                                await self.bot.send_document(chat_id=real_chat_id, document=f, caption=caption if caption else None)
                        try:
                            os.remove(media_path)
                        except:
                            pass
                        await self._mark_published(queue_item)
                        return True
                    except:
                        pass
                
                if caption:
                    try:
                        await self.bot.send_message(chat_id=real_chat_id, text=caption, disable_web_page_preview=True)
                        await self._mark_published(queue_item)
                        return True
                    except:
                        pass
                
                error_text = str(e)[:80].replace("\n", " ")
                await self._mark_failed(queue_item, f"Ошибка отправки: {error_text}")
                return False
        
        elif caption:
            try:
                await self.bot.send_message(chat_id=real_chat_id, text=caption, parse_mode=parse_mode, disable_web_page_preview=True)
                await self._mark_published(queue_item)
                return True
            except TelegramError as e:
                if parse_mode:
                    try:
                        await self.bot.send_message(chat_id=real_chat_id, text=caption, disable_web_page_preview=True)
                        await self._mark_published(queue_item)
                        return True
                    except:
                        pass
                error_text = str(e)[:80].replace("\n", " ")
                await self._mark_failed(queue_item, f"Ошибка отправки: {error_text}")
                return False
        
        await self._mark_failed(queue_item, "Пустой пост")
        return False

    async def _mark_published(self, queue_item: PostQueue):
        async with AsyncSessionLocal() as session:
            await session.execute(update(PostQueue).where(PostQueue.id == queue_item.id).values(status="published", published_at=datetime.utcnow()))
            published = PublishedPost(
                project_id=queue_item.project_id, target_channel_id=queue_item.target_channel_id,
                source_channel_username=queue_item.post_data.get("source_username", ""),
                post_url=queue_item.post_data.get("url", ""),
                post_data=queue_item.post_data
            )
            session.add(published)
            await session.execute(update(TargetChannel).where(TargetChannel.id == queue_item.target_channel_id).values(last_posted=datetime.utcnow()))
            await session.commit()

    async def _mark_failed(self, queue_item: PostQueue, error_message: str):
        clean_error = error_message[:150].replace("\n", " ").strip()
        async with AsyncSessionLocal() as session:
            await session.execute(update(PostQueue).where(PostQueue.id == queue_item.id).values(status="failed", error_message=clean_error))
            await session.commit()
            logger.warning(f"❌ Post {queue_item.id} failed: {clean_error}")