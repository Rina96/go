import datetime
from loguru import logger


class WeekendCalculator:
    """Utility for date calculations. CRM replaced by /leads dashboard."""

    @staticmethod
    def get_upcoming_weekend_dates():
        """Calculates nearest Saturday and Sunday at 13:00 (Almaty UTC+5)."""
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


dates_util = WeekendCalculator()
