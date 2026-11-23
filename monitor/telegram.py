"""Telegram 推送：只有 Bot API 返回 ok 才算成功。

失败保持未推送状态，下轮重试；当前仓库默认干跑，不接真实推送。
token 和 chat id 只读环境变量（GitHub Secrets），不写文件、不进日志。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 4000


class PushError(Exception):
    pass


def credentials() -> tuple:
    return os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def send_message(*args, **kwargs) -> bool:
    """带重试的推送：只重试这一条消息，不重跑整轮。

    401/403 属于配置错误（token 无效、聊天未授权），重试没有意义，直接抛出；
    网络异常、429、5xx 这类临时故障才重试，重试仍失败就抛错，
    由上层写进 pending 队列，下一轮补推。
    """
    attempts = 3
    try:
        attempts = max(1, int(os.environ.get("TELEGRAM_RETRY_ATTEMPTS", "3") or "3"))
    except ValueError:
        attempts = 3
    delays = (1, 3)
    raw_delays = (os.environ.get("TELEGRAM_RETRY_DELAYS") or "").strip()
    if raw_delays:
        try:
            delays = tuple(max(0, int(x)) for x in raw_delays.split(",") if x.strip()) or delays
        except ValueError:
            delays = (1, 3)
    last_exc: Optional[PushError] = None
    for attempt in range(attempts):
        try:
            return _send_once(*args, **kwargs)
        except PushError as exc:
            last_exc = exc
            detail = str(exc)
            if "401" in detail or "403" in detail:
                raise
            if attempt + 1 >= attempts:
                break
            wait = delays[min(attempt, len(delays) - 1)]
            print(f"Telegram 推送失败，{wait} 秒后重试（{attempt + 1}/{attempts - 1}）：{detail}",
                  file=sys.stderr)
            time.sleep(wait)
    raise last_exc


def _send_once(token: str, chat_id: str, text: str, *, timeout: float = 20.0) -> bool:
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
