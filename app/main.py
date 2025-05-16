# app/main.py
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
    TG_TOKEN = os.getenv("TG_TOKEN")
    TG_CHANNEL = os.getenv("TG_CHANNEL")
    YT_KEY = os.getenv("YT_KEY")
    YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID")
    STATE_FILE = "bot_state.json"
    CHECK_INTERVAL = 10  # Интервал проверки в минутах

class StateManager:
    def __init__(self):
        self.state = self._load_state()

    def _load_state(self):
        try:
            with open(Config.STATE_FILE, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded state: {data}")
                return data
        except FileNotFoundError:
            logger.warning("State file not found. Creating new one.")
            self._create_default_state()
            return {'last_video_id': None, 'initialized': False}
        except Exception as e:
            logger.error(f"State load error: {str(e)}")
            return {'last_video_id': None, 'initialized': False}

    def _create_default_state(self):
        with open(Config.STATE_FILE, 'w') as f:
            json.dump({'last_video_id': None, 'initialized': False}, f)

    def save_state(self):
        try:
            with open(Config.STATE_FILE, 'w') as f:
                json.dump(self.state, f)
            logger.info("State saved successfully")
        except Exception as e:
            logger.error(f"State save failed: {str(e)}")

state_manager = StateManager()

@app.route('/')
def health_check():
    return {"status": "running"}, 200

class YouTubeService:
    @staticmethod
    def get_latest_video():
        try:
            youtube = build('youtube', 'v3', developerKey=Config.YT_KEY)
            request = youtube.search().list(
                part="snippet",
                channelId=Config.YT_CHANNEL_ID,
                maxResults=1,
                order="date",
                type="video"
            )
            response = request.execute()
            logger.debug("YouTube API response: %s", response)
            return response
        except HttpError as e:
            logger.error(f"YouTube API Error: {e}")
            return None
        except Exception as e:
            logger.error(f"YouTube Service Error: {e}")
            return None

class TelegramService:
    @staticmethod
    def send_alert(video_data):
        if not all([Config.TG_TOKEN, Config.TG_CHANNEL]):
            logger.error("Telegram credentials not configured!")
            return False
            
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
                timeout=15
            )
            response.raise_for_status()
            logger.info("Message sent to Telegram successfully")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram API Error: {str(e)}")
            if response:
                logger.error(f"Response content: {response.text}")
            return False
        except Exception as e:
            logger.error(f"Telegram Service Error: {str(e)}")
            return False

def check_video_task():
    with lock:
        logger.info("===== Starting video check =====")
        
        # Получаем данные с YouTube
        response = YouTubeService.get_latest_video()
        if not response:
            logger.error("Failed to get YouTube data")
            return

        items = response.get('items')
        if not items:
            logger.warning("No videos found in response")
            return

        try:
            video = items[0]
            current_id = video['id']['videoId']
            title = video['snippet']['title']
            logger.info(f"Latest video: {current_id} - {title}")
        except KeyError as e:
            logger.error(f"Invalid YouTube response structure: {str(e)}")
            return

        # Обработка состояния
        state = state_manager.state
        logger.debug(f"Current state: {state}")

        if not state['initialized']:
            logger.info("Initializing state with first video")
            state['last_video_id'] = current_id
            state['initialized'] = True
            state_manager.save_state()
            return

        if current_id != state['last_video_id']:
            logger.info("New video detected! Sending notification...")
            video_data = {'id': current_id, 'title': title}
            
            if TelegramService.send_alert(video_data):
                state['last_video_id'] = current_id
                state_manager.save_state()
                logger.info("State updated successfully")
            else:
                logger.error("Failed to send notification")
        else:
            logger.info("No new videos found")

scheduler = BackgroundScheduler()
scheduler.add_job(
    check_video_task,
    'interval',
    minutes=Config.CHECK_INTERVAL,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300
)

def graceful_shutdown(signum, frame):
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()
    logger.info("Saving final state...")
    state_manager.save_state()
    logger.info("Shutdown complete")

def create_app():
    # Регистрация обработчиков сигналов
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    # Запуск планировщика
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    
    # Первоначальная проверка
    try:
        check_video_task()
    except Exception as e:
        logger.error(f"Initial check failed: {str(e)}")
    
    return app

if __name__ == "__main__":
    create_app().run(host='0.0.0.0', port=8000)
