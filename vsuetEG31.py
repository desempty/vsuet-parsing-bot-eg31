import telebot
from telebot import types
from telebot import apihelper  
from bs4 import BeautifulSoup
import requests
import os
import logging
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import random
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Настройка часового пояса для функции мониторинга
moscow_tz = ZoneInfo("Europe/Moscow")

# Настройка логирования
logging.basicConfig(    
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Настройка таймаута для запросов к Telegram API
apihelper.API_TIMEOUT = 30

# Использование lock() для защиты двух потоков выполнения кода
data_lock = threading.Lock()

# Настройка конфигурации
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен! Добавьте его в переменные окружения на Railway.")
bot = telebot.TeleBot(BOT_TOKEN)

# Константы
CONFIG = {
    "STUDENT_ID_LENGTH": 6, # Номер зачётной книжки
    "MIN_TABLE_COLUMNS": 3, # Минимальное количество ячеек в таблице (информативность запроса)
    "CHECK_INTERVAL": 3600, # Интервал между проверками рейтинга в час
    "INACTIVE_DAYS": 120,  # 4 Месяца (примерно 120 дней)
    "CLEANUP_INTERVAL": 86400  # Проверка раз в сутки (в секундах)
}

# Словарь предметов и их id на сайте для формирования url на таблицу с рейтингов
DICT_SUBJECT = {
    "Администрирование отеля": "251282",
    "Иностранный язык (второй)": "251287",
    "Организация и контроль в туристской деятельности": "251290",
    "Организация и технологии санаторно-курортного дела": "251291",
    "Основы классификации гостиничных предприятий": "251292",
    "Основы производственно-технологической деятельности гостиниц": "251293",
    "Специальные виды туризма": "251296",
    "Туристское ресурсоведение": "251297",
    "Экономика организаций профессиональной сферы": "251299",
}

# Маскируемся под разные устройства/разных пользователей для безопасности
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]

user_state = {} # Словарь состояний (для отслеживания хода диалога)
user_selected_data = {} # Для хранения данных, введенных пользователем
user_subscriptions = {} # Словарь, хранящий пользователей, которые будут получать уведомления
previous_ratings = {} # Предыдущее состояние рейтинга для того, чтобы бот не спамил предыдущими изменениями
user_last_activity = {}  # Словарь для отслеживания активности пользователя
last_error_time = {}  # Отслеживание ошибок (анти-спам логов)

# 1. ФУНКЦИИ ДЛЯ РАБОТЫ С АКТИВНОСТЬЮ ПОЛЬЗОВАТЕЛЕЙ

# 1.1. Обновление времени последней активности пользователя
def update_activity(chat_id):

    with data_lock:
        user_last_activity[chat_id] = time.time()
    logger.debug(f"Обновлена активность пользователя {chat_id}")

# 1.2. Очистка данных неактивных пользователей (неактивны более 4 месяцев). Запускается в отдельном потоке раз в сутки
def cleanup_inactive_users():

    while True:
        try:
            current_time = time.time()
            inactive_threshold = CONFIG["INACTIVE_DAYS"] * 24 * 60 * 60  # дни в секунды
            
            users_to_delete = []
            
            # Поиск неактивных пользователей (с lock для безопасности)
            with data_lock:
                for chat_id, last_active in list(user_last_activity.items()):
                    if current_time - last_active > inactive_threshold:
                        users_to_delete.append(chat_id)
            
            # Удаление данных неактивных пользователей (с lock)
            with data_lock:
                for chat_id in users_to_delete:
                    # Очищаем все словари
                    user_subscriptions.pop(chat_id, None)
                    previous_ratings.pop(chat_id, None)
                    user_state.pop(chat_id, None)
                    user_selected_data.pop(chat_id, None)
                    user_last_activity.pop(chat_id, None)
                    
                    logger.info(f"Очищены данные неактивного пользователя {chat_id} (неактивен более {CONFIG['INACTIVE_DAYS']} дней)")
            
            if users_to_delete:
                logger.info(f"Очистка завершена. Удалено пользователей: {len(users_to_delete)}")
            
            # Проверка раз в сутки
            time.sleep(CONFIG["CLEANUP_INTERVAL"])
            
        except Exception as e:
            logger.error(f"Ошибка в потоке очистки: {e}")
            time.sleep(3600)  # При ошибке ждём час и пробуем снова

# 1.3. Количествно активных пользователей
def get_active_users_count():
    
    current_time = time.time()
    inactive_threshold = CONFIG["INACTIVE_DAYS"] * 24 * 60 * 60
    active_count = 0
    
    with data_lock:
        for last_active in user_last_activity.values():
            if current_time - last_active <= inactive_threshold:
                active_count += 1
    
    return active_count

# 1.4. Очистка данных при явном выходе пользователя (кнопка отмена)
def cleanup_on_exit(chat_id):

    with data_lock:
        user_subscriptions.pop(chat_id, None)
        previous_ratings.pop(chat_id, None)
        user_state.pop(chat_id, None)
        user_selected_data.pop(chat_id, None)
        user_last_activity.pop(chat_id, None)
    logger.info(f"Данные пользователя {chat_id} очищены при выходе")

# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ

# 2.1. Получение значений из ячеек таблицы
def safe_get_cell(cells, index, default="—"):

    if 0 <= index < len(cells):
        if cells[index].get_text(strip=True) == "":
            return default
        else:
            return cells[index].get_text(strip=True)
    return default

# 2.2 Парсинг рейтинга студента из HTML-таблицы
def parse_student_row(soup, student_id):
     
    #  Для поиска номера зачётной книжки в HTML-коде (обычно с тегом "а")
    link = soup.find("a", string=student_id)
    if not link:
        link = soup.find("td", string=student_id)
    else: 
        return
    
    row = link.find_parent("tr")
    cells = row.find_all("td")
    
    if len(cells) < CONFIG["MIN_TABLE_COLUMNS"]:
        return None
    
    return {
        "Номер по списку": safe_get_cell(cells, 0),
        "Номер зачётной книжки": safe_get_cell(cells, 1),
        "Лекции КТ №1": safe_get_cell(cells, 3),
        "Практики КТ №1": safe_get_cell(cells, 4),
        "ИТОГ КТ №1": safe_get_cell(cells, 7),
        "Лекции КТ №2": safe_get_cell(cells, 8),
        "Практики КТ №2": safe_get_cell(cells, 9),
        "ИТОГ КТ №2": safe_get_cell(cells, 12),
        "Лекции КТ №3": safe_get_cell(cells, 13),
        "Практики КТ №3": safe_get_cell(cells, 14),
        "ИТОГ КТ №3": safe_get_cell(cells, 17),
        "Лекции КТ №4": safe_get_cell(cells, 18),
        "Практики КТ №4": safe_get_cell(cells, 19),
        "ИТОГ КТ №4": safe_get_cell(cells, 22),
        "Лекции КТ №5": safe_get_cell(cells, 23),
        "Практики КТ №5": safe_get_cell(cells, 24),
        "ИТОГ КТ №5": safe_get_cell(cells, 27),
        "Итоговый рейтинг по всем КТ": safe_get_cell(cells, 29),
        "Оценка": safe_get_cell(cells, 30),
    }

# 2.3 Создание клавиатуры с номерами предметов для облегченного выбора
def create_subject_keyboard():
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row_width = 3
    
    # Список кнопок
    buttons = []
    for i in range(1, len(DICT_SUBJECT) + 1):
        buttons.append(str(i))
    
    # Для формирования по три кнопки в строчку 
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i+3])
    
    markup.row("Отмена")
    
    return markup

# 2.4 Для создания списка выбора предметов
def create_subject_menu_text():

    menu_text = "Введите номер предмета:\n\n"
    
    for i, subject in enumerate(DICT_SUBJECT.keys(), 1):
        menu_text += f"{i}. {subject}\n"
    
    menu_text += f"\nВсего предметов: {len(DICT_SUBJECT)}"
    
    return menu_text

# 2.5 Создание изображения с рейтингом студентов (с поддержкой кириллицы)
def create_rating_image(data, student_id, subject_name):

    # Настройка размеров изображения
    width = 800
    row_height = 40
    header_height = 120
    margin = 20

    height = header_height + (len(data) + 1) * row_height + margin * 2

    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # Загрузка шрифтов
    base_dir = os.path.dirname(os.path.abspath(__file__))
    fonts_dir = os.path.join(base_dir, "fonts")

    regular_path = os.path.join(fonts_dir, "DejaVuSans.ttf")
    bold_path = os.path.join(fonts_dir, "DejaVuSans-Bold.ttf")

    if not os.path.exists(regular_path) or not os.path.exists(bold_path):
        raise RuntimeError(
            "Шрифты не найдены в репозитории"
        )

    # Размеры шрифтов
    title_font = ImageFont.truetype(bold_path, 32)
    header_font = ImageFont.truetype(bold_path, 22)
    text_font = ImageFont.truetype(regular_path, 18)
    small_font = ImageFont.truetype(regular_path, 14)

    # Формирование заголовка
    draw.text((margin, 20), "Рейтинг студента", fill="#2E86C1", font=title_font)
    draw.text(
        (margin, 60),
        f"Зачётная книжка: {student_id}",
        fill="#555555",
        font=header_font,
    )

    # перенос длинного названия предмета
    max_width = width - margin * 2
    words = subject_name.split()
    lines, current = [], ""

    for word in words:
        test = (current + " " + word).strip()
        w = draw.textbbox((0, 0), test, font=header_font)[2]
        if w <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    y = 85
    for line in lines:
        draw.text((margin, y), line, fill="#555555", font=header_font)
        y += 24

    # Заголовок таблицы
    y = header_height
    draw.rectangle(
        [margin, y, width - margin, y + row_height],
        fill="#2E86C1",
        outline="#1A5276",
    )

    # Значение колонок
    draw.text((margin + 10, y + 10), "Параметр", fill="#FFFFFF", font=header_font)

    val = "Значение"
    val_w = draw.textbbox((0, 0), val, font=header_font)[2]
    draw.text((width - margin - val_w - 10, y + 10), val, fill="#FFFFFF", font=header_font)

    # Строки
    y += row_height

    for i, (key, value) in enumerate(data.items()):
        bg = "#F9F9F9" if i % 2 == 0 else "#FFFFFF"

        if any(x in key for x in ("ИТОГ", "Оценка", "Итоговый")):
            bg = "#FFF3CD"

        draw.rectangle(
            [margin, y, width - margin, y + row_height],
            fill=bg,
            outline="#DDDDDD",
        )

        key_display = key if len(key) <= 38 else key[:35] + "..."
        draw.text((margin + 10, y + 10), key_display, fill="#000000", font=text_font)

        val = str(value)
        val_w = draw.textbbox((0, 0), val, font=text_font)[2]
        draw.text((width - margin - val_w - 10, y + 10), val, fill="#000000", font=text_font)

        y += row_height

    # Футер
    footer = "ВГУИТ Рейтинг Бот"
    fw = draw.textbbox((0, 0), footer, font=small_font)[2]

    draw.text(
        ((width - fw) // 2, y + 15),
        footer,
        fill="#888888",
        font=small_font,
    )

    # Сохранение фото во временный файль
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return buffer

# 2.6.мФормирование ссылки и получение рейтинга студента
def fetch_rating_from_site(object_index, student_id):
    
    url = f"https://rating.vsuet.ru/web/Ved/Ved.aspx?id={object_index}"
    
    # Выбор случайного User-Agent
    try:
        response = requests.get(
            url,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=40 # Ожидание в 40 секунд для загрузки всего HTML-кода
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # Парсинг данных и получение словаря значений
        data = parse_student_row(soup, student_id)
        
        return data
    
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return None

# 2.7. Создание кнопки "Отмена"
def create_cancel_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add("Отмена")
    return markup

# 2.8. Создание кнопки главного меню
def create_main_menu_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add("Ввести номер зачётной книжки")
    markup.add("Отмена")
    return markup

# 3. ФУНКЦИИ МОНИТОРИНГА

# 3.1. Для отправки уведомлений в случае 
def send_change_notification(chat_id, subject_name, student_id, changes):

    message = f"Изменён рейтинг по предмету: {subject_name} \n\n"
    message += f"Время: {datetime.now(moscow_tz).strftime('%d.%m.%Y %H:%M')}\n\n"
    message += f"Изменения:\n\n"
    
    for change in changes:
        message += f"{change['field']}:\n"
        message += f"Было: {change['old']}\n"
        message += f"Стало: {change['new']}\n\n"
    
    try:
        bot.send_message(chat_id, message)
        logger.info(f"Уведомление отправлено пользователю {chat_id}")
        update_activity(chat_id)  # Обновляем активность при отправке уведомления
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")
# 3.1 Для проверки изменений рейтинга всех подписанных на уведомления пользователей
def check_rating_changes():
    
    now = datetime.now(moscow_tz)
    current_hour = now.hour
    if current_hour >= 19 or current_hour < 10:
        logger.info("Ночное время (19:00-10:00 MSK) — мониторинг приостановлен")
        return

    active_count = get_active_users_count()
    logger.info(f"Начало проверки. Активных пользователей: {active_count}")
    
    # Блок с lock для безопасного доступа к общим данным
    with data_lock:
        subscriptions_copy = list(user_subscriptions.items())
    
    for chat_id, subscription in subscriptions_copy:
        try:
            # Пропускаем неактивных пользователей
            with data_lock:
                if chat_id not in user_last_activity:
                    continue
            
            student_id = subscription.get("student_id")
            subjects = subscription.get("subjects", [])
            
            if not student_id or not subjects:
                continue
            
            for subject_name in subjects:
                if subject_name not in DICT_SUBJECT:
                    continue
                
                object_index = DICT_SUBJECT[subject_name]
                current_data = fetch_rating_from_site(object_index, student_id)
                
                if not current_data:
                    # ЛОГИРОВАНИЕ НЕ ЧАЩЕ 1 РАЗА В ЧАС ВО ИЗБЕЖАНИЯ СПАМА ЛОГОВ
                    last_error = last_error_time.get(chat_id, 0)
                    current_time = time.time()
                    
                    # Логировать только если прошёл час с последней ошибки
                    if current_time - last_error > 3600:
                        logger.warning(f"Не удалось получить данные для {student_id} - {subject_name}")
                        last_error_time[chat_id] = current_time

                    continue
                
                with data_lock:
                    prev_data = previous_ratings.get(chat_id, {}).get(subject_name, {})
                
                changes = []
                fields_to_check = [
                    "Лекции КТ №1", "Лекции КТ №2", "Лекции КТ №3", "Лекции КТ №4", "Лекции КТ №5",
                    "Практики КТ №1", "Практики КТ №2", "Практики КТ №3", "Практики КТ №4", "Практики КТ №5",
                    "ИТОГ КТ №1", "ИТОГ КТ №2", "ИТОГ КТ №3", "ИТОГ КТ №4", "ИТОГ КТ №5",
                    "Итоговый рейтинг по всем КТ",
                    "Оценка"
                ]

                for key in fields_to_check:
                    prev_value = prev_data.get(key, None)
                    curr_value = current_data.get(key, None)
                    
                    if curr_value and curr_value != "—" and prev_value != curr_value:
                        changes.append({
                            "field": key,
                            "old": prev_value,
                            "new": curr_value
                        })
                
                if changes:
                    send_change_notification(chat_id, subject_name, student_id, changes)
                
                with data_lock: 
                    if chat_id not in previous_ratings:
                        previous_ratings[chat_id] = {}
                    previous_ratings[chat_id][subject_name] = current_data
                
                time.sleep(45)
                
        except Exception as e:
            logger.error(f"Ошибка проверки для chat_id {chat_id}: {e}")
    
    logger.info("Проверка завершена")

# 3.2. Фоновый поток для мониторинга
def monitoring_thread():
    
    while True:
        try:
            with data_lock:
                has_subscriptions = bool(user_subscriptions)
            
            if has_subscriptions:
                check_rating_changes()
            time.sleep(CONFIG["CHECK_INTERVAL"])
        except Exception as e:
            logger.error(f"Ошибка в потоке мониторинга: {e}")
            time.sleep(60)


# 4. ОБРАБОТЧИКИ КОМАНД 

# 4.1. Handler = start
@bot.message_handler(commands=['start'])
def start(message):
    """Обработчик команды /start"""
    chat_id = message.chat.id
    update_activity(chat_id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add("Начать")
    try:
        bot.send_message(
            chat_id,
            "Привет! Я бот для мониторинга твоего рейтинга.\n\n"
            "Нажми на кнопку «Начать» или введи команду с клавиатуры",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# 4.2 Handler для отмены действия
@bot.message_handler(func=lambda message: message.text in ["Отмена", "Вернуться назад", "вернуться назад", "отмена", "Отмена"])
def handle_cancel(message):
    
    chat_id = message.chat.id
    
    # Полная очистка всех зарегистрированных данных при выходе
    cleanup_on_exit(chat_id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add("Начать")
    try:
        bot.send_message(
            chat_id,
            "Действие отменено. Вы в главном меню.",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# 4.3. Handler для запуска в работу бота
@bot.message_handler(func=lambda message: message.text.lower() == "начать")
def handle_start(message):
    """Обработчик кнопки «Начать»"""
    chat_id = message.chat.id
    update_activity(chat_id)
    
    try:
        bot.send_message(
            chat_id,
            "Выберите действие:",
            reply_markup=create_main_menu_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# 4.4. Handler для ввода номера зачётной книжки
@bot.message_handler(func=lambda message: message.text.lower() == "ввести номер зачётной книжки")
def handle_choose_subject(message):

    chat_id = message.chat.id
    update_activity(chat_id)
    
    # Блок with для безопасности отдельного потока
    with data_lock:
        user_state.pop(chat_id, None)
        user_selected_data.pop(chat_id, None)
        user_state[chat_id] = "entering_id_first"
    
    try:
        bot.send_message(
            chat_id,
            "Введите номер вашей зачётной книжки (ровно 6 цифр):",
            reply_markup=create_cancel_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# 4.5. Handler для обработки корректности ввода зачётной книжки и, в случае корректности, подключения уведомлений.
@bot.message_handler(func=lambda message: 
    message.chat.id in user_state and 
    user_state[message.chat.id] == "entering_id_first"
)
def handle_student_id_first(message):
    """Обработчик ввода номера студента"""
    chat_id = message.chat.id
    student_id = message.text.strip()
    
    update_activity(chat_id)
    
    if not (student_id.isdigit() and len(student_id) == CONFIG["STUDENT_ID_LENGTH"]):
        try:
            bot.send_message(
                chat_id,
                "Номер зачётной книжки должен содержать ровно 6 цифр.\n"
                "Повторите ввод:",
                reply_markup=create_cancel_markup()
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        return
    
    with data_lock:
        user_selected_data[chat_id] = {"student_id": student_id}
        
        all_subjects = list(DICT_SUBJECT.keys())
        user_subscriptions[chat_id] = {
            "student_id": student_id, 
            "subjects": all_subjects
        }
        user_last_activity[chat_id] = time.time()
    
    try:
        bot.send_message(
            chat_id,
            "Подключение уведомлений... Ожидайте",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
    
    for subject_name in all_subjects:
        object_index = DICT_SUBJECT[subject_name]
        data = fetch_rating_from_site(object_index, student_id)
        
        if data:
            with data_lock:
                if chat_id not in previous_ratings:
                    previous_ratings[chat_id] = {}
                previous_ratings[chat_id][subject_name] = data
        
        time.sleep(1)
    
    try:
        bot.send_message(
            chat_id,
            "Уведомления успешно подключены",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
    
    with data_lock:
        user_state[chat_id] = "choosing_subject_after_id"
    
    try:
        bot.send_message(
            chat_id,
            create_subject_menu_text(),
            reply_markup=create_subject_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

# 4.6. Handler выбора предмета
@bot.message_handler(func=lambda message: 
    message.chat.id in user_state and 
    user_state[message.chat.id] == "choosing_subject_after_id"
)
def handle_subject_choice_after_id(message):

    chat_id = message.chat.id
    text = message.text.strip()

    update_activity(chat_id)

    # ПРЕДУПРЕЖДЕНИЕ О НОЧНОМ ВРЕМЕНИ
    now = datetime.now(moscow_tz)
    current_hour = now.hour
    if current_hour >= 19 or current_hour < 10:
        try:

            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            markup.row("Выбрать другой предмет", "Отмена")

            bot.send_message(
                chat_id,
                "Сайт рейтинга недоступен с 19:00 до 09:00.\n"
                "Попробуйте запросить данные днём.",
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        return

    with data_lock:
        has_session = chat_id in user_selected_data and "student_id" in user_selected_data[chat_id]
    
    if not has_session:
        try:
            bot.send_message(
                chat_id,
                "Сессия устарела. Пожалуйста, начните заново командой /start",
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("Начать")
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        return
    
    if text.lower() == "выбрать другой предмет":
        try:
            bot.send_message(
                chat_id,
                create_subject_menu_text(),
                reply_markup=create_subject_keyboard()
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        return
    
    if text.lower() == "отмена":
        cleanup_on_exit(chat_id)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("Начать")
        try:
            bot.send_message(chat_id, "Возврат в главное меню", reply_markup=markup)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        return
    
    try:
        choice = int(text)
        
        if 1 <= choice <= len(DICT_SUBJECT):
            with data_lock:
                student_id = user_selected_data[chat_id]["student_id"]
            
            subject_name = list(DICT_SUBJECT.keys())[choice - 1]
            object_index = DICT_SUBJECT[subject_name]
            
            try:
                bot.send_message(
                    chat_id,
                    "Загружаем данные с сайта... \nЭто займёт около 5 секунд",
                    reply_markup=types.ReplyKeyboardRemove()
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")
            
            data = fetch_rating_from_site(object_index, student_id)
            
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
            markup.row("Выбрать другой предмет", "Отмена")
            
            if data:
                try:
                    bot.send_message(chat_id, "Данные получены. Откройте изображение ниже", reply_markup=markup)
                    
                    image = create_rating_image(data, student_id, subject_name)
                    
                    bot.send_photo(
                        chat_id,
                        image,
                        caption=f"Рейтинг по предмету: {subject_name.lower()}",
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения: {e}")
            else:
                try:
                    bot.send_message(   
                        chat_id,
                        f"Студент {student_id} не найден в таблице по предмету '{subject_name}'.",
                        reply_markup=markup
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения: {e}")
            
            with data_lock:
                user_state[chat_id] = "choosing_subject_after_id"
        
        else:
            try:
                bot.send_message(
                    chat_id,
                    f"Введите число от 1 до {len(DICT_SUBJECT)}",
                    reply_markup=create_cancel_markup()
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")
    
    except ValueError:
        try:
            bot.send_message(
                chat_id,
                "Введите целое число (цифрой, например: 2) или используйте кнопки",
                reply_markup=create_cancel_markup()
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")

# 5. ЗАПУСК

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Веб-хук удален")
    
    # Запуск потока мониторинга
    monitoring = threading.Thread(target=monitoring_thread, daemon=True)
    monitoring.start()
    logger.info("Мониторинг запущен")
    
    # Запуск потока очистки неактивных пользователей
    cleanup = threading.Thread(target=cleanup_inactive_users, daemon=True)
    cleanup.start()
    logger.info(f"Поток очистки запущен (неактивность более {CONFIG['INACTIVE_DAYS']} дней / 4 месяца)")
    
    # Информация о старте
    logger.info("=" * 50)
    logger.info("Бот успешно запущен!")
    logger.info(f"Интервал мониторинга: {CONFIG['CHECK_INTERVAL']} секунд")
    logger.info(f"Очистка неактивных: {CONFIG['INACTIVE_DAYS']} дней (4 месяца)")
    logger.info("=" * 50)
    
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)