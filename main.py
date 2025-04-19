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

MAX_CONTEXT_CHARS = 100000 # Макс. символов в истории для отправки модели
MAX_HISTORY_MESSAGES = 100 # Макс. сообщений в истории для хранения (чтобы не росла бесконечно)
MAX_OUTPUT_TOKENS = 5000
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VIDEO_CAPABLE_KEYWORDS = ['flash', 'pro', 'ultra', '1.5'] # Модели, которые могут "смотреть" видео
USER_ID_PREFIX_FORMAT = "[User {user_id}]: " # Формат префикса для сообщений пользователя в истории

system_instruction_text = (
"Внимательно следи за историей диалога **в этом чате**, включая предыдущие вопросы, ответы, а также контекст из загруженных изображений, видео или файлов, чтобы твои ответы были последовательными и релевантными, соблюдая нить разговора."
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНИЕ <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
"**В истории диалога сообщения пользователей могут предваряться идентификатором в формате `[User ID]:`. Учитывай, кто задал последний вопрос (`[User ID]`), чтобы отвечать адресно этому пользователю, но сохраняй общий контекст беседы в группе.**"
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
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
# get/set_user_setting теперь используются только для настроек, не для истории
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    """Получает настройку пользователя (модель, температура, поиск)."""
    # В группах настройки тоже индивидуальные для каждого пользователя
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    """Устанавливает настройку пользователя."""
    context.user_data[key] = value

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    """Отправляет сообщение с Markdown, если не удается - отправляет как обычный текст. Отвечает на сообщение."""
    MAX_MESSAGE_LENGTH = 4096
    reply_chunks = [text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]
    sent_message = None
    chat_id = target_message.chat_id
    message_id = target_message.message_id # ID сообщения, на которое отвечаем
    current_user_id = target_message.from_user.id if target_message.from_user else "Unknown"

    try:
        for i, chunk in enumerate(reply_chunks):
            if i == 0:
                # Используем send_message с reply_to_message_id для надежности в группах и с fake_update
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id, parse_mode=ParseMode.MARKDOWN)
            else:
                # Последующие части отправляем без ответа на сообщение
                sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(0.1) # Небольшая задержка между частями
        return sent_message
    except BadRequest as e_md:
        # Обработка ошибок парсинга Markdown или если не найдено сообщение для ответа
        if "Can't parse entities" in str(e_md) or "can't parse" in str(e_md).lower() or "reply message not found" in str(e_md).lower():
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга Markdown или ответа на сообщение ({message_id}): {e_md}. Попытка отправить как обычный текст.")
            try:
                sent_message = None
                for i, chunk in enumerate(reply_chunks):
                     if i == 0: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, reply_to_message_id=message_id)
                     else: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk)
                     await asyncio.sleep(0.1)
                return sent_message
            except Exception as e_plain:
                logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить даже как обычный текст: {e_plain}", exc_info=True)
                try: await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось отправить ответ.")
                except Exception as e_final_send: logger.critical(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке: {e_final_send}")
        else:
            # Другие ошибки BadRequest
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка при отправке ответа (Markdown): {e_md}", exc_info=True)
            try: await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при отправке ответа: {str(e_md)[:100]}...")
            except Exception as e_error_send: logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке отправки: {e_error_send}")
    except Exception as e_other:
        # Другие непредвиденные ошибки
        logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Непредвиденная ошибка при отправке ответа: {e_other}", exc_info=True)
        try: await context.bot.send_message(chat_id=chat_id, text="❌ Произошла непредвиденная ошибка при отправке ответа.")
        except Exception as e_unexp_send: logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение о непредвиденной ошибке: {e_unexp_send}")
    return None
# ==========================================================

# --- Команды (/start, /clear, /temp, /search_on/off, /model) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    # Инициализация настроек пользователя при старте, если их нет
    if 'selected_model' not in context.user_data:
        set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'search_enabled' not in context.user_data:
        set_user_setting(context, 'search_enabled', True)
    if 'temperature' not in context.user_data:
        set_user_setting(context, 'temperature', 1.0)
    # Историю чата инициализируем при первом сообщении, здесь не трогаем

    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Получаем настройку текущего пользователя
    default_model_name = AVAILABLE_MODELS.get(current_model, current_model)
    start_message = (
        f"Привет! Я Google GEMINI **{default_model_name}**"
        f"\n- Умею искать в Google/DDG, понимать изображения, читать картинки и документы, **делать конспекты YouTube видео**."
        f"\n- В группах я помню всю историю чата и понимаю, кто мне пишет."
        f"\n\n**Ваши настройки:**"
        f"\n/model — сменить модель (ваша: {default_model_name})"
        f"\n/search_on / /search_off — вкл/выкл поиск (сейчас: {'Вкл' if get_user_setting(context, 'search_enabled', True) else 'Выкл'})"
        f"\n/temp <0.0-2.0> — установить креативность (ваша: {get_user_setting(context, 'temperature', 1.0):.1f})"
        f"\n\n**Общее для чата:**"
        f"\n/clear — очистить историю **этого** чата"
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    context.chat_data['history'] = [] # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНИЕ: Очищаем общую историю чата
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | История чата очищена.")
    await update.message.reply_text("🧹 История этого чата очищена.")

async def set_temperature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        # Настройки индивидуальны для пользователя
        current_temp = get_user_setting(context, 'temperature', 1.0)
        if not context.args:
            await update.message.reply_text(f"🌡️ Ваша текущая температура (креативность): {current_temp:.1f}\nЧтобы изменить, напиши `/temp <значение>` (например, `/temp 0.8`)")
            return
        temp_str = context.args[0].replace(',', '.')
        temp = float(temp_str)
        if not (0.0 <= temp <= 2.0): raise ValueError("Температура должна быть от 0.0 до 2.0")
        set_user_setting(context, 'temperature', temp) # Сохраняем настройку для пользователя
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Температура установлена на {temp:.1f}.")
        await update.message.reply_text(f"🌡️ Ваша температура установлена на {temp:.1f}")
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"⚠️ Неверный формат. {e}. Укажите число от 0.0 до 2.0. Пример: `/temp 0.8`")
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка в set_temperature: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при установке температуры.")


async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    set_user_setting(context, 'search_enabled', True) # Настройка индивидуальна
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск включен.")
    await update.message.reply_text("🔍 Поиск Google/DDG для вас включён.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    set_user_setting(context, 'search_enabled', False) # Настройка индивидуальна
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск отключен.")
    await update.message.reply_text("🔇 Поиск Google/DDG для вас отключён.")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка индивидуальна
    keyboard = []
    sorted_models = sorted(AVAILABLE_MODELS.items())
    for m, name in sorted_models:
         button_text = f"{'✅ ' if m == current_model else ''}{name}"
         keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_model_{m}")])
    await update.message.reply_text("Выберите модель (это повлияет только на ваши ответы):", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()
    callback_data = query.data
    if callback_data and callback_data.startswith("set_model_"):
        selected = callback_data.replace("set_model_", "")
        if selected in AVAILABLE_MODELS:
            set_user_setting(context, 'selected_model', selected) # Сохраняем для пользователя
            model_name = AVAILABLE_MODELS[selected]
            reply_text = f"Ваша модель установлена: **{model_name}**"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Модель установлена на {model_name}.")
            try:
                await query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e_md:
                 if "Message is not modified" in str(e_md): logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь выбрал ту же модель: {model_name}")
                 else:
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (Markdown): {e_md}. Отправляю новое.")
                     try: await query.edit_message_text(reply_text.replace('**', ''))
                     except Exception as e_edit_plain:
                          logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение даже как простой текст: {e_edit_plain}. Отправляю новое.")
                          await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN) # Отправляем новое, если редактирование не удалось
            except Exception as e:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (другая ошибка): {e}. Отправляю новое.", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь выбрал неизвестную модель: {selected}")
            try: await query.edit_message_text("❌ Неизвестная модель выбрана.")
            except Exception: await context.bot.send_message(chat_id=chat_id, text="❌ Неизвестная модель выбрана.")
    else:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен неизвестный callback_data: {callback_data}")
        try: await query.edit_message_text("❌ Ошибка обработки выбора.")
        except Exception: pass # Игнорируем ошибку редактирования, если не можем обработать

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

# ===== Функция извлечения YouTube ID =====
def extract_youtube_id(url: str) -> str | None:
    """Извлекает ID видео из различных форматов ссылок YouTube."""
    # Регулярное выражение для стандартных и коротких ссылок
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})', # Standard
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',          # Shortened
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})',  # Embed
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]{11})',      # V
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})', # Shorts
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    # Проверка для ссылок с параметрами (на всякий случай)
    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname in ('youtube.com', 'www.youtube.com') and parsed_url.path == '/watch':
            query_params = parse_qs(parsed_url.query)
            if 'v' in query_params and query_params['v']:
                video_id_candidate = query_params['v'][0]
                # Убедимся, что ID состоит из допустимых символов и имеет нужную длину
                if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]):
                    return video_id_candidate[:11] # Берем первые 11 символов
        if parsed_url.hostname in ('youtu.be',) and parsed_url.path:
             video_id_candidate = parsed_url.path[1:] # Убираем '/'
             if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]):
                 return video_id_candidate[:11]
    except Exception as e_parse:
        logger.debug(f"Ошибка парсинга URL для YouTube ID: {e_parse} (URL: {url[:50]}...)")

    return None
# ==================================

# ===== Функция повторного анализа изображения (использует chat_data) =====
async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    """Скачивает изображение по file_id, вызывает Gemini Vision с новым вопросом и отправляет ответ. Использует chat_data."""
    chat_id = update.effective_chat.id
    # User ID берем из вызвавшей функции (кто ответил на сообщение бота)
    requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по фото от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1. Скачивание и кодирование
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

    # 2. Формирование запроса к Vision
    # Добавляем ID пользователя, задающего вопрос, к тексту запроса
    user_question_with_id = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    parts = [{"text": user_question_with_id}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]
    content_for_vision = [{"role": "user", "parts": parts}]

    # 3. Вызов модели (логика ретраев и обработки ошибок)
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка запрашивающего пользователя
    temperature = get_user_setting(context, 'temperature', 1.0) # Настройка запрашивающего пользователя

    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra'] # 'vision' для старых моделей
    is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
    if not is_vision_model:
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models:
            original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0])
            model_id = fallback_model_id
            new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
        else:
            logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет доступных vision моделей.")
            await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения.")
            return

    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель: {model_id}, Темп: {temperature}")
    reply = None
    response_vision = None
    # Цикл ретраев...
    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            # Используем общую историю чата для контекста (если нужно, но reanalyze обычно изолирован)
            # chat_history = context.chat_data.setdefault("history", [])
            # history_for_model = chat_history + content_for_vision # Или просто content_for_vision
            # Для reanalyze лучше не передавать старую историю, чтобы не путать модель
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_vision = await asyncio.to_thread(model.generate_content, content_for_vision) # Отправляем только текущий вопрос и фото

            if hasattr(response_vision, 'text'): reply = response_vision.text
            else: reply = None

            if not reply: # Обработка пустого ответа
                 block_reason_str, finish_reason_str = 'N/A', 'N/A'
                 # Логика извлечения причин...
                 try:
                     if hasattr(response_vision, 'prompt_feedback') and response_vision.prompt_feedback and hasattr(response_vision.prompt_feedback, 'block_reason'):
                         block_reason_enum = response_vision.prompt_feedback.block_reason
                         block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                     if hasattr(response_vision, 'candidates') and response_vision.candidates and isinstance(response_vision.candidates, (list, tuple)) and len(response_vision.candidates) > 0:
                          first_candidate = response_vision.candidates[0]
                          if hasattr(first_candidate, 'finish_reason'):
                               finish_reason_enum = first_candidate.finish_reason
                               finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                 except Exception as e_inner_reason: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка извлечения причины пустого ответа: {e_inner_reason}")
                 logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                 if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не смогла ответить на вопрос об изображении. (Блокировка: {block_reason_str})"
                 elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель не смогла ответить на вопрос об изображении. (Причина: {finish_reason_str})"
                 else: reply = "🤖 Не могу ответить на ваш вопрос об этом изображении (пустой ответ модели)."
                 break # Выходим при пустом ответе без явной ошибки
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply:
                 logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Успешный анализ на попытке {attempt + 1}.")
                 break
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
             reason_str = "неизвестна"
             try: # Попытка извлечь причину
                 if hasattr(e_block_stop, 'args') and e_block_stop.args: reason_str = str(e_block_stop.args[0])
             except Exception: pass
             logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
             reply = f"❌ Не удалось повторно проанализировать изображение (ограничение модели)."
             break
        except Exception as e:
            error_message = str(e)
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            # Обработка неретраиваемых ошибок 4xx...
            if "400" in error_message or "429" in error_message or "location is not supported" in error_message:
                reply = f"❌ Ошибка при повторном анализе изображения ({error_message[:100]}...)."
                break
            elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ожидание {wait_time:.1f} сек...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: reply = f"❌ Ошибка при повторном анализе после {attempt + 1} попыток."
                break

    # 4. Добавление в общую историю чата (chat_data) и отправка ответа
    chat_history = context.chat_data.setdefault("history", []) # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНО
    # Добавляем вопрос пользователя с его ID
    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_question_with_id}], # Сохраняем с ID
        "user_id": requesting_user_id, # Сохраняем ID отдельно для возможного использования
        "message_id": update.message.message_id # ID сообщения с вопросом
        }
    # Важно: не сохраняем здесь file_id, он был у оригинального сообщения
    chat_history.append(history_entry_user)

    if reply:
        # Добавляем ответ модели (без User ID)
        history_entry_model = {
            "role": "model",
            "parts": [{"text": reply}]
            # Можно добавить reply_to_message_id: update.message.message_id, если нужно будет связать ответ модели с этим вопросом
            }
        chat_history.append(history_entry_model)
        # Отправляем ответ на сообщение с вопросом
        await send_reply(update.message, reply, context)
    else:
        # Если reply все еще None после ретраев
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет ответа для отправки пользователю.")
        final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]}) # Сохраняем ошибку в истории
        try: await update.message.reply_text(final_error_msg) # Отправляем как простой текст
        except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось отправить сообщение об ошибке: {e_final_fail}")

    # Ограничиваем размер истории чата
    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)
# =======================================================

# ===== НОВАЯ ФУНКЦИЯ: Ответ на вопросы по конспекту видео (использует chat_data) =====
async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    """Вызывает Gemini с video_id и вопросом пользователя. Использует chat_data."""
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1. Формирование запроса к модели
    youtube_uri = f"https://www.youtube.com/watch?v={video_id}"
    # Добавляем ID пользователя и URI видео к вопросу
    user_question_with_context = (
        f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}\n"
        f"(Ответь на основе видео: {youtube_uri})"
    )
    content_for_video = [{"role": "user", "parts": [{"text": user_question_with_context}]}]

    # 2. Вызов модели (логика ретраев и обработки ошибок)
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка запрашивающего
    temperature = get_user_setting(context, 'temperature', 1.0) # Настройка запрашивающего

    # Проверка, поддерживает ли модель видео
    is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
    if not is_video_model:
        video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
        if video_models:
            original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0])
            model_id = fallback_model_id
            new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель {original_model_name} не video. Временно использую {new_model_name}.")
            # Можно уведомить пользователя
            # await update.message.reply_text(f"ℹ️ Ваша текущая модель не умеет анализировать видео. Временно использую {new_model_name}.", parse_mode=ParseMode.MARKDOWN)
        else:
            logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет доступных video моделей.")
            await update.message.reply_text("❌ Нет доступных моделей для ответа на вопрос по видео.")
            return

    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель: {model_id}, Темп: {temperature}")
    reply = None
    response_video = None
    # Цикл ретраев...
    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            # Отправляем только текущий вопрос и ссылку на видео
            response_video = await asyncio.to_thread(model.generate_content, content_for_video)

            if hasattr(response_video, 'text'): reply = response_video.text
            else: reply = None

            if not reply: # Обработка пустого ответа
                 block_reason_str, finish_reason_str = 'N/A', 'N/A'
                 # Логика извлечения причин...
                 try:
                     if hasattr(response_video, 'prompt_feedback') and response_video.prompt_feedback and hasattr(response_video.prompt_feedback, 'block_reason'):
                         block_reason_enum = response_video.prompt_feedback.block_reason
                         block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                     if hasattr(response_video, 'candidates') and response_video.candidates and isinstance(response_video.candidates, (list, tuple)) and len(response_video.candidates) > 0:
                         first_candidate = response_video.candidates[0]
                         if hasattr(first_candidate, 'finish_reason'):
                             finish_reason_enum = first_candidate.finish_reason
                             finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                 except Exception as e_inner_reason: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка извлечения причины пустого ответа: {e_inner_reason}")
                 logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                 if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не смогла ответить по видео. (Блокировка: {block_reason_str})"
                 elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель не смогла ответить по видео. (Причина: {finish_reason_str})"
                 else: reply = "🤖 Не могу ответить на ваш вопрос по этому видео (пустой ответ модели)."
                 break
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply:
                 logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Успешный анализ на попытке {attempt + 1}.")
                 break
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
             reason_str = "неизвестна"
             try:
                 if hasattr(e_block_stop, 'args') and e_block_stop.args: reason_str = str(e_block_stop.args[0])
             except Exception: pass
             logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
             reply = f"❌ Не удалось ответить по видео (ограничение модели)."
             break
        except Exception as e:
            error_message = str(e)
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message or "processing video" in error_message.lower()
            if "400" in error_message or "429" in error_message or "location is not supported" in error_message:
                 reply = f"❌ Ошибка при ответе по видео ({error_message[:100]}...)."
                 break
            elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ожидание {wait_time:.1f} сек...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: reply = f"❌ Ошибка при ответе по видео после {attempt + 1} попыток."
                break

    # 3. Добавление в общую историю чата (chat_data) и отправка ответа
    chat_history = context.chat_data.setdefault("history", []) # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНО
    # Добавляем вопрос пользователя с его ID
    history_entry_user = {
        "role": "user",
        "parts": [{"text": user_question_with_context}], # Сохраняем с ID и URI
        "user_id": requesting_user_id,
        "message_id": update.message.message_id
        }
    chat_history.append(history_entry_user)

    if reply:
        # Добавляем ответ модели
        history_entry_model = {
            "role": "model",
            "parts": [{"text": reply}]
        }
        chat_history.append(history_entry_model)
        # Отправляем ответ на сообщение с вопросом
        await send_reply(update.message, reply, context)
    else:
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет ответа для отправки пользователю.")
        final_error_msg = "🤖 К сожалению, не удалось ответить на ваш вопрос по видео."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        try: await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось отправить сообщение об ошибке: {e_final_fail}")

    # Ограничиваем размер истории чата
    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)

# =============================================================

# ===== Основной обработчик сообщений (использует chat_data, добавляет User ID) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: # Если не можем определить пользователя (например, в канале?)
        logger.warning(f"ChatID: {chat_id} | Не удалось определить пользователя в update. Игнорирование сообщения.")
        return
    user_id = update.effective_user.id
    message = update.message

    if not message or (not message.text and not hasattr(message, 'image_file_id')): # Пропускаем если нет текста И нет ID от OCR
        # Проверяем, есть ли фото или документ, которые могли не передать текст
        if not message.photo and not message.document:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получено пустое или нетекстовое сообщение без OCR ID, фото или документа.")
            return

    chat_history = context.chat_data.setdefault("history", []) # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНО

    # --- Проверка на ответ к специальным сообщениям ---
    if message.reply_to_message and message.reply_to_message.text and message.text and not message.text.startswith('/'):
        replied_message = message.reply_to_message # Сообщение, на которое ответили
        replied_text = replied_message.text
        user_question = message.text.strip()
        requesting_user_id = user_id # Кто задал вопрос

        # Ищем оригинальное сообщение пользователя, которое вызвало ответ бота
        original_user_message_entry = None
        original_user_id = None
        found_special_context = False

        try:
            # Ищем назад сообщение модели с нужным текстом (описание или конспект)
            for i in range(len(chat_history) - 1, -1, -1):
                model_entry = chat_history[i]
                # Проверяем, что это сообщение модели и текст совпадает с началом отвеченного
                if model_entry.get("role") == "model" and model_entry.get("parts") and isinstance(model_entry["parts"], list) and len(model_entry["parts"]) > 0:
                    model_text = model_entry["parts"][0].get("text", "")
                    is_image_reply = model_text.startswith(IMAGE_DESCRIPTION_PREFIX) and replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) and model_text[:100] == replied_text[:100]
                    is_video_reply = model_text.startswith(YOUTUBE_SUMMARY_PREFIX) and replied_text.startswith(YOUTUBE_SUMMARY_PREFIX) and model_text[:100] == replied_text[:100]

                    if is_image_reply or is_video_reply:
                        # Нашли сообщение модели. Теперь ищем предыдущее сообщение пользователя, у которого есть нужный ID
                        if i > 0:
                            potential_user_entry = chat_history[i - 1]
                            if potential_user_entry.get("role") == "user":
                                if is_image_reply and "image_file_id" in potential_user_entry:
                                    found_file_id = potential_user_entry["image_file_id"]
                                    original_user_id = potential_user_entry.get("user_id", "Unknown")
                                    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найден image_file_id: ...{found_file_id[-10:]} для reanalyze_image (ориг. user: {original_user_id}).")
                                    await reanalyze_image(update, context, found_file_id, user_question, original_user_id)
                                    found_special_context = True
                                    break # Выходим из цикла поиска
                                elif is_video_reply and "youtube_video_id" in potential_user_entry:
                                    found_video_id = potential_user_entry["youtube_video_id"]
                                    original_user_id = potential_user_entry.get("user_id", "Unknown")
                                    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найден youtube_video_id: {found_video_id} для reanalyze_video (ориг. user: {original_user_id}).")
                                    await reanalyze_video(update, context, found_video_id, user_question, original_user_id)
                                    found_special_context = True
                                    break # Выходим из цикла поиска
                                else:
                                    logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найдено сообщение модели, но у предыдущего user-сообщения нет нужного ID (image/video).")
                        else:
                             logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Найдено сообщение модели в самом начале истории, не можем найти user-сообщение.")
                        # Если нашли совпадение по тексту, но не нашли ID, прекращаем поиск для этого случая
                        if not found_special_context: break

        except Exception as e_hist_search:
            logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка при поиске ID для reanalyze в chat_history: {e_hist_search}", exc_info=True)

        if found_special_context:
            return # Завершаем обработку, если вызвали reanalyze

        # Если это был ответ на спец. сообщение, но ID не был найден или reanalyze не вызван
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX):
             logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на спец. сообщение, но ID не найден или reanalyze не запущен. Обработка как обычный текст.")
        # Если это ответ на обычное сообщение бота, продолжаем обработку как обычный текст ниже

    # --- Получение текста и проверка на YouTube ---
    original_user_message_text = ""
    image_file_id_from_ocr = None
    user_message_id = message.message_id # Сохраняем ID текущего сообщения

    if hasattr(message, 'image_file_id'): # Сообщение пришло из OCR
        image_file_id_from_ocr = message.image_file_id
        original_user_message_text = message.text.strip() if message.text else "" # Текст из OCR
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен image_file_id: ...{image_file_id_from_ocr[-10:]} из OCR.")
    elif message.text:
        original_user_message_text = message.text.strip()

    # Добавляем префикс с User ID для истории и промпта модели
    user_message_with_id = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + original_user_message_text

    # Проверяем наличие ссылок YouTube *только* если это не ответ на спец. сообщение (уже обработано выше) и не OCR
    youtube_handled = False
    if not (message.reply_to_message and message.reply_to_message.text and (message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) or message.reply_to_message.text.startswith(YOUTUBE_SUMMARY_PREFIX))) and not image_file_id_from_ocr:
        youtube_id = extract_youtube_id(original_user_message_text)
        if youtube_id:
            youtube_handled = True # Флаг, что мы обрабатываем YouTube
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружена ссылка YouTube (ID: {youtube_id}). Запрос конспекта...")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

            # --- Логика получения конспекта ---
            youtube_uri = f"https://www.youtube.com/watch?v={youtube_id}"
            # Промпт включает ID пользователя и URI
            prompt_for_summary = (
                f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Сделай краткий, но информативный конспект видео по этой ссылке: {youtube_uri}\n"
                f"Основные пункты, ключевые моменты."
            )
            content_for_summary = [{"role": "user", "parts": [{"text": prompt_for_summary}]}]

            model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка пользователя
            temperature = get_user_setting(context, 'temperature', 1.0) # Настройка пользователя
            # Проверка, поддерживает ли модель видео
            is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
            if not is_video_model:
                video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
                if video_models:
                    original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                    fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0])
                    model_id = fallback_model_id
                    new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель {original_model_name} не video. Временно использую {new_model_name}.")
                    # await update.message.reply_text(f"ℹ️ Ваша модель не умеет анализировать видео. Временно использую {new_model_name}.", parse_mode=ParseMode.MARKDOWN)
                else:
                    logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет доступных video моделей.")
                    await update.message.reply_text("❌ Нет доступных моделей для создания конспекта видео.")
                    return # Завершаем обработку

            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель: {model_id}, Темп: {temperature}")
            reply = None
            # Цикл ретраев...
            for attempt in range(RETRY_ATTEMPTS):
                 try:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
                     generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                     model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
                     response_summary = await asyncio.to_thread(model.generate_content, content_for_summary) # Отправляем только промпт с ссылкой

                     if hasattr(response_summary, 'text'): reply = response_summary.text
                     else: reply = None

                     if not reply: # Обработка пустого ответа
                         block_reason_str, finish_reason_str = 'N/A', 'N/A'
                         # Логика извлечения причин...
                         try:
                              if hasattr(response_summary, 'prompt_feedback') and response_summary.prompt_feedback and hasattr(response_summary.prompt_feedback, 'block_reason'):
                                  block_reason_enum = response_summary.prompt_feedback.block_reason
                                  block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                              if hasattr(response_summary, 'candidates') and response_summary.candidates and isinstance(response_summary.candidates, (list, tuple)) and len(response_summary.candidates) > 0:
                                  first_candidate = response_summary.candidates[0]
                                  if hasattr(first_candidate, 'finish_reason'):
                                       finish_reason_enum = first_candidate.finish_reason
                                       finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                         except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка извлечения причины пустого ответа: {e_inner_reason}")
                         logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                         if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не смогла создать конспект. (Блокировка: {block_reason_str})"
                         elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель не смогла создать конспект. (Причина: {finish_reason_str})"
                         else: reply = "🤖 Не удалось создать конспект видео (пустой ответ модели)."
                         break
                     if reply and "не удалось создать конспект" not in reply.lower() and "не смогла создать конспект" not in reply.lower():
                          logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Успешный конспект на попытке {attempt + 1}.")
                          break
                 except (BlockedPromptException, StopCandidateException) as e_block_stop:
                      reason_str = "неизвестна"
                      try:
                          if hasattr(e_block_stop, 'args') and e_block_stop.args: reason_str = str(e_block_stop.args[0])
                      except Exception: pass
                      logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Конспект заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
                      reply = f"❌ Не удалось создать конспект (ограничение модели)."
                      break
                 except Exception as e:
                     error_message = str(e)
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
                     is_retryable = "500" in error_message or "503" in error_message or "processing video" in error_message.lower()
                     if "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported language" in error_message.lower(): # Добавим unsupported language
                          reply = f"❌ Ошибка при создании конспекта ({error_message[:100]}...). Возможно, видео недоступно или на неподдерживаемом языке."
                          break
                     elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                         wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                         logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ожидание {wait_time:.1f} сек...")
                         await asyncio.sleep(wait_time)
                         continue
                     else:
                         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось создать конспект после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                         if reply is None: reply = f"❌ Ошибка при создании конспекта после {attempt + 1} попыток."
                         break

            # --- Сохранение в историю и отправка ---
            # 1. Сообщение пользователя с ID видео
            user_text_for_history = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + f"Пользователь прислал ссылку на видео: {youtube_uri}"
            history_entry_user = {
                "role": "user",
                "parts": [{"text": user_text_for_history}],
                "youtube_video_id": youtube_id, # Сохраняем ID для reanalyze
                "user_id": user_id,
                "message_id": user_message_id
                }
            chat_history.append(history_entry_user)
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (YouTube) в chat_history с youtube_video_id.")

            # 2. Ответ модели (конспект с префиксом или ошибка)
            if reply and "❌" not in reply and "🤖" not in reply:
                model_reply_text_with_prefix = f"{YOUTUBE_SUMMARY_PREFIX}{reply}"
            else:
                model_reply_text_with_prefix = reply if reply else "🤖 Не удалось создать конспект видео."

            history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]} # Сохраняем ответ с префиксом
            chat_history.append(history_entry_model)
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (YouTube) в chat_history.")

            # 3. Отправка пользователю "чистого" ответа (без префикса)
            reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text_with_prefix
            if reply_to_send:
                await send_reply(message, reply_to_send, context)
            else:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет ответа для отправки пользователю.")
                 try: await message.reply_text("🤖 К сожалению, не удалось создать конспект видео.")
                 except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")

            # Ограничиваем историю
            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return # Завершаем обработку здесь для YouTube ссылок
    # --- Конец обработки YouTube ссылок ---

    # --- Стандартная обработка текста (если не было спец. ответа или YouTube) ---
    # Используем user_message_with_id, который уже содержит префикс [User ID]:
    if not original_user_message_text and not image_file_id_from_ocr:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Дошли до конца handle_message без текста для обработки (не YouTube, не OCR).")
        # Возможно, это было сообщение только с фото/документом без подписи - оно обработается в handle_photo/handle_document
        return

    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка пользователя
    temperature = get_user_setting(context, 'temperature', 1.0) # Настройка пользователя
    use_search = get_user_setting(context, 'search_enabled', True) # Настройка пользователя

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # --- Блок поиска ---
    search_context_snippets = []
    search_provider = None
    search_log_msg = "Поиск отключен пользователем"
    if use_search:
        # Используем оригинальный текст без User ID для поиска
        query_for_search = original_user_message_text
        query_short = query_for_search[:50] + '...' if len(query_for_search) > 50 else query_for_search
        search_log_msg = f"Поиск Google/DDG для '{query_short}'"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg}...")
        session = context.bot_data.get('aiohttp_session')
        if not session or session.closed:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Создание новой сессии aiohttp для поиска.")
            timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
            session = aiohttp.ClientSession(timeout=timeout)
            context.bot_data['aiohttp_session'] = session

        google_results = await perform_google_search(query_for_search, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results:
            search_provider = "Google"
            search_context_snippets = google_results
            search_log_msg += f" (Google: {len(search_context_snippets)} рез.)"
        else:
            search_log_msg += " (Google: 0 рез./ошибка)"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Google не дал результатов. Пробуем DuckDuckGo...")
            try:
                ddgs = DDGS()
                # Запускаем синхронный поиск в отдельном потоке
                results_ddg = await asyncio.to_thread(ddgs.text, query_for_search, region='ru-ru', max_results=DDG_MAX_RESULTS)
                if results_ddg:
                    ddg_snippets = [r.get('body', '') for r in results_ddg if r.get('body')]
                    if ddg_snippets:
                        search_provider = "DuckDuckGo"
                        search_context_snippets = ddg_snippets
                        search_log_msg += f" (DDG: {len(search_context_snippets)} рез.)"
                    else: search_log_msg += " (DDG: 0 текст. рез.)"
                else: search_log_msg += " (DDG: 0 рез.)"
            except TimeoutError:
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Таймаут поиска DuckDuckGo.")
                 search_log_msg += " (DDG: таймаут)"
            except TypeError as e_type:
                # Проверка на старую ошибку с timeout, хотя она должна быть исправлена в to_thread
                if "unexpected keyword argument 'timeout'" in str(e_type): logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Снова ошибка с аргументом timeout в DDGS.text(): {e_type}")
                else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка типа при поиске DuckDuckGo: {e_type}", exc_info=True)
                search_log_msg += " (DDG: ошибка типа)"
            except Exception as e_ddg:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка поиска DuckDuckGo: {e_ddg}", exc_info=True)
                search_log_msg += " (DDG: ошибка)"
    # --- Конец блока поиска ---

    # ===== Формирование финального промпта =====
    # Используем сообщение С User ID как основу
    base_user_prompt = user_message_with_id

    if search_context_snippets:
        search_context_lines = [f"- {s.strip()}" for s in search_context_snippets if s.strip()]
        if search_context_lines:
            search_context = "\n".join(search_context_lines)
            # Добавляем контекст ПОСЛЕ основного вопроса пользователя с ID
            final_user_prompt_parts = [
                {"text": base_user_prompt},
                {"text": (
                    f"\n\n(Возможно релевантная доп. информация из поиска, используй с осторожностью, если подходит к вопросу пользователя {USER_ID_PREFIX_FORMAT.format(user_id=user_id)}, иначе игнорируй):\n{search_context}"
                )}
            ]
            final_user_prompt_text = base_user_prompt + final_user_prompt_parts[1]["text"] # Объединяем для логгирования и истории
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен контекст из {search_provider} ({len(search_context_lines)} непустых сниппетов).")
        else:
             # final_user_prompt_parts = [{"text": base_user_prompt}] # Не нужно, используем просто текст
             final_user_prompt_text = base_user_prompt
             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Сниппеты из {search_provider} оказались пустыми, контекст не добавлен.")
             search_log_msg += " (пустые сниппеты)"
    else:
        # final_user_prompt_parts = [{"text": base_user_prompt}]
        final_user_prompt_text = base_user_prompt
    # ==========================================================

    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg}")
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Финальный промпт для Gemini (длина {len(final_user_prompt_text)}):\n{final_user_prompt_text[:500]}...")

    # --- История и ее обрезка (используем chat_history) ---
    # Добавляем текущее сообщение пользователя (текст с ID + возможно ID файла от OCR) в chat_history
    # Важно: Не добавлять еще раз, если это YouTube ссылка, т.к. она уже добавлена выше
    if not youtube_handled:
        history_entry_user = {
            "role": "user",
            "parts": [{"text": user_message_with_id}], # Сохраняем текст с ID
            "user_id": user_id,
            "message_id": user_message_id
            }
        if image_file_id_from_ocr:
            history_entry_user["image_file_id"] = image_file_id_from_ocr
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (OCR) в chat_history с image_file_id.")
        else:
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (текст) в chat_history.")
        chat_history.append(history_entry_user)

    # Обрезка chat_history по количеству символов для отправки модели
    history_for_model_raw = []
    current_total_chars = 0
    # Идем с конца истории, чтобы сохранить свежие сообщения
    for entry in reversed(chat_history):
        entry_text = ""
        if entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text"):
            entry_text = entry["parts"][0]["text"]

        entry_len = len(entry_text)
        if current_total_chars + entry_len <= MAX_CONTEXT_CHARS:
            history_for_model_raw.append(entry)
            current_total_chars += entry_len
        else:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Добавлено {len(history_for_model_raw)} сообщ., ~{current_total_chars} симв.")
            break # Прекращаем добавлять, как только превысили лимит

    # Переворачиваем обратно и заменяем последнее сообщение user на финальный промпт с поиском (если был)
    history_for_model = []
    if history_for_model_raw:
        history_for_model = list(reversed(history_for_model_raw))
        # Заменяем текст последнего user сообщения на final_user_prompt_text
        if history_for_model[-1]["role"] == "user":
             history_for_model[-1]["parts"] = [{"text": final_user_prompt_text}]
        else:
             # Этого не должно случиться, т.к. последнее добавленное - это текущий user
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Последнее сообщение в обрезанной истории не 'user'. Не удалось вставить final_prompt.")
             # В крайнем случае, просто добавим его
             history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]})
    else: # Если история пуста или первое сообщение слишком длинное
        history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]})


    # Исключаем кастомные ключи (user_id, message_id, image_file_id, youtube_video_id) из истории для модели
    history_clean_for_model = []
    for entry in history_for_model:
         clean_entry = {"role": entry["role"], "parts": entry["parts"]}
         history_clean_for_model.append(clean_entry)

    # --- Конец подготовки истории ---


    # --- Вызов модели с РЕТРАЯМИ ---
    reply = None
    response = None
    last_exception = None
    generation_successful = False

    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            # Передаем очищенную историю
            response = await asyncio.to_thread(model.generate_content, history_clean_for_model)

            if hasattr(response, 'text'): reply = response.text
            else: reply = None

            if not reply: # Обработка пустого ответа
                 block_reason_str, finish_reason_str, safety_info_str = 'N/A', 'N/A', 'N/A'
                 try:
                     # Логика извлечения причин...
                     if hasattr(response, 'prompt_feedback') and response.prompt_feedback and hasattr(response.prompt_feedback, 'block_reason'):
                         block_reason_enum = response.prompt_feedback.block_reason
                         block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                     if hasattr(response, 'candidates') and response.candidates and isinstance(response.candidates, (list, tuple)) and len(response.candidates) > 0:
                          first_candidate = response.candidates[0]
                          if hasattr(first_candidate, 'finish_reason'):
                               finish_reason_enum = first_candidate.finish_reason
                               finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                          # Извлечение safety ratings
                          if hasattr(first_candidate, 'safety_ratings') and first_candidate.safety_ratings:
                               safety_ratings = first_candidate.safety_ratings
                               safety_info_parts = []
                               for rating in safety_ratings:
                                   cat_name = rating.category.name if hasattr(rating.category, 'name') else str(rating.category)
                                   prob_name = rating.probability.name if hasattr(rating.probability, 'name') else str(rating.probability)
                                   safety_info_parts.append(f"{cat_name}:{prob_name}")
                               safety_info_str = ", ".join(safety_info_parts)

                 except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка извлечения причины/safety пустого ответа: {e_inner_reason}")

                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пустой ответ или нет текста (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}, Safety: [{safety_info_str}]")
                 if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не дала ответ. (Блокировка: {block_reason_str})"
                 elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель завершила работу без ответа. (Причина: {finish_reason_str})"
                 else:
                     reply = "🤖 Модель дала пустой ответ."
                     generation_successful = True # Считаем успехом
                 break # Выходим при пустом ответе

            if reply: generation_successful = True
            if generation_successful:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Успешная генерация на попытке {attempt + 1}.")
                 break
        except (BlockedPromptException, StopCandidateException) as e_block_stop:
            reason_str = "неизвестна"
            try:
                if hasattr(e_block_stop, 'args') and e_block_stop.args: reason_str = str(e_block_stop.args[0])
            except Exception: pass
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Запрос заблокирован/остановлен моделью (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
            reply = f"❌ Запрос заблокирован/остановлен моделью." # Не указываем причину пользователю
            break
        except Exception as e:
            last_exception = e
            error_message = str(e)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка генерации на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            # Обработка 4xx ошибок
            if "429" in error_message:
                 reply = f"❌ Слишком много запросов к модели. Попробуйте позже."
                 break
            elif "400" in error_message:
                 reply = f"❌ Ошибка в запросе к модели (400 Bad Request). Возможно, проблема с форматом данных."
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка 400 Bad Request: {error_message}", exc_info=True)
                 break
            elif "location is not supported" in error_message:
                 reply = f"❌ Эта модель недоступна в вашем регионе."
                 break

            if is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ожидание {wait_time:.1f} сек перед попыткой {attempt + 2}...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось выполнить генерацию после {attempt + 1} попыток. Последняя ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: reply = f"❌ Ошибка при обращении к модели после {attempt + 1} попыток." # ({error_message[:100]}...) - убрал детали ошибки для пользователя
                break
    # --- Конец блока вызова модели ---

    # Добавляем ответ модели в chat_history (если не YouTube, т.к. там уже добавлено)
    if reply and not youtube_handled:
        history_entry_model = {"role": "model", "parts": [{"text": reply}]}
        chat_history.append(history_entry_model)

    # Отправка ответа пользователю (если не YouTube)
    if reply and not youtube_handled:
        if message: await send_reply(message, reply, context)
        else:
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не найдено сообщение для ответа в update (не YouTube).")
             try: await context.bot.send_message(chat_id=chat_id, text=reply)
             except Exception as e_send_direct: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить ответ напрямую в чат (не YouTube): {e_send_direct}")
    elif not youtube_handled: # Если reply пустой и не YouTube
         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Нет ответа для отправки пользователю после всех попыток (не YouTube).")
         try:
             # Не отправляем сообщение об ошибке, если модель дала пустой ответ намеренно
             if reply != "🤖 Модель дала пустой ответ.":
                  error_message_to_user = "🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток."
                  if message: await message.reply_text(error_message_to_user)
                  else: await context.bot.send_message(chat_id=chat_id, text=error_message_to_user)
         except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение о финальной ошибке (не YouTube): {e_final_fail}")

    # Ограничиваем общий размер истории чата по кол-ву сообщений
    while len(chat_history) > MAX_HISTORY_MESSAGES:
        removed = chat_history.pop(0)
        logger.debug(f"ChatID: {chat_id} | Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed.get('role')}")


# =============================================================

# ===== Обработчик фото (обновлен для chat_data и User ID) =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.photo:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_photo не найдено фото.")
        return

    photo_file_id = message.photo[-1].file_id
    user_message_id = message.message_id # ID сообщения с фото
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
        await message.reply_text("❌ Не удалось загрузить изображение.")
        return

    user_caption = message.caption if message.caption else ""

    # --- OCR ---
    ocr_triggered = False
    if tesseract_available:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            # Запускаем OCR в отдельном потоке с таймаутом
            extracted_text = await asyncio.to_thread(pytesseract.image_to_string, image, lang='rus+eng', timeout=15) # timeout в секундах
            if extracted_text and extracted_text.strip():
                ocr_triggered = True
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружен текст на изображении (OCR).")
                # Формируем текст для handle_message, включая User ID
                ocr_context = f"На изображении обнаружен следующий текст:\n```\n{extracted_text.strip()}\n```"
                if user_caption:
                    user_prompt_ocr = f"{user_caption}. {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                else:
                    user_prompt_ocr = f"{ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"

                # Передаем file_id и user_prompt_ocr (без User ID префикса, он добавится в handle_message) в handle_message
                # Передаем настоящий объект message, добавляя к нему атрибут image_file_id
                message.image_file_id = photo_file_id
                # Заменяем текст сообщения на результат OCR для передачи в handle_message
                message.text = user_prompt_ocr # Перезаписываем текст для handle_message

                logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с OCR текстом и image_file_id.")
                # Просто вызываем handle_message с тем же update, но модифицированным message
                await handle_message(update, context)
                return # Завершаем здесь, остальное сделает handle_message
            else:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | OCR не нашел текст на изображении.")
        except pytesseract.TesseractNotFoundError:
            logger.error("Tesseract не найден! Установите Tesseract и укажите путь, если нужно. OCR отключен.")
            tesseract_available = False # Отключаем для этой сессии
        except RuntimeError as timeout_error:
            if "Tesseract process timeout" in str(timeout_error):
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | OCR таймаут: {timeout_error}")
                 await message.reply_text("⏳ Не удалось распознать текст (слишком долго). Анализирую как фото...")
            else: # Другая ошибка RuntimeError
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка выполнения OCR: {timeout_error}", exc_info=True)
                 await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
        except Exception as e: # Другие ошибки OCR
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка OCR: {e}", exc_info=True)
            await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
    # --- Конец OCR ---

    # --- Обработка как изображение (Vision) ---
    if not ocr_triggered:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обработка фото как изображения (Vision).")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        MAX_IMAGE_BYTES = 20 * 1024 * 1024 # Увеличил лимит, но Gemini все равно может иметь свои ограничения
        if len(file_bytes) > MAX_IMAGE_BYTES:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Изображение ({len(file_bytes) / (1024*1024):.2f} MB) может быть большим для API.")
            # Можно добавить проверку перед кодированием Base64

        try: b64_data = base64.b64encode(file_bytes).decode()
        except Exception as e:
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Base64 кодирования: {e}", exc_info=True)
             await message.reply_text("❌ Ошибка обработки изображения.")
             return

        # Формируем промпт для Vision, включая User ID
        if user_caption:
            prompt_text_vision = f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
        else:
            prompt_text_vision = f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении."

        # Определяем MIME-тип (пытаемся угадать по первым байтам, если возможно)
        # Простая проверка на JPEG и PNG
        mime_type = "image/jpeg" # По умолчанию
        if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
        elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
        # Можно добавить другие типы: GIF, WEBP и т.д., если Gemini их поддерживает

        parts = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
        content_for_vision = [{"role": "user", "parts": parts}]

        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL) # Настройка пользователя
        temperature = get_user_setting(context, 'temperature', 1.0) # Настройка пользователя
        # Проверка на vision модель...
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
                # await message.reply_text(f"ℹ️ Ваша модель не подходит для фото. Временно использую {new_model_name}.", parse_mode=ParseMode.MARKDOWN) # Можно раскомментировать
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
                # Отправляем только текущий запрос с фото
                response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)

                if hasattr(response_vision, 'text'): reply = response_vision.text
                else: reply = None

                if not reply: # Обработка пустого ответа
                    block_reason_str, finish_reason_str = 'N/A', 'N/A'
                    # Логика извлечения причин...
                    try:
                        if hasattr(response_vision, 'prompt_feedback') and response_vision.prompt_feedback and hasattr(response_vision.prompt_feedback, 'block_reason'):
                            block_reason_enum = response_vision.prompt_feedback.block_reason
                            block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                        if hasattr(response_vision, 'candidates') and response_vision.candidates and isinstance(response_vision.candidates, (list, tuple)) and len(response_vision.candidates) > 0:
                            first_candidate = response_vision.candidates[0]
                            if hasattr(first_candidate, 'finish_reason'):
                                finish_reason_enum = first_candidate.finish_reason
                                finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                    except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка извлечения причины пустого ответа: {e_inner_reason}")
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}")
                    if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не смогла описать изображение. (Блокировка: {block_reason_str})"
                    elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель не смогла описать изображение. (Причина: {finish_reason_str})"
                    else: reply = "🤖 Не удалось понять, что на изображении (пустой ответ)."
                    break
                if reply and "Не удалось понять" not in reply and "не смогла описать" not in reply:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Успешный анализ на попытке {attempt + 1}.")
                     break
            except (BlockedPromptException, StopCandidateException) as e_block_stop:
                 reason_str = "неизвестна"
                 try:
                     if hasattr(e_block_stop, 'args') and e_block_stop.args: reason_str = str(e_block_stop.args[0])
                 except Exception: pass
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
                 reply = f"❌ Анализ изображения заблокирован/остановлен моделью."
                 break
            except Exception as e:
                 error_message = str(e)
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
                 is_retryable = "500" in error_message or "503" in error_message
                 # Обработка 4xx ошибок
                 if "400" in error_message or "429" in error_message or "location is not supported" in error_message:
                      reply = f"❌ Ошибка при анализе изображения ({error_message[:100]}...)."
                      break
                 elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                     wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ожидание {wait_time:.1f} сек...")
                     await asyncio.sleep(wait_time)
                     continue
                 else:
                     logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False)
                     if reply is None: reply = f"❌ Ошибка при анализе изображения после {attempt + 1} попыток."
                     break
        # --- Конец блока ретраев ---

        # Сохранение в chat_history и отправка
        chat_history = context.chat_data.setdefault("history", []) # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИЗМЕНЕНО

        # 1. Запись пользователя с file_id и User ID
        user_text_for_history_vision = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото.")
        history_entry_user = {
            "role": "user",
            "parts": [{"text": user_text_for_history_vision}],
            "image_file_id": photo_file_id, # Сохраняем ID для reanalyze
            "user_id": user_id,
            "message_id": user_message_id
            }
        chat_history.append(history_entry_user)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (Vision) в chat_history с image_file_id.")

        # 2. Ответ модели (с префиксом или ошибка)
        if reply and "❌" not in reply and "🤖" not in reply: # Если успешный ответ
            model_reply_text_with_prefix = f"{IMAGE_DESCRIPTION_PREFIX}{reply}"
        else: # Если ошибка или пустой ответ
            model_reply_text_with_prefix = reply if reply else "🤖 Не удалось проанализировать изображение."

        history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]} # Сохраняем с префиксом
        chat_history.append(history_entry_model)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (Vision) в chat_history.")

        # 3. Отправка пользователю "чистого" ответа (без префикса)
        reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text_with_prefix
        if reply_to_send:
            await send_reply(message, reply_to_send, context)
        else: # На всякий случай, если reply_to_send пуст
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Нет ответа для отправки пользователю после всех попыток.")
             try: await message.reply_text("🤖 К сожалению, не удалось проанализировать изображение.")
             except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")

        # Ограничиваем историю
        while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

# ===== Обработчик документов (обновлен для chat_data через handle_message) =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message # Сохраняем для передачи в handle_message

    if not message or not message.document:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_document нет документа.")
        return

    doc = message.document
    # Проверка MIME типов
    allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql')
    allowed_mime_types = ('application/octet-stream',) # Разрешаем как текстовый по умолчанию
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types

    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})")
        return

    # Проверка размера файла
    MAX_FILE_SIZE_MB = 15
    file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0 and doc.file_name: # Игнорируем пустые файлы без имени (системные?)
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
        await update.message.reply_text("❌ Не удалось загрузить файл.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Определение кодировки и декодирование
    text = None
    detected_encoding = None
    # Список кодировок для попытки + обработка BOM
    encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1', 'cp866', 'iso-8859-5']
    # Попытка использовать chardet, если установлен
    chardet_available = False
    try:
        import chardet
        chardet_available = True
    except ImportError:
        logger.info("Библиотека chardet не найдена. Автоопределение кодировки будет ограничено.")

    if chardet_available:
        try:
            # Ограничиваем размер для chardet, чтобы не тратить время на большие файлы
            chardet_limit = min(len(file_bytes), 50 * 1024) # Проверяем первые 50KB
            if chardet_limit > 0:
                 detected = chardet.detect(file_bytes[:chardet_limit])
                 if detected and detected['encoding'] and detected['confidence'] > 0.7: # Порог уверенности 0.7
                      potential_encoding = detected['encoding'].lower()
                      logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet определил: {potential_encoding} (уверенность: {detected['confidence']:.2f}) для '{doc.file_name}'")
                      # Проверяем на UTF-8 BOM отдельно, т.к. chardet может сказать просто 'utf-8'
                      if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'):
                           logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружен UTF-8 BOM, используем 'utf-8-sig'.")
                           detected_encoding = 'utf-8-sig'
                           if 'utf-8-sig' not in encodings_to_try: encodings_to_try.insert(0, 'utf-8-sig')
                           if 'utf-8' in encodings_to_try: encodings_to_try.remove('utf-8')
                      else:
                           detected_encoding = potential_encoding
                           # Добавляем определенную кодировку в начало списка попыток
                           if detected_encoding in encodings_to_try: encodings_to_try.remove(detected_encoding)
                           encodings_to_try.insert(0, detected_encoding)
                 else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet не уверен ({detected.get('confidence', 0):.2f}) или не определил кодировку для '{doc.file_name}'.")
        except Exception as e_chardet:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при использовании chardet для '{doc.file_name}': {e_chardet}")

    # Убираем дубликаты кодировок, сохраняя порядок
    unique_encodings = list(dict.fromkeys(encodings_to_try))
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Попытки декодирования для '{doc.file_name}': {unique_encodings}")
    # Пытаемся декодировать
    for encoding in unique_encodings:
        try:
            text = file_bytes.decode(encoding)
            detected_encoding = encoding # Запоминаем успешную кодировку
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' успешно декодирован как {encoding}.")
            break # Выходим после первой успешной попытки
        except (UnicodeDecodeError, LookupError):
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' не в кодировке {encoding}.")
        except Exception as e_decode:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Неожиданная ошибка при декодировании '{doc.file_name}' как {encoding}: {e_decode}", exc_info=True)

    # Если декодировать не удалось
    if text is None:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось декодировать '{doc.file_name}' ни одной из кодировок: {unique_encodings}")
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте сохранить его в кодировке UTF-8.", parse_mode=ParseMode.MARKDOWN)
        return
    # Если файл декодирован, но текст пустой (например, файл только из BOM)
    if not text.strip() and len(file_bytes) > 0:
         logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' дал пустой текст после декодирования ({detected_encoding}).")
         await update.message.reply_text(f"⚠️ Не удалось извлечь читаемый текст из файла `{doc.file_name}` (возможно, он пуст или содержит только непечатаемые символы).", parse_mode=ParseMode.MARKDOWN)
         return

    # Обрезка текста, если он слишком длинный
    # Используем MAX_CONTEXT_CHARS как ориентир, но берем с запасом (например, половину)
    # Также учитываем примерную длину токена к символу (1 токен ~ 4 символа)
    approx_max_tokens_for_file = MAX_OUTPUT_TOKENS * 2 # Оставляем место для ответа модели
    MAX_FILE_CHARS = min(MAX_CONTEXT_CHARS // 2, approx_max_tokens_for_file * 4)
    truncated_text = text
    truncation_warning = ""
    if len(text) > MAX_FILE_CHARS:
        truncated_text = text[:MAX_FILE_CHARS]
        # Пытаемся обрезать по последнему переносу строки для читаемости
        last_newline = truncated_text.rfind('\n')
        if last_newline > MAX_FILE_CHARS * 0.8: # Обрезаем по переносу, если он не слишком рано
            truncated_text = truncated_text[:last_newline]
        chars_k = len(truncated_text) // 1000
        truncation_warning = f"\n\n**(⚠️ Текст файла был обрезан до ~{chars_k}k символов)**"
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {len(truncated_text)} символов.")

    # Формируем текст для передачи в handle_message (без User ID, он добавится там)
    user_caption = message.caption if message.caption else ""
    file_name = doc.file_name or "файл"
    encoding_info = f"(предположительно {detected_encoding})" if detected_encoding else "(кодировка неизвестна)"
    file_context = f"Содержимое файла `{file_name}` {encoding_info}:\n```\n{truncated_text}\n```{truncation_warning}"
    if user_caption:
        # Экранируем кавычки в подписи на всякий случай
        safe_caption = user_caption.replace('"', '\\"')
        user_prompt_doc = f"Пользователь загрузил файл `{file_name}` с комментарием: \"{safe_caption}\". {file_context}\nПроанализируй, пожалуйста."
    else:
        user_prompt_doc = f"Пользователь загрузил файл `{file_name}`. {file_context}\nЧто можешь сказать об этом тексте?"

    # Передаем управление в handle_message, модифицируя текст сообщения
    # Не передаем file_id, т.к. контекст уже в тексте
    message.text = user_prompt_doc # Перезаписываем текст
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с текстом документа.")
    await handle_message(update, context) # Используем оригинальный update с измененным message

# ====================================================================

# --- Функции веб-сервера и запуска ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    # Указываем типы данных для user_data и chat_data для type hints (если нужно)
    # application = Application.builder().token(TELEGRAM_BOT_TOKEN).user_data_persistence(...).chat_data_persistence(...).build()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Создаем сессию aiohttp при запуске
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
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo)) # Обновленный
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document)) # Обновленный
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)) # Обновленный

    try:
        await application.initialize()
        # Формируем URL вебхука аккуратно
        webhook_host_cleaned = WEBHOOK_HOST.rstrip('/')
        webhook_path_segment = GEMINI_WEBHOOK_PATH.strip('/')
        webhook_url = f"{webhook_host_cleaned}/{webhook_path_segment}"
        logger.info(f"Попытка установки вебхука: {webhook_url}")
        # Установка секретного токена, если он есть в переменных окружения
        secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES, # Получаем все типы апдейтов
            drop_pending_updates=True, # Пропускаем старые апдейты при рестарте
            secret_token=secret_token if secret_token else None # Устанавливаем токен, если он задан
        )
        logger.info(f"Вебхук успешно установлен на {webhook_url}" + (" с секретным токеном." if secret_token else "."))

        # Запускаем веб-сервер для обработки вебхуков
        web_server_coro = run_web_server(application, stop_event)
        return application, web_server_coro
    except Exception as e:
        logger.critical(f"Критическая ошибка при инициализации бота или установке вебхука: {e}", exc_info=True)
        # Закрываем сессию, если она была создана
        if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed:
             await application.bot_data['aiohttp_session'].close()
             logger.info("Сессия aiohttp закрыта из-за ошибки инициализации.")
        raise # Пробрасываем исключение дальше

async def run_web_server(application: Application, stop_event: asyncio.Event):
    """Запускает веб-сервер aiohttp для приема вебхуков Telegram."""
    app = aiohttp.web.Application()

    # Добавляем health check эндпоинт
    async def health_check(request):
        try:
            # Проверяем доступность бота через get_me()
            bot_info = await application.bot.get_me()
            if bot_info:
                logger.debug("Health check successful.")
                return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
            else:
                logger.warning("Health check: Bot info unavailable (get_me вернул None).")
                return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except TelegramError as e_tg:
             logger.error(f"Health check failed (TelegramError): {e_tg}", exc_info=True)
             return aiohttp.web.Response(text=f"Error: Telegram API error ({type(e_tg).__name__})", status=503)
        except Exception as e:
             logger.error(f"Health check failed (Exception): {e}", exc_info=True)
             return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)

    app.router.add_get('/', health_check) # Health check на корневом пути

    # Сохраняем приложение бота для доступа в обработчике вебхука
    app['bot_app'] = application

    # Определяем путь для вебхука
    webhook_path = GEMINI_WEBHOOK_PATH.strip('/')
    if not webhook_path.startswith('/'):
        webhook_path = '/' + webhook_path
    app.router.add_post(webhook_path, handle_telegram_webhook) # Регистрируем обработчик POST запросов
    logger.info(f"Вебхук будет слушаться на пути: {webhook_path}")

    # Настраиваем и запускаем веб-сервер
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000")) # Порт из переменной окружения или 10000
    host = os.getenv("HOST", "0.0.0.0")    # Хост из переменной окружения или 0.0.0.0
    site = aiohttp.web.TCPSite(runner, host, port)

    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на http://{host}:{port}")
        # Ожидаем события остановки (например, по сигналу)
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Задача веб-сервера отменена.")
    except Exception as e:
        logger.error(f"Ошибка при запуске или работе веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally:
        logger.info("Начало остановки веб-сервера...")
        await runner.cleanup() # Корректно останавливаем сервер
        logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Обрабатывает входящие запросы от Telegram (вебхуки)."""
    application = request.app.get('bot_app')
    if not application:
        logger.critical("Приложение бота не найдено в контексте веб-сервера!")
        return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")

    # Проверка секретного токена, если он установлен
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token:
         header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
         if header_token != secret_token:
             logger.warning(f"Неверный секретный токен в заголовке от {request.remote}. Ожидался: ...{secret_token[-4:]}, Получен: {header_token}")
             return aiohttp.web.Response(status=403, text="Forbidden: Invalid secret token.")

    try:
        # Получаем JSON данные из запроса
        data = await request.json()
        # Десериализуем данные в объект Update
        update = Update.de_json(data, application.bot)
        logger.debug(f"Получен Update ID: {update.update_id} от Telegram.")
        # Запускаем обработку апдейта асинхронно (но ждем завершения перед ответом Telegram)
        # Убрали create_task/shield, т.к. это вызывало проблемы с доступом к user_data/chat_data
        await application.process_update(update)
        # Отвечаем Telegram, что апдейт получен и принят в обработку
        return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e_json:
         # Ошибка парсинга JSON от Telegram
         body = await request.text() # Получаем тело запроса как текст для логгирования
         logger.error(f"Ошибка декодирования JSON от Telegram: {e_json}. Тело запроса: {body[:500]}...")
         return aiohttp.web.Response(text="Bad Request: JSON decode error", status=400)
    except TelegramError as e_tg:
        # Ошибки, связанные с API Telegram при обработке Update
        logger.error(f"Ошибка Telegram при обработке вебхука: {e_tg}", exc_info=True)
        # В зависимости от ошибки можно вернуть 500 или другую ошибку Telegram
        return aiohttp.web.Response(text=f"Internal Server Error: Telegram API Error ({type(e_tg).__name__})", status=500)
    except Exception as e:
        # Любые другие непредвиденные ошибки при обработке
        logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    # Настройка уровней логгирования для разных библиотек
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    # Устанавливаем базовый формат логгирования
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO) # Установим INFO по умолчанию, чтобы видеть старт

    # Понижаем уровень логгирования для шумных библиотек
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('google.api_core').setLevel(logging.WARNING)
    logging.getLogger('google.auth').setLevel(logging.WARNING) # Добавил google.auth
    logging.getLogger('google.generativeai').setLevel(logging.INFO) # Оставляем INFO для Gemini
    logging.getLogger('duckduckgo_search').setLevel(logging.INFO)
    logging.getLogger('PIL').setLevel(logging.INFO)
    logging.getLogger('pytesseract').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING) # Логи доступа веб-сервера
    logging.getLogger('telegram.ext').setLevel(logging.INFO) # Логи обработчиков PTB
    logging.getLogger('telegram.bot').setLevel(logging.INFO) # Логи самого бота PTB
    # Устанавливаем уровень для нашего логгера
    logger.setLevel(log_level)
    logger.info(f"--- Установлен уровень логгирования для '{logger.name}': {log_level_str} ({log_level}) ---")

    # Настройка обработчиков сигналов для корректной остановки
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event() # Событие для сигнализации об остановке

    def signal_handler():
        if not stop_event.is_set():
            logger.info("Получен сигнал SIGINT/SIGTERM, инициирую остановку...")
            stop_event.set() # Устанавливаем событие, чтобы основной цикл и сервер могли завершиться
        else:
            logger.warning("Повторный сигнал остановки получен, процесс уже завершается.")

    # Добавляем обработчики для SIGINT (Ctrl+C) и SIGTERM
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             # Для Windows loop.add_signal_handler может не работать
             logger.warning(f"Не удалось установить обработчик сигнала {sig} через loop. Использую signal.signal().")
             try:
                 signal.signal(sig, lambda s, f: signal_handler())
             except Exception as e_signal:
                 logger.error(f"Не удалось установить обработчик сигнала {sig} через signal.signal(): {e_signal}")

    application = None
    web_server_task = None
    aiohttp_session_main = None # Сохраним сессию здесь для закрытия

    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        # Настраиваем бота и запускаем веб-сервер
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask") # Запускаем сервер как задачу
        # Получаем созданную сессию для последующего закрытия
        aiohttp_session_main = application.bot_data.get('aiohttp_session')

        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)...")
        # Основной цикл ожидает события остановки
        await stop_event.wait()

    except asyncio.CancelledError:
        logger.info("Главная задача main() была отменена.")
    except Exception as e:
        logger.critical("Критическая ошибка во время запуска или ожидания.", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки приложения ---")
        # Убедимся, что событие установлено, даже если выход был по другой причине
        if not stop_event.is_set(): stop_event.set()

        # 1. Останавливаем веб-сервер
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера (через stop_event)...")
             try:
                 # Даем время веб-серверу завершиться после установки stop_event
                 await asyncio.wait_for(web_server_task, timeout=15.0)
                 logger.info("Веб-сервер успешно завершен.")
             except asyncio.TimeoutError:
                 logger.warning("Веб-сервер не завершился за 15 секунд, принудительная отмена...")
                 web_server_task.cancel()
                 try: await web_server_task # Ждем завершения отмененной задачи
                 except asyncio.CancelledError: logger.info("Задача веб-сервера успешно отменена.")
                 except Exception as e_cancel_ws: logger.error(f"Ошибка при ожидании отмененной задачи веб-сервера: {e_cancel_ws}", exc_info=True)
             except asyncio.CancelledError: # Если сама задача main была отменена во время ожидания
                 logger.info("Ожидание веб-сервера было отменено.")
             except Exception as e_wait_ws: # Другие ошибки при ожидании
                 logger.error(f"Ошибка при ожидании завершения веб-сервера: {e_wait_ws}", exc_info=True)

        # 2. Останавливаем приложение PTB
        if application:
            logger.info("Остановка приложения Telegram бота (application.shutdown)...")
            try:
                await application.shutdown()
                logger.info("Приложение Telegram бота успешно остановлено.")
            except Exception as e_shutdown:
                logger.error(f"Ошибка во время application.shutdown(): {e_shutdown}", exc_info=True)

        # 3. Закрываем сессию aiohttp
        if aiohttp_session_main and not aiohttp_session_main.closed:
             logger.info("Закрытие основной сессии aiohttp...")
             await aiohttp_session_main.close()
             # Небольшая пауза для завершения закрытия соединений
             await asyncio.sleep(0.5)
             logger.info("Основная сессия aiohttp закрыта.")

        # 4. Отменяем и ждем завершения оставшихся задач (на всякий случай)
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
                     logger.warning(f"Ошибка в отмененной задаче '{task_name}': {res}", exc_info=True) # Логгируем ошибки отмененных задач
                 else:
                      logger.debug(f"Задача '{task_name}' завершилась с результатом: {res}") # Если задача не была отменена и не вызвала исключение
            logger.info(f"Фоновые задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")

        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Перехватываем KeyboardInterrupt здесь, т.к. signal handler может не успеть сработать до выхода из asyncio.run
        logger.info("Приложение прервано пользователем (KeyboardInterrupt в main).")
    except Exception as e_top:
        # Ловим любые другие ошибки на самом верхнем уровне
        logger.critical("Неперехваченная ошибка на верхнем уровне asyncio.run(main).", exc_info=True)

# --- END OF FILE main.py ---
