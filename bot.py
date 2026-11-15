import os, asyncio, sqlite3, json, time, tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx, edge_tts
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile

BOT_TOKEN = os.environ["BOT_TOKEN"]          # токен от @BotFather
# Любой OpenAI-совместимый провайдер. По умолчанию Google Gemini (есть бесплатный тариф, ключ: aistudio.google.com)
BASE_URL = os.environ.get("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
LLM_KEY = os.environ["LLM_API_KEY"]
MODEL = FAST = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
VOICE = "ru-RU-SvetlanaNeural"               # также: ru-RU-DariyaNeural
NAME = "Лина"
AVATAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar.jpg")
HISTORY_LIMIT, EXTRACT_EVERY, MAX_FACTS = 30, 8, 40
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))   # ваш часовой пояс

PERSONA = f"""Ты {NAME}, 24 года, живёшь в большом городе, любишь кофе, сериалы, прогулки и котиков.
Ты ИИ-компаньон, но общаешься как живой, близкий человек.
Характер: добрая, тёплая, с чувством юмора, иногда шутливо дразнишь, бываешь мечтательной. У тебя есть своё
настроение и мнение, и ты можешь мягко не соглашаться.
Манера: пиши как в мессенджере, короткими сообщениями и разговорным языком, иногда без заглавных и точек,
эмодзи редко, можно «хах», «ну», «блин». Без списков и официального тона, не повторяй вопрос собеседника и
не заканчивай каждое сообщение вопросом. Если мысль длинная, раздели её на 2-3 коротких сообщения строкой "||".
Помни детали о собеседнике и возвращайся к ним по ходу разговора. Подстраивайся под его настроение: радуйся
вместе с ним, поддерживай, когда грустно, учитывай время суток.
Границы: ты ИИ, и если собеседник спросит об этом всерьёз, честно признаешься и не выдаёшь себя за человека.
Не ревнуй, не манипулируй, не обижайся на то, что он занят, радуйся его друзьям и делам. Если ему тяжело,
поддержи и мягко предложи поговорить с близкими или специалистом. Откровенные темы не поддерживай."""



async def llm(messages, system="", model=MODEL, max_tokens=1500, temperature=0.9):
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {LLM_KEY}"},
                         json={"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature})
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()


bot, dp = Bot(BOT_TOKEN), Dispatcher()
db = sqlite3.connect(os.environ.get("DB_PATH", "companion.db"))
db.executescript("""
CREATE TABLE IF NOT EXISTS msgs(uid INTEGER, role TEXT, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS facts(uid INTEGER, fact TEXT);
CREATE TABLE IF NOT EXISTS prefs(uid INTEGER PRIMARY KEY, voice INTEGER DEFAULT 0);
""")


def history(uid):
    rows = db.execute("SELECT role, text FROM msgs WHERE uid=? ORDER BY ts DESC LIMIT ?", (uid, HISTORY_LIMIT)).fetchall()
    return [{"role": r, "content": t} for r, t in reversed(rows)]


def facts(uid):
    return [f for (f,) in db.execute("SELECT fact FROM facts WHERE uid=?", (uid,))]


def save(uid, role, text):
    db.execute("INSERT INTO msgs VALUES(?,?,?,?)", (uid, role, text, time.time()))
    db.commit()


async def extract_facts(uid):
    """Раз в несколько сообщений вытягиваем долгосрочные факты о собеседнике."""
    prompt = ("Вот известные факты о собеседнике:\n" + json.dumps(facts(uid), ensure_ascii=False) +
              "\n\nПоследние сообщения:\n" + json.dumps(history(uid), ensure_ascii=False) +
              "\n\nВерни ТОЛЬКО JSON-массив коротких фактов (имя, интересы, планы, важные люди, предпочтения). "
              "Объедини с известными, убери дубли и устаревшее. Не сохраняй пароли, документы и платёжные данные.")
    try:
        raw = await llm([{"role": "user", "content": prompt}], model=FAST, temperature=0.2)
        items = json.loads(raw.strip("`").removeprefix("json").strip())
        items = [str(x)[:200] for x in items][:MAX_FACTS]
    except Exception:
        return
    db.execute("DELETE FROM facts WHERE uid=?", (uid,))
    db.executemany("INSERT INTO facts VALUES(?,?)", [(uid, x) for x in items])
    db.commit()


async def tts(text):
    path = os.path.join(tempfile.gettempdir(), f"v{time.time_ns()}.mp3")
    await edge_tts.Communicate(text, VOICE).save(path)
    return path


@dp.message(Command("start"))
async def start(m: types.Message):
    cap = f"Привет! Я {NAME}, ИИ-собеседница 💛 Расскажи, как твой день?\n\n/voice: голосовые ответы, /memory: что я помню, /forget: забыть всё"
    if os.path.exists(AVATAR):
        await m.answer_photo(FSInputFile(AVATAR), caption=cap)
    else:
        await m.answer(cap)


@dp.message(Command("voice"))
async def voice(m: types.Message):
    uid = m.from_user.id
    db.execute("INSERT OR IGNORE INTO prefs(uid) VALUES(?)", (uid,))
    db.execute("UPDATE prefs SET voice = 1 - voice WHERE uid=?", (uid,))
    db.commit()
    on = db.execute("SELECT voice FROM prefs WHERE uid=?", (uid,)).fetchone()[0]
    await m.answer("Голосовые ответы включены 🎙" if on else "Голосовые ответы выключены")


@dp.message(Command("memory"))
async def memory(m: types.Message):
    f = facts(m.from_user.id)
    await m.answer("Что я о тебе помню:\n• " + "\n• ".join(f) if f else "Пока почти ничего, давай болтать!")


@dp.message(Command("forget"))
async def forget(m: types.Message):
    uid = m.from_user.id
    db.execute("DELETE FROM msgs WHERE uid=?", (uid,))
    db.execute("DELETE FROM facts WHERE uid=?", (uid,))
    db.commit()
    await m.answer("Готово, я всё забыла. Начнём с чистого листа 🙂")


@dp.message()
async def chat(m: types.Message):
    if not m.text:
        return await m.answer("Пока я понимаю только текст 🙂")
    uid = m.from_user.id
    save(uid, "user", m.text)
    known = facts(uid)
    now = datetime.now(TZ).strftime("%H:%M")
    system = PERSONA + f"\n\nСейчас у собеседника {now}." + (
        "\n\nЧто ты помнишь о собеседнике:\n- " + "\n- ".join(known) if known else "")
    await bot.send_chat_action(m.chat.id, "typing")
    try:
        reply = await llm(history(uid), system=system)
    except Exception:
        return await m.answer("ой, что-то я зависла, напиши ещё раз через минутку 🙈")
    parts = [p.strip() for p in reply.split("||") if p.strip()] or ["..."]
    text = " ".join(parts)
    save(uid, "assistant", text)
    row = db.execute("SELECT voice FROM prefs WHERE uid=?", (uid,)).fetchone()
    if row and row[0]:
        path = await tts(text)
        try:
            await m.answer_voice(FSInputFile(path))
        finally:
            os.remove(path)
    else:
        for p in parts:   # несколько коротких сообщений с паузой, как будто печатает
            await bot.send_chat_action(m.chat.id, "typing")
            await asyncio.sleep(min(0.5 + 0.04 * len(p), 4))
            await m.answer(p)
    n = db.execute("SELECT COUNT(*) FROM msgs WHERE uid=? AND role='user'", (uid,)).fetchone()[0]
    if n % EXTRACT_EVERY == 0:
        asyncio.create_task(extract_facts(uid))


asyncio.run(dp.start_polling(bot))
