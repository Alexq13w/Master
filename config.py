import os

TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
PAYMENT_NUMBER = os.environ.get("PAYMENT_NUMBER")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
PAYMENT_ALERTS_CHANNEL = int(os.environ.get("PAYMENT_ALERTS_CHANNEL", 0))
SUBSCRIBE_CHANNEL_ID = int(os.environ.get("SUBSCRIBE_CHANNEL_ID", 0))
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME")

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", 6432))
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DATABASE_URL = os.environ.get("DATABASE_URL")
