import os
import re
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, InputMediaPhoto
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
tlogging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration\
BOT_TOKEN = '8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms'  # Telegram bot token
YANDEX_COOKIES = ('yandexuid=8531756701727155871; yashr=2242844391727155871; yuidss=8531756701727155871; '
                 'ymex=2042515885.yrts.1727155885; gdpr=0; _ym_uid=1727155886351001819; '
                 'amcuid=6860060251727158513; font_loaded=YSv1; receive-cookie-deprecation=1; '
                 'my=YwA=; _ym_d=1737911848; _ymab_param=vUvJUBtmcqz2lDIYGWTAi2n64OEkb6rW-uUnUZCcAvitPKO_bqQ9DvxzAGk6qAgkZeyx-'
                 'hAlUyX9--0rkJr9kZWuEWs; yw_preset=base; skid=2774135661741329601; '
                 'L=XQgIclt1ZWkEcmFQc29Pak9eCH9FAH9nRAYgQCMdPywIKQdVEBAVIlAk.1745002492.16123.383150.a665eed8def1c7643a158335b3a9242e; '
                 'yandex_login=temur.hudaiberdiev; i=GWe6WqYzanWjDUtAiDJB8aLG+Nqxyf4EnZyHz/ogLbobk7FhUknmX0+QD';

# Headers used for Yandex requests
BASE_HEADERS = {
    'accept': '*/*',
    'content-type': 'image/jpeg',
    'origin': 'https://yandex.ru',
    'referer': 'https://yandex.ru/images/',
    'user-agent': 'TelegramBot (python-requests)'
}

CBIR_DOWNLOAD_URL = 'https://yandex.ru/images-apphost/image-download'
CLCK_URL_TEMPLATE = (
    'https://yandex.ru/clck/jclck/'
    'dtype=iweb/path=8.228.1031.4065.1277.2958.2052/'
    'vars=-page={page}/'
    'table=imgs/service=cbir.yandex/ui=images.yandex/*{search_url}'
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Отправь мне фото — и я найду сайты, где оно встречалось.'
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Download file from Telegram
    file = await update.message.photo[-1].get_file()
    img_bytes = await file.download_as_bytearray()

    # Upload to Yandex, get cbir_id
    cbir_id = get_cbir_id(img_bytes)
    if not cbir_id:
        await update.message.reply_text('Не удалось получить cbir_id от Yandex.')
        return

    # Build initial search URL
    search_url = (
        f'https://yandex.ru/images/search?cbir_id={cbir_id}'
        f'&rpt=imageview&url=&cbir_page=sites'
    )

    # Fetch and parse first page
    page_html = fetch_search_page(search_url)
    sites = parse_sites(page_html)

    # Send response
    if sites:
        reply = '\n'.join(f'{title}: {link}' for title, link in sites)
    else:
        reply = 'Сайты не найдены.'
    await update.message.reply_text(reply)

    # Optionally handle pagination for more results
    # for page in range(2, max_pages):
    #     ajax_html = fetch_ajax_page(search_url, page)
    #     more_sites = parse_sites(ajax_html)
    #     # Process more_sites...


def get_cbir_id(img_bytes: bytes) -> str:
    headers = BASE_HEADERS.copy()
    headers['cookie'] = YANDEX_COOKIES
    files = {'file': ('image.jpg', img_bytes, 'image/jpeg')}
    params = {
        'images_avatars_size': 'preview',
        'images_avatars_namespace': 'images-cbir'
    }
    resp = requests.post(CBIR_DOWNLOAD_URL, headers=headers, params=params, files=files)
    if resp.status_code == 200:
        # Expect JSON with cbir_id
        data = resp.json()
        return data.get('cbir_id')
    logger.error('CBIR upload failed: %s', resp.text)
    return ''


def fetch_search_page(search_url: str) -> str:
    headers = {'user-agent': BASE_HEADERS['user-agent']}
    resp = requests.get(search_url, headers=headers)
    resp.raise_for_status()
    return resp.text


def fetch_ajax_page(search_url: str, page: int) -> str:
    headers = {
        'user-agent': BASE_HEADERS['user-agent'],
        'referer': search_url
    }
    url = CLCK_URL_TEMPLATE.format(page=page, search_url=search_url)
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text


def parse_sites(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    items = soup.select('ul.CbirSites-Items li.CbirSites-Item')
    results = []
    for item in items:
        a = item.select_one('.CbirSites-ItemTitle a')
        domain = item.select_one('.CbirSites-ItemDomain').get_text(strip=True)
        if a:
            title = a.get_text(strip=True)
            link = a['href']
            results.append((title, link))
    return results


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.run_polling()


if __name__ == '__main__':
    main()
