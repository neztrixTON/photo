import logging
import tempfile
import requests
import re
import io
import json
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
from html import unescape

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

# Domains or patterns to skip
SKIP_DOMAINS = [
    'avatars.mds.yandex.net',
    'yastatic.net',
    'info-people.com',
    'yandex.ru/support/images',
    'passport.yandex.ru',
]
# Marketplace domains (keys for endswith matching)
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data in ['show_all','show_market']:
        context.user_data['mode'] = 'all' if data=='show_all' else 'market'
    elif data in ['prev','next']:
        mode = context.user_data['mode']
        key = f'page_{mode}'
        context.user_data[key] += 1 if data=='next' else -1
    elif data=='save_excel':
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
        # find key that domain endswith
        name = next((label for key, label in MARKET_DOMAINS.items() if domain.endswith(key)), domain)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">Ссылка {i}</a> ({name})")
    return header + "\n".join(lines)



def search_by_image(image_path):
    import requests
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse
    import re

    # 1. Загрузка картинки
    with open(image_path, 'rb') as f:
        resp = requests.post(YANDEX_UPLOAD_URL, headers=HEADERS_UPLOAD, data=f)
    resp.raise_for_status()
    data = resp.json()
    cbir_id = data.get('cbir_id')
    orig = data.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig:
        return [], []

    # 2. Получаем HTML страницу
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

    # 3. Извлекаем ВСЕ ссылки из строки вида: &quot;https://example.com&quot;
    raw_links = re.findall(r'&quot;(https?://[^"&<>]+)&quot;', html)

    # 4. Фильтрация от мусора
    links = []
    for link in raw_links:
        domain = urlparse(link).netloc
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|gif|webp)(\?|$)', link.lower()):
            continue
        links.append(link)

    # 5. Убираем дубли
    seen = set()
    unique = []
    for l in links:
        if l not in seen:
            seen.add(l)
            unique.append(l)

    # 6. Отдельно собираем ссылки на маркетплейсы
    market = [l for l in unique if any(urlparse(l).netloc.endswith(key) for key in MARKET_DOMAINS)]

    return unique, market

def main():
    application = Application.builder().token('8074669890:AAGvN67WC5BeAOLsJYTNVzuiipbwwUi8KRU').build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    application.run_polling()

if __name__=='__main__':
    main()
