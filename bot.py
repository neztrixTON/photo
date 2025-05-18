import logging
import tempfile
import requests
import re
import io
import json
import pandas as pd
from urllib.parse import urlparse, unquote
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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 YaBrowser/25.4.0.0 Safari/537.36'
}

# Domains or patterns to skip
SKIP_DOMAINS = [
    'avatars.mds.yandex.net',
    'yastatic.net',
    'info-people.com',
    'yandex.ru/support/images',
    'passport.yandex.ru',
]

# Marketplace domains
MARKET_DOMAINS = {
    'ozon.ru': 'Ozon',
    'megamarket.ru': 'Megamarket',
    'wildberries.ru': 'Wb',
    'wb.ru': 'Wb',
    'market.yandex.ru': 'Yandex Market',
    'market.ya.ru': 'Yandex Market',
}

# Handlers
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
    links = context.user_data['all_links'] if mode == 'all' else context.user_data['market_links']
    total = len(links)
    start = page * RESULTS_PER_PAGE
    subset = links[start:start+RESULTS_PER_PAGE]
    text = format_links(subset, page, total) if mode == 'all' else format_market_links(subset, page, total)
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
    if data in ['show_all', 'show_market']:
        context.user_data['mode'] = 'all' if data == 'show_all' else 'market'
    elif data in ['prev', 'next']:
        mode = context.user_data['mode']
        key = f'page_{mode}'
        context.user_data[key] += 1 if data == 'next' else -1
    elif data == 'save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

# Utils
def build_keyboard(page, total):
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data='prev'))
    if (page + 1) * RESULTS_PER_PAGE < total:
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data='next'))
    if nav:
        buttons.append(nav)
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

# CORE SEARCH FUNCTION
def search_by_image(image_path):
    import json
    from html import unescape

    # 1) загрузка изображения
    with open(image_path, 'rb') as f:
        resp = requests.post(YANDEX_UPLOAD_URL, headers=HEADERS_UPLOAD, data=f)
    resp.raise_for_status()
    data = resp.json()
    cbir_id = data.get('cbir_id')
    orig = data.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig:
        return [], []

    # 2) получение HTML страницы «sites»
    params = {
        'cbir_id': cbir_id,
        'cbir_page': 'sites',
        'rpt': 'imageview',
        'url': orig,
    }
    resp = requests.get(YANDEX_SEARCH_URL, params=params, headers={'User-Agent': HEADERS_UPLOAD['User-Agent']})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    links = []

    # 3) Новый: парсим JSON из data-state атрибута <div class="Root" …>
    root = soup.select_one('div.Root[data-state]')
    if root:
        try:
            state = json.loads(unescape(root['data-state']))
            for item in state.get('sites', []):
                url = item.get('url') or item.get('link')
                if url and url not in links:
                    links.append(url)
        except json.JSONDecodeError:
            pass

    # 4) Фоллбэк через HTML (на всякий случай)
    for info in soup.select('.CbirSites-ItemInfo'):
        a_dom = info.select_one('.CbirSites-ItemDomain a')
        if a_dom and a_dom.has_attr('href'):
            url = a_dom['href']
        else:
            a_title = info.select_one('.CbirSites-ItemTitle a')
            url = a_title['href'] if a_title and a_title.has_attr('href') else None
        if url and url not in links:
            links.append(url)

    # 5) фильтрация по доменам, расширениям и дубли
    clean = []
    for link in links:
        domain = urlparse(link).netloc
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        if re.search(r"\.(css|js|jpg|jpeg|png|webp|gif)(?:$|\?)", link.lower()):
            continue
        clean.append(link)

    seen = set(); unique = []
    for link in clean:
        if link not in seen:
            seen.add(link); unique.append(link)

    # 6) маркетплейсы
    market = [l for l in unique if any(urlparse(l).netloc.endswith(key) for key in MARKET_DOMAINS)]
    return unique, market


def main():
    application = Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
