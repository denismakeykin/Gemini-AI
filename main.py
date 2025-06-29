# Версия 4: Архитектура на основе полного набора инструментов (Grounding, Function Calling, URL Context) и кэширования.
import logging
import os
import asyncio
import signal
import re
import pickle
from collections import defaultdict
import psycopg2
from psycopg2 import pool
import io
import html
import time
import base64
import datetime
import pytz

import httpx
import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence, CallbackQueryHandler
from telegram.error import BadRequest

from google import genai
from google.genai import types

from pdfminer.high_level import extract_text

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

# --- КОНСТАНТЫ И ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')

# Строго используем указанную модель
MODEL_NAME = 'gemini-2.5-flash' 
MAX_OUTPUT_TOKENS = 8192

# --- ОПРЕДЕЛЕНИЕ ИНСТРУМЕНТОВ ДЛЯ МОДЕЛИ ---

# 1. Функция, которую сможет вызывать модель
def get_current_time(timezone: str = "Europe/Moscow") -> str:
    """Gets the current date and time for a specified timezone. Default is Moscow."""
    try:
        now_utc = datetime.datetime.now(pytz.utc)
        target_tz = pytz.timezone(timezone)
        now_target = now_utc.astimezone(target_tz)
        return f"Current time in {timezone} is {now_target.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    except pytz.UnknownTimeZoneError:
        return f"Error: Unknown timezone '{timezone}'."

# 2. Собираем все инструменты в один список
# SDK любезно создает схему для функции самостоятельно
function_tool = types.Tool.from_function(get_current_time)

# Полный набор инструментов для каждого запроса
DEFAULT_TOOLS = [
    types.Tool(google_search=types.GoogleSearch()), # Grounding
    function_tool,                                 # Function Calling
    types.Tool(url_context=types.UrlContext())     # URL Analysis
]

# 3. Настройки безопасности
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
              types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
]


# --- КЛАСС PERSISTENCE (без изменений) ---
class PostgresPersistence(BasePersistence):
    # ... (весь код класса PostgresPersistence без изменений)
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None
        self.dsn = database_url
        try: self._connect(); self._initialize_db()
        except psycopg2.Error as e: logger.critical(f"PostgresPersistence: Не удалось подключиться к БД: {e}"); raise
    def _connect(self):
        if self.db_pool:
            try: self.db_pool.closeall()
            except Exception as e: logger.warning(f"Ошибка при закрытии старого пула: {e}")
        dsn = self.dsn
        keepalive_options = "keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=5"
        if "?" in dsn:
             if "keepalives" not in dsn: dsn = f"{dsn}&{keepalive_options}"
        else: dsn = f"{dsn}?{keepalive_options}"
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=dsn)
    def _execute(self, query: str, params: tuple = None, fetch: str = None, retries=3):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        last_exception = None
        for attempt in range(retries):
            conn = None; connection_handled = False
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Postgres: Ошибка соединения (попытка {attempt + 1}/{retries}): {e}")
                last_exception = e
                if conn: self.db_pool.putconn(conn, close=True); connection_handled = True
                if attempt < retries - 1: self._connect(); time.sleep(1 + attempt)
                continue
            finally:
                if conn and not connection_handled: self.db_pool.putconn(conn)
        logger.error(f"Postgres: Не удалось выполнить запрос после {retries} попыток. Последняя ошибка: {last_exception}")
        return None
    def _initialize_db(self): self._execute("CREATE TABLE IF NOT EXISTS persistence_data (key TEXT PRIMARY KEY, data BYTEA NOT NULL);")
    def _get_pickled(self, key: str) -> object | None:
        res = self._execute("SELECT data FROM persistence_data WHERE key = %s;", (key,), fetch="one")
        return pickle.loads(res[0]) if res and res[0] else None
    def _set_pickled(self, key: str, data: object) -> None: self._execute("INSERT INTO persistence_data (key, data) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET data = %s;", (key, pickle.dumps(data), pickle.dumps(data)))
    async def get_bot_data(self) -> dict: return await asyncio.to_thread(self._get_pickled, "bot_data") or {}
    async def update_bot_data(self, data: dict) -> None: await asyncio.to_thread(self._set_pickled, "bot_data", data)
    async def get_chat_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'chat_data_%';", fetch="all")
        chat_data = defaultdict(dict)
        if all_data:
            for k, d in all_data:
                try: chat_data[int(k.split('_')[-1])] = pickle.loads(d)
                except (ValueError, IndexError): logger.warning(f"Обнаружен некорректный ключ чата в БД: '{k}'. Запись пропущена.")
        return chat_data
    async def update_chat_data(self, chat_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"chat_data_{chat_id}", data)
    async def get_user_data(self) -> defaultdict[int, dict]: return defaultdict(dict)
    async def update_user_data(self, user_id: int, data: dict) -> None: pass
    async def drop_chat_data(self, chat_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def drop_user_data(self, user_id: int) -> None: pass
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass
    async def refresh_bot_data(self, bot_data: dict) -> None: pass
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}
        chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: pass
    async def flush(self) -> None: pass
    def close(self):
        if self.db_pool: self.db_pool.closeall()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value):
    return context.user_data.get(key, default_value)

def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value):
    context.user_data[key] = value

# ... (санитизация, отправка ответа и пр. без изменений)
def sanitize_telegram_html(raw_html: str) -> str:
    # ... (код функции без изменений)
    if not raw_html: return ""
    sanitized_text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'</li>', '\n', sanitized_text, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'<li>', '• ', sanitized_text, flags=re.IGNORECASE)
    allowed_tags = {'b', 'i', 'u', 's', 'tg-spoiler', 'a', 'code', 'pre'}
    sanitized_text = re.sub(r'</?(?!(' + '|'.join(allowed_tags) + r'))\b[^>]*>', '', sanitized_text, flags=re.IGNORECASE)
    return sanitized_text.strip()

async def send_reply(target_message: Message, text: str) -> Message | None:
    # ... (код функции без изменений)
    try:
        # Для простоты пока без чанкера
        return await target_message.reply_html(text[:4096])
    except BadRequest as e:
        logger.warning(f"Ошибка парсинга HTML: {e}. Отправка как обычный текст.")
        plain_text = re.sub(r'<[^>]*>', '', text)
        return await target_message.reply_text(plain_text[:4096])
    except Exception as e:
        logger.error(f"Ошибка отправки ответа: {e}", exc_info=True)
    return None

def build_history_for_request(chat_history: list) -> list:
    history = []
    for entry in reversed(chat_history):
        if entry.get("role") in ("user", "model") and "cache_name" not in entry:
            history.append(entry)
        if len(history) >= 20: # Увеличим историю для лучшего контекста
            break
    history.reverse()
    return history


# --- ЯДРО ЛОГИКИ: УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ЗАПРОСОВ ---

async def generate_response(
    client: genai.Client,
    user_prompt_parts: list,
    context: ContextTypes.DEFAULT_TYPE,
    cache_name: str | None = None
) -> str | None:
    """Универсальная функция для генерации ответа с кэшем, инструментами и настройками."""
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "UnifiedGen"
    
    request_contents = user_prompt_parts
    if not cache_name:
        history = build_history_for_request(context.chat_data.get("history", []))
        request_contents = history + user_prompt_parts

    # ИЗМЕНЕНО: Добавляем конфигурацию мышления
    thinking_mode = get_user_setting(context, 'thinking_mode', 'auto')
    thinking_budget = -1 if thinking_mode == 'auto' else 24576
    thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)

    try:
        config = types.GenerateContentConfig(
            safety_settings=SAFETY_SETTINGS,
            tools=DEFAULT_TOOLS,
            thinking_config=thinking_config,
            cached_content=cache_name
        )
        
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=request_contents,
            config=config
        )
        logger.info(f"({log_prefix}) ChatID: {chat_id} | Ответ получен. Кэш: {bool(cache_name)}, Мышление: {thinking_mode}")
        return response.text

    except Exception as e:
        logger.error(f"({log_prefix}) ChatID: {chat_id} | Ошибка: {e}", exc_info=True)
        return f"❌ Ошибка модели: {str(e)[:100]}"


# --- ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Установка начальных настроек при первом запуске
    if 'thinking_mode' not in context.user_data: set_user_setting(context, 'thinking_mode', 'auto')
    
    await update.message.reply_text(
        f"Привет! Я бот на основе <b>Google Gemini {MODEL_NAME}</b>.\n\n"
        "Мои возможности:\n"
        "• 🧠 <b>Мышление:</b> Анализирую сложные запросы.\n"
        "• 🌐 <b>Поиск Google:</b> Нахожу актуальную информацию.\n"
        "• 🔗 <b>Анализ ссылок:</b> Просто пришлите URL.\n"
        "• 📸 <b>Работа с фото:</b> Использую кэш для быстрых ответов на доп. вопросы.\n"
        "• 📞 <b>Вызов функций:</b> Могу выполнять код, например, узнать время.\n\n"
        "Используйте /config для настройки режима мышления.",
        parse_mode=ParseMode.HTML
    )

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_mode = get_user_setting(context, 'thinking_mode', 'auto')
    
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'auto' else ''}Мышление: Авто", callback_data="set_thinking_auto")],
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'max' else ''}Мышление: Максимум", callback_data="set_thinking_max")]
    ]
    await update.message.reply_text("⚙️ Настройки:", reply_markup=InlineKeyboardMarkup(keyboard))

async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data == "set_thinking_auto":
        set_user_setting(context, 'thinking_mode', 'auto')
        await query.edit_message_text("⚙️ Настройки:\n\n✅ <b>Мышление: Авто</b>\nМодель сама решает, когда и сколько думать. Оптимально для большинства задач.")
    elif data == "set_thinking_max":
        set_user_setting(context, 'thinking_mode', 'max')
        await query.edit_message_text("⚙️ Настройки:\n\n✅ <b>Мышление: Максимум</b>\nИспользуется максимальный бюджет для самых сложных запросов. Может работать медленнее.")

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list, user_text: str):
    """Общий обработчик для фото, документов и т.д., использующий кэш."""
    message = update.message
    client = context.bot_data['gemini_client']
    chat_id = message.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # Создаем кэш для контента
        cache = await client.aio.caches.create(
            model=MODEL_NAME,
            contents=content_parts,
            display_name=f"chat_{chat_id}_msg_{message.message_id}",
            ttl=datetime.timedelta(hours=1)
        )
        logger.info(f"ChatID: {chat_id} | Создан кэш '{cache.name}'")
        
        # Сохраняем в историю ссылку на кэш
        context.chat_data.setdefault("history", []).append({
            "role": "user", "parts": [{"text": user_text}], "message_id": message.message_id, "cache_name": cache.name
        })
        
        reply_text = await generate_response(client, [], context, cache_name=cache.name)
        sent_message = await send_reply(message, sanitize_telegram_html(reply_text))

        context.chat_data["history"].append({
            "role": "model", "parts": [{"text": reply_text}], "bot_message_id": sent_message.message_id if sent_message else None
        })

    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Ошибка в handle_content: {e}", exc_info=True)
        await message.reply_text("❌ Не удалось обработать ваш контент.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    user_text = message.caption or "Опиши это изображение."
    content_parts = [
        types.Part(text=user_text),
        types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=photo_bytes))
    ]
    await handle_content(update, context, content_parts, user_text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    client = context.bot_data['gemini_client']
    text = message.text.strip()
    if not text: return

    context.chat_data['id'] = message.chat_id

    # Проверка на ответ сообщению с кэшем
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        replied_msg_id = message.reply_to_message.message_id
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == replied_msg_id and i > 0:
                prev_user_entry = history[i-1]
                if "cache_name" in prev_user_entry:
                    cache_name = prev_user_entry["cache_name"]
                    logger.info(f"ChatID: {message.chat_id} | Ответ на сообщение с кэшем '{cache_name}'.")
                    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
                    
                    reply_text = await generate_response(client, [types.Part(text=text)], context, cache_name=cache_name)
                    sent_message = await send_reply(message, sanitize_telegram_html(reply_text))

                    context.chat_data["history"].append({"role": "user", "parts": [{"text": text}], "message_id": message.message_id})
                    context.chat_data["history"].append({"role": "model", "parts": [{"text": reply_text}], "bot_message_id": sent_message.message_id if sent_message else None})
                    return

    # Обычное текстовое сообщение
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    reply_text = await generate_response(client, [types.Part(text=text)], context, cache_name=None)
    sent_message = await send_reply(message, sanitize_telegram_html(reply_text))

    context.chat_data.setdefault("history", []).append({"role": "user", "parts": [{"text": text}], "message_id": message.message_id})
    context.chat_data["history"].append({"role": "model", "parts": [{"text": reply_text}], "bot_message_id": sent_message.message_id if sent_message else None})

# ... (main, setup, web server - без принципиальных изменений)
async def main():
    # ... (проверки переменных)
    genai.configure(api_key=GOOGLE_API_KEY)
    
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()

    application.bot_data['gemini_client'] = genai.Client()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CallbackQueryHandler(config_callback, pattern="^set_thinking_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # ... (остальной код запуска)
    await application.initialize()
    logger.info("Бот готов к запуску с полной поддержкой инструментов 2.5 Flash.")

if __name__ == '__main__':
    logger.info("Код готов к развертыванию.")
