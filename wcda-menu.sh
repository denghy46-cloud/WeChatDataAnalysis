#!/bin/bash

# WCDA 本地运行管理菜单（兼容 macOS 自带的 Bash 3.2）

set -u

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$SCRIPT_DIR/desktop"
DEV_SCRIPT="$DESKTOP_DIR/scripts/dev.cjs"
RUNTIME_DIR="$SCRIPT_DIR/.wcda-runtime"
PID_FILE="$RUNTIME_DIR/wcda-dev.pid"
LOG_FILE="$RUNTIME_DIR/wcda-dev.log"
HEALTH_URL="http://127.0.0.1:10392/api/health"

if [ -t 1 ]; then
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  RED='\033[0;31m'
  CYAN='\033[0;36m'
  RESET='\033[0m'
else
  GREEN=''
  YELLOW=''
  RED=''
  CYAN=''
  RESET=''
fi

ensure_runtime_dir() {
  mkdir -p "$RUNTIME_DIR"
  touch "$LOG_FILE"
}

read_pid_file() {
  local pid

  [ -f "$PID_FILE" ] || return 1
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$pid"
}

is_wcda_pid() {
  local pid="$1"
  local command_line

  kill -0 "$pid" 2>/dev/null || return 1
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command_line" in
    *"$DEV_SCRIPT"*) return 0 ;;
    *) return 1 ;;
  esac
}

managed_pid() {
  local pid

  pid="$(read_pid_file 2>/dev/null || true)"
  if [ -n "$pid" ] && is_wcda_pid "$pid"; then
    printf '%s\n' "$pid"
    return 0
  fi

  if [ -f "$PID_FILE" ]; then
    unlink "$PID_FILE" 2>/dev/null || true
  fi
  return 1
}

port_in_use() {
  nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

wait_for_startup() {
  local pid="$1"
  local count=0

  while [ "$count" -lt 40 ]; do
    if ! is_wcda_pid "$pid"; then
      return 1
    fi
    if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
    count=$((count + 1))
  done

  # 后端健康检查超时，但主进程仍存活时保留进程，方便通过日志诊断。
  is_wcda_pid "$pid"
}

start_wcda() {
  local pid
  local started_pid

  ensure_runtime_dir

  pid="$(managed_pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    printf "${YELLOW}WCDA 已在运行，PID：%s${RESET}\n" "$pid"
    printf '访问地址：http://127.0.0.1:3000\n'
    return 0
  fi

  if [ ! -f "$DEV_SCRIPT" ] || [ ! -f "$DESKTOP_DIR/package.json" ]; then
    printf "${RED}启动失败：没有找到 desktop/scripts/dev.cjs 或 desktop/package.json。${RESET}\n"
    return 1
  fi

  if ! command -v node >/dev/null 2>&1; then
    printf "${RED}启动失败：没有找到 Node.js。${RESET}\n"
    return 1
  fi

  # 固定端口被其他进程占用时拒绝重复启动，防止出现两个 WCDA 实例。
  if port_in_use 3000 || port_in_use 10392; then
    printf "${RED}启动失败：端口 3000 或 10392 已被占用。${RESET}\n"
    printf '如果 WCDA 已由其他终端启动，请先在那个终端停止它，再从本菜单启动。\n'
    return 1
  fi

  {
    printf '\n============================================================\n'
    printf '[%s] 启动 WCDA\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$LOG_FILE"

  (
    cd "$DESKTOP_DIR" || exit 1
    nohup node "$DEV_SCRIPT" >> "$LOG_FILE" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$PID_FILE.tmp"
    mv "$PID_FILE.tmp" "$PID_FILE"
  )

  started_pid="$(read_pid_file 2>/dev/null || true)"
  if [ -z "$started_pid" ]; then
    printf "${RED}启动失败：没有写入进程 PID。${RESET}\n"
    return 1
  fi

  printf '正在启动 WCDA（PID：%s）...\n' "$started_pid"
  if wait_for_startup "$started_pid"; then
    if curl -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
      printf "${GREEN}WCDA 启动成功。${RESET}\n"
    else
      printf "${YELLOW}WCDA 主进程已启动，后端健康检查尚未完成，请查看实时日志。${RESET}\n"
    fi
    printf '访问地址：http://127.0.0.1:3000\n'
    printf '日志文件：%s\n' "$LOG_FILE"
    return 0
  fi

  printf "${RED}WCDA 启动失败，最近日志如下：${RESET}\n"
  tail -n 30 "$LOG_FILE" 2>/dev/null || true
  unlink "$PID_FILE" 2>/dev/null || true
  return 1
}

stop_wcda() {
  local pid
  local count=0

  pid="$(managed_pid 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    printf "${YELLOW}WCDA 当前没有由本菜单管理的运行实例。${RESET}\n"
    return 0
  fi

  printf '正在停止 WCDA（PID：%s）...\n' "$pid"
  kill -TERM "$pid" 2>/dev/null || true

  while [ "$count" -lt 30 ]; do
    if ! is_wcda_pid "$pid"; then
      unlink "$PID_FILE" 2>/dev/null || true
      printf "${GREEN}WCDA 已停止。${RESET}\n"
      return 0
    fi
    sleep 0.5
    count=$((count + 1))
  done

  if is_wcda_pid "$pid"; then
    printf "${YELLOW}正常停止超时，正在强制停止已确认的 WCDA 进程。${RESET}\n"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi

  unlink "$PID_FILE" 2>/dev/null || true
  if is_wcda_pid "$pid"; then
    printf "${RED}停止失败，请查看 PID %s。${RESET}\n" "$pid"
    return 1
  fi
  printf "${GREEN}WCDA 已停止。${RESET}\n"
}

restart_wcda() {
  stop_wcda || return 1
  sleep 1
  start_wcda
}

show_logs() {
  local tail_pid=''

  ensure_runtime_dir
  printf "${CYAN}正在显示实时日志；按 Ctrl+C 返回菜单。${RESET}\n"

  trap 'if [ -n "$tail_pid" ]; then kill "$tail_pid" 2>/dev/null || true; fi' INT
  tail -n 100 -f "$LOG_FILE" &
  tail_pid="$!"
  wait "$tail_pid" 2>/dev/null || true
  trap - INT
}

show_status() {
  local pid

  pid="$(managed_pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    printf "状态：${GREEN}运行中${RESET}（PID：%s）\n" "$pid"
    printf '地址：http://127.0.0.1:3000\n'
  else
    printf "状态：${YELLOW}已停止${RESET}\n"
  fi
  printf '日志：%s\n' "$LOG_FILE"
}

pause_menu() {
  printf '\n按回车键返回菜单...'
  IFS= read -r _unused
}

interactive_menu() {
  local choice

  while true; do
    if [ -t 1 ] && command -v clear >/dev/null 2>&1; then
      clear
    fi
    printf '========================================\n'
    printf '       WCDA 本地运行管理菜单\n'
    printf '========================================\n'
    show_status
    printf '\n'
    printf '1. 手动启动\n'
    printf '2. 停止\n'
    printf '3. 查看实时 log\n'
    printf '4. 重启\n'
    printf '5. 退出\n'
    printf '\n请选择 [1-5]：'
    IFS= read -r choice

    case "$choice" in
      1) start_wcda; pause_menu ;;
      2) stop_wcda; pause_menu ;;
      3) show_logs ;;
      4) restart_wcda; pause_menu ;;
      5) printf '已退出。\n'; return 0 ;;
      *) printf "${RED}无效选项，请输入 1 到 5。${RESET}\n"; sleep 1 ;;
    esac
  done
}

case "${1:-menu}" in
  menu) interactive_menu ;;
  start) start_wcda ;;
  stop) stop_wcda ;;
  restart) restart_wcda ;;
  logs|log) show_logs ;;
  status) show_status ;;
  *)
    printf '用法：%s [menu|start|stop|restart|logs|status]\n' "$0"
    exit 2
    ;;
esac
