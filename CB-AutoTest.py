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
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from math import radians, sin, cos, sqrt, atan2

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6373.0  # Earth radius in kilometers
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return round(R * c * 1000, 1)  # Convert to meters and round
  
# Configuration
USERNAME = os.getenv('USERNAME')
PASSWORD = os.getenv('PASSWORD')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
AUTHORIZED_USERS = [uid.strip() for uid in os.getenv('AUTHORIZED_USERS', '').split(',') if uid.strip()]
TIMEZONE = pytz.timezone('Asia/Bangkok')
BASE_LATITUDE = float(os.getenv('BASE_LATITUDE', '11.545380'))
BASE_LONGITUDE = float(os.getenv('BASE_LONGITUDE', '104.911449'))
MAX_DEVIATION_METERS = 150
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

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
logger.info(f"TEST_MODE: {TEST_MODE}")

def generate_random_coordinates():
    """Generate random coordinates within MAX_DEVIATION_METERS of base location"""
    radius_deg = MAX_DEVIATION_METERS / 111320
    angle = math.radians(random.uniform(0, 360))
    distance = random.uniform(0, radius_deg)
    new_lat = BASE_LATITUDE + (distance * math.cos(angle))
    new_lon = BASE_LONGITUDE + (distance * math.sin(angle))
    return new_lat, new_lon

def create_driver():
    options = Options()
    options.binary_location = '/usr/bin/chromium'
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
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

async def run_auto_scan_for_user(app, user_id, chat_id):
    """Helper function to run scan for a specific user"""
    try:
        logger.info(f"Starting auto scan for user {user_id}")
        await perform_scan_in(
            app.bot, 
            chat_id, 
            user_id, 
            {"cancelled": False},
            is_auto=True
        )
    except Exception as e:
        logger.error(f"Auto scan failed for user {user_id}: {str(e)}")

async def trigger_auto_scan(app):
    logger.info("⚙️ Auto scan triggered")
    if not auto_scan_enabled:
        logger.info("🚫 Auto scan skipped (paused by user)")
        return

    for user_id in AUTHORIZED_USERS:
        chat_id = int(user_id)
        if user_id in user_scan_tasks and not user_scan_tasks[user_id].done():
            logger.info(f"⚠️ User {user_id} already has an active scan task. Skipping.")
            continue

        task = asyncio.create_task(run_auto_scan_for_user(app, user_id, chat_id))
        user_scan_tasks[user_id] = task
        logger.info(f"🔧 Created auto scan task for user {user_id}")

def schedule_daily_scan(application):
    def schedule_evening_afternoon_scans():
        now = datetime.now(TIMEZONE)
        weekday = now.weekday()

        # TEST MODE: Schedule for 9:00 AM today
        if TEST_MODE:
            logger.info("🔧 TEST_MODE ENABLED: Scheduling scan at 09:00 AM ICT")
            hour = 9
            minute = 0
            day_filter = '*'  # Run every day
            reminder_hour = 8
            reminder_minute = 0
        else:
            if weekday <= 4:  # Mon-Fri
                hour = 18
                minute = random.randint(0, 27)
                if minute < 1:
                    hour = 17
                    minute += 59
            elif weekday == 5:  # Saturday
                hour = 12
                minute = random.randint(7, 17)
            else:  # Sunday
                logger.info("🛌 Sunday - No scan scheduled")
                return
            day_filter = 'mon-fri' if weekday <= 4 else 'sat'
            logger.info(f"✅ Scheduled auto scan at {hour:02d}:{minute:02d} ICT")
            reminder_hour = hour - 1
            reminder_minute = minute

        # Schedule auto scan
        scheduler.add_job(
            trigger_auto_scan,
            CronTrigger(
                day_of_week=day_filter,
                hour=hour,
                minute=minute
            ),
            args=[application],
            id='daily_random_scan',
            replace_existing=True
        )

        # Schedule reminder
        async def send_reminders():
            for user_id in AUTHORIZED_USERS:
                try:
                    message = (
                        f"🔔 [TEST] Reminder: Auto scan will run at {hour:02d}:{minute:02d} ICT"
                        if TEST_MODE else
                        f"🔔 Reminder: Auto scan will run at {hour:02d}:{minute:02d} ICT (in 1 hour)"
                    )
                    await application.bot.send_message(
                        chat_id=int(user_id),
                        text=message
                    )
                except Exception as e:
                    logger.warning(f"Failed to send reminder to {user_id}: {e}")

        scheduler.add_job(
            send_reminders,
            CronTrigger(
                day_of_week=day_filter,
                hour=reminder_hour,
                minute=reminder_minute
            ),
            id='daily_reminder',
            replace_existing=True
        )

        # For TEST_MODE: Add immediate notification
        if TEST_MODE:
            async def notify_schedule():
                for user_id in AUTHORIZED_USERS:
                    job = scheduler.get_job('daily_random_scan')
                    if job and job.next_run_time:
                        time_str = job.next_run_time.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
                        await application.bot.send_message(
                            chat_id=int(user_id),
                            text=f"⏰ [TEST] Auto scan scheduled at: {time_str} (ICT)"
                        )
            scheduler.add_job(
                notify_schedule,
                'date',
                run_date=datetime.now(TIMEZONE) + timedelta(seconds=5)
            )

    # Start scheduler
    schedule_evening_afternoon_scans()
    scheduler.start()

async def perform_scan_in(bot, chat_id, user_id, cancel_flag, is_auto=False):
    driver = None
    try:
        driver, (lat, lon) = create_driver()
        user_drivers[user_id] = driver
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        
        if not is_auto:
            await bot.send_message(chat_id, f"🕒 Automation started at {start_time} (ICT)")
        
        # Automation steps
        wait = WebDriverWait(driver, 15)
        
        # Step 1: Login
        logger.info("Navigating to login page")
        driver.get("https://tinyurl.com/ajrjyvx9")
        
        username_field = wait.until(EC.visibility_of_element_located((By.ID, "txtUserName")))
        username_field.send_keys(USERNAME)
        logger.info("Username entered")

        password_field = driver.find_element(By.ID, "txtPassword")
        password_field.send_keys(PASSWORD)
        logger.info("Password entered")

        driver.find_element(By.ID, "btnSignIn").click()
        logger.info("Processing login...")

        wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "small-box")))
        logger.info("Login successful")

        # Step 2: Navigate to Attendance
        attendance_xpath = "//div[contains(@class,'small-box bg-aqua')]//h3[text()='Attendance']/ancestor::div[contains(@class,'small-box')]"
        attendance_card = wait.until(EC.presence_of_element_located((By.XPATH, attendance_xpath)))
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", attendance_card)
        time.sleep(1)
        more_info_link = attendance_card.find_element(By.XPATH, ".//a[contains(@href, 'ATT/frmclock.aspx')]")
        more_info_link.click()
        logger.info("Clicked 'More info'")

        # Step 3: Clock In
        clock_in_xpath = "//a[contains(@href, 'frmclockin.aspx') and contains(., 'Clock In')]"
        clock_in_link = wait.until(EC.element_to_be_clickable((By.XPATH, clock_in_xpath)))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", clock_in_link)
        time.sleep(0.5)
        clock_in_link.click()
        logger.info("Clicked Clock In link")

        # Step 4: Enable Scan In
        scan_in_btn = wait.until(EC.presence_of_element_located((By.ID, "ctl00_maincontent_btnScanIn")))
        if scan_in_btn.get_attribute("disabled"):
            driver.execute_script("arguments[0].disabled = false;", scan_in_btn)
            time.sleep(0.5)
        scan_in_btn.click()
        logger.info("Processing scan-in...")

        # Step 5: Verify completion
        WebDriverWait(driver, 15).until(EC.url_contains("frmclock.aspx"))
        
        # Capture result
        table_xpath = "//table[@id='ctl00_maincontent_GVList']//tr[contains(., 'Head Office')]"
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, table_xpath)))
        table = driver.find_element(By.ID, "ctl00_maincontent_GVList")
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", table)
        driver.execute_script("arguments[0].style.border='3px solid #00ff00';", table)
        time.sleep(0.5)

        # Save and send screenshot
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        screenshot_file = f"success_{timestamp}.png"
        table.screenshot(screenshot_file)

        distance = calculate_distance(BASE_LATITUDE, BASE_LONGITUDE, lat, lon)
        with open(screenshot_file, 'rb') as photo:
            caption = (
                f"✅ Successful scan at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)\n"
                f"📍 Location: {lat:.6f}, {lon:.6f}\n"
                f"📏 Distance: {distance}m\n"
                f"🗺 [View on Map](https://maps.google.com/maps?q={lat},{lon})"
            )
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode="Markdown"
            )

        return True

    except Exception as e:
        error_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"❌ Failed at {error_time} (ICT): {str(e)}")
        logger.error(traceback.format_exc())
        
        if driver:
            timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
            driver.save_screenshot(f"error_{timestamp}.png")
            with open(f"error_{timestamp}.png", 'rb') as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption="Error screenshot")
        return False
        
    finally:
        if driver and user_id in user_drivers:
            driver.quit()
            del user_drivers[user_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Attendance Bot Ready! Use /test to trigger test scan")

async def test_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to immediately trigger test scan"""
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ Unauthorized")
        return
        
    await update.message.reply_text("🔧 Starting TEST scan immediately...")
    await run_auto_scan_for_user(context.application, user_id, chat_id)

async def next_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job = scheduler.get_job('daily_random_scan')
    if job and job.next_run_time:
        time_str = job.next_run_time.astimezone(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
        await update.message.reply_text(f"⏰ Next auto scan at: {time_str} (ICT)")
    else:
        await update.message.reply_text("⚠️ No upcoming scans scheduled")

async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_scan))
    application.add_handler(CommandHandler("next", next_scan))
    
    # Initialize application
    await application.initialize()
    await application.start()
    
    # Setup scheduler
    schedule_daily_scan(application)
    
    # Notify test mode status
    if TEST_MODE:
        for user_id in AUTHORIZED_USERS:
            try:
                await application.bot.send_message(
                    chat_id=int(user_id),
                    text="🔧 TEST MODE ACTIVE: Scans scheduled for 9:00 AM ICT"
                )
            except Exception as e:
                logger.error(f"Failed to send test notification: {e}")
    
    # Keep application running
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        logger.error(traceback.format_exc())