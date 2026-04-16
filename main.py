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
from integrations import sheets
from scheduler import scheduler_loop
from contextlib import asynccontextmanager
from config import settings

from models import ChatSession
from database import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 АЙЖАН v8.0 STARTING...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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
            else:
                for col_name, col_type in new_cols.items():
                    try:
                        await conn.execute(text(
                            f"ALTER TABLE chat_sessions ADD COLUMN {col_name} {col_type}"
                        ))
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
    """AI Sales Pipeline v8.0 — Google Sheets CRM."""
    try:
        print(f"⚡️ [1/8] Processing message for {chat_id}...")

        cloud_history = await wa_client.get_chat_history(chat_id, count=15)

        human_replied = any(
            msg.get("role") == "assistant" and msg.get("ts", 0) > incoming_ts
            for msg in cloud_history
        )
        if human_replied:
            print(f"🛡 Human takeover in {chat_id}. Silent.")
            return

        print(f"🧠 [2/8] Fetching context for {chat_id}...")
        async with AsyncSessionLocal() as db:
            # Google Sheets lookup
            sheet_lead = await sheets.get_customer_by_phone(chat_id)
            sheet_name = sheet_lead.get("name", "") if sheet_lead else ""
            sheet_row = sheet_lead.get("id") if sheet_lead else None

            session = await crud.get_or_create_session(db, chat_id)
            if sheet_row:
                session.crm_lead_id = str(sheet_row)
            if sheet_name and not session.client_name:
                session.client_name = sheet_name

            # AUDIO TRANSCRIPTION
            if text.startswith("[AUDIO_URL:") and text.endswith("]"):
                audio_url = text[len("[AUDIO_URL:"):-1]
                try:
                    print(f"🎤 [2.5/8] Transcribing audio for {chat_id}...")
                    audio_bytes = await wa_client.download_file(audio_url)
                    transcription = await asyncio.to_thread(llm.transcribe_audio, audio_bytes)
                    text = transcription if transcription else "[Голосовое — не распознано]"
                    print(f"🎤 Transcription: '{text[:60]}...'")
                except Exception as e:
                    logger.error(f"Audio error: {e}")
                    text = "[Голосовое — не распознано]"

            await crud.add_message_to_history(db, session, role="user", text=text)

            db_history = session.history_json or []
            final_history = cloud_history if (cloud_history and len(cloud_history) > len(db_history)) else db_history

            # AI Response
            print(f"🤖 [3/8] Generating AI response for {chat_id}...")
            ai_response = await asyncio.to_thread(
                llm.generate_response,
                user_message=text,
                chat_history=final_history,
                image_url=image_url,
                client_name=sheet_name or session.client_name or ""
            )

            print(f"💬 [4/8] Response: '{ai_response.reply_text[:40]}...'")
            await crud.add_message_to_history(db, session, role="assistant", text=ai_response.reply_text)

            # RESCHEDULING
            if ai_response.is_reschedule_request:
                old_date = session.booked_date
                session.booked_date = None
                session.booked_at = None
                session.is_reminder_sent = False
                if sheet_row:
                    asyncio.create_task(sheets.add_comment(sheet_row, f"🔄 Перенос: отменил {old_date}"))

            # BOOKING
            if ai_response.booked_date:
                session.booked_date = ai_response.booked_date
                session.booked_at = datetime.datetime.utcnow()

            # SAVE QUALIFICATION DATA to session
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

            # PAYMENT
            if ai_response.is_paid_detected and not session.is_paid:
                session.is_paid = True

            success = await wa_client.send_message(chat_id, ai_response.reply_text)
            print(f"{'✅' if success else '❌'} [6/8] WA send: {success}")

            # GOOGLE SHEETS SYNC
            if not sheet_row:
                print(f"📊 [7/8] Creating Sheets lead for {chat_id}...")
                sheet_row = await sheets.sync_customer(
                    chat_id,
                    session.client_name or ai_response.extracted_name or "WA Lead"
                )
                if sheet_row:
                    session.crm_lead_id = str(sheet_row)
                    asyncio.create_task(sheets.add_comment(sheet_row, f"Первое: {text[:100]}"))

            # Update Sheets fields
            if sheet_row:
                if ai_response.extracted_city:
                    asyncio.create_task(sheets.update_field(sheet_row, sheets.COL_CITY, ai_response.extracted_city))
                if ai_response.extracted_audience:
                    aud = "дети" if ai_response.extracted_audience == "children" else "взрослые"
                    asyncio.create_task(sheets.update_field(sheet_row, sheets.COL_AUDIENCE, aud))
                if ai_response.extracted_child_age:
                    asyncio.create_task(sheets.update_field(sheet_row, sheets.COL_CHILD_AGE, ai_response.extracted_child_age))
                if ai_response.extracted_preferred_time:
                    asyncio.create_task(sheets.update_field(sheet_row, sheets.COL_PREF_TIME, ai_response.extracted_preferred_time))
                if ai_response.booked_date:
                    asyncio.create_task(sheets.update_field(sheet_row, sheets.COL_MK_DATE, ai_response.booked_date))
                    asyncio.create_task(sheets.set_status(sheet_row, sheets.STATUS_BOOKED))
                if ai_response.is_paid_detected:
                    asyncio.create_task(sheets.set_status(sheet_row, sheets.STATUS_PAID))

            await db.commit()
            print(f"✅ [8/8] Done: {chat_id}")

    except Exception as e:
        print(f"🚨 ERROR for {chat_id}: {e}")
        logger.error(f"🚨 Worker error: {e}")


@app.post("/webhook/green-api")
async def webhook(request: Request):
    try:
        data = await request.json()
        type_webhook = data.get("typeWebhook", "")

        if type_webhook == "incomingMessageReceived":
            chat_id = data.get("senderData", {}).get("chatId")
            incoming_ts = data.get("timestamp", int(time.time()))
            msg_data = data.get("messageData", {})

            text = ""
            image_url = None
            msg_type = msg_data.get("typeMessage", "")
            file_data = msg_data.get("fileMessageData", {})

            if msg_type == "textMessage" or "textMessageData" in msg_data:
                text = msg_data.get("textMessageData", {}).get("textMessage", "")
            elif msg_type == "extendedTextMessage" or "extendedTextMessageData" in msg_data:
                text = msg_data.get("extendedTextMessageData", {}).get("text", "")
            elif msg_type == "imageMessage":
                image_url = file_data.get("downloadUrl", "")
                text = file_data.get("caption", "") or "Клиент отправил изображение"
            elif msg_type == "audioMessage":
                audio_url = file_data.get("downloadUrl", "")
                if audio_url and settings.WHISPER_ENABLED:
                    text = f"[AUDIO_URL:{audio_url}]"
                else:
                    text = "[Голосовое сообщение]"
            elif msg_type == "documentMessage":
                doc_url = file_data.get("downloadUrl", "")
                mime = file_data.get("mimeType", "")
                if "pdf" in mime:
                    text = f"[PDF_URL:{doc_url}]"
                else:
                    text = file_data.get("caption", "") or "Клиент отправил документ"

            if chat_id and "@c.us" in chat_id and (text or image_url):
                asyncio.create_task(
                    process_incoming_message(chat_id, text, incoming_ts, image_url)
                )

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"🚨 Webhook error: {e}")
        return {"status": "error", "reason": str(e)}


@app.get("/health")
async def health():
    return {"status": "active", "version": "8.0-Aizhan-GoogleSheets"}


@app.api_route("/{full_path:path}", methods=["GET", "POST", "HEAD"])
async def catch_all(request: Request, full_path: str = ""):
    return {"status": "ok", "path": full_path, "bot": "Aizhan 8.0"}
