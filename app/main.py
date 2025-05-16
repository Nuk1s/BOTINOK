import os
import json
import signal
import logging
import threading
import requests
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
lock = threading.Lock()

class Config:
    TG_TOKEN = os.getenv("8044378203:AAFNVsZlYbiF5W0SX10uxr5W3ZT-WYKpebs")  # ЗАМЕНИТЬ НА os.getenv("TG_TOKEN")
    TG_CHANNEL = os.getenv("@pmchat123")  # ЗАМЕНИТЬ НА os.getenv("TG_CHANNEL")
    YT_KEY = os.getenv("AIzaSyBYNDz9yuLS7To77AXFLcWpVf54j2GK8c8")  # ЗАМЕНИТЬ НА os.getenv("YT_KEY")
    YT_CHANNEL_ID = "UCW8eE7SOnIdRUmidxB--nOg"
    STATE_FILE = "bot_state.json"
    CHECK_INTERVAL = 10

class StateManager:
    def __init__(self):
        self.state = self._load_state()

    def _load_state(self):
        try:
            with open(Config.STATE_FILE, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded state: {data}")
                return {
                    'last_video_id': data.get('last_video_id'),
                    'initialized': data.get('initialized', False)
                }
        except Exception as e:
            logger.warning(f"State init: {str(e)}")
            return {'last_video_id': None, 'initialized': False}

    def save_state(self):
        try:
            with open(Config.STATE_FILE, 'w') as f:
                json.dump(self.state, f)
            logger.info(f"Saved state: {self.state}")
        except Exception as e:
            logger.error(f"State save failed: {str(e)}")

state_manager = StateManager()

@app.route('/')
def health_check():
    return {
        "status": "running",
        "last_checked": state_manager.state['last_video_id'] or "never"
    }, 200

class YouTubeService:
    @staticmethod
    def get_latest_video():
        try:
            youtube = build('youtube', 'v3', developerKey=Config.YT_KEY)
            request = youtube.search().list(
                part="id,snippet",
                channelId=Config.YT_CHANNEL_ID,
                maxResults=1,
                order="date",
                type="video"
            )
            return request.execute()
        except HttpError as e:
            logger.error(f"YouTube API error: {str(e)}")
            return None

class TelegramService:
    @staticmethod
    def send_alert(video_data):
        message = (
            f"🎥 Новое видео!\n\n"
            f"<b>{video_data['title']}</b>\n\n"
            f"Ссылка: https://youtu.be/{video_data['id']}"
        )
        
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage",
                json={
                    'chat_id': Config.TG_CHANNEL,
                    'text': message,
                    'parse_mode': 'HTML'
                },
                timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {str(e)}")
            return False

def check_video_task():
    with lock:
        logger.info("Starting video check...")
        
        response = YouTubeService.get_latest_video()
        if not response or not response.get('items'):
            logger.warning("No videos found")
            return

        video = response['items'][0]
        current_id = video['id']['videoId']
        logger.info(f"Current video ID: {current_id}")

        state = state_manager.state
        
        if not state['initialized']:
            state['last_video_id'] = current_id
            state['initialized'] = True
            state_manager.save_state()
            logger.info("Initialization complete")
            return

        if current_id != state['last_video_id']:
            logger.info(f"New video detected: {current_id}")
            
            video_data = {
                'id': current_id,
                'title': video['snippet']['title']
            }
            
            if TelegramService.send_alert(video_data):
                state['last_video_id'] = current_id
                state_manager.save_state()
            else:
                logger.error("Notification failed, state not updated")
        else:
            logger.info("No new videos")

def setup_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_video_task,
        'interval',
        minutes=Config.CHECK_INTERVAL,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1
    )
    return scheduler

scheduler = setup_scheduler()

def graceful_shutdown(signum, frame):
    logger.info("Shutting down...")
    scheduler.shutdown()
    state_manager.save_state()
    logger.info("Service stopped")

def create_app():
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    scheduler.start()
    check_video_task()
    return app

if __name__ == "__main__":
    create_app().run(host='0.0.0.0', port=8000)
