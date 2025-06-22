import asyncio
import logging
import json
import os
import threading
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    Bot,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
)
from aiohttp import ClientSession, CookieJar
from bs4 import BeautifulSoup
from flask import Flask, request, Response, redirect, session as flask_session, make_response
import uuid
import requests

# Токен Telegram-бота
TOKEN = "7690678050:AAGBwTdSUNgE7Q6Z2LpE6481vvJJhetrO-4"
bot = Bot(TOKEN)

# Базовый URL вашего веб-прокси/Flask-сервера
BASE_URL = os.getenv("BASE_URL", "https://mpets.duckdns.org/")


# Путь к файлу для хранения сессий
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.txt")

# Разрешённые пользователи (ID Telegram) для специальных команд
ALLOWED_USER_IDS = [1811568463, 630965641]

TEMP_LOGINS = {}

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# Глобальные структуры данных для сессий
user_sessions = {}        # сохранённые сессии пользователей {user_id: {session_name: {...}}}
user_tasks = {}           # запущенные фоновые задачи { (user_id, session_name): task }
pending_cookies = {}      # куки, ожидающие подтверждения {(user_id, session_name): cookies_dict}

# Инициализация Flask приложения для WebApp
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev_secret")  # секрет для сессии Flask

# Чтение сохранённых сессий из файла
def read_from_file():
    if not os.path.exists(USERS_FILE): return []
    sessions = []
    with open(USERS_FILE, 'r') as f:
        for line in f:
            parts = line.strip().split(' | ')
            if len(parts) != 4:
                logging.warning(f"Bad line: {line.strip()}")
                continue
            name, owner, uid, cj = parts
            try:
                cookies = json.loads(cj)
            except:
                logging.error(f"JSON parse error for session {name}")
                continue
            sessions.append({
                'session_name': name,
                'owner': owner,
                'user_id': int(uid),
                'cookies': cookies,
            })
    return sessions

# Запись новой сессии в файл (добавление в конец)
def write_to_file(session_name, owner, user_id, cookies):
    if session_name is None:
        logging.warning(f"Пытаюсь сохранить сессию с именем None! Пропускаю.")
        return
    with open(USERS_FILE, 'a') as f:
        f.write(f"{session_name} | {owner} | {user_id} | {json.dumps(cookies)}\n")
    logging.info(f"Session {session_name} saved to file.")


# Загрузка сессий из файла в память при старте бота
def load_sessions():
    for sess in read_from_file():
        cookies = ({c['name']:c['value'] for c in sess['cookies']} 
                   if isinstance(sess['cookies'], list) else sess['cookies'])
        user_sessions.setdefault(sess['user_id'], {})[sess['session_name']] = {
            'owner': sess['owner'],
            'cookies': cookies,
            'active': False,
        }


# Команда /start – приветственное сообщение и список команд
async def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📋 Список сессий", callback_data="list")],
        [InlineKeyboardButton("🛟 Помощь",        callback_data="guide")],
        [InlineKeyboardButton("ℹ️ Информация",    callback_data="info")],
    ]
    await update.message.reply_text(
        "Привет! Меня зовут Cobalt, я создан для игры Удивительные Питомцы на сайте mpets.mobi. Благодаря мне ты можешь поставить своего питомца (или нескольких) на 'прокачку', чтобы не заходить в игру каждый день.\n"
        "Все делается автоматически: Кормление, Игра, Выставка, Прогулка, Поиск семян.\n"
        "Обрати внимание: для авторизации питомца в боте требуются cookie. Как получить их, ты можешь узнать в /guide.\n\n"
        "Мои команды:\n"
        "/info – Контактная информация\n"
        "/guide – инструкция по получению cookie\n"
        "/add – добавить новую сессию\n"
        "/del – удалить сессию\n"
        "/list – посмотреть все сессии\n"
        "/on – активировать сессию\n"
        "/off – деактивировать сессию\n"
        "/stats <имя_сессии> – проверить статистику питомца\n"
        "Для удобства ты можешь использовать кнопки",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Команда /info – информация о разработчике и канале
async def info(update: Update, context: CallbackContext):
    message = (
        "📬 Связь с разработчиком: [t.me/bakhusse](https://t.me/bakhusse)\n"
        "📤 Телеграм-канал: [t.me/cobalt_mpets](https://t.me/cobalt_mpets)"
    )
    await update.message.reply_text(message, parse_mode='Markdown')

# Команда /guide – инструкция по получению cookie (обновлена для WebApp)
async def guide(update: Update, context: CallbackContext):
    message = (
        "😍 Теперь получить cookie можно через встроенное мини-приложение Telegram!\n"
        "👉Просто используй команду /add, укажи название новой сессии и следуй инструкции для авторизации в мини-приложении.\n"
        "❗При использовании команды имя сессии нужно вводить вручную."
    )
    await update.message.reply_text(message)

# Команда /add – добавить новую сессию (с поддержкой WebApp авторизации)
async def add_session(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    # Проверяем аргументы: требуется имя сессии
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /add <имя_сессии>")
        return
    session_name = context.args[0]
    # Проверяем, что сессия с таким именем еще не существует у этого пользователя
    if user_id in user_sessions and session_name in user_sessions[user_id]:
        await update.message.reply_text(f"Сессия с именем `{session_name}` уже существует.", parse_mode='Markdown')
        return
    # Формируем URL для открытия WebApp (мини-приложения)
    tgid = user_id
    webapp_url = f"https://mpets.duckdns.org//?tgid={tgid}&name={session_name}"
    # Кнопка для открытия мини-приложения
    web_app_info = WebAppInfo(url=webapp_url)
    button = InlineKeyboardButton("🔑 Авторизоваться через MPets", web_app=web_app_info)
    keyboard = InlineKeyboardMarkup([[button]])
    await update.message.reply_text(
        f"👇 Для сессии *{session_name}* нажмите кнопку ниже и войдите в MPets:",
        parse_mode='Markdown',
        reply_markup=keyboard
    )
    logging.info(f"Пользователь {user_id} инициировал добавление сессии '{session_name}' через WebApp.")

# Команда /confirm – подтвердить и сохранить сессию после авторизации через WebApp
async def confirm_session(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    session_name = None
    cookies = None
    # Если указано имя сессии в аргументах, пытаемся получить куки для неё
    if context.args:
        session_name = context.args[0]
        key = (user_id, session_name)
        cookies = pending_cookies.get(key)
        if not cookies:
            await update.message.reply_text(f"Не найдены авторизационные данные для сессии `{session_name}`. Сначала используйте /add для этой сессии.", parse_mode='Markdown')
            return
    else:
        # Если имя не указано, но у пользователя есть ровно одна ожидающая сессия – используем её
        pending_for_user = [name for (uid, name) in pending_cookies.keys() if uid == user_id]
        if not pending_for_user:
            await update.message.reply_text("Нет незавершённых авторизаций. Сначала воспользуйтесь командой /add.")
            return
        if len(pending_for_user) > 1:
            await update.message.reply_text(
                "У вас несколько сессий ожидают подтверждения. Введите /confirm <имя_сессии> для каждой из них."
            )
            return
        # Единственная ожидающая сессия
        session_name = pending_for_user[0]
        cookies = pending_cookies.get((user_id, session_name))
        if not cookies:
            await update.message.reply_text("Куки не найдены. Попробуйте пройти авторизацию заново через /add.")
            return
    # Добавляем новую сессию в user_sessions
    user_sessions.setdefault(user_id, {})
    if session_name in user_sessions[user_id]:
        # Если по каким-то причинам сессия уже существует (напр., повторное подтверждение)
        await update.message.reply_text(f"Сессия `{session_name}` уже сохранена.", parse_mode='Markdown')
        # Удаляем отложенные куки, если они ещё есть
        pending_cookies.pop((user_id, session_name), None)
        return
    user_sessions[user_id][session_name] = {
        "owner": update.message.from_user.username or "",
        "cookies": cookies,
        "active": False
    }
    # Записываем сессию в файл
    write_to_file(session_name, update.message.from_user.username or "", user_id, cookies)
    # Очищаем временное хранилище куки
    pending_cookies.pop((user_id, session_name), None)
    await update.message.reply_text(f"Сессия *{session_name}* успешно сохранена! Теперь вы можете активировать её командой /on.", parse_mode='Markdown')
    logging.info(f"Пользователь {user_id} подтвердил и сохранил сессию '{session_name}'.")

# Команда /del – удалить сессию
async def remove_session(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /del <имя_сессии>")
        return
    session_name = context.args[0]
    ok = remove_session_data(user_id, session_name)
    if ok:
        await update.message.reply_text(f"🗑️ Сессия {session_name} удалена.")
    else:
        await update.message.reply_text(f"Сессия с именем {session_name} не найдена.")

def remove_session_data(user_id, session_name):
    # Удаляем из памяти
    if user_id in user_sessions and session_name in user_sessions[user_id]:
        if user_sessions[user_id][session_name]["active"]:
            task = user_tasks.pop((user_id, session_name), None)
            if task:
                task.cancel()
        user_sessions[user_id].pop(session_name, None)
    # Удаляем из файла
    sessions = read_from_file()
    new_sessions = [s for s in sessions if not (s['user_id'] == user_id and s['session_name'] == session_name)]
    with open(USERS_FILE, "w") as file:
        for s in new_sessions:
            cookies_json = json.dumps(s['cookies'])
            file.write(f"{s['session_name']} | {s['owner']} | {s['user_id']} | {cookies_json}\n")
    logging.info(f"Сессия {session_name} удалена для пользователя {user_id}.")

# Команда /list – показать все сохранённые сессии пользователя
async def list_sessions(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id in user_sessions and user_sessions[user_id]:
        sessions_list = "\n".join([f"• {name} ({'🟢' if data['active'] else '🔴'})" for name, data in user_sessions[user_id].items()])
        await update.message.reply_text(f"🤖 Ваши сессии:\n{sessions_list}")
    else:
        await update.message.reply_text("🤷‍♀️ У вас нет сохранённых сессий.")

async def list_sessions_buttons(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    sessions = user_sessions.get(user_id, {})
    keyboard = []
    for name, data in sessions.items():
        status = 'Вкл' if data['active'] else 'Выкл'
        keyboard.append([InlineKeyboardButton(f"{name} ({status})", callback_data=f"session:{name}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="add")])
    await update.message.reply_text(
        "🤖 Ваши сессии:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Команда /on – активировать одну или все сессии
async def activate_session(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /on <имя_сессии> или /on all")
        return
    session_name = context.args[0]
    # Если указано "all", активируем все сессии пользователя
    if session_name == "all":
        if user_id in user_sessions and user_sessions[user_id]:
            for name, session in user_sessions[user_id].items():
                if not session["active"]:
                    session["active"] = True
                    logging.info(f"Сессия {name} активирована для пользователя {user_id}.")
                    # Запускаем фоновые действия для этой сессии
                    task = asyncio.create_task(auto_actions(session["cookies"], name))
                    user_tasks[(user_id, name)] = task
            await update.message.reply_text("✅ Все ваши сессии активированы и запущены!")
        else:
            await update.message.reply_text("У вас нет сохранённых сессий.")
    else:
        # Активируем указанную сессию
        if user_id in user_sessions and session_name in user_sessions[user_id]:
            if user_sessions[user_id][session_name]["active"]:
                await update.message.reply_text(f"Сессия {session_name} уже активна.")
            else:
                user_sessions[user_id][session_name]["active"] = True
                task = asyncio.create_task(auto_actions(user_sessions[user_id][session_name]["cookies"], session_name))
                user_tasks[(user_id, session_name)] = task
                await update.message.reply_text(f"Сессия {session_name} активирована!")
                logging.info(f"Сессия {session_name} активирована для пользователя {user_id}.")
        else:
            await update.message.reply_text(f"Сессия с именем {session_name} не найдена.")

# Команда /off – деактивировать одну или все сессии
async def deactivate_session(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /off <имя_сессии> или /off all")
        return
    session_name = context.args[0]
    if session_name == "all":
        if user_id in user_sessions and user_sessions[user_id]:
            for name, session in user_sessions[user_id].items():
                if session["active"]:
                    session["active"] = False
                    task = user_tasks.get((user_id, name))
                    if task:
                        task.cancel()
                        user_tasks.pop((user_id, name), None)
                    logging.info(f"Сессия {name} деактивирована для пользователя {user_id}.")
            await update.message.reply_text("🛑 Все сессии деактивированы.")
        else:
            await update.message.reply_text("У вас нет активных сессий.")
    else:
        if user_id in user_sessions and session_name in user_sessions[user_id]:
            if user_sessions[user_id][session_name]["active"]:
                user_sessions[user_id][session_name]["active"] = False
                task = user_tasks.get((user_id, session_name))
                if task:
                    task.cancel()
                    user_tasks.pop((user_id, session_name), None)
                await update.message.reply_text(f"Сессия {session_name} деактивирована.")
                logging.info(f"Сессия {session_name} деактивирована для пользователя {user_id}.")
            else:
                await update.message.reply_text(f"Сессия {session_name} уже выключена.")
        else:
            await update.message.reply_text(f"Сессия с именем {session_name} не найдена.")

# Команда /stats – получить статистику питомца по указанной сессии
async def stats(update: Update, context: CallbackContext):
    if len(context.args) < 1:
        return await update.message.reply_text("Использование: /stats <имя_сессии>")
    user_id = update.message.from_user.id
    name = context.args[0]
    text = await fetch_stats_for_session(user_id, name)
    await update.message.reply_text(text)


async def fetch_stats_for_session(user_id: int, session_name: str) -> str:
    # та же логика, что у вас в stats(), но возвращает строку
    if user_id not in user_sessions or session_name not in user_sessions[user_id]:
        return f"Сессия «{session_name}» не найдена."

    cookies = user_sessions[user_id][session_name]["cookies"]
    if isinstance(cookies, list):
        cookies = {c["name"]: c["value"] for c in cookies}
    sess = requests.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0"})
    sess.cookies.update(cookies)

    loop = asyncio.get_event_loop()
    try:
        # активируем куки
        await loop.run_in_executor(None, lambda: sess.get("https://mpets.mobi/", allow_redirects=True, timeout=10))
        # проверяем редирект
        resp = await loop.run_in_executor(None, lambda: sess.get("https://mpets.mobi/profile", allow_redirects=False, timeout=10))
    except Exception as e:
        return f"Ошибка при запросе профиля: {e}"

    if resp.status_code in (301,302):
        return (
            f"❌ Сессия «{session_name}» не авторизована или устарела.\n"
            f"Введи `/add {session_name}` и авторизуйся заново."
        )
    if resp.status_code != 200:
        return f"Ошибка при загрузке профиля: {resp.status_code}"

    page = resp.text
    soup = BeautifulSoup(page, "html.parser")
    items = soup.find_all("div", class_="stat_item")
    if not items:
        return "Не удалось найти статистику на странице."

    name_tag = items[0].find("a", class_="darkgreen_link")
    name = name_tag.text.strip() if name_tag else "—"
    level = items[0].text.strip().split()[-2] if name_tag else "—"
    stats_map = {k:"—" for k in ["Опыт","Красота","Монеты","Сердечки","VIP-аккаунт"]}
    for itm in items:
        txt = itm.text.strip()
        for key in stats_map:
            if f"{key}:" in txt:
                stats_map[key] = txt.split(f"{key}:")[-1].strip()

    return (
        f"🐾 {name}, уровень {level}\n"
        f"✨ Опыт: {stats_map['Опыт']}\n"
        f"💄 Красота: {stats_map['Красота']}\n"
        f"💰 Монеты: {stats_map['Монеты']}\n"
        f"❤️ Сердечки: {stats_map['Сердечки']}\n"
        f"👑 VIP: {stats_map['VIP-аккаунт']}"
    )


# Команда для получения информации о владельце сессии
async def get_user(update: Update, context: CallbackContext):
    # Проверка, что пользователь имеет разрешение
    user_id = update.message.from_user.id
    if user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("У вас нет прав на использование этой команды.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Использование: /get_user <имя_сессии>")
        return

    session_name = context.args[0]

    session_info = read_from_file()
    for session in session_info:
        if session["session_name"] == session_name:
            response = f"Сессия: {session_name}\n"
            response += f"Владелец: {session['owner']}\n"

            # Форматируем куки как скрытый блок
            cookies = json.dumps(session['cookies'], indent=4)  # Форматируем куки с отступами для читаемости
            hidden_cookies = f"```json\n{cookies}```"  # Скрываем куки в блоке, доступном для раскрытия

            response += f"Куки:\n {hidden_cookies}"  # Добавляем цитату с куками

            await update.message.reply_text(response, parse_mode='Markdown')
            return

    await update.message.reply_text(f"Сессия с именем {session_name} не найдена.")

# Вспомогательная функция автоматических действий (периодические запросы для прокачки питомца)
async def auto_actions(session_cookies, session_name):
    # URL-адреса для автоматических действий
    actions = [
        "https://mpets.mobi/?action=food",
        "https://mpets.mobi/?action=play",
        "https://mpets.mobi/show",
        "https://mpets.mobi/glade_dig",
        "https://mpets.mobi/show_coin_get",
        "https://mpets.mobi/task_reward?id=46",
        "https://mpets.mobi/task_reward?id=49",
        "https://mpets.mobi/task_reward?id=52",
        "https://mpets.mobi/sell_all?confirm=1"
    ]
    # Формируем словарь cookies (если хранится список объектов)
    cookies_dict = {c['name']: c['value'] for c in session_cookies} if isinstance(session_cookies, list) else (session_cookies.get("cookies", {}) if "cookies" in session_cookies else session_cookies)
    # Создаём aiohttp-сессию с заданными cookie
    cookie_jar = CookieJar()
    for name, value in cookies_dict.items():
        cookie_jar.update_cookies({name: value})
    async with ClientSession(cookie_jar=cookie_jar) as web_session:
        while True:
            if asyncio.current_task().cancelled():
                logging.info(f"Автозадача для сессии {session_name} отменена.")
                return
            # Первые 4 действия повторяем 6 раз с паузой 1 сек
            for action_url in actions[:4]:
                for _ in range(6):
                    await visit_url(web_session, action_url, session_name)
                    await asyncio.sleep(5)
            # Оставшиеся действия выполняем по 1 разу
            for action_url in actions[4:]:
                await visit_url(web_session, action_url, session_name)
                await asyncio.sleep(5)
            # Дополнительные переходы с параметром id от 10 до 1
            for i in range(10, 0, -1):
                url = f"https://mpets.mobi/go_travel?id={i}"
                await visit_url(web_session, url, session_name)
                await asyncio.sleep(5)
            # Пауза между циклами (60 секунд)
            await asyncio.sleep(120)

# Вспомогательная функция для выполнения GET-запроса и логирования результата
async def visit_url(web_session, url, session_name):
    try:
        async with web_session.get(url) as response:
            if response.status == 200:
                logging.info(f"[{session_name}] Переход по {url} выполнен успешно.")
            else:
                logging.error(f"[{session_name}] Ошибка {response.status} при переходе по {url}.")
    except Exception as e:
        logging.error(f"[{session_name}] Ошибка при запросе {url}: {e}")



async def list_sessions_buttons(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    sessions = user_sessions.get(user_id, {})
    keyboard = []
    for name, data in sessions.items():
        status = '🟢' if data['active'] else '🔴'
        keyboard.append([InlineKeyboardButton(f"{name} ({status})", callback_data=f"session:{name}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="add")])
    await update.message.reply_text(
        "🤖 Ваши сессии:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def activate_session(update: Update, context: CallbackContext): pass  # unchanged
async def deactivate_session(update: Update, context: CallbackContext): pass
async def get_user(update: Update, context: CallbackContext): pass

# Обработчик inline-кнопок
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    back_list = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="list")]])
    back_menu = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main")]])

    if data == "main":
        # главное меню
        keyboard = [
            [InlineKeyboardButton("📋 Список сессий", callback_data="list")],
            [InlineKeyboardButton("🛟 Помощь",        callback_data="guide")],
            [InlineKeyboardButton("ℹ️ Информация",    callback_data="info")],
        ]
        return await query.edit_message_text(
            "🏠 Вы в главном меню:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Информация
    if data == "info":
        return await query.edit_message_text(
            "📬 Связь с разработчиком: @bakhusse\n"
            "📤 Телеграм-канал: @cobalt_mpets",
            reply_markup=back_menu
        )

    # Помощь
    if data == "guide":
        return await query.edit_message_text(
            "😍 Теперь получить cookie можно через мини-приложение.\n"
            "👉 Нажми «➕ Добавить» или введи /add <имя_сессии>",
            reply_markup=back_menu
        )

    # Список сессий
    if data == "list":
        sessions = user_sessions.get(user_id, {})
        keyboard = [
            [InlineKeyboardButton(f"{name} ({'🟢' if sess['active'] else '🔴'})",
                                  callback_data=f"session:{name}")]
            for name, sess in sessions.items()
        ]
        keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="add")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="main")])
        return await query.edit_message_text(
            "🤖 Ваши сессии:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Добавить новую сессию
    if data == "add":
        webapp_url = f"{BASE_URL}?tgid={user_id}&new=1"
        kb = [
            [InlineKeyboardButton(
                "🔑 Авторизоваться и добавить",
                web_app=WebAppInfo(url=webapp_url)
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="main")]
        ]
        return await query.edit_message_text(
            "👇 Нажми кнопку ниже, чтобы авторизоваться и сразу создать сессию под твоим ником:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    if data.startswith("stats:"):
        name = data.split(":", 1)[1]
        text = await fetch_stats_for_session(user_id, name)
        return await query.edit_message_text(
            text,
            reply_markup=back_list
        )

    # Выбрали конкретную сессию → показываем меню управления
    if data.startswith("session:"):
        name = data.split(":", 1)[1]
        sess = user_sessions.get(user_id, {}).get(name)
        if not sess:
            return await query.edit_message_text("Сессия не найдена.")
        kb = []
        if sess["active"]:
            kb.append([InlineKeyboardButton("⏹️ Выключить", callback_data=f"off:{name}")])
        else:
            kb.append([InlineKeyboardButton("▶️ Включить", callback_data=f"on:{name}")])
        kb.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"del:{name}")])
        kb.append([InlineKeyboardButton("📊 Статистика", callback_data=f"stats:{name}")])
        #launch_url = f"{BASE_URL}launch/{user_id}/{name}"
        #kb.append([InlineKeyboardButton(
        #    "🔑 Открыть аккаунт",
        #    web_app=WebAppInfo(url=launch_url)
        #)])
        kb.append([InlineKeyboardButton("◀️ Назад", callback_data="list")])
        return await query.edit_message_text(
            f"⚙️ Управление сессией «{name}»:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # Включить сессию
    if data.startswith("on:"):
        name = data.split(":", 1)[1]
        session = user_sessions[user_id][name]
        if not session["active"]:
            session["active"] = True
            task = asyncio.create_task(auto_actions(session["cookies"], name))
            user_tasks[(user_id, name)] = task
        return await query.edit_message_text(
            f"✅ Сессия «{name}» включена!",
            reply_markup=back_list
        )

    # Выключить сессию
    if data.startswith("off:"):
        name = data.split(":", 1)[1]
        session = user_sessions[user_id][name]
        if session["active"]:
            session["active"] = False
            task = user_tasks.pop((user_id, name), None)
            if task:
                task.cancel()
        return await query.edit_message_text(
            f"⏹️ Сессия «{name}» выключена!",
            reply_markup=back_list
        )

    # Удалить сессию
    if data.startswith("del:"):
        name = data.split(":", 1)[1]
        ok = remove_session_data(user_id, name)
        return await query.edit_message_text(
            f"🗑️ Сессия «{name}» удалена!" if ok else f"Сессия «{name}» не найдена.",
            reply_markup=back_list
        )

    # Показать статистику
    if data.startswith("del:"):
        name = data.split(":", 1)[1]
        remove_session_data(user_id, name)
        return await query.edit_message_text(
            f"🗑️ Сессия «{name}» удалена!",
            reply_markup=back_list
        )

    # На всякий случай
    return await query.edit_message_text(
        "❌ Неизвестная команда.",
        reply_markup=back_menu
    )


# Flask маршрут: корневой – перенаправление на страницу авторизации MPets
@app.route('/')
def webapp_root():
    print("QUERY:", request.args)
    tgid = request.args.get("tgid")
    is_new = request.args.get("new") == '1'
    session_name = request.args.get("name")
    print("SESSION_NAME:", session_name)
    if not tgid:
        return "Ошибка: нет tgid.", 400
    flask_session['tgid'] = int(tgid)
    flask_session['is_new'] = is_new
    flask_session['session_name'] = session_name
    print("FLASK_SESSION after set:", flask_session)
    return redirect("/profile")


# Заменённый фрагмент во Flask:

@app.route('/', defaults={'url_path': ''}, methods=['GET', 'POST'])
@app.route('/<path:url_path>', methods=['GET', 'POST'])
def proxy_mpets(url_path):
    qs = request.query_string.decode('utf-8')
    target_url = f"https://mpets.mobi/{url_path}"
    if qs:
        target_url += f"?{qs}"

    # Перехватываем логин и PHPSESSID при POST на /login
    try:
        headers = {k: v for k, v in request.headers if k.lower() != 'host'}
        if request.method == 'POST':
            if url_path.lower() == 'login':
                print("DEBUG request.form:", dict(request.form))
                login_field = request.form.get('name')  # <- только name, ничего больше не надо!
                print(f"[DEBUG] Поймал логин (name): {login_field}")
                LAST_LOGIN = login_field  # просто сохраняем на глобалку
            resp = requests.post(target_url,
                                 data=request.form,
                                 headers=headers,
                                 cookies=request.cookies,
                                 allow_redirects=False)
        else:
            resp = requests.get(target_url,
                                headers=headers,
                                cookies=request.cookies,
                                allow_redirects=False)
    except Exception as e:
        logging.error(f"Proxy error to {target_url}: {e}")
        return "Ошибка соединения с MPets.", 502


    tgid = flask_session.get('tgid')
    session_name = flask_session.get('session_name')
    loop = asyncio.get_event_loop()

    # Обработка авторизации (редирект после логина)
    if url_path.lower() == 'login' and resp.status_code in (301, 302):
        location = resp.headers.get('Location', '')

        # Если ошибка — твой старый блок, ничего не трогай
        if 'error=' in location:
            err = request.args.get('error', 'unknown')
            nick = request.args.get('prev_name', '')
            if tgid:
                loop.create_task(bot.send_message(
                    chat_id=tgid,
                    text=(f"❌ Ошибка авторизации для «{session_name}»!\n"
                          f"Код ошибки: {err}\n"
                          f"Никнейм: {nick}\n"
                          f"Попробуй снова через «➕ Добавить»")
                ))
            return redirect(location)

        # УСПЕШНЫЙ логин
        if tgid:
            cookies_dict = resp.cookies.get_dict()
            php = request.cookies.get('PHPSESSID')
            if php:
                cookies_dict['PHPSESSID'] = php

            # --- Вот тут юзаем последний логин как имя сессии ---
            session_key = LAST_LOGIN or f"session_{uuid.uuid4().hex[:6]}"
            print(f"[DEBUG] Сохраняю сессию с именем: {session_key}")
            user_sessions.setdefault(tgid, {})[session_key] = {
                "owner": "",
                "cookies": cookies_dict,
                "active": False
            }
            write_to_file(session_key, "", tgid, cookies_dict)
            logging.info(f"Сессия '{session_key}' сохранена для user_id={tgid}")
            LAST_LOGIN = None  # очищаем глобалку!

            loop.create_task(bot.send_message(
                chat_id=tgid,
                text=f"✅ Сессия «{session_key}» успешно создана и сохранена!"
            ))
            loop.create_task(bot.send_message(
                chat_id=tgid,
                text="Что будем делать дальше?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Включить", callback_data=f"on:{session_key}")],
                    [InlineKeyboardButton("📊 Статистика", callback_data=f"stats:{session_key}")],
                    [InlineKeyboardButton("◀️ Назад к списку", callback_data="list")]
                ])
            ))

        html_success = """
<html xmlns="http://www.w3.org/1999/xhtml"><head>
                    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
                    <meta name="viewport" content="width=device-width, minimum-scale=1, maximum-scale=1">
                    <link rel="icon" href="/view/image/avatar_icon.png" type="image/png">
                    <link rel="stylesheet" type="text/css" href="/view/style/style.css?2.5699">
                    <title>Удивительные питомцы</title>
                </head>
                <body>
                    <div class="main">
                        <div class="ovh" style="padding-top: 1px;"></div>
                        <div class="content">
                            <div class="start">
                                <div class="msg mrg_msg1 mt10 c_brown4">
                                    <div class="wr_bg">
                                        <div class="wr_c1"><div class="wr_c2"><div class="wr_c3"><div class="wr_c4 font_14">
                                            <span class="wbmenu">
                                                <a href="https://t.me/cobaltMPETS_bot">
                                                    <img src="/view/image/avatar5.png" height="150" alt="Успех!">
                                                </a>
                                            </span>
                                            <br>
                                            <div class="mb10">
                                                ✅ Авторизация успешна! Теперь вы можете закрыть это окно и вернуться в бота. Сессия уже сохранена автоматически.
                                            </div>
                                        </div></div></div></div></div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </body>
                </html>
"""
        response = Response(html_success, mimetype="text/html")
        response.set_cookie("PHPSESSID", "", expires=0)
        return response

    # Ошибка welcome
    if url_path.lower() == 'welcome' and 'error' in request.args:
        err = request.args.get('error')
        nick = request.args.get('prev_name', '')
        if tgid and session_name:
            loop.create_task(bot.send_message(
                chat_id=tgid,
                text=(f"❌ Ошибка авторизации для «{session_name}»!\n"
                      f"Код ошибки: {err}\n"
                      f"Никнейм: {nick}\n"
                      f"Попробуй снова через «➕ Добавить»")
            ))
        excluded = ['content-encoding','transfer-encoding','content-length','connection']
        resp_obj = Response(resp.content, status=resp.status_code)
        for h, v in resp.headers.items():
            if h.lower() not in excluded:
                resp_obj.headers[h] = v
        return resp_obj

    # Всё остальное — просто проксируем
    excluded = ['content-encoding','transfer-encoding','content-length','connection']
    resp_obj = Response(resp.content, status=resp.status_code)
    for h, v in resp.headers.items():
        if h.lower() not in excluded:
            resp_obj.headers[h] = v
    return resp_obj


# Основная функция запуска Telegram-бота
async def main_bot():
    app_tg = Application.builder().token(TOKEN).build()
    load_sessions()
    # Регистрация обработчиков команд
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CommandHandler("info", info))
    app_tg.add_handler(CommandHandler("guide", guide))
    app_tg.add_handler(CommandHandler("add", add_session))
    app_tg.add_handler(CommandHandler("confirm", confirm_session))
    app_tg.add_handler(CommandHandler("del", remove_session))
    app_tg.add_handler(CommandHandler("list", list_sessions))
    app_tg.add_handler(CommandHandler("on", activate_session))
    app_tg.add_handler(CommandHandler("off", deactivate_session))
    app_tg.add_handler(CommandHandler("stats", stats))
    app_tg.add_handler(CommandHandler("get_user", get_user))
    app_tg.add_handler(CallbackQueryHandler(button_handler))
    # Специальные команды для разрешённых пользователей (если нужны)
    app_tg.add_handler(CommandHandler("aon", activate_session))   # возможно, объединяется с /on
    app_tg.add_handler(CommandHandler("aoff", deactivate_session))  # возможно, объединяется с /off
    # Запуск бота (довольно продолжительный, пока бот не остановлен)
    await app_tg.run_polling()

# Запуск Flask и Telegram бота в одном процессе
if __name__ == "__main__":
    # Запускаем веб-сервер Flask в отдельном потоке
    port = int(os.environ.get('PORT', 5000))
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    # Запускаем Telegram-бота в основном потоке
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main_bot())
