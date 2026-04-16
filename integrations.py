import json
import asyncio
import datetime
from typing import Optional, Dict, Any
from loguru import logger
from config import settings

import gspread
from google.oauth2.service_account import Credentials


class GoogleSheetsManager:
    """Google Sheets as CRM — writes leads and updates statuses."""

    STATUS_NEW = "Новый"
    STATUS_BOOKED = "Записан"
    STATUS_PAID = "Оплачен"

    # Column mapping (1-indexed)
    COL_DATE = 1       # A
    COL_NAME = 2       # B
    COL_PHONE = 3      # C
    COL_CITY = 4       # D
    COL_AUDIENCE = 5   # E
    COL_CHILD_AGE = 6  # F
    COL_EXPERIENCE = 7 # G
    COL_PREF_TIME = 8  # H
    COL_MK_DATE = 9    # I
    COL_STATUS = 10     # J
    COL_COMMENTS = 11   # K

    def __init__(self):
        self._sheet = None
        self._init_done = False

    def _get_sheet(self):
        """Lazy init — connect to Google Sheets on first use."""
        if self._sheet and self._init_done:
            return self._sheet
        try:
            creds_json = settings.GOOGLE_SHEETS_CREDENTIALS
            if not creds_json:
                logger.error("❌ GOOGLE_SHEETS_CREDENTIALS not set")
                return None
            creds_data = json.loads(creds_json)
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
            gc = gspread.authorize(creds)
            spreadsheet = gc.open_by_key(settings.GOOGLE_SHEETS_ID)
            self._sheet = spreadsheet.sheet1
            self._init_done = True
            # Ensure header row exists
            first_cell = self._sheet.cell(1, 1).value
            if not first_cell or first_cell != "Дата обращения":
                self._sheet.update("A1:K1", [[
                    "Дата обращения", "Имя", "Телефон", "Город",
                    "Для кого", "Возраст ребенка", "Опыт в Го",
                    "Удобное время", "Дата МК", "Статус", "Комментарии"
                ]])
                logger.info("📝 Header row created in Google Sheets")
            logger.success(f"📊 Google Sheets connected: {settings.GOOGLE_SHEETS_ID}")
            return self._sheet
        except Exception as e:
            logger.error(f"❌ Google Sheets init error: {e}")
            return None

    def _find_row_by_phone(self, phone: str) -> Optional[int]:
        """Find row number by phone. Returns None if not found."""
        sheet = self._get_sheet()
        if not sheet:
            return None
        try:
            clean_phone = "".join(filter(str.isdigit, phone))[-10:]
            phones = sheet.col_values(self.COL_PHONE)
            for i, p in enumerate(phones):
                if clean_phone in p or p in clean_phone:
                    return i + 1  # 1-indexed
            return None
        except Exception as e:
            logger.error(f"⚠️ Sheets find error: {e}")
            return None

    async def get_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        """Find lead by phone. Returns dict with name and row_id."""
        row = await asyncio.to_thread(self._find_row_by_phone, phone)
        if row and row > 1:  # skip header
            sheet = self._get_sheet()
            if sheet:
                try:
                    values = await asyncio.to_thread(sheet.row_values, row)
                    return {
                        "id": row,
                        "name": values[1] if len(values) > 1 else "",
                        "city": values[3] if len(values) > 3 else "",
                        "status": values[9] if len(values) > 9 else "",
                    }
                except Exception as e:
                    logger.error(f"⚠️ Sheets read error: {e}")
        return None

    async def sync_customer(self, phone: str, name: str = "WA Lead") -> Optional[int]:
        """Find or create lead row. Returns row number."""
        existing = await self.get_customer_by_phone(phone)
        if existing:
            row_id = existing["id"]
            current_name = existing.get("name", "")
            if name not in ("WA Lead", "WhatsApp Lead", "") and \
               current_name in ("WA Lead", "WhatsApp Lead", "", "Lead"):
                sheet = self._get_sheet()
                if sheet:
                    try:
                        await asyncio.to_thread(
                            sheet.update_cell, row_id, self.COL_NAME, name
                        )
                    except Exception as e:
                        logger.error(f"⚠️ Sheets name update error: {e}")
            return row_id

        # Create new row
        sheet = self._get_sheet()
        if not sheet:
            return None
        try:
            clean_phone = "".join(filter(str.isdigit, phone))[-10:]
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            new_row = [
                now.strftime("%d.%m.%Y %H:%M"),  # A: Дата
                name,                              # B: Имя
                clean_phone,                       # C: Телефон
                "",                                # D: Город
                "",                                # E: Для кого
                "",                                # F: Возраст
                "",                                # G: Опыт
                "",                                # H: Время
                "",                                # I: Дата МК
                self.STATUS_NEW,                   # J: Статус
                "",                                # K: Комментарии
            ]
            await asyncio.to_thread(sheet.append_row, new_row)
            # Find the new row number
            row = await asyncio.to_thread(self._find_row_by_phone, phone)
            logger.success(f"🆕 Sheets lead created: row {row}, {name}")
            return row
        except Exception as e:
            logger.error(f"⚠️ Sheets create error: {e}")
        return None

    async def set_status(self, row_id: int, status: str):
        """Update status column."""
        sheet = self._get_sheet()
        if not sheet:
            return
        try:
            await asyncio.to_thread(
                sheet.update_cell, row_id, self.COL_STATUS, status
            )
            logger.info(f"📈 Sheets row {row_id} → {status}")
        except Exception as e:
            logger.error(f"⚠️ Sheets status error: {e}")

    async def update_field(self, row_id: int, col: int, value: str):
        """Update any column for a row."""
        sheet = self._get_sheet()
        if not sheet or not value:
            return
        try:
            await asyncio.to_thread(sheet.update_cell, row_id, col, value)
        except Exception as e:
            logger.error(f"⚠️ Sheets update error: {e}")

    async def add_comment(self, row_id: int, text: str):
        """Append comment to comments column."""
        sheet = self._get_sheet()
        if not sheet:
            return
        try:
            current = await asyncio.to_thread(
                sheet.cell, row_id, self.COL_COMMENTS
            )
            existing = current.value or ""
            new_text = f"{existing}\n{text}" if existing else text
            await asyncio.to_thread(
                sheet.update_cell, row_id, self.COL_COMMENTS, new_text[:1000]
            )
        except Exception as e:
            logger.error(f"⚠️ Sheets comment error: {e}")

    @staticmethod
    def get_upcoming_weekend_dates():
        """Calculates nearest Saturday and Sunday at 13:00 (Almaty UTC+5)."""
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
        days_until_sat = (5 - now.weekday()) % 7 or 7
        days_until_sun = (6 - now.weekday()) % 7 or 7
        sat = now + datetime.timedelta(days=days_until_sat)
        sun = now + datetime.timedelta(days=days_until_sun)
        return {
            "saturday": sat.strftime("%d.%m (суббота) в 13:00"),
            "sunday": sun.strftime("%d.%m (воскресенье) в 13:00")
        }


sheets = GoogleSheetsManager()
