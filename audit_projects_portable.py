
#!/usr/bin/env python3
"""Portable Reaper .rpp audit script.

This script is self-contained and only depends on the Python standard library.
It parses one or more Reaper .rpp files and writes a JSON report of audio,
MIDI, plugin, track, and item metadata.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

ValueType = Union[str, int, float]

_TOKEN_RE = re.compile(r'^[A-Z_0-9]+')
_NUMBER_RE = re.compile(r'^-?\d+$')
_FLOAT_RE = re.compile(r'^-?\d+\.\d+$')
_B64_RE = re.compile(r'^[A-Za-z0-9+/]+=*$')


def _is_b64_line(text: str) -> bool:
    """Return True when a line looks like base64 data."""
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_B64_RE.match(stripped))


def _parse_value(token: str) -> ValueType:
    """Convert a token string to int, float, or leave it as text."""
    if _NUMBER_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_params(text: str) -> List[ValueType]:
    """Split a line into Reaper parameter tokens."""
    params: List[ValueType] = []
    i = 0
    length = len(text)

    while i < length:
        while i < length and text[i].isspace():
            i += 1
        if i >= length:
            break

        if text[i] in {'"', "'", '`'}:
            quote = text[i]
            i += 1
            start = i
            while i < length and text[i] != quote:
                i += 1
            if i >= length:
                raise ValueError('Unterminated quoted string')
            value = text[start:i]
            params.append(value)
            i += 1
            continue

        start = i
        while i < length and not text[i].isspace():
            i += 1
        value = text[start:i]
        params.append(_parse_value(value))

    return params


@dataclass
class ReaperStruct:
    token: str
    params: List[ValueType]


class ReaperBase:
    def __init__(
        self,
        token: str,
        params: Sequence[ValueType] | None = None,
        contents: Sequence[Union[ReaperStruct, "ReaperBase"]] | None = None,
        b64_chunks: Sequence[str] | None = None,
        jsfx_data: Sequence[ValueType] | None = None,
    ):
        if not token or not isinstance(token, str):
            raise TypeError('ReaperBase requires a token string')

        self.token = token
        self.params = list(params or [])
        self.contents = list(contents or [])
        self.b64_chunks = list(b64_chunks or [])
        self.jsfx_data = list(jsfx_data or [])


def _build_path_entry(raw_path: Any, project_dir: Path, kind: str, source_type: str | None) -> dict[str, Any]:
    """Build a normalized dependency entry for report output."""
    entry = {
        'kind': kind,
        'source_type': source_type,
        'raw_path': raw_path,
        'is_absolute': False,
        'resolved_path': None,
        'exists': False,
        'project_relative_path': None,
    }
    if isinstance(raw_path, str) and raw_path != '':
        path = Path(raw_path)
        entry['is_absolute'] = path.is_absolute()
        if entry['is_absolute']:
            resolved = path.resolve(strict=False)
        else:
            resolved = (project_dir / raw_path).resolve(strict=False)
            entry['project_relative_path'] = str(Path(raw_path))
        entry['resolved_path'] = str(resolved)
        entry['exists'] = resolved.exists()
    return entry


def _parse_struct_line(line: str) -> Optional[ReaperStruct]:
    """Parse a single Reaper struct line into a token and params."""
    candidate = line.lstrip()
    match = _TOKEN_RE.match(candidate)
    if not match:
        return None
    token = match.group(0)
    rest = candidate[match.end() :]
    params = _split_params(rest)
    return ReaperStruct(token=token, params=params)


def _parse_header(line: str) -> Tuple[str, List[ValueType], bool]:
    """Parse a Reaper object header line into token, params, and self-closing state."""
    trimmed = line.strip()
    if not trimmed.startswith('<'):
        raise ValueError('Object header must start with "<"')
    body = trimmed[1:]
    self_closing = False
    if body.endswith('>'):
        body = body[:-1].strip()
        self_closing = True
    parts = _split_params(body)
    if not parts:
        raise ValueError('Object header must contain a token')
    token = parts[0]
    params = parts[1:]
    return token, params, self_closing


def _parse_object(lines: List[str], start: int) -> Tuple[ReaperBase, int]:
    """Parse a nested Reaper object block from a list of lines."""
    token, params, self_closing = _parse_header(lines[start])
    base = ReaperBase(token=token, params=params)
    if self_closing:
        return base, start + 1

    contents: List[Union[ReaperStruct, ReaperBase]] = []
    b64_chunks: List[str] = []
    jsfx_data: List[ValueType] = []
    idx = start + 1

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped == '':
            idx += 1
            continue
        if stripped == '>':
            idx += 1
            break
        if stripped.startswith('<'):
            child, idx = _parse_object(lines, idx)
            contents.append(child)
            continue

        if token == 'NOTES' and stripped.startswith('|'):
            note_lines: List[str] = []
            while idx < len(lines) and lines[idx].lstrip().startswith('|'):
                note_lines.append(lines[idx].lstrip()[1:])
                idx += 1

            base.params = ['\n'.join(note_lines)]
            while idx < len(lines) and lines[idx].strip() == '':
                idx += 1
            if idx < len(lines) and lines[idx].strip() == '>':
                idx += 1
            base.contents = contents
            base.b64_chunks = b64_chunks
            base.jsfx_data = jsfx_data
            return base, idx

        if token in {'VST', 'RECORD_CFG', 'APPLYFX_CFG', 'RENDER_CFG'}:
            if _is_b64_line(stripped):
                b64_chunks.append(stripped)
                idx += 1
                continue
        if token == 'JS':
            if not _TOKEN_RE.match(stripped):
                jsfx_data.extend(_split_params(stripped))
                idx += 1
                continue

        struct = _parse_struct_line(line)
        if struct is not None:
            contents.append(struct)
            idx += 1
            continue

        contents.append(ReaperStruct(token=stripped, params=[]))
        idx += 1

    base.contents = contents
    base.b64_chunks = b64_chunks
    base.jsfx_data = jsfx_data
    return base, idx


def parse(text: str) -> Union[ReaperBase, ReaperStruct]:
    """Parse raw Reaper project text into a structured object tree."""
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = normalized.split('\n')
    idx = 0
    while idx < len(lines) and lines[idx].strip() == '':
        idx += 1
    if idx >= len(lines):
        raise ValueError('Cannot parse empty input')

    line = lines[idx].strip()
    if line.startswith('<'):
        obj, _ = _parse_object(lines, idx)
        return obj

    struct = _parse_struct_line(lines[idx])
    if struct is not None:
        return struct
    raise ValueError('Unable to parse input')


def extract_dependencies(project: ReaperBase, project_dir: Path) -> dict[str, Any]:
    """Extract file, render, plugin, and track metadata from a parsed project."""
    result = {
        'audio': set(),
        'midi': set(),
        'plugins': [],
        'notes': '',
        'tracks': 0,
        'items': 0,
        'sources': [],
        'render_files': [],
        'missing_source_count': 0,
        'missing_render_file_count': 0,
        'unstructured': [],
    }

    seen_plugins = set()

    def add_path_entry(raw_path: Any, source_type: str | None, kind: str) -> None:
        entry = _build_path_entry(raw_path, project_dir, kind, source_type)
        if kind == 'source':
            if source_type == 'MIDI':
                result['midi'].add(raw_path)
            elif source_type in {'WAVE', 'VORBIS', 'MP3', 'FLAC'}:
                result['audio'].add(raw_path)
            result['sources'].append(entry)
            if not entry['exists']:
                result['missing_source_count'] += 1
        else:
            result['render_files'].append(entry)
            if not entry['exists']:
                result['missing_render_file_count'] += 1

    def walk(obj: Any, depth: int = 0, source_type: str | None = None) -> None:
        if depth > 100:
            return

        if hasattr(obj, 'token') and hasattr(obj, 'params'):
            handled = False
            if obj.token == 'SOURCE' and hasattr(obj, 'contents'):
                handled = True
                source_type = obj.params[0] if obj.params else '?'
            elif obj.token == 'FILE' and source_type is not None:
                handled = True
                if obj.params:
                    add_path_entry(obj.params[0], source_type, 'source')
            elif obj.token == 'RENDER_FILE':
                handled = True
                if obj.params:
                    add_path_entry(obj.params[0], None, 'render')
                else:
                    add_path_entry('', None, 'render')
            elif obj.token == 'NOTES' and obj.params:
                handled = True
                if isinstance(obj.params[0], str):
                    result['notes'] = obj.params[0]
            elif obj.token in {
                'VST', 'VST2', 'VST3', 'VSTI', 'VSTI2', 'VSTI3',
                'AU', 'AUi', 'JS', 'DX', 'LV2', 'AAX',
            }:
                handled = True
                if len(obj.params) >= 2:
                    raw_ui_name = obj.params[0]
                    ui_name = raw_ui_name if isinstance(raw_ui_name, str) else ''
                    raw_plugin_class = obj.params[1]
                    plugin_class = raw_plugin_class if isinstance(raw_plugin_class, str) else ''
                    plugin_alias = None
                    alias_present = False
                    alias_blank = False
                    alias_corrupted = False
                    if len(obj.params) >= 4:
                        alias_present = True
                        raw_alias = obj.params[3]
                        if isinstance(raw_alias, str):
                            if raw_alias.strip() != '':
                                plugin_alias = raw_alias
                            else:
                                alias_blank = True
                        else:
                            alias_corrupted = True

                    ui_name_blank = isinstance(raw_ui_name, str) and raw_ui_name.strip() == ''
                    ui_name_corrupted = not isinstance(raw_ui_name, str)
                    plugin_class_blank = isinstance(raw_plugin_class, str) and raw_plugin_class.strip() == ''
                    plugin_class_corrupted = not isinstance(raw_plugin_class, str)

                    plugin_key = (ui_name, obj.token, plugin_alias)
                    if plugin_key not in seen_plugins:
                        plugin = {
                            'class': plugin_class,
                            'ui_name': ui_name,
                            'type': obj.token,
                        }
                        if plugin_alias is not None:
                            plugin['alias'] = plugin_alias
                        if alias_present:
                            plugin['alias_present'] = True
                        if alias_blank:
                            plugin['alias_blank'] = True
                        if alias_corrupted:
                            plugin['alias_corrupted'] = True
                        if ui_name_blank:
                            plugin['ui_name_blank'] = True
                        if ui_name_corrupted:
                            plugin['ui_name_corrupted'] = True
                        if plugin_class_blank:
                            plugin['class_blank'] = True
                        if plugin_class_corrupted:
                            plugin['class_corrupted'] = True
                        result['plugins'].append(plugin)
                        seen_plugins.add(plugin_key)
            elif obj.token == 'TRACK':
                handled = True
                result['tracks'] += 1
            elif obj.token == 'ITEM':
                handled = True
                result['items'] += 1
            if not handled and obj.params:
                result['unstructured'].append({
                    'token': obj.token,
                    'params': obj.params,
                })

        if hasattr(obj, 'contents'):
            for item in obj.contents:
                walk(item, depth + 1, source_type)

    walk(project)
    result['audio'] = sorted(result['audio'])
    result['midi'] = sorted(result['midi'])
    return result


def gather_files(paths: Sequence[str]) -> List[Path]:
    """Collect .rpp files from input paths and directories."""
    files: List[Path] = []
    for input_path in paths:
        path = Path(input_path)
        if path.is_dir():
            for root, _, filenames in os.walk(path):
                for filename in filenames:
                    if filename.lower().endswith('.rpp'):
                        files.append(Path(root) / filename)
        elif path.suffix.lower() == '.rpp':
            files.append(path)
    return files


def _audit_project_file(path: str) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
    """Load and audit a single .rpp file, returning its result or error."""
    try:
        path_obj = Path(path)
        with open(path_obj, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()
        project = parse(content)
        if not isinstance(project, ReaperBase):
            raise ValueError('Parsed document is not a Reaper project object')
        return path_obj.name, extract_dependencies(project, path_obj.parent), None
    except Exception as exc:
        return Path(path).name, None, {'path': path, 'error': str(exc)}


def batch_audit(project_paths: Sequence[Path]) -> dict[str, Any]:
    """Audit multiple project files in parallel and return combined results."""
    results: dict[str, Any] = {}
    errors: List[dict[str, str]] = []
    paths = [str(path) for path in project_paths]
    total = len(paths)
    print(f'Auditing {total} project file(s)...')
    progress_every = max(1, total // 10)
    processed = 0
    start_time = time.perf_counter()
    last_len = 0

    with concurrent.futures.ThreadPoolExecutor() as executor:
        for name, deps, error in executor.map(_audit_project_file, paths):
            processed += 1
            if error is not None:
                errors.append(error)
            elif deps is not None:
                results[name] = deps
            if processed % progress_every == 0 or processed == total:
                elapsed = time.perf_counter() - start_time
                eta = (elapsed / processed) * (total - processed) if processed else 0.0
                message = (
                    f'Processed {processed}/{total} projects '
                    f'(elapsed {elapsed:.1f}s, eta {eta:.1f}s)'
                )
                padded = message + ' ' * max(0, last_len - len(message))
                end_char = '\n' if processed == total else '\r'
                print(padded, end=end_char, flush=True)
                last_len = len(message)

    if errors:
        results['_errors'] = errors
    return results


def main() -> None:
    """Parse CLI arguments and run the audit workflow."""
    parser = argparse.ArgumentParser(description='Audit Reaper .rpp files and produce JSON output.')
    parser.add_argument('inputs', nargs='+', help='One or more .rpp files or directories containing .rpp files')
    parser.add_argument('-o', '--output', help='Output JSON file path (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON output')
    args = parser.parse_args()

    files = gather_files(args.inputs)
    if not files:
        raise SystemExit('No .rpp files found in the provided input paths.')

    print(f'Found {len(files)} .rpp file(s) to audit.')
    results = batch_audit(files)
    results['_run_context'] = {
        'cwd': str(Path.cwd()),
        'inputs': args.inputs,
    }
    text = json.dumps(results, indent=2 if args.pretty else None)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as out_fh:
            out_fh.write(text)
    else:
        print(text)


if __name__ == '__main__':
    main()
