# main.txt
import logging
import os
import asyncio
import signal
from urllib.parse import urlencode
import base64
import pprint
import json
import time
import re
import datetime
import pytz
import pickle
from collections import defaultdict
import psycopg2
from psycopg2 import pool
import io
import html
from typing import AsyncGenerator

# Новые импорты
import httpx
from bs4 import BeautifulSoup

# --- Базовая настройка логирования ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Импорты Telegram и Gemini ---
import aiohttp
import aiohttp.web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, BotCommand
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    BasePersistence
)
from telegram.error import BadRequest, TelegramError, RetryAfter
import google.generativeai as genai
from google.generativeai.tool import Tool
from google.generativeai.types import content_types
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from youtube_transcript_api._errors import RequestBlocked
from pdfminer.high_level import extract_text

# --- Загрузка системного промпта ---
try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        system_instruction_text = f.read()
except FileNotFoundError:
    logger.critical("Критическая ошибка: system_prompt.md не найден!")
    exit(1)

# --- Переменные окружения и константы ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
DATABASE_URL = os.getenv('DATABASE_URL')
WEATHER_API_KEY = os.getenv('WEATHER_API_KEY') # Для Function Calling

# Проверка обязательных переменных
required_vars = {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH}
if not all(required_vars.values()):
    logger.critical(f"Отсутствуют переменные окружения: {[k for k, v in required_vars.items() if not v]}")
    exit(1)

# --- Настройки моделей (Июнь 2025) ---
# Принимаем твою реальность: модель gemini-2.5-flash
# Код написан с предположением, что API будет совместимым.
AVAILABLE_MODELS = {'gemini-2.5-flash': '2.5 Flash'}
DEFAULT_MODEL = 'gemini-2.5-flash'
TARGET_TIMEZONE = "Europe/Moscow"
USER_ID_PREFIX_FORMAT = "[User {user_id}; Name: {user_name}]: "

# --- Конфигурация Gemini API ---
genai.configure(api_key=GOOGLE_API_KEY)
SAFETY_SETTINGS_BLOCK_NONE = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- Класс для работы с БД (без изменений) ---
class PostgresPersistence(BasePersistence):
    # ... (весь код PostgresPersistence остается здесь без изменений)
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None
        self.dsn = database_url
        try:
            self._connect()
            self._initialize_db()
            logger.info("PostgresPersistence: Соединение с базой данных установлено и таблица проверена.")
        except psycopg2.Error as e:
            logger.critical(f"PostgresPersistence: Не удалось подключиться к базе данных PostgreSQL: {e}")
            raise

    def _connect(self):
        if self.db_pool:
            try:
                self.db_pool.closeall()
            except Exception as e:
                logger.warning(f"PostgresPersistence: Ошибка при закрытии старого пула: {e}")
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=self.dsn)
        logger.info("PostgresPersistence: Пул соединений успешно (пере)создан.")

    def _execute(self, query: str, params: tuple = None, fetch: str = None):
        if not self.db_pool:
            raise ConnectionError("PostgresPersistence: Пул соединений не инициализирован.")
        conn = None
        try:
            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
                return True
        except psycopg2.Error as e:
            logger.error(f"PostgresPersistence: Ошибка SQL: {e}")
            if conn:
                conn.rollback()
            return None
        finally:
            if conn:
                self.db_pool.putconn(conn)

    def _initialize_db(self):
        create_table_query = "CREATE TABLE IF NOT EXISTS persistence_data (key TEXT PRIMARY KEY, data BYTEA NOT NULL);"
        self._execute(create_table_query)

    def _get_pickled(self, key: str) -> object | None:
        result = self._execute("SELECT data FROM persistence_data WHERE key = %s;", (key,), fetch="one")
        return pickle.loads(result[0]) if result and result[0] else None

    def _set_pickled(self, key: str, data: object) -> None:
        pickled_data = pickle.dumps(data)
        query = "INSERT INTO persistence_data (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = %s;"
        self._execute(query, (key, pickled_data, pickled_data))

    async def get_bot_data(self) -> dict: return await asyncio.to_thread(self._get_pickled, "bot_data") or {}
    async def update_bot_data(self, data: dict) -> None: await asyncio.to_thread(self._set_pickled, "bot_data", data)
    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_chat_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict)
        if all_chat_data:
            for key, data in all_chat_data:
                try: chat_data[int(key.split('_')[-1])] = pickle.loads(data)
                except (ValueError, IndexError): logger.warning(f"Не удалось распарсить ключ чата: {key}")
        return chat_data
    async def update_chat_data(self, chat_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    async def get_user_data(self) -> defaultdict[int, dict]:
        all_user_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        if all_user_data:
            for key, data in all_user_data:
                try: user_data[int(key.split('_')[-1])] = pickle.loads(data)
                except (ValueError, IndexError): logger.warning(f"Не удалось распарсить ключ пользователя: {key}")
        return user_data
    async def update_user_data(self, user_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"user_data_{user_id}", data)
    async def drop_chat_data(self, chat_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def drop_user_data(self, user_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass
    async def refresh_bot_data(self, bot_data: dict) -> None: data = await self.get_bot_data(); bot_data.update(data)
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}; chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"user_data_{user_id}") or {}; user_data.update(data)
    async def flush(self) -> None: pass
    def close(self):
        if self.db_pool:
            self.db_pool.closeall()


# --- НОВЫЕ ИНСТРУМЕНТЫ БОТА (FUNCTION CALLING & GROUNDING) ---

async def get_current_weather(city: str) -> dict:
    """Получает текущую погоду для указанного города, используя OpenWeatherMap API."""
    logger.info(f"Вызов функции погоды для города: {city}")
    if not WEATHER_API_KEY:
        return {"error": "API ключ для погоды не настроен на стороне бота."}
    
    url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            return { "city": city, "temperature": f"{data['main']['temp']}°C", "description": data['weather'][0]['description'].capitalize() }
        except Exception as e:
            logger.error(f"Ошибка API погоды: {e}")
            return {"error": "Не удалось получить данные о погоде."}

# Собираем все "скиллы" в один список
google_search_tool = Tool.from_google_search_retrieval()
weather_tool = Tool.from_functions([get_current_weather])
AVAILABLE_TOOLS = [google_search_tool, weather_tool]


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list, **kwargs):
    """Добавляет запись в историю чата, поддерживая сложную структуру."""
    chat_id = context.chat_data.get('id', 'Unknown')
    history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": parts, **kwargs}
    history.append(entry)
    while len(history) > 50: # Ограничиваем историю
        history.pop(0)
    await context.application.persistence.update_chat_data(chat_id, context.chat_data)

def build_context_for_model(chat_history: list) -> list:
    """Собирает историю для отправки в модель, отсекая старое."""
    # Просто возвращаем последние 20 сообщений
    return chat_history[-20:]

def get_current_time_str() -> str:
    return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")

def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:v=|\/v\/|youtu\.be\/|embed\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None

def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?:\/\/[^\s<>"\'`]+', text)
    return match.group(0) if match else None

async def fetch_webpage_content(url: str, session: httpx.AsyncClient) -> str | None:
    try:
        response = await session.get(url, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for e in soup(['script', 'style', 'nav', 'footer', 'header']): e.decompose()
        return ' '.join(soup.stripped_strings)
    except Exception as e:
        logger.error(f"Ошибка при получении контента с {url}: {e}")
        return None

# --- ГЛАВНЫЙ ОРКЕСТРАТОР ВЗАИМОДЕЙСТВИЯ С GEMINI ---

async def stream_and_send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, response_stream: AsyncGenerator) -> tuple[Message | None, list]:
    """Обрабатывает стрим ответа, редактируя сообщение для "эффекта печатания"."""
    sent_message, full_text, buffer, last_sent_time = None, "", "", time.time()
    final_parts = []

    try:
        sent_message = await update.message.reply_text("✍️...")
        async for chunk in response_stream:
            if not chunk.candidates: continue
            
            # Собираем все части из чанка (текст, вызовы функций и т.д.)
            final_parts.extend(chunk.candidates[0].content.parts)
            
            if text_part := chunk.text:
                buffer += text_part
                full_text += text_part

            current_time = time.time()
            if (current_time - last_sent_time > 1.5 and buffer) or len(buffer) > 200:
                try:
                    await sent_message.edit_text(text=full_text + " พิมพ์", parse_mode=ParseMode.HTML)
                    buffer = ""
                    last_sent_time = current_time
                except (BadRequest, RetryAfter) as e:
                    if isinstance(e, RetryAfter): await asyncio.sleep(e.retry_after)
                    if "Message is not modified" not in str(e): logger.warning(f"Ошибка редактирования: {e}")

        if sent_message and full_text:
            await sent_message.edit_text(text=full_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в stream_and_send_reply: {e}", exc_info=True)
        if sent_message: await sent_message.edit_text(f"❌ Ошибка: {e}")
        else: await update.message.reply_text(f"❌ Ошибка: {e}")
            
    return sent_message, final_parts

async def orchestrate_gemini_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, initial_parts: list, use_cache: str | None = None):
    """
    Центральная функция для общения с Gemini.
    Поддерживает стриминг, Function Calling и кэширование.
    """
    chat_id, user, user_id = update.effective_chat.id, update.effective_user, update.effective_user.id
    history = build_context_for_model(context.chat_data.get("history", []))
    history.append({"role": "user", "parts": initial_parts})
    await _add_to_history(context, "user", initial_parts, message_id=update.message.message_id, user_id=user_id, cache_name=use_cache)

    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    
    # Выбор модели: из кэша или стандартная
    if use_cache:
        logger.info(f"Используем кэш '{use_cache}' для запроса.")
        cached_content = genai.caching.CachedContent(use_cache)
        model = genai.GenerativeModel.from_cached_content(cached_content)
    else:
        model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, system_instruction=system_instruction_text, tools=AVAILABLE_TOOLS)

    while True:
        logger.info(f"Отправка запроса в Gemini... История: {len(history)} сообщений.")
        response_stream = await asyncio.to_thread(model.generate_content, history, stream=True)
        
        # Получаем полный ответ и все его части (включая вызовы функций)
        sent_message, response_parts = await stream_and_send_reply(update, context, response_stream)
        
        # Ищем вызов функции в ответе
        function_calls = [part.function_call for part in response_parts if part.function_call]

        if not function_calls:
            await _add_to_history(context, "model", response_parts, bot_message_id=sent_message.message_id if sent_message else None)
            break # Если вызовов нет, диалог закончен

        # Если есть вызов функции
        logger.info(f"Модель запросила вызов функций: {[fc.name for fc in function_calls]}")
        history.append({"role": "model", "parts": response_parts})
        
        function_responses = []
        for fc in function_calls:
            if fc.name == "get_current_weather":
                result = await get_current_weather(**dict(fc.args))
                function_responses.append({"function_response": {"name": fc.name, "response": result}})
            else:
                logger.warning(f"Запрошена неизвестная функция: {fc.name}")

        if function_responses:
            history.append({"role": "function", "parts": function_responses})
        # Цикл продолжается, модель получит результат и даст финальный ответ


# --- ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    await update.message.reply_text("Привет! Я Женя, твой ассистент на базе Gemini 2.5 Flash. Умею искать в Google, узнавать погоду, работать с файлами и фото. Спрашивай!")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await context.application.persistence.drop_chat_data(update.effective_chat.id)
    await update.message.reply_text("🧹 История этого чата очищена.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or message.caption or "").strip()
    if not text: return
    
    # Проверяем, не ответ ли это на сообщение с кэшированным документом
    use_cache = None
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        history = context.chat_data.get("history", [])
        for entry in reversed(history):
            if entry.get("bot_message_id") == message.reply_to_message.message_id:
                if "cache_name" in entry:
                    use_cache = entry["cache_name"]
                break
    
    # Собираем части для отправки в модель
    parts = [{"text": f"Текущее время: {get_current_time_str()}. Мой запрос: {text}"}]
    
    # Проверяем URL
    url = extract_general_url(text)
    youtube_id = extract_youtube_id(text)
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    try:
        if youtube_id:
            transcript = " ".join([d['text'] for d in YouTubeTranscriptApi.get_transcript(youtube_id, languages=['ru', 'en'])])
            parts.append({"text": f"\n\n--- ТРАНСКРИПТ ВИДЕО ---\n{transcript[:15000]}"})
        elif url:
            content = await fetch_webpage_content(url, context.application.http_client)
            if content:
                parts.append({"text": f"\n\n--- КОНТЕНТ СТРАНИЦЫ ---\n{content[:15000]}"})
    except Exception as e:
        logger.warning(f"Не удалось получить контент по URL: {e}")

    await orchestrate_gemini_interaction(update, context, parts, use_cache)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_file = await message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    
    prompt = message.caption or "Опиши это изображение и найди в интернете информацию о том, что на нем."
    
    parts = [
        {"text": prompt},
        {"inline_data": content_types.Blob(mime_type="image/jpeg", data=bytes(file_bytes))}
    ]
    await orchestrate_gemini_interaction(update, context, parts)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    if doc.file_size > 10 * 1024 * 1024:
        await message.reply_text("❌ Файл слишком большой (> 10 MB).")
        return
        
    await message.reply_text("Анализирую файл... Это может занять время.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    
    doc_file = await doc.get_file()
    file_bytes = await doc_file.download_as_bytearray()
    
    text = ""
    try:
        if doc.mime_type == 'application/pdf':
            text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        else:
            text = file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        await message.reply_text(f"❌ Не удалось извлечь текст из файла: {e}")
        return

    if not text:
        await message.reply_text("❌ Файл пуст или не удалось извлечь текст.")
        return

    # Кэшируем контент
    try:
        cache_model_name = f'models/{get_user_setting(context, "selected_model", DEFAULT_MODEL)}'
        doc_cache = genai.caching.CachedContent.create(
            model=cache_model_name,
            display_name=f"doc_{doc.file_id}",
            contents=[{'parts': [{'text': text}]}],
            ttl=datetime.timedelta(hours=1)
        )
        logger.info(f"Документ '{doc.file_name}' успешно закэширован: {doc_cache.name}")
        
        initial_prompt = message.caption or f"Сделай краткий обзор этого документа: {doc.file_name}"
        await orchestrate_gemini_interaction(update, context, [{"text": initial_prompt}], use_cache=doc_cache.name)

    except Exception as e:
        logger.error(f"Ошибка кэширования документа: {e}", exc_info=True)
        # Если кэширование не удалось, работаем по старинке
        parts = [{"text": f"Запрос: {message.caption or 'Обзор файла'}\n\n--- ТЕКСТ ФАЙЛА ---\n{text[:20000]}"}]
        await orchestrate_gemini_interaction(update, context, parts)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_file = await update.message.voice.get_file()
    file_bytes = await voice_file.download_as_bytearray()
    
    model = genai.GenerativeModel(DEFAULT_MODEL)
    logger.info("Отправка аудио на транскрипцию...")
    response = await asyncio.to_thread(
        model.generate_content,
        ["Расшифруй это аудиосообщение.", content_types.Blob(mime_type="audio/ogg", data=bytes(file_bytes))]
    )
    
    transcribed_text = response.text
    if transcribed_text:
        logger.info(f"Аудио расшифровано: '{transcribed_text}'")
        # Отправляем расшифрованный текст на дальнейшую обработку
        parts = [{"text": f"Пользователь сказал голосом: {transcribed_text}"}]
        await orchestrate_gemini_interaction(update, context, parts)
    else:
        await update.message.reply_text("Не удалось распознать речь.")

# --- ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---

async def setup_bot_and_server(stop_event: asyncio.Event):
    # ... (Этот блок остается без изменений, он настраивает вебхук)
    persistence = None
    if DATABASE_URL:
        try:
            persistence = PostgresPersistence(database_url=DATABASE_URL)
        except Exception as e:
            logger.error(f"Не удалось инициализировать PostgresPersistence: {e}. Бот будет работать без сохранения состояния.", exc_info=True)
            persistence = None
    
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence:
        builder.persistence(persistence)
    
    application = builder.build()
    
    # Добавляем http_client в приложение
    application.http_client = httpx.AsyncClient()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await application.initialize()
    await application.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("clear", "Очистить историю чата")
    ])
    
    webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    logger.info(f"Вебхук установлен на {webhook_url}")
    
    web_server_coro = run_web_server(application, stop_event)
    return application, web_server_coro

async def run_web_server(application: Application, stop_event: asyncio.Event):
    # ... (Этот блок остается без изменений, он запускает aiohttp)
    app = aiohttp.web.Application()
    
    async def health_check(request):
        return aiohttp.web.Response(text="OK")

    async def telegram_webhook(request: aiohttp.web.Request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return aiohttp.web.Response(status=200)
        except Exception as e:
            logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
            return aiohttp.web.Response(status=500)

    app.router.add_get('/', health_check)
    app.router.add_post(f'/{GEMINI_WEBHOOK_PATH.strip("/")}', telegram_webhook)
    
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', port)
    
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")
    await stop_event.wait()
    await runner.cleanup()
    logger.info("Веб-сервер остановлен.")

async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    application, web_server_task = None, None
    try:
        logger.info("--- Запуск Gemini Telegram Bot ---")
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro)
        await stop_event.wait()
    except Exception as e:
        logger.critical(f"Критическая ошибка на верхнем уровне: {e}", exc_info=True)
    finally:
        logger.info("--- Начало процесса остановки ---")
        if web_server_task and not web_server_task.done():
            web_server_task.cancel()
        if application:
            if hasattr(application, 'http_client'):
                await application.http_client.aclose()
            await application.shutdown()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    asyncio.run(main())
