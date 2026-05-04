import asyncio
import logging
from datetime import datetime
from sqlalchemy import select
from database import AsyncSessionLocal
from models import PostQueue
from posters import TelegramPoster, VKPoster

logger = logging.getLogger(__name__)


class PostScheduler:
    """Планировщик публикации постов из очереди."""
    
    def __init__(self, telegram_poster: TelegramPoster):
        self.telegram_poster = telegram_poster
        self.vk_poster = VKPoster()
        self._running = False

    async def start(self):
        self._running = True
        logger.info("🟢 PostScheduler started")
        
        while self._running:
            try:
                await self._check_and_publish()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"PostScheduler error: {e}")
                await asyncio.sleep(60)

    async def _check_and_publish(self):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PostQueue).where(
                    PostQueue.status == "pending",
                    PostQueue.scheduled_time <= datetime.utcnow()
                ).order_by(PostQueue.scheduled_time).limit(10)
            )
            pending_items = result.scalars().all()
        
        if not pending_items:
            return
        
        logger.info(f"📤 Processing {len(pending_items)} pending posts")
        
        for queue_item in pending_items:
            try:
                if queue_item.platform == "vk":
                    success = await self.vk_poster.publish_post(queue_item)
                else:
                    success = await self.telegram_poster.publish_post(queue_item)
                
                if success:
                    logger.info(f"✅ Published post {queue_item.id} to {queue_item.platform}")
                else:
                    logger.warning(f"❌ Failed to publish post {queue_item.id}")
                
                await asyncio.sleep(3)
                
            except Exception as e:
                logger.error(f"Error publishing post {queue_item.id}: {e}")

    async def stop(self):
        self._running = False
        logger.info("🔴 PostScheduler stopped")