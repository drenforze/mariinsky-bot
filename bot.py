"""
Telegram-бот для уведомлений о новых событиях в афише Мариинского театра.
Отслеживает страницу: https://www.mariinsky.ru/playbill/playbill/
"""

import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL", "1"))
DATA_FILE = Path("data/seen_events.json")
SUBSCRIBERS_FILE = Path("data/subscribers.json")

BASE_URL = "https://www.mariinsky.ru"
PLAYBILL_URL = f"{BASE_URL}/ru/playbill/playbill/"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Хранилище ────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_seen_events() -> set:
    return set(load_json(DATA_FILE, []))

def save_seen_events(events: set):
    save_json(DATA_FILE, list(events))

def get_subscribers() -> list:
    return load_json(SUBSCRIBERS_FILE, [])

def add_subscriber(chat_id: int) -> bool:
    subs = get_subscribers()
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)
        return True
    return False

def remove_subscriber(chat_id: int) -> bool:
    subs = get_subscribers()
    if chat_id in subs:
        subs.remove(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)
        return True
    return False

# ─── Парсер афиши ─────────────────────────────────────────────────────────────

async def fetch_playbill_events() -> list:
    events = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await page.goto(PLAYBILL_URL, wait_until="networkidle", timeout=60000)
            try:
                await page.wait_for_selector(
                    ".b-playbill-item, .playbill__item, [class*='playbill'], [class*='performance']",
                    timeout=15000
                )
            except Exception:
                pass

            raw = await page.evaluate("""
                () => {
                    const results = [];
                    const candidates = [
                        '.b-playbill-item', '.playbill__item', '.js-playbill-item',
                        '[data-type="performance"]', '.performance-item', '.event-block',
                        'a[href*="/playbill/"]', 'a[href*="/performance/"]'
                    ];
                    let items = [];
                    for (const sel of candidates) {
                        items = [...document.querySelectorAll(sel)];
                        if (items.length > 0) break;
                    }
                    items.forEach(el => {
                        const titleEl =
                            el.querySelector('.b-performance__title, .title, .name, h2, h3, h4') ||
                            (el.tagName === 'A' ? el : null);
                        if (!titleEl) return;
                        const titleText = (titleEl.innerText || titleEl.textContent || '').trim();
                        if (!titleText || titleText.length < 3) return;
                        const dateEl  = el.querySelector('[class*="date"], time');
                        const timeEl  = el.querySelector('[class*="time"]');
                        const venueEl = el.querySelector('[class*="venue"], [class*="hall"], [class*="stage"]');
                        const linkEl  = el.tagName === 'A' ? el : el.querySelector('a[href]');
                        results.push({
                            title: titleText,
                            date:  (dateEl?.innerText  || '').trim(),
                            time:  (timeEl?.innerText  || '').trim(),
                            venue: (venueEl?.innerText || '').trim(),
                            link:  linkEl?.href || ''
                        });
                    });
                    return results;
                }
            """)
            await browser.close()

            seen_titles = set()
            for item in raw:
                title = item.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                link = item.get("link", "")
                if link and not link.startswith("http"):
                    link = BASE_URL + link
                uid_src = f"{title}|{item.get('date', '')}|{item.get('time', '')}"
                event_id = hashlib.md5(uid_src.encode()).hexdigest()[:12]
                events.append({
                    "id": event_id, "title": title,
                    "date": item.get("date", ""), "time": item.get("time", ""),
                    "venue": item.get("venue", ""), "link": link,
                })
    except Exception as e:
        logger.error(f"Ошибка при парсинге: {e}")
    logger.info(f"Найдено событий: {len(events)}")
    return events

# ─── Уведомления ──────────────────────────────────────────────────────────────

def format_event_message(event: dict) -> str:
    lines = ["🎭 <b>Новое в афише Мариинского!</b>\n"]
    lines.append(f"<b>{event['title']}</b>")
    dt = " ".join(filter(None, [event.get("date"), event.get("time")]))
    if dt:
        lines.append(f"📅 {dt}")
    if event.get("venue"):
        lines.append(f"🏛 {event['venue']}")
    if event.get("link"):
        lines.append(f'\n🔗 <a href="{event["link"]}">Подробнее / билеты</a>')
    return "\n".join(lines)

async def notify_subscribers(bot: Bot, new_events: list):
    subscribers = get_subscribers()
    if not subscribers:
        return
    for event in new_events:
        text = format_event_message(event)
        for chat_id in subscribers:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=False,
                )
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Не удалось отправить {chat_id}: {e}")

# ─── Основная задача ──────────────────────────────────────────────────────────

async def check_for_new_events(bot: Bot):
    logger.info("🔍 Проверяем афишу...")
    events = await fetch_playbill_events()
    if not events:
        logger.warning("Список событий пуст.")
        return
    seen_ids = get_seen_events()
    if not seen_ids:
        logger.info(f"Первый запуск — запоминаем {len(events)} событий.")
        save_seen_events({e["id"] for e in events})
        return
    new_events = [e for e in events if e["id"] not in seen_ids]
    if new_events:
        logger.info(f"🆕 Новых: {len(new_events)}")
        await notify_subscribers(bot, new_events)
        seen_ids.update(e["id"] for e in new_events)
        save_seen_events(seen_ids)
    else:
        logger.info("Новых событий нет.")

# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if add_subscriber(chat_id):
        await update.message.reply_text(
            "✅ <b>Вы подписались на уведомления!</b>\n\n"
            "Буду сообщать, когда в афише Мариинского появятся новые показы.\n\n"
            "/stop — отписаться\n/status — статистика\n/check — проверить сейчас",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("Вы уже подписаны! 🎭")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if remove_subscriber(update.effective_chat.id):
        await update.message.reply_text("❌ Вы отписались.")
    else:
        await update.message.reply_text("Вы не были подписаны.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = get_subscribers()
    seen = get_seen_events()
    status = "✅ подписан" if chat_id in subs else "❌ не подписан"
    await update.message.reply_text(
        f"Статус: {status}\nПодписчиков: {len(subs)}\n"
        f"Событий в базе: {len(seen)}\nПроверка каждые {CHECK_INTERVAL_MINUTES} мин."
    )

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Загружаю афишу (~15 сек)...")
    await check_for_new_events(context.application.bot)
    await update.message.reply_text("✅ Готово.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎭 <b>Бот афиши Мариинского</b>\n\n"
        "/start — подписаться\n/stop — отписаться\n"
        "/status — статус\n/check — проверить сейчас",
        parse_mode=ParseMode.HTML,
    )

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_for_new_events,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[application.bot],
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info(f"🚀 Планировщик запущен. Проверка каждые {CHECK_INTERVAL_MINUTES} минут.")

def main():
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА":
        raise RuntimeError("❌ Укажите BOT_TOKEN.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("help",   cmd_help))

    logger.info("🚀 Бот запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
