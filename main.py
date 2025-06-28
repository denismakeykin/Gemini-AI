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

# --- КОНФИГУРАЦИЯ БОТА ---
TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = map(os.getenv, ['TELEGRAM_BOT_TOKEN', 'GOOGLE_API_KEY', 'WEBHOOK_HOST', 'GEMINI_WEBHOOK_PATH', 'DATABASE_URL'])
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Отсутствуют обязательные переменные окружения!")
    exit(1)

DEFAULT_MODEL = 'gemini-2.5-flash'
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_CHARS = 100000
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"
MEDIA_CONTEXT_TURNS_TTL = 4 # Сколько ходов назад бот будет помнить медиаконтекст

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

def build_context_for_model(chat_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    context_for_model = []
    current_chars = 0
    for entry in reversed(chat_history):
        if not all(k in entry for k in ('role', 'parts')): continue
        entry_text = "".join(p.get("text", "") for p in entry.get("parts", []) if isinstance(p, dict))
        entry_chars = len(entry_text)
        if current_chars + entry_chars > MAX_CONTEXT_CHARS and context_for_model:
            logger.info(f"Контекст обрезан. Учтено {len(context_for_model)} из {len(chat_history)} сообщений.")
            break
        # Формируем чистую запись для API
        clean_parts = []
        for part in entry.get("parts", []):
            if isinstance(part, dict) and "text" in part:
                clean_parts.append({"text": part["text"]})
            elif isinstance(part, types.Part): # Если это уже готовая часть (например, медиа)
                 clean_parts.append(part)
        if clean_parts:
            context_for_model.insert(0, {"role": entry["role"], "parts": clean_parts})
            current_chars += entry_chars
    return context_for_model

# --- ОБРАБОТЧИК ЗАПРОСА ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list, content_type: str = None, content_id: str = None):
    message = update.message
    user = update.effective_user
    
    # Мы добавляем в историю сообщение "user" только здесь, один раз.
    await _add_to_history(context, "user", prompt_parts, user_id=user.id, message_id=message.message_id, content_type=content_type, content_id=content_id)
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    try:
        # Строим контекст из истории
        chat_history = build_context_for_model(context.chat_data.get("history", []))
        
        thinking_mode = context.user_data.get('thinking_mode', 'auto')
        thinking_config = {}
        if thinking_mode == 'max':
            thinking_config['budget'] = 24576
            logger.info("Используется максимальный бюджет мышления (24576).")
        else:
            logger.info("Используется автоматический бюджет мышления.")
        
        # Правильно создаем объект config
        config = types.GenerateContentConfig(
            temperature=1.0, 
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=thinking_config,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=system_instruction_text # Системный промпт живет здесь
        )
        
        # Передаем его в generate_content под правильным именем 'config'
        response = await context.bot_data['gemini_client'].aio.models.generate_content(
            model=f'models/{DEFAULT_MODEL}',
            contents=chat_history,
            config=config
        )

        full_reply_text = sanitize_telegram_html(response.text)
        sent_message = await message.reply_text(full_reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
        # Сохраняем ответ модели в историю
        await _add_to_history(context, "model", [{"text": full_reply_text}], bot_message_id=sent_message.message_id)

    except Exception as e:
        logger.error(f"Критическая ошибка в process_query: {e}", exc_info=True)
        await message.reply_text(f"❌ Произошла серьезная ошибка: {str(e)[:500]}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_message = (
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

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied_message = update.message.reply_to_message
    if not (replied_message and replied_message.voice):
        await update.message.reply_text("ℹ️ Используйте эту команду, отвечая на голосовое сообщение."); return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
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
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    content = await fetch_webpage_content(url, context.bot_data['http_client'])
    if not content: await update.message.reply_text("❌ Не удалось получить содержимое страницы."); return
    prompt_text = f"Сделай краткую выжимку (summary) по тексту с веб-страницы: {url}\n\nТЕКСТ:\n{content}"
    await process_query(update, context, [{"text": prompt_text}], content_type="webpage", content_id=url)

async def summarize_yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = extract_youtube_id(" ".join(context.args))
    if not video_id: await update.message.reply_text("Пожалуйста, укажите ссылку на YouTube после команды."); return
    await update.message.reply_text(f"📺 Анализирую видео с YouTube (ID: ...{video_id[-4:]})")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        transcript = " ".join([d['text'] for d in await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ru', 'en'])])
    except Exception as e: await update.message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return
    prompt_text = f"Сделай краткий конспект по транскрипту видео с YouTube.\n\nТРАНСКРИПТ:\n{transcript}"
    await process_query(update, context, [{"text": prompt_text}], content_type="youtube", content_id=video_id)

async def find_and_re_analyze_context(update: Update, context: ContextTypes.DEFAULT_TYPE, original_text: str) -> bool:
    history = context.chat_data.get("history", [])
    if len(history) < 1: return False

    # Ищем последнее сообщение от пользователя с медиа
    last_media_turn = None
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if turn.get("role") == "user" and turn.get("content_type") in ["image", "video", "document", "webpage", "youtube"]:
            # Проверяем, как давно это было. Если слишком давно, игнорируем.
            if (len(history) - 1 - i) <= MEDIA_CONTEXT_TURNS_TTL:
                last_media_turn = turn
            break

    if not last_media_turn: return False

    logger.info(f"Обнаружен уточняющий вопрос к медиа-контексту типа '{last_media_turn['content_type']}'.")
    
    # Теперь мы нашли "якорь". Мы должны передать его вместе с новым вопросом.
    # Медиа-часть из старого сообщения
    original_media_parts = [p for p in last_media_turn.get("parts", []) if not isinstance(p, dict) or "text" not in p]
    # Текст нового вопроса
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))
    new_text_part = {"text": f"(Текущая дата: {get_current_time_str()})\n{user_prefix}Это уточняющий вопрос к предыдущему материалу. Пользователь спрашивает: '{original_text}'. Проанализируй ИСХОДНЫЙ материал еще раз с учетом этого вопроса и ответь."}
    
    # Объединяем новый текст и старое медиа
    final_prompt_parts = [new_text_part] + original_media_parts
    
    # Запускаем process_query с этой новой "склеенной" информацией
    await process_query(update, context, final_prompt_parts, content_type=last_media_turn["content_type"], content_id=last_media_turn["content_id"])
    return True

async def handle_text_or_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    original_text = ""
    
    if message.voice:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        file_bytes = await (await message.voice.get_file()).download_as_bytearray()
        client = context.bot_data['gemini_client']
        try:
            response = await client.aio.models.generate_content(model=f'models/{DEFAULT_MODEL}', contents=[{"text": "Расшифруй это аудио и верни только текст."}, types.Part(inline_data=types.Blob(mime_type=message.voice.mime_type, data=file_bytes))])
            original_text = response.text.strip()
            if original_text:
                 await message.reply_text(f"<i>Вы сказали: «{html.escape(original_text)}»</i>", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            await message.reply_text(f"❌ Не смог расшифровать голосовое: {e}")
            return
    else:
        original_text = (message.text or "").strip()

    if not original_text: return
    
    if await find_and_re_analyze_context(update, context, original_text):
        return

    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))
    prompt_text = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(original_text)}"
    await process_query(update, context, [{"text": prompt_text}])

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message
    
    caption = message.caption or "Подробно опиши этот медиафайл."
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    text_part = {"text": f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}"}
    
    if message.photo:
        file_id, mime_type, content_type = message.photo[-1].file_id, 'image/jpeg', "image"
    elif message.video:
        file_id, mime_type, content_type = message.video.file_id, message.video.mime_type, "video"
    else: return

    file_bytes = await (await context.bot.get_file(file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))
    
    await process_query(update, context, [text_part, media_part], content_type=content_type, content_id=file_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > 15 * 1024 * 1024: await update.message.reply_text("❌ Файл слишком большой."); return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
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
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))
    prompt_text = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}Проанализируй текст из файла '{doc.file_name}'. Мой комментарий: '{caption}'\n\nТЕКСТ:\n{text}"
    await process_query(update, context, [{"text": prompt_text}], content_type="document", content_id=doc.file_id)

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
        BotCommand("transcribe", "Расшифровать аудио (ответом)"),
        BotCommand("summarize_yt", "Конспект видео YouTube"),
        BotCommand("summarize_url", "Выжимка из статьи по ссылке")
    ]
    handlers = [
        CommandHandler("start", start),
        CommandHandler("clear", clear_history),
        CommandHandler("thinking", thinking_command),
        CommandHandler("transcribe", transcribe_command),
        CommandHandler("summarize_url", summarize_url_command),
        CommandHandler("summarize_yt", summarize_yt_command),
        CallbackQueryHandler(select_thinking_callback, pattern="^set_thinking_"),
        MessageHandler(filters.PHOTO | filters.VIDEO, handle_media),
        MessageHandler(filters.Document.TEXT | filters.Document.PDF, handle_document),
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
