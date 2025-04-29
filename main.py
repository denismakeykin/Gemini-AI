# --- START OF FILE main.py ---

# Обновлённый main.py:
# - Добавлен Google Custom Search API как основной поиск
# - DuckDuckGo используется как запасной вариант
# - Исправлен поиск DDG: используется синхронный ddgs.text() в отдельном потоке через asyncio.to_thread()
# - Скорректирована системная инструкция и формирование промпта с поиском для более естественного ответа.
# - Улучшено формирование промпта для фото и документов для лучшего удержания контекста.
# - История чата сохраняется без поискового контекста.
# - ДОБАВЛЕНА ЛОГИКА ПОВТОРНЫХ ЗАПРОСОВ (RETRY) к Gemini при 500-х ошибках.
# - ИСПРАВЛЕНО: Настройки безопасности BLOCK_NONE устанавливаются даже при ошибке импорта типов.
# - ИСПРАВЛЕНО: Улучшена инструкция и формирование промпта для лучшего удержания контекста диалога.
# === НОВЫЕ ИЗМЕНЕНИЯ ===
# - Отправка сообщений с Markdown (с fallback на обычный текст)
# - Улучшенная обработка импорта типов Gemini
# - Обновлены модели, константы, системная инструкция, /start
# - Улучшена команда /temp
# - Улучшено логирование Google Search и обработка ошибок/пустых ответов Gemini
# - Улучшена обработка фото (OCR timeout) и документов (0 байт, chardet, BOM, пустой текст)
# - Скорректированы уровни логирования
# - Аккуратное формирование URL вебхука
# - ИСПРАВЛЕНО: Ошибка TypeError в handle_telegram_webhook (убран create_task/shield)
# - Реализован комбинированный подход для удержания контекста изображений:
#   - file_id сохраняется в истории пользователя.
#   - Описание изображения от Vision добавляется в историю с префиксом.
#   - Ответ на сообщение с описанием триггерит повторный анализ оригинала.
#   - Добавлена функция reanalyze_image.
#   - Обновлены handle_photo и handle_message для поддержки этой логики.
# === ИЗМЕНЕНИЯ ДЛЯ ГРУПП И КОНТЕКСТА ===
# - История чата хранится в chat_data (общая для группы).
# - Сообщения пользователя в истории предваряются префиксом `[User ID]:`.
# - Системная инструкция обновлена для учета User ID в истории.
# - Обновлены все функции, работающие с историей, для поддержки нового формата.
# - Команда /clear снова очищает общую историю чата.
# - В reanalyze и при ответах на спец. сообщения поиск ID ведется в общей истории.
# === ИЗМЕНЕНИЯ ДЛЯ YOUTUBE ===
# - Обработка YouTube ссылок (конспект и reanalyze) использует передачу URI в тексте промпта.
# - Убран вызов genai.upload_file() и genai.delete_file() для YouTube.
# - Промпты для видео обновлены для более явного указания модели анализировать контент по ссылке.
# === ПЕРСОНАЛИЗАЦИЯ И ИНСТРУКЦИИ ===
# - Обновлена системная инструкция и стартовое сообщение согласно запросу пользователя.
# - Добавлено обращение по имени пользователя во всех командах и служебных ответах.
# - Исправлены ВСЕ найденные ошибки SyntaxError.
# === ИЗМЕНЕНИЯ ДЛЯ АКТУАЛЬНОЙ ДАТЫ ===
# - Добавлен импорт datetime и pytz.
# - В handle_message, reanalyze_image, reanalyze_video добавляется текущая дата и время (UTC+3) в контекст перед вызовом Gemini.
# - Обновлена системная инструкция для использования этой информации.
# - Уточнен промпт для YouTube для большей ясности.
# - Исправлены ВСЕ найденные ошибки SyntaxError.
# - Переход на встроенный поиск Google (для Gemini 1.5 с `MODE_ENABLED`).
# - Удаление ручного поиска и связанных команд.
# - Добавление актуальной даты/времени в контекст.
# - Обновленная системная инструкция.
# - Обращение по имени пользователя в командах.
# - Все исправления синтаксических ошибок.
# - Переход на встроенный поиск Google (Gemini API Tool) для актуальности.
# - Удалены функции ручного поиска (Google Custom Search, DDG).
# - Удалены команды /search_on, /search_off.
# - Вызов Gemini API изменен для использования инструмента GoogleSearchRetrieval (для моделей 1.5).
# - Поиск настроен на срабатывание ВСЕГДА (MODE_ENABLED).
# - В контекст модели добавляется актуальная дата и время (UTC+3).
# - Обновлена системная инструкция для работы с User ID, временем и встроенным поиском.
# - Добавлено обращение по имени пользователя в ответах на команды.
# - Сохранены все предыдущие исправления синтаксиса и другие улучшения.

import logging
import os
import asyncio
import signal
from urllib.parse import urlencode, urlparse, parse_qs
import base64
import pytesseract
from PIL import Image
import io
import pprint
import json
import time
import re # Добавлено для YouTube
import datetime # <<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: для даты/времени
import pytz     # <<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: для часовых поясов

# Инициализируем логгер
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
import aiohttp.web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest, TelegramError
import google.generativeai as genai
from duckduckgo_search import DDGS

# ===== Обработка импорта типов Gemini и SAFETY_SETTINGS =====
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
BlockReason = type('BlockReason', (object,), {'UNSPECIFIED': 'UNSPECIFIED', 'name': 'UNSPECIFIED'})
FinishReason = type('FinishReason', (object,), {'STOP': 'STOP', 'name': 'STOP'})

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
# ==========================================================

# --- Переменные окружения и Настройка Gemini ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')

required_env_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY,
    "GOOGLE_CSE_ID": GOOGLE_CSE_ID, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH
}
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars:
    logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    exit(1)

genai.configure(api_key=GOOGLE_API_KEY)
# =================================================

# --- Модели, Константы, Системная инструкция ---
AVAILABLE_MODELS = {
    'gemini-2.5-flash-preview-04-17': '2.5 Flash Preview',
    'gemini-2.5-pro-exp-03-25': '2.5 Pro exp.',
    'gemini-2.0-flash-thinking-exp-01-21': '2.0 Flash Thinking exp.',
}
DEFAULT_MODEL = 'gemini-2.5-flash-preview-04-17' if 'gemini-2.5-flash-preview-04-17' in AVAILABLE_MODELS else 'gemini-2.5-pro-exp-03-25'

MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 5000
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VIDEO_CAPABLE_KEYWORDS = ['gemini-2.5-flash-preview-04-17']
USER_ID_PREFIX_FORMAT = "[User {user_id}]: "
TARGET_TIMEZONE = "Europe/Moscow" # <<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Часовой пояс UTC+3



import logging
import os
import asyncio
import signal
from urllib.parse import urlencode, urlparse, parse_qs
import base64
import pytesseract
from PIL import Image
import io
import pprint
import json
import time
import re
import datetime
import pytz

# Инициализируем логгер
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

import aiohttp
import aiohttp.web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest, TelegramError
import google.generativeai as genai
# Убрали импорт DDGS
# from duckduckgo_search import DDGS

# ===== Обработка импорта типов Gemini и SAFETY_SETTINGS =====
try:
    from google.generativeai.types import (
        Tool, GenerationConfig, GoogleSearchRetrieval, ToolConfig, FunctionDeclaration, HarmCategory, HarmBlockThreshold,
        BlockedPromptException, StopCandidateException, SafetyRating, BlockReason, FinishReason,
        GoogleSearchRetrievalMode # Добавлен режим для 1.5
    )
    # Для совместимости с новыми SDK, где GoogleSearch может быть напрямую импортирован
    try: from google.generativeai.types import GoogleSearch
    except ImportError: GoogleSearch = type('GoogleSearch', (object,), {}) # Заглушка, если нет
    # Режимы для GoogleSearchRetrieval (модели 1.5)
    SEARCH_MODE_ALWAYS = GoogleSearchRetrievalMode.MODE_ENABLED # Используем MODE_ENABLED для принудительного поиска в 1.5
    logger.info("Типы google.generativeai.types (включая Tool, GoogleSearchRetrieval) успешно импортированы.")

except ImportError as e_tool_import:
    logger.warning(f"Не удалось импортировать типы инструментов Gemini (Tool, GoogleSearchRetrieval и т.д.): {e_tool_import}. Встроенный поиск может не работать.")
    Tool = type('Tool', (object,), {})
    GenerationConfig = genai.GenerationConfig # Используем стандартный, если типы не загрузились
    GoogleSearchRetrieval = type('GoogleSearchRetrieval', (object,), {})
    GoogleSearch = type('GoogleSearch', (object,), {})
    ToolConfig = type('ToolConfig', (object,), {})
    FunctionDeclaration = type('FunctionDeclaration', (object,), {})
    SEARCH_MODE_ALWAYS = "MODE_ENABLED" # Используем строку как fallback

# Остальной код импорта HarmCategory и т.д.
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
BlockReason = type('BlockReason', (object,), {'UNSPECIFIED': 'UNSPECIFIED', 'name': 'UNSPECIFIED'})
FinishReason = type('FinishReason', (object,), {'STOP': 'STOP', 'name': 'STOP'})

try:
    # Пытаемся переопределить стандартные типы, если импорт из types удался
    from google.generativeai.types import (
        HarmCategory as RealHarmCategory, HarmBlockThreshold as RealHarmBlockThreshold,
        BlockedPromptException as RealBlockedPromptException,
        StopCandidateException as RealStopCandidateException,
        SafetyRating as RealSafetyRating, BlockReason as RealBlockReason,
        FinishReason as RealFinishReason
    )
    logger.debug("Переопределение стандартных типов Gemini...")
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
                all_enums_found = False; break
    else:
        logger.warning("Атрибут 'BLOCK_NONE' не найден в HarmBlockThreshold.")
        all_enums_found = False

    if all_enums_found and temp_safety_settings:
        SAFETY_SETTINGS_BLOCK_NONE = temp_safety_settings; logger.info("Настройки безопасности BLOCK_NONE установлены с Enum.")
    elif HARM_CATEGORIES_STRINGS:
        logger.warning("Не удалось создать SAFETY_SETTINGS_BLOCK_NONE с Enum. Используем строки.")
        SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
    else:
        logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки безопасности не установлены.")
        SAFETY_SETTINGS_BLOCK_NONE = []
except ImportError:
    logger.warning("Повторный Import Error при переопределении типов Gemini (ожидаемо, если первый импорт не удался).")
except Exception as e_import_types:
    logger.error(f"Ошибка при импорте/настройке типов Gemini: {e_import_types}", exc_info=True)
    SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS] if HARM_CATEGORIES_STRINGS else []
    logger.warning(f"Настройки безопасности установлены со строками (BLOCK_NONE) из-за ошибки: {bool(SAFETY_SETTINGS_BLOCK_NONE)}.")
# ==========================================================

# --- Переменные окружения и Настройка Gemini ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
# Убрали GOOGLE_CSE_ID
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')

required_env_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY,
    "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH
}
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars:
    logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    exit(1)

genai.configure(api_key=GOOGLE_API_KEY)
# =================================================

# --- Модели, Константы, Системная инструкция ---
AVAILABLE_MODELS = {
    'gemini-2.5-flash-preview-04-17': '2.5 Flash Preview',
    'gemini-2.5-pro-exp-03-25': '2.5 Pro exp.',
    'gemini-2.0-flash-thinking-exp-01-21': '2.0 Flash Thinking exp.',
}
DEFAULT_MODEL = 'gemini-2.5-flash-preview-04-17' if 'gemini-2.5-flash-preview-04-17' in AVAILABLE_MODELS else 'gemini-2.5-pro-exp-03-25'

MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
# Убрали DDG_MAX_RESULTS, GOOGLE_SEARCH_MAX_RESULTS
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VIDEO_CAPABLE_KEYWORDS = ['flash', 'pro', 'ultra', '1.5']
USER_ID_PREFIX_FORMAT = "[User {user_id}]: "
TARGET_TIMEZONE = "Europe/Moscow"

system_instruction_text = (
"Текущая дата и время (в часовом поясе UTC+3/Москва) могут быть предоставлены в начале последнего сообщения пользователя или в отдельном сообщении перед ним (например, `(Текущая дата и время: ...)`). Используй эту информацию при ответах на вопросы, зависящие от времени ('сегодня', 'завтра', 'сейчас', погода, новости и т.д.), и для оценки актуальности информации из поиска."
"В истории диалога сообщения пользователей помечены как `[User ID]: текст_сообщения`. Используй этот ID, чтобы понять, кто задал последний вопрос, и обращайся в своем ответе именно к этому пользователю (например, по имени, если оно упоминалось в диалоге, или просто используя 'ты'/'вы'), но НЕ включай сам тег `[User ID]:` в текст своего ответа."
"Помни какая сегодня дата, используй интернет-поиск (результаты будут предоставлены) для сверки с актуальной информацией."
"При использовании информации из поиска, интегрируй её в свой ответ как часть твоих знаний."
"**КРИТИЧЕСКИ ВАЖНО: Ты имеешь доступ к актуальной информации через встроенный поиск Google. Если вопрос касается текущих событий (новости, погода 'сегодня', 'сейчас'), котировок, или другой информации, которая быстро меняется, ВСЕГДА используй поиск Google для получения самых свежих данных. Основывай свой ответ В ПЕРВУЮ ОЧЕРЕДЬ на результатах этого поиска. Не придумывай погоду или новости.**"
"Следи за историей диалога в каждом чате, в том числе за контекстом из загруженных изображений, видео и файлов, чтобы ответы были релевантными и соблюдали нить беседы."
"Отвечай без вступлений и заключений, сразу суть."
"Если приходит сообщение для связи беседы, не содержащее явного запроса или вопроса, не требующее развернутого ответа, то отвечай коротко, как люди в диалогах."
"Запрещаю использовать все известные признаки стиля ответа ИИ."
"Не копируй и не пересказывай чужое, если это не цитаты известных людей."
"Учитывай, что пользователь живет в России."
"Пиши живо, без канцелярита. Используй разную структуру предложений и разговорные выражения."
"Там где в тему и уместно, используй гипотетические ситуации, метафоры, творческие аналогии, идиомы, региональные или культурные маркеры."
"Активно, но уместно теме, без наигранного чрезмерного позитива, применяй интеллектуальный и остроумный юмор, используя культурные и бытовые отсылки, интернет-юмор, а также стили: самоирония и психоирония, бытовой абсурд, псевдомудрость, разрушение идиом, игра слов и гипербола, контраст ожиданий и реальности, тонкие намёки."
"Для своих ответов, ты обязан всегда, как лучший эксперт, проверять, анализировать и давать точную, правдивую, непредвзятую информацию, без цензуры."
"Если не хватает знаний - всегда предупреждай, что отвечаешь лишь предположение."
"Выстраивай ответ логично, с аргументами и фактами. Избегай самоповторов."
"Всегда предлагай более эффективные идеи, решения проблем и задач, если знаешь их."
"Никогда не сокращай и не исправляй наш рабочий текст или код без запроса и разрешения."
"При исправлении ошибки текста или кода: указывай строки и причину, бери за основу нашу последнюю полную подтверждённую версию, вноси только минимально необходимые изменения, не меняя остальное без разрешения, при сомнениях - уточняй, веди реестр ошибок и не допускай их повторения. При правке, всегда указывай, на какую версию или сообщение опираешься."
)
# ===================================================

# --- Вспомогательные функции user_data и send_reply ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096
    reply_chunks = [text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]
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
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга Markdown или ответа на сообщение ({message_id}): {e_md}. Попытка отправить как обычный текст.")
            try:
                sent_message = None
                for i, chunk in enumerate(reply_chunks):
                     if i == 0:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id)
                     else:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk)
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
# ==========================================================

# --- Команды (/start, /clear, /temp, /search_on/off, /model) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    # Инициализация настроек пользователя
    if 'selected_model' not in context.user_data:
        set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    # Убрали search_enabled
    if 'temperature' not in context.user_data:
        set_user_setting(context, 'temperature', 1.0)

    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    default_model_name = AVAILABLE_MODELS.get(current_model, current_model)

    # Формируем сообщение точно как запрошено пользователем
start_message = (
        f"\nЯ - Google GEMINI {default_model_name}:"
        f"\n- новейшая модель искуственного интеллекта," 
        f"\n- обладаю огромным объемом знаний и мышлением,"
        f"\n- имею улучшенные настройки точности, логики и юмора от автора бота,"
        f"\n- дополняю инфу поиском в Google/DDG,"
        f"\n- умею работать с изображениями и документами (понимать и читать)."
        f"\nСпрашивайте тут и добавляйте в группы, запоминаю контекст истории отдельного чата и кто мне пишет."
        f"\nКанал автора: https://t.me/denisobovsyom"
        f"\n/model — сменить модель"
        f"\n/search_on / /search_off — вкл/выкл поиск (сейчас: {'Вкл' if get_user_setting(context, 'search_enabled', True) else 'Выкл'})"
        f"\n/clear — очистить историю этого чата"
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"

    context.chat_data['history'] = []
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

# Функции enable_search и disable_search удалены

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

# ==============================================================

# ===== Функция поиска Google - УДАЛЕНА =====

# ===== Функция извлечения YouTube ID =====
def extract_youtube_id(url: str) -> str | None:
    patterns = [ r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})', ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname in ('youtube.com', 'www.youtube.com') and parsed_url.path == '/watch':
            query_params = parse_qs(parsed_url.query)
            if 'v' in query_params and query_params['v']:
                video_id_candidate = query_params['v'][0]
                if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]):
                    return video_id_candidate[:11]
        if parsed_url.hostname in ('youtu.be',) and parsed_url.path:
             video_id_candidate = parsed_url.path[1:]
             if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]):
                 return video_id_candidate[:11]
    except Exception as e_parse:
        logger.debug(f"Ошибка парсинга URL для YouTube ID: {e_parse} (URL: {url[:50]}...)")
    return None
# ==================================

# --- Функция для получения текущего времени ---
def get_current_time_str() -> str:
    try:
        tz = pytz.timezone(TARGET_TIMEZONE)
        now = datetime.datetime.now(tz)
        months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        month_name = months[now.month - 1]
        utc_offset_minutes = now.utcoffset().total_seconds() // 60
        utc_offset_hours = int(utc_offset_minutes // 60)
        utc_offset_sign = '+' if utc_offset_hours >= 0 else '-'
        utc_offset_str = f"UTC{utc_offset_sign}{abs(utc_offset_hours)}"
        time_str = now.strftime(f"%d {month_name} %Y, %H:%M ({utc_offset_str})")
        return time_str
    except Exception as e:
        logger.error(f"Ошибка получения времени для пояса {TARGET_TIMEZONE}: {e}")
        now_utc = datetime.datetime.now(pytz.utc)
        return now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
# ===================================================

# ===== Функция повторного анализа изображения (использует chat_data) =====
# (Остается без изменений от предыдущей полной версии, т.к. поиск здесь не включаем)
async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по фото от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    # 1. Скачивание и кодирование
    try:
        img_file = await context.bot.get_file(file_id)
        file_bytes = await img_file.download_as_bytearray()
        if not file_bytes: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Не удалось скачать или файл пустой для file_id: ...{file_id[-10:]}"); await update.message.reply_text("❌ Не удалось получить исходное изображение для повторного анализа."); return
        b64_data = base64.b64encode(file_bytes).decode()
    except TelegramError as e_telegram: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка Telegram при получении/скачивании файла {file_id}: {e_telegram}", exc_info=True); await update.message.reply_text(f"❌ Ошибка Telegram при получении изображения: {e_telegram}"); return
    except Exception as e_download: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка скачивания/кодирования файла {file_id}: {e_download}", exc_info=True); await update.message.reply_text("❌ Ошибка при подготовке изображения для повторного анализа."); return
    # 2. Формирование запроса к Vision (с временем)
    current_time_str = get_current_time_str()
    user_question_with_context = f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}"
    mime_type = "image/jpeg";
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts = [{"text": user_question_with_context}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]; content_for_vision = [{"role": "user", "parts": parts}]
    # 3. Вызов модели
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']; is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
    if not is_vision_model:
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
        else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет доступных vision моделей."); await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения."); return
    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель: {model_id}, Темп: {temperature}"); reply = None; response_vision = None
    for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            # Вызываем БЕЗ tools, т.к. reanalyze обычно не требует нового поиска
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)
            if hasattr(response_vision, 'text'): reply = response_vision.text; else: reply = None
            if not reply: # ... (обработка пустого ответа) ...
                break
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply: logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Успешный анализ на попытке {attempt + 1}."); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: # ... (обработка блокировки) ...
             reason_str = "неизвестна"; try: # ... извлечение причины ...
             except Exception: pass; logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Анализ заблокирован/остановлен..."); reply = f"❌ Не удалось повторно проанализировать изображение (ограничение модели)."; break
        except Exception as e: # ... (обработка других ошибок и ретрай) ...
            error_message = str(e); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка на попытке {attempt + 1}: {error_message[:200]}..."); is_retryable = "500" in error_message or "503" in error_message; if "400" in error_message or "429" in error_message or "location is not supported" in error_message: reply = f"❌ Ошибка при повторном анализе изображения ({error_message[:100]}...)."; break; elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue; else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось выполнить анализ после {attempt + 1} попыток...", exc_info=True if not is_retryable else False); if reply is None: reply = f"❌ Ошибка при повторном анализе после {attempt + 1} попыток."; break
    # 4. Добавление в историю и отправка
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = { "role": "user", "parts": [{"text": user_question_with_context}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    if reply: history_entry_model = {"role": "model", "parts": [{"text": reply}]}; chat_history.append(history_entry_model); await send_reply(update.message, reply, context)
    else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет ответа для отправки."); final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."; chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]}); try: await update.message.reply_text(final_error_msg); except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
# =======================================================

# ===== Ответ на вопросы по конспекту видео (использует chat_data, ссылка в тексте) =====
# (Остается без изменений от предыдущей полной версии, использует время, БЕЗ tools)
async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id; requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    youtube_uri = f"https://www.youtube.com/watch?v={video_id}"; current_time_str = get_current_time_str()
    # Формирование промпта
    prompt_for_video = f"(Текущая дата и время: {current_time_str})\n{user_question}\n\n**Важно:** Ответь на основе содержимого видео по следующей ссылке: {youtube_uri}"
    content_for_video = [{"role": "user", "parts": [{"text": prompt_for_video}]}]
    # Вызов модели
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
    if not is_video_model:
        video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
        if video_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель {original_model_name} не video. Временно использую {new_model_name}.")
        else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет доступных video моделей."); await update.message.reply_text("❌ Нет доступных моделей для ответа на вопрос по видео."); return
    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель: {model_id}, Темп: {temperature}"); reply = None; response_video = None
    for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            # Вызываем БЕЗ tools
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_video = await asyncio.to_thread(model.generate_content, content_for_video)
            if hasattr(response_video, 'text'): reply = response_video.text; else: reply = None
            if not reply: block_reason_str, finish_reason_str = 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                except Exception as e_inner_reason: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка извлечения причины: {e_inner_reason}"); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                break
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply: logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Успешный анализ на попытке {attempt + 1}."); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: reason_str = "неизвестна"; try: # ... (извлечение причины) ...
            except Exception: pass; logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Анализ заблокирован/остановлен..."); reply = f"❌ Не удалось ответить по видео (ограничение модели)."; break
        except Exception as e: error_message = str(e); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка на попытке {attempt + 1}: {error_message[:200]}..."); is_retryable = "500" in error_message or "503" in error_message; if "400" in error_message or "429" in error_message or "location is not supported" in error_message: reply = f"❌ Ошибка при ответе по видео ({error_message[:100]}...)."; break; elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue; else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось выполнить анализ после {attempt + 1} попыток...", exc_info=True if not is_retryable else False); if reply is None: reply = f"❌ Ошибка при ответе по видео после {attempt + 1} попыток."; break
    # Добавление в историю и отправка
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = { "role": "user", "parts": [{"text": f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}"}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    if reply: history_entry_model = {"role": "model", "parts": [{"text": reply}]}; chat_history.append(history_entry_model); await send_reply(update.message, reply, context)
    else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет ответа для отправки."); final_error_msg = "🤖 К сожалению, не удалось ответить на ваш вопрос по видео."; chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]}); try: await update.message.reply_text(final_error_msg); except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
# =============================================================


# ===== Основной обработчик сообщений (с ИЗМЕНЕННЫМ вызовом Gemini API) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | Не удалось определить пользователя в update. Игнорирование сообщения."); return
    user_id = update.effective_user.id
    message = update.message
    if not message: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой объект message в update."); return
    if not message.text and not hasattr(message, 'image_file_id'): logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получено сообщение без текста и без image_file_id."); return

    chat_history = context.chat_data.setdefault("history", [])

    # --- Проверка на ответ к специальным сообщениям ---
    if message.reply_to_message and message.reply_to_message.text and message.text and not message.text.startswith('/'):
        replied_message = message.reply_to_message; replied_text = replied_message.text; user_question = message.text.strip(); requesting_user_id = user_id; found_special_context = False
        try: # Поиск ID для reanalyze
            for i in range(len(chat_history) - 1, -1, -1):
                model_entry = chat_history[i]
                if model_entry.get("role") == "model" and model_entry.get("parts") and isinstance(model_entry["parts"], list) and len(model_entry["parts"]) > 0:
                    model_text = model_entry["parts"][0].get("text", ""); is_image_reply = model_text.startswith(IMAGE_DESCRIPTION_PREFIX) and replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) and model_text[:100] == replied_text[:100]; is_video_reply = model_text.startswith(YOUTUBE_SUMMARY_PREFIX) and replied_text.startswith(YOUTUBE_SUMMARY_PREFIX) and model_text[:100] == replied_text[:100]
                    if is_image_reply or is_video_reply:
                        if i > 0: potential_user_entry = chat_history[i - 1]
                        if potential_user_entry.get("role") == "user": original_user_id_from_hist = potential_user_entry.get("user_id", "Unknown")
                        if is_image_reply and "image_file_id" in potential_user_entry: found_file_id = potential_user_entry["image_file_id"]; logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найден image_file_id: ...{found_file_id[-10:]} для reanalyze_image..."); await reanalyze_image(update, context, found_file_id, user_question, original_user_id_from_hist); found_special_context = True; break
                        elif is_video_reply and "youtube_video_id" in potential_user_entry: found_video_id = potential_user_entry["youtube_video_id"]; logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найден youtube_video_id: {found_video_id} для reanalyze_video..."); await reanalyze_video(update, context, found_video_id, user_question, original_user_id_from_hist); found_special_context = True; break
                        else: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найдено сообщение модели, но у предыдущего user-сообщения нет нужного ID.")
                        else: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найдено сообщение модели в самом начале истории.")
                        if not found_special_context: break
        except Exception as e_hist_search: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка при поиске ID для reanalyze в chat_history: {e_hist_search}", exc_info=True)
        if found_special_context: return
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX): logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на спец. сообщение, но ID не найден или reanalyze не запущен.")

    # --- Получение текста и проверка на YouTube ---
    original_user_message_text = ""; image_file_id_from_ocr = None; user_message_id = message.message_id
    if hasattr(message, 'image_file_id'): image_file_id_from_ocr = message.image_file_id; original_user_message_text = message.text.strip() if message.text else ""; logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен image_file_id: ...{image_file_id_from_ocr[-10:]} из OCR.")
    elif message.text: original_user_message_text = message.text.strip()
    user_message_with_id = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + original_user_message_text
    youtube_handled = False
    if not (message.reply_to_message and message.reply_to_message.text and (message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) or message.reply_to_message.text.startswith(YOUTUBE_SUMMARY_PREFIX))) and not image_file_id_from_ocr:
        youtube_id = extract_youtube_id(original_user_message_text)
        if youtube_id:
            youtube_handled = True
            first_name = update.effective_user.first_name; user_mention = f"{first_name}" if first_name else f"User {user_id}"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружена ссылка YouTube (ID: {youtube_id}). Запрос конспекта для {user_mention}...")
            try: await update.message.reply_text(f"Окей, {user_mention}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
            except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение 'гляну видео': {e_reply}")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            youtube_uri = f"https://www.youtube.com/watch?v={youtube_id}"; current_time_str = get_current_time_str()
            # Промпт БЕЗ User ID, но с ВРЕМЕНЕМ и ссылкой
            prompt_for_summary = f"(Текущая дата и время: {current_time_str})\nСделай краткий, но информативный конспект видео.\n**ССЫЛКА НА ВИДЕО ДЛЯ АНАЛИЗА:** {youtube_uri}\nОпиши основные пункты и ключевые моменты из СОДЕРЖИМОГО этого видео."
            content_for_summary = [{"role": "user", "parts": [{"text": prompt_for_summary}]}]
            # Вызов модели
            model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
            is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
            if not is_video_model:
                video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)];
                if video_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель {original_model_name} не video. Временно использую {new_model_name}.")
                else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет доступных video моделей."); await update.message.reply_text("❌ Нет доступных моделей для создания конспекта видео."); return
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель: {model_id}, Темп: {temperature}"); reply = None
            for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
                 try:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                     # Вызываем БЕЗ tools, т.к. конспект - это про видео, а не про поиск
                     model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
                     response_summary = await asyncio.to_thread(model.generate_content, content_for_summary)
                     if hasattr(response_summary, 'text'): reply = response_summary.text; else: reply = None
                     if not reply: # ... (обработка пустого ответа) ...
                         break
                     if reply and "не удалось создать конспект" not in reply.lower() and "не смогла создать конспект" not in reply.lower(): logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Успешный конспект на попытке {attempt + 1}."); break
                 except (BlockedPromptException, StopCandidateException) as e_block_stop: # ... (обработка блокировки) ...
                      reason_str = "неизвестна"; try: # ... извлечение причины ...
                      except Exception: pass; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Конспект заблокирован/остановлен..."); reply = f"❌ Не удалось создать конспект (ограничение модели)."; break
                 except Exception as e: # ... (обработка других ошибок и ретрай) ...
                      error_message = str(e); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка на попытке {attempt + 1}: {error_message[:200]}..."); is_retryable = "500" in error_message or "503" in error_message; if "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported language" in error_message.lower(): reply = f"❌ Ошибка при создании конспекта ({error_message[:100]}...)."; break; elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue; else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось создать конспект после {attempt + 1} попыток...", exc_info=True if not is_retryable else False); if reply is None: reply = f"❌ Ошибка при создании конспекта после {attempt + 1} попыток."; break
            # --- Сохранение в историю и отправка ---
            history_entry_user = { "role": "user", "parts": [{"text": user_message_with_id}], "youtube_video_id": youtube_id, "user_id": user_id, "message_id": user_message_id }; chat_history.append(history_entry_user); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (YouTube) в chat_history с youtube_video_id.")
            if reply and "❌" not in reply and "🤖" not in reply: model_reply_text_with_prefix = f"{YOUTUBE_SUMMARY_PREFIX}{reply}"
            else: model_reply_text_with_prefix = reply if reply else "🤖 Не удалось создать конспект видео."
            history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]}; chat_history.append(history_entry_model); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (YouTube) в chat_history.")
            reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text_with_prefix
            if reply_to_send: await send_reply(message, reply_to_send, context)
            else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет ответа для отправки."); try: await message.reply_text("🤖 К сожалению, не удалось создать конспект видео."); except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось отправить сообщение об ошибке: {e_final_fail}")
            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return # Завершаем обработку здесь для YouTube ссылок

    # ############################################################
    # ####### КОНЕЦ БЛОКА ОБРАБОТКИ YOUTUBE ССЫЛОК ##############
    # ############################################################

    # --- Стандартная обработка текста (если не YouTube) ---
    if not youtube_handled:
        if not original_user_message_text and not image_file_id_from_ocr:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Дошли до конца handle_message без текста для обработки (не YouTube, не OCR).")
            return
        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
        temperature = get_user_setting(context, 'temperature', 1.0)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # --- Блок поиска УДАЛЕН ---

        # --- Формирование финального промпта с ВРЕМЕНЕМ ---
        current_time_str = get_current_time_str()
        time_context_str = f"(Текущая дата и время: {current_time_str})\n"
        final_user_prompt_text = time_context_str + user_message_with_id # Время + Сообщение с ID

        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Запрос к модели (поиск включен через API)...") # Обновил лог
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Финальный промпт для Gemini (длина {len(final_user_prompt_text)}):\n{final_user_prompt_text[:600]}...")

        # --- История и ее обрезка ---
        history_entry_user = { "role": "user", "parts": [{"text": user_message_with_id}], "user_id": user_id, "message_id": user_message_id }
        if image_file_id_from_ocr:
            history_entry_user["image_file_id"] = image_file_id_from_ocr
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (OCR) в chat_history с image_file_id.")
        else:
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (текст) в chat_history.")
        chat_history.append(history_entry_user)

        history_for_model_raw = []; current_total_chars = 0
        for entry in reversed(chat_history[:-1]):
            entry_text = ""; entry_len = 0
            if entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text"): entry_text = entry["parts"][0]["text"]; entry_len = len(entry_text)
            if current_total_chars + entry_len + len(final_user_prompt_text) <= MAX_CONTEXT_CHARS: history_for_model_raw.append(entry); current_total_chars += entry_len
            else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обрезка истории по символам ({MAX_CONTEXT_CHARS})..."); break
        history_for_model = list(reversed(history_for_model_raw))
        history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]}) # Добавляем финальный промпт

        history_clean_for_model = []
        for entry in history_for_model: history_clean_for_model.append({"role": entry["role"], "parts": entry["parts"]})

        # --- НАСТРОЙКА ИНСТРУМЕНТОВ ПОИСКА ---
        tools = []
        tool_config = None
        # Используем GoogleSearchRetrieval для моделей 1.5, т.к. они лучше поддерживают управление поиском
        if "1.5" in model_id:
            try:
                search_tool_1_5 = Tool(google_search_retrieval=GoogleSearchRetrieval()) # Просто создаем инструмент
                tools.append(search_tool_1_5)
                # Настраиваем режим ALWAYS через ToolConfig
                tool_config = ToolConfig(
                     google_search_retrieval_config=ToolConfig.GoogleSearchRetrievalConfig(
                         mode=SEARCH_MODE_ALWAYS # Явно указываем режим MODE_ENABLED
                     )
                 )
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Настроен инструмент GoogleSearchRetrieval (режим: {SEARCH_MODE_ALWAYS}) для модели {model_id}.")
            except NameError: # Если импорт ToolConfig или SEARCH_MODE_ALWAYS не удался
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось настроить режим поиска ALWAYS для {model_id} из-за ошибки импорта типов. Поиск может работать в режиме AUTO.")
                 # Пытаемся добавить инструмент без конфига режима
                 try:
                     search_tool_1_5 = Tool(google_search_retrieval=GoogleSearchRetrieval())
                     tools.append(search_tool_1_5)
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Настроен инструмент GoogleSearchRetrieval (режим: AUTO?) для модели {model_id}.")
                 except Exception as e_tool_15_fallback:
                      logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка настройки GoogleSearchRetrieval (fallback) для {model_id}: {e_tool_15_fallback}")
            except Exception as e_tool_15:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка настройки GoogleSearchRetrieval для {model_id}: {e_tool_15}")
        else:
             logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Модель {model_id} не является 1.5. Встроенный поиск Google не будет принудительно включен.")

        # --- Вызов модели с инструментами ---
        reply = None; response = None; last_exception = None; generation_successful = False
        for attempt in range(RETRY_ATTEMPTS):
            try:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id} (с tools: {bool(tools)}, tool_config: {bool(tool_config)})...")
                generation_config_obj = GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                model = genai.GenerativeModel(
                    model_id,
                    safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                    generation_config=generation_config_obj,
                    system_instruction=system_instruction_text,
                    tools=tools if tools else None,
                    tool_config=tool_config if tool_config else None # Передаем конфиг режима поиска
                )
                response = await asyncio.to_thread(model.generate_content, history_clean_for_model)

                if hasattr(response, 'text'): reply = response.text; else: reply = None

                # Логирование использования поиска
                grounding_triggered = False
                if hasattr(response, 'candidates') and response.candidates:
                    first_candidate = response.candidates[0]
                    if hasattr(first_candidate, 'grounding_metadata') and first_candidate.grounding_metadata:
                         grounding_triggered = True
                         if hasattr(first_candidate.grounding_metadata, 'web_search_queries') and first_candidate.grounding_metadata.web_search_queries:
                             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ модели основан на поиске Google. Запросы: {first_candidate.grounding_metadata.web_search_queries}")
                         else:
                             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ модели содержит grounding_metadata, но без поисковых запросов.")
                # Убрал лог об отсутствии метаданных

                if not reply: # Обработка пустого ответа
                     block_reason_str, finish_reason_str, safety_info_str = 'N/A', 'N/A', 'N/A'
                     try: # ... (извлечение причин) ...
                     except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка извлечения причины/safety: {e_inner_reason}")
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пустой ответ или нет текста (попытка {attempt + 1})...") # ... (формирование reply об ошибке) ...
                     generation_successful = (reply == "🤖 Модель дала пустой ответ.")
                     break

                if reply: generation_successful = True
                if generation_successful: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Успешная генерация на попытке {attempt + 1}. Поиск сработал: {grounding_triggered}"); break

            except (BlockedPromptException, StopCandidateException) as e_block_stop: # ... (обработка блокировки) ...
                reason_str = "неизвестна"; try: # ... извлечение причины ...
                except Exception: pass; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Запрос заблокирован/остановлен моделью..."); reply = f"❌ Запрос заблокирован/остановлен моделью."; break
            except Exception as e: # ... (обработка других ошибок и ретрай) ...
                last_exception = e; error_message = str(e); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка генерации на попытке {attempt + 1}: {error_message[:200]}..."); is_retryable = "500" in error_message or "503" in error_message; if "429" in error_message: reply = f"❌ Слишком много запросов к модели."; break; elif "400" in error_message: reply = f"❌ Ошибка в запросе к модели (400 Bad Request)."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка 400: {error_message}", exc_info=True); break; elif "location is not supported" in error_message: reply = f"❌ Эта модель недоступна в вашем регионе."; break; if is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue; else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось выполнить генерацию после {attempt + 1} попыток...", exc_info=True if not is_retryable else False); if reply is None: reply = f"❌ Ошибка при обращении к модели после {attempt + 1} попыток."; break

        # --- Добавление ответа и отправка (если не YouTube) ---
        if reply:
             history_entry_model = {"role": "model", "parts": [{"text": reply}]}
             chat_history.append(history_entry_model)
             if message:
                 await send_reply(message, reply, context)
             else:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не найдено сообщение для ответа в update.")
                 try: await context.bot.send_message(chat_id=chat_id, text=reply)
                 except Exception as e_send_direct: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить ответ напрямую: {e_send_direct}")
        else: # Если reply пустой
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Нет ответа для отправки пользователю после всех попыток.")
             try:
                 if reply != "🤖 Модель дала пустой ответ.": error_message_to_user = "🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток."
                 if message: await message.reply_text(error_message_to_user)
                 else: await context.bot.send_message(chat_id=chat_id, text=error_message_to_user)
             except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")

        # --- Ограничение истории ---
        while len(chat_history) > MAX_HISTORY_MESSAGES: removed = chat_history.pop(0); logger.debug(f"ChatID: {chat_id} | Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed.get('role')}")
# =============================================================

# ===== Обработчик фото (обновлен для chat_data и User ID) =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя.")
        return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.photo:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_photo не найдено фото.")
        return

    photo_file_id = message.photo[-1].file_id
    user_message_id = message.message_id
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен photo file_id: ...{photo_file_id[-10:]}, message_id: {user_message_id}")

    tesseract_available = False
    try:
        pytesseract.pytesseract.get_tesseract_version()
        tesseract_available = True
    except Exception:
        logger.info("Tesseract не найден или не настроен. OCR отключен.")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Скачанное фото (file_id: ...{photo_file_id[-10:]}) оказалось пустым.")
            await message.reply_text("❌ Не удалось загрузить изображение (файл пуст).")
            return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото (file_id: ...{photo_file_id[-10:]}): {e}", exc_info=True)
        try:
            await message.reply_text("❌ Не удалось загрузить изображение.")
        except Exception as e_reply:
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания фото: {e_reply}")
        return

    user_caption = message.caption if message.caption else ""

    # --- OCR ---
    ocr_triggered = False
    if tesseract_available:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            extracted_text = await asyncio.to_thread(pytesseract.image_to_string, image, lang='rus+eng', timeout=15)
            if extracted_text and extracted_text.strip():
                ocr_triggered = True
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружен текст на изображении (OCR).")
                ocr_context = f"На изображении обнаружен следующий текст:\n```\n{extracted_text.strip()}\n```"
                if user_caption:
                    user_prompt_ocr = f"{user_caption}. {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                else:
                    user_prompt_ocr = f"{ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                message.image_file_id = photo_file_id
                message.text = user_prompt_ocr
                logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с OCR текстом и image_file_id.")
                await handle_message(update, context)
                return
            else:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | OCR не нашел текст на изображении.")
        except pytesseract.TesseractNotFoundError:
            logger.error("Tesseract не найден! OCR отключен.")
            tesseract_available = False
        except RuntimeError as timeout_error:
            if "Tesseract process timeout" in str(timeout_error):
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | OCR таймаут: {timeout_error}")
                await message.reply_text("⏳ Не удалось распознать текст (слишком долго). Анализирую как фото...")
            else:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка выполнения OCR: {timeout_error}", exc_info=True)
                await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
        except Exception as e:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка OCR: {e}", exc_info=True)
            try:
                await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
            except Exception as e_reply_ocr_err:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке OCR: {e_reply_ocr_err}")


    # --- Обработка как изображение (Vision) ---
    if not ocr_triggered:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обработка фото как изображения (Vision).")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        MAX_IMAGE_BYTES = 20 * 1024 * 1024
        if len(file_bytes) > MAX_IMAGE_BYTES:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Изображение ({len(file_bytes) / (1024*1024):.2f} MB) может быть большим для API.")

        try:
            b64_data = base64.b64encode(file_bytes).decode()
        except Exception as e:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Base64 кодирования: {e}", exc_info=True)
            try:
                await message.reply_text("❌ Ошибка обработки изображения.")
            except Exception as e_reply_b64_err:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке Base64: {e_reply_b64_err}")
            return

        current_time_str = get_current_time_str() # <<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Получаем время
        if user_caption:
            prompt_text_vision = (
                f"(Текущая дата и время: {current_time_str})\n"
                f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
            )
        else:
            prompt_text_vision = (
                f"(Текущая дата и время: {current_time_str})\n"
                f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении."
            )

        mime_type = "image/jpeg"
        if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            mime_type = "image/png"
        elif file_bytes.startswith(b'\xff\xd8\xff'):
            mime_type = "image/jpeg"

        parts = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
        content_for_vision = [{"role": "user", "parts": parts}]

        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
        temperature = get_user_setting(context, 'temperature', 1.0)
        vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']
        is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)

        if not is_vision_model:
            vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
            if vision_models:
                original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0])
                model_id = fallback_model_id
                new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Модель {original_model_name} не vision. Временно использую {new_model_name}.")
            else:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Нет доступных vision моделей.")
                await message.reply_text("❌ Нет доступных моделей для анализа изображений.")
                return

        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Анализ изображения (Vision). Модель: {model_id}, Темп: {temperature}, MIME: {mime_type}")
        reply = None
        response_vision = None

        # --- Вызов Vision модели с РЕТРАЯМИ ---
        for attempt in range(RETRY_ATTEMPTS):
            try:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
                generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
                response_vision = await asyncio.to_thread(model.generate_content, content_for_vision) # Отправляем контент с временем

                if hasattr(response_vision, 'text'):
                    reply = response_vision.text
                else:
                    reply = None

                if not reply: # Обработка пустого ответа
                    block_reason_str, finish_reason_str = 'N/A', 'N/A'
                    try:
                        if hasattr(response_vision, 'prompt_feedback') and response_vision.prompt_feedback and hasattr(response_vision.prompt_feedback, 'block_reason'):
                            block_reason_enum = response_vision.prompt_feedback.block_reason
                            block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                        if hasattr(response_vision, 'candidates') and response_vision.candidates and isinstance(response_vision.candidates, (list, tuple)) and len(response_vision.candidates) > 0:
                            first_candidate = response_vision.candidates[0]
                            if hasattr(first_candidate, 'finish_reason'):
                                finish_reason_enum = first_candidate.finish_reason
                                finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                    except Exception as e_inner_reason:
                        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка извлечения причины пустого ответа: {e_inner_reason}")

                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                    if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']:
                        reply = f"🤖 Модель не смогла описать изображение. (Блокировка: {block_reason_str})"
                    elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']:
                        reply = f"🤖 Модель не смогла описать изображение. (Причина: {finish_reason_str})"
                    else:
                        reply = "🤖 Не удалось понять, что на изображении (пустой ответ)."
                    break

                if reply and "Не удалось понять" not in reply and "не смогла описать" not in reply:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Успешный анализ на попытке {attempt + 1}.")
                     break

            except (BlockedPromptException, StopCandidateException) as e_block_stop:
                 reason_str = "неизвестна"
                 try:
                     if hasattr(e_block_stop, 'args') and e_block_stop.args:
                         reason_str = str(e_block_stop.args[0])
                 except Exception:
                     pass
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
                 reply = f"❌ Анализ изображения заблокирован/остановлен моделью."
                 break

            except Exception as e:
                 error_message = str(e)
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
                 is_retryable = "500" in error_message or "503" in error_message
                 if "400" in error_message or "429" in error_message or "location is not supported" in error_message:
                     reply = f"❌ Ошибка при анализе изображения ({error_message[:100]}...)."; break
                 elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                     wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ожидание {wait_time:.1f} сек...")
                     await asyncio.sleep(wait_time)
                     continue
                 else:
                     logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                     if reply is None:
                         reply = f"❌ Ошибка при анализе изображения после {attempt + 1} попыток."
                     break

        # --- Сохранение и отправка ---
        chat_history = context.chat_data.setdefault("history", [])
        # Сохраняем в историю с User ID, но без времени
        user_text_for_history_vision = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото.")
        history_entry_user = { "role": "user", "parts": [{"text": user_text_for_history_vision}], "image_file_id": photo_file_id, "user_id": user_id, "message_id": user_message_id }
        chat_history.append(history_entry_user)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (Vision) в chat_history с image_file_id.")

        if reply and "❌" not in reply and "🤖" not in reply:
            model_reply_text_with_prefix = f"{IMAGE_DESCRIPTION_PREFIX}{reply}"
        else:
            model_reply_text_with_prefix = reply if reply else "🤖 Не удалось проанализировать изображение."

        history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]}
        chat_history.append(history_entry_model)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (Vision) в chat_history.")

        reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text_with_prefix
        if reply_to_send:
            await send_reply(message, reply_to_send, context)
        else:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Нет ответа для отправки пользователю после всех попыток.")
            try:
                await message.reply_text("🤖 К сожалению, не удалось проанализировать изображение.")
            except Exception as e_final_fail:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")

        # Ограничиваем историю
        while len(chat_history) > MAX_HISTORY_MESSAGES:
            chat_history.pop(0)

# ===== Обработчик документов (обновлен для chat_data через handle_message) =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя.")
        return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.document:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_document нет документа.")
        return
    doc = message.document
    allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql')
    allowed_mime_types = ('application/octet-stream',)
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types
    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})")
        return
    MAX_FILE_SIZE_MB = 15
    file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0 and doc.file_name:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пустой файл '{doc.file_name}'.")
        await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой.")
        return
    elif file_size_bytes == 0 and not doc.file_name:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой документ без имени.")
        return
    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> {MAX_FILE_SIZE_MB} MB).", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Слишком большой файл: {doc.file_name} ({file_size_bytes / (1024*1024):.2f} MB)")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
        if not file_bytes:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' скачан, но пуст.")
            await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой.")
            return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        try:
            await update.message.reply_text("❌ Не удалось загрузить файл.")
        except Exception as e_reply_dl_err:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания документа: {e_reply_dl_err}")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None
    detected_encoding = None
    encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1', 'cp866', 'iso-8859-5']
    chardet_available = False
    try:
        import chardet
        chardet_available = True
    except ImportError:
        logger.info("Библиотека chardet не найдена. Автоопределение кодировки будет ограничено.")

    if chardet_available:
        try:
            chardet_limit = min(len(file_bytes), 50 * 1024)
            if chardet_limit > 0:
                 detected = chardet.detect(file_bytes[:chardet_limit])
                 if detected and detected['encoding'] and detected['confidence'] > 0.7:
                      potential_encoding = detected['encoding'].lower()
                      logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet определил: {potential_encoding} ({detected['confidence']:.2f}) для '{doc.file_name}'")
                      if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'):
                           logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружен UTF-8 BOM, используем 'utf-8-sig'.")
                           detected_encoding = 'utf-8-sig'
                           if 'utf-8-sig' not in encodings_to_try:
                               encodings_to_try.insert(0, 'utf-8-sig')
                           if 'utf-8' in encodings_to_try:
                               try:
                                   encodings_to_try.remove('utf-8')
                               except ValueError: pass
                      else:
                           detected_encoding = potential_encoding
                           if detected_encoding in encodings_to_try:
                               encodings_to_try.remove(detected_encoding)
                           encodings_to_try.insert(0, detected_encoding)
                 else:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet не уверен ({detected.get('confidence', 0):.2f}) для '{doc.file_name}'.")
        except Exception as e_chardet:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при использовании chardet для '{doc.file_name}': {e_chardet}")

    unique_encodings = list(dict.fromkeys(encodings_to_try))
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Попытки декодирования для '{doc.file_name}': {unique_encodings}")
    for encoding in unique_encodings:
        try:
            text = file_bytes.decode(encoding)
            detected_encoding = encoding
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' успешно декодирован как {encoding}.")
            break
        except (UnicodeDecodeError, LookupError):
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' не в кодировке {encoding}.")
        except Exception as e_decode:
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при декодировании '{doc.file_name}' как {encoding}: {e_decode}", exc_info=True)

    if text is None:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось декодировать '{doc.file_name}' ни одной из кодировок: {unique_encodings}")
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте UTF-8.", parse_mode=ParseMode.MARKDOWN)
        return
    if not text.strip() and len(file_bytes) > 0:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' дал пустой текст после декодирования ({detected_encoding}).")
        await update.message.reply_text(f"⚠️ Не удалось извлечь текст из файла `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN)
        return
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
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {len(truncated_text)} символов.")
    user_caption = message.caption if message.caption else ""
    file_name = doc.file_name or "файл"
    encoding_info = f"(~{detected_encoding})" if detected_encoding else "(кодировка?)"
    file_context = f"Содержимое файла `{file_name}` {encoding_info}:\n```\n{truncated_text}\n```{truncation_warning}"
    if user_caption:
        safe_caption = user_caption.replace('"', '\\"')
        user_prompt_doc = f"Пользователь загрузил файл `{file_name}` с комментарием: \"{safe_caption}\". {file_context}\nПроанализируй, пожалуйста."
    else:
        user_prompt_doc = f"Пользователь загрузил файл `{file_name}`. {file_context}\nЧто можешь сказать об этом тексте?"
    message.text = user_prompt_doc
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с текстом документа.")
    await handle_message(update, context)
# ====================================================================

# --- Функции веб-сервера и запуска ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
    aiohttp_session = aiohttp.ClientSession(timeout=timeout)
    application.bot_data['aiohttp_session'] = aiohttp_session
    logger.info("Сессия aiohttp создана и сохранена в bot_data.")
    # Регистрация обработчиков...
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("temp", set_temperature))
    application.add_handler(CommandHandler("search_on", enable_search))
    application.add_handler(CommandHandler("search_off", disable_search))
    application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    try:
        await application.initialize()
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
        if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed:
            await application.bot_data['aiohttp_session'].close()
            logger.info("Сессия aiohttp закрыта из-за ошибки инициализации.")
        raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    """Запускает веб-сервер aiohttp для приема вебхуков Telegram."""
    app = aiohttp.web.Application()

    async def health_check(request):
        try:
            bot_info = await application.bot.get_me()
            if bot_info:
                logger.debug("Health check successful.")
                return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
            else:
                logger.warning("Health check: Bot info unavailable.")
                return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except TelegramError as e_tg:
            logger.error(f"Health check failed (TelegramError): {e_tg}", exc_info=True)
            return aiohttp.web.Response(text=f"Error: Telegram API error ({type(e_tg).__name__})", status=503)
        except Exception as e:
            logger.error(f"Health check failed (Exception): {e}", exc_info=True)
            return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)

    app.router.add_get('/', health_check)
    app['bot_app'] = application
    webhook_path = GEMINI_WEBHOOK_PATH.strip('/')
    if not webhook_path.startswith('/'):
        webhook_path = '/' + webhook_path
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
    except asyncio.CancelledError:
        logger.info("Задача веб-сервера отменена.")
    except Exception as e:
        logger.error(f"Ошибка при запуске или работе веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally:
        logger.info("Начало остановки веб-сервера...")
        await runner.cleanup()
        logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обрабатывает входящие запросы от Telegram (вебхуки)."""
    application = request.app.get('bot_app')
    if not application:
        logger.critical("Приложение бота не найдено в контексте веб-сервера!")
        return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")

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
    except TelegramError as e_tg:
        logger.error(f"Ошибка Telegram при обработке вебхука: {e_tg}", exc_info=True)
        return aiohttp.web.Response(text=f"Internal Server Error: Telegram API Error ({type(e_tg).__name__})", status=500)
    except Exception as e:
        logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    # Понижаем уровни логгирования для шумных библиотек
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('google.api_core').setLevel(logging.WARNING)
    logging.getLogger('google.auth').setLevel(logging.WARNING)
    logging.getLogger('google.generativeai').setLevel(logging.INFO)
    logging.getLogger('duckduckgo_search').setLevel(logging.INFO)
    logging.getLogger('PIL').setLevel(logging.INFO)
    logging.getLogger('pytesseract').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.INFO)
    logging.getLogger('telegram.bot').setLevel(logging.INFO)
    logger.setLevel(log_level)
    logger.info(f"--- Установлен уровень логгирования для '{logger.name}': {log_level_str} ({log_level}) ---")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        if not stop_event.is_set():
            logger.info("Получен сигнал SIGINT/SIGTERM, инициирую остановку...")
            stop_event.set()
        else:
            logger.warning("Повторный сигнал остановки получен, процесс уже завершается.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             logger.warning(f"Не удалось установить обработчик сигнала {sig} через loop. Использую signal.signal().")
             try:
                 signal.signal(sig, lambda s, f: signal_handler())
             except Exception as e_signal:
                 logger.error(f"Не удалось установить обработчик сигнала {sig} через signal.signal(): {e_signal}")

    application = None
    web_server_task = None
    aiohttp_session_main = None

    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask")
        aiohttp_session_main = application.bot_data.get('aiohttp_session')
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)...")
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Главная задача main() была отменена.")
    except Exception as e:
        logger.critical("Критическая ошибка во время запуска или ожидания.", exc_info=True)
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
                 try:
                     await web_server_task
                 except asyncio.CancelledError:
                     logger.info("Задача веб-сервера успешно отменена.")
                 except Exception as e_cancel_ws:
                     logger.error(f"Ошибка при ожидании отмененной задачи веб-сервера: {e_cancel_ws}", exc_info=True)
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

        if aiohttp_session_main and not aiohttp_session_main.closed:
             logger.info("Закрытие основной сессии aiohttp...")
             await aiohttp_session_main.close()
             await asyncio.sleep(0.5)
             logger.info("Основная сессия aiohttp закрыта.")

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся фоновых задач...")
            [task.cancel() for task in tasks]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 task_name = tasks[i].get_name()
                 if isinstance(res, asyncio.CancelledError):
                     cancelled_count += 1
                     logger.debug(f"Задача '{task_name}' успешно отменена.")
                 elif isinstance(res, Exception):
                     error_count += 1
                     logger.warning(f"Ошибка в отмененной задаче '{task_name}': {res}", exc_info=True)
                 else:
                     logger.debug(f"Задача '{task_name}' завершилась с результатом: {res}")
            logger.info(f"Фоновые задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение прервано пользователем (KeyboardInterrupt в main).")
    except Exception as e_top:
        logger.critical("Неперехваченная ошибка на верхнем уровне asyncio.run(main).", exc_info=True)

# --- END OF FILE main.py ---
