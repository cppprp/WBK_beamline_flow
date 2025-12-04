#!/bin/bash
# Batch process WebKnossos datasets using manual converter (no path validation)
# For old/archive data with non-standard directory structures

# Usage:
# ./wbk_manual_batch.sh datasets.txt

set -e

# Configuration
SCRIPT_PATH="./wbki_manual.py"

# Function to process a single dataset
process_dataset() {
    local stack_path="$1"
    local voxel_size="$2"
    local jobs="$3"
    local project="$4"
    local sample_name="$5"
    local metadata_file="$6"
    local layer_type="$7"
    local layer_name="$8"

    echo "=========================================="
    if [ -n "$sample_name" ]; then
        echo "Processing: $sample_name"
    else
        echo "Processing: (auto-extract from path)"
    fi
    echo "Stack path: $stack_path"
    if [ -n "$voxel_size" ]; then
        echo "Voxel size: $voxel_size"
    fi
    if [ -n "$jobs" ]; then
        echo "Jobs: $jobs"
    fi
    if [ -n "$project" ]; then
        echo "Project folder: $project"
    fi
    if [ -n "$metadata_file" ]; then
        echo "Metadata file: $metadata_file"
    fi
    if [ -n "$layer_type" ]; then
        echo "Layer type: $layer_type"
    fi
    if [ -n "$layer_name" ]; then
        echo "Layer name: $layer_name"
    fi
    echo "=========================================="

    # Build command with required arguments
    local cmd_args=(
        "$SCRIPT_PATH"
        --stack_path "$stack_path"
    )

    # Add optional arguments only if provided
    if [ -n "$voxel_size" ]; then
        cmd_args+=(--voxel_size "$voxel_size")
    fi
    if [ -n "$jobs" ]; then
        cmd_args+=(--jobs "$jobs")
    fi
    if [ -n "$sample_name" ]; then
        cmd_args+=(--sample_name "$sample_name")
    fi
    if [ -n "$project" ]; then
        cmd_args+=(--project "$project")
    fi
    if [ -n "$metadata_file" ]; then
        cmd_args+=(--metadata_file "$metadata_file")
    fi
    if [ -n "$layer_type" ]; then
        cmd_args+=(--layer_type "$layer_type")
    fi
    if [ -n "$layer_name" ]; then
        cmd_args+=(--layer_name "$layer_name")
    fi

    uv run "${cmd_args[@]}"

    if [ $? -eq 0 ]; then
        echo "✓ Successfully processed: ${sample_name:-auto-detected}"
        return 0
    else
        echo "✗ Failed to process: ${sample_name:-auto-detected}"
        return 1
    fi
}

# Main
if [ $# -eq 0 ]; then
    cat << 'EOF'
Usage: ./wbk_manual_batch.sh <path_list_file>

File format (tab or space-separated columns):
  Column 1: Path to tiff stack (required)
  Column 2: Voxel size (optional, uses Python default: 650,650,650)
  Column 3: Jobs (optional, uses Python default: 24)
  Column 4: Project subfolder (optional)
  Column 5: Sample name (optional, auto-extracted from path if omitted)
  Column 6: Metadata file path (optional, auto-constructed if omitted)
  Column 7: Layer type (optional, uses Python default: color)
  Column 8: Layer name (optional, uses Python default: xray)

Use "-" as placeholder for optional columns you want to skip.

Sample Name Extraction:
  If sample name not provided (column 5), script attempts to extract from path.
  Path should contain: p3l-account, YYYYMMDD date, sample dir, run dir

  Examples:
    /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack
    → 20251130_p3l-eduke_tree_2x2x10_stitching_1

Run Number Handling:
  - stitching_1 → type: stitching, run: 1
  - recon_111_1 → type: recon_111, run: 1
  - Metadata: meta_{run}.json (e.g., meta_1.json)

Example file content:

  # Minimal - just path, use all defaults
  /mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack

  # Custom voxel size
  /mnt/ximg/2025/p3l-user/RECON/20250101/sample/stitching_1/stack    500,500,500

  # Custom jobs
  /mnt/ximg/2025/p3l-user/RECON/20250101/sample/recon_111_1/tomo    -    32

  # With project folder (skip voxel_size and jobs)
  /mnt/beegfs7/P14/2025/p3l-cardiac/RECON/20250921/MA040/stitching_1/stack    -    -    cardiac_study

  # Explicit sample name (skip first 3 optional cols)
  /weird/backup/location/tiffs    -    -    -    20180920_p3l-archive_bone_1

  # With metadata file
  /archive/data/stack    -    -    -    20190301_p3l-user_test_2    /archive/meta_2.json

  # Custom layer type and name
  /path/to/predictions    -    -    neuro    -    -    segmentation    predictions

  # Full specification
  /path    500,500,500    32    project    20170101_p3l-user_sample_5    /meta_5.json    color    xray

Lines starting with # are ignored (comments)

Note:
- Any column can be omitted or use "-" to use Python script defaults
- Sample name format: YYYYMMDD_p3l-account_SampleName_TypeRun
- Metadata auto-constructed as parent_dir/meta_{run_number}.json if not specified
EOF
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

declare -a datasets_stack
declare -a datasets_voxel
declare -a datasets_jobs
declare -a datasets_project
declare -a datasets_name
declare -a datasets_metadata
declare -a datasets_layer_type
declare -a datasets_layer_name

line_num=0
while IFS= read -r line || [ -n "$line" ]; do
    line_num=$((line_num + 1))

    # Skip empty lines and comments
    if [[ -z "$line" ]] || [[ "$line" =~ ^[[:space:]]*# ]]; then
        continue
    fi

    # Parse line into columns (space or tab separated)
    read -r stack_path voxel_size jobs project sample_name metadata_file layer_type layer_name <<< "$line"

    # Validate required fields
    if [ -z "$stack_path" ]; then
        echo "Warning (line $line_num): Missing stack path"
        continue
    fi

    # Check if stack directory exists
    if [ ! -d "$stack_path" ]; then
        echo "Warning (line $line_num): Directory not found: $stack_path"
        continue
    fi

    # Clear optional fields if placeholder or empty
    if [ -z "$voxel_size" ] || [ "$voxel_size" = "-" ]; then
        voxel_size=""
    fi
    if [ -z "$jobs" ] || [ "$jobs" = "-" ]; then
        jobs=""
    fi
    if [ -z "$project" ] || [ "$project" = "-" ]; then
        project=""
    fi
    if [ -z "$sample_name" ] || [ "$sample_name" = "-" ]; then
        sample_name=""
    fi
    if [ -z "$metadata_file" ] || [ "$metadata_file" = "-" ]; then
        metadata_file=""
    fi
    if [ -z "$layer_type" ] || [ "$layer_type" = "-" ]; then
        layer_type=""
    fi
    if [ -z "$layer_name" ] || [ "$layer_name" = "-" ]; then
        layer_name=""
    fi

    # Validate metadata file if provided
    if [ -n "$metadata_file" ] && [ ! -f "$metadata_file" ]; then
        echo "Warning (line $line_num): Metadata file not found: $metadata_file"
        echo "  Proceeding without metadata"
        metadata_file=""
    fi

    datasets_stack+=("$stack_path")
    datasets_voxel+=("$voxel_size")
    datasets_jobs+=("$jobs")
    datasets_project+=("$project")
    datasets_name+=("$sample_name")
    datasets_metadata+=("$metadata_file")
    datasets_layer_type+=("$layer_type")
    datasets_layer_name+=("$layer_name")
done < "$LIST_FILE"

if [ ${#datasets_stack[@]} -eq 0 ]; then
    echo "No valid datasets found in $LIST_FILE"
    exit 1
fi

echo "Found ${#datasets_stack[@]} dataset(s) to process:"
for i in "${!datasets_stack[@]}"; do
    if [ -n "${datasets_name[$i]}" ]; then
        echo -n "  $((i+1)). ${datasets_name[$i]}"
    else
        echo -n "  $((i+1)). (auto-extract)"
    fi

    # Show configured parameters
    params=""
    [ -n "${datasets_voxel[$i]}" ] && params+="voxel: ${datasets_voxel[$i]}, "
    [ -n "${datasets_jobs[$i]}" ] && params+="jobs: ${datasets_jobs[$i]}, "
    [ -n "${datasets_project[$i]}" ] && params+="project: ${datasets_project[$i]}, "
    [ -n "${datasets_metadata[$i]}" ] && params+="metadata: yes, "
    [ -n "${datasets_layer_type[$i]}" ] && params+="type: ${datasets_layer_type[$i]}, "
    [ -n "${datasets_layer_name[$i]}" ] && params+="layer: ${datasets_layer_name[$i]}, "

    # Remove trailing comma and space
    params="${params%, }"

    if [ -n "$params" ]; then
        echo " ($params)"
    else
        echo " (all defaults)"
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

for i in "${!datasets_stack[@]}"; do
    stack_path="${datasets_stack[$i]}"
    voxel_size="${datasets_voxel[$i]}"
    jobs="${datasets_jobs[$i]}"
    project="${datasets_project[$i]}"
    sample_name="${datasets_name[$i]}"
    metadata_file="${datasets_metadata[$i]}"
    layer_type="${datasets_layer_type[$i]}"
    layer_name="${datasets_layer_name[$i]}"

    echo ""
    echo "[$((i+1))/${#datasets_stack[@]}]"

    if process_dataset "$stack_path" "$voxel_size" "$jobs" "$project" "$sample_name" "$metadata_file" "$layer_type" "$layer_name"; then
        success_count=$((success_count + 1))
    else
        failure_count=$((failure_count + 1))
        failed_datasets+=("${sample_name:-$stack_path}")
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