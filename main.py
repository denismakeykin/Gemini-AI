import logging
import os
import asyncio
import signal
import re
import datetime
import pytz
import pickle
from collections import defaultdict
import psycopg2
from psycopg2 import pool
import io
import html
import time
from typing import Coroutine
import mimetypes
import json

import httpx 
import aiohttp
import aiohttp.web
from telegram import Update, Message, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, BasePersistence, CallbackQueryHandler
from telegram.error import BadRequest

from google import genai
from google.genai import types

from youtube_transcript_api import YouTubeTranscriptApi

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    with open('system_prompt.md', 'r', encoding='utf-8') as f:
        system_instruction_text = f.read()
    logger.info("Системный промпт успешно загружен.")
except FileNotFoundError:
    logger.critical("Критическая ошибка: файл system_prompt.md не найден!")
    exit(1)

# --- БАЗА ДАННЫХ (НАДЕЖНАЯ ВЕРСИЯ) ---
class PostgresPersistence(BasePersistence):
    def __init__(self, database_url: str):
        super().__init__()
        self.db_pool = None
        self.dsn = database_url
        try:
            self._connect()
            self._initialize_db()
        except psycopg2.Error as e:
            logger.critical(f"PostgresPersistence: Не удалось подключиться к БД: {e}")
            raise

    def _connect(self):
        if self.db_pool:
            try: self.db_pool.closeall()
            except Exception as e: logger.warning(f"Ошибка при закрытии старого пула: {e}")
        dsn = self.dsn
        keepalive_options = "keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=5"
        if "?" in dsn:
             if "keepalives" not in dsn: dsn = f"{dsn}&{keepalive_options}"
        else:
             dsn = f"{dsn}?{keepalive_options}"
        self.db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=dsn)
        logger.info(f"Пул соединений с БД (пере)создан.")


    def _execute(self, query: str, params: tuple = None, fetch: str = None, retries=3):
        if not self.db_pool: raise ConnectionError("Пул соединений не инициализирован.")
        last_exception = None
        for attempt in range(retries):
            conn = None
            connection_handled = False
            try:
                conn = self.db_pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    if fetch == "one": return cur.fetchone()
                    if fetch == "all": return cur.fetchall()
                    conn.commit()
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Postgres: Ошибка соединения (попытка {attempt + 1}/{retries}): {e}. Попытка переподключения...")
                last_exception = e
                if conn:
                    self.db_pool.putconn(conn, close=True)
                    connection_handled = True
                if attempt < retries - 1: self._connect(); time.sleep(1 + attempt)
                continue
            finally:
                if conn and not connection_handled:
                    self.db_pool.putconn(conn)
        
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

    async def get_user_data(self) -> defaultdict[int, dict]:
        all_data = await asyncio.to_thread(self._execute, "SELECT key, data FROM persistence_data WHERE key LIKE 'user_data_%';", fetch="all")
        user_data = defaultdict(dict)
        if all_data:
            for k, d in all_data:
                try: user_data[int(k.split('_')[-1])] = pickle.loads(d)
                except (ValueError, IndexError): logger.warning(f"Обнаружен некорректный ключ пользователя в БД: '{k}'. Запись пропущена.")
        return user_data
    async def update_user_data(self, user_id: int, data: dict) -> None: await asyncio.to_thread(self._set_pickled, f"user_data_{user_id}", data)

    async def drop_chat_data(self, chat_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"chat_data_{chat_id}",))
    async def drop_user_data(self, user_id: int) -> None: await asyncio.to_thread(self._execute, "DELETE FROM persistence_data WHERE key = %s;", (f"user_data_{user_id}",))

    async def get_callback_data(self) -> dict | None: return None
    async def update_callback_data(self, data: dict) -> None: pass
    async def get_conversations(self, name: str) -> dict: return {}
    async def update_conversation(self, name: str, key: tuple, new_state: object | None) -> None: pass

    async def refresh_bot_data(self, bot_data: dict) -> None: data = await self.get_bot_data(); bot_data.update(data)
    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"chat_data_{chat_id}") or {}; chat_data.update(data)
    async def refresh_user_data(self, user_id: int, user_data: dict) -> None: data = await asyncio.to_thread(self._get_pickled, f"user_data_{user_id}") or {}; user_data.update(data)
    async def flush(self) -> None: pass
    def close(self):
        if self.db_pool: self.db_pool.closeall()

# --- КОНФИГУРАЦИЯ БОТА ---
TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH, DATABASE_URL = map(os.getenv, ['TELEGRAM_BOT_TOKEN', 'GOOGLE_API_KEY', 'WEBHOOK_HOST', 'GEMINI_WEBHOOK_PATH', 'DATABASE_URL'])
if not all([TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, WEBHOOK_HOST, GEMINI_WEBHOOK_PATH]):
    logger.critical("Отсутствуют обязательные переменные окружения!")
    exit(1)

DEFAULT_MODEL = 'gemini-2.5-flash'
MAX_HISTORY_MESSAGES = 100
MAX_OUTPUT_TOKENS = 8192
MAX_CONTEXT_CHARS = 100000 
USER_ID_PREFIX_FORMAT, TARGET_TIMEZONE = "[User {user_id}; Name: {user_name}]: ", "Europe/Moscow"

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_current_time_str() -> str: return datetime.datetime.now(pytz.timezone(TARGET_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
def extract_youtube_id(url_text: str) -> str | None:
    match = re.search(r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})", url_text)
    return match.group(1) if match else None
def extract_general_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\'`]+', text)
    if match: return match.group(0).rstrip('.,?!')
    return None

# --- ФУНКЦИИ-ВЕРСТАЛЬЩИКИ ДЛЯ TELEGRAM ---
def sanitize_telegram_html(raw_html: str) -> str:
    if not raw_html: return ""
    s = re.sub(r'<br\s*/?>', '\n', raw_html, flags=re.IGNORECASE)
    s = re.sub(r'<li>', '• ', s, flags=re.IGNORECASE)
    s = re.sub(r'</?(?!b>|i>|u>|s>|code>|pre>|a>|tg-spoiler>)\w+\s*[^>]*>', '', s)
    return s.strip()

def html_safe_chunker(text: str, chunk_size: int = 4096) -> list[str]:
    chunks = []
    if not text: return ['']
    tag_stack = []
    remaining_text = text
    tag_regex = re.compile(r'</?(b|i|u|s|code|pre|a|tg-spoiler)>', re.IGNORECASE)

    while remaining_text:
        if len(remaining_text) <= chunk_size:
            chunks.append(remaining_text)
            break
        split_pos = remaining_text.rfind('\n', 0, chunk_size)
        if split_pos == -1: split_pos = chunk_size
        current_chunk = remaining_text[:split_pos]
        temp_stack = list(tag_stack)
        for match in tag_regex.finditer(current_chunk):
            tag_name = match.group(1).lower()
            if f'</{tag_name}>' == match.group(0).lower():
                if temp_stack and temp_stack[-1] == tag_name: temp_stack.pop()
            else:
                temp_stack.append(tag_name)
        closing_tags = ''.join(f'</{tag}>' for tag in reversed(temp_stack))
        chunks.append(current_chunk + closing_tags)
        tag_stack = temp_stack
        opening_tags = ''.join(f'<{tag}>' for tag in tag_stack)
        remaining_text = opening_tags + remaining_text[split_pos:].lstrip()
    return chunks

# --- ЛОГИКА ИСТОРИИ И КОНТЕКСТА ---
async def _add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, parts: list, **kwargs):
    history = context.chat_data.setdefault("history", [])
    entry = {"role": role, "parts": parts, **kwargs}
    history.append(entry)
    while len(history) > MAX_HISTORY_MESSAGES:
        history.pop(0)

def build_context_for_model(chat_history: list) -> list:
    context_for_model = []
    current_chars = 0
    for entry in reversed(chat_history):
        if not all(k in entry for k in ('role', 'parts')):
            continue

        raw_parts = entry.get("parts", [])
        if not raw_parts:
            continue

        repaired_parts = [
            types.Part(text=p) if isinstance(p, str) else p 
            for p in raw_parts if p is not None and (isinstance(p, str) or hasattr(p, 'text') or hasattr(p, 'inline_data'))
        ]
        if not repaired_parts:
            continue
        
        # --- ИЗМЕНЕНО: КРИТИЧЕСКИЙ ФИКС БАГА С TypeError ---
        # Конструкция `p.text or ''` элегантно заменяет None (у медиа-файлов) на пустую строку,
        # предотвращая падение `"".join()`.
        entry_text = "".join(p.text or '' for p in repaired_parts)
        entry_chars = len(entry_text)
        
        if current_chars + entry_chars > MAX_CONTEXT_CHARS and context_for_model:
            logger.info(f"Контекст обрезан. Учтено {len(context_for_model)} из {len(chat_history)} сообщений.")
            break
        
        try:
            content_object = types.Content(role=entry["role"], parts=repaired_parts)
            context_for_model.insert(0, content_object)
            current_chars += entry_chars
        except Exception as e:
            logger.warning(f"Не удалось преобразовать запись истории в Content: {e}. Запись: {entry}")
            
    return context_for_model

# --- ФУНКЦИИ ОТВЕТА ---
async def stream_and_send_reply(message_to_edit: Message, stream: Coroutine) -> str:
    full_text, buffer, last_edit_time = "", "", time.time()
    try:
        async for chunk in stream:
            if text_part := getattr(chunk, 'text', ''): buffer += text_part
            current_time = time.time()
            if current_time - last_edit_time > 1.2 or len(buffer) > 150:
                new_text_portion = full_text + buffer
                sanitized_chunk = sanitize_telegram_html(new_text_portion)
                if sanitized_chunk != message_to_edit.text:
                    try:
                        await message_to_edit.edit_text(sanitized_chunk + " ▌")
                        full_text, buffer, last_edit_time = new_text_portion, "", current_time
                    except BadRequest as e:
                        if "Message is not modified" not in str(e): logger.warning(f"Ошибка редактирования: {e}")
        final_text = full_text + buffer
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON в стриме: {e}", exc_info=False)
        final_text = full_text + buffer + "\n\n[❗️Ответ был прерван из-за сетевой ошибки.]"
    except Exception as e:
        logger.error(f"Ошибка стриминга: {e}", exc_info=True)
        final_text = full_text + buffer + f"\n\n[❌ Ошибка стриминга: {str(e)[:100]}]"

    return final_text

async def send_final_reply(placeholder_message: Message, full_text: str, context: ContextTypes.DEFAULT_TYPE) -> Message:
    sanitized_text = sanitize_telegram_html(full_text)
    if not sanitized_text.strip(): sanitized_text = "🤖 Модель не дала ответа."

    chunks = html_safe_chunker(sanitized_text)

    sent_message = None
    try:
        await placeholder_message.edit_text(chunks[0])
        sent_message = placeholder_message

        if len(chunks) > 1:
            for chunk in chunks[1:]:
                sent_message = await context.bot.send_message(chat_id=placeholder_message.chat_id, text=chunk, parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.1)
    except Exception as e:
        logger.error(f"Критическая ошибка при финальной отправке ответа: {e}", exc_info=True)

    return sent_message or placeholder_message

# --- ГЛАВНЫЙ ПРОЦЕССОР ЗАПРОСОВ ---
async def process_query(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt_parts: list, content_type: str = None, content_id: str = None, tools: list = None):
    message = update.message
    logger.info(f"Начало process_query для чата {message.chat_id}. Тип контента: {content_type or 'text'}")
    
    user = update.effective_user
    await _add_to_history(context, "user", prompt_parts, user_id=user.id, message_id=message.message_id, content_type=content_type, content_id=content_id)
    client = context.bot_data['gemini_client']
    placeholder_message = await message.reply_text("...")

    try:
        context_for_model = build_context_for_model(context.chat_data.get("history", []))

        thinking_budget_mode = context.user_data.get('thinking_budget', 'auto')
        thinking_config_obj = None
        if thinking_budget_mode == 'max':
            logger.info("Используется максимальный бюджет мышления (24576).")
            thinking_config_obj = types.ThinkingConfig(budget=24576)

        final_tools = tools if tools is not None else [types.Tool(google_search=types.GoogleSearch())]

        request_config = types.GenerateContentConfig(
            temperature=1.0, 
            max_output_tokens=MAX_OUTPUT_TOKENS,
            system_instruction=system_instruction_text,
            tools=final_tools,
            thinking_config=thinking_config_obj
        )
        
        logger.info(f"Отправка запроса к модели {DEFAULT_MODEL}...")
        stream = await client.generative_models.generate_content(
            model_name=f"models/{DEFAULT_MODEL}",
            contents=context_for_model,
            generation_config=request_config,
            stream=True
        )
        
        logger.info("Начало стриминга ответа...")
        full_reply_text = await stream_and_send_reply(placeholder_message, stream)
        final_message = await send_final_reply(placeholder_message, full_reply_text, context)
        
        await _add_to_history(context, "model", [types.Part(text=full_reply_text)], bot_message_id=final_message.message_id)
        logger.info(f"Ответ успешно отправлен в чат {message.chat_id}.")
    except Exception as e:
        logger.error(f"Критическая ошибка в process_query для чата {message.chat_id}: {e}", exc_info=True)
        await placeholder_message.edit_text(f"❌ Произошла серьезная ошибка: {html.escape(str(e)[:500])}")

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /start от пользователя {update.effective_user.id} в чате {update.effective_chat.id}")
    start_message = (
        "Я - Женя, лучший ИИ-ассистент на базе Google GEMINI 2.5 Flash:\n"
        "• 💬 Веду диалог, понимаю контекст, анализирую данные\n"
        "• 🎤 Понимаю голосовые сообщения, могу переводить в текст\n"
        "• 🖼 Анализирую изображения и видео (до 20 мб)\n"
        "• 📄 Читаю репосты, txt, pdf и веб-страницы\n"
        "• 🌐 Использую умный Google-поиск и огромный объем собственных знаний\n\n"
        "<b>Команды:</b>\n"
        "/transcribe - <i>(в ответе на голосовое)</i> Расшифровать аудио\n"
        "/summarize_yt <i><ссылка></i> - Конспект видео с YouTube\n"
        "/summarize_url <i><ссылка></i> - Выжимка из статьи\n"
        "/thinking - Настроить режим размышлений\n"
        "/clear - Очистить историю чата\n\n"
        "(!) Пользуясь ботом, вы автоматически соглашаетесь на отправку сообщений для получения ответов через Google Gemini API."
    )
    await update.message.reply_text(start_message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /clear от пользователя {update.effective_user.id} в чате {update.effective_chat.id}")
    context.chat_data.clear()
    await update.message.reply_text("🧹 История этого чата очищена.")

async def thinking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /thinking от пользователя {update.effective_user.id}")
    current_mode = context.user_data.get('thinking_budget', 'auto')
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'auto' else ''}Авто (Рекомендуется)", callback_data="set_thinking_auto")],
        [InlineKeyboardButton(f"{'✅ ' if current_mode == 'max' else ''}Максимум (Медленнее)", callback_data="set_thinking_max")],
    ]
    await update.message.reply_text("Выберите режим размышлений модели:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_thinking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split('_')[-1]
    context.user_data['thinking_budget'] = choice
    logger.info(f"Пользователь {update.effective_user.id} сменил режим размышлений на: {choice}")
    text = "✅ Режим размышлений установлен на <b>'Авто'</b>.\nЭто обеспечивает лучший баланс скорости и качества."
    if choice == 'max':
        text = "✅ Режим размышлений установлен на <b>'Максимум'</b>.\nОтветы могут быть качественнее, но и дольше."
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)

async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /transcribe от пользователя {update.effective_user.id}")
    replied_message = update.message.reply_to_message
    if not (replied_message and replied_message.voice):
        await update.message.reply_text("ℹ️ Используйте эту команду, отвечая на голосовое сообщение."); return
    await update.message.reply_text("🎤 Расшифровываю...")
    file_bytes = await (await replied_message.voice.get_file()).download_as_bytearray()
    client = context.bot_data['gemini_client']
    try:
        model = client.generative_models(DEFAULT_MODEL)
        response = await model.generate_content_async(
            contents=[types.Part(text="Расшифруй это аудио и верни только текст."), types.Part(inline_data=types.Blob(mime_type=replied_message.voice.mime_type, data=file_bytes))]
        )
        await update.message.reply_text(f"<b>Транскрипт:</b>\n{html.escape(response.text)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка сервиса распознавания: {e}")
        
async def summarize_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = extract_general_url(" ".join(context.args))
    logger.info(f"Команда /summarize_url от {update.effective_user.id}, URL: {url}")
    if not url:
        await update.message.reply_text("Пожалуйста, укажите URL после команды."); return
    await update.message.reply_text(f"🌐 Читаю страницу: {url}")
    prompt_text = f"Сделай краткую выжимку (summary) по тексту с веб-страницы: {url}"
    prompt_parts = [types.Part(text=prompt_text)]
    url_tool = types.Tool(url_context=types.UrlContext())
    await process_query(update, context, prompt_parts, content_type="webpage", content_id=url, tools=[url_tool])

async def summarize_yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = extract_youtube_id(" ".join(context.args))
    logger.info(f"Команда /summarize_yt от {update.effective_user.id}, video_id: {video_id}")
    if not video_id: await update.message.reply_text("Пожалуйста, укажите ссылку на YouTube после команды."); return
    await update.message.reply_text(f"📺 Анализирую видео с YouTube (ID: ...{video_id[-4:]})")
    try:
        transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['ru', 'en'])
        transcript = " ".join([d['text'] for d in transcript_list])
    except Exception as e: await update.message.reply_text(f"❌ Ошибка получения субтитров: {e}"); return
    prompt = f"Сделай краткий конспект по транскрипту видео с YouTube.\n\nТРАНСКРИПТ:\n{transcript}"
    await process_query(update, context, [types.Part(text=prompt)], content_type="youtube", content_id=video_id)

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ СООБЩЕНИЙ ---
async def handle_text_and_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    original_text = (message.text or "").strip()
    if not original_text: return
    logger.info(f"Получено текстовое сообщение от {user.id} в чате {message.chat_id}")

    if message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id:
        history = context.chat_data.get("history", [])
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("bot_message_id") == message.reply_to_message.message_id:
                prev_user_turn = history[i-1] if i > 0 else None
                if prev_user_turn and prev_user_turn.get("role") == "user":
                    content_type, content_id = prev_user_turn.get("content_type"), prev_user_turn.get("content_id")
                    if content_type and content_id:
                        logger.info(f"Обработка ответа на сообщение с контентом (тип: {content_type})")
                        prompt_text = f"Это уточняющий вопрос к предыдущему контенту. Пользователь спрашивает: '{original_text}'. Проанализируй исходный материал еще раз и ответь."
                        parts = [types.Part(text=prompt_text)]
                        try:
                            if content_type == "document":
                                file = await context.bot.get_file(content_id)
                                file_bytes = await file.download_as_bytearray()
                                parts.append(types.Part(inline_data=types.Blob(mime_type=file.mime_type, data=file_bytes)))
                            elif content_type in ["image", "video", "voice"]:
                                file_bytes = await(await context.bot.get_file(content_id)).download_as_bytearray()
                                mime_map = {'image': 'image/jpeg', 'voice': 'audio/ogg', 'video': 'video/mp4'}
                                mime = mime_map.get(content_type)
                                if mime:
                                    parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=file_bytes)))
                                else:
                                    raise ValueError(f"Неизвестный content_type: {content_type}")
                            elif content_type == "webpage":
                                prompt_text += f"\n\nПроанализируй еще раз страницу: {content_id}"
                                parts[0].text = prompt_text
                                await process_query(update, context, parts, content_type=content_type, content_id=content_id, tools=[types.Tool(url_context=types.UrlContext())])
                                return
                            elif content_type == "youtube":
                                transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, content_id, languages=['ru', 'en'])
                                transcript = " ".join([d['text'] for d in transcript_list])
                                prompt_text += f"\n\nИСХОДНЫЙ ТРАНСКРИПТ:\n{transcript}"
                                parts[0].text = prompt_text
                                await process_query(update, context, parts, content_type=content_type, content_id=content_id)
                                return
                            await process_query(update, context, parts, content_type=content_type, content_id=content_id)
                            return
                        except Exception as e:
                            logger.error(f"Не удалось получить исходный контент для ответа: {e}")
                            await message.reply_text(f"❌ Не удалось получить исходный контент для повторного анализа: {e}")
                            return
                            
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    prompt_parts = [types.Part(text=f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(original_text)}")]
    await process_query(update, context, prompt_parts)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    logger.info(f"Получено медиа от {user.id} в чате {message.chat_id}")
    if message.photo:
        await handle_photo_with_search(update, context)
        return

    caption = message.caption or "Опиши, что на этом медиафайле."
    if message.video:
        file_id, mime_type, content_type = message.video.file_id, message.video.mime_type, "video"
    elif message.voice:
        file_id, mime_type, content_type = message.voice.file_id, message.voice.mime_type, "voice"
        caption = "Расшифруй это голосовое сообщение и ответь на него."
    else: return

    file_bytes = await (await context.bot.get_file(file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes))
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    text_part = types.Part(text=f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}")
    await process_query(update, context, [text_part, media_part], content_type=content_type, content_id=file_id)

async def handle_photo_with_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user = update.message, update.effective_user
    logger.info(f"Обработка фото от {user.id} в чате {message.chat_id}")
    client = context.bot_data['gemini_client']
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    photo_file = message.photo[-1]
    file_bytes = await (await context.bot.get_file(photo_file.file_id)).download_as_bytearray()
    media_part = types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=file_bytes))
    extraction_prompt = "Проанализируй это изображение. Если на нем есть хорошо читаемый текст, извлеки его. Если текста нет, опиши ключевые объекты 1-3 словами. Ответ должен быть ОЧЕНЬ коротким и содержать только текст или слова, подходящие для веб-поиска."
    search_query = None
    try:
        model = client.generative_models(DEFAULT_MODEL)
        response_extract = await model.generate_content_async(contents=[types.Part(text=extraction_prompt), media_part])
        search_query = response_extract.text.strip()
    except Exception as e:
        logger.warning(f"Ошибка при извлечении ключевых слов с фото: {e}")
    caption = message.caption or "Подробно опиши это изображение."
    user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=user.id, user_name=html.escape(user.first_name or ''))
    final_text_prompt = f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}"
    if search_query and len(search_query) > 2:
        await message.reply_text(f"🔍 Нашел на картинке «<i>{html.escape(search_query[:60])}</i>», ищу информацию...", parse_mode=ParseMode.HTML)
        final_text_prompt += f"\n\nПроанализируй изображение, а также используй поиск, чтобы дополнить ответ по теме: '{search_query}'."
    final_prompt_parts = [types.Part(text=final_text_prompt), media_part]
    await process_query(update, context, final_prompt_parts, content_type="image", content_id=photo_file.file_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    logger.info(f"Обработка документа '{doc.file_name}' ({doc.mime_type}) от {update.effective_user.id}")
    
    if doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ Файл слишком большой (лимит 20 МБ)."); return

    await update.message.reply_text(f"📄 Читаю файл '{doc.file_name}'...")
    try:
        file_bytes = await (await doc.get_file()).download_as_bytearray()
        file_part = types.Part(inline_data=types.Blob(mime_type=doc.mime_type, data=file_bytes))
        caption = update.message.caption or f"Проанализируй содержимое этого файла ({doc.file_name})."
        user_prefix = USER_ID_PREFIX_FORMAT.format(user_id=update.effective_user.id, user_name=html.escape(update.effective_user.first_name or ''))
        text_part = types.Part(text=f"(Текущая дата: {get_current_time_str()})\n{user_prefix}{html.escape(caption)}")
        prompt_parts = [text_part, file_part]
        await process_query(update, context, prompt_parts, content_type="document", content_id=doc.file_id)
    except Exception as e:
        logger.error(f"Ошибка при обработке документа {doc.file_name}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Не удалось обработать файл: {e}")

# --- НОВЫЙ МЕХАНИЗМ ЗАПУСКА ---
async def worker(application: Application, update_queue: asyncio.Queue):
    logger.info("Воркер запущен и готов к работе.")
    while True:
        try:
            update_json = await update_queue.get()
            logger.info("Воркер получил новое обновление из очереди.")
            update = Update.de_json(json.loads(update_json) if isinstance(update_json, str) else update_json, application.bot)
            await application.process_update(update)
            update_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Воркер остановлен.")
            break
        except Exception:
            logger.error("Критическая ошибка в воркере:", exc_info=True)

async def run_web_server(update_queue: asyncio.Queue, stop_event: asyncio.Event):
    app = aiohttp.web.Application()
    async def webhook_handler(request: aiohttp.web.Request):
        try:
            logger.info(f"Получен входящий запрос на вебхук от {request.remote}")
            await update_queue.put(await request.json())
            return aiohttp.web.Response(status=200)
        except Exception as e:
            logger.error(f"Ошибка в обработчике вебхука (до очереди): {e}", exc_info=True)
            return aiohttp.web.Response(status=500)
    app.router.add_post(f"/{GEMINI_WEBHOOK_PATH.strip('/')}", webhook_handler)
    app.router.add_get('/', lambda r: aiohttp.web.Response(text="Bot is running"))

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))

    try:
        await site.start()
        logger.info(f"Веб-сервер запущен на порту {os.getenv('PORT', '10000')}.")
        await stop_event.wait()
    finally:
        await runner.cleanup()
        logger.info("Веб-сервер остановлен.")

async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop_event.set)

    update_queue = asyncio.Queue()

    persistence = PostgresPersistence(DATABASE_URL) if DATABASE_URL else None
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if persistence: builder.persistence(persistence)
    application = builder.build()

    doc_filter = filters.Document.MimeType(
        ['application/pdf', 'text/plain', 'text/markdown', 'text/csv', 'text/html', 'text/xml', 'application/json', 'text/css']
    )

    handlers = [
        CommandHandler("start", start), CommandHandler("clear", clear_history),
        CommandHandler("thinking", thinking_command), CommandHandler("transcribe", transcribe_command),
        CommandHandler("summarize_url", summarize_url_command), CommandHandler("summarize_yt", summarize_yt_command),
        CallbackQueryHandler(select_thinking_callback, pattern="^set_thinking_"),
        MessageHandler(filters.PHOTO, handle_media),
        MessageHandler(filters.VIDEO | filters.VOICE, handle_media),
        MessageHandler(doc_filter, handle_document),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_and_replies)
    ]
    application.add_handlers(handlers)

    web_task, worker_task = None, None
    try:
        web_task = asyncio.create_task(run_web_server(update_queue, stop_event))

        await application.initialize()
        logger.info("Приложение инициализировано.")

        genai.configure(api_key=GOOGLE_API_KEY)
        application.bot_data['gemini_client'] = genai
        
        application.bot_data['http_client'] = httpx.AsyncClient()
        logger.info("API клиенты (Gemini, HTTPX) успешно созданы и добавлены в bot_data.")

        worker_task = asyncio.create_task(worker(application, update_queue))

        webhook_url = f"{WEBHOOK_HOST.rstrip('/')}/{GEMINI_WEBHOOK_PATH.strip('/')}"
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES, secret_token=os.getenv('WEBHOOK_SECRET_TOKEN'))
        logger.info(f"Вебхук установлен: {webhook_url}")

        commands = [
            BotCommand("start", "Инфо и помощь"), BotCommand("clear", "Очистить историю"),
            BotCommand("thinking", "Настроить режим размышлений"), BotCommand("transcribe", "Расшифровать аудио (ответом)"),
            BotCommand("summarize_yt", "Конспект видео YouTube"), BotCommand("summarize_url", "Выжимка из статьи")
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Команды бота установлены.")

        await stop_event.wait()

    finally:
        logger.info("--- Остановка приложения ---")
        tasks_to_cancel = [t for t in [web_task, worker_task] if t and not t.done()]
        if tasks_to_cancel:
            for task in tasks_to_cancel:
                task.cancel()
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if application:
            if http_client := application.bot_data.get('http_client'):
                if not http_client.is_closed: await http_client.aclose()
            await application.shutdown()
            if persistence: persistence.close()
        logger.info("--- Приложение полностью остановлено ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Приложение остановлено пользователем.")
