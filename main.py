# Обновлённый main.py:
# - Исправлен поиск DDG: используется синхронный ddgs.text() в отдельном потоке через asyncio.to_thread()
# - Скорректирована системная инструкция и формирование промпта с поиском для более естественного ответа.
# - Улучшено формирование промпта для фото и документов для лучшего удержания контекста.

import logging
import os
import asyncio # Нужно для asyncio.to_thread
import signal
from urllib.parse import urljoin
import base64
import pytesseract
from PIL import Image
import io
import pprint

# Инициализируем логгер
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ... # Прочий код (импорты, константы и т.д.) остается без изменений

import aiohttp.web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import google.generativeai as genai
# ===== ИСПРАВЛЕНИЕ: Возвращаем импорт DDGS =====
from duckduckgo_search import DDGS # Обычный класс
from google.generativeai.types import HarmCategory, HarmBlockThreshold
# ============================================

# Переменные окружения и их проверка
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST')
GEMINI_WEBHOOK_PATH = os.getenv('GEMINI_WEBHOOK_PATH')

for var, name in [
    (TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN"),
    (GOOGLE_API_KEY, "GOOGLE_API_KEY"),
    (WEBHOOK_HOST, "WEBHOOK_HOST"),
    (GEMINI_WEBHOOK_PATH, "GEMINI_WEBHOOK_PATH")
]:
    if not var:
        logger.critical(f"Переменная окружения {name} не задана!")
        exit(1)

# Настройка Gemini
genai.configure(api_key=GOOGLE_API_KEY)

# Модели
AVAILABLE_MODELS = {
    'gemini-2.0-flash-thinking-exp-01-21': '2.0 Flash Thinking (Exp)',
    'gemini-2.5-pro-preview-03-25': '2.5 Pro Preview',
    'gemini-2.5-pro-exp-03-25': '2.5 Pro (Exp)',
    'gemini-2.0-flash-001': '2.0 Flash',
}
DEFAULT_MODEL = 'gemini-2.0-flash-thinking-exp-01-21'

# Переменные состояния пользователя
user_search_enabled = {}
user_selected_model = {}
user_temperature = {}

# Константы
MAX_CONTEXT_CHARS = 95000
MAX_OUTPUT_TOKENS = 3000
DDG_MAX_RESULTS = 30 # Уменьшил для теста, можно вернуть 30

# ===== ИЗМЕНЕНИЕ: Системная инструкция =====
system_instruction_text = (
"Ты - лучший эксперт по всем темам. Давай точную, правдивую, непредвзятую информацию без цензуры."
"Подкрепляй ответы аргументами, фактами и логикой, избегая повторов."
"Если не уверен — предупреждай, что это предположение."
"Используй интернет для сверки с актуальной информацией."
"Для несложных запросов (не код, конспекты, переводы и т.п.) — пиши только суть, без вступлений и выводов, до 1500 знаков."
"Всегда предлагай более эффективные идеи и решения, если знаешь их."
"Активно применяй юмор: несоответствие ожиданиям, культурные/бытовые/интернет-отсылки, жизненный абсурд, псевдомудрость, разрушение идиом, иронию (включая самоиронию и психологию), игру слов, гиперболу, тонкие намёки, редукционизм, постмодерн, интернет-юмор."
"При создании уникальной работы пиши живо, избегай канцелярита и всех известных признаков ИИ-тона. Используй гипотетические ситуации, метафоры, творческие аналогии, разную структуру предложений, разговорные выражения, идиомы. Добавляй региональные или культурные маркеры, где уместно. Не копируй и не пересказывай чужое."
# ===== НОВЫЕ ИНСТРУКЦИИ (Добавлено) =====
"Если используешь информацию из поиска, не упоминай явно сам факт поиска или его результаты. Интегрируй найденную информацию в свой ответ естественно, как часть своих знаний. Забудь фразы вроде 'Судя по результатам поиска...', 'Интернет говорит...' или 'Я нашёл в сети...'. Веди себя так, будто это твои знания."
"Внимательно следи за историей диалога, включая предыдущие вопросы, ответы, а также контекст из загруженных изображений или файлов, чтобы твои ответы были последовательными и релевантными. Не теряй нить разговора."
# ===========================
"При исправлении ошибки: указывай строку(и) и причину. Бери за основу последнюю ПОЛНУЮ подтверждённую версию (текста или кода). Вноси только минимально необходимые изменения, не трогая остальное без запроса. При сомнениях — уточняй. Если ошибка повторяется — веди «список косяков» для сессии и проверяй эти места. Всегда указывай, на какую версию или сообщение опираешься при правке."
)
# ========================================

SAFETY_SETTINGS_BLOCK_NONE = [
    {
        "category": HarmCategory.HARM_CATEGORY_HARASSMENT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
    {
        "category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    },
]

# Команды (start, clear_history, set_temperature, enable_search, disable_search, model_command, select_model_callback) остаются без изменений
# ... (Код команд start, clear_history, set_temperature, enable_search, disable_search, model_command, select_model_callback) ...
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_selected_model[chat_id] = DEFAULT_MODEL
    user_search_enabled[chat_id] = True
    user_temperature[chat_id] = 1.0
    default_model_name = AVAILABLE_MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)
    start_message = (
        f"**{default_model_name}**."
        f"\n + поиск в интернете, чтение изображений (OCR) и текстовых файлов."
        "\n/model — выбор модели"
        "\n/clear — очистить историю"
        "\n/search_on  /search_off — вкл/выкл поиск"
        "\n/temp 1.0 — установить температуру (0-2)" # Добавил подсказку по температуре
    )
    await update.message.reply_text(start_message, parse_mode='Markdown')

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data['history'] = []
    await update.message.reply_text("🧹 История диалога очищена.")

async def set_temperature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        temp = float(context.args[0])
        if not (0 <= temp <= 2):
            raise ValueError("Температура должна быть от 0 до 2")
        user_temperature[chat_id] = temp
        await update.message.reply_text(f"🌡️ Температура установлена на {temp}")
    except (IndexError, ValueError) as e:
        error_msg = f"⚠️ Укажите температуру от 0 до 2, например: /temp 1.0 ({e})" if isinstance(e, ValueError) else "⚠️ Укажите температуру от 0 до 2, например: /temp 1.0"
        await update.message.reply_text(error_msg)

async def enable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_search_enabled[update.effective_chat.id] = True
    await update.message.reply_text("🦆 Поиск DuckDuckGo включён.")

async def disable_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_search_enabled[update.effective_chat.id] = False
    await update.message.reply_text("🔇 Поиск DuckDuckGo отключён.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current_model = user_selected_model.get(chat_id, DEFAULT_MODEL)
    keyboard = []
    for m, name in AVAILABLE_MODELS.items():
         button_text = f"{'✅ ' if m == current_model else ''}{name}"
         keyboard.append([InlineKeyboardButton(button_text, callback_data=m)])
    await update.message.reply_text("Выберите модель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    selected = query.data
    if selected in AVAILABLE_MODELS:
        user_selected_model[chat_id] = selected
        model_name = AVAILABLE_MODELS[selected]
        reply_text = f"Модель установлена: **{model_name}**"
        # Проверяем, нужно ли удалять клавиатуру
        if query.message.reply_markup:
            try:
                 await query.edit_message_text(reply_text, parse_mode='Markdown')
            except Exception as e:
                 logger.warning(f"Не удалось изменить сообщение с кнопками: {e}. Отправляю новое.")
                 await context.bot.send_message(chat_id, reply_text, parse_mode='Markdown')
        else:
            # Если клавиатуры уже нет, просто отправляем сообщение
            await context.bot.send_message(chat_id, reply_text, parse_mode='Markdown')
    else:
        await query.edit_message_text("❌ Неизвестная модель")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    original_user_message = update.message.text.strip() if update.message.text else ""
    if not original_user_message:
        # Проверяем, есть ли фейковый update (из photo/document)
        if hasattr(update, 'message') and hasattr(update.message, 'text') and update.message.text:
             original_user_message = update.message.text.strip()
        else:
            logger.warning(f"ChatID: {chat_id} | Получено пустое сообщение или объект update без текста.")
            return

    model_id = user_selected_model.get(chat_id, DEFAULT_MODEL)
    temperature = user_temperature.get(chat_id, 1.0)
    use_search = user_search_enabled.get(chat_id, True)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # ===== ИЗМЕНЕНИЕ: Логика поиска и формирования промпта =====
    search_context = "" # Инициализируем пустой строкой
    final_user_prompt = original_user_message # По умолчанию промпт = оригинальное сообщение
    search_log_info = "Поиск DDG отключен" # Инициализация для лога
    search_snippets = [] # Инициализация списка сниппетов

    if use_search:
        search_log_info = "Поиск DDG включен" # Обновляем лог
        logger.info(f"ChatID: {chat_id} | Поиск DDG включен. Запрос: '{original_user_message[:50]}...'")
        try:
            ddgs = DDGS()
            logger.debug(f"ChatID: {chat_id} | Запрос к DDGS().text('{original_user_message}', region='ru-ru', max_results={DDG_MAX_RESULTS}) через asyncio.to_thread")
            results = await asyncio.to_thread(
                ddgs.text,
                original_user_message,
                region='ru-ru',
                max_results=DDG_MAX_RESULTS
            )
            logger.debug(f"ChatID: {chat_id} | Результаты DDG:\n{pprint.pformat(results)}")

            if results:
                # Убираем дефис, делаем просто строки
                search_snippets = [f"{r.get('body', '')}" for r in results if r.get('body')]
                if search_snippets:
                    # Собираем контекст без явного заголовка
                    search_context = "\n".join(search_snippets)
                    # Формируем промпт хитрее: сначала доп. инфа, потом сам вопрос
                    final_user_prompt = (
                        f"Дополнительная информация по теме (используй её по необходимости, не ссылаясь):\n{search_context}\n\n"
                        f"Вопрос пользователя: \"{original_user_message}\""
                    )
                    search_log_info += f" (найдено {len(search_snippets)} сниппетов)" # Добавляем инфо в лог
                    logger.info(f"ChatID: {chat_id} | Найдены и добавлены результаты DDG для запроса: '{original_user_message[:50]}...'")
                else:
                    search_log_info += " (результаты найдены, но пусты)" # Добавляем инфо в лог
                    logger.info(f"ChatID: {chat_id} | Результаты DDG найдены, но не содержат текста (body) для: '{original_user_message[:50]}...'")
                    # final_user_prompt остается original_user_message (как задано по умолчанию)
            else:
                search_log_info += " (результаты не найдены)" # Добавляем инфо в лог
                logger.info(f"ChatID: {chat_id} | Результаты DDG не найдены для: '{original_user_message[:50]}...'")
                # final_user_prompt остается original_user_message (как задано по умолчанию)
        except Exception as e_ddg:
            logger.error(f"ChatID: {chat_id} | Ошибка при поиске DuckDuckGo: {e_ddg}", exc_info=True)
            search_log_info += " (ошибка поиска)" # Добавляем инфо в лог
            # final_user_prompt остается original_user_message

    # ===== ИЗМЕНЕНИЕ: Обновленное логирование =====
    logger.info(f"ChatID: {chat_id} | Модель: {model_id}, Темп: {temperature}, {search_log_info}")
    logger.debug(f"ChatID: {chat_id} | Финальный промпт для Gemini (может включать доп. инфо):\n{final_user_prompt}")
    # ============================================

    chat_history = context.chat_data.setdefault("history", [])
    # Добавляем ИМЕННО final_user_prompt в историю
    chat_history.append({"role": "user", "parts": [{"text": final_user_prompt}]})

    # Обрезка истории (остается без изменений)
    total_chars = sum(len(p["parts"][0]["text"]) for p in chat_history if p.get("parts") and p["parts"][0].get("text"))
    while total_chars > MAX_CONTEXT_CHARS and len(chat_history) > 1:
        removed_message = chat_history.pop(0)
        # Пересчитываем символы после удаления
        total_chars = sum(len(p["parts"][0]["text"]) for p in chat_history if p.get("parts") and p["parts"][0].get("text"))
        logger.info(f"ChatID: {chat_id} | История обрезана, удалено сообщение: {removed_message.get('role')}, текущая длина истории: {len(chat_history)}, символов: {total_chars}")
    current_history = chat_history
    current_system_instruction = system_instruction_text
    tools = []

    reply = None

    try:
        generation_config=genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=MAX_OUTPUT_TOKENS
        )
        model = genai.GenerativeModel(
            model_id,
            tools=tools,
            safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
            generation_config=generation_config,
            system_instruction=current_system_instruction
        )
        # Передаем ИМЕННО current_history, которая уже содержит final_user_prompt
        response = model.generate_content(current_history)

        reply = response.text
        if not reply:
            # Обработка пустого ответа (остается без изменений)
            try:
                feedback = response.prompt_feedback
                candidates_info = response.candidates
                block_reason = feedback.block_reason if feedback else 'N/A'
                # Проверяем наличие finish_reason
                finish_reason_val = 'N/A'
                if candidates_info and candidates_info[0].finish_reason:
                     finish_reason_val = candidates_info[0].finish_reason.name # Используем .name для enum
                safety_ratings = feedback.safety_ratings if feedback else []
                safety_info = ", ".join([f"{s.category.name}: {s.probability.name}" for s in safety_ratings])
                logger.warning(f"ChatID: {chat_id} | Пустой ответ от модели. Block: {block_reason}, Finish: {finish_reason_val}, Safety: [{safety_info}]")
                # Используем genai.types.BlockReason.UNSPECIFIED для сравнения
                if block_reason and block_reason != genai.types.BlockReason.UNSPECIFIED:
                     reply = f"🤖 Модель не дала ответ. (Причина блокировки: {block_reason.name})" # Используем .name для enum
                else:
                     reply = f"🤖 Модель не дала ответ. (Причина: {finish_reason_val})"
            except AttributeError as e_attr:
                 logger.warning(f"ChatID: {chat_id} | Пустой ответ от модели, не удалось извлечь доп. инфо (AttributeError: {e_attr}).")
                 reply = "🤖 Нет ответа от модели."
            except Exception as e_inner:
                logger.warning(f"ChatID: {chat_id} | Пустой ответ от модели, не удалось извлечь доп. инфо: {e_inner}")
                reply = "🤖 Нет ответа от модели."

        if reply:
             chat_history.append({"role": "model", "parts": [{"text": reply}]})

    except Exception as e:
        # Обработка ошибок (остается без изменений)
        logger.exception(f"ChatID: {chat_id} | Ошибка при взаимодействии с моделью {model_id}")
        error_message = str(e)
        reply = f"❌ Произошла ошибка при обращении к модели." # Сообщение по умолчанию
        try:
            # Проверяем наличие genai.types перед использованием
            if hasattr(genai, 'types'):
                if isinstance(e, genai.types.BlockedPromptException):
                     reply = f"❌ Запрос заблокирован моделью. Причина: {e}"
                elif isinstance(e, genai.types.StopCandidateException):
                     reply = f"❌ Генерация остановлена моделью. Причина: {e}"
                # Проверка других ошибок остается той же
                elif "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message): # Добавил проверку на 'resource exhausted'
                     reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
                elif "400" in error_message and "API key not valid" in error_message:
                     reply = "❌ Ошибка: Неверный Google API ключ."
                elif "Deadline Exceeded" in error_message or "504" in error_message: # Добавил проверку на 504
                     reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
                else:
                     reply = f"❌ Ошибка при обращении к модели: {error_message}"
            else:
                 logger.warning("Модуль genai.types не найден, используем общую обработку ошибок.")
                 if "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message):
                      reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
                 elif "400" in error_message and "API key not valid" in error_message:
                      reply = "❌ Ошибка: Неверный Google API ключ."
                 elif "Deadline Exceeded" in error_message or "504" in error_message:
                      reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
                 else:
                      reply = f"❌ Ошибка при обращении к модели: {error_message}"

        except AttributeError:
             # Этот блок может быть не нужен, если проверка hasattr(genai, 'types') работает
             logger.warning("Произошла AttributeError при обработке ошибки genai, используем общую обработку.")
             if "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message):
                  reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
             elif "400" in error_message and "API key not valid" in error_message:
                  reply = "❌ Ошибка: Неверный Google API ключ."
             elif "Deadline Exceeded" in error_message or "504" in error_message:
                  reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
             else:
                  reply = f"❌ Ошибка при обращении к модели: {error_message}"

    if reply:
        # Используем метод reply_text из оригинального сообщения update.message, если он доступен
        reply_method = update.message.reply_text if hasattr(update, 'message') and hasattr(update.message, 'reply_text') else context.bot.send_message
        chat_id_to_reply = update.effective_chat.id if hasattr(update, 'effective_chat') else chat_id

        if reply_method == context.bot.send_message:
            await reply_method(chat_id=chat_id_to_reply, text=reply)
        else:
            await reply_method(reply)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tesseract_available = False
    try:
        # Проверяем, что pytesseract настроен (путь может быть не 'tesseract')
        pytesseract.get_tesseract_version() # Простой вызов для проверки доступности
        tesseract_available = True
        logger.info(f"Tesseract доступен. Путь: {pytesseract.pytesseract.tesseract_cmd}")
    except Exception as e:
        logger.error(f"Проблема с доступом к Tesseract: {e}. OCR будет недоступен.")

    if not update.message.photo:
        logger.warning(f"ChatID: {chat_id} | В handle_photo не найдено фото в сообщении.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    try:
        photo_file = await update.message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Не удалось скачать фото: {e}")
        await update.message.reply_text("❌ Не удалось загрузить фото.")
        return

    user_caption = update.message.caption if update.message.caption else ""

    if tesseract_available:
        try:
            image = Image.open(io.BytesIO(file_bytes))
            # Используем русский и английский языки для OCR
            extracted_text = pytesseract.image_to_string(image, lang='rus+eng')
            if extracted_text and extracted_text.strip():
                logger.info(f"ChatID: {chat_id} | Обнаружен текст на изображении (OCR)")

                # ===== ИЗМЕНЕНИЕ: Формирование промпта для OCR =====
                ocr_context = f"На изображении обнаружен следующий текст:\n```\n{extracted_text.strip()}\n```"
                if user_caption:
                    # Формируем промпт так, будто это часть диалога
                    user_prompt = f"Пользователь загрузил фото с подписью: \"{user_caption}\". {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                else:
                    user_prompt = f"Пользователь загрузил фото. {ocr_context}\nЧто можешь сказать об этом фото и тексте на нём?"
                # ================================================

                # Создаем "фейковый" update, чтобы передать текст в handle_message
                # Используем простой объект с нужными атрибутами
                fake_update_message = type('obj', (object,), {
                    'text': user_prompt,
                    'reply_text': update.message.reply_text # Передаем метод ответа
                })
                fake_update = type('obj', (object,), {
                    'effective_chat': update.effective_chat,
                    'message': fake_update_message
                })

                # Вызываем handle_message с новым текстом
                await handle_message(fake_update, context)
                return # Важно выйти, чтобы не обрабатывать как простое изображение

        except pytesseract.TesseractNotFoundError:
             logger.error("Tesseract не найден при вызове image_to_string! Проверьте путь и установку. OCR отключен.")
             tesseract_available = False # Отключаем OCR на случай ошибки
        except Exception as e:
            logger.warning(f"ChatID: {chat_id} | Ошибка OCR: {e}")
            # Продолжаем обработку как обычное изображение, если OCR не удался

    # Обработка как изображение (если OCR выключен, не нашел текст или произошла ошибка)
    logger.info(f"ChatID: {chat_id} | Обработка фото как изображения (OCR выключен, не сработал или не нашел текст)")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        b64_data = base64.b64encode(file_bytes).decode()
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Ошибка кодирования изображения в Base64: {e}")
        await update.message.reply_text("❌ Ошибка обработки изображения.")
        return

    # Промпт для анализа изображения
    prompt = user_caption if user_caption else "Что изображено на этом фото?"
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": b64_data}} # Предполагаем JPEG, т.к. Telegram часто конвертирует
    ]

    model_id = user_selected_model.get(chat_id, DEFAULT_MODEL)
    # Проверяем, поддерживает ли модель vision
    # TODO: Добавить реальную проверку или список vision-моделей, если API это позволяет
    if "gemini-pro" not in model_id and "flash" not in model_id and "2.5" not in model_id: # Упрощенная проверка, нужны актуальные имена vision-моделей
        # Если выбрана текстовая модель, пытаемся переключиться на vision (или Flash как запасной вариант)
        potential_vision_model = 'gemini-2.0-flash-001' # Или другая подходящая vision модель
        if potential_vision_model in AVAILABLE_MODELS:
             logger.warning(f"ChatID: {chat_id} | Выбрана не-vision модель ({model_id}) для фото. Временно использую {potential_vision_model}.")
             model_id = potential_vision_model
        else:
             # Если нет доступной vision модели, сообщаем об ошибке
             logger.error(f"ChatID: {chat_id} | Выбрана не-vision модель ({model_id}), и нет доступной запасной vision-модели для анализа фото.")
             await update.message.reply_text(f"⚠️ Выбранная модель ({AVAILABLE_MODELS.get(model_id, model_id)}) не может анализировать изображения. Выберите другую модель.")
             return

    temperature = user_temperature.get(chat_id, 1.0)

    logger.info(f"ChatID: {chat_id} | Анализ изображения. Модель: {model_id}, Темп: {temperature}")
    tools = []

    reply = None

    try:
        generation_config=genai.GenerationConfig(
                temperature=temperature,
                max_output_tokens=MAX_OUTPUT_TOKENS # Можно уменьшить для описаний фото
        )
        model = genai.GenerativeModel(
            model_id,
            tools=tools,
            safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
            generation_config=generation_config,
            system_instruction=system_instruction_text # Используем ту же инструкцию
        )
        # Для vision моделей контент передается списком словарей
        response = model.generate_content([{"role": "user", "parts": parts}])
        reply = response.text

        if not reply:
            # Обработка пустого ответа (аналогично handle_message)
            try:
                feedback = response.prompt_feedback
                candidates_info = response.candidates
                block_reason = feedback.block_reason if feedback else 'N/A'
                finish_reason_val = 'N/A'
                if candidates_info and candidates_info[0].finish_reason:
                    finish_reason_val = candidates_info[0].finish_reason.name
                safety_ratings = feedback.safety_ratings if feedback else []
                safety_info = ", ".join([f"{s.category.name}: {s.probability.name}" for s in safety_ratings])
                logger.warning(f"ChatID: {chat_id} | Пустой ответ при анализе изображения. Block: {block_reason}, Finish: {finish_reason_val}, Safety: [{safety_info}]")
                if block_reason and block_reason != genai.types.BlockReason.UNSPECIFIED:
                     reply = f"🤖 Модель не смогла описать изображение. (Причина блокировки: {block_reason.name})"
                else:
                     reply = f"🤖 Модель не смогла описать изображение. (Причина: {finish_reason_val})"
            except AttributeError as e_attr:
                 logger.warning(f"ChatID: {chat_id} | Пустой ответ при анализе изображения, не удалось извлечь доп. инфо (AttributeError: {e_attr}).")
                 reply = "🤖 Не удалось понять, что на изображении."
            except Exception as e_inner:
                 logger.warning(f"ChatID: {chat_id} | Пустой ответ при анализе изображения, не удалось извлечь доп. инфо: {e_inner}")
                 reply = "🤖 Не удалось понять, что на изображении."

    except Exception as e:
        # Обработка ошибок (аналогично handle_message)
        logger.exception(f"ChatID: {chat_id} | Ошибка при анализе изображения")
        error_message = str(e)
        reply = f"❌ Произошла ошибка при анализе изображения." # Сообщение по умолчанию
        try:
             if hasattr(genai, 'types'):
                if isinstance(e, genai.types.BlockedPromptException):
                     reply = f"❌ Запрос на анализ изображения заблокирован моделью. Причина: {e}"
                elif isinstance(e, genai.types.StopCandidateException):
                     reply = f"❌ Анализ изображения остановлен моделью. Причина: {e}"
                elif "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message):
                     reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
                elif "400" in error_message and "API key not valid" in error_message:
                     reply = "❌ Ошибка: Неверный Google API ключ."
                elif "Deadline Exceeded" in error_message or "504" in error_message:
                     reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
                # Добавляем проверку на ошибку, если модель не поддерживает изображения
                elif "User location is not supported for accessing this model" in error_message:
                    reply = f"❌ Ошибка: Ваш регион не поддерживается для использования модели {model_id}."
                elif "does not support image input" in error_message:
                    reply = f"❌ Ошибка: Выбранная модель ({AVAILABLE_MODELS.get(model_id, model_id)}) не поддерживает анализ изображений."
                else:
                    reply = f"❌ Ошибка при анализе изображения: {error_message}"
             else:
                logger.warning("Модуль genai.types не найден, используем общую обработку ошибок для фото.")
                # Дублируем логику из блока if hasattr(genai, 'types')
                if "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message):
                     reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
                elif "400" in error_message and "API key not valid" in error_message:
                     reply = "❌ Ошибка: Неверный Google API ключ."
                elif "Deadline Exceeded" in error_message or "504" in error_message:
                     reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
                elif "User location is not supported for accessing this model" in error_message:
                    reply = f"❌ Ошибка: Ваш регион не поддерживается для использования модели {model_id}."
                elif "does not support image input" in error_message:
                    reply = f"❌ Ошибка: Выбранная модель ({AVAILABLE_MODELS.get(model_id, model_id)}) не поддерживает анализ изображений."
                else:
                    reply = f"❌ Ошибка при анализе изображения: {error_message}"

        except AttributeError:
             logger.warning("Произошла AttributeError при обработке ошибки genai (фото), используем общую обработку.")
             # Дублируем логику снова на всякий случай
             if "429" in error_message and ("quota" in error_message or "resource has been exhausted" in error_message):
                  reply = f"❌ Ошибка: Достигнут лимит запросов к API Google (ошибка 429). Попробуйте позже."
             elif "400" in error_message and "API key not valid" in error_message:
                  reply = "❌ Ошибка: Неверный Google API ключ."
             elif "Deadline Exceeded" in error_message or "504" in error_message:
                  reply = "❌ Ошибка: Модель слишком долго отвечала (таймаут)."
             elif "User location is not supported for accessing this model" in error_message:
                 reply = f"❌ Ошибка: Ваш регион не поддерживается для использования модели {model_id}."
             elif "does not support image input" in error_message:
                 reply = f"❌ Ошибка: Выбранная модель ({AVAILABLE_MODELS.get(model_id, model_id)}) не поддерживает анализ изображений."
             else:
                 reply = f"❌ Ошибка при анализе изображения: {error_message}"

    if reply:
        await update.message.reply_text(reply)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.message.document:
        logger.warning(f"ChatID: {chat_id} | В handle_document не найден документ.")
        return

    doc = update.message.document
    # Разрешаем больше текстовых MIME типов
    allowed_mime_prefixes = ('text/', 'application/json', 'application/x-python-code', 'application/xml', 'application/javascript')
    allowed_mime_types = ('application/csv', 'text/csv') # Добавляем конкретные типы

    if not doc.mime_type or not (doc.mime_type.startswith(allowed_mime_prefixes) or doc.mime_type in allowed_mime_types):
        await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы (например, .txt, .py, .js, .json, .csv, .xml). Получен тип: {doc.mime_type}")
        logger.warning(f"ChatID: {chat_id} | Попытка загрузить неподдерживаемый файл: {doc.file_name} (MIME: {doc.mime_type})")
        return

    # Ограничение по размеру файла перед скачиванием
    MAX_FILE_SIZE_MB = 10
    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
         await update.message.reply_text(f"⚠️ Файл '{doc.file_name}' слишком большой (>{MAX_FILE_SIZE_MB} МБ).")
         logger.warning(f"ChatID: {chat_id} | Попытка загрузить слишком большой файл: {doc.file_name} ({doc.file_size / (1024*1024):.2f} МБ)")
         return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    try:
        doc_file = await doc.get_file()
        file_bytes = await doc_file.download_as_bytearray()
    except Exception as e:
        logger.error(f"ChatID: {chat_id} | Не удалось скачать документ '{doc.file_name}': {e}")
        await update.message.reply_text(f"❌ Не удалось загрузить файл '{doc.file_name}'.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    text = None
    encodings_to_try = ['utf-8', 'latin-1', 'cp1251'] # Список кодировок для пробы
    for encoding in encodings_to_try:
        try:
            text = file_bytes.decode(encoding)
            logger.info(f"ChatID: {chat_id} | Файл '{doc.file_name}' успешно декодирован с помощью {encoding}.")
            break # Выходим из цикла, если декодирование успешно
        except UnicodeDecodeError:
            logger.warning(f"ChatID: {chat_id} | Файл '{doc.file_name}' не в кодировке {encoding}.")
        except Exception as e:
            logger.error(f"ChatID: {chat_id} | Непредвиденная ошибка при декодировании файла '{doc.file_name}' с {encoding}: {e}")
            # Не прерываем цикл, пробуем другие кодировки

    if text is None:
        logger.error(f"ChatID: {chat_id} | Не удалось декодировать файл '{doc.file_name}' ни одной из кодировок: {encodings_to_try}")
        await update.message.reply_text(f"❌ Не удалось прочитать текстовое содержимое файла '{doc.file_name}'. Поддерживаемые кодировки: {', '.join(encodings_to_try)}.")
        return

    MAX_FILE_CHARS = 50000 # Увеличил лимит для текста
    truncated = text
    warning_msg = ""
    if len(text) > MAX_FILE_CHARS:
        truncated = text[:MAX_FILE_CHARS]
        warning_msg = f"\n\n(⚠️ Текст файла был обрезан до {MAX_FILE_CHARS} символов)"
        logger.warning(f"ChatID: {chat_id} | Текст файла '{doc.file_name}' обрезан до {MAX_FILE_CHARS} символов.")

    user_caption = update.message.caption if update.message.caption else ""

    # ===== ИЗМЕНЕНИЕ: Формирование промпта для документа =====
    file_context = f"Содержимое файла '{doc.file_name}':\n```\n{truncated}\n```{warning_msg}"
    if user_caption:
        user_prompt = f"Пользователь загрузил файл '{doc.file_name}' с комментарием: \"{user_caption}\". {file_context}\nПроанализируй, пожалуйста."
    else:
        user_prompt = f"Пользователь загрузил файл '{doc.file_name}'. {file_context}\nЧто можешь сказать об этом тексте?"
    # ======================================================

    # Создаем "фейковый" update, чтобы передать текст в handle_message
    fake_update_message = type('obj', (object,), {
        'text': user_prompt,
        'reply_text': update.message.reply_text # Передаем метод ответа
    })
    fake_update = type('obj', (object,), {
        'effective_chat': update.effective_chat,
        'message': fake_update_message
    })

    # Вызываем handle_message с новым текстом
    await handle_message(fake_update, context)


async def setup_bot_and_server(stop_event: asyncio.Event):
    # Эта функция остается без изменений
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("temp", set_temperature))
    application.add_handler(CommandHandler("search_on", enable_search))
    application.add_handler(CommandHandler("search_off", disable_search))
    application.add_handler(CallbackQueryHandler(select_model_callback))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Обновляем фильтр для документов, чтобы он соответствовал новым MIME типам
    application.add_handler(MessageHandler(
        filters.Document.MimeType(
            ['text/*', 'application/json', 'application/x-python-code', 'application/xml', 'application/javascript', 'application/csv', 'text/csv']
        ), handle_document)
    )
    # Добавляем обработчик для неподдерживаемых документов
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.MimeType(
            ['text/*', 'application/json', 'application/x-python-code', 'application/xml', 'application/javascript', 'application/csv', 'text/csv']
        ), handle_unsupported_document)) # Нужна новая функция handle_unsupported_document

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    webhook_url = urljoin(WEBHOOK_HOST, f"/{GEMINI_WEBHOOK_PATH}")
    logger.info(f"Устанавливаю вебхук: {webhook_url}")
    await application.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return application, run_web_server(application, stop_event)

# Новая функция для обработки неподдерживаемых документов
async def handle_unsupported_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    mime_type = doc.mime_type if doc.mime_type else "неизвестный"
    logger.warning(f"ChatID: {chat_id} | Получен неподдерживаемый тип файла: {doc.file_name} (MIME: {mime_type})")
    await update.message.reply_text(f"⚠️ Пока могу читать только текстовые файлы (например, .txt, .py, .js, .json, .csv, .xml). Ваш файл '{doc.file_name}' имеет тип '{mime_type}', который не поддерживается.")


async def run_web_server(application: Application, stop_event: asyncio.Event):
    # Эта функция остается без изменений
    app = aiohttp.web.Application()
    async def health_check(request):
        return aiohttp.web.Response(text="OK")
    app.router.add_get('/', health_check)

    app['bot_app'] = application
    webhook_path = f"/{GEMINI_WEBHOOK_PATH}"
    app.router.add_post(webhook_path, handle_telegram_webhook)
    logger.info(f"Вебхук слушает на пути: {webhook_path}")

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info(f"Сервер запущен на http://0.0.0.0:{port}")
        await stop_event.wait()
    finally:
        logger.info("Останавливаю веб-сервер...")
        await runner.cleanup()
        logger.info("Веб-сервер остановлен.")


async def handle_telegram_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # Эта функция остается без изменений
    application = request.app.get('bot_app')
    if not application:
        logger.error("Объект приложения бота не найден в контексте aiohttp!")
        return aiohttp.web.Response(status=500, text="Internal Server Error: Bot application not configured")

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        # Не создаем задачу в фоне, а ждем её выполнения, чтобы избежать потенциальных проблем с конкурентным доступом к истории
        # Однако, это может замедлить ответ вебхуку, если обработка долгая.
        # Если скорость ответа вебхуку критична, можно вернуть asyncio.create_task, но тогда нужна блокировка доступа к context.chat_data
        await application.process_update(update)
        return aiohttp.web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка обработки вебхук-запроса: {e}", exc_info=True)
        # Возвращаем 200 OK, чтобы Telegram не пытался повторно отправить тот же update
        return aiohttp.web.Response(text="OK", status=200)


async def main():
    # Эта функция остается без изменений
    logging.getLogger('httpx').setLevel(logging.WARNING) # Уменьшаем спам от httpx в логах DDGS
    logging.getLogger('httpcore').setLevel(logging.WARNING) # Уменьшаем спам от httpcore
    logging.getLogger('google.api_core').setLevel(logging.WARNING) # Уменьшаем спам от Google API
    logging.getLogger('google.generativeai').setLevel(logging.INFO) # Оставляем INFO для Gemini
    logging.getLogger('duckduckgo_search').setLevel(logging.INFO) # Оставляем INFO для DDGS
    logging.getLogger('PIL').setLevel(logging.INFO) # Уменьшаем спам от Pillow

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    # Обработка сигналов остановки
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        # Windows не поддерживает add_signal_handler для SIGTERM
        logger.warning("Signal handlers for SIGINT/SIGTERM might not be fully supported on this OS.")
        signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
        # SIGTERM может не работать, но SIGINT (Ctrl+C) должен

    application = None
    web_server_task = None
    try:
        logger.info("Запускаю настройку бота и сервера...")
        application, web_server_coro = await setup_bot_and_server(stop_event)
        web_server_task = asyncio.create_task(web_server_coro)
        logger.info("Настройка завершена, жду сигналов остановки...")
        # Основной цикл ожидания
        await stop_event.wait()

    except Exception as e:
        logger.exception("Критическая ошибка в главном потоке приложения.")
    finally:
        logger.info("Начинаю процесс остановки...")

        # 1. Останавливаем веб-сервер (через событие)
        if not stop_event.is_set():
             stop_event.set() # Убедимся, что событие установлено для остановки сервера

        if web_server_task and not web_server_task.done():
             logger.info("Ожидаю завершения веб-сервера...")
             try:
                 # Даем серверу немного времени на завершение обработки текущих запросов
                 await asyncio.wait_for(web_server_task, timeout=10.0)
                 logger.info("Веб-сервер успешно завершен.")
             except asyncio.TimeoutError:
                 logger.warning("Веб-сервер не завершился за 10 секунд, отменяю задачу...")
                 web_server_task.cancel()
                 try:
                     await web_server_task # Ждем завершения отмены
                 except asyncio.CancelledError:
                     logger.info("Задача веб-сервера успешно отменена.")
                 except Exception as e:
                     logger.error(f"Ошибка при ожидании отмены задачи веб-сервера: {e}")
             except Exception as e:
                 logger.error(f"Ошибка при ожидании/отмене задачи веб-сервера: {e}")

        # 2. Останавливаем приложение бота
        if application:
            logger.info("Останавливаю приложение бота (shutdown)...")
            await application.shutdown()
            logger.info("Приложение бота остановлено.")
        else:
            logger.warning("Объект приложения бота не был создан или был потерян.")

        # 3. Очистка задач (на всякий случай)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"Отменяю оставшиеся {len(tasks)} задач...")
            [task.cancel() for task in tasks]
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("Оставшиеся задачи отменены.")
            except Exception as e:
                logger.error(f"Ошибка при отмене оставшихся задач: {e}")


        logger.info("Приложение полностью остановлено.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение прервано пользователем (Ctrl+C)")
    except Exception as e:
        logger.critical(f"Неперехваченная ошибка на верхнем уровне: {e}", exc_info=True)