import logging
import tempfile
import requests
import re
import io
import json
import pandas as pd
from urllib.parse import urlparse, quote_plus
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

# Domains to skip in final results
SKIP_DOMAINS = [
    'avatars.mds.yandex.net',
    'yastatic.net',
    'info-people.com',
    'yandex.ru/support/images',
    'passport.yandex.ru',
]
# Marketplace domains mapping
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
        "Привет! Отправь мне картинку, и я найду сайты с похожим изображением."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await photo.download_to_drive(tf.name)
        image_path = tf.name

    try:
        all_links, market_links = search_by_image(image_path)
        logger.info(f"Найдено {len(all_links)} ссылок, из них {len(market_links)} маркетплейсов")
    except Exception:
        logger.exception("Ошибка при поиске по изображению")
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
    links = context.user_data['all_links'] if mode == 'all' else context.user_data['market_links']
    total = len(links)
    start_idx = page * RESULTS_PER_PAGE
    subset = links[start_idx:start_idx + RESULTS_PER_PAGE]

    if mode == 'all':
        header = f"🖼 Страница {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
        text = header + "\n".join(
            f"{i}. 🔗 <a href=\"{url}\">{url}</a>"
            for i, url in enumerate(subset, start=start_idx+1)
        )
    else:
        header = f"🛒 Маркетплейсы {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
        lines = []
        for i, url in enumerate(subset, start=start_idx+1):
            domain = urlparse(url).netloc
            name = next((lbl for dom, lbl in MARKET_DOMAINS.items() if domain.endswith(dom)), domain)
            lines.append(f"{i}. 🔗 <a href=\"{url}\">{url}</a> ({name})")
        text = header + "\n".join(lines)

    keyboard = build_keyboard(page, total)
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=keyboard, disable_web_page_preview=True, parse_mode=ParseMode.HTML
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            text, reply_markup=keyboard, disable_web_page_preview=True, parse_mode=ParseMode.HTML
        )

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
        caption='Файл Excel со списком ссылок'
    )
    await update.callback_query.answer()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data in ('show_all', 'show_market'):
        context.user_data['mode'] = 'all' if data == 'show_all' else 'market'
    elif data in ('prev', 'next'):
        mode = context.user_data['mode']
        key = f'page_{mode}'
        context.user_data[key] += (1 if data == 'next' else -1)
    elif data == 'save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

async def error_handler(update, context):
    logger.exception("Ошибка обработки апдейта:")

def build_keyboard(page, total):
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data='prev'))
    if (page+1)*RESULTS_PER_PAGE < total:
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data='next'))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton('Общие результаты', callback_data='show_all'),
        InlineKeyboardButton('Маркетплейсы', callback_data='show_market')
    ])
    buttons.append([InlineKeyboardButton('💾 В Excel', callback_data='save_excel')])
    return InlineKeyboardMarkup(buttons)

def search_by_image(image_path):
    """
    1) Загрузить картинку в Yandex Vision
    2) Получить cbir_id и orig URL
    3) Запросить страницы сайтов и спарсить все ссылки
    """
    # 1) upload
    logger.debug(f"Uploading image for CBIR: {image_path}")
    with open(image_path, 'rb') as f:
        upload = requests.post(
            YANDEX_UPLOAD_URL,
            headers={
                'Accept': '*/*',
                'Accept-Language': 'ru,en;q=0.9',
                'Content-Type': 'image/jpeg',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            },
            data=f
        )

    if upload.status_code != 200:
        logger.error(f"[upload] Ошибка загрузки: {upload.status_code} | {upload.text}")
        upload.raise_for_status()

    uj = upload.json()
    cbir_id = uj.get('cbir_id')
    orig = uj.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig:
        logger.error("Не получили cbir_id или orig из ответа Яндекса")
        return [], []

    # 2) запрос сайтов
    params = {
        'cbir_id': cbir_id,
        'rpt': 'imageview',
        'url': orig,
        'cbir_page': 'sites'
    }
    logger.debug(f"Fetching Yandex sites page with params: {params}")
    sr = requests.get(
        YANDEX_SEARCH_URL,
        params=params,
        headers={
            'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru,en;q=0.9',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
    )
    sr.raise_for_status()
    soup = BeautifulSoup(sr.text, 'html.parser')

    links = []
    # 3) попробуем вытянуть из data-state
    div = soup.select_one('div.Root[data-state]')
    if div:
        raw = json.loads(div['data-state'])
        for item in raw.get('sites', []):
            url = item.get('url') or item.get('link')
            if url:
                links.append(url)

    # 4) HTML fallback
    for info in soup.select('.CbirSites-ItemInfo'):
        a_dom = info.select_one('.CbirSites-ItemDomain a')
        if a_dom and a_dom.has_attr('href'):
            links.append(a_dom['href'])
        else:
            a_title = info.select_one('.CbirSites-ItemTitle a')
            if a_title and a_title.has_attr('href'):
                links.append(a_title['href'])

    # 5) фильтрация, дедуп
    clean = []
    for u in links:
        nl = urlparse(u).netloc
        if any(skip in nl for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|webp|gif)(?:$|\?)', u.lower()):
            continue
        clean.append(u)
    unique = list(dict.fromkeys(clean))

    # 6) выделяем маркетплейсы
    market = [
        u for u in unique
        if any(urlparse(u).netloc.endswith(dom) for dom in MARKET_DOMAINS)
    ]

    return unique, market


if __name__ == '__main__':
    app = Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    app.run_polling()
