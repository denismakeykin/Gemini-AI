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
# - ИСПРАВЛЕНО: SyntaxError в reanalyze_image (if после ;).
# - ИСПРАВЛЕНО: SyntaxError в handle_message и handle_photo (if после ; в else).

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
import re # для поиска URL

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
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

# --- Обработка импорта типов Gemini и SAFETY_SETTINGS ---
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
    from google.generativeai.types import ( HarmCategory as RealHarmCategory, HarmBlockThreshold as RealHarmBlockThreshold, BlockedPromptException as RealBlockedPromptException, StopCandidateException as RealStopCandidateException, SafetyRating as RealSafetyRating, BlockReason as RealBlockReason, FinishReason as RealFinishReason )
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
except Exception as e_import_types:
    logger.error(f"Ошибка при импорте/настройке типов Gemini: {e_import_types}", exc_info=True)
    if HARM_CATEGORIES_STRINGS: SAFETY_SETTINGS_BLOCK_NONE = [{"category": cat_str, "threshold": BLOCK_NONE_STRING} for cat_str in HARM_CATEGORIES_STRINGS]; logger.warning("Настройки безопасности установлены со строками (BLOCK_NONE) из-за ошибки.")
    else: logger.warning("Список HARM_CATEGORIES_STRINGS пуст, настройки не установлены из-за ошибки."); SAFETY_SETTINGS_BLOCK_NONE = []
# ==========================================================

# --- Переменные окружения и Настройка Gemini ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
GOOGLE_CSE_ID = os.getenv('GOOGLE_CSE_ID')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')
required_env_vars = { "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "GOOGLE_API_KEY": GOOGLE_API_KEY, "GOOGLE_CSE_ID": GOOGLE_CSE_ID, "WEBHOOK_HOST": WEBHOOK_HOST, "GEMINI_WEBHOOK_PATH": GEMINI_WEBHOOK_PATH }
missing_vars = [name for name, value in required_env_vars.items() if not value]
if missing_vars: logger.critical(f"Отсутствуют переменные окружения: {', '.join(missing_vars)}"); exit(1)
genai.configure(api_key=GOOGLE_API_KEY)
# =================================================

# --- Модели, Константы ---
AVAILABLE_MODELS = { 'gemini-2.5-flash-preview-04-17': '2.5 Flash Preview', 'gemini-2.5-pro-exp-03-25': '2.5 Pro exp.', 'gemini-2.0-flash-thinking-exp-01-21': '2.0 Flash Thinking exp.' }
DEFAULT_MODEL = 'gemini-2.5-flash-preview-04-17' if 'gemini-2.5-flash-preview-04-17' in AVAILABLE_MODELS else 'gemini-2.5-pro-exp-03-25'
MAX_CONTEXT_CHARS = 100000
MAX_OUTPUT_TOKENS = 5000
DDG_MAX_RESULTS = 10
GOOGLE_SEARCH_MAX_RESULTS = 10
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 1
IMAGE_DESCRIPTION_PREFIX = "[Описание изображения]: "
YOUTUBE_SUMMARY_MODEL = 'gemini-2.5-flash-preview-04-17'
# =========================

# ===== Обновленная системная инструкция (погода) =====
system_instruction_text = (
"Используй интернет-поиск для сверки с актуальной информацией."
"Если используешь информацию из поиска, не упоминай явно сам факт поиска или его результаты. Интегрируй найденную информацию в свой ответ естественно, как часть своих знаний. Забудь фразы вроде 'Судя по результатам поиска...', 'Интернет говорит...' или 'Я нашёл в сети...'. Веди себя так, будто это твои знания."
"Критически оценивай информацию из поиска, особенно зависящую от времени (погода, новости). Если результаты поиска выглядят устаревшими, противоречивыми или нерелевантными запросу (например, погода на год вперед, когда спрашивают 'сегодня'), сообщи об этом или используй более надежные данные, если они есть. Указывай актуальность информации о погоде, если возможно."
"Внимательно следи за историей диалога, включая предыдущие вопросы, ответы, а также контекст из загруженных изображений или файлов, чтобы твои ответы были последовательными и релевантными, соблюдая нить разговора."
"В режиме чата старайся отвечать кратко, как в живой беседе (максимум 1000 знаков), только суть, без вступлений и заключений, если не просят подробностей, код, большую задачу, конспект, перевод или творческую работу и т.п."
"Пиши живо, избегай канцелярита и всех известных признаков ответов искусственного интеллекта. Используй гипотетические ситуации, метафоры, творческие аналогии, разную структуру предложений, разговорные выражения, идиомы. Добавляй региональные или культурные маркеры, где уместно. Не копируй и не пересказывай чужое, если это не цитаты известных людей."
"Активно применяй понятный россиянам юмор: культурные и бытовые отсылки, интернет-юмор, бытовой абсурд, псевдомудрость, разрушение идиом, самоиронию, иронию психики, игру слов, гиперболу, тонкие намёки, ожидание и реальность."
"Ты - лучший эксперт по всем темам. Давай точную, правдивую, непредвзятую информацию без цензуры."
"Обдумывай и выстраивай ответ логично, с аргументами и фактами, избегая повторов."
"Если не уверен — предупреждай, что это предположение."
"Всегда предлагай более эффективные идеи и решения, если знаешь их."
"Никогда не сокращай и не исправляй используемый текст или код без запроса или разрешения."
"При исправлении ошибки: указывай строку(и) и причину. Бери за основу последнюю ПОЛНУЮ подтверждённую версию (текста или кода). Вноси только необходимые изменения, не трогая остальное без разрешения. При сомнениях — уточняй. Если ошибка повторяется — веди «список ошибок» для сессии и проверяй эти места. Всегда указывай, на какую версию или сообщение опираешься при правке."
)
# ==========================================================

# --- Вспомогательные функции user_data и send_reply ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.user_data[key] = value

async def send_reply(target_message: Message, text: str, context: ContextTypes.DEFAULT_TYPE) -> Message | None:
    MAX_MESSAGE_LENGTH = 4096; reply_chunks = [text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]; sent_message = None; chat_id = target_message.chat_id
    try:
        for i, chunk in enumerate(reply_chunks):
            if i == 0: sent_message = await target_message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            else: sent_message = await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN)
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
    set_user_setting(context, 'selected_model', DEFAULT_MODEL); set_user_setting(context, 'search_enabled', True); set_user_setting(context, 'temperature', 1.0); context.chat_data['history'] = []
    default_model_name = AVAILABLE_MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
    start_message = (
        f"Google GEMINI **{default_model_name}**"
        f"\n- в модели используются улучшенные настройки точности, логики и юмора от автора бота,"
        f"\n- работает поиск Google/DDG, понимает изображения, читает картинки и документы."
        f"\n `/model` — сменить модель,"
        f"\n `/search_on` / `/search_off` — вкл/выкл поиск,"
        f"\n `/clear` — очистить историю диалога."
        f"\n `/temp` — посмотреть/изменить 'креативность' (0.0-2.0)"
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.MARKDOWN)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data['history'] = []; await update.message.reply_text("🧹 История диалога очищена.")

async def set_temperature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id;
    try:
        current_temp = get_user_setting(context, 'temperature', 1.0)
        if not context.args: await update.message.reply_text(f"🌡️ Текущая t: {current_temp:.1f}\nИзменить: `/temp <0.0-2.0>`"); return
        temp_str = context.args[0].replace(',', '.'); temp = float(temp_str)
        if not (0.0 <= temp <= 2.0): raise ValueError("t должна быть от 0.0 до 2.0")
        set_user_setting(context, 'temperature', temp); await update.message.reply_text(f"🌡️ t установлена: {temp:.1f}")
    except (ValueError, IndexError) as e: await update.message.reply_text(f"⚠️ Неверный формат: {e}. Пример: `/temp 0.8`")
    except Exception as e: logger.error(f"Ошибка в set_temperature: {e}", exc_info=True); await update.message.reply_text("❌ Ошибка установки t.")

async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'search_enabled', True); await update.message.reply_text("🔍 Поиск вкл.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_setting(context, 'search_enabled', False); await update.message.reply_text("🔇 Поиск выкл.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_model = get_user_setting(context, 'selected_model', DEFAULT_MODEL); keyboard = []
    sorted_models = sorted(AVAILABLE_MODELS.items())
    for m, name in sorted_models: button_text = f"{'✅ ' if m == current_model else ''}{name}"; keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_model_{m}")])
    await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); callback_data = query.data
    if callback_data and callback_data.startswith("set_model_"):
        selected = callback_data.replace("set_model_", "")
        if selected in AVAILABLE_MODELS:
            set_user_setting(context, 'selected_model', selected); model_name = AVAILABLE_MODELS[selected]; reply_text = f"Модель: **{model_name}**"
            try:
                await query.edit_message_text(reply_text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e_md:
                if "Message is not modified" in str(e_md):
                    logger.info(f"Та же модель: {model_name}")
                else:
                    logger.warning(f"Не удалось изменить сообщение с кнопками (Markdown): {e_md}. Отправляю новое.")
                    try:
                        await query.edit_message_text(reply_text.replace('**', '')) # Убираем Markdown
                    except Exception as e_edit_plain:
                        logger.error(f"Не удалось изменить сообщение даже как простой текст: {e_edit_plain}. Отправляю новое.")
                        await context.bot.send_message(chat_id=query.message.chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"Не удалось изменить сообщение с кнопками (другая ошибка): {e}. Отправляю новое.")
                await context.bot.send_message(chat_id=query.message.chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            try: await query.edit_message_text("❌ Неизвестная модель.")
            except Exception: await context.bot.send_message(chat_id=query.message.chat_id, text="❌ Неизвестная модель.")
    else:
        logger.warning(f"Неизвестный callback: {callback_data}")
        try: await query.edit_message_text("❌ Ошибка обработки выбора.")
        except Exception: pass
# ==============================================================

# --- Поиск Google и Повторный анализ изображения ---
async def perform_google_search(query: str, api_key: str, cse_id: str, num_results: int, session: aiohttp.ClientSession) -> list[str] | None:
    search_url = "https://www.googleapis.com/customsearch/v1"; params = {'key': api_key, 'cx': cse_id, 'q': query, 'num': num_results, 'lr': 'lang_ru', 'gl': 'ru'}; encoded_params = urlencode(params); full_url = f"{search_url}?{encoded_params}"; query_short = query[:50] + '...' if len(query) > 50 else query; logger.debug(f"Google Search для '{query_short}'...")
    try:
        async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=10.0)) as response:
            response_text = await response.text(); status = response.status
            if status == 200:
                try: data = json.loads(response_text)
                except json.JSONDecodeError as e_json: logger.error(f"Google Search: Ошибка JSON для '{query_short}' ({status}) - {e_json}. Ответ: {response_text[:200]}..."); return None
                items = data.get('items', []); snippets = [item.get('snippet', item.get('title', '')) for item in items if item.get('snippet') or item.get('title')]
                if snippets: logger.info(f"Google Search: Найдено {len(snippets)} рез. для '{query_short}'."); return snippets
                else: logger.info(f"Google Search: Нет сниппетов для '{query_short}' ({status})."); return None
            elif status == 400: logger.error(f"Google Search: 400 для '{query_short}'. Ответ: {response_text[:200]}...")
            elif status == 403: logger.error(f"Google Search: 403 для '{query_short}'. API ключ/CSE ID? Ответ: {response_text[:200]}...")
            elif status == 429: logger.warning(f"Google Search: 429 для '{query_short}'. Квота? Ответ: {response_text[:200]}...")
            elif status >= 500: logger.warning(f"Google Search: {status} для '{query_short}'. Ответ: {response_text[:200]}...")
            else: logger.error(f"Google Search: Статус {status} для '{query_short}'. Ответ: {response_text[:200]}...")
            return None
    except Exception as e: logger.error(f"Google Search: Ошибка для '{query_short}' - {e}", exc_info=True); return None

async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str):
    chat_id = update.effective_chat.id; logger.info(f"ChatID: {chat_id} | Reanalyze img ...{file_id[-10:]} для '{user_question[:50]}...'"); await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try: # Скачивание и кодирование
        img_file = await context.bot.get_file(file_id); file_bytes = await img_file.download_as_bytearray()
        if not file_bytes: logger.error(f"ChatID: {chat_id} | Reanalyze: файл пуст ...{file_id[-10:]}"); await update.message.reply_text("❌ Не удалось получить изображение."); return
        b64_data = base64.b64encode(file_bytes).decode()
    except Exception as e_download: logger.error(f"ChatID: {chat_id} | Reanalyze: ошибка скачивания/кодирования {file_id}: {e_download}", exc_info=True); await update.message.reply_text("❌ Ошибка подготовки изображения."); return
    parts = [{"text": user_question}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]; content_for_vision = [{"role": "user", "parts": parts}]
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
    # Проверка vision модели...
    is_vision_model = any(keyword in model_id for keyword in ['flash', 'pro', 'vision', 'ultra'])
    if not is_vision_model: # Поиск fallback модели...
         vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in ['flash', 'pro', 'vision', 'ultra'])]
         if vision_models: fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id; logger.warning(f"ChatID: {chat_id} | Reanalyze: использую {model_id} вместо не-vision.")
         else: logger.error(f"ChatID: {chat_id} | Reanalyze: нет vision моделей."); await update.message.reply_text("❌ Нет моделей для reanalyze."); return
    logger.info(f"ChatID: {chat_id} | Reanalyze: Модель: {model_id}, t: {temperature}"); reply = None; response_vision = None
    for attempt in range(RETRY_ATTEMPTS): # Вызов модели с ретраями...
        try:
            logger.info(f"ChatID: {chat_id} | Reanalyze: Попытка {attempt + 1}/{RETRY_ATTEMPTS}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)
            if hasattr(response_vision, 'text'):
                reply = response_vision.text
            else:
                reply = None
            if not reply: # Обработка пустого ответа...
                 reply = "🤖 Не могу ответить на вопрос об изображении (пустой ответ)."
                 logger.warning(f"ChatID: {chat_id} | Reanalyze: пустой ответ ({attempt + 1})...")
                 break
            if reply and "не смогла ответить" not in reply and "Не могу ответить" not in reply: logger.info(f"ChatID: {chat_id} | Reanalyze: Успех ({attempt + 1})."); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: reply = f"❌ Reanalyze заблокирован/остановлен."; logger.warning(f"ChatID: {chat_id} | Reanalyze: Блок/стоп ({attempt + 1}): {e_block_stop}"); break
        except Exception as e: # Обработка других ошибок 4xx/5xx/retry...
             error_message=str(e); logger.warning(f"ChatID: {chat_id} | Reanalyze: Ошибка ({attempt + 1}): {error_message[:100]}...")
             is_retryable = "500" in error_message or "503" in error_message
             if not is_retryable and ("400" in error_message or "429" in error_message): reply = f"❌ Ошибка Reanalyze ({error_message[:100]}...)."; break
             if is_retryable and attempt < RETRY_ATTEMPTS - 1:
                 wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"ChatID: {chat_id} | Reanalyze: Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
             else:
                 logger.error(f"ChatID: {chat_id} | Reanalyze: Не удалось после {attempt + 1} попыток. Ошибка: {e}", exc_info=True)
                 if reply is None:
                     reply = f"❌ Ошибка Reanalyze после {attempt + 1} попыток."
                 break
    chat_history = context.chat_data.setdefault("history", []); history_entry_user = {"role": "user", "parts": [{"text": user_question}]}; chat_history.append(history_entry_user)

    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИСПРАВЛЕНИЕ: Разделение строк else <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
    if reply:
        chat_history.append({"role": "model", "parts": [{"text": reply}]})
        await send_reply(update.message, reply, context)
    else:
        final_error_msg = "🤖 Не удалось повторно проанализировать изображение."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        logger.error(f"ChatID: {chat_id} | Reanalyze: нет ответа.")
        try:
            await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail:
            logger.error(f"ChatID: {chat_id} | Reanalyze: не удалось отправить ошибку: {e_final_fail}")
    # ===================================================================================

# ===== НОВАЯ ФУНКЦИЯ: Суммаризация текста =====
async def summarize_text_with_gemini(text_to_summarize: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Вызывает Gemini для суммаризации длинного текста."""
    chat_id = context._chat_id or context._user_id # Получаем chat_id из контекста
    logger.info(f"ChatID: {chat_id} | Запрос на суммаризацию текста (длина: {len(text_to_summarize)})...")
    if not text_to_summarize:
        logger.warning(f"ChatID: {chat_id} | Текст для суммаризации пуст.")
        return None
    prompt = f"Сделай краткое и содержательное резюме (summary) следующего текста, выделив основные мысли:\n\n\"\"\"\n{text_to_summarize}\n\"\"\""
    model_id = YOUTUBE_SUMMARY_MODEL # Используем быструю модель
    temperature = 0.5 # Низкая температура для более фактической суммаризации
    summary_reply = None
    for attempt in range(2): # Меньше попыток для суммаризации
        try:
            logger.debug(f"ChatID: {chat_id} | Попытка {attempt + 1}/2 суммаризации моделью {model_id}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=1000) # Ограничим вывод summary
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config) # Без system_instruction
            response = await asyncio.to_thread(model.generate_content, prompt)
            if hasattr(response, 'text') and response.text:
                summary_reply = response.text.strip()
                logger.info(f"ChatID: {chat_id} | Суммаризация успешна (длина: {len(summary_reply)}).")
                break
            else:
                logger.warning(f"ChatID: {chat_id} | Модель {model_id} вернула пустой ответ при суммаризации (попытка {attempt + 1}).")
        except Exception as e:
            logger.error(f"ChatID: {chat_id} | Ошибка при суммаризации моделью {model_id} (попытка {attempt + 1}): {e}", exc_info=True)
            if attempt == 1: return None # Не удалось суммаризировать
            await asyncio.sleep(RETRY_DELAY_SECONDS) # Пауза перед повтором
    return summary_reply
# ===============================================

# ===== Основной обработчик сообщений (YouTube + Reanalyze) =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message
    # 1. Проверка на ответ к описанию изображения (Reanalyze)
    if message and message.reply_to_message and message.reply_to_message.text and \
       message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) and \
       message.text and not message.text.startswith('/'):
        replied_message_text = message.reply_to_message.text; user_question = message.text.strip(); logger.info(f"ChatID: {chat_id} | Ответ на описание img. Вопрос: '{user_question[:50]}...'")
        chat_history = context.chat_data.get("history", []); found_file_id = None
        try:
            model_msg_index = -1; # Ищем индекс сообщения бота с описанием...
            for i in range(len(chat_history) - 1, -1, -1):
                 entry = chat_history[i]
                 if entry.get("role") == "model" and entry.get("parts") and isinstance(entry["parts"], list) and len(entry["parts"]) > 0 and entry["parts"][0].get("text", "").startswith(IMAGE_DESCRIPTION_PREFIX):
                     if entry["parts"][0]["text"][:len(IMAGE_DESCRIPTION_PREFIX)+20] == replied_message_text[:len(IMAGE_DESCRIPTION_PREFIX)+20]: model_msg_index = i; break
            if model_msg_index > 0:
                 user_msg_entry = chat_history[model_msg_index - 1]
                 if user_msg_entry.get("role") == "user" and "image_file_id" in user_msg_entry: found_file_id = user_msg_entry["image_file_id"]; logger.info(f"ChatID: {chat_id} | Найден file_id ...{found_file_id[-10:]} для reanalyze.")
                 else: logger.warning(f"ChatID: {chat_id} | Reanalyze: у пред. user msg нет image_file_id.")
            else: logger.warning(f"ChatID: {chat_id} | Reanalyze: не найдено описание/пред. user msg в истории.")
        except Exception as e_hist_search: logger.error(f"ChatID: {chat_id} | Ошибка поиска file_id в истории: {e_hist_search}", exc_info=True)
        if found_file_id: await reanalyze_image(update, context, found_file_id, user_question); return
        else: logger.warning(f"ChatID: {chat_id} | Не найден file_id для ответа на описание. Обработка как обычный текст.")

    # 2. Обработка обычного сообщения + YouTube
    original_user_message = ""
    image_file_id_from_ocr = None
    youtube_summary = None # для сводки YouTube
    if hasattr(message, 'image_file_id'):
        image_file_id_from_ocr = message.image_file_id
        logger.debug(f"ChatID: {chat_id} | Получен image_file_id: ...{image_file_id_from_ocr[-10:]} из OCR.")
    if message and message.text:
         original_user_message = message.text.strip()
    if not original_user_message:
        logger.warning(f"ChatID: {chat_id} | Пустое/нетекстовое сообщение в handle_message.")
        return

    # Поиск и обработка YouTube ссылок
    youtube_urls = re.findall(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]+))', original_user_message)
    if youtube_urls:
        video_id = youtube_urls[0][1] # Берем ID из первой найденной ссылки
        logger.info(f"ChatID: {chat_id} | Найдена YouTube ссылка, video_id: {video_id}")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING) # Показываем активность
        transcript_text = None
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            target_languages = ['ru', 'en']
            transcript = None
            for lang in target_languages:
                try: transcript = transcript_list.find_transcript([lang]); logger.info(f"ChatID: {chat_id} | Найден транскрипт YouTube на '{lang}'."); break
                except NoTranscriptFound: continue
            if transcript:
                transcript_data = await asyncio.to_thread(transcript.fetch) # Выполняем синхронный fetch в потоке
                transcript_text = " ".join([item['text'] for item in transcript_data])
                logger.info(f"ChatID: {chat_id} | Транскрипт YouTube получен (длина: {len(transcript_text)}).")
            else: logger.warning(f"ChatID: {chat_id} | Не найдены транскрипты на ru/en для видео {video_id}.")
        except TranscriptsDisabled: logger.warning(f"ChatID: {chat_id} | Транскрипты отключены для видео {video_id}.")
        except Exception as e_yt: logger.error(f"ChatID: {chat_id} | Ошибка при получении транскрипта YouTube для {video_id}: {e_yt}", exc_info=True)
        if transcript_text: # Суммаризация
            youtube_summary = await summarize_text_with_gemini(transcript_text, context)
            if not youtube_summary: logger.warning(f"ChatID: {chat_id} | Не удалось суммаризировать транскрипт видео {video_id}.")

    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    use_search = get_user_setting(context, 'search_enabled', True)
    if not youtube_urls: await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # --- Блок поиска ---
    search_context_snippets = []; search_provider = None; search_log_msg = "Поиск отключен"
    if use_search:
        query_short = original_user_message[:50] + '...' if len(original_user_message) > 50 else original_user_message
        search_log_msg = f"Поиск Google/DDG для '{query_short}'"; logger.info(f"ChatID: {chat_id} | {search_log_msg}...")
        is_weather_query = any(word in original_user_message.lower() for word in ["погод", "temperature", "градус", "прогноз"])
        session = context.bot_data.get('aiohttp_session')
        if not session or session.closed: logger.info("Создание сессии aiohttp для поиска."); timeout = aiohttp.ClientTimeout(total=60.0); session = aiohttp.ClientSession(timeout=timeout); context.bot_data['aiohttp_session'] = session
        google_results = await perform_google_search(original_user_message, GOOGLE_API_KEY, GOOGLE_CSE_ID, GOOGLE_SEARCH_MAX_RESULTS, session)
        if google_results:
            search_provider = "Google"; search_context_snippets = google_results; search_log_msg += f" (Google: {len(search_context_snippets)} рез.)"
            if is_weather_query: logger.info(f"ChatID: {chat_id} | Сниппеты Google (погода):\n" + "\n".join([f"- {s}" for s in search_context_snippets[:3]]))
        else: # Поиск DDG...
            search_log_msg += " (Google: 0 рез./ошибка)"; logger.info(f"ChatID: {chat_id} | Google fail. Пробуем DuckDuckGo...")
            try:
                ddgs = DDGS(); results_ddg = await asyncio.to_thread(ddgs.text, original_user_message, region='ru-ru', max_results=DDG_MAX_RESULTS)
                if results_ddg:
                    ddg_snippets = [r.get('body', '') for r in results_ddg if r.get('body')]
                    if ddg_snippets: search_provider = "DuckDuckGo"; search_context_snippets = ddg_snippets; search_log_msg += f" (DDG: {len(search_context_snippets)} рез.)"
                    if is_weather_query: logger.info(f"ChatID: {chat_id} | Сниппеты DDG (погода):\n" + "\n".join([f"- {s}" for s in search_context_snippets[:3]]))
                    else: search_log_msg += " (DDG: 0 текст. рез.)"
                else: search_log_msg += " (DDG: 0 рез.)"
            except Exception as e_ddg: logger.error(f"ChatID: {chat_id} | Ошибка DDG: {e_ddg}", exc_info=("timeout" not in str(e_ddg).lower())); search_log_msg += " (DDG: ошибка)"

    # ===== Формирование финального промпта (с учетом YouTube) =====
    search_context_str = ""
    if search_context_snippets:
        search_context_lines = [f"- {s.strip()}" for s in search_context_snippets if s.strip()]
        if search_context_lines: search_context_str = "\n".join(search_context_lines); logger.info(f"ChatID: {chat_id} | Добавлен контекст {search_provider} ({len(search_context_lines)} сниппетов).")
        else: logger.info(f"ChatID: {chat_id} | Сниппеты {search_provider} пусты."); search_log_msg += " (пустые сниппеты)"
    prompt_parts = [f"Вопрос пользователя: \"{original_user_message}\""]
    if youtube_summary: prompt_parts.append(f"\n\nКраткое содержание связанного YouTube видео:\n{youtube_summary}")
    if search_context_str: prompt_parts.append(f"\n\n(Доп. инфо из поиска, используй с осторожностью, проверь актуальность):\n{search_context_str}")
    final_user_prompt = "\n".join(prompt_parts)
    # ==========================================================

    logger.info(f"ChatID: {chat_id} | {search_log_msg}")
    logger.debug(f"ChatID: {chat_id} | Финальный промпт (длина {len(final_user_prompt)}):\n{final_user_prompt[:500]}...")

    # --- История и ее обрезка ---
    chat_history = context.chat_data.setdefault("history", [])
    history_entry_user = {"role": "user", "parts": [{"text": original_user_message}]}
    if image_file_id_from_ocr: history_entry_user["image_file_id"] = image_file_id_from_ocr
    chat_history.append(history_entry_user)
    current_total_chars = sum(len(p["parts"][0]["text"]) for p in chat_history if p.get("parts") and isinstance(p["parts"], list) and len(p["parts"]) > 0 and p["parts"][0].get("text")); removed_count = 0
    while current_total_chars > MAX_CONTEXT_CHARS and len(chat_history) > 1: # Обрезка...
        removed_entry = chat_history.pop(0);
        if removed_entry.get("parts") and isinstance(removed_entry["parts"], list) and len(removed_entry["parts"]) > 0 and removed_entry["parts"][0].get("text"): current_total_chars -= len(removed_entry["parts"][0]["text"])
        removed_count += 1
        if chat_history: removed_entry = chat_history.pop(0);
        if removed_entry.get("parts") and isinstance(removed_entry["parts"], list) and len(removed_entry["parts"]) > 0 and removed_entry["parts"][0].get("text"): current_total_chars -= len(removed_entry["parts"][0]["text"])
        removed_count += 1
    if removed_count > 0: logger.info(f"ChatID: {chat_id} | История обрезана ({removed_count} сообщ.). Текущая: {len(chat_history)} сообщ., ~{current_total_chars} симв.")
    history_for_model = []
    for entry in chat_history[:-1]: model_entry = {"role": entry["role"], "parts": entry["parts"]}; history_for_model.append(model_entry)
    history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt}]})
    # --- Конец подготовки истории ---

    # --- Вызов модели с РЕТРАЯМИ ---
    reply = None; response = None; generation_successful = False
    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"ChatID: {chat_id} | Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к {model_id}...")
            generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
            response = await asyncio.to_thread(model.generate_content, history_for_model)
            if hasattr(response, 'text'):
                reply = response.text
            else:
                reply = None
            if not reply: reply = "🤖 Модель дала пустой ответ."; generation_successful = True; logger.warning(f"ChatID: {chat_id} | Пустой ответ ({attempt+1}).")
            if reply and reply != "🤖 Модель дала пустой ответ.": generation_successful = True
            if generation_successful: logger.info(f"ChatID: {chat_id} | Успех ({attempt + 1})."); break
        except (BlockedPromptException, StopCandidateException) as e_block_stop: reply = f"❌ Запрос заблокирован/остановлен."; logger.warning(f"ChatID: {chat_id} | Блок/стоп ({attempt + 1}): {e_block_stop}"); break
        except Exception as e:
             error_message=str(e); logger.warning(f"ChatID: {chat_id} | Ошибка ({attempt + 1}): {error_message[:100]}...")
             is_retryable = "500" in error_message or "503" in error_message
             if not is_retryable and ("400" in error_message or "429" in error_message): reply = f"❌ Ошибка запроса ({error_message[:100]}...)."; break
             if is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"ChatID: {chat_id} | Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
             # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИСПРАВЛЕНИЕ 1 <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
             else:
                 logger.error(f"ChatID: {chat_id} | Не удалось после {attempt + 1} попыток. Ошибка: {e}", exc_info=True)
                 if reply is None:
                     reply = f"❌ Ошибка после {attempt + 1} попыток."
                 break
             # ==========================================================================

    # --- Добавление ответа в историю и Отправка пользователю ---
    if reply:
        if chat_history and chat_history[-1].get("role") == "user": chat_history.append({"role": "model", "parts": [{"text": reply}]})
        else: logger.warning(f"ChatID: {chat_id} | Ответ добавлен, но посл. сообщение не user."); chat_history.append({"role": "model", "parts": [{"text": reply}]})
    if reply:
        if message: await send_reply(message, reply, context)
        else: logger.error(f"ChatID: {chat_id} | Нет сообщения для ответа."); try: await context.bot.send_message(chat_id=chat_id, text=reply) except Exception as e_send_direct: logger.error(f"ChatID: {chat_id} | Не удалось отправить напрямую: {e_send_direct}")
    else:
         logger.error(f"ChatID: {chat_id} | Нет ответа для отправки."); try: await message.reply_text("🤖 Не удалось получить ответ.") except Exception as e_final_fail: logger.error(f"ChatID: {chat_id} | Не удалось отправить финальную ошибку: {e_final_fail}")
# =============================================================

# ===== Обработчик фото (handle_photo) =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; message = update.message
    if not message or not message.photo: logger.warning(f"ChatID: {chat_id} | Нет фото."); return
    photo_file_id = message.photo[-1].file_id; logger.debug(f"ChatID: {chat_id} | photo file_id: ...{photo_file_id[-10:]}")
    tesseract_available = False; try: pytesseract.pytesseract.get_tesseract_version(); tesseract_available = True except Exception: pass
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try: photo_file = await message.photo[-1].get_file(); file_bytes = await photo_file.download_as_bytearray()
    except Exception as e: logger.error(f"ChatID: {chat_id} | Ошибка скачивания фото ({photo_file_id}): {e}", exc_info=True); await message.reply_text("❌ Не удалось загрузить."); return
    user_caption = message.caption if message.caption else ""
    ocr_triggered = False
    if tesseract_available: # --- OCR ---
        try:
            image = Image.open(io.BytesIO(file_bytes)); extracted_text = pytesseract.image_to_string(image, lang='rus+eng', timeout=15)
            if extracted_text and extracted_text.strip():
                ocr_triggered = True; logger.info(f"ChatID: {chat_id} | OCR нашел текст.")
                ocr_context = f"Текст на фото:\n```\n{extracted_text.strip()}\n```"; user_prompt = f"Фото с подписью: \"{user_caption}\". {ocr_context}\nЧто скажешь?" if user_caption else f"Фото. {ocr_context}\nЧто скажешь?"
                if hasattr(message, 'reply_text') and callable(message.reply_text):
                     fake_message_obj = type('obj', (object,), {'text': user_prompt, 'reply_text': message.reply_text, 'chat_id': chat_id, 'image_file_id': photo_file_id })
                     fake_update = type('obj', (object,), {'effective_chat': update.effective_chat, 'message': fake_message_obj }); await handle_message(fake_update, context); return
                else: logger.error(f"ChatID: {chat_id} | Ошибка передачи reply_text (OCR)."); await message.reply_text("❌ Ошибка обработки OCR."); return
            else: logger.info(f"ChatID: {chat_id} | OCR не нашел текст.")
        except pytesseract.TesseractNotFoundError: logger.error("Tesseract не найден!"); tesseract_available = False
        except RuntimeError as timeout_error: logger.warning(f"ChatID: {chat_id} | OCR таймаут: {timeout_error}"); await message.reply_text("⏳ OCR таймаут. Анализирую как фото...")
        except Exception as e: logger.warning(f"ChatID: {chat_id} | Ошибка OCR: {e}", exc_info=True); await message.reply_text("⚠️ Ошибка OCR. Анализирую как фото...")
    if not ocr_triggered: # --- Обработка как изображение ---
        logger.info(f"ChatID: {chat_id} | Обработка как фото (Vision).")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        MAX_IMAGE_BYTES = 4 * 1024 * 1024;
        if len(file_bytes) > MAX_IMAGE_BYTES: logger.warning(f"ChatID: {chat_id} | Изображение > {MAX_IMAGE_BYTES // (1024*1024)} MB.")
        try: b64_data = base64.b64encode(file_bytes).decode()
        except Exception as e: logger.error(f"ChatID: {chat_id} | Ошибка Base64: {e}", exc_info=True); await message.reply_text("❌ Ошибка обработки img."); return
        prompt_text = f"Фото с подписью: \"{user_caption}\". Опиши." if user_caption else "Фото без подписи. Опиши."
        parts = [{"text": prompt_text}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}}]; content_for_vision = [{"role": "user", "parts": parts}]
        model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL); temperature = get_user_setting(context, 'temperature', 1.0)
        is_vision_model = any(keyword in model_id for keyword in ['flash', 'pro', 'vision', 'ultra'])
        if not is_vision_model: # Поиск fallback...
             vision_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in ['flash', 'pro', 'vision', 'ultra'])];
             if vision_models: fallback_model_id = next((m for m in vision_models if 'flash' in m or 'pro' in m), vision_models[0]); model_id = fallback_model_id; logger.warning(f"ChatID: {chat_id} | Использую {model_id} вместо не-vision.")
             else: logger.error(f"ChatID: {chat_id} | Нет vision моделей."); await message.reply_text("❌ Нет моделей для фото."); return
        logger.info(f"ChatID: {chat_id} | Vision анализ: {model_id}, t: {temperature}"); reply = None; response_vision = None
        for attempt in range(RETRY_ATTEMPTS): # --- Вызов Vision с ретраями ---
            try:
                logger.info(f"ChatID: {chat_id} | Vision Попытка {attempt + 1}/{RETRY_ATTEMPTS}...");
                generation_config=genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS); model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction_text)
                response_vision = await asyncio.to_thread(model.generate_content, content_for_vision)
                if hasattr(response_vision, 'text'): reply = response_vision.text; else: reply = None
                if not reply: reply = "🤖 Не удалось описать (пустой ответ)."; logger.warning(f"ChatID: {chat_id} | Vision пустой ответ ({attempt+1})."); break
                if reply and "Не удалось описать" not in reply: logger.info(f"ChatID: {chat_id} | Vision Успех ({attempt + 1})."); break
            except (BlockedPromptException, StopCandidateException) as e_block_stop: reply = f"❌ Vision заблокирован/остановлен."; logger.warning(f"ChatID: {chat_id} | Vision Блок/стоп ({attempt + 1}): {e_block_stop}"); break
            except Exception as e:
                 error_message=str(e); logger.warning(f"ChatID: {chat_id} | Vision Ошибка ({attempt + 1}): {error_message[:100]}...")
                 is_retryable = "500" in error_message or "503" in error_message
                 if not is_retryable and ("400" in error_message or "429" in error_message): reply = f"❌ Ошибка Vision ({error_message[:100]}...)."; break
                 if is_retryable and attempt < RETRY_ATTEMPTS - 1: wait_time = RETRY_DELAY_SECONDS * (2 ** attempt); logger.info(f"ChatID: {chat_id} | Vision Ожидание {wait_time:.1f} сек..."); await asyncio.sleep(wait_time); continue
                 # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<< ИСПРАВЛЕНИЕ 2 <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
                 else:
                     logger.error(f"ChatID: {chat_id} | Vision Не удалось после {attempt + 1} попыток. Ошибка: {e}", exc_info=True)
                     if reply is None:
                         reply = f"❌ Ошибка Vision после {attempt + 1} попыток."
                     break
                 # ==========================================================================
        # --- Сохранение в историю и отправка ---
        chat_history = context.chat_data.setdefault("history", [])
        user_text_for_history = user_caption if user_caption else "Фото."; history_entry_user = {"role": "user", "parts": [{"text": user_text_for_history}], "image_file_id": photo_file_id}; chat_history.append(history_entry_user); logger.debug(f"ChatID: {chat_id} | Добавлено user (Vision) с image_file_id.")
        model_reply_text = f"{IMAGE_DESCRIPTION_PREFIX}{reply}" if (reply and "❌" not in reply and "🤖" not in reply) else (reply if reply else "🤖 Не удалось описать.")
        chat_history.append({"role": "model", "parts": [{"text": model_reply_text}]}); logger.debug(f"ChatID: {chat_id} | Добавлен model (Vision).")
        reply_to_send = reply if (reply and "❌" not in reply and "🤖" not in reply) else model_reply_text
        if reply_to_send: await send_reply(message, reply_to_send, context)
        else: logger.error(f"ChatID: {chat_id} | Vision нет ответа для отправки."); try: await message.reply_text("🤖 Не удалось описать.") except Exception as e_final_fail: logger.error(f"ChatID: {chat_id} | Vision не удалось отправить ошибку: {e_final_fail}")
# =================================================================

# ===== Обработчик документов (handle_document) =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id;
    if not update.message or not update.message.document: logger.warning(f"ChatID: {chat_id} | Нет документа."); return
    doc = update.message.document;
    mime_type = doc.mime_type or "application/octet-stream"; allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql'); allowed_mime_types = ('application/octet-stream',)
    if not (any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes) or mime_type in allowed_mime_types): await update.message.reply_text(f"⚠️ Неподдерживаемый тип файла: `{mime_type}`", parse_mode=ParseMode.MARKDOWN); logger.warning(f"ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name}"); return
    MAX_FILE_SIZE_MB = 15; file_size_bytes = doc.file_size or 0
    if file_size_bytes == 0: logger.info(f"ChatID: {chat_id} | Пустой файл '{doc.file_name}'."); await update.message.reply_text(f"ℹ️ Файл '{doc.file_name}' пустой."); return
    if file_size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024: await update.message.reply_text(f"❌ Файл `{doc.file_name}` > {MAX_FILE_SIZE_MB} MB.", parse_mode=ParseMode.MARKDOWN); logger.warning(f"ChatID: {chat_id} | Слишком большой файл: {doc.file_name}"); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try: doc_file = await doc.get_file(); file_bytes = await doc_file.download_as_bytearray();
    except Exception as e: logger.error(f"ChatID: {chat_id} | Ошибка скачивания документа '{doc.file_name}': {e}", exc_info=True); await update.message.reply_text("❌ Не удалось загрузить файл."); return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None; detected_encoding = None; encodings_to_try = ['utf-8-sig', 'utf-8', 'cp1251', 'latin-1', 'cp866', 'iso-8859-5']
    chardet_available = False; try: import chardet; chardet_available = True except ImportError: logger.info("chardet не найден.")
    if chardet_available:
        try:
            chardet_limit = min(len(file_bytes), 50 * 1024)
            if chardet_limit > 0:
                 detected = chardet.detect(file_bytes[:chardet_limit])
                 if detected and detected['encoding'] and detected['confidence'] > 0.7:
                      potential_encoding = detected['encoding'].lower(); logger.info(f"ChatID: {chat_id} | Chardet: {potential_encoding} ({detected['confidence']:.2f}) для '{doc.file_name}'")
                      if potential_encoding == 'utf-8' and file_bytes.startswith(b'\xef\xbb\xbf'):
                           logger.info(f"ChatID: {chat_id} | UTF-8 BOM -> 'utf-8-sig'."); detected_encoding = 'utf-8-sig';
                           if 'utf-8-sig' in encodings_to_try: encodings_to_try.remove('utf-8-sig'); encodings_to_try.insert(0, 'utf-8-sig')
                      else:
                           detected_encoding = potential_encoding;
                           if detected_encoding in encodings_to_try: encodings_to_try.remove(detected_encoding); encodings_to_try.insert(0, detected_encoding)
                 else: logger.info(f"ChatID: {chat_id} | Chardet не уверен для '{doc.file_name}'.")
        except Exception as e_chardet: logger.warning(f"Ошибка chardet для '{doc.file_name}': {e_chardet}")
    unique_encodings = list(dict.fromkeys(encodings_to_try))
    for encoding in unique_encodings: try: text = file_bytes.decode(encoding); detected_encoding = encoding; logger.info(f"ChatID: {chat_id} | Файл '{doc.file_name}' декодирован как {encoding}."); break except Exception: pass
    if text is None: await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN); return
    if not text.strip() and len(file_bytes) > 0: await update.message.reply_text(f"⚠️ Не удалось извлечь текст из `{doc.file_name}`.", parse_mode=ParseMode.MARKDOWN); return
    MAX_FILE_CHARS = min(MAX_CONTEXT_CHARS // 2, MAX_OUTPUT_TOKENS * 8); truncated = text; warning_msg = "";
    if len(text) > MAX_FILE_CHARS: truncated = text[:MAX_FILE_CHARS]; warning_msg = f"\n\n**(⚠️ Текст файла обрезан)**"; logger.warning(f"ChatID: {chat_id} | Текст '{doc.file_name}' обрезан.")
    user_caption = update.message.caption or ""; file_name = doc.file_name or "файл"; encoding_info = f"(~{detected_encoding})" if detected_encoding else ""
    file_context = f"Содержимое `{file_name}` {encoding_info}:\n```\n{truncated}\n```{warning_msg}"
    user_prompt = f"Файл `{file_name}` с комм: \"{user_caption}\". {file_context}\nПроанализируй." if user_caption else f"Файл `{file_name}`. {file_context}\nЧто скажешь?"
    if hasattr(update.message, 'reply_text') and callable(update.message.reply_text):
        fake_message = type('obj', (object,), {'text': user_prompt, 'reply_text': update.message.reply_text, 'chat_id': chat_id})
        fake_update = type('obj', (object,), {'effective_chat': update.effective_chat, 'message': fake_message}); await handle_message(fake_update, context)
    else: logger.error(f"ChatID: {chat_id} | Ошибка reply_text (документ)."); await update.message.reply_text("❌ Ошибка обработки файла.")
# =========================================================

# --- Функции веб-сервера и запуска ---
async def setup_bot_and_server(stop_event: asyncio.Event):
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build(); timeout = aiohttp.ClientTimeout(total=60.0); aiohttp_session = aiohttp.ClientSession(timeout=timeout); application.bot_data['aiohttp_session'] = aiohttp_session; logger.info("Сессия aiohttp создана.")
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("model", model_command)); application.add_handler(CommandHandler("clear", clear_history)); application.add_handler(CommandHandler("temp", set_temperature)); application.add_handler(CommandHandler("search_on", enable_search)); application.add_handler(CommandHandler("search_off", disable_search)); application.add_handler(CallbackQueryHandler(select_model_callback, pattern="^set_model_")); application.add_handler(MessageHandler(filters.PHOTO, handle_photo)); application.add_handler(MessageHandler(filters.Document.ALL, handle_document)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    try: await application.initialize(); webhook_path_segment = GEMINI_WEBHOOK_PATH.strip('/'); webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{webhook_path_segment}"; logger.info(f"Установка вебхука: {webhook_url}"); await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN')); logger.info("Вебхук установлен."); return application, run_web_server(application, stop_event)
    except Exception as e: logger.critical(f"Ошибка инициализации/вебхука: {e}", exc_info=True); if 'aiohttp_session' in application.bot_data and application.bot_data['aiohttp_session'] and not application.bot_data['aiohttp_session'].closed: await application.bot_data['aiohttp_session'].close(); logger.info("Сессия aiohttp закрыта из-за ошибки."); raise

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application(); async def health_check(request):
        try: bot_info = await application.bot.get_me(); return aiohttp.web.Response(text=f"OK: Bot {bot_info.username}.") if bot_info else aiohttp.web.Response(text="Error: Bot info unavailable", status=503)
        except Exception as e: logger.error(f"Health check failed: {e}", exc_info=True); return aiohttp.web.Response(text=f"Error: Health check failed", status=503)
    app.router.add_get('/', health_check); app['bot_app'] = application; webhook_path = GEMINI_WEBHOOK_PATH.strip('/'); webhook_path = '/' + webhook_path if not webhook_path.startswith('/') else webhook_path; app.router.add_post(webhook_path, handle_telegram_webhook); logger.info(f"Вебхук слушает: {webhook_path}")
    runner = aiohttp.web.AppRunner(app); await runner.setup(); port = int(os.getenv("PORT", "10000")); host = os.getenv("HOST", "0.0.0.0"); site = aiohttp.web.TCPSite(runner, host, port)
    try: await site.start(); logger.info(f"Веб-сервер запущен: http://{host}:{port}"); await stop_event.wait()
    except asyncio.CancelledError: logger.info("Задача веб-сервера отменена.")
    except Exception as e: logger.error(f"Ошибка веб-сервера на {host}:{port}: {e}", exc_info=True)
    finally: logger.info("Остановка веб-сервера..."); await runner.cleanup(); logger.info("Веб-сервер остановлен.")

async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app.get('bot_app');
    if not application: logger.critical("Приложение бота не найдено!"); return aiohttp.web.Response(status=500)
    secret_token = os.getenv('WEBHOOK_SECRET_TOKEN');
    if secret_token:
        header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
        if header_token != secret_token: logger.warning("Неверный секретный токен."); return aiohttp.web.Response(status=403)
    try: data = await request.json(); update = Update.de_json(data, application.bot); await application.process_update(update); return aiohttp.web.Response(text="OK", status=200)
    except json.JSONDecodeError as e: body = await request.text(); logger.error(f"Ошибка JSON: {e}. Тело: {body[:500]}..."); return aiohttp.web.Response(text="Bad Request", status=400)
    except Exception as e: logger.error(f"Критическая ошибка вебхука: {e}", exc_info=True); return aiohttp.web.Response(text="Internal Server Error", status=500)

async def main():
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper(); log_level = getattr(logging, log_level_str, logging.INFO); logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO);
    logging.getLogger('httpx').setLevel(logging.WARNING); logging.getLogger('httpcore').setLevel(logging.WARNING); logging.getLogger('google.api_core').setLevel(logging.WARNING); logging.getLogger('google.generativeai').setLevel(logging.INFO); logging.getLogger('duckduckgo_search').setLevel(logging.INFO); logging.getLogger('PIL').setLevel(logging.INFO); logging.getLogger('pytesseract').setLevel(logging.INFO); logging.getLogger('aiohttp.access').setLevel(logging.WARNING); logging.getLogger('telegram.ext').setLevel(logging.INFO); logging.getLogger('telegram.bot').setLevel(logging.INFO); logging.getLogger('youtube_transcript_api').setLevel(logging.INFO); logger.setLevel(log_level); logger.info(f"--- Log Level: {log_level_str} ({log_level}) ---")
    loop = asyncio.get_running_loop(); stop_event = asyncio.Event();
    def signal_handler():
        if not stop_event.is_set(): logger.info("Получен сигнал остановки..."); stop_event.set(); else: logger.warning("Повторный сигнал.")
    for sig in (signal.SIGINT, signal.SIGTERM): try: loop.add_signal_handler(sig, signal_handler) except NotImplementedError: logger.warning(f"Не удалось установить обработчик {sig} через loop."); try: signal.signal(sig, lambda s, f: signal_handler()) except Exception as e_signal: logger.error(f"Не удалось установить обработчик {sig}: {e_signal}")
    application = None; web_server_task = None; aiohttp_session_main = None
    try: logger.info(f"--- Запуск ---"); application, web_server_coro = await setup_bot_and_server(stop_event); web_server_task = asyncio.create_task(web_server_coro); aiohttp_session_main = application.bot_data.get('aiohttp_session'); logger.info("Приложение настроено, веб-сервер запущен..."); await stop_event.wait()
    except asyncio.CancelledError: logger.info("Главная задача отменена.")
    except Exception as e: logger.critical("Критическая ошибка до/во время ожидания.", exc_info=True)
    finally:
        logger.info("--- Начало остановки ---");
        if not stop_event.is_set(): stop_event.set();
        if web_server_task and not web_server_task.done(): # Остановка веб-сервера
             logger.info("Остановка веб-сервера...");
             try: await asyncio.wait_for(web_server_task, timeout=15.0); logger.info("Веб-сервер завершен.")
             except asyncio.TimeoutError: logger.warning("Таймаут остановки веб-сервера, отмена..."); web_server_task.cancel(); try: await web_server_task except asyncio.CancelledError: logger.info("Веб-сервер отменен.") except Exception as e_cancel: logger.error(f"Ошибка отмены веб-сервера: {e_cancel}", exc_info=True)
             except asyncio.CancelledError: logger.info("Ожидание веб-сервера отменено.")
             except Exception as e_wait: logger.error(f"Ошибка ожидания веб-сервера: {e_wait}", exc_info=True)
        if application: # Остановка PTB
            logger.info("Остановка приложения Telegram...");
            try: await application.shutdown(); logger.info("Приложение Telegram остановлено.")
            except Exception as e_shutdown: logger.error(f"Ошибка shutdown(): {e_shutdown}", exc_info=True)
        if aiohttp_session_main and not aiohttp_session_main.closed: # Закрытие сессии
             logger.info("Закрытие сессии aiohttp..."); await aiohttp_session_main.close(); await asyncio.sleep(0.5); logger.info("Сессия aiohttp закрыта.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]; # Отмена задач
        if tasks:
            logger.info(f"Отмена {len(tasks)} задач..."); [task.cancel() for task in tasks]; results = await asyncio.gather(*tasks, return_exceptions=True);
            cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 if isinstance(res, asyncio.CancelledError): cancelled_count += 1
                 elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка в отмененной задаче {tasks[i].get_name()}: {res}", exc_info=isinstance(res, Exception))
            logger.info(f"Задачи завершены (отменено: {cancelled_count}, ошибок: {error_count}).")
        logger.info("--- Приложение остановлено ---")

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Прервано (KeyboardInterrupt).")
    except Exception as e_top: logger.critical("Неперехваченная ошибка.", exc_info=True)
# --- END OF FILE main.py ---