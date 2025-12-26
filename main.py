import os
import asyncio
import uuid
import shutil
import json
import yt_dlp
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

API_TOKEN = os.environ[""]
WEBHOOK_BASE = os.environ.get("WEBHOOK_URL", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
app = FastAPI()
TELEGRAM_API = f"https://api.telegram.org/bot{API_TOKEN}"

def is_shorts(u: str):
    uu = u.lower()
    return "youtube.com/shorts/" in uu or "youtu.be/" in uu and "shorts" in uu

def download_shorts_sync(url: str):
    name = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{name}.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        title = info.get("title") or ""
        thumb_url = info.get("thumbnail")
        return filename, title, thumb_url

def download_file_sync(url: str, target: str):
    try:
        r = requests.get(url, timeout=15, stream=True)
        if r.status_code == 200:
            with open(target, "wb") as f:
                shutil.copyfileobj(r.raw, f)
            return True
    except:
        pass
    return False

def send_video_sync(chat_id: int, video_path: str, caption: str, thumb_path: str | None):
    url = f"{TELEGRAM_API}/sendVideo"
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true"}
    files = {}
    vf = open(video_path, "rb")
    files["video"] = ("video.mp4", vf)
    tf = None
    if thumb_path and os.path.exists(thumb_path):
        tf = open(thumb_path, "rb")
        files["thumb"] = ("thumb.jpg", tf)
    try:
        r = requests.post(url, data=data, files=files, timeout=600)
        return r.status_code, r.text
    finally:
        try:
            vf.close()
        except:
            pass
        try:
            if tf:
                tf.close()
        except:
            pass

async def process_message(chat_id: int, text: str):
    if not is_shorts(text):
        await asyncio.to_thread(requests.post, f"{TELEGRAM_API}/sendMessage", data={"chat_id": chat_id, "text": "Fadlan YouTube Shorts link soo dir"})
        return
    await asyncio.to_thread(requests.post, f"{TELEGRAM_API}/sendChatAction", data={"chat_id": chat_id, "action": "upload_video"})
    async with semaphore:
        try:
            filename, title, thumb_url = await asyncio.to_thread(download_shorts_sync, text)
        except Exception as e:
            await asyncio.to_thread(requests.post, f"{TELEGRAM_API}/sendMessage", data={"chat_id": chat_id, "text": "Download failed"})
            return
        thumb_path = None
        if thumb_url:
            thumb_path = os.path.splitext(filename)[0] + ".thumb.jpg"
            ok = await asyncio.to_thread(download_file_sync, thumb_url, thumb_path)
            if not ok:
                thumb_path = None
        await asyncio.to_thread(send_video_sync, chat_id, filename, title, thumb_path)
        try:
            if os.path.exists(filename):
                os.remove(filename)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
        except:
            pass

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != API_TOKEN:
        raise HTTPException(status_code=403)
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return {"ok": True}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text") or message.get("caption") or ""
    if not chat_id or not text:
        return {"ok": True}
    asyncio.create_task(process_message(chat_id, text.strip()))
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<html><body><h3>Bot running</h3></body></html>"

async def set_webhook():
    if not WEBHOOK_BASE:
        return
    webhook_url = WEBHOOK_BASE.rstrip("/") + f"/webhook/{API_TOKEN}"
    await asyncio.to_thread(requests.post, f"{TELEGRAM_API}/setWebhook", data={"url": webhook_url})

@app.on_event("startup")
async def startup():
    await set_webhook()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
