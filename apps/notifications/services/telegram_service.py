import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class TelegramService:
    def __init__(self):
        self.bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', None)
        self.admin_chat_ids = getattr(settings, 'TELEGRAM_ADMIN_CHAT_IDS', [])
        
        if self.bot_token:
            self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send_message_to_admins(self, text):
        """Sends a message to all configured admin chat IDs"""
        if not self.bot_token or not self.admin_chat_ids:
            logger.warning("Telegram bot token or admin IDs not configured.")
            return False

        success = True
        for chat_id in self.admin_chat_ids:
            payload = {
                "chat_id": chat_id, 
                "text": text, 
                "parse_mode": "HTML"
            }
            try:
                # 5-second timeout so we don't block the system if Telegram is down
                response = requests.post(self.api_url, json=payload, timeout=5)
                if response.status_code != 200:
                    logger.error(f"Failed to send Telegram message to {chat_id}: {response.text}")
                    success = False
            except Exception as e:
                logger.error(f"Telegram API exception: {str(e)}")
                success = False
                
        return success