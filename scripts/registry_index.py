#!/usr/bin/env python3
"""Builds the public Locus plugin registry from the source entries.

Source entries live in entries/v1/plugins/<bucket>/<plugin-id>.json. Two
public indexes are generated with the same layout (manifest.json, shards/,
plugins/, search/summaries.json):

- public/v1: plugins whose archive's locus.plugin.json uses no manifest v2
  features (`schemaVersion` 2 or `contributes`), for Locus 0.9.3 and older.
  Its entries keep the original v1 shape.
- public/v2: plugins that need manifest v2, for later Locus releases. Entries
  also carry `compatibility.locus`, `manifestSchemaVersion` and
  `requiresManifestV2`, read from the archive.

Each plugin is in exactly one index. The previous public index is the cache;
a v1 entry stands for a plugin that needs no manifest v2. A release asset whose URL and
SHA-256 digest (or, without a digest, size and release time) are unchanged
is not downloaded again, so registry CI no longer counts towards release
download numbers. Stars and release downloads refresh only with
--refresh-stats (daily in CI) or for plugins seen for the first time. A
plugin that fails to resolve keeps its previous public entry and is
reported as a warning instead of stopping the whole build. An index whose
content did not change keeps its files, including `generatedAt`.
"""
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
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ENTRY_ROOT = Path("entries/v1/plugins")
PUBLIC_ROOT = Path("public")
INDEX_VERSIONS = ("v1", "v2")
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$|^[A-Za-z0-9]$")
SUMMARY_KEYS = [
    "id", "name", "summary", "summaryI18n", "author", "tags", "latestVersion",
    "updatedAt", "icon", "compatibility", "stats",
]
V2_ONLY_ENTRY_KEYS = ("manifestSchemaVersion", "requiresManifestV2")
GITHUB_API = "https://api.github.com"
LEGACY_ENTRY_KEYS = {"schema", "version", "homepage", "repository", "components"}
GENERATED_ENTRY_KEYS = {"latestVersion", "updatedAt", "download", *V2_ONLY_ENTRY_KEYS}
REQUIRED_ENTRY_TEXT_KEYS = ["author", "repo", "license", "summary", "description"]
REQUIRED_STAT_IDS = {"githubStars", "releaseDownloads"}
RELEASE_KINDS = {"latestrelease", "githublatestrelease", "release", "releasetag", "githubrelease"}
URL_KINDS = {"url", "archive", "zip"}


class ResolveError(Exception):
    """A plugin could not be resolved from its release or archive."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bucket(plugin_id: str) -> str:
    return hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:2]


def read_json(path: Path):
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def warn(message: str):
    print(f"::warning::{message}")


# ── network ──────────────────────────────────────────────────────────────


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
        raise ResolveError(f"Failed to fetch {label}: HTTP {error.code} {detail}")
    except urllib.error.URLError as error:
        raise ResolveError(f"Failed to fetch {label}: {error}")


def fetch_json(url: str, label: str):
    return json.loads(fetch_bytes(url, label).decode("utf-8-sig"))


def download_archive(url: str, label: str) -> bytes:
    """The only call that downloads a plugin archive (and so counts as a download)."""
    return fetch_bytes(url, label)


def parse_github_repo(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().removesuffix(".git").strip("/")
    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc.lower() != "github.com":
            raise ValueError(f"GitHub repo must use github.com: {value}")
        raw = parsed.path.strip("/").removesuffix(".git")
    parts = [part for part in raw.split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"GitHub repo must be owner/repo: {value}")
    return parts[0], parts[1]


def github_release_url(owner: str, repo: str, tag):
    if tag:
        return f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    return f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"


def fetch_github_stats(owner: str, repo: str, plugin_id: str):
    repo_info = fetch_json(f"{GITHUB_API}/repos/{owner}/{repo}", f"GitHub repo metadata for {plugin_id}")
    total = 0
    page = 1
    while True:
        releases = fetch_json(
            f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=100&page={page}",
            f"GitHub releases for {owner}/{repo}",
        )
        if not isinstance(releases, list) or not releases:
            break
        for release in releases:
            for asset in release.get("assets") or []:
                total += int(asset.get("download_count") or 0)
        if len(releases) < 100:
            break
        page += 1
    return {"githubStars": int(repo_info.get("stargazers_count") or 0), "releaseDownloads": total}


# ── source entries ───────────────────────────────────────────────────────


def normalize_source_kind(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def source_ref(source, *keys):
    for key in keys:
        value = str(source.get(key, "")).strip()
        if value:
            return value
    return ""


def source_has_value(source) -> bool:
    if not isinstance(source, dict):
        return False
    keys = [
        "type", "input", "url", "repo", "ref", "branch", "tag", "commit",
        "asset", "assetPattern", "asset_pattern", "sha256", "version",
    ]
    return any(str(source.get(key, "")).strip() for key in keys) or source.get("sizeBytes") is not None


def entry_github_repo(entry):
    source = entry.get("downloadSource") or {}
    repo_value = source_ref(source, "repo") or str(entry.get("repo", "")).strip()
    if not repo_value:
        return None
    return parse_github_repo(repo_value)


def validate_entry(entry, path: Path):
    """Offline checks of one source entry; raises SystemExit on authoring errors."""
    plugin_id = str(entry.get("id", "")).strip()
    if not PLUGIN_ID_RE.match(plugin_id) or "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
        raise SystemExit(f"Invalid plugin id in {path}: {plugin_id!r}")
    expected = ENTRY_ROOT / bucket(plugin_id) / f"{plugin_id}.json"
    if path.as_posix() != expected.as_posix():
        raise SystemExit(f"Entry path mismatch for {plugin_id}: expected {expected}, got {path}")
    if not str(entry.get("name", "")).strip():
        raise SystemExit(f"Plugin entry {path} is missing name")
    legacy = sorted(key for key in LEGACY_ENTRY_KEYS if key in entry)
    if legacy:
        raise SystemExit(
            f"Plugin entry {path} uses legacy registry fields {legacy}; use schemaVersion, repo, author, stats, and downloadSource instead"
        )
    for key in REQUIRED_ENTRY_TEXT_KEYS:
        if not str(entry.get(key, "")).strip():
            raise SystemExit(f"Plugin entry {path} is missing {key}")
    stats = entry.get("stats")
    if not isinstance(stats, list) or not stats:
        raise SystemExit(f"Plugin entry {path} is missing stats")
    stat_ids = set()
    for stat in stats:
        if not isinstance(stat, dict):
            raise SystemExit(f"Plugin entry {path} has invalid stats item")
        stat_id = str(stat.get("id", "")).strip()
        if not stat_id or not str(stat.get("label", "")).strip() or "value" not in stat:
            raise SystemExit(f"Plugin entry {path} has incomplete stats item")
        stat_ids.add(stat_id)
    missing_stats = sorted(REQUIRED_STAT_IDS - stat_ids)
    if missing_stats:
        raise SystemExit(f"Plugin entry {path} is missing required stats {missing_stats}")
    source = entry.get("downloadSource") or {}
    if source_has_value(source):
        kind = normalize_source_kind(source.get("type"))
        if kind not in RELEASE_KINDS | URL_KINDS:
            raise SystemExit(f"Unsupported downloadSource type for {plugin_id}: {source.get('type')!r}")
        if kind in {"release", "releasetag", "githubrelease"} and not source_ref(source, "tag", "ref"):
            raise SystemExit(f"downloadSource release tag is required for {plugin_id}")
        if kind in URL_KINDS and not source_ref(source, "url", "input"):
            raise SystemExit(f"downloadSource url is required for {plugin_id}")
    else:
        download = entry.get("download") or {}
        if not str(entry.get("latestVersion", "")).strip() or not str(download.get("url", "")).strip():
            raise SystemExit(f"Plugin entry {path} needs downloadSource, or latestVersion and download.url")
    try:
        entry_github_repo(entry)
    except ValueError as error:
        raise SystemExit(f"Plugin entry {path}: {error}")
    return plugin_id


def iter_entry_files():
    if not ENTRY_ROOT.exists():
        return []
    return sorted(path for path in ENTRY_ROOT.glob("*/*.json") if path.is_file())


def load_source_entries():
    entries = []
    seen = {}
    for path in iter_entry_files():
        entry = read_json(path)
        plugin_id = validate_entry(entry, path)
        if plugin_id in seen:
            raise SystemExit(f"Duplicate plugin id {plugin_id}: {seen[plugin_id]} and {path}")
        entry["id"] = plugin_id
        seen[plugin_id] = path
        entries.append((path, entry))
    return entries


# ── previous public index (the cache) ────────────────────────────────────


def previous_entry(plugin_id: str):
    """The last generated public entry and whether v1 listed it.

    Once public/v2 exists, v1 is written by this script and lists only
    plugins that need no manifest v2, so a v1 entry gets that manifest info
    and serves as the cache. A v1 index from before the split (no v2 yet)
    does not: its archives are read once.
    """
    item_bucket = bucket(plugin_id)
    in_v1 = (PUBLIC_ROOT / "v1" / "plugins" / item_bucket / f"{plugin_id}.json").is_file()
    for version in ("v2", "v1"):
        path = PUBLIC_ROOT / version / "plugins" / item_bucket / f"{plugin_id}.json"
        if not path.is_file():
            continue
        try:
            entry = read_json(path)
        except (OSError, ValueError):
            return None, in_v1
        if version == "v1" and split_index_exists() and isinstance(entry, dict) and not has_manifest_info(entry):
            entry = {**entry, "manifestSchemaVersion": 1, "requiresManifestV2": False}
        return entry, in_v1
    return None, in_v1


def split_index_exists() -> bool:
    return (PUBLIC_ROOT / "v2" / "manifest.json").is_file()


def has_manifest_info(entry) -> bool:
    return isinstance(entry, dict) and "manifestSchemaVersion" in entry and "requiresManifestV2" in entry


# ── archives ─────────────────────────────────────────────────────────────


def inspect_archive(plugin_id: str, data: bytes, label: str):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("locus.plugin.json").decode("utf-8-sig"))
    except KeyError:
        raise ResolveError(f"Archive for {plugin_id} is missing root locus.plugin.json: {label}")
    except zipfile.BadZipFile:
        raise ResolveError(f"Archive for {plugin_id} is not a valid zip: {label}")
    except ValueError:
        raise ResolveError(f"Archive for {plugin_id} has an invalid locus.plugin.json: {label}")
    if not isinstance(manifest, dict) or manifest.get("id") != plugin_id:
        raise ResolveError(f"Archive id mismatch for {plugin_id}: {manifest.get('id') if isinstance(manifest, dict) else None}")
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise ResolveError(f"Archive for {plugin_id} is missing manifest version")
    schema_version = manifest.get("schemaVersion", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        schema_version = 1
    contributes = manifest.get("contributes")
    compatibility = manifest.get("compatibility") if isinstance(manifest.get("compatibility"), dict) else {}
    locus_range = compatibility.get("locus")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "sizeBytes": len(data),
        "version": version,
        "manifestSchemaVersion": schema_version,
        "requiresManifestV2": schema_version >= 2 or (contributes is not None and contributes != {}),
        "locus": locus_range.strip() if isinstance(locus_range, str) and locus_range.strip() else None,
    }


def cached_archive(previous):
    """Archive facts of the previous public entry, when it has them."""
    if not has_manifest_info(previous):
        return None
    download = previous.get("download") or {}
    return {
        "sha256": str(download.get("sha256", "")).lower(),
        "sizeBytes": download.get("sizeBytes"),
        "version": previous.get("latestVersion"),
        "manifestSchemaVersion": previous.get("manifestSchemaVersion"),
        "requiresManifestV2": previous.get("requiresManifestV2") is True,
        "locus": (previous.get("compatibility") or {}).get("locus"),
    }


def select_release_asset(release, source, plugin_id: str):
    assets = release.get("assets") or []
    exact = source_ref(source, "asset")
    pattern = source_ref(source, "assetPattern", "asset_pattern")
    if exact:
        for asset in assets:
            if asset.get("name") == exact:
                return asset
        raise ResolveError(f"Release asset not found for {plugin_id}: {exact}")
    if pattern:
        matches = [asset for asset in assets if fnmatch.fnmatchcase(str(asset.get("name", "")), pattern)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(asset.get("name", "") for asset in matches)
            raise ResolveError(f"Release asset pattern is ambiguous for {plugin_id}: {pattern} matched {names}")
        raise ResolveError(f"Release asset pattern matched no assets for {plugin_id}: {pattern}")
    zip_assets = [asset for asset in assets if str(asset.get("name", "")).lower().endswith(".zip")]
    if len(zip_assets) == 1:
        return zip_assets[0]
    if len(zip_assets) > 1:
        names = ", ".join(asset.get("name", "") for asset in zip_assets)
        raise ResolveError(f"Release has multiple zip assets for {plugin_id}; set downloadSource.asset or downloadSource.assetPattern. Assets: {names}")
    if len(assets) == 1:
        return assets[0]
    raise ResolveError(f"Release has no selectable plugin archive asset for {plugin_id}")


def asset_digest(asset) -> str:
    digest = str(asset.get("digest") or "").strip().lower()
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else ""


def resolve_release(entry, source, kind, previous):
    plugin_id = entry["id"]
    repo_value = source_ref(source, "repo") or str(entry.get("repo", "")).strip()
    owner, repo = parse_github_repo(repo_value)
    tag = source_ref(source, "tag", "ref") if kind in {"release", "releasetag", "githubrelease"} else None
    release = fetch_json(github_release_url(owner, repo, tag), f"GitHub release metadata for {plugin_id}")
    asset = select_release_asset(release, source, plugin_id)
    url = str(asset.get("browser_download_url", "")).strip()
    if not url:
        raise ResolveError(f"Release asset for {plugin_id} has no download URL")
    published = release.get("published_at") or release.get("created_at") or utc_now()
    digest = asset_digest(asset)
    cached = cached_archive(previous)
    if cached and (previous.get("download") or {}).get("url") == url:
        unchanged = (
            cached["sha256"] == digest
            if digest
            else cached["sizeBytes"] == asset.get("size") and previous.get("updatedAt") == published
        )
        if unchanged:
            return {**cached, "url": url, "updatedAt": published}, "cached"
    data = download_archive(url, f"plugin archive for {plugin_id}")
    archive = inspect_archive(plugin_id, data, url)
    if digest and archive["sha256"] != digest:
        raise ResolveError(f"Archive sha256 for {plugin_id} does not match the release asset digest")
    return {**archive, "url": url, "updatedAt": published}, "downloaded"


def resolve_url(entry, url, expected_sha, previous, recheck):
    plugin_id = entry["id"]
    cached = cached_archive(previous)
    if cached and not recheck and (previous.get("download") or {}).get("url") == url:
        if not expected_sha or cached["sha256"] == expected_sha:
            return {**cached, "url": url, "updatedAt": previous.get("updatedAt") or utc_now()}, "cached"
    data = download_archive(url, f"plugin archive for {plugin_id}")
    archive = inspect_archive(plugin_id, data, url)
    if expected_sha and archive["sha256"] != expected_sha:
        raise ResolveError(f"Download sha256 mismatch for {plugin_id}: {archive['sha256']}")
    updated = previous.get("updatedAt") if cached and cached["sha256"] == archive["sha256"] else utc_now()
    return {**archive, "url": url, "updatedAt": updated}, "downloaded"


def resolve_archive(entry, previous, recheck_urls: bool):
    source = entry.get("downloadSource") or {}
    if source_has_value(source):
        kind = normalize_source_kind(source.get("type"))
        if kind in RELEASE_KINDS:
            return resolve_release(entry, source, kind, previous)
        url = source_ref(source, "url", "input")
        return resolve_url(entry, url, str(source.get("sha256", "")).strip().lower(), previous, recheck_urls)
    download = entry.get("download") or {}
    resolved, status = resolve_url(
        entry, str(download["url"]).strip(), str(download.get("sha256", "")).strip().lower(), previous, recheck_urls,
    )
    if resolved["version"] != str(entry.get("latestVersion", "")).strip():
        raise ResolveError(f"Archive version mismatch for {entry['id']}: {resolved['version']}")
    return resolved, status


def apply_stats(entry, previous, refresh: bool):
    stats = entry.get("stats")
    if not isinstance(stats, list):
        return
    values = None
    if refresh or previous is None:
        github_repo = entry_github_repo(entry)
        if github_repo:
            values = fetch_github_stats(*github_repo, entry["id"])
    if values is None and previous is not None:
        values = {
            stat.get("id"): stat.get("value")
            for stat in previous.get("stats") or []
            if isinstance(stat, dict) and "value" in stat
        }
    for stat in stats:
        if isinstance(stat, dict) and values and stat.get("id") in values:
            stat["value"] = values[stat["id"]]


def public_entry(entry, archive):
    """The v2 public entry for a source entry and its resolved archive."""
    resolved = copy.deepcopy(entry)
    for key in GENERATED_ENTRY_KEYS - {"latestVersion", "download"}:
        resolved.pop(key, None)
    resolved["latestVersion"] = archive["version"]
    resolved["updatedAt"] = archive["updatedAt"]
    resolved["download"] = {"url": archive["url"], "sha256": archive["sha256"], "sizeBytes": archive["sizeBytes"]}
    source = resolved.get("downloadSource")
    if source_has_value(source):
        source["version"] = archive["version"]
    compatibility = dict(resolved.get("compatibility") or {})
    compatibility.pop("locus", None)
    if archive.get("locus"):
        compatibility["locus"] = archive["locus"]
    if compatibility or "compatibility" in resolved:
        resolved["compatibility"] = compatibility
    resolved["manifestSchemaVersion"] = archive["manifestSchemaVersion"]
    resolved["requiresManifestV2"] = archive["requiresManifestV2"]
    return resolved


def v1_entry(entry):
    """A v2 public entry in the original v1 shape."""
    legacy = copy.deepcopy(entry)
    for key in V2_ONLY_ENTRY_KEYS:
        legacy.pop(key, None)
    compatibility = legacy.get("compatibility")
    if isinstance(compatibility, dict):
        compatibility.pop("locus", None)
        if not compatibility:
            legacy.pop("compatibility")
    return legacy


def resolve_entries(sources, refresh_stats: bool, strict_ids=None):
    """Public entries in the v2 shape, the ids v1 lists, and one report row per plugin."""
    strict_ids = set(strict_ids or [])
    resolved = []
    v1_ids = set()
    report = []
    for path, entry in sources:
        plugin_id = entry["id"]
        previous, previously_in_v1 = previous_entry(plugin_id)
        try:
            archive, status = resolve_archive(entry, previous, recheck_urls=refresh_stats)
            item = public_entry(entry, archive)
        except (ResolveError, ValueError, KeyError) as error:
            if plugin_id in strict_ids:
                raise SystemExit(f"{path}: {error}")
            if previous is not None and has_manifest_info(previous):
                warn(f"{plugin_id}: {error}; keeping the previous public entry")
                resolved.append(previous)
                if previously_in_v1:
                    v1_ids.add(plugin_id)
                report.append((plugin_id, previous.get("latestVersion"), "kept previous", str(error)))
            elif previous is not None:
                # Only a v1 entry so far: keep it in v1, where it was.
                warn(f"{plugin_id}: {error}; keeping the previous v1 entry")
                resolved.append({**previous, "manifestSchemaVersion": 1, "requiresManifestV2": False})
                v1_ids.add(plugin_id)
                report.append((plugin_id, previous.get("latestVersion"), "kept previous", str(error)))
            else:
                warn(f"{plugin_id}: {error}; not listed until it resolves")
                report.append((plugin_id, None, "skipped", str(error)))
            continue
        try:
            apply_stats(item, previous, refresh_stats)
        except (ResolveError, ValueError) as error:
            warn(f"{plugin_id}: stats not refreshed: {error}")
            if previous is not None:
                apply_stats(item, previous, False)
        resolved.append(item)
        if not item["requiresManifestV2"]:
            v1_ids.add(plugin_id)
        report.append((plugin_id, item["latestVersion"], status, ""))
    resolved.sort(key=lambda item: item["id"])
    return resolved, v1_ids, report


# ── output ───────────────────────────────────────────────────────────────


def summary_from_entry(entry):
    return {key: entry[key] for key in SUMMARY_KEYS if key in entry}


def build_index(entries, output_root: Path, registry_version: int, generated_at: str):
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
    write_json(output_root / "search" / "summaries.json", {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "plugins": [summary_from_entry(entry) for entry in entries],
    })
    write_json(output_root / "manifest.json", {
        "schemaVersion": 1,
        "registryVersion": registry_version,
        "generatedAt": generated_at,
        "entryCount": len(entries),
        "bucketStrategy": "sha256-id-prefix-2",
        "bucketCount": 256,
        "entryBasePath": "plugins",
        "summaryBasePath": "shards",
        "searchIndexPath": "search/summaries.json",
        "availableBuckets": available,
        "shards": shard_meta,
    })


def same_tree(left: Path, right: Path) -> bool:
    comparison = filecmp.dircmp(left, right)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(left, right, comparison.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(same_tree(left / name, right / name) for name in comparison.common_dirs)


def write_index(entries, output_root: Path, registry_version: int) -> bool:
    """Writes one index; returns False (and leaves the files) when nothing changed."""
    previous_generated = None
    manifest_path = output_root / "manifest.json"
    if manifest_path.is_file():
        try:
            previous_generated = read_json(manifest_path).get("generatedAt")
        except (OSError, ValueError):
            previous_generated = None
    if previous_generated:
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "index"
            build_index(entries, candidate, registry_version, previous_generated)
            if same_tree(candidate, output_root):
                return False
    build_index(entries, output_root, registry_version, utc_now())
    return True


def write_report(report, changed):
    lines = ["| Plugin | Version | Archive | Note |", "|---|---|---|---|"]
    for plugin_id, version, status, note in report:
        lines.append(f"| {plugin_id} | {version or '-'} | {status} | {note.replace('|', '/')} |")
    lines.append("")
    lines.append("Changed indexes: " + (", ".join(changed) if changed else "none"))
    text = "\n".join(lines)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("## Registry index\n\n" + text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh-stats", action="store_true",
                        help="refresh stars and release downloads (daily in CI) and recheck plain URL archives")
    parser.add_argument("--validate-only", action="store_true",
                        help="check source entries without writing; --strict-entries are also resolved")
    parser.add_argument("--strict-entries", nargs="*", default=[],
                        help="entry files that must resolve (fail instead of keeping the previous entry)")
    args = parser.parse_args(argv)

    sources = load_source_entries()
    strict_paths = {Path(path).as_posix() for path in args.strict_entries if path}
    strict_ids = {entry["id"] for path, entry in sources if path.as_posix() in strict_paths}
    if args.validate_only:
        if strict_ids:
            subset = [(path, entry) for path, entry in sources if entry["id"] in strict_ids]
            _, _, report = resolve_entries(subset, refresh_stats=False, strict_ids=strict_ids)
            write_report(report, [])
        print(f"Validated {len(sources)} source entries.")
        return 0

    entries, v1_ids, report = resolve_entries(sources, args.refresh_stats)
    changed = []
    if write_index([v1_entry(entry) for entry in entries if entry["id"] in v1_ids], PUBLIC_ROOT / "v1", 1):
        changed.append("v1")
    if write_index([entry for entry in entries if entry["id"] not in v1_ids], PUBLIC_ROOT / "v2", 2):
        changed.append("v2")
    write_report(report, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
