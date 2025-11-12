#!/bin/bash
# Process WebKnossos datasets from a list file

# Usage:
# ./wbk_acp_batch.sh datasets.txt

set -e

# Configuration
SCRIPT_PATH="./wbki_acp.py"
TOKEN="${WBK_TOKEN}"
VOXEL_SIZE="650,650,650"
JOBS=24

if [ -z "$TOKEN" ]; then
    echo "Error: WBK_TOKEN environment variable not set"
    echo "Usage: export WBK_TOKEN='your_token_here'"
    exit 1
fi

# Function to extract sample_name from path
extract_sample_name() {
    local input_path="$1"

    # Example: /mnt/ximg/2025/p3l-gabrielsalg/RECON/20251007/HD1152-1A_P1/stitching_1
    # Will construct: 20251007_p3l-gabrielsalg_HD1152-1A_P1_stitching_1

    local run_dir=$(basename "$input_path")
    local sample_dir=$(basename "$(dirname "$input_path")")
    local date_dir=$(basename "$(dirname "$(dirname "$input_path")")")
    local p3l_account=$(echo "$input_path" | grep -oP 'p3l-[^/]+')

    echo "${date_dir}_${p3l_account}_${sample_dir}_${run_dir}"
}

# Function to process a single dataset
process_dataset() {
    local input_path="$1"
    local sample_name=$(extract_sample_name "$input_path")

    echo "=========================================="
    echo "Processing: $sample_name"
    echo "Input path: $input_path"
    echo "=========================================="

    uv run "$SCRIPT_PATH" \
        --input_dir "$input_path" \
        --sample_name "$sample_name" \
        --token "$TOKEN" \
        --voxel_size "$VOXEL_SIZE" \
        --jobs "$JOBS"

    if [ $? -eq 0 ]; then
        echo "✓ Successfully processed: $sample_name"
        return 0
    else
        echo "✗ Failed to process: $sample_name"
        return 1
    fi
}

# Main
if [ $# -eq 0 ]; then
    echo "Usage: $0 <path_list_file>"
    echo ""
    echo "Example:"
    echo "  $0 datasets.txt"
    echo ""
    echo "The file should contain one path per line:"
    echo "  /mnt/ximg/2025/p3l-gabrielsalg/RECON/20251007/HD1152-1A_P1/stitching_1"
    echo "  /mnt/ximg/2025/p3l-gabrielsalg/RECON/20251007/HD1152-1A_P1/recon_111_1"
    echo "  /mnt/ximg/2025/p3l-eduke/RECON/20251106/E5/stitching_1"
    echo ""
    echo "Lines starting with # are ignored (comments)"
    exit 1
fi

LIST_FILE="$1"

if [ ! -f "$LIST_FILE" ]; then
    echo "Error: File not found: $LIST_FILE"
    exit 1
fi

# Read paths from file
echo "Reading dataset paths from: $LIST_FILE"
echo ""

datasets=()
line_num=0
while IFS= read -r line || [ -n "$line" ]; do
    line_num=$((line_num + 1))

    # Skip empty lines and comments
    if [[ -z "$line" ]] || [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    # Trim whitespace
    line=$(echo "$line" | xargs)

    # Check if directory exists
    if [ ! -d "$line" ]; then
        echo "Warning (line $line_num): Directory not found: $line"
        continue
    fi

    datasets+=("$line")
done < "$LIST_FILE"

if [ ${#datasets[@]} -eq 0 ]; then
    echo "No valid datasets found in $LIST_FILE"
    exit 1
fi

echo "Found ${#datasets[@]} dataset(s) to process:"
for i in "${!datasets[@]}"; do
    sample_name=$(extract_sample_name "${datasets[$i]}")
    echo "  $((i+1)). $sample_name"
done
echo ""

read -p "Process these datasets? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted"
    exit 0
fi

# Process each dataset
success_count=0
failure_count=0
failed_datasets=()
start_time=$(date +%s)

for i in "${!datasets[@]}"; do
    input_path="${datasets[$i]}"
    echo ""
    echo "[$((i+1))/${#datasets[@]}]"

    if process_dataset "$input_path"; then
        success_count=$((success_count + 1))
    else
        failure_count=$((failure_count + 1))
        failed_datasets+=("$input_path")
    fi
done

end_time=$(date +%s)
duration=$((end_time - start_time))

# Summary
echo ""
echo "=========================================="
echo "SUMMARY"
echo "=========================================="
echo "Total processed: $((success_count + failure_count))"
echo "Successful: $success_count"
echo "Failed: $failure_count"
echo "Time elapsed: $((duration / 60)) minutes $((duration % 60)) seconds"

if [ $failure_count -gt 0 ]; then
    echo ""
    echo "Failed datasets:"
    for dataset in "${failed_datasets[@]}"; do
        echo "  - $dataset"
    done
    exit 1
fi

echo ""
echo "✓ All datasets processed successfully!"