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

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

app = Flask(__name__)
app_lock = threading.Lock()

class Config:
    TG_TOKEN = os.getenv("TG_TOKEN")
    TG_CHANNEL = os.getenv("TG_CHANNEL")
    YT_KEY = os.getenv("YT_KEY")
    YT_CHANNEL_ID = os.getenv("YT_CHANNEL_ID")
    STATE_FILE = "bot_state.json"
    CHECK_INTERVAL = 10
    MAX_VIDEO_AGE = timedelta(hours=24)
    PORT = int(os.getenv("PORT", 5000))  # Исправлено значение по умолчанию

class StateManager:
    def __init__(self):
        self._state = self._load_state()
    
    def _load_state(self):
        try:
            with open(Config.STATE_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'last_video_id': None, 'initialized': False}
    
    def update(self, new_state):
        with app_lock:
            self._state.update(new_state)
            try:
                with open(Config.STATE_FILE, 'w') as f:
                    json.dump(self._state, f, indent=2)
            except Exception as e:
                logger.error(f"Ошибка сохранения: {str(e)}")

state_manager = StateManager()

@app.route('/')
def health_check():
    return {"status": "OK", "port": Config.PORT}, 200

def youtube_fetch():
    try:
        youtube = build('youtube', 'v3', developerKey=Config.YT_KEY)
        req = youtube.search().list(
            part="snippet",
            channelId=Config.YT_CHANNEL_ID,
            maxResults=1,
            order="date",
            type="video"
        )
        return req.execute()
    except Exception as e:
        logger.error(f"Ошибка YouTube: {str(e)}")
        return None

def telegram_send(video_data):
    if not all([Config.TG_TOKEN, Config.TG_CHANNEL]):
        return False
    
    message = f"🎥 Новое видео!\n<b>{video_data['title']}</b>\nhttps://youtu.be/{video_data['id']}"
    
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
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Ошибка Telegram: {str(e)}")
        return False

def check_task():
    with app_lock:
        try:
            data = youtube_fetch()
            if not data or not data.get('items'):
                return

            video = data['items'][0]
            video_id = video['id']['videoId']
            published = datetime.fromisoformat(
                video['snippet']['publishedAt'].replace('Z', '') + '+00:00'
            )

            if (datetime.now(timezone.utc) - published) > Config.MAX_VIDEO_AGE:
                return

            current_state = state_manager.state

            if not current_state['initialized']:
                state_manager.update({'last_video_id': video_id, 'initialized': True})
                return

            if video_id != current_state['last_video_id']:
                if telegram_send({'id': video_id, 'title': video['snippet']['title']}):
                    state_manager.update({'last_video_id': video_id})

        except Exception as e:
            logger.error(f"Ошибка проверки: {str(e)}")

scheduler = BackgroundScheduler()

def shutdown_handler(signum, frame):
    logger.info("Завершение работы...")
    scheduler.shutdown()

def create_app():
    if not os.environ.get("GUNICORN_WORKER"):
        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)
        
        scheduler.add_job(
            check_task,
            'interval',
            minutes=Config.CHECK_INTERVAL,
            max_instances=1
        )
        
        try:
            if not scheduler.running:
                scheduler.start()
                logger.info(f"Сервер запущен на порту: {Config.PORT}")
                check_task()
        except Exception as e:
            logger.error(f"Ошибка запуска: {str(e)}")

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host='0.0.0.0', port=Config.PORT)
