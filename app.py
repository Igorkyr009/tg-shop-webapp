# app.py — два бота: покупательский (shop) + админ (admin). aiogram 3.7
import os, asyncio, time, json
from typing import List, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import aiosqlite

# ---------- ENV ----------
load_dotenv()
SHOP_TOKEN      = "8324679528:AAEqvd8T0-oB5GywVNT6EKxGAiCRT6RLkrs"
ADMIN_BOT_TOKEN = "8389668734:AAFeEvBK36YDhgYfc4-YsDAKUN3kSO3J_uI"
WEBAPP_URL      = "https://tg-shop-webapp.vercel.app/index.html"
DB_PATH         = os.getenv("DB_PATH", "/data/shop.db").strip()  # локально можно "shop.db"

if not SHOP_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN пуст. Вставь токен покупательского бота в .env.")
print("WEBAPP_URL =", WEBAPP_URL or "<пусто>")
print("DB_PATH    =", DB_PATH)

bot_shop  = Bot(SHOP_TOKEN,      default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp_shop   = Dispatcher()
bot_admin = Bot(ADMIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if ADMIN_BOT_TOKEN else None
dp_admin  = Dispatcher() if ADMIN_BOT_TOKEN else None

# ---------- DB ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS products (
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'UAH',
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_user_id INTEGER NOT NULL,
  tg_username TEXT,
  tg_name TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'UAH',
  city TEXT,
  branch TEXT,
  receiver TEXT,
  phone TEXT,
  status TEXT DEFAULT 'new',
  np_ttn TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  product_sku TEXT NOT NULL,
  product_title TEXT NOT NULL,
  price INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""

async def db():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with await db() as d:
        await d.executescript(CREATE_SQL)
        # seed products, если пусто
        cur = await d.execute("SELECT COUNT(*) FROM products")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            await d.executemany(
                "INSERT INTO products (sku,title,price,currency,is_active) VALUES (?,?,?,?,1)",
                [
                    ("coffee_1kg", "Кофе в зёрнах 1 кг", 1299, "UAH"),
                    ("mug_brand",  "Кружка бренда",       299, "UAH"),
                ],
            )
        await d.commit()

# helpers settings
async def set_setting(key:str, value:str):
    async with await db() as d:
        await d.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await d.commit()

async def get_setting(key:str) -> Optional[str]:
    async with await db() as d:
        cur = await d.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---------- SHOP BOT handlers ----------
from aiogram.types import WebAppInfo, MenuButtonWebApp

@dp_shop.message(Command("start"))
async def shop_start(m: Message):
    if WEBAPP_URL:
        kb = InlineKeyboardBuilder()
        kb.button(text="🛍 Открыть витрину", web_app=WebAppInfo(url=WEBAPP_URL))
        kb.adjust(1)
        await m.answer("Добро пожаловать! Нажми кнопку, чтобы открыть витрину:", reply_markup=kb.as_markup())
    else:
        await m.answer("Витрина временно недоступна. Укажи WEBAPP_URL в .env.")

@dp_shop.message(Command("webapp"))
async def shop_webapp(m: Message):
    if not WEBAPP_URL:
        return await m.answer("WEBAPP_URL пуст. Добавь ссылку в .env.")
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Открыть витрину (внутри Telegram)", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.button(text="🌐 Открыть в браузере", url=WEBAPP_URL)
    kb.adjust(1)
    await m.answer("Открой витрину:", reply_markup=kb.as_markup())

@dp_shop.message(Command("debug"))
async def shop_debug(m: Message):
    await m.answer(f"WEBAPP_URL сейчас: {WEBAPP_URL}\nDB_PATH: {DB_PATH}")

# Приём данных из WebApp
@dp_shop.message(F.web_app_data)
async def on_webapp_data(m: Message):
    # ожидаем {"type":"checkout","items":[{"sku":"...","qty":1},...], "city":..., "branch":..., "receiver":..., "phone":...}
    try:
        data = json.loads(m.web_app_data.data)
    except Exception:
        return await m.answer("Не получилось прочитать данные из витрины.")
    if data.get("type") != "checkout":
        return await m.answer("Получены данные витрины, но тип неизвестен.")

    # подгрузим актуальные цены из БД
    async with await db() as d:
        total = 0
        currency = "UAH"
        items_to_save: List[Tuple[str,str,int,int]] = []
        for it in data.get("items", []):
            sku = str(it.get("sku"))
            qty = int(it.get("qty", 1))
            cur = await d.execute("SELECT title, price, currency FROM products WHERE sku=? AND is_active=1", (sku,))
            row = await cur.fetchone()
            if not row or qty <= 0:
                continue
            title, price, currency = row
            total += price * qty
            items_to_save.append((sku, title, price, qty))

        if not items_to_save:
            return await m.answer("Корзина пуста или товары недоступны.")

        city     = (data.get("city") or "").strip()
        branch   = (data.get("branch") or "").strip()
        receiver = (data.get("receiver") or "").strip()
        phone    = (data.get("phone") or "").strip()

        # сохранить заказ
        cur = await d.execute(
            "INSERT INTO orders (tg_user_id,tg_username,tg_name,total,currency,city,branch,receiver,phone,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m.from_user.id,
             f"@{m.from_user.username}" if m.from_user.username else None,
             f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip(),
             total, currency, city, branch, receiver, phone, "new", int(time.time()))
        )
        await d.commit()
        order_id = cur.lastrowid
        for sku, title, price, qty in items_to_save:
            await d.execute(
                "INSERT INTO order_items (order_id, product_sku, product_title, price, qty) VALUES (?,?,?,?,?)",
                (order_id, sku, title, price, qty)
            )
        await d.commit()

    await m.answer(f"✅ Заказ #{order_id} создан! Мы свяжемся по доставке НП.")

    # уведомить админа
    items_txt = "\n".join([f"• {t} × {q} = {p*q} {currency}" for _, t, p, q in items_to_save])
    msg = (f"🆕 Новый заказ #{order_id}\n"
           f"Покупатель: {m.from_user.first_name} {m.from_user.last_name or ''} "
           f"({('@'+m.from_user.username) if m.from_user.username else '—'})\n"
           f"ID: {m.from_user.id}\n"
           f"{items_txt}\nИтого: {total} {currency}\n"
           f"Город: {city}\nОтделение: {branch}\n"
           f"Получатель: {receiver} / {phone}")
    await notify_admin(msg)

async def notify_admin(text: str):
    # шлём в админ-бот, если задан токен и зарегистрирован чат, иначе — игнор
    chat_id = await get_setting("ADMIN_CHAT_ID")
    if bot_admin and chat_id:
        try:
            await bot_admin.send_message(int(chat_id), text)
            return
        except Exception as e:
            print("notify_admin через админ-бот: ошибка:", e)
    # запасной вариант — в личку себе из shop-бота (если кто-то установил)
    fallback = await get_setting("SHOP_ADMIN_CHAT_ID")
    if fallback:
        try:
            await bot_shop.send_message(int(fallback), text)
        except Exception:
            pass

# ---------- ADMIN BOT handlers ----------
HELP_TEXT = (
    "Команды админ-бота:\n"
    "/setme — назначить этот чат админским (для уведомлений)\n"
    "/orders — последние 10 заказов\n"
    "/order <id> — детали заказа\n"
    "/status <id> <new|paid|packed|shipped|done|cancelled> — сменить статус\n"
    "/ttn <id> <номер> — сохранить ТТН НП\n"
    "/products — список товаров\n"
    "/addproduct <sku> | <Название> | <цена> [| UAH] — добавить товар\n"
    "/setprice <sku> <цена> — обновить цену\n"
    "/settitle <sku> | <Новое название> — обновить название\n"
    "/toggle <sku> — включить/выключить товар"
)

if dp_admin:

    @dp_admin.message(Command("start"))
    async def admin_start(m: Message):
        await m.answer("Админ-бот. " + HELP_TEXT)

    @dp_admin.message(Command("setme"))
    async def admin_setme(m: Message):
        await set_setting("ADMIN_CHAT_ID", str(m.chat.id))
        await m.answer(f"Ок, этот чат сохранён для уведомлений: <code>{m.chat.id}</code>")

    @dp_admin.message(Command("orders"))
    async def admin_orders(m: Message):
        async with await db() as d:
            cur = await d.execute(
                "SELECT id,total,currency,city,branch,receiver,phone,status,created_at FROM orders ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            return await m.answer("Заказов нет.")
        lines = []
        for oid, total, curr, city, branch, recv, phone, status, ts in rows:
            lines.append(f"#{oid} • {total} {curr} • {status} • {time.strftime('%d.%m %H:%M', time.localtime(ts))}\n"
                         f"{city} / {branch}\n{recv} / {phone}\n———")
        await m.answer("\n".join(lines))

    @dp_admin.message(Command("order"))
    async def admin_order(m: Message, command: CommandObject):
        if not command.args:
            return await m.answer("Формат: /order <id>")
        oid = int(command.args.strip())
        async with await db() as d:
            cur = await d.execute("SELECT id,total,currency,city,branch,receiver,phone,status,created_at FROM orders WHERE id=?", (oid,))
            o = await cur.fetchone()
            if not o:
                return await m.answer("Не найдено.")
            cur = await d.execute("SELECT product_title, price, qty FROM order_items WHERE order_id=?", (oid,))
            items = await cur.fetchall()
        items_txt = "\n".join([f"• {t} × {q} = {p*q}" for t,p,q in items])
        await m.answer(
            f"Заказ #{o[0]} • {o[1]} {o[2]} • {o[7]} • {time.strftime('%d.%m %H:%M', time.localtime(o[8]))}\n"
            f"{items_txt}\nГород: {o[3]}\nОтделение: {o[4]}\nПолучатель: {o[5]} / {o[6]}"
        )

    @dp_admin.message(Command("status"))
    async def admin_status(m: Message, command: CommandObject):
        try:
            oid, new_status = command.args.split(maxsplit=1)
            oid = int(oid)
        except Exception:
            return await m.answer("Формат: /status <id> <new|paid|packed|shipped|done|cancelled>")
        async with await db() as d:
            await d.execute("UPDATE orders SET status=? WHERE id=?", (new_status.strip(), oid))
            await d.commit()
        await m.answer(f"Статус заказа #{oid} → {new_status}")

    @dp_admin.message(Command("ttn"))
    async def admin_ttn(m: Message, command: CommandObject):
        try:
            oid, ttn = command.args.split(maxsplit=1)
            oid = int(oid)
        except Exception:
            return await m.answer("Формат: /ttn <id> <номер>")
        async with await db() as d:
            await d.execute("UPDATE orders SET np_ttn=? WHERE id=?", (ttn.strip(), oid))
            await d.commit()
        await m.answer(f"TTN для заказа #{oid} сохранён.")

    @dp_admin.message(Command("products"))
    async def admin_products(m: Message):
        async with await db() as d:
            cur = await d.execute("SELECT sku,title,price,currency,is_active FROM products ORDER BY title")
            rows = await cur.fetchall()
        if not rows:
            return await m.answer("Каталог пуст.")
        txt = "\n".join([f"{'✅' if r[4] else '⛔️'} <b>{r[1]}</b> [{r[0]}] — {r[2]} {r[3]}" for r in rows])
        await m.answer(txt)

    @dp_admin.message(Command("addproduct"))
    async def admin_addproduct(m: Message, command: CommandObject):
        # /addproduct sku | Название | 999 [| UAH]
        if not command.args or "|" not in command.args:
            return await m.answer("Формат: /addproduct <sku> | <Название> | <цена> [| Валюта]")
        parts = [p.strip() for p in command.args.split("|")]
        if len(parts) < 3:
            return await m.answer("Нужно минимум: sku | Название | цена")
        sku, title, price = parts[:3]
        currency = parts[3] if len(parts) >= 4 else "UAH"
        try:
            price = int(price)
        except:
            return await m.answer("Цена должна быть целым числом (в копейках/гривнах без копеек — как решишь).")
        async with await db() as d:
            await d.execute("INSERT INTO products (sku,title,price,currency,is_active) VALUES (?,?,?,?,1) ON CONFLICT(sku) DO UPDATE SET title=excluded.title, price=excluded.price, currency=excluded.currency, is_active=1", (sku, title, price, currency))
            await d.commit()
        await m.answer(f"Товар [{sku}] добавлен/обновлён: {title} — {price} {currency}")

    @dp_admin.message(Command("setprice"))
    async def admin_setprice(m: Message, command: CommandObject):
        try:
            sku, price = command.args.split(maxsplit=1)
            price = int(price)
        except Exception:
            return await m.answer("Формат: /setprice <sku> <цена>")
        async with await db() as d:
            await d.execute("UPDATE products SET price=? WHERE sku=?", (price, sku))
            await d.commit()
        await m.answer(f"Цена {sku} → {price}")

    @dp_admin.message(Command("settitle"))
    async def admin_settitle(m: Message, command: CommandObject):
        if "|" not in (command.args or ""):
            return await m.answer("Формат: /settitle <sku> | <Новое название>")
        sku, title = [p.strip() for p in command.args.split("|", 1)]
        async with await db() as d:
            await d.execute("UPDATE products SET title=? WHERE sku=?", (title, sku))
            await d.commit()
        await m.answer(f"Название {sku} → {title}")

    @dp_admin.message(Command("toggle"))
    async def admin_toggle(m: Message, command: CommandObject):
        sku = (command.args or "").strip()
        if not sku:
            return await m.answer("Формат: /toggle <sku>")
        async with await db() as d:
            cur = await d.execute("SELECT is_active FROM products WHERE sku=?", (sku,))
            row = await cur.fetchone()
            if not row:
                return await m.answer("SKU не найден.")
            newv = 0 if row[0] else 1
            await d.execute("UPDATE products SET is_active=? WHERE sku=?", (newv, sku))
            await d.commit()
        await m.answer(f"{'Включен' if newv else 'Отключен'} товар {sku}")

# ---------- MENU BUTTON для shop ----------
async def setup_menu_button():
    if WEBAPP_URL:
        try:
            await bot_shop.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🛍 Открыть витрину", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        except Exception as e:
            print("set_chat_menu_button error:", e)

# ---------- RUN ----------
async def main():
    await init_db()
    await setup_menu_button()
    tasks = [asyncio.create_task(dp_shop.start_polling(bot_shop))]
    if dp_admin and bot_admin:
        tasks.append(asyncio.create_task(dp_admin.start_polling(bot_admin)))
    print("Shop bot и Admin bot запущены. Нажми Ctrl+C для остановки.")
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
