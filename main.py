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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
import aiohttp.web
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, BotCommand
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
from telegram.error import BadRequest, TelegramError
import google.generativeai as genai
from duckduckgo_search import DDGS
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from youtube_transcript_api._errors import RequestBlocked
from pdfminer.high_level import extract_text

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        system_instruction_text = f.read()
    logger.info("Системный промпт успешно загружен из файла system_prompt.md.")
except FileNotFoundError:
    logger.critical("Критическая ошибка: файл system_prompt.md не найден! Бот не может работать без него.")
    system_instruction_text = "Ты — полезный ассистент."
    exit(1)
except Exception as e_prompt_file:
    logger.critical(f"Критическая ошибка при чтении файла system_prompt.md: {e_prompt_file}", exc_info=True)
    system_instruction_text = "Ты — полезный ассистент."
    exit(1)

class PostgresPersistence(BasePersistence):
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
        if not self.db_pool: raise ConnectionError("PostgresPersistence: Пул соединений не инициализирован.")
        last_exception = None
        for attempt in range(3):
            conn = None
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one":
                        result = cur.fetchone()
                        self.db_pool.putconn(conn)
                        return result
                    if fetch == "all":
                        result = cur.fetchall()
                        self.db_pool.putconn(conn)
                        return result
                    conn.commit()
                    self.db_pool.putconn(conn)
                    return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgresPersistence: Ошибка соединения (попытка {attempt + 1}): {e}. Попытка переподключения...")
                last_exception = e
                if conn: self.db_pool.putconn(conn, close=True)
                if attempt < 2: self._connect()
                time.sleep(1 + attempt)
                continue
            except psycopg2.Error as e:
                logger.error(f"PostgresPersistence: Невосстановимая ошибка SQL: {e}")
                if conn:
                    conn.rollback()
                    self.db_pool.putconn(conn)
                return None
        logger.error(f"PostgresPersistence: Не удалось выполнить запрос после всех попыток. Последняя ошибка: {last_exception}")
        return None

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

    async def get_bot_data(self) -> dict:
        return await asyncio.to_thread(self._get_pickled, "bot_data") or {}
    async def update_bot_data(self, data: dict) -> None:
        await asyncio.to_thread(self._set_pickled, "bot_data", data)
    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_chat_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict)
        if all_chat_data:
            for key, data in all_chat_data:
                try: chat_data[int(key.split('_')[-1])] = pickle.loads(data)
                except (ValueError, IndexError): logger.warning(f"PostgresPersistence: Не удалось распарсить ключ чата: {key}")
        return chat_data
    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    async def get_user_data(self) -> defaultdict[int, dict]:
        all_user_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        if all_user_data:
            for key, data in all_user_data:
                try: user_data[int(key.split('_')[-1])] = pickle.loads(data)
                except (ValueError, IndexError): logger.warning(f"PostgresPersistence: Не удалось распарсить ключ пользователя: {key}")
        return user_data
    async def update_user_data(self, user_id: int, data: dict) -> None:
        await asyncio.to_thread(self._set_pickled, f"user_data_{user_id}", data)
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass
    async def drop_chat_data(self, chat_id: int) -> None:
        logger.info(f"PostgresPersistence: Принудительное удаление данных для чата {chat_id}")
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def drop_user_data(self, user_id: int) -> None:
        logger.info(f"PostgresPersistence: Принудительное удаление данных для пользователя {user_id}")
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))
    async def refresh_bot_data(self, bot_data: dict) -> None: data = await self.get_bot_data(); bot_data.update(data)
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}; chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"user_data_{user_id}") or {}; user_data.update(data)
    async def flush(self) -> None: pass

    def close(self):
        if self.db_pool: self.db_pool.closeall(); logger.info("PostgresPersistence: Все соединения с базой данных успешно закрыты.")

HARM_CATEGORIES_STRINGS = ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
BLOCK_NONE_STRING = "BLOCK_NONE"
SAFETY_SETTINGS_BLOCK_NONE = []
BlockedPromptException = type('BlockedPromptException', (Exception,), {})
StopCandidateException = type('StopCandidateException', (Exception,), {})
HarmCategory, HarmBlockThreshold, SafetyRating, BlockReason, FinishReason = (type(n, (object,), {}) for n in ['HarmCategory', 'HarmBlockThreshold', 'SafetyRating', 'BlockReason', 'FinishReason'])

try:
    from google.generativeai.types import HarmCategory as RealHarmCategory, HarmBlockThreshold as RealHarmBlockThreshold, BlockedPromptException as RealBlockedPromptException, StopCandidateException as RealStopCandidateException, SafetyRating as RealSafetyRating, BlockReason as RealBlockReason, FinishReason as RealFinishReason
    logger.info("Типы google.generativeai.types успешно импортированы.")
    HarmCategory, HarmBlockThreshold, BlockedPromptException, StopCandidateException, SafetyRating, BlockReason, FinishReason = RealHarmCategory, RealHarmBlockThreshold, RealBlockedPromptException, RealStopCandidateException, RealSafetyRating, RealBlockReason, RealFinishReason
    temp_safety_settings = []
    all_enums_found = True
    if hasattr(HarmBlockThreshold, 'BLOCK_NONE'):
        block_none_enum = HarmBlockThreshold.BLOCK_NONE
        for cat_str in HARM_CATEGORIES_STRINGS:
            if hasattr(HarmCategory, cat_str): temp_safety_settings.append({"category": getattr(HarmCategory, cat_str), "threshold": block_none_enum})
            else: logger.warning(f"Атрибут категории '{cat_str}' не найден в HarmCategory."); all_enums_found = False; break
    else: logger.warning("Атрибут 'BLOCK_NONE' не найден в HarmBlockThreshold."); all_enums_found = False
    if all_enums_found and temp_safety_settings: SAFETY_SETTINGS_BLOCK_NONE = temp_safety_settings; logger.info("Настройки безопасности BLOCK_NONE установлены с Enum.")
    elif HARM_CATEGORIES_STRINGS: logger.warning("Не удалось создать SAFETY_SETTINGS_BLOCK_NONE с Enum. Используем строки."); SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
    else: logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки безопасности не установлены."); SAFETY_SETTINGS_BLOCK_NONE = []
except ImportError:
    logger.warning("Не удалось импортировать типы из google.generativeai.types. Используем строки и заглушки.")
    if HARM_CATEGORIES_STRINGS: SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]; logger.warning("Настройки безопасности установлены со строками (BLOCK_NONE).")
    else: logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки не установлены."); SAFETY_SETTINGS_BLOCK_NONE = []
except Exception as e:
    logger.error(f"Ошибка при импорте/настройке типов Gemini: {e}", exc_info=True)
    if HARM_CATEGORIES_STRINGS: SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]; logger.warning("Настройки безопасности установлены со строками (BLOCK_NONE) из-за ошибки.")
    else: logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки не установлены из-за ошибки."); SAFETY_SETTINGS_BLOCK_NONE = []

TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, GOOGLE_CSE_ID, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = (os.getenv(k) for k in ["TELEGRAM_BOT_TOKEN", "GOOGLE_API_KEY", "GOOGLE_CSE_ID", "WEBHOOK_HOST", "GEMINI_WEBHOOK_PATH", "DATABASE_URL"])
required_env_vars = {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY, "GOOGLE_CSE_ID": GOOGLE_CSE_ID, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH}
if missing_vars := [name for name, value in required_env_vars.items() if not value]: logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}"); exit(1)

genai.configure(api_key=GOOGLE_API_KEY)

AVAILABLE_MODELS = {'gemini-2.5-flash': '2.5 Flash', 'gemini-2.0-flash': '2.0 Flash'}
DEFAULT_MODEL = 'gemini-2.5-flash' if 'gemini-2.5-flash' in AVAILABLE_MODELS else 'gemini-2.0-flash'
MAX_CONTEXT_CHARS, MAX_HISTORY_MESSAGES, MAX_OUTPUT_TOKENS = 100000, 100, 65536
DDG_MAX_RESULTS, GOOGLE_SEARCH_MAX_RESULTS, RETRY_ATTEMPTS, RETRY_DELAY_SECONDS = 10, 10, 5, 1
IMAGE_DESCRIPTION_PREFIX, YOUTUBE_SUMMARY_PREFIX = "[Описание изображения]: ", "[Конспект видео]: "
VISION_CAPABLE_KEYWORDS, VIDEO_CAPABLE_KEYWORDS = ['gemini-2.5-flash', 'pro', 'vision', 'ultra'], ['gemini-2.5-flash']
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE, REASONING_PROMPT_ADDITION = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow", ""

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.user_data[key] = value

async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, text: str, bot_message_id: int | None = None, content_type: str | None = None, content_id: str | None = None, user_id: int | None = None, message_id: int | None = None):
    chat_id = context.chat_data.get('id', 'Unknown')
    chat_history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": [{"text": text}]}
    if bot_message_id: entry["bot_message_id"] = bot_message_id
    if content_type: entry["content_type"] = content_type
    if content_id: entry["content_id"] = content_id
    if user_id: entry["user_id"] = user_id
    if message_id: entry["message_id"] = message_id
    chat_history.append(entry)
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
    if context.application.persistence: await context.application.persistence.update_chat_data(chat_id, context.chat_data)

def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    allowed_tags = ['b', 'i', 'u', 's', 'tg-spoiler', 'a', 'code', 'pre']
    sanitized_text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'</li>', '\n', sanitized_text, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'<li>', '• ', sanitized_text, flags=re.IGNORECASE)
    def strip_unsupported_tags(match):
        tag_name = match.group(2).lower()
        return match.group(0) if tag_name in allowed_tags else ''
    tag_regex = re.compile(r'<(/?)([a-z0-9]+)[^>]*>', re.IGNORECASE)
    sanitized_text = tag_regex.sub(strip_unsupported_tags, sanitized_text)
    return sanitized_text.strip()

def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks, tag_stack, remaining_text = [], [], text_to_chunk
    tag_regex = re.compile(r'<(/?)(b|i|u|s|code|pre|a|tg-spoiler)>', re.IGNORECASE) # <br> обрабатывается санитайзером
    while len(remaining_text) > chunk_size:
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = remaining_text.rfind(' ', 0, chunk_size)
        if split_pos == -1 or split_pos == 0: split_pos = chunk_size
        current_chunk = remaining_text[:split_pos]
        temp_stack = list(tag_stack)
        for match in tag_regex.finditer(current_chunk):
            tag_name, is_closing = match.group(2).lower(), bool(match.group(1))
            if tag_name == 'a': continue # Не отслеживаем теги <a>, т.к. они сложные
            if not is_closing: temp_stack.append(tag_name)
            elif temp_stack and temp_stack[-1] == tag_name: temp_stack.pop()
        closing_tags = ''.join(f'</{tag}>' for tag in reversed(temp_stack))
        chunks.append(current_chunk + closing_tags)
        tag_stack = temp_stack
        opening_tags = ''.join(f'<{tag}>' for tag in tag_stack)
        remaining_text = opening_tags + remaining_text[split_pos:].lstrip()
    chunks.append(remaining_text)
    return chunks

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096
    reply_chunks = html_safe_chunker(text, MAX_MESSAGE_LENGTH)
    sent_message = None
    chat_id, message_id = target_message.chat_id, target_message.message_id
    current_user_id = target_message.from_user.id if target_message.from_user else "Unknown"
    try:
        for i, chunk in enumerate(reply_chunks):
            reply_to = message_id if i == 0 else None
            sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=reply_to, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e:
        if "Can't parse entities" in str(e).lower():
            problem_chunk_preview = reply_chunks[locals().get('i', 0)][:500] if 'reply_chunks' in locals() else "N/A"
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга HTML: {e}. Чанк: '{problem_chunk_preview}...'. Отправка как обычный текст.")
            plain_text = re.sub(r'<[^>]*>', '', text)
            plain_chunks = [plain_text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(plain_text), MAX_MESSAGE_LENGTH)]
            for i, chunk in enumerate(plain_chunks):
                reply_to = message_id if i == 0 else None
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=reply_to)
                await asyncio.sleep(0.1)
            return sent_message
        else:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Непредвиденная ошибка BadRequest: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка: {html.escape(str(e)[:100])}...")
    except Exception as e:
        logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Общая ошибка отправки: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="❌ Произошла ошибка при отправке ответа.")
    return None

def _get_text_from_response(response_obj, user_id_for_log, chat_id_for_log, log_prefix_for_func) -> str | None:
    try:
        if (text := response_obj.text): return text.strip()
    except (ValueError, Exception) as e: logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Ошибка доступа к response.text: {e}")
    if hasattr(response_obj, 'candidates') and response_obj.candidates:
        try:
            if (parts := response_obj.candidates[0].content.parts): return "".join(p.text for p in parts if hasattr(p, 'text')).strip()
        except (AttributeError, IndexError, Exception) as e: logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Ошибка извлечения текста из candidates: {e}", exc_info=True)
    return None

def _get_effective_context_for_task(task_type: str, original_context: ContextTypes.DEFAULT_TYPE, user_id: int | str, chat_id: int, log_prefix: str) -> ContextTypes.DEFAULT_TYPE:
    capability_map = {"vision": VISION_CAPABLE_KEYWORDS, "video": VIDEO_CAPABLE_KEYWORDS, "audio": ['gemini-2.5', 'pro', 'flash']}
    required_keywords = capability_map.get(task_type, [])
    selected_model = get_user_setting(original_context, 'selected_model', DEFAULT_MODEL)
    if any(keyword in selected_model for keyword in required_keywords): return original_context
    capable_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in required_keywords)]
    if not capable_models: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Нет доступных моделей для '{task_type}'."); return original_context
    fallback_model_id = next((m for m in capable_models if 'flash' in m), capable_models[0])
    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Модель '{selected_model}' не подходит для '{task_type}'. Временно используется '{fallback_model_id}'.")
    temp_context = ContextTypes.DEFAULT_TYPE(application=original_context.application, chat_id=chat_id, user_id=user_id)
    temp_context.user_data = original_context.user_data.copy()
    temp_context.user_data['selected_model'] = fallback_model_id
    return temp_context

def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url: str) -> str | None: return (m.group(1) if (m := re.search(r"(?:v=|\/)([a-zA-Z0-9_-]{11}).*", url)) else None)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key, value in {'selected_model': DEFAULT_MODEL, 'search_enabled': True, 'temperature': 1.0, 'detailed_reasoning_enabled': True}.items():
        context.user_data.setdefault(key, value)
    start_message = f"""Меня зовут Женя, работаю на Google Gemini {AVAILABLE_MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)} с настройками от автора бота: https://t.me/denisobovsyom
- обладаю огромным объемом знаний до начала 2025 года и поиском Google,
- умею понимать и обсуждать изображения, голосовые сообщения (!), файлы txt, pdf и веб-страницы,
- знаю ваше имя, помню историю чата. Пишите лично и добавляйте меня в группы.
(!) Пользуясь данным ботом, вы автоматически соглашаетесь на отправку ваших сообщений через Google (Search + Gemini API) для получения ответов."""
    await update.message.reply_text(start_message, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, user_id, chat_id = update.effective_user, update.effective_user.id, update.effective_chat.id
    user_mention = f"{html.escape(user.first_name)}" if user.first_name else f"User {user_id}"
    context.chat_data.clear()
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата в памяти очищена.")
    if context.application.persistence: await context.application.persistence.drop_chat_data(chat_id)
    await update.message.reply_text(f"🧹 Окей, {user_mention}, история этого чата очищена.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_mention = f"{html.escape(update.effective_user.first_name)}" if update.effective_user.first_name else f"User {update.effective_user.id}"
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if m == current_model else ''}{name}", callback_data=f"set_model_{m}")] for m, name in sorted(AVAILABLE_MODELS.items())]
    await update.message.reply_text(f"{user_mention}, выбери модель (сейчас: {AVAILABLE_MODELS.get(current_model, current_model)}):", reply_markup=InlineKeyboardMarkup(keyboard))

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (replied_message := update.message.reply_to_message): return await update.message.reply_text("ℹ️ Используйте команду ответом на голосовое сообщение.")
    if not replied_message.voice: return await update.message.reply_text("❌ Команда работает только с голосовыми сообщениями.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        voice_file = await replied_message.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
        prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки."
        model = genai.GenerativeModel(get_user_setting(context, 'selected_model', DEFAULT_MODEL))
        response = await asyncio.to_thread(model.generate_content, [prompt, {"mime_type": "audio/ogg", "data": bytes(file_bytes)}])
        if (transcribed_text := _get_text_from_response(response, update.effective_user.id, update.effective_chat.id, "TranscribeCmd")):
            await replied_message.reply_text(f"📝 <b>Транскрипт:</b>\n\n{html.escape(transcribed_text)}", parse_mode=ParseMode.HTML)
        else: await replied_message.reply_text("🤖 Не удалось распознать речь.")
    except Exception as e:
        logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка сервиса распознавания: {str(e)[:100]}")

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected_model_id = query.data.replace("set_model_", "")
    if selected_model_id in AVAILABLE_MODELS:
        set_user_setting(context, 'selected_model', selected_model_id)
        model_name = AVAILABLE_MODELS[selected_model_id]
        user_mention = f"{html.escape(query.from_user.first_name)}" if query.from_user.first_name else f"User {query.from_user.id}"
        reply_text = f"Ок, {user_mention}, твоя модель установлена: <b>{model_name}</b>"
        try: await query.edit_message_text(reply_text, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"Ошибка изменения сообщения (выбор модели): {e}")
                await context.bot.send_message(chat_id=query.message.chat_id, text=reply_text, parse_mode=ParseMode.HTML)

async def _generate_gemini_response(user_prompt_text_initial: str, chat_history_for_model_initial: list, user_id: int | str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, is_text_request_with_search: bool = False) -> str | None:
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = 1.0
    reply = None
    search_block_pattern_to_remove = re.compile(r"\n*\s*==== РЕЗУЛЬТАТЫ ПОИСКА .*?====\n.*", re.DOTALL)
    for attempt in range(RETRY_ATTEMPTS):
        contents_to_use = chat_history_for_model_initial
        if not contents_to_use or contents_to_use[-1]['role'] != 'user':
            contents_to_use.append({'role': 'user', 'parts': [{'text': user_prompt_text_initial}]})
        else:
            contents_to_use[-1]['parts'][0]['text'] = user_prompt_text_initial

        try:
            generation_config = genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model_obj = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_obj = await asyncio.to_thread(model_obj.generate_content, contents_to_use)
            reply = _get_text_from_response(response_obj, user_id, chat_id, "GeminiGen")
            if reply: break
            
            block_reason = getattr(getattr(response_obj, 'prompt_feedback', None), 'block_reason', None)
            if is_text_request_with_search and block_reason:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Попытка с поиском заблокирована ({block_reason}). Повторная попытка без поиска.")
                prompt_without_search = search_block_pattern_to_remove.sub("", user_prompt_text_initial).strip()
                return await _generate_gemini_response(prompt_without_search, chat_history_for_model_initial[:-1], user_id, chat_id, context, False)

        except (BlockedPromptException, StopCandidateException) as e:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Запрос заблокирован/остановлен: {e}")
            reply = "❌ Запрос заблокирован моделью."
            break
        except Exception as e:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка генерации (попытка {attempt + 1}): {e}", exc_info=True)
            if "429" in str(e): reply = "❌ Слишком много запросов к модели, попробуйте позже."
            elif attempt == RETRY_ATTEMPTS - 1: reply = f"❌ Ошибка модели после {RETRY_ATTEMPTS} попыток: {str(e)[:100]}"
            await asyncio.sleep(RETRY_DELAY_SECONDS * (2 ** attempt))
    return reply

async def process_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user, msg, uid, cid = update.effective_user, update.message, update.effective_user.id, update.effective_chat.id
    history = build_context_for_model(context.chat_data.get("history", []))
    search_str, is_search = "", False
    if get_user_setting(context, 'search_enabled', True):
        if (results := await perform_web_search(text, context)):
            search_str, is_search = f"\n\n==== РЕЗУЛЬТАТЫ ПОИСКА ({results[1]}) ====\n{results[0]}", True
    
    safe_name = html.escape(user.first_name or f"User {uid}")
    prompt = f"(Время: {get_current_time_str()})\n{USER_ID_PREFIX_FORMAT.format(user_id=uid, user_name=safe_name)}{html.escape(text)}{search_str}"
    
    await _add_to_history(context, "user", prompt, user_id=uid, message_id=msg.message_id, content_type="voice" if msg.voice else None, content_id=msg.voice.file_id if msg.voice else None)
    
    raw_reply = await _generate_gemini_response(prompt, history, uid, cid, context, is_search)
    sanitized_reply = sanitize_telegram_html(raw_reply or "🤖 Модель не дала ответ.")

    sent_msg = await send_reply(msg, sanitized_reply, context)
    await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_msg.message_id if sent_msg else None)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This logic just transcribes and then passes to the main text processor.
    user, msg, uid, cid = update.effective_user, update.message, update.effective_user.id, update.effective_chat.id
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    try:
        voice_file = await msg.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
        prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки."
        model = genai.GenerativeModel(get_user_setting(context, 'selected_model', DEFAULT_MODEL))
        response = await asyncio.to_thread(model.generate_content, [prompt, {"mime_type": "audio/ogg", "data": bytes(file_bytes)}])
        if (transcribed_text := _get_text_from_response(response, uid, cid, "VoiceHandler")):
            await process_text_query(update, context, transcribed_text)
        else: await msg.reply_text("🤖 Не удалось распознать речь.")
    except Exception as e:
        logger.error(f"Ошибка обработки голоса: {e}", exc_info=True)
        await msg.reply_text(f"❌ Ошибка сервиса распознавания: {str(e)[:100]}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, text = update.message, (update.message.text or update.message.caption or "").strip()
    if not text: return
    context.user_data.setdefault('id', update.effective_user.id); context.user_data.setdefault('first_name', update.effective_user.first_name); context.chat_data.setdefault('id', update.effective_chat.id)
    
    if (r_msg := msg.reply_to_message) and r_msg.from_user.id == context.bot.id and not text.startswith('/'):
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == r_msg.message_id and i > 0:
                prev_entry = history[i-1]
                if (c_type := prev_entry.get("content_type")) and (c_id := prev_entry.get("content_id")):
                    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
                    reanalyze_func = {'image': reanalyze_image_from_id, 'document': reanalyze_document_from_id}.get(c_type)
                    if reanalyze_func and (new_raw_reply := await reanalyze_func(c_id, r_msg.text, text, context)):
                        sanitized_reply = sanitize_telegram_html(new_raw_reply)
                        sent = await send_reply(msg, sanitized_reply, context)
                        safe_name = html.escape(msg.from_user.first_name or f"User {msg.from_user.id}")
                        prompt = f"{USER_ID_PREFIX_FORMAT.format(user_id=msg.from_user.id, user_name=safe_name)}{html.escape(text)}"
                        await _add_to_history(context, "user", prompt, user_id=msg.from_user.id, message_id=msg.message_id)
                        await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent.message_id if sent else None)
                    return
    
    if (yt_id := extract_youtube_id(text)):
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, yt_id, languages=['ru', 'en'])
            transcript = " ".join([d['text'] for d in transcript_list])
            summary_prompt = f"Сделай конспект из расшифровки видео с YouTube. Запрос пользователя: '{html.escape(text)}'. Расшифровка:\n{transcript[:20000]}"
            await process_text_query(update, context, summary_prompt)
        except Exception as e:
            logger.error(f"Ошибка обработки YouTube {yt_id}: {e}", exc_info=True)
            await msg.reply_text(f"❌ Ошибка при обработке видео: {str(e)[:150]}")
        return
        
    await process_text_query(update, context, text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg, uid, cid = update.effective_user, update.message, update.effective_user.id, update.effective_chat.id
    if not msg.photo: return
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    try:
        photo_file = await msg.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        b64_data = base64.b64encode(file_bytes).decode()
        mime_type = "image/jpeg" if file_bytes.startswith(b'\xff\xd8\xff') else "image/png"
        safe_caption = html.escape(msg.caption or "")
        safe_name = html.escape(user.first_name or f"User {uid}")
        
        prompt_text = f"{USER_ID_PREFIX_FORMAT.format(user_id=uid, user_name=safe_name)}Опиши изображение. Подпись: \"{safe_caption}\""
        parts = [{"text": prompt_text}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
        
        model = genai.GenerativeModel(get_user_setting(context, 'selected_model', DEFAULT_MODEL))
        response = await asyncio.to_thread(model.generate_content, parts)
        raw_reply = _get_text_from_response(response, uid, cid, "PhotoHandler")
        sanitized_reply = sanitize_telegram_html(f"{IMAGE_DESCRIPTION_PREFIX}{raw_reply}" if raw_reply else "🤖 Не удалось проанализировать изображение.")
        
        sent_msg = await send_reply(msg, sanitized_reply, context)
        
        history_prompt = f"{USER_ID_PREFIX_FORMAT.format(user_id=uid, user_name=safe_name)}{safe_caption or 'Прислал фото'}"
        await _add_to_history(context, "user", history_prompt, user_id=uid, message_id=msg.message_id, content_type="image", content_id=msg.photo[-1].file_id)
        await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_msg.message_id if sent_msg else None)

    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}", exc_info=True)
        await msg.reply_text(f"❌ Ошибка обработки изображения: {str(e)[:150]}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg, uid, cid = update.effective_user, update.message, update.effective_user.id, update.effective_chat.id
    if not msg.document: return
    doc = msg.document
    if doc.file_size > 15 * 1024 * 1024: return await msg.reply_text("❌ Файл слишком большой (>15MB).")
    await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
        text = ""
        if doc.mime_type == 'application/pdf':
            text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        else:
            try: text = file_bytes.decode('utf-8')
            except UnicodeDecodeError: text = file_bytes.decode('cp1251', errors='ignore')
        
        safe_caption = html.escape(msg.caption or "")
        safe_name = html.escape(user.first_name or f"User {uid}")
        
        prompt = f"Проанализируй текст из файла `{doc.file_name}`. Мой комментарий: \"{safe_caption}\"\n\n```\n{text[:10000]}\n```"
        full_prompt = f"{USER_ID_PREFIX_FORMAT.format(user_id=uid, user_name=safe_name)}{prompt}"
        
        raw_reply = await _generate_gemini_response(full_prompt, [], uid, cid, context, False)
        sanitized_reply = sanitize_telegram_html(raw_reply or "🤖 Не удалось обработать документ.")

        sent_msg = await send_reply(msg, sanitized_reply, context)
        
        history_prompt = f"{USER_ID_PREFIX_FORMAT.format(user_id=uid, user_name=safe_name)}{safe_caption or f'Прислал документ: {doc.file_name}'}"
        await _add_to_history(context, "user", history_prompt, user_id=uid, message_id=msg.message_id, content_type="document", content_id=doc.file_id)
        await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_msg.message_id if sent_msg else None)

    except Exception as e:
        logger.error(f"Ошибка обработки документа {doc.file_name}: {e}", exc_info=True)
        await msg.reply_text(f"❌ Ошибка обработки файла: {str(e)[:150]}")

async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    if not persistence: logger.warning("DATABASE_URL не установлена, бот работает без сохранения состояния.")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()
    
    handlers = [
        CommandHandler("start", start), CommandHandler("clear", clear_history),
        CommandHandler("model", model_command), CommandHandler("transcribe", transcribe_command),
        CallbackQueryHandler(select_model_callback, pattern="^set_model_"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        MessageHandler(filters.VOICE, handle_voice), MessageHandler(filters.PHOTO, handle_photo),
        MessageHandler(filters.Document.ALL, handle_document)
    ]
    for handler in handlers: application.add_handler(handler)
    
    await application.initialize()
    commands = [BotCommand("start", "Инфо и старт"), BotCommand("clear", "Очистить историю"), BotCommand("model", "Выбрать модель"), BotCommand("transcribe", "Голос в текст")]
    await application.bot.set_my_commands(commands)
    
    webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN'))
    logger.info(f"Вебхук установлен на {webhook_url}")
    
    return application, run_web_server(application, stop_event)

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    app['bot_app'] = application
    app.router.add_get('/', lambda r: aiohttp.web.Response(text="OK"))
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), handle_telegram_webhook)
    
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000")))
    
    await site.start()
    logger.info("Веб-сервер запущен.")
    await stop_event.wait()
    await runner.cleanup()
    logger.info("Веб-сервер остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request):
    application = request.app['bot_app']
    if (secret := os.getenv('WEBHOOK_SECRET_TOKEN')) and request.headers.get('X-Telegram-Bot-Api-Secret-Token') != secret:
        return aiohttp.web.Response(status=403)
    try:
        await application.process_update(Update.de_json(await request.json(), application.bot))
        return aiohttp.web.Response()
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(status=500)

async def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=os.getenv("LOG_LEVEL", "INFO").upper())
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def shutdown_handler():
        if not stop_event.is_set(): stop_event.set()
        
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError: signal.signal(sig, lambda s, f: shutdown_handler())
        
    application, web_server_task, http_client = None, None, None
    try:
        http_client = httpx.AsyncClient()
        application, web_server_coro = await setup_bot_and_server(stop_event)
        setattr(application, 'http_client', http_client)
        web_server_task = asyncio.create_task(web_server_coro)
        await stop_event.wait()
    except Exception as e:
        logger.critical("Критическая ошибка на верхнем уровне", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки ---")
        if web_server_task and not web_server_task.done(): web_server_task.cancel()
        if application: await application.shutdown()
        if http_client and not http_client.is_closed: await http_client.aclose()
        if (p := getattr(application, 'persistence', None)) and isinstance(p, PostgresPersistence): p.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")
