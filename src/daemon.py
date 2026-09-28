#!/usr/bin/env python3
"""Захарыч — Telegram-демон на pi CLI.

Личность: ~/Documents/ai/AGENTS.md (грузит сам pi)
Память:   ~/Documents/ai/memory/ (общая с интерактивными сессиями)
Мозг:     pi --print --session-id zaharych-tg (именная сессия, контекст помнится)
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from locales import load as load_locale

ROOT = Path(__file__).resolve().parent.parent          # zaharych/
WORKDIR = ROOT.parent                                   # ~/Documents/ai (AGENTS.md + memory/)
CHAT_FILE = ROOT / "chat.id"
LOG_FILE = ROOT / "logs" / "daemon.log"
REMINDERS_FILE = ROOT / "reminders.json"  # [{"at":"17:00","text":"...","created":iso}]
INBOX = ROOT / "inbox"  # скачанные картинки, чистятся через сутки
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
T = load_locale(ENV.get("ZAHARYCH_LANG", "ru"))  # локализация: ru | en

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


# --- картинки ---

def tg_download(file_id: str) -> Path | None:
    """Скачать файл из телеги в inbox/. None при ошибке."""
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30)
        fp = (r.json().get("result") or {}).get("file_path")
        if not fp:
            log.warning("getFile не дал file_path")
            return None
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", timeout=120)
        r.raise_for_status()
        if len(r.content) > 10 * 1024 * 1024:
            log.warning("Файл слишком велик: %s байт", len(r.content))
            return None
        INBOX.mkdir(exist_ok=True)
        path = INBOX / f"{int(time.time())}_{Path(fp).name}"
        path.write_bytes(r.content)
        return path
    except (requests.RequestException, ValueError):
        log.exception("tg_download")
        return None


def clean_inbox() -> None:
    """Картинки старше суток — в утиль."""
    if not INBOX.exists():
        return
    cutoff = time.time() - 24 * 3600
    for f in INBOX.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


# --- мозг ---

# служебный протокол: pi назначает напоминания строками REMIND ЧЧ:ММ | текст
PI_PROTOCOL = T.PI_PROTOCOL

REMIND_RE = re.compile(r"^REMIND\s+(\d{1,2}):(\d{2})\s*\|\s*(.+)$", re.MULTILINE)
reminders_lock = threading.Lock()


def load_reminders() -> list[dict]:
    if REMINDERS_FILE.exists():
        try:
            return json.loads(REMINDERS_FILE.read_text())
        except Exception:
            log.exception("reminders.json битый")
    return []


def save_reminders(items: list[dict]) -> None:
    REMINDERS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1))


def add_reminder(hm: str, text: str) -> None:
    with reminders_lock:
        cur = load_reminders()
        if len(cur) >= 20:  # защита от плодовитости
            cur = cur[-19:]
        cur.append({"at": hm, "text": text,
                    "created": datetime.now(TZ).isoformat(timespec="minutes")})
        save_reminders(cur)
    log.info("Напоминание заведено: %s | %s", hm, text[:60])


def harvest_reminds(answer: str) -> str:
    """Вырезать из ответа pi строки REMIND и завести по ним таймеры.
    Вернуть чистый текст для отправки."""
    found: list[tuple[str, str]] = []

    def _cap(m: re.Match) -> str:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60 and m.group(3).strip():
            found.append((f"{hh:02d}:{mm:02d}", m.group(3).strip()))
        return ""

    clean = REMIND_RE.sub(_cap, answer)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    for at, txt in found:
        add_reminder(at, txt)
    return clean


def ask_pi(prompt: str, image: Path | None = None) -> str | None:
    """Спросить pi. None при любой ошибке — вызывающий решает про фолбэк."""
    with pi_lock:
        try:
            args = ["pi", "--print", "--session-id", SESSION_ID]
            if image:
                args.append(f"@{image}")  # картинка в контекст сессии
            args.append(prompt + PI_PROTOCOL)
            r = subprocess.run(
                args,
                cwd=WORKDIR, capture_output=True, text=True, timeout=PI_TIMEOUT,
            )
            if r.returncode != 0:
                log.error("pi rc=%s stderr=%s", r.returncode, r.stderr[-500:])
                return None
            out = r.stdout.strip()
            return harvest_reminds(out) if out else None
        except subprocess.TimeoutExpired:
            log.error("pi timeout (%s сек)", PI_TIMEOUT)
            return None
        except FileNotFoundError:
            log.error("pi не найден в PATH")
            return None


def reminders_loop() -> None:
    """Каждые 20 сек: что наступило — то отправить и снять с доски."""
    while True:
        try:
            hm = now_hm()
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            chat = load_chat()
            fired: list[str] = []
            with reminders_lock:
                left = []
                for it in load_reminders():
                    if chat and hm >= it["at"]:
                        tag = "" if it["created"][:10] == today else T.REMIND_LATE_TAG
                        fired.append(f"{T.REMIND_FIRED}{tag}: {it['text']}")
                        log.info("Напоминание сработало: %s | %s", it["at"], it["text"][:60])
                    else:
                        left.append(it)
                if fired:
                    save_reminders(left)
            if fired and chat:
                tg_send(chat, "\n\n".join(fired))
        except Exception:
            log.exception("reminders_loop")
        time.sleep(20)


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
    msg = ask_pi(T.INITIATIVE_TEMPLATE.format(date=date, hm=hm))
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
        tpl = T.DAY_PING_TEMPLATE if job["key"] == "day" else T.SLEEP_PING_TEMPLATE
        msg = ask_pi(tpl.format(date=date, hm=job["hm"]))
        if msg is None or msg.upper().startswith("SKIP"):
            pool = T.DAY_PINGS if job["key"] == "day" else T.SLEEP_PINGS
            msg = pool[int(time.time()) // 60 % len(pool)]
            log.warning("Живой пинг не вышел, послал статик")
    else:  # brief
        msg = ask_pi(T.BRIEF_TEMPLATE.format(date=date)) or T.BRIEF_FALLBACK
    tg_send(chat, msg)
    log.info("Отправлено [%s] %s", job["hm"], job["type"])


# --- обработка сообщений ---
def handle(msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    registered = load_chat()

    if not registered:  # первый, кто написал — хозяин
        save_chat(chat_id)
        tg_send(chat_id, T.WELCOME)
        return
    if chat_id != registered:
        tg_send(chat_id, T.STRANGER)
        return
    if text == "/ping":
        tg_send(chat_id, T.PONG)
        return
    m = re.match(T.REMIND_CMD_RE, text)
    if m:  # ручное напоминание: /напомни 17:00 ревью письма (en: /remind)
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60:
            add_reminder(f"{hh:02d}:{mm:02d}", m.group(3).strip())
            tg_send(chat_id, T.REMIND_ADDED.format(hm=f"{hh:02d}:{mm:02d}", text=m.group(3).strip()))
        else:
            tg_send(chat_id, T.REMIND_BAD_TIME)
        return
    if text in T.REMINDERS_CMD:
        with reminders_lock:
            cur = load_reminders()
        if not cur:
            tg_send(chat_id, T.REMIND_NONE)
        else:
            lines = "\n".join(f"{it['at']} — {it['text']}" for it in sorted(cur, key=lambda x: x["at"]))
            tg_send(chat_id, T.REMIND_LIST_HEADER + lines)
        return

    # фото/скрины: крупнейший размер, подпись — как текст сообщения
    file_id = None
    if msg.get("photo"):
        file_id = max(msg["photo"], key=lambda s: s.get("width", 0))["file_id"]
    elif str(msg.get("document", {}).get("mime_type", "")).startswith("image/"):
        file_id = msg["document"]["file_id"]
    if file_id:
        image = tg_download(file_id)
        clean_inbox()
        if not image:
            tg_send(chat_id, T.IMG_FAIL)
            return
        caption = (msg.get("caption") or "").strip()
        log.info("Космос прислал картинку: %s (подпись: %s)", image.name, caption[:60])
        prompt = caption or T.IMG_PROMPT
        respond(chat_id, prompt, image=image)
        return

    if not text:
        return

    log.info("Космос: %s", text[:100])
    respond(chat_id, text)


def respond(chat_id: str, prompt: str, image: Path | None = None) -> None:
    """Задать pi вопрос (с картинкой или без) и доставить ответ. Простыни жмём."""
    answer = ask_pi(prompt, image=image) or T.RESPOND_FAIL
    if len(answer) > 700 or answer.count("\n") > 8:  # простыня — жмём
        squeezed = ask_pi(T.SQUEEZE_PROMPT + answer)
        if squeezed and len(squeezed) < len(answer):
            answer = squeezed
            log.info("Ответ сжат повторным проходом")
    tg_send(chat_id, answer)
    log.info("Захарыч: %s", answer[:100])


def main() -> None:
    # одиночество демона: второй живой — уходит с кодом 42 (хранитель поймёт)
    pid_file = Path("/tmp/zaharych.daemon.pid")
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            if old != os.getpid() and Path(f"/proc/{old}").exists():
                log.error("Демон уже работает (pid %s) — второй не нужен.", old)
                raise SystemExit(42)
        except ValueError:
            pass
    pid_file.write_text(str(os.getpid()))

    if not TOKEN:
        raise SystemExit("Нет TG_BOT_TOKEN в .env — скопируй .env.example в .env и вставь токен.")
    log.info("Захарыч поднимается. workdir=%s session=%s", WORKDIR, SESSION_ID)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=initiative_loop, daemon=True).start()
    threading.Thread(target=reminders_loop, daemon=True).start()

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
