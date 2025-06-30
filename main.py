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
import time
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
from duckduckgo_search import DDGS

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
    logger.critical("Критическая ошибка: не заданы все необходимые переменные окружения!")
    exit(1)

# --- КОНСТАНТЫ И НАСТРОЙКИ МОДЕЛЕЙ ---
MODEL_NAME = 'gemini-2.5-flash'
YOUTUBE_REGEX = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
URL_REGEX = r'https?:\/\/[^\s/$.?#].[^\s]*'
MAX_CONTEXT_CHARS = 120000 

# --- ОПРЕДЕЛЕНИЕ ИНСТРУМЕНТОВ ДЛЯ МОДЕЛИ ---
def get_current_time_str(timezone: str = "Europe/Moscow") -> str:
    return datetime.datetime.now(pytz.timezone(timezone)).strftime('%Y-%m-%d %H:%M:%S %Z')

function_declaration = types.FunctionDeclaration(
    name='get_current_time_str',
    description="Gets the current date and time for a specified timezone. Default is Moscow.",
    parameters=types.Schema(type=types.Type.OBJECT, properties={'timezone': types.Schema(type=types.Type.STRING)})
)

TEXT_TOOLS = [types.Tool(google_search=types.GoogleSearch()), types.Tool(code_execution=types.ToolCodeExecution())]
MEDIA_TOOLS = [types.Tool(google_search=types.GoogleSearch())]
FUNCTION_CALLING_TOOLS = [types.Tool(function_declarations=[function_declaration])]
SAFETY_SETTINGS = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
              types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT)
]

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f: SYSTEM_INSTRUCTION = f.read()
    logger.info("Системный промпт успешно загружен из файла.")
except FileNotFoundError:
    logger.error("Файл system_prompt.md не найден! Будет использована инструкция по умолчанию.")
    SYSTEM_INSTRUCTION = "You are a helpful and friendly assistant named Zhenya."

# --- КЛАСС PERSISTENCE --- (Без изменений)
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
        conn = None
        for attempt in range(retries):
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
                self.db_pool.putconn(conn)
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Postgres: Ошибка соединения (попытка {attempt + 1}/{retries}): {e}")
                if conn: self.db_pool.putconn(conn, close=True)
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    self._connect()
                else:
                    logger.error(f"Postgres: Не удалось выполнить запрос после {retries} попыток.")
                    raise e
            except Exception as e:
                if conn: self.db_pool.putconn(conn)
                raise e
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
                except (ValueError, IndexError, pickle.UnpicklingError): logger.warning(f"Обнаружен некорректный ключ или данные чата в БД: '{k}'. Запись пропущена.")
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
def get_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, default_value): return context.chat_data.get(key, default_value)
def set_user_setting(context: ContextTypes.DEFAULT_TYPE, key: str, value): context.chat_data[key] = value

def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks, tag_stack, remaining_text = [], [], text_to_chunk
    tag_regex = re.compile(r'<(/?)(b|i|u|s|code|pre|a|tg-spoiler|br)>', re.IGNORECASE)
    while len(remaining_text) > chunk_size:
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = chunk_size
        current_chunk = remaining_text[:split_pos]
        temp_stack = list(tag_stack)
        for match in tag_regex.finditer(current_chunk):
            tag_name, is_closing = match.group(2).lower(), bool(match.group(1))
            if tag_name == 'br': continue
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
    sanitized_text = re.sub(r'<br\s*/?>', '\n', text)
    chunks = html_safe_chunker(sanitized_text)
    sent_message = None
    try:
        for i, chunk in enumerate(chunks):
            if i == 0: sent_message = await target_message.reply_html(chunk)
            else: sent_message = await target_message.get_bot().send_message(chat_id=target_message.chat_id, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.1)
        return sent_message
    except BadRequest as e:
        if "Can't parse entities" in str(e) or "unsupported start tag" in str(e):
            logger.warning(f"Ошибка парсинга HTML: {e}. Отправляю как обычный текст.")
            plain_text = re.sub(r'<[^>]*>', '', sanitized_text)
            chunks = html_safe_chunker(plain_text)
            for i, chunk in enumerate(chunks):
                if i == 0: sent_message = await target_message.reply_text(chunk)
                else: sent_message = await target_message.get_bot().send_message(chat_id=target_message.chat_id, text=chunk)
            return sent_message
    except Exception as e: logger.error(f"Критическая ошибка отправки ответа: {e}", exc_info=True)
    return None

def part_to_dict(part: types.Part) -> dict:
    if part.text:
        return {'type': 'text', 'content': part.text}
    if part.file_data:
        return {'type': 'file', 'uri': part.file_data.file_uri, 'mime': part.file_data.mime_type}
    return {}

def dict_to_part(part_dict: dict) -> types.Part | None:
    if not isinstance(part_dict, dict): return None
    if part_dict.get('type') == 'text':
        return types.Part(text=part_dict.get('content', ''))
    if part_dict.get('type') == 'file':
        return types.Part(file_data=types.FileData(file_uri=part_dict['uri'], mime_type=part_dict['mime']))
    return None

async def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list[types.Part], **kwargs):
    chat_history = context.chat_data.setdefault("history", [])
    serializable_parts = [part_to_dict(p) for p in parts if part_to_dict(p)]
    if not serializable_parts: return
    entry = {"role": role, "parts": serializable_parts, **kwargs}
    chat_history.append(entry)
    if len(chat_history) > 40:
        context.chat_data["history"] = chat_history[-40:]
    await context.application.persistence.update_chat_data(context.chat_data.get('id'), context.chat_data)

def build_history_for_request(chat_history: list) -> list[types.Content]:
    valid_history, current_chars = [], 0
    for entry in reversed(chat_history):
        if entry.get("role") in ("user", "model") and isinstance(entry.get("parts"), list):
            api_parts = [p for p in (dict_to_part(part_dict) for part_dict in entry["parts"]) if p is not None]
            if not api_parts: continue
            entry_text_len = sum(len(p.text) for p in api_parts if p.text)
            if current_chars + entry_text_len > MAX_CONTEXT_CHARS:
                logger.info(f"Достигнут лимит контекста ({MAX_CONTEXT_CHARS} симв). История обрезана до {len(valid_history)} сообщений.")
                break
            clean_content = types.Content(role=entry["role"], parts=api_parts)
            valid_history.append(clean_content)
            current_chars += entry_text_len
    valid_history.reverse()
    return valid_history

async def upload_and_wait_for_file(client: genai.Client, file_bytes: bytes, mime_type: str, file_name: str) -> types.Part:
    logger.info(f"Загрузка файла '{file_name}' ({len(file_bytes) / 1024:.2f} KB) через File API...")
    uploaded_file_response = await client.aio.files.upload(
        file=io.BytesIO(file_bytes), config=types.UploadFileConfig(mime_type=mime_type, display_name=file_name)
    )
    logger.info(f"Файл '{file_name}' загружен. Имя: {uploaded_file_response.name}. Ожидание статуса ACTIVE...")
    for _ in range(15):
        file_state_response = await client.aio.files.get(name=uploaded_file_response.name)
        state = file_state_response.state.name
        if state == 'ACTIVE':
            logger.info(f"Файл '{file_name}' активен.")
            return types.Part(file_data=types.FileData(file_uri=uploaded_file_response.uri, mime_type=mime_type))
        if state == 'FAILED':
            raise IOError(f"Ошибка обработки файла '{file_name}' на сервере Google.")
        await asyncio.sleep(2)
    raise asyncio.TimeoutError(f"Файл '{file_name}' не стал активным за 30 секунд.")

# --- ПРОАКТИВНЫЙ ПОИСК (ВОЗВРАЩЕНО) ---
async def perform_proactive_search(query: str) -> str | None:
    try:
        logger.info(f"Выполняется проактивный поиск по запросу: '{query}'")
        results = await asyncio.to_thread(DDGS().text, keywords=query, region='ru-ru', max_results=3)
        if results:
            snippets = "\n".join(f"- {r['body']}" for r in results)
            logger.info("Проактивный поиск: Успешно получены сниппеты из DuckDuckGo.")
            return snippets
    except Exception as e:
        logger.warning(f"Проактивный DDG поиск не удался: {e}")
    return None

# --- ЯДРО ЛОГИКИ ---
async def generate_response(client: genai.Client, request_contents: list, context: ContextTypes.DEFAULT_TYPE, tools: list) -> str:
    chat_id = context.chat_data.get('id', 'Unknown')
    thinking_mode = get_user_setting(context, 'thinking_mode', 'auto')
    config = types.GenerateContentConfig(
        safety_settings=SAFETY_SETTINGS, 
        tools=tools,
        thinking_config=types.ThinkingConfig(thinking_budget=-1 if thinking_mode == 'auto' else 24576),
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)])
    )
    try:
        response = await client.aio.models.generate_content(model=MODEL_NAME, contents=request_contents, config=config)
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts and response.candidates[0].content.parts[0].function_call:
             function_call = response.candidates[0].content.parts[0].function_call
             if function_call.name == 'get_current_time_str':
                 args = function_call.args
                 result = get_current_time_str(timezone=args.get('timezone', 'Europe/Moscow'))
                 function_response_part = types.Part(function_response=types.FunctionResponse(name='get_current_time_str', response={'result': result}))
                 response = await client.aio.models.generate_content(
                     model=MODEL_NAME, 
                     contents=request_contents + [response.candidates[0].content, types.Content(parts=[function_response_part], role="tool")],
                     config=config
                 )
        logger.info(f"ChatID: {chat_id} | Ответ получен. Инструменты: {[type(t.to_proto()).__name__ for t in tools]}")
        return response.text
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Ошибка: {e}", exc_info=True)
        return f"❌ Ошибка модели: {str(e)[:250]}"

async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list):
    message, client = update.message, context.bot_data['gemini_client']
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    history = build_history_for_request(context.chat_data.get("history", []))
    
    final_content_parts = list(content_parts)
    request_contents = history + [types.Content(parts=final_content_parts, role="user")]
    
    has_media_in_context = any(p.file_data for content in request_contents for p in content.parts)
    tools = MEDIA_TOOLS if has_media_in_context else TEXT_TOOLS
    
    text_part_index = next((i for i, part in enumerate(final_content_parts) if part.text), -1)
    if text_part_index != -1:
        original_text = final_content_parts[text_part_index].text
        
        if get_user_setting(context, 'proactive_search', False) and not any(p.file_data for p in final_content_parts):
            search_results = await perform_proactive_search(original_text)
            search_context = f"\n\n--- Контекст из веба для справки ---\n{search_results}\n--------------------------\n" if search_results else ""
        else:
            search_context = ""

        date_context = f"(Текущая дата: {get_current_time_str()})\n"
        final_content_parts[text_part_index].text = f"{date_context}{search_context}{original_text}"

    try:
        reply_text = await generate_response(client, request_contents, context, tools)
        sent_message = await send_reply(message, reply_text)
        
        await add_to_history(context, role="user", parts=content_parts, message_id=message.message_id)
        if sent_message:
            await add_to_history(context, role="model", parts=[types.Part(text=reply_text)], bot_message_id=sent_message.message_id)
    except (IOError, asyncio.TimeoutError) as e:
        logger.error(f"Ошибка обработки файла для ChatID {message.chat_id}: {e}")
        await message.reply_text(f"❌ Ошибка обработки файла: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в process_request для ChatID {message.chat_id}: {e}", exc_info=True)
        await message.reply_text("❌ Произошла непредвиденная ошибка. Попробуйте еще раз.")

# --- ОБРАБОТЧИКИ КОМАНД И СООБЩЕНИЙ ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.setdefault('thinking_mode', 'auto')
    context.chat_data.setdefault('proactive_search', False)
    start_text = """Я - Женя, интеллект на Google Gemini 2.5 Flash

🌐 Использую интеллектуальный поиск Google в интернете, обладаю знаниями во всех сферах.
🧠 Размышляю над данными, сообщением и контекстом.
💬 Отвечаю по принципу 'Айсберга знаний' в позитивном стиле, иногда с юмором.

Отправляй сюда:
🎤 Голосовые и текст,
🎧 Аудиофайлы,
📸 Изображения,
📹 YouTube-видео,
🎞 Видеофайлы (до 50 мб),
🔗 Веб-страницы,
📑 Файлы PDF, TXT, JSON,
- делаю описание, расшифровку в текст, пересказ, ищу по содержимому и отвечаю на вопросы.

Пользуйтесь тут и добавляйте в группы!

(!) Используя бот, Вы автоматически соглашаетесь на передачу сообщений и файлов для получения ответов через Google Gemini API."""
    await update.message.reply_html(start_text)

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = get_user_setting(context, 'thinking_mode', 'auto')
    search = get_user_setting(context, 'proactive_search', False)
    keyboard = [
        [InlineKeyboardButton(f"Мышление: {'✅ ' if mode == 'auto' else ''}Авто", callback_data="set_thinking_auto"),
         InlineKeyboardButton(f"Мышление: {'✅ ' if mode == 'max' else ''}Максимум", callback_data="set_thinking_max")],
        [InlineKeyboardButton(f"Проактивный поиск: {'✅ Вкл' if search else '❌ Выкл'}", callback_data="toggle_proactive_search")]
    ]
    await update.message.reply_text("⚙️ Настройки:", reply_markup=InlineKeyboardMarkup(keyboard))

async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, data = update.callback_query, update.callback_query.data
    await query.answer()
    if data.startswith("set_thinking_"):
        set_user_setting(context, 'thinking_mode', data.replace("set_thinking_", ""))
    elif data == "toggle_proactive_search":
        set_user_setting(context, 'proactive_search', not get_user_setting(context, 'proactive_search', False))
        
    mode = get_user_setting(context, 'thinking_mode', 'auto')
    search = get_user_setting(context, 'proactive_search', False)
    keyboard = [
        [InlineKeyboardButton(f"Мышление: {'✅ ' if mode == 'auto' else ''}Авто", callback_data="set_thinking_auto"),
         InlineKeyboardButton(f"Мышление: {'✅ ' if mode == 'max' else ''}Максимум", callback_data="set_thinking_max")],
        [InlineKeyboardButton(f"Проактивный поиск: {'✅ Вкл' if search else '❌ Выкл'}", callback_data="toggle_proactive_search")]
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await context.application.persistence.drop_chat_data(update.effective_chat.id)
    await update.message.reply_text("История чата и связанные данные очищены.")
    
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, client = update.message, context.bot_data['gemini_client']
    user_text = message.caption or "Опиши это изображение."
    photo_file = await message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    file_part = await upload_and_wait_for_file(client, photo_bytes, 'image/jpeg', photo_file.file_unique_id + ".jpg")
    content_parts = [file_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, doc, client = update.message, update.message.document, context.bot_data['gemini_client']
    if doc.file_size > 50 * 1024 * 1024: return await message.reply_text("❌ Файл слишком большой (> 50 MB).")
    if doc.mime_type and doc.mime_type.startswith("audio/"):
        return await handle_audio(update, context, doc)
    await message.reply_text(f"Загружаю документ '{doc.file_name}'...", reply_to_message_id=message.message_id)
    doc_file = await doc.get_file()
    doc_bytes = await doc_file.download_as_bytearray()
    file_part = await upload_and_wait_for_file(client, doc_bytes, doc.mime_type, doc.file_name or "document")
    user_text = message.caption or "Проанализируй содержимое этого документа."
    content_parts = [file_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, video, client = update.message, update.message.video, context.bot_data['gemini_client']
    if video.file_size > 50 * 1024 * 1024: return await message.reply_text("❌ Видеофайл слишком большой (> 50 MB).")
    await message.reply_text("Загружаю видео...", reply_to_message_id=message.message_id)
    video_file = await video.get_file()
    video_bytes = await video_file.download_as_bytearray()
    video_part = await upload_and_wait_for_file(client, video_bytes, video.mime_type, video.file_name or "video.mp4")
    user_text = message.caption or "Опиши это видео и сделай краткий пересказ."
    content_parts = [video_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_source=None):
    message, client = update.message, context.bot_data['gemini_client']
    audio = audio_source or message.audio or message.voice
    if not audio: return
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    file_name = getattr(audio, 'file_name', 'voice_message.ogg')
    audio_file = await audio.get_file()
    audio_bytes = await audio_file.download_as_bytearray()
    audio_part = await upload_and_wait_for_file(client, audio_bytes, audio.mime_type, file_name)
    user_text = message.caption or "Расшифруй это аудио и ответь на его содержание. Если это просто фраза - дай на нее развернутый ответ."
    content_parts = [audio_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts)

async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, update.message.text or ""
    match = re.search(YOUTUBE_REGEX, text)
    if not match: return
    youtube_url = f"https://www.youtube.com/watch?v={match.group(1)}"
    await message.reply_text("Анализирую видео с YouTube...", reply_to_message_id=message.message_id)
    youtube_part = types.Part(file_data=types.FileData(mime_type="video/youtube", file_uri=youtube_url))
    user_prompt = text.replace(match.group(0), "").strip() or "Сделай краткий пересказ этого видео."
    content_parts = [youtube_part, types.Part(text=user_prompt)]
    await process_request(update, context, content_parts)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, update.message.text or ""
    prompt = f"Проанализируй содержимое по этой ссылке и ответь на мой вопрос: {text}"
    await process_request(update, context, [types.Part(text=prompt)])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, (update.message.text or "").strip()
    if not text: return
    context.chat_data['id'] = message.chat_id
    await process_request(update, context, [types.Part(text=text)])

async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await update.message.reply_text("🕰️ Уточняю время у модели...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    prompt = "Сколько сейчас времени?"
    if context.args: prompt += f" в { ' '.join(context.args) }"
    client = context.bot_data['gemini_client']
    request_contents = [types.Content(parts=[types.Part(text=prompt)], role="user")]
    reply_text = await generate_response(client, request_contents, context, tools=FUNCTION_CALLING_TOOLS)
    await message.edit_text(reply_text)

# --- ЗАПУСК БОТА ---
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
    application.bot_data['gemini_client'] = genai.Client(api_key=GOOGLE_API_KEY)
    
    commands = [
        BotCommand("start", "Инфо и начало работы"),
        BotCommand("config", "Настроить режим и поиск"),
        BotCommand("time", "Узнать точное время"),
        BotCommand("clear", "Очистить историю чата")
    ]
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("time", time_command))
    application.add_handler(CallbackQueryHandler(config_callback))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.VOICE, handle_audio))
    application.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_REGEX), handle_youtube_url))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(URL_REGEX), handle_url))
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
