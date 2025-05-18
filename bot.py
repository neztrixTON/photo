
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
        image_path = tf.name
    try:
        all_links, market_links = search_by_image(image_path)
        logger.info(f"Найдено {len(all_links)} общих, {len(market_links)} маркетплейсов")
    except Exception as e:
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
        context.user_data[f'page_{mode}'] += 1 if data=='next' else -1
    elif data=='save_excel':
        await save_excel(update, context)
        return
    await display_links(update, context)

async def error_handler(update, context):
    logger.exception("Ошибка обработки:")

# помощники

def build_keyboard(page, total):
    buttons=[]; nav=[]
    if page>0: nav.append(InlineKeyboardButton('⬅️ Назад', callback_data='prev'))
    if (page+1)*RESULTS_PER_PAGE<total: nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data='next'))
    if nav: buttons.append(nav)
    buttons.append([
        InlineKeyboardButton('Общие результаты', callback_data='show_all'),
        InlineKeyboardButton('Маркетплейсы', callback_data='show_market')
    ])
    buttons.append([InlineKeyboardButton('💾 В Excel', callback_data='save_excel')])
    return InlineKeyboardMarkup(buttons)

def format_links(urls, page, total):
    header=f"🖼 Страница {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    return header+"\n".join(
        f"{i}. 🔗 <a href=\"{url}\">{url}</a>"
        for i,url in enumerate(urls, start=page*RESULTS_PER_PAGE+1)
    )

def format_market_links(urls, page, total):
    header=f"🛒 Маркетплейсы {page+1}/{(total-1)//RESULTS_PER_PAGE+1}\n"
    lines=[]
    for i,url in enumerate(urls, start=page*RESULTS_PER_PAGE+1):
        name=next((lbl for k,lbl in MARKET_DOMAINS.items() if urlparse(url).netloc.endswith(k)), urlparse(url).netloc)
        lines.append(f"{i}. 🔗 <a href=\"{url}\">{url}</a> ({name})")
    return header+"\n".join(lines)

# основная логика

def search_by_image(image_path):
    import requests, re
    from bs4 import BeautifulSoup
    from html import unescape
    from urllib.parse import urlparse

    # — 1) Заливаем картинку и получаем cbir_id + orig (как и было раньше) —

    # 2) Забираем HTML раздела sites
    resp = requests.get(
        'https://yandex.ru/images/search',
        params={
            'cbir_id': cbir_id,
            'cbir_page': 'sites',
            'rpt': 'imageview',
            'url': orig,
        },
        headers={'User-Agent':'Mozilla/5.0'}
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    links = []

    # — 3) Берём «сырую» строку data-state и сразу regex-ом выхватываем все HTTP(S)-ссылки —
    div = soup.select_one('div.Root[data-state]')
    if div:
        raw = unescape(div['data-state'])
        # на этом шаге raw — длинная строка со всеми &quot;…&quot;, нативным JSON не является
        found = re.findall(r'https?://[^"\'<>\s]+', raw)
        links.extend(found)

    # — 4) HTML-фоллбэк (старый код) —
    for info in soup.select('.CbirSites-ItemInfo'):
        a = info.select_one('.CbirSites-ItemDomain a')
        url = a['href'] if a and a.has_attr('href') else None
        if not url:
            t = info.select_one('.CbirSites-ItemTitle a')
            url = t['href'] if t and t.has_attr('href') else None
        if url:
            links.append(url)

    # — 5) Фильтрация и дедупликация —
    SKIP = ['avatars.mds.yandex.net','yastatic.net','info-people.com',
            'yandex.ru/support/images','passport.yandex.ru']
    clean = []
    for u in links:
        net = urlparse(u).netloc
        if any(skip in net for skip in SKIP):
            continue
        if re.search(r'\.(css|js|jpe?g|png|webp|gif)(?:$|\?)', u.lower()):
            continue
        clean.append(u)
    unique = list(dict.fromkeys(clean))

    # — 6) Отдельно маркетплейсы —
    MARKET = {'ozon.ru':'Ozon','megamarket.ru':'Megamarket',
              'wildberries.ru':'Wb','wb.ru':'Wb',
              'market.yandex.ru':'Yandex Market','market.ya.ru':'Yandex Market'}
    market = [u for u in unique if any(urlparse(u).netloc.endswith(k) for k in MARKET)]

    return unique, market

if __name__=='__main__':
    app=Application.builder().token('8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms').build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(MessageHandler(filters.PHOTO,handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    app.run_polling()

