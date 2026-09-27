from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import sqlite3
import threading
import time
import cohere
from dateutil.relativedelta import relativedelta
import json
import re
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# Cohere Configuration
cohere_api_key = os.getenv("COHERE_API_KEY")
co = cohere.Client(cohere_api_key)

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
client_twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Database Setup
def init_db():
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reminders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, frequency TEXT, start_date TEXT, end_date TEXT, time TEXT, user_number TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Extract Entities Using Cohere
def extract_entities(text):
    try:
        # Include current date and time in the prompt
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_time = datetime.now().strftime("%H:%M")
        prompt = (
            f"Extract task, frequency, start date, end date, and time from: '{text}'. "
            f"Today's date is {current_date}, and the current time is {current_time}. "
            "If not mentioned, set time to 9 AM, start date to today, and end date to 9999-12-31. "
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
        }
        normalized_data = {}
        for key, value in reminder_data.items():
            new_key = key_mapping.get(key, key)
            normalized_data[new_key] = value

        print("Extracted entities (normalized):", normalized_data)

        return normalized_data

    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: {e}. Raw response: {raw_text}")
    except Exception as e:
        print(f"Error extracting entities: {e}")

    return None  # Return None if extraction fails

# Handle Incoming WhatsApp Messages
@app.route('/webhook', methods=['POST'])
def webhook():
    user_number = request.form['From']
    user_message = request.form['Body'].strip().lower()

    # Handle Greetings

    if user_message in ["hi", "hello", "hey"]:
        response_message = "Hello! How can I assist you today?"
        send_whatsapp_message(user_number, response_message)
    elif "thank you" in user_message or "thanks" in user_message:
        response_message = "You're welcome! Let me know if you need anything else."
        send_whatsapp_message(user_number, response_message)
    elif "list all reminders" in user_message or "give me all reminders" in user_message:
        response_message = list_all_reminders(user_number)
        send_whatsapp_message(user_number, response_message)
    elif user_message.startswith("delete"):
        task_to_delete = user_message.replace("delete", "").strip()
        response_message = delete_reminder(user_number, task_to_delete)
        send_whatsapp_message(user_number, response_message)

    elif user_message.startswith("update"):
        task_to_update = user_message.replace("update", "").strip()
        response_message, new_reminder = update_reminder(user_number, task_to_update)
        if new_reminder:
            send_reminder_confirmation(user_number, new_reminder['task'], new_reminder['time'], new_reminder['start_date'])
        else:
            send_whatsapp_message(user_number, response_message)
    # Handle Reminder Creation
    else:
        reminder = extract_entities(user_message)
        if not reminder:
            response_message = "Sorry, I couldn't understand your reminder. Please try again."
            send_whatsapp_message(user_number, response_message)
        else:
            conn = sqlite3.connect('reminders.db')
            c = conn.cursor()
            c.execute('INSERT INTO reminders (task, frequency, start_date, end_date, time, user_number) VALUES (?, ?, ?, ?, ?, ?)',
                      (reminder['task'], reminder['frequency'], reminder['start_date'], reminder['end_date'], reminder['time'], user_number))
            conn.commit()
            conn.close()

            send_reminder_confirmation(user_number, reminder['task'], reminder['time'], reminder['start_date'])

    return str(MessagingResponse())

# List All Reminders
def list_all_reminders(user_number):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('SELECT * FROM reminders WHERE user_number = ?', (user_number,))
    reminders = c.fetchall()
    conn.close()

    if not reminders:
        return "You have no reminders set."

    reminder_list = "\n".join([f"{reminder[1]} at {reminder[5]} on {reminder[3]}" for reminder in reminders])
    return f"Your reminders:\n{reminder_list}"

# Delete Reminder
def delete_reminder(user_number, task_to_delete):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute('DELETE FROM reminders WHERE user_number = ? AND task LIKE ?', (user_number, f"%{task_to_delete}%"))
    conn.commit()
    conn.close()
    return f"Reminder for '{task_to_delete}' has been deleted."

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
    c.execute('UPDATE reminders SET task = ?, frequency = ?, start_date = ?, end_date = ?, time = ? WHERE id = ?',
              (new_reminder['task'], new_reminder['frequency'], new_reminder['start_date'], new_reminder['end_date'], new_reminder['time'], reminder[0]))
    conn.commit()
    conn.close()
    return "updated", new_reminder

# Send WhatsApp Message
CONFIRMATION_CONTENT_SID = os.getenv("CONFIRMATION_CONTENT_SID")
print(f"DEBUG - Content SID yang terbaca: '{CONFIRMATION_CONTENT_SID}'")

def send_reminder_confirmation(to, task, time_val, date_val):
    try:
        client_twilio.messages.create(
            content_sid=CONFIRMATION_CONTENT_SID,
            content_variables=json.dumps({"1": task, "2": time_val, "3": date_val}),
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to
        )
    except Exception as e:
        print(f"Error sending confirmation: {e}")

def send_whatsapp_message(to, message):
    try:
        client_twilio.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to
        )
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
                r_id, task, frequency, start_date, end_date, r_time, user_number = reminder
                if r_time != current_time:
                    continue
                if today < start_date or today > end_date:
                    continue

                send_whatsapp_notification(reminder)

                if frequency == "once":
                    conn2 = sqlite3.connect('reminders.db')
                    c2 = conn2.cursor()
                    c2.execute('DELETE FROM reminders WHERE id = ?', (r_id,))
                    conn2.commit()
                    conn2.close()
        except Exception as e:
            print(f"Error in check_reminders loop: {e}")

        time.sleep(60)

# Send WhatsApp Notification
def send_whatsapp_notification(reminder):
    try:
        prompt = (
            f"We are sending a reminder to the user. The reminder details are: "
            f"Task: {reminder[1]}, Time: {reminder[5]}, Date: {reminder[3]}. "
            "Generate a friendly and human-like reminder message without any additional text."
        )
        response = co.chat(
            model="command-a-vision-07-2025",
            message=prompt,
            max_tokens=50,
        )
        message = response.text.strip()
    except Exception as e:
        print(f"Error generating notification: {e}")
        message = f"Reminder: {reminder[1]} at {reminder[5]}."

    send_whatsapp_message(reminder[6], message)

# Run the reminder checker in a separate thread
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    threading.Thread(target=check_reminders, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True)