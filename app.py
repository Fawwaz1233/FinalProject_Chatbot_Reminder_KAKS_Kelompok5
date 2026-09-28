from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import sqlite3
import threading
import time
import cohere
from dateutil.relativedelta import relativedelta
import json
import re
import os
import requests
import base64
from dotenv import load_dotenv

load_dotenv()


app = Flask(__name__)
CORS(app)


# Cohere Configuration
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)
co_v2 = cohere.ClientV2(cohere_api_key)


# Twilio Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Database Setup
def init_db():
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reminders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, frequency TEXT, start_date TEXT, end_date TEXT, time TEXT, user_number TEXT, created_at TEXT, note TEXT)''')
    try:
        c.execute('ALTER TABLE reminders ADD COLUMN created_at TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE reminders ADD COLUMN note TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def preprocess_relative_time(text, current_dt):
    match = re.search(r'in\s+(\d+)\s*(minute|min)s?', text, re.IGNORECASE)
    if match:
        target_dt = current_dt + timedelta(minutes=int(match.group(1)))
        return target_dt.strftime("%H:%M"), target_dt.strftime("%Y-%m-%d")
    match = re.search(r'in\s+(\d+)\s*(hour|hr)s?', text, re.IGNORECASE)
    if match:
        target_dt = current_dt + timedelta(hours=int(match.group(1)))
        return target_dt.strftime("%H:%M"), target_dt.strftime("%Y-%m-%d")
    return None, None

def preprocess_duration_and_frequency(text, current_dt):
    match = re.search(r'for\s+(\d+)\s*month', text, re.IGNORECASE)
    end_date = None
    if match:
        end_date = (current_dt + relativedelta(months=int(match.group(1)))).strftime("%Y-%m-%d")

    day_match = re.search(r'every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', text, re.IGNORECASE)
    frequency = "weekly" if day_match else None

    return end_date, frequency

def next_weekday_date(day_name, current_dt):
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    try:
        target = days.index(day_name.strip().lower())
    except ValueError:
        return current_dt.strftime("%Y-%m-%d")
    days_ahead = (target - current_dt.weekday()) % 7
    return (current_dt + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def extract_schedule_from_image(image_base64, current_dt):
    try:
        current_date = current_dt.strftime("%Y-%m-%d")
        prompt_text = (
            "This image contains a university class schedule table. "
            "Extract EVERY row/course as a separate item. For each course, extract: "
            "'task' (course name), 'day_of_week' (e.g. Monday), "
            "'time' (24-hour HH:MM, class start time), 'room' (room/location). "
            f"Today's date is {current_date}. "
            "Return ONLY a JSON array, no explanation, like: "
            '[{"task": "Course Name", "day_of_week": "Monday", "time": "07:30", "room": "Room 420"}]'
        )
        response = co_v2.chat(
            model="command-a-vision-07-2025",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                    ]
                }
            ],
            max_tokens=1500,
        )
        raw_text = getattr(response, "text", None)
        if not raw_text and hasattr(response, "message"):
            raw_text = response.message.content[0].text
        raw_text = raw_text.strip()
        print("Cohere vision response:", raw_text)

        json_match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
        json_text = json_match.group(1).strip() if json_match else raw_text
        courses = json.loads(json_text)
        return courses
    except Exception as e:
        print(f"Error extracting schedule from image: {e}")
        return None

# Extract Entities Using Cohere
def extract_entities(text):
    try:
        # Include current date and time in the prompt

        current_dt = datetime.now()
        current_date = current_dt.strftime("%Y-%m-%d")
        current_time = current_dt.strftime("%H:%M")
        prompt = (
            f"Extract task, frequency, start date, end date, time, and note from: '{text}'. "
            f"Today's date is {current_date}, and the current time is {current_time}. "
            "If not mentioned, set time to 9 AM, start date to today, end date to 9999-12-31, and note to empty string. "
            "The note is any extra detail mentioned besides the main task, date, and time. "
            "Handle relative dates like 'today', '2 years', 'this year', etc. Return as JSON."
        )
        
        response = co.chat(
            model="command-a-vision-07-2025",
            message=prompt,
            max_tokens=100,
        )
        raw_text = response.text.strip()
        print("Cohere response:", raw_text)


        # Extract JSON from triple backticks
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
        if json_match:
            json_text = json_match.group(1).strip()  # Extract JSON part only
        else:
            json_text = raw_text  # Fallback (if Cohere response is already plain JSON)


        # Load JSON
        reminder_data = json.loads(json_text)


        # Normalisasi nama key: dari camelCase ke snake_case

        key_mapping = {
            'startDate': 'start_date',
            'endDate': 'end_date',
            'start_date': 'start_date',
            'end_date': 'end_date',
            'task': 'task',
            'frequency': 'frequency',
            'time': 'time',
            'note': 'note',
        }
        normalized_data = {}
        for key, value in reminder_data.items():
            new_key = key_mapping.get(key, key)
            normalized_data[new_key] = value

        if 'note' not in normalized_data:
            normalized_data['note'] = ""

        print("Extracted entities (normalized):", normalized_data)

        forced_time, forced_date = preprocess_relative_time(text, current_dt)
        if forced_time:
            normalized_data['time'] = forced_time
            normalized_data['start_date'] = forced_date

        forced_end_date, forced_frequency = preprocess_duration_and_frequency(text, current_dt)
        if forced_end_date:
            normalized_data['end_date'] = forced_end_date
        if forced_frequency:
            normalized_data['frequency'] = forced_frequency

        return normalized_data


    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: {e}. Raw response: {raw_text}")
    except Exception as e:
        print(f"Error extracting entities: {e}")


    return None  # Return None if extraction fails


# Handle Incoming WhatsApp Messages
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    message = data.get('message', {})
    chat_id = message.get('chat', {}).get('id')

    if not chat_id:
        return jsonify({"ok": True})

    if 'photo' in message:
        try:
            file_id = message['photo'][-1]['file_id']
            file_info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile", params={"file_id": file_id}).json()
            file_path = file_info['result']['file_path']
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
            image_bytes = requests.get(file_url).content
            image_base64 = base64.b64encode(image_bytes).decode('utf-8')

            current_dt = datetime.now()
            courses = extract_schedule_from_image(image_base64, current_dt)

            if not courses:
                send_whatsapp_message(chat_id, "Sorry, I couldn't read the schedule from that image. Please try a clearer photo.")
                return jsonify({"ok": True})

            end_date = (current_dt + relativedelta(months=6)).strftime("%Y-%m-%d")
            created = []
            conn = sqlite3.connect('reminders.db')
            c = conn.cursor()
            for course in courses:
                task = (course.get('task') or '').strip()
                day_name = (course.get('day_of_week') or '').strip()
                time_val = (course.get('time') or '09:00').strip()
                room = course.get('room') or ''
                if not task or not day_name:
                    continue
                start_date = next_weekday_date(day_name, current_dt)
                note = f"Room: {room}" if room else ""
                c.execute('INSERT INTO reminders (task, frequency, start_date, end_date, time, user_number, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (task, "weekly", start_date, end_date, time_val, chat_id, current_dt.strftime('%Y-%m-%d'), note))
                created.append(f"{len(created) + 1}. {task} ({day_name} {time_val})")
            conn.commit()
            conn.close()

            if created:
                send_whatsapp_message(chat_id, f"Created {len(created)} weekly reminders for 6 months:\n" + "\n".join(created))
            else:
                send_whatsapp_message(chat_id, "Sorry, I couldn't extract any valid course from that image.")
        except Exception as e:
            print(f"Error processing schedule image: {e}")
            send_whatsapp_message(chat_id, "Sorry, something went wrong while processing the image.")
        return jsonify({"ok": True})

    if 'text' not in message:
        send_whatsapp_message(chat_id, "Sorry, I can only understand text messages or schedule photos right now.")
        return jsonify({"ok": True})

    user_message = message['text'].strip().lower()


    # Handle Greetings
    if user_message in ["hi", "hello", "hey"]:
        response_message = "Hello! How can I assist you today?"
        send_whatsapp_message(chat_id, response_message)
    elif "thank you" in user_message or "thanks" in user_message:
        response_message = "You're welcome! Let me know if you need anything else."
        send_whatsapp_message(chat_id, response_message)
    elif "list all reminders" in user_message or "give me all reminders" in user_message:
        response_message = list_all_reminders(chat_id)
        send_whatsapp_message(chat_id, response_message)
    elif "delete all" in user_message:
        response_message = delete_all_reminders(chat_id)
        send_whatsapp_message(chat_id, response_message)
    elif user_message.startswith("delete"):
        task_to_delete = user_message.replace("delete", "").strip()
        response_message = delete_reminder(chat_id, task_to_delete)
        send_whatsapp_message(chat_id, response_message)
    elif user_message.startswith("update"):
        task_to_update = user_message.replace("update", "").strip()
        response_message, new_reminder = update_reminder(chat_id, task_to_update)
        if new_reminder:
            send_reminder_confirmation(chat_id, new_reminder['task'], new_reminder['time'], new_reminder['start_date'], new_reminder.get('note', ''))
        else:
            send_whatsapp_message(chat_id, response_message)
    # Handle Reminder Creation
    else:
        reminder = extract_entities(user_message)
        if not reminder:
            response_message = "Sorry, I couldn't understand your reminder. Please try again."
            send_whatsapp_message(chat_id, response_message)
        else:
            conn = sqlite3.connect('reminders.db')
            c = conn.cursor()
            c.execute('INSERT INTO reminders (task, frequency, start_date, end_date, time, user_number, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (reminder['task'], reminder['frequency'], reminder['start_date'], reminder['end_date'], reminder['time'], chat_id, datetime.now().strftime('%Y-%m-%d'), reminder.get('note', '')))
            conn.commit()
            conn.close()

            send_reminder_confirmation(chat_id, reminder['task'], reminder['time'], reminder['start_date'], reminder.get('note', ''))

    return jsonify({"ok": True})


# List All Reminders
def list_all_reminders(user_number):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('SELECT * FROM reminders WHERE user_number = ?', (user_number,))
    reminders = c.fetchall()
    conn.close()

    if not reminders:
        return "You have no reminders set."


    header = f"{'No.':<4}{'Created':<12}{'Activity':<20}{'Deadline':<20}{'Note':<15}\n"
    separator = "-" * 71 + "\n"
    rows = ""
    for idx, reminder in enumerate(reminders, start=1):
        created = reminder[7] if reminder[7] else "-"
        activity = reminder[1][:18]
        deadline = f"{reminder[5]} on {reminder[3]}"
        note = reminder[8][:13] if len(reminder) > 8 and reminder[8] else "-"
        rows += f"{idx:<4}{created:<12}{activity:<20}{deadline:<20}{note:<15}\n"

    table = header + separator + rows
    return f"```\n{table}```"


# Delete Reminder
def delete_reminder(user_number, task_to_delete):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('DELETE FROM reminders WHERE user_number = ? AND task LIKE ?', (user_number, f"%{task_to_delete}%"))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    if rows_affected == 0:
        return f"No reminder found matching '{task_to_delete}'."
    return f"Reminder for '{task_to_delete}' has been deleted."

def delete_all_reminders(user_number):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('DELETE FROM reminders WHERE user_number = ?', (user_number,))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()
    if rows_affected == 0:
        return "You have no reminders to delete."
    return f"All {rows_affected} reminders have been deleted."

# Update Reminder
def update_reminder(user_number, task_to_update):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('SELECT * FROM reminders WHERE user_number = ? AND task LIKE ?', (user_number, f"%{task_to_update}%"))
    reminder = c.fetchone()
    if not reminder:
        conn.close()
        return f"No reminder found for '{task_to_update}'.", None


    # Extract new details using Cohere
    new_reminder = extract_entities(task_to_update)
    if not new_reminder:
        conn.close()
        return "Sorry, I couldn't understand the update. Please try again.", None


    # Update reminder in database
    c.execute('UPDATE reminders SET task = ?, frequency = ?, start_date = ?, end_date = ?, time = ?, note = ? WHERE id = ?',
          (new_reminder['task'], new_reminder['frequency'], new_reminder['start_date'], new_reminder['end_date'], new_reminder['time'], new_reminder.get('note', ''), reminder[0]))

    conn.commit()
    conn.close()
    return "updated", new_reminder


# Send WhatsApp Message
def send_reminder_confirmation(to, task, time_val, date_val, note=""):
    try:
        task_display = task[0].upper() + task[1:] if task else task
        message_body = f"Reminder set: *{task_display}* at *{time_val} on {date_val}*."
        if note:
            message_body += f"\nNote: {note}"
        send_whatsapp_message(to, message_body)
    except Exception as e:
        print(f"Error sending confirmation: {e}")


def send_whatsapp_message(to, message):
    try:
        def send_telegram_message(chat_id, message):
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})

        send_telegram_message(to, message)

    except Exception as e:
        print(f"Error sending message: {e}")


# Check Reminders and Send Notifications
def check_reminders():
    while True:
        try:
            conn = sqlite3.connect('reminders.db')
            c = conn.cursor()
            c.execute('SELECT * FROM reminders')
            reminders = c.fetchall()
            conn.close()


            current_time = datetime.now().strftime('%H:%M')
            today = datetime.now().strftime('%Y-%m-%d')


            for reminder in reminders:
                r_id, task, frequency, start_date, end_date, r_time, user_number, created_at, note = reminder

                if today < start_date or today > end_date:
                    continue
                if today == start_date and r_time > current_time:
                    continue


                send_whatsapp_notification(reminder)

                conn2 = sqlite3.connect('reminders.db')
                c2 = conn2.cursor()
                if frequency == "once" or start_date == end_date:
                    c2.execute('DELETE FROM reminders WHERE id = ?', (r_id,))


                else:
                    step_days = 7 if frequency == "weekly" else 1
                    next_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=step_days)).strftime("%Y-%m-%d")
                    c2.execute('UPDATE reminders SET start_date = ? WHERE id = ?', (next_date, r_id))
                conn2.commit()
                conn2.close()

        except Exception as e:
            print(f"Error in check_reminders loop: {e}")


        time.sleep(60)


# Send WhatsApp Notification
def send_whatsapp_notification(reminder):
    note_text = reminder[8] if len(reminder) > 8 and reminder[8] else None
    task_display = reminder[1][0].upper() + reminder[1][1:] if reminder[1] else reminder[1]
    try:
        prompt = (
            f"We are sending a reminder to the user for this task: {reminder[1]}. "
            "Generate a short, warm, natural 1-sentence reminder message. "
            "Do not mention any specific date, time, or schedule in the message. "
            "Avoid the word 'today' — if a time reference is needed, use 'now' instead. "
            "Do not include any extra notes or details — just a friendly nudge about the task itself. "
            "Return only the message text, no additional commentary."
        )
        response = co.chat(model="command-a-vision-07-2025", message=prompt, max_tokens=50)
        ai_message = response.text.strip()
        ai_message = re.sub(r'\btoday\b', 'now', ai_message, flags=re.IGNORECASE)
        message = f"*{task_display}* — {ai_message}"
        if note_text:
            message += f" *{note_text}*"
    except Exception as e:
        print(f"Error generating notification: {e}")
        message = f"Reminder: *{task_display}*"
        if note_text:
            message += f" *{note_text}*"
    send_whatsapp_message(reminder[6], message)


# Run the reminder checker in a separate thread
threading.Thread(target=check_reminders, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, threaded=True)