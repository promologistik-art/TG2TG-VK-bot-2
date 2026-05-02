import aiohttp
import asyncio
import re
import logging
import json
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
from config import Config
from utils import parse_number

logger = logging.getLogger(__name__)

# Маркеры рекламы
AD_KEYWORDS = [
    '#реклама', '#спонсор', '#партнер', '#партнёр', '#ad', '#рекламныйпост',
    'реклама', 'спонсор', 'партнёрский', 'промо', 'сообщение от партнёра',
    'на правах рекламы', 'платное размещение', 'спонсируется', 'advertisement',
    '#sponsored', '#promo', '#sponsor'
]


class TelegramScraper:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        headers = {
            "User-Agent": Config.SCRAPER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        self.session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _fetch(self, url: str) -> Optional[str]:
        for attempt in range(Config.SCRAPER_RETRIES):
            try:
                async with self.session.get(url, timeout=Config.SCRAPER_TIMEOUT) as resp:
                    if resp.status == 200:
                        return await resp.text()
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
                await asyncio.sleep(2)
        return None

    async def get_channel_info(self, username: str) -> Optional[Dict]:
        url = f"https://t.me/{username}"
        html = await self._fetch(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("meta", property="og:title")
        title = title_tag["content"] if title_tag else username
        title = title.replace("Telegram: Contact @", "").strip()
        return {"username": username, "title": title}

    async def get_posts(self, username: str, limit: int = 10) -> List[Dict]:
        url = f"https://t.me/s/{username}"
        html = await self._fetch(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        posts = []
        for msg_div in soup.find_all("div", class_="tgme_widget_message")[:limit]:
            try:
                post = self._parse_message(msg_div, username)
                if post:
                    if self._is_advertisement(post):
                        logger.debug(f"Skipping ad: {post.get('url', '')}")
                        continue
                    posts.append(post)
            except Exception as e:
                logger.error(f"Parse error: {e}")
        posts.sort(key=lambda x: x.get("datetime", ""), reverse=True)
        return posts

    def _is_advertisement(self, post: Dict) -> bool:
        text = post.get("text", "").lower()
        for keyword in AD_KEYWORDS:
            if keyword.lower() in text:
                return True
        return False

    def _is_forwarded(self, msg_div) -> bool:
        forwarded = msg_div.find("div", class_="tgme_widget_message_forwarded")
        if forwarded:
            return True
        if msg_div.get("data-forward"):
            return True
        return False

    def _has_external_tme_links(self, text: str, current_username: str = None) -> bool:
        links = re.findall(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', text)
        if current_username:
            links = [l for l in links if l != current_username]
        return len(links) > 0

    def _parse_message(self, msg_div, username: str) -> Optional[Dict]:
        data_post = msg_div.get("data-post")
        if not data_post:
            return None
        parts = data_post.split("/")
        if len(parts) < 2:
            return None
        message_id = parts[1]
        post_url = f"https://t.me/{username}/{message_id}"
        
        post_datetime = ""
        time_tag = msg_div.find("time")
        if time_tag and time_tag.has_attr("datetime"):
            post_datetime = time_tag["datetime"]
        
        text_div = msg_div.find("div", class_="tgme_widget_message_text")
        text = text_div.get_text(strip=False) if text_div else ""
        
        views = 0
        views_span = msg_div.find("span", class_="tgme_widget_message_views")
        if views_span:
            views = parse_number(views_span.get_text(strip=True))
        
        reactions = self._parse_reactions(msg_div)
        is_forwarded = self._is_forwarded(msg_div)
        has_external_links = self._has_external_tme_links(text, username)
        
        has_photo = False
        has_video = False
        media_url = None
        media_type = None
        
        photo_wrap = msg_div.find("a", class_="tgme_widget_message_photo_wrap")
        if photo_wrap:
            has_photo = True
            media_type = "photo"
            img = photo_wrap.find("img")
            if img:
                media_url = img.get("src")
            if not media_url:
                style = photo_wrap.get("style", "")
                bg_match = re.search(r"url\('(.+?)'\)", style)
                if bg_match:
                    media_url = bg_match.group(1)
        
        if not has_photo:
            gallery = msg_div.find("div", class_="tgme_widget_message_album_wrap")
            if gallery:
                first_photo = gallery.find("a", class_="tgme_widget_message_photo_wrap")
                if first_photo:
                    has_photo = True
                    media_type = "photo"
                    img = first_photo.find("img")
                    if img:
                        media_url = img.get("src")
        
        if not has_photo:
            video = msg_div.find("video")
            if video:
                src = video.get("src", "")
                if src and ("file/" in src or "video/" in src):
                    has_video = True
                    media_type = "video"
                    media_url = src
        
        if not has_photo and not has_video:
            round_video = msg_div.find("video", class_="tgme_widget_message_roundvideo")
            if round_video:
                src = round_video.get("src", "")
                if src:
                    has_video = True
                    media_type = "video"
                    media_url = src
        
        return {
            "url": post_url,
            "message_id": message_id,
            "text": text,
            "views": views,
            "reactions": reactions,
            "has_photo": has_photo,
            "has_video": has_video,
            "has_media": has_photo or has_video,
            "media_url": media_url,
            "media_type": media_type,
            "datetime": post_datetime,
            "is_forwarded": is_forwarded,
            "has_external_links": has_external_links,
            "is_advertisement": False
        }

    def _parse_reactions(self, msg_div) -> int:
        total = 0
        
        reactions_div = msg_div.find("div", class_="tgme_widget_message_reactions")
        if not reactions_div:
            return 0
        
        for span in reactions_div.find_all("span", class_="tgme_reaction"):
            text = span.get_text(strip=True)
            if not text:
                continue
            
            match = re.search(r'[\d]+(?:[.,]\d+)?[KkMm]?$', text)
            if match:
                num = parse_number(match.group())
                if num > 0:
                    total += num
        
        if total == 0:
            scripts = msg_div.find_all("script", type="application/json")
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    total += self._extract_reactions_from_json(data)
                except:
                    pass
        
        return total

    def _extract_reactions_from_json(self, data, depth=0) -> int:
        if depth > 5:
            return 0
        total = 0
        if isinstance(data, dict):
            for key in ['reactions', 'reaction_count', 'count', 'total_reactions']:
                if key in data:
                    try:
                        if isinstance(data[key], (int, float)):
                            total += int(data[key])
                        elif isinstance(data[key], str):
                            total += parse_number(data[key])
                        elif isinstance(data[key], list):
                            for item in data[key]:
                                if isinstance(item, dict) and 'count' in item:
                                    total += int(item.get('count', 0))
                    except:
                        pass
            for value in data.values():
                total += self._extract_reactions_from_json(value, depth + 1)
        elif isinstance(data, list):
            for item in data:
                total += self._extract_reactions_from_json(item, depth + 1)
        return total

    async def download_media(self, media_url: str, save_path: str) -> bool:
        try:
            if not media_url:
                logger.warning("❌ download_media: empty URL")
                return False
            
            logger.info(f"⬇️ Downloading {media_url[:100]}...")
            headers = {
                "User-Agent": Config.SCRAPER_USER_AGENT,
                "Referer": "https://t.me/",
            }
            async with self.session.get(media_url, headers=headers, timeout=60) as resp:
                if resp.status == 200:
                    with open(save_path, "wb") as f:
                        f.write(await resp.read())
                    logger.info(f"✅ Downloaded to {save_path}")
                    return True
                else:
                    logger.warning(f"❌ HTTP {resp.status} for {media_url[:100]}")
        except Exception as e:
            logger.error(f"❌ Download failed: {e}")
        return False