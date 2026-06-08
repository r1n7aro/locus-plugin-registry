#!/usr/bin/env python3
import argparse
import copy
import filecmp
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
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
GITHUB_API = "https://api.github.com"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def normalize_source_kind(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def source_has_value(source) -> bool:
    if not isinstance(source, dict):
        return False
    keys = [
        "type", "input", "url", "repo", "ref", "branch", "tag", "commit",
        "asset", "assetPattern", "asset_pattern", "sha256", "version",
    ]
    return any(str(source.get(key, "")).strip() for key in keys) or source.get("sizeBytes") is not None


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Locus-Plugin-Registry-CI",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url: str, label: str) -> bytes:
    request = urllib.request.Request(url, headers=github_headers())
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"Failed to fetch {label}: HTTP {error.code} {detail}")
    except urllib.error.URLError as error:
        raise SystemExit(f"Failed to fetch {label}: {error}")


def fetch_json(url: str, label: str):
    return json.loads(fetch_bytes(url, label).decode("utf-8-sig"))


def parse_github_repo(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().removesuffix(".git").strip("/")
    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc.lower() != "github.com":
            raise SystemExit(f"GitHub downloadSource repo must use github.com: {value}")
        raw = parsed.path.strip("/").removesuffix(".git")
    parts = [part for part in raw.split("/") if part]
    if len(parts) != 2:
        raise SystemExit(f"GitHub downloadSource repo must be owner/repo: {value}")
    return parts[0], parts[1]


def github_release_url(owner: str, repo: str, tag):
    if tag:
        encoded_tag = urllib.parse.quote(tag, safe="")
        return f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{encoded_tag}"
    return f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"


def source_ref(source, *keys):
    for key in keys:
        value = str(source.get(key, "")).strip()
        if value:
            return value
    return ""


def select_release_asset(release, source, plugin_id: str):
    assets = release.get("assets") or []
    exact = source_ref(source, "asset")
    pattern = source_ref(source, "assetPattern", "asset_pattern")
    if exact:
        for asset in assets:
            if asset.get("name") == exact:
                return asset
        raise SystemExit(f"Release asset not found for {plugin_id}: {exact}")
    if pattern:
        matches = [asset for asset in assets if fnmatch.fnmatchcase(str(asset.get("name", "")), pattern)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(asset.get("name", "") for asset in matches)
            raise SystemExit(f"Release asset pattern is ambiguous for {plugin_id}: {pattern} matched {names}")
        raise SystemExit(f"Release asset pattern matched no assets for {plugin_id}: {pattern}")
    zip_assets = [asset for asset in assets if str(asset.get("name", "")).lower().endswith(".zip")]
    if len(zip_assets) == 1:
        return zip_assets[0]
    if len(zip_assets) > 1:
        names = ", ".join(asset.get("name", "") for asset in zip_assets)
        raise SystemExit(f"Release has multiple zip assets for {plugin_id}; set downloadSource.asset or downloadSource.assetPattern. Assets: {names}")
    if len(assets) == 1:
        return assets[0]
    raise SystemExit(f"Release has no selectable plugin archive asset for {plugin_id}")


def inspect_archive(entry, data: bytes, label: str):
    digest = hashlib.sha256(data).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("locus.plugin.json").decode("utf-8-sig"))
    except KeyError:
        raise SystemExit(f"Archive for {entry['id']} is missing root locus.plugin.json: {label}")
    except zipfile.BadZipFile:
        raise SystemExit(f"Archive for {entry['id']} is not a valid zip: {label}")
    if manifest.get("id") != entry["id"]:
        raise SystemExit(f"Archive id mismatch for {entry['id']}: {manifest.get('id')}")
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise SystemExit(f"Archive for {entry['id']} is missing manifest version")
    return {"sha256": digest, "sizeBytes": len(data), "version": version}


def resolve_download_source(entry):
    source = entry.get("downloadSource") or {}
    kind = normalize_source_kind(source.get("type"))
    plugin_id = entry["id"]
    if kind in {"latestrelease", "githublatestrelease", "release", "releasetag", "githubrelease"}:
        repo_value = source_ref(source, "repo") or str(entry.get("repo", "")).strip()
        owner, repo = parse_github_repo(repo_value)
        tag = None
        if kind in {"release", "releasetag", "githubrelease"}:
            tag = source_ref(source, "tag", "ref")
            if not tag:
                raise SystemExit(f"downloadSource release tag is required for {plugin_id}")
        release = fetch_json(github_release_url(owner, repo, tag), f"GitHub release metadata for {plugin_id}")
        asset = select_release_asset(release, source, plugin_id)
        url = str(asset.get("browser_download_url", "")).strip()
        if not url:
            raise SystemExit(f"Release asset for {plugin_id} has no download URL")
        data = fetch_bytes(url, f"plugin archive for {plugin_id}")
        resolved = inspect_archive(entry, data, url)
        resolved["url"] = url
        resolved["updatedAt"] = release.get("published_at") or release.get("created_at") or utc_now()
        return resolved
    if kind in {"url", "archive", "zip"}:
        url = source_ref(source, "url", "input")
        if not url:
            raise SystemExit(f"downloadSource url is required for {plugin_id}")
        data = fetch_bytes(url, f"plugin archive for {plugin_id}")
        resolved = inspect_archive(entry, data, url)
        resolved["url"] = url
        resolved["updatedAt"] = utc_now()
        return resolved
    raise SystemExit(f"Unsupported downloadSource type for {plugin_id}: {source.get('type')!r}")


def validate_static_download(entry, validate_downloads: bool):
    download = entry.get("download") or {}
    url = str(download.get("url", "")).strip()
    sha256 = str(download.get("sha256", "")).strip().lower()
    latest = str(entry.get("latestVersion", "")).strip()
    if not latest:
        raise SystemExit(f"Plugin entry for {entry['id']} is missing latestVersion")
    if not url:
        raise SystemExit(f"Plugin entry for {entry['id']} is missing download.url")
    if not sha256:
        raise SystemExit(f"Plugin entry for {entry['id']} is missing download.sha256")
    if not validate_downloads:
        return
    data = fetch_bytes(url, f"plugin archive for {entry['id']}")
    resolved = inspect_archive(entry, data, url)
    if resolved["sha256"] != sha256:
        raise SystemExit(f"Download sha256 mismatch for {entry['id']}: {resolved['sha256']}")
    size = download.get("sizeBytes")
    if size is not None and int(size) != resolved["sizeBytes"]:
        raise SystemExit(f"Download size mismatch for {entry['id']}: {resolved['sizeBytes']}")
    if resolved["version"] != latest:
        raise SystemExit(f"Archive version mismatch for {entry['id']}: {resolved['version']}")


def resolve_entry(entry, validate_downloads: bool):
    resolved_entry = copy.deepcopy(entry)
    source = resolved_entry.get("downloadSource") or {}
    if source_has_value(source):
        resolved = resolve_download_source(resolved_entry)
        resolved_entry["latestVersion"] = resolved["version"]
        resolved_entry["updatedAt"] = resolved.get("updatedAt") or resolved_entry.get("updatedAt") or utc_now()
        resolved_entry["download"] = {
            "url": resolved["url"],
            "sha256": resolved["sha256"],
            "sizeBytes": resolved["sizeBytes"],
        }
        source["version"] = resolved["version"]
        resolved_entry["downloadSource"] = source
    else:
        validate_static_download(resolved_entry, validate_downloads)
    return resolved_entry


def load_entries(validate_downloads: bool):
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
        entry["id"] = plugin_id
        seen[plugin_id] = path
        entries.append(resolve_entry(entry, validate_downloads))
    entries.sort(key=lambda item: item["id"])
    return entries


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
        "generatedAt": utc_now(),
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

    entries = load_entries(args.validate_downloads)
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


