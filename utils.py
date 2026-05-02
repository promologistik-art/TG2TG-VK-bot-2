import re
from typing import Optional, Tuple
from datetime import datetime, timedelta
import pytz

def extract_channel_username(text: str) -> Optional[str]:
    patterns = [
        r'(?:https?://)?t(?:elegram)?\.me/([a-zA-Z0-9_]+)',
        r'@([a-zA-Z0-9_]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def calculate_score(post: dict, criteria: dict, post_time: datetime = None) -> Tuple[int, bool]:
    views = post.get("views", 0)
    reactions = post.get("reactions", 0)
    
    min_views = criteria.get("min_views", 0)
    min_reactions = criteria.get("min_reactions", 0)
    
    passes_criteria = True
    
    if min_views and views < min_views:
        passes_criteria = False
    if min_reactions and reactions < min_reactions:
        passes_criteria = False
    
    if not min_views and not min_reactions:
        passes_criteria = True
    
    if passes_criteria:
        score = 0
        if min_views:
            score += (views // 1000) * 10
        if min_reactions:
            score += reactions
        if post.get("has_media", False):
            score += 5
        if score == 0:
            score = 1
        return (score, False)
    else:
        return (-1, True)


def clean_caption(text: str) -> str:
    if not text:
        return ""
    
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'(https?://)?t\.me/\S+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\.([А-ЯA-Z])', r'. \1', text)
    text = re.sub(r'\!([А-ЯA-Z])', r'! \1', text)
    text = re.sub(r'\?([А-ЯA-Z])', r'? \1', text)
    text = text.strip()
    
    if len(text) > 1024:
        text = text[:1021] + "..."
    
    return text


def calculate_next_post_time(project) -> Optional[datetime]:
    moscow_tz = pytz.timezone("Europe/Moscow")
    now_moscow = datetime.now(moscow_tz)
    
    current_hour = now_moscow.hour
    
    if current_hour < project.active_hours_start:
        next_time = now_moscow.replace(hour=project.active_hours_start, minute=0, second=0, microsecond=0)
        return next_time
    
    if current_hour >= project.active_hours_end:
        next_time = now_moscow.replace(hour=project.active_hours_start, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return next_time
    
    next_time = now_moscow + timedelta(hours=project.post_interval_hours)
    
    if next_time.hour >= project.active_hours_end:
        next_time = now_moscow.replace(hour=project.active_hours_start, minute=0, second=0, microsecond=0) + timedelta(days=1)
    
    return next_time


def get_moscow_time() -> datetime:
    moscow_tz = pytz.timezone("Europe/Moscow")
    return datetime.now(moscow_tz)


def format_datetime(dt: datetime) -> str:
    if not dt:
        return "никогда"
    moscow_tz = pytz.timezone("Europe/Moscow")
    if dt.tzinfo is None:
        dt = moscow_tz.localize(dt)
    return dt.strftime("%d.%m.%Y %H:%M")


def format_number(num: int) -> str:
    if num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    return str(num)


def parse_number(text: str) -> int:
    if not text:
        return 0
    
    text = str(text).strip().upper().replace(" ", "")
    text = text.replace(",", ".")
    
    if "K" in text:
        return int(float(text.replace("K", "")) * 1000)
    elif "M" in text:
        return int(float(text.replace("M", "")) * 1000000)
    else:
        try:
            clean = re.sub(r'[^\d.]', '', text)
            if clean:
                return int(float(clean))
        except:
            pass
    
    return 0