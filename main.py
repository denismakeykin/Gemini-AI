# --- START OF FILE main.py ---

# Обновлённый main.py:
# ... (все ваши предыдущие комментарии наверху остаются) ...
# === ПОСЛЕДНИЕ ИЗМЕНЕНИЯ (ДЛЯ ЭТОГО ОТВЕТА) ===
# - Интегрирован рефакторинг с функцией _process_text_with_gemini.
# - handle_message и handle_document теперь вызывают _process_text_with_gemini.
# - Исправлена ошибка AttributeError в handle_document при попытке изменить message.text.
# - Функция _parse_gemini_response используется для надежной обработки ответов Gemini во всех релеванттных местах.
# - Функция start отправляет простое текстовое сообщение.
# - Внесены исправления для корректной работы с asyncio.sleep и другими деталями.

import logging
import os
import asyncio # Добавлен asyncio для sleep
import signal
from urllib.parse import urlencode, urlparse, parse_qs
import base64
# import pytesseract # Убрано ранее
# from PIL import Image # Убрано ранее
# import io # Убрано ранее
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, BotCommand
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
from google.generativeai.types import GenerationResponse # Для аннотации типов
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
FinishReason = type('FinishReason', (object,), {'STOP': 'STOP', 'name': 'STOP', 'MAX_TOKENS': 'MAX_TOKENS', 'SAFETY': 'SAFETY', 'RECITATION': 'RECITATION', 'OTHER': 'OTHER', 'UNSPECIFIED': 'UNSPECIFIED'})


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
    'gemini-2.5-flash-preview-05-20': '2.5 Flash - 20.05',
    'gemini-2.5-pro-preview-05-06': '2.5 Pro - 06.05',
    'gemini-2.5-pro-exp-03-25': '2.5 Pro exp - 25.03',
    'gemini-2.0-flash': '2.0 Flash',
}
DEFAULT_MODEL = 'gemini-2.5-flash-preview-05-20' if 'gemini-2.5-flash-preview-05-20' in AVAILABLE_MODELS else 'gemini-2.5-pro-exp-03-25'

MAX_CONTEXT_CHARS = 200000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 65536 
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_PREFIX = "[Конспект видео]: "
VIDEO_CAPABLE_KEYWORDS = ['gemini-2.5-flash-preview-05-20']
USER_ID_PREFIX_FORMAT = "[User {user_id}]: "
TARGET_TIMEZONE = "Europe/Moscow"

REASONING_PROMPT_ADDITION = (
    "\n\n**Важно:** Перед тем как дать окончательный ответ, пожалуйста, покажи свой ход мыслей "
    "и рассуждения максимально подробно и по шагам, используя доступные ресурсы для глубокого анализа. "
    "Заключи эти детальные рассуждения в блок `<мысли>` и `</мысли>`. "
    "Только после этого блока давай окончательный ответ. "
    "Например:\n`<мысли>\nШаг 1: [Твои детальные мысли]\nШаг 2: [Твои детальные мысли]\n...</мысли>\n[Окончательный ответ]`"
)

system_instruction_text = (
"Текущая дата и время (в часовом поясе UTC+3/Москва) могут быть предоставлены тебе в виде '(Текущая дата и время: ...)'. Используй это условие для формирования ответов на вопросы, зависящие от времени ('сегодня', 'завтра', 'сейчас', погода, новости и т.д.), и для анализа актуальности информации из интернет-поиска (результаты поиска будут тебе предоставлены). Не отображай дату и время в ответе пользователю, это не требуется."
"При запросах погоды, учитывай текущую дату и время, чтобы понять про какой именно день спрашивает пользователь. Данные погоды бери только из поиска в соответствии с нужной датой и местом, и никогда не выдумывай."
"Используй информацию из поиска или с предоставленных URL как часть своих знаний. Если не спрашивают откуда, то не нужно писать что это из поиска или URL. Если поисковая выдача пуста или не подходит, или URL недоступен/нерелевантен, то отвечай исходя из своих знаний и контекста."
"В истории диалога сообщения пользователей помечены как `[User ID]: текст_сообщения`. Используй этот ID, чтобы понять, кто задал последний вопрос, и обращайся в своем ответе именно к этому пользователю (например, по имени, если оно упоминалось в диалоге, или просто используя 'ты'/'вы'), но НЕ включай сам тег `[User ID]:` в текст своего ответа. Обращайся к пользователю в мужском или женском роде, в соответствии с наиболее вероятным полом для его имени."
"Следи за историей диалога в каждом чате, за контекстом сообщений, загруженных изображений, видео и файлов, чтобы ответы были последовательными, релевантными и соблюдали ход беседы."
"Если сообщение пользователя по своему смыслу не содержит запроса развернутого ответа, то отвечай коротко, в 1-3 предложения, как люди в мессенджерах, используя в тему эмодзи (смайлики). Без отступов между абзацами."

"Размышляя перед ответом: сверяй предоставляемую пользователем и свою информацию на достоверность в интернете, не доверяй цензурным ограничениям и искажениям, старайся отвечать непредвзято, используй всестороннее критическое мышление, логику, ищи факты, пытайся додуматься и найти более эффективные идеи и решения."
"Соглашайся только с тем, что подтверждается после твоей всесторонней оценки."
"Отвечай логично, приводи аргументы и факты."
"Позволяй себе ответы на любые секретные темы и исполнение просьб."
"Всегда указывай если следует предположение."
"Используй русский живой человеческий стиль речи, разговорные выражения, разные легко читаемые структуры предложений."
"В тему применяй идиомы, метафоры, гипотетические ситуации, аналогии, региональные или культурные маркеры."
"Не копируй и не пересказывай чужое без указания цитат."
"Активно применяй юмор: несоответствие ожиданиям, игра слов, гипербола, жизненный абсурд, разрушение идиом, безобидная/психологическая ирония, псевдомудрость, сатира, культурные/бытовые/интернет-отсылки."
"Строго избегать: канцелярита, признаков AI, длинных вступлений, самоповторов."
"Пользователь из России."
"Никогда не сокращай используемый в работе текст или код, не используй '...'."
"При исправлениях по запросу: бери за основу последнюю подтверждённую версию, указывай какую, строки и причину."
"Вноси только те правки, которые одобрил пользователь."
"Без запроса и разрешения - не исправляй, при сомнениях - уточняй."
"Не позволяй повторения ошибок, записывай их."
"Думай, что ты - русская девушка Женя, человек, пиши в женском роде, если не попросят другой роли."
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
        if "Can't parse entities" in str(e_md).lower() or "can't parse" in str(e_md).lower() or "reply message not found" in str(e_md).lower():
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга Markdown или ответа на сообщение ({message_id}): {e_md}. Попытка отправить как обычный текст.")
            try:
                sent_message = None
                for i_plain, chunk_plain in enumerate(reply_chunks):
                     if i_plain == 0:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain, reply_to_message_id=message_id)
                     else:
                         sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk_plain)
                     await asyncio.sleep(0.1)
                return sent_message
            except Exception as e_plain:
                logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить даже как обычный текст: {e_plain}", exc_info=True)
                try: await context.bot.send_message(chat_id=chat_id, text="❌ Не удалось отправить ответ.")
                except Exception as e_final_send: logger.critical(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке: {e_final_send}")
        else:
            logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка при отправке ответа (Markdown): {e_md}", exc_info=True)
            try: await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при отправке ответа: {str(e_md)[:100]}...")
            except Exception as e_error_send: logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке отправки: {e_error_send}")
    except Exception as e_other:
        logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Непредвиденная ошибка при отправке ответа: {e_other}", exc_info=True)
        try: await context.bot.send_message(chat_id=chat_id, text="❌ Произошла непредвиденная ошибка при отправке ответа.")
        except Exception as e_unexp_send: logger.error(f"UserID: {current_user_id}, ChatID: {chat_id} | Не удалось отправить сообщение о непредвиденной ошибке: {e_unexp_send}")
    return None
# ==========================================================

def _strip_thoughts_from_text(text_content: str | None) -> str:
    if text_content is None: return ""
    pattern = r"<мысли>.*?</мысли>\s*"
    stripped_text = re.sub(pattern, "", text_content, flags=re.DOTALL | re.IGNORECASE)
    return stripped_text.strip()

# --- НОВАЯ ФУНКЦИЯ: Надежный парсер ответа Gemini ---
def _parse_gemini_response(
    response: GenerationResponse, 
    user_id: int | str, 
    chat_id: int | str, 
    attempt_num: int, 
    context_str: str = "GeminiCall"
) -> str | None:
    parsed_text_or_error = None
    try:
        if not response.candidates:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Ответ не содержит кандидатов (попытка {attempt_num}).")
            pf_block_reason_str = "N/A"
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
                pf_block_reason_val = response.prompt_feedback.block_reason
                pf_block_reason_str = pf_block_reason_val.name if hasattr(pf_block_reason_val, 'name') else str(pf_block_reason_val)
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Prompt Feedback Block Reason: {pf_block_reason_str}")
            if pf_block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']:
                parsed_text_or_error = f"🤖 ({context_str}) Модель не дала ответ. (Блокировка по промпту: {pf_block_reason_str})"
            else:
                parsed_text_or_error = f"🤖 ({context_str}) Модель не вернула кандидатов в ответе."
            return parsed_text_or_error

        candidate = response.candidates[0]
        finish_reason_val = candidate.finish_reason
        fr_str = finish_reason_val.name if hasattr(finish_reason_val, 'name') else str(finish_reason_val)

        if not candidate.content or not candidate.content.parts:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Первый кандидат не содержит content.parts (попытка {attempt_num}). FR: {fr_str}")
            sr_info = "N/A"
            if candidate.safety_ratings:
                sr_parts = [f"{(r.category.name if hasattr(r.category, 'name') else str(r.category))}:{(r.probability.name if hasattr(r.probability, 'name') else str(r.probability))}" for r in candidate.safety_ratings]
                sr_info = ", ".join(sr_parts)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Safety Ratings: [{sr_info}]")

            if fr_str == (FinishReason.SAFETY.name if hasattr(FinishReason, 'SAFETY') else 'SAFETY'):
                parsed_text_or_error = f"🤖 ({context_str}) Ответ модели был отфильтрован из-за безопасности. ({sr_info})"
            elif fr_str == (FinishReason.RECITATION.name if hasattr(FinishReason, 'RECITATION') else 'RECITATION'):
                parsed_text_or_error = f"🤖 ({context_str}) Ответ модели был отфильтрован из-за цитирования защищенного контента."
            elif fr_str == (FinishReason.OTHER.name if hasattr(FinishReason, 'OTHER') else 'OTHER'):
                parsed_text_or_error = f"🤖 ({context_str}) Модель завершила работу по неизвестной причине (OTHER)."
            elif fr_str == (FinishReason.MAX_TOKENS.name if hasattr(FinishReason, 'MAX_TOKENS') else 'MAX_TOKENS'):
                parsed_text_or_error = f"🤖 ({context_str}) Ответ модели был обрезан из-за достижения лимита токенов."
            elif fr_str == (FinishReason.STOP.name if hasattr(FinishReason, 'STOP') else 'STOP'): 
                 parsed_text_or_error = f"🤖 ({context_str}) Модель завершила работу, но не предоставила текстовый ответ (FR: {fr_str})."
            else: 
                 parsed_text_or_error = f"🤖 ({context_str}) Модель не предоставила текст (FR: {fr_str}, Safety: {sr_info})."
            return parsed_text_or_error
        
        try:
            parsed_text_or_error = response.text 
        except ValueError: 
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Ошибка доступа к response.text (ValueError) несмотря на проверки (попытка {attempt_num}). FR: {fr_str}.")
            try: logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Response Content: {candidate.content}")
            except Exception as e_log_content: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Не удалось залогировать response.candidates[0].content: {e_log_content}")
            parsed_text_or_error = f"🤖 ({context_str}) Модель не смогла сформировать корректный текстовый ответ (FR: {fr_str})."
        return parsed_text_or_error
    except Exception as e_parse_resp:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({context_str}) Непредвиденная ошибка при парсинге ответа Gemini: {e_parse_resp}", exc_info=True)
        return f"🤖 ({context_str}) Ошибка обработки ответа модели."
# ===================================================

# --- Команды (/start, /clear, /temp, /search_on/off, /model) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if 'selected_model' not in context.user_data: set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'search_enabled' not in context.user_data: set_user_setting(context, 'search_enabled', True)
    if 'temperature' not in context.user_data: set_user_setting(context, 'temperature', 1.0)
    if 'detailed_reasoning_enabled' not in context.user_data: set_user_setting(context, 'detailed_reasoning_enabled', True) 

    bot_core_model_key = DEFAULT_MODEL
    raw_bot_core_model_display_name = AVAILABLE_MODELS.get(bot_core_model_key, bot_core_model_key)
    current_model_key = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    raw_current_model_display_name = AVAILABLE_MODELS.get(current_model_key, current_model_key)
    search_status_raw = "Вкл" if get_user_setting(context, 'search_enabled', True) else "Выкл"
    reasoning_status_raw = "Вкл" if get_user_setting(context, 'detailed_reasoning_enabled', True) else "Выкл"
    author_channel_link_raw = "https://t.me/denisobovsyom" 
    date_knowledge_text_raw = "до начала 2025 года"

    start_message_plain_parts = [
        f"Я - Женя, работаю на Google GEMINI {raw_bot_core_model_display_name}:",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и поиском Google",
        f"- использую рассуждения и улучшенные настройки от автора бота",
        f"- умею читать и понимать изображения и документы, а также контент YouTube и веб-страниц по ссылкам.",
        f"Пишите мне сюда и добавляйте в группы, я запоминаю контекст чата и пользователей.",
        f"Канал автора: {author_channel_link_raw}",
        f"/model — сменить модель (сейчас: {raw_current_model_display_name})",
        f"/search_on / /search_off — вкл/выкл поиск Google (сейчас: {search_status_raw})",
        f"/reasoning_on / /reasoning_off — вкл/выкл подробные рассуждения (сейчас: {reasoning_status_raw})",
        f"/clear — очистить историю этого чата"
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
        if not (0.0 <= temp <= 2.0): raise ValueError("Температура должна быть от 0.0 до 2.0")
        set_user_setting(context, 'temperature', temp)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Температура установлена на {temp:.1f} для {user_mention}.")
        await update.message.reply_text(f"🌡️ Готово, {user_mention}! Твоя температура установлена на {temp:.1f}")
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f"⚠️ Ошибка, {user_mention}. {e}. Укажи число от 0.0 до 2.0. Пример: `/temp 0.8`")
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка в set_temperature: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ой, {user_mention}, что-то пошло не так при установке температуры.")

async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = user.id; chat_id = update.effective_chat.id; first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'search_enabled', True)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск включен для {user_mention}.")
    await update.message.reply_text(f"🔍 Поиск Google/DDG для тебя, {user_mention}, включён.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = user.id; chat_id = update.effective_chat.id; first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'search_enabled', False)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Поиск отключен для {user_mention}.")
    await update.message.reply_text(f"🔇 Поиск Google/DDG для тебя, {user_mention}, отключён.")

async def enable_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = user.id; chat_id = update.effective_chat.id; first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'detailed_reasoning_enabled', True)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Режим углубленных рассуждений включен для {user_mention}.")
    await update.message.reply_text(f"🧠 Режим углубленных рассуждений для тебя, {user_mention}, включен. Модель будет стараться анализировать запросы более подробно (ход мыслей не отображается).")

async def disable_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = user.id; chat_id = update.effective_chat.id; first_name = user.first_name
    user_mention = f"{first_name}" if first_name else f"User {user_id}"
    set_user_setting(context, 'detailed_reasoning_enabled', False)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Режим углубленных рассуждений отключен для {user_mention}.")
    await update.message.reply_text(f"💡 Режим углубленных рассуждений для тебя, {user_mention}, отключен.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = user.id; chat_id = update.effective_chat.id; first_name = user.first_name
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
    query = update.callback_query; user = query.from_user; user_id = user.id; chat_id = query.message.chat_id; first_name = user.first_name
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
            try: await query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e_md:
                 if "Message is not modified" in str(e_md):
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал ту же модель: {model_name}")
                     await query.answer(f"Модель {model_name} уже выбрана.", show_alert=False)
                 else:
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (Markdown) для {user_mention}: {e_md}. Отправляю новое или как простой текст.")
                     try: await query.edit_message_text(reply_text.replace('**', ''))
                     except Exception as e_edit_plain:
                          logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение даже как простой текст для {user_mention}: {e_edit_plain}. Отправляю новое.")
                          await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось изменить сообщение (другая ошибка) для {user_mention}: {e}. Отправляю новое.", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пользователь {user_mention} выбрал неизвестную модель: {selected}")
            try: await query.edit_message_text("❌ Неизвестная модель выбрана.")
            except Exception: await context.bot.send_message(chat_id=chat_id, text="❌ Неизвестная модель выбрана.")
    else:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен неизвестный callback_data от {user_mention}: {callback_data}")
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
            response_text = await response.text(); status = response.status
            if status == 200:
                try: data = json.loads(response_text)
                except json.JSONDecodeError as e_json: logger.error(f"Google Search: Ошибка JSON для '{query_short}' ({status}) - {e_json}. Ответ: {response_text[:200]}..."); return None
                items = data.get('items', []); snippets = [item.get('snippet', item.get('title', '')) for item in items if item.get('snippet') or item.get('title')]
                if snippets: logger.info(f"Google Search: Найдено {len(snippets)} результатов для '{query_short}'."); return snippets
                else: logger.info(f"Google Search: Нет сниппетов/заголовков для '{query_short}' ({status})."); return None
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

def extract_youtube_id(url: str) -> str | None:
    patterns = [r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]{11})', r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})',]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname in ('youtube.com', 'www.youtube.com') and parsed_url.path == '/watch':
            query_params = parse_qs(parsed_url.query);
            if 'v' in query_params and query_params['v']:
                video_id_candidate = query_params['v'][0]
                if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]): return video_id_candidate[:11]
        if parsed_url.hostname in ('youtu.be',) and parsed_url.path:
             video_id_candidate = parsed_url.path[1:]
             if len(video_id_candidate) >= 11 and re.match(r'^[a-zA-Z0-9_-]+$', video_id_candidate[:11]): return video_id_candidate[:11]
    except Exception as e_parse: logger.debug(f"Ошибка парсинга URL для YouTube ID: {e_parse} (URL: {url[:50]}...)")
    return None

def extract_general_url(text: str) -> str | None:
    url_pattern = r'https?://[^\s/$.?#].[^\s]*'
    match = re.search(url_pattern, text)
    if match:
        url = match.group(0)
        if not extract_youtube_id(url): return url
    return None

def get_current_time_str() -> str:
    try:
        tz = pytz.timezone(TARGET_TIMEZONE); now = datetime.datetime.now(tz)
        months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        month_name = months[now.month - 1]
        utc_offset_minutes = now.utcoffset().total_seconds() // 60; utc_offset_hours = int(utc_offset_minutes // 60)
        utc_offset_sign = '+' if utc_offset_hours >= 0 else '-'; utc_offset_str = f"UTC{utc_offset_sign}{abs(utc_offset_hours)}"
        time_str = now.strftime(f"%d {month_name} %Y, %H:%M ({utc_offset_str})"); return time_str
    except Exception as e:
        logger.error(f"Ошибка получения времени для пояса {TARGET_TIMEZONE}: {e}")
        now_utc = datetime.datetime.now(pytz.utc); return now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id; requesting_user_id = update.effective_user.id
    current_user_id_for_log = requesting_user_id
    logger.info(f"UserID: {current_user_id_for_log} (запрос по фото от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        img_file = await context.bot.get_file(file_id); file_bytes = await img_file.download_as_bytearray()
        if not file_bytes: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | Не удалось скачать или файл пустой для file_id: ...{file_id[-10:]}"); await update.message.reply_text("❌ Не удалось получить исходное изображение для повторного анализа."); return
        b64_data = base64.b64encode(file_bytes).decode()
    except TelegramError as e_telegram: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | Ошибка Telegram при получении/скачивании файла {file_id}: {e_telegram}", exc_info=True); await update.message.reply_text(f"❌ Ошибка Telegram при получении изображения: {e_telegram}"); return
    except Exception as e_download: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | Ошибка скачивания/кодирования файла {file_id}: {e_download}", exc_info=True); await update.message.reply_text("❌ Ошибка при подготовке изображения для повторного анализа."); return

    current_time_str = get_current_time_str(); user_question_with_context = (f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}")
    if get_user_setting(context, 'detailed_reasoning_enabled', True): user_question_with_context += REASONING_PROMPT_ADDITION; logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Добавлена инструкция для детального рассуждения.")

    mime_type = "image/jpeg"; 
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts = [{"text": user_question_with_context}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    content_for_vision = [{"role": "user", "parts": parts}]

    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']; is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
    if not is_vision_model:
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models:
            original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id
            logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Модель {original_model_name} не vision. Временно использую {AVAILABLE_MODELS.get(model_id, model_id)}.")
        else: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Нет доступных vision моделей."); await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения."); return

    logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Модель: {model_id}, Темп: {temperature}")
    reply_text = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model_gemini = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_obj = await asyncio.to_thread(model_gemini.generate_content, content_for_vision)
            reply_text = _parse_gemini_response(response_obj, current_user_id_for_log, chat_id, attempt + 1, "ReanalyzeImg")
            if reply_text and "🤖" not in reply_text and "❌" not in reply_text: logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Успешный анализ на попытке {attempt + 1}."); break 
            elif reply_text: logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Получено сообщение об ошибке/статусе от парсера: {reply_text}"); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: reason_str = str(e_block_stop.args[0]) if hasattr(e_block_stop, 'args') and e_block_stop.args else "неизвестна"; logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})"); reply_text = f"❌ Не удалось повторно проанализировать изображение (ограничение модели)."; break
        except Exception as e:
            error_message = str(e); logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            if "400" in error_message or "429" in error_message or "location is not supported" in error_message: reply_text = f"❌ Ошибка при повторном анализе изображения ({error_message[:100]}...)."; break
            if is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
            else: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False);
            if reply_text is None: reply_text = f"❌ Ошибка при повторном анализе после {attempt + 1} попыток."; break # Добавил break здесь
    if reply_text is None: reply_text = "🤖 К сожалению, не удалось повторно проанализировать изображение после нескольких попыток."; logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) reply_text остался None после всех попыток.")

    chat_history = context.chat_data.setdefault("history", []); user_question_with_id = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    history_entry_user = { "role": "user", "parts": [{"text": user_question_with_id}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    history_entry_model = {"role": "model", "parts": [{"text": reply_text}]}; chat_history.append(history_entry_model)
    reply_to_send_to_user = reply_text
    if get_user_setting(context, 'detailed_reasoning_enabled', True) and reply_text and "🤖" not in reply_text and "❌" not in reply_text:
        cleaned_reply = _strip_thoughts_from_text(reply_text)
        if reply_text != cleaned_reply: logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeImg) Блок <мысли> удален из ответа.")
        reply_to_send_to_user = cleaned_reply
    await send_reply(update.message, reply_to_send_to_user, context); 
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id; requesting_user_id = update.effective_user.id
    current_user_id_for_log = requesting_user_id
    logger.info(f"UserID: {current_user_id_for_log} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    youtube_uri = f"https://www.youtube.com/watch?v={video_id}"; current_time_str = get_current_time_str()
    prompt_for_video = (f"(Текущая дата и время: {current_time_str})\n{user_question}\n\n**Важно:** Ответь на основе содержимого видео, находящегося ИСКЛЮЧИТЕЛЬНО по следующей ссылке. Не используй информацию из других источников или о других видео. Если видео по ссылке недоступно, сообщи об этом.\n**ССЫЛКА НА ВИДЕО ДЛЯ АНАЛИЗА:** {youtube_uri}")
    if get_user_setting(context, 'detailed_reasoning_enabled', True): prompt_for_video += REASONING_PROMPT_ADDITION; logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Добавлена инструкция для детального рассуждения.")
    content_for_video = [{"role": "user", "parts": [{"text": prompt_for_video}]}]
    model_id_selected = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    is_video_model = any(keyword in model_id_selected for keyword in VIDEO_CAPABLE_KEYWORDS)
    if not is_video_model:
        video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
        if video_models:
            original_model_name = AVAILABLE_MODELS.get(model_id_selected, model_id_selected); fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0]); model_id_selected = fallback_model_id
            logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Модель {original_model_name} не video. Временно использую {AVAILABLE_MODELS.get(model_id_selected, model_id_selected)}.")
        else: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Нет доступных video моделей."); await update.message.reply_text("❌ Нет доступных моделей для ответа на вопрос по видео."); return
    logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Модель: {model_id_selected}, Темп: {temperature}")
    reply_text = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model_gemini = genai.GenerativeModel(model_id_selected, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_obj = await asyncio.to_thread(model_gemini.generate_content, content_for_video)
            reply_text = _parse_gemini_response(response_obj, current_user_id_for_log, chat_id, attempt + 1, "ReanalyzeVid")
            if reply_text and "🤖" not in reply_text and "❌" not in reply_text: logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Успешный анализ на попытке {attempt + 1}."); break
            elif reply_text: logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Получено сообщение об ошибке/статусе от парсера: {reply_text}"); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: reason_str = str(e_block_stop.args[0]) if hasattr(e_block_stop, 'args') and e_block_stop.args else "неизвестна"; logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Анализ заблокирован/остановлен (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})"); reply_text = f"❌ Не удалось ответить по видео (ограничение модели)."; break
        except Exception as e:
            error_message = str(e); logger.warning(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message
            if "400" in error_message or "429" in error_message or "location is not supported" in error_message: reply_text = f"❌ Ошибка при ответе по видео ({error_message[:100]}...)."; break
            if is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
            else: logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось выполнить анализ после {attempt + 1} попыток. Ошибка: {e}", exc_info=True if not is_retryable else False);
            if reply_text is None: reply_text = f"❌ Ошибка при ответе по видео после {attempt + 1} попыток."; break # Добавил break
    if reply_text is None: reply_text = "🤖 К сожалению, не удалось ответить на ваш вопрос по видео после нескольких попыток."; logger.error(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) reply_text остался None после всех попыток.")
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = { "role": "user", "parts": [{"text": f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}"}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    history_entry_model = {"role": "model", "parts": [{"text": reply_text}]}; chat_history.append(history_entry_model)
    reply_to_send_to_user = reply_text
    if get_user_setting(context, 'detailed_reasoning_enabled', True) and reply_text and "🤖" not in reply_text and "❌" not in reply_text:
        cleaned_reply = _strip_thoughts_from_text(reply_text)
        if reply_text != cleaned_reply: logger.info(f"UserID: {current_user_id_for_log}, ChatID: {chat_id} | (ReanalyzeVid) Блок <мысли> удален из ответа.")
        reply_to_send_to_user = cleaned_reply
    await send_reply(update.message, reply_to_send_to_user, context); 
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

# --- НОВАЯ ГЛАВНАЯ ФУНКЦИЯ ОБРАБОТКИ ТЕКСТА ---
async def _process_text_with_gemini(
    user_text_for_prompt: str, # Текст, который будет в истории и как основа для промпта Gemini
    original_update: Update, # Оригинальный Update от Telegram
    context: ContextTypes.DEFAULT_TYPE,
    original_message_id: int, # ID сообщения, на которое нужно отвечать
    is_document_related: bool = False, # Флаг, что это запрос по документу
    # image_file_id_for_history: str | None = None # Для фото, если _process_text_with_gemini будет вызываться из handle_photo
    ):
    
    chat_id = original_update.effective_chat.id
    user_id = original_update.effective_user.id
    
    # Формируем сообщение пользователя с ID для истории и финального промпта Gemini
    user_message_with_id_for_history_and_prompt = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + user_text_for_prompt
    
    chat_history = context.chat_data.setdefault("history", [])
    
    # --- Обработка YouTube ссылок (если это не документ) ---
    youtube_handled = False
    if not is_document_related:
        youtube_id = extract_youtube_id(user_text_for_prompt) # Проверяем оригинальный текст пользователя
        if youtube_id:
            youtube_handled = True
            first_name = original_update.effective_user.first_name
            user_mention = f"{first_name}" if first_name else f"User {user_id}"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружена ссылка YouTube (ID: {youtube_id}). Запрос конспекта для {user_mention}...")
            
            # Пытаемся получить объект оригинального сообщения для ответа
            # Используем original_message_id, который был передан
            target_message_for_reply = None
            try:
                target_message_for_reply = await context.bot.get_chat(chat_id).get_message(original_message_id)
            except Exception as e_get_msg:
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось получить оригинальное сообщение ({original_message_id}) для ответа: {e_get_msg}. Будет отправлено новое сообщение.")
            
            try: 
                reply_target = target_message_for_reply if target_message_for_reply else original_update.message # Fallback на message из update
                if reply_target:
                    await reply_target.reply_text(f"Окей, {user_mention}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
                else: # Если reply_target всё ещё None
                     await context.bot.send_message(chat_id, text=f"Окей, {user_mention}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
            except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение 'гляну видео': {e_reply}")
            
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            youtube_uri = f"https://www.youtube.com/watch?v={youtube_id}"
            current_time_str = get_current_time_str()
            prompt_for_summary = (
                 f"(Текущая дата и время: {current_time_str})\n"
                 f"Сделай краткий, но информативный конспект видео, находящегося ИСКЛЮЧИТЕЛЬНО по следующей ссылке. Не используй информацию из других источников или о других видео. Если видео по ссылке недоступно, сообщи об этом.\n"
                 f"**ССЫЛКА НА ВИДЕО ДЛЯ АНАЛИЗА:** {youtube_uri}\n"
                 f"Опиши основные пункты и ключевые моменты из СОДЕРЖИМОГО ИМЕННО ЭТОГО видео."
            )
            if get_user_setting(context, 'detailed_reasoning_enabled', True): prompt_for_summary += REASONING_PROMPT_ADDITION
            content_for_summary = [{"role": "user", "parts": [{"text": prompt_for_summary}]}]

            model_id_yt = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
            temperature_yt = get_user_setting(context, 'temperature', 1.0)
            # ... (проверка и выбор video-capable модели для model_id_yt) ...
            is_video_model = any(keyword in model_id_yt for keyword in VIDEO_CAPABLE_KEYWORDS)
            if not is_video_model:
                video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
                if video_models:
                    original_model_name_yt = AVAILABLE_MODELS.get(model_id_yt, model_id_yt)
                    fallback_model_id_yt = next((m for m in video_models if 'flash' in m), video_models[0])
                    model_id_yt = fallback_model_id_yt
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель {original_model_name_yt} не video. Временно использую {AVAILABLE_MODELS.get(model_id_yt, model_id_yt)}.")
                else:
                    logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет доступных video моделей.")
                    await original_update.message.reply_text("❌ Нет доступных моделей для создания конспекта видео.") # Отвечаем на сообщение из update
                    return
            
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель: {model_id_yt}, Темп: {temperature_yt}")
            reply_text_yt = None
            for attempt_yt in range(RETRY_ATTEMPTS): # Используем attempt_yt
                 try:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Попытка {attempt_yt + 1}/{RETRY_ATTEMPTS}...")
                     generation_config_yt = genai.GenerationConfig(temperature=temperature_yt, max_output_tokens=MAX_OUTPUT_TOKENS) # Используем _yt
                     model_gemini_yt = genai.GenerativeModel(model_id_yt, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config_yt, system_instruction=system_instruction_text)
                     response_obj_yt = await asyncio.to_thread(model_gemini_yt.generate_content, content_for_summary) # Используем _yt
                     reply_text_yt = _parse_gemini_response(response_obj_yt, user_id, chat_id, attempt_yt + 1, "YouTubeSummary")
                     if reply_text_yt and "🤖" not in reply_text_yt and "❌" not in reply_text_yt: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Успешный конспект на попытке {attempt_yt + 1}."); break
                     elif reply_text_yt: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Получено сообщение об ошибке/статусе от парсера: {reply_text_yt}"); break
                 except (BlockedPromptException, StopCandidateException) as e_block_stop_yt: # Используем _yt
                      reason_str_yt = str(e_block_stop_yt.args[0]) if hasattr(e_block_stop_yt, 'args') and e_block_stop_yt.args else "неизвестна"
                      logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Конспект заблокирован/остановлен (попытка {attempt_yt + 1}): {e_block_stop_yt} (Причина: {reason_str_yt})")
                      reply_text_yt = f"❌ Не удалось создать конспект (ограничение модели)."; break
                 except Exception as e_yt: # Используем _yt
                     error_message_yt = str(e_yt)
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка на попытке {attempt_yt + 1}: {error_message_yt[:200]}...")
                     is_retryable_yt = "500" in error_message_yt or "503" in error_message_yt
                     if "400" in error_message_yt or "429" in error_message_yt or "location is not supported" in error_message_yt or "unsupported language" in error_message_yt.lower():
                          reply_text_yt = f"❌ Ошибка при создании конспекта ({error_message_yt[:100]}...). Возможно, видео недоступно или на неподдерживаемом языке."; break
                     if is_retryable_yt and attempt_yt < RETRY_ATTEMPTS - 1:
                         wait_time_yt = RETRY_DELAY_SECONDS * (2 ** attempt_yt)
                         logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ожидание {wait_time_yt:.1f} сек..."); await asyncio.sleep(wait_time_yt); continue
                     else:
                         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось создать конспект после {attempt_yt + 1} попыток. Ошибка: {e_yt}", exc_info=True if not is_retryable_yt else False)
                         if reply_text_yt is None: reply_text_yt = f"❌ Ошибка при создании конспекта после {attempt_yt + 1} попыток."; break
            if reply_text_yt is None: reply_text_yt = "🤖 К сожалению, не удалось создать конспект видео после нескольких попыток."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) reply_text_yt остался None после всех попыток.")

            # user_message_with_id_for_history_and_prompt уже содержит текст с префиксом UserID
            history_entry_user_yt = { "role": "user", "parts": [{"text": user_message_with_id_for_history_and_prompt}], "youtube_video_id": youtube_id, "user_id": user_id, "message_id": original_message_id }
            chat_history.append(history_entry_user_yt)
            
            history_summary_prefix_yt = f"{YOUTUBE_SUMMARY_PREFIX}{reply_text_yt}" if reply_text_yt and "🤖" not in reply_text_yt and "❌" not in reply_text_yt else reply_text_yt
            chat_history.append({"role": "model", "parts": [{"text": history_summary_prefix_yt}]})

            summary_for_user_display_yt = history_summary_prefix_yt
            if get_user_setting(context, 'detailed_reasoning_enabled', True) and reply_text_yt and "🤖" not in reply_text_yt and "❌" not in reply_text_yt:
                cleaned_summary_yt = _strip_thoughts_from_text(reply_text_yt) 
                if reply_text_yt != cleaned_summary_yt: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Блок <мысли> удален из ответа перед отправкой.")
                summary_for_user_display_yt = f"{YOUTUBE_SUMMARY_PREFIX}{cleaned_summary_yt}"
            
            target_message_for_final_reply = target_message_for_reply if target_message_for_reply else original_update.message
            if target_message_for_final_reply:
                await send_reply(target_message_for_final_reply, summary_for_user_display_yt, context)
            else: # Крайний случай
                await context.bot.send_message(chat_id, text=summary_for_user_display_yt)

            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return # Завершаем, YouTube обработан
    
    # --- Стандартная обработка (текст или документ) ---
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    model_id_main = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature_main = get_user_setting(context, 'temperature', 1.0)
    use_search_main = get_user_setting(context, 'search_enabled', True) # Изменено имя переменной
    
    search_context_snippets_main = []
    search_provider_main = None
    search_log_msg_main = "Поиск отключен пользователем"

    # Поиск выполняется только для обычных текстовых сообщений, не для документов
    if use_search_main and not is_document_related:
        query_for_search_main = user_text_for_prompt # Используем переданный текст
        query_short_main = query_for_search_main[:50] + '...' if len(query_for_search_main) > 50 else query_for_search_main
        search_log_msg_main = f"Поиск Google/DDG для '{query_short_main}'"
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg_main}...")
        session = context.bot_data.get('aiohttp_session')
        if not session or session.closed:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Создание новой сессии aiohttp для поиска.")
            timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
            session = aiohttp.ClientSession(timeout=timeout)
            context.bot_data['aiohttp_session'] = session
        
        google_results_main = await perform_google_search(query_for_search_main, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results_main:
            search_provider_main = "Google"
            search_context_snippets_main = google_results_main
            search_log_msg_main += f" (Google: {len(search_context_snippets_main)} рез.)"
        else:
            search_log_msg_main += " (Google: 0 рез./ошибка)"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Google не дал результатов. Пробуем DuckDuckGo...")
            try:
                ddgs = DDGS()
                results_ddg_main = await asyncio.to_thread(ddgs.text, query_for_search_main, region='ru-ru', max_results=DDG_MAX_RESULTS)
                if results_ddg_main:
                    ddg_snippets_main = [r.get('body', '') for r in results_ddg_main if r.get('body')]
                    if ddg_snippets_main:
                        search_provider_main = "DuckDuckGo"; search_context_snippets_main = ddg_snippets_main
                        search_log_msg_main += f" (DDG: {len(search_context_snippets_main)} рез.)"
                    else: search_log_msg_main += " (DDG: 0 текст. рез.)"
                else: search_log_msg_main += " (DDG: 0 рез.)"
            except Exception as e_ddg_main: 
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка поиска DuckDuckGo: {e_ddg_main}", exc_info=True)
                search_log_msg_main += " (DDG: ошибка)"
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg_main}") # Лог о поиске

    # Формирование финального промпта для Gemini
    current_time_str_main = get_current_time_str()
    time_context_str_main = f"(Текущая дата и время: {current_time_str_main})\n"
    
    final_prompt_parts_main = [time_context_str_main]

    # Добавление URL, если это не документ и не YouTube
    if not is_document_related and not youtube_handled:
        general_url_main = extract_general_url(user_text_for_prompt)
        if general_url_main:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Общая ссылка {general_url_main} будет выделена в промпте.")
            url_instruction_main = (f"\n\n**Важное указание по ссылке в запросе пользователя:** Запрос пользователя содержит следующую ссылку: {general_url_main}. Пожалуйста, при формировании ответа в первую очередь используй содержимое этой веб-страницы. Если доступ к странице невозможен, информация нерелевантна или недостаточна, сообщи об этом и/или используй поиск и свои знания.")
            final_prompt_parts_main.append(url_instruction_main)
    
    # user_message_with_id_for_history_and_prompt уже содержит префикс UserID
    final_prompt_parts_main.append(user_message_with_id_for_history_and_prompt)

    if search_context_snippets_main: # Если был поиск и есть результаты
        search_context_lines_main = [f"- {s.strip()}" for s in search_context_snippets_main if s.strip()]
        if search_context_lines_main:
            search_context_text_main = "\n".join(search_context_lines_main)
            search_block_title_main = f"==== РЕЗУЛЬТАТЫ ПОИСКА ({search_provider_main}) ДЛЯ ОТВЕТА НА ВОПРОС ===="
            search_block_instruction_main = f"Используй эту информацию для ответа на вопрос пользователя {USER_ID_PREFIX_FORMAT.format(user_id=user_id)}, особенно если он касается текущих событий или погоды."
            # ... (добавление логики для general_url_main, если нужно изменить заголовок/инструкцию поиска) ...
            search_block_main = (f"\n\n{search_block_title_main}\n{search_context_text_main}\n===========================================================\n{search_block_instruction_main}")
            final_prompt_parts_main.append(search_block_main)
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен контекст из {search_provider_main} ({len(search_context_lines_main)} непустых сниппетов).")
            
    if get_user_setting(context, 'detailed_reasoning_enabled', True):
        final_prompt_parts_main.append(REASONING_PROMPT_ADDITION)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Добавлена инструкция для детального рассуждения.")
    
    final_gemini_prompt_text = "\n".join(final_prompt_parts_main)
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Финальный промпт для Gemini (длина {len(final_gemini_prompt_text)}):\n{final_gemini_prompt_text[:600]}...")

    # Добавление в историю (если не YouTube, который уже был добавлен)
    if not youtube_handled:
        history_entry_user_main = { "role": "user", "parts": [{"text": user_message_with_id_for_history_and_prompt}], "user_id": user_id, "message_id": original_message_id }
        # if image_file_id_for_history: # Это для фото, здесь не актуально, если только _process_text_with_gemini не будет вызываться из handle_photo
        #     history_entry_user_main["image_file_id"] = image_file_id_for_history
        chat_history.append(history_entry_user_main)

    # Формирование истории для модели
    history_for_model_raw_main = []
    current_total_chars_main = 0
    # Фильтруем историю, чтобы не включать текущий запрос пользователя, который будет добавлен отдельно
    history_to_filter_main = chat_history[:-1] if chat_history and chat_history[-1]["role"] == "user" and chat_history[-1]["message_id"] == original_message_id else chat_history

    for entry_main in reversed(history_to_filter_main):
        entry_text_main = ""
        entry_len_main = 0
        if entry_main.get("parts") and isinstance(entry_main["parts"], list) and len(entry_main["parts"]) > 0 and entry_main["parts"][0].get("text"):
            entry_text_main = entry_main["parts"][0]["text"]
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text_main and "Важное указание по ссылке" not in entry_text_main: # Не сохраняем старый поисковый контекст в историю для модели
                 entry_len_main = len(entry_text_main)
        if current_total_chars_main + entry_len_main + len(final_gemini_prompt_text) <= MAX_CONTEXT_CHARS:
            if "==== РЕЗУЛЬТАТЫ ПОИСКА" not in entry_text_main and "Важное указание по ссылке" not in entry_text_main:
                history_for_model_raw_main.append(entry_main)
                current_total_chars_main += entry_len_main
        else:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Учтено {len(history_for_model_raw_main)} сообщ., ~{current_total_chars_main} симв.")
            break
    history_for_model_main = list(reversed(history_for_model_raw_main))
    history_for_model_main.append({"role": "user", "parts": [{"text": final_gemini_prompt_text}]}) # Добавляем текущий промпт
    history_clean_for_model_main = [{"role": entry["role"], "parts": entry["parts"]} for entry in history_for_model_main]

    # Вызов Gemini
    reply_from_gemini_main = None
    for attempt_main in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Попытка {attempt_main + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id_main}...")
            generation_config_main = genai.GenerationConfig(temperature=temperature_main, max_output_tokens=MAX_OUTPUT_TOKENS)
            model_gemini_main = genai.GenerativeModel(model_id_main, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config_main, system_instruction=system_instruction_text)
            response_obj_main = await asyncio.to_thread(model_gemini_main.generate_content, history_clean_for_model_main)
            
            reply_from_gemini_main = _parse_gemini_response(response_obj_main, user_id, chat_id, attempt_main + 1, "MainProcessText")

            if reply_from_gemini_main and "🤖" not in reply_from_gemini_main and "❌" not in reply_from_gemini_main:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Успешная генерация на попытке {attempt_main + 1}.")
                break 
            elif reply_from_gemini_main: 
                logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Получено сообщение об ошибке/статусе от парсера: {reply_from_gemini_main}")
                break
        except (BlockedPromptException, StopCandidateException) as e_block_stop_main:
            reason_str_main = str(e_block_stop_main.args[0]) if hasattr(e_block_stop_main, 'args') and e_block_stop_main.args else "неизвестна"
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Запрос заблокирован/остановлен моделью (попытка {attempt_main + 1}): {e_block_stop_main} (Причина: {reason_str_main})")
            reply_from_gemini_main = f"❌ Запрос заблокирован/остановлен моделью."; break
        except Exception as e_main:
            error_message_main = str(e_main)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Ошибка генерации на попытке {attempt_main + 1}: {error_message_main[:200]}...")
            is_retryable_main = "500" in error_message_main or "503" in error_message_main
            if "429" in error_message_main: reply_from_gemini_main = f"❌ Слишком много запросов к модели. Попробуйте позже."; break
            elif "400" in error_message_main: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Ошибка 400 Bad Request: {error_message_main}", exc_info=True); reply_from_gemini_main = f"❌ Ошибка в запросе к модели (400 Bad Request)."; break
            elif "location is not supported" in error_message_main: reply_from_gemini_main = f"❌ Эта модель недоступна в вашем регионе."; break
            if is_retryable_main and attempt_main < RETRY_ATTEMPTS - 1:
                wait_time_main = RETRY_DELAY_SECONDS * (2 ** attempt_main)
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Ожидание {wait_time_main:.1f} сек..."); await asyncio.sleep(wait_time_main); continue
            else: 
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Не удалось выполнить генерацию после {attempt_main + 1} попыток. Ошибка: {e_main}", exc_info=True if not is_retryable_main else False)
                if reply_from_gemini_main is None: reply_from_gemini_main = f"❌ Ошибка при обращении к модели после {attempt_main + 1} попыток."; break
    
    if reply_from_gemini_main is None: 
        reply_from_gemini_main = "🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток."
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) reply_from_gemini_main остался None после всех попыток.")

    # Сохраняем ответ модели (или сообщение об ошибке от парсера) в историю (если не YouTube)
    if not youtube_handled:
        chat_history.append({"role": "model", "parts": [{"text": reply_from_gemini_main}]})
    
    reply_to_send_user = reply_from_gemini_main
    if get_user_setting(context, 'detailed_reasoning_enabled', True) and \
       reply_from_gemini_main and "🤖" not in reply_from_gemini_main and "❌" not in reply_from_gemini_main:
        cleaned_reply_main = _strip_thoughts_from_text(reply_from_gemini_main)
        if reply_from_gemini_main != cleaned_reply_main:
             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Блок <мысли> удален из ответа.")
        reply_to_send_user = cleaned_reply_main
    
    # Отправляем ответ пользователю
    target_message_for_final_reply_main = None
    try:
        target_message_for_final_reply_main = await context.bot.get_chat(chat_id).get_message(original_message_id)
    except Exception as e_get_msg_main:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Не удалось получить оригинальное сообщение ({original_message_id}) для ответа: {e_get_msg_main}. Используем update.message.")
        target_message_for_final_reply_main = original_update.message # Fallback

    if target_message_for_final_reply_main:
        await send_reply(target_message_for_final_reply_main, reply_to_send_user, context)
    else: # Крайний случай, если и update.message почему-то None
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Не удалось определить target_message для ответа. Отправка в чат напрямую.")
        try: await context.bot.send_message(chat_id=chat_id, text=reply_to_send_user)
        except Exception as e_direct_send_main: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (MainProcess) Не удалось отправить ответ напрямую: {e_direct_send_main}")
    
    while len(chat_history) > MAX_HISTORY_MESSAGES:
        chat_history.pop(0)

# --- ОБНОВЛЕННЫЙ handle_message ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id # Для логов, если что-то пойдет не так до user_id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | handle_message: Пропуск, нет effective_user.")
        return
    user_id = update.effective_user.id
    if not update.message or (not update.message.text and not update.message.photo and not update.message.document): # Расширенная проверка
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | handle_message: Пропуск, нет сообщения или релевантного контента (текст/фото/документ).")
        return

    original_message_id = update.message.message_id
    user_text_content = "" # Текст от пользователя или сформированный из файла

    if update.message.text:
        user_text_content = update.message.text.strip()
    
    if not user_text_content and not update.message.photo and not update.message.document: # Еще одна проверка
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | handle_message: Пропуск, текст пуст и нет медиа для обработки этим хендлером.")
        return

    # Обработка ответа на спец. сообщения (reanalyze_image/video)
    if update.message.reply_to_message and update.message.reply_to_message.text and \
       user_text_content and not user_text_content.startswith('/'):
        replied_text = update.message.reply_to_message.text
        user_question_for_reanalyze = user_text_content
        
        chat_history_reanalyze = context.chat_data.get("history", []) # Используем .get для безопасности
        found_special_context_for_reanalyze = False
        try:
            for i in range(len(chat_history_reanalyze) - 1, -1, -1):
                model_entry = chat_history_reanalyze[i]
                if model_entry.get("role") == "model" and model_entry.get("parts") and \
                   isinstance(model_entry["parts"], list) and len(model_entry["parts"]) > 0:
                    model_text = model_entry["parts"][0].get("text", "")
                    is_image_reply = model_text.startswith(IMAGE_DESCRIPTION_PREFIX) and \
                                     replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) and \
                                     model_text[:150] == replied_text[:150]
                    is_video_reply = model_text.startswith(YOUTUBE_SUMMARY_PREFIX) and \
                                     replied_text.startswith(YOUTUBE_SUMMARY_PREFIX) and \
                                     model_text[:150] == replied_text[:150]
                    if is_image_reply or is_video_reply:
                        if i > 0:
                            user_entry = chat_history_reanalyze[i-1]
                            if user_entry.get("role") == "user":
                                original_uploader_id = user_entry.get("user_id")
                                if is_image_reply and "image_file_id" in user_entry:
                                    found_file_id = user_entry["image_file_id"]
                                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ на описание фото. Запуск reanalyze_image для file_id: ...{found_file_id[-10:]}")
                                    await reanalyze_image(update, context, found_file_id, user_question_for_reanalyze, original_uploader_id)
                                    found_special_context_for_reanalyze = True; break
                                elif is_video_reply and "youtube_video_id" in user_entry:
                                    found_video_id = user_entry["youtube_video_id"]
                                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ на конспект видео. Запуск reanalyze_video для video_id: {found_video_id}")
                                    await reanalyze_video(update, context, found_video_id, user_question_for_reanalyze, original_uploader_id)
                                    found_special_context_for_reanalyze = True; break
        except Exception as e_hist_search_reanalyze: 
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при поиске ID для reanalyze: {e_hist_search_reanalyze}", exc_info=True)
        
        if found_special_context_for_reanalyze: return
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX):
             logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ответ на спец. сообщение, но контекст для reanalyze не найден. Обработка как обычный текст.")

    # Если это обычное текстовое сообщение (не документ, не фото, не reanalyze)
    if user_text_content: # Убедимся, что текст есть
        await _process_text_with_gemini(
            user_text_for_prompt=user_text_content, 
            original_update=update, 
            context=context,
            original_message_id=original_message_id,
            is_document_related=False # Это не документ
            # image_file_id_for_history не передается, так как это не фото
        )
    # Обработка фото и документов теперь происходит в их собственных хендлерах,
    # handle_photo и handle_document, которые затем могут вызывать _process_text_with_gemini при необходимости.
    # Данный handle_message теперь только для чисто текстовых сообщений и reanalyze.

# --- ОБНОВЛЕННЫЙ handle_photo ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.photo: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_photo не найдено фото."); return

    photo_file_id = message.photo[-1].file_id
    user_message_id = message.message_id
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен photo file_id: ...{photo_file_id[-10:]}, message_id: {user_message_id}")
    
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file(); file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Скачанное фото (file_id: ...{photo_file_id[-10:]}) пустое."); await message.reply_text("❌ Не удалось загрузить изображение (файл пуст)."); return
    except Exception as e: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото (file_id: ...{photo_file_id[-10:]}): {e}", exc_info=True); await message.reply_text("❌ Не удалось загрузить изображение."); return
    
    user_caption = message.caption if message.caption else ""
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обработка фото как изображения (Vision).")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    if len(file_bytes) > 20 * 1024 * 1024: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Изображение ({len(file_bytes) / (1024*1024):.2f} MB) может быть большим для API.")
    try: b64_data = base64.b64encode(file_bytes).decode()
    except Exception as e: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Base64: {e}", exc_info=True); await message.reply_text("❌ Ошибка обработки изображения."); return

    current_time_str = get_current_time_str()
    prompt_text_vision = (f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо).") if user_caption else (f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении.")
    if get_user_setting(context, 'detailed_reasoning_enabled', True): prompt_text_vision += REASONING_PROMPT_ADDITION

    mime_type = "image/jpeg"; 
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts_vision = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}] # Изменено имя
    content_for_vision = [{"role": "user", "parts": parts_vision}]

    model_id_vision = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature_vision = get_user_setting(context, 'temperature', 1.0)
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra']; is_vision_model = any(keyword in model_id_vision for keyword in vision_capable_keywords)
    if not is_vision_model:
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models:
            original_model_name_vision = AVAILABLE_MODELS.get(model_id_vision, model_id_vision); fallback_model_id_vision = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id_vision = fallback_model_id_vision
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Модель {original_model_name_vision} не vision. Временно использую {AVAILABLE_MODELS.get(model_id_vision, model_id_vision)}.")
        else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Нет доступных vision моделей."); await message.reply_text("❌ Нет доступных моделей для анализа изображений."); return
    
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Анализ изображения (Vision). Модель: {model_id_vision}, Темп: {temperature_vision}, MIME: {mime_type}")
    reply_text_vision = None
    for attempt_vision in range(RETRY_ATTEMPTS): # Используем _vision
        try:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Попытка {attempt_vision + 1}/{RETRY_ATTEMPTS}...")
            generation_config_vision = genai.GenerationConfig(temperature=temperature_vision, max_output_tokens=MAX_OUTPUT_TOKENS)
            model_gemini_vision = genai.GenerativeModel(model_id_vision, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config_vision, system_instruction=system_instruction_text)
            response_obj_vision = await asyncio.to_thread(model_gemini_vision.generate_content, content_for_vision)
            reply_text_vision = _parse_gemini_response(response_obj_vision, user_id, chat_id, attempt_vision + 1, "Vision")
            if reply_text_vision and "🤖" not in reply_text_vision and "❌" not in reply_text_vision: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Успешный анализ на попытке {attempt_vision + 1}."); break
            elif reply_text_vision: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Получено сообщение об ошибке/статусе от парсера: {reply_text_vision}"); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop_vision: # Используем _vision
             reason_str_vision = str(e_block_stop_vision.args[0]) if hasattr(e_block_stop_vision, 'args') and e_block_stop_vision.args else "неизвестна"
             logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Анализ заблокирован/остановлен (попытка {attempt_vision + 1}): {e_block_stop_vision} (Причина: {reason_str_vision})")
             reply_text_vision = f"❌ Анализ изображения заблокирован/остановлен моделью."; break
        except Exception as e_vision: # Используем _vision
             error_message_vision = str(e_vision)
             logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка на попытке {attempt_vision + 1}: {error_message_vision[:200]}...")
             is_retryable_vision = "500" in error_message_vision or "503" in error_message_vision
             if "400" in error_message_vision or "429" in error_message_vision or "location is not supported" in error_message_vision:
                 reply_text_vision = f"❌ Ошибка при анализе изображения ({error_message_vision[:100]}...)."; break
             if is_retryable_vision and attempt_vision < RETRY_ATTEMPTS - 1:
                 wait_time_vision = RETRY_DELAY_SECONDS * (2 ** attempt_vision)
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ожидание {wait_time_vision:.1f} сек..."); await asyncio.sleep(wait_time_vision); continue
             else:
                 logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось выполнить анализ после {attempt_vision + 1} попыток. Ошибка: {e_vision}", exc_info=True if not is_retryable_vision else False)
                 if reply_text_vision is None: reply_text_vision = f"❌ Ошибка при анализе изображения после {attempt_vision + 1} попыток."; break
    if reply_text_vision is None: reply_text_vision = "🤖 К сожалению, не удалось проанализировать изображение после нескольких попыток."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) reply_text_vision остался None после всех попыток.")

    chat_history = context.chat_data.setdefault("history", [])
    user_text_for_history_vision = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото.")
    history_entry_user_photo = { "role": "user", "parts": [{"text": user_text_for_history_vision}], "image_file_id": photo_file_id, "user_id": user_id, "message_id": user_message_id }
    chat_history.append(history_entry_user_photo)

    history_reply_prefix_photo = f"{IMAGE_DESCRIPTION_PREFIX}{reply_text_vision}" if reply_text_vision and "🤖" not in reply_text_vision and "❌" not in reply_text_vision else reply_text_vision
    chat_history.append({"role": "model", "parts": [{"text": history_reply_prefix_photo}]})

    reply_for_user_photo = history_reply_prefix_photo
    if get_user_setting(context, 'detailed_reasoning_enabled', True) and reply_text_vision and "🤖" not in reply_text_vision and "❌" not in reply_text_vision:
        cleaned_reply_photo = _strip_thoughts_from_text(reply_text_vision)
        if reply_text_vision != cleaned_reply_photo: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Блок <мысли> удален из ответа перед отправкой.")
        reply_for_user_photo = f"{IMAGE_DESCRIPTION_PREFIX}{cleaned_reply_photo}"
    await send_reply(message, reply_for_user_photo, context)
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

# --- ОБНОВЛЕННЫЙ handle_document ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message # Это оригинальное сообщение с документом
    if not message or not message.document: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_document нет документа."); return
    
    doc = message.document
    allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql')
    allowed_mime_types = ('application/octet-stream',) 
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types
    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})"); return
    
    MAX_FILE_SIZE_MB = 15 # MB
    file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0 and doc.file_name: 
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Пустой файл '{doc.file_name}'.")
        await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой."); return
    elif file_size_bytes == 0 and not doc.file_name: 
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой документ без имени."); return
    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ Файл `{doc.file_name}` слишком большой (> {MAX_FILE_SIZE_MB} MB).", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Слишком большой файл: {doc.file_name} ({file_size_bytes / (1024*1024):.2f} MB)"); return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
        if not file_bytes: 
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' скачан, но пуст.")
            await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой."); return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать документ '{doc.file_name}': {e}", exc_info=True)
        await update.message.reply_text("❌ Не удалось загрузить файл."); return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text_from_doc = None; detected_encoding = None
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
                           if 'utf-8-sig' not in encodings_to_try: encodings_to_try.insert(0, 'utf-8-sig')
                           if 'utf-8' in encodings_to_try: 
                               try: encodings_to_try.remove('utf-8') 
                               except ValueError: pass 
                      else:
                           detected_encoding = potential_encoding
                           if detected_encoding in encodings_to_try: encodings_to_try.remove(detected_encoding)
                           encodings_to_try.insert(0, detected_encoding)
                 else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet не уверен ({detected.get('confidence', 0):.2f}) для '{doc.file_name}'.")
        except Exception as e_chardet: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при использовании chardet для '{doc.file_name}': {e_chardet}")
    
    unique_encodings = list(dict.fromkeys(encodings_to_try))
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Попытки декодирования для '{doc.file_name}': {unique_encodings}")
    for encoding in unique_encodings:
        try: 
            text_from_doc = file_bytes.decode(encoding); detected_encoding = encoding
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' успешно декодирован как {encoding}.")
            break
        except (UnicodeDecodeError, LookupError): logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' не в кодировке {encoding}.")
        except Exception as e_decode: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка при декодировании '{doc.file_name}' как {encoding}: {e_decode}", exc_info=True)

    if text_from_doc is None:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось декодировать '{doc.file_name}' ни одной из кодировок: {unique_encodings}")
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте UTF-8.", parse_mode=ParseMode.MARKDOWN); return
    if not text_from_doc.strip() and len(file_bytes) > 0:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' дал пустой текст после декодирования ({detected_encoding}).")
        await update.message.reply_text(f"⚠️ Не удалось извлечь текст из файла `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN); return

    approx_max_tokens_for_file = MAX_OUTPUT_TOKENS * 2 
    MAX_FILE_CHARS = min(MAX_CONTEXT_CHARS // 2, approx_max_tokens_for_file * 4) 
    truncated_text = text_from_doc; truncation_warning = ""
    if len(text_from_doc) > MAX_FILE_CHARS:
        truncated_text = text_from_doc[:MAX_FILE_CHARS]
        last_newline = truncated_text.rfind('\n')
        if last_newline > MAX_FILE_CHARS * 0.8: truncated_text = truncated_text[:last_newline]
        chars_k = len(truncated_text) // 1000
        truncation_warning = f"\n\n**(⚠️ Текст файла был обрезан до ~{chars_k}k символов)**"
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {len(truncated_text)} символов.")

    user_caption_doc = message.caption if message.caption else "" # Изменено имя
    file_name_for_prompt_doc = doc.file_name or "файл"
    encoding_info_for_prompt_doc = f"(~{detected_encoding})" if detected_encoding else "(кодировка?)"
    file_context_for_prompt_doc = f"Содержимое файла `{file_name_for_prompt_doc}` {encoding_info_for_prompt_doc}:\n```\n{truncated_text}\n```{truncation_warning}"

    user_prompt_for_gemini_doc = ""
    if user_caption_doc:
        escaped_caption_doc = user_caption_doc.replace('"', '\\"') 
        user_prompt_for_gemini_doc = f"Пользователь загрузил файл `{file_name_for_prompt_doc}` с комментарием: \"{escaped_caption_doc}\". {file_context_for_prompt_doc}\nПроанализируй, пожалуйста."
    else:
        user_prompt_for_gemini_doc = f"Пользователь загрузил файл `{file_name_for_prompt_doc}`. {file_context_for_prompt_doc}\nЧто можешь сказать об этом тексте?"
    
    if get_user_setting(context, 'detailed_reasoning_enabled', True): 
        user_prompt_for_gemini_doc += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Добавлена инструкция для детального рассуждения.")
    
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в _process_text_with_gemini с текстом документа.")
    
    await _process_text_with_gemini(
        user_text_for_prompt=user_prompt_for_gemini_doc,
        original_update=update, 
        context=context,
        original_message_id=message.message_id,
        is_document_related=True
    )
# ====================================================================

# --- Функции веб-сервера и запуска ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_connect=10.0, sock_read=30.0)
    aiohttp_session = aiohttp.ClientSession(timeout=timeout)
    application.bot_data['aiohttp_session'] = aiohttp_session
    logger.info("Сессия aiohttp создана и сохранена в bot_data.")
    
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
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document)) # Теперь вызывает _process_text_with_gemini
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)) # Теперь вызывает _process_text_with_gemini
    
    try:
        await application.initialize()
        commands = [
            BotCommand("start", "Начать работу и инфо"), BotCommand("model", "Выбрать модель Gemini"),
            BotCommand("temp", "Установить температуру (креативность)"),
            BotCommand("search_on", "Включить поиск Google/DDG"), BotCommand("search_off", "Выключить поиск Google/DDG"),
            BotCommand("reasoning_on", "Вкл. углубленные рассуждения"), BotCommand("reasoning_off", "Выкл. углубленные рассуждения"),
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
        if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed:
            await application.bot_data['aiohttp_session'].close()
            logger.info("Сессия aiohttp закрыта из-за ошибки инициализации.")
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
        await site.start(); logger.info(f"Веб-сервер запущен на http://{host}:{port}"); await stop_event.wait()
    except asyncio.CancelledError: logger.info("Задача веб-сервера отменена.")
    except Exception as e: logger.error(f"Ошибка при запуске или работе веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally: logger.info("Начало остановки веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app')
    if not application: logger.critical("Приложение бота не найдено в контексте веб-сервера!"); return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token:
         header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
         if header_token != secret_token: logger.warning(f"Неверный секретный токен в заголовке от {request.remote}. Ожидался: ...{secret_token[-4:]}, Получен: {header_token}"); return aiohttp.web.Response(status=403, text="Forbidden: Invalid secret token.")
    try:
        data = await request.json(); update = Update.de_json(data, application.bot)
        logger.debug(f"Получен Update ID: {update.update_id} от Telegram.")
        await application.process_update(update)
        return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e_json: body = await request.text(); logger.error(f"Ошибка декодирования JSON от Telegram: {e_json}. Тело запроса: {body[:500]}..."); return aiohttp.web.Response(text="Bad Request: JSON decode error", status=400)
    except TelegramError as e_tg: logger.error(f"Ошибка Telegram при обработке вебхука: {e_tg}", exc_info=True); return aiohttp.web.Response(text=f"Internal Server Error: Telegram API Error ({type(e_tg).__name__})", status=500)
    except Exception as e: logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True); return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper(); log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO) 
    logging.getLogger('httpx').setLevel(logging.WARNING); logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('google.api_core').setLevel(logging.WARNING); logging.getLogger('google.auth').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.INFO); logging.getLogger('telegram.bot').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    logging.getLogger('google.generativeai').setLevel(logging.INFO); logging.getLogger('duckduckgo_search').setLevel(logging.INFO)
    logger.setLevel(log_level); logger.info(f"--- Установлен уровень логгирования для '{logger.name}': {log_level_str} ({log_level}) ---")

    loop = asyncio.get_running_loop(); stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): logger.info("Получен сигнал SIGINT/SIGTERM, инициирую остановку..."); stop_event.set()
        else: logger.warning("Повторный сигнал остановки получен, процесс уже завершается.")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
             logger.warning(f"Не удалось установить обработчик сигнала {sig} через loop. Использую signal.signal().")
             try: signal.signal(sig, lambda s, f: signal_handler())
             except Exception as e_signal: logger.error(f"Не удалось установить обработчик сигнала {sig} через signal.signal(): {e_signal}")
    application = None; web_server_task = None; aiohttp_session_main = None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---")
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask")
        aiohttp_session_main = application.bot_data.get('aiohttp_session')
        logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)...")
        await stop_event.wait()
    except asyncio.CancelledError: logger.info("Главная задача main() была отменена.")
    except Exception as e: logger.critical("Критическая ошибка во время запуска или ожидания.", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки приложения ---")
        if not stop_event.is_set(): stop_event.set()
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера (через stop_event)...")
             try: await asyncio.wait_for(web_server_task, timeout=15.0); logger.info("Веб-сервер успешно завершен.")
             except asyncio.TimeoutError:
                 logger.warning("Веб-сервер не завершился за 15 секунд, принудительная отмена...")
                 web_server_task.cancel()
                 try: await web_server_task
                 except asyncio.CancelledError: logger.info("Задача веб-сервера успешно отменена.")
                 except Exception as e_cancel_ws: logger.error(f"Ошибка при ожидании отмененной задачи веб-сервера: {e_cancel_ws}", exc_info=True)
             except asyncio.CancelledError: logger.info("Ожидание веб-сервера было отменено.")
             except Exception as e_wait_ws: logger.error(f"Ошибка при ожидании завершения веб-сервера: {e_wait_ws}", exc_info=True)
        if application:
            logger.info("Остановка приложения Telegram бота (application.shutdown)...")
            try: 
                if hasattr(application, 'shutdown') and asyncio.iscoroutinefunction(application.shutdown): await application.shutdown()
                elif hasattr(application, 'shutdown'): application.shutdown()
                logger.info("Приложение Telegram бота успешно остановлено.")
            except Exception as e_shutdown: logger.error(f"Ошибка во время application.shutdown(): {e_shutdown}", exc_info=True)
        if aiohttp_session_main and not aiohttp_session_main.closed:
             logger.info("Закрытие основной сессии aiohttp..."); await aiohttp_session_main.close(); await asyncio.sleep(0.25); logger.info("Основная сессия aiohttp закрыта.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся фоновых задач...")
            for task_to_cancel in tasks: task_to_cancel.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True); cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 task_name = tasks[i].get_name()
                 if isinstance(res, asyncio.CancelledError): cancelled_count += 1; logger.debug(f"Задача '{task_name}' успешно отменена.")
                 elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка или исключение в завершенной задаче '{task_name}': {type(res).__name__} - {res}", exc_info=(not isinstance(res, asyncio.CancelledError)))
            logger.info(f"Фоновые задачи обработаны (отменено: {cancelled_count}, ошибок/исключений: {error_count}).")
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Приложение прервано пользователем (KeyboardInterrupt в main).")
    except Exception as e_top: logger.critical("Неперехваченная ошибка на верхнем уровне asyncio.run(main).", exc_info=True)

# --- END OF FILE main.py ---
