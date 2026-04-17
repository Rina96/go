import asyncio
from sqlalchemy.future import select
from sqlalchemy import func, and_
from loguru import logger
from datetime import datetime, timedelta, timezone
from database import AsyncSessionLocal
from models import ChatSession
from integrations import send_telegram_alert

ALMATY_TZ = timezone(timedelta(hours=5))

def now_almaty() -> datetime:
    return datetime.now(ALMATY_TZ).replace(tzinfo=None)


async def check_all_proactive_tasks():
    """Daily report + follow-ups for silent leads."""
    try:
        async with AsyncSessionLocal() as db:
            await handle_daily_report(db)
            await handle_followups(db)
    except Exception as e:
        logger.error(f"❌ SCHEDULER ERROR: {e}")


# ═══════════════════════════════════════════
# FOLLOW-UP: 24ч и 3 дня для остановившихся
# ═══════════════════════════════════════════

async def handle_followups(db):
    """Follow-up для лидов которые замолчали. Только те, с кем бот общался."""
    try:
        from green_api import wa_client

        now = now_almaty()

        # Не отправлять ночью (до 9:00 и после 21:00 Алматы)
        if now.hour < 9 or now.hour >= 21:
            return

        # Лиды в активных стадиях (не declined, не paid, не new без истории)
        active_stages = ["name", "city", "qualified", "paused", "rescheduled"]
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.funnel_stage.in_(active_stages),
                ChatSession.human_takeover != True,
                ChatSession.followup_count < 2,  # макс 2 follow-up
            )
        )
        leads = result.scalars().all()

        for s in leads:
            if not s.last_interaction:
                continue

            hours_silent = (now - s.last_interaction).total_seconds() / 3600
            history = s.history_json or []

            # Минимум 2 сообщения в истории (бот с ним общался)
            if len(history) < 2:
                continue

            # Последнее сообщение от бота? (клиент не ответил)
            last_msg = history[-1] if history else {}
            if last_msg.get("role") != "assistant":
                continue  # клиент последний писал — бот уже ответил

            name = s.client_name or ""
            phone = (s.whatsapp_chat_id or "").replace("@c.us", "")

            # Follow-up 1: через 24 часа
            if hours_silent >= 24 and s.followup_count == 0:
                msg = _build_followup_1(name, s)
                await wa_client.send_message(s.whatsapp_chat_id, msg)
                s.followup_count = 1
                s.funnel_stage = "paused"
                await db.commit()
                logger.info(f"📩 Follow-up 1 (24ч): {name or phone}")

            # Follow-up 2: через 3 дня
            elif hours_silent >= 72 and s.followup_count == 1:
                msg = _build_followup_2(name, s)
                await wa_client.send_message(s.whatsapp_chat_id, msg)
                s.followup_count = 2
                await db.commit()
                logger.info(f"📩 Follow-up 2 (3 дня): {name or phone}")

    except Exception as e:
        logger.error(f"Follow-up Error: {e}")


def _build_followup_1(name: str, session) -> str:
    """Первое касание через 24ч — мягкое напоминание со срочностью."""
    from integrations import dates_util
    dates = dates_util.get_upcoming_weekend_dates()

    greeting = f"{name}, " if name else ""

    if session.funnel_stage == "qualified" or session.client_audience:
        return f"{greeting}добрый день! 😊 Напоминаю — ближайший пробный урок {dates['saturday']}. Осталось 4 места из 8, запись до пятницы. Записать вас?"
    elif session.funnel_stage == "rescheduled":
        return f"{greeting}здравствуйте! 😊 Мы переносили ваш урок — какая дата будет удобна? Ближайшие: {dates['saturday']} и {dates['sunday']}"
    elif session.client_city:
        return f"{greeting}добрый день! 😊 Мы недавно общались про школу Го. Ещё актуально? Ближайший пробный урок {dates['saturday']} — осталось несколько мест!"
    else:
        return f"{greeting}добрый день! 😊 Вы интересовались школой Го. Ближайший пробный урок {dates['saturday']} — всего 2000 тг за 90 минут. Хотите записаться?"


def _build_followup_2(name: str, session) -> str:
    """Второе касание через 3 дня — последняя попытка."""
    greeting = f"{name}, " if name else ""
    return f"{greeting}привет! 😊 Это Айжан из школы Го. Если передумаете — мы всегда рады видеть! Просто напишите когда удобно, подберём время 🙏"


# ═══════════════════════════════════════════
# НАПОМИНАНИЕ ЗАПИСАННЫМ — за день до МК
# ═══════════════════════════════════════════

async def handle_mk_reminder(db):
    """Напоминание за день до МК записанным клиентам."""
    try:
        from green_api import wa_client
        from integrations import dates_util

        now = now_almaty()
        # Отправляем напоминания в 18:00
        if now.hour != 18 or now.minute >= 30:
            return

        tomorrow = (now + timedelta(days=1)).strftime("%d.%m")

        result = await db.execute(
            select(ChatSession).where(
                ChatSession.booked_date == tomorrow,
                ChatSession.is_reminder_sent != True,
                ChatSession.human_takeover != True,
            )
        )
        leads = result.scalars().all()

        for s in leads:
            name = s.client_name or ""
            greeting = f"{name}, " if name else ""
            city = s.client_city or ""

            address = ""
            if "астана" in city.lower():
                address = "📍 Керей, Жанибек хандар 12Б (Aykun)"
            elif "алматы" in city.lower():
                address = "📍 Жамбыла 67 (Школа Го)"
            else:
                address = "📍 Адрес уточните у менеджера"

            msg = f"{greeting}напоминаю — завтра пробный урок! 😊\n{address}\nЖдём вас! 🎯"
            await wa_client.send_message(s.whatsapp_chat_id, msg)
            s.is_reminder_sent = True
            await db.commit()
            logger.info(f"🔔 MK reminder: {name or s.whatsapp_chat_id}")

    except Exception as e:
        logger.error(f"MK Reminder Error: {e}")


# ═══════════════════════════════════════════
# DAILY REPORT
# ═══════════════════════════════════════════

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
        for stage in ["new", "name", "city", "qualified", "booked", "paused", "rescheduled", "declined", "paid"]:
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
            f"  ⏸ Остановился: {stages['paused']}\n"
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
