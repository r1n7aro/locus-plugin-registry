"""Tests of scripts/registry_index.py against a fake GitHub API.

    python -m unittest discover -s tests -v

No network access is needed.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("registry_index", ROOT / "scripts" / "registry_index.py")
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


def archive(plugin_id: str, version: str, **manifest) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr("locus.plugin.json", json.dumps({"id": plugin_id, "version": version, **manifest}))
        handle.writestr("README.md", f"# {plugin_id} {version}")
    return buffer.getvalue()


class FakeGitHub:
    """Releases, repo metadata and archive downloads, with a download counter."""

    def __init__(self):
        self.releases = {}
        self.stars = {}
        self.archives = {}
        self.downloads = []
        self.stat_calls = []
        self.failing = set()

    def publish(self, repo: str, plugin_id: str, version: str, *, digest=True, name=None, **manifest):
        data = archive(plugin_id, version, **manifest)
        name = name or f"{plugin_id}-{version}.zip"
        url = f"https://github.com/{repo}/releases/download/v{version}/{name}"
        self.archives[url] = data
        asset = {"name": name, "browser_download_url": url, "size": len(data), "download_count": 7}
        if digest:
            asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
        self.releases[repo] = {"tag_name": f"v{version}", "published_at": f"2026-11-0{len(self.archives) % 9 + 1}T00:00:00Z", "assets": [asset]}
        return url

    def fetch_json(self, url, label):
        repo = "/".join(url.split("/repos/", 1)[1].split("/")[:2])
        if repo in self.failing:
            raise registry.ResolveError(f"Failed to fetch {label}: HTTP 404")
        if url.endswith("/releases/latest"):
            return self.releases[repo]
        if "/releases?" in url:
            self.stat_calls.append(repo)
            return [self.releases[repo]] if repo in self.releases and "page=1" in url else []
        return {"stargazers_count": self.stars.get(repo, 0)}

    def download(self, url, label):
        self.downloads.append(url)
        return self.archives[url]


def source_entry(plugin_id: str, repo: str, **extra):
    entry = {
        "schemaVersion": 1,
        "id": plugin_id,
        "name": plugin_id.title(),
        "author": "tester",
        "repo": repo,
        "license": "MIT",
        "summary": f"{plugin_id} summary",
        "description": f"{plugin_id} description",
        "tags": ["test"],
        "compatibility": {"projectIndependent": True},
        "downloadSource": {"type": "latestRelease", "repo": repo, "assetPattern": f"{plugin_id}-*.zip"},
        "stats": [
            {"id": "githubStars", "label": "Stars", "value": 0},
            {"id": "releaseDownloads", "label": "Release downloads", "value": 0},
        ],
    }
    entry.update(extra)
    return entry


class RegistryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.github = FakeGitHub()
        self.patches = {
            "fetch_json": registry.fetch_json,
            "download_archive": registry.download_archive,
        }
        registry.fetch_json = self.github.fetch_json
        registry.download_archive = self.github.download

    def tearDown(self):
        registry.fetch_json = self.patches["fetch_json"]
        registry.download_archive = self.patches["download_archive"]
        os.chdir(self.cwd)
        self.temp.cleanup()

    def add_entry(self, entry):
        path = registry.ENTRY_ROOT / registry.bucket(entry["id"]) / f"{entry['id']}.json"
        registry.write_json(path, entry)
        return path

    def public(self, version: str, plugin_id: str):
        path = registry.PUBLIC_ROOT / version / "plugins" / registry.bucket(plugin_id) / f"{plugin_id}.json"
        return registry.read_json(path) if path.is_file() else None

    def manifest(self, version: str):
        return registry.read_json(registry.PUBLIC_ROOT / version / "manifest.json")

    def run_index(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return registry.main(list(args))

    def test_classic_plugins_go_to_both_indexes_and_v1_keeps_its_shape(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()

        v1 = self.public("v1", "classic")
        v2 = self.public("v2", "classic")
        self.assertEqual(v1["latestVersion"], "1.0.0")
        self.assertNotIn("manifestSchemaVersion", v1)
        self.assertNotIn("requiresManifestV2", v1)
        self.assertEqual(v1["compatibility"], {"projectIndependent": True})
        self.assertEqual(
            list(v1)[-3:], ["latestVersion", "updatedAt", "download"],
            "generated fields follow the source fields as before",
        )
        self.assertEqual(v2["manifestSchemaVersion"], 1)
        self.assertFalse(v2["requiresManifestV2"])
        self.assertEqual(v2["downloadSource"]["version"], "1.0.0")
        self.assertEqual(self.manifest("v1")["registryVersion"], 1)
        self.assertEqual(self.manifest("v2")["registryVersion"], 2)
        self.assertEqual(self.manifest("v1")["entryCount"], 1)

    def test_manifest_v2_plugins_are_only_in_v2_with_their_locus_range(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.github.publish("a/modern", "modern", "0.2.0", schemaVersion=2,
                            compatibility={"locus": ">=0.9.4 <0.11.0"}, contributes={"tools": []})
        self.github.publish("a/contrib", "contrib", "0.1.0", contributes={"settings": {}})
        for plugin_id in ("classic", "modern", "contrib"):
            self.add_entry(source_entry(plugin_id, f"a/{plugin_id}"))
        self.run_index()

        self.assertIsNone(self.public("v1", "modern"))
        self.assertIsNone(self.public("v1", "contrib"))
        self.assertEqual(self.manifest("v1")["entryCount"], 1)
        self.assertEqual(self.manifest("v2")["entryCount"], 3)
        modern = self.public("v2", "modern")
        self.assertTrue(modern["requiresManifestV2"])
        self.assertEqual(modern["manifestSchemaVersion"], 2)
        self.assertEqual(modern["compatibility"], {"projectIndependent": True, "locus": ">=0.9.4 <0.11.0"})
        summaries = registry.read_json(registry.PUBLIC_ROOT / "v2" / "search" / "summaries.json")["plugins"]
        self.assertEqual([item["id"] for item in summaries], ["classic", "contrib", "modern"])
        self.assertEqual(summaries[2]["compatibility"]["locus"], ">=0.9.4 <0.11.0")
        v1_summaries = registry.read_json(registry.PUBLIC_ROOT / "v1" / "search" / "summaries.json")["plugins"]
        self.assertEqual([item["id"] for item in v1_summaries], ["classic"])

    def test_unchanged_release_assets_are_not_downloaded_again(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        self.assertEqual(len(self.github.downloads), 1)
        self.run_index()
        self.run_index("--refresh-stats")
        self.assertEqual(len(self.github.downloads), 1, "the digest matched; no archive download")

        self.github.publish("a/classic", "classic", "1.1.0")
        self.run_index()
        self.assertEqual(len(self.github.downloads), 2)
        self.assertEqual(self.public("v2", "classic")["latestVersion"], "1.1.0")

    def test_assets_without_a_digest_compare_size_and_release_time(self):
        self.github.publish("a/classic", "classic", "1.0.0", digest=False)
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        self.run_index()
        self.assertEqual(len(self.github.downloads), 1)
        self.github.releases["a/classic"]["published_at"] = "2026-12-01T00:00:00Z"
        self.run_index()
        self.assertEqual(len(self.github.downloads), 2)

    def test_a_digest_mismatch_keeps_the_previous_entry(self):
        url = self.github.publish("a/classic", "classic", "1.0.0")
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        self.github.releases["a/classic"]["assets"][0]["digest"] = "sha256:" + "0" * 64
        self.github.archives[url] = archive("classic", "1.0.1")
        self.run_index()
        self.assertEqual(self.public("v2", "classic")["latestVersion"], "1.0.0")

    def test_one_failing_plugin_keeps_its_entry_and_others_still_update(self):
        self.github.publish("a/one", "one", "1.0.0")
        self.github.publish("a/two", "two", "1.0.0")
        self.add_entry(source_entry("one", "a/one"))
        self.add_entry(source_entry("two", "a/two"))
        self.run_index()
        self.github.failing.add("a/one")
        self.github.publish("a/two", "two", "2.0.0")
        self.run_index()
        self.assertEqual(self.public("v1", "one")["latestVersion"], "1.0.0")
        self.assertEqual(self.public("v2", "one")["latestVersion"], "1.0.0")
        self.assertEqual(self.public("v2", "two")["latestVersion"], "2.0.0")

    def test_a_new_failing_plugin_is_skipped_but_strict_validation_fails(self):
        self.github.publish("a/one", "one", "1.0.0")
        self.add_entry(source_entry("one", "a/one"))
        path = self.add_entry(source_entry("broken", "a/broken"))
        self.github.failing.add("a/broken")
        self.run_index()
        self.assertIsNone(self.public("v2", "broken"))
        self.assertEqual(self.manifest("v2")["entryCount"], 1)
        with self.assertRaises(SystemExit) as raised:
            self.run_index("--validate-only", "--strict-entries", path.as_posix())
        self.assertIn("HTTP 404", str(raised.exception))

    def test_stats_refresh_daily_or_for_new_plugins_only(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.github.stars["a/classic"] = 5
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        stats = {stat["id"]: stat["value"] for stat in self.public("v2", "classic")["stats"]}
        self.assertEqual(stats, {"githubStars": 5, "releaseDownloads": 7})
        self.assertEqual(len(self.github.stat_calls), 1)

        self.github.stars["a/classic"] = 9
        self.run_index()
        self.assertEqual(len(self.github.stat_calls), 1, "hourly runs keep the previous stats")
        self.assertEqual(self.public("v1", "classic")["stats"][0]["value"], 5)
        self.run_index("--refresh-stats")
        self.assertEqual(self.public("v1", "classic")["stats"][0]["value"], 9)

    def test_nothing_changed_leaves_the_files_and_generated_at(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        before = {path: path.read_bytes() for path in registry.PUBLIC_ROOT.rglob("*.json")}
        self.run_index()
        after = {path: path.read_bytes() for path in registry.PUBLIC_ROOT.rglob("*.json")}
        self.assertEqual(before, after)

    def test_migration_from_a_v1_only_index_downloads_each_archive_once(self):
        self.github.publish("a/classic", "classic", "1.0.0")
        self.add_entry(source_entry("classic", "a/classic"))
        self.run_index()
        registry.shutil.rmtree(registry.PUBLIC_ROOT / "v2")
        self.run_index()
        self.assertEqual(len(self.github.downloads), 2)
        self.run_index()
        self.assertEqual(len(self.github.downloads), 2)

    def test_authoring_errors_stop_the_build(self):
        entry = source_entry("classic", "a/classic")
        del entry["license"]
        self.add_entry(entry)
        with self.assertRaises(SystemExit) as raised:
            self.run_index("--validate-only")
        self.assertIn("missing license", str(raised.exception))

        misplaced = registry.ENTRY_ROOT / "00" / "other.json"
        registry.write_json(misplaced, source_entry("other", "a/other"))
        with self.assertRaises(SystemExit):
            registry.validate_entry(registry.read_json(misplaced), misplaced)


if __name__ == "__main__":
    unittest.main()
