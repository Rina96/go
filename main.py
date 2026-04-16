import asyncio
import io
import csv
import time
import datetime
from typing import Optional
from loguru import logger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from database import engine, AsyncSessionLocal
from crud import crud
from llm_engine import llm
from green_api import wa_client
from scheduler import scheduler_loop
from contextlib import asynccontextmanager
from config import settings
from sqlalchemy.future import select

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
        print("✅ DB READY")
    except Exception as e:
        print(f"❌ DB ERROR: {e}")

    asyncio.create_task(scheduler_loop())
    yield


app = FastAPI(lifespan=lifespan)


# ═══════════════════════════════════════════
# LEADS DASHBOARD
# ═══════════════════════════════════════════

@app.get("/leads", response_class=HTMLResponse)
async def leads_dashboard():
    """Web dashboard showing all leads."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ChatSession).order_by(ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()

    rows_html = ""
    for s in sessions:
        phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
        status = "💰 Оплачен" if s.is_paid else ("📅 Записан" if s.booked_date else ("✅ Квалифицирован" if s.is_qualified else "🆕 Новый"))
        date = s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else ""
        audience = ""
        if s.client_audience == "children":
            audience = "👶 Дети"
        elif s.client_audience == "adults":
            audience = "👤 Взрослые"
        child_age = str(s.child_age) if s.child_age else ""

        rows_html += f"""<tr>
            <td>{date}</td>
            <td><b>{s.client_name or '—'}</b></td>
            <td>{phone}</td>
            <td>{s.client_city or '—'}</td>
            <td>{audience}</td>
            <td>{child_age}</td>
            <td>{s.preferred_time or '—'}</td>
            <td>{s.booked_date or '—'}</td>
            <td>{status}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Айжан — Лиды</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
  h1 {{ font-size: 24px; color: #333; }}
  .stats {{ display: flex; gap: 15px; margin-bottom: 20px; }}
  .stat {{ background: white; padding: 15px 25px; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .stat-num {{ font-size: 28px; font-weight: bold; color: #2563eb; }}
  .stat-label {{ font-size: 12px; color: #666; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background: #2563eb; color: white; padding: 12px 15px; text-align: left; font-size: 13px; }}
  td {{ padding: 10px 15px; border-bottom: 1px solid #eee; font-size: 13px; }}
  tr:hover {{ background: #f8fafc; }}
  .btn {{ background: #2563eb; color: white; padding: 8px 20px; border-radius: 6px; text-decoration: none; font-size: 13px; }}
  .btn:hover {{ background: #1d4ed8; }}
  .empty {{ text-align: center; padding: 60px; color: #999; }}
</style>
</head><body>
<div class="header">
  <h1>🎯 Айжан — Панель лидов</h1>
  <a href="/leads/csv" class="btn">📥 Скачать CSV</a>
</div>
<div class="stats">
  <div class="stat"><div class="stat-num">{len(sessions)}</div><div class="stat-label">Всего лидов</div></div>
  <div class="stat"><div class="stat-num">{sum(1 for s in sessions if s.booked_date)}</div><div class="stat-label">Записаны на МК</div></div>
  <div class="stat"><div class="stat-num">{sum(1 for s in sessions if s.is_paid)}</div><div class="stat-label">Оплачено</div></div>
</div>
{"<table><thead><tr><th>Дата</th><th>Имя</th><th>Телефон</th><th>Город</th><th>Для кого</th><th>Возраст</th><th>Удобно</th><th>Дата МК</th><th>Статус</th></tr></thead><tbody>" + rows_html + "</tbody></table>" if sessions else '<div class="empty">Пока нет лидов. Когда клиент напишет боту — он появится здесь.</div>'}
<script>setTimeout(()=>location.reload(), 30000)</script>
</body></html>"""


@app.get("/leads/csv")
async def leads_csv():
    """Download leads as CSV for Excel/Google Sheets."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ChatSession).order_by(ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата", "Имя", "Телефон", "Город", "Для кого", "Возраст ребенка", "Удобное время", "Дата МК", "Статус"])
    for s in sessions:
        phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
        status = "Оплачен" if s.is_paid else ("Записан" if s.booked_date else ("Квалифицирован" if s.is_qualified else "Новый"))
        date = s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else ""
        aud = "Дети" if s.client_audience == "children" else ("Взрослые" if s.client_audience == "adults" else "")
        writer.writerow([date, s.client_name or "", phone, s.client_city or "", aud, s.child_age or "", s.preferred_time or "", s.booked_date or "", status])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"}
    )


# ═══════════════════════════════════════════
# MESSAGE PROCESSING
# ═══════════════════════════════════════════

async def process_incoming_message(
    chat_id: str, text: str, incoming_ts: int,
    image_url: Optional[str] = None
):
    try:
        cloud_history = await wa_client.get_chat_history(chat_id, count=15)

        human_replied = any(
            msg.get("role") == "assistant" and msg.get("ts", 0) > incoming_ts
            for msg in cloud_history
        )
        if human_replied:
            return

        async with AsyncSessionLocal() as db:
            session = await crud.get_or_create_session(db, chat_id)

            # AUDIO TRANSCRIPTION
            if text.startswith("[AUDIO_URL:") and text.endswith("]"):
                audio_url = text[len("[AUDIO_URL:"):-1]
                try:
                    audio_bytes = await wa_client.download_file(audio_url)
                    transcription = await asyncio.to_thread(llm.transcribe_audio, audio_bytes)
                    text = transcription if transcription else "[Голосовое — не распознано]"
                except Exception as e:
                    logger.error(f"Audio error: {e}")
                    text = "[Голосовое — не распознано]"

            await crud.add_message_to_history(db, session, role="user", text=text)

            db_history = session.history_json or []
            final_history = cloud_history if (cloud_history and len(cloud_history) > len(db_history)) else db_history

            ai_response = await asyncio.to_thread(
                llm.generate_response,
                user_message=text,
                chat_history=final_history,
                image_url=image_url,
                client_name=session.client_name or ""
            )

            await crud.add_message_to_history(db, session, role="assistant", text=ai_response.reply_text)

            # RESCHEDULING
            if ai_response.is_reschedule_request:
                session.booked_date = None
                session.booked_at = None
                session.is_reminder_sent = False

            # BOOKING
            if ai_response.booked_date:
                session.booked_date = ai_response.booked_date
                session.booked_at = datetime.datetime.utcnow()

            # SAVE QUALIFICATION
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

            await wa_client.send_message(chat_id, ai_response.reply_text)
            await db.commit()

    except Exception as e:
        logger.error(f"🚨 Error for {chat_id}: {e}")


# ═══════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════

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
        logger.error(f"Webhook error: {e}")
        return {"status": "error"}


@app.get("/health")
async def health():
    return {"status": "active", "version": "8.0-Aizhan-Dashboard"}
