from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler
import os
import traceback
import logging
import time
from datetime import datetime, timedelta
import pytz
import math
import random
from telegram import BotCommand
from math import radians, sin, cos, sqrt, atan2
import asyncio
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# Configuration
USERNAME = os.getenv('USERNAME')
PASSWORD = os.getenv('PASSWORD')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
AUTHORIZED_USERS = [uid.strip() for uid in os.getenv('AUTHORIZED_USERS', '').split(',') if uid.strip()]
TIMEZONE = pytz.timezone('Asia/Bangkok')
BASE_LATITUDE = float(os.getenv('BASE_LATITUDE', '11.545380'))
BASE_LONGITUDE = float(os.getenv('BASE_LONGITUDE', '104.911449'))
MAX_DEVIATION_METERS = 150

# Initialize application and scheduler
application = Application.builder().token(TELEGRAM_TOKEN).build()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
auto_scan_enabled = True
user_scan_tasks = {}
user_drivers = {}

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6373.0
    lat1 = radians(lat1)
    lon1 = radians(lon1)
    lat2 = radians(lat2)
    lon2 = radians(lon2)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return round(R * c * 1000, 1)

def generate_random_coordinates():
    radius_deg = MAX_DEVIATION_METERS / 111320
    angle = math.radians(random.uniform(0, 360))
    distance = random.uniform(0, radius_deg)
    new_lat = BASE_LATITUDE + (distance * math.cos(angle))
    new_lon = BASE_LONGITUDE + (distance * math.sin(angle))
    return new_lat, new_lon

def create_driver():
    options = Options()
    options.binary_location = '/usr/bin/chromium-browser'  # Render-specific path
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--allow-geolocation')
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.geolocation": 1,
    })
    service = Service(executable_path='/usr/bin/chromedriver')
    driver = webdriver.Chrome(service=service, options=options)
    lat, lon = generate_random_coordinates()
    logger.info(f"Using coordinates: {lat:.6f}, {lon:.6f}")
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": lat,
        "longitude": lon,
        "accuracy": 100
    })
    return driver, (lat, lon)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    task = user_scan_tasks.get(user_id)
    if task and not task.done():
        await context.bot.send_message(chat_id, "⛔ Cancelling scan-in process...")
        task.cancel()
        driver = user_drivers.get(user_id)
        if driver:
            timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
            screenshot_path = f"cancelled_{timestamp}.png"
            driver.save_screenshot(screenshot_path)
            with open(screenshot_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=f"🚫 Operation cancelled at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)"
                )
            driver.quit()
            user_drivers.pop(user_id, None)
        else:
            await context.bot.send_message(chat_id, "⚠️ No active browser session found.")
    else:
        await context.bot.send_message(chat_id, "ℹ️ No active scan-in process to cancel.")

async def next_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response_lines = []
    job = scheduler.get_job('daily_random_scan')
    if job and job.next_run_time:
        time_str = job.next_run_time.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
        response_lines.append(f"🕒 Auto Scan-In → {time_str} (ICT)")
    else:
        response_lines.append("⚠️ Auto Scan-In not scheduled.")
    await update.message.reply_text("📅 Scheduled Times:\n" + "\n".join(response_lines))

async def trigger_auto_scan():
    logger.info("⚙️ Auto scan triggered")
    if not auto_scan_enabled:
        logger.info("🚫 Auto scan skipped (paused by user)")
        return
    for user_id in AUTHORIZED_USERS:
        chat_id = int(user_id)
        if user_id in user_scan_tasks and not user_scan_tasks[user_id].done():
            logger.info(f"Skipping user {user_id}: scan already in progress")
            continue
        async def scan_task():
            try:
                await perform_scan_in(application.bot, chat_id, user_id, {"cancelled": False})
            except Exception as e:
                logger.error(f"Auto scan failed for user {user_id}: {str(e)}")
        task = asyncio.create_task(scan_task())
        user_scan_tasks[user_id] = task

def schedule_daily_scan():
    def get_random_scan_time():
        now = datetime.now(TIMEZONE)
        weekday = now.weekday()
        if weekday <= 4:  # Monday–Friday: 17:59–18:27
            hour = 18
            minute = random.randint(0, 27)
            if minute < 1:
                hour = 17
                minute += 59
        elif weekday == 5:  # Saturday: 12:07–12:17
            hour = 12
            minute = random.randint(7, 17)
        else:  # Sunday: No scan
            return None
        scan_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scan_time <= now:  # If time is in the past, schedule for next day
            scan_time += timedelta(days=1)
        return scan_time

    async def schedule_scan():
        scan_time = get_random_scan_time()
        if not scan_time:
            logger.info("🛌 Sunday – No scan scheduled")
            return
        logger.info(f"✅ Scheduling auto scan at {scan_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")
        scheduler.add_job(
            lambda: asyncio.create_task(trigger_auto_scan()),
            DateTrigger(run_date=scan_time, timezone=TIMEZONE),
            id='daily_random_scan',
            replace_existing=True
        )
        tomorrow = datetime.now(TIMEZONE) + timedelta(days=1)
        next_schedule_time = tomorrow.replace(hour=6, minute=0, second=0, microsecond=0)
        scheduler.add_job(
            schedule_scan,
            DateTrigger(run_date=next_schedule_time, timezone=TIMEZONE),
            id='daily_rescheduler',
            replace_existing=True
        )

    asyncio.create_task(schedule_scan())
    scheduler.start()

async def pause_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_scan_enabled
    auto_scan_enabled = False
    await update.message.reply_text("⛔ Auto scan-in paused.")

async def resume_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_scan_enabled
    auto_scan_enabled = True
    await update.message.reply_text("✅ Auto scan-in resumed.")

async def perform_scan_in(bot, chat_id, user_id, cancel_flag):
    driver = None
    try:
        driver, (lat, lon) = create_driver()
        user_drivers[user_id] = driver
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"🕒 Automation started at {start_time} (ICT)")
        await bot.send_message(chat_id, "🚀 Starting browser automation...")
        wait = WebDriverWait(driver, 15)
        await bot.send_message(chat_id, "🌐 Navigating to login page")
        driver.get("https://tinyurl.com/ajrjyvx9")
        username_field = wait.until(EC.visibility_of_element_located((By.ID, "txtUserName")))
        username_field.send_keys(USERNAME)
        await bot.send_message(chat_id, "👤 Username entered")
        password_field = driver.find_element(By.ID, "txtPassword")
        password_field.send_keys(PASSWORD)
        await bot.send_message(chat_id, "🔑 Password entered")
        driver.find_element(By.ID, "btnSignIn").click()
        await bot.send_message(chat_id, "🔄 Processing login...")
        wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "small-box")))
        await bot.send_message(chat_id, "✅ Login successful")
        await bot.send_message(chat_id, "🔍 Finding attendance card...")
        attendance_xpath = "//div[contains(@class,'small-box bg-aqua')]//h3[text()='Attendance']/ancestor::div[contains(@class,'small-box')]"
        attendance_card = wait.until(EC.presence_of_element_located((By.XPATH, attendance_xpath)))
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", attendance_card)
        time.sleep(1)
        more_info_link = attendance_card.find_element(By.XPATH, ".//a[contains(@href, 'ATT/frmclock.aspx')]")
        more_info_link.click()
        await bot.send_message(chat_id, "✅ Clicked 'More info'")
        await bot.send_message(chat_id, "⏳ Waiting for Clock In link...")
        clock_in_xpath = "//a[contains(@href, 'frmclockin.aspx') and contains(., 'Clock In')]"
        clock_in_link = wait.until(EC.element_to_be_clickable((By.XPATH, clock_in_xpath)))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clock_in_link)
        time.sleep(0.5)
        clock_in_link.click()
        await bot.send_message(chat_id, "✅ Clicked Clock In link")
        await bot.send_message(chat_id, "🔍 Locating Scan In button...")
        scan_in_btn = wait.until(EC.presence_of_element_located((By.ID, "ctl00_maincontent_btnScanIn")))
        if scan_in_btn.get_attribute("disabled"):
            driver.execute_script("arguments[0].disabled = false;", scan_in_btn)
            time.sleep(0.5)
        scan_in_btn.click()
        await bot.send_message(chat_id, "🔄 Processing scan-in...")
        await bot.send_message(chat_id, "⏳ Verifying scan completion...")
        WebDriverWait(driver, 15).until(EC.url_contains("frmclock.aspx"))
        await bot.send_message(chat_id, "📸 Capturing attendance record...")
        table_xpath = "//table[@id='ctl00_maincontent_GVList']//tr[contains(., 'Head Office')]"
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, table_xpath)))
        table = driver.find_element(By.ID, "ctl00_maincontent_GVList")
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", table)
        driver.execute_script("arguments[0].style.border='3px solid #00ff00';", table)
        time.sleep(0.5)
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        screenshot_file = f"success_{timestamp}.png"
        table.screenshot(screenshot_file)
        base_lat = float(os.getenv('BASE_LATITUDE', '11.545380'))
        base_lon = float(os.getenv('BASE_LONGITUDE', '104.911449'))
        distance = calculate_distance(base_lat, base_lon, lat, lon)
        with open(screenshot_file, 'rb') as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=(
                    f"✅ Successful scan confirmed at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)\n"
                    f"📍 *Location:* `{lat:.6f}, {lon:.6f}`\n"
                    f"📏 *Distance from Office:* {distance}m\n"
                    f"🗺 [View on Map](https://maps.google.com/maps?q={lat},{lon})"
                ),
                parse_mode="Markdown"
            )
        return True
    except Exception as e:
        error_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        error_text = str(e).strip() or "Unknown error"
        await bot.send_message(chat_id, f"❌ Failed at {error_time} (ICT): {error_text}")
        logger.error(traceback.format_exc())
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        if driver:
            driver.save_screenshot(f"error_{timestamp}.png")
            with open(f"page_source_{timestamp}.html", "w") as f:
                f.write(driver.page_source)
            with open(f"error_{timestamp}.png", 'rb') as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption="Error screenshot")
        return False
    finally:
        if driver and not cancel_flag["cancelled"]:
            driver.quit()
            user_drivers.pop(user_id, None)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Attendance Bot Ready!\n"
        "Use /scanin to trigger the automation process"
    )

async def scanin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Unauthorized")
        return
    if user_id in user_scan_tasks and not user_scan_tasks[user_id].done():
        await update.message.reply_text("⚠️ Scan in progress. Use /cancel")
        return
    async def scan_task():
        try:
            await context.bot.send_message(chat_id, "⏳ Starting scan...")
            success = await perform_scan_in(context.bot, chat_id, user_id, {"cancelled": False})
        except asyncio.CancelledError:
            await cancel(update, context)
        finally:
            if user_id in user_drivers:
                user_drivers[user_id].quit()
                user_drivers.pop(user_id, None)
    task = asyncio.create_task(scan_task())
    user_scan_tasks[user_id] = task

async def handle_health_check(request):
    return web.Response(text="OK")

async def handle_telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="OK")

async def handle_root(request):
    return web.Response(text="Bot is running")

async def main():
    await application.initialize()
    commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("scanin", "Manual scan-in"),
        BotCommand("cancel", "Cancel current scan"),
        BotCommand("pause_auto", "Pause daily auto scan"),
        BotCommand("resume_auto", "Resume daily auto scan"),
        BotCommand("next", "Show next auto scan-in time"),
    ]
    await application.bot.set_my_commands(commands)
    schedule_daily_scan()
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health_check)
    app.router.add_post("/webhook", handle_telegram_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))  # Render default port
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        raise ValueError("WEBHOOK_URL environment variable not set")
    await application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
    webhook_info = await application.bot.get_webhook_info()
    logger.info(f"Webhook Info: {webhook_info}")
    if webhook_info.url != webhook_url:
        logger.error(f"Webhook URL mismatch: {webhook_info.url} != {webhook_url}")
    else:
        logger.info("✅ Webhook successfully set")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
