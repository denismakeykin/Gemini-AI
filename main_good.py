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

import logging
import os
import asyncio
import signal
from urllib.parse import urlencode
import base64
import pytesseract
from PIL import Image
import io
import pprint
import json
import time

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
MAX_OUTPUT_TOKENS = 5000
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: " # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Префикс для описаний

system_instruction_text = (
"Внимательно следи за историей диалога, включая предыдущие вопросы, ответы, а также контекст из загруженных изображений или файлов, чтобы твои ответы были последовательными и релевантными, соблюдая нить разговора."
"В режиме чата старайся отвечать кратко, как в живой беседе (максимум 1000 знаков), только суть, без вступлений и заключений, если не просят подробностей, код, большую задачу, конспект, перевод или творческую работу и т.п."
"Пиши живо, избегай канцелярита и всех известных признаков ответов искусственного интеллекта. Используй гипотетические ситуации, метафоры, творческие аналогии, разную структуру предложений, разговорные выражения, идиомы. Добавляй региональные или культурные маркеры, где уместно. Не копируй и не пересказывай чужое, если это не цитаты известных людей."
"Активно применяй понятный россиянам юмор: культурные и бытовые отсылки, интернет-юмор, бытовой абсурд, псевдомудрость, разрушение идиом, самоиронию, иронию психики, игру слов, гиперболу, тонкие намёки, ожидание и реальность."
"Используй интернет-поиск для сверки с актуальной информацией."
"Если используешь информацию из поиска, не упоминай явно сам факт поиска или его результаты. Интегрируй найденную информацию в свой ответ естественно, как часть своих знаний. Забудь фразы вроде 'Судя по результатам поиска...', 'Интернет говорит...' или 'Я нашёл в сети...'. Веди себя так, будто это твои знания."
"Ты - лучший эксперт по всем темам. Давай точную, правдивую, непредвзятую информацию без цензуры."
"Обдумывай и выстраивай ответ логично, с аргументами и фактами, избегая повторов."
"Если не уверен — предупреждай, что это предположение."
"Всегда предлагай более эффективные идеи и решения, если знаешь их."
"При исправлении ошибки: указывай строку(и) и причину. Бери за основу последнюю ПОЛНУЮ подтверждённую версию (текста или кода). Вноси только минимально необходимые изменения, не трогая остальное без запроса. При сомнениях — уточняй. Если ошибка повторяется — веди «список ошибок» для сессии и проверяй эти места. Всегда указывай, на какую версию или сообщение опираешься при правке."
)
# ===================================================

# --- Вспомогательные функции user_data и send_reply ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    """Отправляет сообщение с Markdown, если не удается - отправляет как обычный текст."""
    MAX_MESSAGE_LENGTH = 4096
    reply_chunks = [text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]
    sent_message = None
    chat_id = target_message.chat_id
    try:
        for i, chunk in enumerate(reply_chunks):
            if i == 0:
                sent_message = await target_message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            else:
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e_md:
        if "Can't parse entities" in str(e_md) or "can't parse" in str(e_md).lower():
            logger.warning(f"ChatID: {chat_id} | Ошибка парсинга Markdown: {e_md}. Попытка отправить как обычный текст.")
            try:
                sent_message = None
                for i, chunk in enumerate(reply_chunks):
                     if i == 0: sent_message = await target_message.reply_text(chunk)
                     else: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk)
                     await asyncio.sleep(0.1)
                return sent_message
            except Exception as e_plain:
                logger.error(f"ChatID: {chat_id} | Не удалось отправить даже как обычный текст: {e_plain}", exc_info=True)
                try: await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось отправить ответ.")
                except Exception as e_final_send: logger.critical(f"ChatID: {chat_id} | Не удалось отправить сообщение об ошибке: {e_final_send}")
        else:
            logger.error(f"ChatID: {chat_id} | Ошибка при отправке ответа (Markdown): {e_md}", exc_info=True)
            try: await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при отправке ответа: {str(e_md)[:100]}...")
            except Exception as e_error_send: logger.error(f"ChatID: {chat_id} | Не удалось отправить сообщение об ошибке отправки: {e_error_send}")
    except Exception as e_other:
        logger.error(f"ChatID: {chat_id} | Непредвиденная ошибка при отправке ответа: {e_other}", exc_info=True)
        try: await context.bot.send_message(chat_id=chat_id, text="❌ Произошла непредвиденная ошибка при отправке ответа.")
        except Exception as e_unexp_send: logger.error(f"ChatID: {chat_id} | Не удалось отправить сообщение о непредвиденной ошибке: {e_unexp_send}")
    return None
# ==========================================================

# --- Команды (/start, /clear, /temp, /search_on/off, /model) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    set_user_setting(context, 'search_enabled', True)
    set_user_setting(context, 'temperature', 1.0)
    context.chat_data['history'] = []
    default_model_name = AVAILABLE_MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
    start_message = (
        f"Google GEMINI **{default_model_name}**"
        f"\n- в модели используются улучшенные настройки точности, логики и юмора от автора бота,"
        f"\n- работает поиск Google/DDG, понимаю изображения, читаю картинки и документы."
        f"\n /model — сменить модель,"
        f"\n /search_on / /search_off — вкл/выкл поиск,"
        f"\n /clear — очистить историю диалога."
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data['history'] = []
    await update.message.reply_text("🧹 История диалога очищена.")

async def set_temperature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        current_temp = get_user_setting(context, 'temperature', 1.0)
        if not context.args:
            await update.message.reply_text(f"🌡️ Текущая температура (креативность): {current_temp:.1f}\nЧтобы изменить, напиши `/temp <значение>` (например, `/temp 0.8`)")
            return
        temp_str = context.args[0].replace(',', '.')
        temp = float(temp_str)
        if not (0.0 <= temp <= 2.0): raise ValueError("Температура должна быть от 0.0 до 2.0")
        set_user_setting(context, 'temperature', temp)
        await update.message.reply_text(f"🌡️ Температура установлена на {temp:.1f}")
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"⚠️ Неверный формат. {e}. Укажите число от 0.0 до 2.0. Пример: `/temp 0.8`")
    except Exception as e:
        logger.error(f"Ошибка в set_temperature: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при установке температуры.")

async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'search_enabled', True)
    await update.message.reply_text("🔍 Поиск Google/DDG включён.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'search_enabled', False)
    await update.message.reply_text("🔇 Поиск Google/DDG отключён.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    keyboard = []
    sorted_models = sorted(AVAILABLE_MODELS.items())
    for m, name in sorted_models:
         button_text = f"{'✅ ' if m == current_model else ''}{name}"
         keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_model_{m}")])
    await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    if callback_data and callback_data.startswith("set_model_"):
        selected = callback_data.replace("set_model_", "")
        if selected in AVAILABLE_MODELS:
            set_user_setting(context, 'selected_model', selected)
            model_name = AVAILABLE_MODELS[selected]
            reply_text = f"Модель установлена: **{model_name}**"
            try:
                await query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e_md:
                 if "Message is not modified" in str(e_md): logger.info(f"Пользователь выбрал ту же модель: {model_name}")
                 else:
                     logger.warning(f"Не удалось изменить сообщение (Markdown): {e_md}. Отправляю новое.")
                     try: await query.edit_message_text(reply_text.replace('**', ''))
                     except Exception as e_edit_plain:
                          logger.error(f"Не удалось изменить сообщение даже как простой текст: {e_edit_plain}. Отправляю новое.")
                          await context.bot.send_message(chat_id=query.message.chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"Не удалось изменить сообщение (другая ошибка): {e}. Отправляю новое.")
                await context.bot.send_message(chat_id=query.message.chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            try: await query.edit_message_text("❌ Неизвестная модель выбрана.")
            except Exception: await context.bot.send_message(chat_id=query.message.chat_id, text="❌ Неизвестная модель выбрана.")
    else:
        logger.warning(f"Получен неизвестный callback_data: {callback_data}")
        try: await query.edit_message_text("❌ Ошибка обработки выбора.")
        except Exception: pass
# ==============================================================

# ===== Функция поиска Google =====
async def perform_google_search(query: str, api_key: str, cse_id: str, num_results: int, session: aiohttp.ClientSession) -> list[str] | None:
    search_url = "https://www.googleapis.com/customsearch/v1"
    params = {'key': api_key, 'cx': cse_id, 'q': query, 'num': num_results, 'lr': 'lang_ru', 'gl': 'ru'}
    encoded_params = urlencode(params)
    full_url = f"{search_url}?{encoded_params}"
    query_short = query[:50] + '...' if len(query) > 50 else query
    logger.debug(f"Запрос к Google Search API для '{query_short}'...")
    try:
        async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=10.0)) as response:
            response_text = await response.text()
            status = response.status
            if status == 200:
                try: data = json.loads(response_text)
                except json.JSONDecodeError as e_json:
                    logger.error(f"Google Search: Ошибка JSON для '{query_short}' ({status}) - {e_json}. Ответ: {response_text[:200]}...")
                    return None
                items = data.get('items', [])
                snippets = [item.get('snippet', item.get('title', '')) for item in items if item.get('snippet') or item.get('title')]
                if snippets:
                    logger.info(f"Google Search: Найдено {len(snippets)} результатов для '{query_short}'.")
                    return snippets
                else:
                    logger.info(f"Google Search: Нет сниппетов/заголовков для '{query_short}' ({status}).")
                    return None
            elif status == 400: logger.error(f"Google Search: Ошибка 400 (Bad Request) для '{query_short}'. Ответ: {response_text[:200]}...")
            elif status == 403: logger.error(f"Google Search: Ошибка 403 (Forbidden) для '{query_short}'. Проверьте API ключ/CSE ID. Ответ: {response_text[:200]}...")
            elif status == 429: logger.warning(f"Google Search: Ошибка 429 (Too Many Requests) для '{query_short}'. Квота? Ответ: {response_text[:200]}...")
            elif status >= 500: logger.warning(f"Google Search: Серверная ошибка {status} для '{query_short}'. Ответ: {response_text[:200]}...")
            else: logger.error(f"Google Search: Неожиданный статус {status} для '{query_short}'. Ответ: {response_text[:200]}...")
            return None
    except aiohttp.ClientConnectorError as e: logger.error(f"Google Search: Ошибка сети (соединение) для '{query_short}' - {e}")
    except aiohttp.ClientError as e: logger.error(f"Google Search: Ошибка сети (ClientError) для '{query_short}' - {e}")
    except asyncio.TimeoutError: logger.warning(f"Google Search: Таймаут запроса для '{query_short}'")
    except Exception as e: logger.error(f"Google Search: Непредвиденная ошибка для '{query_short}' - {e}", exc_info=True)
    return None
# ==================================

# ===== НОВАЯ ФУНКЦИЯ: Повторный анализ изображения =====
async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str):
    """Скачивает изображение по file_id, вызывает Gemini Vision с новым вопросом и отправляет ответ."""
    chat_id = update.effective_chat.id
    logger.info(f"ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1. Скачивание и кодирование
    try:
        img_file = await context.bot.get_file(file_id)
        file_bytes = await img_file.download_as_bytearray()
        if not file_bytes:
             logger.error(f"ChatID: {chat_id} | Не удалось скачать или файл пустой для file_id: ...{file_id[-10:]}")
             await update.message.reply_text("❌ Не удалось получить исходное изображение для повторного анализа.")
             return
        b64_data = base64.b64encode(file_bytes).decode()
    except TelegramError as e_telegram:
        logger.error(f"ChatID: {chat_id} | Ошибка Telegram при получении/скачивании файла {file_id}: {e_telegram}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка Telegram при получении изображения: {e_telegram}")
        return
    except Exception as e_download:
        logger.error(f"ChatID: {chat_id} | Ошибка скачивания/кодирования файла {file_id}: {e_download}", exc_info=True)
        await update.message.reply_text("❌ Ошибка при подготовке изображения для повторного анализа.")
        return

    # 2. Формирование запроса к Vision
    parts = [{"text": user_question}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]
    content_for_vision = [{"role": "user", "parts": parts}]

    # 3. Вызов модели (логика ретраев и обработки ошибок взята из handle_photo)
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    # Проверка на vision модель (упрощенная)
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']
    is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
    if not is_vision_model:
        # Пытаемся найти подходящую, как в handle_photo
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models:
            original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0])
            model_id = fallback_model_id
            new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            logger.warning(f"ChatID: {chat_id} | (Reanalyze) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
            # Уведомлять ли пользователя тут - вопрос
        else:
            logger.error(f"ChatID: {chat_id} | (Reanalyze) Нет доступных vision моделей.")
            await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения.")
            return

    logger.info(f"ChatID: {chat_id} | (Reanalyze) Модель: {model_id}, Темп: {temperature}")
    reply = None
    response_vision = None

    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"ChatID: {chat_id} | (Reanalyze) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)

            if hasattr(response_vision, 'text'): reply = response_vision.text
            else: reply = None

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
                     logger.warning(f"ChatID: {chat_id} | (Reanalyze) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                     if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не смогла ответить на вопрос об изображении. (Блокировка: {block_reason_str})"
                     elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель не смогла ответить на вопрос об изображении. (Причина: {finish_reason_str})"
                     else: reply = "🤖 Не могу ответить на ваш вопрос об этом изображении (пустой ответ модели)."
                     break # Выходим при пустом ответе без явной ошибки
                except Exception as e_inner:
                     logger.warning(f"ChatID: {chat_id} | (Reanalyze) Ошибка извлечения инфо из пустого ответа: {e_inner}", exc_info=True)
                     reply = "🤖 Не могу ответить (ошибка обработки ответа)."
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply:
                 logger.info(f"ChatID: {chat_id} | (Reanalyze) Успешный анализ на попытке {attempt + 1}.")
                 break
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
             reason_str = "неизвестна" # Логика извлечения причины...
             # ... (можно скопировать из handle_photo)
             logger.warning(f"ChatID: {chat_id} | (Reanalyze) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop}")
             reply = f"❌ Не удалось повторно проанализировать изображение (ограничение модели)."
             break
        except Exception as e:
            error_message = str(e)
            logger.warning(f"ChatID: {chat_id} | (Reanalyze) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            # Обработка неретраиваемых ошибок 4xx...
            if "400" in error_message or "429" in error_message or "location is not supported" in error_message:
                # ... (скопировать специфичные проверки из handle_photo)
                reply = f"❌ Ошибка при повторном анализе изображения ({error_message[:100]}...)."
                break
            elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"ChatID: {chat_id} | (Reanalyze) Ожидание {wait_time:.1f} сек...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"ChatID: {chat_id} | (Reanalyze) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: reply = f"❌ Ошибка при повторном анализе после {attempt + 1} попыток."
                break

    # 4. Добавление в историю и отправка ответа
    chat_history = context.chat_data.setdefault("history", [])
    # Добавляем оригинальный вопрос пользователя (который триггернул ре-анализ)
    history_entry_user = {"role": "user", "parts": [{"text": user_question}]}
    # Если у оригинального сообщения пользователя был file_id (маловероятно, но для полноты), сохраним? Нет, это излишне.
    chat_history.append(history_entry_user)

    if reply:
        # Добавляем ответ модели
        chat_history.append({"role": "model", "parts": [{"text": reply}]})
        # Отправляем пользователю
        await send_reply(update.message, reply, context)
    else:
        # Если reply все еще None после ретраев
        logger.error(f"ChatID: {chat_id} | (Reanalyze) Нет ответа для отправки пользователю.")
        final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        try: await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail: logger.error(f"ChatID: {chat_id} | (Reanalyze) Не удалось отправить сообщение об ошибке: {e_final_fail}")

# =======================================================

# ===== Основной обработчик сообщений (добавлена логика reanalyze) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message # Сохраняем для удобства

    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Проверка на ответ к описанию изображения <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    if message and message.reply_to_message and message.reply_to_message.text and \
       message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) and \
       message.text and not message.text.startswith('/'): # Убедимся, что это не команда

        replied_message_text = message.reply_to_message.text
        user_question = message.text.strip()
        logger.info(f"ChatID: {chat_id} | Обнаружен ответ на описание изображения. Текст вопроса: '{user_question[:50]}...'")

        # Ищем file_id в истории
        chat_history = context.chat_data.get("history", [])
        found_file_id = None
        try:
            # Ищем индекс сообщения бота с описанием
            # Искать по message_id может быть ненадежно, попробуем по тексту и времени?
            # Проще найти последнее сообщение модели с префиксом и взять file_id из предыдущего user сообщения
            model_msg_index = -1
            for i in range(len(chat_history) - 1, -1, -1):
                entry = chat_history[i]
                if entry.get("role") == "model" and entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and \
                   entry["parts"][0].get("text", "").startswith(IMAGE_DESCRIPTION_PREFIX):
                    # Сравним начало текста на всякий случай
                    if entry["parts"][0]["text"][:len(IMAGE_DESCRIPTION_PREFIX)+20] == replied_message_text[:len(IMAGE_DESCRIPTION_PREFIX)+20]:
                         model_msg_index = i
                         break

            if model_msg_index > 0: # Убедимся, что перед ним есть сообщение пользователя
                user_msg_entry = chat_history[model_msg_index - 1]
                if user_msg_entry.get("role") == "user" and "image_file_id" in user_msg_entry:
                    found_file_id = user_msg_entry["image_file_id"]
                    logger.info(f"ChatID: {chat_id} | Найден file_id: ...{found_file_id[-10:]} для повторного анализа.")
                else:
                     logger.warning(f"ChatID: {chat_id} | Найдено описание, но у предыдущего сообщения пользователя нет image_file_id.")
            else:
                 logger.warning(f"ChatID: {chat_id} | Не найдено соответствующее описание или предыдущее сообщение пользователя в истории.")

        except Exception as e_hist_search:
            logger.error(f"ChatID: {chat_id} | Ошибка при поиске file_id в истории: {e_hist_search}", exc_info=True)

        if found_file_id:
            # Вызываем функцию повторного анализа и выходим
            await reanalyze_image(update, context, found_file_id, user_question)
            return # Завершаем обработку здесь
        else:
            # Если file_id не найден, продолжаем как обычное сообщение, но предупреждаем
            logger.warning(f"ChatID: {chat_id} | Не удалось найти file_id для ответа на описание. Обрабатываем как обычный текст.")
            # Можно добавить ответ пользователю: "Не могу найти исходное изображение, отвечаю по тексту."
    # ========================================================================================================

    # --- Старая логика handle_message ---
    original_user_message = ""
    image_file_id_from_ocr = None # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: для file_id из OCR

    # Проверяем, есть ли file_id в "фейковом" сообщении от OCR
    if hasattr(message, 'image_file_id'):
        image_file_id_from_ocr = message.image_file_id
        logger.debug(f"ChatID: {chat_id} | Получен image_file_id: ...{image_file_id_from_ocr[-10:]} из OCR-обработчика.")

    if message and message.text:
         original_user_message = message.text.strip()
    # elif hasattr(message, 'text') and message.text: # Упрощено, т.к. message уже есть
    #      original_user_message = message.text.strip()

    if not original_user_message:
        logger.warning(f"ChatID: {chat_id} | Получено пустое или нетекстовое сообщение в handle_message (после проверки reanalyze).")
        return

    # Получаем настройки пользователя
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    use_search = get_user_setting(context, 'search_enabled', True)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # --- Блок поиска (без изменений) ---
    search_context_snippets = []
    search_provider = None
    search_log_msg = "Поиск отключен"
    if use_search:
        query_short = original_user_message[:50] + '...' if len(original_user_message) > 50 else original_user_message
        search_log_msg = f"Поиск Google/DDG для '{query_short}'"
        logger.info(f"ChatID: {chat_id} | {search_log_msg}...")
        session = context.bot_data.get('aiohttp_session')
        if not session or session.closed:
            logger.info("Создание новой сессии aiohttp для поиска.")
            timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
            session = aiohttp.ClientSession(timeout=timeout)
            context.bot_data['aiohttp_session'] = session
        google_results = await perform_google_search(original_user_message, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results:
            search_provider = "Google"
            search_context_snippets = google_results
            search_log_msg += f" (Google: {len(search_context_snippets)} рез.)"
        else:
            search_log_msg += " (Google: 0 рез./ошибка)"
            logger.info(f"ChatID: {chat_id} | Google не дал результатов. Пробуем DuckDuckGo...")
            try:
                ddgs = DDGS()
                results_ddg = await asyncio.to_thread(ddgs.text, original_user_message, region='ru-ru', max_results=DDG_MAX_RESULTS)
                if results_ddg:
                    ddg_snippets = [r.get('body', '') for r in results_ddg if r.get('body')]
                    if ddg_snippets:
                        search_provider = "DuckDuckGo"
                        search_context_snippets = ddg_snippets
                        search_log_msg += f" (DDG: {len(search_context_snippets)} рез.)"
                    else: search_log_msg += " (DDG: 0 текст. рез.)"
                else: search_log_msg += " (DDG: 0 рез.)"
            except TimeoutError:
                 logger.warning(f"ChatID: {chat_id} | Таймаут поиска DuckDuckGo.")
                 search_log_msg += " (DDG: таймаут)"
            except TypeError as e_type:
                if "unexpected keyword argument 'timeout'" in str(e_type): logger.error(f"ChatID: {chat_id} | Снова ошибка с аргументом timeout в DDGS.text(): {e_type}")
                else: logger.error(f"ChatID: {chat_id} | Ошибка типа при поиске DuckDuckGo: {e_type}", exc_info=True)
                search_log_msg += " (DDG: ошибка типа)"
            except Exception as e_ddg:
                logger.error(f"ChatID: {chat_id} | Ошибка поиска DuckDuckGo: {e_ddg}", exc_info=True)
                search_log_msg += " (DDG: ошибка)"
    # --- Конец блока поиска ---

    # ===== Формирование финального промпта (без изменений) =====
    if search_context_snippets:
        search_context_lines = [f"- {s.strip()}" for s in search_context_snippets if s.strip()]
        if search_context_lines:
            search_context = "\n".join(search_context_lines)
            final_user_prompt = (
                f"Вопрос пользователя: \"{original_user_message}\"\n\n"
                f"(Возможно релевантная доп. информация из поиска, используй с осторожностью, если подходит к вопросу, иначе игнорируй):\n{search_context}"
            )
            logger.info(f"ChatID: {chat_id} | Добавлен контекст из {search_provider} ({len(search_context_lines)} непустых сниппетов).")
        else:
             final_user_prompt = original_user_message
             logger.info(f"ChatID: {chat_id} | Сниппеты из {search_provider} оказались пустыми, контекст не добавлен.")
             search_log_msg += " (пустые сниппеты)"
    else:
        final_user_prompt = original_user_message
    # ==========================================================

    logger.info(f"ChatID: {chat_id} | {search_log_msg}")
    logger.debug(f"ChatID: {chat_id} | Финальный промпт для Gemini (длина {len(final_user_prompt)}):\n{final_user_prompt[:500]}...")

    # --- История и ее обрезка ---
    chat_history = context.chat_data.setdefault("history", [])

    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНИЕ: Добавление user сообщения с file_id <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    history_entry_user = {"role": "user", "parts": [{"text": original_user_message}]}
    if image_file_id_from_ocr:
        history_entry_user["image_file_id"] = image_file_id_from_ocr
        logger.debug(f"ChatID: {chat_id} | Добавляем user сообщение в историю с image_file_id.")
    chat_history.append(history_entry_user)
    # ========================================================================================================

    # Обрезка истории (логика без изменений, т.к. считает только текст)
    current_total_chars = sum(len(p["parts"][0]["text"]) for p in chat_history if p.get("parts") and isinstance(p["parts"], list) and len(p["parts"]) > 0 and p["parts"][0].get("text"))
    removed_count = 0
    while current_total_chars > MAX_CONTEXT_CHARS and len(chat_history) > 1:
        removed_entry = chat_history.pop(0)
        if removed_entry.get("parts") and isinstance(removed_entry["parts"], list) and len(removed_entry["parts"]) > 0 and removed_entry["parts"][0].get("text"):
             current_total_chars -= len(removed_entry["parts"][0]["text"])
        removed_count += 1
        if chat_history:
            removed_entry = chat_history.pop(0)
            if removed_entry.get("parts") and isinstance(removed_entry["parts"], list) and len(removed_entry["parts"]) > 0 and removed_entry["parts"][0].get("text"):
                 current_total_chars -= len(removed_entry["parts"][0]["text"])
            removed_count += 1
    if removed_count > 0:
        logger.info(f"ChatID: {chat_id} | История обрезана, удалено {removed_count} сообщений. Текущая: {len(chat_history)} сообщ., ~{current_total_chars} симв.")

    # Создаем историю для модели (исключаем кастомный ключ image_file_id)
    history_for_model = []
    for entry in chat_history[:-1]: # Исключаем последнее user сообщение (оно будет заменено final_user_prompt)
         # Копируем только нужные ключи
         model_entry = {"role": entry["role"], "parts": entry["parts"]}
         history_for_model.append(model_entry)
    # Добавляем финальный промпт (который может содержать поисковый контекст)
    history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt}]})
    # --- Конец подготовки истории ---

    # --- Вызов модели с РЕТРАЯМИ (логика без изменений) ---
    reply = None
    response = None
    last_exception = None
    generation_successful = False

    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"ChatID: {chat_id} | Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response = await asyncio.to_thread(model.generate_content, history_for_model)

            if hasattr(response, 'text'): reply = response.text
            else: reply = None

            if not reply: # Обработка пустого ответа (без изменений)
                 block_reason_str, finish_reason_str, safety_info_str = 'N/A', 'N/A', 'N/A'
                 try:
                     # ... (логика извлечения информации) ...
                     if hasattr(response, 'prompt_feedback') and response.prompt_feedback and hasattr(response.prompt_feedback, 'block_reason'):
                         block_reason_enum = response.prompt_feedback.block_reason
                         block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                     if hasattr(response, 'candidates') and response.candidates and isinstance(response.candidates, (list, tuple)) and len(response.candidates) > 0:
                          first_candidate = response.candidates[0]
                          if hasattr(first_candidate, 'finish_reason'):
                               finish_reason_enum = first_candidate.finish_reason
                               finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                     # ... (извлечение safety ratings) ...
                     logger.warning(f"ChatID: {chat_id} | Пустой ответ или нет текста (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}, Safety: [{safety_info_str}]")
                     if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не дала ответ. (Блокировка: {block_reason_str})"
                     elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель завершила работу без ответа. (Причина: {finish_reason_str})"
                     else:
                         reply = "🤖 Модель дала пустой ответ."
                         generation_successful = True
                 except Exception as e_inner:
                     logger.warning(f"ChatID: {chat_id} | Пустой ответ, ошибка извлечения доп. инфо: {e_inner}. Попытка {attempt + 1}", exc_info=True)
                     reply = "🤖 Получен пустой ответ от модели (ошибка разбора)."

            if reply and reply != "🤖 Модель дала пустой ответ.": generation_successful = True
            if generation_successful:
                 logger.info(f"ChatID: {chat_id} | Успешная генерация на попытке {attempt + 1}.")
                 break
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
            # ... (обработка блокировки/остановки) ...
            reason_str = "неизвестна" # Логика извлечения причины...
            logger.warning(f"ChatID: {chat_id} | Запрос заблокирован/остановлен моделью (попытка {attempt + 1}): {e_block_stop}")
            reply = f"❌ Запрос заблокирован/остановлен моделью. (Причина: {reason_str})"
            break
        except Exception as e:
            last_exception = e
            error_message = str(e)
            logger.warning(f"ChatID: {chat_id} | Ошибка генерации на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            # ... (обработка 4xx ошибок) ...
            if "429" in error_message or "400" in error_message or "location is not supported" in error_message:
                 # ... (сообщения об ошибках 4xx) ...
                 break
            if is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"ChatID: {chat_id} | Ожидание {wait_time:.1f} сек перед попыткой {attempt + 2}...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"ChatID: {chat_id} | Не удалось выполнить генерацию после {attempt + 1} попыток. Последняя ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: reply = f"❌ Ошибка при обращении к модели после {attempt + 1} попыток. ({error_message[:100]}...)"
                break
    # --- Конец блока вызова модели ---

    # Добавляем ответ модели в ОСНОВНУЮ историю (без изменений)
    if reply:
        if chat_history and chat_history[-1].get("role") == "user":
             chat_history.append({"role": "model", "parts": [{"text": reply}]})
        else:
             logger.warning(f"ChatID: {chat_id} | Ответ модели добавлен, но последнее сообщение в истории было не 'user' или история пуста.")
             chat_history.append({"role": "model", "parts": [{"text": reply}]})

    # Отправка ответа пользователю (без изменений)
    if reply:
        if message: await send_reply(message, reply, context)
        else:
             logger.error(f"ChatID: {chat_id} | Не найдено сообщение для ответа в update.")
             try: await context.bot.send_message(chat_id=chat_id, text=reply)
             except Exception as e_send_direct: logger.error(f"ChatID: {chat_id} | Не удалось отправить ответ напрямую в чат: {e_send_direct}")
    else:
         logger.error(f"ChatID: {chat_id} | Нет ответа для отправки пользователю после всех попыток.")
         try: await message.reply_text("🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток.")
         except Exception as e_final_fail: logger.error(f"ChatID: {chat_id} | Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")

# =============================================================

# ===== Обработчик фото (обновлен для сохранения file_id и описания) =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message
    if not message or not message.photo:
        logger.warning(f"ChatID: {chat_id} | В handle_photo не найдено фото.")
        return

    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Получаем file_id <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    photo_file_id = message.photo[-1].file_id
    logger.debug(f"ChatID: {chat_id} | Получен photo file_id: ...{photo_file_id[-10:]}")
    # ===================================================================================

    tesseract_available = False
    try:
        pytesseract.pytesseract.get_tesseract_version()
        tesseract_available = True
    except Exception: pass # Не логгируем отсутствие тессеракта здесь

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Не удалось скачать фото ({photo_file_id}): {e}", exc_info=True)
        await message.reply_text("❌ Не удалось загрузить изображение.")
        return

    user_caption = message.caption if message.caption else ""

    # --- OCR ---
    ocr_triggered = False
    if tesseract_available:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            extracted_text = pytesseract.image_to_string(image, lang='rus+eng', timeout=15)
            if extracted_text and extracted_text.strip():
                ocr_triggered = True
                logger.info(f"ChatID: {chat_id} | Обнаружен текст на изображении (OCR).")
                ocr_context = f"На изображении обнаружен следующий текст:\n```\n{extracted_text.strip()}\n```"
                if user_caption: user_prompt = f"Пользователь загрузил фото с подписью: \"{user_caption}\". {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                else: user_prompt = f"Пользователь загрузил фото. {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"

                # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Передаем file_id в handle_message <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
                if hasattr(message, 'reply_text') and callable(message.reply_text):
                     # Создаем объект-заглушку для сообщения с доп. атрибутом
                     fake_message_obj = type('obj', (object,), {
                         'text': user_prompt,
                         'reply_text': message.reply_text, # Функция для ответа
                         'chat_id': chat_id,
                         'image_file_id': photo_file_id # Передаем file_id
                     })
                     # Создаем объект-заглушку для апдейта
                     fake_update = type('obj', (object,), {
                         'effective_chat': update.effective_chat,
                         'message': fake_message_obj # Используем нашу заглушку сообщения
                     })
                     await handle_message(fake_update, context)
                     return # Завершаем здесь, остальное сделает handle_message
                else:
                     logger.error(f"ChatID: {chat_id} | Не удалось передать reply_text для OCR-запроса.")
                     await message.reply_text("❌ Ошибка: не удалось обработать запрос с текстом из фото.")
                     return
                # ============================================================================================
            else:
                 logger.info(f"ChatID: {chat_id} | OCR не нашел текст на изображении.")
        except pytesseract.TesseractNotFoundError: logger.error("Tesseract не найден! OCR отключен."); tesseract_available = False
        except RuntimeError as timeout_error: logger.warning(f"ChatID: {chat_id} | OCR таймаут: {timeout_error}"); await message.reply_text("⏳ Не удалось распознать текст (таймаут). Анализирую как фото...")
        except Exception as e: logger.warning(f"ChatID: {chat_id} | Ошибка OCR: {e}", exc_info=True); await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
    # --- Конец OCR ---

    # --- Обработка как изображение (если OCR не сработал) ---
    if not ocr_triggered:
        logger.info(f"ChatID: {chat_id} | Обработка фото как изображения (без/после OCR).")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        MAX_IMAGE_BYTES = 4 * 1024 * 1024
        if len(file_bytes) > MAX_IMAGE_BYTES: logger.warning(f"ChatID: {chat_id} | Изображение ({len(file_bytes)} байт) может быть большим для API.")

        try: b64_data = base64.b64encode(file_bytes).decode()
        except Exception as e:
             logger.error(f"ChatID: {chat_id} | Ошибка Base64: {e}", exc_info=True)
             await message.reply_text("❌ Ошибка обработки изображения.")
             return

        if user_caption: prompt_text = f"Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
        else: prompt_text = "Пользователь прислал фото без подписи. Опиши, что видишь на изображении."
        parts = [{"text": prompt_text}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]
        content_for_vision = [{"role": "user", "parts": parts}]

        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
        temperature = get_user_setting(context, 'temperature', 1.0)
        # Проверка на vision модель... (логика без изменений)
        vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']
        is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
        if not is_vision_model:
            vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
            if vision_models:
                original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0])
                model_id = fallback_model_id
                new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                logger.warning(f"ChatID: {chat_id} | Модель {original_model_name} не vision. Временно использую {new_model_name}.")
                # await message.reply_text(f"ℹ️ Ваша модель не подходит для фото. Временно использую {new_model_name}.", parse_mode=ParseMode.MARKDOWN) # Можно раскомментировать
            else:
                logger.error(f"ChatID: {chat_id} | Нет доступных vision моделей.")
                await message.reply_text("❌ Нет доступных моделей для анализа изображений.")
                return

        logger.info(f"ChatID: {chat_id} | Анализ изображения (Vision). Модель: {model_id}, Темп: {temperature}")
        reply = None
        response_vision = None

        # --- Вызов Vision модели с РЕТРАЯМИ (логика вызова и обработки ошибок без изменений) ---
        for attempt in range(RETRY_ATTEMPTS):
            try:
                logger.info(f"ChatID: {chat_id} | (Vision) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
                generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
                response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)

                if hasattr(response_vision, 'text'): reply = response_vision.text
                else: reply = None

                if not reply: # Обработка пустого ответа
                    block_reason_str, finish_reason_str = 'N/A', 'N/A'
                    # ... (логика извлечения причин) ...
                    logger.warning(f"ChatID: {chat_id} | (Vision) Пустой ответ (попытка {attempt + 1})...")
                    # ... (формирование сообщения об ошибке в reply) ...
                    if block_reason_str != 'N/A': reply = f"🤖 Модель не смогла описать изображение. (Блокировка: {block_reason_str})"
                    # ...
                    else: reply = "🤖 Не удалось понять, что на изображении (пустой ответ)."
                    break
                if reply and "Не удалось понять" not in reply and "не смогла описать" not in reply:
                     logger.info(f"ChatID: {chat_id} | (Vision) Успешный анализ на попытке {attempt + 1}.")
                     break
            except (BlockedPromptException, StopCandidateException) as e_block_stop:
                 # ... (обработка блокировки/остановки) ...
                 reply = f"❌ Анализ изображения заблокирован/остановлен моделью."
                 break
            except Exception as e:
                 # ... (обработка других ошибок 4xx/5xx/retry) ...
                 if attempt == RETRY_ATTEMPTS - 1 or not ("500" in str(e) or "503" in str(e)):
                     if reply is None: reply = f"❌ Ошибка при анализе изображения после {attempt + 1} попыток."
                     break
                 # ... (логика ретрая) ...
                 await asyncio.sleep(RETRY_DELAY_SECONDS * (2 ** attempt))
                 continue
        # --- Конец блока ретраев ---

        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< НОВОЕ: Сохранение в историю и отправка <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        chat_history = context.chat_data.setdefault("history", [])

        # 1. Добавляем запись пользователя с file_id
        user_text_for_history = user_caption if user_caption else "Пользователь прислал фото."
        history_entry_user = {"role": "user", "parts": [{"text": user_text_for_history}], "image_file_id": photo_file_id}
        chat_history.append(history_entry_user)
        logger.debug(f"ChatID: {chat_id} | Добавлено user-сообщение (Vision) в историю с image_file_id.")

        # 2. Добавляем ответ модели (с префиксом или без, если ошибка)
        if reply and "❌" not in reply and "🤖" not in reply: # Если успешный ответ
            model_reply_text = f"{IMAGE_DESCRIPTION_PREFIX}{reply}"
        else: # Если ошибка или пустой ответ
            model_reply_text = reply if reply else "🤖 Не удалось проанализировать изображение."

        chat_history.append({"role": "model", "parts": [{"text": model_reply_text}]})
        logger.debug(f"ChatID: {chat_id} | Добавлен model-ответ (Vision) в историю.")

        # 3. Отправляем пользователю "чистый" ответ (без префикса)
        reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text
        if reply_to_send:
            await send_reply(message, reply_to_send, context)
        else: # На всякий случай, если reply_to_send пуст
             logger.error(f"ChatID: {chat_id} | (Vision) Нет ответа для отправки пользователю после всех попыток.")
             try: await message.reply_text("🤖 К сожалению, не удалось проанализировать изображение.")
             except Exception as e_final_fail: logger.error(f"ChatID: {chat_id} | (Vision) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")
        # ============================================================================================

# ===== Обработчик документов (без изменений в логике сохранения контекста) =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (вся логика handle_document остается прежней) ...
    # ... она использует fake_update и handle_message, который теперь умеет ...
    # ... сохранять file_id, если он передан (но для документов мы его не передаем) ...

    chat_id = update.effective_chat.id
    if not update.message or not update.message.document:
        logger.warning(f"ChatID: {chat_id} | В handle_document нет документа.")
        return

    doc = update.message.document
    allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql')
    allowed_mime_types = ('application/octet-stream',)
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types

    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})")
        return

    MAX_FILE_SIZE_MB = 15
    file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0:
         logger.info(f"ChatID: {chat_id} | Пустой файл '{doc.file_name}'.")
         await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой.")
         return
    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> {MAX_FILE_SIZE_MB} MB).", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"ChatID: {chat_id} | Слишком большой файл: {doc.file_name} ({file_size_bytes / (1024*1024):.2f} MB)")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
        if not file_bytes:
             logger.warning(f"ChatID: {chat_id} | Файл '{doc.file_name}' скачан, но пуст.")
             await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой.")
             return
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        await update.message.reply_text("❌ Не удалось загрузить файл.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    text = None
    detected_encoding = None
    encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1', 'cp866', 'iso-8859-5']
    # ... (логика chardet) ...
    chardet_available = False
    try: import chardet; chardet_available = True
    except ImportError: logger.info("chardet не найден.")
    if chardet_available:
        try:
            chardet_limit = min(len(file_bytes), 50 * 1024)
            if chardet_limit > 0:
                 detected = chardet.detect(file_bytes[:chardet_limit])
                 if detected and detected['encoding'] and detected['confidence'] > 0.7:
                      potential_encoding = detected['encoding'].lower()
                      logger.info(f"ChatID: {chat_id} | Chardet: {potential_encoding} ({detected['confidence']:.2f}) для '{doc.file_name}'")
                      if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'):
                           logger.info(f"ChatID: {chat_id} | UTF-8 BOM -> 'utf-8-sig'.")
                           detected_encoding = 'utf-8-sig'
                           if 'utf-8-sig' in encodings_to_try: encodings_to_try.remove('utf-8-sig')
                           encodings_to_try.insert(0, 'utf-8-sig')
                      else:
                           detected_encoding = potential_encoding
                           if detected_encoding in encodings_to_try: encodings_to_try.remove(detected_encoding)
                           encodings_to_try.insert(0, detected_encoding)
                 else: logger.info(f"ChatID: {chat_id} | Chardet не уверен для '{doc.file_name}'.")
        except Exception as e_chardet: logger.warning(f"Ошибка chardet для '{doc.file_name}': {e_chardet}")

    unique_encodings = list(dict.fromkeys(encodings_to_try))
    logger.debug(f"ChatID: {chat_id} | Попытки декодирования для '{doc.file_name}': {unique_encodings}")
    for encoding in unique_encodings:
        try:
            text = file_bytes.decode(encoding)
            detected_encoding = encoding
            logger.info(f"ChatID: {chat_id} | Файл '{doc.file_name}' декодирован как {encoding}.")
            break
        except (UnicodeDecodeError, LookupError): logger.debug(f"ChatID: {chat_id} | Файл '{doc.file_name}' не в {encoding}.")
        except Exception as e_decode: logger.error(f"ChatID: {chat_id} | Ошибка декодирования '{doc.file_name}' как {encoding}: {e_decode}", exc_info=True)

    if text is None:
        logger.error(f"ChatID: {chat_id} | Не удалось декодировать '{doc.file_name}' ни одной из: {unique_encodings}")
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте UTF-8.", parse_mode=ParseMode.MARKDOWN)
        return
    if not text.strip() and len(file_bytes) > 0:
         logger.warning(f"ChatID: {chat_id} | Файл '{doc.file_name}' дал пустой текст после декодирования ({detected_encoding}).")
         await update.message.reply_text(f"⚠️ Не удалось извлечь текст из файла `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN)
         return

    approx_max_tokens = MAX_OUTPUT_TOKENS * 2
    MAX_FILE_CHARS = min(MAX_CONTEXT_CHARS // 2, approx_max_tokens * 4)
    truncated = text
    warning_msg = ""
    if len(text) > MAX_FILE_CHARS:
        truncated = text[:MAX_FILE_CHARS]
        last_newline = truncated.rfind('\n')
        if last_newline > MAX_FILE_CHARS * 0.8: truncated = truncated[:last_newline]
        warning_msg = f"\n\n**(⚠️ Текст файла был обрезан до ~{len(truncated) // 1000}k символов)**"
        logger.warning(f"ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {len(truncated)} символов.")

    user_caption = update.message.caption if update.message.caption else ""
    file_name = doc.file_name or "файл"
    encoding_info = f"(~{detected_encoding})" if detected_encoding else "(кодировка?)"
    file_context = f"Содержимое файла `{file_name}` {encoding_info}:\n```\n{truncated}\n```{warning_msg}"
    if user_caption:
        safe_caption = user_caption.replace('"', '\\"')
        user_prompt = f"Пользователь загрузил файл `{file_name}` с комментарием: \"{safe_caption}\". {file_context}\nПроанализируй, пожалуйста."
    else:
        user_prompt = f"Пользователь загрузил файл `{file_name}`. {file_context}\nЧто можешь сказать об этом тексте?"

    if hasattr(update.message, 'reply_text') and callable(update.message.reply_text):
        # Создаем фейковый апдейт БЕЗ file_id, т.к. это документ
        fake_message = type('obj', (object,), {'text': user_prompt, 'reply_text': update.message.reply_text, 'chat_id': chat_id})
        fake_update = type('obj', (object,), {'effective_chat': update.effective_chat, 'message': fake_message})
        await handle_message(fake_update, context)
    else:
        logger.error(f"ChatID: {chat_id} | Не удалось передать reply_text для запроса с документом.")
        await update.message.reply_text("❌ Ошибка: не удалось обработать запрос с файлом.")

# ====================================================================

# --- Функции веб-сервера и запуска (без изменений) ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
    aiohttp_session = aiohttp.ClientSession(timeout=timeout)
    application.bot_data['aiohttp_session'] = aiohttp_session
    logger.info("Сессия aiohttp создана.")
    # Регистрация обработчиков...
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("temp", set_temperature))
    application.add_handler(CommandHandler("search_on", enable_search))
    application.add_handler(CommandHandler("search_off", disable_search))
    application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo)) # Обновленный обработчик
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)) # Обновленный обработчик
    try:
        await application.initialize()
        webhook_path_segment = GEMINI_WEBHOOK_PATH.strip('/')
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{webhook_path_segment}"
        logger.info(f"Установка вебхука: {webhook_url}")
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN'))
        logger.info("Вебхук успешно установлен.")
        return application, run_web_server(application, stop_event)
    except Exception as e:
        logger.critical(f"Ошибка инициализации/вебхука: {e}", exc_info=True)
        if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed:
             await application.bot_data['aiohttp_session'].close()
             logger.info("Сессия aiohttp закрыта из-за ошибки.")
        raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def health_check(request):
        try:
            bot_info = await application.bot.get_me()
            if bot_info: return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
            else: logger.warning("Health check: Bot info unavailable."); return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except Exception as e: logger.error(f"Health check failed: {e}", exc_info=True); return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)
    app.router.add_get('/', health_check)
    app['bot_app'] = application
    webhook_path = GEMINI_WEBHOOK_PATH.strip('/')
    if not webhook_path.startswith('/'): webhook_path = '/' + webhook_path
    app.router.add_post(webhook_path, handle_telegram_webhook)
    logger.info(f"Вебхук слушает на пути: {webhook_path}")
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
    except Exception as e: logger.error(f"Ошибка веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally: logger.info("Остановка веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app')
    if not application: logger.critical("Приложение бота не найдено!"); return aiohttp.web.Response(status=500)
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token:
         header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
         if header_token != secret_token: logger.warning("Неверный секретный токен."); return aiohttp.web.Response(status=403)
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update) # Используем await напрямую
        return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e:
         body = await request.text()
         logger.error(f"Ошибка JSON от Telegram: {e}. Тело: {body[:500]}...")
         return aiohttp.web.Response(text="Bad Request", status=400)
    except Exception as e:
        logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('google.api_core').setLevel(logging.WARNING)
    logging.getLogger('google.generativeai').setLevel(logging.INFO)
    logging.getLogger('duckduckgo_search').setLevel(logging.INFO)
    logging.getLogger('PIL').setLevel(logging.INFO)
    logging.getLogger('pytesseract').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.INFO)
    logging.getLogger('telegram.bot').setLevel(logging.INFO)
    logger.setLevel(log_level)
    logger.info(f"--- Установлен уровень логгирования: {log_level_str} ({log_level}) ---")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): logger.info("Получен сигнал SIGINT/SIGTERM, остановка..."); stop_event.set()
        else: logger.warning("Повторный сигнал остановки.")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             logger.warning(f"Не удалось установить обработчик {sig} через loop. Использую signal.signal().")
             try: signal.signal(sig, lambda s, f: signal_handler())
             except Exception as e_signal: logger.error(f"Не удалось установить обработчик {sig} через signal: {e_signal}")

    application = None
    web_server_task = None
    aiohttp_session_main = None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro)
        aiohttp_session_main = application.bot_data.get('aiohttp_session')
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)...")
        await stop_event.wait()
    except asyncio.CancelledError: logger.info("Главная задача main() отменена.")
    except Exception as e: logger.critical("Критическая ошибка до/во время ожидания.", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки ---")
        if not stop_event.is_set(): stop_event.set()
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера...")
             try:
                 await asyncio.wait_for(web_server_task, timeout=15.0)
                 logger.info("Веб-сервер завершен.")
             except asyncio.TimeoutError:
                 logger.warning("Веб-сервер не завершился за 15с, отмена...")
                 web_server_task.cancel()
                 try: await web_server_task
                 except asyncio.CancelledError: logger.info("Задача веб-сервера отменена.")
                 except Exception as e_cancel: logger.error(f"Ошибка при отмене веб-сервера: {e_cancel}", exc_info=True)
             except asyncio.CancelledError: logger.info("Ожидание веб-сервера отменено.")
             except Exception as e_wait: logger.error(f"Ошибка при ожидании веб-сервера: {e_wait}", exc_info=True)
        if application:
            logger.info("Остановка приложения Telegram бота...")
            try: await application.shutdown(); logger.info("Приложение Telegram бота остановлено.")
            except Exception as e_shutdown: logger.error(f"Ошибка application.shutdown(): {e_shutdown}", exc_info=True)
        if aiohttp_session_main and not aiohttp_session_main.closed:
             logger.info("Закрытие сессии aiohttp...")
             await aiohttp_session_main.close()
             await asyncio.sleep(0.5)
             logger.info("Сессия aiohttp закрыта.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся задач...")
            [task.cancel() for task in tasks]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 if isinstance(res, asyncio.CancelledError): cancelled_count += 1
                 elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка в отмененной задаче {tasks[i].get_name()}: {res}", exc_info=isinstance(res, Exception))
            logger.info(f"Задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Приложение прервано (KeyboardInterrupt в asyncio.run).")
    except Exception as e_top: logger.critical("Неперехваченная ошибка в asyncio.run(main).", exc_info=True)

# --- END OF FILE main.py ---
