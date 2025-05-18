import logging
import tempfile
import requests
import re
import io
import json
import pandas as pd
from html import unescape
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

# Подробное логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Константы
YANDEX_UPLOAD_URL = (
    'https://yandex.ru/images-apphost/image-download'
    '?cbird=111&images_avatars_size=preview&images_avatars_namespace=images-cbir'
)
YANDEX_SEARCH_URL = 'https://yandex.ru/images/search'
RESULTS_PER_PAGE = 10

# Фильтры доменов
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

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Пришли мне картинку — я найду сайты, где она встречается."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    photo = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
        await photo.download_to_drive(tf.name)
        image_path = tf.name

    try:
        all_links, market_links = search_by_image(image_path)
        logger.info(f"Найдено {len(all_links)} ссылок, из них {len(market_links)} маркетплейсов")
    except Exception:
        logger.exception("Ошибка в search_by_image:")
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
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    mode = context.user_data['mode']
    page = context.user_data[f'page_{mode}']
    links = context.user_data['all_links'] if mode == 'all' else context.user_data['market_links']
    total = len(links)
    start = page * RESULTS_PER_PAGE
    subset = links[start:start + RESULTS_PER_PAGE]

    if mode == 'all':
        text = format_links(subset, page, total)
    else:
        text = format_market_links(subset, page, total)
    keyboard = build_keyboard(page, total)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            text, reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )

async def save_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)
    all_links = context.user_data.get('all_links', [])
    market_links = context.user_data.get('market_links', [])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        pd.DataFrame({'Ссылка': all_links}).to_excel(writer, index=False, sheet_name='Все ссылки')
        pd.DataFrame({'Ссылка': market_links}).to_excel(writer, index=False, sheet_name='Маркетплейсы')
    buffer.seek(0)
    await update.callback_query.message.reply_document(
        document=InputFile(buffer, filename='results.xlsx'),
        caption='📊 Результаты в Excel'
    )
    await update.callback_query.answer()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data in ['show_all', 'show_market']:
        context.user_data['mode'] = 'all' if data == 'show_all' else 'market'
    elif data in ['prev', 'next']:
        mode = context.user_data['mode']
        context.user_data[f'page_{mode}'] += (1 if data == 'next' else -1)
    elif data == 'save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

async def error_handler(update, context):
    logger.exception("Ошибка обработки апдейта:")

# UI-помощники
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
        InlineKeyboardButton('Все ссылки', callback_data='show_all'),
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
        domain = urlparse(url).netloc
        name = next((lbl for k, lbl in MARKET_DOMAINS.items() if domain.endswith(k)), domain)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">{url}</a> ({name})")
    return header + "\n".join(lines)

# Новая логика поиска по изображению
def search_by_image(image_path):
    # 1) Загрузить изображение, получить cbir_id и orig URL
    up = requests.post(
        YANDEX_UPLOAD_URL,
        headers={
            'Accept':'*/*','Accept-Language':'ru,en;q=0.9',
            'Content-Type':'image/jpeg','User-Agent':'Mozilla/5.0'
        },
        data=open(image_path,'rb')
    )
    up.raise_for_status()
    uj = up.json()
    cbir_id = uj.get('cbir_id')
    orig = uj.get('sizes', {}).get('orig', {}).get('path')
    if not cbir_id or not orig:
        return [], []

    all_links = []
    market_links = []
    params = {
        'cbir_id': cbir_id,
        'cbir_page': 'sites',
        'rpt': 'imageview',
        'url': orig
    }
    headers = {'User-Agent': 'Mozilla/5.0'}

    cursor = None
    while True:
        # AJAX‐скролл: если есть cursor, добавляем в запрос
        if cursor:
            params['cursor'] = cursor
            params['forest'] = '1'

        resp = requests.get(YANDEX_SEARCH_URL, params=params, headers=headers)
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            data = resp.json()
            html = data.get('html', '')
            cursor = data.get('cursor')
        else:
            html = resp.text
            cursor = None  # первый HTML‐ответ

        soup = BeautifulSoup(html, 'html.parser')

        # 2) Из data-state «Root» вынимаем все URL
        div = soup.select_one('div.Root[data-state]')
        if div:
            state = unescape(div['data-state'])
            found = re.findall(r'https?://[^\s"\'<>]+', state)
            all_links.extend(found)

        # 3) Фоллбэк по элементам HTML
        for info in soup.select('.CbirSites-ItemInfo'):
            a_dom = info.select_one('.CbirSites-ItemDomain a')
            url = a_dom['href'] if a_dom and a_dom.has_attr('href') else None
            if not url:
                a_title = info.select_one('.CbirSites-ItemTitle a')
                url = a_title['href'] if a_title and a_title.has_attr('href') else None
            if url:
                all_links.append(url)

        # Если курсор отсутствует или нет нового набора — выходим
        if not cursor:
            break

    # 4) Фильтрация, дедуп
    clean = []
    for u in all_links:
        net = urlparse(u).netloc
        if any(skip in net for skip in SKIP_DOMAINS):
            continue
        if re.search(r'\.(css|js|jpe?g|png|webp|gif)(?:$|\?)', u.lower()):
            continue
        clean.append(u)
    unique = list(dict.fromkeys(clean))

    # 5) Маркетплейсы
    for u in unique:
        net = urlparse(u).netloc
        if any(net.endswith(k) for k in MARKET_DOMAINS):
            market_links.append(u)

    return unique, market_links

# Точка входа
if __name__ == '__main__':
    app = Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    app.run_polling()
