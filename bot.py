#!/usr/bin/env python3
import logging
import tempfile
import requests
import re
import json
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackContext,
    filters
)

# Enable detailed logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram bot token
BOT_TOKEN = '8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms'

# Yandex CBIR endpoints
YANDEX_UPLOAD_URL = (
    'https://yandex.ru/images-apphost/image-download'
    '?cbird=111&images_avatars_size=preview&images_avatars_namespace=images-cbir'
)
YANDEX_SEARCH_URL = 'https://yandex.ru/images/search'

# Filters
SKIP_DOMAINS = [
    'avatars.mds.yandex.net', 'yastatic.net', 'info-people.com',
    'yandex.ru/support/images', 'passport.yandex.ru'
]
MARKET_DOMAINS = {
    'ozon.ru': 'Ozon', 'megamarket.ru': 'Megamarket',
    'wildberries.ru': 'Wildberries', 'wb.ru': 'Wildberries',
    'market.yandex.ru': 'Yandex Market', 'market.ya.ru': 'Yandex Market'
}

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        'Привет! Отправь мне картинку, и я найду сайты, где она встречалась.'
    )

async def handle_photo(update: Update, context: CallbackContext):
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    photo = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await photo.download_to_drive(tf.name)
        image_path = tf.name

    # Upload image and get cbir_id + orig
    cbir_id, orig = get_cbir_id_and_orig(image_path)
    if not cbir_id or not orig:
        await update.message.reply_text('Не удалось получить cbir_id от Yandex.')
        return

    # Search by image
    all_links, market_links = search_by_image(cbir_id, orig)
    if not all_links:
        await update.message.reply_text('По вашему изображению ссылки не найдены.')
        return

    # Prepare message
    text = 'Найденные сайты:\n'
    for url in all_links:
        text += f'- {url}\n'

    await update.message.reply_text(text)

    # Optionally send marketplace separately
    if market_links:
        mtext = 'Маркетплейсы:\n' + '\n'.join(f'- {u}' for u in market_links)
        await update.message.reply_text(mtext)


def get_cbir_id_and_orig(image_path: str):
    """
    Загружает изображение в Yandex CBIR, возвращает (cbir_id, orig)
    """
    with open(image_path, 'rb') as f:
        resp = requests.post(
            YANDEX_UPLOAD_URL,
            headers={
                'Accept': '*/*',
                'Accept-Language': 'ru,en;q=0.9',
                'Content-Type': 'image/jpeg',
                'User-Agent': 'Mozilla/5.0'
            },
            data=f
        )
    if resp.status_code != 200:
        logger.error('Upload error %s: %s', resp.status_code, resp.text)
        return None, None

    try:
        data = resp.json()
    except json.JSONDecodeError:
        logger.error('Invalid JSON in upload response')
        return None, None

    cbir_id = data.get('cbir_id')
    orig = data.get('sizes', {}).get('orig', {}).get('path')
    return cbir_id, orig


def search_by_image(cbir_id: str, orig: str):
    """
    Выполняет поиск по изображению и возвращает все ссылки и маркетплейсы
    """
    params = {
        'cbir_id': cbir_id,
        'rpt': 'imageview',
        'url': orig,
        'cbir_page': 'sites'
    }
    resp = requests.get(YANDEX_SEARCH_URL, params=params, headers={
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.9',
        'Accept-Language': 'ru,en;q=0.9',
        'User-Agent': 'Mozilla/5.0'
    })
    if resp.status_code != 200:
        logger.error('Search error %s', resp.status_code)
        return [], []

    soup = BeautifulSoup(resp.text, 'html.parser')
    links = []

    # HTML parsing fallback
    for info in soup.select('.CbirSites-ItemInfo'):
        a_dom = info.select_one('.CbirSites-ItemDomain')
        if a_dom and a_dom.has_attr('href'):
            links.append(a_dom['href'])
            continue
        a_title = info.select_one('.CbirSites-ItemTitle a')
        if a_title and a_title.has_attr('href'):
            links.append(a_title['href'])

    # Clean and dedupe
    clean = []
    for u in links:
        nl = urlparse(u).netloc
        if any(skip in nl for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|webp|gif)(?:$|\?)', u.lower()):
            continue
        clean.append(u)
    unique = list(dict.fromkeys(clean))

    # Market
    market = [u for u in unique if any(urlparse(u).netloc.endswith(dom) for dom in MARKET_DOMAINS)]
    return unique, market


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()


if __name__ == '__main__':
    main()
