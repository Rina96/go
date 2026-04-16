import asyncio
from sqlalchemy.future import select
from sqlalchemy import func
from loguru import logger
from datetime import datetime, timedelta, timezone
from database import AsyncSessionLocal
from models import ChatSession
from integrations import send_telegram_alert

ALMATY_TZ = timezone(timedelta(hours=5))

def now_almaty() -> datetime:
    return datetime.now(ALMATY_TZ).replace(tzinfo=None)


async def check_all_proactive_tasks():
    """Only daily report to Telegram. No WhatsApp spam."""
    try:
        async with AsyncSessionLocal() as db:
            await handle_daily_report(db)
    except Exception as e:
        logger.error(f"❌ SCHEDULER ERROR: {e}")


async def handle_daily_report(db):
    """Send daily report to Telegram at 21:00 Almaty time."""
    try:
        now = now_almaty()
        if now.hour != 21 or now.minute >= 30:
            return

        today_start = now.replace(hour=0, minute=0, second=0)

        # All leads
        all_result = await db.execute(select(func.count()).select_from(ChatSession))
        total = all_result.scalar() or 0

        # Today's new leads
        new_result = await db.execute(
            select(func.count()).select_from(ChatSession).where(
                ChatSession.created_at >= today_start
            )
        )
        new_today = new_result.scalar() or 0

        # Active today (had interaction)
        active_result = await db.execute(
            select(func.count()).select_from(ChatSession).where(
                ChatSession.last_interaction >= today_start
            )
        )
        active_today = active_result.scalar() or 0

        # By stage
        stages = {}
        for stage in ["new", "name", "city", "qualified", "booked", "rescheduled", "declined", "paid"]:
            r = await db.execute(
                select(func.count()).select_from(ChatSession).where(
                    ChatSession.funnel_stage == stage
                )
            )
            stages[stage] = r.scalar() or 0

        # Today's bookings
        booked_today_r = await db.execute(
            select(func.count()).select_from(ChatSession).where(
                ChatSession.booked_at >= today_start,
                ChatSession.booked_date != None
            )
        )
        booked_today = booked_today_r.scalar() or 0

        # Today's payments
        paid_today_r = await db.execute(
            select(func.count()).select_from(ChatSession).where(
                ChatSession.is_paid == True,
                ChatSession.last_interaction >= today_start
            )
        )
        paid_today = paid_today_r.scalar() or 0

        # Recent leads (last 5 new today)
        recent_r = await db.execute(
            select(ChatSession).where(
                ChatSession.created_at >= today_start
            ).order_by(ChatSession.created_at.desc()).limit(5)
        )
        recent = recent_r.scalars().all()

        recent_text = ""
        for s in recent:
            phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
            name = s.client_name or "—"
            city = s.client_city or "—"
            note = s.ai_notes or ""
            recent_text += f"  • {name} ({phone}) — {city}"
            if note:
                recent_text += f" — {note}"
            recent_text += "\n"

        if not recent_text:
            recent_text = "  Нет новых лидов сегодня\n"

        conversion = int((stages['booked'] + stages['paid']) / total * 100) if total else 0

        report = (
            f"📊 <b>Ежедневный отчёт Айжан</b>\n"
            f"📅 {now.strftime('%d.%m.%Y')}\n"
            f"{'─' * 28}\n\n"
            f"<b>📈 Сегодня:</b>\n"
            f"  🆕 Новых лидов: <b>{new_today}</b>\n"
            f"  💬 Активных диалогов: <b>{active_today}</b>\n"
            f"  📅 Записались на МК: <b>{booked_today}</b>\n"
            f"  💰 Оплатили: <b>{paid_today}</b>\n\n"
            f"<b>🎯 Воронка (всего {total}):</b>\n"
            f"  🆕 Новый: {stages['new']}\n"
            f"  👤 Имя: {stages['name']}\n"
            f"  📍 Город: {stages['city']}\n"
            f"  ✅ Квалиф.: {stages['qualified']}\n"
            f"  📅 Записан: {stages['booked']}\n"
            f"  🔄 Перенос: {stages['rescheduled']}\n"
            f"  ❌ Отказ: {stages['declined']}\n"
            f"  💰 Оплачен: {stages['paid']}\n\n"
            f"<b>📊 Конверсия: {conversion}%</b>\n\n"
            f"<b>🆕 Новые лиды сегодня:</b>\n{recent_text}\n"
            f"🔗 <a href='https://school-go-whatsapp-bot.onrender.com/leads'>Открыть CRM</a>"
        )

        await send_telegram_alert(report)
        logger.info("📊 Daily report sent to Telegram")

    except Exception as e:
        logger.error(f"Daily Report Error: {e}")


async def scheduler_loop():
    """Protected infinite loop — runs every 30 min."""
    logger.info("📅 Scheduler started (30 min interval, Almaty UTC+5)")
    while True:
        try:
            await check_all_proactive_tasks()
        except Exception as e:
            logger.error(f"🚨 SCHEDULER FAILURE: {e}")
        await asyncio.sleep(1800)
