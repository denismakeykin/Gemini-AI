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

# --- КОРРЕКТНЫЙ ИМПОРТ SDK И ЕГО КОМПОНЕНТОВ ---
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from google.generativeai.errors import BlockedPromptError, StopCandidateError

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

# --- КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ (из вашего кода, без изменений) ---
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
    async def get_user_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        if all_data:
            for k, d in all_data:
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

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID') # Для поиска Google
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
DATABASE_URL = os.getenv('DATABASE_URL')

required_env_vars = {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY, "GOOGLE_CSE_ID": GOOGLE_CSE_ID, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH}
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars:
    logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    exit(1)

# --- НАСТРОЙКИ БОТА И МОДЕЛИ ---
DEFAULT_MODEL = 'gemini-2.5-flash'
AVAILABLE_MODELS = {'gemini-2.5-flash': '2.5 Flash'} # Пока только одна модель
MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
DDG_MAX_RESULTS = 5
GOOGLE_SEARCH_MAX_RESULTS = 5
USER_ID_PREFIX_FORMAT = "[User {user_id}; Name: {user_name}]: "
TARGET_TIMEZONE = "Europe/Moscow"

# --- НАСТРОЙКИ БЕЗОПАСНОСТИ GEMINI (из вашего кода) ---
SAFETY_SETTINGS = [
    {"category": HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": HarmBlockThreshold.BLOCK_NONE},
]

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (из вашего кода) ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value
async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, text: str, **kwargs):
    history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": [{"text": text}], **kwargs}
    history.append(entry)
    while len(history) > MAX_HISTORY_MESSAGES: history.pop(0)
def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    sanitized_text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'<li>', '• ', sanitized_text, flags=re.IGNORECASE)
    allowed_tags = ['b', 'i', 'u', 's', 'tg-spoiler', 'a', 'code', 'pre']
    tag_regex = re.compile(r'<(/?)([a-z0-9]+)[^>]*>', re.IGNORECASE)
    def strip_unsupported_tags(match):
        return match.group(0) if match.group(2).lower() in allowed_tags else ''
    sanitized_text = tag_regex.sub(strip_unsupported_tags, sanitized_text)
    return sanitized_text.strip()
def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks = []
    tag_stack, remaining_text = [], text_to_chunk
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
def get_current_time_str() -> str:
    return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    if match: return match.group(0).rstrip('.,?!')
    return None
async def fetch_webpage_content(url: str, session: httpx.AsyncClient) -> str | None:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = await session.get(url, timeout=15.0, headers=headers, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]): element.decompose()
        return ' '.join(soup.stripped_strings)
    except Exception as e:
        logger.error(f"Ошибка при скрапинге {url}: {e}")
        return None
def build_context_for_model(chat_history: list) -> list:
    history_for_model, current_chars = [], 0
    for entry in reversed(chat_history):
        if entry.get("role") not in ("user", "model"): continue
        entry_text = "".join(p.get("text", "") for p in entry.get("parts", []))
        if current_chars + len(entry_text) > MAX_CONTEXT_CHARS: break
        history_for_model.append(entry)
        current_chars += len(entry_text)
    return list(reversed(history_for_model))

# --- ЛОГИКА ПОИСКА (из вашего кода) ---
async def perform_google_search(query: str, api_key: str, cse_id: str, num_results: int, session: httpx.AsyncClient) -> list[str] | None:
    # ... (код без изменений)
    search_url, params = "https://www.googleapis.com/customsearch/v1", {'key': api_key, 'cx': cse_id, 'q': query, 'num': num_results, 'lr': 'lang_ru'}
    try:
        response = await session.get(search_url, params=params, timeout=10.0)
        response.raise_for_status()
        items = response.json().get('items', [])
        return [item.get('snippet', item.get('title', '')) for item in items if item.get('snippet') or item.get('title')]
    except Exception as e: logger.error(f"Google Search: Ошибка - {e}", exc_info=True); return None
async def perform_ddg_search(query: str, num_results: int) -> list[str] | None:
    # ... (код без изменений)
    try:
        results = await asyncio.to_thread(DDGS().text, keywords=query, region='ru-ru', max_results=num_results)
        return [r['body'] for r in results] if results else None
    except Exception as e: logger.error(f"DDG Search: Ошибка - {e}", exc_info=True); return None
async def perform_web_search(query: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[str | None, str | None]:
    # ... (код без изменений)
    session = context.bot_data.get('http_client')
    if session and GOOGLE_API_KEY and GOOGLE_CSE_ID:
        google_results = await perform_google_search(query, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results: return "\n".join(f"- {s.strip()}" for s in google_results), "Google"
    ddg_results = await perform_ddg_search(query, DDG_MAX_RESULTS)
    if ddg_results: return "\n".join(f"- {s.strip()}" for s in ddg_results), "DuckDuckGo"
    return None, None

# --- ИЗМЕНЕННАЯ ФУНКЦИЯ ВЫЗОВА GEMINI ---
async def _generate_gemini_response(context: ContextTypes.DEFAULT_TYPE, prompt_parts: list, system_instruction: str, log_prefix: str) -> str:
    user_id = context.user_data.get('id', 'Unknown')
    chat_id = context.chat_data.get('id', 'Unknown')
    client = context.bot_data['gemini_client']
    
    generation_config = genai.GenerationConfig(
        temperature=1.0, 
        max_output_tokens=MAX_OUTPUT_TOKENS
    )
    # --- ИЗМЕНЕНО: Создаем модель с нужными параметрами прямо здесь ---
    model = client.generative_model(
        model_name=DEFAULT_MODEL,
        safety_settings=SAFETY_SETTINGS,
        generation_config=generation_config,
        system_instruction=system_instruction
    )
    
    history = build_context_for_model(context.chat_data.get("history", []))
    contents = history + [{"role": "user", "parts": prompt_parts}]
    
    try:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Отправка запроса к {DEFAULT_MODEL}...")
        response = await model.generate_content_async(contents)
        return response.text
    except (BlockedPromptError, StopCandidateError) as e:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Запрос заблокирован моделью: {e}")
        return "❌ Мой внутренний фильтр безопасности счел запрос или ответ неприемлемым."
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Критическая ошибка при вызове API: {e}", exc_info=True)
        return f"❌ Произошла ошибка при обращении к нейросети: {str(e)[:100]}"
    
# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений)
    model_name = AVAILABLE_MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
    start_message = (
        f"Привет! Я Женя, ассистент на базе Google Gemini <b>{model_name}</b>.\n\n"
        "Я могу:\n"
        "• 💬 Вести диалог с учетом контекста.\n"
        "• 🖼 Анализировать изображения.\n"
        "• 🎤 Понимать голосовые сообщения.\n"
        "• 📄 Читать текстовые файлы, PDF и веб-страницы.\n"
        "• 🌐 Искать актуальную информацию в интернете.\n\n"
        "Просто напиши мне, отправь картинку или файл!"
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений)
    chat_id, user = update.effective_chat.id, update.effective_user
    context.chat_data.clear()
    if context.application.persistence: await context.application.persistence.drop_chat_data(chat_id)
    await update.message.reply_text(f"🧹 История этого чата для меня очищена, {user.first_name}.")

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений)
    replied_message = update.message.reply_to_message
    if not (replied_message and replied_message.voice):
        await update.message.reply_text("ℹ️ Используйте эту команду, отвечая на голосовое сообщение."); return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    voice_file = await replied_message.voice.get_file()
    file_bytes = await voice_file.download_as_bytearray()
    
    client = context.bot_data['gemini_client']
    model = client.generative_model(DEFAULT_MODEL)
    response = await model.generate_content_async(["Расшифруй это аудио и верни только текст.", {"mime_type": "audio/ogg", "data": bytes(file_bytes)}])
    
    await update.message.reply_text(f"📝 <b>Транскрипт:</b>\n\n{html.escape(response.text)}", parse_mode=ParseMode.HTML)

# --- ГЛАВНЫЕ ОБРАБОТЧИКИ ---
async def handle_text_or_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    text = (message.text or message.caption or "").strip()
    if not text: return
    
    # Сохраняем ID для логирования
    context.user_data['id'], context.chat_data['id'] = user.id, message.chat_id
    
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    # --- ЛОГИКА ОПРЕДЕЛЕНИЯ ТИПА ЗАПРОСА (из вашего кода) ---
    content_to_process, content_type, content_id = text, "text", None
    
    youtube_id = extract_youtube_id(text)
    general_url = extract_general_url(text)

    if youtube_id:
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])
            transcript = " ".join([d['text'] for d in transcript_list])
            content_to_process = f"Сделай конспект по транскрипту видео с YouTube: {text}\n\nТРАНСКРИПТ:\n{transcript[:20000]}"
            content_type, content_id = "youtube", youtube_id
        except Exception as e:
            logger.warning(f"Не удалось получить транскрипт для {youtube_id}: {e}")
            content_to_process = f"Сделай краткое описание видео по ссылке (субтитры недоступны): {text}"
            content_type, content_id = "youtube_no_transcript", youtube_id
    elif general_url:
        web_content = await fetch_webpage_content(general_url, context.bot_data['http_client'])
        if web_content:
            content_to_process = f"Проанализируй текст с веб-страницы и ответь на запрос: {text}\n\nТЕКСТ СТРАНИЦЫ:\n{web_content[:20000]}"
            content_type, content_id = "webpage", general_url
        else:
            content_to_process = f"Не удалось загрузить страницу, но ответь на запрос, используя поиск: {text}"
            content_type, content_id = "webpage_failed", general_url
    
    # --- ПОИСК В ИНТЕРНЕТЕ ---
    search_results, search_source = await perform_web_search(content_to_process, context)
    search_context_str = ""
    if search_results:
        search_context_str = f"\n\n==== РЕЗУЛЬТАТЫ ПОИСКА ({search_source}) ====\n{search_results}"

    # --- ФОРМИРОВАНИЕ ИСТОРИИ И ЗАПРОСА ---
    safe_user_name = html.escape(user.first_name or "Пользователь")
    user_prompt_for_history = f"{get_current_time_str()}\n{USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=safe_user_name)}{text}"
    await _add_to_history(context, "user", user_prompt_for_history, content_type=content_type, content_id=content_id)
    
    prompt_for_model = [f"{content_to_process}{search_context_str}"]
    
    # --- ВЫЗОВ МОДЕЛИ И ОТПРАВКА ОТВЕТА ---
    raw_reply = await _generate_gemini_response(context, prompt_for_model, system_instruction_text, "TextQuery")
    sanitized_reply = sanitize_telegram_html(raw_reply or "🤖 Модель не дала ответ.")
    
    sent_message = await message.reply_text(sanitized_reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    if sent_message:
        await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_message.message_id)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    # --- ЭТАП 1: Извлечение ключевых слов (как у вас) ---
    client = context.bot_data['gemini_client']
    extraction_model = client.generative_model(DEFAULT_MODEL)
    extraction_prompt = "Проанализируй это изображение. Если на нем есть хорошо читаемый текст, извлеки его. Если текста нет, опиши ключевые объекты 1-3 словами. Ответ должен быть очень коротким."
    
    search_query = ""
    try:
        response_extract = await extraction_model.generate_content_async([extraction_prompt, {"mime_type": "image/jpeg", "data": bytes(photo_bytes)}])
        search_query = response_extract.text.strip()
    except Exception as e:
        logger.warning(f"Ошибка при извлечении ключевых слов с фото: {e}")
        
    # --- ЭТАП 2: Поиск ---
    search_context_str = ""
    if search_query:
        search_results, search_source = await perform_web_search(search_query, context)
        if search_results:
            search_context_str = f"\n\n==== РЕЗУЛЬТАТЫ ПОИСКА ({search_source}) по '{html.escape(search_query)}' ====\n{search_results}"
            await message.reply_text(f"🔍 Нашел на картинке «_{html.escape(search_query[:60])}_», ищу информацию...", parse_mode=ParseMode.HTML)
            await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    # --- ЭТАП 3: Финальный анализ ---
    caption = message.caption or "Подробно опиши это изображение."
    safe_user_name = html.escape(user.first_name or "Пользователь")
    final_prompt = [
        f"{USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=safe_user_name)}{caption}{search_context_str}",
        {"mime_type": "image/jpeg", "data": bytes(photo_bytes)}
    ]
    
    raw_reply = await _generate_gemini_response(context, final_prompt, system_instruction_text, "PhotoQuery")
    sanitized_reply = sanitize_telegram_html(raw_reply or "🤖 Не удалось проанализировать изображение.")

    await _add_to_history(context, "user", f"{caption or 'Изображение без подписи'}", content_type="image", content_id=photo_file.file_id)
    sent_message = await message.reply_text(sanitized_reply, parse_mode=ParseMode.HTML)
    if sent_message:
        await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_message.message_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    mime_type = doc.mime_type or "application/octet-stream"

    if not (mime_type.startswith('text/') or mime_type == 'application/pdf'):
        await message.reply_text(f"⚠️ Пока могу читать только текстовые файлы и PDF. Ваш тип: `{mime_type}`", parse_mode=ParseMode.HTML); return
    
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
    
    caption = message.caption or f"Проанализируй содержимое файла '{doc.file_name}'"
    await handle_text_or_link(update, context) # Передаем в общий обработчик с извлеченным текстом

# --- ФУНКЦИИ ЗАПУСКА И ОСТАНОВКИ ---
async def handle_telegram_webhook(request: aiohttp.web.Request):
    application = request.app['bot_app']
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return aiohttp.web.Response(text="OK")
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(text="Error", status=500)

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
    # --- ИЗМЕНЕНО: Корректная инициализация клиента ---
    client = genai.Client(api_key=GOOGLE_API_KEY)
    
    persistence = None
    if DATABASE_URL:
        try: persistence = PostgresPersistence(database_url=DATABASE_URL)
        except Exception as e: logger.error(f"Не удалось инициализировать Postgres: {e}. Бот будет работать без сохранения состояния.")
    
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    
    # --- ИЗМЕНЕНО: Добавляем клиент в bot_data ---
    application.bot_data['gemini_client'] = client
    application.bot_data['http_client'] = httpx.AsyncClient()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("transcribe", transcribe_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_or_link))
    application.add_handler(MessageHandler(filters.VOICE, handle_text_or_link)) # Голос тоже идет в текстовый обработчик

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
    
    try:
        await application.bot.set_my_commands([BotCommand("start", "Инфо и помощь"), BotCommand("clear", "Очистить историю"), BotCommand("transcribe", "Текст из голоса")])
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        logger.info(f"Вебхук установлен на {webhook_url}")
        
        await run_web_server(application, stop_event)

    finally:
        logger.info("--- Остановка приложения ---")
        await application.bot_data['http_client'].aclose()
        if persistence: persistence.close()

if __name__ == '__main__':
    asyncio.run(main())
