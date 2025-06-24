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

# Замени весь класс PostgresPersistence на этот
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
        for attempt in range(3):  # Увеличим до 3 попыток
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
                    self.db_pool.putconn(conn, close=True) # Закрываем "сломанное" соединение
                if attempt < 2: # Перед последней попыткой пересоздаем весь пул
                    self._connect()
                time.sleep(1 + attempt) # Увеличиваем задержку
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
    
    # ... (остальные неиспользуемые, но обязательные методы BasePersistence) ...
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
    'gemini-2.5-flash': '2.5 Flash',
    'gemini-2.0-flash': '2.0 Flash',
}
DEFAULT_MODEL = 'gemini-2.5-flash' if 'gemini-2.5-flash' in AVAILABLE_MODELS else 'gemini-2.0-flash'

MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 65536
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VISION_CAPABLE_KEYWORDS = ['gemini-2.5-flash', 'pro', 'vision', 'ultra']
VIDEO_CAPABLE_KEYWORDS = ['gemini-2.5-flash']
USER_ID_PREFIX_FORMAT = "[User {user_id}; Name: {user_name}]: "
TARGET_TIMEZONE = "Europe/Moscow"

# Эта константа больше не нужна, так как вся логика перенесена в system_prompt.md.
# Оставляем ее пустой для обратной совместимости на случай, если где-то остался ее вызов.
REASONING_PROMPT_ADDITION = ""

def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

# <<< НАЧАЛО БЛОКА ИСПРАВЛЕНИЯ (ФИНАЛЬНАЯ ВЕРСИЯ) >>>
def prepare_html_for_telegram(text: str) -> str:
    """
    Финальная, надежная функция для подготовки текста к отправке в Telegram в режиме HTML.
    Сначала заменяет Markdown, потом экранирует ВСЕ спецсимволы,
    а затем "разэкранирует" только разрешенные HTML-теги.
    """
    # 1. Заменяем остатки Markdown, если модель их сгенерировала
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'```(.*?)```', r'<code>\1</code>', text, flags=re.DOTALL)
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
    
    # 2. Экранируем амперсанд в первую очередь, т.к. он используется для экранирования
    text = text.replace('&', '&')
    
    # 3. Экранируем угловые скобки
    text = text.replace('<', '<')
    text = text.replace('>', '>')
    
    # 4. Теперь, когда все "опасно", "разрешаем" наши теги обратно
    allowed_tags_map = {
        '<b>': '<b>', '</b>': '</b>',
        '<i>': '<i>', '</i>': '</i>',
        '<code>': '<code>', '</code>': '</code>',
    }
    for escaped, unescaped in allowed_tags_map.items():
        text = text.replace(escaped, unescaped)
        
    return text

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096

    def smart_chunker(text_to_chunk, chunk_size):
        # Эта внутренняя функция остается без изменений
        chunks = []
        remaining_text = text_to_chunk
        while len(remaining_text) > 0:
            if len(remaining_text) <= chunk_size:
                chunks.append(remaining_text)
                break
            split_pos = remaining_text.rfind('\n', 0, chunk_size)
            if split_pos == -1: split_pos = remaining_text.rfind(' ', 0, chunk_size)
            if split_pos == -1 or split_pos == 0: split_pos = chunk_size
            chunks.append(remaining_text[:split_pos])
            remaining_text = remaining_text[split_pos:].lstrip()
        return chunks

    # Применяем единую, надежную функцию очистки
    final_safe_text = prepare_html_for_telegram(text)

    reply_chunks = smart_chunker(final_safe_text, MAX_MESSAGE_LENGTH)
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
    except BadRequest as e_html:
        if "Can't parse entities" in str(e_html).lower():
            problematic_chunk_preview = "N/A"
            if 'i' in locals() and i < len(reply_chunks):
                problematic_chunk_preview = reply_chunks[i][:500].replace('\n', '\\n')
            logger.critical(f"UserID: {current_user_id}, ChatID: {chat_id} | ФИНАЛЬНАЯ ЗАЩИТА НЕ СРАБОТАЛА! Ошибка: {e_html}. ИСХОДНЫЙ ТЕКСТ: '{text[:500]}...'. Проблемный чанк: '{problematic_chunk_preview}...'. Отправляю как обычный текст.")
            
            try:
                sent_message = None
                plain_text = re.sub(r'<[^>]*>', '', text) # Удаляем все теги из исходного текста
                plain_chunks = [plain_text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(plain_text), MAX_MESSAGE_LENGTH)]
                for i_plain, chunk_plain in enumerate(plain_chunks):
                     if i_plain == 0: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain, reply_to_message_id=message_id)
                     else: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain)
                     await asyncio.sleep(0.1)
                return sent_message
            except Exception as e_plain:
                logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить даже как обычный текст: {e_plain}", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось отправить ответ.")
        else:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка при отправке ответа (HTML): {e_html}", exc_info=True)
    except Exception as e_other:
        logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Непредвиденная ошибка при отправке ответа: {e_other}", exc_info=True)
    return None
# <<< КОНЕЦ БЛОКА ИСПРАВЛЕНИЯ >>>

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
        "video": VIDEO_CAPABLE_KEYWORDS,
        "audio": ['gemini-2.5', 'pro', 'flash']
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
    # Установка значений по умолчанию, если они не существуют
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
        f"Меня зовут Женя, работаю на Google Gemini {raw_bot_core_model_display_name} с настройками от автора бота: {author_channel_link_raw}",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и поиском Google,",
        f"- умею понимать и обсуждать изображения, голосовые сообщения (!), файлы txt, pdf и веб-страницы,",
        f"- знаю ваше имя, помню историю чата. Пишите лично и добавляйте меня в группы.",
        f"(!) Пользуясь данным ботом, вы автоматически соглашаетесь на отправку ваших сообщений через Google (Search + Gemini API) для получения ответов."
    ]

    start_message_plain = "\n".join(start_message_plain_parts)
    logger.debug(f"Attempting to send start_message (Plain Text):\n{start_message_plain}")
    try:
        await update.message.reply_text(start_message_plain, disable_web_page_preview=True)
        logger.info("Successfully sent start_message as plain text.")
    except Exception as e:
        logger.error(f"Failed to send start_message (Plain Text): {e}", exc_info=True)

# Замени свою старую clear_history на эту
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    
    # 1. Очищаем состояние в оперативной памяти
    context.chat_data.clear()
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата в памяти очищена по команде от {user_mention}.")
    
    # 2. Принудительно удаляем данные из персистентного хранилища
    if context.application.persistence:
        await context.application.persistence.drop_chat_data(chat_id)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата в базе данных удалена.")

    await update.message.reply_text(f"🧹 Окей, {user_mention}, история этого чата очищена.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
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

# <<< НАЧАЛО: НОВЫЙ БЛОК ДЛЯ КОМАНДЫ ТРАНСКРИПЦИИ >>>

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /transcribe, использованную в ответ на голосовое сообщение,
    и возвращает его дословную текстовую расшифровку.
    """
    message = update.message
    replied_message = message.reply_to_message
    
    # 1. Проверяем, что команда использована в ответ на сообщение
    if not replied_message:
        await message.reply_text("ℹ️ Пожалуйста, используйте эту команду, отвечая на голосовое сообщение, которое нужно превратить в текст.")
        return

    # 2. Проверяем, что ответ был именно на голосовое сообщение
    if not replied_message.voice:
        await message.reply_text("❌ Эта команда работает только с голосовыми сообщениями.")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    log_prefix = "TranscribeCmd"

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 3. Скачиваем аудиофайл из сообщения, на которое ответили
    try:
        voice_file = await replied_message.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания голоса: {e}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке исходного голосового сообщения.")
        return

    # 4. Отправляем в Gemini с той же логикой, что и в handle_voice
    transcription_prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки, без каких-либо добавлений или комментариев."
    effective_context = _get_effective_context_for_task("audio", context, user_id, chat_id, log_prefix)
    model_id = get_user_setting(effective_context, 'selected_model', DEFAULT_MODEL)
    model_obj = genai.GenerativeModel(model_id)

    try:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Запрос на чистую транскрипцию в {model_id}.")
        response = await asyncio.to_thread(
            model_obj.generate_content,
            [transcription_prompt, {"mime_type": "audio/ogg", "data": bytes(file_bytes)}]
        )
        transcribed_text = _get_text_from_response(response, user_id, chat_id, log_prefix)

        if not transcribed_text:
            await message.reply_text("🤖 Не удалось распознать речь в указанном сообщении.")
            return
            
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка при транскрипции: {e}", exc_info=True)
        await message.reply_text(f"❌ Ошибка сервиса распознавания речи: {str(e)[:100]}")
        return

    # 5. Отправляем результат пользователю
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Транскрипция успешна.")
    await message.reply_text(f"📝 *Транскрипт:*\n\n{transcribed_text}", parse_mode=ParseMode.HMTL)

# <<< КОНЕЦ: НОВЫЙ БЛОК ДЛЯ КОМАНДЫ ТРАНСКРИПЦИИ >>>

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
                await query.edit_message_text(reply_text, parse_mode=ParseMode.HTML)
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
                          await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (другая ошибка) для {user_mention}: {e}. Отправляю новое.", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML)
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
    # Температура теперь жестко задана и не берется из настроек пользователя
    temperature = 1.0
    reply = None

    search_block_pattern_to_remove = re.compile(
        r"\n*\s*==== РЕЗУЛЬТАТЫ ПОИСКА .*?====\n.*?Используй эту информацию для ответа на вопрос пользователя \[User \d+; Name: .*?\]:.*?\n\s*===========================================================\n\s*.*?\n",
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

async def reanalyze_image_from_id(file_id: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Асинхронно скачивает изображение и выполняет его повторный анализ с полным контекстом."""
    user_id = context.user_data.get('id', 'Unknown')
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeImgV3"
    
    try:
        img_file = await context.bot.get_file(file_id)
        file_bytes = await img_file.download_as_bytearray()
        if not file_bytes:
            return "❌ Не удалось получить исходное изображение (файл пуст)."
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания {file_id}: {e}")
        return f"❌ Ошибка при получении изображения: {e}"

    b64_data = base64.b64encode(file_bytes).decode()
    effective_context = _get_effective_context_for_task("vision", context, user_id, chat_id, log_prefix)
    user_name = context.user_data.get('first_name', 'Пользователь')

    prompt_text = (
        f"Это уточняющий вопрос к изображению, которое ты уже видела.\n"
        f"ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ:\n---\n{old_bot_response}\n---\n\n"
        f"НОВЫЙ ВОПРОС ОТ {user_name}: \"{user_question}\"\n\n"
        f"ЗАДАЧА: Внимательно посмотри на изображение ещё раз и ответь на новый вопрос. Будь краткой и точной."
    )
    parts = [{"text": prompt_text}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]
    
    return await _generate_gemini_response(
        user_prompt_text_initial=prompt_text,
        chat_history_for_model_initial=[{"role": "user", "parts": parts}],
        user_id=user_id, chat_id=chat_id, context=effective_context,
        system_instruction=system_instruction_text, log_prefix=log_prefix
    )

async def reanalyze_document_from_id(file_id: str, old_bot_response: str, user_question: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Асинхронно скачивает документ и выполняет его повторный анализ."""
    user_id = context.user_data.get('id', 'Unknown')
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "ReanalyzeDocV1"
    
    try:
        doc_file = await context.bot.get_file(file_id)
        file_bytes = await doc_file.download_as_bytearray()
        # Тут должна быть ваша логика декодирования, как в handle_document
        text = file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания/чтения {file_id}: {e}")
        return f"❌ Ошибка при получении документа: {e}"

    user_name = context.user_data.get('first_name', 'Пользователь')
    prompt_text = (
        f"Это уточняющий вопрос к документу, который ты уже читала.\n"
        f"ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ:\n---\n{old_bot_response}\n---\n\n"
        f"СОДЕРЖИМОЕ ДОКУМЕНТА (для справки):\n---\n{text[:5000]}\n---\n\n"
        f"НОВЫЙ ВОПРОС ОТ {user_name}: \"{user_question}\"\n\n"
        f"ЗАДАЧА: Внимательно перечитай документ и ответь на новый вопрос."
    )
    
    return await _generate_gemini_response(
        user_prompt_text_initial=prompt_text,
        chat_history_for_model_initial=[{"role": "user", "parts": [{"text": prompt_text}]}],
        user_id=user_id, chat_id=chat_id, context=context,
        system_instruction=system_instruction_text, log_prefix=log_prefix
    )

def build_context_for_model(chat_history: list) -> list:
    """
    Собирает и фильтрует историю чата для передачи модели.
    Обеспечивает, чтобы история не превышала лимиты и содержала только релевантные части.
    """
    clean_history = []
    # Собираем только 'чистые' сообщения
    for entry in chat_history:
        if entry.get("role") in ("user", "model") and isinstance(entry.get("parts"), list):
            # Копируем только нужные поля, отсекая служебные
            clean_entry = {"role": entry["role"], "parts": []}
            for part in entry["parts"]:
                if isinstance(part, dict) and "text" in part:
                    clean_entry["parts"].append({"text": part["text"]})
            if clean_entry["parts"]:
                clean_history.append(clean_entry)

    # Обрезаем по длине, начиная с самых свежих сообщений
    history_for_model = []
    current_chars = 0
    for entry in reversed(clean_history):
        entry_text = "".join(p.get("text", "") for p in entry.get("parts", []))
        # Простая проверка, чтобы не обрезать посреди сообщения
        if current_chars + len(entry_text) > MAX_CONTEXT_CHARS:
            logger.info(f"Обрезка истории по символам. Учтено {len(history_for_model)} сообщений из {len(clean_history)}.")
            break
        history_for_model.append(entry)
        current_chars += len(entry_text)
    
    # Возвращаем в правильном хронологическом порядке
    history_for_model.reverse()
    return history_for_model

# <<< НАЧАЛО БЛОКА ИСПРАВЛЕНИЯ >>>

async def perform_google_search(query: str, api_key: str, cse_id: str, num_results: int, session: httpx.AsyncClient) -> list[str] | None:
    # Эта функция остается без изменений, она работает хорошо.
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
    except httpx.HTTPStatusError as e:
        logger.error(f"Google Search: Ошибка HTTP {e.response.status_code} для '{query_short}'. Ответ: {e.response.text[:200]}...")
    except Exception as e:
        logger.error(f"Google Search: Непредвиденная ошибка для '{query_short}' - {e}", exc_info=True)
    return None

async def perform_ddg_search(query: str, num_results: int) -> list[str] | None:
    """Выполняет поиск через DuckDuckGo как запасной вариант."""
    query_short = query[:50] + '...' if len(query) > 50 else query
    logger.info(f"Запрос к DDG Search API для '{query_short}'...")
    try:
        # DDGS().text() является синхронной, поэтому запускаем в отдельном потоке
        results = await asyncio.to_thread(DDGS().text, keywords=query, region='ru-ru', max_results=num_results)
        if results:
            snippets = [r['body'] for r in results]
            logger.info(f"DDG Search: Найдено {len(snippets)} результатов для '{query_short}'.")
            return snippets
        logger.info(f"DDG Search: Не найдено результатов для '{query_short}'.")
        return None
    except Exception as e:
        logger.error(f"DDG Search: Непредвиденная ошибка для '{query_short}' - {e}", exc_info=True)
        return None

async def perform_web_search(query: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[str | None, str | None]:
    """
    Универсальная функция поиска. Сначала пытается через Google, при неудаче - через DDG.
    Возвращает кортеж (строка_с_результатами, источник_поиска).
    """
    session = getattr(context.application, 'http_client', None)
    if session and not session.is_closed:
        google_results = await perform_google_search(query, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results:
            search_str = "\n".join(f"- {s.strip()}" for s in google_results)
            return search_str, "Google"
            
    logger.warning(f"Поиск Google не дал результатов для '{query[:50]}...'. Переключаюсь на DuckDuckGo.")
    ddg_results = await perform_ddg_search(query, DDG_MAX_RESULTS)
    if ddg_results:
        search_str = "\n".join(f"- {s.strip()}" for s in ddg_results)
        return search_str, "DuckDuckGo"
        
    return None, None

async def process_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_process: str):
    """
    Основная логика обработки текстового запроса. Выполняет поиск, вызывает модель и отправляет ответ.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    message = update.message
    user_id = user.id
    
    chat_history = context.chat_data.setdefault("history", [])
    user_name = user.first_name if user.first_name else "Пользователь"
    user_message_for_history = USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name) + text_to_process

    # --- Универсальный Поиск ---
    search_context_str = ""
    search_actually_performed = False
    search_results, search_source = await perform_web_search(text_to_process, context)
    if search_results:
        search_context_str = f"\n\n==== РЕЗУЛЬТАТЫ ПОИСКА ({search_source}) ====\n{search_results}"
        search_actually_performed = True
            
    # --- Формирование промпта и вызов модели ---
    current_time_str = get_current_time_str()
    time_prefix_for_prompt = f"(Текущая дата и время: {current_time_str})\n"
    final_user_prompt = f"{time_prefix_for_prompt}{user_message_for_history}{search_context_str}"

    history_entry_user = {
        "role": "user", "parts": [{"text": user_message_for_history}],
        "user_id": user_id, "message_id": message.message_id
    }
    if message.voice:
        history_entry_user["content_type"] = "voice"
        history_entry_user["content_id"] = message.voice.file_id
    chat_history.append(history_entry_user)

    context_for_model = build_context_for_model(chat_history)
    if context_for_model and context_for_model[-1]["role"] == "user":
        context_for_model[-1]["parts"][0]["text"] = final_user_prompt

    gemini_reply_text = await _generate_gemini_response(
        user_prompt_text_initial=final_user_prompt,
        chat_history_for_model_initial=context_for_model,
        user_id=user_id, chat_id=chat_id, context=context, system_instruction=system_instruction_text,
        is_text_request_with_search=search_actually_performed
    )
    
    sent_message = await send_reply(message, gemini_reply_text, context)
    
    chat_history.append({
        "role": "model", "parts": [{"text": gemini_reply_text or "🤖 Не удалось получить ответ."}],
        "bot_message_id": sent_message.message_id if sent_message else None
    })

    if context.application.persistence:
        await context.application.persistence.update_chat_data(chat_id, context.chat_data)

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает голос: расшифровывает и передает текст в process_text_query.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user: return
    message = update.message
    if not message or not message.voice: return
    
    user_id = user.id
    log_prefix = "VoiceHandler"
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        voice_file = await message.voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка скачивания голоса: {e}", exc_info=True)
        await message.reply_text("❌ Ошибка при загрузке вашего голосового сообщения.")
        return

    transcription_prompt = "Расшифруй это аудиосообщение и верни только текст расшифровки, без каких-либо добавлений или комментариев."
    effective_context = _get_effective_context_for_task("audio", context, user_id, chat_id, log_prefix)
    model_id = get_user_setting(effective_context, 'selected_model', DEFAULT_MODEL)
    model_obj = genai.GenerativeModel(model_id)

    try:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Отправка аудио на расшифровку в {model_id}.")
        response = await asyncio.to_thread(model_obj.generate_content, [transcription_prompt, {"mime_type": "audio/ogg", "data": bytes(file_bytes)}])
        transcribed_text = _get_text_from_response(response, user_id, chat_id, log_prefix)
        if not transcribed_text:
            await message.reply_text("🤖 Не удалось распознать речь. Попробуйте еще раз.")
            return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка при расшифровке: {e}", exc_info=True)
        await message.reply_text(f"❌ Ошибка сервиса распознавания речи: {str(e)[:100]}")
        return

    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Голос расшифрован -> '{transcribed_text}'. Передача в обработчик.")
    await process_text_query(update, context, transcribed_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает все входящие текстовые сообщения, ссылки YouTube и уточняющие вопросы (ответы на сообщения бота).
    """
    message = update.message
    if not message or (not message.text and not message.caption): return
    
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    original_text = (message.text or message.caption).strip()
    chat_history = context.chat_data.setdefault("history", [])
    
    context.user_data['id'] = user_id
    context.user_data['first_name'] = user.first_name
    context.chat_data['id'] = chat_id
    
    # --- 1. Обработка уточняющих вопросов (re-analyze) ---
    if message.reply_to_message and not original_text.startswith('/'):
        # Логика re-analyze остается без изменений
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
                        if content_type == "image": new_reply_text = await reanalyze_image_from_id(content_id, old_bot_response, original_text, context)
                        elif content_type == "document": new_reply_text = await reanalyze_document_from_id(content_id, old_bot_response, original_text, context)
                        if new_reply_text:
                            user_name = user.first_name or "Пользователь"
                            chat_history.append({"role": "user", "parts": [{"text": USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name) + original_text}], "user_id": user_id, "message_id": message.message_id})
                            sent_message = await send_reply(message, new_reply_text, context)
                            chat_history.append({"role": "model", "parts": [{"text": new_reply_text}], "bot_message_id": sent_message.message_id if sent_message else None})
                            if context.application.persistence: await context.application.persistence.update_chat_data(chat_id, context.chat_data)
                            return
                break
    
    # --- 2. Обработка ссылок YouTube ---
    youtube_id = extract_youtube_id(original_text)
    if youtube_id:
        log_prefix = "YouTubeHandler"
        user_name = user.first_name or "Пользователь"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Обнаружена ссылка YouTube (ID: {youtube_id}).")
        await message.reply_text(f"Окей, {user_name}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        
        transcript_text = None
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, youtube_id, languages=['ru', 'en'])
            transcript_text = " ".join([d['text'] for d in transcript_list])
        except (TranscriptsDisabled, NoTranscriptFound):
            await message.reply_text("❌ К сожалению, для этого видео нет субтитров, поэтому я не могу сделать конспект.")
            return
        except RequestBlocked:
            await message.reply_text("❌ Ой, похоже, YouTube временно заблокировал мои запросы. Попробуйте позже.")
            return
        except Exception as e_transcript:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка при получении расшифровки для {youtube_id}: {e_transcript}", exc_info=True)
            await message.reply_text("❌ Произошла ошибка при попытке получить субтитры из видео.")
            return

        summary_prompt = (
            f"Проанализируй следующую расшифровку видео с YouTube и сделай из нее информативный конспект. "
            f"Оригинальный запрос пользователя был: '{original_text}'. Ответь на русском языке.\n\n"
            f"--- НАЧАЛО РАСШИФРОВКИ ---\n{transcript_text[:20000]}\n--- КОНЕЦ РАСШИФРОВКИ ---"
        )
        # Обрезаем до 20к символов на всякий случай
        
        # Передаем задачу на обработку основной функции
        await process_text_query(update, context, summary_prompt)
        return

    # --- 3. Обработка обычного текстового сообщения ---
    await process_text_query(update, context, original_text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = user.id
    message = update.message
    log_prefix_handler = "PhotoVision"

    if not message or not message.photo:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_photo не найдено фото."); return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    photo_file_id = message.photo[-1].file_id
    
    try:
        photo_file = await context.bot.get_file(photo_file_id)
        file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes:
            await message.reply_text("❌ Не удалось загрузить изображение (файл пуст)."); return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото по file_id: {photo_file_id}: {e}", exc_info=True)
        await message.reply_text("❌ Не удалось загрузить изображение."); return

    user_caption = message.caption or ""
    
    effective_context_photo = _get_effective_context_for_task("vision", context, user_id, chat_id, log_prefix_handler)
    user_name = user.first_name if user.first_name else "Пользователь"
    current_time_str_photo = get_current_time_str()
    prompt_text_vision = (f"(Текущая дата и время: {current_time_str_photo})\n"
                          f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name)}Опиши это изображение. Подпись от пользователя: \"{user_caption}\"")
    prompt_text_vision += REASONING_PROMPT_ADDITION
    
    try:
        b64_data = base64.b64encode(file_bytes).decode()
    except Exception:
        await message.reply_text("❌ Ошибка обработки изображения."); return

    mime_type = "image/jpeg" if file_bytes.startswith(b'\xff\xd8\xff') else "image/png"
    parts_photo = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    
    reply_photo = await _generate_gemini_response(
        user_prompt_text_initial=prompt_text_vision,
        chat_history_for_model_initial=[{"role": "user", "parts": parts_photo}],
        user_id=user_id, chat_id=chat_id, context=effective_context_photo,
        system_instruction=system_instruction_text, log_prefix="PhotoVisionGen"
    )

    chat_history = context.chat_data.setdefault("history", [])
    
    user_text_for_history = USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name) + (user_caption or "Пользователь прислал фото.")
    
    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_text_for_history}],
        "content_type": "image",
        "content_id": photo_file_id,
        "user_id": user_id,
        "message_id": message.message_id
    }
    chat_history.append(history_entry_user)

    reply_for_user_display = f"{IMAGE_DESCRIPTION_PREFIX}{reply_photo}" if reply_photo and not reply_photo.startswith(("🤖", "❌")) else (reply_photo or "🤖 Не удалось проанализировать изображение.")
    
    sent_message = await send_reply(message, reply_for_user_display, context)

    history_entry_model = {
        "role": "model",
        "parts": [{"text": reply_for_user_display}],
        "bot_message_id": sent_message.message_id if sent_message else None
    }
    chat_history.append(history_entry_model)
    
    if context.application.persistence:
        await context.application.persistence.update_chat_data(chat_id, context.chat_data)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата (фото) принудительно сохранена.")

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = user.id
    message = update.message
    log_prefix_handler = "DocHandler"
    if not message or not message.document:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) В handle_document нет документа."); return

    doc = message.document
    allowed_mime_prefixes = ('text/',)
    allowed_mime_types = ('application/pdf', 'application/json', 'application/xml', 'application/csv')
    mime_type = doc.mime_type or "application/octet-stream"
    if not (any(mime_type.startswith(p) for p in allowed_mime_prefixes) or mime_type in allowed_mime_types):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы и PDF... Ваш тип: `{mime_type}`", parse_mode=ParseMode.HTML)
        return

    if doc.file_size > 15 * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> 15 MB).")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        await message.reply_text("❌ Не удалось загрузить файл.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None
    if mime_type == 'application/pdf':
        try:
            text = await asyncio.to_thread(extract_text, io.BytesIO(file_bytes))
        except Exception as e_pdf:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix_handler}) Ошибка извлечения из PDF '{doc.file_name}': {e_pdf}", exc_info=True)
            await update.message.reply_text(f"❌ Не удалось извлечь текст из PDF-файла `{doc.file_name}`.")
            return
    else:
        try:
            text = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode('cp1251')
            except UnicodeDecodeError:
                await update.message.reply_text(f"❌ Не удалось прочитать текстовый файл `{doc.file_name}`. Попробуйте кодировку UTF-8 или CP1251.")
                return
    
    if text is None:
        await update.message.reply_text(f"❌ Не удалось извлечь текст из файла `{doc.file_name}`.")
        return

    user_caption_original = message.caption or ""
    current_time_str_doc = get_current_time_str()
    time_prefix_for_prompt_doc = f"(Текущая дата и время: {current_time_str_doc})\n"

    file_context_for_prompt = f"Содержимое файла `{doc.file_name or 'файл'}`:\n```\n{text[:10000]}\n```"

    user_name = user.first_name if user.first_name else "Пользователь"
    user_prompt_doc_for_gemini = (f"{time_prefix_for_prompt_doc}"
                                  f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name)}"
                                  f"Проанализируй текст из файла. Мой комментарий: \"{user_caption_original}\".\n{file_context_for_prompt}")
    user_prompt_doc_for_gemini += REASONING_PROMPT_ADDITION

    gemini_reply_doc = await _generate_gemini_response(
        user_prompt_text_initial=user_prompt_doc_for_gemini,
        chat_history_for_model_initial=[{"role": "user", "parts": [{"text": user_prompt_doc_for_gemini}]}],
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="DocGen"
    )

    chat_history = context.chat_data.setdefault("history", [])

    doc_caption_for_history = user_caption_original or f"Загружен документ: {doc.file_name}"
    user_message_with_id_for_history = USER_ID_PREFIX_FORMAT.format(user_id=user_id, user_name=user_name) + doc_caption_for_history

    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_message_with_id_for_history}],
        "content_type": "document",
        "content_id": doc.file_id,
        "user_id": user_id,
        "message_id": message.message_id,
    }
    chat_history.append(history_entry_user)

    sent_message = await send_reply(message, gemini_reply_doc, context)

    history_entry_model = {"role": "model", "parts": [{"text": gemini_reply_doc or "🤖 Не удалось обработать документ."}], "bot_message_id": sent_message.message_id if sent_message else None}
    chat_history.append(history_entry_model)

    if context.application.persistence:
        await context.application.persistence.update_chat_data(chat_id, context.chat_data)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата (документ) принудительно сохранена.")

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)

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
        logger.warning("Переменная окружения DATABASE_URL не установлена. Бот будет работать без сохранения состояния.")

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence:
        builder.persistence(persistence)

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
        commands = [
            BotCommand("start", "Начать работу и инфо"),
            BotCommand("transcribe", "Превратить голосовое в текст"),
            BotCommand("model", "Выбрать модель Gemini"),
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
    # ... (настройки логгирования других библиотек)
    logger.setLevel(log_level)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             signal.signal(sig, lambda s, f: signal_handler())
             
    application = None
    web_server_task = None
    http_client_custom = None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        http_client_custom = httpx.AsyncClient()
        application, web_server_coro = await setup_bot_and_server(stop_event)
        
        # Прикрепляем http_client к application, а не к bot_data
        setattr(application, 'http_client', http_client_custom)
        
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask")
        
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки...")
        await stop_event.wait()
    except Exception as e:
        logger.critical(f"Критическая ошибка во время запуска или ожидания: {e}", exc_info=True)
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
             logger.info("Закрытие HTTPX клиента...");
             await http_client_custom.aclose()
        
        persistence = getattr(application, 'persistence', None)
        if persistence and isinstance(persistence, PostgresPersistence):
            logger.info("Закрытие соединений с базой данных...")
            persistence.close()
            
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение прервано пользователем (KeyboardInterrupt).")
    except Exception as e_top:
        logger.critical(f"Неперехваченная ошибка на верхнем уровне: {e_top}", exc_info=True)
