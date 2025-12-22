import os
import asyncio
import logging
import aiohttp
import aiofiles
import json
from threading import Thread
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ChatAction
import yt_dlp

def _get_env(key, default=None, required=False, cast=None):
    val = os.environ.get(key, None if default is None else str(default))
    if val is None and required:
        raise RuntimeError(f"Environment variable {key} is required")
    if val is None:
        return None
    if cast:
        try:
            return cast(val)
        except Exception as e:
            raise RuntimeError(f"Failed to cast env {key}: {e}")
    return val

class YTDLPLogger:
    def __init__(self):
        self._messages = []
    def debug(self, msg):
        self._messages.append(("DEBUG", str(msg)))
    def info(self, msg):
        self._messages.append(("INFO", str(msg)))
    def warning(self, msg):
        self._messages.append(("WARNING", str(msg)))
    def error(self, msg):
        self._messages.append(("ERROR", str(msg)))
    def text(self):
        return "\n".join(f"[{lvl}] {m}" for lvl, m in self._messages) if self._messages else ""

COOKIES_TXT_CONTENT = _get_env("COOKIES_TXT_CONTENT")
COOKIES_TXT_PATH = "cookies.txt"

def setup_cookies_file():
    if COOKIES_TXT_CONTENT:
        try:
            with open(COOKIES_TXT_PATH, "w") as f:
                f.write(COOKIES_TXT_CONTENT)
            logging.info(f"Successfully created {COOKIES_TXT_PATH} from COOKIES_TXT_CONTENT.")
        except Exception as e:
            logging.error(f"Failed to write COOKIES_TXT_CONTENT to file: {e}")
    elif not os.path.exists(COOKIES_TXT_PATH):
        open(COOKIES_TXT_PATH, "a").close()
        logging.warning(f"COOKIES_TXT_CONTENT not set. Created empty {COOKIES_TXT_PATH}.")

API_ID = _get_env("API_ID", required=True, cast=int)
API_HASH = _get_env("API_HASH", required=True)
BOT1_TOKEN = _get_env("BOT_TOKEN", required=True)
PORT = _get_env("PORT", 8080, cast=int)

DOWNLOAD_PATH = _get_env("DOWNLOAD_PATH", "downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = _get_env("MAX_CONCURRENT_DOWNLOADS", 5, cast=int)
MAX_VIDEO_DURATION_YOUTUBE = _get_env("MAX_VIDEO_DURATION_YOUTUBE", 600, cast=int)

BOT_USERNAME = None

YDL_OPTS = {
    "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "outtmpl": os.path.join(DOWNLOAD_PATH, "%(title)s.%(ext)s"),
    "noplaylist": True,
    "quiet": True,
    "cookiefile": COOKIES_TXT_PATH
}

SUPPORTED_DOMAINS = ["youtube.com", "youtu.be"]

pyro_client = Client("youtube_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT1_TOKEN)
flask_app = Flask(__name__)

active_downloads = 0
queue = None
lock = None

async def ensure_primitives():
    global queue, lock
    if queue is None:
        queue = asyncio.Queue()
    if lock is None:
        lock = asyncio.Lock()

async def get_bot_username(client):
    global BOT_USERNAME
    if BOT_USERNAME is None:
        try:
            me = await client.get_me()
            BOT_USERNAME = me.username
        except Exception:
            return "Bot"
    return BOT_USERNAME

async def download_thumbnail(url, target_path):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    f = await aiofiles.open(target_path, mode='wb')
                    await f.write(await resp.read())
                    await f.close()
                    if os.path.exists(target_path):
                        return target_path
    except:
        pass
    return None

def extract_metadata_from_info(info):
    width = info.get("width")
    height = info.get("height")
    duration = info.get("duration")
    if not width or not height:
        formats = info.get("formats") or []
        best = None
        for f in formats:
            if f.get("width") and f.get("height"):
                best = f
                break
        if best:
            if not width:
                width = best.get("width")
            if not height:
                height = best.get("height")
            if not duration:
                dms = best.get("duration_ms")
                duration = info.get("duration") or (dms / 1000 if dms else None)
    return width, height, duration

async def download_video(url: str, bot_username: str):
    loop = asyncio.get_running_loop()
    try:
        lowered = url.lower()
        is_youtube = "youtube.com" in lowered or "youtu.be" in lowered
        if not is_youtube:
            return ("UNSUPPORTED",)
        ydl_opts = YDL_OPTS.copy()
        logger = YTDLPLogger()
        def extract_info_sync():
            opts = ydl_opts.copy()
            opts["logger"] = logger
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        try:
            info = await loop.run_in_executor(None, extract_info_sync)
        except Exception as e:
            text = logger.text() or str(e)
            return ("YTDLP_ERROR", text)
        width, height, duration = extract_metadata_from_info(info)
        limit = MAX_VIDEO_DURATION_YOUTUBE
        if duration and duration > limit:
            return ("TOO_LONG", limit)
        def download_sync():
            opts = ydl_opts.copy()
            opts["logger"] = logger
            with yt_dlp.YoutubeDL(opts) as ydl:
                info_dl = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info_dl)
                return info_dl, filename
        try:
            info, filename = await loop.run_in_executor(None, download_sync)
        except Exception as e:
            text = logger.text() or str(e)
            return ("YTDLP_ERROR", text)
        title = info.get("title") or ""
        caption = title or f"@{bot_username}"
        if len(caption) > 1024:
            caption = caption[:1024]
        thumb = None
        thumb_url = info.get("thumbnail")
        if thumb_url:
            thumb_path = os.path.splitext(filename)[0] + ".jpg"
            thumb = await download_thumbnail(thumb_url, thumb_path)
        return caption, filename, width, height, duration, thumb
    except Exception as e:
        logging.exception(e)
        return "ERROR"

async def _download_worker(client, message, url):
    bot_username = await get_bot_username(client)
    try:
        await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    except:
        pass
    attempts = 0
    max_attempts = 2
    result = None
    while attempts < max_attempts:
        try:
            result = await download_video(url, bot_username)
        except Exception:
            result = "ERROR"
        if result == "ERROR" or (isinstance(result, tuple) and result and result[0] == "YTDLP_ERROR") or result is None or (isinstance(result, tuple) and result[0] == "UNSUPPORTED"):
            attempts += 1
            try:
                await asyncio.sleep(1)
            except:
                pass
            continue
        break
    if isinstance(result, tuple) and result[0] == "TOO_LONG":
        yt_limit = int(MAX_VIDEO_DURATION_YOUTUBE / 60)
        try:
            await message.reply(
                f"Cannot download videos longer than {yt_limit} minutes."
            )
        except:
            pass
    elif result is None:
        try:
            await message.reply("Cannot download this video 👍")
        except:
            pass
    elif isinstance(result, tuple) and result[0] == "YTDLP_ERROR":
        error_text = result[1] or "Unknown yt-dlp error"
        try:
            if len(error_text) > 4000:
                error_text = error_text[:3990] + "\n... (truncated)"
            await message.reply(f"yt-dlp error log:\n\n{error_text}")
        except:
            try:
                await message.reply("yt-dlp produced an error but it could not be sent.")
            except:
                pass
    elif result == "ERROR":
        try:
            await message.reply("An unexpected error occurred.")
        except:
            pass
    else:
        caption, file_path, width, height, duration, thumb = result
        try:
            await client.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
        except:
            pass
        kwargs = {"video": file_path, "caption": caption, "supports_streaming": True}
        if width: kwargs["width"] = int(width)
        if height: kwargs["height"] = int(height)
        if duration: kwargs["duration"] = int(float(duration))
        if thumb and os.path.exists(thumb): kwargs["thumb"] = thumb
        sent_video = False
        try:
            await client.send_video(message.chat.id, **kwargs)
            sent_video = True
        except Exception:
            logging.exception("Sending video failed")
        for f in [file_path, thumb]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

async def download_task_wrapper(client, message, url):
    global active_downloads
    await ensure_primitives()
    async with lock:
        active_downloads += 1
    try:
        await _download_worker(client, message, url)
    finally:
        async with lock:
            active_downloads -= 1
        await start_next_download()

async def start_next_download():
    await ensure_primitives()
    global active_downloads
    async with lock:
        while not queue.empty() and active_downloads < MAX_CONCURRENT_DOWNLOADS:
            client, message, url = await queue.get()
            asyncio.create_task(download_task_wrapper(client, message, url))

@pyro_client.on_message(filters.private & filters.command("start"))
async def start(client, message: Message):
    await message.reply("""Welcome 👋

This bot downloads YouTube videos.

Send a YouTube link to download the video.""")

@pyro_client.on_message(filters.private & filters.text)
async def handle_link(client, message: Message):
    url = message.text.strip()
    if not any(domain in url.lower() for domain in SUPPORTED_DOMAINS):
        await message.reply("Please send a valid YouTube link 👍")
        return
    await ensure_primitives()
    async with lock:
        if active_downloads < MAX_CONCURRENT_DOWNLOADS:
            asyncio.create_task(download_task_wrapper(client, message, url))
        else:
            await queue.put((client, message, url))

@flask_app.route("/", methods=["GET"])
def keep_alive():
    html = (
        "<!doctype html>"
        "<html>"
        "<head>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>"
        "body{font-family:Arial,Helvetica,sans-serif;margin:0;padding:0;display:flex;align-items:center;justify-content:center;height:100vh;background:#f7f7f7}"
        ".card{background:#fff;padding:20px;border-radius:12px;box-shadow:0 6px 18px rgba(0,0,0,0.08);text-align:center;width:90%;max-width:420px}"
        ".status{font-size:18px;margin-top:8px;color:#333}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='card'><div style='font-size:28px'>✅</div><div class='status'>YouTube downloader bot is running</div></div>"
        "</body>"
        "</html>"
    )
    return html, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(PORT))

def run_bot():
    pyro_client.run()

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    setup_cookies_file()
    Thread(target=run_flask, daemon=True).start()
    run_bot()
