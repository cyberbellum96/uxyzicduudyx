# Стандартні бібліотеки Python
import json
import logging
import os
from datetime import datetime, timedelta
from threading import Thread
from typing import Dict, Any
from collections import defaultdict

# Сторонні бібліотеки
import nest_asyncio
import asyncio
from flask import Flask
from pytz import timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

# Telegram-специфічні імпорти
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    constants,
    Bot,
)
from telegram.ext import (
    ApplicationHandlerStop,
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# Налаштування та константи
BLACKLIST_FILE = "blacklist.json"

# Ініціалізація
nest_asyncio.apply()


def load_config(file_path):
    # Дозволений каталог
    allowed_directory = os.path.realpath("config")
    # Абсолютний шлях до файлу
    absolute_file_path = os.path.realpath(file_path)

    # Перевірка розширення файлу
    if not file_path.endswith(".json"):
        raise ValueError("Дозволено лише файли з розширенням .json.")

    # Перевіряємо, чи знаходиться файл у дозволеному каталозі
    if not absolute_file_path.startswith(allowed_directory):
        raise ValueError("Доступ до файлу за межами дозволеного каталогу заборонено.")

    # Якщо все добре, відкриваємо файл
    with open(absolute_file_path, "r") as file:
        return json.load(file)


# Завантаження конфігурації
config = load_config("config.json")


def check_time():
    kyiv_tz = timezone("Europe/Kyiv")
    current_time = datetime.now(kyiv_tz)
    logging.info("Поточний час в Україні: %s", current_time)


# Налаштування логування
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),  # Вказуємо файл для збереження логів
        logging.StreamHandler(),  # Додатково виводимо в консоль
    ],
)

# Пробний запис у лог
logging.info("Бот запущено!")

# Стани для ConversationHandler //Тут
CHOOSING_ACTION, AWAITING_REPORT, AWAITING_PHOTOS = range(3)


app = Flask("__name__")

# Додаємо словник для відстеження щоденної статистики
daily_stats = {
    "buying": 0,  # Купівля
    "selling": 0,  # Продаж
    "announcement": 0,  # Оголошення
    "advertising": 0,  # Реклама
}

# Використовуємо конфігураційні параметри
TOKEN = config["TOKEN"]
ADMIN_IDS = config["ADMIN_IDS"]
MODERATOR_IDS = config["MODERATOR_IDS"]
PAYMENT_CARD = config["PAYMENT_CARD"]
RULES_LINK = config["RULES_LINK"]


@app.route("/")
def home():
    return "Бот працює!"


def run_flask():
  app.run(host="127.0.0.1", port=1488)


# //Тут


# Лічильники користувачів
user_post_counts = defaultdict(lambda: {"count": 0, "reset_time": datetime.now()})
user_report_counts = defaultdict(lambda: {"count": 0, "reset_time": datetime.now()})


# Використовуємо посилання на форми
BASE_URL = config["BASE_URL"]

FORMS_URL = {
    "Купівля": f"{BASE_URL}?type=buying",
    "Продаж": f"{BASE_URL}?type=selling",
    "Оголошення": f"{BASE_URL}?type=announcement",
    "Реклама": f"{BASE_URL}?type=advertising",
}


def get_main_keyboard():
    buttons = [
        [
            KeyboardButton(
                text="Купівля", web_app=WebAppInfo(url=FORMS_URL["Купівля"])
            ),
            KeyboardButton(text="Продаж", web_app=WebAppInfo(url=FORMS_URL["Продаж"])),
        ],
        [
            KeyboardButton(
                text="Оголошення", web_app=WebAppInfo(url=FORMS_URL["Оголошення"])
            ),
            KeyboardButton(
                text="Реклама", web_app=WebAppInfo(url=FORMS_URL["Реклама"])
            ),
        ],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def handle_scheduler_error(event):
    logging.error("Помилка планувальника: %s", event.exception)


def check_and_update_limits(user_id, limit_type="post"):
    current_time = datetime.now()
    if limit_type == "post":
        user_data = user_post_counts[user_id]
        max_limit = 5
    else:
        user_data = user_report_counts[user_id]
        max_limit = 10

    if current_time - user_data["reset_time"] > timedelta(days=1):
        user_data["count"] = 0
        user_data["reset_time"] = current_time

    if user_data["count"] >= max_limit:
        return False

    user_data["count"] += 1
    return True


class BlacklistEntry:
    def __init__(self, user_id: int, end_date: datetime, reason: str):
        self.user_id = user_id
        self.end_date = end_date
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "end_date": self.end_date.isoformat(),
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "BlacklistEntry":
        return BlacklistEntry(
            user_id=data["user_id"],
            end_date=datetime.fromisoformat(data["end_date"]),
            reason=data["reason"],
        )


def load_blacklist() -> Dict[int, BlacklistEntry]:
    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return {
                int(user_id): BlacklistEntry.from_dict(entry_data)
                for user_id, entry_data in data.items()
            }
    except FileNotFoundError:
        return {}


def save_blacklist(blacklist: Dict[int, BlacklistEntry]):
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as file:
        json.dump(
            {str(user_id): entry.to_dict() for user_id, entry in blacklist.items()},
            file,
            indent=2,
            ensure_ascii=False,
        )


BLACKLIST: Dict[int, BlacklistEntry] = load_blacklist()


async def notify_user(bot: Bot, user_id: int, text: str):
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.error(
            "Помилка при відправці повідомлення користувачу %d: %s", user_id, e
        )


async def blacklist_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id in BLACKLIST:
        entry = BLACKLIST[user_id]
        if entry.end_date > datetime.now():
            remaining_time = entry.end_date - datetime.now()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            await update.message.reply_text(
                f"❌ Ви не можете використовувати цього бота.\n"
                f"⏳ Термін блокування: {days}д {hours}г\n"
                f"📝 Причина: {entry.reason}",
            )
            raise ApplicationHandlerStop()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Очищаємо всі дані стану
    context.user_data.clear()

    await update.message.reply_text(
        "Оберіть категорію:", reply_markup=get_main_keyboard()
    )
    return CHOOSING_ACTION


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Очищаємо всі попередні дані
    context.user_data.clear()

    keyboard = ReplyKeyboardMarkup([["Надіслати", "Повернутися"]], resize_keyboard=True)
    await update.message.reply_text(
        "Сформулюйте своє звернення одним повідомленням, "
        "та адміністратор зв'яжеться з вами найближчим часом.",
        reply_markup=keyboard,
    )
    return AWAITING_REPORT


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text

    if message_text == "Повернутися":
        context.user_data.clear()
        return await start(update, context)

    if message_text == "Надіслати":
        if not context.user_data.get("report_message"):
            await update.message.reply_text(
                "Будь ласка, спочатку напишіть ваше звернення."
            )
            return AWAITING_REPORT

        if not check_and_update_limits(update.effective_user.id, "report"):
            await update.message.reply_text(
                "На сьогодні ліміт запитів адміністраторам вичерпано, "
                "зверніться будь ласка завтра.",
                reply_markup=get_main_keyboard(),
            )
            context.user_data.clear()
            return CHOOSING_ACTION

        report_message = context.user_data["report_message"]
        user = update.effective_user
        admin_message = f"""
Нове звернення!
ID: {user.id}
Ім'я: {user.first_name} {user.last_name if user.last_name else ''}
Username: @{user.username if user.username else 'немає'}
Звернення: {report_message}
"""
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_message)
            except Exception as e:
                logging.error(
                    "Помилка відправки повідомлення адміну %d: %s", admin_id, e
                )

        await update.message.reply_text(
            "Дякуємо за Ваше звернення, адміністратори вже опрацьовують його",
            reply_markup=get_main_keyboard(),
        )
        context.user_data.clear()
        return CHOOSING_ACTION
    else:
        context.user_data["report_message"] = message_text
        await update.message.reply_text(
            "Повідомлення збережено. Натисніть 'Надіслати' для відправки або 'Повернутися' для скасування."
        )
        return AWAITING_REPORT


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data.clear()
        user = update.effective_user

        if not check_and_update_limits(user.id, "post"):
            await update.message.reply_text(
                "Ви досягли ліміту оголошень на сьогодні. "
                "Для розміщення додаткових оголошень зверніться до адміністрації через команду /report",
                reply_markup=get_main_keyboard(),
            )
            return CHOOSING_ACTION

        data = json.loads(update.effective_message.web_app_data.data)
        form_type = data.get("formType", "невідомий тип")

        # Оновлюємо статистику
        if form_type in daily_stats:
            daily_stats[form_type] += 1
            logging.info("Додано новий запит типу %s", form_type)

        context.user_data["form_data"] = data
        context.user_data["form_type"] = form_type
        context.user_data["user"] = user
        context.user_data["formatted_message"] = format_message(data, form_type, user)

        await update.message.reply_text(
            "Тепер ви можете додати фотографії до вашого оголошення (максимум 10 фото).\n"
            "Надішліть фотографії одну за одною.\n"
            "Коли закінчите, натисніть 'Завершити' ⬇️",
            reply_markup=ReplyKeyboardMarkup([["Завершити"]], resize_keyboard=True),
        )

        return AWAITING_PHOTOS

    except Exception as e:
        logging.error("Помилка обробки даних форми: %s", e)
        context.user_data.clear()
        await update.message.reply_text(
            "Вибачте, сталася помилка при обробці форми. Спробуйте ще раз пізніше.",
            reply_markup=get_main_keyboard(),
        )
        return CHOOSING_ACTION


def can_add_photo(user_data):
    """Перевіряє, чи можна додати ще одну фотографію."""
    if "photos" not in user_data:
        user_data["photos"] = []
    return len(user_data["photos"]) < 10


async def add_photo(user_data, photo_file_id):
    """Додає фото до списку, якщо це можливо."""
    if can_add_photo(user_data):
        user_data["photos"].append(photo_file_id)
        return True
    return False


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "photos" not in context.user_data:
        context.user_data["photos"] = []

    if update.message.text == "Завершити":
        return await finish_with_photos(update, context)

    if not can_add_photo(context.user_data):
        await update.message.reply_text(
            "Ви вже додали максимальну кількість фотографій (10).\n"
            "Натисніть 'Завершити', щоб опублікувати оголошення."
        )
        return AWAITING_PHOTOS

    try:
        photo_file_id = update.message.photo[-1].file_id
        if await add_photo(context.user_data, photo_file_id):
            await update.message.reply_text(
                f"Фото додано! ({len(context.user_data['photos'])}/10)\n"
                "Ви можете додати ще фото або натиснути 'Завершити'."
            )
        else:
            await update.message.reply_text(
                "Не вдалося додати фото. Перевірте кількість фото."
            )
    except Exception as e:
        logging.error("Помилка додавання фото: %s", e)
        await update.message.reply_text(
            "Сталася помилка при додаванні фото. Спробуйте ще раз."
        )

    return AWAITING_PHOTOS


async def finish_with_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.user_data.get("formatted_message"):
            await update.message.reply_text(
                "Виникла помилка. Будь ласка, почніть спочатку з команди /start",
                reply_markup=get_main_keyboard(),
            )
            context.user_data.clear()
            return CHOOSING_ACTION

        formatted_message = context.user_data["formatted_message"]
        photos = context.user_data.get("photos", [])
        user = context.user_data["user"]

        for moderator_id in MODERATOR_IDS:
            try:
                await context.bot.send_message(
                    chat_id=moderator_id, text=formatted_message, parse_mode="HTML"
                )

                for photo_id in photos:
                    caption = f"Фото від: {user.first_name} {user.last_name if user.last_name else ''} (@{user.username if user.username else 'немає'})"
                    await context.bot.send_photo(
                        chat_id=moderator_id, photo=photo_id, caption=caption
                    )
            except Exception as e:
                logging.error(
                    "Помилка надсилання повідомлення модератору %d: %s", moderator_id, e
                )

        await update.message.reply_text(
            "Дякуємо! Ваше оголошення прийнято та буде опубліковано після модерації.",
            reply_markup=get_main_keyboard(),
        )

        context.user_data.clear()
        return CHOOSING_ACTION

    except Exception as e:
        logging.error("Помилка при завершенні обробки форми: %s", e)
        context.user_data.clear()
        await update.message.reply_text(
            "Вибачте, сталася помилка при обробці даних. Спробуйте ще раз пізніше.",
            reply_markup=get_main_keyboard(),
        )
        return CHOOSING_ACTION


# Шаблони повідомлень в окремих константах
MESSAGE_TEMPLATES = {
    "user_info": {
        "default": """👤 Користувач: {first_name} {last_name}
🔗 Username: @{username}
🆔 User ID: {user_id}""",
        "with_paid": """👤 Користувач: {first_name} {last_name}
🔗 Username: @{username}
🆔 User ID: {user_id}
💳 Paid: {is_pinned}""",
        "advertising": """👤 Користувач: {first_name} {last_name}
🔗 Username: @{username}
🆔 User ID: {user_id}
📝 Type: {ad_type}
💰 Price: {price} грн
⏱ Period: {duration}""",
    },
    "content": {
        "advertising": """<pre>📢 Реклама

🏷️ Назва: {company_name}
📝 Опис: {description}
📞 Контакти: {contact}

✏️️ Подати оголошення:
@slavuta_ads_bot</pre>
""",
        "buying": """<pre>🛒 Купівля

🏷️ Назва: {product_name}
📝 Опис: {description}
💰 Вартість до: {max_price} {currency}
📞 Контакти: {contact}

🔍Схожі запити:
#{category}

✏️ Подати оголошення:
@slavuta_ads_bot</pre>
""",
        "selling": """<pre>🛒 Продаж

🏷️ Назва: {item_name}
💰 Вартість: {price_info} {negotiable}
📦 Стан: {condition}
📝 Опис: {description}
📞 Контакти: {contact}

🔍 Схожі товари:
#{category}

✏️ Подати оголошення:
@slavuta_ads_bot</pre>
""",
        "announcement": """<pre>📰 Оголошення

📝 Опис: {description}
📞 Контакти: {contact}

🔍 Схожі оголошення:
#{category}

✏️ Подати оголошення:
@slavuta_ads_bot</pre>
""",
    },
}


def get_price_info(data):
    """Допоміжна функція для формування інформації про ціну"""
    if data.get("isFree"):
        return "Віддам даром"
    if data.get("priceType") == "negotiablePrice":
        return "Договірна"
    return f"{data.get('price', 'Не вказано')} {data.get('priceCurrency', 'грн')}"


def format_user_info(user, form_type, data):
    """Форматує інформацію про користувача"""
    user_data = {
        "first_name": user.first_name,
        "last_name": user.last_name if user.last_name else "",
        "username": user.username if user.username else "немає",
        "user_id": user.id,
    }

    if form_type == "advertising":
        return MESSAGE_TEMPLATES["user_info"]["advertising"].format(
            **user_data,
            ad_type=data.get("adType", "Не вказано"),
            price=data.get("finalPrice", "0"),
            duration=data.get("duration", "Не вказано"),
        )
    elif form_type in ["buying", "selling", "announcement"]:
        return MESSAGE_TEMPLATES["user_info"]["with_paid"].format(
            **user_data, is_pinned="Так" if data.get("isPinned") else "Ні"
        )
    else:
        return MESSAGE_TEMPLATES["user_info"]["default"].format(**user_data)


def format_message(data, form_type, user):
    """Форматує повне повідомлення залежно від типу форми"""
    user_info = format_user_info(user, form_type, data)

    if form_type == "advertising":
        content = MESSAGE_TEMPLATES["content"]["advertising"].format(
            company_name=data.get("companyName", "Не вказано"),
            description=data.get("adDescription", "Не вказано"),
            contact=data.get("adContact", "Не вказано"),
        )
    elif form_type == "buying":
        content = MESSAGE_TEMPLATES["content"]["buying"].format(
            product_name=data.get("productName", "Не вказано"),
            description=data.get("buyerDescription", "Не вказано"),
            max_price=data.get("maxPrice", "Не вказано"),
            currency=data.get("maxPriceCurrency", "грн"),
            contact=data.get("buyerContact", "Не вказано"),
            category=data.get("buyingCategory", "").lower(),
        )
    elif form_type == "selling":
        content = MESSAGE_TEMPLATES["content"]["selling"].format(
            item_name=data.get("itemName", "Не вказано"),
            price_info=get_price_info(data),
            negotiable="(Торг)" if data.get("isNegotiable") else "",
            condition=data.get("condition", "Не вказано"),
            description=data.get("sellerDescription", "Не вказано"),
            contact=data.get("sellerContact", "Не вказано"),
            category=data.get("sellingCategory", "").lower(),
        )
    else:  # announcement
        content = MESSAGE_TEMPLATES["content"]["announcement"].format(
            description=data.get("description", "Не вказано"),
            contact=data.get("contact", "Не вказано"),
            category=data.get("category", "").lower(),
        )

    return f"{user_info}\n{content}"


def check_bot_status():
    logging.info("Бот працює. Перевірка статусу.")


# Список команд адміністратора


async def adm_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду списку команд адміністратора."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /adm_help, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    response = (
        "📉 /reset_counters <user_id> - Скинути лічильник оголошень та звернень\n"
        "📊 /view_counters <user_id> - Переглянути лічильник користувача\n"
        "👥 /list_users - Переглянути всіх користувачів з лічильником\n"
        "📈 /check_stats - Переглянути статистику оголошень\n"
        "🗨️ /ans <user_id> <text> - Відповісти користувачу\n"
        "🚫 /ban <user_id> <days> <reason> - Заблокувати користувача\n"
        "🔓 /unban <user_id> - Розблокувати користувача\n"
        "🗑️ /blacklist - Переглянути чорний список"
    )

    await update.message.reply_text(response)
    logging.info("Адміністратор %d переглянув список команд.", update.effective_user.id)


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для додавання користувача до чорного списку з вказанням терміну та причини."""
    if not update.effective_user:
        return

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /ban, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    # Перевіряємо наявність аргументів
    if not context.args:
        await update.message.reply_text(
            "❗Використовуйте: /ban <user_id> <days> <reason>"
        )
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "❗Використовуйте: /ban <user_id> <days> <reason>"
        )
        return

    try:
        user_id = int(context.args[0])
        days = int(context.args[1])
        reason = " ".join(context.args[2:])

        if days <= 0:
            await update.message.reply_text("❗Кількість днів має бути більше 0.")
            return

        end_date = datetime.now() + timedelta(days=days)

        BLACKLIST[user_id] = BlacklistEntry(user_id, end_date, reason)
        save_blacklist(BLACKLIST)

        ban_message = (
            f"⛔️ Адміністратор заблокував доступ до бота на {days} днів.\n"
            f"📝 Причина: {reason}"
        )

        try:
            # Повідомлення забаненому користувачу
            await notify_user(context.bot, user_id, ban_message)
        except Exception as e:
            logging.error(
                "Помилка при відправці повідомлення користувачу %d: %s", user_id, str(e)
            )
            await update.message.reply_text(
                "⚠️ Користувача додано до чорного списку, але не вдалося надіслати йому повідомлення."
            )

        # Повідомлення адміністратору
        await update.message.reply_text(
            f"✅ Користувач {user_id} доданий до чорного списку\n"
            f"⏳ До: {end_date.strftime('%d.%m.%Y %H:%M')}\n"
            f"📝 Причина: {reason}"
        )

        logging.info(
            "Адміністратор %d забанив користувача %d на %d днів. Причина: %s",
            update.effective_user.id,
            user_id,
            days,
            reason,
        )

    except ValueError as e:
        await update.message.reply_text(
            "❗Будь ласка, введіть коректні дані:\n"
            "- user_id має бути числом\n"
            "- кількість днів має бути цілим числом"
        )
        logging.error("Помилка при обробці команди ban: %s", str(e))
    except Exception as e:
        await update.message.reply_text(
            "❗Сталася непередбачена помилка. Спробуйте ще раз."
        )
        logging.error("Непередбачена помилка в команді ban: %s", str(e))


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для видалення користувача з чорного списку."""
    if not update.effective_user:
        return

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /unban, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    if len(context.args) != 1:
        await update.message.reply_text("❗Використовуйте: /unban <user_id>")
        return

    try:
        user_id = int(context.args[0])
        if user_id in BLACKLIST:
            # Зберігаємо причину бану для логу
            ban_reason = BLACKLIST[user_id].reason

            # Видаляємо з чорного списку
            del BLACKLIST[user_id]
            save_blacklist(BLACKLIST)

            # Відправляємо повідомлення користувачу про розблокування
            unban_message = (
                "✅ Ваші обмеження були зняті адміністратором, можете далі користуватися ботом.\n"
                "⚠️ Будь ласка, не порушуйте правила користування ботом.\n"
                f"📋 Ознайомитись з правилами можна тут: {RULES_LINK}"
            )
            await notify_user(context.bot, user_id, unban_message)

            # Повідомлення адміністратору
            await update.message.reply_text(
                f"✅ Користувач {user_id} видалений з чорного списку.",
            )

            logging.info(
                "Адміністратор %d розблокував користувача %d (був заблокований за: %s)",
                update.effective_user.id,
                user_id,
                ban_reason,
            )
        else:
            await update.message.reply_text(
                f"❗Користувач {user_id} не знаходиться в чорному списку.",
            )
    except ValueError:
        await update.message.reply_text(
            "❗Будь ласка, введіть коректний user_id (число)."
        )


async def check_bans(bot: Bot):
    """Перевірка банів кожну годину."""
    try:
        current_time = datetime.now()
        users_to_unban = [
            user_id
            for user_id, entry in BLACKLIST.items()
            if entry.end_date <= current_time
        ]

        for user_id in users_to_unban:
            unban_message = (
                "✅ Ваші обмеження зняті, можете далі користуватися ботом.\n"
                "⚠️ Будь ласка, не порушуйте правила користування ботом.\n"
                f"📋 Ознайомитись з правилами можна тут: {RULES_LINK}"
            )

            await notify_user(bot, user_id, unban_message)

            del BLACKLIST[user_id]
            save_blacklist(BLACKLIST)

            logging.info("Користувача %d було автоматично розблоковано.", user_id)

    except Exception as e:
        logging.error("Помилка при перевірці банів: %s", e)


async def view_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду чорного списку."""
    if not update.effective_user:
        return

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /blacklist, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    if not BLACKLIST:
        await update.message.reply_text("📋 Чорний список порожній.")
    else:
        blacklist_text = "📋 Чорний список користувачів:\n\n"
        for user_id, entry in BLACKLIST.items():
            remaining_time = entry.end_date - datetime.now()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            blacklist_text += (
                f"🆔 ID: {user_id}\n"
                f"⏳ Закінчення бану: {entry.end_date.strftime('%d.%m.%Y %H:%M')}\n"
                f"⌛️ Залишилось: {days}д {hours}г\n"
                f"📝 Причина: {entry.reason}\n\n"
            )
        await update.message.reply_text(blacklist_text)


async def answer_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для відповіді на репорт користувача."""
    if update.effective_user.id not in ADMIN_IDS:
        # Перевіряємо, чи користувач є адміністратором
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /ans, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗Використовуйте: /ans <user_id> <text>")
        return

    try:
        user_id = int(args[0])
        answer_text = " ".join(args[1:])
    except ValueError:
        await update.message.reply_text(
            "❗Будь ласка, введіть коректний user_id та текст відповіді."
        )
        logging.warning(
            "Адміністратор %d ввів некоректний user_id: %s",
            update.effective_user.id,
            args[0],
        )
        return

    try:
        # Надсилаємо відповідь користувачу
        await context.bot.send_message(
            chat_id=user_id, text=f"📬 Відповідь від адміністрації:\n\n{answer_text}"
        )

        # Підтверджуємо адміністратору, що відповідь надіслано
        await update.message.reply_text(
            f"✅ Відповідь успішно надіслано користувачу {user_id}."
        )

        logging.info(
            "Адміністратор %d надіслав відповідь користувачу %d (%s).",
            update.effective_user.id,
            user_id,
            answer_text,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка при надсиланні відповіді: {str(e)}")
        logging.error(
            "Помилка при надсиланні відповіді від адміністратора %d користувачу %d: %s",
            update.effective_user.id,
            user_id,
            str(e),
        )


async def check_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду поточної статистики за день."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /check_stats, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    total_requests = sum(daily_stats.values())

    stats_message = (
        f"📊 Статистика за {datetime.now():%d/%m/%Y}:\n\n"
        f"📢 Оголошення: {daily_stats['announcement']}\n"
        f"🎯 Реклама: {daily_stats['advertising']}\n"
        f"💰 Продажі: {daily_stats['selling']}\n"
        f"🛒 Купівля: {daily_stats['buying']}\n"
        f"📈 Всього запитів: {total_requests}"
    )

    await update.message.reply_text(stats_message)
    logging.info("Адміністратор %d переглянув статистику.", update.effective_user.id)


async def send_daily_stats(application):
    logging.info("Початок відправки щоденної статистики.")

    total_requests = sum(daily_stats.values())

    stats_message = (
        f"📊 Статистика за {datetime.now():%d/%m/%Y}:\n\n"
        f"📢 Оголошення: {daily_stats['announcement']}\n"
        f"🎯 Реклама: {daily_stats['advertising']}\n"
        f"💰 Продажі: {daily_stats['selling']}\n"
        f"🛒 Купівля: {daily_stats['buying']}\n"
        f"📈 Всього запитів: {total_requests}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(chat_id=admin_id, text=stats_message)
            logging.info("Статистику відправлено адміну %d", admin_id)
        except Exception as e:
            logging.error("Помилка відправки статистики адміну %d: %s", admin_id, e)

    logging.info("Завершення відправки щоденної статистики.")


def reset_daily_stats():
    """Скидання щоденної статистики."""
    daily_stats.update({key: 0 for key in daily_stats})
    logging.info("Щоденна статистика скинута.")


async def reset_counters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для скидання лічильників оголошень та репортів для вказаного користувача."""
    # Перевіряємо, чи користувач є адміністратором
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /reset_counters, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❗Використовуйте: /reset_counters <user_id>")
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "❗Будь ласка, введіть коректний user_id (число)."
        )
        logging.warning(
            "Адміністратор %d ввів некоректний user_id: %s",
            update.effective_user.id,
            args[0],
        )
        return

    if user_id not in user_post_counts and user_id not in user_report_counts:
        await update.message.reply_text("❗Цей користувач не має лічильників.")
        logging.info(
            "Адміністратор %d спробував скинути лічильники для користувача %d, але лічильники відсутні.",
            update.effective_user.id,
            user_id,
        )
        return

    user_post_counts.pop(user_id, None)
    user_report_counts.pop(user_id, None)

    await update.message.reply_text(
        f"🔄 Лічильники для користувача з ID {user_id} були скинуті."
    )
    logging.info(
        "Користувач %d скинув лічильники для користувача %d.",
        update.effective_user.id,
        user_id,
    )


async def view_counters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду лічильників оголошень та репортів для вказаного користувача."""
    # Перевіряємо, чи користувач є адміністратором
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /view_counters, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❗Використовуйте: /view_counters <user_id>")
        return

    try:
        user_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "❗Будь ласка, введіть коректний user_id (число)."
        )
        logging.warning(
            "Адміністратор %d ввів некоректний user_id: %s",
            update.effective_user.id,
            args[0],
        )
        return

    post_data = user_post_counts.get(
        user_id, {"count": 0, "reset_time": datetime.now()}
    )
    report_data = user_report_counts.get(
        user_id, {"count": 0, "reset_time": datetime.now()}
    )

    response = (
        f"📊 Лічильники для користувача з ID {user_id}:\n"
        f"📢 Оголошення: {post_data['count']}/{5} (скидається о {post_data['reset_time'] + timedelta(days=1):%Y-%m-%d %H:%M:%S})\n"
        f"📝 Репорти: {report_data['count']}/{10} (скидається о {report_data['reset_time'] + timedelta(days=1):%Y-%m-%d %H:%M:%S})"
    )

    await update.message.reply_text(response)
    logging.info(
        "Користувач %d переглянув лічильники для користувача %d.",
        update.effective_user.id,
        user_id,
    )


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду списку користувачів з активними лічильниками."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише адміністраторам.")
        logging.warning(
            "Користувач %d намагався використати /list_users, але не є адміністратором.",
            update.effective_user.id,
        )
        return

    users_with_posts = [str(user_id) for user_id in user_post_counts.keys()]
    users_with_reports = [str(user_id) for user_id in user_report_counts.keys()]

    response = "📊 Користувачі з активними лічильниками:\n\n"
    response += (
        f"📢 Оголошення ({len(users_with_posts)} користувачів):\n"
        + ", ".join(users_with_posts)
        + "\n\n"
    )
    response += f"📝 Репорти ({len(users_with_reports)} користувачів):\n" + ", ".join(
        users_with_reports
    )

    await update.message.reply_text(
        response
        if users_with_posts or users_with_reports
        else "❗Немає користувачів з активними лічильниками."
    )
    logging.info(
        "Адміністратор %d переглянув список користувачів з активними лічильниками.",
        update.effective_user.id,
    )


def reset_all_counters():
    """Функція для скидання всіх лічильників."""
    current_time = datetime.now()
    for user_id in list(user_post_counts.keys()):
        user_post_counts[user_id]["count"] = 0
        user_post_counts[user_id]["reset_time"] = current_time

    for user_id in list(user_report_counts.keys()):
        user_report_counts[user_id]["count"] = 0
        user_report_counts[user_id]["reset_time"] = current_time

    logging.info("Всі лічильники були скинуті.")


# Список команд модератора


async def mod_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду списку команд модератора."""
    if update.effective_user.id not in MODERATOR_IDS:
        await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /mod_help, але не є модератором.",
            update.effective_user.id,
        )
        return

    response = (
        "✅ /buy_accept <user_id> - Схвалити запит на купівлю\n"
        "✅ /sell_accept <user_id> - Схвалити запит на продаж\n"
        "✅ /ad_accept <user_id> - Схвалити оголошення\n"
        "✅ /an_accept <user_id> - Схвалити запит на рекламу\n\n"
        "❌ /buy_reject <user_id> <reason> - Відхилити запит на купівлю\n"
        "❌ /sell_reject <user_id> <reason> - Відхилити запит на продаж\n"
        "❌ /ad_reject <user_id> <reason> - Відхилити оголошення\n"
        "❌ /an_reject <user_id> <reason> - Відхилити запит на рекламу\n\n"
        "💳 /payment <user_id> <service_id> <sum> - Надіслати реквізити оплати\n\n"
        "Service ID's:\n"
        "1 - закріплення оголошення на 3 доби\n"
        "2 - публікація\n"
        "3 - публікація + розміщення реклами на 12 годин\n"
        "4 - публікація + розміщення реклами на 1 добу\n"
        "5 - публікація + розміщення реклами на 3 доби\n"
        "6 - публікація + розміщення реклами на 7 діб\n"
        "7 - публікація + закріплення + розміщення реклами на 12 годин\n"
        "8 - публікація + закріплення + розміщення реклами на 1 добу\n"
        "9 - публікація + закріплення + розміщення реклами на 3 доби\n"
        "10 - публікація + закріплення + розміщення реклами на 7 діб"
    )

    await update.message.reply_text(response)
    logging.info("Модератор %d переглянув список команд.", update.effective_user.id)


# Команда надсилання реквізитів


async def payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для надсилання реквізитів для оплати."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /payment, але не є модератором.",
            update.effective_user.id,
        )
        return

    if len(context.args) != 3:
        if update.message:
            await update.message.reply_text(
                "❗Використовуйте: /payment <user_id> <service_id> <amount>"
            )
        return

    try:
        user_id = int(context.args[0])
        id_prod = int(context.args[1])
        amount = float(context.args[2])

        # Визначення послугу на основі id_prod
        services = {
            1: "закріплення оголошення на 3 доби",
            2: "публікація",
            3: "публікація + розміщення реклами на 12 годин",
            5: "публікація + розміщення реклами на 1 добу",
            6: "публікація + розміщення реклами на 3 доби",
            7: "публікація + розміщення реклами на 7 діб",
            8: "публікація + закріплення + розміщення реклами на 12 годин",
            9: "публікація + закріплення + розміщення реклами на 1 добу",
            10: "публікація + закріплення + розміщення реклами на 3 доби",
            11: "публікація + закріплення + розміщення реклами на 7 діб",
        }

        service_description = services.get(id_prod)
        if not service_description:
            await update.message.reply_text("❗Некоректний service_id.")
            return

        message = (
            f"💼 Ви надіслали запит на платні послуги, а саме: {service_description}.\n"
            f"💳 Для публікації - необхідно здійснити оплату на банківську картку: \n"
            f"`{PAYMENT_CARD}`\n"
            f"💰 Загальна вартість: `{amount}` грн.\n"
            f"📝 У призначенні платежу обов'язково вкажіть:\n"
            f"`Оплата за послуги №{user_id}`\n"
            "⏳ Після оплати, публікація з'явиться протягом 24-х годин, а ви отримаєте сповіщення.\n"
            "🙏 Дякуємо, що скористалися нашими послугами!"
        )

        await context.bot.send_message(
            chat_id=user_id, text=message, parse_mode=constants.ParseMode.MARKDOWN
        )

        logging.info(
            "Модератор %d надіслав реквізити оплати для користувача %d.",
            update.effective_user.id,
            user_id,
        )
        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення з реквізитами надіслано користувачу {user_id}."
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректні параметри введення.")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці реквізитів: %s", e)


async def buy_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для прийняття запиту на купівлю."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /buy_accept, але не є модератором.",
            update.effective_user.id,
        )
        return

    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❗Використовуйте: /buy_accept <user_id>")
        return

    try:
        user_id = int(context.args[0])

        message = (
            "✅ Ваш запит на купівлю успішно пройшов модерацію.\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )

        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d схвалив запит на купівлю для користувача %d.",
            update.effective_user.id,
            user_id,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про прийняття надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення прийняття: %s", e)


async def sell_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для прийняття запиту на продаж."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /sell_accept, але не є модератором.",
            update.effective_user.id,
        )
        return

    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❗Використовуйте: /sell_accept <user_id>")
        return

    try:
        user_id = int(context.args[0])
        message = (
            "✅ Ваш запит на продаж успішно пройшов модерацію.\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d схвалив запит на продаж для користувача %d.",
            update.effective_user.id,
            user_id,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про прийняття надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення прийняття: %s", e)


async def ad_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для прийняття запиту на оголошення."""
    # Тут

    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /ad_accept, але не є модератором.",
            update.effective_user.id,
        )
        return

    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❗Використовуйте: /ad_accept <user_id>")
        return

    try:
        user_id = int(context.args[0])
        message = (
            "✅ Ваш запит на оголошення успішно пройшов модерацію.\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d схвалив запит на оголошення для користувача %d.",
            update.effective_user.id,
            user_id,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про прийняття надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення прийняття: %s", e)


async def an_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для прийняття запиту на рекламу."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /an_accept, але не є модератором.",
            update.effective_user.id,
        )
        return

    if not context.args or len(context.args) != 1:
        if update.message:
            await update.message.reply_text("❗Використовуйте: /an_accept <user_id>")
        return

    try:
        user_id = int(context.args[0])
        message = (
            "✅ Ваш запит на рекламу успішно пройшов модерацію.\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d схвалив запит на рекламу для користувача %d.",
            update.effective_user.id,
            user_id,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про прийняття надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення прийняття: %s", e)


# Функції для відхилення запитів
async def buy_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для відхилення запиту на купівлю."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❗Використовуйте: /buy_reject <user_id> <reason>"
        )
        return

    try:
        user_id = int(context.args[0])
        reason = " ".join(context.args[1:])
        message = (
            "❌ Ваш запит на купівлю відхилено.\n"
            f"📝 Причина: {reason}\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d відхилив запит на купівлю для користувача %d.\n"
            "Причина: %s",
            update.effective_user.id,
            user_id,
            reason,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про відхилення надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення відхилення: %s", e)


async def sell_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для відхилення запиту на продаж."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /sell_reject, але не є модератором.",
            update.effective_user.id,
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❗Використовуйте: /sell_reject <user_id> <reason>"
        )
        return

    try:
        user_id = int(context.args[0])
        reason = " ".join(context.args[1:])
        message = (
            "❌ Ваш запит на продаж відхилено.\n"
            f"📝 Причина: {reason}\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d відхилив запит на продаж для користувача %d.\n" "Причина: %s",
            update.effective_user.id,
            user_id,
            reason,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про відхилення надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення відхилення: %s", e)


async def ad_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для відхилення запиту на оголошення."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /ad_reject, але не є модератором.",
            update.effective_user.id,
        )
        return

    if len(context.args) < 2:
        if update.message:
            await update.message.reply_text(
                "❗Використовуйте: /ad_reject <user_id> <reason>"
            )
        return

    try:
        user_id = int(context.args[0])
        reason = " ".join(context.args[1:])
        message = (
            "❌ Ваш запит на оголошення відхилено.\n"
            f"📝 Причина: {reason}\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d відхилив запит на оголошення для користувача %d. Причина: %s",
            update.effective_user.id,
            user_id,
            reason,
        )

        if update.message:
            await update.message.reply_text(
                f"✅ Повідомлення про відхилення надіслано користувачу {user_id}"
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення відхилення: %s", e)


async def an_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для відхилення запиту на рекламу."""
    if update.effective_user.id not in MODERATOR_IDS:
        if update.message:
            await update.message.reply_text("⚠️ Ця команда доступна лише модераторам.")
        logging.warning(
            "Користувач %d намагався використати /an_reject, але не є модератором.",
            update.effective_user.id,
        )
        return

    if len(context.args) < 2:
        if update.message:
            await update.message.reply_text(
                "❗Використовуйте: /an_reject <user_id> <reason>"
            )
        return

    try:
        user_id = int(context.args[0])
        reason = " ".join(context.args[1:])
        message = (
            "❌ Ваш запит на рекламу відхилено.\n"
            f"📝 Причина: {reason}\n"
            "🔍 Переглянути всі оголошення можна на нашому каналі:\n"
            "📢 @slavuta_ads"
        )
        await context.bot.send_message(chat_id=user_id, text=message)

        logging.info(
            "Модератор %d відхилив запит на рекламу для користувача %d.\n"
            "Причина: %s",
            update.effective_user.id,
            user_id,
            reason,
        )

        if update.message:
            await update.message.reply_text(
                "✅ Повідомлення про відхилення надіслано користувачу %d", user_id
            )
    except ValueError:
        if update.message:
            await update.message.reply_text("❗Некоректний user_id")
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ Помилка при відправці повідомлення: {str(e)}"
            )
        logging.error("Помилка при відправці повідомлення відхилення: %s", str(e))


def main():
    # Ініціалізація бота
    application = Application.builder().token(TOKEN).build()
    check_time()

    # Головний ConversationHandler для основної логіки бота
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("report", report_command),
        ],
        states={
            CHOOSING_ACTION: [
                MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data),
                CommandHandler("report", report_command),
            ],
            AWAITING_REPORT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report),
                CommandHandler("start", start),
            ],
            AWAITING_PHOTOS: [
                MessageHandler(
                    filters.PHOTO | (filters.TEXT & ~filters.COMMAND), handle_photo
                ),
                CommandHandler("start", start),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("report", report_command),
        ],
        per_message=False,
    )

    # 1. Middleware handlers
    application.add_handler(MessageHandler(filters.ALL, blacklist_middleware), group=-1)
    application.add_handler(conv_handler)

    # 2. Адміністративні команди модерації
    moderation_handlers = [
        CommandHandler("ban", ban_user),
        CommandHandler("unban", unban_user),
        CommandHandler("blacklist", view_blacklist),
        CommandHandler("ans", answer_report),
    ]
    for handler in moderation_handlers:
        application.add_handler(handler)

    # 3. Команди управління та статистики
    management_handlers = [
        CommandHandler("reset_counters", reset_counters),
        CommandHandler("view_counters", view_counters),
        CommandHandler("list_users", list_users),
        CommandHandler("check_stats", check_stats),
    ]
    for handler in management_handlers:
        application.add_handler(handler)

    # 4. Довідкові команди
    application.add_handler(CommandHandler("adm_help", adm_help))
    application.add_handler(CommandHandler("mod_help", mod_help))

    # 5. Команди для обробки платежів
    application.add_handler(CommandHandler("payment", payment))

    # 6. Команди прийняття заявок
    acceptance_handlers = [
        CommandHandler("buy_accept", buy_accept),
        CommandHandler("sell_accept", sell_accept),
        CommandHandler("ad_accept", ad_accept),
        CommandHandler("an_accept", an_accept),
    ]
    for handler in acceptance_handlers:
        application.add_handler(handler)

    # 7. Команди відхилення заявок
    rejection_handlers = [
        CommandHandler("buy_reject", buy_reject),
        CommandHandler("sell_reject", sell_reject),
        CommandHandler("ad_reject", ad_reject),
        CommandHandler("an_reject", an_reject),
    ]
    for handler in rejection_handlers:
        application.add_handler(handler)

    # 8. Налаштування періодичних завдань бота
    application.job_queue.run_repeating(
        callback=lambda context: check_bans(context.bot),
        interval=3600,  # кожну годину
        first=10,  # перший запуск через 10 секунд після старту
    )

    # 9. Налаштування планувальника
    scheduler = BackgroundScheduler()

    # Щоденні завдання
    daily_jobs = [
        # Відправка статистики о 23:59
        {
            "func": lambda: asyncio.run(send_daily_stats(application)),
            "trigger": "cron",
            "hour": 23,
            "minute": 59,
        },
        # Скидання статистики о 00:00
        {"func": reset_daily_stats, "trigger": "cron", "hour": 0, "minute": 0},
        # Скидання лічильників користувачів
        {"func": reset_all_counters, "trigger": "cron", "hour": 0, "minute": 0},
    ]

    for job in daily_jobs:
        scheduler.add_job(
            job["func"],
            job["trigger"],
            hour=job["hour"],
            minute=job["minute"],
            timezone="Europe/Kyiv",
        )

    # Періодична перевірка статусу
    scheduler.add_job(check_bot_status, "interval", minutes=30)
    scheduler.add_listener(handle_scheduler_error, EVENT_JOB_ERROR)

    # 10. Запуск усіх компонентів
    nest_asyncio.apply()
    scheduler.start()
    logging.info("Планувальник запущено успішно.")

    # Запуск Flask сервера у окремому потоці
    Thread(target=run_flask).start()

    # Запуск бота
    application.run_polling()


if __name__ == "__main__":
    main()
