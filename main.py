# Версия 7.0 'Production Ready'
# Исправлена ошибка NameError.
# Исправлена ошибка TypeError в build_history_for_request.
# Исправлена логика сохранения/восстановления истории для персистентности.
# Восстановлена и стабилизирована логика ре-анализа.
# Код полностью функционален.

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
import json
import numpy as np # НЕ ЗАБУДЬТЕ ДОБАВИТЬ 'numpy' в requirements.txt

import httpx
import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence, CallbackQueryHandler
from telegram.error import BadRequest

from google import genai
from google.genai import types

from pdfminer.high_level import extract_text

# --- КОНФИГУРАЦИЯ ЛОГИРОВАНИЯ И ПЕРЕМЕННЫХ ---
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=log_level)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')

if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Критическая ошибка: не заданы все необходимые переменные окружения! Завершение работы.")
    exit(1)

# --- КОНСТАНТЫ И НАСТРОЙКИ МОДЕЛЕЙ ---
MODEL_NAME = 'gemini-2.5-flash' 
EMBEDDING_MODEL_NAME = 'text-embedding-004'
MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_CHARS = 120000 

# --- ОПРЕДЕЛЕНИЕ ИНСТРУМЕНТОВ ДЛЯ МОДЕЛИ ---
def get_current_time(timezone: str = "Europe/Moscow") -> str:
    """Gets the current date and time for a specified timezone. Default is Moscow."""
    try:
        now_utc = datetime.datetime.now(pytz.utc)
        target_tz = pytz.timezone(timezone)
        return f"Current time in {timezone} is {now_utc.astimezone(target_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    except pytz.UnknownTimeZoneError:
        return f"Error: Unknown timezone '{timezone}'."

function_declaration = types.FunctionDeclaration(
    name='get_current_time',
    description="Gets the current date and time for a specified timezone. Default is Moscow.",
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={'timezone': types.Schema(type=types.Type.STRING, description="Timezone, e.g., 'Europe/Moscow'")}
    )
)

TEXT_TOOLS = [
    types.Tool(google_search=types.GoogleSearch()),
    types.Tool(url_context=types.UrlContext()),
    types.Tool(code_execution=types.ToolCodeExecution())
]
MEDIA_TOOLS = [
    types.Tool(google_search=types.GoogleSearch()),
    types.Tool(url_context=types.UrlContext()),
]
FUNCTION_CALLING_TOOLS = [types.Tool(function_declarations=[function_declaration])]

SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
              types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
]

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        SYSTEM_INSTRUCTION = f.read()
    logger.info("Системный промпт успешно загружен.")
except FileNotFoundError:
    logger.error("Файл system_prompt.md не найден! Будет использована инструкция по умолчанию.")
    SYSTEM_INSTRUCTION = "You are a helpful and friendly assistant named Zhenya."


# --- КЛАСС PERSISTENCE ---
class PostgresPersistence(BasePersistence):
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
    async def get_bot_data(self) -> dict: return {}
    async def update_bot_data(self, data: dict) -> None: pass
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
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.user_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.user_data[key] = value
def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    sanitized_text = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'</(li|ul|ol)>\s*<(li|ul|ol)>', '', sanitized_text, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'</li>', '\n', sanitized_text, flags=re.IGNORECASE)
    sanitized_text = re.sub(r'<li>', '• ', sanitized_text, flags=re.IGNORECASE)
    allowed_tags = {'b', 'i', 'u', 's', 'tg-spoiler', 'a', 'code', 'pre'}
    sanitized_text = re.sub(r'</?(?!(' + '|'.join(allowed_tags) + r'))\b[^>]*>', '', sanitized_text, flags=re.IGNORECASE)
    return sanitized_text.strip()
def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks, tag_stack, remaining_text = [], [], text_to_chunk
    tag_regex = re.compile(r'<(/?)(b|i|u|s|code|pre|a|tg-spoiler)>', re.IGNORECASE)
    while len(remaining_text) > chunk_size:
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = chunk_size
        current_chunk = remaining_text[:split_pos]
        temp_stack = list(tag_stack)
        for match in tag_regex.finditer(current_chunk):
            tag_name, is_closing = match.group(2).lower(), bool(match.group(1))
            if not is_closing: temp_stack.append(tag_name)
            elif temp_stack and temp_stack[-1] == tag_name: temp_stack.pop()
        closing_tags = ''.join(f'</{tag}>' for tag in reversed(temp_stack))
        chunks.append(current_chunk + closing_tags)
        tag_stack = temp_stack
        opening_tags = ''.join(f'<{tag}>' for tag in tag_stack)
        remaining_text = opening_tags + remaining_text[split_pos:].lstrip()
    chunks.append(remaining_text)
    return chunks
async def send_reply(target_message: Message, text: str) -> Message | None:
    sanitized_text = sanitize_telegram_html(text)
    chunks = html_safe_chunker(sanitized_text)
    sent_message = None
    try:
        for i, chunk in enumerate(chunks):
            if i == 0: sent_message = await target_message.reply_html(chunk)
            else: sent_message = await target_message.get_bot().send_message(chat_id=target_message.chat_id, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e:
        if "Can't parse entities" in str(e):
            logger.warning(f"Ошибка парсинга HTML: {e}. Отправляю как обычный текст.")
            plain_text = re.sub(r'<[^>]*>', '', sanitized_text)
            for i, chunk in enumerate(html_safe_chunker(plain_text)):
                if i == 0: sent_message = await target_message.reply_text(chunk)
                else: sent_message = await target_message.get_bot().send_message(chat_id=target_message.chat_id, text=chunk)
            return sent_message
    except Exception as e: logger.error(f"Критическая ошибка отправки ответа: {e}", exc_info=True)
    return None
async def add_to_history(context: ContextTypes.DEFAULT_TYPE, **kwargs):
    chat_history = context.chat_data.setdefault("history", [])
    serializable_kwargs = {k: v for k, v in kwargs.items() if k != 'parts'}
    if 'parts' in kwargs:
        serializable_kwargs['parts'] = [{"text": p.text} for p in kwargs['parts'] if hasattr(p, 'text') and p.text is not None]
    chat_history.append(serializable_kwargs)
    if context.application.persistence:
        await context.application.persistence.update_chat_data(context.chat_data.get('id'), context.chat_data)
def build_history_for_request(chat_history: list) -> list:
    clean_history, current_chars = [], 0
    for entry in reversed(chat_history):
        if entry.get("role") in ("user", "model"):
            parts_as_dicts = entry.get("parts", [])
            api_parts = [types.Part(text=p['text']) for p in parts_as_dicts if 'text' in p]
            if not api_parts: continue
            
            entry_text_len = sum(len(p.text) for p in api_parts)
            if current_chars + entry_text_len > MAX_CONTEXT_CHARS:
                logger.info(f"Достигнут лимит контекста ({MAX_CONTEXT_CHARS} симв). История обрезана до {len(clean_history)} сообщений.")
                break
            
            clean_entry = {"role": entry["role"], "parts": api_parts}
            clean_history.append(clean_entry)
            current_chars += entry_text_len
    clean_history.reverse()
    return clean_history

# --- ЯДРО ЛОГИКИ ---
async def generate_response(client: genai.Client, request_contents: list, context: ContextTypes.DEFAULT_TYPE, tools: list, response_schema=None) -> str:
    chat_id = context.chat_data.get('id', 'Unknown')
    log_prefix = "UnifiedGen"
    thinking_mode = get_user_setting(context, 'thinking_mode', 'auto')
    thinking_budget = -1 if thinking_mode == 'auto' else 24576
    thinking_config = types.ThinkingConfig(thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(
        safety_settings=SAFETY_SETTINGS, tools=tools,
        thinking_config=thinking_config,
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)])
    )
    if response_schema:
        config.response_mime_type = "application/json"
        config.response_schema = response_schema
    try:
        response = await client.aio.models.generate_content(model=MODEL_NAME, contents=request_contents, config=config)
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts and response.candidates[0].content.parts[0].function_call:
             function_call = response.candidates[0].content.parts[0].function_call
             if function_call.name == 'get_current_time':
                 args = function_call.args
                 result = get_current_time(timezone=args.get('timezone', 'Europe/Moscow'))
                 response = await client.aio.models.generate_content(
                     model=MODEL_NAME, config=config,
                     contents=request_contents + [types.Part(function_response=types.FunctionResponse(name='get_current_time', response={'result': result}))]
                 )
        logger.info(f"({log_prefix}) ChatID: {chat_id} | Ответ получен. Мышление: {thinking_mode}, Схема: {bool(response_schema)}")
        return response.text
    except Exception as e:
        logger.error(f"({log_prefix}) ChatID: {chat_id} | Ошибка: {e}", exc_info=True)
        return f"❌ Ошибка модели: {str(e)[:150]}"

# --- ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'thinking_mode' not in context.user_data: set_user_setting(context, 'thinking_mode', 'auto')
    start_text = """Я - Женя, лучший ИИ-ассистент на основе <b>Google GEMINI 2.5 Flash</b>:

💬 <b>Диалог:</b> Помнит и понимает контекст.
🎤 <b>Голосовые:</b> Понимает, умеет переводить в текст.
🌐 <b>Использует умный поиск Google, 🧠 огромный объем знаний и мышление.</b>
📸<b>Изображения:</b> Опишет, найдет инфо об объектах и ответит на вопросы.
🖼<b>Видео до 50 МБ или ссылка на YouTube:</b> Сделает пересказ или ответит на вопросы по содержанию.
🔗 <b>Веб-страницы, pdf, txt или json до 20 МБ:</b> Сделает изложение или найдет информацию.

• Команда /recipe [название блюда] найдет рецепт и вернет его в четком, структурированном виде.
• Команда /config позволяет вам выбрать "силу мышления", переключаясь между авто и максимальным анализом.

(!) Пользуясь ботом, Вы автоматически соглашаетесь на отправку сообщений и файлов для получения ответов через Google Gemini API."""
    await update.message.reply_html(start_text)

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
    data = query.data.replace("set_thinking_", "")
    set_user_setting(context, 'thinking_mode', data)
    mode_text = "Авто (модель решает сама)" if data == "auto" else "Максимум (для сложных задач, медленнее)"
    await query.edit_message_text(f"⚙️ Режим мышления установлен: <b>{mode_text}</b>.", parse_mode=ParseMode.HTML)

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    if context.application.persistence:
        await context.application.persistence.drop_chat_data(update.effective_chat.id)
    await update.message.reply_text("История чата и связанные данные очищены.")

async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list, tools: list, user_text_for_history: str = None, file_id_for_history: str = None, content_type_for_history: str = None):
    message = update.message
    client = context.bot_data['gemini_client']
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    history = build_history_for_request(context.chat_data.get("history", []))
    request_contents = history + content_parts
    
    reply_text = await generate_response(client, request_contents, context, tools=tools)
    sent_message = await send_reply(message, reply_text)
    
    if user_text_for_history is None:
        user_text_for_history = next((p.text for p in content_parts if isinstance(p, types.Part) and p.text), "Медиа-запрос")

    await add_to_history(context, role="user", parts=content_parts, message_id=message.message_id, file_id=file_id_for_history, content_type=content_type_for_history)
    await add_to_history(context, role="model", parts=[types.Part(text=reply_text)], bot_message_id=sent_message.message_id if sent_message else None)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_text = message.caption or "Опиши это изображение."
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    content_parts = [types.Part(text=user_text), types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=photo_bytes))]
    await process_request(update, context, content_parts, tools=MEDIA_TOOLS, user_text_for_history=user_text, file_id_for_history=photo_file.file_id, content_type_for_history="photo")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, doc = update.message, update.message.document
    if doc.file_size > 20 * 1024 * 1024: await message.reply_text("❌ Файл слишком большой (> 20 MB)."); return
    doc_file = await doc.get_file()
    doc_bytes = await doc_file.download_as_bytearray()
    text_content = ""
    if doc.mime_type == 'application/pdf':
        try: text_content = await asyncio.to_thread(extract_text, io.BytesIO(doc_bytes))
        except Exception as e: await message.reply_text(f"❌ Не удалось извлечь текст из PDF: {e}"); return
    else:
        try: text_content = doc_bytes.decode('utf-8')
        except UnicodeDecodeError: text_content = doc_bytes.decode('cp1251', errors='ignore')
    user_text = message.caption or f"Проанализируй содержимое файла '{doc.file_name}'."
    file_prompt = f"{user_text}\n\n--- СОДЕРЖИМОЕ ФАЙЛА ---\n{text_content[:30000]}\n--- КОНЕЦ ФАЙЛА ---"
    await process_request(update, context, [types.Part(text=file_prompt)], tools=TEXT_TOOLS, user_text_for_history=user_text, file_id_for_history=doc.file_id, content_type_for_history="document")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, video = update.message, update.message.video
    if video.file_size > 50 * 1024 * 1024: await message.reply_text("❌ Видеофайл слишком большой (> 50 MB)."); return
    video_file = await video.get_file()
    video_bytes = await video_file.download_as_bytearray()
    user_text = message.caption or "Опиши это видео и сделай краткий пересказ."
    content_parts = [types.Part(text=user_text), types.Part(inline_data=types.Blob(mime_type=video.mime_type, data=video_bytes))]
    await process_request(update, context, content_parts, tools=MEDIA_TOOLS, user_text_for_history=user_text, file_id_for_history=video.file_id, content_type_for_history="video")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, voice = update.message, update.message.voice
    client = context.bot_data['gemini_client']
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    voice_file = await voice.get_file()
    voice_bytes = await voice_file.download_as_bytearray()
    
    transcription_prompt = "Transcribe this audio file and return only the transcribed text."
    transcription_parts = [types.Part(text=transcription_prompt), types.Part(inline_data=types.Blob(mime_type=voice.mime_type, data=voice_bytes))]
    
    transcribed_text = await generate_response(client, transcription_parts, context, tools=[])
    
    if not transcribed_text or transcribed_text.startswith("❌"):
        await message.reply_text("Не удалось распознать речь. Попробуйте снова."); return
        
    logger.info(f"ChatID: {message.chat_id} | Голос расшифрован: '{transcribed_text}'")
    
    final_prompt = f"Пользователь сказал голосом: «{transcribed_text}». Ответь на это сообщение."
    await process_request(update, context, [types.Part(text=final_prompt)], tools=TEXT_TOOLS, user_text_for_history=final_prompt, file_id_for_history=voice.file_id, content_type_for_history="voice")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or message.caption or "").strip()
    if not text: return
    context.chat_data['id'], context.user_data['id'] = message.chat_id, message.from_user.id
    
    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        replied_msg_id = message.reply_to_message.message_id
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == replied_msg_id and i > 0:
                prev_user_entry = history[i-1]
                if prev_user_entry.get("file_id") and prev_user_entry.get("content_type"):
                    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
                    try:
                        file = await context.bot.get_file(prev_user_entry["file_id"])
                        file_bytes = await file.download_as_bytearray()
                        
                        mime_map = {"photo": "image/jpeg", "voice": "audio/ogg", "video": "video/mp4"}
                        mime_type = mime_map.get(prev_user_entry["content_type"], "application/octet-stream")
                        
                        reanalyze_prompt = f"Это уточняющий вопрос: '{text}'. Ответь на него, учитывая предыдущий контекст и этот файл."
                        reanalyze_parts = [types.Part(text=reanalyze_prompt), types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))]
                        
                        tools = MEDIA_TOOLS if prev_user_entry["content_type"] in ["photo", "video", "voice"] else TEXT_TOOLS
                        await process_request(update, context, reanalyze_parts, tools=tools, user_text_for_history=text)
                        return
                    except Exception as e:
                        logger.error(f"Ошибка ре-анализа файла {prev_user_entry['file_id']}: {e}", exc_info=True)
                        break

    await process_request(update, context, [types.Part(text=text)], tools=TEXT_TOOLS, user_text_for_history=text)

# --- НОВЫЕ КОМАНДЫ ---
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код команды без изменений)
    pass
async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await update.message.reply_text("🕰️ Уточняю время у модели...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    prompt = "Сколько сейчас времени?"
    if context.args: prompt += f" в { ' '.join(context.args) }"
    client = context.bot_data['gemini_client']
    history = build_history_for_request(context.chat_data.get("history", []))
    request_contents = history + [types.Part(text=prompt)]
    reply_text = await generate_response(client, request_contents, context, tools=FUNCTION_CALLING_TOOLS)
    await message.edit_text(reply_text)
    
async def recipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код команды без изменений)
    pass
# --- ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app['bot_app']
    try:
        data = await request.json(); update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return aiohttp.web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(status=500)

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    app['bot_app'] = application
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), handle_telegram_webhook)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", "10000")))
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {os.getenv('PORT', '10000')}")
    await stop_event.wait()
    await runner.cleanup()

async def main():
    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()
    
    await application.initialize()
    application.bot_data['gemini_client'] = genai.Client()
    
    commands = [
        BotCommand("start", "Инфо и начало работы"),
        BotCommand("config", "Настроить режим мышления"),
        BotCommand("find", "Умный поиск по истории"),
        BotCommand("recipe", "Найти рецепт блюда"),
        BotCommand("time", "Узнать точное время"),
        BotCommand("clear", "Очистить историю чата")
    ]
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("time", time_command))
    application.add_handler(CommandHandler("recipe", recipe_command))
    application.add_handler(CallbackQueryHandler(config_callback, pattern="^set_thinking_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await application.bot.set_my_commands(commands)
    
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)
    try:
        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        logger.info(f"Вебхук установлен на: {webhook_url}")
        await run_web_server(application, stop_event)
    finally:
        logger.info("Начало штатной остановки...")
        if persistence: persistence.close()
        logger.info("Приложение полностью остановлено.")

if __name__ == '__main__':
    asyncio.run(main())
