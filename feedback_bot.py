import os
import asyncio
import aiohttp
from dotenv import load_dotenv
import logging
import sqlite3
from datetime import datetime, timedelta
import json
from notion_client import Client

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_MEETINGS_DB_ID = os.getenv("NOTION_MEETINGS_DB_ID")
NOTION_FEEDBACK_DB_ID = os.getenv("NOTION_FEEDBACK_DB_ID")
ERROR_CHAT_ID = os.getenv("ERROR_CHAT_ID")

# Константы
POLLING_INTERVAL = 60 * 60 * 8   # 8 часов в секундах
REMINDER_INTERVAL = 20 * 60 * 8  # 8 часов в секундах

class FeedbackBot:
    def __init__(self):
        self.notion = Client(auth=NOTION_API_KEY)
        self.init_database()
        logger.info("Бот инициализирован")

    def init_database(self):
        """Инициализация базы данных SQLite"""
        with sqlite3.connect("feedback.db") as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS questionnaires (
                    chat_id TEXT,
                    meeting_id TEXT,
                    meeting_name TEXT,
                    student_id TEXT,  
                    status TEXT,
                    current_question INTEGER,
                    answers TEXT,
                    last_message_id TEXT,
                    created_at TEXT,
                    PRIMARY KEY (chat_id, meeting_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_meetings (
                    meeting_id TEXT PRIMARY KEY
                )
            """)
            conn.commit()

    # async def start(self):
    #     """Запуск бота"""
    #     logger.info("Запуск бота")
    #     await asyncio.sleep(3600)  # Заглушка, позже заменим на фоновые задачи

    async def start(self):
        """Запуск бота с фоновыми задачами"""
        logger.info("Запуск бота")
        tasks = [
            asyncio.create_task(self.run_notion_checker()),
            asyncio.create_task(self.run_reminder_checker()),
            asyncio.create_task(self.run_telegram_polling())
        ]
        await asyncio.gather(*tasks)

    async def fetch_notion_meetings(self):
        """Получение завершенных встреч из Notion"""
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        today = datetime.now().isoformat()
        fourteen_days_ago = (datetime.now() - timedelta(days=14)).isoformat()

        payload = {
            "filter": {
                "and": [
                    {"property": "Status", "status": {"equals": "Done"}},
                    {"property": "Date", "date": {"on_or_after": fourteen_days_ago}},
                    {"property": "Date", "date": {"on_or_before": today}},
                    {"property": "BOT Feedback Received", "checkbox": {"equals": False}}
                ]
            }
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                    f"https://api.notion.com/v1/databases/{NOTION_MEETINGS_DB_ID}/query",
                    headers=headers,
                    json=payload
            ) as response:
                data = await response.json()
                return data.get('results', [])

    async def process_meeting(self, meeting):
        properties = meeting['properties']
        meeting_id = meeting['id']
        meeting_name = properties['Name']['title'][0]['text']['content']
        mentor_relation = properties['Mentor(s)']['relation'][0]['id']
        mentor_name = await self.get_notion_page_name(mentor_relation)
        student_id = properties['Student']['relation'][0]['id']
        chat_id_array = properties['TG_CHAT_ID']['rollup']['array']
        chat_id = str(chat_id_array[0]['number'])

        if self.is_meeting_processed(meeting_id):
            return

        # Сохраняем анкету
        self.save_questionnaire(chat_id, meeting_id, meeting_name, mentor_name, student_id)
        logger.info(f"Сохранена новая анкета для chat_id {chat_id}, meeting_id {meeting_id}")

        # Отправляем начальное сообщение сразу
        message_id = await self.send_initial_message(chat_id, meeting_name, mentor_name)
        with sqlite3.connect("feedback.db") as conn:
            conn.execute(
                "UPDATE questionnaires SET last_message_id = ? WHERE chat_id = ? AND meeting_id = ?",
                (message_id, chat_id, meeting_id)
            )
            conn.commit()

        # Отмечаем встречу как обработанную
        self.mark_meeting_processed(meeting_id)



    async def get_notion_page_name(self, page_id):
        """Получение имени страницы Notion по ID"""
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers
            ) as response:
                data = await response.json()
                return data['properties']['Name']['title'][0]['text']['content']

    def is_meeting_processed(self, meeting_id):
        """Проверка, обработана ли встреча"""
        with sqlite3.connect("feedback.db") as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_meetings WHERE meeting_id = ?",
                (meeting_id,)
            )
            return cursor.fetchone() is not None

    def mark_meeting_processed(self, meeting_id):
        """Отметка встречи как обработанной"""
        with sqlite3.connect("feedback.db") as conn:
            conn.execute(
                "INSERT INTO processed_meetings (meeting_id) VALUES (?)",
                (meeting_id,)
            )
            conn.commit()

    def save_questionnaire(self, chat_id, meeting_id, meeting_name, mentor_name, student_id):
        with sqlite3.connect("feedback.db") as conn:
            conn.execute("""
                INSERT INTO questionnaires 
                (chat_id, meeting_id, meeting_name, student_id, status, current_question, answers, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, '{}', ?)
            """, (chat_id, meeting_id, meeting_name, student_id, datetime.now().isoformat()))
            conn.commit()

    async def send_initial_message(self, chat_id, meeting_name, mentor_name):
        """Отправка начального сообщения с кнопкой 'Начать'"""
        keyboard = {
            "inline_keyboard": [[{
                "text": "Начать",
                "callback_data": f"start,{chat_id},{meeting_name}"
            }]]
        }
        message_text = (
            f"Пожалуйста, оставьте обратную связь по встрече:\n"
            f"<b>{meeting_name}</b> с ментором {mentor_name}."
        )
        message = await self.send_telegram_message(chat_id, message_text, keyboard)
        return message['result']['message_id']

    async def send_telegram_message(self, chat_id, text, keyboard=None):
        """Отправка сообщения в Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }
        if keyboard:
            payload['reply_markup'] = json.dumps(keyboard)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return await response.json()

    async def handle_callback_query(self, callback_query):
        """Обработка callback_query"""
        data = callback_query['data'].split(',')
        action = data[0]

        if action == "start":
            chat_id = data[1]
            meeting_name = data[2] if len(data) > 2 else ""
            await self.start_questionnaire(chat_id, meeting_name, callback_query['message']['message_id'])
        elif action == "answer":
            chat_id = data[1]
            meeting_id = data[2]
            question_num = int(data[3])
            points = int(data[4])
            await self.process_answer(chat_id, question_num, points, callback_query['message']['message_id'])

    async def start_questionnaire(self, chat_id, meeting_name, message_id):
        """Начало анкеты: отправка первого вопроса"""
        with sqlite3.connect("feedback.db") as conn:
            cursor = conn.execute(
                "SELECT meeting_id FROM questionnaires WHERE chat_id = ? AND status = 'pending' LIMIT 1",
                (chat_id,)
            )
            row = cursor.fetchone()
            if row:
                meeting_id = row[0]
                self.update_questionnaire_status(chat_id, meeting_id, 'in_progress', 1)
                keyboard = self.generate_question_keyboard(1, chat_id, meeting_id)
                question_text = self.get_question_text(1)
                await self.edit_telegram_message(chat_id, message_id, question_text, keyboard)

    def update_questionnaire_status(self, chat_id, meeting_id, status, current_question):
        """Обновление статуса анкеты"""
        with sqlite3.connect("feedback.db") as conn:
            conn.execute(
                "UPDATE questionnaires SET status = ?, current_question = ? WHERE chat_id = ? AND meeting_id = ?",
                (status, current_question, chat_id, meeting_id)
            )
            conn.commit()

    def generate_question_keyboard(self, question_num, chat_id, meeting_id):
        """Генерация клавиатуры для вопроса"""
        return {
            "inline_keyboard": [[
                {"text": f"{i} ⭐️", "callback_data": f"answer,{chat_id},{meeting_id},{question_num},{i}"}
                for i in range(1, 6)
            ]]
        }

    def get_question_text(self, question_num):
        """Получение текста вопроса"""
        questions = [
            "Насколько продуктивно была проведена встреча со стороны ментора?",
            "Насколько быстро ментор отвечает на ваши вопросы?",
            "Насколько вам понятен план действий до следующей встречи?",
            "Оцените уровень экспертизы ментора по основной теме встречи.",
            "Насколько быстро и эффективно ваш координатор помогает вам?"
        ]
        return questions[question_num - 1]

    async def process_answer(self, chat_id, question_num, points, message_id):
        with sqlite3.connect("feedback.db") as conn:
            cursor = conn.execute(
                "SELECT meeting_id, answers, current_question FROM questionnaires WHERE chat_id = ? AND status = 'in_progress'",
                (chat_id,)
            )
            row = cursor.fetchone()
            if row:
                meeting_id, answers_json, current_question = row
                answers = json.loads(answers_json) if answers_json else {}
                answers[question_num] = points
                logger.info(f"Сохранен ответ на вопрос {question_num}: {points} для meeting_id {meeting_id}")

                next_question = current_question + 1
                total_questions = 5
                if next_question <= total_questions:
                    conn.execute(
                        "UPDATE questionnaires SET answers = ?, current_question = ? WHERE chat_id = ? AND meeting_id = ?",
                        (json.dumps(answers), next_question, chat_id, meeting_id)
                    )
                    conn.commit()
                    keyboard = self.generate_question_keyboard(next_question, chat_id, meeting_id)
                    question_text = self.get_question_text(next_question)
                    await self.edit_telegram_message(chat_id, message_id, question_text, keyboard)
                else:
                    conn.execute(
                        "UPDATE questionnaires SET answers = ?, status = 'completed', current_question = ? WHERE chat_id = ? AND meeting_id = ?",
                        (json.dumps(answers), next_question, chat_id, meeting_id)
                    )
                    conn.commit()
                    # Получаем Summary из Notion
                    summary = await self.get_meeting_summary(meeting_id)

                    await self.edit_telegram_message(chat_id, message_id, f"Спасибо за обратную связь! \nSummary встречи:\n{summary}")
                    await self.save_feedback_to_notion(chat_id, meeting_id, answers)
                    await self.mark_notion_meeting_completed(meeting_id)




    async def edit_telegram_message(self, chat_id, message_id, text, keyboard=None):
        """Редактирование сообщения в Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
        payload = {
            'chat_id': chat_id,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'HTML'
        }
        if keyboard:
            payload['reply_markup'] = json.dumps(keyboard)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return await response.json()

    async def save_feedback_to_notion(self, chat_id, meeting_id, answers):
        with sqlite3.connect("feedback.db") as conn:
            cursor = conn.execute(
                "SELECT student_id, meeting_name FROM questionnaires WHERE chat_id = ? AND meeting_id = ?",
                (chat_id, meeting_id)
            )
            row = cursor.fetchone()
            if not row:
                logger.error(f"Анкета для chat_id {chat_id} и meeting_id {meeting_id} не найдена")
                return
            student_id, meeting_name = row

        feedback_data = {
            "parent": {"database_id": NOTION_FEEDBACK_DB_ID},
            "properties": {
                "Meeting": {"relation": [{"id": meeting_id}]},
                "Student": {"relation": [{"id": student_id}]},
                "[1] MEETING PRODUCTIVITY": {"number": answers.get('1', 0)},
                "[2] RESPONSE SPEED": {"number": answers.get('2', 0)},
                "[3] PLAN UNDERSTANDING": {"number": answers.get('3', 0)},
                "[4] EXPERTISE": {"number": answers.get('4', 0)},
                "[5] EFFECTIVENESS (TRACKER)": {"number": answers.get(5, 0)},
                "Filler Name": {"rich_text": [{"text": {"content": "BOT"}}]},
                "Date": {"date": {"start": datetime.now().isoformat()}},
                "Meeting Name": {"title": [{"text": {"content": meeting_name}}]},
                "TG_CHAT_ID": {"rich_text": [{"text": {"content": chat_id}}]}
            }
        }
        # Убираем await, так как метод синхронный
        response = self.notion.pages.create(**feedback_data)
        logger.info(f"Feedback saved to Notion: {response}")

    async def mark_notion_meeting_completed(self, meeting_id):
        """Отметка встречи как обработанной в Notion"""
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        payload = {
            "properties": {
                "BOT Feedback Received": {"checkbox": True}
            }
        }
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                    f"https://api.notion.com/v1/pages/{meeting_id}",
                    headers=headers,
                    json=payload
            ) as response:
                if response.status != 200:
                    error_data = await response.json()
                    logger.error(f"Ошибка обновления встречи: {error_data}")

    async def run_notion_checker(self):
        """Фоновая проверка завершенных встреч"""
        while True:
            try:
                meetings = await self.fetch_notion_meetings()
                logger.info(f"Найдено {len(meetings)} встреч для обработки")
                for meeting in meetings:
                    await self.process_meeting(meeting)
            except Exception as e:
                logger.error(f"Ошибка в notion_checker: {e}")
            await asyncio.sleep(POLLING_INTERVAL)

    async def run_reminder_checker(self):
        """Фоновая отправка напоминаний"""
        while True:
            try:
                with sqlite3.connect("feedback.db") as conn:
                    cursor = conn.execute(
                        "SELECT chat_id, meeting_id, meeting_name, last_message_id FROM questionnaires WHERE status = 'pending'"
                    )
                    pending = cursor.fetchall()
                    logger.info(f"Найдено {len(pending)} анкет со статусом 'pending'")
                    for chat_id, meeting_id, meeting_name, last_message_id in pending:
                        if last_message_id:
                            await self.delete_telegram_message(chat_id, last_message_id)

                        # Получаем имя ментора из Notion по meeting_id
                        mentor_name = await self.get_mentor_name_from_notion(meeting_id)

                        # Отправляем сообщение с реальными meeting_name и mentor_name
                        message_id = await self.send_initial_message(chat_id, meeting_name, mentor_name)

                        conn.execute(
                            "UPDATE questionnaires SET last_message_id = ? WHERE chat_id = ? AND meeting_id = ?",
                            (message_id, chat_id, meeting_id)
                        )
                        conn.commit()
            except Exception as e:
                logger.error(f"Ошибка в reminder_checker: {e}")
            await asyncio.sleep(REMINDER_INTERVAL)

    async def get_mentor_name_from_notion(self, meeting_id):
        """Получение имени ментора из Notion по meeting_id"""
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"https://api.notion.com/v1/pages/{meeting_id}",
                    headers=headers
            ) as response:
                data = await response.json()
                # Извлекаем ID ментора из свойства 'Mentor(s)' и получаем его имя
                mentor_relation = data['properties']['Mentor(s)']['relation'][0]['id']
                mentor_name = await self.get_notion_page_name(mentor_relation)
                return mentor_name

    async def delete_telegram_message(self, chat_id, message_id):
        """Удаление сообщения в Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
        payload = {'chat_id': chat_id, 'message_id': message_id}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return await response.json()

    async def run_telegram_polling(self):
        """Polling для обновлений Telegram"""
        offset = 0
        while True:
            try:
                updates = await self.get_telegram_updates(offset)
                for update in updates:
                    offset = update['update_id'] + 1
                    if 'callback_query' in update:
                        await self.handle_callback_query(update['callback_query'])
                    elif 'message' in update:
                        message = update['message']
                        if 'text' in message and message['text'].strip() == '/chat_id@Feedback_Impact_bot':
                            chat_id = message['chat']['id']
                            await self.send_telegram_message(chat_id, f"ID чата: {chat_id}")
            except Exception as e:
                logger.error(f"Ошибка в run_telegram_polling: {e}")
                await asyncio.sleep(5)  # Задержка перед повторной попыткой

    async def get_telegram_updates(self, offset):
        """Получение обновлений от Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {'offset': offset, 'timeout': 30}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                data = await response.json()
                return data.get('result', [])

    async def get_meeting_summary(self, meeting_id):
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"https://api.notion.com/v1/pages/{meeting_id}",
                    headers=headers
            ) as response:
                data = await response.json()
                summary_property = data['properties'].get('Summary', {})
                if summary_property and summary_property['type'] == 'rich_text':
                    summary_text = ''.join([text['plain_text'] for text in summary_property['rich_text']])
                    return summary_text
                return None

if __name__ == "__main__":
    bot = FeedbackBot()
    asyncio.run(bot.start())