import logging
from requests import post, get

logger = logging.getLogger(__name__)


def send_notification(username, content, settings, proxy=""):
    """Send notification to all configured channels.

    Args:
        username: Apple ID username (prefixed to message)
        content: notification text
        settings: dict with keys tg_bot_token, tg_chat_id, wx_pusher_id, webhook_url
        proxy: optional proxy URL for requests
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    full_content = f"[{username}] {content}"

    # Telegram
    tg_token = settings.get("tg_bot_token", "")
    tg_chat = settings.get("tg_chat_id", "")
    if tg_token and tg_chat:
        try:
            post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_chat, "text": full_content},
                proxies=proxies,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")

    # WeChat pushplus
    wx_token = settings.get("wx_pusher_id", "")
    if wx_token:
        try:
            post(
                "http://www.pushplus.plus/send",
                data={"token": wx_token, "content": full_content},
                proxies=proxies,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"WeChat notification failed: {e}")

    # Webhook
    webhook_url = settings.get("webhook_url", "")
    if webhook_url:
        try:
            post(
                webhook_url,
                json={"username": username, "content": content},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Webhook notification failed: {e}")
