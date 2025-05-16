# app/main.py
import os
import json
import signal
import logging
import threading
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.DEBUG,
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
        self._state = self._load_state()
        self.last_sent = None

    @property
    def state(self):
        return self._state.copy()

    def _load_state(self):
        try:
            with open(Config.STATE_FILE, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded state: {json.dumps(data, indent=2)}")
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("Initializing new state")
            return self._create_default_state()
        except Exception as e:
            logger.error(f"Load error: {str(e)}")
            return self._create_default_state()

    def _create_default_state(self):
        return {'last_video_id': None, 'initialized': False}

    def update_and_save(self, new_state):
        with lock:
            self._state.update(new_state)
            try:
                with open(Config.STATE_FILE, 'w') as f:
                    json.dump(self._state, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                logger.debug(f"Saved state: {json.dumps(self._state, indent=2)}")
            except Exception as e:
                logger.error(f"Save error: {str(e)}")

state_manager = StateManager()

@app.route('/')
def health_check():
    return {
        "status": "running",
        "last_video": state_manager.state['last_video_id'],
        "initialized": state_manager.state['initialized']
    }, 200

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
            logger.debug(f"YouTube API Response: {json.dumps(response, indent=2)}")
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
            logger.error("Missing Telegram credentials!")
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
                timeout=25
            )
            response.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            logger.error(f"Telegram HTTP Error: {e.response.text}")
        except Exception as e:
            logger.error(f"Telegram Send Error: {str(e)}")
        return False

def check_video_task():
    with lock:
        logger.info("\n" + "="*40)
        logger.info("Starting video check")

        response = YouTubeService.get_latest_video()
        if not response or not response.get('items'):
            return

        try:
            video = response['items'][0]
            current_id = video['id']['videoId']
            title = video['snippet']['title']
            published_str = video['snippet']['publishedAt'].replace('Z', '')
            published_at = datetime.fromisoformat(published_str).replace(tzinfo=timezone.utc)
        except KeyError as e:
            logger.error(f"Invalid YouTube response: {e}")
            return

        current_time = datetime.now(timezone.utc)
        time_diff = current_time - published_at
        
        # Проверка возраста видео (максимум 2 интервала проверки)
        max_age = timedelta(minutes=Config.CHECK_INTERVAL * 2)
        if time_diff > max_age:
            logger.warning(f"Skipping old video ({time_diff} old)")
            return

        current_state = state_manager.state
        
        # Инициализация состояния при первом запуске
        if not current_state['initialized']:
            logger.info("Initializing state")
            state_manager.update_and_save({
                'last_video_id': current_id,
                'initialized': True
            })
            current_state = state_manager.state  # Обновляем локальное состояние
            return

        # Основная логика проверки
        if current_id != current_state['last_video_id'] and current_id != state_manager.last_sent:
            logger.info(f"New video detected: {current_id}")
            if TelegramService.send_alert({'id': current_id, 'title': title}):
                state_manager.last_sent = current_id
                state_manager.update_and_save({'last_video_id': current_id})
                logger.info("State updated successfully")
            else:
                logger.error("Notification failed")
        else:
            logger.info("No new videos")

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
    logger.info("\nShutting down...")
    scheduler.shutdown(wait=False)
    state_manager.update_and_save(state_manager.state)
    logger.info("Service stopped")

def create_app():
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    
    try:
        check_video_task()
    except Exception as e:
        logger.error(f"Initial check failed: {e}")
    
    return app

if __name__ == "__main__":
    create_app().run(host='0.0.0.0', port=8000)
