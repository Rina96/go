import asyncio
from sqlalchemy.future import select
from loguru import logger
from datetime import datetime, timedelta, timezone
from database import AsyncSessionLocal
from models import ChatSession
from llm_engine import llm
from green_api import wa_client

# FIX 4: Алматы timezone UTC+5
ALMATY_TZ = timezone(timedelta(hours=5))

def now_almaty() -> datetime:
    """Returns current time in Almaty timezone (UTC+5)."""
    return datetime.now(ALMATY_TZ).replace(tzinfo=None)


async def check_all_proactive_tasks():
    """All proactive outbound messages DISABLED. Bot only replies to incoming."""
    # Disabled: bot should NOT send messages first, only reply
    # await handle_sunday_broadcast(db)
    # await handle_pre_event_reminders(db)
    # await handle_post_event_feedback(db)
    # await handle_reactivations(db)
    # await handle_subscription_followup(db)
    pass


async def handle_sunday_broadcast(db):
    """Sunday 11:00 Almaty — morning reminder for booked & paid clients."""
    try:
        now = now_almaty()  # FIX 4: Use Almaty time
        if now.weekday() == 6 and now.hour == 11 and now.minute < 30:
            today_str = now.strftime("%d.%m")  # Match booked_date format "19.04"
            query = select(ChatSession).where(
                ChatSession.booked_date == today_str,
                ChatSession.is_paid == True
            )
            result = await db.execute(query)
            sessions = result.scalars().all()
            for session in sessions:
                name = session.client_name or ""
                msg = f"Доброе утро{f', {name}' if name else ''}! ☀️ Ждем вас сегодня на мастер-классе в 13:00!"
                await wa_client.send_message(session.whatsapp_chat_id, msg)
            await db.commit()
            logger.info(f"📣 Sunday broadcast sent to {len(sessions)} clients")
    except Exception as e:
        logger.error(f"Sunday Broadcast Error: {e}")
        await db.rollback()


async def handle_pre_event_reminders(db):
    """Send reminder 2 hours before booked_at."""
    try:
        now = now_almaty()  # FIX 4: Use Almaty time
        threshold = now + timedelta(minutes=120)
        query = select(ChatSession).where(
            ChatSession.booked_at <= threshold,
            ChatSession.booked_at > now,
            ChatSession.is_paid == True,
            ChatSession.is_reminder_sent == False
        )
        result = await db.execute(query)
        sessions = result.scalars().all()
        for session in sessions:
            name = session.client_name or ""
            msg = f"{'Привет' + (f', {name}' if name else '')}! ⏰ Напоминаю — ваш мастер-класс через 2 часа. До встречи! 😊"
            await wa_client.send_message(session.whatsapp_chat_id, msg)
            session.is_reminder_sent = True
        await db.commit()
        if sessions:
            logger.info(f"⏰ Pre-event reminders sent: {len(sessions)}")
    except Exception as e:
        logger.error(f"Pre-event Reminder Error: {e}")
        await db.rollback()


async def handle_post_event_feedback(db):
    """Ask for feedback 3 hours after booked_at."""
    try:
        now = now_almaty()  # FIX 4: Use Almaty time
        threshold = now - timedelta(minutes=180)
        query = select(ChatSession).where(
            ChatSession.booked_at <= threshold,
            ChatSession.is_paid == True,
            ChatSession.is_feedback_sent == False
        )
        result = await db.execute(query)
        sessions = result.scalars().all()
        for session in sessions:
            name = session.client_name or ""
            msg = f"{'Привет' + (f', {name}' if name else '')}! 😊 Как прошел мастер-класс? Поделитесь впечатлениями!"
            await wa_client.send_message(session.whatsapp_chat_id, msg)
            session.is_feedback_sent = True
        await db.commit()
        if sessions:
            logger.info(f"💬 Post-event feedback sent: {len(sessions)}")
    except Exception as e:
        logger.error(f"Post-event Feedback Error: {e}")
        await db.rollback()


async def handle_reactivations(db):
    """Re-engage unqualified leads silent for 24 hours."""
    try:
        now = now_almaty()  # FIX 4: Use Almaty time
        threshold = now - timedelta(hours=24)
        query = select(ChatSession).where(
            ChatSession.is_qualified == False,
            ChatSession.last_interaction < threshold,
            ChatSession.followup_count < 2
        )
        result = await db.execute(query)
        sessions = result.scalars().all()
        for session in sessions:
            prompt = f"Клиент молчит 24 часа. Имя: {session.client_name or 'неизвестно'}. Спроси мягко — всё ли в порядке и интересна ли им игра Го."
            # FIX 2: Run blocking call in thread pool
            ai_resp = await asyncio.to_thread(
                llm.generate_response,
                user_message=prompt,
                chat_history=session.history_json or [],
                client_name=session.client_name or ""
            )
            await wa_client.send_message(session.whatsapp_chat_id, ai_resp.reply_text)
            session.followup_count = (session.followup_count or 0) + 1
        await db.commit()
        if sessions:
            logger.info(f"🔄 Reactivations sent: {len(sessions)}")
    except Exception as e:
        logger.error(f"Reactivation Error: {e}")
        await db.rollback()


async def handle_subscription_followup(db):
    """Offer subscription 24h after feedback was sent."""
    try:
        now = now_almaty()
        threshold = now - timedelta(hours=24)
        query = select(ChatSession).where(
            ChatSession.is_feedback_sent == True,
            ChatSession.is_subscription_offered == False,
            ChatSession.is_paid == True,
            ChatSession.last_interaction < threshold
        )
        result = await db.execute(query)
        sessions = result.scalars().all()
        for session in sessions:
            name = session.client_name or ""
            greeting = f"{name}, " if name else ""
            msg = (
                f"{greeting}рады, что вы побывали на нашем мастер-классе! 😊\n\n"
                f"Для тех, кто был на пробном уроке, действует специальная цена "
                f"на абонемент — от 28 000 тг (обычная цена от 50 000 тг).\n\n"
                f"Абонемент включает 2 месяца обучения, 12-13 тем для освоения базовых стратегий. "
                f"Хотите узнать подробнее? 🎯"
            )
            await wa_client.send_message(session.whatsapp_chat_id, msg)
            session.is_subscription_offered = True
        await db.commit()
        if sessions:
            logger.info(f"💰 Subscription followup sent: {len(sessions)}")
    except Exception as e:
        logger.error(f"Subscription Followup Error: {e}")
        await db.rollback()


async def scheduler_loop():
    """Protected infinite loop — one failure never stops the scheduler."""
    logger.info("📅 Scheduler started (30 min interval, Almaty UTC+5)")
    while True:
        try:
            await check_all_proactive_tasks()
        except Exception as e:
            logger.error(f"🚨 CRITICAL SCHEDULER FAILURE: {e}")
        await asyncio.sleep(1800)  # 30 minutes
