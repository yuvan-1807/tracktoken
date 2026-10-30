# Hospital Token Tracker

## 1. Run on your computer (Windows)

    cd "C:\path\to\hospital_token_app"
    python -m pip install -r requirements.txt
    copy .streamlit\secrets.example.toml .streamlit\secrets.toml

Open `.streamlit\secrets.toml` in Notepad and set your admin username and password (see section 2). Then:

    python -m streamlit run app.py

Open the address Streamlit prints (usually http://localhost:8501).

## 2. Admin account

The first admin is created from two secrets: `ADMIN_USERNAME` and `ADMIN_PASSWORD`. It is created once, on first start. Staff accounts are then created by the admin from the Admin screen.

Patients create their own accounts on the Patient sign in page.

Change the admin password by creating a new admin in the Admin screen and deleting the old one, or by deleting `hospital.db` and restarting (this also clears the queue).

## 3. Turn on real SMS (Twilio)

1. Create a Twilio account at twilio.com and verify your phone number.
2. In the Twilio Console, copy the **Account SID** and **Auth Token** from the dashboard.
3. Buy an SMS-capable phone number under Phone Numbers, and copy it in `+<country code><number>` format.
4. Put these in `secrets.toml` (or the cloud Secrets box):

       SMS_PROVIDER = "twilio"
       TWILIO_ACCOUNT_SID = "AC..."
       TWILIO_AUTH_TOKEN = "..."
       TWILIO_FROM_NUMBER = "+1..."

5. Restart the app. The SMS tab in Admin should show "Twilio (live SMS)".

In India, transactional SMS normally needs sender ID and message template registration (DLT) before delivery. Check with your provider first.

Without these settings, messages are logged as "simulated" and nothing is sent.

## Files

- app.py: screens and login for patient, staff and admin
- queue_engine.py: queue rules and the SMS messages they trigger
- auth.py: accounts, roles and hashed passwords
- sms.py: SMS sending (Twilio or simulated) and the SMS log
- config.py: reads settings from environment variables or secrets
- hospital.db: created automatically on first run (the database)
