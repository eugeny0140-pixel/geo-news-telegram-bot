import os
import re
import logging
import feedparser
import threading
import time
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import Bot
from telegram.constants import ParseMode
from deep_translator import GoogleTranslator, MyMemoryTranslator

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === Конфигурация ===
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
PORT = int(os.getenv('PORT', 10000))

if not TELEGRAM_BOT_TOKEN or not CHANNEL_ID:
    logger.critical("Отсутствуют TELEGRAM_BOT_TOKEN или CHANNEL_ID")
    exit(1)

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# === Источники с RSS ===
SOURCES = [
    ("Good Judgment", "https://goodjudgment.com/feed/"),
    ("Johns Hopkins", "https://www.centerforhealthsecurity.org/feed/"),
    ("Metaculus", "https://www.metaculus.com/feed/"),
    ("RAND Corporation", "https://www.rand.org/rss.xml"),
    ("World Economic Forum", "https://www.weforum.org/rss"),
    ("CSIS", "https://www.csis.org/rss.xml"),
    ("Atlantic Council", "https://www.atlanticcouncil.org/feed/"),
    ("Chatham House", "https://www.chathamhouse.org/feed"),
    ("The Economist", "https://www.economist.com/rss/rss.xml"),
    ("Bloomberg", "https://www.bloomberg.com/feed/podcast/"),
    ("Foreign Affairs", "https://www.foreignaffairs.com/rss.xml"),
    ("CFR", "https://www.cfr.org/rss.xml"),
    ("BBC Future", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("Carnegie Endowment", "https://carnegieendowment.org/feed/rss"),
    ("Bruegel", "https://www.bruegel.org/feed"),
    ("E3G", "https://www.e3g.org/feed/")
]

KEYWORDS = {
    'russia', 'ukraine', 'putin', 'kremlin', 'sanctions', 'gas', 'oil',
    'military', 'nato', 'eu', 'usa', 'europe', 'war', 'conflict',
    'russian', 'ukrainian', 'moscow', 'kiev', 'kyiv', 'belarus',
    'baltic', 'donbas', 'crimea', 'black sea', 'energy', 'grain',
    'weapons', 'defense', 'geopolitic', 'strategic', 'security'
}

seen_urls = set()
pending_articles = []
lock = threading.Lock()

# === Вспомогательные функции ===

def contains_keywords(text: str) -> bool:
    return any(kw in text.lower() for kw in KEYWORDS)

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def translate_with_fallback(text: str, target='ru') -> str:
    """Пытается перевести через Google, при ошибке — через MyMemory."""
    if not text or not text.strip():
        return ""
    try:
        translated = GoogleTranslator(source='auto', target=target).translate(text.strip())
        if translated and translated.strip():
            return translated.strip()
    except Exception as e:
        logger.warning(f"Google Translate failed: {e}")

    try:
        translated = MyMemoryTranslator(source='en', target=target).translate(text.strip())
        if translated and translated.strip():
            return translated.strip()
    except Exception as e:
        logger.warning(f"MyMemoryTranslator failed: {e}")

    return ""

def is_generic_description(desc: str) -> bool:
    generic = ['appeared first on', 'read more', 'click here', 'continue reading', '©', 'All rights reserved']
    return any(phrase in desc.lower() for phrase in generic)

def get_lead(desc: str) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', desc.strip())
    if sentences and sentences[0].strip():
        return sentences[0].strip()
    return desc[:300].strip()

def fetch_articles_for_window():
    global pending_articles
    new_articles = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=40)

    logger.info("🔍 Начало предварительной проверки источников (за 10 мин до отправки)")

    for name, feed_url in SOURCES:
        try:
            feed = feedparser.parse(feed_url)
            if not hasattr(feed, 'entries'):
                continue

            for entry in feed.entries:
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if not pub_date or pub_date < cutoff:
                    continue

                url = entry.get('link', '').strip()
                if not url:
                    continue
                url, _ = urldefrag(url)

                with lock:
                    if url in seen_urls:
                        continue

                title = entry.get('title', '').strip()
                desc = entry.get('summary', '').strip()

                if not desc or not title or is_generic_description(desc):
                    continue

                if not contains_keywords(title + ' ' + desc):
                    continue

                lead = get_lead(desc)
                prefix = re.sub(r'[^a-z0-9]', '', name.lower())

                # Проверяем перевод ДО добавления
                title_ru = translate_with_fallback(title)
                lead_ru = translate_with_fallback(lead)

                if not title_ru or not lead_ru:
                    logger.info(f"Пропущена статья от {name} — не удалось перевести: {url}")
                    continue

                new_articles.append((prefix, title, lead, url))

                break  # только одна статья на источник

        except Exception as e:
            logger.error(f"Ошибка при парсинге {name}: {e}")

    with lock:
        pending_articles = new_articles
        for _, _, _, url in new_articles:
            seen_urls.add(url)

    logger.info(f"✅ Найдено {len(new_articles)} статей для отправки")

def send_pending_articles():
    global pending_articles
    articles_to_send = []
    with lock:
        articles_to_send = pending_articles.copy()
        pending_articles.clear()

    logger.info(f"📤 Отправка {len(articles_to_send)} статей")
    for prefix, title, lead, url in articles_to_send:
        try:
            title_ru = translate_with_fallback(title)
            lead_ru = translate_with_fallback(lead)

            if not title_ru or not lead_ru:
                logger.warning(f"Пропущена при отправке (перевод не удался): {url}")
                continue

            message = (
                f"{prefix}: {title_ru}\n\n"
                f"{lead_ru}\n\n"
                f"[Источник]({url})"
            )
            message = escape_markdown_v2(message)

            bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=False
            )
            logger.info(f"Отправлено: {url}")
        except Exception as e:
            logger.error(f"Не удалось отправить {url}: {e}")

def schedule_next_tasks():
    while True:
        now = datetime.now(timezone.utc)
        current_minute = now.minute

        if current_minute < 20:
            next_check = now.replace(minute=20, second=0, microsecond=0)
        elif current_minute < 50:
            next_check = now.replace(minute=50, second=0, microsecond=0)
        else:
            next_check = (now + timedelta(hours=1)).replace(minute=20, second=0, microsecond=0)

        sleep_seconds = (next_check - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        threading.Thread(target=fetch_articles_for_window, daemon=True).start()

        send_time = next_check + timedelta(minutes=10)
        now = datetime.now(timezone.utc)
        sleep_to_send = (send_time - now).total_seconds()
        if sleep_to_send > 0:
            time.sleep(sleep_to_send)

        threading.Thread(target=send_pending_articles, daemon=True).start()

# === HTTP-сервер для Render ===
app = Flask(__name__)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def health_check(path):
    return "OK", 200

# === Запуск ===
if __name__ == '__main__':
    logger.info("🚀 Бот запущен. Инициализация планировщика...")
    threading.Thread(target=schedule_next_tasks, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
