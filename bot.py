import logging
import tempfile
import requests
import re
import io
import json
import uuid
import os
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

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
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

SKIP_URLS = [
    'https://yandex.ru/tune/search/',
    'https://yandex.ru/images-apphost',
    'https://yandex.ru/support/images/troubleshooting.html',
    'https://yandex.ru/support/images/',
]

MARKET_DOMAINS = {
    'ozon.ru': 'Ozon',
    'megamarket.ru': 'Megamarket',
    'wildberries.ru': 'Wb',
    'wb.ru': 'Wb',
    'market.yandex.ru': 'Yandex Market',
    'market.ya.ru': 'Yandex Market',
}

MEMORY_FILE = 'memory.json'


def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_memory(mem):
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)


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

    session_id = str(uuid.uuid4())
    chat_id = str(update.effective_chat.id)

    mem = load_memory()
    mem.setdefault(chat_id, {})
    mem[chat_id][session_id] = {
        'all': all_links,
        'market': market_links
    }
    save_memory(mem)

    context.user_data['mode'] = 'all'
    context.chat_data[session_id] = {'page_all': 0, 'page_market': 0}

    await display_links(update, context, session_id)


async def display_links(update, context, session_id):
    mem = load_memory()
    chat_id = str(update.effective_chat.id)
    session_data = mem.get(chat_id, {}).get(session_id)
    if not session_data:
        await update.effective_message.reply_text("Сессия устарела или не найдена.")
        return

    mode = context.user_data.get('mode', 'all')
    page = context.chat_data[session_id].get(f'page_{mode}', 0)
    links = session_data['all'] if mode == 'all' else session_data['market']
    total = len(links)
    start = page * RESULTS_PER_PAGE
    subset = links[start:start + RESULTS_PER_PAGE]
    text = format_links(subset, page, total) if mode == 'all' else format_market_links(subset, page, total)

    keyboard = build_keyboard(page, total, session_id)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )


async def save_excel(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    chat_id = str(update.effective_chat.id)
    mem = load_memory()
    session_data = mem.get(chat_id, {}).get(session_id, {})
    all_links = session_data.get('all', [])
    market_links = session_data.get('market', [])

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
    if ':' not in data:
        return

    session_id, action = data.split(':', 1)
    mem = load_memory()
    chat_id = str(update.effective_chat.id)
    if session_id not in mem.get(chat_id, {}):
        await update.callback_query.answer("Сессия не найдена или устарела")
        return

    if session_id not in context.chat_data:
        context.chat_data[session_id] = {'page_all': 0, 'page_market': 0}

    if action == 'all':
        context.user_data['mode'] = 'all'
    elif action == 'market':
        context.user_data['mode'] = 'market'
    elif action == 'next':
        mode = context.user_data.get('mode', 'all')
        key = f'page_{mode}'
        context.chat_data[session_id][key] += 1
    elif action == 'prev':
        mode = context.user_data.get('mode', 'all')
        key = f'page_{mode}'
        context.chat_data[session_id][key] = max(0, context.chat_data[session_id][key] - 1)
    elif action == 'save':
        await save_excel(update, context, session_id)
        return

    await display_links(update, context, session_id)


def build_keyboard(page, total, session_id):
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data=f'{session_id}:prev'))
    if (page + 1) * RESULTS_PER_PAGE < total:
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f'{session_id}:next'))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton('Общие результаты', callback_data=f'{session_id}:all'),
        InlineKeyboardButton('Маркетплейсы', callback_data=f'{session_id}:market')
    ])
    buttons.append([
        InlineKeyboardButton('💾 Excel', callback_data=f'{session_id}:save')
    ])
    return InlineKeyboardMarkup(buttons)


def format_links(urls, page, total):
    header = f"🖼 Страница {page + 1}/{(total - 1) // RESULTS_PER_PAGE + 1}\n"
    lines = [f"{i}. 🔗 <a href=\"{url}\">Ссылка {i}</a>" for i, url in enumerate(urls, start=page * RESULTS_PER_PAGE + 1)]
    return header + "\n".join(lines)


def format_market_links(urls, page, total):
    header = f"🛒 Маркетплейсы {page + 1}/{(total - 1) // RESULTS_PER_PAGE + 1}\n"
    lines = []
    for i, url in enumerate(urls, start=page * RESULTS_PER_PAGE + 1):
        domain = urlparse(url).netloc
        name = next((label for key, label in MARKET_DOMAINS.items() if domain.endswith(key)), domain)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">Ссылка {i}</a> ({name})")
    return header + "\n".join(lines)


def search_by_image(image_path):
    with open(image_path, 'rb') as f:
        resp = requests.post(YANDEX_UPLOAD_URL, headers=HEADERS_UPLOAD, data=f)
    resp.raise_for_status()
    data = resp.json()
    cbir_id = data.get('cbir_id')
    orig = data.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig:
        return [], []

    params = {
        'cbir_id': cbir_id,
        'cbir_page': 'sites',
        'crop': '0.016;0.5703;0.9664;0.984',
        'rpt': 'imageview',
        'url': orig,
    }
    resp = requests.get(YANDEX_SEARCH_URL, params=params, headers={'User-Agent': HEADERS_UPLOAD['User-Agent']})
    resp.raise_for_status()
    html = resp.text

    raw_links = re.findall(r'&quot;(https?://[^"&<>]+)&quot;', html)

    links = []
    for link in raw_links:
        if link.strip() in SKIP_URLS:
            continue
        domain = urlparse(link).netloc
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|gif|webp)(\?|$)', link.lower()):
            continue
        links.append(link)

    seen = set()
    unique = []
    for l in links:
        if l not in seen:
            seen.add(l)
            unique.append(l)

    market = [l for l in unique if any(urlparse(l).netloc.endswith(key) for key in MARKET_DOMAINS)]

    return unique, market


def main():
    application = Application.builder().token('Bot-token').build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    application.run_polling()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


if __name__ == '__main__':
    main()
