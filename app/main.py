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

# Инициализация логгера
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
    CHECK_INTERVAL = 10  # минут
    MAX_VIDEO_AGE = timedelta(hours=24)
    PORT = int(os.getenv("PORT", 5000))  # Важно для Render

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
                logger.error(f"State save error: {str(e)}")
    
    @property
    def state(self):
        return self._state.copy()

state_manager = StateManager()

@app.route('/')
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}, 200

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
        logger.error(f"YouTube API Error: {e}")
        return None

def telegram_send(video_data):
    if not all([Config.TG_TOKEN, Config.TG_CHANNEL]):
        return False
    
    message = f"🎥 New Video!\n<b>{video_data['title']}</b>\nhttps://youtu.be/{video_data['id']}"
    
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage",
            json={'chat_id': Config.TG_CHANNEL, 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram Error: {str(e)}")
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
            logger.error(f"Task failed: {str(e)}")

scheduler = BackgroundScheduler()
scheduler.add_job(
    check_task,
    'interval',
    minutes=Config.CHECK_INTERVAL,
    max_instances=1
)

def shutdown_handler(signum, frame):
    logger.info("Shutting down...")
    scheduler.shutdown()
    logger.info("Scheduler stopped")

def create_app():
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
        check_task()
    
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host='0.0.0.0', port=Config.PORT)
