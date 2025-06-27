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

# --- БАЗА ДАННЫХ (НАДЕЖНАЯ ВЕРСИЯ) ---
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
        keepalive_args = "keepalives=1 keepalives_idle=60 keepalives_interval=10 keepalives_count=5"
        dsn_with_keepalives = f"{self.dsn} {keepalive_args}" if 'keepalives' not in self.dsn else self.dsn
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=dsn_with_keepalives)
        logger.info("Пул соединений с БД (пере)создан с параметрами keepalive.")

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

genai.configure(api_key=GOOGLE_API_KEY)
AVAILABLE_MODELS = {'gemini-1.5-flash-latest': '1.5 Flash'}
DEFAULT_MODEL = 'gemini-1.5-flash-latest'
MAX_HISTORY_MESSAGES = 50
MAX_OUTPUT_TOKENS = 8192
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.user_data[key] = value
def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    if match:
        url = match.group(0)
        return url.rstrip('.,?!')
    return None
async def fetch_webpage_content(url: str, session: httpx.AsyncClient) -> str | None:
    try:
        response = await session.get(url, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        [s.decompose() for s in soup(['script', 'style', 'nav', 'footer', 'header', 'aside'])]
        return ' '.join(soup.stripped_strings)
    except Exception as e:
        logger.error(f"Ошибка скрапинга {url}: {e}")
        return None
def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    # Заменяем <br> на переносы строк и преобразуем списки
    s = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    s = re.sub(r'<li>', '• ', s, flags=re.IGNORECASE)
    # Удаляем все теги, кроме разрешенных Telegram
    s = re.sub(r'</?(?!b>|i>|u>|s>|code>|pre>|a>|tg-spoiler>)\w+\s*[^>]*>', '', s)
    return s.strip()

async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list, **kwargs):
    history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": parts, **kwargs}
    history.append(entry)
    while len(history) > MAX_HISTORY_MESSAGES:
        history.pop(0)

# --- НОВЫЙ МЕХАНИЗМ СТРИМИНГА И ОБРАБОТКИ ---
async def stream_and_send_reply(message_to_edit: Message, stream: Coroutine) -> str:
    full_text, buffer, last_edit_time = "", "", time.time()
    try:
        async for chunk in stream:
            if text_part := getattr(chunk, 'text', ''):
                buffer += text_part
            current_time = time.time()
            if current_time - last_edit_time > 1.2 or len(buffer) > 150:
                new_text_portion = full_text + buffer
                sanitized_chunk = sanitize_telegram_html(new_text_portion)
                if sanitized_chunk != message_to_edit.text:
                    try:
                        await message_to_edit.edit_text(sanitized_chunk + " ▌")
                        full_text = new_text_portion
                        buffer = ""
                        last_edit_time = current_time
                    except BadRequest as e:
                        if "Message is not modified" not in str(e): logger.warning(f"Ошибка редактирования: {e}")
        
        final_text = full_text + buffer
        sanitized_final = sanitize_telegram_html(final_text)
        if sanitized_final != message_to_edit.text.removesuffix(" ▌"):
             await message_to_edit.edit_text(sanitized_final)
        return final_text # Возвращаем несанитизированный текст для сохранения в историю
    except Exception as e:
        logger.error(f"Ошибка стриминга: {e}", exc_info=True)
        await message_to_edit.edit_text(f"❌ Ошибка стриминга: {e}")
        return ""

async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list, content_type: str = None, content_id: str = None):
    message, user = update.message, update.effective_user
    safe_user_name = html.escape(user.first_name or "Пользователь")
    
    # Добавляем сообщение пользователя в историю
    await _add_to_history(context, "user", prompt_parts, user_id=user.id, message_id=message.message_id, content_type=content_type, content_id=content_id)
    
    # Готовимся к генерации
    model_name = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    client = context.bot_data['gemini_client']
    model = client.models.get(f'models/{model_name}')

    placeholder_message = await message.reply_text("...")
    
    try:
        # Используем stateless streaming
        stream = model.generate_content_stream(
            context.chat_data.get("history", []),
            generation_config=types.GenerationConfig(temperature=1.0, max_output_tokens=MAX_OUTPUT_TOKENS),
            system_instruction=system_instruction_text,
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
        full_reply_text = await stream_and_send_reply(placeholder_message, stream)
        
        # Добавляем ответ модели в историю
        await _add_to_history(context, "model", [{"text": full_reply_text}], bot_message_id=placeholder_message.message_id)

    except Exception as e:
        logger.error(f"Критическая ошибка в process_query: {e}", exc_info=True)
        await placeholder_message.edit_text(f"❌ Произошла серьезная ошибка: {e}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    await update.message.reply_text(
        "Меня зовут Женя, я ваш персональный ассистент на базе Google Gemini 1.5 Flash.\n\n"
        "<b>Что я умею:</b>\n"
        "• 💬 Вести диалог и помнить контекст\n"
        "• 🖼️ Анализировать изображения\n"
        "• 🎤 Понимать голосовые сообщения\n"
        "• 📄 Читать текстовые файлы и PDF\n"
        "• 🌐 Искать актуальную информацию в Google\n\n"
        "<b>Полезные команды:</b>\n"
        "/start - Это сообщение\n"
        "/clear - Очистить историю чата\n"
        "/model - Выбрать другую модель Gemini\n"
        "/transcribe - <i>(в ответе на голосовое)</i> Просто расшифровать аудио\n"
        "/summarize_yt <i><ссылка></i> - Сделать конспект видео с YouTube\n"
        "/summarize_url <i><ссылка></i> - Сделать выжимку из статьи по ссылке\n\n"
        "Просто напишите мне, прикрепите файл или отправьте голосовое!",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await update.message.reply_text("🧹 История этого чата очищена.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if m == current_model else ''}{name}", callback_data=f"set_model_{m}")] for m, name in sorted(AVAILABLE_MODELS.items())]
    await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    selected = query.data.replace("set_model_", "")
    if selected in AVAILABLE_MODELS:
        set_user_setting(context, 'selected_model', selected)
        await query.edit_message_text(f"Модель установлена: <b>{AVAILABLE_MODELS[selected]}</b>.", parse_mode=ParseMode.HTML)

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied_message = update.message.reply_to_message
    if not (replied_message and replied_message.voice):
        await update.message.reply_text("ℹ️ Используйте эту команду, отвечая на голосовое сообщение."); return
    
    await update.message.reply_text("🎤 Расшифровываю...")
    file_bytes = await (await replied_message.voice.get_file()).download_as_bytearray()
    
    client = context.bot_data['gemini_client']
    model = client.models.get(f'models/{DEFAULT_MODEL}') # Используем модель по умолчанию для этой задачи
    
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [{"text": "Расшифруй это аудио и верни только текст."}, types.Part(inline_data=types.Blob(mime_type=replied_message.voice.mime_type, data=file_bytes))]
        )
        await update.message.reply_text(f"<b>Транскрипт:</b>\n{html.escape(response.text)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка сервиса распознавания: {e}")

async def summarize_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_general_url(" ".join(context.args))
    if not url: await update.message.reply_text("Пожалуйста, укажите URL после команды. \nПример: `/summarize_url https://...`"); return
    
    await update.message.reply_text(f"🌐 Читаю страницу: {url}")
    content = await fetch_webpage_content(url, context.bot_data['http_client'])
    if not content: await update.message.reply_text("❌ Не удалось получить содержимое страницы."); return
    
    prompt = f"Сделай краткую выжимку (summary) по тексту с веб-страницы: {url}\n\nТЕКСТ:\n{content[:20000]}"
    await process_query(update, context, [{"text": prompt}], content_type="webpage", content_id=url)

async def summarize_yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = extract_youtube_id(" ".join(context.args))
    if not video_id: await update.message.reply_text("Пожалуйста, укажите ссылку на YouTube после команды."); return
    
    await update.message.reply_text(f"📺 Анализирую видео с YouTube (ID: ...{video_id[-4:]})")
    try:
        transcript = " ".join([d['text'] for d in await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ru', 'en'])])
    except Exception as e: await update.message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return

    prompt = f"Сделай краткий конспект по транскрипту видео с YouTube.\n\nТРАНСКРИПТ:\n{transcript[:20000]}"
    await process_query(update, context, [{"text": prompt}], content_type="youtube", content_id=video_id)

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_text_and_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    original_text = (message.text or "").strip()
    if not original_text: return
    
    # Логика для ответов на сообщения бота (повторный анализ)
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == message.reply_to_message.message_id:
                prev_user_turn = history[i-1] if i > 0 else None
                if prev_user_turn and prev_user_turn.get("role") == "user":
                    content_type = prev_user_turn.get("content_type")
                    content_id = prev_user_turn.get("content_id")
                    
                    if content_type and content_id:
                        # Запускаем повторный анализ
                        prompt = f"Это уточняющий вопрос к предыдущему контенту. Пользователь спрашивает: '{original_text}'. Проанализируй исходный материал еще раз и ответь на этот вопрос."
                        
                        # Загружаем исходный контент в parts
                        parts = [{"text": prompt}]
                        try:
                            if content_type in ["image", "video", "voice"]:
                                file_bytes = await(await context.bot.get_file(content_id)).download_as_bytearray()
                                mime = mimetypes.guess_type(content_id)[0] or 'application/octet-stream'
                                parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=file_bytes)))
                            elif content_type == "document":
                                file_bytes = await(await context.bot.get_file(content_id)).download_as_bytearray()
                                parts[0]["text"] += f"\n\nТЕКСТ ДОКУМЕНТА:\n{file_bytes.decode('utf-8', 'ignore')[:15000]}"
                            # и т.д. для других типов...
                            
                            await process_query(update, context, parts, content_type=content_type, content_id=content_id)
                            return
                        except Exception as e:
                            await message.reply_text(f"❌ Не удалось получить исходный контент для повторного анализа: {e}")
                            return

    # Обычный текстовый запрос
    time_prefix = f"(Текущая дата и время: {get_current_time_str()})\n"
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    await process_query(update, context, [{"text": f"{time_prefix}{user_prefix}{html.escape(original_text)}"}])

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    caption = message.caption or "Опиши, что на этом медиафайле."
    file_id, mime_type, content_type = None, None, None

    if message.photo:
        file_id, mime_type, content_type = message.photo[-1].file_id, 'image/jpeg', "image"
    elif message.video:
        file_id, mime_type, content_type = message.video.file_id, message.video.mime_type, "video"
    elif message.voice:
        file_id, mime_type, content_type = message.voice.file_id, message.voice.mime_type, "voice"
        caption = "Расшифруй это голосовое сообщение и ответь на него."
    else: return
    
    file_bytes = await (await context.bot.get_file(file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))
    
    time_prefix = f"(Текущая дата и время: {get_current_time_str()})\n"
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    text_part = {"text": f"{time_prefix}{user_prefix}{html.escape(caption)}"}
    
    await process_query(update, context, [text_part, media_part], content_type=content_type, content_id=file_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > 15 * 1024 * 1024: await update.message.reply_text("❌ Файл слишком большой."); return
    
    file_bytes = await (await doc.get_file()).download_as_bytearray()
    text, error = None, None
    if doc.mime_type == 'application/pdf':
        try: text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        except Exception as e: error = f"Ошибка извлечения из PDF: {e}"
    else:
        try: text = file_bytes.decode('utf-8')
        except UnicodeDecodeError: text = file_bytes.decode('cp1251', errors='ignore')
    
    if error or text is None: await update.message.reply_text(error or "❌ Не удалось прочитать файл."); return

    caption = update.message.caption or "Проанализируй содержимое этого файла."
    prompt = f"Проанализируй текст из файла '{doc.file_name}'. Запрос пользователя: {caption}\n\nТЕКСТ:\n{text[:20000]}"
    await process_query(update, context, [{"text": prompt}], content_type="document", content_id=doc.file_id)

# --- НАСТРОЙКА И ЗАПУСК БОТА ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    
    # Добавляем клиенты в bot_data
    application.bot_data['gemini_client'] = genai.Client()
    application.bot_data['http_client'] = httpx.AsyncClient()

    # Регистрируем обработчики
    commands_to_register = [
        BotCommand("start", "Инфо и помощь"),
        BotCommand("clear", "Очистить историю"),
        BotCommand("model", "Выбрать модель"),
        BotCommand("transcribe", "Расшифровать аудио (ответом на него)"),
        BotCommand("summarize_yt", "Конспект видео YouTube"),
        BotCommand("summarize_url", "Выжимка из статьи по ссылке")
    ]
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("transcribe", transcribe_command))
    application.add_handler(CommandHandler("summarize_url", summarize_url_command))
    application.add_handler(CommandHandler("summarize_yt", summarize_yt_command))
    application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.VOICE, handle_media))
    application.add_handler(MessageHandler(filters.Document.TEXT | filters.Document.PDF, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_replies))
    
    await application.initialize()
    await application.bot.set_my_commands(commands_to_register)
    
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
        await stop_event.wait()
    finally:
        await runner.cleanup()

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
            if http_client := application.bot_data.get('http_client'):
                if not http_client.is_closed: await http_client.aclose()
            await application.shutdown()
            if hasattr(application, 'persistence') and application.persistence: application.persistence.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Приложение остановлено пользователем.")
