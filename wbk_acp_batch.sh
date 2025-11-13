#!/bin/bash
# Process WebKnossos datasets from a list file

# Usage:
# ./wbk_acp_batch.sh datasets.txt

set -e

# Configuration
SCRIPT_PATH="./wbki_acp.py"
TOKEN="${WBK_TOKEN}"
DEFAULT_VOXEL_SIZE="650,650,650"
JOBS=24

if [ -z "$TOKEN" ]; then
    echo "Error: WBK_TOKEN environment variable not set"
    echo "Usage: export WBK_TOKEN='your_token_here'"
    exit 1
fi

extract_sample_name() {
    local input_path="$1"

    local run_dir=$(basename "$input_path")
    local sample_dir=$(basename "$(dirname "$input_path")")
    local date_dir=$(basename "$(dirname "$(dirname "$input_path")")")
    local p3l_account=$(echo "$input_path" | grep -oP 'p3l-[^/]+')

    # Handle recon_* vs stitching_* differently
    if [[ "$run_dir" =~ ^recon_.*_([0-9]+)$ ]]; then
        local run_number="${BASH_REMATCH[1]}"
        echo "${date_dir}_${p3l_account}_${sample_dir}_${run_number}"
    else
        echo "${date_dir}_${p3l_account}_${sample_dir}_${run_dir}"
    fi
}

# Function to process a single dataset
process_dataset() {
    local input_path="$1"
    local voxel_size="${2:-$DEFAULT_VOXEL_SIZE}"
    local project="${3:-}"

    local sample_name=$(extract_sample_name "$input_path")

    echo "=========================================="
    echo "Processing: $sample_name"
    echo "Input path: $input_path"
    echo "Voxel size: $voxel_size"
    if [ -n "$project" ]; then
        echo "Project folder: $project"
    fi
    echo "=========================================="

    # Build command with optional project argument
    local cmd_args=(
        "$SCRIPT_PATH"
        --input_dir "$input_path"
        --sample_name "$sample_name"
        --token "$TOKEN"
        --voxel_size "$voxel_size"
        --jobs "$JOBS"
    )

    if [ -n "$project" ]; then
        cmd_args+=(--project "$project")
    fi

    uv run "${cmd_args[@]}"

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
    echo "File format (tab or space-separated columns):"
    echo "  Column 1: Path to dataset (required)"
    echo "  Column 2: Voxel size (optional, default: $DEFAULT_VOXEL_SIZE)"
    echo "  Column 3: Project subfolder (optional)"
    echo ""
    echo "Example file content:"
    echo "  # Simple path only (uses defaults)"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample1/stitching_1"
    echo ""
    echo "  # Path with custom voxel size"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample2/recon_111_1    500,500,500"
    echo ""
    echo "  # Path with voxel size and project folder"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample3/stitching_1    650,650,650    cardiac_study"
    echo ""
    echo "  # Path with default voxel size but project folder (use - as placeholder)"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample4/stitching_1    -    cardiac_study"
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
echo "Reading dataset configurations from: $LIST_FILE"
echo ""

declare -a datasets_path
declare -a datasets_voxel
declare -a datasets_project

line_num=0
while IFS= read -r line || [ -n "$line" ]; do
    line_num=$((line_num + 1))

    # Skip empty lines and comments
    if [[ -z "$line" ]] || [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    # Parse line into columns (space or tab separated)
    read -r path voxel_size project <<< "$line"

    # Check if directory exists
    if [ ! -d "$path" ]; then
        echo "Warning (line $line_num): Directory not found: $path"
        continue
    fi

    # Use default voxel size if not provided or is placeholder
    if [ -z "$voxel_size" ] || [ "$voxel_size" = "-" ]; then
        voxel_size="$DEFAULT_VOXEL_SIZE"
    fi

    datasets_path+=("$path")
    datasets_voxel+=("$voxel_size")
    datasets_project+=("$project")
done < "$LIST_FILE"

if [ ${#datasets_path[@]} -eq 0 ]; then
    echo "No valid datasets found in $LIST_FILE"
    exit 1
fi

echo "Found ${#datasets_path[@]} dataset(s) to process:"
for i in "${!datasets_path[@]}"; do
    sample_name=$(extract_sample_name "${datasets_path[$i]}")
    echo -n "  $((i+1)). $sample_name (voxel: ${datasets_voxel[$i]}"
    if [ -n "${datasets_project[$i]}" ]; then
        echo ", project: ${datasets_project[$i]})"
    else
        echo ")"
    fi
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

for i in "${!datasets_path[@]}"; do
    input_path="${datasets_path[$i]}"
    voxel_size="${datasets_voxel[$i]}"
    project="${datasets_project[$i]}"

    echo ""
    echo "[$((i+1))/${#datasets_path[@]}]"

    if process_dataset "$input_path" "$voxel_size" "$project"; then
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