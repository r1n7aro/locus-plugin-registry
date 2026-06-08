#!/usr/bin/env python3
import argparse
import filecmp
import hashlib
import json
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ENTRY_ROOT = Path("entries/v1/plugins")
PUBLIC_ROOT = Path("public/v1")
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$|^[A-Za-z0-9]$")
SUMMARY_KEYS = [
    "id", "name", "summary", "summaryI18n", "author", "tags", "latestVersion",
    "updatedAt", "icon", "compatibility", "stats",
]


def bucket(plugin_id: str) -> str:
    return hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:2]


def read_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summary_from_entry(entry):
    return {key: entry[key] for key in SUMMARY_KEYS if key in entry}


def iter_entry_files():
    if not ENTRY_ROOT.exists():
        return []
    return sorted(path for path in ENTRY_ROOT.glob("*/*.json") if path.is_file())


def load_entries():
    entries = []
    seen = {}
    for path in iter_entry_files():
        entry = read_json(path)
        plugin_id = str(entry.get("id", "")).strip()
        if not PLUGIN_ID_RE.match(plugin_id) or "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
            raise SystemExit(f"Invalid plugin id in {path}: {plugin_id!r}")
        expected = ENTRY_ROOT / bucket(plugin_id) / f"{plugin_id}.json"
        if path.as_posix() != expected.as_posix():
            raise SystemExit(f"Entry path mismatch for {plugin_id}: expected {expected}, got {path}")
        if plugin_id in seen:
            raise SystemExit(f"Duplicate plugin id {plugin_id}: {seen[plugin_id]} and {path}")
        if not str(entry.get("name", "")).strip():
            raise SystemExit(f"Plugin entry {path} is missing name")
        if not str(entry.get("latestVersion", "")).strip():
            raise SystemExit(f"Plugin entry {path} is missing latestVersion")
        download = entry.get("download") or {}
        if not str(download.get("url", "")).strip():
            raise SystemExit(f"Plugin entry {path} is missing download.url")
        if not str(download.get("sha256", "")).strip():
            raise SystemExit(f"Plugin entry {path} is missing download.sha256")
        seen[plugin_id] = path
        entries.append(entry)
    entries.sort(key=lambda item: item["id"])
    return entries


def validate_download(entry):
    download = entry.get("download") or {}
    url = download.get("url", "")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != str(download.get("sha256", "")).lower():
        raise SystemExit(f"Download sha256 mismatch for {entry['id']}: {digest}")
    size = download.get("sizeBytes")
    if size is not None and int(size) != len(data):
        raise SystemExit(f"Download size mismatch for {entry['id']}: {len(data)}")
    with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
        handle.write(data)
        handle.flush()
        with zipfile.ZipFile(handle.name) as archive:
            manifest = json.loads(archive.read("locus.plugin.json").decode("utf-8-sig"))
    if manifest.get("id") != entry["id"]:
        raise SystemExit(f"Archive id mismatch for {entry['id']}: {manifest.get('id')}")
    if manifest.get("version") != entry.get("latestVersion"):
        raise SystemExit(f"Archive version mismatch for {entry['id']}: {manifest.get('version')}")


def build_index(entries, output_root=PUBLIC_ROOT):
    if output_root.exists():
        shutil.rmtree(output_root)
    plugins_by_bucket = {}
    for entry in entries:
        item_bucket = bucket(entry["id"])
        plugins_by_bucket.setdefault(item_bucket, []).append(summary_from_entry(entry))
        write_json(output_root / "plugins" / item_bucket / f"{entry['id']}.json", entry)

    available = sorted(plugins_by_bucket)
    shard_meta = {}
    for item_bucket in available:
        shard = {
            "schemaVersion": 1,
            "bucket": item_bucket,
            "plugins": sorted(plugins_by_bucket[item_bucket], key=lambda item: item["id"]),
        }
        shard_path = output_root / "shards" / f"{item_bucket}.json"
        write_json(shard_path, shard)
        shard_meta[item_bucket] = {
            "count": len(shard["plugins"]),
            "sha256": hashlib.sha256(shard_path.read_bytes()).hexdigest(),
        }

    summaries = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "plugins": [summary_from_entry(entry) for entry in entries],
    }
    write_json(output_root / "search" / "summaries.json", summaries)
    manifest = {
        "schemaVersion": 1,
        "registryVersion": 1,
        "generatedAt": summaries["generatedAt"],
        "entryCount": len(entries),
        "bucketStrategy": "sha256-id-prefix-2",
        "bucketCount": 256,
        "entryBasePath": "plugins",
        "summaryBasePath": "shards",
        "searchIndexPath": "search/summaries.json",
        "availableBuckets": available,
        "shards": shard_meta,
    }
    write_json(output_root / "manifest.json", manifest)


def compare_dirs(left: Path, right: Path):
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
        return False
    return all(compare_dirs(Path(left, name), Path(right, name)) for name in comparison.common_dirs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-downloads", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    entries = load_entries()
    if args.validate_downloads:
        for entry in entries:
            validate_download(entry)
    if args.validate_only:
        return
    if args.check:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated = Path(temp_dir) / "public" / "v1"
            build_index(entries, generated)
            if not PUBLIC_ROOT.exists() or not compare_dirs(generated, PUBLIC_ROOT):
                raise SystemExit("Generated registry index is stale. Run scripts/registry_index.py.")
        return
    build_index(entries)


if __name__ == "__main__":
    main()
