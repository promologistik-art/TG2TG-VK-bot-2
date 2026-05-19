import os
import asyncio
import logging
from datetime import datetime, timedelta
from config import Config

logger = logging.getLogger(__name__)


class TempCleaner:
    """Очистка временной папки от старых файлов (ежедневно в 3:00 МСК)."""
    
    def __init__(self, temp_dir: str = None, max_age_hours: int = 24):
        self.temp_dir = temp_dir or Config.TEMP_DIR
        self.max_age_hours = max_age_hours
        self._running = False
        self._task = None
    
    async def start(self):
        """Запустить авто-очистку."""
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("🟢 TempCleaner started (daily at 03:00 MSK)")
    
    async def _get_next_cleanup_time(self) -> datetime:
        """Возвращает следующее время очистки (сегодня в 3:00 МСК или завтра)."""
        from utils import get_moscow_time
        
        now = get_moscow_time()
        # Целевое время: сегодня в 3:00
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        
        if now >= target:
            # Если уже прошло 3:00, ждём до завтра
            target = target + timedelta(days=1)
        
        return target
    
    async def _cleanup_loop(self):
        """Цикл очистки (один раз в сутки в 3:00 МСК)."""
        while self._running:
            try:
                # Ждём до следующего времени очистки
                next_cleanup = await self._get_next_cleanup_time()
                wait_seconds = (next_cleanup - datetime.now()).total_seconds()
                
                if wait_seconds > 0:
                    logger.info(f"⏰ Next temp cleanup at {next_cleanup.strftime('%d.%m.%Y %H:%M')} MSK")
                    await asyncio.sleep(wait_seconds)
                
                if self._running:
                    await self._cleanup()
                    
            except Exception as e:
                logger.error(f"TempCleaner error: {e}")
                await asyncio.sleep(3600)  # Ждём час при ошибке
    
    async def _cleanup(self):
        """Очистка старых файлов."""
        if not os.path.exists(self.temp_dir):
            logger.warning(f"Temp directory not found: {self.temp_dir}")
            return
        
        from utils import get_moscow_time
        
        now = get_moscow_time()
        deleted_count = 0
        deleted_size = 0
        
        for filename in os.listdir(self.temp_dir):
            file_path = os.path.join(self.temp_dir, filename)
            if not os.path.isfile(file_path):
                continue
            
            try:
                file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                # Добавляем часовой пояс для корректного сравнения
                from pytz import timezone
                msk_tz = timezone("Europe/Moscow")
                file_mtime_msk = file_mtime.replace(tzinfo=msk_tz)
                
                age_hours = (now - file_mtime_msk).total_seconds() / 3600
                
                if age_hours > self.max_age_hours:
                    file_size = os.path.getsize(file_path)
                    os.remove(file_path)
                    deleted_count += 1
                    deleted_size += file_size
                    logger.debug(f"Deleted old file: {filename} (age: {age_hours:.1f} hours)")
            except Exception as e:
                logger.warning(f"Failed to delete {filename}: {e}")
        
        if deleted_count > 0:
            logger.info(f"🧹 TempCleaner: deleted {deleted_count} files ({deleted_size / 1024 / 1024:.2f} MB)")
        else:
            logger.info("🧹 TempCleaner: no old files to delete")
    
    async def stop(self):
        """Остановить авто-очистку."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("🔴 TempCleaner stopped")