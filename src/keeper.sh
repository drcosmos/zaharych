#!/bin/sh
# Хранитель Захарыча: держит демона живым. Ровно ОДИН на машину — дубликаты выходят сами.
# Стоп: touch /tmp/zaharych.stop && pkill -f daemon.py
LOG="$HOME/Documents/ai/zaharych/logs/daemon.log"
STOP=/tmp/zaharych.stop
LOCK=/tmp/zaharych.keeper.lock

# --- одиночество хранителя ---
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "$(date '+%F %T') INFO хранитель: дубликат, уже работает pid=$(cat "$LOCK") — выхожу" >> "$LOG"
    exit 0
fi
echo $$ > "$LOCK"

while :; do
    if [ -f "$STOP" ]; then
        echo "$(date '+%F %T') INFO хранитель: стоп-файл найден, ухожу" >> "$LOG"
        rm -f "$STOP" "$LOCK"
        exit 0
    fi
    echo "$(date '+%F %T') INFO хранитель: поднимаю демона" >> "$LOG"
    python3 "$HOME/Documents/ai/zaharych/src/daemon.py"
    rc=$?
    if [ "$rc" -eq 42 ]; then
        # демон сам нашёл живого брата — значит, где-то трудится другой хранитель
        echo "$(date '+%F %T') INFO хранитель: демон сообщил о дубликате (rc=42), выхожу" >> "$LOG"
        rm -f "$LOCK"
        exit 0
    fi
    echo "$(date '+%F %T') WARNING хранитель: демон умер (rc=$rc), рестарт через 5 сек" >> "$LOG"
    sleep 5
done
