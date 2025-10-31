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

# === ИСТОЧНИКИ ===
SOURCES = [
    ("E3G", "https://www.e3g.org/feed/"),
    ("Foreign Affairs", "https://www.foreignaffairs.com/rss.xml"),
    ("Reuters Institute", "https://reutersinstitute.politics.ox.ac.uk/rss.xml"),
    ("Bruegel", "https://www.bruegel.org/feed"),
    ("Chatham House", "https://www.chathamhouse.org/feed"),
    ("CSIS", "https://www.csis.org/rss.xml"),
    ("Atlantic Council", "https://www.atlanticcouncil.org/feed/"),
    ("RAND Corporation", "https://www.rand.org/rss.xml"),
    ("CFR", "https://www.cfr.org/rss.xml"),
    ("Carnegie Endowment", "https://carnegieendowment.org/feed/rss"),
    ("The Economist", "https://www.economist.com/rss/rss.xml"),
    ("Bloomberg Politics", "https://www.bloomberg.com/politics/feeds/site.xml"),
]

# === Ключевые слова ===
KEYWORDS_PATTERNS = [
    r"\brussia\b", r"\brussian\b", r"\bputin\b", r"\bukraine\b", r"\bzelensky\b",
    r"\bkremlin\b", r"\bmoscow\b", r"\bsanction[s]?\b", r"\bgazprom\b",
    r"\bnord\s?stream\b", r"\bwagner\b", r"\blavrov\b", r"\bnato\b", r"\bwar\b",
    r"\bukrainian\b", r"\bkyiv\b", r"\bkiev\b", r"\bcrimea\b", r"\bdonbas\b",
    r"\benergy\b", r"\boil\b", r"\bgas\b", r"\bgrain\b", r"\beu\b", r"\busa\b"
]

def contains_keywords(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(pattern, text_lower) for pattern in KEYWORDS_PATTERNS)

def get_prefix(name: str) -> str:
    name = name.lower()
    if "e3g" in name: return "e3g"
    if "foreign affairs" in name: return "foreignaffairs"
    if "reuters" in name: return "reuters"
    if "bruegel" in name: return "bruegel"
    if "chatham" in name: return "chathamhouse"
    if "csis" in name: return "csis"
    if "atlantic" in name: return "atlanticcouncil"
    if "rand" in name: return "rand"
    if "cfr" in name: return "cfr"
    if "carnegie" in name: return "carnegie"
    if "economist" in name: return "economist"
    if "bloomberg" in name: return "bloomberg"
    return re.sub(r'[^a-z0-9]', '', name.split()[0])

def translate_with_fallback(text: str, target='ru') -> str:
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

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def is_generic_description(desc: str) -> bool:
    generic = ['appeared first on', 'read more', 'click here', 'continue reading', '©', 'All rights reserved']
    return any(phrase in desc.lower() for phrase in generic)

def get_lead(desc: str) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', desc.strip())
    if sentences and sentences[0].strip():
        return sentences[0].strip()
    return desc[:300].strip()

seen_urls = set()
pending_articles = []
lock = threading.Lock()

# === Тестовая отправка при запуске ===
def send_startup_test_message():
    try:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        test_msg = (
            "🚀 Тестовое сообщение: бот запущен и готов к работе.\n\n"
            f"Время запуска: {now_utc}\n\n"
            "Новости будут приходить в :00 и :30 каждого часа."
        )
        test_msg = escape_markdown_v2(test_msg)
        bot.send_message(
            chat_id=CHANNEL_ID,
            text=test_msg,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        logger.info("✅ Тестовое сообщение отправлено при запуске.")
    except Exception as e:
        logger.error(f"❌ Не удалось отправить тестовое сообщение: {e}")

# === Основные функции (сбор, отправка, keep-alive) ===
def fetch_articles_for_window():
    global pending_articles
    new_articles = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=40)

    logger.info("🔍 Сбор статей для ближайшей отправки...")

    for name, feed_url in SOURCES:
        try:
            feed = feedparser.parse(feed_url)
            if not hasattr(feed, 'entries') or not feed.entries:
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

                if not title or not desc or is_generic_description(desc):
                    continue

                if not contains_keywords(title + ' ' + desc):
                    continue

                lead = get_lead(desc)
                prefix = get_prefix(name)

                title_ru = translate_with_fallback(title)
                lead_ru = translate_with_fallback(lead)

                if not title_ru or not lead_ru:
                    logger.info(f"Пропущена статья от {name} — перевод не удался: {url}")
                    continue

                new_articles.append((prefix, title, lead, url))
                break

        except Exception as e:
            logger.error(f"Ошибка при парсинге {name}: {e}")

    with lock:
        pending_articles = new_articles
        for _, _, _, url in new_articles:
            seen_urls.add(url)

    logger.info(f"✅ Найдено {len(new_articles)} статей")

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
            logger.error(f"Ошибка отправки {url}: {e}")

def keep_alive_activity():
    while True:
        try:
            logger.info("🔄 Keep-alive: проверка активности (14 мин)")
            fetch_articles_for_window()
        except Exception as e:
            logger.debug(f"Keep-alive error: {e}")
        time.sleep(14 * 60)

def schedule_send_loop():
    while True:
        now = datetime.now(timezone.utc)
        current_minute = now.minute

        if current_minute < 30:
            next_send = now.replace(minute=30, second=0, microsecond=0)
        else:
            next_send = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

        sleep_sec = (next_send - now).total_seconds()
        if sleep_sec > 0:
            time.sleep(sleep_sec)

        threading.Thread(target=send_pending_articles, daemon=True).start()

# === HTTP-сервер ===
app = Flask(__name__)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def health_check(path):
    return "OK", 200

# === Запуск ===
if __name__ == '__main__':
    logger.info("🚀 Бот запущен. Отправка тестового сообщения...")
    # Отправляем тестовое сообщение в фоне (не блокируя сервер)
    threading.Thread(target=send_startup_test_message, daemon=True).start()

    threading.Thread(target=keep_alive_activity, daemon=True).start()
    threading.Thread(target=schedule_send_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
