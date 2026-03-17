#!/bin/bash
# Control Gregcoin miners on Pi cluster nodes
# Usage: ./miner-control.sh [deploy|start|stop|restart|status] [--cpu 25|50|75|100]

COINBASE_ADDR="grc1qthh3zwq09k22yqegv7265xgfvzx447y3rwf3a0"
RPC_USER="grcuser"
RPC_PASS="96615093ce049f332cba4a2dbe76598811e2"
RPC_HOST="10.0.1.220"
RPC_PORT="8445"
GREGMINER_BIN="/home/pi/gregminer/gregminer"

# CPU throttle: percentage of cores to use (25/50/75/100)
CPU_PCT=100

SSH_USER="pi"
SSH_PASS="abacadabra"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=5"

# Format: "ip:name:rpc_host"
# picard uses 127.0.0.1 for RPC (local), workers use picard's IP
NODES=(
  "10.0.1.220:picard:127.0.0.1"
  "10.0.1.221:riker:${RPC_HOST}"
  "10.0.1.222:data:${RPC_HOST}"
  "10.0.1.223:laforge:${RPC_HOST}"
  "10.0.1.224:worf:${RPC_HOST}"
  "10.0.1.218:lore:${RPC_HOST}"
  "10.0.1.219:troi:${RPC_HOST}"
  "10.0.1.217:wesley:${RPC_HOST}"
)

ssh_run() {
  local ip="$1"; local cmd="$2"
  if [[ "$ip" == "10.0.1.220" ]]; then
    bash -c "$cmd" 2>/dev/null
  else
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS "${SSH_USER}@${ip}" "$cmd" 2>/dev/null
  fi
}

scp_to() {
  local ip="$1"; local src="$2"; local dst="$3"
  if [[ "$ip" == "10.0.1.220" ]]; then
    cp "$src" "$dst"
  else
    sshpass -p "$SSH_PASS" scp $SSH_OPTS "$src" "${SSH_USER}@${ip}:${dst}" 2>/dev/null
  fi
}

do_deploy() {
  echo "Deploying gregminer binary to all nodes..."
  for entry in "${NODES[@]}"; do
    local ip="${entry%%:*}"; local rest="${entry#*:}"; local name="${rest%%:*}"
    echo -n "  [$name] copying binary... "
    ssh_run "$ip" "mkdir -p /home/pi/gregminer"
    scp_to "$ip" "$GREGMINER_BIN" "/home/pi/gregminer/gregminer"
    ssh_run "$ip" "chmod +x /home/pi/gregminer/gregminer && /home/pi/gregminer/gregminer --version 2>&1 | head -1"
  done
  echo "Deploy complete."
}

threads_for_pct() {
  # Compute thread count = max(1, floor(nproc * pct / 100)) on the remote host
  local pct="$1"
  echo "python3 -c \"import math,os; print(max(1, math.floor(os.cpu_count()*${pct}/100)))\""
}

do_start() {
  local pct="${CPU_PCT}"
  echo "  CPU throttle: ${pct}% of cores"
  for entry in "${NODES[@]}"; do
    local ip="${entry%%:*}"; local rest="${entry#*:}"
    local name="${rest%%:*}"; local rpc_host="${rest##*:}"
    local threads_cmd
    threads_cmd=$(threads_for_pct "$pct")
    local cmd="/home/pi/gregminer/gregminer -a sha256d \
      -o http://${RPC_USER}:${RPC_PASS}@${rpc_host}:${RPC_PORT} \
      --coinbase-addr=${COINBASE_ADDR} \
      --api-bind=0.0.0.0:4048 --api-remote \
      --scantime=30 \
      -t \$($threads_cmd) -q"
    echo -n "  [$name] starting... "
    ssh_run "$ip" "nohup $cmd > /home/pi/gregminer.log 2>&1 & disown; echo started"
  done
}

do_stop() {
  for entry in "${NODES[@]}"; do
    local ip="${entry%%:*}"; local rest="${entry#*:}"; local name="${rest%%:*}"
    echo -n "  [$name] stopping... "
    ssh_run "$ip" "pkill -f 'gregminer/gregminer' && echo stopped || echo 'not running'"
  done
}

do_restart() {
  echo "Stopping all miners..."
  do_stop
  sleep 2
  echo "Starting all miners..."
  do_start
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cpu)
        shift
        case "$1" in
          25|50|75|100) CPU_PCT="$1" ;;
          *) echo "Error: --cpu must be 25, 50, 75, or 100"; exit 1 ;;
        esac
        ;;
    esac
    shift
  done
}

do_status() {
  echo "Miner status:"
  for entry in "${NODES[@]}"; do
    local ip="${entry%%:*}"; local rest="${entry#*:}"; local name="${rest%%:*}"
    printf "  %-10s (%s): " "$name" "$ip"
    local result
    result=$(ssh_run "$ip" "pgrep -c -f 'gregminer/gregminer' 2>/dev/null")
    if [[ "$result" -gt 0 ]] 2>/dev/null; then
      echo "RUNNING"
    else
      echo "STOPPED"
    fi
  done
}

CMD="${1:-status}"; shift || true
parse_args "$@"

case "$CMD" in
  deploy)  do_deploy  ;;
  start)   do_start   ;;
  stop)    do_stop    ;;
  restart) do_restart ;;
  status)  do_status  ;;
  *)
    echo "Usage: $0 [deploy|start|stop|restart|status] [--cpu 25|50|75|100]"
    exit 1
    ;;
esac
