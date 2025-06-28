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
import mimetypes

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

# --- БАЗА ДАННЫХ (НАДЕЖНАЯ ВЕРСИЯ С ПОЛНЫМ КОНТРАКТОМ) ---
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
    async def refresh_bot_data(self, bot_data: dict) -> None: data = await self.get_bot_data(); bot_data.update(data)
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}; chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"user_data_{user_id}") or {}; user_data.update(data)
    async def flush(self) -> None: pass
    def close(self):
        if self.db_pool: self.db_pool.closeall()

    # --- НОВЫЕ ОБЯЗАТЕЛЬНЫЕ МЕТОДЫ-ЗАГЛУШКИ ---
    async def drop_user_data(self, user_id: int) -> None:
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass


# --- КОНФИГУРАЦИЯ БОТА ---
TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = map(os.getenv, ['TELEGRAM_BOT_TOKEN', 'GOOGLE_API_KEY', 'WEBHOOK_HOST', 'GEMINI_WEBHOOK_PATH', 'DATABASE_URL'])
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Отсутствуют обязательные переменные окружения!")
    exit(1)

DEFAULT_MODEL = 'gemini-2.5-flash'
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_CHARS = 100000
THINKING_BUDGET = 24576
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    if match: return match.group(0).rstrip('.,?!')
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
    s = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    s = re.sub(r'<li>', '• ', s, flags=re.IGNORECASE)
    s = re.sub(r'</?(?!b>|i>|u>|s>|code>|pre>|a>|tg-spoiler>)\w+\s*[^>]*>', '', s)
    return s.strip()

async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list, **kwargs):
    history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": parts, **kwargs}
    history.append(entry)
    while len(history) > MAX_HISTORY_MESSAGES:
        history.pop(0)

def build_context_for_model(chat_history: list) -> list:
    context_for_model = []
    current_chars = 0
    for entry in reversed(chat_history):
        if not all(k in entry for k in ('role', 'parts')): continue
        entry_text = "".join(p.get("text", "") for p in entry.get("parts", []) if isinstance(p, dict))
        entry_chars = len(entry_text)
        if current_chars + entry_chars > MAX_CONTEXT_CHARS and context_for_model:
            logger.info(f"Контекст обрезан. Учтено {len(context_for_model)} из {len(chat_history)} сообщений.")
            break
        clean_entry = {"role": entry["role"], "parts": entry["parts"]}
        context_for_model.insert(0, clean_entry)
        current_chars += entry_chars
    return context_for_model

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096
    sanitized_text = sanitize_telegram_html(text)
    chunks = [sanitized_text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(sanitized_text), MAX_MESSAGE_LENGTH)]
    sent_message = None
    try:
        for i, chunk in enumerate(chunks):
            if i == 0: sent_message = await target_message.reply_html(chunk)
            else: sent_message = await context.bot.send_message(chat_id=target_message.chat_id, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e:
        if "Can't parse entities" in str(e) or "can't parse" in str(e).lower():
            plain_text = re.sub(r'<[^>]*>', '', text)
            plain_chunks = [plain_text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(plain_text), MAX_MESSAGE_LENGTH)]
            for i, chunk in enumerate(plain_chunks):
                if i == 0: sent_message = await target_message.reply_text(chunk)
                else: sent_message = await context.bot.send_message(chat_id=target_message.chat_id, text=chunk)
            return sent_message
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при отправке ответа: {e}", exc_info=True)
    return None

async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list, content_type: str = None, content_id: str = None):
    message, user = update.message, update.effective_user
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    await _add_to_history(context, "user", prompt_parts, user_id=user.id, message_id=message.message_id, content_type=content_type, content_id=content_id)
    client = context.bot_data['gemini_client']
    try:
        context_for_model = build_context_for_model(context.chat_data.get("history", []))
        
        # Настройка бюджета мышления
        thinking_mode = context.user_data.get('thinking_mode', 'auto')
        tool_config = None
        if thinking_mode == 'max':
            tool_config = {"thinking_config": {"max_decoding_steps": THINKING_BUDGET, "temperature": 1.0}}
            logger.info(f"Используется максимальный бюджет мышления: {THINKING_BUDGET} шагов.")
        else:
            logger.info("Используется автоматический бюджет мышления.")

        request_config = types.GenerateContentConfig(
            temperature=1.0, max_output_tokens=MAX_OUTPUT_TOKENS,
            system_instruction=system_instruction_text,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            tool_config=tool_config
        )
        response = await client.aio.models.generate_content(
            model=f'models/{DEFAULT_MODEL}', contents=context_for_model, config=request_config
        )
        reply_text = response.text
        sent_message = await send_reply(message, reply_text, context)
        await _add_to_history(context, "model", [{"text": reply_text}], bot_message_id=sent_message.message_id if sent_message else None)
    except Exception as e:
        logger.error(f"Критическая ошибка в process_query: {e}", exc_info=True)
        await message.reply_text(f"❌ Произошла серьезная ошибка: {str(e)[:500]}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_message = (
        "Я - Женя, лучший ИИ-ассистент на базе Google GEMINI 2.5 Flash:\n\n"
        "• 💬 Веду диалог, понимаю контекст, анализирую данные\n"
        "• 🎤 Понимаю голосовые сообщения, могу переводить в текст\n"
        "• 🖼 Анализирую изображения и видео (до 20 мб)\n"
        "• 📄 Читаю репосты, txt, pdf и веб-страницы\n"
        "• 🌐 Использую умный Google-поиск и огромный объем собственных знаний\n\n"
        "<b>Команды:</b>\n"
        "/transcribe - <i>(в ответе на голосовое)</i> Расшифровать аудио\n"
        "/summarize_yt <i><ссылка></i> - Конспект видео с YouTube\n"
        "/summarize_url <i><ссылка></i> - Выжимка из статьи\n"
        "/thinking - Настроить режим размышлений\n"
        "/clear - Очистить историю чата\n\n"
        "(!) Пользуясь ботом, вы автоматически соглашаетесь на отправку сообщений для получения ответов через Google Gemini API."
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await update.message.reply_text("🧹 История этого чата очищена.")

async def thinking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_mode = context.user_data.get('thinking_mode', 'auto')
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'auto' else ''}Авто (Рекомендуется)", callback_data="set_thinking_auto")],
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'max' else ''}Максимум (Медленнее)", callback_data="set_thinking_max")]
    ]
    await update.message.reply_text("Выберите режим размышлений модели:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_thinking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    new_mode = 'auto' if choice == 'set_thinking_auto' else 'max'
    context.user_data['thinking_mode'] = new_mode
    
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if new_mode == 'auto' else ''}Авто (Рекомендуется)", callback_data="set_thinking_auto")],
        [InlineKeyboardButton(f"{'✅ ' if new_mode == 'max' else ''}Максимум (Медленнее)", callback_data="set_thinking_max")]
    ]
    
    text = f"Режим размышлений установлен на: <b>{'Авто' if new_mode == 'auto' else 'Максимум'}</b>."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied_message = update.message.reply_to_message
    if not (replied_message and replied_message.voice):
        await update.message.reply_text("ℹ️ Используйте эту команду, отвечая на голосовое сообщение."); return
    await update.message.reply_text("🎤 Расшифровываю...")
    file_bytes = await (await replied_message.voice.get_file()).download_as_bytearray()
    client = context.bot_data['gemini_client']
    try:
        response = await client.aio.models.generate_content(
            model=f'models/{DEFAULT_MODEL}',
            contents=[{"text": "Расшифруй это аудио и верни только текст."}, types.Part(inline_data=types.Blob(mime_type=replied_message.voice.mime_type, data=file_bytes))]
        )
        await update.message.reply_text(f"<b>Транскрипт:</b>\n{html.escape(response.text)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка сервиса распознавания: {e}")

async def summarize_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_general_url(" ".join(context.args))
    if not url: await update.message.reply_text("Пожалуйста, укажите URL после команды."); return
    await update.message.reply_text(f"🌐 Читаю страницу: {url}")
    content = await fetch_webpage_content(url, context.bot_data['http_client'])
    if not content: await update.message.reply_text("❌ Не удалось получить содержимое страницы."); return
    prompt = f"Сделай краткую выжимку (summary) по тексту с веб-страницы: {url}\n\nТЕКСТ:\n{content}"
    await process_query(update, context, [{"text": prompt}], content_type="webpage", content_id=url)

async def summarize_yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = extract_youtube_id(" ".join(context.args))
    if not video_id: await update.message.reply_text("Пожалуйста, укажите ссылку на YouTube после команды."); return
    await update.message.reply_text(f"📺 Анализирую видео с YouTube (ID: ...{video_id[-4:]})")
    try:
        transcript = " ".join([d['text'] for d in await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ru', 'en'])])
    except Exception as e: await update.message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return
    prompt = f"Сделай краткий конспект по транскрипту видео с YouTube.\n\nТРАНСКРИПТ:\n{transcript}"
    await process_query(update, context, [{"text": prompt}], content_type="youtube", content_id=video_id)

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    if not message: return
    
    parts, text_parts_for_history = [], []
    content_type, content_id = None, None
    caption_for_history = ""
    
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    base_text_prompt = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}"

    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == message.reply_to_message.message_id:
                prev_user_turn = history[i-1] if i > 0 else None
                if prev_user_turn and prev_user_turn.get("role") == "user":
                    content_type, content_id = prev_user_turn.get("content_type"), prev_user_turn.get("content_id")
                    if content_type and content_id:
                        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
                        prompt_text = f"Это уточняющий вопрос к предыдущему контенту. Пользователь спрашивает: '{message.text}'. Проанализируй исходный материал еще раз и ответь."
                        parts.append({"text": prompt_text})
                        try:
                            if content_type in ["image", "video", "voice"]:
                                file_bytes = await(await context.bot.get_file(content_id)).download_as_bytearray()
                                mime, _ = mimetypes.guess_type(content_id)
                                if not mime: mime = {'image': 'image/jpeg', 'voice': 'audio/ogg', 'video': 'video/mp4'}.get(content_type)
                                parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=file_bytes)))
                            elif content_type == "document":
                                file_bytes = await(await context.bot.get_file(content_id)).download_as_bytearray()
                                doc_text = file_bytes.decode('utf-8', 'ignore')
                                parts[0]["text"] += f"\n\nТЕКСТ ИСХОДНОГО ДОКУМЕНТА:\n{doc_text}"
                            await process_query(update, context, parts, content_type, content_id)
                            return
                        except Exception as e:
                            await message.reply_text(f"❌ Не удалось получить исходный контент для повторного анализа: {e}")
                            return

    if message.photo:
        content_type, content_id = "image", message.photo[-1].file_id
        file_bytes = await (await context.bot.get_file(content_id)).download_as_bytearray()
        parts.append(types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=file_bytes)))
        caption = message.caption or "Опиши это изображение."
        parts.insert(0, {"text": f"{base_text_prompt}{html.escape(caption)}"})
    elif message.video:
        content_type, content_id = "video", message.video.file_id
        file_bytes = await (await context.bot.get_file(content_id)).download_as_bytearray()
        parts.append(types.Part(inline_data=types.Blob(mime_type=message.video.mime_type, data=file_bytes)))
        caption = message.caption or "Опиши это видео."
        parts.insert(0, {"text": f"{base_text_prompt}{html.escape(caption)}"})
    elif message.voice:
        content_type, content_id = "voice", message.voice.file_id
        file_bytes = await (await context.bot.get_file(content_id)).download_as_bytearray()
        parts.append(types.Part(inline_data=types.Blob(mime_type=message.voice.mime_type, data=file_bytes)))
        caption = "Расшифруй это голосовое сообщение и ответь на него."
        parts.insert(0, {"text": f"{base_text_prompt}{html.escape(caption)}"})
    elif message.document:
        doc = message.document
        if doc.file_size > 15 * 1024 * 1024: await message.reply_text("❌ Файл слишком большой."); return
        content_type, content_id = "document", doc.file_id
        file_bytes = await (await doc.get_file()).download_as_bytearray()
        text = ""
        if doc.mime_type == 'application/pdf':
            try: text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
            except Exception as e: await message.reply_text(f"❌ Ошибка извлечения из PDF: {e}"); return
        else:
            try: text = file_bytes.decode('utf-8')
            except UnicodeDecodeError: text = file_bytes.decode('cp1251', errors='ignore')
        caption = message.caption or "Проанализируй содержимое этого файла."
        parts.append({"text": f"{base_text_prompt}{html.escape(f'Анализ файла {doc.file_name}. Комментарий: {caption}')}\n\nТЕКСТ ФАЙЛА:\n{text}"})
    elif message.text:
        parts.append({"text": f"{base_text_prompt}{html.escape(message.text)}"})
    
    if parts:
        await process_query(update, context, parts, content_type, content_id)

# --- НАСТРОЙКА И ЗАПУСК БОТА ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    await application.initialize()
    application.bot_data['gemini_client'] = genai.Client()
    application.bot_data['http_client'] = httpx.AsyncClient()
    commands = [
        BotCommand("start", "Инфо и помощь"),
        BotCommand("clear", "Очистить историю"),
        BotCommand("thinking", "Настроить режим размышлений"),
        BotCommand("transcribe", "Расшифровать аудио (ответом)"),
        BotCommand("summarize_yt", "Конспект видео YouTube"),
        BotCommand("summarize_url", "Выжимка из статьи по ссылке")
    ]
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("thinking", thinking_command))
    application.add_handler(CommandHandler("transcribe", transcribe_command))
    application.add_handler(CommandHandler("summarize_url", summarize_url_command))
    application.add_handler(CommandHandler("summarize_yt", summarize_yt_command))
    application.add_handler(CallbackQueryHandler(select_thinking_callback, pattern="^set_thinking_"))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_message))
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
    runner, site = aiohttp.web.AppRunner(app), aiohttp.web.TCPSite(runner, os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000")))
    try:
        await runner.setup()
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
