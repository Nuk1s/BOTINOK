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

# Настройка логирования
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
    MAX_VIDEO_AGE_HOURS = 24  # Макс. возраст видео для инициализации
    PORT = int(os.getenv("PORT", 10000))  # Фиксированный порт для Render

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
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return self._create_default_state()
        except Exception as e:
            logger.error(f"Ошибка загрузки: {str(e)}")
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
            except Exception as e:
                logger.error(f"Ошибка сохранения: {str(e)}")

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
            return request.execute()
        except Exception as e:
            logger.error(f"Ошибка YouTube API: {str(e)}")
            return None

class TelegramService:
    @staticmethod
    def send_alert(video_data):
        if not all([Config.TG_TOKEN, Config.TG_CHANNEL]):
            return False

        message = (
            f"🎥 Новое видео!\n\n"
            f"<b>{video_data['title']}</b>\n\n"
            f"Ссылка: https://youtu.be/{video_data['id']}"
        )
        
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage",
                json={'chat_id': Config.TG_CHANNEL, 'text': message, 'parse_mode': 'HTML'},
                timeout=25
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Ошибка Telegram: {str(e)}")
            return False

def check_video_task():
    with lock:
        try:
            response = YouTubeService.get_latest_video()
            if not response or not response.get('items'):
                return

            video = response['items'][0]
            current_id = video['id']['videoId']
            title = video['snippet']['title']
            published_at = datetime.fromisoformat(
                video['snippet']['publishedAt'].replace('Z', '')
            ).replace(tzinfo=timezone.utc)

            if (datetime.now(timezone.utc) - published_at) > timedelta(hours=Config.MAX_VIDEO_AGE_HOURS):
                return

            current_state = state_manager.state
            
            if not current_state['initialized']:
                state_manager.update_and_save({'last_video_id': current_id, 'initialized': True})
                return

            if current_id != current_state['last_video_id'] and current_id != state_manager.last_sent:
                if TelegramService.send_alert({'id': current_id, 'title': title}):
                    state_manager.last_sent = current_id
                    state_manager.update_and_save({'last_video_id': current_id})

        except Exception as e:
            logger.error(f"Ошибка в задаче: {str(e)}")

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
    scheduler.shutdown()
    state_manager.update_and_save(state_manager.state)

def create_app():
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    if not scheduler.running:
        scheduler.start()
    
    check_video_task()  # Первая проверка при старте
    return app

if __name__ == "__main__":
    create_app().run(host='0.0.0.0', port=Config.PORT)
