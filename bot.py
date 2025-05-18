import logging
import tempfile
import requests
import re
import io
import json
import pandas as pd
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes,
    CommandHandler,
)

# Enable detailed logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Constants
YANDEX_UPLOAD_URL = (
    'https://yandex.ru/images-apphost/image-download'
    '?cbird=111&images_avatars_size=preview&images_avatars_namespace=images-cbir'
)
YANDEX_SEARCH_URL = 'https://yandex.ru/images/search'
RESULTS_PER_PAGE = 10
HEADERS_UPLOAD = {
    'Accept': '*/*',
    'Accept-Language': 'ru,en;q=0.9',
    'Content-Type': 'image/jpeg',
    'User-Agent': 'Mozilla/5.0'
}

SKIP_DOMAINS = [
    'avatars.mds.yandex.net',
    'yastatic.net',
    'info-people.com',
    'yandex.ru/support/images',
    'passport.yandex.ru',
]
MARKET_DOMAINS = {
    'ozon.ru': 'Ozon',
    'megamarket.ru': 'Megamarket',
    'wildberries.ru': 'Wb',
    'wb.ru': 'Wb',
    'market.yandex.ru': 'Yandex Market',
    'market.ya.ru': 'Yandex Market',
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне картинку, и я найду похожие изображения."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await photo.download_to_drive(tf.name)
    try:
        all_links, market_links = search_by_image(tf.name)
    except Exception as e:
        logger.error(f"Search error: {e}")
        all_links, market_links = [], []
    context.user_data.update({
        'all_links': all_links,
        'market_links': market_links,
        'page_all': 0,
        'page_market': 0,
        'mode': 'all'
    })
    await display_links(update, context)

async def display_links(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    mode = context.user_data['mode']
    page = context.user_data[f'page_{mode}']
    links = context.user_data['all_links'] if mode=='all' else context.user_data['market_links']
    total = len(links)
    start = page*RESULTS_PER_PAGE
    subset = links[start:start+RESULTS_PER_PAGE]
    text = format_links(subset, page, total) if mode=='all' else format_market_links(subset, page, total)
    keyboard = build_keyboard(page, total)
    if update.callback_query:
        msg = update.callback_query.message
        await msg.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text, reply_markup=keyboard, disable_web_page_preview=True, parse_mode=ParseMode.HTML)

async def save_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    all_links = context.user_data.get('all_links', [])
    market_links = context.user_data.get('market_links', [])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame({'Ссылка': all_links}).to_excel(writer, index=False, sheet_name='Общие результаты')
        pd.DataFrame({'Ссылка': market_links}).to_excel(writer, index=False, sheet_name='Маркетплейсы')
    buffer.seek(0)
    await update.callback_query.message.reply_document(
        document=InputFile(buffer, filename='results.xlsx'),
        caption='Файл Excel с результатами'
    )
    await update.callback_query.answer()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data in ['show_all','show_market']:
        context.user_data['mode'] = 'all' if data=='show_all' else 'market'
    elif data in ['prev','next']:
        mode = context.user_data['mode']
        key = f'page_{mode}'
        context.user_data[key] += 1 if data=='next' else -1
    elif data=='save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


def build_keyboard(page, total):
    buttons = []
    nav = []
    if page>0: nav.append(InlineKeyboardButton('⬅️ Назад', callback_data='prev'))
    if (page+1)*RESULTS_PER_PAGE<total: nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data='next'))
    if nav: buttons.append(nav)
    buttons.append([
        InlineKeyboardButton('Общие результаты', callback_data='show_all'),
        InlineKeyboardButton('Маркетплейсы', callback_data='show_market'),
    ])
    buttons.append([InlineKeyboardButton('💾 Сохранить в Excel', callback_data='save_excel')])
    return InlineKeyboardMarkup(buttons)


def format_links(urls, page, total):
    header = f"🖼 Страница {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    lines = [f"{i}. 🔗 <a href=\"{url}\">Ссылка {i}</a>" for i, url in enumerate(urls, start=page*RESULTS_PER_PAGE+1)]
    return header + "\n".join(lines)

def format_market_links(urls, page, total):
    header = f"🛒 Маркетплейсы {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    lines = []
    for i, url in enumerate(urls, start=page*RESULTS_PER_PAGE+1):
        domain = urlparse(url).netloc
        name = next((label for key, label in MARKET_DOMAINS.items() if domain.endswith(key)), domain)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">Ссылка {i}</a> ({name})")
    return header + "\n".join(lines)


def search_by_image(image_path):
    logger.debug("=== Поиск по изображению ===")
    logger.debug("1) Загрузка картинки на Yandex")
    with open(image_path,'rb') as f:
        resp = requests.post(YANDEX_UPLOAD_URL, headers=HEADERS_UPLOAD, data=f)
    resp.raise_for_status()
    data = resp.json()
    cbir_id = data.get('cbir_id')
    orig = data.get('sizes',{}).get('orig',{}).get('path')
    if not cbir_id or not orig:
        logger.debug("Не получили cbir_id или orig")
        return [], []

    logger.debug(f"2) Получение HTML для cbir_id={cbir_id}")
    params = {
        'cbir_id': cbir_id,
        'cbir_page': 'sites',
        'rpt': 'imageview',
        'url': orig,
    }
    resp = requests.get(YANDEX_SEARCH_URL, params=params, headers={'User-Agent': HEADERS_UPLOAD['User-Agent']})
    resp.raise_for_status()
    html = resp.text
    logger.debug(f"Получили HTML длиной {len(html)}")

    soup = BeautifulSoup(html, 'html.parser')
    links = []

    logger.debug("3) Парсим JSON из data-state")
    root_div = soup.select_one('div.Root[data-state]')
    if root_div:
        from html import unescape
        state_raw = unescape(root_div['data-state'])
        logger.debug(f"Raw data-state JSON length: {len(state_raw)}")
        try:
            state = json.loads(state_raw)
            sites = state.get('cbirSitesList', {}).get('sites', [])
            logger.debug(f"Найдено {len(sites)} элементов в cbirSitesList['sites']")
            for item in sites:
                url = item.get('url') or item.get('link')
                if url and url not in links:
                    links.append(url)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")

    logger.debug("4) HTML-фоллбэк по селекторам .CbirSites-ItemInfo")
    for info in soup.select('.CbirSites-ItemInfo'):
        a_dom = info.select_one('.CbirSites-ItemDomain a')
        url = a_dom['href'] if a_dom and a_dom.has_attr('href') else None
        if not url:
            a_title = info.select_one('.CbirSites-ItemTitle a')
            url = a_title['href'] if a_title and a_title.has_attr('href') else None
        if url and url not in links:
            links.append(url)
    logger.debug(f"После фоллбэка всего ссылок: {len(links)}")

    logger.debug("5) Фильтрация доменов и расширений")
    clean = []
    for link in links:
        domain = urlparse(link).netloc
        if any(skip in domain for skip in SKIP_DOMAINS): continue
        if re.search(r"\.(css|js|jpg|jpeg|png|webp|gif)(?:$|\?)", link.lower()): continue
        clean.append(link)
    unique = []
    seen = set()
    for link in clean:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    market = [l for l in unique if any(urlparse(l).netloc.endswith(key) for key in MARKET_DOMAINS)]

    logger.info(f"Найдено {len(unique)} общих ссылок, {len(market)} на маркетплейсы")
    return unique, market


def main():
    application = Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    application.run_polling()

if __name__=='__main__':
    main()
