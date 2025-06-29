import logging
import os
import asyncio
import signal
import re
import datetime
import pytz
import pickle
from collections import defaultdict
import psycopg2
from psycopg2 import pool
import io
import html
import time

import httpx
from bs4 import BeautifulSoup
import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence
from telegram.error import BadRequest

from google import genai
from google.genai import types

from duckduckgo_search import DDGS
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, RequestBlocked
from pdfminer.high_level import extract_text

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        system_instruction_text = f.read()
    logger.info("Системный промпт успешно загружен.")
except FileNotFoundError:
    logger.critical("Критическая ошибка: файл system_prompt.md не найден!")
    exit(1)

# --- КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---
class PostgresPersistence(BasePersistence):
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None
        self.dsn = database_url
        try:
            self._connect()
            self._initialize_db()
        except psycopg2.Error as e:
            logger.critical(f"PostgresPersistence: Не удалось подключиться к БД: {e}")
            raise

    def _connect(self):
        if self.db_pool:
            try: self.db_pool.closeall()
            except Exception as e: logger.warning(f"Ошибка при закрытии старого пула: {e}")
        dsn = self.dsn
        keepalive_options = "keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=5"
        if "?" in dsn:
             if "keepalives" not in dsn: dsn = f"{dsn}&{keepalive_options}"
        else:
             dsn = f"{dsn}?{keepalive_options}"
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=dsn)

    def _execute(self, query: str, params: tuple = None, fetch: str = None, retries=3):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        last_exception = None
        for attempt in range(retries):
            conn = None
            connection_handled = False
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Postgres: Ошибка соединения (попытка {attempt + 1}/{retries}): {e}")
                last_exception = e
                if conn:
                    self.db_pool.putconn(conn, close=True)
                    connection_handled = True
                if attempt < retries - 1:
                    self._connect(); time.sleep(1 + attempt)
                continue
            finally:
                if conn and not connection_handled:
                    self.db_pool.putconn(conn)
        
        logger.error(f"Postgres: Не удалось выполнить запрос после {retries} попыток. Последняя ошибка: {last_exception}")
        return None
    
    def _initialize_db(self): self._execute("CREATE TABLE IF NOT EXISTS persistence_data (key TEXT PRIMARY KEY, data BYTEA NOT NULL);")
    def _get_pickled(self, key: str) -> object | None:
        res = self._execute("SELECT data FROM persistence_data WHERE key = %s;", (key,), fetch="one")
        return pickle.loads(res[0]) if res and res[0] else None
    def _set_pickled(self, key: str, data: object) -> None: self._execute("INSERT INTO persistence_data (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = %s;", (key, pickle.dumps(data), pickle.dumps(data)))
    
    async def get_bot_data(self) -> dict: return await asyncio.to_thread(self._get_pickled, "bot_data") or {}
    async def update_bot_data(self, data: dict) -> None: await asyncio.to_thread(self._set_pickled, "bot_data", data)
    
    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict)
        if all_data:
            for k, d in all_data:
                try: chat_data[int(k.split('_')[-1])] = pickle.loads(d)
                except (ValueError, IndexError): logger.warning(f"Обнаружен некорректный ключ чата в БД: '{k}'. Запись пропущена.")
        return chat_data
    
    async def update_chat_data(self, chat_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    async def get_user_data(self) -> defaultdict[int, dict]: return defaultdict(dict)
    async def update_user_data(self, user_id: int, data: dict) -> None: pass
    
    async def drop_chat_data(self, chat_id: int) -> None:
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def drop_user_data(self, user_id: int) -> None: pass
    
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass
    async def refresh_bot_data(self, bot_data: dict) -> None: pass
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}
        chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: pass
    async def flush(self) -> None: pass
    def close(self):
        if self.db_pool: self.db_pool.closeall()

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ И КОНСТАНТЫ ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
DATABASE_URL = os.getenv('DATABASE_URL')
DEFAULT_MODEL = 'gemini-2.5-flash'
MAX_HISTORY_MESSAGES = 50
SAFETY_SETTINGS = [
    {"category": c, "threshold": types.HarmBlockThreshold.BLOCK_NONE} for c in 
    (types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, 
     types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
]

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def perform_web_search(query: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    session = context.bot_data.get('http_client')
    search_results = None
    if session and GOOGLE_API_KEY and GOOGLE_CSE_ID:
        try:
            params = {'key': GOOGLE_API_KEY, 'cx': GOOGLE_CSE_ID, 'q': query, 'num': 5, 'lr': 'lang_ru'}
            response = await session.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=10.0)
            if response.status_code == 200:
                items = response.json().get('items', [])
                search_results = "\n".join([item.get('snippet', item.get('title', '')) for item in items])
        except Exception as e: logger.error(f"Google Search Error: {e}")
    if not search_results:
        try:
            results = await asyncio.to_thread(DDGS().text, keywords=query, region='ru-ru', max_results=5)
            if results: search_results = "\n".join([r['body'] for r in results])
        except Exception as e: logger.error(f"DDG Search Error: {e}")
    return search_results

# --- ГЛАВНАЯ ФУНКЦИЯ ОБРАЩЕНИЯ К GEMINI ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list[types.Part]):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    history = context.chat_data.setdefault("history", [])
    
    # --- ИЗМЕНЕНО: Исправлена сериализация Part в словарь для истории ---
    parts_for_history = []
    for part in prompt_parts:
        if hasattr(part, 'text') and part.text:
            parts_for_history.append({'text': part.text})
        elif hasattr(part, 'data'):
            # Кодируем бинарные данные для сохранения в JSON-совместимом виде
            encoded_data = base64.b64encode(part.data).decode('utf-8')
            parts_for_history.append({'inline_data': {'mime_type': part.mime_type, 'data': encoded_data}})
            
    history.append({"role": "user", "parts": parts_for_history})
    
    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL, safety_settings=SAFETY_SETTINGS, system_instruction=system_instruction_text)
    
    try:
        # Для API нам нужны реальные бинарные данные, а не base64
        # Собираем историю заново для отправки в API
        api_history = []
        for entry in history[-MAX_HISTORY_MESSAGES:]:
            api_parts = []
            for p in entry['parts']:
                if 'text' in p:
                    api_parts.append(types.Part(text=p['text']))
                elif 'inline_data' in p:
                    decoded_data = base64.b64decode(p['inline_data']['data'])
                    api_parts.append(types.Part(data=decoded_data, mime_type=p['inline_data']['mime_type']))
            api_history.append({'role': entry['role'], 'parts': api_parts})

        response = await model.generate_content_async(api_history)
        reply_text = response.text
        
        history.append({"role": "model", "parts": [{"text": reply_text}]})
        context.chat_data['history'] = history[-MAX_HISTORY_MESSAGES:]
        
        await update.message.reply_html(reply_text, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка при генерации ответа: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я Женя, ваш ассистент. Просто напишите мне, отправьте фото или файл.")
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.chat_data.clear()
    if context.application.persistence:
        await context.application.persistence.drop_chat_data(chat_id)
    await update.message.reply_text("История чата очищена.")

# --- ОБРАБОТЧИКИ КОНТЕНТА ---
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    return match.group(0).rstrip('.,?!') if match else None
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None

async def handle_text_and_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, (update.message.text or "").strip()
    if not text: return
    
    youtube_id = extract_youtube_id(text)
    if youtube_id:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])
            transcript = " ".join([d['text'] for d in transcript_list])
            prompt_text = f"Сделай конспект по транскрипту видео: {text}\n\nТРАНСКРИПТ:\n{transcript[:20000]}"
        except (NoTranscriptFound, TranscriptsDisabled):
            prompt_text = f"Сделай краткое описание видео по ссылке (субтитры недоступны): {text}"
        except RequestBlocked:
            await message.reply_text("❌ YouTube заблокировал мои запросы с этого сервера."); return
        except Exception as e:
            await message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return
        await process_query(update, context, [types.Part(text=prompt_text)])
        return

    general_url = extract_general_url(text)
    if general_url:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        http_client = context.bot_data['http_client']
        try:
            response = await http_client.get(general_url, timeout=15.0, follow_redirects=True)
            soup = BeautifulSoup(response.text, 'lxml')
            for element in soup(["script", "style", "nav", "footer", "header", "aside"]): element.decompose()
            web_content = ' '.join(soup.stripped_strings)
            prompt_text = f"Проанализируй текст со страницы: {text}\n\nТЕКСТ:\n{web_content[:20000]}"
        except Exception as e:
            logger.error(f"Ошибка скрапинга {general_url}: {e}")
            prompt_text = f"Не удалось загрузить страницу. Ответь, используя поиск: {text}"
        await process_query(update, context, [types.Part(text=prompt_text)])
        return
        
    search_results = await perform_web_search(text, context)
    prompt_text = f"{text}\n\nРезультаты поиска:\n{search_results}" if search_results else text
    await process_query(update, context, [types.Part(text=prompt_text)])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    image_part = types.Part(data=photo_bytes, mime_type='image/jpeg')
    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL)
    
    response_extract = await model.generate_content_async(["Опиши изображение 1-3 словами для поиска.", image_part])
    search_query = response_extract.text.strip()
    
    search_context_str = ""
    if search_query:
        search_results = await perform_web_search(search_query, context)
        if search_results:
            search_context_str = f"\n\nРезультаты поиска по '{html.escape(search_query)}':\n{search_results}"
    
    final_prompt_text = f"Подробно опиши изображение и ответь на комментарий: '{message.caption or ''}'. {search_context_str}"
    await process_query(update, context, [types.Part(text=final_prompt_text), image_part])

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, doc = update.message, update.message.document
    if not doc: return
    
    mime_type = doc.mime_type or "application/octet-stream"
    if not (mime_type.startswith('text/') or mime_type == 'application/pdf'):
        await message.reply_text(f"⚠️ Пока могу читать только текстовые файлы и PDF."); return
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    doc_file = await doc.get_file()
    file_bytes = await doc_file.download_as_bytearray()

    text = ""
    if mime_type == 'application/pdf':
        try: text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        except Exception as e: await message.reply_text(f"❌ Не удалось извлечь текст из PDF: {e}"); return
    else:
        try: text = file_bytes.decode('utf-8')
        except UnicodeDecodeError: text = file_bytes.decode('cp1251', errors='ignore')
    
    prompt_text = f"Проанализируй текст из файла '{doc.file_name}'. Мой комментарий: '{message.caption or ''}'\n\nТЕКСТ:\n{text[:20000]}"
    await process_query(update, context, [types.Part(text=prompt_text)])

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    voice_file = await message.voice.get_file()
    file_bytes = await voice_file.download_as_bytearray()

    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL)
    response = await model.generate_content_async([types.Part(text="Расшифруй аудио и ответь на него."), types.Part(data=file_bytes, mime_type="audio/ogg")])
    
    message.text = response.text
    await handle_text_and_links(update, context)

# --- ФУНКЦИИ ЗАПУСКА И ОСТАНОВКИ ---
async def handle_telegram_webhook(request: aiohttp.web.Request):
    application = request.app['bot_app']
    try:
        update = Update.de_json(await request.json(), application.bot)
        await application.process_update(update)
        return aiohttp.web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(status=500)
async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    app['bot_app'] = application
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), handle_telegram_webhook)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", "10000")))
    await site.start()
    await stop_event.wait()
    await runner.cleanup()
async def main():
    persistence = PostgresPersistence(database_url=DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    
    application.bot_data['gemini_client'] = genai.Client(api_key=GOOGLE_API_KEY)
    application.bot_data['http_client'] = httpx.AsyncClient()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_links))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await application.initialize()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
    try:
        await application.bot.set_my_commands([BotCommand("start", "Инфо"), BotCommand("clear", "Очистить историю")])
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        await run_web_server(application, stop_event)
    finally:
        if application.bot_data.get('http_client'):
            await application.bot_data['http_client'].aclose()
        if persistence: persistence.close()

if __name__ == '__main__':
    asyncio.run(main())
