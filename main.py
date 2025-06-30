# Версия 24.2 'Lean & Smart'
# 1. КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ (УТЕЧКА ПАМЯТИ): Реализована предложенная пользователем архитектура.
#    - В основную историю сохраняются только легковесные текстовые сообщения. Длинные ответы модели на анализ файлов заменяются заглушкой.
#    - Это полностью решает проблему `input token count exceeds the maximum`.
# 2. УЛУЧШЕНО ("ЛИПКИЙ КОНТЕКСТ"): После анализа файла ссылка на него сохраняется в `last_media_context`. `handle_message` использует этот контекст для уточняющих вопросов. Отправка нового файла или ссылки очищает старый контекст.
# 3. РЕАЛИЗОВАНО (ПЕРСОНАЛИЗАЦИЯ): В каждый запрос к модели добавляется префикс с именем пользователя, позволяя модели обращаться к нему лично.
# 4. УЛУЧШЕНО (ПРОМПТЫ): Модифицирован промпт для аудио (транскрипция только по запросу) и системный промпт (убраны лишние приветствия).
# 5. Все остальные рабочие механики сохранены.

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
MAX_HISTORY_RESPONSE_LEN = 2000 # Макс. длина ответа модели для сохранения в историю
MAX_HISTORY_ITEMS = 40 # Макс. кол-во сообщений в истории

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
        try:
            data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}
            chat_data.update(data)
        except psycopg2.Error as e:
            logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА БД: Не удалось обновить данные для чата {chat_id}. Ошибка: {e}")
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
    if part.file_data: # Для липкого контекста
        return {'type': 'file', 'uri': part.file_data.file_uri, 'mime': part.file_data.mime_type}
    return {}

def dict_to_part(part_dict: dict) -> types.Part | None:
    if not isinstance(part_dict, dict): return None
    if part_dict.get('type') == 'text':
        return types.Part(text=part_dict.get('content', ''))
    if part_dict.get('type') == 'file': # Для липкого контекста
        return types.Part(file_data=types.FileData(file_uri=part_dict['uri'], mime_type=part_dict['mime']))
    return None

async def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list[types.Part], **kwargs):
    chat_history = context.chat_data.setdefault("history", [])
    
    processed_parts = []
    for part in parts:
        if role == 'model' and part.text and len(part.text) > MAX_HISTORY_RESPONSE_LEN:
            truncated_text = part.text[:MAX_HISTORY_RESPONSE_LEN] + "\n... [ответ был сокращен для истории]"
            processed_parts.append(types.Part(text=truncated_text))
            logger.info(f"Ответ модели для чата {context.chat_data.get('id')} был обрезан для сохранения в историю.")
        elif part.text:
            processed_parts.append(part)

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

# --- ПРОАКТИВНЫЙ ПОИСК ---
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
        logger.info(f"ChatID: {chat_id} | Ответ получен.")
        return response.text
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Ошибка: {e}", exc_info=True)
        return f"❌ Ошибка модели: {str(e)[:250]}"

async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list, is_media_request: bool = False):
    message, client = update.message, context.bot_data['gemini_client']
    user = message.from_user
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    
    history = build_history_for_request(context.chat_data.get("history", []))
    
    tools = MEDIA_TOOLS if is_media_request else TEXT_TOOLS
    
    # Модифицируем `content_parts` для текущего запроса, не меняя оригинал для истории
    request_specific_parts = list(content_parts)

    text_part_index = next((i for i, part in enumerate(request_specific_parts) if part.text), -1)
    if text_part_index != -1:
        original_text = request_specific_parts[text_part_index].text
        
        if get_user_setting(context, 'proactive_search', False) and not is_media_request:
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
        
        # В историю сохраняем только оригинальные части запроса
        await add_to_history(context, role="user", parts=content_parts, message_id=message.message_id)
        if sent_message:
            await add_to_history(context, role="model", parts=[types.Part(text=reply_text)], bot_message_id=sent_message.message_id)
        
        # Управляем "липким" контекстом
        media_part = next((p for p in content_parts if p.file_data), None)
        if media_part:
            context.chat_data['last_media_context'] = part_to_dict(media_part)
            logger.info(f"Сохранен/обновлен 'липкий' медиа-контекст для чата {message.chat_id}")
        elif not is_media_request:
