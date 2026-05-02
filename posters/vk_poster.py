import os
import logging
import aiohttp
from datetime import datetime
from sqlalchemy import select, update
from database import AsyncSessionLocal
from models import PostQueue, PublishedPost, TargetChannel, Project
from utils import clean_caption

logger = logging.getLogger(__name__)


class VKPoster:
    def __init__(self):
        self.api_url = "https://api.vk.com/method/"
        self.api_version = "5.199"

    async def publish_post(self, queue_item: PostQueue) -> bool:
        try:
            post_data = queue_item.post_data
            
            # Получаем данные целевого канала
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TargetChannel).where(TargetChannel.id == queue_item.target_channel_id)
                )
                target = result.scalar_one_or_none()
                
                if not target or not target.vk_token:
                    await self._mark_failed(queue_item, "VK token not configured")
                    return False
                
                vk_token = target.vk_token
                group_id = target.vk_group_id
            
            # Удаление текста если настроено
            remove_text = post_data.get("remove_original_text", False)
            if remove_text:
                caption = ""
            else:
                caption = clean_caption(post_data.get("text", ""))
            
            # Подпись проекта
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Project).where(Project.id == queue_item.project_id))
                project = result.scalar_one_or_none()
                signature = project.signature if project else None
            
            if signature:
                # Для VK убираем HTML-теги из подписи
                import re
                plain_signature = re.sub(r'<[^>]+>', '', signature)
                if caption:
                    caption += f"\n\n{plain_signature}"
                else:
                    caption = plain_signature
            
            media_path = post_data.get("media_path")
            media_type = post_data.get("media_type")
            
            attachments = []
            
            # Загружаем медиа в VK
            if media_path and os.path.exists(media_path):
                if media_type == "photo":
                    photo_info = await self._upload_photo(vk_token, group_id, media_path)
                    if photo_info:
                        attachments.append(f"photo{photo_info['owner_id']}_{photo_info['id']}")
                elif media_type == "video":
                    video_info = await self._upload_video(vk_token, group_id, media_path, caption or "Video")
                    if video_info:
                        attachments.append(f"video{video_info['owner_id']}_{video_info['id']}")
            
            # Публикуем пост
            success = await self._wall_post(vk_token, group_id, caption, attachments)
            
            if success:
                try:
                    os.remove(media_path)
                except:
                    pass
                await self._mark_published(queue_item, target)
                return True
            else:
                await self._mark_failed(queue_item, "VK wall.post failed")
                return False
                
        except Exception as e:
            logger.error(f"VK publish error: {e}")
            await self._mark_failed(queue_item, str(e)[:200])
            return False

    async def _upload_photo(self, token: str, group_id: int, file_path: str) -> dict:
        """Загрузка фото в VK."""
        try:
            # Получаем URL для загрузки
            params = {"access_token": token, "v": self.api_version, "group_id": abs(group_id)}
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_url}photos.getWallUploadServer", params=params) as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"VK getWallUploadServer error: {data['error']}")
                        return None
                    upload_url = data["response"]["upload_url"]
                
                # Загружаем файл
                with open(file_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field("photo", f, filename=os.path.basename(file_path))
                    async with session.post(upload_url, data=form) as resp:
                        upload_data = await resp.json()
                
                # Сохраняем фото
                params = {
                    "access_token": token, "v": self.api_version,
                    "group_id": abs(group_id),
                    "photo": upload_data["photo"],
                    "server": upload_data["server"],
                    "hash": upload_data["hash"]
                }
                async with session.get(f"{self.api_url}photos.saveWallPhoto", params=params) as resp:
                    save_data = await resp.json()
                    if "error" in save_data:
                        logger.error(f"VK saveWallPhoto error: {save_data['error']}")
                        return None
                    return save_data["response"][0]
        except Exception as e:
            logger.error(f"VK upload photo error: {e}")
        return None

    async def _upload_video(self, token: str, group_id: int, file_path: str, title: str) -> dict:
        """Загрузка видео в VK."""
        try:
            params = {
                "access_token": token, "v": self.api_version,
                "group_id": abs(group_id),
                "name": title[:255]
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_url}video.save", params=params) as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"VK video.save error: {data['error']}")
                        return None
                    upload_url = data["response"]["upload_url"]
                
                # Загружаем видео
                with open(file_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field("video_file", f, filename=os.path.basename(file_path))
                    async with session.post(upload_url, data=form) as resp:
                        upload_data = await resp.json()
                
                return {
                    "owner_id": upload_data.get("owner_id", -group_id),
                    "id": upload_data.get("video_id", 0)
                }
        except Exception as e:
            logger.error(f"VK upload video error: {e}")
        return None

    async def _wall_post(self, token: str, group_id: int, message: str, attachments: list) -> bool:
        """Публикация поста на стену."""
        try:
            params = {
                "access_token": token, "v": self.api_version,
                "owner_id": -abs(group_id),
                "from_group": 1,
                "message": message[:4096] if message else "",
                "attachments": ",".join(attachments) if attachments else ""
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.api_url}wall.post", params=params) as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"VK wall.post error: {data['error']}")
                        return False
                    return True
        except Exception as e:
            logger.error(f"VK wall.post error: {e}")
        return False

    async def _mark_published(self, queue_item: PostQueue, target: TargetChannel):
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PostQueue)
                .where(PostQueue.id == queue_item.id)
                .values(status="published", published_at=datetime.utcnow())
            )
            published = PublishedPost(
                project_id=queue_item.project_id,
                target_channel_id=queue_item.target_channel_id,
                platform="vk",
                source_channel_username=queue_item.post_data.get("source_username", ""),
                post_url=queue_item.post_data.get("url", ""),
                post_data=queue_item.post_data
            )
            session.add(published)
            await session.execute(
                update(TargetChannel)
                .where(TargetChannel.id == target.id)
                .values(last_posted=datetime.utcnow())
            )
            await session.commit()

    async def _mark_failed(self, queue_item: PostQueue, error_message: str):
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(PostQueue)
                .where(PostQueue.id == queue_item.id)
                .values(status="failed", error_message=error_message)
            )
            await session.commit()