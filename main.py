import asyncio
import time
import datetime
from typing import Optional
from loguru import logger
from fastapi import FastAPI, Request
from database import engine, AsyncSessionLocal
from crud import crud
from llm_engine import llm
from green_api import wa_client
from integrations import alfa_crm
from scheduler import scheduler_loop
from contextlib import asynccontextmanager
from config import settings

# CRITICAL: Import models before Base to ensure tables are registered
from models import ChatSession
from database import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 АЙЖАН v7.0 STARTING...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Migrate: add new columns using IF NOT EXISTS (Postgres-safe)
        from sqlalchemy import text
        is_postgres = "postgres" in settings.DATABASE_URL
        new_cols = {
            "client_city": "VARCHAR",
            "client_audience": "VARCHAR",
            "preferred_time": "VARCHAR",
            "funnel_stage": "VARCHAR DEFAULT 'new'",
            "is_subscription_offered": "BOOLEAN DEFAULT FALSE",
        }
        async with engine.begin() as conn:
            if is_postgres:
                for col_name, col_type in new_cols.items():
                    await conn.execute(text(
                        f"DO $$ BEGIN "
                        f"ALTER TABLE chat_sessions ADD COLUMN {col_name} {col_type}; "
                        f"EXCEPTION WHEN duplicate_column THEN NULL; END $$;"
                    ))
                    print(f"  ✅ Ensured column: {col_name}")
            else:
                for col_name, col_type in new_cols.items():
                    try:
                        await conn.execute(text(
                            f"ALTER TABLE chat_sessions ADD COLUMN {col_name} {col_type}"
                        ))
                        print(f"  ✅ Added column: {col_name}")
                    except Exception:
                        pass
        print("✅ DATABASE INITIALIZED")
        logger.success("✅ Database Schema Ready.")
    except Exception as e:
        print(f"❌ DATABASE ERROR: {e}")
        logger.error(f"❌ DB Init Fail: {e}")

    asyncio.create_task(scheduler_loop())
    yield
    print("🔌 АЙЖАН SHUTTING DOWN...")


app = FastAPI(lifespan=lifespan)


async def process_incoming_message(
    chat_id: str,
    text: str,
    incoming_ts: int,
    image_url: Optional[str] = None
):
    """
    AI Sales Pipeline v7.0
    - Audio transcription via Whisper
    - Name-first qualification flow
    - Full CRM data sync
    - Rescheduling support
    """
    try:
        print(f"⚡️ [1/8] Processing message for {chat_id}...")

        # Check Human Takeover (timestamp-based)
        cloud_history = await wa_client.get_chat_history(chat_id, count=15)

        human_replied = any(
            msg.get("role") == "assistant" and msg.get("ts", 0) > incoming_ts
            for msg in cloud_history
        )

        if human_replied:
            print(f"🛡 Human takeover detected in {chat_id}. Айжан silent.")
            return

        print(f"🧠 [2/8] Fetching context for {chat_id}...")
        async with AsyncSessionLocal() as db:
            # CRM CONTEXT
            crm_lead = await alfa_crm.get_customer_by_phone(chat_id)
            crm_name = crm_lead.get("name", "") if crm_lead else ""
            crm_id = crm_lead.get("id") if crm_lead else None

            # DB Session
            session = await crud.get_or_create_session(db, chat_id)
            if crm_id:
                session.crm_lead_id = str(crm_id)

            if crm_name and not session.client_name:
                session.client_name = crm_name

            # AUDIO TRANSCRIPTION (before saving to history)
            if text.startswith("[AUDIO_URL:") and text.endswith("]"):
                audio_url = text[len("[AUDIO_URL:"):-1]
                try:
                    print(f"🎤 [2.5/8] Transcribing audio for {chat_id}...")
                    audio_bytes = await wa_client.download_file(audio_url)
                    transcription = await asyncio.to_thread(
                        llm.transcribe_audio, audio_bytes
                    )
                    text = transcription if transcription else "[Голосовое сообщение — не удалось распознать]"
                    print(f"🎤 Transcription: '{text[:60]}...'")
                except Exception as e:
                    logger.error(f"Audio transcription error: {e}")
                    text = "[Голосовое сообщение — не удалось распознать]"

            await crud.add_message_to_history(db, session, role="user", text=text)

            # Merge: DB history is primary, cloud fills gaps
            db_history = session.history_json or []
            if cloud_history and len(cloud_history) > len(db_history):
                final_history = cloud_history
            else:
                final_history = db_history

            # AI Response
            print(f"🤖 [3/8] Generating AI response for {chat_id}...")
            ai_response = await asyncio.to_thread(
                llm.generate_response,
                user_message=text,
                chat_history=final_history,
                image_url=image_url,
                client_name=crm_name or session.client_name or ""
            )

            print(f"💬 [4/8] Sending response to {chat_id}: '{ai_response.reply_text[:40]}...'")
            await crud.add_message_to_history(db, session, role="assistant", text=ai_response.reply_text)

            # RESCHEDULING: clear old booking if client wants to reschedule
            if ai_response.is_reschedule_request:
                old_date = session.booked_date
                session.booked_date = None
                session.booked_at = None
                session.is_reminder_sent = False
                print(f"🔄 [5/8] Reschedule for {chat_id}, cleared: {old_date}")
                if crm_id:
                    asyncio.create_task(
                        alfa_crm.add_comment(int(crm_id), f"🔄 Перенос: клиент отменил {old_date}")
                    )

            # BOOKING: set new date (works for both initial and reschedule)
            if ai_response.booked_date:
                session.booked_date = ai_response.booked_date
                session.booked_at = datetime.datetime.utcnow()
                print(f"📅 [5/8] Booking set for {chat_id}: {ai_response.booked_date}")

            # SAVE ALL QUALIFICATION DATA
            if ai_response.extracted_name and not session.client_name:
                session.client_name = ai_response.extracted_name
            if ai_response.extracted_city and not session.client_city:
                session.client_city = ai_response.extracted_city
            if ai_response.extracted_audience and not session.client_audience:
                session.client_audience = ai_response.extracted_audience
            if ai_response.extracted_child_age and not session.child_age:
                try:
                    session.child_age = int(ai_response.extracted_child_age)
                except (ValueError, TypeError):
                    pass
            if ai_response.extracted_preferred_time and not session.preferred_time:
                session.preferred_time = ai_response.extracted_preferred_time

            success = await wa_client.send_message(chat_id, ai_response.reply_text)
            print(f"{'✅' if success else '❌'} [6/8] WA send status: {success}")

            # CRM SYNC: Create or update lead
            if not crm_id:
                print(f"📋 [7/8] Creating new CRM lead for {chat_id}...")
                new_crm_id = await alfa_crm.sync_customer(
                    chat_id,
                    session.client_name or ai_response.extracted_name or "WA Lead"
                )
                if new_crm_id:
                    session.crm_lead_id = str(new_crm_id)
                    crm_id = new_crm_id
                    print(f"✅ CRM lead created: ID={new_crm_id}")
                    asyncio.create_task(
                        alfa_crm.add_comment(int(new_crm_id), f"Первое сообщение: {text[:100]}")
                    )

            # SYNC QUALIFICATION DATA TO CRM
            if crm_id:
                qual_parts = []
                if ai_response.extracted_city:
                    qual_parts.append(f"Город: {ai_response.extracted_city}")
                if ai_response.extracted_audience:
                    aud = "дети" if ai_response.extracted_audience == "children" else "взрослые"
                    qual_parts.append(f"Аудитория: {aud}")
                if ai_response.extracted_child_age:
                    qual_parts.append(f"Возраст ребенка: {ai_response.extracted_child_age}")
                if ai_response.extracted_preferred_time:
                    qual_parts.append(f"Удобное время: {ai_response.extracted_preferred_time}")
                if qual_parts:
                    asyncio.create_task(
                        alfa_crm.add_comment(int(crm_id), "📋 " + ", ".join(qual_parts))
                    )

            # Mark payment in DB
            if ai_response.is_paid_detected and not session.is_paid:
                session.is_paid = True
                print(f"💰 [PAID] {chat_id} — payment detected!")

            # Update CRM status
            if crm_id:
                if ai_response.is_paid_detected:
                    asyncio.create_task(
                        alfa_crm.set_status(int(crm_id), alfa_crm.STATUS_PAID)
                    )
                    asyncio.create_task(
                        alfa_crm.add_comment(int(crm_id), "✅ Оплата подтверждена через WhatsApp")
                    )
                elif ai_response.booked_date:
                    asyncio.create_task(
                        alfa_crm.set_status(int(crm_id), alfa_crm.STATUS_BOOKED)
                    )
                    asyncio.create_task(
                        alfa_crm.add_comment(int(crm_id), f"📅 Записан на МК: {ai_response.booked_date}")
                    )
                elif ai_response.is_qualified:
                    asyncio.create_task(
                        alfa_crm.set_status(int(crm_id), alfa_crm.STATUS_NEW)
                    )

            await db.commit()
            print(f"✅ [8/8] Finished processing {chat_id}")
            logger.success(f"✅ Cycle complete for {chat_id}")

    except Exception as e:
        print(f"🚨 CRITICAL WORKER ERROR for {chat_id}: {e}")
        logger.error(f"🚨 Worker error: {e}")


@app.post("/webhook/green-api")
async def webhook(request: Request):
    """Green API webhook handler with audio support."""
    try:
        data = await request.json()
        print(f"DEBUG: WEBHOOK ARRIVED! Keys: {list(data.keys())}")

        type_webhook = data.get("typeWebhook", "")

        if type_webhook == "incomingMessageReceived":
            chat_id = data.get("senderData", {}).get("chatId")
            incoming_ts = data.get("timestamp", int(time.time()))
            msg_data = data.get("messageData", {})

            text = ""
            image_url = None

            msg_type = msg_data.get("typeMessage", "")

            if "textMessageData" in msg_data:
                text = msg_data["textMessageData"].get("textMessage", "")
            elif "extendedTextMessageData" in msg_data:
                text = msg_data["extendedTextMessageData"].get("text", "")
            elif "imageMessageData" in msg_data:
                image_url = msg_data["imageMessageData"].get("downloadUrl")
                text = msg_data["imageMessageData"].get("caption", "Image")
            elif msg_type == "audioMessage" or "fileMessageData" in msg_data and msg_type == "audioMessage":
                file_data = msg_data.get("fileMessageData", {})
                audio_url = file_data.get("downloadUrl", "")
                if audio_url and settings.WHISPER_ENABLED:
                    text = f"[AUDIO_URL:{audio_url}]"
                    print(f"🎤 Audio detected: {audio_url[:60]}...")
                else:
                    text = "[Голосовое сообщение]"
            elif "audioMessageData" in msg_data:
                audio_url = msg_data["audioMessageData"].get("downloadUrl", "")
                if audio_url and settings.WHISPER_ENABLED:
                    text = f"[AUDIO_URL:{audio_url}]"
                else:
                    text = "[Голосовое сообщение]"

            if chat_id and "@c.us" in chat_id and (text or image_url):
                print(f"📩 MESSAGE from {chat_id}: '{text[:40]}'")
                asyncio.create_task(
                    process_incoming_message(chat_id, text, incoming_ts, image_url)
                )
            elif chat_id and "@g.us" in chat_id:
                print(f"🛡 IGNORED: Group message from {chat_id}")
            else:
                print(f"⚠️ IGNORED: Non-individual chat ({chat_id}) or no content.")
        else:
            print(f"ℹ️ NON-MESSAGE WEBHOOK: {type_webhook}")

        return {"status": "ok"}
    except Exception as e:
        print(f"🚨 WEBHOOK ERROR: {e}")
        logger.error(f"🚨 Webhook error: {e}")
        return {"status": "error", "reason": str(e)}


@app.get("/health")
async def health():
    return {"status": "active", "version": "7.0-Aizhan-AI-Sales"}


@app.api_route("/{full_path:path}", methods=["GET", "POST", "HEAD"])
async def catch_all(request: Request, full_path: str = ""):
    return {"status": "ok", "path": full_path, "bot": "Aizhan 7.0"}
