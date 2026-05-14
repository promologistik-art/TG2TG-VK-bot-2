import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, update
from database import AsyncSessionLocal, is_post_parsed, mark_post_parsed
from models import User, Project, SourceChannel, TargetChannel, PostQueue, PublishedPost
from scrapers import TelegramScraper
from posters import TelegramPoster
from utils import calculate_score, get_moscow_time
from config import Config

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, poster: TelegramPoster):
        self.poster = poster
        self._running = False
        self._tasks = {}
        self._last_daily_report = None
        self._last_check = {}

    async def start(self):
        self._running = True
        logger.info("🟢 Scheduler started")
        
        while self._running:
            try:
                await self._check_projects()
                await self._check_daily_tasks()
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

    async def _check_daily_tasks(self):
        now = get_moscow_time()
        if now.hour == 9 and now.minute == 0:
            today = now.date()
            if self._last_daily_report != today:
                self._last_daily_report = today
                await self._send_daily_report()

    async def _send_daily_report(self):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User))
                users = result.scalars().all()
                users_count = len(users)
                
                result = await session.execute(select(Project).where(Project.is_active == True))
                projects = result.scalars().all()
                projects_count = len(projects)
                
                result = await session.execute(
                    select(SourceChannel).where(SourceChannel.is_active == True)
                )
                sources = result.scalars().all()
                sources_count = len(sources)
                
                total_parsed = sum(p.posts_parsed_today for p in projects)
                total_posted = sum(p.posts_posted_today for p in projects)
                
                result = await session.execute(
                    select(PostQueue).where(PostQueue.status == "pending")
                )
                pending = len(result.scalars().all())
                
                result = await session.execute(
                    select(PostQueue).where(PostQueue.status == "failed")
                )
                failed = len(result.scalars().all())
                
                sorted_projects = sorted(projects, key=lambda p: p.posts_posted_today, reverse=True)
                top3 = sorted_projects[:3]
            
            now = datetime.utcnow()
            date_str = now.strftime('%d.%m.%Y')
            
            text = f"📊 <b>Отчёт за {date_str}</b>\n\n"
            text += f"👥 Пользователей: {users_count}\n"
            text += f"📁 Проектов: {projects_count}\n"
            text += f"📥 Источников: {sources_count}\n"
            text += f"🔄 Спарсено сегодня: {total_parsed}\n"
            text += f"📤 Опубликовано сегодня: {total_posted}\n"
            text += f"📬 В очереди: {pending}\n"
            text += f"❌ Ошибок публикации: {failed}\n"
            
            if top3:
                text += f"\n🏆 <b>Топ-{len(top3)} активных проекта:</b>\n"
                for p in top3:
                    text += f"• «{p.name}» — {p.posts_posted_today} постов\n"
            
            from telegram import Bot
            bot = Bot(token=Config.BOT_TOKEN)
            await bot.send_message(
                chat_id=Config.ADMIN_ID,
                text=text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Daily report failed: {e}")

    async def _check_projects(self):
        now = datetime.utcnow()
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Project).where(Project.is_active == True))
            projects = result.scalars().all()
        
        for project in projects:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User).where(User.telegram_id == project.user_id))
                user = result.scalar_one_or_none()
                if not user:
                    continue
                
                if not user.is_admin:
                    has_access = False
                    if user.subscription_active and user.subscription_ends_at and user.subscription_ends_at > now:
                        has_access = True
                    elif user.trial_ends_at and user.trial_ends_at > now:
                        has_access = True
                    if not has_access:
                        continue
                
                interval = project.check_interval_minutes
                if not user.is_admin:
                    interval = max(interval, user.min_check_interval_minutes)
                
                last_check = self._last_check.get(project.id)
                if last_check:
                    elapsed = (now - last_check).total_seconds() / 60
                    if elapsed < interval:
                        continue
                
                self._last_check[project.id] = now
                
                task_key = f"project_{project.id}"
                if task_key not in self._tasks or self._tasks[task_key].done():
                    task = asyncio.create_task(self._process_project(project))
                    self._tasks[task_key] = task
                    logger.info(f"⏰ Project '{project.name}' (ID: {project.id}) scheduled")

    async def _process_project(self, project: Project):
        logger.info(f"🔍 Processing project '{project.name}' (ID: {project.id})")
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == project.user_id))
            user = result.scalar_one_or_none()
            if not user:
                return
            
            if not user.is_admin:
                has_access = False
                now = datetime.utcnow()
                if user.subscription_active and user.subscription_ends_at and user.subscription_ends_at > now:
                    has_access = True
                elif user.trial_ends_at and user.trial_ends_at > now:
                    has_access = True
                if not has_access:
                    return
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SourceChannel).where(SourceChannel.project_id == project.id, SourceChannel.is_active == True)
            )
            sources = result.scalars().all()
            
            result = await session.execute(
                select(TargetChannel).where(TargetChannel.project_id == project.id, TargetChannel.is_active == True)
            )
            target = result.scalar_one_or_none()
        
        if not sources or not target:
            logger.warning(f"⚠️ Project '{project.name}' has no sources or target")
            return
        
        logger.info(f"📊 Project '{project.name}': {len(sources)} sources → {target.channel_title or '—'}")
        
        posts_to_publish = []
        total_parsed = 0
        
        async with TelegramScraper() as scraper:
            for source in sources:
                logger.info(f"📡 Fetching @{source.channel_username}")
                
                try:
                    posts = await scraper.get_posts(source.channel_username, limit=100)
                    logger.info(f"📨 @{source.channel_username}: {len(posts)} posts fetched")
                except Exception as e:
                    logger.error(f"❌ Failed to fetch @{source.channel_username}: {e}")
                    continue
                
                best_post = None
                best_score = -1
                
                for post in posts:
                    if await is_post_parsed(project.id, post["url"]):
                        continue
                    
                    # Пропускаем рекламу
                    if post.get("is_advertisement", False):
                        continue
                    
                    # ЖЁСТКАЯ ПРОВЕРКА ФИЛЬТРА МЕДИА (первичная)
                    media_type = post.get("media_type")
                    has_media = post.get("has_media", False)
                    
                    if source.media_filter == "photo_only":
                        if not has_media or media_type != "photo":
                            logger.debug(f"⏭️ Skipping @{source.channel_username}: not a photo (type={media_type})")
                            continue
                    
                    elif source.media_filter == "video_only":
                        if not has_media or media_type != "video":
                            logger.debug(f"⏭️ Skipping @{source.channel_username}: not a video (type={media_type})")
                            continue
                    
                    post["source_username"] = source.channel_username
                    post["source_title"] = source.channel_title
                    post["media_filter"] = source.media_filter
                    post["remove_original_text"] = source.remove_original_text
                    post["max_video_duration"] = source.max_video_duration
                    post["exclude_phrases"] = source.exclude_phrases
                    
                    post_time = datetime.utcnow()
                    if post.get("datetime"):
                        try:
                            post_time = datetime.fromisoformat(post["datetime"].replace("Z", "+00:00"))
                        except:
                            pass
                    
                    score, is_fallback = calculate_score(post, source.criteria, post_time)
                    
                    if is_fallback:
                        continue
                    
                    if score > best_score:
                        best_score = score
                        best_post = post
                
                if best_post:
                    # Проверка длительности видео
                    if source.max_video_duration and source.max_video_duration > 0:
                        video_dur = best_post.get("video_duration", 0)
                        if video_dur > 0 and video_dur > source.max_video_duration:
                            logger.info(
                                f"⏰ Video too long from @{source.channel_username}: "
                                f"{video_dur}s > {source.max_video_duration}s max"
                            )
                            continue
                    
                    # ПОВТОРНАЯ ПРОВЕРКА ФИЛЬТРА МЕДИА (после выбора лучшего поста)
                    media_type = best_post.get("media_type")
                    has_media = best_post.get("has_media", False)
                    
                    if source.media_filter == "photo_only":
                        if not has_media or media_type != "photo":
                            logger.info(
                                f"⚠️ FINAL CHECK FAIL: @{source.channel_username} "
                                f"media_filter=photo_only but post has media={has_media}, type={media_type}"
                            )
                            continue
                    
                    elif source.media_filter == "video_only":
                        if not has_media or media_type != "video":
                            logger.info(
                                f"⚠️ FINAL CHECK FAIL: @{source.channel_username} "
                                f"media_filter=video_only but post has media={has_media}, type={media_type}"
                            )
                            continue
                    
                    logger.info(
                        f"🏆 Selected from @{source.channel_username}: "
                        f"score={best_score}, type={media_type}, "
                        f"duration={best_post.get('video_duration', 0)}s"
                    )
                    
                    await mark_post_parsed(project.id, source.id, best_post["url"])
                    total_parsed += 1
                    
                    # СКАЧИВАНИЕ МЕДИА
                    media_downloaded = False
                    if best_post.get("has_media") and best_post.get("media_url"):
                        ext = "jpg" if best_post.get("media_type") == "photo" else "mp4"
                        filename = f"{uuid.uuid4()}.{ext}"
                        media_path = os.path.join(Config.TEMP_DIR, filename)
                        
                        if await scraper.download_media(best_post["media_url"], media_path):
                            best_post["media_path"] = media_path
                            media_downloaded = True
                            logger.info(f"💾 Media saved: {media_path}")
                        else:
                            logger.warning(f"⚠️ Media download failed for @{source.channel_username}")
                    
                    # КРИТИЧЕСКАЯ ПРОВЕРКА: media_filter требует медиа, но медиа не скачалось
                    if source.media_filter in ("photo_only", "video_only"):
                        if not media_downloaded:
                            logger.info(
                                f"🚫 BLOCKED: media_filter={source.media_filter} but media download failed "
                                f"for @{source.channel_username}"
                            )
                            continue
                    
                    # ПРОВЕРКА: remove_original_text без медиа
                    if source.remove_original_text and not media_downloaded:
                        logger.info(
                            f"📝 Skipping post (text removed, no media) from @{source.channel_username}"
                        )
                        continue
                    
                    # ПРОВЕРКА: пустой пост
                    has_text = bool(best_post.get("text", "").strip())
                    if not has_text and not media_downloaded:
                        logger.info(f"📭 Empty post from @{source.channel_username}, skipping")
                        continue
                    
                    posts_to_publish.append(best_post)
                    
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(SourceChannel)
                            .where(SourceChannel.id == source.id)
                            .values(last_parsed=datetime.utcnow(), last_post_url=best_post["url"])
                        )
                        await session.commit()
                else:
                    logger.info(f"😴 @{source.channel_username}: no suitable posts")
        
        if posts_to_publish:
            logger.info(f"📤 Found {len(posts_to_publish)} posts to queue")
            
            interval_minutes = max(
                int(project.post_interval_hours * 60),
                user.min_post_interval_minutes,
                Config.MIN_POST_INTERVAL_MINUTES
            )
            
            msk_now = get_moscow_time().replace(tzinfo=None)
            
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(PostQueue).where(
                        PostQueue.project_id == project.id
                    ).order_by(PostQueue.scheduled_time.desc()).limit(1)
                )
                last_queued = result.scalar_one_or_none()
            
            if last_queued:
                last_msk = last_queued.scheduled_time + timedelta(hours=3)
                next_time = last_msk + timedelta(minutes=interval_minutes)
                if next_time < msk_now:
                    slots_passed = ((msk_now - next_time).total_seconds() / 60) // interval_minutes + 1
                    next_time = next_time + timedelta(minutes=slots_passed * interval_minutes)
            else:
                start_hour = project.active_hours_start
                next_time = msk_now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                if next_time < msk_now:
                    next_time = next_time + timedelta(days=1)
            
            for i, post in enumerate(posts_to_publish):
                if i > 0:
                    next_time = next_time + timedelta(minutes=interval_minutes)
                
                utc_time = next_time - timedelta(hours=3)
                
                await self.poster.add_to_queue(
                    project_id=project.id,
                    target_channel_id=target.id,
                    post_data=post,
                    scheduled_time=utc_time,
                    platform=target.platform
                )
                logger.info(f"📅 Post {i+1} scheduled for {next_time.strftime('%d.%m.%Y %H:%M')} MSK")
            
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Project).where(Project.id == project.id))
                db_project = result.scalar_one()
                today = datetime.utcnow().date()
                if db_project.last_reset.date() < today:
                    db_project.posts_parsed_today = 0
                    db_project.posts_posted_today = 0
                    db_project.last_reset = datetime.utcnow()
                db_project.posts_parsed_today += total_parsed
                await session.commit()
        
        logger.info(f"✅ Project '{project.name}' processing completed")

    async def stop(self):
        self._running = False
        for task_key, task in self._tasks.items():
            if not task.done():
                task.cancel()
        logger.info("🔴 Scheduler stopped")