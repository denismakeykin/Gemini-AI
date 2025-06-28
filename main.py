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
from typing import List, Dict, Any
import mimetypes
import json

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence, CallbackQueryHandler
from telegram.error import BadRequest

from google import genai
from google.genai import types

from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from pdfminer.high_level import extract_text

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        system_instruction_text = f.read()
    logger.info("Системный промпт успешно загружен.")
except FileNotFoundError:
    logger.critical("Критическая ошибка: файл system_prompt.md не найден!")
    exit(1)

# --- БАЗА ДАННЫХ ---
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
        logger.info(f"Пул соединений с БД (пере)создан. DSN: ...{dsn[-70:]}")


    def _execute(self, query: str, params: tuple = None, fetch: str = None, retries=1):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        try:
            conn = self.db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
            finally: self.db_pool.putconn(conn)
        except psycopg2.OperationalError as e:
            logger.warning(f"Ошибка соединения с БД: {e}. Попытка переподключения...")
            if retries > 0:
                self._connect()
                return self._execute(query, params, fetch, retries - 1)
            else:
                logger.error("Не удалось выполнить запрос после переподключения.")
                raise e

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
        for k, d in all_data or []:
            try: chat_data[int(k.split('_')[-1])] = pickle.loads(d)
            except (ValueError, IndexError): logger.warning(f"Обнаружен некорректный ключ чата в БД: '{k}'. Запись пропущена.")
        return chat_data
    async def update_chat_data(self, chat_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    
    async def get_user_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        for k, d in all_data or []:
            try: user_data[int(k.split('_')[-1])] = pickle.loads(d)
            except (ValueError, IndexError): logger.warning(f"Обнаружен некорректный ключ пользователя в БД: '{k}'. Запись пропущена.")
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
        if self.db_pool: self.db_pool.closeall()

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = map(os.getenv, ['TELEGRAM_BOT_TOKEN', 'GOOGLE_API_KEY', 'WEBHOOK_HOST', 'GEMINI_WEBHOOK_PATH', 'DATABASE_URL'])
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Отсутствуют обязательные переменные окружения!")
    exit(1)

DEFAULT_MODEL = 'gemini-2.5-flash-001'
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"
CACHE_TTL_SECONDS = 3600

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    s = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    s = re.sub(r'<li>', '• ', s, flags=re.IGNORECASE)
    s = re.sub(r'</?(?!b>|i>|u>|s>|code>|pre>|a>|tg-spoiler>)\w+\s*[^>]*>', '', s)
    return s.strip()

async def get_or_create_cache(context: ContextTypes.DEFAULT_TYPE, content_id: str, content_parts: List[Any]) -> str | None:
    client = context.bot_data['gemini_client']
    cache_store = context.chat_data.setdefault("content_cache", {})
    
    if content_id in cache_store:
        cache_name, expiry_time = cache_store[content_id]
        if time.time() < expiry_time:
            logger.info(f"Используется существующий кэш: {cache_name}")
            return cache_name
        else:
            logger.info(f"Кэш для {content_id} устарел. Удаляем: {cache_name}")
            # Безопасно пытаемся удалить старый кэш, но не падаем, если не вышло
            try: await client.aio.caches.delete(name=cache_name)
            except Exception as e: logger.warning(f"Не удалось удалить старый кэш {cache_name}: {e}")
            del cache_store[content_id]

    try:
        logger.info(f"Создается новый кэш для {content_id}")
        cache_config = {"contents": content_parts, "ttl": datetime.timedelta(seconds=CACHE_TTL_SECONDS)}
        cache = await client.aio.caches.create(model=f'models/{DEFAULT_MODEL}', **cache_config)
        cache_store[content_id] = (cache.name, time.time() + CACHE_TTL_SECONDS)
        logger.info(f"Кэш {cache.name} успешно создан.")
        return cache.name
    except Exception as e:
        logger.error(f"Не удалось создать кэш для {content_id}: {e}")
        return None

# --- ГЛАВНЫЙ ОБРАБОТЧИК ЗАПРОСА ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, content_parts: List[Any] = None, content_id: str = None):
    message = update.message
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    try:
        chat_history = context.chat_data.get("history", [])
        
        thinking_mode = context.user_data.get('thinking_mode', 'auto')
        thinking_config = {}
        if thinking_mode == 'max':
            thinking_config['budget'] = 24576
            logger.info("Используется максимальный бюджет мышления (24576).")
        else:
            logger.info("Используется автоматический бюджет мышления.")
        
        config = types.GenerateContentConfig(
            temperature=1.0, 
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=thinking_config,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=system_instruction_text
        )
        
        model_contents = list(chat_history)
        
        if content_id and content_parts:
            cache_name = await get_or_create_cache(context, content_id, content_parts)
            if cache_name:
                config.cached_content = cache_name
        
        model_contents.append({'role': 'user', 'parts': [{'text': user_text}]})
        
        client = context.bot_data['gemini_client']
        model = client.get_model(f"models/{DEFAULT_MODEL}")
        response = await model.generate_content_async(contents=model_contents, config=config)

        full_reply_text = sanitize_telegram_html(response.text)
        await message.reply_text(full_reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
        context.chat_data["history"] = [h.to_dict() for h in response.prompt.history]
        
    except Exception as e:
        logger.error(f"Критическая ошибка в process_query: {e}", exc_info=True)
        await message.reply_text(f"❌ Произошла серьезная ошибка: {str(e)[:500]}")

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я - Женя, лучший ИИ-ассистент на базе Google GEMINI 2.5 Flash:\n"
        "• 💬 Веду диалог, понимаю контекст, анализирую данные\n"
        "• 🎤 Понимаю голосовые сообщения, могу переводить в текст\n"
        "• 🖼 Анализирую изображения и видео (до 20 мб)\n"
        "• 📄 Читаю репосты, txt, pdf и веб-страницы\n"
        "• 🌐 Использую умный Google-поиск и огромный объем собственных знаний\n\n"
        "<b>Команды:</b>\n"
        "/transcribe - <i>(в ответе на голосовое)</i> Расшифровать аудио\n"
        "/summarize_yt <i>ссылка</i> - Конспект видео с YouTube\n"
        "/summarize_url <i>ссылка</i> - Выжимка из статьи\n"
        "/thinking - Настроить режим размышлений\n"
        "/clear - Очистить историю чата\n\n"
        "(!) Пользуясь ботом, вы автоматически соглашаетесь на отправку сообщений для получения ответов через Google Gemini API.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await update.message.reply_text("🧹 История этого чата очищена.")

async def thinking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_mode = context.user_data.get('thinking_mode', 'auto')
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'auto' else ''}Авто (Рекомендуется)", callback_data="set_thinking_auto")],
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'max' else ''}Максимум (Медленнее)", callback_data="set_thinking_max")],
    ]
    await update.message.reply_text("Выберите режим размышлений модели:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_thinking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split('_')[-1]
    context.user_data['thinking_mode'] = choice
    text = "✅ Режим размышлений установлен на **'Авто'**.\nЭто обеспечивает лучший баланс скорости и качества."
    if choice == 'max':
        text = "✅ Режим размышлений установлен на **'Максимум'**.\nОтветы могут быть качественнее, но и дольше."
    await query.edit_message_text(text.replace("**", "<b>"), parse_mode=ParseMode.HTML)

async def handle_text_or_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or update.message.caption or ""
    await process_query(update, context, user_text)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    caption = message.caption or "Подробно опиши этот медиафайл."
    
    if message.photo:
        file_id, mime_type = message.photo[-1].file_id, 'image/jpeg'
    elif message.video:
        file_id, mime_type = message.video.file_id, message.video.mime_type
    else: return

    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    prompt_text = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}"
    
    file_bytes = await (await context.bot.get_file(file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))
    
    await process_query(update, context, prompt_text, content_parts=[media_part], content_id=file_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > 15 * 1024 * 1024: await update.message.reply_text("❌ Файл слишком большой."); return
    
    file_bytes = await (await doc.get_file()).download_as_bytearray()
    doc_part = types.Part(inline_data=types.Blob(mime_type=doc.mime_type, data=file_bytes))
    
    caption = update.message.caption or f"Проанализируй содержимое файла '{doc.file_name}'."
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))
    prompt_text = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}"
    
    await process_query(update, context, prompt_text, content_parts=[doc_part], content_id=doc.file_id)

# --- НАСТРОЙКА И ЗАПУСК БОТА ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    await application.initialize()
    
    client = genai.Client()
    application.bot_data['gemini_client'] = client
    application.bot_data['http_client'] = httpx.AsyncClient()

    commands = [
        BotCommand("start", "Инфо и помощь"),
        BotCommand("clear", "Очистить историю"),
        BotCommand("thinking", "Настроить режим размышлений"),
        BotCommand("summarize_yt", "Конспект видео YouTube"),
        BotCommand("summarize_url", "Выжимка из статьи по ссылке")
    ]
    handlers = [
        CommandHandler("start", start),
        CommandHandler("clear", clear_history),
        CommandHandler("thinking", thinking_command),
        CommandHandler("summarize_url", summarize_url_command),
        CommandHandler("summarize_yt", summarize_yt_command),
        CallbackQueryHandler(select_thinking_callback, pattern="^set_thinking_"),
        MessageHandler(filters.PHOTO | filters.VIDEO, handle_media),
        MessageHandler(filters.Document, handle_document),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_or_voice),
        MessageHandler(filters.VOICE, handle_text_or_voice)
    ]
    application.add_handlers(handlers)
    await application.bot.set_my_commands(commands)
    webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN'))
    logger.info(f"Вебхук установлен: {webhook_url}")
    return application, asyncio.create_task(run_web_server(application, stop_event))

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def webhook_handler(request: aiohttp.web.Request):
        secret = os.getenv('WEBHOOK_SECRET_TOKEN')
        if secret and request.headers.get('X-Telegram-Bot-Api-Secret-Token') != secret: return aiohttp.web.Response(status=403)
        try:
            await application.process_update(Update.de_json(await request.json(), application.bot))
            return aiohttp.web.Response(status=200)
        except Exception as e:
            logger.error(f"Ошибка вебхука: {e}", exc_info=True)
            return aiohttp.web.Response(status=500)
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), webhook_handler)
    app.router.add_get('/', lambda r: aiohttp.web.Response(text="Bot is running"))
    
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000")))
    
    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на порту {os.getenv('PORT', '10000')}")
        await stop_event.wait()
    finally:
        await runner.cleanup()
        logger.info("Веб-сервер остановлен.")

async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
    application, web_task = None, None
    try:
        application, web_task = await setup_bot_and_server(stop_event)
        await stop_event.wait()
    finally:
        logger.info("--- Остановка приложения ---")
        if web_task and not web_task.done(): web_task.cancel()
        if application:
            if http_client := application.bot_data.pop('http_client', None):
                if not http_client.is_closed:
                    await http_client.aclose()
            application.bot_data.pop('gemini_client', None)
            
            await application.shutdown()
            if hasattr(application, 'persistence') and application.persistence:
                application.persistence.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Приложение остановлено пользователем.")
    except Exception as e:
        logger.critical(f"Необработанное исключение в main: {e}", exc_info=True)
