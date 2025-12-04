# Manual Workflow

## Key Concept

The batch script acts as a **pass-through**: it reads optional columns from the txt file and passes them to Python only if specified. If a column is empty or "-", Python's default is used.

## Input File Format (8 Columns)

```
Column 1: stack_path (required)
Column 2: voxel_size (optional, default: 650,650,650)
Column 3: jobs (optional, default: 24)
Column 4: project (optional): subfolder which will be created in the p3l-user folder
Column 5: sample_name (optional, auto-extracted)
Column 6: metadata_file (optional, auto-found)
Column 7: layer_type (optional, default: color)
Column 8: layer_name (optional, default: xray)
```

Use "-" to skip columns and use defaults.

## Examples

### Minimal (All Defaults)
```bash
/mnt/ximg/2025/p3l-user/RECON/20250101/sample/stitching_1/stack
```
Uses all Python defaults, will construct 2025101_p3l-user_sample_stitching_1 as the storage name and sample_stitching_1 as the WBK name

### Custom Voxel Size Only
```bash
/mnt/ximg/2025/p3l-user/RECON/20250101/sample/stitching_1/stack    325,325,325
```
Custom voxel size, all other defaults.

### Custom Jobs (Skip Voxel)
```bash
/mnt/ximg/2025/p3l-user/RECON/20250101/sample/recon_111_1/tomo    -    48
```
Default voxel size, 48 jobs, other defaults.

### With Project
```bash
/mnt/ximg/2025/p3l-cardiac/RECON/20250301/heart/stitching_1/stack    -    -    cardiac_study
```
All defaults except project folder.

### Segmentation Layer
```bash
/path/to/predictions    -    -    neuro    -    -    segmentation    predictions
```
Defaults for voxel/jobs, custom subfolder, layer type (will create a segmentation layer) and name.

### Full Specification
```bash
/path    325,325,325    12    project    20170101_p3l-user_sample_5    path/to/meta_5.json    segmentation    infected_cells
```
Every parameter explicitly set.

# Quick Start - Manual Workflow

## Input File Format (4 Columns)

```
Column 1: stack_path (required) - direct path to tiff directory
Column 2: project (optional) - subfolder under p3l-account
Column 3: sample_name (optional) - auto-extracted from path if omitted
Column 4: metadata_file (optional) - auto-constructed as meta_{run}.json if omitted
```

Use `-` as placeholder for optional columns.

## Basic Example

```bash
# Create input file
cat > datasets.txt << 'EOF'
# Just the path - everything else is automatic
/mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack

# With project folder
/mnt/ximg/2025/p3l-cardiac/RECON/20250301/heart/stitching_1/stack    cardiac_study

# Non-standard path - provide sample name explicitly
/weird/backup/tiffs    -    20180920_p3l-archive_sample_1
EOF

# Run batch processing
./wbk_manual_batch.sh datasets.txt
```

## What Gets Auto-Detected

### From Path
**Standard path:**
```
/mnt/ximg/2025/p3l-eduke/RECON/20251130/tree_2x2x10/stitching_1/stack
```

**Extracts:**
- Date: `20251130`
- P3L account: `p3l-eduke`
- Sample: `tree_2x2x10`
- Type: `stitching`
- Run: `1`

**Constructs sample name:**
```
20251130_p3l-eduke_tree_2x2x10_stitching_1
```

**Looks for metadata:**
```
/mnt/ximg/.../tree_2x2x10/meta_1.json
```

### From Sample Name
Once you have a sample name (extracted or explicit), the script derives:
- P3L account → WebKnossos team
- Run number → Metadata file lookup
- Readable name → Display name in WebKnossos

## Common Patterns

### Pattern 1: All Standard Paths
```bash
# Input file - just list the paths
/mnt/ximg/2025/p3l-user/RECON/20250101/sample_a/stitching_1/stack
/mnt/ximg/2025/p3l-user/RECON/20250101/sample_b/stitching_1/stack
/mnt/ximg/2025/p3l-user/RECON/20250101/sample_c/recon_111_1/tomo
```

Everything auto-extracted, metadata auto-found.

### Pattern 2: With Project Organization
```bash
# Group datasets into project folders
/mnt/ximg/2025/p3l-cardiac/RECON/20250301/heart_01/stitching_1/stack    cardiac_march
/mnt/ximg/2025/p3l-cardiac/RECON/20250301/heart_02/stitching_1/stack    cardiac_march
```

Creates hierarchy: `Datasets/p3l-cardiac/cardiac_march/`

### Pattern 3: Mixed Standard and Non-Standard
```bash
# Standard paths
/mnt/ximg/2025/p3l-current/RECON/20250101/exp_a/stitching_1/stack    current_work
/mnt/ximg/2025/p3l-current/RECON/20250101/exp_b/stitching_1/stack    current_work

# Non-standard - explicit sample name and metadata
/weird/backup/old_data    -    20180920_p3l-archive_old_1    /weird/meta_1.json
```

### Pattern 4: Archive with Custom Metadata Location
```bash
# Metadata not in standard location
/archive/2019/batch_5/tiffs    archive_2019    20190501_p3l-archive_exp5_1    /archive/metadata/exp5_meta.json
```


## When Auto-Extraction Fails

**Path doesn't match standard structure:**
```
Error: Could not extract sample name from path
```

**Solution:** Provide explicit sample name in column 3:
```bash
/weird/path/to/tiffs    -    20250101_p3l-user_sample_1
```

**Invalid sample name format:**
```
Error: Sample name does not match required format
```

**Solution:** Fix format to: `YYYYMMDD_p3l-account_SampleName_Type_Run`

## Tips

✅ **Start simple** - Just stack paths, let everything auto-detect 

✅ **Use projects** - Organize related datasets into project folders 

✅ **Check extraction** - Script shows what it extracted before processing

✅ **Standard paths work best** - Follows beamline conventions automatically

✅ **Explicit when needed** - Provide sample names for unexpected path structures or custom data saves

## See Also

- **[manual_datasets_example.txt](manual_datasets_example.txt)** - More examples