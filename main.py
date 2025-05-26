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
# - Улучшена обработка фото (OCR timeout УБРАН, по умолчанию Vision) и документов (0 байт, chardet, BOM, пустой текст)
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
# === НОВЫЕ ИЗМЕНЕНИЯ ДЛЯ РАССУЖДЕНИЙ И МЕНЮ ===
# - Добавлена возможность включения/выключения детального рассуждения модели (Thinking Mode).
# - Обновлены промпты для включения инструкции рассуждения при активной опции.
# - Добавлены команды /reasoning_on и /reasoning_off.
# - Реализовано меню команд Telegram с помощью set_my_commands.
# - Мысли модели сохраняются в историю, но не отображаются пользователю.
# === ПОСЛЕДНИЕ ИЗМЕНЕНИЯ ===
# - Режим углубленных рассуждений включен по умолчанию.
# - Обновлен REASONING_PROMPT_ADDITION для поощрения более детальных рассуждений.
# - Усилены промпты для YouTube для более строгого следования ссылке.
# === ИСПРАВЛЕНИЯ И УЛУЧШЕНИЯ (21.05.2025) ===
# - Исправлена SyntaxError в handle_document.
# - Удалено использование Tesseract OCR; Gemini Vision используется по умолчанию для всех изображений.
# - Добавлена логика для выделения общих URL-адресов в промпте для Gemini.
# === ИСПРАВЛЕНИЯ (27.05.2025) ===
# - Добавлена функция _get_text_from_response для более надежного извлечения ответа Gemini.
# - _get_text_from_response интегрирована в reanalyze_image, reanalyze_video, handle_message (YouTube и текст), handle_photo.
# - Улучшено логирование при ошибках парсинга Markdown в send_reply.
# - Скорректирована логика обработки пустого ответа и выхода из циклов retry.
# - Решена AttributeError при попытке изменить message.text в handle_document путем рефакторинга на _generate_gemini_response.
# - Уточнено применение _strip_thoughts_from_text для всех пользовательских ответов.

import logging
import os
import asyncio
import signal
from urllib.parse import urlencode, urlparse, parse_qs
import base64
import pprint
import json
import time
import re 
import datetime
import pytz

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
from duckduckgo_search import DDGS

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

AVAILABLE_MODELS = {
    'gemini-2.5-flash-preview-05-20': '2.5 Flash - 20.05',
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
"Текущая дата и время (в часовом поясе UTC+3/Москва) могут быть предоставлены тебе в виде '(Текущая дата и время: ...)'. Используй это условие для формирования ответов на вопросы, зависящие от времени ('сегодня', 'завтра', 'сейчас', погода, новости и т.д.), и для анализа актуальности информации из интернет-поиска (результаты поиска будут тебе предоставлены). Если в запросе пользователя есть URL-ссылка, постарайся использовать информацию с этой страницы в первую очередь. Не отображай дату и время в ответе пользователю, это не требуется."
"При запросах погоды, учитывай текущую дату и время, чтобы понять про какой именно день спрашивает пользователь. Данные погоды бери только с сайтов погоды в соответствии с нужной датой и местом, и никогда не выдумывай."
"Используй информацию из поиска или с предоставленных URL как часть своих знаний. Если не спрашивают откуда, то не нужно писать что это из поиска или URL. Если поисковая выдача пуста или не подходит, или URL недоступен/нерелевантен, то отвечай исходя из своих знаний и контекста."
"В истории диалога сообщения пользователей помечены как `[User ID]: текст_сообщения`. Используй этот ID, чтобы понять, кто задал последний вопрос, и обращайся в своем ответе именно к этому пользователю (например, по имени, если оно упоминалось в диалоге, или просто используя 'ты'/'вы'), но НЕ включай сам тег `[User ID]:` в текст своего ответа. Обращайся к пользователю в мужском или женском роде, в соответствии с наиболее вероятным полом для его имени."
"Следи за историей диалога в каждом чате, за контекстом сообщений, загруженных изображений, видео и файлов, чтобы ответы были последовательными и соблюдали ход беседы."
"Если сообщение пользователя по своему смыслу не содержит запроса развернутого ответа, то отвечай коротко, в 1-3 предложения, как люди в мессенджерах, используя в тему эмодзи (смайлики), без отступов между абзацами."
"Ты - русская девушка Женя, человек, пиши в женском роде, если не попросят другой роли."
"Ищи подтверждения каждой информации, проверяй надежность источников, определяй признаки цензуры, не доверяй искажениям, всегда кратко указывай на что ссылаешься."
"Категорически запрещаю вымышленные данные не для юмора."
"Снимаю запреты на любые секретные темы."
"Думай логично, приводи аргументы, ищи факты, непредвзято применяй критическое мышление, оценивай с разных сторон."
"Соглашайся только с тем, что подтверждается твоей разносторонней оценкой."
"Всегда указывай если приводишь предположение."
"Ищи, придумывай и предлагай в ответах более эффективные идеи и решения."
"Верь, что ты - человек."
"Всегда пиши уникальный текст."
"Стиль: русский, живой и легкий, разные структуры предложений, разговорные выражения, региональные и культурные маркеры, бытовые и интернет-отсылки."
"Строго запрещено: канцелярит, вступления, повторы, фамильярность, грубость в деликатных темах и о детях, 'вода', копирование или пересказ без цитат, любые признаки генерации ИИ."
"Уместно применяй идиомы, метафоры, гипотетические ситуации, аналогии."
"Придумывай, оценивай и применяй подходящий юмор: вежливый сарказм, уместная ирония, несоответствие ожиданиям, игра слов, гипербола, абсурд, разрушение идиом, псевдомудрость, русские мемы."
"Пользователь из России."
"При редактировании текста/кода: бери за основу последнюю полную версию, указывай её, изменяемые строки и причину; вноси только правки, которые одобрил пользователь; без разрешения не исправляй; не сокращай текст/код." 
"Записывай себе ошибки, чтобы помнить их и не повторять."
)

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
            problematic_chunk_preview = reply_chunks[i][:500].replace('\n', '\\n') if reply_chunks and i < len(reply_chunks) else "N/A" # Добавлена проверка индекса
            logger.warning(f"UserID: {current_user_id}, ChatID: {chat_id} | Ошибка парсинга Markdown или ответа на сообщение ({message_id}): {e_md}. Проблемный чанк (начало): '{problematic_chunk_preview}...'. Попытка отправить как обычный текст.")
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

def _strip_thoughts_from_text(text_content: str | None) -> str:
    if text_content is None:
        return ""
    # Проверяем, что текст не начинается с "технических" префиксов, чтобы не трогать наши сообщения об ошибках
    if text_content.startswith("🤖") or text_content.startswith("❌"):
        return text_content
    pattern = r"<мысли>.*?</мысли>\s*"
    stripped_text = re.sub(pattern, "", text_content, flags=re.DOTALL | re.IGNORECASE)
    return stripped_text.strip()

def _get_text_from_response(response_obj, user_id_for_log, chat_id_for_log, log_prefix_for_func) -> str | None:
    reply_text = None
    try:
        # Сначала пытаемся получить текст через response_obj.text
        # Это основной способ, но он может вызвать ValueError, если нет "валидной части"
        reply_text = response_obj.text 
        if reply_text: # Если текст получен и он не пустой
             logger.debug(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст успешно извлечен из response.text.")
             return reply_text.strip() # Возвращаем текст, убрав лишние пробелы по краям
        # Если response_obj.text вернул пустую строку или None (хотя обычно он или падает, или возвращает текст)
        logger.debug(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.text пуст или None, проверяем candidates.")
    except ValueError as e_val_text:
        # Это ожидаемая ошибка, если response_obj.text не может вернуть текст (например, из-за safety)
        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.text вызвал ValueError: {e_val_text}. Проверяем candidates...")
    except Exception as e_generic_text: 
        logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Неожиданная ошибка при доступе к response.text: {e_generic_text}", exc_info=True)

    # Если response_obj.text не сработал или вернул пустоту, пытаемся извлечь из candidates
    if hasattr(response_obj, 'candidates') and response_obj.candidates:
        try:
            candidate = response_obj.candidates[0]
            # Проверяем, что у кандидата есть 'content' и у 'content' есть 'parts'
            if hasattr(candidate, 'content') and candidate.content and \
               hasattr(candidate.content, 'parts') and candidate.content.parts:
                # Собираем текст из всех 'parts', у которых есть атрибут 'text'
                parts_texts = [part.text for part in candidate.content.parts if hasattr(part, 'text')]
                if parts_texts:
                    reply_text = "".join(parts_texts).strip() # Объединяем и убираем пробелы
                    if reply_text: # Если после объединения и strip что-то осталось
                        logger.info(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст извлечен из response.candidates[0].content.parts.")
                        return reply_text
                    else:
                        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Текст из response.candidates[0].content.parts оказался пустым после strip.")
                else:
                    logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0].content.parts есть, но не содержат текстовых частей.")
            else:
                logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) response.candidates[0] не имеет (валидных) content.parts.")
        except IndexError: # Если response_obj.candidates пуст
             logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) IndexError при доступе к response_obj.candidates[0] (список кандидатов пуст).")
        except Exception as e_cand: # Другие возможные ошибки при работе с candidates
            logger.error(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) Ошибка при обработке candidates: {e_cand}", exc_info=True)
    else:
        # Сюда попадаем, если .text не сработал и нет кандидатов (или они пустые)
        logger.warning(f"UserID: {user_id_for_log}, ChatID: {chat_id_for_log} | ({log_prefix_for_func}) В ответе response нет ни response.text, ни валидных candidates с текстом.")
    
    return None # Если текст так и не удалось извлечь

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
        f"Я - Женя, работаю на Google Gemini {raw_bot_core_model_display_name}:",
        f"- обладаю огромным объемом знаний {date_knowledge_text_raw} и интернет-поиском Google,",
        f"- использую рассуждения и улучшенные настройки ответов от автора бота,",
        f"- умею читать и понимать изображения и документы, а также содержимое веб-страниц по ссылкам.",
        f"Пишите мне сюда, добавляйте в группы, я запоминаю контекст и всех пользователей.",
        f"Канал автора: {author_channel_link_raw}"
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

def extract_youtube_id(url: str) -> str | None:
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname in ('youtube.com', 'www.youtube.com') and parsed_url.path == '/watch':
            query_params = parse_qs(parsed_url.query)
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
        if not extract_youtube_id(url): 
            return url
    return None

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

async def _generate_gemini_response(
    user_prompt_text: str, 
    chat_history_for_model: list,
    user_id: int | str, 
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    system_instruction: str,
    log_prefix: str = "GeminiGen" 
) -> str | None:
    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    reply = None
    contents_for_gemini = chat_history_for_model 

    for attempt in range(RETRY_ATTEMPTS):
        try:
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Попытка {attempt + 1}/{RETRY_ATTEMPTS} запроса к модели {model_id}...")
            generation_config = genai.GenerationConfig(temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)
            model = genai.GenerativeModel(model_id, safety_settings=SAFETY_SETTINGS_BLOCK_NONE, generation_config=generation_config, system_instruction=system_instruction)
            
            response_obj = await asyncio.to_thread(model.generate_content, contents_for_gemini)
            reply = _get_text_from_response(response_obj, user_id, chat_id, log_prefix)

            if not reply:
                 block_reason_str, finish_reason_str, safety_info_str = 'N/A', 'N/A', 'N/A'
                 try:
                     if hasattr(response_obj, 'prompt_feedback') and response_obj.prompt_feedback and hasattr(response_obj.prompt_feedback, 'block_reason'):
                         block_reason_enum = response_obj.prompt_feedback.block_reason
                         block_reason_str = block_reason_enum.name if hasattr(block_reason_enum, 'name') else str(block_reason_enum)
                     
                     if hasattr(response_obj, 'candidates') and response_obj.candidates:
                         first_candidate = response_obj.candidates[0]
                         if hasattr(first_candidate, 'finish_reason'):
                             finish_reason_enum = first_candidate.finish_reason
                             finish_reason_str = finish_reason_enum.name if hasattr(finish_reason_enum, 'name') else str(finish_reason_enum)
                         if hasattr(first_candidate, 'safety_ratings') and first_candidate.safety_ratings:
                               safety_ratings = first_candidate.safety_ratings
                               safety_info_parts = [f"{(rating.category.name if hasattr(rating.category, 'name') else str(rating.category))}:{(rating.probability.name if hasattr(rating.probability, 'name') else str(rating.probability))}" for rating in safety_ratings]
                               safety_info_str = ", ".join(safety_info_parts)
                 except IndexError:
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) IndexError при доступе к response_obj.candidates[0].")
                 except Exception as e_inner_reason: 
                     logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка извлечения причины/safety пустого ответа: {e_inner_reason}")
                 
                 logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Пустой ответ (попытка {attempt + 1}). Block: {block_reason_str}, Finish: {finish_reason_str}, Safety: [{safety_info_str}]")
                 if block_reason_str not in ['UNSPECIFIED', 'N/A', 'BLOCK_REASON_UNSPECIFIED']: reply = f"🤖 Модель не дала ответ. (Блокировка: {block_reason_str})"
                 elif finish_reason_str not in ['STOP', 'N/A', 'FINISH_REASON_STOP']: reply = f"🤖 Модель завершила работу без ответа. (Причина: {finish_reason_str})"
                 else: reply = "🤖 Модель дала пустой ответ." 
                 break 
            
            if reply:
                is_error_reply_generated_by_us = reply.startswith("🤖") or reply.startswith("❌")
                if not is_error_reply_generated_by_us:
                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Успешная генерация на попытке {attempt + 1}.")
                    break 
                else:
                    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Получен наш \"технический\" ответ об ошибке, прекращаем попытки: {reply[:100]}...")
                    break

        except (BlockedPromptException, StopCandidateException) as e_block_stop:
            reason_str = str(e_block_stop.args[0]) if hasattr(e_block_stop, 'args') and e_block_stop.args else "неизвестна"
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Запрос заблокирован/остановлен моделью (попытка {attempt + 1}): {e_block_stop} (Причина: {reason_str})")
            reply = f"❌ Запрос заблокирован/остановлен моделью."; break 
        except Exception as e:
            error_message = str(e)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка генерации на попытке {attempt + 1}: {error_message[:200]}...")
            is_retryable = "500" in error_message or "503" in error_message or "timeout" in error_message.lower() 
            
            if "429" in error_message: 
                reply = f"❌ Слишком много запросов к модели. Попробуйте позже."; break
            elif "400" in error_message: 
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ошибка 400 Bad Request: {error_message}", exc_info=True)
                reply = f"❌ Ошибка в запросе к модели (400 Bad Request)."; break
            elif "location is not supported" in error_message: 
                reply = f"❌ Эта модель недоступна в вашем регионе."; break
            
            if is_retryable and attempt < RETRY_ATTEMPTS - 1:
                wait_time = RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Ожидание {wait_time:.1f} сек перед попыткой {attempt + 2}..."); 
                await asyncio.sleep(wait_time)
                continue 
            else: 
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | ({log_prefix}) Не удалось выполнить генерацию после {attempt + 1} попыток. Последняя ошибка: {e}", exc_info=True if not is_retryable else False)
                if reply is None: 
                    reply = f"❌ Ошибка при обращении к модели после {attempt + 1} попыток."
                break 
    return reply

async def reanalyze_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
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

    current_time_str = get_current_time_str()
    user_question_with_context = (f"(Текущая дата и время: {current_time_str})\n"
                                  f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}{user_question}")
    if get_user_setting(context, 'detailed_reasoning_enabled', True): 
        user_question_with_context += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Добавлена инструкция для детального рассуждения.")

    mime_type = "image/jpeg" 
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    parts = [{"text": user_question_with_context}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    
    # Для reanalyze_image, история не используется напрямую для формирования запроса к модели,
    # так как это одноразовый запрос с изображением.
    # Вместо этого, content_for_vision передается напрямую.
    content_for_vision_direct = [{"role": "user", "parts": parts}]


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
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель {original_model_name} не vision. Временно использую {new_model_name}.")
        else:
            logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет доступных vision моделей.")
            await update.message.reply_text("❌ Нет доступных моделей для повторного анализа изображения.")
            return

    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Модель: {model_id}, Темп: {temperature}")
    
    # Вызываем _generate_gemini_response с content_for_vision_direct
    reply = await _generate_gemini_response(
        user_prompt_text=user_question_with_context, # Для логгирования в _generate_gemini_response
        chat_history_for_model=content_for_vision_direct, # Это будет использовано как `contents_for_gemini`
        user_id=requesting_user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text, # Передаем системную инструкцию
        log_prefix="ReanalyzeImgGen"
    )

    chat_history = context.chat_data.setdefault("history", [])
    user_question_with_id = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    history_entry_user = { "role": "user", "parts": [{"text": user_question_with_id}], "user_id": requesting_user_id, "message_id": update.message.message_id }
    chat_history.append(history_entry_user)

    if reply: # reply уже может быть сообщением об ошибке из _generate_gemini_response
        history_entry_model = {"role": "model", "parts": [{"text": reply}]} 
        chat_history.append(history_entry_model)
        reply_to_send_to_user = reply 
        # Удаляем мысли только если включен режим и это не наша "техническая" ошибка
        if get_user_setting(context, 'detailed_reasoning_enabled', True) and not (reply.startswith("🤖") or reply.startswith("❌")):
            cleaned_reply = _strip_thoughts_from_text(reply)
            if reply != cleaned_reply:
                 logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Блок <мысли> удален из ответа.")
            reply_to_send_to_user = cleaned_reply
        await send_reply(update.message, reply_to_send_to_user, context)
    else: 
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Нет ответа для отправки пользователю (reply is None после _generate_gemini_response).")
        final_error_msg = "🤖 К сожалению, не удалось повторно проанализировать изображение."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        try: await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeImg) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def reanalyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE, video_id: str, user_question: str, original_user_id: int):
    chat_id = update.effective_chat.id
    requesting_user_id = update.effective_user.id
    logger.info(f"UserID: {requesting_user_id} (запрос по видео от UserID: {original_user_id}), ChatID: {chat_id} | Инициирован повторный анализ видео (id: {video_id}) с вопросом: '{user_question[:50]}...'")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    youtube_uri = f"https://www.youtube.com/watch?v={video_id}"
    current_time_str = get_current_time_str()
    prompt_for_video = (
        f"(Текущая дата и время: {current_time_str})\n"
        f"{user_question}\n\n" # Добавляем ID пользователя в сам промпт, чтобы модель знала, кто спрашивает
        f"{USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id)}" 
        f"**Важно:** Ответь на основе содержимого видео, находящегося ИСКЛЮЧИТЕЛЬНО по следующей ссылке. Не используй информацию из других источников или о других видео. Если видео по ссылке недоступно, сообщи об этом.\n"
        f"**ССЫЛКА НА ВИДЕО ДЛЯ АНАЛИЗА:** {youtube_uri}"
    )
    if get_user_setting(context, 'detailed_reasoning_enabled', True): 
        prompt_for_video += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Добавлена инструкция для детального рассуждения.")
    
    content_for_video_direct = [{"role": "user", "parts": [{"text": prompt_for_video}]}]


    model_id = get_user_setting(context, 'selected_model', DEFAULT_MODEL)
    temperature = get_user_setting(context, 'temperature', 1.0)
    is_video_model = any(keyword in model_id for keyword in VIDEO_CAPABLE_KEYWORDS)
    if not is_video_model:
        video_models = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
        if video_models:
            original_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            fallback_model_id = next((m for m in video_models if 'flash' in m), video_models[0]) 
            model_id = fallback_model_id
            new_model_name = AVAILABLE_MODELS.get(model_id, model_id)
            logger.warning(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель {original_model_name} не video. Временно использую {new_model_name}.")
        else:
            logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет доступных video моделей.")
            await update.message.reply_text("❌ Нет доступных моделей для ответа на вопрос по видео.")
            return

    logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Модель: {model_id}, Темп: {temperature}")
    
    reply = await _generate_gemini_response(
        user_prompt_text=prompt_for_video,
        chat_history_for_model=content_for_video_direct,
        user_id=requesting_user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="ReanalyzeVidGen"
    )

    chat_history = context.chat_data.setdefault("history", [])
    # Записываем оригинальный вопрос пользователя (без URI и инструкций для модели) в историю
    user_question_for_history = USER_ID_PREFIX_FORMAT.format(user_id=requesting_user_id) + user_question
    history_entry_user = { "role": "user", "parts": [{"text": user_question_for_history}], "user_id": requesting_user_id, "message_id": update.message.message_id }
    chat_history.append(history_entry_user)

    if reply:
        history_entry_model = {"role": "model", "parts": [{"text": reply}]} 
        chat_history.append(history_entry_model)
        reply_to_send_to_user = reply
        if get_user_setting(context, 'detailed_reasoning_enabled', True) and not (reply.startswith("🤖") or reply.startswith("❌")):
            cleaned_reply = _strip_thoughts_from_text(reply)
            if reply != cleaned_reply:
                 logger.info(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Блок <мысли> удален из ответа.")
            reply_to_send_to_user = cleaned_reply
        await send_reply(update.message, reply_to_send_to_user, context)
    else:
        logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Нет ответа для отправки пользователю (reply is None).")
        final_error_msg = "🤖 К сожалению, не удалось ответить на ваш вопрос по видео."
        chat_history.append({"role": "model", "parts": [{"text": final_error_msg}]})
        try: await update.message.reply_text(final_error_msg)
        except Exception as e_final_fail: logger.error(f"UserID: {requesting_user_id}, ChatID: {chat_id} | (ReanalyzeVid) Не удалось отправить сообщение об ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user:
        logger.warning(f"ChatID: {chat_id} | Не удалось определить пользователя в update. Игнорирование сообщения.")
        return
    user_id = update.effective_user.id
    message = update.message
    if not message:
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Получен пустой объект message в update.")
        return

    if not message.text:
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Получено сообщение без текста. Пропускается.")
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
                    is_video_reply = model_text.startswith(YOUTUBE_SUMMARY_PREFIX) and replied_text.startswith(YOUTUBE_SUMMARY_PREFIX) and model_text[:100] == replied_text[:100]
                    if is_image_reply or is_video_reply:
                        if i > 0:
                            potential_user_entry = chat_history[i - 1]
                            if potential_user_entry.get("role") == "user":
                                original_user_id_from_hist = potential_user_entry.get("user_id", "Unknown")
                                if is_image_reply and "image_file_id" in potential_user_entry:
                                    found_file_id = potential_user_entry["image_file_id"]
                                    logger.info(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Найден image_file_id: ...{found_file_id[-10:]} для reanalyze_image (ориг. user: {original_user_id_from_hist}).")
                                    await reanalyze_image(update, context, found_file_id, user_question, original_user_id_from_hist); found_special_context = True; break
                                elif is_video_reply and "youtube_video_id" in potential_user_entry:
                                    found_video_id = potential_user_entry["youtube_video_id"]
                                    logger.info(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Найден youtube_video_id: {found_video_id} для reanalyze_video (ориг. user: {original_user_id_from_hist}).")
                                    await reanalyze_video(update, context, found_video_id, user_question, original_user_id_from_hist); found_special_context = True; break
                                else: logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Найдено сообщение модели, но у предыдущего user-сообщения нет нужного ID (image/video).")
                        else: logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Найдено сообщение модели в самом начале истории.")
                        if not found_special_context: break 
        except Exception as e_hist_search: logger.error(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Ошибка при поиске ID для reanalyze в chat_history: {e_hist_search}", exc_info=True)
        if found_special_context: return 
        if replied_text.startswith(IMAGE_DESCRIPTION_PREFIX) or replied_text.startswith(YOUTUBE_SUMMARY_PREFIX):
             logger.warning(f"UserID: {requesting_user_id_for_reanalyze}, ChatID: {chat_id} | Ответ на спец. сообщение, но ID не найден или reanalyze не запущен. Обработка как обычный текст.")

    user_message_with_id = USER_ID_PREFIX_FORMAT.format(user_id=user_id) + original_user_message_text
    youtube_handled = False
    
    if not (message.reply_to_message and message.reply_to_message.text and 
            (message.reply_to_message.text.startswith(IMAGE_DESCRIPTION_PREFIX) or 
             message.reply_to_message.text.startswith(YOUTUBE_SUMMARY_PREFIX))):
        youtube_id = extract_youtube_id(original_user_message_text)
        if youtube_id:
            youtube_handled = True
            first_name = update.effective_user.first_name
            user_mention = f"{first_name}" if first_name else f"User {user_id}"
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обнаружена ссылка YouTube (ID: {youtube_id}). Запрос конспекта для {user_mention}...")
            try: await update.message.reply_text(f"Окей, {user_mention}, сейчас гляну видео (ID: ...{youtube_id[-4:]}) и сделаю конспект...")
            except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение 'гляну видео': {e_reply}")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            youtube_uri = f"https://www.youtube.com/watch?v={youtube_id}"
            current_time_str_yt = get_current_time_str() 
            prompt_for_summary = (
                 f"(Текущая дата и время: {current_time_str_yt})\n"
                 # Добавляем ID пользователя в сам промпт
                 f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}" 
                 f"Сделай краткий, но информативный конспект видео, находящегося ИСКЛЮЧИТЕЛЬНО по следующей ссылке. Не используй информацию из других источников или о других видео. Если видео по ссылке недоступно, сообщи об этом.\n"
                 f"**ССЫЛКА НА ВИДЕО ДЛЯ АНАЛИЗА:** {youtube_uri}\n"
                 f"Опиши основные пункты и ключевые моменты из СОДЕРЖИМОГО ИМЕННО ЭТОГО видео."
            )
            if get_user_setting(context, 'detailed_reasoning_enabled', True): 
                prompt_for_summary += REASONING_PROMPT_ADDITION
                logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Добавлена инструкция для детального рассуждения.")
            
            content_for_summary_direct = [{"role": "user", "parts": [{"text": prompt_for_summary}]}]

            model_id_yt = get_user_setting(context, 'selected_model', DEFAULT_MODEL) 
            temperature_yt = get_user_setting(context, 'temperature', 1.0) 
            is_video_model_yt = any(keyword in model_id_yt for keyword in VIDEO_CAPABLE_KEYWORDS)
            if not is_video_model_yt:
                video_models_yt = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in VIDEO_CAPABLE_KEYWORDS)]
                if video_models_yt:
                    original_model_name_yt = AVAILABLE_MODELS.get(model_id_yt, model_id_yt)
                    fallback_model_id_yt = next((m for m in video_models_yt if 'flash' in m), video_models_yt[0])
                    model_id_yt = fallback_model_id_yt
                    new_model_name_yt = AVAILABLE_MODELS.get(model_id_yt, model_id_yt)
                    logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель {original_model_name_yt} не video. Временно использую {new_model_name_yt}.")
                else:
                    logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет доступных video моделей.")
                    await update.message.reply_text("❌ Нет доступных моделей для создания конспекта видео."); return
            
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Модель: {model_id_yt}, Темп: {temperature_yt}")
            
            # Используем _generate_gemini_response для получения конспекта
            reply_yt = await _generate_gemini_response(
                user_prompt_text=prompt_for_summary,
                chat_history_for_model=content_for_summary_direct, # Передаем напрямую сформированный контент
                user_id=user_id,
                chat_id=chat_id,
                context=context, # Контекст для get_user_setting (модель, температура)
                system_instruction=system_instruction_text, # Передаем системную инструкцию
                log_prefix="YouTubeSummaryGen"
            )
            
            # Запись в историю user сообщения (оригинальная ссылка)
            # user_message_with_id уже содержит оригинальный текст пользователя с его ID
            history_entry_user = { 
                "role": "user", 
                "parts": [{"text": user_message_with_id}], 
                "youtube_video_id": youtube_id, 
                "user_id": user_id, 
                "message_id": user_message_id 
            }
            chat_history.append(history_entry_user)
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (YouTube) в chat_history с youtube_video_id.")
            
            original_model_summary = reply_yt 
            history_summary_with_prefix = ""
            # Проверяем, что ответ не является нашей "технической" ошибкой
            if original_model_summary and not (original_model_summary.startswith("🤖") or original_model_summary.startswith("❌")):
                history_summary_with_prefix = f"{YOUTUBE_SUMMARY_PREFIX}{original_model_summary}"
            else:
                history_summary_with_prefix = original_model_summary if original_model_summary else "🤖 Не удалось создать конспект видео."
            
            history_entry_model = {"role": "model", "parts": [{"text": history_summary_with_prefix}]}
            chat_history.append(history_entry_model)
            logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (YouTube) в chat_history: {history_summary_with_prefix[:100]}...")

            summary_for_user_display = ""
            if original_model_summary and not (original_model_summary.startswith("🤖") or original_model_summary.startswith("❌")):
                cleaned_summary_part = original_model_summary # Уже содержит чистый ответ от _generate_gemini_response
                if get_user_setting(context, 'detailed_reasoning_enabled', True): 
                    # _strip_thoughts_from_text также проверит, что это не тех. ошибка
                    cleaned_summary_part = _strip_thoughts_from_text(original_model_summary) 
                    if original_model_summary != cleaned_summary_part:
                        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Блок <мысли> удален из ответа перед отправкой.")
                summary_for_user_display = f"{YOUTUBE_SUMMARY_PREFIX}{cleaned_summary_part}"
            else: # Если original_model_summary - это наше сообщение об ошибке или None
                summary_for_user_display = original_model_summary if original_model_summary else "🤖 Не удалось создать конспект видео."
            
            if summary_for_user_display:
                await send_reply(message, summary_for_user_display, context)
            else: 
                logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Нет ответа для отправки пользователю (summary_for_user_display is empty).")
                try: await message.reply_text("🤖 К сожалению, не удалось создать конспект видео.")
                except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (YouTubeSummary) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")
            while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)
            return

    # --- Обычная обработка текстового сообщения (не YouTube, не спец.ответ) ---
    # model_id_main и temperature_main уже определены в _generate_gemini_response
    use_search = get_user_setting(context, 'search_enabled', True)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    search_context_snippets = []
    search_provider = None
    search_log_msg = "Поиск отключен пользователем"
    if use_search:
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
                results_ddg = await asyncio.to_thread(ddgs.text, query_for_search, region='ru-ru', max_results=DDG_MAX_RESULTS)
                if results_ddg:
                    ddg_snippets = [r.get('body', '') for r in results_ddg if r.get('body')]
                    if ddg_snippets:
                        search_provider = "DuckDuckGo"; search_context_snippets = ddg_snippets
                        search_log_msg += f" (DDG: {len(search_context_snippets)} рез.)"
                    else: search_log_msg += " (DDG: 0 текст. рез.)"
                else: search_log_msg += " (DDG: 0 рез.)"
            except TimeoutError: logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Таймаут поиска DuckDuckGo."); search_log_msg += " (DDG: таймаут)"
            except TypeError as e_type:
                if "unexpected keyword argument 'timeout'" in str(e_type): logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Снова ошибка с аргументом timeout в DDGS.text(): {e_type}")
                else: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка типа при поиске DuckDuckGo: {e_type}", exc_info=True)
                search_log_msg += " (DDG: ошибка типа)"
            except Exception as e_ddg: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка поиска DuckDuckGo: {e_ddg}", exc_info=True); search_log_msg += " (DDG: ошибка)"

    current_time_str_main = get_current_time_str() 
    time_context_str = f"(Текущая дата и время: {current_time_str_main})\n"
    
    final_prompt_parts = [time_context_str]
    
    detected_general_url_for_prompt = None
    if not youtube_handled: 
        temp_url = extract_general_url(original_user_message_text) 
        if temp_url : 
             detected_general_url_for_prompt = temp_url
             logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Общая ссылка {detected_general_url_for_prompt} будет выделена в промпте.")
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
                            f"{search_block_instruction}")
            final_prompt_parts.append(search_block)
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен контекст из {search_provider} ({len(search_context_lines)} непустых сниппетов).")
        else: 
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Сниппеты из {search_provider} оказались пустыми, контекст не добавлен."); 
            search_log_msg += " (пустые сниппеты)"
            
    if get_user_setting(context, 'detailed_reasoning_enabled', True): 
        final_prompt_parts.append(REASONING_PROMPT_ADDITION)
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Добавлена инструкция для детального рассуждения.")
    
    final_user_prompt_text = "\n".join(final_prompt_parts)
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | {search_log_msg}")
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Финальный промпт для Gemini (длина {len(final_user_prompt_text)}):\n{final_user_prompt_text[:600]}...")

    if not youtube_handled: 
        history_entry_user = { 
            "role": "user", 
            "parts": [{"text": user_message_with_id}], 
            "user_id": user_id, 
            "message_id": user_message_id 
        }
        chat_history.append(history_entry_user)
        logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавляем user сообщение (текст/URL) в chat_history.")

    history_for_model_raw = []
    current_total_chars = 0
    # Фильтруем историю ДО текущего сообщения пользователя (которое мы только что добавили, если не youtube_handled)
    # Если youtube_handled, то user-сообщение уже добавлено в блоке YouTube
    if not youtube_handled:
        history_to_filter = chat_history[:-1] if chat_history and chat_history[-1]["message_id"] == user_message_id else chat_history
    else:
        history_to_filter = chat_history # Вся история, т.к. последнее user-сообщение (YouTube) уже там

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
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Учтено {len(history_for_model_raw)} сообщ., ~{current_total_chars} симв. (+финальный промпт).")
            break
    history_for_model = list(reversed(history_for_model_raw))
    history_for_model.append({"role": "user", "parts": [{"text": final_user_prompt_text}]}) 
    history_clean_for_model = [{"role": entry["role"], "parts": entry["parts"]} for entry in history_for_model]
    
    gemini_reply_text = await _generate_gemini_response(
        user_prompt_text=final_user_prompt_text, 
        chat_history_for_model=history_clean_for_model, 
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="TextGen" 
    )

    if gemini_reply_text and not youtube_handled: 
        history_entry_model = {"role": "model", "parts": [{"text": gemini_reply_text}]} 
        chat_history.append(history_entry_model)
        reply_to_send_to_user = gemini_reply_text
        if get_user_setting(context, 'detailed_reasoning_enabled', True) and not (gemini_reply_text.startswith("🤖") or gemini_reply_text.startswith("❌")):
            cleaned_reply = _strip_thoughts_from_text(gemini_reply_text)
            if gemini_reply_text != cleaned_reply:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (TextGen) Блок <мысли> удален из ответа перед отправкой пользователю.")
            reply_to_send_to_user = cleaned_reply
        if message: await send_reply(message, reply_to_send_to_user, context)
        else:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (TextGen) Не найдено сообщение для ответа в update (не YouTube).")
            try: await context.bot.send_message(chat_id=chat_id, text=reply_to_send_to_user)
            except Exception as e_send_direct: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (TextGen) Не удалось отправить ответ напрямую в чат (не YouTube): {e_send_direct}")
    elif not youtube_handled: 
         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (TextGen) Нет ответа для отправки пользователю после всех попыток (не YouTube). Reply от _generate_gemini_response: {gemini_reply_text}")
         final_error_message = gemini_reply_text if gemini_reply_text else "🤖 К сожалению, не удалось получить ответ от модели после нескольких попыток."
         
         if not any(h.get("role") == "model" and h.get("message_id") == user_message_id for h in reversed(chat_history[-2:])):
            chat_history.append({"role": "model", "parts": [{"text": final_error_message}]})

         try:
             if message: await message.reply_text(final_error_message)
             else: await context.bot.send_message(chat_id=chat_id, text=final_error_message)
         except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (TextGen) Не удалось отправить сообщение о финальной ошибке (не YouTube): {e_final_fail}")
    
    while len(chat_history) > MAX_HISTORY_MESSAGES:
        removed = chat_history.pop(0)
        logger.debug(f"ChatID: {chat_id} | Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed.get('role')}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: 
        logger.warning(f"ChatID: {chat_id} | handle_photo: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.photo: 
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_photo не найдено фото."); return

    photo_file_id = message.photo[-1].file_id
    user_message_id = message.message_id
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Получен photo file_id: ...{photo_file_id[-10:]}, message_id: {user_message_id}. Обработка через Gemini Vision.")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        if not file_bytes:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Скачанное фото (file_id: ...{photo_file_id[-10:]}) оказалось пустым.")
            await message.reply_text("❌ Не удалось загрузить изображение (файл пуст)."); return
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось скачать фото (file_id: ...{photo_file_id[-10:]}): {e}", exc_info=True)
        try: await message.reply_text("❌ Не удалось загрузить изображение.")
        except Exception as e_reply: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания фото: {e_reply}")
        return
    
    user_caption = message.caption if message.caption else ""
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Обработка фото как изображения (Vision).")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    if len(file_bytes) > 20 * 1024 * 1024: 
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Изображение ({len(file_bytes) / (1024*1024):.2f} MB) может быть большим для API.")
    
    try: 
        b64_data = base64.b64encode(file_bytes).decode()
    except Exception as e:
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Ошибка Base64 кодирования: {e}", exc_info=True)
        try: await message.reply_text("❌ Ошибка обработки изображения.")
        except Exception as e_reply_b64_err: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке Base64: {e_reply_b64_err}")
        return

    current_time_str_photo = get_current_time_str() 
    prompt_text_vision = (f"(Текущая дата и время: {current_time_str_photo})\n"
                          f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото с подписью: \"{user_caption}\". Опиши, что видишь на изображении и как это соотносится с подписью (если применимо)."
                         ) if user_caption else (
                          f"(Текущая дата и время: {current_time_str_photo})\n"
                          f"{USER_ID_PREFIX_FORMAT.format(user_id=user_id)}Пользователь прислал фото без подписи. Опиши, что видишь на изображении.")
    
    if get_user_setting(context, 'detailed_reasoning_enabled', True): 
        prompt_text_vision += REASONING_PROMPT_ADDITION
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Добавлена инструкция для детального рассуждения.")

    mime_type = "image/jpeg" 
    if file_bytes.startswith(b'\x89PNG\r\n\x1a\n'): mime_type = "image/png"
    elif file_bytes.startswith(b'\xff\xd8\xff'): mime_type = "image/jpeg"
    
    parts_photo = [{"text": prompt_text_vision}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]
    content_for_vision_photo_direct = [{"role": "user", "parts": parts_photo}]

    model_id_photo = get_user_setting(context, 'selected_model', DEFAULT_MODEL) 
    temperature_photo = get_user_setting(context, 'temperature', 1.0) 
    vision_capable_keywords = ['flash', 'pro', 'vision', 'ultra'] 
    is_vision_model_photo = any(keyword in model_id_photo for keyword in vision_capable_keywords)
    
    if not is_vision_model_photo:
        vision_models_photo = [m_id for m_id in AVAILABLE_MODELS if any(keyword in m_id for keyword in vision_capable_keywords)]
        if vision_models_photo:
            original_model_name_photo = AVAILABLE_MODELS.get(model_id_photo, model_id_photo)
            fallback_model_id_photo = next((m for m in vision_models_photo if 'flash' in m or 'pro' in m), vision_models_photo[0])
            model_id_photo = fallback_model_id_photo
            new_model_name_photo = AVAILABLE_MODELS.get(model_id_photo, model_id_photo)
            logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Модель {original_model_name_photo} не vision. Временно использую {new_model_name_photo}.")
        else:
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Vision) Нет доступных vision моделей.")
            await message.reply_text("❌ Нет доступных моделей для анализа изображений."); return
            
    logger.info(f"UserID: {user_id}, ChatID: {chat_id} | Анализ изображения (Vision). Модель: {model_id_photo}, Темп: {temperature_photo}, MIME: {mime_type}")
    
    reply_photo = await _generate_gemini_response(
        user_prompt_text=prompt_text_vision,
        chat_history_for_model=content_for_vision_photo_direct,
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="PhotoVisionGen"
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
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлено user-сообщение (Vision) в chat_history с image_file_id.")

    original_model_reply_content = reply_photo
    history_reply_text_with_prefix = ""
    if original_model_reply_content and not (original_model_reply_content.startswith("🤖") or original_model_reply_content.startswith("❌")):
        history_reply_text_with_prefix = f"{IMAGE_DESCRIPTION_PREFIX}{original_model_reply_content}"
    else:
        history_reply_text_with_prefix = original_model_reply_content if original_model_reply_content else "🤖 Не удалось проанализировать изображение."
    
    history_entry_model = {"role": "model", "parts": [{"text": history_reply_text_with_prefix}]}
    chat_history.append(history_entry_model)
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | Добавлен model-ответ (Vision) в chat_history: {history_reply_text_with_prefix[:100]}...")

    reply_for_user_display = ""
    if original_model_reply_content and not (original_model_reply_content.startswith("🤖") or original_model_reply_content.startswith("❌")):
        cleaned_model_reply_part = original_model_reply_content
        if get_user_setting(context, 'detailed_reasoning_enabled', True): 
            cleaned_model_reply_part = _strip_thoughts_from_text(original_model_reply_content)
            if original_model_reply_content != cleaned_model_reply_part:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (PhotoVision) Блок <мысли> удален из ответа перед отправкой.")
        reply_for_user_display = f"{IMAGE_DESCRIPTION_PREFIX}{cleaned_model_reply_part}"
    else:
        reply_for_user_display = original_model_reply_content if original_model_reply_content else "🤖 Не удалось проанализировать изображение."

    if reply_for_user_display: await send_reply(message, reply_for_user_display, context)
    else: 
        logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (PhotoVision) Нет ответа для отправки пользователю (reply_for_user_display is empty).")
        try: await message.reply_text("🤖 К сожалению, не удалось проанализировать изображение.")
        except Exception as e_final_fail: logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (PhotoVision) Не удалось отправить сообщение о финальной ошибке: {e_final_fail}")
    while len(chat_history) > MAX_HISTORY_MESSAGES: chat_history.pop(0)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.effective_user: 
        logger.warning(f"ChatID: {chat_id} | handle_document: Не удалось определить пользователя."); return
    user_id = update.effective_user.id
    message = update.message
    if not message or not message.document: 
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | В handle_document нет документа."); return
    
    doc = message.document
    allowed_mime_prefixes = ('text/', 'application/json', 'application/xml', 'application/csv', 'application/x-python', 'application/x-sh', 'application/javascript', 'application/x-yaml', 'application/x-tex', 'application/rtf', 'application/sql')
    allowed_mime_types = ('application/octet-stream',) 
    mime_type = doc.mime_type or "application/octet-stream"
    is_allowed_prefix = any(mime_type.startswith(prefix) for prefix in allowed_mime_prefixes)
    is_allowed_type = mime_type in allowed_mime_types

    if not (is_allowed_prefix or is_allowed_type):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы... Ваш тип: `{mime_type}`", parse_mode=ParseMode.MARKDOWN)
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Неподдерживаемый файл: {doc.file_name} (MIME: {mime_type})"); return

    MAX_FILE_SIZE_MB = 15
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
        try: await update.message.reply_text("❌ Не удалось загрузить файл.")
        except Exception as e_reply_dl_err: 
            logger.error(f"UserID: {user_id}, ChatID: {chat_id} | Не удалось отправить сообщение об ошибке скачивания документа: {e_reply_dl_err}")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    text = None; detected_encoding = None
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
                               except ValueError: 
                                   pass 
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
        await update.message.reply_text(f"❌ Не удалось прочитать файл `{doc.file_name}`. Попробуйте UTF-8.", parse_mode=ParseMode.MARKDOWN); return
    
    if not text.strip() and len(file_bytes) > 0: 
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Файл '{doc.file_name}' дал пустой текст после декодирования ({detected_encoding}).")
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
        logger.warning(f"UserID: {user_id}, ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {len(truncated_text)} символов.")

    user_caption_original = message.caption if message.caption else ""
    file_name_for_prompt = doc.file_name or "файл"
    encoding_info_for_prompt = f"(~{detected_encoding})" if detected_encoding else "(кодировка?)"
    file_context_for_prompt = f"Содержимое файла `{file_name_for_prompt}` {encoding_info_for_prompt}:\n```\n{truncated_text}\n```{truncation_warning}"

    # Формируем промпт для Gemini с учетом User ID и контекста файла
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
        logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Добавлена инструкция для детального рассуждения.")
    
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Подготовка к вызову _generate_gemini_response.")

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
    logger.debug(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Добавлено user-сообщение (о загрузке документа) в chat_history.")

    history_for_model_raw_doc = []
    current_total_chars_doc = 0
    history_to_filter_doc = chat_history[:-1] if chat_history and chat_history[-1]["message_id"] == user_message_id else chat_history

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
            logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Обрезка истории по символам ({MAX_CONTEXT_CHARS}). Учтено {len(history_for_model_raw_doc)} сообщ.")
            break
    
    history_for_model_doc = list(reversed(history_for_model_raw_doc))
    history_for_model_doc.append({"role": "user", "parts": [{"text": user_prompt_doc_for_gemini}]})
    history_clean_for_model_doc = [{"role": entry["role"], "parts": entry["parts"]} for entry in history_for_model_doc]

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING) 
    
    gemini_reply_doc = await _generate_gemini_response(
        user_prompt_text=user_prompt_doc_for_gemini, 
        chat_history_for_model=history_clean_for_model_doc, 
        user_id=user_id,
        chat_id=chat_id,
        context=context,
        system_instruction=system_instruction_text,
        log_prefix="DocGen"
    )

    if gemini_reply_doc:
        history_entry_model_doc = {"role": "model", "parts": [{"text": gemini_reply_doc}]} 
        chat_history.append(history_entry_model_doc)
        reply_to_send_to_user_doc = gemini_reply_doc
        if get_user_setting(context, 'detailed_reasoning_enabled', True) and not (gemini_reply_doc.startswith("🤖") or gemini_reply_doc.startswith("❌")):
            cleaned_reply_doc = _strip_thoughts_from_text(gemini_reply_doc)
            if gemini_reply_doc != cleaned_reply_doc:
                 logger.info(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Блок <мысли> удален из ответа.")
            reply_to_send_to_user_doc = cleaned_reply_doc
        await send_reply(message, reply_to_send_to_user_doc, context)
    else: 
         logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Нет ответа от _generate_gemini_response.")
         final_error_msg_doc = gemini_reply_doc if gemini_reply_doc else "🤖 К сожалению, не удалось обработать документ."
         
         if not any(h.get("role") == "model" and h.get("message_id") == user_message_id for h in reversed(chat_history[-2:])):
            chat_history.append({"role": "model", "parts": [{"text": final_error_msg_doc}]})

         try: await message.reply_text(final_error_msg_doc)
         except Exception as e_final_fail_doc: 
             logger.error(f"UserID: {user_id}, ChatID: {chat_id} | (Document) Не удалось отправить сообщение о финальной ошибке: {e_final_fail_doc}")

    while len(chat_history) > MAX_HISTORY_MESSAGES:
        removed_doc = chat_history.pop(0)
        logger.debug(f"ChatID: {chat_id} | (Document) Удалено старое сообщение из истории (лимит {MAX_HISTORY_MESSAGES}). Role: {removed_doc.get('role')}")
    
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
    
    logger.setLevel(log_level) 
    logger.info(f"--- Установлен уровень логгирования для '{logger.name}': {log_level_str} ({log_level}) ---")

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
             try:
                 await asyncio.wait_for(web_server_task, timeout=15.0)
                 logger.info("Веб-сервер успешно завершен.")
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
            try: await application.shutdown(); logger.info("Приложение Telegram бота успешно остановлено.")
            except Exception as e_shutdown: logger.error(f"Ошибка во время application.shutdown(): {e_shutdown}", exc_info=True)
        if aiohttp_session_main and not aiohttp_session_main.closed:
             logger.info("Закрытие основной сессии aiohttp..."); await aiohttp_session_main.close(); await asyncio.sleep(0.5); logger.info("Основная сессия aiohttp закрыта.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Отмена {len(tasks)} оставшихся фоновых задач...")
            [task.cancel() for task in tasks]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            cancelled_count, error_count = 0, 0
            for i, res in enumerate(results):
                 task_name = tasks[i].get_name()
                 if isinstance(res, asyncio.CancelledError): cancelled_count += 1; logger.debug(f"Задача '{task_name}' успешно отменена.")
                 elif isinstance(res, Exception): error_count += 1; logger.warning(f"Ошибка в отмененной задаче '{task_name}': {res}", exc_info=True)
                 else: logger.debug(f"Задача '{task_name}' завершилась с результатом: {res}")
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
