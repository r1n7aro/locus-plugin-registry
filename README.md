# Locus Plugin Registry

Source entries live under `entries/v1/plugins/<bucket>/<plugin-id>.json`, where `<bucket>` is the first two hex characters of the SHA-256 of the plugin id. CI generates the public indexes under `public/`; do not edit them by hand.

## Public indexes

Both indexes have the same layout: `manifest.json`, `shards/<bucket>.json`, `plugins/<bucket>/<plugin-id>.json` and `search/summaries.json`.

| Index | Read by | Contents |
|---|---|---|
| `public/v2` | Locus releases after 0.9.3 | Every plugin. Entries also carry `compatibility.locus` (the `locus.plugin.json` range), `manifestSchemaVersion` and `requiresManifestV2`. |
| `public/v1` | Locus 0.9.3 and older | Plugins whose archive uses no plugin manifest v2 features (`schemaVersion` 2 or `contributes`), in the original entry shape. |

Base URLs:

- `https://raw.githubusercontent.com/r1n7aro/locus-plugin-registry/main/public/v2`
- `https://raw.githubusercontent.com/r1n7aro/locus-plugin-registry/main/public/v1`

## Source entries

An entry describes the plugin (`name`, `author`, `repo`, `license`, `summary`, `description`, optional `summaryI18n`, `descriptionI18n`, `descriptionSource`, `descriptionSourceI18n`, `tags`, `icon`, `compatibility`, `dependencies`) and where its archive comes from:

```json
"downloadSource": { "type": "latestRelease", "repo": "owner/repo", "assetPattern": "plugin-id-*.zip" }
```

It also lists the `githubStars` and `releaseDownloads` stats. CI fills in `latestVersion`, `updatedAt`, `download` (URL, SHA-256, size), `downloadSource.version` and the stat values; do not write them in the entry. The archive must have `locus.plugin.json` at its root with the entry's id.

## How CI works

`scripts/registry_index.py` builds both indexes (`python -m unittest discover -s tests` runs its tests):

- **Hourly**: checks each plugin's release through the GitHub API. An archive is downloaded only when its asset changed (a new URL, or a different SHA-256 digest; size and release time when GitHub reports no digest), so the registry adds about one download per release.
- **Daily**: refreshes stars and release download counts.
- A plugin whose release cannot be resolved keeps its previous public entry, and the run reports a warning; other plugins still update.
- An index whose content did not change is left as is (including `generatedAt`), so runs without changes make no commit.

Release download counts before November 2026 include downloads made by registry CI itself, which used to fetch every archive on each hourly run.
