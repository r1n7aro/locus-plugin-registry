# Locus Plugin Registry

Static public registry for Locus plugins.

## Layout

- `v1/manifest.json`: registry metadata and available shard list.
- `v1/shards/<bucket>.json`: plugin summaries for browsing and search.
- `v1/plugins/<bucket>/<plugin-id>.json`: plugin detail and download metadata.

Bucket values use the first two hex characters of `sha256(pluginId)`.

## Entry Metadata

Summary entries in `shards` and detail entries in `plugins` may include `icon`.

Use a Locus icon library id:

```json
{
  "icon": {
    "type": "locus",
    "id": "Puzzle"
  }
}
```

Use an external icon URL:

```json
{
  "icon": {
    "type": "url",
    "url": "https://example.com/plugin-icon.png"
  }
}
```

Detail entries may include `descriptionSource` for rich Markdown detail content. Locus fetches this file only when the user opens the plugin detail window.

Use the plugin repository README:

```json
{
  "repo": "owner/plugin-repo",
  "descriptionSource": {
    "type": "github",
    "path": "README.md"
  }
}
```

Use another Markdown file or branch:

```json
{
  "descriptionSource": {
    "type": "github",
    "repo": "owner/plugin-repo",
    "branch": "docs",
    "path": "docs/details.md"
  }
}
```

Use a direct Markdown URL:

```json
{
  "descriptionSource": {
    "type": "url",
    "url": "https://raw.githubusercontent.com/owner/plugin-repo/main/README.md"
  }
}
```

Markdown image paths can be relative to the Markdown file.

Recommended Locus icon ids include `Puzzle`, `Package`, `Box`, `Grid2X2`, `Workflow`, `Network`, `FileCode2`, and `ScanSearch`.