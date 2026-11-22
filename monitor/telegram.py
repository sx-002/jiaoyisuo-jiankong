"""Telegram 推送：只有 Bot API 返回 ok 才算成功。

失败保持未推送状态，下轮重试；当前仓库默认干跑，不接真实推送。
token 和 chat id 只读环境变量（GitHub Secrets），不写文件、不进日志。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 4000


class PushError(Exception):
    pass


def credentials() -> tuple:
    return os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
    if not token or not chat_id:
        raise PushError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise PushError("消息为空")
    payload = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API.format(token=token), data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        hint = ""
        if exc.code == 401:
            hint = "（bot token 无效或已被 BotFather 吊销，请重新复制）"
        elif exc.code == 403:
            hint = "（bot 无权访问该会话，通常是该聊天尚未启动过这个 bot）"
        raise PushError(f"Telegram HTTP {exc.code}{hint}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PushError(f"Telegram 请求失败: {type(exc).__name__}") from exc
    if not body.get("ok"):
        raise PushError(f"Telegram 返回失败: {str(body.get('description') or '')[:160]}")
    return True
