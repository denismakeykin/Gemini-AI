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

# Новые импорты для веб-скрапинга
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
import aiohttp.web
# httpx уже импортирован выше
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
# ИЗМЕНЕНО: Импортируем тот самый Tool для нативного поиска
from google.generativeai.tool import Tool
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
        """Инициализирует или пересоздает пул соединений."""
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
                if conn:
                    self.db_pool.putconn(conn, close=True)
                if attempt < 2:
                    self._connect()
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
        create_table_query = """
        CREATE TABLE IF NOT EXISTS persistence_data (
            key TEXT PRIMARY KEY,
            data BYTEA NOT NULL
        );
        """
        self._execute(create_table_query)

    def _get_pickled(self, key: str) -> object | None:
        result = self._execute("SELECT data FROM persistence_data WHERE key = %s;", (key,), fetch="one")
        return pickle.loads(result[0]) if result and result[0] else None

    def _set_pickled(self, key: str, data: object) -> None:
        pickled_data = pickle.dumps(data)
        query = "INSERT INTO persistence_data (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = %s;"
        self._execute(query, (key, pickled_data, pickled_data))

    async def get_bot_data(self) -> dict:
        bot_data = await asyncio.to_thread(self._get_pickled, "bot_data")
        return bot_data or {}

    async def update_bot_data(self, data: dict) -> None:
        await asyncio.to_thread(self._set_pickled, "bot_data", data)

    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_chat_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict)
        if all_chat_data:
            for key, data in all_chat_data:
                try:
                    chat_id = int(key.split('_')[-1])
                    chat_data[chat_id] = pickle.loads(data)
                except (ValueError, IndexError):
                    logger.warning(f"PostgresPersistence: Не удалось распарсить ключ чата: {key}")
        return chat_data

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)

    async def get_user_data(self) -> defaultdict[int, dict]:
        all_user_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        if all_user_data:
            for key, data in all_user_data:
                try:
                    user_id = int(key.split('_')[-1])
                    user_data[user_id] = pickle.loads(data)
                except (ValueError, IndexError):
                    logger.warning(f"PostgresPersistence: Не удалось распарсить ключ пользователя: {key}")
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
        if self.db_pool:
            self.db_pool.closeall()
            logger.info("PostgresPersistence: Все соединения с базой данных успешно закрыты.")

HARM_CATEGORIES_STRINGS = ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]
BLOCK_NONE_STRING = "BLOCK_NONE"
SAFETY_SETTINGS_BLOCK_NONE = []
BlockedPromptException = type('BlockedPromptException', (Exception,), {})
StopCandidateException = type('StopCandidateException', (Exception,), {})
HarmCategory = type('HarmCategory', (object,), {})
HarmBlockThreshold = type('HarmBlockThreshold', (object,), {})
SafetyRating = type('SafetyRating', (object,), {'category': None, 'probability': None})
BlockReason = type('BlockReason', (object,), {'UNSPECIFIED': 'UNSPECIFIED', 'OTHER': 'OTHER', 'SAFETY': 'SAFETY', 'name': 'UNSPECIFIED'})
FinishReason = type('FinishReason', (object,), {'STOP': 'STOP', 'SAFETY': 'SAFETY', 'RECITATION': 'RECITATION', 'OTHER':'OTHER', 'MAX_TOKENS':'MAX_TOKENS', 'name': 'STOP'})

try:
    from google.generativeai.types import (
        HarmCategory as RealHarmCategory, HarmBlockThreshold as RealHarmBlockThreshold,
        BlockedPromptException as RealBlockedPromptException, StopCandidateException as RealStopCandidateException,
        SafetyRating as RealSafetyRating, BlockReason as RealBlockReason, FinishReason as RealFinishReason
    )
    logger.info("Типы google.generativeai.types успешно импортированы.")
    HarmCategory, HarmBlockThreshold, BlockedPromptException, StopCandidateException, SafetyRating, BlockReason, FinishReason = \
        RealHarmCategory, RealHarmBlockThreshold, RealBlockedPromptException, RealStopCandidateException, RealSafetyRating, RealBlockReason, RealFinishReason

    temp_safety_settings = []
    all_enums_found = True
    if hasattr(HarmBlockThreshold, 'BLOCK_NONE'):
        block_none_enum = HarmBlockThreshold.BLOCK_NONE
        for cat_str in HARM_CATEGORIES_STRINGS:
            if hasattr(HarmCategory, cat_str):
                temp_safety_settings.append({"category": getattr(HarmCategory, cat_str), "threshold": block_none_enum})
            else:
                logger.warning(f"Атрибут категории '{cat_str}' не найден в HarmCategory.")
                all_enums_found = False
                break
    else:
        logger.warning("Атрибут 'BLOCK_NONE' не найден в HarmBlockThreshold.")
        all_enums_found = False

    if all_enums_found and temp_safety_settings:
        SAFETY_SETTINGS_BLOCK_NONE = temp_safety_settings
        logger.info("Настройки безопасности BLOCK_NONE установлены с Enum.")
    elif HARM_CATEGORIES_STRINGS:
        logger.warning("Не удалось создать SAFETY_SETTINGS_BLOCK_NONE с Enum. Используем строки.")
        SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
    else:
        logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки безопасности не установлены.")
        SAFETY_SETTINGS_BLOCK_NONE = []
except ImportError:
    logger.warning("Не удалось импортировать типы из google.generativeai.types. Используем строки и заглушки.")
    if HARM_CATEGORIES_STRINGS:
        SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
        logger.warning("Настройки безопасности установлены со строками (BLOCK_NONE).")
    else:
        logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки не установлены.")
        SAFETY_SETTINGS_BLOCK_NONE = []
except Exception as e_import_types:
    logger.error(f"Ошибка при импорте/настройке типов Gemini: {e_import_types}", exc_info=True)
    if HARM_CATEGORIES_STRINGS:
         SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
         logger.warning("Настройки безопасности установлены со строками (BLOCK_NONE) из-за ошибки.")
    else:
         logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки не установлены из-за ошибки.")
         SAFETY_SETTINGS_BLOCK_NONE = []

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
# УДАЛЕНО: GOOGLE_CSE_ID больше не нужен для нативного поиска
# GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
DATABASE_URL = os.getenv('DATABASE_URL')

# ИЗМЕНЕНО: Убрали GOOGLE_CSE_ID из обязательных переменных
required_env_vars = {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH}
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars:
    logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    exit(1)

genai.configure(api_key=GOOGLE_API_KEY)

# ИЗМЕНЕНО: Обновляем список моделей для совместимости с grounding. Google рекомендует 1.5 Pro/Flash.
# Оставим твои, но стоит проверить их работоспособность с grounding.
AVAILABLE_MODELS = {'gemini-2.5-flash': '2.5 Flash', 'gemini-2.0-flash': '2.0 Flash'}
DEFAULT_MODEL = 'gemini-2.5-flash'
MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192 # Стандартное значение для 1.5 Flash
# УДАЛЕНО: Константы для ручного поиска больше не нужны
# DDG_MAX_RESULTS = 10
# GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
# ИЗМЕНЕНО: Актуализируем ключевые слова для моделей
VISION_CAPABLE_KEYWORDS = ['flash', 'pro', 'vision', 'ultra']
VIDEO_CAPABLE_KEYWORDS = ['flash', 'pro'] # 1.5 модели тоже могут
USER_ID_PREFIX_FORMAT = "[User {user_id}; Name: {user_name}]: "
TARGET_TIMEZONE = "Europe/Moscow"
REASONING_PROMPT_ADDITION = ""

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

async def _add_to_history(
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    text: str,
    bot_message_id: int | None = None,
    content_type: str | None = None,
    content_id: str | None = None,
    user_id: int | None = None,
    message_id: int | None = None,
):
    """Добавляет запись в историю чата и обрезает старые сообщения."""
    chat_id = context.chat_data.get('id', 'Unknown')
    chat_history = context.chat_data.setdefault("history", [])

    entry = {"role": role, "parts": [{"text": text}]}
    if bot_message_id: entry["bot_message_id"] = bot_message_id
    if content_type: entry["content_type"] = content_type
    if content_id: entry["content_id"] = content_id
    if user_id: entry["user_id"] = user_id
    if message_id: entry["message_id"] = message_id
    
    chat_history.append(entry)

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)

    if context.application.persistence:
        await context.application.persistence.update_chat_data(chat_id, context.chat_data)

def sanitize_telegram_html(raw_html: str) -> str:
    """
    Очищает HTML от Gemini, оставляя только теги, поддерживаемые Telegram,
    и преобразуя некоторые неподдерживаемые в текстовые аналоги.
    """
    if not raw_html:
        return ""

    # Теги, которые Telegram поддерживает "как есть"
    allowed_tags = ['b', 'i', 'u', 's', 'tg-spoiler', 'a', 'code', 'pre']
    
    # 1. Заменяем <br> на переносы строк
    sanitized_text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)

    # 2. Преобразуем списки <ul> и <li>
    sanitized_text = re.sub(r'</li>', '\n', sanitized_text, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'<li>', '• ', sanitized_text, flags=re.IGNORECASE)
    
    # 3. Удаляем все остальные теги, которых нет в списке разрешенных
    def strip_unsupported_tags(match):
        tag_name = match.group(2).lower()
        if tag_name in allowed_tags:
            return match.group(0) # Оставляем разрешенный тег
        else:
            return '' # Удаляем неподдерживаемый тег

    tag_regex = re.compile(r'<(/?)([a-z0-9]+)[^>]*>', re.IGNORECASE)
    sanitized_text = tag_regex.sub(strip_unsupported_tags, sanitized_text)
    
    return sanitized_text.strip()

def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    """
    Разделяет текст на части, безопасные для отправки с parse_mode=HTML.
    Корректно обрабатывает вложенные теги с помощью стека.
    """
    chunks = []
    tag_stack = []
    remaining_text = text_to_chunk
    
    tag_regex = re.compile(r'<(/?)(b|i|u|s|code|pre|a|tg-spoiler)>', re.IGNORECASE)

    while len(remaining_text) > chunk_size:
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = remaining_text.rfind(' ', 0, chunk_size)
        if split_pos == -1 or split_pos == 0: split_pos = chunk_size

        current_chunk = remaining_text[:split_pos]
        
        temp_stack = list(tag_stack)
        # Упрощенная логика стека для поддерживаемых тегов
        for match in tag_regex.finditer(current_chunk):
            tag_name = match.group(2).lower()
            is_closing = bool(match.group(1))
            
            if not is_closing:
                temp_stack.append(tag_name)
            elif temp_stack and temp_stack[-1] == tag_name:
                temp_stack.pop()
        
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
    chat_id = target_message.chat_id
    message_id = target_message.message_id
    current_user_id = target_message.from_user.id if target_message.from_user else "Unknown"

    try:
        for i, chunk in enumerate(reply_chunks):
            if i == 0:
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id, parse_mode=ParseMode.HTML)
            else:
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e_md:
        if "Can't parse entities" in str(e_md) or "can't parse" in str(e_md).lower() or "reply message not found" in str(e_md).lower():
            problematic_chunk_preview = "N/A"
            if 'i' in locals() and i < len(reply_chunks):
                problematic_chunk_preview = reply_chunks[i][:500].replace('\n', '\\n')
            
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга HTML ({message_id}): {e_md}. Проблемный чанк (начало): '{problematic_chunk_preview}...'. Попытка отправить как обычный текст без тегов.")
            
            plain_text = re.sub(r'<[^>]*>', '', text)
            plain_chunks = [plain_text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(plain_text), MAX_MESSAGE_LENGTH)]

            for i_plain, chunk_plain in enumerate(plain_chunks):
                 if i_plain == 0:
                     sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain, reply_to_message_id=message_id)
                 else:
                     sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain)
                 await asyncio.sleep(0.1)
            return sent_message
        else:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка при отправке ответа (HTML): {e_md}", exc_info=True)
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при отправке ответа: {html.escape(str(e_md)[:100])}...")
            except Exception as e_error_send:
                logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке отправки: {e_error_send}")
    except Exception as e_other:
        logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Непредвиденная ошибка при отправке ответа: {e_other}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=chat_id, text="❌ Произошла непредвиденная ошибка при отправке ответа.")
        except Exception as e_unexp_send:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение о непредвиденной ошибке: {e_unexp_send}")
    return None

def _get_text_from_response(response_obj, user_id_for_log, chat_id_for_log, log_prefix_for_func) -> str | None:
    reply_text = None
    try:
        reply_text = response_obj.text
        if reply_text:
             logger.debug(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст успешно извлечен из response.text.")
             return reply_text.strip()
        logger.debug(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.text пуст или None, проверяем candidates.")
    except ValueError as e_val_text:
        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.text вызвал ValueError: {e_val_text}. Проверяем candidates...")
    except Exception as e_generic_text:
        logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Неожиданная ошибка при доступе к response.text: {e_generic_text}", exc_info=True)

    if hasattr(response_obj, 'candidates') and response_obj.candidates:
        try:
            candidate = response_obj.candidates[0]
            if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                parts_texts = [part.text for part in candidate.content.parts if hasattr(part, 'text')]
                if parts_texts:
                    reply_text = "".join(parts_texts).strip()
                    if reply_text:
                        logger.info(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст извлечен из response.candidates[0].content.parts.")
                        return reply_text
                    else:
                        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст из response.candidates[0].content.parts оказался пустым после strip.")
                else:
                    logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0].content.parts есть, но не содержат текстовых частей.")
            else:
                fr_candidate = getattr(candidate, 'finish_reason', None)
                fr_name = "N/A"
                if fr_candidate is not None: fr_name = getattr(fr_candidate, 'name', str(fr_candidate))
                is_safety_other_reason = False
                if FinishReason and hasattr(FinishReason, 'SAFETY') and hasattr(FinishReason, 'OTHER'): is_safety_other_reason = (fr_candidate == FinishReason.SAFETY or fr_candidate == FinishReason.OTHER)
                elif fr_name in ['SAFETY', 'OTHER']: is_safety_other_reason = True
                if fr_candidate and not is_safety_other_reason: logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0] не имеет (валидных) content.parts, но finish_reason={fr_name}.")
                else: logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0] не имеет (валидных) content.parts. Finish_reason: {fr_name}")
        except IndexError: logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) IndexError при доступе к response_obj.candidates[0] (список кандидатов пуст).")
        except Exception as e_cand: logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Ошибка при обработке candidates: {e_cand}", exc_info=True)
    else: logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) В ответе response нет ни response.text, ни валидных candidates с текстом.")
    return None

def _get_effective_context_for_task(task_type: str, original_context: ContextTypes.DEFAULT_TYPE, user_id: int | str, chat_id: int, log_prefix: str) -> ContextTypes.DEFAULT_TYPE:
    capability_map = {"vision": VISION_CAPABLE_KEYWORDS, "video": VIDEO_CAPABLE_KEYWORDS, "audio": ['flash', 'pro']}
    required_keywords = capability_map.get(task_type)
    if not required_keywords:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Неизвестный тип задачи '{task_type}' для выбора модели.")
        return original_context
    selected_model = get_user_setting(original_context, 'selected_model', DEFAULT_MODEL)
    is_capable = any(keyword in selected_model for keyword in required_keywords)
    if is_capable:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Модель пользователя '{selected_model}' подходит для задачи '{task_type}'.")
        return original_context
    available_capable_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in required_keywords)]
    if not available_capable_models:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Нет доступных моделей для задачи '{task_type}'.")
        return original_context
    fallback_model_id = next((m for m in available_capable_models if 'flash' in m), available_capable_models[0])
    original_model_name = AVAILABLE_MODELS.get(selected_model, selected_model)
    new_model_name = AVAILABLE_MODELS.get(fallback_model_id, fallback_model_id)
    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Модель пользователя '{original_model_name}' не подходит для '{task_type}'. Временно используется '{new_model_name}'.")
    temp_context = ContextTypes.DEFAULT_TYPE(application=original_context.application, chat_id=chat_id, user_id=user_id)
    temp_context.user_data = original_context.user_data.copy()
    temp_context.user_data['selected_model'] = fallback_model_id
    return temp_context

def get_current_time_str() -> str:
    now_utc = datetime.datetime.now(pytz.utc)
    target_tz = pytz.timezone(TARGET_TIMEZONE)
    now_target = now_utc.astimezone(target_tz)
    return now_target.strftime("%Y-%m-%d %H:%M:%S %Z")

def extract_youtube_id(url_text: str) -> str | None:
    regex = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})"
    match = re.search(regex, url_text)
    return match.group(1) if match else None

def extract_general_url(text: str) -> str | None:
    regex = r'(?<![)\]])https?:\/\/[^\s<>"\'`]+'
    match = re.search(regex, text)
    if match:
        url = match.group(0)
        while url.endswith(('.', ',', '?', '!')): url = url[:-1]
        return url
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if 'selected_model' not in context.user_data: set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'search_enabled' not in context.user_data: set_user_setting(context, 'search_enabled', True)
    if 'temperature' not in context.user_data: set_user_setting(context, 'temperature', 1.0)
    if 'detailed_reasoning_enabled' not in context.user_data: set_user_setting(context, 'detailed_reasoning_enabled', True)
    bot_core_model_key = DEFAULT_MODEL
    raw_bot_core_model_display_name = AVAILABLE_MODELS.get(bot_core_model_key, bot_core_model_key)
    author_channel_link_raw = "https://t.me/denisobovsyom"
    date_knowledge_text_raw = "до начала 2023 года" # ИЗМЕНЕНО: Стандартная дата среза знаний, поиск ее обходит
    start_message_plain_parts = [
        f"Меня зовут Женя, работаю на Google Gemini {raw_bot_core_model_display_name} с настройками от автора бота: {author_channel_link_raw}",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и поиском Google (он включается автоматически для актуальных тем),", # ИЗМЕНЕНО: Пояснение про авто-поиск
        f"- умею понимать и обсуждать изображения, голосовые сообщения (!), файлы txt, pdf и веб-страницы,",
        f"- знаю ваше имя, помню историю чата. Пишите лично и добавляйте меня в группы.",
        f"(!) Пользуясь данным ботом, вы автоматически соглашаетесь на отправку ваших сообщений через Google (Search + Gemini API) для получения ответов."]
    start_message_plain = "\n".join(start_message_plain_parts)
    logger.debug(f"Attempting to send start_message (Plain Text):\n{start_message_plain}")
    try:
        await update.message.reply_text(start_message_plain, disable_web_page_preview=True)
        logger.info("Successfully sent start_message as plain text.")
    except Exception as e: logger.error(f"Failed to send start_message (Plain Text): {e}", exc_info=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    safe_first_name = html.escape(user.first_name) if user.first_name else None
    user_mention = f"{safe_first_name}" if safe_first_name else f"User {user_id}"
    context.chat_data.clear()
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата в памяти очищена по команде от {user_mention}.")
    if context.application.persistence:
        await context.application.persistence.drop_chat_data(chat_id)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата в базе данных удалена.")
    await update.message.reply_text(f"🧹 Окей, {user_mention}, история этого чата очищена.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    safe_first_name = html.escape(user.first_name) if user.first_name else None
    user_mention = f"{safe_first_name}" if safe_first_name else f"User {user_id}"
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = []
    sorted_models = sorted(AVAILABLE_MODELS.items())
    for m, name in sorted_models:
         button_text = f"{'✅ ' if m == current_model else ''}{name}"
         keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_model_{m}")])
    current_model_name = AVAILABLE_MODELS.get(current_model, current_model)
    await update.message.reply_text(f"{user_mention}, выбери модель (сейчас у тебя: {current_model_name}):", reply_markup=InlineKeyboardMarkup(keyboard))

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    replied_message = message.reply_to_message
    if not replied_message:
        await message.reply_text("ℹ️ Пожалуйста, используйте эту команду, отвечая на голосовое сообщение, которое нужно превратить в текст.")
        return
    if not replied_message.voice:
        await message.reply_text("❌ Эта команда работает только с голосовыми сообщениями.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    log_prefix = "TranscribeCmd"
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        voice_file = await replied_message.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания голоса: {e}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке исходного голосового сообщения.")
        return
    transcription_prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки, без каких-либо добавлений или комментариев."
    effective_context = _get_effective_context_for_task("audio", context, user_id, chat_id, log_prefix)
    model_id = get_user_setting(effective_context, 'selected_model', DEFAULT_MODEL)
    model_obj = genai.GenerativeModel(model_id)
    try:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Запрос на чистую транскрипцию в {model_id}.")
        response = await asyncio.to_thread(model_obj.generate_content, [transcription_prompt, {"mime_type": "audio/ogg", "data": bytes(file_bytes)}])
        transcribed_text = _get_text_from_response(response, user_id, chat_id, log_prefix)
        if not transcribed_text:
            await message.reply_text("🤖 Не удалось распознать речь в указанном сообщении.")
            return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка при транскрипции: {e}", exc_info=True)
        await message.reply_text(f"❌ Ошибка сервиса распознавания речи: {str(e)[:100]}")
        return
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Транскрипция успешна.")
    await message.reply_text(f"📝 <b>Транскрипт:</b>\n\n{html.escape(transcribed_text)}", parse_mode=ParseMode.HTML)

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id
    safe_first_name = html.escape(user.first_name) if user.first_name else None
    user_mention = f"{safe_first_name}" if safe_first_name else f"User {user_id}"
    await query.answer()
    callback_data = query.data
    if callback_data and callback_data.startswith("set_model_"):
        selected = callback_data.replace("set_model_", "")
        if selected in AVAILABLE_MODELS:
            set_user_setting(context, 'selected_model', selected)
            model_name = AVAILABLE_MODELS[selected]
            reply_text = f"Ок, {user_mention}, твоя модель установлена: <b>{model_name}</b>"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Модель установлена на {model_name} для {user_mention}.")
            try:
                await query.edit_message_text(reply_text, parse_mode=ParseMode.HTML)
            except BadRequest as e_md:
                 if "Message is not modified" in str(e_md):
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал ту же модель: {model_name}")
                     await query.answer(f"Модель {model_name} уже выбрана.", show_alert=False)
                 else:
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (HTML) для {user_mention}: {e_md}. Отправляю новое.")
                     plain_reply_text = re.sub('<[^<]+?>', '', reply_text)
                     try:
                         await query.edit_message_text(plain_reply_text)
                     except Exception as e_edit_plain:
                          logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение даже как простой текст для {user_mention}: {e_edit_plain}. Отправляю новое.")
                          await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (другая ошибка) для {user_mention}: {e}. Отправляю новое.", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML)
        else:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал неизвестную модель: {selected}")
            try: await query.edit_message_text("❌ Неизвестная модель выбрана.")
            except Exception: await context.bot.send_message(chat_id=chat_id, text="❌ Неизвестная модель выбрана.")
    else:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен неизвестный callback_data от {user_mention}: {callback_data}")
        try: await query.edit_message_text("❌ Ошибка обработки выбора.")
        except Exception: pass

# ИЗМЕНЕНО: Функция генерации ответа значительно упрощена.
# Больше нет нужды в передаче `is_text_request_with_search` и логике с суб-попытками.
async def _generate_gemini_response(
    chat_history_for_model: list,
    user_id: int | str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    system_instruction: str,
    log_prefix: str = "GeminiGen",
    use_google_search: bool = False
) -> str | None:
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = 1.0 # Убрана переменная `temperature` из настроек пользователя для простоты
    reply = None
    
    tools_to_use = [Tool.from_google_search_retrieval()] if use_google_search else None
    
    for attempt in range(RETRY_ATTEMPTS):
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Попытка {attempt + 1}. Поиск: {'Включен' if use_google_search else 'Выключен'}.")
        
        try:
            generation_config = genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            
            # Инициализируем модель с инструментами или без
            model_obj = genai.GenerativeModel(
                model_id, 
                safety_settings=SAFETY_SETTINGS_BLOCK_NONE, 
                generation_config=generation_config, 
                system_instruction=system_instruction,
                tools=tools_to_use
            )
            
            response_obj = await asyncio.to_thread(model_obj.generate_content, chat_history_for_model)
            reply = _get_text_from_response(response_obj, user_id, chat_id, log_prefix)
            
            # Проверка на пустой ответ и выход из цикла, если ответ успешен
            if reply and not reply.startswith(("🤖", "❌")):
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Успешная генерация на попытке {attempt + 1}.")
                break # Успех, выходим из цикла ретраев

            # Обработка пустого ответа или ошибки от модели
            if not reply:
                block_reason_str, finish_reason_str = 'N/A', 'N/A'
                try:
                    if hasattr(response_obj, 'prompt_feedback') and response_obj.prompt_feedback:
                        block_reason_str = response_obj.prompt_feedback.block_reason.name
                    if hasattr(response_obj, 'candidates') and response_obj.candidates:
                        finish_reason_str = response_obj.candidates[0].finish_reason.name
                except Exception as e_reason:
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Не удалось извлечь причину пустого ответа: {e_reason}")

                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                
                if block_reason_str not in ['UNSPECIFIED', 'N/A']:
                    reply = f"🤖 Модель не дала ответ. (Блокировка: {block_reason_str})"
                elif finish_reason_str not in ['STOP', 'UNSPECIFIED', 'N/A']:
                     reply = f"🤖 Модель завершила работу без ответа. (Причина: {finish_reason_str})"
                else:
                    reply = "🤖 Модель дала пустой ответ."
                break # Получили технический ответ, выходим
                
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
            reason_str = str(e_block_stop.args[0]) if e_block_stop.args else "неизвестна"
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Запрос заблокирован/остановлен (попытка {attempt + 1}): {reason_str}")
            reply = f"❌ Запрос заблокирован/остановлен моделью."
            break # Фатальная ошибка для этого запроса
            
        except Exception as e:
            error_message = str(e)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка генерации (попытка {attempt + 1}): {error_message[:200]}...")
            if "429" in error_message or "503" in error_message or "500" in error_message:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Серверная ошибка. Ожидание {wait_time:.1f} сек перед попыткой {attempt + 2}...")
                await asyncio.sleep(wait_time)
                continue # Переходим к следующей попытке
            elif "400" in error_message:
                reply = f"❌ Ошибка в запросе к модели (400 Bad Request)."
            elif "location is not supported" in error_message:
                reply = f"❌ Эта модель недоступна в вашем регионе."
            else:
                reply = f"❌ Непредвиденная ошибка при генерации: {error_message[:100]}..."
            break # Неретраябл ошибка, выходим
            
    # Если после всех попыток ответа нет
    if reply is None:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Не удалось получить ответ после {RETRY_ATTEMPTS} попыток.")
        reply = f"❌ Ошибка: не удалось связаться с моделью после {RETRY_ATTEMPTS} попыток."
        
    return reply

async def fetch_webpage_content(url: str, session: httpx.AsyncClient) -> str | None:
    """Получает и очищает основной текстовый контент с веб-страницы."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = await session.get(url, timeout=15.0, headers=headers, follow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()

        text = ' '.join(soup.stripped_strings)
        return text
    except httpx.HTTPStatusError as e:
        logger.error(f"Ошибка статуса HTTP при получении контента с {url}: {e}")
        return f"Не удалось загрузить страницу: HTTP {e.response.status_code}."
    except Exception as e:
        logger.error(f"Ошибка при получении контента с {url}: {e}", exc_info=True)
        return "Произошла непредвиденная ошибка при загрузке страницы."

async def reanalyze_image_from_id(file_id: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    user_id = context.user_data.get('id', 'Unknown')
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeImgV3"
    try:
        img_file = await context.bot.get_file(file_id)
        file_bytes = await img_file.download_as_bytearray()
        if not file_bytes: return "❌ Не удалось получить исходное изображение (файл пуст)."
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания {file_id}: {e}")
        return f"❌ Ошибка при получении изображения: {e}"
    
    effective_context = _get_effective_context_for_task("vision", context, user_id, chat_id, log_prefix)
    safe_user_name = html.escape(context.user_data.get('first_name', 'Пользователь'))
    
    # ИЗМЕНЕНО: Построение истории для повторного анализа
    # Модель получит старый ответ и новый вопрос в виде диалога.
    chat_history_for_reanalysis = [
        {"role": "user", "parts": [
            # Здесь можно было бы передать исходное изображение, но для краткости передаем только текст.
            # Для полноценного re-analyze лучше передавать и изображение снова.
            {"text": f"Это уточняющий вопрос к изображению. Мой предыдущий вопрос был про него."}
        ]},
        {"role": "model", "parts": [{"text": old_bot_response}]},
        {"role": "user", "parts": [
            {"text": f"Отлично. Теперь новый вопрос от {safe_user_name}: \"{html.escape(user_question)}\"\n\nПосмотри на изображение ещё раз и ответь."},
            # Передаем изображение снова для точного ответа
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(file_bytes).decode()}}
        ]}
    ]
    
    return await _generate_gemini_response(
        chat_history_for_model=chat_history_for_reanalysis, 
        user_id=user_id, 
        chat_id=chat_id, 
        context=effective_context, 
        system_instruction=system_instruction_text, 
        log_prefix=log_prefix,
        use_google_search=True # Включаем поиск и для повторного анализа
    )

async def reanalyze_document_from_id(file_id: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    user_id = context.user_data.get('id', 'Unknown')
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeDocV1"
    try:
        doc_file = await context.bot.get_file(file_id)
        file_bytes = await doc_file.download_as_bytearray()
        text = file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания/чтения {file_id}: {e}")
        return f"❌ Ошибка при получении документа: {e}"
    safe_user_name = html.escape(context.user_data.get('first_name', 'Пользователь'))
    
    chat_history_for_reanalysis = [
        {"role": "user", "parts": [{"text": f"Это был анализ документа. Твой ответ был таким."}]},
        {"role": "model", "parts": [{"text": old_bot_response}]},
        {"role": "user", "parts": [{"text": (
            f"Теперь новый вопрос от {safe_user_name}: \"{html.escape(user_question)}\"\n\n"
            f"ЗАДАЧА: Внимательно перечитай документ и ответь на новый вопрос.\n\n"
            f"СОДЕРЖИМОЕ ДОКУМЕНТА:\n---\n{text[:15000]}\n---"
        )}]}
    ]
    
    return await _generate_gemini_response(
        chat_history_for_model=chat_history_for_reanalysis, 
        user_id=user_id, 
        chat_id=chat_id, 
        context=context, 
        system_instruction=system_instruction_text, 
        log_prefix=log_prefix,
        use_google_search=True # Поиск может быть полезен для контекста
    )

async def reanalyze_youtube_from_id(video_id: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    user_id, chat_id = context.user_data.get('id', 'Unknown'), context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeYouTubeV1"
    try:
        transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ru', 'en'])
        transcript_text = " ".join([d['text'] for d in transcript_list])
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка при повторном получении транскрипта {video_id}: {e}")
        return f"❌ Не удалось получить исходный транскрипт видео: {e}"

    safe_user_name = html.escape(context.user_data.get('first_name', 'Пользователь'))
    
    chat_history_for_reanalysis = [
        {"role": "user", "parts": [{"text": "Это был конспект видео с YouTube."}]},
        {"role": "model", "parts": [{"text": old_bot_response}]},
        {"role": "user", "parts": [{"text": (
             f"Теперь новый вопрос от {safe_user_name}: \"{html.escape(user_question)}\"\n\n"
             f"ЗАДАЧА: Внимательно перечитай транскрипт и ответь на новый вопрос.\n\n"
             f"ТРАНСКРИПТ ВИДЕО:\n---\n{transcript_text[:15000]}\n---"
        )}]}
    ]
    return await _generate_gemini_response(
        chat_history_for_model=chat_history_for_reanalysis, 
        user_id=user_id, 
        chat_id=chat_id, 
        context=context, 
        system_instruction=system_instruction_text, 
        log_prefix=log_prefix,
        use_google_search=True
    )

async def reanalyze_webpage_from_url(url: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    user_id, chat_id = context.user_data.get('id', 'Unknown'), context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeWebpageV1"
    session = getattr(context.application, 'http_client', None)
    if not session or session.is_closed:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) HTTP клиент не доступен.")
        return "❌ Ошибка: отсутствует HTTP клиент для выполнения запроса."

    web_content = await fetch_webpage_content(url, session)
    if not web_content or web_content.startswith("Не удалось") or web_content.startswith("Произошла"):
        return f"❌ Не удалось получить содержимое страницы для повторного анализа. {web_content}"

    safe_user_name = html.escape(context.user_data.get('first_name', 'Пользователь'))
    chat_history_for_reanalysis = [
        {"role": "user", "parts": [{"text": "Это был анализ веб-страницы."}]},
        {"role": "model", "parts": [{"text": old_bot_response}]},
        {"role": "user", "parts": [{"text": (
             f"Теперь новый вопрос от {safe_user_name}: \"{html.escape(user_question)}\"\n\n"
             f"ЗАДАЧА: Внимательно перечитай текст страницы и ответь на новый вопрос.\n\n"
             f"ТЕКСТ СТРАНИЦЫ:\n---\n{web_content[:15000]}\n---"
        )}]}
    ]
    return await _generate_gemini_response(
        chat_history_for_model=chat_history_for_reanalysis, 
        user_id=user_id, 
        chat_id=chat_id, 
        context=context, 
        system_instruction=system_instruction_text, 
        log_prefix=log_prefix,
        use_google_search=True
    )

def build_context_for_model(chat_history: list) -> list:
    # ИЗМЕНЕНО: Эта функция теперь еще и правильно обрабатывает мультимодальные входы
    clean_history = []
    for entry in chat_history:
        if entry.get("role") in ("user", "model") and isinstance(entry.get("parts"), list):
            # Пропускаем пустые записи
            if not any(part.get("text") or part.get("inline_data") for part in entry["parts"]):
                continue
            clean_history.append(entry)

    history_for_model = []
    current_chars = 0
    # Идем в обратном порядке, чтобы собрать самый свежий контекст
    for entry in reversed(clean_history):
        entry_text = "".join(p.get("text", "") for p in entry.get("parts", []))
        # Простое ограничение по символам. Для мультимодальных чатов может потребоваться более сложная логика.
        if current_chars + len(entry_text) > MAX_CONTEXT_CHARS:
            logger.info(f"Обрезка истории по символам. Учтено {len(history_for_model)} сообщений из {len(clean_history)}.")
            break
        history_for_model.append(entry)
        current_chars += len(entry_text)
    
    history_for_model.reverse() # Восстанавливаем хронологический порядок
    return history_for_model

# УДАЛЕНО: Функции ручного поиска больше не нужны в основном потоке.
# async def perform_google_search(...)
# async def perform_ddg_search(...)
# async def perform_web_search(...)

# ИЗМЕНЕНО: Функция полностью переписана для использования нативного поиска.
async def process_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_process: str, content_type: str | None = None, content_id: str | None = None):
    chat_id, user, message, user_id = update.effective_chat.id, update.effective_user, update.message, update.effective_user.id
    safe_user_name = html.escape(user.first_name) if user.first_name else "Пользователь"
    
    # --- ЭТАП 1: Подготовка истории для модели ---
    current_time_str = get_current_time_str()
    time_prefix_for_prompt = f"(Текущая дата и время: {current_time_str})\n"
    user_message_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=safe_user_name)
    
    # Мы больше не добавляем результаты поиска в историю. Просто чистый запрос пользователя.
    prompt_for_history = f"{time_prefix_for_prompt}{user_message_prefix}{html.escape(text_to_process)}"
    
    final_content_type = content_type if content_type else ("voice" if message.voice else None)
    final_content_id = content_id if content_id else (message.voice.file_id if message.voice else None)
    
    await _add_to_history(
        context,
        "user",
        prompt_for_history,
        user_id=user_id,
        message_id=message.message_id,
        content_type=final_content_type,
        content_id=final_content_id,
    )

    # --- ЭТАП 2: Вызов модели с активированным поиском ---
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Вызов Gemini с нативным Google Search Grounding.")

    history_for_model = build_context_for_model(context.chat_data.get("history", []))

    # Вызываем нашу упрощенную функцию-обертку, явно включая поиск
    raw_gemini_reply = await _generate_gemini_response(
        chat_history_for_model=history_for_model,
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="GroundedGen",
        use_google_search=True # Главное изменение!
    )

    # --- ЭТАП 3: Отправка ответа и сохранение в историю ---
    sanitized_reply = sanitize_telegram_html(raw_gemini_reply or "🤖 Модель не дала ответ.")
    sent_message = await send_reply(message, sanitized_reply, context)
    # Сохраняем чистый ответ модели в историю
    await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_message.message_id if sent_message else None)
    
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user, message = update.effective_chat.id, update.effective_user, update.message
    if not user or not message or not message.voice: return
    user_id, log_prefix = user.id, "VoiceHandler"
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        voice_file = await message.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания голоса: {e}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке вашего голосового сообщения."); return
    
    # Используем модель для транскрипции. Здесь поиск не нужен.
    effective_context = _get_effective_context_for_task("audio", context, user_id, chat_id, log_prefix)
    
    transcription_prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки, без каких-либо добавлений или комментариев."
    history_for_transcribe = [{"role": "user", "parts": [
        transcription_prompt, 
        {"mime_type": "audio/ogg", "data": bytes(file_bytes)}
    ]}]

    transcribed_text = await _generate_gemini_response(
        chat_history_for_model=history_for_transcribe,
        user_id=user_id,
        chat_id=chat_id,
        context=effective_context,
        system_instruction="Ты - точный инструмент транскрибации.", # Упрощенная инструкция
        log_prefix="VoiceTranscribe",
        use_google_search=False # Поиск для транскрипции не нужен
    )
    
    if not transcribed_text or transcribed_text.startswith(("🤖", "❌")):
        await message.reply_text(transcribed_text or "🤖 Не удалось распознать речь. Попробуйте еще раз."); return

    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Голос расшифрован -> '{transcribed_text}'. Передача в обработчик.")
    # ИЗМЕНЕНО: передаем file_id голосового сообщения
    await process_text_query(update, context, transcribed_text, content_type="voice", content_id=message.voice.file_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or (not message.text and not message.caption):
        return

    chat_id, user, user_id = update.effective_chat.id, update.effective_user, update.effective_user.id
    original_text = (message.text or message.caption).strip()
    chat_history = context.chat_data.setdefault("history", [])
    context.user_data['id'], context.user_data['first_name'], context.chat_data['id'] = user_id, user.first_name, chat_id
    safe_user_name = html.escape(user.first_name or "Пользователь")

    # --- БЛОК 1: ОБРАБОТКА ОТВЕТА НА СООБЩЕНИЕ БОТА ---
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id and not original_text.startswith('/'):
        replied_to_id = message.reply_to_message.message_id
        old_bot_response = message.reply_to_message.text or ""

        for i in range(len(chat_history) - 1, -1, -1):
            if chat_history[i].get("role") == "model" and chat_history[i].get("bot_message_id") == replied_to_id:
                if i > 0 and chat_history[i-1].get("role") == "user":
                    prev_user_entry = chat_history[i-1]
                    content_type = prev_user_entry.get("content_type")
                    content_id = prev_user_entry.get("content_id")

                    if content_type and content_id:
                        new_reply_text = None
                        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

                        if content_type == "image":
                            new_reply_text = await reanalyze_image_from_id(content_id, old_bot_response, original_text, context)
                        elif content_type == "document":
                            new_reply_text = await reanalyze_document_from_id(content_id, old_bot_response, original_text, context)
                        elif content_type == "youtube":
                            new_reply_text = await reanalyze_youtube_from_id(content_id, old_bot_response, original_text, context)
                        elif content_type == "webpage":
                            new_reply_text = await reanalyze_webpage_from_url(content_id, old_bot_response, original_text, context)
                        # ИЗМЕНЕНО: Добавляем обработку ответа на голосовое сообщение
                        elif content_type == "voice":
                            # Для голоса просто продолжаем диалог, так как аудио уже обработано.
                            # Здесь можно было бы реализовать re-analyze аудио, но это сложнее.
                            # Пока просто передаем в обычный обработчик.
                            await process_text_query(update, context, original_text)
                            return
                        
                        if new_reply_text:
                            # История для re-analyze уже добавлена внутри самих функций reanalyze_*
                            # Поэтому просто отправляем ответ.
                            sanitized_reply = sanitize_telegram_html(new_reply_text)
                            sent_message = await send_reply(message, sanitized_reply, context)
                            # И добавляем ответ бота в историю
                            await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_message.message_id if sent_message else None)
                            return
                break 
    
    # --- БЛОК 2: ОБРАБОТКА НОВОГО СООБЩЕНИЯ СО ССЫЛКОЙ ---
    youtube_id = extract_youtube_id(original_text)
    general_url = extract_general_url(original_text)
    session = getattr(context.application, 'http_client', None)

    if youtube_id:
        log_prefix = "YouTubeHandler"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Обнаружена ссылка YouTube (ID: {youtube_id}).")
        await message.reply_text(f"Окей, {safe_user_name}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])
            transcript_text = " ".join([d['text'] for d in transcript_list])
        except (TranscriptsDisabled, NoTranscriptFound): await message.reply_text("❌ К сожалению, для этого видео нет субтитров."); return
        except RequestBlocked: await message.reply_text("❌ YouTube временно заблокировал мои запросы. Попробуйте позже."); return
        except Exception as e_transcript:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка: {e_transcript}", exc_info=True)
            await message.reply_text("❌ Ошибка при получении субтитров."); return

        summary_prompt = (f"Сделай информативный конспект по расшифровке видео с YouTube. "
                          f"Запрос пользователя: '{html.escape(original_text)}'.\n\n"
                          f"--- РАСШИФРОВКА ---\n{transcript_text[:20000]}\n--- КОНЕЦ ---")
        await process_text_query(update, context, summary_prompt, content_type="youtube", content_id=youtube_id)
        return

    elif general_url and session and not session.is_closed:
        log_prefix = "WebpageHandler"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Обнаружена ссылка на веб-страницу: {general_url}")
        await message.reply_text(f"Минуточку, {safe_user_name}, читаю страницу...")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        web_content = await fetch_webpage_content(general_url, session)
        if not web_content or web_content.startswith("Не удалось") or web_content.startswith("Произошла"):
            await message.reply_text(f"Не удалось обработать страницу. {web_content}")
            return

        summary_prompt = (f"Проанализируй текст с веб-страницы и ответь на запрос пользователя. "
                          f"Запрос пользователя: '{html.escape(original_text)}'.\n\n"
                          f"--- ТЕКСТ СТРАНИЦЫ ---\n{web_content[:20000]}\n--- КОНЕЦ ---")
        await process_text_query(update, context, summary_prompt, content_type="webpage", content_id=general_url)
        return

    # --- БЛОК 3: ОБРАБОТКА ОБЫЧНОГО ТЕКСТОВОГО СООБЩЕНИЯ ---
    await process_text_query(update, context, original_text)

# ИЗМЕНЕНО: Функция радикально упрощена. Больше нет двухэтапного анализа.
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user = update.effective_chat.id, update.effective_user
    if not user:
        logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя.")
        return
    user_id, message, log_prefix_handler = user.id, update.message, "PhotoVision"
    if not message or not message.photo:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_photo не найдено фото.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    photo_file_id = message.photo[-1].file_id

    try:
        photo_file = await context.bot.get_file(photo_file_id)
        file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes:
            await message.reply_text("❌ Не удалось загрузить изображение (файл пуст).")
            return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото по file_id: {photo_file_id}: {e}", exc_info=True)
        await message.reply_text("❌ Не удалось загрузить изображение.")
        return

    safe_user_caption = html.escape(message.caption or "Проанализируй это изображение.")
    safe_user_name = html.escape(user.first_name if user.first_name else "Пользователь")
    effective_context_photo = _get_effective_context_for_task("vision", context, user_id, chat_id, log_prefix_handler)
    
    # --- ЭТАП 1: Подготовка истории и вызов модели ---
    current_time_str_photo = get_current_time_str()
    prompt_text = (
        f"(Текущая дата и время: {current_time_str_photo})\n"
        f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=safe_user_name)}"
        f"{safe_user_caption}"
    )
    
    # Создаем запись для истории, которая пойдет в модель
    history_for_model = build_context_for_model(context.chat_data.get("history", []))
    
    mime_type = "image/jpeg" if file_bytes.startswith(b'\xff\xd8\xff') else "image/png"
    
    # Добавляем текущий запрос с изображением
    history_for_model.append({
        "role": "user",
        "parts": [
            {"text": prompt_text},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode()}}
        ]
    })
    
    # Сохраняем в "вечную" историю чата для контекста будущих сообщений
    await _add_to_history(
        context, 
        "user", 
        USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=safe_user_name) + (message.caption or "Отправлено изображение"), 
        user_id=user_id, 
        message_id=message.message_id, 
        content_type="image", 
        content_id=photo_file_id
    )

    # Вызываем модель с поиском. Она сама разберется, что искать.
    raw_reply_photo = await _generate_gemini_response(
        chat_history_for_model=history_for_model,
        user_id=user_id,
        chat_id=chat_id,
        context=effective_context_photo,
        system_instruction=system_instruction_text,
        log_prefix="PhotoVisionGrounded",
        use_google_search=True
    )
    
    # --- ЭТАП 2: Отправка ответа и сохранение ---
    sanitized_reply = sanitize_telegram_html(raw_reply_photo or "🤖 Не удалось проанализировать изображение.")
    sent_message = await send_reply(message, sanitized_reply, context)
    await _add_to_history(context, "model", sanitized_reply, bot_message_id=sent_message.message_id if sent_message else None)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user = update.effective_chat.id, update.effective_user
    if not user: logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id, message, log_prefix_handler = user.id, update.message, "DocHandler"
    if not message or not message.document: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_document нет документа."); return
    doc = message.document
    allowed_mime_prefixes, allowed_mime_types = ('text/',), ('application/pdf', 'application/json', 'application/xml', 'application/csv')
    mime_type = doc.mime_type or "application/octet-stream"
    if not (any(mime_type.startswith(p) for p in allowed_mime_prefixes) or mime_type in allowed_mime_types):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы и PDF... Ваш тип: `{mime_type}`", parse_mode=ParseMode.HTML); return
    if doc.file_size > 15 * 1024 * 1024: await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> 15 MB)."); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        await message.reply_text("❌ Не удалось загрузить файл."); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None
    if mime_type == 'application/pdf':
        try: text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        except Exception as e_pdf:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка извлечения из PDF '{doc.file_name}': {e_pdf}", exc_info=True)
            await update.message.reply_text(f"❌ Не удалось извлечь текст из PDF-файла `{doc.file_name}`."); return
    else:
        try: text = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try: text = file_bytes.decode('cp1251')
            except UnicodeDecodeError: await update.message.reply_text(f"❌ Не удалось прочитать текстовый файл `{doc.file_name}`. Попробуйте кодировку UTF-8 или CP1251."); return
    if text is None: await update.message.reply_text(f"❌ Не удалось извлечь текст из файла `{doc.file_name}`."); return
    
    # Используем process_text_query для унификации
    user_comment = message.caption or ""
    prompt_with_doc = (f"Проанализируй текст из файла и ответь на мой комментарий. "
                       f"Мой комментарий: '{html.escape(user_comment)}'.\n\n"
                       f"--- ТЕКСТ ФАЙЛА '{doc.file_name}' ---\n{text[:15000]}\n--- КОНЕЦ ---")
                       
    await process_text_query(update, context, prompt_with_doc, content_type="document", content_id=doc.file_id)

async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = None
    if DATABASE_URL:
        try:
            persistence = PostgresPersistence(database_url=DATABASE_URL)
            logger.info("Персистентность включена (PostgreSQL).")
        except Exception as e:
            logger.error(f"Не удалось инициализировать PostgresPersistence: {e}. Бот будет работать без сохранения состояния.", exc_info=True)
            persistence = None
    else: logger.warning("Переменная окружения DATABASE_URL не установлена. Бот будет работать без сохранения состояния.")
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("transcribe", transcribe_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_"))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    try:
        await application.initialize()
        commands = [BotCommand("start", "Начать работу и инфо"), BotCommand("transcribe", "Превратить голосовое в текст"), BotCommand("model", "Выбрать модель Gemini"), BotCommand("clear", "Очистить историю чата")]
        await application.bot.set_my_commands(commands)
        logger.info("Команды меню бота успешно установлены.")
        webhook_host_cleaned = WEBHOOK_HOST.rstrip('/')
        webhook_path_segment = GEMINI_WEBHOOK_PATH.strip('/')
        webhook_url = f"{webhook_host_cleaned}/{webhook_path_segment}"
        logger.info(f"Попытка установки вебхука: {webhook_url}")
        secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
        await application.bot.set_webhook( url=webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, secret_token=secret_token if secret_token else None )
        logger.info(f"Вебхук успешно установлен на {webhook_url}" + (" с секретным токеном." if secret_token else "."))
        web_server_coro = run_web_server(application, stop_event)
        return application, web_server_coro
    except Exception as e:
        logger.critical(f"Критическая ошибка при инициализации бота или установке вебхука: {e}", exc_info=True)
        if persistence and isinstance(persistence, PostgresPersistence): persistence.close()
        raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def health_check(request):
        try:
            bot_info = await application.bot.get_me()
            if bot_info: return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
            else: return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except Exception as e: return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)
    app.router.add_get('/', health_check)
    app['bot_app'] = application
    webhook_path = '/' + GEMINI_WEBHOOK_PATH.strip('/')
    app.router.add_post(webhook_path, handle_telegram_webhook)
    logger.info(f"Вебхук будет слушаться на пути: {webhook_path}")
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    site = aiohttp.web.TCPSite(runner, host, port)
    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{host}:{port}")
        await stop_event.wait()
    except asyncio.CancelledError: logger.info("Задача веб-сервера отменена.")
    finally:
        logger.info("Начало остановки веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app')
    if not application: return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token and request.headers.get('X-Telegram-Bot-Api-Secret-Token') != secret_token:
        return aiohttp.web.Response(status=403, text="Forbidden: Invalid secret token.")
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return aiohttp.web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=log_level)
    logger.setLevel(log_level)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError: signal.signal(sig, lambda s, f: signal_handler())
    application, web_server_task, http_client_custom = None, None, None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        http_client_custom = httpx.AsyncClient()
        application, web_server_coro = await setup_bot_and_server(stop_event)
        setattr(application, 'http_client', http_client_custom)
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask")
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки...")
        await stop_event.wait()
    except Exception as e: logger.critical(f"Критическая ошибка во время запуска или ожидания: {e}", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки ---")
        if not stop_event.is_set(): stop_event.set()
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера...")
             web_server_task.cancel()
             try: await web_server_task
             except asyncio.CancelledError: logger.info("Задача веб-сервера успешно отменена.")
        if application:
            logger.info("Остановка приложения Telegram бота...")
            await application.shutdown()
        if http_client_custom and not http_client_custom.is_closed:
             logger.info("Закрытие HTTPX клиента..."); await http_client_custom.aclose()
        persistence = getattr(application, 'persistence', None)
        if persistence and isinstance(persistence, PostgresPersistence):
            logger.info("Закрытие соединений с базой данных...")
            persistence.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Приложение прервано пользователем (KeyboardInterrupt).")
    except Exception as e_top: logger.critical(f"Неперехваченная ошибка на верхнем уровне: {e_top}", exc_info=True)
