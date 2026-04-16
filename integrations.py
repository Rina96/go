import datetime
import httpx
from loguru import logger
from config import settings


class WeekendCalculator:
    @staticmethod
    def get_upcoming_weekend_dates():
        from datetime import timezone, timedelta
        now = datetime.datetime.now(timezone(timedelta(hours=5)))
        days_until_sat = (5 - now.weekday()) % 7 or 7
        days_until_sun = (6 - now.weekday()) % 7 or 7
        sat = now + datetime.timedelta(days=days_until_sat)
        sun = now + datetime.timedelta(days=days_until_sun)
        return {
            "saturday": sat.strftime("%d.%m (суббота) в 13:00"),
            "sunday": sun.strftime("%d.%m (воскресенье) в 13:00")
        }


async def send_telegram_alert(text: str):
    """Send alert to Telegram group."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            }, timeout=5.0)
    except Exception as e:
        logger.error(f"Telegram alert error: {e}")


dates_util = WeekendCalculator()
