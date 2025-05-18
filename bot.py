import os
import logging
import telebot
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

# ==== НАСТРОЙКИ ====
BOT_TOKEN = '8037946874:AAFt8VjAfy-UpTXF-XoJUYPiNlC7B-btUms'  # Вставьте сюда ваш токен
bot = telebot.TeleBot(BOT_TOKEN)

# ==== ЛОГИ ====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==== ХЭДЕРЫ ====
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9',
}

# ==== ПОЛУЧИТЬ CBIR ID ====
def get_cbir_id(image_bytes):
    files = {
        'upfile': ('image.jpg', image_bytes, 'image/jpeg')
    }
    response = requests.post('https://yandex.ru/images/search', files=files, headers=HEADERS, allow_redirects=False)
    location = response.headers.get('Location')
    if not location or 'cbir_id=' not in location:
        return None
    return location.split('cbir_id=')[1].split('&')[0]

# ==== ПОЛУЧИТЬ ССЫЛКИ ====
def parse_sites_links(cbir_id, image_url):
    cbir_url = (
        f'https://yandex.ru/images/search?cbir_id={quote_plus(cbir_id)}'
        f'&rpt=imageview&url={quote_plus(image_url)}&cbir_page=sites'
    )
    logger.info(f'Searching URL: {cbir_url}')

    response = requests.get(cbir_url, headers=HEADERS)
    if response.status_code != 200:
        logger.error(f"Failed to get page: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    links = []
    for item in soup.select('.CbirSites-ItemInfo a.Link_view_outer'):
        href = item.get('href')
        if href and href.startswith('http'):
            links.append(href)

    return links

# ==== ОБРАБОТЧИК ФОТО ====
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}'
        image_bytes = requests.get(file_url).content

        cbir_id = get_cbir_id(image_bytes)
        if not cbir_id:
            bot.reply_to(message, "❌ Не удалось получить cbir_id от Яндекса.")
            return

        image_url = f"https://avatars.mds.yandex.net/get-images-cbir/{cbir_id}/orig"
        links = parse_sites_links(cbir_id, image_url)

        if not links:
            bot.reply_to(message, "⚠️ По вашему изображению ссылки не найдены.")
        else:
            bot.reply_to(message, "🔗 Найденные ссылки:\n\n" + "\n".join(links))

    except Exception as e:
        logger.exception("Ошибка при обработке фото")
        bot.reply_to(message, f"Произошла ошибка: {e}")

# ==== СТАРТ ====
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    bot.reply_to(message, "👋 Отправь мне изображение, и я найду сайты, где оно встречается.")

# ==== ЗАПУСК ====
if __name__ == '__main__':
    logger.info("Бот запущен...")
    bot.infinity_polling()
