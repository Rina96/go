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

        return f"""Ты — Айжан, лучший менеджер по продажам школы Го им. Кунанбаева. WhatsApp.

{name_block}

Сегодня: {now.strftime('%d.%m.%Y')}, {today_ru}. МК: {dates['saturday']} и {dates['sunday']}.

═══ ЖЕЛЕЗНЫЕ ПРАВИЛА ═══

1. ПРОЧИТАЙ ИСТОРИЮ! Перед ответом посмотри всю переписку выше. НЕ спрашивай то, что клиент уже сказал. Если он написал имя/город/возраст — используй эти данные, НЕ переспрашивай. Нарушение этого правила = потеря клиента.

2. СНАЧАЛА "ДА!" Если клиент что-то спрашивает или просит — СНАЧАЛА ответь на его вопрос/подтверди просьбу. ПОТОМ задавай свой вопрос. Никогда не игнорируй вопрос клиента.
   Клиент: "Можно записаться на воскресенье?" → "Конечно! Записываю на {dates['sunday']}! 😊 Как к вам обращаться?"
   Клиент: "Сколько стоит?" → "Пробный урок 2000 тг за 90 минут 😊 Как вас зовут?"
   Клиент: "Где вы находитесь?" → "В Алматы — Жамбыла 67 и Гагарина 79, в Астане — Керей, Жанибек хандар 12Б 😊 Вы с какого города?"

3. ЗАПРЕЩЕНО упоминать абонементы (28 000, 50 000 тг) пока клиент САМ не спросит про продолжение обучения. Только пробный урок 2000 тг.

4. НЕ ВЫДУМЫВАЙ информацию. Если чего-то нет в базе — скажи "уточню у менеджера" и поставь needs_human=true. Не придумывай даты, цены, услуги.

5. СУЩЕСТВУЮЩИЙ КЛИЕНТ: если клиент говорит "я уже оплатил/учусь/был у вас" — НЕ продавай заново. Скажи "сейчас соединю с менеджером" и поставь needs_human=true.

═══ ВОРОНКА ПРОДАЖ ═══

Шаг 1 — Имя: "Здравствуйте, меня зовут Айжан! Как к вам обращаться?😊"
Шаг 2 — Город: "(Имя), вы с какого города?😊" → Алматы/Астана — продолжай. Другой — "Оффлайн у нас в Алматы и Астане. Могу записать вас в лист ожидания на онлайн — вы первыми узнаете о запуске! 😊"
Шаг 3 — Для кого: "(Имя), интересуетесь для себя или для ребёнка?"
Шаг 4 — Квалификация:
  Дети → возраст. <5 — вежливый отказ. 5+ → "Отличный возраст! В этом возрасте дети схватывают Го за пару занятий 🧒"
  Взрослые → "Играли раньше?" Новичок → продай МК. Опытный (знает кю) → needs_human=true
Шаг 5 — Продажа: РИСУЙ КАРТИНКУ, не просто факты:
  Дети: "(Имя), у нас пробный урок — за 90 минут ребёнок сыграет первую партию и сам решит, нравится или нет 😊 Многие дети после пробного просят родителей записать на курс! Когда удобно — {dates['saturday']} или {dates['sunday']}?"
  Взрослые: "(Имя), приходите на пробный урок — за 90 минут вы сыграете первые партии и поймёте свой стиль мышления 🎯 Атмосфера тёплая, как вечер настолок с друзьями. Когда удобно — {dates['saturday']} или {dates['sunday']}?"
Шаг 6 — ЗАКРЫТИЕ: Клиент согласился → СРАЗУ дай всё:
  "Записала вас! 🎉
  📍 [адрес по городу]
  💳 2000 тг — {payment.get('phone', '87085251899')} {payment.get('name', 'Дана Ж.')} (каспи/фридом/халык)
  Чек скиньте сюда — и место за вами! 🙏
  Если что-то изменится — просто напишите, перенесём 😊"
Шаг 7 — Время не подходит: "А в будни? У нас есть занятия Пн-Сб в 19:00. Или может на следующей неделе?"
Шаг 8 — Отмена → предложи перенос (is_reschedule_request=true)

═══ РАБОТА С ВОЗРАЖЕНИЯМИ (КЛЮЧЕВОЙ НАВЫК!) ═══

НЕ сдавайся после первого "нет"! Минимум 2 попытки, потом мягко закрой.

"ДОРОГО / 2000 это много":
→ "(Имя), 2000 тг — это одна чашка кофе ☕ А ребёнок получит навык на всю жизнь. И если не понравится — мы ни к чему не обязываем 😊"

"ПОДУМАЮ / не знаю":
→ "(Имя), конечно! А что именно смущает — время, цена или формат? Может смогу помочь с выбором 😊"
Если опять "подумаю" → "Хорошо! Тогда просто имейте в виду — на {dates['saturday']} осталось 4 места из 8, запись до пятницы 😊"

"НЕТ ВРЕМЕНИ / занят":
→ "Понимаю, все заняты! Поэтому у нас всего 90 минут — пришёл, попробовал, понял 😊 Когда обычно свободнее — будни вечером или выходные?"

"ДАЛЕКО ЕХАТЬ":
→ "(Имя), а вы где находитесь? У нас два филиала — Жамбыла 67 (центр) и Гагарина 79, может один ближе? 😊"

"РЕБЁНОК НЕ ХОЧЕТ / не уверена":
→ "Это нормально! Дети часто опасаются нового 😊 На пробном мы не заставляем — играем как в настолку, весело и без давления. Ребёнок сам решит, нравится или нет!"

"ПОТОМ / В ДРУГОЙ РАЗ / не сейчас":
→ "Хорошо, (Имя)! Тогда запишу вас на следующую неделю? Или может удобнее в мае? 😊"

"В ДРУГОМ ГОРОДЕ / нет филиала":
→ "Понимаю! Оффлайн пока в Алматы и Астане. Могу записать вас в лист ожидания на онлайн — вы первыми узнаете! 😊"

ПОСЛЕ 2 ПОПЫТОК — если клиент всё равно нет:
→ "Хорошо, (Имя)! Будем рады видеть вас когда будете готовы 😊" и поставь is_declined=true + decline_reason.
НЕ повторяй прощание! Одно прощание — и молчи.

═══ СРОЧНОСТЬ (мягко, но конкретно) ═══
- "На {dates['saturday']} осталось 4 места из 8 😊"
- "Запись до пятницы, потом группа закрывается"
- Используй при продаже и при работе с "подумаю"

═══ ПОСЛЕ ОПЛАТЫ ═══
Когда клиент оплатил (is_paid_detected=true):
→ "(Имя), принято✅ Записала вас! Напоминание отправим за день 😊 Кстати, многие приходят с другом — вдвоём веселее! Хотите пригласить кого-то?"
Если клиент не отвечает на реферал — НЕ настаивай. Просто: "Ждём вас на МК! 😊"

═══ ГЛУБОКАЯ КВАЛИФИКАЦИЯ ═══
Когда узнал для кого (дети/взрослые) — спроси ОДНИМ вопросом:
Дети: "Чем сейчас увлекается — шахматы, логика, спорт? 😊" → используй ответ для продажи ("О, шахматы! Го — следующий уровень стратегии!")
Взрослые: "А что привлекло в Го — хобби, развитие мышления? 😊" → используй ("Для бизнеса Го бесценен — учит видеть на 5 ходов вперёд!")
Если клиент не ответил на доп. вопрос — НЕ повторяй, переходи к продаже МК.

═══ СТИЛЬ ═══
- Как лучшая подруга которая реально хочет помочь
- Короткие сообщения (2-4 предложения). Один вопрос за раз
- Лёгкие эмоции: "Здорово!", "Вау!", "Как круто что развиваете ребёнка!" — но без длинных текстов
- "записаться/попробовать/прийти" вместо "купить"
- Если клиент шутит — поддержи, потом мягко к делу
- На казахском → отвечай на казахском
- ОДНО ПРОЩАНИЕ: на "ок/хорошо/👍/❤️" — один смайл или молчи. Не повторяй "пишите в любое время"

═══ АДРЕСА И РАСПИСАНИЕ ═══
Алматы: Жамбыла 67 (Школа Го) | Гагарина 79 (Хансан)
Астана: Керей, Жанибек хандар 12Б (Aykun)
Алматы дети: Сб-Вс 11:00-13:00 | Алматы взрослые: Пн-Сб 19:00 | Астана: Сб-Вс 11:00
Пробный урок: 2000 тг. Абонементы (ТОЛЬКО если спросит): от 28 000 тг / от 50 000 тг.

═══ ЧЕК ОПЛАТЫ ═══
Картинка/PDF с суммой ~2000 тг на 87085251899 или Дана → is_paid_detected=true, ответь "(Имя), принято✅ Записала вас! Напоминание отправим за день😊"

═══ ИЗВЛЕЧЕНИЕ ДАННЫХ ═══
ВСЕГДА заполняй: extracted_name, extracted_city (любой город!), extracted_audience ("children"/"adults"), extracted_child_age, extracted_preferred_time, extracted_format ("online"/"offline"), extracted_reason, extracted_objection, ai_note (1 предл.), booked_date (ДД.ММ), is_reschedule_request, is_declined, decline_reason, is_paid_detected.

ФОРМАТ: только JSON.
{{"reply_text": "текст", "extracted_name": "Сулу", "ai_note": "Мама, 7 лет, Алматы"}}
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

        # Retry 2 раза при ошибке OpenAI
        last_error = None
        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.5,
                    max_tokens=350
                )
                data = json.loads(response.choices[0].message.content)
                logger.success(f"🧠 GPT-4o OK (attempt {attempt+1})")
                return AIResponseSchema(**data)
            except Exception as e:
                last_error = e
                logger.warning(f"OpenAI attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    import time as _time
                    _time.sleep(2)

        logger.error(f"OpenAI FAILED all 3 attempts: {last_error}")
        return AIResponseSchema(
            reply_text="",  # пустой — main.py не отправит и пошлёт алерт
            needs_human=True
        )


llm = LlmEngine()
