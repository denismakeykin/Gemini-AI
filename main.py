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

# --- ВСЕ НЕОБХОДИМЫЕ ИМПОРТЫ ИЗ ВАШЕГО КОДА ---
import httpx
from bs4 import BeautifulSoup
import aiohttp
import aiohttp.web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, BotCommand, File
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    BasePersistence
)
from telegram.error import BadRequest

# --- КОРРЕКТНЫЙ ИМПОРТ SDK И ЕГО КОМПОНЕНТОВ ---
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold, Part
from google.generativeai.errors import BlockedPromptError, StopCandidateError, ServerError

from duckduckgo_search import DDGS
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, RequestBlocked
from pdfminer.high_level import extract_text

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗАГРУЗКА СИСТЕМНОГО ПРОМПТА ---
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
        logger.info(f"Пул соединений с БД (пере)создан.")

    def _execute(self, query: str, params: tuple = None, fetch: str = None, retries=3):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        last_exception = None
        for attempt in range(retries):
            conn = None
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Postgres: Ошибка соединения (попытка {attempt + 1}/{retries}): {e}. Попытка переподключения...")
                last_exception = e
                if conn: self.db_pool.putconn(conn, close=True)
                if attempt < retries - 1: self._connect(); time.sleep(1 + attempt)
            finally:
                if conn: self.db_pool.putconn(conn)
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
    async def get_user_data(self) -> defaultdict[int, dict]: return defaultdict(dict) # Упрощено, т.к. не используется
    async def update_user_data(self, user_id: int, data: dict) -> None: pass # Упрощено
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
MAX_OUTPUT_TOKENS = 8192
USER_ID_PREFIX_FORMAT = "[User {user_id}; Name: {user_name}]: "
TARGET_TIMEZONE = "Europe/Moscow"
SAFETY_SETTINGS = [
    {"category": c, "threshold": HarmBlockThreshold.BLOCK_NONE} for c in 
    (HarmCategory.HARM_CATEGORY_HARASSMENT, HarmCategory.HARM_CATEGORY_HATE_SPEECH, 
     HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
]

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    return match.group(0).rstrip('.,?!') if match else None
def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks, tag_stack, remaining_text = [], [], text_to_chunk
    tag_regex = re.compile(r'<(/?)(b|i|u|s|code|pre|a|tg-spoiler)>', re.IGNORECASE)
    while len(remaining_text) > chunk_size:
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = chunk_size
        current_chunk = remaining_text[:split_pos]
        temp_stack = list(tag_stack)
        for match in tag_regex.finditer(current_chunk):
            tag_name, is_closing = match.group(2).lower(), bool(match.group(1))
            if not is_closing: temp_stack.append(tag_name)
            elif temp_stack and temp_stack[-1] == tag_name: temp_stack.pop()
        closing_tags = ''.join(f'</{tag}>' for tag in reversed(temp_stack))
        chunks.append(current_chunk + closing_tags)
        tag_stack = temp_stack
        opening_tags = ''.join(f'<{tag}>' for tag in tag_stack)
        remaining_text = opening_tags + remaining_text[split_pos:].lstrip()
    chunks.append(remaining_text)
    return chunks

# --- ФУНКЦИЯ ЗАГРУЗКИ ФАЙЛОВ ---
async def upload_file_to_google(client: genai.GenerativeModel, file_bytes: bytes, mime_type: str | None = None) -> Part | None:
    logger.info(f"Загрузка файла ({len(file_bytes) / 1024:.2f} KB) в Google...")
    try:
        # --- ИЗМЕНЕНО: Используем file_bytes напрямую. display_name убран. ---
        uploaded_file = await client.upload_file_async(file=file_bytes, mime_type=mime_type)
        logger.info(f"Файл успешно загружен. URI: {uploaded_file.uri}")
        return Part.from_uri(uri=uploaded_file.uri, mime_type=uploaded_file.mime_type)
    except Exception as e:
        logger.error(f"Не удалось загрузить файл в Google: {e}", exc_info=True)
        return None

# --- ГЛАВНАЯ ФУНКЦИЯ ОБРАЩЕНИЯ К GEMINI ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list):
    chat_id, user = update.effective_chat.id, update.effective_user
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Добавляем текстовую часть в историю
    text_part = next((p.text for p in prompt_parts if hasattr(p, 'text')), "")
    history = context.chat_data.setdefault("history", [])
    history.append({"role": "user", "parts": [part.to_dict() for part in prompt_parts]})
    if len(history) > MAX_HISTORY_MESSAGES: history.pop(0)
    
    # Формируем контент для API
    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL, safety_settings=SAFETY_SETTINGS, system_instruction=system_instruction_text)
    
    try:
        response = await model.generate_content_async(history)
        reply_text = response.text
        
        # Обновляем историю ответом модели
        history.append({"role": "model", "parts": [{"text": reply_text}]})
        
        # Отправляем ответ
        chunks = html_safe_chunker(reply_text, 4096)
        sent_message = None
        for i, chunk in enumerate(chunks):
            if i == 0: sent_message = await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
            else: sent_message = await context.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка при генерации ответа: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {e}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я Женя, ваш ассистент. Просто напишите мне, отправьте фото или файл.")
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await update.message.reply_text("История чата очищена.")

# --- ОБРАБОТЧИКИ КОНТЕНТА ---
async def handle_text_and_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, (update.message.text or "").strip()
    if not text: return
    
    # --- Логика обработки ссылок (из вашего кода) ---
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
            await message.reply_text("❌ YouTube заблокировал мои запросы с этого сервера. Не могу получить субтитры."); return
        except Exception as e:
            await message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return
        await process_query(update, context, [Part.from_text(prompt_text)])
        return

    general_url = extract_general_url(text)
    if general_url:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        web_content = await fetch_webpage_content(general_url, context.bot_data['http_client'])
        prompt_text = f"Проанализируй текст со страницы: {text}\n\nТЕКСТ:\n{web_content[:20000]}" if web_content else f"Не удалось загрузить страницу. Ответь, используя поиск: {text}"
        await process_query(update, context, [Part.from_text(prompt_text)])
        return
        
    await process_query(update, context, [Part.from_text(text)])

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    image_part = Part.from_data(data=photo_bytes, mime_type='image/jpeg')
    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL)
    
    # --- Этап 1: Извлечение ---
    extraction_prompt = "Проанализируй изображение. Если есть текст, извлеки его. Если нет, опиши ключевые объекты 1-3 словами."
    response_extract = await model.generate_content_async([extraction_prompt, image_part])
    search_query = response_extract.text.strip()
    
    # --- Этап 2: Поиск ---
    search_context_str = ""
    if search_query:
        search_results, _ = await perform_web_search(search_query, context)
        if search_results:
            search_context_str = f"\n\n==== РЕЗУЛЬТАТЫ ПОИСКА по '{html.escape(search_query)}' ====\n{search_results}"
            await message.reply_text(f"🔍 Нашел на картинке «_{html.escape(search_query[:60])}_»...", parse_mode=ParseMode.HTML, disable_notification=True)
    
    # --- Этап 3: Финальный анализ ---
    final_prompt_text = f"Подробно опиши изображение и ответь на комментарий: '{message.caption or ''}'. {search_context_str}"
    await process_query(update, context, [Part.from_text(final_prompt_text), image_part])

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, doc = update.message, update.message.document
    if not doc: return
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()

    # --- ИЗМЕНЕНО: Загрузка через upload_file ---
    client = context.bot_data['gemini_client']
    file_part = await upload_file_to_google(client, file_bytes, doc.mime_type)
    
    if not file_part:
        await message.reply_text("❌ Не удалось загрузить файл для обработки."); return
        
    prompt_text = f"Проанализируй этот файл. Мой комментарий: '{message.caption or ''}'"
    await process_query(update, context, [Part.from_text(prompt_text), file_part])

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
    runner, site = aiohttp.web.AppRunner(app), aiohttp.web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", "10000")))
    await site.start()
    await stop_event.wait()
    await runner.cleanup()
async def main():
    client = genai.Client(api_key=GOOGLE_API_KEY)
    persistence = PostgresPersistence(database_url=DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    application.bot_data['gemini_client'] = client
    application.bot_data['http_client'] = httpx.AsyncClient()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_links))
    application.add_handler(MessageHandler(filters.VOICE, handle_file)) # Голос как файл

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
    try:
        await application.bot.set_my_commands([BotCommand("start", "Инфо"), BotCommand("clear", "Очистить историю")])
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        await run_web_server(application, stop_event)
    finally:
        await application.bot_data['http_client'].aclose()
        if persistence: persistence.close()

if __name__ == '__main__':
    asyncio.run(main())
