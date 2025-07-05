# Версия 11.2 (с твоими правками)

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
import html
from functools import wraps

import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand, User
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence
from telegram.error import BadRequest

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

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
    logger.critical("Критическая ошибка: не заданы все необходимые переменные окружения для базовой работы!")
    exit(1)

# --- КОНСТАНТЫ И НАСТРОЙКИ ---
MODEL_NAME = 'gemini-2.5-flash'
YOUTUBE_REGEX = r'(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/|shorts\/)|youtu\.be\/|youtube-nocookie\.com\/embed\/)([a-zA-Z0-9_-]{11})'
URL_REGEX = r'https?:\/\/[^\s/$.?#].[^\s]*'
DATE_TIME_REGEX = r'^\s*(какой\s+)?(день|дата|число|время|который\s+час)\??\s*$'
MAX_CONTEXT_CHARS = 150000
MAX_HISTORY_RESPONSE_LEN = 3000
MAX_HISTORY_ITEMS = 50
MAX_MEDIA_CONTEXTS = 10
MEDIA_CONTEXT_TTL_SECONDS = 47 * 3600
TELEGRAM_FILE_LIMIT_MB = 20

# --- ИНСТРУМЕНТЫ И ПРОМПТЫ ---
TEXT_TOOLS = [types.Tool(google_search=types.GoogleSearch(), code_execution=types.ToolCodeExecution(), url_context=types.UrlContext())]
MEDIA_TOOLS = [types.Tool(google_search=types.GoogleSearch())] 

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
    SYSTEM_INSTRUCTION = """(System Note: Today is {current_time}.)
    ВАЖНОЕ КРИТИЧЕСКОЕ ПРАВИЛО: Твоя внутренняя память устарела. Не отвечай на основе памяти, если вопрос подразумевает факты (события, личности, даты, статистика и т.д.) и любые данные, которые могут меняться со временем. Ты ОБЯЗАН ВСЕГДА АКТИВНО использовать инструмент Grounding with Google Search. Не анонсируй свои внутренние действия. Выполняй их в скрытом режиме.
    АБСОЛЮТНЫЕ ЗАПРЕТЫ: НИКОГДА не показывай `tool_code`, `thought` или другие внутренние рассуждения. НИКОГДА не начинай ответ с префикса пользователя (например, `[12345; Name: User]:`). Отвечай только по существу.
    """

# --- КЛАСС PERSISTENCE ---
class PostgresPersistence(BasePersistence):
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None
        self.dsn = database_url
        self._connect_with_retry()

    def _connect_with_retry(self, retries=5, delay=5):
        for attempt in range(retries):
            try:
                self._connect()
                self._initialize_db()
                logger.info("PostgresPersistence: Успешное подключение к БД.")
                return
            except psycopg2.Error as e:
                logger.error(f"PostgresPersistence: Не удалось подключиться к БД (попытка {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
                else:
                    raise

    def _connect(self):
        if self.db_pool and not self.db_pool.closed:
            self.db_pool.closeall()
        dsn = self.dsn
        keepalive_options = "keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=5"
        if "?" in dsn:
            if "keepalives" not in dsn: dsn = f"{dsn}&{keepalive_options}"
        else:
            dsn = f"{dsn}?{keepalive_options}"
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
                    try: self.db_pool.putconn(conn, close=True)
                    except psycopg2.pool.PoolError: logger.warning("Postgres: Не удалось вернуть 'сломанное' соединение в пул.")
                    conn = None
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
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
    async def get_bot_data(self) -> dict: return defaultdict(dict)
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
def get_current_time_str(timezone: str = "Europe/Moscow") -> str:
    now = datetime.datetime.now(pytz.timezone(timezone))
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    day_of_week = days[now.weekday()]
    return f"Сегодня {day_of_week}, {now.day} {months[now.month-1]} {now.year} года, время {now.strftime('%H:%M')} (MSK)."

def html_safe_chunker(text_to_chunk: str, chunk_size: int = 4096) -> list[str]:
    chunks, tag_stack, remaining_text = [], [], text_to_chunk
    tag_regex = re.compile(r'<(/?)(b|i|code|pre|a|tg-spoiler|br)>', re.IGNORECASE)
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

def ignore_if_processing(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update or not update.effective_message:
            return await func(update, context, *args, **kwargs)

        message_id = update.effective_message.message_id
        chat_id = update.effective_chat.id
        processing_key = f"{chat_id}_{message_id}"
        
        processing_messages = context.application.bot_data.setdefault('processing_messages', set())

        if processing_key in processing_messages:
            logger.warning(f"Сообщение {processing_key} уже в обработке. Новый запрос проигнорирован.")
            return

        processing_messages.add(processing_key)
        try:
            await func(update, context, *args, **kwargs)
        finally:
            processing_messages.discard(processing_key)
            
    return wrapper

def part_to_dict(part: types.Part) -> dict:
    if part.text: return {'type': 'text', 'content': part.text}
    if part.file_data: return {'type': 'file', 'uri': part.file_data.file_uri, 'mime': part.file_data.mime_type, 'timestamp': time.time()}
    return {}

def dict_to_part(part_dict: dict) -> types.Part | None:
    if not isinstance(part_dict, dict): return None
    if part_dict.get('type') == 'text': return types.Part(text=part_dict.get('content', ''))
    if part_dict.get('type') == 'file':
        if time.time() - part_dict.get('timestamp', 0) > MEDIA_CONTEXT_TTL_SECONDS:
            logger.info(f"Медиа-контекст {part_dict.get('uri')} протух и будет проигнорирован.")
            return None
        return types.Part(file_data=types.FileData(file_uri=part_dict['uri'], mime_type=part_dict['mime']))
    return None

def build_history_for_request(chat_history: list) -> list[types.Content]:
    valid_history, current_chars = [], 0
    for entry in reversed(chat_history):
        if entry.get("role") in ("user", "model") and isinstance(entry.get("parts"), list):
            entry_api_parts = []
            entry_text_len = 0
            if entry.get("role") == "user":
                user_id = entry.get('user_id', 'Unknown')
                user_name = entry.get('user_name', 'User')
                user_prefix = f"[{user_id}; Name: {user_name}]: "
                
                for part_dict in entry["parts"]:
                    # В историю для API отправляется только текст
                    if part_dict.get('type') == 'text':
                        prefixed_text = f"{user_prefix}{part_dict.get('content', '')}"
                        entry_api_parts.append(types.Part(text=prefixed_text))
                        entry_text_len += len(prefixed_text)
            else: # model
                for part_dict in entry["parts"]:
                    if part_dict.get('type') == 'text':
                        text = part_dict.get('content', '')
                        entry_api_parts.append(types.Part(text=text))
                        entry_text_len += len(text)

            if not entry_api_parts: continue
            
            if current_chars + entry_text_len > MAX_CONTEXT_CHARS:
                logger.info(f"Достигнут лимит контекста ({MAX_CONTEXT_CHARS} симв). История обрезана до {len(valid_history)} сообщений.")
                break

            clean_content = types.Content(role=entry["role"], parts=entry_api_parts)
            valid_history.append(clean_content)
            current_chars += entry_text_len
            
    valid_history.reverse()
    return valid_history

def find_media_context_in_history(context: ContextTypes.DEFAULT_TYPE, reply_to_id: int) -> dict | None:
    chat_id = context.effective_chat.id
    history = context.chat_data.get("history", [])
    all_media_contexts = context.application.bot_data.setdefault('media_contexts', {})
    chat_media_contexts = all_media_contexts.get(chat_id, {})
    
    current_reply_id = reply_to_id
    for _ in range(len(history)):
        bot_message = next((msg for msg in reversed(history) if msg.get("role") == "model" and msg.get("bot_message_id") == current_reply_id), None)
        if bot_message and 'original_message_id' in bot_message:
            user_msg_id = bot_message['original_message_id']
            if user_msg_id in chat_media_contexts:
                media_context = chat_media_contexts[user_msg_id]
                if time.time() - media_context.get('timestamp', 0) < MEDIA_CONTEXT_TTL_SECONDS:
                    return media_context
                else:
                    logger.info(f"Найденный медиа-контекст для msg_id {user_msg_id} протух.")
                    return None
            current_reply_id = user_msg_id
        else:
            if current_reply_id in chat_media_contexts:
                media_context = chat_media_contexts[current_reply_id]
                if time.time() - media_context.get('timestamp', 0) < MEDIA_CONTEXT_TTL_SECONDS:
                    return media_context
                else:
                    logger.info(f"Найденный медиа-контекст для msg_id {current_reply_id} протух.")
                    return None
            break
    return None

async def upload_and_wait_for_file(client: genai.Client, file_bytes: bytes, mime_type: str, file_name: str) -> types.Part:
    logger.info(f"Загрузка файла '{file_name}' ({len(file_bytes) / 1024:.2f} KB) через File API...")
    try:
        upload_config = types.UploadFileConfig(mime_type=mime_type, display_name=file_name)
        upload_response = await client.aio.files.upload(
            file=io.BytesIO(file_bytes),
            config=upload_config
        )
        logger.info(f"Файл '{file_name}' загружен. Имя: {upload_response.name}. Ожидание статуса ACTIVE...")
        
        file_response = await client.aio.files.get(name=upload_response.name)
        
        for _ in range(15):
            if file_response.state.name == 'ACTIVE':
                logger.info(f"Файл '{file_name}' активен.")
                return types.Part(file_data=types.FileData(file_uri=file_response.uri, mime_type=mime_type))
            if file_response.state.name == 'FAILED':
                raise IOError(f"Ошибка обработки файла '{file_name}' на сервере Google.")
            await asyncio.sleep(2)
            file_response = await client.aio.files.get(name=upload_response.name)

        raise asyncio.TimeoutError(f"Файл '{file_name}' не стал активным за 30 секунд.")

    except Exception as e:
        logger.error(f"Ошибка при загрузке файла через File API: {e}", exc_info=True)
        raise IOError(f"Не удалось загрузить или обработать файл '{file_name}' на сервере Google.")

async def generate_response(client: genai.Client, request_contents: list, context: ContextTypes.DEFAULT_TYPE, tools: list) -> types.GenerateContentResponse | str:
    chat_id = context.chat_data.get('id', 'Unknown')
    
    try:
        final_system_instruction = SYSTEM_INSTRUCTION.format(current_time=get_current_time_str())
    except KeyError:
        logger.warning("В system_prompt.md отсутствует плейсхолдер {current_time}. Дата не будет подставлена.")
        final_system_instruction = SYSTEM_INSTRUCTION

    config = types.GenerateContentConfig(
        safety_settings=SAFETY_SETTINGS, 
        tools=tools,
        system_instruction=types.Content(parts=[types.Part(text=final_system_instruction)]),
        temperature=1.0,
        thinking_config=types.ThinkingConfig(thinking_budget=24576) # Максимальный бюджет на мышление
    )
    
    try:
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=request_contents,
            config=config
        )
        logger.info(f"ChatID: {chat_id} | Ответ от Gemini API получен.")
        return response
    except genai_errors.APIError as e:
        error_text = str(e).lower()
        logger.error(f"ChatID: {chat_id} | Ошибка Google API: {e}", exc_info=False)
        
        if "input token count" in error_text and "exceeds the maximum" in error_text:
            return "🤯 <b>Слишком длинная история!</b>\nКажется, мы заболтались, и я уже не могу удержать в голове весь наш диалог. Пожалуйста, очистите историю командой /clear, чтобы начать заново."
        
        if "resource has been exhausted" in error_text:
            return "⏳ <b>Слишком много запросов!</b>\nПожалуйста, подождите минуту, я немного перегрузилась."

        if "permission denied" in error_text:
            return "❌ <b>Ошибка доступа к файлу.</b>\nВозможно, файл был удален с серверов Google (срок хранения 48 часов) или возникла другая проблема. Попробуйте отправить файл заново."

        return f"❌ <b>Ошибка Google API:</b>\n<code>{html.escape(str(e))}</code>"
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Неизвестная ошибка генерации: {e}", exc_info=True)
        return f"❌ <b>Произошла внутренняя ошибка:</b>\n<code>{html.escape(str(e))}</code>"

def format_gemini_response(response: types.GenerateContentResponse) -> str:
    try:
        if not response or not response.candidates:
            logger.warning("Получен пустой или некорректный ответ от API (нет кандидатов).")
            return "Я не смогла сформировать ответ. Попробуйте еще раз."
            
        candidate = response.candidates[0]
        if candidate.finish_reason.name == "SAFETY":
            logger.warning("Ответ заблокирован по соображениям безопасности.")
            return "Мой ответ был заблокирован из-за внутренних правил безопасности. Пожалуйста, переформулируйте запрос."

        if not candidate.content or not candidate.content.parts:
            logger.warning("Получен пустой или некорректный ответ от API (нет частей контента).")
            return "Я не смогла сформировать ответ. Попробуйте еще раз."
            
        text_parts = [part.text for part in candidate.content.parts if part.text is not None]
        
        if not text_parts:
            logger.warning("В ответе модели не найдено текстовых частей.")
            return "Я получила нетекстовый ответ, который не могу отобразить."

        full_text = "".join(text_parts)
        
        sanitized_text = re.sub(r'tool_code\n.*?thought\n', '', full_text, flags=re.DOTALL)
        user_prefix_pattern = r'\[\d+;\s*Name:\s*.*?\]:\s*'
        sanitized_text = re.sub(user_prefix_pattern, '', sanitized_text)
        
        return sanitized_text.strip()
        
    except (AttributeError, IndexError) as e:
        logger.error(f"Ошибка при парсинге ответа Gemini: {e}", exc_info=True)
        return "Произошла ошибка при обработке ответа от нейросети."

async def send_reply(target_message: Message, response_text: str, add_context_hint: bool = False) -> Message | None:
    sanitized_text = re.sub(r'<br\s*/?>', '\n', response_text)
    chunks = html_safe_chunker(sanitized_text)
    
    if add_context_hint:
        hint = "\n\n<i>💡 Чтобы задать вопрос по этому файлу, ответьте на это сообщение.</i>"
        if len(chunks[-1]) + len(hint) <= 4096:
            chunks[-1] += hint
        else:
            chunks.append(hint)
            
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
            plain_chunks = [plain_text[i:i+4096] for i in range(0, len(plain_text), 4096)]
            for i, chunk in enumerate(plain_chunks):
                if i == 0: sent_message = await target_message.reply_text(chunk)
                else: sent_message = await target_message.get_bot().send_message(chat_id=target_message.chat_id, text=chunk)
            return sent_message
    except Exception as e: logger.error(f"Критическая ошибка отправки ответа: {e}", exc_info=True)
    return None

async def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list[types.Part], user: User = None, **kwargs):
    chat_history = context.chat_data.setdefault("history", [])
    
    entry_parts = []
    # В постоянную историю сохраняем только текст
    for part in parts:
        if part.text:
            entry_parts.append(part_to_dict(part))

    if not entry_parts: return # Не сохраняем в историю сообщения без текста
            
    entry = {"role": role, "parts": entry_parts, **kwargs}
    if role == 'user' and user:
        entry['user_id'] = user.id
        entry['user_name'] = user.first_name
    
    chat_history.append(entry)
    if len(chat_history) > MAX_HISTORY_ITEMS:
        context.chat_data["history"] = chat_history[-MAX_HISTORY_ITEMS:]

async def process_request(update: Update, context: ContextTypes.DEFAULT_TYPE, content_parts: list, is_media_request: bool = False):
    message, client = update.message, context.bot_data['gemini_client']
    user = message.from_user
    chat_id = message.chat_id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Шаг 1: Предварительная проверка на запрос времени/даты. Вынесена из try-except.
    text_part_content = next((p.text for p in content_parts if p.text), None)
    if text_part_content and re.search(DATE_TIME_REGEX, text_part_content, re.IGNORECASE):
        logger.info("Обнаружен запрос о времени/дате. Отвечаем напрямую.")
        time_str = get_current_time_str()
        response_text = f"{user.first_name}, {time_str[0].lower()}{time_str[1:]}"
        sent_message = await send_reply(message, response_text)
        if sent_message:
            await add_to_history(context, role="user", parts=content_parts, user=user, original_message_id=message.message_id)
            await add_to_history(context, role="model", parts=[types.Part(text=response_text)], original_message_id=message.message_id, bot_message_id=sent_message.message_id)
        return # Завершаем выполнение здесь

    # Шаг 2: Основная логика в блоке try-except
    try:
        history_for_api = build_history_for_request(context.chat_data.get("history", []))
        
        user_prefix = f"[{user.id}; Name: {user.first_name}]: "
        prompt_text = next((p.text for p in content_parts if p.text), "")
        
        has_media = any(p.file_data for p in content_parts)

        if not has_media:
            grounding_instruction = """
ВАЖНОЕ КРИТИЧЕСКОЕ ПРАВИЛО: Твоя внутренняя память устарела. Не отвечай на основе памяти, если вопрос подразумевает факты (события, личности, даты, статистика и т.д.). Ты ОБЯЗАН ВСЕГДА АКТИВНО использовать инструмент Grounding with Google Search. Тебе уже предоставлены точная дата и время в System Note, используй эти данные и не пытайся вычислять их самостоятельно. Не анонсируй свои внутренние действия, а выполняй их в скрытом режиме.
"""
            final_prompt_text = f"{grounding_instruction}\n{user_prefix}{prompt_text}"
        else:
            final_prompt_text = f"{user_prefix}{prompt_text}"

        current_request_parts = []
        text_part_found = False
        for part in content_parts:
            if part.text and not text_part_found:
                if final_prompt_text:
                    current_request_parts.append(types.Part(text=final_prompt_text))
                text_part_found = True
            elif not part.text:
                current_request_parts.append(part)

        request_contents = history_for_api + [types.Content(parts=current_request_parts, role="user")]
        
        tools = MEDIA_TOOLS if is_media_request else TEXT_TOOLS
        response_obj = await generate_response(client, request_contents, context, tools)
        
        if isinstance(response_obj, str):
            reply_text = response_obj
        else:
            reply_text = format_gemini_response(response_obj)
        
        if len(reply_text) > MAX_HISTORY_RESPONSE_LEN:
            full_response_for_history = reply_text[:MAX_HISTORY_RESPONSE_LEN] + "..."
            logger.info(f"Ответ модели для чата {chat_id} был обрезан для сохранения в историю.")
        else:
            full_response_for_history = reply_text

        sent_message = await send_reply(message, reply_text, add_context_hint=is_media_request)
        
        if sent_message:
            await add_to_history(context, role="user", parts=content_parts, user=user, original_message_id=message.message_id)
            await add_to_history(context, role="model", parts=[types.Part(text=full_response_for_history)], original_message_id=message.message_id, bot_message_id=sent_message.message_id)
            
            if is_media_request:
                media_part = next((p for p in content_parts if p.file_data), None)
                if media_part:
                    all_media_contexts = context.application.bot_data.setdefault('media_contexts', {})
                    chat_media_contexts = all_media_contexts.setdefault(chat_id, OrderedDict())
                    
                    chat_media_contexts[message.message_id] = part_to_dict(media_part)
                    if len(chat_media_contexts) > MAX_MEDIA_CONTEXTS: chat_media_contexts.popitem(last=False)
                    logger.info(f"Сохранен сессионный медиа-контекст для msg_id {message.message_id} в чате {chat_id}")
            
            await context.application.persistence.update_chat_data(chat_id, context.chat_data)
        else:
            logger.error(f"Не удалось отправить ответ для msg_id {message.message_id}. История не будет сохранена, чтобы избежать повреждения.")

    except (IOError, asyncio.TimeoutError) as e:
        logger.error(f"Ошибка обработки файла: {e}", exc_info=False)
        await message.reply_text(f"❌ <b>Ошибка обработки файла:</b> {html.escape(str(e))}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка в process_request: {e}", exc_info=True)
        await message.reply_text(f"❌ <b>Произошла критическая внутренняя ошибка:</b>\n<code>{html.escape(str(e))}</code>")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_text = """Я - Женя, интеллект новой Google Gemini 2.5 Flash с лучшим поиском:

🌐 Обладаю глубокими знаниями во всех сферах и умно использую Google.
🧠 Анализирую и размышляю над сообщением, контекстом и всеми знаниями.
💬 Отвечу на любые вопросы в понятном и приятном стиле, иногда с юмором. Могу сделать описание/конспект, расшифровку, искать по содержимому.

Принимаю и понимаю:
✉️ Текстовые, 🎤 Голосовые и 🎧 Аудиофайлы,
📸 Изображения, 🎞 Видео (до 50 мб), 📹 ссылки на YouTube, 
🔗 Веб-страницы,📑 Файлы PDF, TXT, JSON.

Пользуйтесь и добавляйте в свои группы!

(!) Используя бот, Вы автоматически соглашаетесь на передачу сообщений и файлов для получения ответов через Google Gemini API."""
    await update.message.reply_html(start_text)

@ignore_if_processing
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        chat_id = update.effective_chat.id
        
        context.chat_data.clear()
        
        bot_data = context.application.bot_data
        bot_data.get('media_contexts', {}).pop(chat_id, None)
        
        await context.application.persistence.update_chat_data(chat_id, context.chat_data)
        
        await update.message.reply_text("✅ История чата и весь медиа-контекст полностью очищены.")
        logger.info(f"Полная очистка контекста для чата {chat_id} по команде /clear.")
    else:
        logger.warning("Не удалось определить chat_id для команды /clear")

@ignore_if_processing
async def newtopic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        chat_id = update.effective_chat.id
        bot_data = context.application.bot_data
        bot_data.get('media_contexts', {}).pop(chat_id, None)
        await update.message.reply_text("Контекст предыдущих файлов очищен. Начинаем новую тему.")

@ignore_if_processing
async def utility_media_command(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    if not update.message or not update.message.reply_to_message:
        return await update.message.reply_text("Пожалуйста, используйте эту команду в ответ на сообщение с медиафайлом или ссылкой.")
    
    context.chat_data['id'] = update.effective_chat.id
    replied_message = update.message.reply_to_message
    media_obj = replied_message.audio or replied_message.voice or replied_message.video or replied_message.photo or replied_message.document
    
    media_part = None
    client = context.bot_data['gemini_client']
    
    try:
        if media_obj:
            if hasattr(media_obj, 'file_size') and media_obj.file_size > TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
                return await update.message.reply_text(f"❌ Файл слишком большой (> {TELEGRAM_FILE_LIMIT_MB} MB) для обработки этой командой.")
            media_file = await media_obj.get_file()
            media_bytes = await media_file.download_as_bytearray()
            media_part = await upload_and_wait_for_file(client, media_bytes, media_obj.mime_type, getattr(media_obj, 'file_name', 'media.bin'))
        elif replied_message.text:
            yt_match = re.search(YOUTUBE_REGEX, replied_message.text)
            if yt_match:
                youtube_url = f"https://www.youtube.com/watch?v={yt_match.group(1)}"
                media_part = types.Part(file_data=types.FileData(mime_type="video/youtube", file_uri=youtube_url))
            else:
                return await update.message.reply_text("В цитируемом сообщении нет поддерживаемого медиафайла или YouTube-ссылки.")
        else:
            return await update.message.reply_text("Не удалось найти медиафайл в цитируемом сообщении.")

        await update.message.reply_text("Анализирую...", reply_to_message_id=update.message.message_id)
        
        content_parts = [media_part, types.Part(text=prompt)]
        
        response_obj = await generate_response(client, [types.Content(parts=content_parts, role="user")], context, MEDIA_TOOLS)
        result_text = format_gemini_response(response_obj) if not isinstance(response_obj, str) else response_obj
        await send_reply(update.message, result_text, add_context_hint=True)
    
    except BadRequest as e:
        if "File is too big" in str(e):
             await update.message.reply_text(f"❌ Файл слишком большой (> {TELEGRAM_FILE_LIMIT_MB} MB) для обработки.")
        else:
             logger.error(f"Ошибка BadRequest в утилитарной команде: {e}", exc_info=True)
             await update.message.reply_text(f"❌ Произошла ошибка Telegram: {e}")
    except Exception as e:
        logger.error(f"Ошибка в утилитарной команде: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Не удалось выполнить команду: {e}")

@ignore_if_processing
async def transcript_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await utility_media_command(update, context, "Transcribe this audio/video file. Return only the transcribed text, without any comments or introductory phrases.")

@ignore_if_processing
async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await utility_media_command(update, context, "Summarize this material in a few paragraphs. Provide a concise but comprehensive overview.")

@ignore_if_processing
async def keypoints_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await utility_media_command(update, context, "Extract the key points or main theses from this material. Present them as a structured bulleted list.")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_media_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_part: types.Part, user_text: str):
    content_parts = [file_part, types.Part(text=user_text)]
    await process_request(update, context, content_parts, is_media_request=True)

@ignore_if_processing
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.photo: return
    
    context.chat_data['id'] = message.chat_id
    
    photo = message.photo[-1]
    if photo.file_size > TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
        await message.reply_text(f"🖼️ Изображение слишком большое (> {TELEGRAM_FILE_LIMIT_MB} MB), я не могу его проанализировать, но сейчас отвечу на текстовую часть сообщения, если она есть.")
        if message.caption:
            await handle_message(update, context, custom_text=message.caption)
        return

    try:
        photo_file = await photo.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        file_part = await upload_and_wait_for_file(context.bot_data['gemini_client'], photo_bytes, 'image/jpeg', photo_file.file_unique_id + ".jpg")
        await handle_media_request(update, context, file_part, message.caption or "В ПЕРВУЮ ОЧЕРЕДЬ проанализируй содержимое этого изображения. Лаконично перескажи, что на нем, и ответь на вопросы, если они подразумеваются. ПОСЛЕ ЭТОГО выскажи свое мнение.")
    except (BadRequest, IOError) as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await message.reply_text(f"❌ Ошибка обработки изображения: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке изображения: {e}", exc_info=True)
        await message.reply_text("❌ Произошла внутренняя ошибка при обработке изображения.")

@ignore_if_processing
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document: return
    
    context.chat_data['id'] = message.chat_id
    doc = message.document
    
    if doc.file_size > TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
        await message.reply_text(f"📑 Файл больше {TELEGRAM_FILE_LIMIT_MB} МБ, я не могу его скачать. Отвечу на текст, если он есть.")
        if message.caption:
            await handle_message(update, context, custom_text=message.caption)
        return

    if doc.mime_type and doc.mime_type.startswith("audio/"):
        return await handle_audio(update, context, doc)
    
    await message.reply_text(f"Загружаю документ '{doc.file_name}'...", reply_to_message_id=message.id)
    try:
        doc_file = await doc.get_file()
        doc_bytes = await doc_file.download_as_bytearray()
        file_part = await upload_and_wait_for_file(context.bot_data['gemini_client'], doc_bytes, doc.mime_type, doc.file_name or "document")
        await handle_media_request(update, context, file_part, message.caption or "В ПЕРВУЮ ОЧЕРЕДЬ проанализируй содержимое этого документа. Лаконично перескажи его суть и ответь на вопросы, если они подразумеваются. ПОСЛЕ ЭТОГО выскажи свое мнение.")
    except (BadRequest, IOError) as e:
        logger.error(f"Ошибка при обработке документа: {e}")
        await message.reply_text(f"❌ Ошибка обработки документа: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке документа: {e}", exc_info=True)
        await message.reply_text("❌ Внутренняя ошибка при обработке документа.")

@ignore_if_processing
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.video: return

    context.chat_data['id'] = message.chat_id
    video = message.video

    if video.file_size > TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
        await message.reply_text(f"📹 Видеофайл больше {TELEGRAM_FILE_LIMIT_MB} МБ, я не могу его скачать. Отвечу на текст, если он есть.")
        if message.caption:
            await handle_message(update, context, custom_text=message.caption)
        return
    
    await message.reply_text("Загружаю видео...", reply_to_message_id=message.id)
    try:
        video_file = await video.get_file()
        video_bytes = await video_file.download_as_bytearray()
        video_part = await upload_and_wait_for_file(context.bot_data['gemini_client'], video_bytes, video.mime_type, video.file_name or "video.mp4")
        await handle_media_request(update, context, video_part, message.caption or "В ПЕРВУЮ ОЧЕРЕДЬ проанализируй содержимое этого видео. Лаконично перескажи его суть и ответь на вопросы, если они подразумеваются. ПОСЛЕ ЭТОГО выскажи свое мнение. Не вставляй транскрипт и таймкоды, если я не просил.")
    except (BadRequest, IOError) as e:
        logger.error(f"Ошибка при обработке видео: {e}")
        await message.reply_text(f"❌ Ошибка обработки видео: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке видео: {e}", exc_info=True)
        await message.reply_text("❌ Внутренняя ошибка при обработке видео.")

@ignore_if_processing
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_source=None):
    message = update.message
    if not message: return
    
    context.chat_data['id'] = message.chat_id
    audio = audio_source or message.audio or message.voice
    if not audio: return

    if audio.file_size > TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
         await message.reply_text(f"🎧 Аудиофайл больше {TELEGRAM_FILE_LIMIT_MB} МБ, я не могу его скачать. Отвечу на текст, если он есть.")
         if message.caption:
            await handle_message(update, context, custom_text=message.caption)
         return

    file_name = getattr(audio, 'file_name', 'voice_message.ogg')
    user_text = message.caption or "В ПЕРВУЮ ОЧЕРЕДЬ проанализируй и ответь на это голосовое сообщение. ПОСЛЕ ЭТОГО выскажи свое мнение. Не вставляй транскрипт и таймкоды, если я не просил."
    
    try:
        audio_file = await audio.get_file()
        audio_bytes = await audio_file.download_as_bytearray()
        audio_part = await upload_and_wait_for_file(context.bot_data['gemini_client'], audio_bytes, audio.mime_type, file_name)
        await handle_media_request(update, context, audio_part, user_text)
    except (BadRequest, IOError) as e:
        logger.error(f"Ошибка при обработке аудио: {e}")
        await message.reply_text(f"❌ Ошибка обработки аудио: {e}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при обработке аудио: {e}", exc_info=True)
        await message.reply_text("❌ Внутренняя ошибка при обработке аудио.")

@ignore_if_processing
async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, text = update.message, update.message.text or ""
    
    context.chat_data['id'] = message.chat_id
    match = re.search(YOUTUBE_REGEX, text)
    if not match: return
    
    youtube_url = f"https://www.youtube.com/watch?v={match.group(1)}"
    await message.reply_text("Анализирую видео с YouTube...", reply_to_message_id=message.id)
    try:
        youtube_part = types.Part(file_data=types.FileData(mime_type="video/youtube", file_uri=youtube_url))
        
        user_prompt = text.replace(match.group(0), "").strip() or "В ПЕРВУЮ ОЧЕРЕДЬ проанализируй видео по этой ссылке. Лаконично перескажи его суть и ответь на вопросы, если они подразумеваются. ПОСЛЕ ЭТОГО выскажи свое мнение. Не вставляй транскрипт и таймкоды, если я не просил."
        
        await handle_media_request(update, context, youtube_part, user_prompt)
    except Exception as e:
        logger.error(f"Ошибка при обработке YouTube URL {youtube_url}: {e}", exc_info=True)
        await message.reply_text("❌ Не удалось обработать ссылку на YouTube. Возможно, видео недоступно или имеет ограничения.")

@ignore_if_processing
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    context.chat_data['id'] = message.chat_id
    await process_request(update, context, [types.Part(text=message.text)])

@ignore_if_processing
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, custom_text: str = None):
    message = update.message
    if not message or not message.from_user: return
    
    text = custom_text or (message.text or "").strip()
    if not text: return
        
    chat_id = message.chat_id
    context.chat_data['id'] = chat_id
    
    content_parts = [types.Part(text=text)]
    is_media_request = False
    
    if custom_text is None and message.reply_to_message:
        media_context = find_media_context_in_history(context, message.reply_to_message.message_id)
        if media_context:
            media_part = dict_to_part(media_context)
            if media_part:
                content_parts.insert(0, media_part)
                is_media_request = True
                logger.info(f"Применен ЯВНЫЙ медиа-контекст (через reply) для чата {chat_id}")

    await process_request(update, context, content_parts, is_media_request=is_media_request)

# --- ЗАПУСК БОТА ---
async def handle_health_check(request: aiohttp.web.Request) -> aiohttp.web.Response:
    logger.info("Health check OK")
    return aiohttp.web.Response(text="OK", status=200)
    
async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    application = request.app['bot_app']
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return aiohttp.web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}", exc_info=True)
        return aiohttp.web.Response(status=500)

async def run_web_server(application: Application, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    app['bot_app'] = application
    app.router.add_post('/' + GEMINI_WEBHOOK_PATH.strip('/'), handle_telegram_webhook)
    app.router.add_get('/', handle_health_check) 
    
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = aiohttp.web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")
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
        BotCommand("transcript", "Транскрипция медиа (ответом)"),
        BotCommand("summarize", "Краткий пересказ (ответом)"),
        BotCommand("keypoints", "Ключевые тезисы (ответом)"),
        BotCommand("newtopic", "Сбросить контекст файлов"),
        BotCommand("clear", "Очистить всю историю чата")
    ]
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("transcript", transcript_command))
    application.add_handler(CommandHandler("summarize", summarize_command))
    application.add_handler(CommandHandler("keypoints", keypoints_command))
    application.add_handler(CommandHandler("newtopic", newtopic_command))
    
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_video))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_audio))
    application.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND, handle_audio))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))

    url_filter = filters.Entity("url") | filters.Entity("text_link")
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_REGEX), handle_youtube_url))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & url_filter, handle_url))
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
