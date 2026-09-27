#!/usr/bin/env python3
"""Захарыч — Telegram-демон на pi CLI.

Личность: ~/Documents/ai/AGENTS.md (грузит сам pi)
Память:   ~/Documents/ai/memory/ (общая с интерактивными сессиями)
Мозг:     pi --print --session-id zaharych-tg (именная сессия, контекст помнится)
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from pings import (BRIEF_TEMPLATE, DAY_PING_TEMPLATE, DAY_PINGS,
                   INITIATIVE_TEMPLATE, SLEEP_PING_TEMPLATE, SLEEP_PINGS)

ROOT = Path(__file__).resolve().parent.parent          # zaharych/
WORKDIR = ROOT.parent                                   # ~/Documents/ai (AGENTS.md + memory/)
CHAT_FILE = ROOT / "chat.id"
LOG_FILE = ROOT / "logs" / "daemon.log"
SESSION_ID = "zaharych-tg-v2"  # v2: чистый контекст под новый голос (v1 — архив в sessions/)
PI_TIMEOUT = 600  # сек на размышление

# --- .env (простейший парсер, без зависимостей) ---
ENV = {}
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            ENV[k.strip()] = v.strip()

TOKEN = ENV.get("TG_BOT_TOKEN", "")
TZ = ZoneInfo(ENV.get("ZAHARYCH_TZ", "Europe/Minsk"))
API = f"https://api.telegram.org/bot{TOKEN}"

# --- логи ---
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("zaharych")

pi_lock = threading.Lock()  # pi — один за раз


def now_hm() -> str:
    return datetime.now(TZ).strftime("%H:%M")


# --- chat id ---
def load_chat() -> str | None:
    return CHAT_FILE.read_text().strip() if CHAT_FILE.exists() else None


def save_chat(chat_id: str) -> None:
    CHAT_FILE.write_text(chat_id)
    log.info("Зарегистрирован chat_id=%s", chat_id)


# --- telegram ---
def tg_send(chat_id: str, text: str) -> None:
    for chunk_start in range(0, len(text), 4000):  # лимит телеги 4096
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text[chunk_start:chunk_start + 4000]},
            timeout=30,
        )


def tg_get_updates(offset: int) -> list[dict]:
    try:
        r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60)
        return r.json().get("result", [])
    except requests.RequestException as e:
        log.warning("tg_get_updates: %s", e)
        time.sleep(5)
        return []


# --- мозг ---
def ask_pi(prompt: str) -> str | None:
    """Спросить pi. None при любой ошибке — вызывающий решает про фолбэк."""
    with pi_lock:
        try:
            r = subprocess.run(
                ["pi", "--print", "--session-id", SESSION_ID, prompt],
                cwd=WORKDIR, capture_output=True, text=True, timeout=PI_TIMEOUT,
            )
            if r.returncode != 0:
                log.error("pi rc=%s stderr=%s", r.returncode, r.stderr[-500:])
                return None
            return r.stdout.strip() or None
        except subprocess.TimeoutExpired:
            log.error("pi timeout (%s сек)", PI_TIMEOUT)
            return None
        except FileNotFoundError:
            log.error("pi не найден в PATH")
            return None


# --- планировщик ---
SCHEDULE = [
    {"hm": "07:00", "type": "brief"},
    {"hm": "12:00", "type": "ping", "key": "day"},
    {"hm": "21:30", "type": "ping", "key": "sleep"},
]
_last_hm = ""

# инициативы: тихие проверки «стоит ли написать?» вне расписания
INIT_EVERY_MIN = 120          # попытка раз в 2 часа
INIT_MAX_PER_DAY = 2          # не больше 2 попыток в сутки
INIT_QUIET = ("22:30", "07:00")  # тихие часы (через полночь)
_init_state = {"day": "", "count": 0}


def in_quiet(hm: str) -> bool:
    start, end = INIT_QUIET
    return hm >= start or hm < end


def initiative_loop() -> None:
    while True:
        try:
            hm = now_hm()
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            if _init_state["day"] != today:
                _init_state.update(day=today, count=0)
            slot = int(time.time()) // 60
            if (slot % INIT_EVERY_MIN == 0 and not in_quiet(hm)
                    and _init_state["count"] < INIT_MAX_PER_DAY):
                initiative_attempt(hm)
        except Exception:
            log.exception("initiative_loop")
        time.sleep(60)


def initiative_attempt(hm: str) -> None:
    _init_state["count"] += 1
    date = datetime.now(TZ).strftime("%d.%m.%Y, %A")
    msg = ask_pi(INITIATIVE_TEMPLATE.format(date=date, hm=hm))
    if msg is None or msg.upper().startswith("SKIP"):
        log.info("Инициатива #%s: SKIP", _init_state["count"])
        return
    chat = load_chat()
    if not chat:
        return
    tg_send(chat, msg)
    log.info("Инициатива #%s отправлена: %s", _init_state["count"], msg[:80])


def scheduler_loop() -> None:
    global _last_hm
    while True:
        hm = now_hm()
        if hm != _last_hm:
            _last_hm = hm
            for job in SCHEDULE:
                if job["hm"] == hm:
                    fire(job)
        time.sleep(15)


def fire(job: dict) -> None:
    chat = load_chat()
    if not chat:
        log.info("Пинг %s пропущен: чат не зарегистрирован", job["hm"])
        return
    date = datetime.now(TZ).strftime("%d.%m.%Y, %A")
    if job["type"] == "ping":
        tpl = DAY_PING_TEMPLATE if job["key"] == "day" else SLEEP_PING_TEMPLATE
        msg = ask_pi(tpl.format(date=date, hm=job["hm"]))
        if msg is None or msg.upper().startswith("SKIP"):
            pool = DAY_PINGS if job["key"] == "day" else SLEEP_PINGS
            msg = pool[int(time.time()) // 60 % len(pool)]
            log.warning("Живой пинг не вышел, послал статик")
    else:  # brief
        msg = ask_pi(BRIEF_TEMPLATE.format(date=date)) or (
            "Бриф не вышел — техническая заминка. Но задание известно без брифа: аутрич. Ход за тобой.")
    tg_send(chat, msg)
    log.info("Отправлено [%s] %s", job["hm"], job["type"])


# --- обработка сообщений ---
def handle(msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text:
        return
    registered = load_chat()

    if not registered:  # первый, кто написал — хозяин
        save_chat(chat_id)
        tg_send(chat_id, "Космос. Это Захарыч — отныне я при тебе. Распорядок: бриф в 7:00, проверка в 12:00, отбой в 21:30. Пиши, спрашивай, оправдывайся. Ход за тобой.")
        return
    if chat_id != registered:
        tg_send(chat_id, "Захарыч разговаривает только с Космосом. Ничего личного.")
        return
    if text == "/ping":
        tg_send(chat_id, "На связи. Служу.")
        return

    log.info("Космос: %s", text[:100])
    answer = ask_pi(text) or "Космос, техническая заминка — ответа не вышло. Глянь logs/daemon.log."
    tg_send(chat_id, answer)
    log.info("Захарыч: %s", answer[:100])


def main() -> None:
    if not TOKEN:
        raise SystemExit("Нет TG_BOT_TOKEN в .env — скопируй .env.example в .env и вставь токен.")
    log.info("Захарыч поднимается. workdir=%s session=%s", WORKDIR, SESSION_ID)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=initiative_loop, daemon=True).start()

    offset = 0
    while True:
        for upd in tg_get_updates(offset):
            offset = upd["update_id"] + 1
            if "message" in upd:
                try:
                    handle(upd["message"])
                except Exception:
                    log.exception("handle error")


if __name__ == "__main__":
    main()
