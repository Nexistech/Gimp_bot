#!/bin/bash
# Keep the Discord bot running. Intended for cron, e.g.:
#   */5 * * * * /home/strikebot/run_check.sh check >> /home/strikebot/watchdog.log 2>&1
#
# Usage: ./run_check.sh {start|stop|restart|status|check}

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="${BOT_DIR:-$SCRIPT_DIR}"
PID_FILE="${PID_FILE:-$BOT_DIR/bot.pid}"
BOT_SCRIPT="${BOT_SCRIPT:-bot.py}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
LOG_FILE="${LOG_FILE:-$BOT_DIR/bot.log}"

is_running() {
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
    if [ -f "$PID_FILE" ]; then
        tr -d '[:space:]' < "$PID_FILE"
    fi
}

start_bot() {
    local pid
    pid="$(read_pid)"
    if is_running "$pid"; then
        echo "Bot is already running (PID $pid)"
        return 0
    fi

    if [ -f "$PID_FILE" ]; then
        echo "Removing stale PID file..."
        rm -f "$PID_FILE"
    fi

    if [ ! -x "$PYTHON_BIN" ]; then
        echo "Python not found: $PYTHON_BIN"
        return 1
    fi
    if [ ! -f "$BOT_DIR/$BOT_SCRIPT" ]; then
        echo "Bot script not found: $BOT_DIR/$BOT_SCRIPT"
        return 1
    fi

    echo "Starting bot in $BOT_DIR ..."
    cd "$BOT_DIR" || return 1
    nohup "$PYTHON_BIN" "$BOT_SCRIPT" >>"$LOG_FILE" 2>&1 &

    sleep 2
    pid="$(read_pid)"
    if is_running "$pid"; then
        echo "Bot started (PID $pid). Logs: $LOG_FILE"
        return 0
    fi

    echo "Bot failed to start. Last log lines:"
    tail -n 30 "$LOG_FILE" 2>/dev/null || true
    return 1
}

stop_bot() {
    local pid
    pid="$(read_pid)"
    if ! is_running "$pid"; then
        echo "Bot is not running."
        rm -f "$PID_FILE"
        return 0
    fi

    echo "Stopping bot (PID $pid)..."
    kill "$pid"
    for _ in $(seq 1 10); do
        if ! is_running "$pid"; then
            break
        fi
        sleep 1
    done

    if is_running "$pid"; then
        echo "Bot did not stop gracefully. Sending SIGKILL..."
        kill -9 "$pid" >/dev/null 2>&1 || true
    fi

    rm -f "$PID_FILE"
    echo "Bot stopped."
}

status_bot() {
    local pid
    pid="$(read_pid)"
    if is_running "$pid"; then
        echo "Bot is running (PID $pid)"
        return 0
    fi
    if [ -f "$PID_FILE" ]; then
        echo "Bot is NOT running (stale PID file)"
        return 1
    fi
    echo "Bot is stopped."
    return 1
}

check_bot() {
    local pid
    pid="$(read_pid)"
    if is_running "$pid"; then
        return 0
    fi
    echo "Bot not running; attempting restart..."
    rm -f "$PID_FILE"
    start_bot
}

case "${1:-check}" in
    start) start_bot ;;
    stop) stop_bot ;;
    restart) stop_bot; start_bot ;;
    status) status_bot ;;
    check) check_bot ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|check}"
        exit 1
        ;;
esac
