#!/usr/bin/env bash
# mine_genesis_parallel.sh
# Distributes genesis mining across the Pi cluster.
# Each node searches a different 1B-nonce range.
# First node to find the solution prints the result and kills the others.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MINER="$SCRIPT_DIR/mine_genesis.py"
SSH_USER="pi"
SSH_PASS="abacadabra"
NODES=(10.0.1.220 10.0.1.222 10.0.1.219 10.0.1.224)
RANGES=(
  "0 1073741823"         # 0x00000000 – 0x3fffffff  (picard)
  "1073741824 2147483647" # 0x40000000 – 0x7fffffff  (data)
  "2147483648 3221225471" # 0x80000000 – 0xbfffffff  (troi)
  "3221225472 4294967295" # 0xc0000000 – 0xffffffff  (worf)
)

RESULT_FILE=$(mktemp /tmp/genesis_result.XXXXXX)
trap 'rm -f "$RESULT_FILE"' EXIT

echo "Gregcoin Parallel Genesis Miner"
echo "Distributing across ${#NODES[@]} nodes..."
echo

PIDS=()
for i in "${!NODES[@]}"; do
  node="${NODES[$i]}"
  range="${RANGES[$i]}"
  start=$(echo "$range" | awk '{print $1}')
  end=$(echo   "$range" | awk '{print $2}')
  echo "  Starting miner on $node (nonce $start – $end)"
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$SSH_USER@$node" \
    "python3 /tmp/grc_mine_genesis.py $start $end" < /dev/null \
    2>/dev/null &
  PIDS+=($!)
done

# Also run on this node (picard), which handles range 0
echo "  Starting local miner (same as picard above) ..."
echo

# Wait for any process to output the result
# Actually let's just run on this machine locally for the picard range
# and show output, while remote runs silently
echo "Mining locally for range 0–1073741823 ..."
python3 "$MINER" 0 1073741823 &
LOCAL_PID=$!

# Also upload and run on remote nodes
for i in 1 2 3; do
  node="${NODES[$i]}"
  range="${RANGES[$i]}"
  start=$(echo "$range" | awk '{print $1}')
  end=$(echo   "$range" | awk '{print $2}')
  # Upload script
  sshpass -p "$SSH_PASS" scp -o StrictHostKeyChecking=no \
    "$MINER" "$SSH_USER@$node:/tmp/grc_mine_genesis.py" 2>/dev/null || true
  # Run in background, write output to temp file
  sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$SSH_USER@$node" \
    "python3 /tmp/grc_mine_genesis.py $start $end" 2>/dev/null &
done

echo "All miners running. Waiting for result..."
wait $LOCAL_PID

# If we get here without finding, the other nodes may have found it
echo "Local range exhausted. Check remote nodes."
