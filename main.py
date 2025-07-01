# Версия 25.1 'Smart Context'
# 1. КРИТИЧЕСКОЕ АРХИТЕКТУРНОЕ ИЗМЕНЕНИЕ: Реализован двухуровневый "умный" контекст.
#    - Явный контекст: Ответ на сообщение в ветке диалога заставляет бота найти и применить исходный медиафайл этой ветки.
#    - Неявный контекст: Сообщение без ответа использует "липкий" контекст последнего проанализированного файла.
# 2. НОВАЯ ФУНКЦИЯ: Добавлена `find_media_context_in_history` для рекурсивного поиска контекста по цепочке ответов.
# 3. УЛУЧШЕНО: `handle_message` полностью переработан для поддержки новой логики.
# 4. СОХРАНЕНО: Все работающие механики (персонализация, "заземление", обработка файлов, легковесная история) сохранены и улучшены.

import logging
import os
import asyncio
import signal
import re
import pickle
from collections import defaultdict, OrderedDict
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

# --- КОНФИГУРАЦИЯ ---
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

# --- КОНСТАНТЫ И НАСТРОЙКИ ---
MODEL_NAME = 'gemini-2.5-flash'
YOUTUBE_REGEX = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
URL_REGEX = r'https?:\/\/[^\s/$.?#].[^\s]*'
MAX_CONTEXT_CHARS = 120000 
MAX_HISTORY_RESPONSE_LEN = 2000
MAX_HISTORY_ITEMS = 50
MAX_MEDIA_CONTEXTS = 10 

# --- ИНСТРУМЕНТЫ И ПРОМПТЫ ---
def get_current_time_str(timezone: str = "Europe/Moscow") -> str:
    return datetime.datetime.now(pytz.timezone(timezone)).strftime('%Y-%m-%d %H:%M:%S %Z')

TEXT_TOOLS = [types.Tool(google_search=types.GoogleSearch()), types.Tool(code_execution=types.ToolCodeExecution())]
MEDIA_TOOLS = [types.Tool(google_search=types.GoogleSearch())]
FUNCTION_CALLING_TOOLS = [types.Tool(function_declarations=[types.FunctionDeclaration(
    name='get_current_time_str', description="Gets the current date and time.",
    parameters=types.Schema(type=types.Type.OBJECT, properties={'timezone': types.Schema(type=types.Type.STRING)})
)])]
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
        last_exception = None
        for attempt in range(retries):
            conn = None
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
                if conn:
                    self.db_pool.putconn(conn, close=True)
                    conn = None
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    self._connect()
                continue
            finally:
                if conn: self.db_pool.putconn(conn)
        
        logger.error(f"Postgres: Не удалось выполнить запрос после {retries} попыток. Последняя ошибка: {last_exception}")
        if last_exception: raise last_exception

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
    async def drop_chat_data(self, chat_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        try:
            data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}
            chat_data.update(data)
        except psycopg2.Error as e:
            logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА БД: Не удалось обновить данные для чата {chat_id}. Ошибка: {e}")
    async def get_user_data(self) -> defaultdict[int, dict]: return defaultdict(dict)
    async def update_user_data(self, user_id: int, data: dict) -> None: pass
    async def drop_user_data(self, user_id: int) -> None: pass
    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass
    async def refresh_bot_data(self, bot_data: dict) -> None: pass
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
    if part.text: return {'type': 'text', 'content': part.text}
    if part.file_data: return {'type': 'file', 'uri': part.file_data.file_uri, 'mime': part.file_data.mime_type}
    return {}

def dict_to_part(part_dict: dict) -> types.Part | None:
    if not isinstance(part_dict, dict): return None
    if part_dict.get('type') == 'text': return types.Part(text=part_dict.get('content', ''))
    if part_dict.get('type') == 'file': return types.Part(file_data=types.FileData(file_uri=part_dict['uri'], mime_type=part_dict['mime']))
    return None

async def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list[types.Part], **kwargs):
    chat_history = context.chat_data.setdefault("history", [])
    
    processed_parts = []
    if role == 'model':
        text_part = next((p.text for p in parts if p.text), None)
        if text_part:
            text_to_save = (text_part[:MAX_HISTORY_RESPONSE_LEN] + "...") if len(text_part) > MAX_HISTORY_RESPONSE_LEN else text_part
            if "Загружаю" in text_part or "Анализирую" in text_part or len(text_to_save) > 500:
                 processed_parts.append(types.Part(text="[Был дан развернутый ответ на анализ файла]"))
            else:
                 processed_parts.append(types.Part(text=text_to_save))
    elif role == 'user':
        text_part = next((p.text for p in parts if p.text), None)
        if text_part:
            processed_parts.append(types.Part(text=text_part))

    serializable_parts = [part_to_dict(p) for p in processed_parts if p]
    if not serializable_parts: return

    entry = {"role": role, "parts": serializable_parts, **kwargs}
    chat_history.append(entry)
    if len(chat_history) > MAX_HISTORY_ITEMS:
        context.chat_data["history"] = chat_history[-MAX_HISTORY_ITEMS:]
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

def find_media_context_in_history(context: ContextTypes.DEFAULT_TYPE, reply_to_id: int) -> dict | None:
    history = context.chat_data.get("history", [])
    media_contexts = context.chat_data.get("media_contexts", {})
    
    current_reply_id = reply_to_id
    for _ in range(len(history)): # Ограничиваем глубину поиска
        # Сначала ищем ответ бота, на который ответили
        bot_message = next((msg for msg in reversed(history) if msg.get("role") == "model" and msg.get("bot_message_id") == current_reply_id), None)
        if bot_message and 'original_user_message_id' in bot_message:
            user_msg_id = bot_message['original_user_message_id']
            if user_msg_id in media_contexts:
                return media_contexts[user_msg_id]
            current_reply_id = user_msg_id # Продолжаем поиск вверх по цепочке
        else:
            # Если это ответ на сообщение пользователя напрямую
            if current_reply_id in media_contexts:
                return media_contexts[current_reply_id]
            break
    return None

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

async def generate_response(client: genai.Client, request_contents: list, context: ContextTypes.DEFAULT_TYPE, tools: list) -> str:
    # ... (код без изменений) ...

async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list, is_media_request: bool = False):
    message, client = update.message, context.bot_data['gemini_client']
    user = message.from_user
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    history = build_history_for_request(context.chat_data.get("history", []))
    tools = MEDIA_TOOLS if is_media_request else TEXT_TOOLS
    
    request_specific_parts = list(content_parts)
    text_part_index = next((i for i, part in enumerate(request_specific_parts) if part.text), -1)
    if text_part_index != -1:
        original_text = request_specific_parts[text_part_index].text
        
        if get_user_setting(context, 'proactive_search', True) and not is_media_request:
            search_results = await perform_proactive_search(original_text)
            search_context = f"\n\n--- Контекст из веба для справки ---\n{search_results}\n--------------------------\n" if search_results else ""
        else:
            search_context = ""

        user_prefix = f"[{user.id}; Name: {user.first_name}]: "
        date_prefix = f"(System Note: Today is {get_current_time_str()}. Verify facts using Google Search.)\n"
        request_specific_parts[text_part_index].text = f"{date_prefix}{search_context}{user_prefix}{original_text}"
    
    request_contents = history + [types.Content(parts=request_specific_parts, role="user")]

    try:
        reply_text = await generate_response(client, request_contents, context, tools)
        sent_message = await send_reply(message, reply_text)
        
        # Сохранение в историю и управление "липким" контекстом
        await add_to_history(context, role="user", parts=content_parts, original_message_id=message.message_id, reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None)
        if sent_message:
            await add_to_history(context, role="model", parts=[types.Part(text=reply_text)], original_message_id=message.message_id, bot_message_id=sent_message.message_id)
        
        media_part = next((p for p in content_parts if p.file_data), None)
        if media_part:
            media_contexts = context.chat_data.setdefault('media_contexts', OrderedDict())
            media_contexts[message.message_id] = part_to_dict(media_part)
            if len(media_contexts) > MAX_MEDIA_CONTEXTS:
                media_contexts.popitem(last=False) # Удаляем самый старый
            context.chat_data['last_media_context'] = media_contexts[message.message_id]
            logger.info(f"Сохранен медиа-контекст для msg_id {message.message_id}")
        elif not is_media_request and not message.reply_to_message:
            if 'last_media_context' in context.chat_data:
                del context.chat_data['last_media_context']
                logger.info(f"Очищен 'липкий' медиа-контекст для чата {message.chat_id}")

    except (IOError, asyncio.TimeoutError) as e:
        logger.error(f"Ошибка обработки файла: {e}", exc_info=False)
        await message.reply_text(f"❌ Ошибка обработки файла: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в process_request: {e}", exc_info=True)
        await message.reply_text("❌ Произошла внутренняя ошибка. Попробуйте еще раз.")


# --- ОБРАБОТЧИКИ КОМАНД ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await context.application.persistence.drop_chat_data(update.effective_chat.id)
    await update.message.reply_text("История чата и связанные данные очищены.")
    
async def transcript_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Пожалуйста, используйте эту команду в ответ на сообщение с аудио или видеофайлом.")
        return

    replied_message = update.message.reply_to_message
    media = replied_message.audio or replied_message.voice or replied_message.video
    if not media:
        await update.message.reply_text("В цитируемом сообщении нет аудио или видео для расшифровки.")
        return

    await update.message.reply_text("Начинаю расшифровку...", reply_to_message_id=update.message.message_id)
    client = context.bot_data['gemini_client']
    try:
        media_file = await media.get_file()
        media_bytes = await media_file.download_as_bytearray()
        file_part = await upload_and_wait_for_file(client, media_bytes, media.mime_type, getattr(media, 'file_name', 'media.bin'))
        
        prompt = "Transcribe this audio/video file. Return only the transcribed text, without any comments or introductory phrases."
        content_parts = [file_part, types.Part(text=prompt)]
        
        transcript_text = await generate_response(client, [types.Content(parts=content_parts, role="user")], context, tools=MEDIA_TOOLS)
        await update.message.reply_text(f"<b>Расшифровка:</b>\n\n{transcript_text}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в /transcript: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Не удалось выполнить транскрипцию: {e}")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_part: types.Part, user_text: str):
    context.chat_data.pop('last_media_context', None)
    content_parts = [file_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts, is_media_request=True)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_source=None):
    # ... (код без изменений) ...

async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (код без изменений) ...

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.pop('media_contexts', None)
    context.chat_data.pop('last_media_context', None)
    await process_request(update, context, [types.Part(text=update.message.text)])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, (update.message.text or "").strip()
    if not text: return
    context.chat_data['id'] = message.chat_id
    
    content_parts = [types.Part(text=text)]
    is_media_follow_up = False
    
    if message.reply_to_message:
        media_context = find_media_context_in_history(context, message.reply_to_message.message_id)
        if media_context:
            media_part = dict_to_part(media_context)
            if media_part:
                content_parts.insert(0, media_part)
                is_media_follow_up = True
                logger.info(f"Применен ЯВНЫЙ медиа-контекст (через reply) для чата {message.chat_id}")

    if not is_media_follow_up:
        last_media_context_dict = context.chat_data.get('last_media_context')
        if last_media_context_dict:
            media_part = dict_to_part(last_media_context_dict)
            if media_part:
                content_parts.insert(0, media_part)
                is_media_follow_up = True
                logger.info(f"Применен НЕЯВНЫЙ 'липкий' медиа-контекст для чата {message.chat_id}")

    await process_request(update, context, content_parts, is_media_request=is_media_follow_up)

# --- ЗАПУСК БОТА ---
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
        BotCommand("transcript", "Сделать транскрипцию (ответом на медиа)"),
        BotCommand("clear", "Очистить историю чата")
    ]
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("transcript", transcript_command))
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
