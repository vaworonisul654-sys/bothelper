import os
import sqlite3
import logging
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import AsyncOpenAI

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8676390709:AAG89EroTpMrwLPJWR73sbADnBqmXP66xCo"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
DB_FILE = "assistant.db"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
agent_sessions = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        text TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        chat_id INTEGER,
                        text TEXT,
                        remind_at TIMESTAMP,
                        is_sent BOOLEAN DEFAULT 0,
                        sent_at TIMESTAMP DEFAULT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        timezone TEXT DEFAULT "Europe/Moscow")''')
    try:
        cursor.execute("ALTER TABLE reminders ADD COLUMN sent_at TIMESTAMP DEFAULT NULL")
    except:
        pass
    conn.commit()
    conn.close()

def get_user_timezone(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_timezone(user_id, timezone):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (user_id, timezone) VALUES (?, ?)", (user_id, timezone))
    conn.commit()
    conn.close()

def get_user_now(user_id):
    tz_name = get_user_timezone(user_id)
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except:
            pass
    return datetime.now(ZoneInfo("Europe/Moscow"))

def add_note(user_id, text):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO notes (user_id, text) VALUES (?, ?)", (user_id, text))
    conn.commit()
    conn.close()

def get_notes(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, text, created_at FROM notes WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_note(note_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
    conn.commit()
    conn.close()

def add_reminder(user_id, chat_id, text, remind_at):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO reminders (user_id, chat_id, text, remind_at) VALUES (?, ?, ?, ?)",
                   (user_id, chat_id, text, remind_at))
    remind_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return remind_id

def mark_reminder_sent(remind_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE reminders SET is_sent = 1, sent_at = ? WHERE id = ?",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), remind_id))
    conn.commit()
    conn.close()

def delete_reminder(remind_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM reminders WHERE id = ?", (remind_id,))
    conn.commit()
    conn.close()

def cleanup_old_sent_reminders():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("DELETE FROM reminders WHERE is_sent = 1 AND sent_at IS NOT NULL AND sent_at < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        logger.info("Autocleaned " + str(deleted) + " old reminders.")

def get_pending_reminders(user_id=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if user_id:
        cursor.execute("SELECT id, chat_id, text, remind_at FROM reminders WHERE is_sent = 0 AND user_id = ? ORDER BY remind_at ASC", (user_id,))
    else:
        cursor.execute("SELECT id, chat_id, text, remind_at FROM reminders WHERE is_sent = 0")
    rows = cursor.fetchall()
    conn.close()
    return rows

async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data['chat_id']
    text = job_data['text']
    remind_id = job_data['remind_id']
    keyboard = [
        [
            InlineKeyboardButton("Отложить 15 мин", callback_data="snooze_" + str(remind_id) + "_15"),
            InlineKeyboardButton("Отложить 1 час", callback_data="snooze_" + str(remind_id) + "_60")
        ],
        [InlineKeyboardButton("Завершить", callback_data="snooze_" + str(remind_id) + "_done")]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text="НАПОМИНАНИЕ:\n\n" + text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    mark_reminder_sent(remind_id)

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_sent_reminders()

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("Мои заметки"), KeyboardButton("Мои напоминания")],
        [KeyboardButton("Агент-советник"), KeyboardButton("Что ты умеешь?")],
        [KeyboardButton("Мой часовой пояс")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_agent_keyboard():
    keyboard = [[KeyboardButton("Главное меню")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['mode'] = 'default'
    user_id = update.message.from_user.id
    tz = get_user_timezone(user_id)
    if not tz:
        context.user_data['mode'] = 'setup_timezone'
        await update.message.reply_text(
            "Привет! Я твой умный бот-ассистент с ИИ.\n\n"
            "Для точных напоминаний мне нужно знать твой город или часовой пояс.\n\n"
            "Напиши название своего города, например: Москва, Новосибирск, Киев, Алматы"
        )
    else:
        await update.message.reply_text(
            "Привет! Я твой умный бот-ассистент с ИИ.\n\nОтправь текст или голосовое сообщение!",
            reply_markup=get_main_keyboard()
        )

async def list_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = get_notes(update.message.from_user.id)
    if not notes:
        await update.message.reply_text("У тебя пока нет сохраненных заметок.", reply_markup=get_main_keyboard())
        return
    response = "Твои последние заметки:\n\n"
    keyboard = []
    for idx, (note_id, text, created_at) in enumerate(notes, 1):
        text_short = text[:80] + "..." if len(text) > 80 else text
        response += str(idx) + ". " + text_short + "\n"
        keyboard.append([InlineKeyboardButton("Удалить #" + str(idx) + ": " + text[:30], callback_data="del_note_" + str(note_id))])
    await update.message.reply_text(response, reply_markup=InlineKeyboardMarkup(keyboard))

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reminders = get_pending_reminders(user_id)
    if not reminders:
        await update.message.reply_text("У тебя нет активных напоминаний.", reply_markup=get_main_keyboard())
        return
    response = "Твои ожидающие напоминания:\n\n"
    for idx, (rem_id, chat_id, text, remind_at_str) in enumerate(reminders, 1):
        try:
            dt = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%d.%m.%Y в %H:%M")
        except:
            time_str = remind_at_str
        text_short = text[:50] + "..." if len(text) > 50 else text
        response += time_str + " - " + text_short + "\n"
    response += "\nВыполненные напоминания удаляются через 24 часа."
    await update.message.reply_text(response, reply_markup=get_main_keyboard())

async def show_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    tz = get_user_timezone(user_id)
    now = get_user_now(user_id)
    if tz:
        await update.message.reply_text(
            "Твой часовой пояс: " + tz + "\nТекущее время у тебя: " + now.strftime("%d.%m.%Y %H:%M") + "\n\nЧтобы изменить - напиши свой город.",
            reply_markup=get_main_keyboard()
        )
        context.user_data['mode'] = 'setup_timezone'
    else:
        await update.message.reply_text("Часовой пояс не установлен. Напиши свой город.")
        context.user_data['mode'] = 'setup_timezone'

async def start_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    context.user_data['mode'] = 'agent'
    agent_sessions[user_id] = []
    await update.message.reply_text(
        "Агент-советник активирован!\n\nЯ помню наш разговор. Задавай любые вопросы!\n\nЧтобы выйти нажми Главное меню.",
        reply_markup=get_agent_keyboard()
    )

async def agent_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.message.from_user.id
    if user_id not in agent_sessions:
        agent_sessions[user_id] = []
    agent_sessions[user_id].append({"role": "user", "content": text})
    if len(agent_sessions[user_id]) > 20:
        agent_sessions[user_id] = agent_sessions[user_id][-20:]
    status_msg = await update.message.reply_text("Думаю...")
    system_prompt = "Ты умный, дружелюбный и честный советник-ассистент с памятью диалога. Давай развернутые полезные ответы. Отвечай на языке пользователя."
    try:
        messages = [{"role": "system", "content": system_prompt}] + agent_sessions[user_id]
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=1000
        )
        reply = response.choices[0].message.content
        agent_sessions[user_id].append({"role": "assistant", "content": reply})
        await status_msg.edit_text(reply)
    except Exception as e:
        logger.error("Agent error: " + str(e))
        await status_msg.edit_text("Ошибка. Попробуй ещё раз.")

async def detect_timezone_with_ai(city: str) -> str:
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Определи часовой пояс по названию города или страны. Верни ТОЛЬКО строку в формате IANA timezone, например: Europe/Moscow, Asia/Yekaterinburg, America/New_York. Ничего больше не пиши."},
                {"role": "user", "content": city}
            ]
        )
        tz = response.choices[0].message.content.strip()
        ZoneInfo(tz)
        return tz
    except Exception as e:
        logger.error("Timezone detect error: " + str(e))
        return None

async def analyze_with_ai(text: str, user_now: datetime) -> dict:
    system_prompt = (
        "Ты умный ассистент. Текущее время пользователя: " + user_now.strftime("%Y-%m-%d %H:%M") + ".\n"
        "Определи тип сообщения:\n"
        "- note: просто мысль или информация для сохранения\n"
        "- reminder: есть указание времени (через X минут, завтра, в 15:00, и т.п.)\n"
        "- advice: вопрос или просьба совета\n\n"
        "Верни строго JSON:\n"
        "- type: note, reminder или advice\n"
        "- text: красиво сформулированный текст или совет\n"
        "- time_delay_seconds: только для reminder (через сколько секунд от текущего времени), иначе 0"
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error("OpenAI error: " + str(e))
        return {"type": "note", "text": text, "time_delay_seconds": 0}

async def process_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    status_msg = await update.message.reply_text("Думаю...")
    user_now = get_user_now(user_id)
    ai_result = await analyze_with_ai(raw_text, user_now)
    action_type = ai_result.get("type", "note")
    clean_text = ai_result.get("text", raw_text)
    if action_type == "reminder":
        delay_seconds = ai_result.get("time_delay_seconds", 0)
        if delay_seconds <= 0:
            delay_seconds = 60
        remind_time = datetime.now() + timedelta(seconds=delay_seconds)
        remind_time_user = user_now + timedelta(seconds=delay_seconds)
        remind_id = add_reminder(user_id, chat_id, clean_text, remind_time.strftime("%Y-%m-%d %H:%M:%S"))
        context.job_queue.run_once(
            send_reminder_job,
            delay_seconds,
            data={'chat_id': chat_id, 'text': clean_text, 'remind_id': remind_id}
        )
        dt_str = remind_time_user.strftime('%d.%m.%Y в %H:%M')
        await status_msg.edit_text("Напоминание установлено!\n\n" + clean_text + "\n\nВремя: " + dt_str)
    elif action_type == "advice":
        await status_msg.edit_text("Совет:\n\n" + clean_text)
    else:
        add_note(user_id, clean_text)
        await status_msg.edit_text("Сохранил в заметки:\n\n" + clean_text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mode = context.user_data.get('mode', 'default')
    user_id = update.message.from_user.id

    if mode == 'setup_timezone':
        status_msg = await update.message.reply_text("Определяю часовой пояс...")
        tz = await detect_timezone_with_ai(text)
        if tz:
            set_user_timezone(user_id, tz)
            now = get_user_now(user_id)
            context.user_data['mode'] = 'default'
            await status_msg.edit_text("Отлично! Часовой пояс: " + tz + "\nТекущее время: " + now.strftime("%d.%m.%Y %H:%M"))
            await update.message.reply_text("Теперь все напоминания будут по твоему времени!", reply_markup=get_main_keyboard())
        else:
            await status_msg.edit_text("Не смог определить. Попробуй написать иначе, например: Москва, Берлин, Токио")
        return

    if text == "Главное меню":
        context.user_data['mode'] = 'default'
        await update.message.reply_text("Главное меню", reply_markup=get_main_keyboard())
        return
    if text == "Мои заметки":
        await list_notes(update, context)
        return
    if text == "Мои напоминания":
        await list_reminders(update, context)
        return
    if text == "Агент-советник":
        await start_agent(update, context)
        return
    if text == "Мой часовой пояс":
        await show_timezone(update, context)
        return
    if text == "Что ты умеешь?":
        await update.message.reply_text(
            "Я умею:\n\n"
            "1. Сохранять заметки\n"
            "2. Ставить напоминания по твоему времени\n"
            "3. Давать советы\n"
            "4. Распознавать голосовые сообщения\n"
            "5. Агент-советник с памятью диалога\n"
            "6. Удалять заметки\n"
            "7. Авто-удаление напоминаний через 24 часа\n"
            "8. Поддержка часовых поясов",
            reply_markup=get_main_keyboard()
        )
        return
    if mode == 'agent':
        await agent_chat(update, context, text)
        return

    tz = get_user_timezone(user_id)
    if not tz:
        context.user_data['mode'] = 'setup_timezone'
        await update.message.reply_text("Сначала укажи свой город. Напиши название:")
        return

    await process_user_input(update, context, text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get('mode', 'default')
    status_msg = await update.message.reply_text("Слушаю голосовое...")
    file = await update.message.voice.get_file()
    file_path = "voice_" + str(update.message.message_id) + ".ogg"
    await file.download_to_drive(file_path)
    try:
        with open(file_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        os.remove(file_path)
        await status_msg.delete()
        if mode == 'agent':
            await agent_chat(update, context, transcript)
        else:
            await process_user_input(update, context, transcript)
    except Exception as e:
        logger.error("Voice error: " + str(e))
        await status_msg.edit_text("Не удалось распознать голосовое сообщение.")
        if os.path.exists(file_path):
            os.remove(file_path)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("del_note_"):
        note_id = int(data.split("_")[2])
        delete_note(note_id, query.from_user.id)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Заметка удалена!", reply_markup=get_main_keyboard())
        return
    if not data.startswith("snooze_"):
        return
    parts = data.split("_")
    remind_id = int(parts[1])
    action = parts[2]
    if action == "done":
        delete_reminder(remind_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Напоминание завершено.")
        return
    minutes = int(action)
    delay_seconds = minutes * 60
    new_remind_time = datetime.now() + timedelta(seconds=delay_seconds)
    orig_text = query.message.text.replace("НАПОМИНАНИЕ:\n\n", "").strip()
    new_id = add_reminder(query.from_user.id, query.message.chat_id, orig_text, new_remind_time.strftime("%Y-%m-%d %H:%M:%S"))
    context.job_queue.run_once(
        send_reminder_job,
        delay_seconds,
        data={'chat_id': query.message.chat_id, 'text': orig_text, 'remind_id': new_id}
    )
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Отложено на " + str(minutes) + " мин. до " + new_remind_time.strftime('%H:%M'))

async def restore_reminders(application: Application):
    reminders = get_pending_reminders()
    now = datetime.now()
    count = 0
    for rem_id, chat_id, text, remind_at_str in reminders:
        try:
            remind_time = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M:%S")
            delay = (remind_time - now).total_seconds()
            if delay < 0:
                delay = 5.0
            application.job_queue.run_once(
                send_reminder_job,
                delay,
                data={'chat_id': chat_id, 'text': text, 'remind_id': rem_id}
            )
            count += 1
        except Exception as e:
            logger.error("Restore error: " + str(e))
    if count > 0:
        logger.info("Restored " + str(count) + " reminders.")
    application.job_queue.run_repeating(cleanup_job, interval=21600, first=60)

def main():
    init_db()
    application = Application.builder().token(TOKEN).post_init(restore_reminders).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("notes", list_notes))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(CallbackQueryHandler(callback_handler))
    print("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
