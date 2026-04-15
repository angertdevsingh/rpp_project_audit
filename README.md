# rpp_project_audit

A portable, self-contained Python script for auditing Reaper `.rpp` project files.

This repository contains `audit_projects_portable.py`, a self-contained script that uses only the Python standard library to parse Reaper project files and emit JSON metadata for:

- audio and MIDI source files
- plugin instances and alias details
- render output files
- track and item counts
- missing source/render file detection
- unstructured tokens for unusual project content

## Credit

This tool is a portable audit wrapper built on Reaper project parsing concepts heavily inspired by the `rppp` project parser: https://github.com/CharlesHolbrow/rppp

## Requirements

- Python 3.11+
- No third-party dependencies required

## Usage

Run the script with one or more `.rpp` files or directories:

```bash
python audit_projects_portable.py "Reaper Projects" -o audit_results.json --pretty
```

Arguments:

- `inputs`: one or more `.rpp` files or directories containing `.rpp` files
- `-o`, `--output`: write JSON output to a file instead of stdout
- `--pretty`: pretty-print JSON with indentation

## Output

The script produces JSON keyed by project file name. Each project entry includes:

- `audio`: list of audio source paths
- `midi`: list of MIDI source paths
- `plugins`: list of plugin metadata objects
- `tracks`: total track count
- `items`: total item count
- `sources`: normalized source entries with resolved path and existence checks
- `render_files`: normalized render output entries
- `missing_source_count`: number of missing source files
- `missing_render_file_count`: number of missing render output files
- `unstructured`: captured tokens that were not handled explicitly

A `_run_context` section is also added to record the working directory and inputs used.

## Example

```bash
python audit_projects_portable.py "Reaper Projects" -o audit_results.json --pretty
```

This scans `Reaper Projects` recursively for `.rpp` files, audits them in parallel, and writes a readable JSON report to `audit_results.json`.
