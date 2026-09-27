#!/bin/sh
# Хранитель Захарыча: держит демона живым.
# Стоп: touch /tmp/zaharych.stop && pkill -f daemon.py
LOG="$HOME/Documents/ai/zaharych/logs/daemon.log"
STOP=/tmp/zaharych.stop
while :; do
    if [ -f "$STOP" ]; then
        echo "$(date '+%F %T') INFO хранитель: стоп-файл найден, ухожу" >> "$LOG"
        rm -f "$STOP"
        exit 0
    fi
    echo "$(date '+%F %T') INFO хранитель: поднимаю демона" >> "$LOG"
    python3 "$HOME/Documents/ai/zaharych/src/daemon.py"
    echo "$(date '+%F %T') WARNING хранитель: демон умер (rc=$?), рестарт через 5 сек" >> "$LOG"
    sleep 5
done
