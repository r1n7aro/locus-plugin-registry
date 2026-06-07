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

Recommended Locus icon ids include `Puzzle`, `Package`, `Box`, `Grid2X2`, `Workflow`, `Network`, `FileCode2`, and `ScanSearch`.