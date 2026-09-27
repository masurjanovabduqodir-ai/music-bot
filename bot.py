

import os
import asyncio
import logging
import random
import math

import time
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    FSInputFile, 
    BotCommand
)
import aiosqlite
import yt_dlp

# ----------------------------------------------------------------------
# НАСТРОЙКИ
# ----------------------------------------------------------------------
BOT_TOKEN = "8856854400:AAGbXk6AnUawkyp86KSVC5CQxq21v8FXbhk"  # Замените на ваш токен от @BotFather
DB_NAME = "music_cache.db"
DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище временных данных поиска
user_searches = {}

# ----------------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite)
# ----------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                yt_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                performer TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                yt_id TEXT,
                title TEXT,
                performer TEXT,
                PRIMARY KEY (user_id, yt_id)
            )
        """)
        await db.commit()

async def get_cached_file(yt_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT file_id, title, performer FROM cache WHERE yt_id = ?", (yt_id,)) as cursor:
            return await cursor.fetchone()

async def save_to_cache(yt_id: str, file_id: str, title: str, performer: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache (yt_id, file_id, title, performer) VALUES (?, ?, ?, ?)",
            (yt_id, file_id, title, performer)
        )
        await db.commit()

async def add_to_favorites(user_id: int, yt_id: str, title: str, performer: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites (user_id, yt_id, title, performer) VALUES (?, ?, ?, ?)",
            (user_id, yt_id, title, performer)
        )
        await db.commit()

async def remove_from_favorites(user_id: int, yt_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM favorites WHERE user_id = ? AND yt_id = ?", (user_id, yt_id))
        await db.commit()

async def get_user_favorites(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT yt_id, title, performer FROM favorites WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchall()

async def add_favorite(user_id: int, yt_id: str, title: str, performer: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO favorites (user_id, yt_id, title, performer) VALUES (?, ?, ?, ?)",
            (user_id, yt_id, title, performer)
        )
        await db.commit()

# ----------------------------------------------------------------------
# ПРОГРЕСС-БАР И СКАЧИВАНИЕ
# ----------------------------------------------------------------------
def make_progress_bar(percent: float, length: int = 10) -> str:
    """Создаёт красивый прямоугольный ползунок"""
    filled_length = int(round(length * percent / 100))
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {int(percent)}%"

def search_youtube(query: str):
    ydl_opts = {'extract_flat': True, 'skip_download': True, 'quiet': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch15:{query}", download=False)
        entries = info.get('entries', [])
        results = []
        for entry in entries:
            results.append({
                'id': entry.get('id'),
                'title': entry.get('title', 'Без названия'),
                'uploader': entry.get('uploader', 'Исполнитель неизвестен'),
                'duration': entry.get('duration', 0)
            })
        return results

def download_audio_with_progress(yt_id: str, loop, status_msg: types.Message):
    url = f"https://www.youtube.com/watch?v={yt_id}"
    last_text = {"val": ""}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                percent = (downloaded / total) * 100
                bar = make_progress_bar(percent)
                text = f"📥 Загрузка аудио:\n{bar}"
                
                # Обновляем сообщение не слишком часто, чтобы Telegram не забанил за спам
                if text != last_text["val"]:
                    last_text["val"] = text
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(text, parse_mode="Markdown"), loop
                    )
        elif d['status'] == 'finished':
            text = "⚙️ Конвертация в MP3...\n[██████████] 100%"
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(text, parse_mode="Markdown"), loop
            )

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, f"{yt_id}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'progress_hooks': [progress_hook],
        'quiet': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'Аудиозапись')
        uploader = info.get('uploader', 'YouTube')
        
    out_path = os.path.join(DOWNLOAD_DIR, f"{yt_id}.mp3")
    return out_path, title, uploader

# ----------------------------------------------------------------------
# ГЕНЕРАЦИЯ КЛАВИАТУР
# ----------------------------------------------------------------------
def build_search_keyboard(user_id: int, page: int = 1):
    results = user_searches.get(user_id, [])
    items_per_page = 5
    total_pages = max(1, (len(results) + items_per_page - 1) // items_per_page)
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    current_items = results[start_idx:end_idx]

    keyboard = []
    for idx, item in enumerate(current_items, start_idx + 1):
        dur_mins = f"{int(item['duration']) // 60}:{int(item['duration']) % 60:02d}" if item['duration'] else ""
        dur_str = f" [{dur_mins}]" if dur_mins else ""
        btn_text = f"{idx}. {item['title'][:35]}{dur_str}"
        keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"dl:{item['id']}")])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"Стр. {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page + 1}"))

    keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ----------------------------------------------------------------------
# ХЕНДЛЕРЫ КОМАНД
# ----------------------------------------------------------------------
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "👋 Привет! Я твой продвинутый музыкальный бот.\n\n"
        "🔍 Напиши название песни или исполнителя, и я найду варианты.\n"
        "⭐ Используй /favorites для просмотра сохранённых треков.\n"
        "🎲 Напиши /random если не знаешь, что послушать!"
    )

@dp.message(Command("favorites"))
async def favorites_cmd(message: types.Message):
    favs = await get_user_favorites(message.from_user.id)
    if not favs:
        await message.answer("⭐ Ваш список избранного пока пуст. Добавляйте треки кнопкой под отправленной песней!")
        return
    kb = []
    for yt_id, title, performer in favs:
        btn_text = f"🎵 {performer[:15]} - {title[:25]}"
        kb.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"dl:{yt_id}"),
            InlineKeyboardButton(text="❌", callback_data=f"rem_fav:{yt_id}")
        ])

    await message.answer("⭐ Ваши избранные треки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.message(Command("random"))
async def random_cmd(message: types.Message):
    popular_keywords = ["Phonk 2026", "Top Hits 2026", "Lofi Hip Hop", "Deep House", "Rock Hits"]
    query = random.choice(popular_keywords)
    status_msg = await message.answer(f"🎲 Подбираю случайную подборку по запросу: *{query}*...")
    
    results = await asyncio.to_thread(search_youtube, query)
    if results:
        user_searches[message.from_user.id] = results
        kb = build_search_keyboard(message.from_user.id, page=1)
        await status_msg.edit_text(f"🎲 Случайная подборка: _{query}_", reply_markup=kb, parse_mode="Markdown")
    else:
        await status_msg.edit_text("⚠️ Не удалось загрузить случайную подборку.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(
        "📌 Как пользоваться ботом:\n"
        "1. Отправь текст с названием трека.\n"
        "2. Выберите нужный вариант кнопкой.\n"
        "3. Листайте страницы кнопками ⬅️ / ➡️.\n"
        "4. Сохраняйте треки в ⭐ Избранное, чтобы не терять!"
    )

# ----------------------------------------------------------------------
# ПОИСК И ПАГИНАЦИЯ
# ----------------------------------------------------------------------
@dp.message(F.text)
async def handle_search(message: types.Message):
    query = message.text.strip()
    status_msg = await message.answer("🔍 Ищу варианты...")

    try:
        results = await asyncio.to_thread(search_youtube, query)
        if not results:
            await status_msg.edit_text("❌ Ничего не найдено. Попробуй другой запрос.")
            return

        user_searches[message.from_user.id] = results
        kb = build_search_keyboard(message.from_user.id, page=1)
        await status_msg.edit_text(
            f"🎧 Результаты поиска по запросу: _{query}_\nВыберите трек для скачивания:", 
            reply_markup=kb, 
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Search error: {e}")
        await status_msg.edit_text("⚠️ Произошла ошибка при поиске.")

@dp.callback_query(F.data.startswith("page:"))
async def handle_page_change(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    kb = build_search_keyboard(callback.from_user.id, page=page)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "noop")
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()


# ------------------------------------------------------------------
# СКАЧИВАНИЕ И ИЗБРАННОЕ
# ------------------------------------------------------------------
@dp.callback_query(F.data.startswith("dl:"))
async def handle_download(callback: types.CallbackQuery):
    await callback.answer("⏳ Начинаем скачивание...")
    yt_id = callback.data.split(":")[1]

    fav_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Добавить в избранное", callback_data=f"add_fav:{yt_id}")
    ]])

    # Проверка кэша
    cached = await get_cached_file(yt_id)
    if cached:
        file_id, title, performer = cached
        await callback.message.answer_audio(
            audio=file_id,
            caption=f"🎵 *{title}*\n⚡ _Загружено мгновенно из кэша_",
            reply_markup=fav_kb,
            parse_mode="Markdown"
        )
        return

    status_msg = await callback.message.answer("📥 Скачивание трека...")

    try:
        loop = asyncio.get_running_loop()
        file_path, title, performer = await asyncio.to_thread(
            download_audio_with_progress_impl, yt_id, loop, status_msg
        )

        if os.path.exists(file_path):
            await status_msg.edit_text("📤 Отправка файла...")
            audio_file = FSInputFile(file_path)
            sent_msg = await callback.message.answer_audio(
                audio=audio_file,
                title=title,
                performer=performer,
                caption=f"🎵 *{title}*",
                reply_markup=fav_kb,
                parse_mode="Markdown"
            )

            await save_to_cache(yt_id, sent_msg.audio.file_id, title, performer)
            if os.path.exists(file_path):
                os.remove(file_path)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Ошибка: файл не найден после скачивания.")

    except Exception as e:
        logging.error(f"Ошибка скачивания: {e}")
        await status_msg.edit_text(f"❌ Не удалось скачать файл: {e}")


@dp.callback_query(F.data.startswith("add_fav:"))
async def handle_add_favorite(callback: types.CallbackQuery):
    yt_id = callback.data.split(":")[1]
    cached = await get_cached_file(yt_id)
    if cached:
        file_id, title, performer = cached
        await add_favorite(callback.from_user.id, yt_id, title, performer)
        await callback.answer("⭐️ Добавлено в избранное!", show_alert=True)
    else:
        await callback.answer("⚠️ Не удалось добавить в избранное.", show_alert=True)


def download_audio_with_progress_impl(yt_id, loop, status_msg):
    url = f"https://www.youtube.com/watch?v={yt_id}"
    last_text = {"val": ""}
    last_time = {"val": 0.0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                percent = int(downloaded / total * 100)
                filled = int(percent / 10)
                bar = "█" * filled + "░" * (10 - filled)
                text = f"⏳ Скачивание: [{bar}] {percent}%"
                
                # now = time.time()
                import time as t; now = t.time()
                if now - last_time["val"] > 1.5 and last_text["val"] != text:
                    last_text["val"] = text
                    last_time["val"] = now
        elif d['status'] == 'finished':
            text = "⚡ Конвертация в MP3..."
            if last_text["val"] != text:
                last_text["val"] = text

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = os.path.join(DOWNLOAD_DIR, f"{yt_id}.mp3")
        title = info.get('title', 'Без названия')
        performer = info.get('uploader', 'Музыка')
        return file_path, title, performer

import os
from aiohttp import web

async def handle(request):
    return web.Response(text="I am alive!")

app = web.Application()
app.router.add_get("/", handle)

async def web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await init_db()
    print("🤖 Бот успешно запущен!")
    await asyncio.gather(
        web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
