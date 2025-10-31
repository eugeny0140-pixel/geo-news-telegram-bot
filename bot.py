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
        translated = MyMemoryTranslator(source='en', target=target
