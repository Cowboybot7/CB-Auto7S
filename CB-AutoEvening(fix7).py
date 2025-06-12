from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from telegram import Bot
import os
import traceback
import logging
from datetime import datetime
import pytz
import math
import random
from math import radians, sin, cos, sqrt, atan2
import asyncio
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

# Initialize scheduler
scheduler = AsyncIOScheduler(timezone=TIMEZONE)
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

async def perform_scan_in(bot, chat_id, user_id):
    driver = None
    try:
        driver, (lat, lon) = create_driver()
        user_drivers[user_id] = driver
        start_time = datetime.now(TIMEZONE).strftime("%H:%M:%S")
        await bot.send_message(chat_id, f"🕒 Test automation started at {start_time} (ICT)")
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
        screenshot_file = f"test_success_{timestamp}.png"
        table.screenshot(screenshot_file)
        base_lat = float(os.getenv('BASE_LATITUDE', '11.545380'))
        base_lon = float(os.getenv('BASE_LONGITUDE', '104.911449'))
        distance = calculate_distance(base_lat, base_lon, lat, lon)
        with open(screenshot_file, 'rb') as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=(
                    f"✅ Test scan confirmed at {datetime.now(TIMEZONE).strftime('%H:%M:%S')} (ICT)\n"
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
        await bot.send_message(chat_id, f"❌ Test scan failed at {error_time} (ICT): {error_text}")
        logger.error(traceback.format_exc())
        timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
        if driver:
            driver.save_screenshot(f"test_error_{timestamp}.png")
            with open(f"test_page_source_{timestamp}.html", "w") as f:
                f.write(driver.page_source)
            with open(f"test_error_{timestamp}.png", 'rb') as photo:
                await bot.send_photo(chat_id=chat_id, photo=photo, caption="Test error screenshot")
        return False
    finally:
        if driver:
            driver.quit()
            user_drivers.pop(user_id, None)

async def trigger_test_scan():
    logger.info("⚙️ Test auto scan triggered at 13:00 ICT")
    bot = Bot(token=TELEGRAM_TOKEN)
    for user_id in AUTHORIZED_USERS:
        chat_id = int(user_id)
        async def scan_task():
            try:
                await perform_scan_in(bot, chat_id, user_id)
            except Exception as e:
                logger.error(f"Test scan failed for user {user_id}: {str(e)}")
        await scan_task()  # Run sequentially to avoid resource issues

async def schedule_test_scan():
    # Schedule test scan for 13:00 ICT today (June 12, 2025)
    test_time = datetime(2025, 6, 12, 13, 0, 0, tzinfo=TIMEZONE)
    logger.info(f"✅ Scheduling test scan at {test_time.strftime('%Y-%m-%d %H:%M:%S')} ICT")
    scheduler.add_job(
        lambda: asyncio.create_task(trigger_test_scan()),
        DateTrigger(run_date=test_time, timezone=TIMEZONE),
        id='test_scan',
        replace_existing=True
    )
    scheduler.start()

async def main():
    # Initialize Telegram bot
    bot = Bot(token=TELEGRAM_TOKEN)
    for user_id in AUTHORIZED_USERS:
        await bot.send_message(
            chat_id=int(user_id),
            text="🔔 Test script started. Auto scan scheduled for 13:00 ICT."
        )
    # Schedule and run the test scan
    await schedule_test_scan()
    # Keep the script running until the scan completes
    await asyncio.sleep(3600)  # Sleep for 1 hour to ensure scan runs

if __name__ == "__main__":
    asyncio.run(main())
