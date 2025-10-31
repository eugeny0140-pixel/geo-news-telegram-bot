import os
import re
import logging
import feedparser
import threading
import time
from datetime import datetime, timedelta
from flask import Flask
from telegram import Bot
from telegram.constants import ParseMode
from deep_translator import GoogleTranslator
from urllib.parse import urldefrag

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
pending_articles = []  # [(prefix, title, lead, url), ...]
lock = threading.Lock()

# === Вспомогательные функции ===

def contains_keywords(text: str) -> bool:
    return any(kw in text.lower() for kw in KEYWORDS)

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def translate_safe(text: str, target='ru') -> str:
    try:
        if not text.strip():
            return text
        return GoogleTranslator(source='auto', target=target).translate(text)
    except Exception as e:
        logger.warning(f"Ошибка перевода: {e}")
        return text

def is_generic_description(desc: str) -> bool:
    generic = ['appeared first on', 'read more', 'click here', 'continue reading', '©']
    return any(phrase in desc.lower() for phrase in generic)

def get_lead(desc: str) -> str:
    # Берём первое предложение или первые 300 символов
    sentences = re.split(r'(?<=[.!?])\s+', desc.strip())
    if sentences and sentences[0].strip():
        return sentences[0].strip()
    return desc[:300].strip()

def fetch_articles_for_window():
    """Собирает по одной статье на источник за последние ~30+ минут"""
    global pending_articles
    new_articles = []
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=40)  # чуть больше, чтобы не пропустить

    logger.info("🔍 Начало предварительной проверки источников (за 10 мин до отправки)")

    for name, feed_url in SOURCES:
        try:
            feed = feedparser.parse(feed_url)
            if not hasattr(feed, 'entries'):
                continue

            for entry in feed.entries:
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6])
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

                if not desc or is_generic_description(desc):
                    continue

                if not contains_keywords(title + ' ' + desc):
                    continue

                lead = get_lead(desc)
                prefix = re.sub(r'[^a-z0-9]', '', name.lower())
                new_articles.append((prefix, title, lead, url))

                # Берём только первую подходящую статью от источника
                break

        except Exception as e:
            logger.error(f"Ошибка при парсинге {name}: {e}")

    # Обновляем очередь
    with lock:
        pending_articles = new_articles
        for _, _, _, url in new_articles:
            seen_urls.add(url)

    logger.info(f"✅ Найдено {len(new_articles)} статей для отправки")

def send_pending_articles():
    """Отправляет накопленные статьи ровно в :00 или :30"""
    global pending_articles
    articles_to_send = []
    with lock:
        articles_to_send = pending_articles.copy()
        pending_articles.clear()

    logger.info(f"📤 Отправка {len(articles_to_send)} статей")
    for prefix, title, lead, url in articles_to_send:
        try:
            title_ru = translate_safe(title)
            lead_ru = translate_safe(lead)

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
    """Запускает цикл: проверка за 10 мин → отправка в :00/:30"""
    while True:
        now = datetime.utcnow()
        current_minute = now.minute
        current_second = now.second

        # Определяем ближайшую отметку :20 или :50 (проверка за 10 мин до :30/:00)
        if current_minute < 20:
            next_check = now.replace(minute=20, second=0, microsecond=0)
        elif current_minute < 50:
            next_check = now.replace(minute=50, second=0, microsecond=0)
        else:
            next_check = (now + timedelta(hours=1)).replace(minute=20, second=0, microsecond=0)

        # Ждём до момента проверки
        sleep_seconds = (next_check - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        # Запускаем проверку в фоне (быстро, без блокировки)
        threading.Thread(target=fetch_articles_for_window, daemon=True).start()

        # Теперь ждём до отправки (:30 или :00)
        send_time = next_check + timedelta(minutes=10)  # :20 → :30, :50 → :00
        now = datetime.utcnow()
        sleep_to_send = (send_time - now).total_seconds()
        if sleep_to_send > 0:
            time.sleep(sleep_to_send)

        # Отправляем
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

    # Первый запуск: проверим, не пора ли уже что-то делать
    threading.Thread(target=schedule_next_tasks, daemon=True).start()

    # Запуск веб-сервера (обязательно для Render Web Service)
    app.run(host='0.0.0.0', port=PORT)
