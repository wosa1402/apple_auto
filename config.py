import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/appleid.db")
    WEBDRIVER_URL = os.environ.get("WEBDRIVER_URL", "local")
    HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
    LANG = os.environ.get("LANG", "zh_cn")
    DATA_DIR = os.environ.get("DATA_DIR", "data")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", "5000"))
