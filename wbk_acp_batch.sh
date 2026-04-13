#!/bin/bash
# Process WebKnossos datasets from a list file

# Usage:
# ./wbk_acp_batch.sh datasets.txt

set -e

# Configuration
SCRIPT_PATH="./wbki_acp.py"
DEFAULT_VOXEL_SIZE="650,650,650"
JOBS=24

extract_sample_name() {
    local input_path="$1"

    # Remove trailing slashes
    input_path="${input_path%/}"

    # Strip /stack or /tomo from the end if present
    local basename_check=$(basename "$input_path")
    if [[ "$basename_check" == "stack" ]] || [[ "$basename_check" == "tomo" ]]; then
        input_path="$(dirname "$input_path")"
    fi

    # Get the run directory (stitching, recon_111_1, etc.)
    local run_dir=$(basename "$input_path")
    local sample_dir=$(basename "$(dirname "$input_path")")

    # Extract p3l account from full path
    local p3l_account=$(echo "$input_path" | grep -oP 'p3l-[^/]+')

    # Find the date directory by searching for YYYYMMDD pattern
    # This works regardless of whether RAW_DATA is present
    local date_dir=""
    local temp_path="$input_path"
    while [[ "$temp_path" != "/" && "$temp_path" != "." ]]; do
        local dir_name=$(basename "$temp_path")
        if [[ "$dir_name" =~ ^[0-9]{8}$ ]]; then
            date_dir="$dir_name"
            break
        fi
        temp_path="$(dirname "$temp_path")"
    done

    if [[ -z "$date_dir" ]]; then
        echo "Error: Could not find date directory (YYYYMMDD) in path: $input_path" >&2
        return 1
    fi

    # Handle recon_* vs stitching differently
    if [[ "$run_dir" =~ ^recon_.*_([0-9]+)$ ]]; then
        # Extract just the run number from recon_111_1 -> 1
        local run_number="${BASH_REMATCH[1]}"
        echo "${date_dir}_${p3l_account}_${sample_dir}_${run_number}"
    else
        # Keep stitching as is
        echo "${date_dir}_${p3l_account}_${sample_dir}_${run_dir}"
    fi
}

# Function to process a single dataset
process_dataset() {
    local input_path="$1"
    local voxel_size="$2"
    local project="$3"
    local sample_name="$4"
    local flags="$5"

    echo "=========================================="
    echo "Processing: $sample_name"
    echo "Input path: $input_path"
    echo "Voxel size: $voxel_size"
    if [ -n "$project" ]; then
        echo "Project folder: $project"
    fi
    if [ -n "$flags" ]; then
        echo "Flags: $flags"
    fi
    echo "=========================================="

    # Build command with optional project argument
    local cmd_args=(
        "$SCRIPT_PATH"
        --input_dir "$input_path"
        --sample_name "$sample_name"
        --voxel_size "$voxel_size"
        --jobs "$JOBS"
    )

    if [ -n "$project" ]; then
        cmd_args+=(--project "$project")
    fi

    # Add any additional flags (like --skip_conversion)
    if [ -n "$flags" ]; then
        # Split flags by whitespace and add them
        read -ra flag_array <<< "$flags"
        cmd_args+=("${flag_array[@]}")
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
    echo "  Column 4: Sample name (optional, auto-extracted from path if omitted)"
    echo "  Column 5: Additional flags (optional, e.g., --skip_conversion)"
    echo ""
    echo "Example file content:"
    echo "  # Simple path only (uses defaults, auto-extracts sample name)"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample1/stitching"
    echo ""
    echo "  # Path with custom voxel size"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample2/recon_111_1    500,500,500"
    echo ""
    echo "  # Path with voxel size and project folder"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample3/stitching    650,650,650    cardiac_study"
    echo ""
    echo "  # Skip conversion, only update permissions/metadata"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample4/stitching    -    -    -    --skip_conversion"
    echo ""
    echo "  # Non-standard path with custom sample name"
    echo "  /weird/backup/location/data    -    -    20250901_p3l-user_custom_sample"
    echo ""
    echo "  # Multiple flags"
    echo "  /mnt/ximg/2025/p3l-user/RECON/20251007/Sample5/stitching    -    -    -    --skip_conversion --skip_permissions"
    echo ""
    echo "Lines starting with # are ignored (comments)"
    echo ""
    echo "Note: WebKnossos token is read from ~/.webknossos/token by the Python script"
    echo "      Paths ending in /stack or /tomo will be automatically trimmed"
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
declare -a datasets_name
declare -a datasets_flags

line_num=0
while IFS= read -r line || [ -n "$line" ]; do
    line_num=$((line_num + 1))

    # Skip empty lines and comments
    if [[ -z "$line" ]] || [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    # Parse line into columns (space or tab separated)
    # Read first 4 columns explicitly, rest goes into flags
    read -r path voxel_size project sample_name flags <<< "$line"

    # Check if directory exists
    if [ ! -d "$path" ]; then
        echo "Warning (line $line_num): Directory not found: $path"
        continue
    fi

    # Use default voxel size if not provided or is placeholder
    if [ -z "$voxel_size" ] || [ "$voxel_size" = "-" ]; then
        voxel_size="$DEFAULT_VOXEL_SIZE"
    fi

    # Clear project if placeholder
    if [ "$project" = "-" ]; then
        project=""
    fi

    # Extract sample name if not provided or is placeholder
    if [ -z "$sample_name" ] || [ "$sample_name" = "-" ]; then
        sample_name=$(extract_sample_name "$path")
        if [ $? -ne 0 ]; then
            echo "Error (line $line_num): Failed to extract sample name from: $path"
            echo "  Please provide sample_name explicitly as 4th column"
            continue
        fi
    fi

    # Clear flags if placeholder
    if [ "$flags" = "-" ]; then
        flags=""
    fi

    datasets_path+=("$path")
    datasets_voxel+=("$voxel_size")
    datasets_project+=("$project")
    datasets_name+=("$sample_name")
    datasets_flags+=("$flags")
done < "$LIST_FILE"

if [ ${#datasets_path[@]} -eq 0 ]; then
    echo "No valid datasets found in $LIST_FILE"
    exit 1
fi

echo "Found ${#datasets_path[@]} dataset(s) to process:"
for i in "${!datasets_path[@]}"; do
    echo -n "  $((i+1)). ${datasets_name[$i]} (voxel: ${datasets_voxel[$i]}"
    if [ -n "${datasets_project[$i]}" ]; then
        echo -n ", project: ${datasets_project[$i]}"
    fi
    if [ -n "${datasets_flags[$i]}" ]; then
        echo -n ", flags: ${datasets_flags[$i]}"
    fi
    echo ")"
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
    sample_name="${datasets_name[$i]}"
    flags="${datasets_flags[$i]}"

    echo ""
    echo "[$((i+1))/${#datasets_path[@]}]"

    if process_dataset "$input_path" "$voxel_size" "$project" "$sample_name" "$flags"; then
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