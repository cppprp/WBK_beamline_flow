#!/bin/bash
# Performance monitoring script for WebKnossos conversions
# Usage: ./monitor_performance.sh <output_file> <sample_interval_seconds>

OUTPUT_FILE="${1:-performance_log.txt}"
INTERVAL="${2:-5}"  # Sample every 5 seconds by default

echo "Performance Monitor Started: $(date)" > "$OUTPUT_FILE"
echo "Sampling every ${INTERVAL} seconds" >> "$OUTPUT_FILE"
echo "========================================" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Get baseline info
echo "SYSTEM INFO:" >> "$OUTPUT_FILE"
echo "CPU cores (physical): $(lscpu | grep '^Core(s)' | awk '{print $4}')" >> "$OUTPUT_FILE"
echo "CPU cores (logical): $(nproc)" >> "$OUTPUT_FILE"
echo "Total RAM: $(free -h | awk '/^Mem:/ {print $2}')" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Function to capture snapshot
capture_snapshot() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # Count processes and threads
    local python_procs=$(pgrep -c python || echo 0)
    local uv_procs=$(pgrep -c uv || echo 0)
    local total_threads=$(ps -eLf | grep -E 'python|uv' | wc -l)

    # Get CPU usage
    local cpu_usage=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)
    local cpu_wait=$(top -bn1 | grep "Cpu(s)" | awk '{print $10}' | cut -d'%' -f1)

    # Get memory usage
    local mem_used=$(free -h | awk '/^Mem:/ {print $3}')
    local mem_percent=$(free | awk '/^Mem:/ {printf "%.1f", $3/$2 * 100}')

    # Get load average
    local load_avg=$(uptime | awk -F'load average:' '{print $2}' | xargs)

    # Output snapshot
    echo "[$timestamp] CPU: ${cpu_usage}% | IO-wait: ${cpu_wait}% | Processes: ${python_procs} python, ${uv_procs} uv | Threads: ${total_threads} | RAM: ${mem_used} (${mem_percent}%) | Load: ${load_avg}"
}

# Monitor loop
echo "PERFORMANCE DATA:" >> "$OUTPUT_FILE"
echo "Format: [timestamp] CPU% | IO-wait% | Process counts | Thread count | RAM usage | Load average" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

while true; do
    capture_snapshot >> "$OUTPUT_FILE"
    sleep "$INTERVAL"
done