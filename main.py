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
            "client_format": "VARCHAR",
            "client_reason": "VARCHAR",
            "client_objection": "VARCHAR",
            "ai_notes": "VARCHAR",
            "decline_reason": "VARCHAR",
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
    """CRM-style kanban dashboard with funnel stages."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ChatSession).order_by(ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()

    stages = {
        "new": {"label": "🆕 Новый", "color": "#6b7280", "items": []},
        "name": {"label": "👤 Имя", "color": "#8b5cf6", "items": []},
        "city": {"label": "📍 Город", "color": "#3b82f6", "items": []},
        "qualified": {"label": "✅ Квалиф.", "color": "#f59e0b", "items": []},
        "booked": {"label": "📅 Записан", "color": "#10b981", "items": []},
        "rescheduled": {"label": "🔄 Перенос", "color": "#f97316", "items": []},
        "declined": {"label": "❌ Отказ", "color": "#ef4444", "items": []},
        "paid": {"label": "💰 Оплачен", "color": "#059669", "items": []},
    }

    for s in sessions:
        stage = s.funnel_stage or "new"
        if stage not in stages:
            stage = "new"
        phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
        aud = "👶" if s.client_audience == "children" else ("👤" if s.client_audience == "adults" else "")
        fmt = "🌐" if getattr(s, 'client_format', None) == "online" else ("🏫" if getattr(s, 'client_format', None) == "offline" else "")
        date = s.created_at.strftime("%d.%m %H:%M") if s.created_at else ""
        last = s.last_interaction.strftime("%d.%m %H:%M") if s.last_interaction else ""
        stages[stage]["items"].append({"id": s.id, "name": s.client_name or "—", "phone": phone, "city": s.client_city or "", "aud": aud, "fmt": fmt, "date": date, "last": last, "mk": s.booked_date or "", "age": str(s.child_age) if s.child_age else ""})

    cols_html = ""
    for key, st in stages.items():
        cards = ""
        for item in st["items"]:
            info = f"{item['city']}" if item['city'] else ""
            if item['aud']:
                info += f" {item['aud']}"
            if item['age']:
                info += f" ({item['age']} лет)"
            if item['fmt']:
                info += f" {item['fmt']}"
            if item['mk']:
                info += f" 📅{item['mk']}"
            cards += f"""<a href="/leads/{item['id']}" class="card">
              <div class="card-name">{item['name']}</div>
              <div class="card-phone">{item['phone']}</div>
              <div class="card-info">{info}</div>
              <div class="card-time">Последний: {item['last']}</div>
            </a>"""
        cols_html += f"""<div class="col">
          <div class="col-header" style="background:{st['color']}">{st['label']} <span class="count">{len(st['items'])}</span></div>
          <div class="col-body">{cards if cards else '<div class="empty-col">—</div>'}</div>
        </div>"""

    total = len(sessions)
    booked = sum(1 for s in sessions if s.funnel_stage == "booked")
    paid = sum(1 for s in sessions if s.is_paid)
    declined = sum(1 for s in sessions if s.funnel_stage == "declined")
    rescheduled = sum(1 for s in sessions if s.funnel_stage == "rescheduled")
    online = sum(1 for s in sessions if getattr(s, 'client_format', None) == "online")
    offline = sum(1 for s in sessions if getattr(s, 'client_format', None) == "offline")

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Айжан CRM</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#0f172a; color:#e2e8f0; padding:15px; }}
  .top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; }}
  h1 {{ font-size:20px; }}
  .stats {{ display:flex; gap:10px; margin-bottom:15px; }}
  .stat {{ background:#1e293b; padding:12px 20px; border-radius:8px; text-align:center; }}
  .stat-num {{ font-size:24px; font-weight:bold; color:#38bdf8; }}
  .stat-label {{ font-size:11px; color:#94a3b8; }}
  .board {{ display:flex; gap:10px; overflow-x:auto; padding-bottom:10px; }}
  .col {{ min-width:200px; flex:1; background:#1e293b; border-radius:10px; }}
  .col-header {{ padding:10px 12px; border-radius:10px 10px 0 0; color:white; font-size:13px; font-weight:600; display:flex; justify-content:space-between; }}
  .count {{ background:rgba(255,255,255,0.2); padding:1px 8px; border-radius:10px; font-size:11px; }}
  .col-body {{ padding:8px; display:flex; flex-direction:column; gap:6px; max-height:70vh; overflow-y:auto; }}
  .card {{ background:#0f172a; border:1px solid #334155; border-radius:8px; padding:10px; text-decoration:none; color:inherit; display:block; cursor:pointer; transition:border-color 0.2s; }}
  .card:hover {{ border-color:#2563eb; }}
  .card-name {{ font-weight:600; font-size:13px; color:#f1f5f9; }}
  .card-phone {{ font-size:11px; color:#64748b; margin-top:2px; }}
  .card-info {{ font-size:11px; color:#94a3b8; margin-top:4px; }}
  .card-time {{ font-size:10px; color:#475569; margin-top:4px; }}
  .empty-col {{ text-align:center; padding:20px; color:#475569; font-size:12px; }}
  .btn {{ background:#2563eb; color:white; padding:7px 16px; border-radius:6px; text-decoration:none; font-size:12px; }}
  .btn:hover {{ background:#1d4ed8; }}
  .tabs {{ display:flex; gap:8px; margin-bottom:15px; }}
  .tab {{ padding:6px 14px; border-radius:6px; font-size:12px; cursor:pointer; border:1px solid #334155; color:#94a3b8; text-decoration:none; }}
  .tab.active {{ background:#2563eb; color:white; border-color:#2563eb; }}
</style>
</head><body>
<div class="top">
  <h1>��� Айжан CRM</h1>
  <div><a href="/leads/table" class="btn" style="margin-right:6px">📋 Таблица</a><a href="/leads/csv" class="btn">📥 CSV</a></div>
</div>
<div class="stats">
  <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">Всего</div></div>
  <div class="stat"><div class="stat-num">{booked}</div><div class="stat-label">Записаны</div></div>
  <div class="stat"><div class="stat-num">{rescheduled}</div><div class="stat-label">Перенос</div></div>
  <div class="stat"><div class="stat-num">{declined}</div><div class="stat-label">Отказ</div></div>
  <div class="stat"><div class="stat-num">{paid}</div><div class="stat-label">Оплачено</div></div>
  <div class="stat"><div class="stat-num">🏫{offline} 🌐{online}</div><div class="stat-label">Формат</div></div>
  <div class="stat"><div class="stat-num">{int((booked+paid)/total*100) if total else 0}%</div><div class="stat-label">Конверсия</div></div>
</div>
<div class="board">{cols_html}</div>
<script>setTimeout(()=>location.reload(), 30000)</script>
</body></html>"""


@app.get("/leads/table", response_class=HTMLResponse)
async def leads_table():
    """Table view of all leads."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ChatSession).order_by(ChatSession.created_at.desc()))
        sessions = result.scalars().all()

    rows = ""
    for s in sessions:
        phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
        stage_map = {"new":"🆕 Новый","name":"👤 Имя","city":"📍 Город","qualified":"✅ Квалиф.","booked":"📅 Записан","rescheduled":"🔄 Перенос","declined":"❌ Отказ","paid":"💰 Оплачен"}
        status = stage_map.get(s.funnel_stage or "new", "🆕 Новый")
        date = s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else ""
        aud = "Дети" if s.client_audience == "children" else ("Взрослые" if s.client_audience == "adults" else "—")
        rows += f"<tr><td>{date}</td><td><b>{s.client_name or '—'}</b></td><td>{phone}</td><td>{s.client_city or '—'}</td><td>{aud}</td><td>{s.child_age or '—'}</td><td>{s.preferred_time or '��'}</td><td>{s.booked_date or '—'}</td><td>{status}</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Айжан — Таблица</title>
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;padding:15px}}.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}}h1{{font-size:20px}}table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden}}th{{background:#2563eb;color:white;padding:10px 12px;text-align:left;font-size:12px}}td{{padding:8px 12px;border-bottom:1px solid #334155;font-size:12px}}tr:hover{{background:#334155}}.btn{{background:#2563eb;color:white;padding:7px 16px;border-radius:6px;text-decoration:none;font-size:12px}}</style></head><body>
<div class="top"><h1>📋 Айжан — Таблица лидов</h1><div><a href="/leads" class="btn" style="margin-right:6px">🎯 Воронка</a><a href="/leads/csv" class="btn">📥 CSV</a></div></div>
<table><thead><tr><th>Дата</th><th>Имя</th><th>Телефон</th><th>Город</th><th>Дл�� кого</th><th>Возраст</th><th>Удобно</th><th>Дата МК</th><th>Стадия</th></tr></thead><tbody>{rows}</tbody></table>
<script>setTimeout(()=>location.reload(),30000)</script></body></html>"""


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


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_card(lead_id: int):
    """Client card with full info and chat history."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ChatSession).where(ChatSession.id == lead_id))
        s = result.scalar_one_or_none()
    if not s:
        return HTMLResponse("<h1>Клиент не найден</h1>", status_code=404)

    phone = (s.whatsapp_chat_id or "").replace("@c.us", "")
    stage_map = {"new":"🆕 Новый","name":"👤 Имя получено","city":"📍 Город указан","qualified":"✅ Квалифицирован","booked":"📅 Записан на МК","rescheduled":"🔄 Перенёс МК","declined":"❌ Отказался","paid":"💰 Оплачен"}
    status = stage_map.get(s.funnel_stage or "new", "🆕 Новый")
    aud = "👶 Дети" if s.client_audience == "children" else ("👤 Взрослые" if s.client_audience == "adults" else "—")
    fmt = "🌐 Онлайн" if getattr(s, 'client_format', None) == "online" else ("🏫 Оффлайн" if getattr(s, 'client_format', None) == "offline" else "��")
    created = s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else "—"
    last = s.last_interaction.strftime("%d.%m.%Y %H:%M") if s.last_interaction else "—"

    # Chat history
    history = s.history_json or []
    chat_html = ""
    for msg in history:
        role = msg.get("role", "")
        text = msg.get("text", "")
        if role == "user":
            chat_html += f'<div class="msg msg-user"><div class="bubble bubble-user">{text}</div></div>'
        else:
            chat_html += f'<div class="msg msg-bot"><div class="bubble bubble-bot">{text}</div></div>'

    # Info rows
    info = f"""
    <div class="info-grid">
      <div class="info-item"><span class="info-label">Стадия</span><span class="info-value stage">{status}</span></div>
      <div class="info-item"><span class="info-label">Телефон</span><span class="info-value">{phone}</span></div>
      <div class="info-item"><span class="info-label">Город</span><span class="info-value">{s.client_city or '—'}</span></div>
      <div class="info-item"><span class="info-label">Для кого</span><span class="info-value">{aud}</span></div>
      <div class="info-item"><span class="info-label">Возраст ребёнка</span><span class="info-value">{s.child_age or '—'}</span></div>
      <div class="info-item"><span class="info-label">Формат</span><span class="info-value">{fmt}</span></div>
      <div class="info-item"><span class="info-label">��добное время</span><span class="info-value">{s.preferred_time or '—'}</span></div>
      <div class="info-item"><span class="info-label">Дата МК</span><span class="info-value">{s.booked_date or '—'}</span></div>
      <div class="info-item"><span class="info-label">Оплата</span><span class="info-value">{'✅ Да' if s.is_paid else '❌ Нет'}</span></div>
      <div class="info-item"><span class="info-label">Причина интереса</span><span class="info-value">{getattr(s, 'client_reason', None) or '—'}</span></div>
      <div class="info-item"><span class="info-label">Возражение</span><span class="info-value">{getattr(s, 'client_objection', None) or '—'}</span></div>
      <div class="info-item"><span class="info-label">AI заметка</span><span class="info-value">{getattr(s, 'ai_notes', None) or '—'}</span></div>
      <div class="info-item"><span class="info-label">Причина отказа</span><span class="info-value">{getattr(s, 'decline_reason', None) or '—'}</span></div>
      <div class="info-item"><span class="info-label">Первый контакт</span><span class="info-value">{created}</span></div>
      <div class="info-item"><span class="info-label">Последнее сообщение</span><span class="info-value">{last}</span></div>
      <div class="info-item"><span class="info-label">Сообщений</span><span class="info-value">{len(history)}</span></div>
    </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{s.client_name or phone} — Айжан CRM</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;padding:15px;max-width:900px;margin:0 auto}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}}
h1{{font-size:20px}}
.btn{{background:#2563eb;color:white;padding:7px 16px;border-radius:6px;text-decoration:none;font-size:12px}}
.card-layout{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}
@media(max-width:600px){{.card-layout{{grid-template-columns:1fr}}}}
.panel{{background:#1e293b;border-radius:10px;padding:15px}}
.panel-title{{font-size:14px;font-weight:600;margin-bottom:12px;color:#94a3b8}}
.info-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.info-item{{background:#0f172a;border-radius:6px;padding:8px 10px}}
.info-label{{display:block;font-size:10px;color:#64748b;text-transform:uppercase}}
.info-value{{display:block;font-size:13px;color:#e2e8f0;margin-top:2px}}
.stage{{font-size:14px;font-weight:bold}}
.chat-box{{max-height:60vh;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:5px}}
.msg{{display:flex}}
.msg-user{{justify-content:flex-end}}
.msg-bot{{justify-content:flex-start}}
.bubble{{max-width:80%;padding:8px 12px;border-radius:12px;font-size:13px;line-height:1.4;word-wrap:break-word}}
.bubble-user{{background:#2563eb;color:white;border-bottom-right-radius:4px}}
.bubble-bot{{background:#334155;color:#e2e8f0;border-bottom-left-radius:4px}}
</style></head><body>
<div class="top">
  <h1>{'👤 ' + (s.client_name or phone)}</h1>
  <a href="/leads" class="btn">← Воронка</a>
</div>
<div class="card-layout">
  <div class="panel"><div class="panel-title">📋 Информация</div>{info}</div>
  <div class="panel"><div class="panel-title">💬 Переписка ({len(history)} сообщений)</div><div class="chat-box">{chat_html if chat_html else '<div style="text-align:center;color:#475569;padding:20px">Нет сообщений</div>'}</div></div>
</div>
</body></html>"""


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

            # Build client memory context from DB
            client_memory = ""
            parts = []
            if session.client_name:
                parts.append(f"Имя: {session.client_name}")
            if session.client_city:
                parts.append(f"Город: {session.client_city}")
            if session.client_audience:
                parts.append(f"Для: {'детей' if session.client_audience == 'children' else 'взрослых'}")
            if session.child_age:
                parts.append(f"Возраст ребёнка: {session.child_age}")
            if getattr(session, 'client_format', None):
                parts.append(f"Формат: {'онлайн' if session.client_format == 'online' else 'оффлайн'}")
            if session.preferred_time:
                parts.append(f"Удобное время: {session.preferred_time}")
            if session.booked_date:
                parts.append(f"Записан на МК: {session.booked_date}")
            if session.is_paid:
                parts.append("Оплатил ✅")
            if session.client_reason:
                parts.append(f"Причина интереса: {session.client_reason}")
            if session.client_objection:
                parts.append(f"Возражение: {session.client_objection}")
            if session.ai_notes:
                parts.append(f"Заметка: {session.ai_notes}")
            if session.decline_reason:
                parts.append(f"Причина отказа: {session.decline_reason}")
            if session.funnel_stage:
                stage_ru = {"new":"новый","name":"назвал имя","city":"указал город","qualified":"квалифицирован","booked":"записан","rescheduled":"перенёс","declined":"отказался","paid":"оплатил"}
                parts.append(f"Стадия: {stage_ru.get(session.funnel_stage, session.funnel_stage)}")
            if parts:
                client_memory = "ПАМЯТЬ О КЛИЕНТЕ: " + " | ".join(parts)

            ai_response = await asyncio.to_thread(
                llm.generate_response,
                user_message=text,
                chat_history=final_history,
                image_url=image_url,
                client_name=session.client_name or "",
                client_memory=client_memory
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

            # SAVE ALL DATA (overwrite if updated)
            if ai_response.extracted_name:
                session.client_name = ai_response.extracted_name
            if ai_response.extracted_city:
                session.client_city = ai_response.extracted_city
            if ai_response.extracted_audience:
                session.client_audience = ai_response.extracted_audience
            if ai_response.extracted_child_age:
                try:
                    session.child_age = int(ai_response.extracted_child_age)
                except (ValueError, TypeError):
                    pass
            if ai_response.extracted_preferred_time:
                session.preferred_time = ai_response.extracted_preferred_time
            if ai_response.extracted_format:
                session.client_format = ai_response.extracted_format
            if ai_response.extracted_reason:
                session.client_reason = ai_response.extracted_reason
            if ai_response.extracted_objection:
                session.client_objection = ai_response.extracted_objection
            if ai_response.ai_note:
                session.ai_notes = ai_response.ai_note
            if ai_response.decline_reason:
                session.decline_reason = ai_response.decline_reason

            # PAYMENT
            if ai_response.is_paid_detected and not session.is_paid:
                session.is_paid = True

            # AUTO FUNNEL STAGE
            if session.is_paid:
                session.funnel_stage = "paid"
            elif ai_response.is_declined:
                session.funnel_stage = "declined"
            elif ai_response.is_reschedule_request and not ai_response.booked_date:
                session.funnel_stage = "rescheduled"
            elif session.booked_date:
                session.funnel_stage = "booked"
            elif session.client_audience:
                session.funnel_stage = "qualified"
            elif session.client_city:
                session.funnel_stage = "city"
            elif session.client_name:
                session.funnel_stage = "name"
            else:
                session.funnel_stage = "new"

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
