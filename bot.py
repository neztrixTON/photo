import logging
import tempfile
import requests
import re
import io
import pandas as pd
from urllib.parse import urlparse, quote
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

RESULTS_PER_PAGE = 10
HEADERS_HTML = {
    'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ru,en;q=0.9',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 YaBrowser/25.4.0.0 Safari/537.36'
}

SKIP_DOMAINS = [
    'avatars.mds.yandex.net', 'yastatic.net',
    'info-people.com', 'yandex.ru/support/images', 'passport.yandex.ru'
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
    await update.message.reply_text("Привет! Отправь мне картинку, и я найду похожие изображения.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await photo.download_to_drive(tf.name)
        image_path = tf.name
    try:
        all_links, market_links = search_by_image(image_path)
        logger.info(f"Найдено {len(all_links)} ссылок, из них {len(market_links)} маркетплейсов")
    except Exception as e:
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
    start = page * RESULTS_PER_PAGE
    subset = links[start:start+RESULTS_PER_PAGE]
    if not total:
        text = '❌ Ничего не найдено.'
    else:
        text = format_links(subset, page, total) if mode == 'all' else format_market_links(subset, page, total)
    keyboard = build_keyboard(page, total)
    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=keyboard,
            disable_web_page_preview=True, parse_mode=ParseMode.HTML
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text, reply_markup=keyboard, disable_web_page_preview=True, parse_mode=ParseMode.HTML)

async def save_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    all_links = context.user_data.get('all_links', [])
    market_links = context.user_data.get('market_links', [])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame({'Ссылка': all_links}).to_excel(writer, index=False, sheet_name='Общие')
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
        context.user_data[f'page_{mode}'] += 1 if data == 'next' else -1
    elif data == 'save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

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
        InlineKeyboardButton('Общие', callback_data='show_all'),
        InlineKeyboardButton('Маркетплейсы', callback_data='show_market')
    ])
    buttons.append([InlineKeyboardButton('💾 В Excel', callback_data='save_excel')])
    return InlineKeyboardMarkup(buttons)

def format_links(urls, page, total):
    header = f"🖼 Страница {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    return header + "\n".join(
        f"{i}. 🔗 <a href=\"{url}\">{url}</a>"
        for i, url in enumerate(urls, start=page*RESULTS_PER_PAGE+1)
    )

def format_market_links(urls, page, total):
    header = f"🛒 Маркетплейсы {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    lines = []
    for i, url in enumerate(urls, start=page*RESULTS_PER_PAGE+1):
        name = next((v for k,v in MARKET_DOMAINS.items() if urlparse(url).netloc.endswith(k)), urlparse(url).netloc)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">{url}</a> ({name})")
    return header + "\n".join(lines)

def search_by_image(image_path):
    session = requests.Session()
    session.headers.update(HEADERS_HTML)

    # 1) Загрузка картинки
    upload = session.post(
        'https://yandex.ru/images-apphost/image-download?cbird=111&images_avatars_size=preview&images_avatars_namespace=images-cbir',
        files={'upfile': open(image_path, 'rb')},
        headers={
            'Content-Type': 'image/jpeg',
            'User-Agent': HEADERS_HTML['User-Agent'],
            'Accept': '*/*'
        }
    )
    upload.raise_for_status()
    j = upload.json()
    cbir_id = j.get('cbir_id')
    orig_path = j.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig_path:
        return [], []

    # 2) Поиск по cbir_id
    url = f"https://yandex.ru/images/search?cbir_id={quote(cbir_id)}&rpt=imageview&url={quote(orig_path)}&cbir_page=sites"
    resp = session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    # 3) Парсинг .CbirSites-ItemInfo
    links = []
    for info in soup.select('.CbirSites-ItemInfo'):
        a = info.select_one('.CbirSites-ItemDomain a')
        href = a['href'] if a and a.has_attr('href') else None
        if not href:
            t = info.select_one('.CbirSites-ItemTitle a')
            href = t['href'] if t and t.has_attr('href') else None
        if href:
            links.append(href)

    # 4) Очистка и фильтрация
    clean = []
    for u in links:
        netloc = urlparse(u).netloc
        if any(skip in netloc for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|webp|gif)(?:$|\?)', u.lower()):
            continue
        clean.append(u)
    unique = list(dict.fromkeys(clean))

    # 5) Маркетплейсы
    market = [u for u in unique if any(urlparse(u).netloc.endswith(k) for k in MARKET_DOMAINS)]

    return unique, market

async def error_handler(update, context):
    logger.exception("Ошибка:")

if __name__ == '__main__':
    app = Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    app.run_polling()
