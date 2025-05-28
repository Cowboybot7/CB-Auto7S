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
from datetime import datetime, timedelta
from datetime import time as dt_time
import time
import pytz
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import math
import random
from telegram import BotCommand
from math import radians, sin, cos, sqrt, atan2
from selenium.common.exceptions import TimeoutException
from threading import Lock
from asyncio import create_task
from aiohttp import web
import asyncio

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculate distance in meters between two coordinates using Haversine formula
    """
    R = 6373.0  # Earth radius in kilometers

    lat1 = radians(lat1)
    lon1 = radians(lon1)
    lat2 = radians(lat2)
    lon2 = radians(lon2)

    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))

    return round(R * c * 1000, 1)  # Convert to meters and round
  
# Configuration
USERNAME = os.getenv('PMD_USERNAME')
PASSWORD = os.getenv('PMD_PASSWORD')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
AUTHORIZED_USERS = [uid.strip() for uid in os.getenv('AUTHORIZED_USERS', '').split(',') if uid.strip()]
TIMEZONE = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Bangkok'))
BASE_LATITUDE = float(os.getenv('BASE_LATITUDE', '11.545380'))
BASE_LONGITUDE = float(os.getenv('BASE_LONGITUDE', '104.911449'))
MAX_DEVIATION_METERS = 150
CHAT_ID = os.getenv('CHAT_ID')

active_drivers = {}
driver_lock = Lock()
scan_tasks = {}
is_auto_scan_running = False

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def generate_random_coordinates():
    """Generate random coordinates within MAX_DEVIATION_METERS of base location"""
    # Convert meters to degrees (approximate)
    radius_deg = MAX_DEVIATION_METERS / 111320  # 1 degree ≈ 111,320 meters
    
    # Random direction (0-360 degrees)
    angle = math.radians(random.uniform(0, 360))
    
    # Random distance (0 to max deviation)
    distance = random.uniform(0, radius_deg)
    
    # Calculate new coordinates
    new_lat = BASE_LATITUDE + (distance * math.cos(angle))
    new_lon = BASE_LONGITUDE + (distance * math.sin(angle))
    
    return new_lat, new_lon

def create_driver():
    options = Options()
    options.binary_location = '/usr/bin/chromium'
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
    
    # Generate random coordinates
    lat, lon = generate_random_coordinates()
    logger.info(f"Using coordinates: {lat:.6f}, {lon:.6f}")
    
    # Set randomized geolocation
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": lat,
        "longitude": lon,
        "accuracy": 100
    })
    return driver, (lat, lon)
    
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Send reminder about upcoming mission"""
    try:
        chat_id = os.getenv('CHAT_ID')
        next_run = context.job.data  # ONLY THIS LINE NEEDED
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Reminder: Auto mission scheduled at {next_run.strftime('%H:%M')} ICT (in 1 hour)"
        )
        logger.info(f"Sent reminder for {next_run}")
    except Exception as e:
        logger.error(f"Reminder error: {str(e)}")
        
async def auto_scanin_job(context: ContextTypes.DEFAULT_TYPE):
    global is_auto_scan_running
    last_run_key = "last_auto_run"
    current_time = datetime.now(TIMEZONE)

    # Prevent auto scan-in from running more than once every 30 minutes
    last_run = context.bot_data.get(last_run_key)
    if last_run and (current_time - last_run).total_seconds() < 1800:
        logger.warning("⚠️ Skipping auto scan-in: last run was too recent.")
        return
    context.bot_data[last_run_key] = current_time

    if is_auto_scan_running:
        logger.warning("⚠️ Skipping auto mission: already running.")
        return
    is_auto_scan_running = True

    try:
        chat_id = os.getenv('CHAT_ID')
        logger.info("🔄 Starting automated mission...")
        await context.bot.send_message(chat_id, "⏰ Starting automated mission...")
        success = await perform_scan_in(context.bot, chat_id)
    except Exception as e:
        logger.error(f"Automated mission failed: {str(e)}")
        await context.bot.send_message(chat_id, f"❌ Automated mission failed: {str(e)}")
    finally:
        is_auto_scan_running = False
        logger.info("✅ Mission automatically job completed. Cleaning up and scheduling next.")
        for name in ["auto_scanin_morning", "auto_scanin_afternoon", "reminder_morning", "reminder_afternoon"]:
            for job in context.job_queue.get_jobs_by_name(name):
                job.schedule_removal()

        schedule_next_scan(context.job_queue)
        
def schedule_next_scan(job_queue):
    """Schedule both morning and afternoon scan-ins for the next Saturday."""
    # Remove existing scan-in and reminder jobs
    for name in ["auto_scanin_morning", "auto_scanin_afternoon", "reminder_morning", "reminder_afternoon"]:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    now = TIMEZONE.localize(datetime.now())

    # Define both time slots
    saturday_slots = [
        ("morning", 7, 45, 57),
        ("afternoon", 13, 7, 27),
    ]

    # Find the next Saturday
    for i in range(1, 8):
        target_day = now + timedelta(days=i)
        if target_day.weekday() == 5:
            next_saturday = target_day
            break

    for scan_type, hour, min_start, min_end in saturday_slots:
        minute = random.randint(min_start, min_end)
        run_time = TIMEZONE.localize(datetime.combine(next_saturday.date(), dt_time(hour, minute)))

        if run_time <= now:
            continue

        delay = (run_time - now).total_seconds()
        reminder_time = run_time - timedelta(hours=1)
        reminder_delay = (reminder_time - now).total_seconds()

        job_queue.run_once(auto_scanin_job, when=delay, name=f"auto_scanin_{scan_type}")
        logger.info(f"✅ Scheduled {scan_type} scan-in at {run_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")

        if reminder_delay > 0:
            job_queue.run_once(send_reminder, when=reminder_delay, data=run_time, name=f"reminder_{scan_type}")
            logger.info(f"⏰ Reminder for {scan_type} set at {reminder_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")
    
async def cancelauto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this command")
        return

    # Cancel all auto scan-in and reminder jobs (both morning and afternoon)
    job_names = [
        "auto_scanin_morning", "auto_scanin_afternoon",
        "reminder_morning", "reminder_afternoon"
    ]
    jobs = []
    for name in job_names:
        jobs += context.job_queue.get_jobs_by_name(name)

    if not jobs:
        await update.message.reply_text("ℹ️ No scheduled auto missions to cancel")
        return

    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text(
        "🚫 All Saturday auto scan-ins and reminders canceled.\n"
        "⚠️ Reminder: You can still manually trigger with /letgo.",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Unauthorized")
        return

    task = scan_tasks.get(chat_id)
    with driver_lock:
        driver = active_drivers.get(chat_id)

    if not task and not driver:
        await update.message.reply_text("ℹ️ No active operation to cancel")
        return

    if task:
        task.cancel()
        scan_tasks.pop(chat_id, None)

    if driver:
        try:
            filename = f"cancelled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            driver.save_screenshot(filename)
            with open(filename, 'rb') as photo:
                await update.message.reply_photo(photo=photo)
            driver.quit()
            await update.message.reply_text(f"🛑 Operation cancelled with screenshot: {filename}")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error during cancellation: {str(e)}")
        finally:
            del active_drivers[chat_id]
            if os.path.exists(filename):
                os.remove(filename)
    else:
        await update.message.reply_text("✅ Operation cancelled")
            
# Update post_init
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Show welcome message"),
        BotCommand("letgo", "Initiate mission"),
        BotCommand("cancelauto", "Cancel next auto mission and reschedule"),
        BotCommand("cancel", "Cancel ongoing operation")
    ])
    schedule_next_scan(application.job_queue)  # Start scheduling
    
async def perform_scan_in(bot, chat_id, context=None):
    driver, (lat, lon) = create_driver()
    screenshot_file = None
    with driver_lock:
        active_drivers[chat_id] = driver
    try:
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"🕒 Automation started at {start_time} (ICT)")
        
        # Step 1: Login
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
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

        # Step 2: Navigate to Attendance
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
        await bot.send_message(chat_id, "🔍 Finding attendance card...")
        attendance_xpath = "//div[contains(@class,'small-box bg-aqua')]//h3[text()='Attendance']/ancestor::div[contains(@class,'small-box')]"
        attendance_card = wait.until(EC.presence_of_element_located((By.XPATH, attendance_xpath)))
        
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", attendance_card)
        time.sleep(1)
        more_info_link = attendance_card.find_element(By.XPATH, ".//a[contains(@href, 'ATT/frmclock.aspx')]")
        more_info_link.click()
        await bot.send_message(chat_id, "✅ Clicked 'More info'")

        # Step 3: Clock In
        with driver_lock:
            if chat_id not in active_drivers:
                await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                return False
        try:
            await bot.send_message(chat_id, "⏳ Waiting for Clock In link...")
            clock_in_xpath = "//a[contains(@href, 'frmclockin.aspx') and contains(., 'Clock In')]"
            clock_in_link = wait.until(EC.element_to_be_clickable((By.XPATH, clock_in_xpath)))
            
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clock_in_link)
            time.sleep(0.5)
            clock_in_link.click()
            await bot.send_message(chat_id, "✅ Clicked Clock In link")
            
        except Exception as e:
            await bot.send_message(chat_id, f"⏰ Clock-in error: {str(e)}")
            raise
            
        # Step 4: Enable Scan In
        try:
            with driver_lock:  # ADD THIS CHECK
                if chat_id not in active_drivers:
                    await bot.send_message(chat_id, "⚠️ Operation cancelled by user")
                    return False
            await bot.send_message(chat_id, "🔍 Locating Scan In button...")
            scan_in_btn = wait.until(EC.presence_of_element_located((By.ID, "ctl00_maincontent_btnScanIn")))
            
            # Scroll to button for visibility
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", scan_in_btn)
            time.sleep(0.5)
            
            if scan_in_btn.get_attribute("disabled"):
                driver.execute_script("arguments[0].disabled = false;", scan_in_btn)
                time.sleep(0.5)
        
            scan_in_btn.click()
            await bot.send_message(chat_id, "🔄 Processing mission...")
            
            # Add timeout fallback
            WebDriverWait(driver, 25).until(
                EC.url_contains("frmclock.aspx")
            )
        except TimeoutException:
            await bot.send_message(chat_id, "❌ Timeout waiting for Mission button")
            driver.save_screenshot("timeout_mission.png")
            raise
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Mission Error: {str(e)}")
            raise
        # Step 5: Capture attendance table screenshot
        await bot.send_message(chat_id, "📸 Capturing attendance record...")
        
        # Wait for table to load with fresh data
        table_xpath = "//table[@id='ctl00_maincontent_GVList']//tr[contains(., 'Head Office')]"
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, table_xpath))
        )
        # Scroll to table and highlight
        table = driver.find_element(By.ID, "ctl00_maincontent_GVList")
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", table)
        driver.execute_script("arguments[0].style.border='3px solid #00ff00';", table)
        time.sleep(0.5)  # Allow border animation

        # Capture screenshot
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        screenshot_file = f"success_{timestamp}.png"
        table.screenshot(screenshot_file)  # Direct table capture

        # Send confirmation with screenshot
        base_lat = float(os.getenv('BASE_LATITUDE', '11.545380'))
        base_lon = float(os.getenv('BASE_LONGITUDE', '104.911449'))
        distance = calculate_distance(base_lat, base_lon, lat, lon)
        with open(screenshot_file, 'rb') as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=(
                  f"✅ Successful mission confirmed at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)\n"
                  f"📍 *Location:* `{lat:.6f}, {lon:.6f}`\n"
                  f"📏 *Distance from Office:* {distance}m\n"
                  f"🗺 [View on Map](https://maps.google.com/maps?q={lat},{lon})"
                ),
                parse_mode="Markdown"
            )

        return True

    except Exception as e:
        error_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"❌ Failed at {error_time} (ICT): {str(e)}")
        logger.error(traceback.format_exc())
        
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        driver.save_screenshot(f"error_{timestamp}.png")
        with open(f"page_source_{timestamp}.html", "w") as f:
            f.write(driver.page_source)
            
        with open(f"error_{timestamp}.png", 'rb') as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo, caption="Error screenshot")
        return False
    finally:
        # Cleanup code
        with driver_lock:
            if chat_id in active_drivers:
                del active_drivers[chat_id]
        if screenshot_file and os.path.exists(screenshot_file):
            try:
                os.remove(screenshot_file)
            except Exception as e:
                logger.error(f"Error cleaning screenshot: {str(e)}")
        driver.quit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    await update.message.reply_text(
        "🚀 Mission Bot Ready!\n"
        "Use /letgo to trigger the automation process"
    )

async def letgo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this bot")
        return

    if chat_id in scan_tasks:
        await update.message.reply_text("⚠️ A mission is already in progress. Use /cancel to stop it.")
        return

    await update.message.reply_text("⏳ Mission task started in background...")
    
    async def task_wrapper():
        try:
            success = await perform_scan_in(context.bot, chat_id)
            if success:
                await context.bot.send_message(chat_id, "✅ Mission process completed successfully!")
            else:
                await context.bot.send_message(chat_id, "❌ Mission failed. Check previous messages for details.")
        finally:
            scan_tasks.pop(chat_id, None)

    task = create_task(task_wrapper())
    scan_tasks[chat_id] = task

async def handle_health_check(request):
    return web.Response(text="OK")
    
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("letgo", letgo))
    application.add_handler(CommandHandler("cancelauto", cancelauto))
    application.add_handler(CommandHandler("cancel", cancel))
    application.post_init = post_init
    await post_init(application)
    
    # Start health check server
    app = web.Application()
    app.router.add_get("/healthz", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()
    print("✅ Health check route available at /healthz")

    # Start bot manually
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("🤖 Bot polling started...")

    try:
        await asyncio.Event().wait()  # Keep alive forever
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
