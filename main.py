from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from typing import List
from playwright.async_api import async_playwright
import httpx
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

class UploadRequest(BaseModel):
    image_urls: List[str]   # Telegram CDN URLs
    caption: str
    telegram_chat_id: str   # Để Railway báo lại kết quả cho user
    telegram_bot_token: str

@app.get("/")
def health():
    return {"status": "Railway TikTok Upload Service is running"}

@app.post("/upload-tiktok")
async def upload_tiktok(payload: UploadRequest, bg: BackgroundTasks):
    bg.add_task(do_upload, payload)
    return {"status": "queued", "images": len(payload.image_urls)}

async def notify_telegram(bot_token: str, chat_id: str, message: str):
    """Gửi thông báo kết quả về Telegram cho user."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": message
        })

async def do_upload(payload: UploadRequest):
    session_id = os.getenv("TIKTOK_SESSION_ID")
    if not session_id:
        logger.error("TIKTOK_SESSION_ID not set")
        await notify_telegram(
            payload.telegram_bot_token,
            payload.telegram_chat_id,
            "❌ TikTok upload failed: session not configured"
        )
        return

    # Download ảnh từ Telegram CDN về /tmp
    img_paths = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i, url in enumerate(payload.image_urls):
            try:
                logger.info(f"Downloading image {i+1}/{len(payload.image_urls)} from: {url}")
                r = await client.get(url)
                r.raise_for_status()  # ✅ Kiểm tra status (throw 4xx/5xx)
                
                # Validate file size
                if len(r.content) == 0:
                    logger.warning(f"Image {i+1} is empty (0 bytes)")
                    continue
                    
                path = f"/tmp/tiktok_img_{i}.jpg"
                with open(path, "wb") as f:
                    f.write(r.content)
                img_paths.append(path)
                logger.info(f"✅ Downloaded image {i+1}/{len(payload.image_urls)} ({len(r.content)} bytes)")
            except Exception as e:
                logger.error(f"❌ Failed to download image {i}: {e}")

    if not img_paths:
        await notify_telegram(
            payload.telegram_bot_token,
            payload.telegram_chat_id,
            "❌ TikTok upload failed: could not download images"
        )
        return

    # Upload lên TikTok bằng Playwright
    try:
        await upload_to_tiktok(img_paths, payload.caption, session_id)
        await notify_telegram(
            payload.telegram_bot_token,
            payload.telegram_chat_id,
            f"✅ Đã đăng {len(img_paths)} ảnh lên TikTok thành công!"
        )
    except Exception as e:
        logger.error(f"TikTok upload failed: {e}")
        await notify_telegram(
            payload.telegram_bot_token,
            payload.telegram_chat_id,
            f"❌ TikTok upload failed: {str(e)}"
        )
    finally:
        # Cleanup /tmp
        for p in img_paths:
            try:
                os.remove(p)
            except:
                pass

async def upload_to_tiktok(img_paths: list, caption: str, session_id: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        # Inject TikTok session cookie
        await context.add_cookies([
            {"name": "sessionid", "value": session_id, "domain": ".tiktok.com", "path": "/"},
            {"name": "sessionid_ss", "value": session_id, "domain": ".tiktok.com", "path": "/"},
        ])

        page = await context.new_page()
        
        # Vào trang upload TikTok
        await page.goto("https://www.tiktok.com/creator-center/upload?lang=en", wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Click tab "Image" / "Photo"
        try:
            await page.click("div[data-e2e='upload-image-tab']", timeout=5000)
        except:
            try:
                await page.click("text=Photo", timeout=5000)
            except:
                logger.warning("Could not find Photo tab, trying direct upload")

        await page.wait_for_timeout(2000)

        # Upload ảnh
        logger.info(f"Looking for file input to upload {len(img_paths)} images")
        file_input = page.locator("input[type='file']").first
        try:
            await file_input.wait_for(timeout=60000)  # ✅ Chờ element tồn tại
            logger.info("File input element found, uploading...")
            await file_input.set_input_files(img_paths, timeout=60000)  # ✅ Tăng timeout
            logger.info(f"✅ Successfully uploaded {len(img_paths)} images to TikTok form")
        except Exception as e:
            logger.error(f"❌ File input error: {e}")
            # Debug screenshot
            await page.screenshot(path="/tmp/tiktok_debug.png")
            logger.info("Screenshot saved to /tmp/tiktok_debug.png")
            raise
        await page.wait_for_timeout(5000)

        # Nhập caption
        try:
            caption_box = page.locator("[data-e2e='caption-input']").first
            await caption_box.click()
            await caption_box.fill(caption)
        except:
            logger.warning("Caption input not found, skipping")

        await page.wait_for_timeout(2000)

        # Click Post
        try:
            await page.click("button[data-e2e='post-button']", timeout=5000)
        except:
            await page.click("button:has-text('Post')", timeout=5000)

        await page.wait_for_timeout(5000)
        logger.info("TikTok upload completed")
        await browser.close()
