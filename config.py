import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class Config:
    PORT = int(os.getenv("PORT", 7860))
    ADDON_URL = os.getenv("ADDON_URL", f"http://localhost:{PORT}").rstrip("/")
    API_KEY = os.getenv("API_KEY", "")
    CACHE_TTL = int(os.getenv("CACHE_TTL", 1800))
    TIMEZONE = os.getenv("TIMEZONE", "UTC")

    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")

    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
    LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
    PARALLEL_CONNECTIONS = int(os.getenv("PARALLEL_CONNECTIONS", 3))

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN and not cls.USER_SESSION_STRING:
            missing.append("BOT_TOKEN or USER_SESSION_STRING")
        if missing:
            raise ValueError(
                f"Missing critical configuration variables: {', '.join(missing)}. "
                "Please configure them in your environment or a .env file."
            )

        try:
            cls.API_ID = int(cls.API_ID)
        except (ValueError, TypeError):
            raise ValueError("API_ID must be a valid integer.")

        if cls.TELEGRAM_CHANNEL_ID and isinstance(cls.TELEGRAM_CHANNEL_ID, str):
            val = cls.TELEGRAM_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.TELEGRAM_CHANNEL_ID = int(val)
                except ValueError:
                    pass

        if cls.LOG_CHANNEL_ID and isinstance(cls.LOG_CHANNEL_ID, str):
            val = cls.LOG_CHANNEL_ID.strip()
            if val.startswith("-") or val.isdigit():
                try:
                    cls.LOG_CHANNEL_ID = int(val)
                except ValueError:
                    pass
