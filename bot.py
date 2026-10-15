import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, Message,
                           MessageOriginChannel)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from yt_dlp import YoutubeDL

# ====== SOZLAMALAR (env orqali) ======
BOT_TOKEN = os.environ["8958554064:AAGLTMOA05G4Yi8DNc9ijmFpjk2SjgfBbAs"]
OWNER_ID = int(os.environ["8738813282"])          # asosiy admin Telegram ID
MAX_MB = int(os.environ.get("MAX_MB", "550"))    # oddiy Bot API limiti 50 MB
PORT = int(os.environ.get("PORT", "8080"))      # Hosting uchun port
DB = "bot.db"

URL_RE = re.compile(r"https?://\S+")
pending: dict[str, str] = {}                    # "chat:msg" -> url
router = Router()


# ====== VEB SERVER (Hosting uchun aldamchi kod) ======
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h1>Bot muvaffaqiyatli ishlamoqda!</h1><p>Telegram bot faol holatda.</p>")

    def log_message(self, format, *args):
        pass  # Konsolni keraksiz loglar bilan to'ldirmaslik uchun

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), DummyHandler)
    server.serve_forever()


# ====== BAZA ======
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, downloads INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS channels(chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        defaults = {
            "welcome": "Salom! Menga Instagram, YouTube, TikTok va boshqa tarmoqlardan havola yuboring — video yoki audio qilib beraman.",
            "forced_sub": "1",
            "caption": "",
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
        await db.commit()


async def get_setting(key):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r[0] if r else ""


async def set_setting(key, value):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
        await db.commit()


async def q(sql, args=(), one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        async with db.execute(sql, args) as c:
            rows = None if commit else (await c.fetchone() if one else await c.fetchall())
        if commit:
            await db.commit()
        return rows


async def is_admin(uid):
    if uid == OWNER_ID:
        return True
    return bool(await q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True))


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return await is_admin(event.from_user.id)


# ====== MAJBURIY OBUNA ======
async def unsubscribed(bot: Bot, uid: int):
    if await get_setting("forced_sub") != "1":
        return []
    result = []
    for chat_id, title, link in await q("SELECT chat_id,title,link FROM channels"):
        try:
            m = await bot.get_chat_member(chat_id, uid)
            if m.status in ("left", "kicked"):
                result.append((title, link))
        except Exception:
            pass  
    return result


def sub_keyboard(chs):
    kb = InlineKeyboardBuilder()
    for title, link in chs:
        kb.button(text=f"➕ {title}", url=link)
    kb.button(text="✅ Tekshirish", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()


# ====== YUKLASH ======
def _download(url: str, audio: bool):
    tmp = tempfile.mkdtemp()
    opts = {
        "outtmpl": f"{tmp}/%(title).60s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": MAX_MB * 1024 * 1024,
    }
    if audio:
        opts.update(
            format="bestaudio/best",
            postprocessors=[{"key": "FFmpegExtractAudio",
                             "preferredcodec": "mp3", "preferredquality": "192"}],
        )
    else:
        opts.update(format="bv*[height<=720]+ba/b[height<=720]/b",
                    merge_output_format="mp4")
    with YoutubeDL(opts) as y:
        y.download([url])
    files = glob.glob(f"{tmp}/*")
    return tmp, (files[0] if files else None)


# ====== FOYDALANUVCHI ======
@router.message(CommandStart())
async def start(m: Message):
    await q("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,), commit=True)
    await m.answer(await get_setting("welcome"))


@router.message(F.text.regexp(URL_RE))
async def got_link(m: Message, bot: Bot):
    await q("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,), commit=True)
    chs = await unsubscribed(bot, m.from_user.id)
    if chs:
        return await m.answer("Botdan foydalanish uchun kanallarga obuna bo'ling:",
                              reply_markup=sub_keyboard(chs))
    url = URL_RE.search(m.text).group(0)
    key = f"{m.chat.id}:{m.message_id}"
    pending[key] = url
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Video", callback_data=f"dl:v:{m.message_id}")
    kb.button(text="🎵 Audio (MP3)", callback_data=f"dl:a:{m.message_id}")
    await m.reply("Qaysi formatda yuklab beray?", reply_markup=kb.as_markup())


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    chs = await unsubscribed(bot, c.from_user.id)
    if chs:
        return await c.answer("Hali hamma kanalga obuna bo'lmadingiz ❌", show_alert=True)
    await c.message.edit_text("✅ Rahmat! Endi havolani yuboring.")


@router.callback_query(F.data.startswith("dl:"))
async def download(c: CallbackQuery, bot: Bot):
    _, kind, mid = c.data.split(":")
    key = f"{c.message.chat.id}:{mid}"
    url = pending.get(key)
    if not url:
        return await c.answer("Havola eskirgan, qayta yuboring.", show_alert=True)
    if await unsubscribed(bot, c.from_user.id):
        return await c.answer("Avval obuna bo'ling!", show_alert=True)
    await c.message.edit_text("⏳ Yuklanmoqda...")
    tmp = None
    try:
        loop = asyncio.get_running_loop()
        tmp, path = await loop.run_in_executor(None, _download, url, kind == "a")
        if not path or os.path.getsize(path) > MAX_MB * 1024 * 1024:
            return await c.message.edit_text(f"❌ Fayl topilmadi yoki {MAX_MB} MB dan katta.")
        caption = await get_setting("caption") or None
        f = FSInputFile(path)
        if kind == "a":
            await bot.send_audio(c.message.chat.id, f, caption=caption)
        else:
            await bot.send_video(c.message.chat.id, f, caption=caption)
        await q("UPDATE users SET downloads=downloads+1 WHERE id=?",
                (c.from_user.id,), commit=True)
        await c.message.delete()
    except Exception as e:
        logging.exception(e)
        await c.message.edit_text("❌ Yuklab bo'lmadi. Havola to'g'riligini tekshiring.")
    finally:
        pending.pop(key, None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# ====== ADMIN PANEL ======
admin = Router()
admin.message.filter(IsAdmin())
admin
