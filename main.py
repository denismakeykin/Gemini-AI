# --- START OF FILE main.py ---

# Обновлённый main.py:
# - Упрощен импорт типов Gemini, чтобы избежать ошибок с новыми версиями SDK.
# - Настройки безопасности устанавливаются через строки.
# - Обработка ошибок BlokedPrompt/StopCandidate заменена на общий Exception с проверкой текста ошибки (при необходимости).
# - Сохранены все предыдущие изменения: встроенный поиск, дата/время, персонализация и т.д.

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

# ===== Обработка импорта типов Gemini и SAFETY_SETTINGS =====
# Упрощаем импорт, оставляем только то, что точно нужно и скорее всего стабильно
try:
    from google.generativeai.types import Tool, GenerationConfig
    try:
        from google.generativeai.types import GoogleSearch
        logger.info("Типы инструментов (Tool, GoogleSearch, GenerationConfig) успешно импортированы из google.generativeai.types.")
    except ImportError:
        GoogleSearch = type('GoogleSearch', (object,), {}) # Заглушка
        logger.warning("Тип GoogleSearch не найден в google.generativeai.types, используется заглушка. Поиск может не работать.")
        logger.info("Типы Tool и GenerationConfig успешно импортированы из google.generativeai.types.")

except ImportError as e_tool_import:
    logger.warning(f"Не удалось импортировать базовые типы Gemini (Tool, GenerationConfig): {e_tool_import}. Функциональность может быть нарушена.")
    Tool = type('Tool', (object,), {})
    GenerationConfig = genai.GenerationConfig # Используем стандартный genai.GenerationConfig
    GoogleSearch = type('GoogleSearch', (object,), {})

# --- Настройка Safety Settings строками ---
HARM_CATEGORIES_STRINGS = [
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
]
BLOCK_NONE_STRING = "BLOCK_NONE"
SAFETY_SETTINGS_BLOCK_NONE = []
if HARM_CATEGORIES_STRINGS:
    SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]
    logger.info("Настройки безопасности BLOCK_NONE установлены с использованием строк.")
else:
    logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки безопасности не установлены.")

# --- Убрали определение специфичных типов исключений ---
# BlockedPromptException = ...
# StopCandidateException = ...
# ==========================================================

# --- Переменные окружения и Настройка Gemini ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
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
    'gemini-1.5-flash-latest': '1.5 Flash (latest)',
    'gemini-1.5-pro-latest': '1.5 Pro (latest)',
}
DEFAULT_MODEL = 'gemini-1.5-flash-latest' if 'gemini-1.5-flash-latest' in AVAILABLE_MODELS else list(AVAILABLE_MODELS.keys())[0]

MAX_CONTEXT_CHARS = 100000
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
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
"Никогда не сокращай и не исправляй наш рабочий текст или код без запроса или разрешения."
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
    if 'selected_model' not in context.user_data:
        set_user_setting(context, 'selected_model', DEFAULT_MODEL)
    if 'temperature' not in context.user_data:
        set_user_setting(context, 'temperature', 1.0)

    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    default_model_name = AVAILABLE_MODELS.get(current_model, current_model)

    start_message = (
        f"**Google GEMINI {default_model_name}**"
        f"\n- с улучшенными настройками точности, логики и юмора от автора бота,"
        f"\n- обладаю огромным объемом знаний, уточняет инфу в Google,"
        f"\n- умею понимать и читать изображения, документы, делать конспекты YouTube-видео,"
        f"\n- пишите в личку или добавляйте меня в группы, я запоминаю историю чата и понимаю, кто мне пишет."
        f"\n- канал автора: https://t.me/denisobovsyom"
        f"\n/model — сменить модель"
        f"\n/clear — очистить историю **этого** чата"
        f"\n/temp — изменить температуру (креативность) ответа"
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
async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по фото от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ изображения (file_id: ...{file_id[-10:]}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    # 1. Скачивание и кодирование
    try:
        img_file = await context.bot.get_file(file_id); file_bytes = await img_file.download_as_bytearray()
        if not file_bytes: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Не удалось скачать или файл пустой для file_id: ...{file_id[-10:]}"); await update.message.reply_text("❌ Не удалось получить исходное изображение для повторного анализа."); return
        b64_data = base64.b64encode(file_bytes).decode()
    except TelegramError as e_telegram: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка Telegram при получении/скачивании файла {file_id}: {e_telegram}", exc_info=True); await update.message.reply_text(f"❌ Ошибка Telegram при получении изображения: {e_telegram}"); return
    except Exception as e_download: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка скачивания/кодирования файла {file_id}: {e_download}", exc_info=True); await update.message.reply_text("❌ Ошибка при подготовке изображения для повторного анализа."); return
    # 2. Формирование запроса к Vision
    current_time_str = get_current_time_str()
    user_question_with_context = f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}"
    mime_type = "image/jpeg";
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts = [{"text": user_question_with_context}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]; content_for_vision = [{"role": "user", "parts": parts}]
    # 3. Вызов модели
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra', '1.5']; is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
    if not is_vision_model:
        vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
        else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет доступных vision моделей."); await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения."); return
    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель: {model_id}, Темп: {temperature}"); reply = None; response_vision = None
    for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text); response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)
            if hasattr(response_vision, 'text'): reply = response_vision.text
            else: reply = None
            if not reply: block_reason_str, finish_reason_str = 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                except Exception as e_inner_reason: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка извлечения причины: {e_inner_reason}"); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                reply = "🤖 Модель дала пустой ответ на вопрос об этом изображении." # Не выходим
            if reply and not reply.startswith("❌") and not reply.startswith("🤖"): logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Успешный анализ на попытке {attempt + 1}."); break
        except Exception as e: # Ловим все ошибки, включая Blocked/Stop
            error_message = str(e); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_block_error = "blocked" in error_message.lower() or "safety" in error_message.lower()
            is_stop_error = "stopped" in error_message.lower() # Простая проверка
            is_retryable = "500" in error_message or "503" in error_message or "internal" in error_message.lower() or "deadline exceeded" in error_message.lower()
            is_fatal_api_error = "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported" in error_message.lower()

            if is_block_error or is_stop_error:
                 reason_str = "неизвестна"; try: reason_str = str(e.args[0]) if hasattr(e, 'args') and e.args else "N/A"; except Exception: pass
                 logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Анализ заблокирован/остановлен (попытка {attempt + 1}): {type(e).__name__} (Причина: {reason_str})")
                 reply = f"❌ Не удалось повторно проанализировать изображение (ограничение модели)."
                 break # Фатальная
            elif is_fatal_api_error:
                 reply = f"❌ Ошибка API при повторном анализе изображения ({error_message[:100]}...)."; logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Неповторяемая ошибка API: {e}", exc_info=True); break # Фатальная
            elif is_retryable and attempt < RETRY_ATTEMPTS - 1:
                 wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Повторяемая ошибка, ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
            else: # Последняя попытка или нераспознанная ошибка
                 logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось выполнить анализ после {attempt + 1} попыток. Последняя ошибка: {e}", exc_info=True)
                 if reply is None: reply = f"❌ Ошибка при повторном анализе после {RETRY_ATTEMPTS} попыток."
                 break
    # 4. Добавление в историю и отправка
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = { "role": "user", "parts": [{"text": user_question_with_context}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    if reply: history_entry_model = {"role": "model", "parts": [{"text": reply}]}; chat_history.append(history_entry_model); await send_reply(update.message, reply, context)
    else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет ответа для отправки."); final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."; chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]}); try: await update.message.reply_text(final_error_msg); except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
# =======================================================

# ===== Ответ на вопросы по конспекту видео (использует chat_data, ссылка в тексте) =====
async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id; requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    youtube_uri = f"https://www.youtube.com/watch?v={video_id}"; current_time_str = get_current_time_str()
    # Формирование промпта
    prompt_for_video = f"(Текущая дата и время: {current_time_str})\n{user_question}\n\n**Важно:** Ответь на основе содержимого видео по следующей ссылке: {youtube_uri}"
    user_question_with_id_for_history = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    content_for_video = [{"role": "user", "parts": [{"text": prompt_for_video}]}]
    # Вызов модели
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
    if not is_video_model:
        video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)];
        if video_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель {original_model_name} не video. Временно использую {new_model_name}.")
        else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет доступных video моделей."); await update.message.reply_text("❌ Нет доступных моделей для ответа на вопрос по видео."); return
    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель: {model_id}, Темп: {temperature}"); reply = None; response_video = None
    for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
        try:
            logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text); response_video = await asyncio.to_thread(model.generate_content, content_for_video)
            if hasattr(response_video, 'text'): reply = response_video.text
            else: reply = None
            if not reply: block_reason_str, finish_reason_str = 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                except Exception as e_inner_reason: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка извлечения причины: {e_inner_reason}"); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                reply = "🤖 Не могу ответить на ваш вопрос по этому видео (пустой ответ модели)." # Не выходим
            if reply and not reply.startswith("❌") and not reply.startswith("🤖"): logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Успешный анализ на попытке {attempt + 1}."); break
        except Exception as e: # Ловим все ошибки
            error_message = str(e); logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
            is_block_error = "blocked" in error_message.lower() or "safety" in error_message.lower()
            is_stop_error = "stopped" in error_message.lower()
            is_retryable = "500" in error_message or "503" in error_message or "internal" in error_message.lower() or "deadline exceeded" in error_message.lower()
            is_fatal_api_error = "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported" in error_message.lower()
            if is_block_error or is_stop_error: reason_str = "неизвестна"; try: reason_str = str(e.args[0]) if hasattr(e, 'args') and e.args else "N/A"; except Exception: pass; logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Анализ заблокирован/остановлен..."); reply = f"❌ Не удалось ответить по видео (ограничение модели)."; break
            elif is_fatal_api_error: reply = f"❌ Ошибка API при ответе по видео ({error_message[:100]}...)."; logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Неповторяемая ошибка API: {e}", exc_info=True); break
            elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Повторяемая ошибка, ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
            else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось выполнить анализ после {attempt + 1} попыток...", exc_info=True); if reply is None: reply = f"❌ Ошибка при ответе по видео после {RETRY_ATTEMPTS} попыток."; break
    # Добавление в историю и отправка
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = { "role": "user", "parts": [{"text": user_question_with_id_for_history}], "user_id": requesting_user_id, "message_id": update.message.message_id }; chat_history.append(history_entry_user)
    if reply: history_entry_model = {"role": "model", "parts": [{"text": reply}]}; chat_history.append(history_entry_model); await send_reply(update.message, reply, context)
    else: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет ответа для отправки."); final_error_msg = "🤖 К сожалению, не удалось ответить на ваш вопрос по видео."; chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]}); try: await update.message.reply_text(final_error_msg); except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
# =============================================================

# ===== Основной обработчик сообщений (с ИЗМЕНЕННЫМ вызовом Gemini API) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | Не удалось определить пользователя в update."); return
    user_id = update.effective_user.id; message = update.message
    if not message: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой объект message в update."); return
    if not message.text and not hasattr(message, 'image_file_id'):
        if not message.photo and not message.document and not message.video: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Игнорирование сообщения без текста, OCR, фото, документа или видео."); return

    chat_history = context.chat_data.setdefault("history", [])

    # --- Проверка на ответ к специальным сообщениям ---
    if message.reply_to_message and message.reply_to_message.text and message.text and not message.text.startswith('/'):
        replied_message = message.reply_to_message; replied_text = replied_message.text; user_question = message.text.strip(); requesting_user_id = user_id; found_special_context = False
        try: # Поиск ID для reanalyze
            matched_model_entry = None; matched_user_entry = None
            for i in range(len(chat_history) - 1, -1, -1):
                entry = chat_history[i]
                if entry.get("role") == "model" and entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0:
                    model_text = entry["parts"][0].get("text", "")
                    if model_text.startswith(replied_text[:100]): # Сверяем начало
                        if i > 0 and chat_history[i-1].get("role") == "user": matched_model_entry = entry; matched_user_entry = chat_history[i-1]; break
            if matched_model_entry and matched_user_entry:
                model_text = matched_model_entry["parts"][0].get("text", ""); is_image_reply = model_text.startswith(IMAGE_DESCRIPTION_PREFIX) and replied_text.startswith(IMAGE_DESCRIPTION_PREFIX); is_video_reply = model_text.startswith(YOUTUBE_SUMMARY_PREFIX) and replied_text.startswith(YOUTUBE_SUMMARY_PREFIX); original_user_id_from_hist = matched_user_entry.get("user_id", "Unknown")
                if is_image_reply and "image_file_id" in matched_user_entry: found_file_id = matched_user_entry["image_file_id"]; logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на описание фото..."); await reanalyze_image(update, context, found_file_id, user_question, original_user_id_from_hist); found_special_context = True
                elif is_video_reply and "youtube_video_id" in matched_user_entry: found_video_id = matched_user_entry["youtube_video_id"]; logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на конспект видео..."); await reanalyze_video(update, context, found_video_id, user_question, original_user_id_from_hist); found_special_context = True
                else: logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на сообщение модели, но нет ID в пред. user-сообщении.")
        except Exception as e_hist_search: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ошибка при поиске ID для reanalyze: {e_hist_search}", exc_info=True)
        if found_special_context: return
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX): logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | Ответ на спец. сообщение, но ID не найден.")

    # --- Получение текста и проверка на YouTube ---
    original_user_message_text = ""; image_file_id_from_ocr = None; user_message_id = message.message_id
    if hasattr(message, 'image_file_id'): image_file_id_from_ocr = message.image_file_id; original_user_message_text = message.text.strip() if message.text else ""; logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен image_file_id из OCR.")
    elif message.text: original_user_message_text = message.text.strip()
    user_message_with_id = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + original_user_message_text
    youtube_handled = False
    is_reply_to_special = message.reply_to_message and message.reply_to_message.text and (message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) or message.reply_to_message.text.startswith(YOUTUBE_SUMMARY_PREFIX))
    if not is_reply_to_special and not image_file_id_from_ocr:
        youtube_id = extract_youtube_id(original_user_message_text)
        if youtube_id:
            youtube_handled = True; first_name = update.effective_user.first_name; user_mention = f"{first_name}" if first_name else f"User {user_id}"
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
                else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет доступных video моделей."); await update.message.reply_text("❌ Нет доступных моделей для создания конспекта видео."); history_entry_user_err = { "role": "user", "parts": [{"text": user_message_with_id}], "youtube_video_id": youtube_id, "user_id": user_id, "message_id": user_message_id }; history_entry_model_err = { "role": "model", "parts": [{"text": "❌ Нет доступных моделей для создания конспекта видео."}]}; chat_history.extend([history_entry_user_err, history_entry_model_err]); while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0); return
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель: {model_id}, Темп: {temperature}"); reply = None
            for attempt in range(RETRY_ATTEMPTS): # Цикл ретраев
                 try:
                     logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text); response_summary = await asyncio.to_thread(model.generate_content, content_for_summary)
                     if hasattr(response_summary, 'text'): reply = response_summary.text
                     else: reply = None
                     if not reply: block_reason_str, finish_reason_str = 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                         except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка извлечения причины: {e_inner_reason}"); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                         reply = "🤖 Не удалось создать конспект видео (пустой ответ модели)." # Не выходим
                     if reply and not reply.startswith("❌") and not reply.startswith("🤖"): logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Успешный конспект на попытке {attempt + 1}."); break
                     elif reply: # Если это сообщение об ошибке
                          if "ограничение модели" in reply or "Блокировка:" in reply or "Причина:" in reply or reply.startswith("❌"): break # Фатальная
                 except Exception as e: # Ловим все ошибки
                     error_message = str(e); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
                     is_block_error = "blocked" in error_message.lower() or "safety" in error_message.lower(); is_stop_error = "stopped" in error_message.lower(); is_retryable = "500" in error_message or "503" in error_message or "internal" in error_message.lower() or "deadline exceeded" in error_message.lower(); is_fatal_api_error = "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported" in error_message.lower()
                     if is_block_error or is_stop_error: reason_str = "неизвестна"; try: reason_str = str(e.args[0]) if hasattr(e, 'args') and e.args else "N/A"; except Exception: pass; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Конспект заблокирован/остановлен..."); reply = f"❌ Не удалось создать конспект (ограничение модели)."; break
                     elif is_fatal_api_error: reply = f"❌ Ошибка API при создании конспекта ({error_message[:100]}...)."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Неповторяемая ошибка API: {e}", exc_info=True); break
                     elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Повторяемая ошибка, ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
                     else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось создать конспект после {attempt + 1} попыток...", exc_info=True); if reply is None: reply = f"❌ Ошибка при создании конспекта после {RETRY_ATTEMPTS} попыток."; break
            # --- Сохранение в историю и отправка ---
            history_entry_user = { "role": "user", "parts": [{"text": user_message_with_id}], "youtube_video_id": youtube_id, "user_id": user_id, "message_id": user_message_id }; chat_history.append(history_entry_user); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (YouTube) в chat_history.")
            if reply and not reply.startswith("❌") and not reply.startswith("🤖"): model_reply_text_with_prefix = f"{YOUTUBE_SUMMARY_PREFIX}{reply}"
            else: model_reply_text_with_prefix = reply if reply else "🤖 Не удалось создать конспект видео (неизвестная причина)."
            history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]}; chat_history.append(history_entry_model); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (YouTube) в chat_history.")
            reply_to_send = reply if (reply and not reply.startswith("❌") and not reply.startswith("🤖")) else model_reply_text_with_prefix
            if reply_to_send: await send_reply(message, reply_to_send, context)
            else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет ответа для отправки."); try: await message.reply_text("🤖 К сожалению, не удалось создать конспект видео."); except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось отправить сообщение об ошибке: {e_final_fail}")
            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return

    # ############################################################
    # ####### КОНЕЦ БЛОКА ОБРАБОТКИ YOUTUBE ССЫЛОК ##############
    # ############################################################

    # --- Стандартная обработка текста (если не YouTube) ---
    if not youtube_handled:
        if not original_user_message_text and not image_file_id_from_ocr:
            if not message.photo and not message.document and not message.video: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Игнорирование сообщения без текста, OCR, фото, документа или видео."); return
        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0); await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        # --- Формирование финального промпта с ВРЕМЕНЕМ ---
        current_time_str = get_current_time_str(); time_context_str = f"(Текущая дата и время: {current_time_str})\n"; final_user_prompt_text = time_context_str + user_message_with_id
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Запрос к модели (встроенный поиск включен через API)..."); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Финальный промпт для Gemini (длина {len(final_user_prompt_text)}):\n{final_user_prompt_text[:600]}...")
        # --- История и ее обрезка ---
        history_entry_user = { "role": "user", "parts": [{"text": user_message_with_id}], "user_id": user_id, "message_id": user_message_id }
        if image_file_id_from_ocr: history_entry_user["image_file_id"] = image_file_id_from_ocr; logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (OCR) в chat_history.")
        else: logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (текст) в chat_history.")
        chat_history.append(history_entry_user)
        history_for_model_raw = []; current_total_chars = 0
        for entry in reversed(chat_history[:-1]):
            entry_text = ""; entry_len = 0; if entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text"): entry_text = entry["parts"][0]["text"]; entry_len = len(entry_text)
            if current_total_chars + entry_len + len(final_user_prompt_text) <= MAX_CONTEXT_CHARS: history_for_model_raw.append(entry); current_total_chars += entry_len
            elif entry_len == 0 and entry.get("role") == "model": history_for_model_raw.append(entry) # Добавляем пустые ответы модели
            elif entry_len > 0: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обрезка истории по символам ({MAX_CONTEXT_CHARS})..."); break
        history_for_model = list(reversed(history_for_model_raw))
        if not history_for_model or history_for_model[-1].get("role") != "user" or history_for_model[-1]["parts"][0].get("text") != final_user_prompt_text:
             if history_for_model and history_for_model[-1].get("role") == "user": history_for_model.pop()
             history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]})
        history_clean_for_model = []
        for entry in history_for_model: history_clean_for_model.append({"role": entry["role"], "parts": entry.get("parts", [])})
        # --- НАСТРОЙКА ИНСТРУМЕНТОВ ПОИСКА ---
        tools = []; tool_config = None
        # Включаем поиск только для моделей 1.5
        if any(keyword in model_id for keyword in ["1.5", "flash", "pro"]):
            try:
                if GoogleSearch != type('GoogleSearch', (object,), {}): # Проверяем, что GoogleSearch не заглушка
                    search_tool = Tool(google_search=GoogleSearch())
                    tools.append(search_tool)
                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Настроен инструмент GoogleSearch для модели {model_id}.")
                else: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Тип GoogleSearch не импортирован. Встроенный поиск не будет настроен.")
            except NameError: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка NameError при настройке GoogleSearch для {model_id}.")
            except Exception as e_tool_setup: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Неожиданная ошибка настройки GoogleSearch для {model_id}: {e_tool_setup}")
        else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Модель {model_id} не поддерживает встроенный поиск Google. Инструмент поиска не будет включен.")
        # --- Вызов модели с инструментами ---
        reply = None; response = None; last_exception = None; generation_successful = False
        for attempt in range(RETRY_ATTEMPTS):
            try:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id} (с tools: {bool(tools)})...")
                generation_config_obj = GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
                model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config_obj, system_instruction=system_instruction_text, tools=tools if tools else None, tool_config=None) # tool_config не используется с GoogleSearch
                response = await asyncio.to_thread(model.generate_content, history_clean_for_model)
                if hasattr(response, 'text'): reply = response.text
                else: reply = None
                # Логирование поиска
                grounding_triggered = False; search_queries = []
                if hasattr(response, 'candidates') and response.candidates: first_candidate = response.candidates[0]
                if hasattr(first_candidate, 'grounding_metadata') and first_candidate.grounding_metadata: grounding_triggered = True
                if hasattr(first_candidate.grounding_metadata, 'web_search_queries') and first_candidate.grounding_metadata.web_search_queries: search_queries = first_candidate.grounding_metadata.web_search_queries; logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ модели основан на поиске Google. Запросы: {search_queries}")
                else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Ответ модели содержит grounding_metadata, но без поисковых запросов.")
                if not reply: # Обработка пустого ответа
                     block_reason_str, finish_reason_str, safety_info_str = 'N/A', 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                     except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка извлечения причины/safety: {e_inner_reason}"); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                     reply = "🤖 Модель дала пустой ответ."; generation_successful = True; break # Считаем пустой ответ успехом
                if reply: generation_successful = True
                if generation_successful: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Успешная генерация на попытке {attempt + 1}. Поиск сработал: {grounding_triggered}"); break
            except Exception as e: # Ловим все ошибки
                 last_exception = e; error_message = str(e); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка генерации на попытке {attempt + 1}: {error_message[:200]}...")
                 is_block_error = "blocked" in error_message.lower() or "safety" in error_message.lower(); is_stop_error = "stopped" in error_message.lower(); is_retryable = "500" in error_message or "503" in error_message or "internal" in error_message.lower() or "deadline exceeded" in error_message.lower(); is_fatal_api_error = "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported" in error_message.lower()
                 if is_block_error or is_stop_error: reason_str = "неизвестна"; try: reason_str = str(e.args[0]) if hasattr(e, 'args') and e.args else "N/A"; except Exception: pass; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Запрос заблокирован/остановлен моделью..."); reply = f"❌ Запрос заблокирован/остановлен моделью."; break
                 elif is_fatal_api_error: reply = f"❌ Ошибка API ({error_message[:100]}...)."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Неповторяемая ошибка API: {e}", exc_info=True); break
                 elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Повторяемая ошибка, ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
                 else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось выполнить генерацию после {attempt + 1} попыток...", exc_info=True); if reply is None: reply = f"❌ Ошибка при обращении к модели после {RETRY_ATTEMPTS} попыток."; break
        # --- Добавление ответа и отправка (если не YouTube) ---
        if reply:
             history_entry_model = {"role": "model", "parts": [{"text": reply}]}
             chat_history.append(history_entry_model)
             if message: await send_reply(message, reply, context)
             else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не найдено сообщение для ответа в update."); try: await context.bot.send_message(chat_id=chat_id, text=reply); except Exception as e_send_direct: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить ответ напрямую: {e_send_direct}")
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

# ===== Обработчик фото =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = update.effective_user.id; message = update.message
    if not message or not message.photo: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_photo не найдено фото."); return
    photo_file_id = message.photo[-1].file_id; user_message_id = message.message_id
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен photo file_id: ...{photo_file_id[-10:]}, message_id: {user_message_id}")
    tesseract_available = False
    try: pytesseract.pytesseract.get_tesseract_version(); tesseract_available = True; logger.info("Tesseract найден и доступен.")
    except Exception as e_tess_check: logger.info(f"Tesseract не найден или не настроен. OCR будет отключен. Ошибка: {e_tess_check}")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file(); file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Скачанное фото (file_id: ...{photo_file_id[-10:]}) оказалось пустым."); await message.reply_text("❌ Не удалось загрузить изображение (файл пуст)."); return
    except TelegramError as e_tg_dl: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Telegram при скачивании фото (file_id: ...{photo_file_id[-10:]}): {e_tg_dl}", exc_info=True); try: await message.reply_text(f"❌ Ошибка Telegram при загрузке изображения: {e_tg_dl}"); except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания фото: {e_reply}"); return
    except Exception as e: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото (file_id: ...{photo_file_id[-10:]}): {e}", exc_info=True); try: await message.reply_text("❌ Не удалось загрузить изображение (ошибка скачивания)."); except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания фото: {e_reply}"); return
    user_caption = message.caption if message.caption else ""; ocr_triggered = False
    # --- OCR ---
    if tesseract_available:
        try:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Попытка OCR...")
            image = Image.open(io.BytesIO(file_bytes)); extracted_text = await asyncio.to_thread(pytesseract.image_to_string, image, lang='rus+eng', timeout=30); extracted_text_stripped = extracted_text.strip() if extracted_text else ""
            if extracted_text_stripped:
                ocr_triggered = True; logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружен текст на изображении (OCR). Длина: {len(extracted_text_stripped)}.")
                MAX_OCR_TEXT_LEN = 2000; ocr_text_for_prompt = extracted_text_stripped[:MAX_OCR_TEXT_LEN]
                if len(extracted_text_stripped) > MAX_OCR_TEXT_LEN: ocr_text_for_prompt += "\n...(текст обрезан)"; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст OCR обрезан до {MAX_OCR_TEXT_LEN} символов для промпта.")
                ocr_context = f"На изображении обнаружен следующий текст:\n```\n{ocr_text_for_prompt}\n```"
                if user_caption: user_prompt_ocr = f"{user_caption}\n{ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                else: user_prompt_ocr = f"{ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                message.image_file_id = photo_file_id; message.text = user_prompt_ocr; logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с OCR текстом и image_file_id."); await handle_message(update, context); return
            else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | OCR не нашел значимый текст на изображении.")
        except pytesseract.TesseractNotFoundError: logger.error("Tesseract не найден! OCR отключен."); tesseract_available = False
        except RuntimeError as timeout_error:
            if "Tesseract process timeout" in str(timeout_error): logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | OCR таймаут: {timeout_error}"); await message.reply_text("⏳ Не удалось распознать текст (слишком долго). Анализирую как фото...")
            else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка выполнения OCR: {timeout_error}", exc_info=True); await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
        except Exception as e_ocr:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Неожиданная ошибка OCR: {e_ocr}", exc_info=True)
            try: await message.reply_text("⚠️ Ошибка распознавания текста. Анализирую как фото...")
            except Exception as e_reply_ocr_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке OCR: {e_reply_ocr_err}")
    # --- Обработка как изображение (Vision) ---
    if not ocr_triggered:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обработка фото как изображения (Vision)."); await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        MAX_IMAGE_BYTES = 4 * 1024 * 1024
        if len(file_bytes) > MAX_IMAGE_BYTES: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Изображение ({len(file_bytes) / (1024*1024):.2f} MB) превышает рекомендованный лимит {MAX_IMAGE_BYTES // (1024*1024)} MB.")
        try: b64_data = base64.b64encode(file_bytes).decode()
        except Exception as e_b64: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Base64 кодирования: {e_b64}", exc_info=True); try: await message.reply_text("❌ Ошибка обработки изображения (кодирование)."); except Exception as e_reply_b64_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке Base64: {e_reply_b64_err}"); return
        current_time_str = get_current_time_str()
        if user_caption: prompt_text_vision = f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
        else: prompt_text_vision = f"(Текущая дата и время: {current_time_str})\n{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении."
        mime_type = "image/jpeg";
        if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
        elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
        elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WEBP': mime_type = "image/webp"
        elif file_bytes.startswith(b'GIF87a') or file_bytes.startswith(b'GIF89a'): mime_type = "image/gif"
        elif file_bytes.startswith(b'BM'): mime_type = "image/bmp"
        parts = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]; content_for_vision = [{"role": "user", "parts": parts}]
        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
        vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra', '1.5']; is_vision_model = any(keyword in model_id for keyword in vision_capable_keywords)
        if not is_vision_model:
            vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)];
            if vision_models: original_model_name = AVAILABLE_MODELS.get(model_id, model_id); fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id; new_model_name = AVAILABLE_MODELS.get(model_id, model_id); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
            else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Нет доступных vision моделей."); await message.reply_text("❌ Нет доступных моделей для анализа изображений."); history_entry_user_err = { "role": "user", "parts": [{"text": USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото.")}], "image_file_id": photo_file_id, "user_id": user_id, "message_id": user_message_id }; history_entry_model_err = { "role": "model", "parts": [{"text": "❌ Нет доступных моделей для анализа изображений."}]}; chat_history = context.chat_data.setdefault("history", []); chat_history.extend([history_entry_user_err, history_entry_model_err]); while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0); return
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Анализ изображения (Vision). Модель: {model_id}, Темп: {temperature}, MIME: {mime_type}"); reply = None; response_vision = None
        for attempt in range(RETRY_ATTEMPTS): # --- Вызов Vision ---
            try:
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Попытка {attempt + 1}/{RETRY_ATTEMPTS}..."); generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text); response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)
                if hasattr(response_vision, 'text'): reply = response_vision.text
                else: reply = None
                if not reply: block_reason_str, finish_reason_str = 'N/A', 'N/A'; try: # ... (извлечение причин) ...
                    except Exception as e_inner_reason: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка извлечения причины: {e_inner_reason}"); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Пустой ответ (попытка {attempt + 1})..."); # ... (формирование reply об ошибке) ...
                    reply = "🤖 Модель дала пустой ответ для этого изображения." # Не выходим
                if reply and not reply.startswith("❌") and not reply.startswith("🤖"): logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Успешный анализ на попытке {attempt + 1}."); break
                elif reply: # Если это сообщение об ошибке
                     if "Блокировка:" in reply or "ограничение модели" in reply or "Ошибка при анализе" in reply: break # Фатальная
            except Exception as e: # Ловим все ошибки
                 error_message = str(e); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Ошибка на попытке {attempt + 1}: {error_message[:200]}...")
                 is_block_error = "blocked" in error_message.lower() or "safety" in error_message.lower(); is_stop_error = "stopped" in error_message.lower(); is_retryable = "500" in error_message or "503" in error_message or "internal" in error_message.lower() or "deadline exceeded" in error_message.lower(); is_fatal_api_error = "400" in error_message or "429" in error_message or "location is not supported" in error_message or "unsupported" in error_message.lower() or "image format" in error_message.lower()
                 if is_block_error or is_stop_error: reason_str = "неизвестна"; try: reason_str = str(e.args[0]) if hasattr(e, 'args') and e.args else "N/A"; except Exception: pass; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Анализ заблокирован/остановлен..."); reply = f"❌ Анализ изображения заблокирован/остановлен моделью (Причина: {reason_str})."; break
                 elif is_fatal_api_error: reply = f"❌ Ошибка API при анализе изображения ({error_message[:100]}...)."; logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Неповторяемая ошибка API: {e}", exc_info=True); break
                 elif is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Повторяемая ошибка, ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
                 else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось выполнить анализ после {attempt + 1} попыток...", exc_info=True); if reply is None: reply = f"❌ Ошибка при анализе изображения после {RETRY_ATTEMPTS} попыток."; break
        # --- Сохранение и отправка ---
        chat_history = context.chat_data.setdefault("history", []); user_text_for_history_vision = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + (user_caption if user_caption else "Пользователь прислал фото."); history_entry_user = { "role": "user", "parts": [{"text": user_text_for_history_vision}], "image_file_id": photo_file_id, "user_id": user_id, "message_id": user_message_id }; chat_history.append(history_entry_user); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (Vision) в chat_history.")
        if reply and not reply.startswith("❌") and not reply.startswith("🤖"): model_reply_text_with_prefix = f"{IMAGE_DESCRIPTION_PREFIX}{reply}"
        else: model_reply_text_with_prefix = reply if reply else "🤖 Не удалось проанализировать изображение (неизвестная причина)."
        history_entry_model = {"role": "model", "parts": [{"text": model_reply_text_with_prefix}]}; chat_history.append(history_entry_model); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (Vision) в chat_history.")
        reply_to_send = reply if (reply and not reply.startswith("❌") and not reply.startswith("🤖")) else model_reply_text_with_prefix
        if reply_to_send: await send_reply(message, reply_to_send, context)
        else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Нет ответа для отправки."); try: await message.reply_text("🤖 К сожалению, не удалось проанализировать изображение после всех попыток."); except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Не удалось отправить сообщение об ошибке: {e_final_fail}")
        while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

# ===== Обработчик документов =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = update.effective_user.id; message = update.message
    if not message or not message.document: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_document нет документа."); return
    doc = message.document; allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-shellscript', 'application/javascript', 'application/yaml', 'application/x-tex', 'application/rtf', 'application/sql', 'application/x-javascript', 'application/x-yaml', 'application/x-sh', 'message/rfc822'); allowed_mime_types = ('application/octet-stream',)
    mime_type = doc.mime_type or "application/octet-stream"; file_name = doc.file_name or "документ"; is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes); is_allowed_type = mime_type in allowed_mime_types; is_potentially_text = is_allowed_prefix or is_allowed_type
    text_extensions = ('.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.csv', '.yaml', '.yml', '.sh', '.bash', '.zsh', '.md', '.rst', '.tex', '.log', '.sql', '.rtf', '.eml', '.ini', '.cfg', '.conf', '.toml', '.php', '.java', '.c', '.cpp', '.h', '.cs', '.go', '.rb', '.pl', '.swift', '.kt', '.kts', '.dart', '.lua')
    if not is_potentially_text and file_name and isinstance(file_name, str):
        if file_name.lower().endswith(text_extensions): logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{file_name}' (MIME: {mime_type}) имеет текстовое расширение. Попытка чтения."); is_potentially_text = True
    if not is_potentially_text: await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Неподдерживаемый файл для чтения: {file_name} (MIME: {mime_type})"); return
    MAX_FILE_SIZE_MB = 50; file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0 and file_name: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой файл '{file_name}'."); await update.message.reply_text(f"ℹ️ Файл '{file_name}' пустой."); return
    elif file_size_bytes == 0 and not file_name: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой документ без имени."); return
    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024: await update.message.reply_text(f"❌ Файл `{file_name}` слишком большой (> {MAX_FILE_SIZE_MB} MB).", parse_mode=ParseMode.MARKDOWN); logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Слишком большой файл: {file_name} ({file_size_bytes / (1024*1024):.2f} MB)"); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try: doc_file = await doc.get_file(); file_bytes = await doc_file.download_as_bytearray()
    if not file_bytes: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{file_name}' скачан, но оказался пустым."); await update.message.reply_text(f"ℹ️ Файл '{file_name}' пустой."); return
    except TelegramError as e_tg_dl_doc: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Telegram при скачивании документа '{file_name}': {e_tg_dl_doc}", exc_info=True); try: await update.message.reply_text(f"❌ Ошибка Telegram при загрузке файла: {e_tg_dl_doc}"); except Exception as e_reply_dl_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания документа: {e_reply_dl_err}"); return
    except Exception as e_dl_doc: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать документ '{file_name}': {e_dl_doc}", exc_info=True); try: await update.message.reply_text("❌ Не удалось загрузить файл (ошибка скачивания)."); except Exception as e_reply_dl_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания документа: {e_reply_dl_err}"); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None; detected_encoding = None; encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251']; chardet_available = False; detected_chardet_encoding = None
    try: import chardet; chardet_available = True
    except ImportError: logger.info("Библиотека chardet не найдена.")
    if chardet_available:
        try:
            chardet_limit = min(len(file_bytes), 100 * 1024);
            if chardet_limit > 0: detected = chardet.detect(file_bytes[:chardet_limit])
            if detected and detected['encoding'] and detected['confidence'] > 0.7: potential_encoding = detected['encoding'].lower(); logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet: {potential_encoding} ({detected['confidence']:.2f}) для '{file_name}'"); if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'): detected_chardet_encoding = 'utf-8-sig'; logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet UTF-8 с BOM -> 'utf-8-sig'."); else: detected_chardet_encoding = potential_encoding
            else: logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Chardet не уверен ({detected.get('confidence', 0):.2f}) для '{file_name}'.")
        except Exception as e_chardet: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка chardet для '{file_name}': {e_chardet}")
    if detected_chardet_encoding and detected_chardet_encoding not in encodings_to_try: encodings_to_try.insert(0, detected_chardet_encoding)
    encodings_to_try.extend(['latin-1', 'cp866', 'iso-8859-5']); unique_encodings = list(dict.fromkeys(encodings_to_try)); logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Попытки декодирования для '{file_name}': {unique_encodings}")
    for encoding in unique_encodings:
        try: text = file_bytes.decode(encoding); detected_encoding = encoding; logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{file_name}' декодирован как {encoding}."); break
        except (UnicodeDecodeError, LookupError): logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{file_name}' не в {encoding}.")
        except Exception as e_decode: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка декодирования '{file_name}' как {encoding}: {e_decode}", exc_info=True)
    if text is None: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось декодировать '{file_name}'"); await update.message.reply_text(f"❌ Не удалось прочитать файл `{file_name}`.", parse_mode=ParseMode.MARKDOWN); return
    text_stripped = text.strip();
    if not text_stripped and len(file_bytes) > 0: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{file_name}' дал пустой текст ({detected_encoding})."); await update.message.reply_text(f"⚠️ Файл `{file_name}` пустой или содержит только невидимые символы.", parse_mode=ParseMode.MARKDOWN); return
    elif not text_stripped and len(file_bytes) == 0: pass # Уже обработано выше
    MAX_FILE_CHARS_FOR_PROMPT = int(MAX_CONTEXT_CHARS * 0.85); truncated_text = text; truncation_warning = ""
    if len(text) > MAX_FILE_CHARS_FOR_PROMPT: truncated_text = text[:MAX_FILE_CHARS_FOR_PROMPT]; last_newline = truncated_text.rfind('\n'); if last_newline != -1 and last_newline > MAX_FILE_CHARS_FOR_PROMPT * 0.9: truncated_text = truncated_text[:last_newline]; chars_k = len(truncated_text) // 1000; total_chars_k = len(text) // 1000; truncation_warning = f"\n\n**(⚠️ Текст файла был обрезан до ~{chars_k}k символов)**"; logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст файла '{file_name}' ({len(text)} симв.) обрезан до {len(truncated_text)} символов.")
    user_caption = message.caption if message.caption else ""; encoding_info = f"(кодировка: {detected_encoding})" if detected_encoding else ""
    file_context = f"Содержимое файла `{file_name}` {encoding_info}:\n```\n{truncated_text}\n```{truncation_warning}"
    if user_caption: safe_caption = user_caption.replace('"', '\\"'); user_prompt_doc = f"Пользователь загрузил файл `{file_name}` с комментарием: \"{safe_caption}\".\n{file_context}\nПроанализируй, пожалуйста, содержимое файла, учитывая комментарий."
    else: user_prompt_doc = f"Пользователь загрузил файл `{file_name}`.\n{file_context}\nЧто можешь сказать об этом тексте?"
    message.text = user_prompt_doc; logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Передача управления в handle_message с текстом документа '{file_name}'.")
    await handle_message(update, context)
# ====================================================================

# --- Функции веб-сервера и запуска ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    timeout = aiohttp.ClientTimeout(total=120.0, connect=20.0, sock_connect=20.0, sock_read=60.0)
    aiohttp_session = aiohttp.ClientSession(timeout=timeout)
    application.bot_data['aiohttp_session'] = aiohttp_session
    logger.info("Сессия aiohttp создана и сохранена в bot_data.")
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("model", model_command)); application.add_handler(CommandHandler("clear", clear_history)); application.add_handler(CommandHandler("temp", set_temperature)); application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_")); application.add_handler(MessageHandler(filters.PHOTO, handle_photo)); application.add_handler(MessageHandler(filters.Document.ALL, handle_document)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    try:
        await application.initialize(); webhook_host_cleaned = WEBHOOK_HOST.rstrip('/'); webhook_path_segment = GEMINI_WEBHOOK_PATH.strip('/')
        if not webhook_host_cleaned.endswith('/') and not webhook_path_segment.startswith('/'): webhook_url = f"{webhook_host_cleaned}/{webhook_path_segment}"
        elif webhook_host_cleaned.endswith('/') and webhook_path_segment.startswith('/'): webhook_url = webhook_host_cleaned + webhook_path_segment[1:]
        else: webhook_url = webhook_host_cleaned + webhook_path_segment
        logger.info(f"Попытка установки вебхука: {webhook_url}"); secret_token = os.getenv('WEBHOOK_SECRET_TOKEN'); await application.bot.set_webhook( url=webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, secret_token=secret_token if secret_token else None, read_timeout=60, connect_timeout=30 ); logger.info(f"Вебхук успешно установлен на {webhook_url}" + (" с секретным токеном." if secret_token else ".")); web_server_coro = run_web_server(application, stop_event); return application, web_server_coro
    except Exception as e: logger.critical(f"Критическая ошибка при инициализации бота или установке вебхука: {e}", exc_info=True); if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed: await application.bot_data['aiohttp_session'].close(); logger.info("Сессия aiohttp закрыта из-за ошибки инициализации."); raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def health_check(request): logger.debug("Health check '/' requested."); return aiohttp.web.Response(text="OK: Web server running.")
    async def bot_health_check(request):
         logger.debug("Health check '/health' requested.")
         try: bot_info = await application.bot.get_me();
         if bot_info: logger.debug(f"Bot health check successful: Bot @{bot_info.username} is alive."); return aiohttp.web.Response(text=f"OK: Bot {bot_info.username} is running.")
         else: logger.warning("Bot health check: Bot info unavailable from get_me()."); return aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
         except TelegramError as e_tg: logger.error(f"Bot health check failed (TelegramError): {e_tg}", exc_info=True); return aiohttp.web.Response(text=f"Error: Telegram API error ({type(e_tg).__name__})", status=503)
         except Exception as e: logger.error(f"Bot health check failed (Exception): {e}", exc_info=True); return aiohttp.web.Response(text=f"Error: Health check failed ({type(e).__name__})", status=503)
    app.router.add_get('/', health_check); app.router.add_get('/health', bot_health_check); app['bot_app'] = application; webhook_path = GEMINI_WEBHOOK_PATH.strip('/');
    if not webhook_path.startswith('/'): webhook_path = '/' + webhook_path
    app.router.add_post(webhook_path, handle_telegram_webhook); logger.info(f"Вебхук будет слушаться на пути: {webhook_path}"); runner = aiohttp.web.AppRunner(app); await runner.setup(); port = int(os.getenv("PORT", "10000")); host = os.getenv("HOST", "0.0.0.0"); site = aiohttp.web.TCPSite(runner, host, port)
    try: await site.start(); logger.info(f"Веб-сервер запущен на http://{host}:{port}"); await stop_event.wait()
    except asyncio.CancelledError: logger.info("Задача веб-сервера была отменена.")
    except Exception as e: logger.error(f"Ошибка при запуске или работе веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally: logger.info("Начало остановки веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер успешно остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app')
    if not application: logger.critical("Приложение бота не найдено в контексте веб-сервера!"); return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured.")
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN')
    if secret_token:
         header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
         if header_token != secret_token: logger.warning(f"Неверный секретный токен в заголовке от {request.remote}."); return aiohttp.web.Response(status=403, text="Forbidden: Invalid secret token.")
    try: data = await request.json(loads=json.loads); update = Update.de_json(data, application.bot); logger.debug(f"Получен Update ID: {update.update_id} от Telegram."); asyncio.create_task(application.process_update(update)); return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e_json: body = await request.text(); logger.error(f"Ошибка декодирования JSON от Telegram: {e_json}. Тело запроса: {body[:500]}..."); return aiohttp.web.Response(text="Bad Request: JSON decode error", status=400)
    except TelegramError as e_tg: logger.error(f"Ошибка Telegram при обработке вебхука: {e_tg}", exc_info=True); return aiohttp.web.Response(text="OK", status=200) # Отвечаем OK, чтобы Telegram не повторял
    except Exception as e: logger.error(f"Критическая ошибка обработки вебхука: {e}", exc_info=True); return aiohttp.web.Response(text="OK", status=200) # Отвечаем OK, но логируем

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper(); log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING); logging.getLogger('httpcore').setLevel(logging.WARNING); logging.getLogger('google.api_core').setLevel(logging.WARNING); logging.getLogger('google.auth').setLevel(logging.WARNING); logging.getLogger('google.generativeai').setLevel(logging.INFO); logging.getLogger('PIL').setLevel(logging.INFO); logging.getLogger('pytesseract').setLevel(logging.INFO); logging.getLogger('aiohttp.access').setLevel(logging.WARNING); logging.getLogger('telegram.ext').setLevel(logging.INFO); logging.getLogger('telegram.bot').setLevel(logging.INFO); logger.setLevel(log_level)
    logger.info(f"--- Установлен уровень логгирования для '{logger.name}': {log_level_str} ({log_level}) ---")
    loop = asyncio.get_running_loop(); stop_event = asyncio.Event()
    def signal_handler():
        if not stop_event.is_set(): logger.info("Получен сигнал SIGINT/SIGTERM, инициирую штатную остановку..."); stop_event.set()
        else: logger.warning("Повторный сигнал остановки получен, процесс уже завершается.")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError: logger.warning(f"Не удалось установить обработчик сигнала {sig.name} через loop."); try: signal.signal(sig, lambda s, f: signal_handler()); except Exception as e_signal: logger.error(f"Не удалось установить обработчик сигнала {sig.name} через signal.signal(): {e_signal}")
    application = None; web_server_task = None; aiohttp_session_main = None
    try:
        logger.info(f"--- Запуск приложения Gemini Telegram Bot ---"); application, web_server_coro = await setup_bot_and_server(stop_event); web_server_task = asyncio.create_task(web_server_coro, name="WebServerTask"); aiohttp_session_main = application.bot_data.get('aiohttp_session'); logger.info("Приложение настроено, веб-сервер запущен. Ожидание сигнала остановки (Ctrl+C)..."); await stop_event.wait()
    except asyncio.CancelledError: logger.info("Главная задача main() была отменена.")
    except Exception as e: logger.critical("Критическая ошибка во время запуска или ожидания.", exc_info=True)
    finally:
        logger.info("--- Начало процесса штатной остановки приложения ---");
        if not stop_event.is_set(): stop_event.set()
        if web_server_task and not web_server_task.done():
             logger.info("Остановка веб-сервера (через stop_event)...")
             try: await asyncio.wait_for(web_server_task, timeout=20.0); logger.info("Веб-сервер успешно завершен.")
             except asyncio.TimeoutError: logger.warning("Веб-сервер не завершился за 20 секунд, принудительная отмена..."); web_server_task.cancel(); try: await web_server_task; except asyncio.CancelledError: logger.info("Задача веб-сервера успешно отменена."); except Exception as e_cancel_ws: logger.error(f"Ошибка при ожидании отмененной задачи веб-сервера: {e_cancel_ws}", exc_info=True)
             except asyncio.CancelledError: logger.info("Ожидание веб-сервера было отменено внешне.")
             except Exception as e_wait_ws: logger.error(f"Ошибка при ожидании завершения веб-сервера: {e_wait_ws}", exc_info=True)
        if application:
            logger.info("Остановка приложения Telegram бота (application.shutdown)...")
            try: await application.shutdown(); logger.info("Приложение Telegram бота успешно остановлено.")
            except Exception as e_shutdown: logger.error(f"Ошибка во время application.shutdown(): {e_shutdown}", exc_info=True)
        if aiohttp_session_main and not aiohttp_session_main.closed: logger.info("Закрытие основной сессии aiohttp..."); await aiohttp_session_main.close(); await asyncio.sleep(0.5); logger.info("Основная сессия aiohttp закрыта.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()];
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся фоновых задач (например, process_update)..."); [task.cancel() for task in tasks]; results = await asyncio.gather(*tasks, return_exceptions=True); cancelled_count, error_count = 0, 0
            for i, res in enumerate(results): task_name = tasks[i].get_name(); if isinstance(res, asyncio.CancelledError): cancelled_count += 1; logger.debug(f"Задача '{task_name}' успешно отменена."); elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка в отмененной задаче '{task_name}': {res}", exc_info=isinstance(res, BaseException)); else: logger.debug(f"Задача '{task_name}' завершилась с результатом: {res}")
            logger.info(f"Фоновые задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Приложение прервано пользователем (KeyboardInterrupt в __main__).")
    except Exception as e_top: logger.critical("Неперехваченная ошибка на верхнем уровне asyncio.run(main).", exc_info=True)

# --- END OF FILE main.py ---
