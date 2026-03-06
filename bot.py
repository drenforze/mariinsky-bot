"""
Telegram-бот для Мариинского театра.
1. Уведомляет о новых спектаклях в афише
2. Уведомляет о появлении билетов «Место с ограниченной видимостью» в 3-м ярусе
"""

import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime, timedelta
import re
from pathlib import Path

from playwright.async_api import async_playwright
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN        = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА")
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "5"))    # афиша — каждые 5 мин
TICKETS_INTERVAL = int(os.getenv("TICKETS_INTERVAL", "15")) # билеты — каждые 15 мин

DATA_FILE        = Path("data/seen_events.json")
TICKETS_FILE     = Path("data/seen_tickets.json")
SUBSCRIBERS_FILE = Path("data/subscribers.json")

BASE_URL         = "https://www.mariinsky.ru"
PLAYBILL_URL     = f"{BASE_URL}/ru/playbill/playbill/"

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

def save_seen_events(e: set):
    save_json(DATA_FILE, list(e))

def get_seen_tickets() -> set:
    return set(load_json(TICKETS_FILE, []))

def save_seen_tickets(t: set):
    save_json(TICKETS_FILE, list(t))

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

async def fetch_playbill_events(page) -> list:
    events = []
    try:
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
                    '[data-type="performance"]', '.performance-item',
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
                    const venueEl = el.querySelector('[class*="venue"], [class*="hall"]');
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

        seen_titles = set()
        for item in raw:
            title = item.get("title", "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            link = item.get("link", "")
            if link and not link.startswith("http"):
                link = BASE_URL + link
            uid = hashlib.md5(
                f"{title}|{item.get('date','')}|{item.get('time','')}".encode()
            ).hexdigest()[:12]
            events.append({
                "id": uid, "title": title,
                "date": item.get("date", ""), "time": item.get("time", ""),
                "venue": item.get("venue", ""), "link": link,
            })
    except Exception as e:
        logger.error(f"Ошибка парсинга афиши: {e}")

    logger.info(f"Найдено событий в афише: {len(events)}")
    return events

# ─── Парсер билетов с ограниченной видимостью ─────────────────────────────────

async def check_restricted_tickets(page, event: dict) -> list:
    """
    Ищет доступные места в 3-м ярусе (боковые) на странице спектакля.
    Доступное место = img.place с value содержащим '3-й ярус' и 'сторона'.
    """
    if not event.get("link"):
        return []

    found_seats = []
    try:
        await page.goto(event["link"], wait_until="domcontentloaded", timeout=15000)
        try:
            await page.wait_for_selector("img.place", timeout=8000)
        except Exception:
            return []

        seats = await page.evaluate("""
            () => {
                const results = [];
                const places = document.querySelectorAll('img.place');
                places.forEach(el => {
                    const value = el.getAttribute('value') || '';
                    // value формат: "3-й ярус;Левая сторона;Ряд 2;Место 73;;1600;2400;;;1;;0;1"
                    if (
                        value.includes('3-й ярус') &&
                        (value.includes('Левая сторона') || value.includes('Правая сторона'))
                    ) {
                        const parts = value.split(';');
                        const tier   = parts[0] || '';
                        const side   = parts[1] || '';
                        const row    = parts[2] || '';
                        const seat   = parts[3] || '';
                        const price  = parts[5] || '';
                        results.push({
                            info:  `${tier}, ${side}, ${row}, ${seat}`.replace(/\s+/g, ' ').trim(),
                            price: price ? price + ' ₽' : ''
                        });
                    }
                });
                return results;
            }
        """)

        found_seats = seats or []

    except Exception as e:
        logger.warning(f"Ошибка проверки билетов '{event['title']}': {e}")

    return found_seats


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

def format_ticket_message(event: dict, seats: list) -> str:
    lines = ["🎟 <b>Места с ограниченной видимостью в 3-м ярусе!</b>\n"]
    lines.append(f"<b>{event['title']}</b>")
    dt = " ".join(filter(None, [event.get("date"), event.get("time")]))
    if dt:
        lines.append(f"📅 {dt}")
    if event.get("venue"):
        lines.append(f"🏛 {event['venue']}")
    lines.append(f"\n💺 Найдено мест: {len(seats)}")
    for seat in seats[:3]:  # показываем максимум 3 примера
        info = seat.get("info", "")
        price = seat.get("price", "")
        if price:
            lines.append(f"  • {info[:80]} — {price}")
        else:
            lines.append(f"  • {info[:80]}")
    if len(seats) > 3:
        lines.append(f"  ...и ещё {len(seats) - 3}")
    if event.get("link"):
        lines.append(f'\n🔗 <a href="{event["link"]}">Купить билет</a>')
    return "\n".join(lines)

async def send_to_all(bot: Bot, text: str):
    for chat_id in get_subscribers():
        try:
            await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Не удалось отправить {chat_id}: {e}")

# ─── Основная задача ──────────────────────────────────────────────────────────

async def check_playbill(bot: Bot):
    """Проверяет только новые спектакли в афише. Быстро (~5 сек)."""
    logger.info("🔍 Проверяем афишу...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))
        events = await fetch_playbill_events(page)
        await browser.close()

    if events:
        seen_ids = get_seen_events()
        if not seen_ids:
            logger.info(f"Первый запуск — запоминаем {len(events)} событий.")
            save_seen_events({e["id"] for e in events})
        else:
            new_events = [e for e in events if e["id"] not in seen_ids]
            if new_events:
                logger.info(f"🆕 Новых спектаклей: {len(new_events)}")
                for event in new_events:
                    await send_to_all(bot, format_event_message(event))
                seen_ids.update(e["id"] for e in new_events)
                save_seen_events(seen_ids)
            else:
                logger.info("Новых спектаклей нет.")
    logger.info("✅ Афиша проверена.")


async def check_tickets(bot: Bot):
    """Проверяет билеты 3-го яруса на исторической сцене (7 дней). ~15–20 сек."""
    logger.info("🎟 Проверяем билеты...")

    # Берём список спектаклей из уже сохранённой базы
    # (афиша проверяется отдельно и чаще)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))
        # Получаем свежий список спектаклей
        events = await fetch_playbill_events(page)

        today = datetime.now().date()
        deadline = today + timedelta(days=7)

        def parse_event_date(date_str: str):
            months = {
                "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
                "мая": 5, "июня": 6, "июля": 7, "августа": 8,
                "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
            }
            try:
                m = re.search(r'(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?', date_str.lower())
                if m:
                    day = int(m.group(1))
                    month = months.get(m.group(2))
                    year = int(m.group(3)) if m.group(3) else today.year
                    if month:
                        return datetime(year, month, day).date()
                m2 = re.search(r'(\d{2})\.(\d{2})(?:\.(\d{4}))?', date_str)
                if m2:
                    day, month = int(m2.group(1)), int(m2.group(2))
                    year = int(m2.group(3)) if m2.group(3) else today.year
                    return datetime(year, month, day).date()
            except Exception:
                pass
            return None

        historical_events = []
        for e in events:
            venue = e.get("venue", "").lower()
            if "мариинский театр" not in venue:
                continue
            if "мариинский-2" in venue or "концертный" in venue:
                continue
            event_date = parse_event_date(e.get("date", ""))
            if event_date is None or today <= event_date <= deadline:
                historical_events.append(e)

        logger.info(f"Спектаклей на исторической сцене (7 дней): {len(historical_events)}")

        seen_tickets = get_seen_tickets()
        first_ticket_run = len(seen_tickets) == 0

        for event in historical_events:
            if not event.get("link"):
                continue
            seats = await check_restricted_tickets(page, event)
            if not seats:
                continue
            ticket_key = f"{event['id']}:{len(seats)}"
            if first_ticket_run:
                seen_tickets.add(ticket_key)
            elif ticket_key not in seen_tickets:
                logger.info(f"🎟 Новые места: {event['title']} ({len(seats)} мест)")
                await send_to_all(bot, format_ticket_message(event, seats))
                seen_tickets.add(ticket_key)
            await asyncio.sleep(0.3)

        if first_ticket_run and seen_tickets:
            logger.info(f"Первый запуск билетов — запомнили {len(seen_tickets)} записей.")
        save_seen_tickets(seen_tickets)
        await browser.close()

    logger.info("✅ Билеты проверены.")


async def check_all(bot: Bot):
    """Запускает обе проверки последовательно (используется в /check)."""
    await check_playbill(bot)
    await check_tickets(bot)

# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if add_subscriber(update.effective_chat.id):
        await update.message.reply_text(
            "✅ <b>Вы подписались на уведомления!</b>\n\n"
            "Буду сообщать о:\n"
            "🎭 Новых спектаклях в афише\n"
            "🎟 Местах с ограниченной видимостью в 3-м ярусе\n\n"
            "/stop — отписаться\n/status — статистика\n"
            "/check_playbill — проверить афишу\n"
            "/check_tickets — проверить билеты\n"
            "/check — проверить всё сразу",
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
    seen_t = get_seen_tickets()
    status = "✅ подписан" if chat_id in subs else "❌ не подписан"
    await update.message.reply_text(
        f"Статус: {status}\n"
        f"Подписчиков: {len(subs)}\n"
        f"Спектаклей в базе: {len(seen)}\n"
        f"Записей о билетах: {len(seen_t)}\n"
        f"Афиша: каждые {CHECK_INTERVAL} мин.\n"
        f"Билеты: каждые {TICKETS_INTERVAL} мин."
    )

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Проверяю всё...")
    await check_playbill(context.application.bot)
    await update.message.reply_text("🎭 Афиша проверена. Проверяю билеты (~15 сек)...")
    await check_tickets(context.application.bot)
    await update.message.reply_text("✅ Готово.")

async def cmd_check_playbill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Проверяю афишу...")
    await check_playbill(context.application.bot)
    await update.message.reply_text("✅ Афиша проверена.")

async def cmd_check_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎟 Проверяю билеты (~15 сек)...")
    await check_tickets(context.application.bot)
    await update.message.reply_text("✅ Билеты проверены.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎭 <b>Бот Мариинского театра</b>\n\n"
        "Отслеживает:\n"
        "• Новые спектакли в афише\n"
        "• Места с ограниченной видимостью в 3-м ярусе\n\n"
        "/start — подписаться\n/stop — отписаться\n"
        "/status — статус\n"
        "/check_playbill — проверить афишу\n"
        "/check_tickets — проверить билеты\n"
        "/check — проверить всё сразу",
        parse_mode=ParseMode.HTML,
    )

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    scheduler = AsyncIOScheduler()
    # Афиша — часто
    scheduler.add_job(
        check_playbill, "interval",
        minutes=CHECK_INTERVAL,
        args=[application.bot],
        next_run_time=datetime.now(),
    )
    # Билеты — реже, через 1 минуту после старта
    from datetime import timedelta as td
    scheduler.add_job(
        check_tickets, "interval",
        minutes=TICKETS_INTERVAL,
        args=[application.bot],
        next_run_time=datetime.now() + td(minutes=1),
    )
    scheduler.start()
    logger.info(
        f"🚀 Планировщик запущен. "
        f"Афиша каждые {CHECK_INTERVAL} мин., билеты каждые {TICKETS_INTERVAL} мин."
    )

def main():
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА":
        raise RuntimeError("❌ Укажите BOT_TOKEN.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("stop",           cmd_stop))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("check",          cmd_check))
    app.add_handler(CommandHandler("check_playbill", cmd_check_playbill))
    app.add_handler(CommandHandler("check_tickets",  cmd_check_tickets))
    app.add_handler(CommandHandler("help",           cmd_help))

    logger.info("🚀 Бот запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
