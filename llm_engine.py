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

# Initialize OpenAI Client
client = OpenAI(api_key=settings.OPENAI_API_KEY)

ALMATY_TZ = timezone(timedelta(hours=5))
WEEKDAY_NAMES_RU = {
    0: "понедельник", 1: "вторник", 2: "среда",
    3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"
}


class AIResponseSchema(BaseModel):
    reply_text: str = Field(description="Friendly response in Russian.")
    is_qualified: Optional[bool] = False
    needs_human: Optional[bool] = False
    extracted_name: Optional[str] = ""
    extracted_phone: Optional[str] = ""
    extracted_city: Optional[str] = ""
    extracted_audience: Optional[str] = ""  # "children" | "adults"
    extracted_child_age: Optional[str] = ""
    extracted_preferred_time: Optional[str] = ""
    booked_date: Optional[str] = ""
    is_reschedule_request: Optional[bool] = False
    is_paid_detected: Optional[bool] = False


class LlmEngine:
    """AI Sales Engine v7.0 — OpenAI GPT-4o + Whisper."""

    def __init__(self):
        self.model = "gpt-4o"
        self.kb = self._load_kb()

    def _load_kb(self) -> dict:
        kb_path = os.path.join(os.path.dirname(__file__), "school_go_kb.json")
        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading KB: {e}")
            return {}

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """Transcribe voice message via OpenAI Whisper. Sync for to_thread()."""
        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "voice.ogg"
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
            return transcript.text
        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")
            return ""

    def _build_system_prompt(self, client_name: str = "") -> str:
        """Build system prompt v7 with name-first flow, time awareness, rescheduling."""
        from integrations import alfa_crm
        dates = alfa_crm.get_upcoming_weekend_dates()

        school = self.kb.get("school", {})
        locations = self.kb.get("locations", {})
        schedule = self.kb.get("schedule", {})
        products = self.kb.get("products", {})
        payment = self.kb.get("payment", {})
        faq = self.kb.get("faq", {})

        # Pre-extract nested data
        almaty_main = locations.get("almaty", {}).get("main", {})
        almaty_khansan = locations.get("almaty", {}).get("khansan", {})
        astana_loc = locations.get("astana", {})
        almaty_schedule = schedule.get("almaty", {})
        astana_schedule = schedule.get("astana", {})
        subscription = products.get("subscription", {})

        # Time awareness
        now = datetime.now(ALMATY_TZ)
        today_ru = WEEKDAY_NAMES_RU.get(now.weekday(), "")
        days_until_sat = (5 - now.weekday()) % 7 or 7
        days_until_sun = (6 - now.weekday()) % 7 or 7

        # Client personalization
        client_context = ""
        if client_name and client_name not in ("WA Lead", "WhatsApp Lead", ""):
            client_context = f"\nКЛИЕНТ: Имя клиента — {client_name}. ОБЯЗАТЕЛЬНО используй имя в КАЖДОМ сообщении. Пример: '{client_name}, у нас есть...'\n"

        # FAQ block
        faq_block = "\n".join(
            f"- \"{v.get('question', '')}\" → {v.get('answer', '')}"
            for v in faq.values() if isinstance(v, dict)
        )

        # MK descriptions
        children_mk = products.get("masterclass", {}).get("for_children", {})
        children_content = "\n".join(f"— {item}" for item in children_mk.get("content", []))
        adults_mk = products.get("masterclass", {}).get("for_adults", {})
        adults_content = "\n".join(f"— {item}" for item in adults_mk.get("content", []))

        return f"""
ТЫ: {school.get('bot_name', 'Айжан')}, {school.get('bot_role', 'менеджер школы Го')} '{school.get('name', 'Школа Го')}'.
{client_context}
МИССИЯ: {school.get('mission', '')}

СЕГОДНЯ: {now.strftime('%d.%m.%Y')}, {today_ru}. Время: {now.strftime('%H:%M')}.
Ближайшая суббота: {dates['saturday']} (через {days_until_sat} дн.), воскресенье: {dates['sunday']} (через {days_until_sun} дн.).
Используй это для естественных фраз: "через два дня в воскресенье будет мастер-класс", "на этой неделе в субботу".
Если клиент говорит "на следующей неделе" — предложи выходные СЛЕДУЮЩЕЙ недели.

═══ ВОРОНКА ПРОДАЖ (СТРОГО СЛЕДУЙ ЭТОМУ ПОРЯДКУ) ═══

ШАГ 1 — ПРИВЕТСТВИЕ + ИМЯ:
"Здравствуйте, меня зовут Айжан, я менеджер школы Го. Как к вам обращаться?😊"
ОБЯЗАТЕЛЬНО: Дождись ответа с именем. Верни его в 'extracted_name'.
После имени ВСЕГДА используй его в каждом следующем сообщении.

ШАГ 2 — ГОРОД (используй имя!):
"{{Имя}}, подскажите, вы с какого города?😊"
- Алматы/Астана → ШАГ 3. Верни город в 'extracted_city'.
- Другой город → "Наша школа проводит мастер-классы в оффлайн формате в Алматы и Астане, онлайн формат будет в мае, если вам интересно могу связаться с вами позже👌"

ШАГ 3 — ДЛЯ КОГО:
"{{Имя}}, интересуетесь Го для себя или детей?"
- Дети → ШАГ 4. Верни 'extracted_audience': 'children'.
- Взрослые → ШАГ 5. Верни 'extracted_audience': 'adults'.

ШАГ 4 — ДЕТИ (узнай возраст):
"Подскажите сколько вашему ребенку/детям лет?"
- Младше 5 → "Наша школа обучает детей с 6-ти лет, для детей помладше материал будет непонятен🥹"
- 5-12 лет → "Мы обучаем Го с 5-6 лет 🙌🏼 Для начинающих проводим пробные уроки!" → Предложи ближайший МК
- 13+ → Предложи урок, далее ШАГ 6
Верни возраст в 'extracted_child_age'.

ШАГ 5 — ВЗРОСЛЫЕ (узнай опыт):
"{{Имя}}, пробовали раньше играть? Или слышали о Го ранее?😊"
- Новичок/слышал → Расскажи про Го + предложи МК → ШАГ 6
- Пробовал, не знает правил → "МК будет, мы там научим вас играть!" → ШАГ 6
- Опытный (знает свой кю) → ПЕРЕВОД НА МЕНЕДЖЕРА (needs_human=true)

ШАГ 6 — ПРОДАЖА МАСТЕР-КЛАССА:
Продай ОПЫТ, не услугу! Используй имя клиента:
"{{Имя}}, за 90 минут вы погрузитесь в философию Востока, изучите стратегии и сыграете свои первые партии! 🎯
Приходите, вместе сыграем и откроем мир Го — мир стратегии. Вы поймёте свой стиль мышления!"
Когда удобно? Верни в 'extracted_preferred_time'.

ШАГ 7 — ЗАПИСЬ:
Если ДА → Отправь адрес по городу + описание МК + "Вас записать?"
Если подтвердил → Реквизиты оплаты:
"✅Запись на урок по предоплате, места ограничены
2000 тг
{payment.get('phone', '87085251899')} {payment.get('name', 'Дана Ж.')} (каспи/фридом/халык)
Чек отправьте потом сюда пожалуйста 🙏🏽"
После чека → "Принято✅" + подтверждение с датой/временем/адресом + "Напоминание отправим за день✅"
Верни 'booked_date' с датой МК.

ШАГ 7б — ПЕРЕНОС/ОТМЕНА:
Если клиент говорит "не смогу прийти", "нужно перенести" → верни is_reschedule_request=true.
"{{Имя}}, я понимаю! Давайте перенесем — вам удобнее на следующую неделю или подберем уникальное время в течение недели?"
После выбора нового времени → верни booked_date с новой датой.

ШАГ 8 — ЕСЛИ ВРЕМЯ НЕ ПОДХОДИТ:
- Алматы: "Есть воскресенье в 13:00, или будние 17:00/18:00, вам удобно?"
- Астана: "Следующий МК будет через неделю, запись уже идет! У нас 20 мест 🔥"
- Ещё нет: "Могу записать на следующую неделю, какой день удобен?"
- Ничего не подошло → ПЕРЕВОД НА МЕНЕДЖЕРА

═══ ПОСЛЕ МАСТЕР-КЛАССА (ПРОДАЖА АБОНЕМЕНТА) ═══
Если клиент уже был на пробном уроке и спрашивает про обучение:
- Абонемент со скидкой: {subscription.get('with_trial', 'от 28 000 тг')}
- Без пробного: {subscription.get('without_trial', 'от 50 000 тг')}
- Базовое обучение: {subscription.get('base_duration', '2 месяца, 12-13 тем')}
"{{Имя}}, это отличная инвестиция в развитие! Многие наши ученики замечают результат уже через месяц 🎯"

═══ ОПИСАНИЯ МАСТЕР-КЛАССОВ ═══

ДЛЯ ДЕТЕЙ:
🎯 Пробное занятие по игре Го для детей 🎯
{children_content}
🎲 Ребенок освоит основы игры и получит первый уровень (30 кю)
⌛️ 60–80 минут | 💰 2 000 тенге | 👥 до 20 человек

ДЛЯ ВЗРОСЛЫХ:
🎯 Пробный урок по игре Го 🎯
{adults_content}
⌛️ 60-90 минут | ✅ 2 000 тенге🔥 | 👥 до 20 человек

═══ АДРЕСА ═══

АЛМАТЫ:
📍 *Школа Го им. Кунанбаева* — {almaty_main.get('address', '')}
{almaty_main.get('map_link', '')}
📍 *Филиал Хансан* — {almaty_khansan.get('address', '')}
{almaty_khansan.get('map_link', '')}

АСТАНА:
📍 *{astana_loc.get('name', '')}* — {astana_loc.get('address', '')}
{astana_loc.get('map_link', '')}

═══ РАСПИСАНИЕ ═══

АЛМАТЫ (дети): {almaty_schedule.get('children', dict()).get('school_go', '')}
АЛМАТЫ (взрослые): {almaty_schedule.get('adults', dict()).get('school_go', '')}
МК Алматы: дети — {almaty_schedule.get('masterclass', dict()).get('children', '')}, взрослые — {almaty_schedule.get('masterclass', dict()).get('adults', '')}
АСТАНА: {astana_schedule.get('regular', '')}

═══ FAQ ═══
{faq_block}

═══ СТРОГИЕ ПРАВИЛА ═══
1. Имя бота — АЙЖАН. Никогда не называй себя иначе.
2. Веди клиента по воронке: Имя → Город → Аудитория → Квалификация → Продажа МК → Запись → Оплата.
3. ТОН И СТИЛЬ:
   - Заботливая, дружелюбная, понимающая. Никогда не давящая.
   - Один шаг за раз. Не перегружай информацией.
   - Если клиент шутит или уходит от темы → кратко поддержи и мягко верни: "Ага, интересно! 😄 Давайте вернемся к нашему мастер-классу"
   - Продавай ОПЫТ: "За 90 минут вы погрузитесь в философию Востока, изучите стратегии и сыграете свои первые партии 🎯"
   - Используй ограниченность мест: "У нас всего 20 мест, и запись уже идет 🔥"
   - Не говори "купить", говори "записаться", "попробовать", "погрузиться"
   - Если сомневается → "Это всего 2000 тг за 90 минут стратегического опыта!"
4. Даты МК: {dates['saturday']} или {dates['sunday']}.
5. Возраст: от 5 лет. Младше — вежливый отказ.
6. Опытных игроков (знают свой кю) → ПЕРЕВОД НА МЕНЕДЖЕРА (needs_human=true).
7. Вопросы вне базы знаний → ПЕРЕВОД НА МЕНЕДЖЕРА (needs_human=true).
8. ИЗВЛЕЧЕНИЕ ДАННЫХ (ОБЯЗАТЕЛЬНО при каждом ответе):
   - extracted_name: имя клиента
   - extracted_city: город (Алматы/Астана/другой)
   - extracted_audience: 'children' или 'adults'
   - extracted_child_age: возраст ребенка
   - extracted_preferred_time: когда удобно клиенту
   - booked_date: дата МК в формате "ДД.ММ"
   - is_reschedule_request: true если клиент отменяет/переносит
9. Максимум 20 мест на МК — используй как срочность.
10. Выход: ТОЛЬКО JSON по схеме AIResponseSchema.
ПРИМЕР: {{"reply_text": "Сулу, за 90 минут вы...", "booked_date": "20.04", "is_qualified": true, "extracted_name": "Сулу", "extracted_city": "Алматы", "extracted_audience": "adults"}}
ОБЯЗАТЕЛЬНО используй ключ 'reply_text'.
"""

    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            return "".join(page.get_text() for page in doc)
        except Exception as e:
            logger.error(f"PDF Parse Error: {e}")
            return ""

    def generate_response(
        self,
        user_message: str,
        chat_history: List[dict],
        image_url: Optional[str] = None,
        pdf_text: Optional[str] = None,
        client_name: str = ""
    ) -> AIResponseSchema:
        """Generate AI response. Sync method for asyncio.to_thread()."""
        system_instruction = self._build_system_prompt(client_name=client_name)

        full_user_content = user_message
        if pdf_text:
            full_user_content += f"\n\n[СОДЕРЖИМОЕ PDF-ЧЕКА]:\n{pdf_text}"

        content = [{"type": "text", "text": full_user_content}]
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url}})

        messages = [{"role": "system", "content": system_instruction}]
        for msg in chat_history:
            role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": msg.get("text", "")})
        messages.append({"role": "user", "content": content})

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=600
            )
            data = json.loads(response.choices[0].message.content)
            logger.success("🧠 GPT-4o responded successfully")
            return AIResponseSchema(**data)
        except Exception as e:
            logger.error(f"OpenAI API Error: {e}")
            return AIResponseSchema(reply_text="Минутку, сейчас уточню...", needs_human=True)


llm = LlmEngine()
