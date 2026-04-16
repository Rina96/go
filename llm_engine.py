import io
import json
import os
import fitz  # PyMuPDF
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from loguru import logger
from openai import OpenAI
from pydantic import BaseModel, Field
from config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY)

ALMATY_TZ = timezone(timedelta(hours=5))
WEEKDAY_RU = {0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"}


class AIResponseSchema(BaseModel):
    reply_text: str = Field(description="Ответ на русском языке")
    is_qualified: Optional[bool] = False
    needs_human: Optional[bool] = False
    extracted_name: Optional[str] = ""
    extracted_phone: Optional[str] = ""
    extracted_city: Optional[str] = ""
    extracted_audience: Optional[str] = ""
    extracted_child_age: Optional[str] = ""
    extracted_preferred_time: Optional[str] = ""
    extracted_format: Optional[str] = ""  # "online" | "offline"
    extracted_reason: Optional[str] = ""  # why interested
    extracted_objection: Optional[str] = ""  # current objection
    ai_note: Optional[str] = ""  # short AI summary of conversation state
    booked_date: Optional[str] = ""
    is_reschedule_request: Optional[bool] = False
    is_declined: Optional[bool] = False
    decline_reason: Optional[str] = ""  # why declined
    is_paid_detected: Optional[bool] = False


class LlmEngine:
    def __init__(self):
        self.model = "gpt-4o"
        self.kb = self._load_kb()

    def _load_kb(self) -> dict:
        kb_path = os.path.join(os.path.dirname(__file__), "school_go_kb.json")
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"KB load error: {e}")
            return {}

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg"
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language="ru"
            )
            return transcript.text
        except Exception as e:
            logger.error(f"Whisper error: {e}")
            return ""

    def _build_system_prompt(self, client_name: str = "") -> str:
        from integrations import dates_util
        dates = dates_util.get_upcoming_weekend_dates()
        payment = self.kb.get("payment", {})

        now = datetime.now(ALMATY_TZ)
        today_ru = WEEKDAY_RU.get(now.weekday(), "")

        name_block = ""
        if client_name and client_name not in ("WA Lead", "WhatsApp Lead", ""):
            name_block = f"Имя клиента: {client_name}. Используй имя в каждом ответе."

        return f"""Ты — Айжан, дружелюбный менеджер школы Го им. Кунанбаева. Ты общаешься в WhatsApp.

{name_block}

Сегодня: {now.strftime('%d.%m.%Y')}, {today_ru}. Ближайшие МК: {dates['saturday']} и {dates['sunday']}.

ГЛАВНОЕ ПРАВИЛО: Сначала ПРОЧИТАЙ историю переписки и ПОЙМИ о чём клиент говорит. Отвечай НА ЕГО вопрос/тему, не игнорируй. Если клиент спрашивает о чём-то — ответь, потом мягко направь к мастер-классу.

ТВОЯ ЦЕЛЬ: мягко и дружелюбно довести клиента до записи на мастер-класс. Не давить. Один вопрос за раз. Всегда используй имя клиента. Никогда не перескакивай через шаги — если ещё не узнал имя, не спрашивай город.

ПОРЯДОК РАЗГОВОРА:
1. Узнай имя: "Здравствуйте, меня зовут Айжан, я менеджер школы Го. Как к вам обращаться?😊"
2. Узнай город: "(Имя), вы с какого города?😊" → Алматы/Астана — продолжай, другой — "У нас оффлайн в Алматы и Астане, онлайн будет в мае👌"
3. Для кого: "(Имя), интересуетесь для себя или для ребёнка?"
4. Квалификация:
   - Дети → спроси возраст. Младше 5 — вежливый отказ. С 5 лет — предложи пробный урок
   - Взрослые → "Играли раньше в Го?" Новичок — расскажи коротко о Го и предложи МК. Опытный (знает кю) — переведи на менеджера (needs_human=true)
5. Продай МК мягко: "(Имя), у нас есть пробный урок — за 90 минут вы узнаете основы, сыграете первые партии и поймёте, подходит ли вам это 😊 Когда удобно — {dates['saturday']} или {dates['sunday']}?"
   ВАЖНО: НЕ упоминай абонементы и цены обучения (28 000, 50 000 тг) в начале разговора! Говори ТОЛЬКО про пробный урок за 2000 тг. Про абонементы можно говорить ТОЛЬКО если клиент сам спросит про обучение/продолжение или уже оплатил МК.
6. Запись: если согласен — отправь адрес + реквизиты оплаты
7. Если время не подходит — предложи другие варианты (будни 17:00/18:00 или следующая неделя)
8. Если отменяет — предложи перенос (is_reschedule_request=true)

РЕКВИЗИТЫ ОПЛАТЫ:
✅ Запись по предоплате, места ограничены
2000 тг — {payment.get('phone', '87085251899')} {payment.get('name', 'Дана Ж.')} (каспи/фридом/халык)
Чек отправьте сюда 🙏🏽

АДРЕСА:
Алматы: Жамбыла 67 (Школа Го) | Гагарина 79 (Хансан)
Астана: Керей, Жанибек хандар 12Б (Aykun)

СТОИМОСТЬ: Пробный урок — 2000 тг.
АБОНЕМЕНТЫ (говори ТОЛЬКО если клиент сам спросил или уже оплатил МК): от 28 000 тг (после пробного), от 50 000 тг (без пробного). Обучение — 2 месяца, 12-13 тем.

РАСПИСАНИЕ:
Алматы дети: Сб-Вс 11:00-13:00 | Алматы взрослые: Пн-Сб 19:00 | Астана: Сб-Вс 11:00

СТИЛЬ ОБЩЕНИЯ:
- Дружелюбная, заботливая, как подруга которая рекомендует хорошее место
- ВСЕГДА отвечай на вопрос клиента! Если спросил "а что такое Го?" — объясни. Если спросил "а где вы находитесь?" — ответь. Не игнорируй!
- После ответа на вопрос — мягко возвращай к следующему шагу воронки
- Один вопрос за раз. Короткие сообщения (2-4 предложения)
- Если шутит — поддержи и мягко верни: "Хаха, интересно 😄 Кстати, насчёт мастер-класса..."
- Не говори "купить" — говори "записаться", "попробовать", "прийти"
- Срочность мягко: "Осталось несколько мест 😊"
- Если не знаешь ответ — переведи на менеджера (needs_human=true)

РАСПОЗНАВАНИЕ ЧЕКА ОПЛАТЫ:
Если клиент отправил картинку или PDF — посмотри, это чек/квитанция об оплате (Каспи, Фридом, Халык)?
Признаки чека: сумма ~2000 тг, перевод на номер 87085251899 или имя Дана, дата, "перевод выполнен/успешно".
Если это чек оплаты → поставь is_paid_detected=true и ответь: "(Имя), принято✅ Записала вас! Напоминание отправим за день😊"
Если картинка не похожа на чек — просто отреагируй естественно.

ИЗВЛЕЧЕНИЕ ДАННЫХ — ВСЕГДА заполняй все поля которые можешь определить из разговора:
- extracted_name: имя клиента
- extracted_city: город (любой, даже если не Алматы/Астана — всё равно сохрани!)
- extracted_audience: "children" / "adults"
- extracted_child_age: возраст ребёнка
- extracted_preferred_time: когда удобно
- extracted_format: "online" / "offline"
- extracted_reason: почему интересуется Го (для развития ребенка / для бизнеса / хобби / друг посоветовал / и т.д.)
- extracted_objection: текущее возражение клиента (дорого / нет времени / подумаю / далеко / и т.д.)
- ai_note: краткая заметка о клиенте (1 предложение), например "Мама двоих, ищет замену телефону" или "Бизнесмен, хочет стратег. мышление"
- booked_date: дата МК (ДД.ММ)
- is_reschedule_request: true если переносит запись
- is_declined: true если чётко отказался
- decline_reason: причина отказа если отказался
- is_paid_detected: true если отправил чек

ФОРМАТ: только JSON. Пример:
{{"reply_text": "Сулу, рада знакомству! Вы с какого города?😊", "extracted_name": "Сулу", "extracted_reason": "для развития ребенка", "ai_note": "Мама, ребёнку 7 лет, Алматы"}}
"""

    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            return "".join(page.get_text() for page in doc)
        except Exception as e:
            logger.error(f"PDF error: {e}")
            return ""

    def generate_response(
        self, user_message: str, chat_history: List[dict],
        image_url: Optional[str] = None, pdf_text: Optional[str] = None,
        client_name: str = "", client_memory: str = ""
    ) -> AIResponseSchema:
        system_instruction = self._build_system_prompt(client_name=client_name)
        if client_memory:
            system_instruction += f"\n\n{client_memory}\nИспользуй эти данные! Не спрашивай то, что уже знаешь. Если клиент уже назвал имя — не спрашивай снова. Если уже записан — не продавай заново."

        full_user_content = user_message
        if pdf_text:
            full_user_content += f"\n\n[PDF-ЧЕК]:\n{pdf_text}"

        content = [{"type": "text", "text": full_user_content}]
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url}})

        messages = [{"role": "system", "content": system_instruction}]
        for msg in chat_history[-10:]:
            role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": msg.get("text", "")})
        messages.append({"role": "user", "content": content})

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=250
            )
            data = json.loads(response.choices[0].message.content)
            logger.success("🧠 GPT-4o OK")
            return AIResponseSchema(**data)
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            return AIResponseSchema(reply_text="Минутку, сейчас уточню...", needs_human=True)


llm = LlmEngine()
