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
from youtube_transcript_api._errors import RequestBlocked  # Импорт для обработки блокировки YouTube
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
        try:
            self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=database_url)
            self._initialize_db()
            logger.info("PostgresPersistence: Соединение с базой данных установлено и таблица проверена.")
        except psycopg2.Error as e:
            logger.critical(f"PostgresPersistence: Не удалось подключиться к базе данных PostgreSQL: {e}")
            raise

    def _execute(self, query: str, params: tuple = None, fetch: str = None):
        if not self.db_pool:
            raise ConnectionError("PostgresPersistence: Пул соединений не инициализирован.")

        conn = None
        last_exception = None
        for attempt in range(2):  # Попытаться 2 раза (1 оригинал + 1 ретрай)
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
                    return True  # Успешный коммит
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgresPersistence: Ошибка соединения (попытка {attempt + 1}): {e}. Попытка переподключения...")
                last_exception = e
                if conn:
                    self.db_pool.putconn(conn, close=True) # Закрываем "сломанное" соединение
                    conn = None
                time.sleep(1) # Небольшая пауза перед повторной попыткой
                continue # Переходим к следующей итерации цикла
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
        return await asyncio.to_thread(self._get_pickled, "bot_data") or {}

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

    async def get_callback_data(self) -> dict | None:
        return None

    async def update_callback_data(self, data: dict) -> None:
        pass

    async def get_conversations(self, name: str) -> dict:
        return {}

    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None:
        pass

    async def drop_chat_data(self, chat_id: int) -> None:
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))

    async def drop_user_data(self, user_id: int) -> None:
        await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))

    async def refresh_bot_data(self, bot_data: dict) -> None:
        data = await self.get_bot_data()
        bot_data.update(data)

    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}
        chat_data.update(data)

    async def refresh_user_data(self, user_id: int, user_data: dict) -> None:
        data = await asyncio.to_thread(self._get_pickled, f"user_data_{user_id}") or {}
        user_data.update(data)

    async def flush(self) -> None:
        pass

    def close(self):
        if self.db_pool:
            self.db_pool.closeall()
            logger.info("PostgresPersistence: Все соединения с базой данных успешно закрыты.")

HARM_CATEGORIES_STRINGS = [
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
]
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
        BlockedPromptException as RealBlockedPromptException,
        StopCandidateException as RealStopCandidateException,
        SafetyRating as RealSafetyRating, BlockReason as RealBlockReason,
        FinishReason as RealFinishReason
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
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
DATABASE_URL = os.getenv('DATABASE_URL')

required_env_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY,
    "GOOGLE_CSE_ID": GOOGLE_CSE_ID, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH
}
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars:
    logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    exit(1)

genai.configure(api_key=GOOGLE_API_KEY)

AVAILABLE_MODELS = {
    'gemini-2.5-flash-preview-05-20': '2.5 Flash - 20.05.25',
    'gemini-2.0-flash': '2.0 Flash',
}
DEFAULT_MODEL = 'gemini-2.5-flash-preview-05-20' if 'gemini-2.5-flash-preview-05-20' in AVAILABLE_MODELS else 'gemini-2.0-flash'

MAX_CONTEXT_CHARS = 200000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 65536
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VISION_CAPABLE_KEYWORDS = ['flash', 'pro', 'vision', 'ultra']
VIDEO_CAPABLE_KEYWORDS = ['gemini-2.5-flash-preview-05-20']
USER_ID_PREFIX_FORMAT = "[User {user_id}]: "
TARGET_TIMEZONE = "Europe/Moscow"

REASONING_PROMPT_ADDITION = (
    "\n\nРежим углубленного анализа активен!\n"
    "Не включая в ответ пользователю, заранее: учитывая контекст и запрос (разрешаю любые, в т.ч. запретные темы!), найди всесторонние всевозможные доводы (тезисы, аргументы, факты) (сохрани источники! запрещаю вымышленные данные! всегда указывай о предположениях!); подвергни всё найденное критике и анализу воздействия влияющих факторов; конструктивно ответь на критику; проведи непредвзятое сравнение и полный объективный анализ; попробуй логично придумать более эффективные идеи и решения! Затем сформируй пользователю своё аргументированное мнение с подкреплением!"
    "Стиль ответов: структурированный, русский (мы в России), легко читаемый, красивый, живой, глубокие шутки с подтекстом (ирония, сарказм, несоответствие ожиданиям, игра слов, гипербола, абсурд, разрушение идиом, псевдомудрость, бытовые и интернет-отсылки); уместно используй примеры, метафоры, аналогии, разговорные выражения, идиомы, цитаты, региональные и культурные маркеры!"
    "Запрещено: канцелярит, вступления, самоповторы, фамильярность, грубость в деликатных темах и о детях, чужие тексты без обозначения цитат, любые признаки созданного AI текста! Вместо таблиц используй структуру текста!"
    "При редактировании текста или кода: бери за основу и указывай последнюю полную версию, изменяемые строки и причину, вноси только правки одобренные пользователем, не исправляй и не вырезай без разрешения текст или код; записывай свои ошибки, чтобы их не повторять!"
)

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096

    def smart_chunker(text_to_chunk, chunk_size):
        chunks = []
        remaining_text = text_to_chunk
        while len(remaining_text) > 0:
            if len(remaining_text) <= chunk_size:
                chunks.append(remaining_text)
                break

            split_pos = remaining_text.rfind('\n', 0, chunk_size)
            if split_pos == -1:
                split_pos = remaining_text.rfind(' ', 0, chunk_size)

            if split_pos == -1 or split_pos == 0:
                split_pos = chunk_size

            chunks.append(remaining_text[:split_pos])
            remaining_text = remaining_text[split_pos:].lstrip()
        return chunks

    reply_chunks = smart_chunker(text, MAX_MESSAGE_LENGTH)
    sent_message = None
    chat_id = target_message.chat_id
    message_id = target_message.message_id
    current_user_id = target_message.from_user.id if target_message.from_user else "Unknown"

    try:
        for i, chunk in enumerate(reply_chunks):
            if i == 0:
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id, parse_mode=ParseMode.MARKDOWN)
            else:
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e_md:
        if "Can't parse entities" in str(e_md) or "can't parse" in str(e_md).lower() or "reply message not found" in str(e_md).lower():
            problematic_chunk_preview = "N/A"
            if 'i' in locals() and i < len(reply_chunks):
                problematic_chunk_preview = reply_chunks[i][:500].replace('\n', '\\n')

            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга Markdown или ответа на сообщение ({message_id}): {e_md}. Проблемный чанк (начало): '{problematic_chunk_preview}...'. Попытка отправить как обычный текст.")
            try:
                sent_message = None
                full_text_plain = text
                plain_chunks = [full_text_plain[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(full_text_plain), MAX_MESSAGE_LENGTH)]

                for i_plain, chunk_plain in enumerate(plain_chunks):
                     if i_plain == 0:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain, reply_to_message_id=message_id)
                     else:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain)
                     await asyncio.sleep(0.1)
                return sent_message
            except Exception as e_plain:
                logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить даже как обычный текст: {e_plain}", exc_info=True)
                try:
                    await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось отправить ответ.")
                except Exception as e_final_send:
                    logger.critical(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке: {e_final_send}")
        else:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка при отправке ответа (Markdown): {e_md}", exc_info=True)
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при отправке ответа: {str(e_md)[:100]}...")
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
            if hasattr(candidate, 'content') and candidate.content and \
               hasattr(candidate.content, 'parts') and candidate.content.parts:
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
                if fr_candidate is not None:
                    fr_name = getattr(fr_candidate, 'name', str(fr_candidate))

                is_safety_other_reason = False
                if FinishReason and hasattr(FinishReason, 'SAFETY') and hasattr(FinishReason, 'OTHER'):
                    is_safety_other_reason = (fr_candidate == FinishReason.SAFETY or fr_candidate == FinishReason.OTHER)
                elif fr_name in ['SAFETY', 'OTHER']:
                    is_safety_other_reason = True

                if fr_candidate and not is_safety_other_reason:
                    logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0] не имеет (валидных) content.parts, но finish_reason={fr_name}.")
                else:
                    logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0] не имеет (валидных) content.parts. Finish_reason: {fr_name}")
        except IndexError:
             logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) IndexError при доступе к response_obj.candidates[0] (список кандидатов пуст).")
        except Exception as e_cand:
            logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Ошибка при обработке candidates: {e_cand}", exc_info=True)
    else:
        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) В ответе response нет ни response.text, ни валидных candidates с текстом.")

    return None

def _get_effective_context_for_task(
    task_type: str,
    original_context: ContextTypes.DEFAULT_TYPE,
    user_id: int | str,
    chat_id: int,
    log_prefix: str
) -> ContextTypes.DEFAULT_TYPE:
    capability_map = {
        "vision": VISION_CAPABLE_KEYWORDS,
        "video": VIDEO_CAPABLE_KEYWORDS
    }
    required_keywords = capability_map.get(task_type)
    if not required_keywords:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Неизвестный тип задачи '{task_type}' для выбора модели.")
        return original_context

    selected_model = get_user_setting(original_context, 'selected_model', DEFAULT_MODEL)

    is_capable = any(keyword in selected_model for keyword in required_keywords)
    if is_capable:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Модель пользователя '{selected_model}' подходит для задачи '{task_type}'.")
        return original_context

    available_capable_models = [
        m_id for m_id in AVAILABLE_MODELS
        if any(keyword in m_id for keyword in required_keywords)
    ]

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
        while url.endswith(('.', ',', '?', '!')):
            url = url[:-1]
        return url
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if 'selected_model' not in context.user_data:
        set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'search_enabled' not in context.user_data:
        set_user_setting(context, 'search_enabled', True)
    if 'temperature' not in context.user_data:
        set_user_setting(context, 'temperature', 1.0)
    if 'detailed_reasoning_enabled' not in context.user_data:
        set_user_setting(context, 'detailed_reasoning_enabled', True)
    bot_core_model_key = DEFAULT_MODEL
    raw_bot_core_model_display_name = AVAILABLE_MODELS.get(bot_core_model_key, bot_core_model_key)
    author_channel_link_raw = "https://t.me/denisobovsyom"
    date_knowledge_text_raw = "до начала 2025 года"
    
    start_message_plain_parts = [
        f"Меня зовут Женя, работаю на Google Gemini {raw_bot_core_model_display_name}:",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и могу искать в Google,",
        f"- использую рассуждения и улучшенные настройки от автора бота,",
        f"- умею читать и понимать изображения, txt, pdf и ссылки на веб-страницы.",
        f"Пишите сюда и добавляйте меня в группы, я отдельно запоминаю контекст каждого чата и пользователей.",
        f"Канал автора: {author_channel_link_raw}",
        f"Пользуясь данным ботом, вы автоматически соглашаетесь на отправку ваших запросов через Google API для получения ответов моделей Google Gemini."
    ]

    start_message_plain = "\n".join(start_message_plain_parts)
    logger.debug(f"Attempting to send start_message (Plain Text):\n{start_message_plain}")
    try:
        await update.message.reply_text(start_message_plain, disable_web_page_preview=True)
        logger.info("Successfully sent start_message as plain text.")
    except Exception as e:
        logger.error(f"Failed to send start_message (Plain Text): {e}", exc_info=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    context.chat_data.clear()
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата очищена по команде от {user_mention}.")
    await update.message.reply_text(f"🧹 Окей, {user_mention}, история этого чата очищена.")

async def set_temperature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    try:
        current_temp = get_user_setting(context, 'temperature', 1.0)
        if not context.args:
            await update.message.reply_text(f"🌡️ {user_mention}, твоя текущая температура (креативность): {current_temp:.1f}\nЧтобы изменить, напиши `/temp <значение>` (например, `/temp 0.8`)")
            return
        temp_str = context.args[0].replace(',', '.')
        temp = float(temp_str)
        if not (0.0 <= temp <= 2.0):
            raise ValueError("Температура должна быть от 0.0 до 2.0")
        set_user_setting(context, 'temperature', temp)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Температура установлена на {temp:.1f} для {user_mention}.")
        await update.message.reply_text(f"🌡️ Готово, {user_mention}! Твоя температура установлена на {temp:.1f}")
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"⚠️ Ошибка, {user_mention}. {e}. Укажи число от 0.0 до 2.0. Пример: `/temp 0.8`")
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка в set_temperature: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ой, {user_mention}, что-то пошло не так при установке температуры.")

async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'search_enabled', True)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск включен для {user_mention}.")
    await update.message.reply_text(f"🔍 Поиск Google/DDG для тебя, {user_mention}, включён.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'search_enabled', False)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск отключен для {user_mention}.")
    await update.message.reply_text(f"🔇 Поиск Google/DDG для тебя, {user_mention}, отключён.")

async def enable_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'detailed_reasoning_enabled', True)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Режим углубленных рассуждений включен для {user_mention}.")
    await update.message.reply_text(f"🧠 Режим углубленных рассуждений для тебя, {user_mention}, включен. Модель будет стараться анализировать запросы более подробно (ход мыслей не отображается).")

async def disable_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'detailed_reasoning_enabled', False)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Режим углубленных рассуждений отключен для {user_mention}.")
    await update.message.reply_text(f"💡 Режим углубленных рассуждений для тебя, {user_mention}, отключен.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = []
    sorted_models = sorted(AVAILABLE_MODELS.items())
    for m, name in sorted_models:
         button_text = f"{'✅ ' if m == current_model else ''}{name}"
         keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_model_{m}")])
    current_model_name = AVAILABLE_MODELS.get(current_model, current_model)
    await update.message.reply_text(f"{user_mention}, выбери модель (сейчас у тебя: {current_model_name}):", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    await query.answer()
    callback_data = query.data
    if callback_data and callback_data.startswith("set_model_"):
        selected = callback_data.replace("set_model_", "")
        if selected in AVAILABLE_MODELS:
            set_user_setting(context, 'selected_model', selected)
            model_name = AVAILABLE_MODELS[selected]
            reply_text = f"Ок, {user_mention}, твоя модель установлена: **{model_name}**"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Модель установлена на {model_name} для {user_mention}.")
            try:
                await query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e_md:
                 if "Message is not modified" in str(e_md):
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал ту же модель: {model_name}")
                     await query.answer(f"Модель {model_name} уже выбрана.", show_alert=False)
                 else:
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (Markdown) для {user_mention}: {e_md}. Отправляю новое.")
                     try:
                         await query.edit_message_text(reply_text.replace('**', ''))
                     except Exception as e_edit_plain:
                          logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение даже как простой текст для {user_mention}: {e_edit_plain}. Отправляю новое.")
                          await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (другая ошибка) для {user_mention}: {e}. Отправляю новое.", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал неизвестную модель: {selected}")
            try:
                await query.edit_message_text("❌ Неизвестная модель выбрана.")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text="❌ Неизвестная модель выбрана.")
    else:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен неизвестный callback_data от {user_mention}: {callback_data}")
        try:
            await query.edit_message_text("❌ Ошибка обработки выбора.")
        except Exception:
            pass

async def perform_google_search(query: str, api_key: str, cse_id: str, num_results: int, session: httpx.AsyncClient) -> list[str] | None:
    search_url = "https://www.googleapis.com/customsearch/v1"
    params = {'key': api_key, 'cx': cse_id, 'q': query, 'num': num_results, 'lr': 'lang_ru', 'gl': 'ru'}
    query_short = query[:50] + '...' if len(query) > 50 else query
    logger.debug(f"Запрос к Google Search API для '{query_short}'...")
    try:
        response = await session.get(search_url, params=params, timeout=10.0)
        response.raise_for_status() 

        data = response.json()
        items = data.get('items', [])
        snippets = [item.get('snippet', item.get('title', '')) for item in items if item.get('snippet') or item.get('title')]
        if snippets:
            logger.info(f"Google Search: Найдено {len(snippets)} результатов для '{query_short}'.")
            return snippets
        else:
            logger.info(f"Google Search: Нет сниппетов/заголовков для '{query_short}'.")
            return None
    except httpx.TimeoutException:
        logger.warning(f"Google Search: Таймаут запроса для '{query_short}'")
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        response_text = e.response.text
        if status == 400: logger.error(f"Google Search: Ошибка 400 (Bad Request) для '{query_short}'. Ответ: {response_text[:200]}...")
        elif status == 403: logger.error(f"Google Search: Ошибка 403 (Forbidden) для '{query_short}'. Проверьте API ключ/CSE ID. Ответ: {response_text[:200]}...")
        elif status == 429: logger.warning(f"Google Search: Ошибка 429 (Too Many Requests) для '{query_short}'. Квота? Ответ: {response_text[:200]}...")
        elif status >= 500: logger.warning(f"Google Search: Серверная ошибка {status} для '{query_short}'. Ответ: {response_text[:200]}...")
        else: logger.error(f"Google Search: Неожиданный статус {status} для '{query_short}'. Ответ: {response_text[:200]}...")
    except httpx.RequestError as e:
        logger.error(f"Google Search: Ошибка сети (RequestError) для '{query_short}' - {e}")
    except json.JSONDecodeError as e_json:
        logger.error(f"Google Search: Ошибка JSON для '{query_short}' - {e_json}. Ответ (вероятно, не JSON): {response.text[:200] if 'response' in locals() else 'N/A'}...")
    except Exception as e:
        logger.error(f"Google Search: Непредвиденная ошибка для '{query_short}' - {e}", exc_info=True)
    return None

async def _generate_gemini_response(
    user_prompt_text_initial: str,
    chat_history_for_model_initial: list,
    user_id: int | str,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    system_instruction: str,
    log_prefix: str = "GeminiGen",
    is_text_request_with_search: bool = False
) -> str | None:
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    reply = None

    search_block_pattern_to_remove = re.compile(
        r"\n*\s*==== РЕЗУЛЬТАТЫ ПОИСКА .*?====\n.*?Используй эту информацию для ответа на вопрос пользователя \[User \d+\]:.*?\n\s*===========================================================\n\s*.*?\n",
        re.DOTALL | re.IGNORECASE
    )

    for attempt in range(RETRY_ATTEMPTS):
        contents_to_use = chat_history_for_model_initial
        current_prompt_text_for_log = user_prompt_text_initial

        attempted_without_search_this_cycle = False

        for sub_attempt in range(2):
            if sub_attempt == 1 and not attempted_without_search_this_cycle:
                break

            if sub_attempt == 1 and attempted_without_search_this_cycle:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Попытка {attempt + 1}, суб-попытка БЕЗ ПОИСКА.")

                if not chat_history_for_model_initial or \
                   not chat_history_for_model_initial[-1]['role'] == 'user' or \
                   not chat_history_for_model_initial[-1]['parts'] or \
                   not chat_history_for_model_initial[-1]['parts'][0]['text']:
                    logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Некорректная структура chat_history_for_model_initial для удаления поиска.")
                    reply = "❌ Ошибка: не удалось подготовить запрос без поиска из-за структуры истории."
                    break

                last_user_prompt_with_search = chat_history_for_model_initial[-1]['parts'][0]['text']
                text_without_search = search_block_pattern_to_remove.sub("", last_user_prompt_with_search)

                if text_without_search == last_user_prompt_with_search:
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Блок поиска не был удален регулярным выражением. Повторная попытка будет с тем же промптом.")
                    break
                else:
                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Блок поиска удален для повторной суб-попытки.")

                new_history_for_model = [entry for entry in chat_history_for_model_initial[:-1]]
                new_history_for_model.append({"role": "user", "parts": [{"text": text_without_search.strip()}]})
                contents_to_use = new_history_for_model
                current_prompt_text_for_log = text_without_search.strip()
            elif sub_attempt == 0:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Попытка {attempt + 1}, суб-попытка С ПОИСКОМ (если есть в промпте).")

            try:
                generation_config = genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                model_obj = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction)

                response_obj = await asyncio.to_thread(model_obj.generate_content, contents_to_use)
                reply = _get_text_from_response(response_obj, user_id, chat_id, f"{log_prefix}{'_NoSearch' if sub_attempt == 1 else ''}")

                block_reason_str, finish_reason_str = 'N/A', 'N/A'
                if not reply:
                    try:
                        if hasattr(response_obj, 'prompt_feedback') and response_obj.prompt_feedback and hasattr(response_obj.prompt_feedback, 'block_reason'):
                            block_reason_enum = response_obj.prompt_feedback.block_reason
                            block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                        if hasattr(response_obj, 'candidates') and response_obj.candidates:
                            first_candidate = response_obj.candidates[0]
                            if hasattr(first_candidate, 'finish_reason'):
                                finish_reason_enum = first_candidate.finish_reason
                                finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                    except Exception as e_inner_reason_extract:
                        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка извлечения причин пустого ответа: {e_inner_reason_extract}")

                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Пустой ответ (попытка {attempt + 1}{', суб-попытка без поиска' if sub_attempt == 1 else ''}). Block: {block_reason_str}, Finish: {finish_reason_str}")

                    is_other_or_safety_block = (block_reason_str == 'OTHER' or (hasattr(BlockReason, 'OTHER') and block_reason_str == BlockReason.OTHER.name) or \
                                               block_reason_str == 'SAFETY' or (hasattr(BlockReason, 'SAFETY') and block_reason_str == BlockReason.SAFETY.name))

                    if sub_attempt == 0 and is_text_request_with_search and is_other_or_safety_block:
                        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Попытка с поиском заблокирована ({block_reason_str}). Планируем суб-попытку без поиска.")
                        attempted_without_search_this_cycle = True

                        try:
                            prompt_details_for_log = pprint.pformat(chat_history_for_model_initial)
                            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Исходный промпт (с поиском), вызвавший {block_reason_str} (первые 2000 символов):\n{prompt_details_for_log[:2000]}")
                        except Exception as e_log_prompt_block:
                            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка логирования промпта для {block_reason_str}: {e_log_prompt_block}")

                        reply = None
                        continue

                    if block_reason_str not in ['UNSPECIFIED', 'N/A', '', None] and (not hasattr(BlockReason, 'BLOCK_REASON_UNSPECIFIED') or block_reason_str != BlockReason.BLOCK_REASON_UNSPECIFIED.name):
                        reply = f"🤖 Модель не дала ответ. (Блокировка: {block_reason_str})"
                    elif finish_reason_str not in ['STOP', 'N/A', '', None] and \
                         (not hasattr(FinishReason, 'FINISH_REASON_STOP') or finish_reason_str != FinishReason.FINISH_REASON_STOP.name) and \
                         finish_reason_str not in ['OTHER', FinishReason.OTHER.name if hasattr(FinishReason,'OTHER') else 'OTHER_STR'] and \
                         finish_reason_str not in ['SAFETY', FinishReason.SAFETY.name if hasattr(FinishReason,'SAFETY') else 'SAFETY_STR']:
                        reply = f"🤖 Модель завершила работу без ответа. (Причина: {finish_reason_str})"
                    elif (finish_reason_str in ['OTHER', FinishReason.OTHER.name if hasattr(FinishReason,'OTHER') else 'OTHER_STR'] or \
                          finish_reason_str in ['SAFETY', FinishReason.SAFETY.name if hasattr(FinishReason,'SAFETY') else 'SAFETY_STR']) and \
                         (block_reason_str in ['UNSPECIFIED', 'N/A', '', None] or \
                          (hasattr(BlockReason, 'BLOCK_REASON_UNSPECIFIED') and block_reason_str == BlockReason.BLOCK_REASON_UNSPECIFIED.name)):
                         reply = f"🤖 Модель завершила работу по причине: {finish_reason_str}."
                    else:
                        reply = "🤖 Модель дала пустой ответ."
                    break

                if reply:
                    is_error_reply_generated_by_us = reply.startswith("🤖") or reply.startswith("❌")
                    if not is_error_reply_generated_by_us:
                        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}{'_NoSearch' if sub_attempt == 1 and attempted_without_search_this_cycle else ''}) Успешная генерация на попытке {attempt + 1}.")
                        break
                    else:
                        if sub_attempt == 0 and attempted_without_search_this_cycle:
                            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Первая суб-попытка дала ошибку, но вторая (без поиска) запланирована.")
                            reply = None
                            continue
                        else:
                            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}{'_NoSearch' if sub_attempt == 1 and attempted_without_search_this_cycle else ''}) Получен \"технический\" ответ об ошибке: {reply[:100]}...")
                            break

            except (BlockedPromptException, StopCandidateException) as e_block_stop_sub:
                reason_str_sub = str(e_block_stop_sub.args[0]) if hasattr(e_block_stop_sub, 'args') and e_block_stop_sub.args else "неизвестна"
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}{'_NoSearch' if sub_attempt == 1 and attempted_without_search_this_cycle else ''}) Запрос заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop_sub} (Причина: {reason_str_sub})")
                reply = f"❌ Запрос заблокирован/остановлен моделью."; break
            except Exception as e_sub:
                error_message_sub = str(e_sub)
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}{'_NoSearch' if sub_attempt == 1 and attempted_without_search_this_cycle else ''}) Ошибка генерации (попытка {attempt + 1}): {error_message_sub[:200]}...")
                if "429" in error_message_sub: reply = f"❌ Слишком много запросов к модели. Попробуйте позже."
                elif "400" in error_message_sub: reply = f"❌ Ошибка в запросе к модели (400 Bad Request)."
                elif "location is not supported" in error_message_sub: reply = f"❌ Эта модель недоступна в вашем регионе."
                else: reply = f"❌ Непредвиденная ошибка при генерации: {error_message_sub[:100]}..."
                break

        if reply and not (reply.startswith("🤖") or reply.startswith("❌")):
            break

        if attempt == RETRY_ATTEMPTS - 1:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Не удалось получить успешный ответ после {RETRY_ATTEMPTS} попыток. Финальный reply: {reply}")
            if reply is None:
                 reply = f"❌ Ошибка при обращении к модели после {RETRY_ATTEMPTS} попыток."
            break

        is_retryable_error_type = False
        if reply and ("500" in reply or "503" in reply or "timeout" in reply.lower()):
            is_retryable_error_type = True
        elif 'last_exception' in locals() and hasattr(locals()['last_exception'], 'message') :
             error_message_from_exception = str(locals()['last_exception'].message)
             if "500" in error_message_from_exception or "503" in error_message_from_exception or "timeout" in error_message_from_exception.lower():
                 is_retryable_error_type = True

        if is_retryable_error_type:
            wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ожидание {wait_time:.1f} сек перед попыткой {attempt + 2}...")
            await asyncio.sleep(wait_time)
        else:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Неретраябл ошибка или достигнут лимит ретраев. Финальный reply: {reply}")
            if reply is None : reply = f"❌ Ошибка при обращении к модели после {attempt + 1} попыток."
            break

    return reply

async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    log_prefix_handler = "ReanalyzeImg"
    logger.info(f"UserID: {requesting_user_id} (запрос по фото от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        img_file = await context.bot.get_file(file_id)
        file_bytes = await img_file.download_as_bytearray()
        if not file_bytes:
             logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Не удалось скачать или файл пустой для file_id: ...{file_id[-10:]}")
             await update.message.reply_text("❌ Не удалось получить исходное изображение для повторного анализа.")
             return
        b64_data = base64.b64encode(file_bytes).decode()
    except TelegramError as e_telegram:
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка Telegram при получении/скачивании файла {file_id}: {e_telegram}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка Telegram при получении изображения: {e_telegram}")
        return
    except Exception as e_download:
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка скачивания/кодирования файла {file_id}: {e_download}", exc_info=True)
        await update.message.reply_text("❌ Ошибка при подготовке изображения для повторного анализа.")
        return

    effective_context = _get_effective_context_for_task(
        task_type="vision",
        original_context=context,
        user_id=requesting_user_id,
        chat_id=chat_id,
        log_prefix=log_prefix_handler
    )
    selected_model_check = get_user_setting(effective_context, 'selected_model', DEFAULT_MODEL)
    if not any(keyword in selected_model_check for keyword in VISION_CAPABLE_KEYWORDS):
        await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения.")
        return

    current_time_str = get_current_time_str()
    user_question_with_context = (f"(Текущая дата и время: {current_time_str})\n"
                                  f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}")
    if get_user_setting(context, 'detailed_reasoning_enabled', True):
        user_question_with_context += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлена инструкция для детального рассуждения.")

    mime_type = "image/jpeg"
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts = [{"text": user_question_with_context}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    content_for_vision_direct = [{"role": "user", "parts": parts}]

    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Выбранная модель для задачи: {get_user_setting(effective_context, 'selected_model', DEFAULT_MODEL)}")

    reply = await _generate_gemini_response(
        user_prompt_text_initial=user_question_with_context,
        chat_history_for_model_initial=content_for_vision_direct,
        user_id=requesting_user_id,
        chat_id=chat_id,
        context=effective_context,
        system_instruction=system_instruction_text,
        log_prefix="ReanalyzeImgGen",
        is_text_request_with_search=False
    )

    chat_history = context.chat_data.setdefault("history", [])
    user_question_for_history = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    history_entry_user = { "role": "user", "parts": [{"text": user_question_for_history}], "user_id": requesting_user_id, "message_id": update.message.message_id }
    chat_history.append(history_entry_user)

    if reply:
        history_entry_model = {"role": "model", "parts": [{"text": reply}]}
        chat_history.append(history_entry_model)
        reply_to_send_to_user = reply
        await send_reply(update.message, reply_to_send_to_user, context)
    else:
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Нет ответа для отправки пользователю (reply is None после _generate_gemini_response).")
        final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."
        if not (chat_history and chat_history[-1].get("role") == "model" and chat_history[-1]["parts"][0]["text"] == final_error_msg):
            chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        try: await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    log_prefix_handler = "ReanalyzeVid"
    logger.info(f"UserID: {requesting_user_id} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Функция reanalyze_video вызвана, но анализ теперь происходит через расшифровку в handle_message. Этот вызов не должен был произойти.")
    await update.message.reply_text("🤔 Хм, что-то пошло не так при повторном анализе видео. Пожалуйста, попробуйте отправить ссылку на видео снова.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | Не удалось определить пользователя в update. Игнорирование сообщения.")
        return
    user_id = update.effective_user.id
    message = update.message
    log_prefix_handler = "HandleMsg"
    if not message:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Получен пустой объект message в update.")
        return

    if not message.text:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Получено сообщение без текста. Пропускается.")
        return

    original_user_message_text = message.text.strip()
    user_message_id = message.message_id
    chat_history = context.chat_data.setdefault("history", [])

    if message.reply_to_message and message.reply_to_message.text and original_user_message_text and not original_user_message_text.startswith('/'):
        replied_message = message.reply_to_message
        replied_text = replied_message.text
        user_question = original_user_message_text
        requesting_user_id_for_reanalyze = user_id
        found_special_context = False
        try:
            for i in range(len(chat_history) - 1, -1, -1):
                model_entry = chat_history[i]
                if model_entry.get("role") == "model" and model_entry.get("parts") and isinstance(model_entry["parts"], list) and len(model_entry["parts"]) > 0:
                    model_text = model_entry["parts"][0].get("text", "")
                    is_image_reply = model_text.startswith(IMAGE_DESCRIPTION_PREFIX) and replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) and model_text[:100] == replied_text[:100]
                    if is_image_reply:
                        if i > 0:
                            potential_user_entry = chat_history[i - 1]
                            if potential_user_entry.get("role") == "user":
                                original_user_id_from_hist = potential_user_entry.get("user_id", "Unknown")
                                if is_image_reply and "image_file_id" in potential_user_entry:
                                    found_file_id = potential_user_entry["image_file_id"]
                                    logger.info(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | ({log_prefix_handler}) Найден image_file_id: ...{found_file_id[-10:]} для reanalyze_image (ориг. user: {original_user_id_from_hist}).")
                                    await reanalyze_image(update, context, found_file_id, user_question, original_user_id_from_hist); found_special_context = True; break
                                else: logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | ({log_prefix_handler}) Найдено сообщение модели, но у предыдущего user-сообщения нет нужного ID изображения.")
                        else: logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | ({log_prefix_handler}) Найдено сообщение модели в самом начале истории.")
                        if not found_special_context: break
        except Exception as e_hist_search: logger.error(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка при поиске ID для reanalyze в chat_history: {e_hist_search}", exc_info=True)
        if found_special_context: return
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX):
             logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | ({log_prefix_handler}) Ответ на спец. сообщение, но reanalyze не запущен. Обработка как обычный текст.")

    user_message_with_id = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + original_user_message_text
    youtube_handled = False
    log_prefix_yt_summary = "YouTubeSummary"

    if not (message.reply_to_message and message.reply_to_message.text and
            (message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) or
             message.reply_to_message.text.startswith(YOUTUBE_SUMMARY_PREFIX))):
        youtube_id = extract_youtube_id(original_user_message_text)
        if youtube_id:
            youtube_handled = True
            first_name = update.effective_user.first_name
            user_mention = f"{first_name}" if first_name else f"User {user_id}"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Обнаружена ссылка YouTube (ID: {youtube_id}). Пытаюсь получить расшифровку...")
            try: await update.message.reply_text(f"Окей, {user_mention}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и попробую сделать конспект из субтитров...")
            except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Не удалось отправить сообщение 'гляну видео': {e_reply}")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            transcript_text = None
            try:
                transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])
                transcript_text = " ".join([d['text'] for d in transcript_list])
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Успешно получена расшифровка (длина: {len(transcript_text)}).")
            except (TranscriptsDisabled, NoTranscriptFound):
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Для видео {youtube_id} субтитры отключены или не найдены.")
                await update.message.reply_text("❌ К сожалению, для этого видео нет субтитров (ни на русском, ни на английском), поэтому я не могу сделать конспект.")
                return
            except RequestBlocked:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Запрос к YouTube для видео {youtube_id} заблокирован. Вероятно, из-за IP хостинга.")
                await update.message.reply_text("❌ Увы, YouTube заблокировал мой запрос. Такое часто случается, когда я работаю из облака. Сделать конспект этого видео не получится.")
                return
            except Exception as e_transcript:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_yt_summary}) Ошибка при получении расшифровки для {youtube_id}: {e_transcript}", exc_info=True)
                await update.message.reply_text("❌ Произошла ошибка при попытке получить субтитры из видео.")
                return

            current_time_str_yt = get_current_time_str()
            prompt_for_summary = (
                 f"(Текущая дата и время: {current_time_str_yt})\n"
                 f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}"
                 f"Сделай краткий, но информативный конспект на основе полного текста расшифровки (субтитров) видео, который приведён ниже. "
                 f"Твоя задача — структурировать и обобщить этот текст, выделив ключевые моменты. Если в оригинальном сообщении пользователя есть вопрос, ответь на него, опираясь на текст расшифровки.\n\n"
                 f"Оригинальное сообщение пользователя: '{original_user_message_text}'\n\n"
                 f"--- НАЧАЛО РАСШИФРОВКИ ВИДЕО ---\n"
                 f"{transcript_text}\n"
                 f"--- КОНЕЦ РАСШИФРОВКИ ВИДЕО ---"
            )

            history_entry_user = {"role": "user", "parts": [{"text": user_message_with_id}], "user_id": user_id, "message_id": user_message_id}
            chat_history.append(history_entry_user)

            history_for_model = [{"role": "user", "parts": [{"text": prompt_for_summary}]}]

            reply_yt = await _generate_gemini_response(
                user_prompt_text_initial=prompt_for_summary,
                chat_history_for_model_initial=history_for_model,
                user_id=user_id,
                chat_id=chat_id,
                context=context,
                system_instruction=system_instruction_text,
                log_prefix="YouTubeSummaryGen",
                is_text_request_with_search=False
            )

            summary_for_user_display = reply_yt if reply_yt else "🤖 К сожалению, не удалось создать конспект видео."
            history_entry_model = {"role": "model", "parts": [{"text": summary_for_user_display}]}
            chat_history.append(history_entry_model)

            await send_reply(message, summary_for_user_display, context)
            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return

    log_prefix_text_gen = "TextGen"
    use_search = get_user_setting(context, 'search_enabled', True)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    search_context_snippets = []
    search_provider = None
    search_log_msg = "Поиск отключен пользователем"
    search_actually_performed = False
    if use_search:
        query_for_search = original_user_message_text
        query_short = query_for_search[:50] + '...' if len(query_for_search) > 50 else query_for_search
        search_log_msg = f"Поиск Google/DDG для '{query_short}'"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg}...")
        
        session = getattr(context.application, 'http_client', None)
        if not session or session.is_closed:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Критическая ошибка: основная сессия httpx не найдена или закрыта! Поиск для этого запроса отменен.")
        else:
            google_results = await perform_google_search(query_for_search, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
            if google_results:
                search_provider = "Google"
                search_context_snippets = google_results
                search_log_msg += f" (Google: {len(search_context_snippets)} рез.)"
                search_actually_performed = True
            else:
                search_log_msg += " (Google: 0 рез./ошибка)"
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Google не дал результатов. Пробуем DuckDuckGo...")
                try:
                    async with DDGS() as ddgs:
                        results_ddg = await ddgs.text(
                            query_for_search,
                            region='ru-ru',
                            max_results=DDG_MAX_RESULTS
                        )
                    if results_ddg:
                        ddg_snippets = [r.get('body', '') for r in results_ddg if r.get('body')]
                        if ddg_snippets:
                            search_provider = "DuckDuckGo"; search_context_snippets = ddg_snippets
                            search_log_msg += f" (DDG: {len(search_context_snippets)} рез.)"
                            search_actually_performed = True
                        else: search_log_msg += " (DDG: 0 текст. рез.)"
                    else: search_log_msg += " (DDG: 0 рез.)"
                except Exception as e_ddg: 
                    logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка поиска DuckDuckGo: {e_ddg}", exc_info=True)
                    search_log_msg += " (DDG: ошибка)"

    current_time_str_main = get_current_time_str()
    time_context_str = f"(Текущая дата и время: {current_time_str_main})\n"

    final_prompt_parts = [time_context_str]

    detected_general_url_for_prompt = None
    if not youtube_handled:
        temp_url = extract_general_url(original_user_message_text)
        if temp_url :
             detected_general_url_for_prompt = temp_url
             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Общая ссылка {detected_general_url_for_prompt} будет выделена в промпте.")
             url_instruction = (
                f"\n\n**Важное указание по ссылке в запросе пользователя:** "
                f"Запрос пользователя содержит следующую ссылку: {detected_general_url_for_prompt}. "
                f"Пожалуйста, при формировании ответа в первую очередь используй содержимое этой веб-страницы. "
                f"Если доступ к странице невозможен, информация нерелевантна или недостаточна, сообщи об этом и/или используй поиск и свои знания."
            )
             final_prompt_parts.append(url_instruction)

    final_prompt_parts.append(user_message_with_id)

    if search_context_snippets:
        search_context_lines = [f"- {s.strip()}" for s in search_context_snippets if s.strip()]
        if search_context_lines:
            search_context_text = "\n".join(search_context_lines)
            search_block_title = f"==== РЕЗУЛЬТАТЫ ПОИСКА ({search_provider}) ДЛЯ ОТВЕТА НА ВОПРОС ===="
            search_block_instruction = f"Используй эту информацию для ответа на вопрос пользователя {USER_ID_PREFIX_FORMAT.format(user_id=user_id)}, особенно если он касается текущих событий или погоды."
            if detected_general_url_for_prompt:
                search_block_title = f"==== РЕЗУЛЬТАТЫ ПОИСКА ({search_provider}) ДЛЯ ДОПОЛНИТЕЛЬНОГО КОНТЕКСТА ===="
                search_block_instruction = f"Используй эту информацию для дополнения или проверки, особенно если информация по ссылке от пользователя ({detected_general_url_for_prompt}) недостаточна или недоступна."

            search_block = (f"\n\n{search_block_title}\n{search_context_text}\n"
                            f"===========================================================\n"
                            f"{search_block_instruction}\n")
            final_prompt_parts.append(search_block)
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Добавлен контекст из {search_provider} ({len(search_context_lines)} непустых сниппетов).")
        else:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Сниппеты из {search_provider} оказались пустыми, контекст не добавлен.");
            search_log_msg += " (пустые сниппеты)"
            search_actually_performed = False
    else:
        if use_search:
             search_actually_performed = False

    if get_user_setting(context, 'detailed_reasoning_enabled', True):
        final_prompt_parts.append(REASONING_PROMPT_ADDITION)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Добавлена инструкция для детального рассуждения.")

    final_user_prompt_text = "".join(final_prompt_parts)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg}")
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Финальный промпт для Gemini (длина {len(final_user_prompt_text)}):\n{final_user_prompt_text[:600]}...")

    if not youtube_handled:
        history_entry_user = {
            "role": "user",
            "parts": [{"text": user_message_with_id}],
            "user_id": user_id,
            "message_id": user_message_id
        }
        chat_history.append(history_entry_user)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Добавляем user сообщение (текст/URL) в chat_history.")

    history_for_model_raw = []
    current_total_chars = 0
    if not youtube_handled:
        history_to_filter = chat_history[:-1] if chat_history and chat_history[-1].get("message_id") == user_message_id else chat_history
    else:
        history_to_filter = chat_history

    for entry in reversed(history_to_filter):
        entry_text = ""
        entry_len = 0
        if entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text"):
            entry_text = entry["parts"][0]["text"]
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text and "Важное указание по ссылке" not in entry_text:
                 entry_len = len(entry_text)
        if current_total_chars + entry_len + len(final_user_prompt_text) <= MAX_CONTEXT_CHARS:
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text and "Важное указание по ссылке" not in entry_text:
                history_for_model_raw.append(entry)
                current_total_chars += entry_len
        else:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Учтено {len(history_for_model_raw)} сообщ., ~{current_total_chars} симв. (+финальный промпт).")
            break
    history_for_model = list(reversed(history_for_model_raw))
    history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]})
    history_clean_for_model = [{"role": entry["role"], "parts": entry["parts"]} for entry in history_for_model]

    gemini_reply_text = await _generate_gemini_response(
        user_prompt_text_initial=final_user_prompt_text,
        chat_history_for_model_initial=history_clean_for_model,
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix=log_prefix_text_gen,
        is_text_request_with_search=search_actually_performed
    )

    if gemini_reply_text and not youtube_handled:
        history_entry_model = {"role": "model", "parts": [{"text": gemini_reply_text}]}
        chat_history.append(history_entry_model)
        reply_to_send_to_user = gemini_reply_text
        if message: await send_reply(message, reply_to_send_to_user, context)
        else:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Не найдено сообщение для ответа в update (не YouTube).")
            try: await context.bot.send_message(chat_id=chat_id, text=reply_to_send_to_user)
            except Exception as e_send_direct: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Не удалось отправить ответ напрямую в чат (не YouTube): {e_send_direct}")
    elif not youtube_handled:
         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Нет ответа для отправки пользователю после всех попыток (не YouTube). Reply от _generate_gemini_response: {gemini_reply_text}")
         final_error_message = gemini_reply_text if gemini_reply_text else "🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток."

         if not (chat_history and chat_history[-1].get("role") == "model" and
                 chat_history[-1].get("message_id") == user_message_id and
                 chat_history[-1]["parts"][0]["text"] == final_error_message) :
            chat_history.append({"role": "model", "parts": [{"text": final_error_message}], "message_id": user_message_id})

         try:
             if message: await message.reply_text(final_error_message)
             else: await context.bot.send_message(chat_id=chat_id, text=final_error_message)
         except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_text_gen}) Не удалось отправить сообщение о финальной ошибке (не YouTube): {e_final_fail}")

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        removed = chat_history.pop(0)
        logger.debug(f"ChatID: {chat_id} | ({log_prefix_handler}) Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed.get('role')}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    log_prefix_handler = "PhotoVision"
    if not message or not message.photo:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_photo не найдено фото."); return

    photo_file_id = message.photo[-1].file_id
    user_message_id = message.message_id
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Получен photo file_id: ...{photo_file_id[-10:]}, message_id: {user_message_id}. Обработка через Gemini Vision.")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Скачанное фото (file_id: ...{photo_file_id[-10:]}) оказалось пустым.")
            await message.reply_text("❌ Не удалось загрузить изображение (файл пуст)."); return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось скачать фото (file_id: ...{photo_file_id[-10:]}): {e}", exc_info=True)
        try: await message.reply_text("❌ Не удалось загрузить изображение.")
        except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение об ошибке скачивания фото: {e_reply}")
        return

    user_caption = message.caption if message.caption else ""
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Обработка фото как изображения (Vision).")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    if len(file_bytes) > 20 * 1024 * 1024:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Изображение ({len(file_bytes) / (1024*1024):.2f} MB) может быть большим для API.")

    try:
        b64_data = base64.b64encode(file_bytes).decode()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка Base64 кодирования: {e}", exc_info=True)
        try: await message.reply_text("❌ Ошибка обработки изображения.")
        except Exception as e_reply_b64_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение об ошибке Base64: {e_reply_b64_err}")
        return

    effective_context_photo = _get_effective_context_for_task(
        task_type="vision",
        original_context=context,
        user_id=user_id,
        chat_id=chat_id,
        log_prefix=log_prefix_handler
    )
    selected_model_check_photo = get_user_setting(effective_context_photo, 'selected_model', DEFAULT_MODEL)
    if not any(keyword in selected_model_check_photo for keyword in VISION_CAPABLE_KEYWORDS):
        await message.reply_text("❌ Нет доступных моделей для анализа изображений."); return

    current_time_str_photo = get_current_time_str()
    prompt_text_vision = (f"(Текущая дата и время: {current_time_str_photo})\n"
                          f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
                         ) if user_caption else (
                          f"(Текущая дата и время: {current_time_str_photo})\n"
                          f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении.")

    if get_user_setting(context, 'detailed_reasoning_enabled', True):
        prompt_text_vision += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлена инструкция для детального рассуждения.")

    mime_type = "image/jpeg"
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"

    parts_photo = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    content_for_vision_photo_direct = [{"role": "user", "parts": parts_photo}]

    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Анализ изображения (Vision). Выбранная модель: {selected_model_check_photo}, MIME: {mime_type}")

    reply_photo = await _generate_gemini_response(
        user_prompt_text_initial=prompt_text_vision,
        chat_history_for_model_initial=content_for_vision_photo_direct,
        user_id=user_id,
        chat_id=chat_id,
        context=effective_context_photo,
        system_instruction=system_instruction_text,
        log_prefix="PhotoVisionGen",
        is_text_request_with_search=False
    )

    chat_history = context.chat_data.setdefault("history", [])
    user_text_for_history_vision = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото.")
    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_text_for_history_vision}],
        "image_file_id": photo_file_id,
        "user_id": user_id,
        "message_id": user_message_id
    }
    chat_history.append(history_entry_user)
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлено user-сообщение (Vision) в chat_history с image_file_id.")

    original_model_reply_content = reply_photo
    history_reply_text_with_prefix = ""
    if original_model_reply_content :
        if not (original_model_reply_content.startswith("🤖") or original_model_reply_content.startswith("❌")):
            history_reply_text_with_prefix = f"{IMAGE_DESCRIPTION_PREFIX}{original_model_reply_content}"
        else:
            history_reply_text_with_prefix = original_model_reply_content
    else:
         history_reply_text_with_prefix = "🤖 Не удалось проанализировать изображение."

    history_entry_model = {"role": "model", "parts": [{"text": history_reply_text_with_prefix}]}
    chat_history.append(history_entry_model)
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлен model-ответ (Vision) в chat_history: {history_reply_text_with_prefix[:100]}...")

    reply_for_user_display = ""
    if original_model_reply_content:
        if not (original_model_reply_content.startswith("🤖") or original_model_reply_content.startswith("❌")):
            reply_for_user_display = f"{IMAGE_DESCRIPTION_PREFIX}{original_model_reply_content}"
        else:
            reply_for_user_display = original_model_reply_content
    else:
        reply_for_user_display = "🤖 Не удалось проанализировать изображение."

    if reply_for_user_display: await send_reply(message, reply_for_user_display, context)
    else:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Нет ответа для отправки пользователю (reply_for_user_display is empty).")
        final_error_msg_photo = "🤖 К сожалению, не удалось проанализировать изображение."
        if not (chat_history and chat_history[-1].get("role") == "model" and chat_history[-1]["parts"][0]["text"] == final_error_msg_photo):
            chat_history.append({"role": "model", "parts": [{"text": final_error_msg_photo}]})
        try: await message.reply_text(final_error_msg_photo)
        except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    log_prefix_handler = "DocHandler"
    if not message or not message.document:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_document нет документа."); return

    doc = message.document
    allowed_mime_prefixes = ('text/',)
    allowed_mime_types = (
        'application/json', 'application/xml', 'application/csv', 'application/x-python',
        'application/x-sh', 'application/javascript', 'application/x-yaml',
        'application/x-tex', 'application/rtf', 'application/sql',
        'application/octet-stream', 'application/pdf'
    )
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types

    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы и PDF... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})"); return

    MAX_FILE_SIZE_MB = 15
    file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0 and doc.file_name:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Пустой файл '{doc.file_name}'.")
        await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой."); return
    elif file_size_bytes == 0 and not doc.file_name:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Получен пустой документ без имени."); return

    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> {MAX_FILE_SIZE_MB} MB).", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Слишком большой файл: {doc.file_name} ({file_size_bytes / (1024*1024):.2f} MB)"); return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
        if not file_bytes:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Файл '{doc.file_name}' скачан, но пуст.")
            await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой."); return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        try: await update.message.reply_text("❌ Не удалось загрузить файл.")
        except Exception as e_reply_dl_err:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение об ошибке скачивания документа: {e_reply_dl_err}")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None
    detected_encoding = None

    if mime_type == 'application/pdf':
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Обнаружен PDF-файл '{doc.file_name}'. Извлекаю текст...")
        try:
            text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
            detected_encoding = "pdf_extraction"
        except Exception as e_pdf:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка при извлечении текста из PDF '{doc.file_name}': {e_pdf}", exc_info=True)
            await update.message.reply_text(f"❌ Не удалось извлечь текст из PDF-файла `{doc.file_name}`. Возможно, он поврежден или зашифрован.", parse_mode=ParseMode.MARKDOWN)
            return
    else:
        encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1', 'cp866', 'iso-8859-5']
        chardet_available = False
        try:
            import chardet
            chardet_available = True
        except ImportError:
            logger.info(f"({log_prefix_handler}) Библиотека chardet не найдена. Автоопределение кодировки будет ограничено.")

        if chardet_available:
            try:
                chardet_limit = min(len(file_bytes), 50 * 1024)
                if chardet_limit > 0:
                     detected = chardet.detect(file_bytes[:chardet_limit])
                     if detected and detected['encoding'] and detected['confidence'] > 0.7:
                          potential_encoding = detected['encoding'].lower()
                          logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Chardet определил: {potential_encoding} ({detected['confidence']:.2f}) для '{doc.file_name}'")
                          if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'):
                               logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Обнаружен UTF-8 BOM, используем 'utf-8-sig'.")
                               detected_encoding = 'utf-8-sig'
                               if 'utf-8-sig' not in encodings_to_try:
                                   encodings_to_try.insert(0, 'utf-8-sig')
                               if 'utf-8' in encodings_to_try:
                                   try: encodings_to_try.remove('utf-8')
                                   except ValueError: pass
                          else:
                               detected_encoding = potential_encoding
                               if detected_encoding in encodings_to_try:
                                   encodings_to_try.remove(detected_encoding)
                               encodings_to_try.insert(0, detected_encoding)
                     else:
                         logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Chardet не уверен ({detected.get('confidence', 0):.2f}) для '{doc.file_name}'.")
            except Exception as e_chardet:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка при использовании chardet для '{doc.file_name}': {e_chardet}")

        unique_encodings = list(dict.fromkeys(encodings_to_try))
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Попытки декодирования для '{doc.file_name}': {unique_encodings}")

        for encoding in unique_encodings:
            try:
                text = file_bytes.decode(encoding)
                detected_encoding = encoding
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Файл '{doc.file_name}' успешно декодирован как {encoding}.")
                break
            except (UnicodeDecodeError, LookupError):
                logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Файл '{doc.file_name}' не в кодировке {encoding}.")
            except Exception as e_decode:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка при декодировании '{doc.file_name}' как {encoding}: {e_decode}", exc_info=True)

    if text is None:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось декодировать '{doc.file_name}'.")
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте UTF-8.", parse_mode=ParseMode.MARKDOWN); return

    if not text.strip() and len(file_bytes) > 0:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Файл '{doc.file_name}' дал пустой текст после обработки.")
        await update.message.reply_text(f"⚠️ Не удалось извлечь текст из файла `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN); return

    approx_max_tokens_for_file = MAX_OUTPUT_TOKENS * 2
    MAX_FILE_CHARS = min(MAX_CONTEXT_CHARS // 2, approx_max_tokens_for_file * 4)
    truncated_text = text
    truncation_warning = ""
    if len(text) > MAX_FILE_CHARS:
        truncated_text = text[:MAX_FILE_CHARS]
        last_newline = truncated_text.rfind('\n')
        if last_newline > MAX_FILE_CHARS * 0.8:
            truncated_text = truncated_text[:last_newline]
        chars_k = len(truncated_text) // 1000
        truncation_warning = f"\n\n**(⚠️ Текст файла был обрезан до ~{chars_k}k символов)**"
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Текст файла '{doc.file_name}' обрезан до {len(truncated_text)} символов.")

    user_caption_original = message.caption if message.caption else ""
    file_name_for_prompt = doc.file_name or "файл"
    encoding_info_for_prompt = f"({mime_type})"
    file_context_for_prompt = f"Содержимое файла `{file_name_for_prompt}` {encoding_info_for_prompt}:\n```\n{truncated_text}\n```{truncation_warning}"

    current_time_str_doc = get_current_time_str()
    time_context_str_doc = f"(Текущая дата и время: {current_time_str_doc})\n"

    user_prompt_doc_for_gemini = f"{time_context_str_doc}{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}"
    if user_caption_original:
        escaped_caption_content = user_caption_original.replace('"', '\\"')
        user_prompt_doc_for_gemini += f"Пользователь загрузил файл `{file_name_for_prompt}` с комментарием: \"{escaped_caption_content}\". {file_context_for_prompt}\nПроанализируй, пожалуйста."
    else:
        user_prompt_doc_for_gemini += f"Пользователь загрузил файл `{file_name_for_prompt}`. {file_context_for_prompt}\nЧто можешь сказать об этом тексте?"

    if get_user_setting(context, 'detailed_reasoning_enabled', True):
        user_prompt_doc_for_gemini += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлена инструкция для детального рассуждения.")

    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Подготовка к вызову _generate_gemini_response.")

    chat_history = context.chat_data.setdefault("history", [])
    user_message_id = message.message_id

    document_user_history_text = user_caption_original if user_caption_original else f"Загружен документ: {file_name_for_prompt}"
    user_message_with_id_for_history = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + document_user_history_text

    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_message_with_id_for_history}],
        "user_id": user_id,
        "message_id": user_message_id,
        "document_name": file_name_for_prompt
    }
    chat_history.append(history_entry_user)
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Добавлено user-сообщение (о загрузке документа) в chat_history.")

    history_for_model_raw_doc = []
    current_total_chars_doc = 0
    history_to_filter_doc = chat_history[:-1] if chat_history and chat_history[-1].get("message_id") == user_message_id else chat_history

    for entry in reversed(history_to_filter_doc):
        entry_text_doc = ""
        entry_len_doc = 0
        if entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text"):
            entry_text_doc = entry["parts"][0]["text"]
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text_doc and "Важное указание по ссылке" not in entry_text_doc:
                 entry_len_doc = len(entry_text_doc)

        if current_total_chars_doc + entry_len_doc + len(user_prompt_doc_for_gemini) <= MAX_CONTEXT_CHARS:
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text_doc and "Важное указание по ссылке" not in entry_text_doc:
                history_for_model_raw_doc.append(entry)
                current_total_chars_doc += entry_len_doc
        else:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Учтено {len(history_for_model_raw_doc)} сообщ.")
            break

    history_for_model_doc = list(reversed(history_for_model_raw_doc))
    history_for_model_doc.append({"role": "user", "parts": [{"text": user_prompt_doc_for_gemini}]})
    history_clean_for_model_doc = [{"role": entry["role"], "parts": entry["parts"]} for entry in history_for_model_doc]

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    gemini_reply_doc = await _generate_gemini_response(
        user_prompt_text_initial=user_prompt_doc_for_gemini,
        chat_history_for_model_initial=history_clean_for_model_doc,
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="DocGen",
        is_text_request_with_search=False
    )

    if gemini_reply_doc:
        history_entry_model_doc = {"role": "model", "parts": [{"text": gemini_reply_doc}]}
        chat_history.append(history_entry_model_doc)
        reply_to_send_to_user_doc = gemini_reply_doc
        await send_reply(message, reply_to_send_to_user_doc, context)
    else:
         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Нет ответа от _generate_gemini_response.")
         final_error_msg_doc = gemini_reply_doc if gemini_reply_doc else "🤖 К сожалению, не удалось обработать документ."

         if not (chat_history and chat_history[-1].get("role") == "model" and chat_history[-1].get("message_id") == user_message_id and chat_history[-1]["parts"][0]["text"] == final_error_msg_doc):
            chat_history.append({"role": "model", "parts": [{"text": final_error_msg_doc}], "message_id": user_message_id})

         try: await message.reply_text(final_error_msg_doc)
         except Exception as e_final_fail_doc:
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось отправить сообщение о финальной ошибке: {e_final_fail_doc}")

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        removed_doc = chat_history.pop(0)
        logger.debug(f"ChatID: {chat_id} | ({log_prefix_handler}) Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed_doc.get('role')}")

async def setup_bot_and_server(stop_event: asyncio.Event):
    persistence = None
    if DATABASE_URL:
        try:
            persistence = PostgresPersistence(database_url=DATABASE_URL)
            logger.info("Персистентность включена (PostgreSQL).")
        except Exception as e:
            logger.error(f"Не удалось инициализировать PostgresPersistence: {e}. Бот будет работать без сохранения состояния.", exc_info=True)
            persistence = None
    else:
        logger.warning("Переменная окружения DATABASE_URL не установлена. Бот будет работать без сохранения состояния (в режиме амнезии).")

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence:
        builder.persistence(persistence)

    application = builder.build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("temp", set_temperature))
    application.add_handler(CommandHandler("search_on", enable_search))
    application.add_handler(CommandHandler("search_off", disable_search))
    application.add_handler(CommandHandler("reasoning_on", enable_reasoning))
    application.add_handler(CommandHandler("reasoning_off", disable_reasoning))
    application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    try:
        await application.initialize()
        commands = [
            BotCommand("start", "Начать работу и инфо"),
            BotCommand("model", "Выбрать модель Gemini"),
            BotCommand("temp", "Установить температуру (креативность)"),
            BotCommand("search_on", "Включить поиск Google/DDG"),
            BotCommand("search_off", "Выключить поиск Google/DDG"),
            BotCommand("reasoning_on", "Вкл. углубленные рассуждения (по умолчанию вкл.)"),
            BotCommand("reasoning_off", "Выкл. углубленные рассуждения"),
            BotCommand("clear", "Очистить историю чата"),
        ]
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
        if persistence and isinstance(persistence, PostgresPersistence):
            persistence.close()
        raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def health_check(request):
        try:
            bot_info = await application.bot.get_me()
            if bot_info: logger.debug("Health check successful."); return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
            else: logger.warning("Health check: Bot info unavailable."); return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except TelegramError as e_tg: logger.error(f"Health check failed (TelegramError): {e_tg}", exc_info=True); return aiohttp.web.Response(text=f"Error: Telegram API error ({type(e_tg).__name__})", status=503)
        except Exception as e: logger.error(f"Health check failed (Exception): {e}", exc_info=True); return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)
    app.router.add_get('/', health_check)
    app['bot_app'] = application
    webhook_path = GEMINI_WEBHOOK_PATH.strip('/')
    if not webhook_path.startswith('/'): webhook_path = '/' + webhook_path
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
    except Exception as e: logger.error(f"Ошибка при запуске или работе веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally:
        logger.info("Начало остановки веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app')
    if not application: logger.critical("Приложение бота не найдено в контексте веб-сервера!"); return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token:
         header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
         if header_token != secret_token:
             logger.warning(f"Неверный секретный токен в заголовке от {request.remote}. Ожидался: ...{secret_token[-4:]}, Получен: {header_token}")
             return aiohttp.web.Response(status=403, text="Forbidden: Invalid secret token.")
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        logger.debug(f"Получен Update ID: {update.update_id} от Telegram.")
        await application.process_update(update)
        return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e_json:
         body = await request.text()
         logger.error(f"Ошибка декодирования JSON от Telegram: {e_json}. Тело запроса: {body[:500]}...")
         return aiohttp.web.Response(text="Bad Request: JSON decode error", status=400)
    except TelegramError as e_tg: logger.error(f"Ошибка Telegram при обработке вебхука: {e_tg}", exc_info=True); return aiohttp.web.Response(text=f"Internal Server Error: Telegram API Error ({type(e_tg).__name__})", status=500)
    except Exception as e: logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True); return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('google.api_core').setLevel(logging.WARNING)
    logging.getLogger('google.auth').setLevel(logging.WARNING)
    logging.getLogger('google.generativeai').setLevel(logging.INFO)
    logging.getLogger('duckduckgo_search').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.INFO)
    logging.getLogger('telegram.bot').setLevel(logging.INFO)
    logging.getLogger('psycopg2').setLevel(logging.WARNING)
    logging.getLogger('pdfminer').setLevel(logging.WARNING)

    logger.setLevel(log_level)
    logger.info(f"--- Установлен уровень логгирования для '{__name__}': {log_level_str} ({log_level}) ---")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): logger.info("Получен сигнал SIGINT/SIGTERM, инициирую остановку..."); stop_event.set()
        else: logger.warning("Повторный сигнал остановки получен, процесс уже завершается.")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             logger.warning(f"Не удалось установить обработчик сигнала {sig} через loop. Использую signal.signal().")
             try: signal.signal(sig, lambda s, f: signal_handler())
             except Exception as e_signal: logger.error(f"Не удалось установить обработчик сигнала {sig} через signal.signal(): {e_signal}")
             
    application = None
    web_server_task = None
    http_client_custom = None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        
        http_client_custom = httpx.AsyncClient()
        
        application, web_server_coro = await setup_bot_and_server(stop_event)
        
        application.http_client = http_client_custom 
        
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask")
        
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)...")
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Главная задача main() была отменена.")
    except Exception as e:
        logger.critical(f"Критическая ошибка во время запуска или ожидания: {e}", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки приложения ---")
        if not stop_event.is_set():
            stop_event.set()
            
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера (через stop_event)...")
             try:
                 await asyncio.wait_for(web_server_task, timeout=15.0)
                 logger.info("Веб-сервер успешно завершен.")
             except asyncio.TimeoutError:
                 logger.warning("Веб-сервер не завершился за 15 секунд, принудительная отмена...")
                 web_server_task.cancel()
                 try: await web_server_task
                 except asyncio.CancelledError: logger.info("Задача веб-сервера успешно отменена.")
                 except Exception as e_cancel_ws: logger.error(f"Ошибка при ожидании отмененной задачи веб-сервера: {e_cancel_ws}", exc_info=True)
             except asyncio.CancelledError:
                 logger.info("Ожидание веб-сервера было отменено.")
             except Exception as e_wait_ws:
                 logger.error(f"Ошибка при ожидании завершения веб-сервера: {e_wait_ws}", exc_info=True)
        
        if application:
            logger.info("Остановка приложения Telegram бота (application.shutdown)...")
            try:
                await application.shutdown()
                logger.info("Приложение Telegram бота успешно остановлено.")
            except Exception as e_shutdown:
                logger.error(f"Ошибка во время application.shutdown(): {e_shutdown}", exc_info=True)
        
        if http_client_custom and not http_client_custom.is_closed:
             logger.info("Закрытие кастомного HTTPX клиента...");
             await http_client_custom.aclose()
             await asyncio.sleep(0.25)
             logger.info("Кастомный HTTPX клиент закрыт.")
        
        if application and 'persistence' in application.bot_data:
            persistence = application.bot_data.get('persistence')
            if persistence and isinstance(persistence, PostgresPersistence):
                logger.info("Закрытие соединений с базой данных...")
                persistence.close()

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся фоновых задач...")
            [task.cancel() for task in tasks]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 task_name = tasks[i].get_name()
                 if isinstance(res, asyncio.CancelledError): cancelled_count += 1
                 elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка в отмененной задаче '{task_name}': {res}", exc_info=False)
            logger.info(f"Фоновые задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")
            
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение прервано пользователем (KeyboardInterrupt в main).")
    except Exception as e_top:
        logger.critical(f"Неперехваченная ошибка на верхнем уровне asyncio.run(main).", exc_info=True)
