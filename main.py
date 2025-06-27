import logging
import os
import asyncio
import signal
import base64
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
from typing import Coroutine

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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
from telegram.error import BadRequest

# --- ИМПОРТЫ ДЛЯ НОВОГО GOOGLE GEN AI SDK ---
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

# --- БАЗА ДАННЫХ (полная версия) ---
class PostgresPersistence(BasePersistence):
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None; self.dsn = database_url
        try: self._connect(); self._initialize_db()
        except psycopg2.Error as e: logger.critical(f"PostgresPersistence: Не удалось подключиться к БД: {e}"); raise
    def _connect(self):
        if self.db_pool:
            try: self.db_pool.closeall()
            except Exception as e: logger.warning(f"Ошибка при закрытии старого пула: {e}")
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=self.dsn)
    def _execute(self, query: str, params: tuple = None, fetch: str = None):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        conn = self.db_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetch == "one": return cur.fetchone()
                if fetch == "all": return cur.fetchall()
                conn.commit()
        finally: self.db_pool.putconn(conn)
    def _initialize_db(self): self._execute("CREATE TABLE IF NOT EXISTS persistence_data (key TEXT PRIMARY KEY, data BYTEA NOT NULL);")
    def _get_pickled(self, key: str) -> object | None:
        res = self._execute("SELECT data FROM persistence_data WHERE key = %s;", (key,), fetch="one")
        return pickle.loads(res[0]) if res and res[0] else None
    def _set_pickled(self, key: str, data: object) -> None: self._execute("INSERT INTO persistence_data (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = %s;", (key, pickle.dumps(data), pickle.dumps(data)))
    async def get_bot_data(self) -> dict: return await asyncio.to_thread(self._get_pickled, "bot_data") or {}
    async def update_bot_data(self, data: dict) -> None: await asyncio.to_thread(self._set_pickled, "bot_data", data)
    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict); [chat_data.update({int(k.split('_')[-1]): pickle.loads(d)}) for k, d in all_data or []]
        return chat_data
    async def update_chat_data(self, chat_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    async def get_user_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict); [user_data.update({int(k.split('_')[-1]): pickle.loads(d)}) for k, d in all_data or []]
        return user_data
    async def update_user_data(self, user_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"user_data_{user_id}", data)
    async def drop_user_data(self, user_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))
    async def drop_chat_data(self, chat_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
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

# --- КОНФИГУРАЦИЯ БОТА ---
TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = map(os.getenv, ['TELEGRAM_BOT_TOKEN', 'GOOGLE_API_KEY', 'WEBHOOK_HOST', 'GEMINI_WEBHOOK_PATH', 'DATABASE_URL'])
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Отсутствуют обязательные переменные окружения!")
    exit(1)

# <<< НАЧАЛО: ВАШИ МОДЕЛИ ВОЗВРАЩЕНЫ
AVAILABLE_MODELS = {'gemini-2.5-flash': '2.5 Flash'}
DEFAULT_MODEL = 'gemini-2.5-flash'
# <<< КОНЕЦ: ВАШИ МОДЕЛИ ВОЗВРАЩЕНЫ

MAX_OUTPUT_TOKENS = 8192
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"

# --- ИНСТРУМЕНТЫ ДЛЯ FUNCTION CALLING ---
def get_current_time(timezone: str = "Europe/Moscow") -> str:
    """Возвращает текущую дату и время для указанной временной зоны."""
    try: return f"Текущее время в {timezone}: {datetime.datetime.now(pytz.timezone(timezone)).strftime('%Y-%m-%d %H:%M:%S')}"
    except pytz.UnknownTimeZoneError: return f"Ошибка: Неизвестная временная зона '{timezone}'."

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.user_data[key] = value
async def fetch_webpage_content(url: str, session: httpx.AsyncClient) -> str | None:
    try:
        response = await session.get(url, timeout=15.0, follow_redirects=True); response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser'); [s.decompose() for s in soup(['script', 'style', 'nav', 'footer', 'header', 'aside'])]
        return ' '.join(soup.stripped_strings)
    except Exception as e: logger.error(f"Ошибка скрапинга {url}: {e}"); return None

# --- НОВЫЙ МЕХАНИЗМ СТРИМИНГА ОТВЕТА В TELEGRAM ---
async def stream_and_send_reply(message_to_edit: Message, stream: Coroutine) -> str:
    full_text, buffer, last_edit_time = "", "", 0
    try:
        async for chunk in stream:
            if text_part := getattr(chunk, 'text', ''): buffer += text_part
            if time.time() - last_edit_time > 1.5 or len(buffer) > 100:
                new_text_portion = full_text + buffer
                if new_text_portion != message_to_edit.text:
                    try:
                        await message_to_edit.edit_text(new_text_portion + " ▌")
                        full_text = new_text_portion; buffer = ""
                        last_edit_time = time.time()
                    except BadRequest as e:
                        if "Message is not modified" not in str(e): logger.warning(f"Ошибка редактирования: {e}")
        
        final_text = full_text + buffer
        if final_text != message_to_edit.text: await message_to_edit.edit_text(final_text)
        return final_text
    except Exception as e:
        logger.error(f"Ошибка стриминга: {e}", exc_info=True)
        final_text_on_error = full_text + buffer + f"\n\n❌ Ошибка: {e}"
        await message_to_edit.edit_text(final_text_on_error)
        return final_text_on_error

# --- ГЛАВНЫЙ ОБРАБОТЧИК ЗАПРОСОВ К GEMINI (ИСПОЛЬЗУЕТ НАТИВНЫЙ ЧАТ) ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_process: str, **kwargs):
    client = context.application.gemini_client
    
    if 'chat_session' not in context.chat_data:
        model_name = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
        tools = [get_current_time, types.Tool(code_execution=types.ToolCodeExecution())]
        if get_user_setting(context, 'search_enabled', True):
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        context.chat_data['chat_session'] = client.chats.create(
            model=f'models/{model_name}',
            history=[],
            config=types.CreateChatConfig(system_instruction=system_instruction_text, tools=tools, temperature=1.0, max_output_tokens=MAX_OUTPUT_TOKENS)
        )
    chat_session = context.chat_data['chat_session']

    placeholder_message = await update.message.reply_text("...")
    
    prompt_text = f"(Текущая дата: {datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime('%Y-%m-%d')})\n{USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))}{html.escape(text_to_process)}"
    message_parts = [prompt_text] + kwargs.get('content_parts', [])

    try:
        stream = chat_session.send_message_stream(message=message_parts)
        await stream_and_send_reply(placeholder_message, stream)
    except Exception as e:
        logger.error(f"Критическая ошибка в process_query: {e}", exc_info=True)
        await placeholder_message.edit_text(f"❌ Произошла серьезная ошибка: {e}")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ TELEGRAM ---
# <<< НАЧАЛО: ВАШЕ СТАРТОВОЕ СООБЩЕНИЕ ВОЗВРАЩЕНО
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if 'selected_model' not in context.user_data: set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'search_enabled' not in context.user_data: set_user_setting(context, 'search_enabled', True)
    if 'temperature' not in context.user_data: set_user_setting(context, 'temperature', 1.0)
    if 'detailed_reasoning_enabled' not in context.user_data: set_user_setting(context, 'detailed_reasoning_enabled', True)
    bot_core_model_key = DEFAULT_MODEL
    raw_bot_core_model_display_name = AVAILABLE_MODELS.get(bot_core_model_key, bot_core_model_key)
    author_channel_link_raw = "https://t.me/denisobovsyom"
    date_knowledge_text_raw = "до начала 2025 года"
    start_message_plain_parts = [
        f"Меня зовут Женя, работаю на Google Gemini {raw_bot_core_model_display_name} с настройками автора бота: {author_channel_link_raw}",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и поиском Google,",
        f"- читаю и понимаю голосовые сообщения, изображения, txt, pdf и веб-страницы,",
        f"- знаю ваше имя, помню историю чата. Пишите лично и добавляйте меня в группы.",
        f"(!) Пользуясь данным ботом, вы автоматически соглашаетесь на отправку ваших сообщений через Google (Search + Gemini API) для получения ответов."]
    start_message_plain = "\n".join(start_message_plain_parts)
    try:
        await update.message.reply_text(start_message_plain, disable_web_page_preview=True)
    except Exception as e: logger.error(f"Failed to send start_message (Plain Text): {e}", exc_info=True)
# <<< КОНЕЦ: ВАШЕ СТАРТОВОЕ СООБЩЕНИЕ ВОЗВРАЩЕНО

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'chat_session' in context.chat_data:
        del context.chat_data['chat_session']
    await update.message.reply_text("🧹 История чата и сессия сброшены.")
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if m == current_model else ''}{name}", callback_data=f"set_model_{m}")] for m, name in sorted(AVAILABLE_MODELS.items())]
    await update.message.reply_text("Выберите модель (сбросит текущую сессию):", reply_markup=InlineKeyboardMarkup(keyboard))
async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    selected = query.data.replace("set_model_", "")
    if selected in AVAILABLE_MODELS:
        set_user_setting(context, 'selected_model', selected)
        if 'chat_session' in context.chat_data: del context.chat_data['chat_session']
        await query.edit_message_text(f"Модель установлена: <b>{AVAILABLE_MODELS[selected]}</b>. Сессия сброшена.", parse_mode=ParseMode.HTML)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text: return
    
    youtube_id = extract_youtube_id(text)
    if youtube_id:
        await update.message.reply_text("📺 Анализирую видео...")
        try:
            transcript = " ".join([d['text'] for d in await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])])
            await process_query(update, context, f"Конспект видео. Запрос: '{text}'.\nТранскрипт:\n{transcript[:30000]}", content_type="youtube", content_id=youtube_id)
        except Exception as e: await update.message.reply_text(f"❌ Ошибка YouTube: {e}"); return
        return

    general_url = extract_general_url(text)
    if general_url:
        await update.message.reply_text("🌐 Читаю страницу...")
        content = await fetch_webpage_content(general_url, context.application.http_client)
        if content: await process_query(update, context, f"Анализ страницы. Запрос: '{text}'.\nТекст:\n{content[:30000]}", content_type="webpage", content_id=general_url)
        else: await update.message.reply_text("❌ Не удалось получить содержимое.")
        return

    await process_query(update, context, text)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or "Опиши, что на этом медиафайле."
    
    file_id, mime_type, content_type_str = None, None, None
    if message.photo:
        await message.reply_text("🖼️ Анализирую фото..."); file_id, mime_type, content_type_str = message.photo[-1].file_id, 'image/jpeg', "image"
    elif message.video:
        await message.reply_text("🎬 Анализирую видео..."); file_id, mime_type, content_type_str = message.video.file_id, message.video.mime_type, "video"
    elif message.voice:
        await message.reply_text("🎤 Слушаю..."); file_id, mime_type, content_type_str = message.voice.file_id, message.voice.mime_type, "voice"
        caption = "Расшифруй это голосовое сообщение и ответь на него."
    
    if not file_id: return
    
    file_bytes = await (await context.bot.get_file(file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))
    await process_query(update, context, caption, content_parts=[media_part], content_type=content_type_str, content_id=file_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > 15 * 1024 * 1024: await update.message.reply_text("❌ Файл слишком большой."); return
    await update.message.reply_text("📄 Изучаю документ...")
    file_bytes = await (await doc.get_file()).download_as_bytearray()
    
    text = None
    if doc.mime_type == 'application/pdf':
        try: text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        except Exception as e: await update.message.reply_text(f"❌ Ошибка PDF: {e}"); return
    else:
        try: text = file_bytes.decode('utf-8')
        except UnicodeDecodeError: text = file_bytes.decode('cp1251', errors='ignore')

    if text: await process_query(update, context, f"Анализ файла '{doc.file_name}'. Запрос: {update.message.caption or ''}\nТекст:\n{text[:30000]}", content_type="document", content_id=doc.file_id)
    else: await update.message.reply_text("❌ Не удалось прочитать файл.")

# --- НАСТРОЙКА И ЗАПУСК БОТА ---
async def setup_bot_and_server(stop_event: asyncio.Event, client: genai.client.Client):
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    application.gemini_client = client

    handlers = [
        CommandHandler("start", start), CommandHandler("model", model_command), CommandHandler("clear", clear_history),
        CallbackQueryHandler(select_model_callback, pattern="^set_model_"),
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.VOICE, handle_media),
        MessageHandler(filters.Document.TEXT | filters.Document.PDF, handle_document),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
    ]
    application.add_handlers(handlers)
    
    await application.initialize()
    await application.bot.set_my_commands([BotCommand("start", "Инфо"), BotCommand("model", "Выбор модели"), BotCommand("clear", "Сброс сессии")])
    
    webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN'))
    logger.info(f"Вебхук установлен: {webhook_url}")
    return application, asyncio.create_task(run_web_server(application, stop_event))

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def webhook_handler(request: aiohttp.web.Request):
        secret = os.getenv('WEBHOOK_SECRET_TOKEN')
        if secret and request.headers.get('X-Telegram-Bot-Api-Secret-Token') != secret: return aiohttp.web.Response(status=403)
        try: await application.process_update(Update.de_json(await request.json(), application.bot)); return aiohttp.web.Response(status=200)
        except Exception as e: logger.error(f"Ошибка вебхука: {e}", exc_info=True); return aiohttp.web.Response(status=500)
            
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), webhook_handler)
    app.router.add_get('/', lambda r: aiohttp.web.Response(text="Bot is running"))
    
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000")))
    
    try: await site.start(); await stop_event.wait()
    finally: await runner.cleanup()

async def main():
    if not os.getenv('GOOGLE_API_KEY'):
        logger.critical("Переменная GOOGLE_API_KEY не установлена!")
        exit(1)
        
    client = genai.Client()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)

    application, web_task, http_client = None, None, None
    try:
        http_client = httpx.AsyncClient()
        application, web_task = await setup_bot_and_server(stop_event, client)
        application.http_client = http_client
        await stop_event.wait()
    finally:
        logger.info("--- Остановка приложения ---")
        if web_task and not web_task.done(): web_task.cancel()
        if application: await application.shutdown()
        if http_client and not http_client.is_closed: await http_client.aclose()
        if application and hasattr(application, 'persistence') and application.persistence: application.persistence.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Приложение остановлено пользователем.")
