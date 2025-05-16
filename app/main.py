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
    level=logging.DEBUG,  # Уровень DEBUG для детальных логов
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
                logger.info(f"Состояние загружено: {data}")
                return data
        except FileNotFoundError:
            logger.warning("Файл состояния не найден. Создание нового.")
            return self._create_default_state()
        except json.JSONDecodeError:
            logger.error("Ошибка чтения файла состояния. Пересоздаю.")
            return self._create_default_state()
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния: {str(e)}")
            return {'last_video_id': None, 'initialized': False}

    def _create_default_state(self):
        default_state = {'last_video_id': None, 'initialized': False}
        with open(Config.STATE_FILE, 'w') as f:
            json.dump(default_state, f)
        return default_state

    def save_state(self):
        try:
            with open(Config.STATE_FILE, 'w') as f:
                json.dump(self.state, f)
            logger.info("Состояние успешно сохранено")
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния: {str(e)}")

state_manager = StateManager()

@app.route('/')
def health_check():
    return {"status": "running", "last_checked": state_manager.state.get('last_video_id')}, 200

@app.route('/test_telegram')
def test_telegram():
    """Ручная проверка отправки сообщения в Telegram"""
    test_message = "🚀 Тестовое сообщение от бота!"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{Config.TG_TOKEN}/sendMessage",
            json={
                'chat_id': Config.TG_CHANNEL,
                'text': test_message,
                'parse_mode': 'HTML'
            },
            timeout=10
        )
        response.raise_for_status()
        return {"status": "success", "message": "Тест отправлен!"}, 200
    except Exception as e:
        logger.error(f"Тест Telegram провален: {str(e)}")
        return {"status": "error", "message": str(e)}, 500

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
            logger.debug(f"Ответ YouTube API: {json.dumps(response, indent=2)}")
            return response
        except HttpError as e:
            logger.error(f"Ошибка YouTube API: {e}")
            return None
        except Exception as e:
            logger.error(f"Общая ошибка YouTubeService: {e}")
            return None

class TelegramService:
    @staticmethod
    def send_alert(video_data):
        if not all([Config.TG_TOKEN, Config.TG_CHANNEL]):
            logger.error("Отсутствуют настройки Telegram!")
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
            logger.info("Сообщение успешно отправлено в Telegram")
            return True
        except requests.exceptions.HTTPError as e:
            logger.error(f"Ошибка HTTP: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Ошибка отправки в Telegram: {str(e)}")
            return False

def check_video_task():
    with lock:
        logger.info("\n" + "="*40)
        logger.info("Начало проверки видео")
        
        # Получаем данные YouTube
        response = YouTubeService.get_latest_video()
        if not response:
            logger.error("Не удалось получить данные с YouTube")
            return

        items = response.get('items', [])
        if not items:
            logger.warning("В ответе YouTube нет видео")
            return

        try:
            video = items[0]
            current_id = video['id']['videoId']
            title = video['snippet']['title']
            logger.info(f"Последнее видео: {current_id} - {title}")
        except KeyError as e:
            logger.error(f"Некорректная структура ответа YouTube: {e}")
            return

        # Обработка состояния
        state = state_manager.state
        logger.debug(f"Текущее состояние: {state}")

        if not state['initialized']:
            logger.info("Инициализация состояния первым видео")
            state.update(last_video_id=current_id, initialized=True)
            state_manager.save_state()
            return

        if current_id != state['last_video_id']:
            logger.info("Обнаружено новое видео! Отправка уведомления...")
            video_data = {'id': current_id, 'title': title}
            
            if TelegramService.send_alert(video_data):
                state['last_video_id'] = current_id
                state_manager.save_state()
                logger.info("Состояние обновлено")
            else:
                logger.error("Не удалось отправить уведомление")
        else:
            logger.info("Новых видео не найдено")

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
    logger.info("\nЗавершение работы...")
    scheduler.shutdown()
    state_manager.save_state()
    logger.info("Сервис остановлен")

def create_app():
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    if not scheduler.running:
        scheduler.start()
        logger.info("Планировщик задач запущен")
    
    try:
        check_video_task()
    except Exception as e:
        logger.error(f"Ошибка при стартовой проверке: {e}")
    
    return app

if __name__ == "__main__":
    create_app().run(host='0.0.0.0', port=8000)
