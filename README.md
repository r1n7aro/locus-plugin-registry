# Locus Plugin Registry

Static public registry for Locus plugins.

## Layout

- `v1/manifest.json`: registry metadata and available shard list.
- `v1/shards/<bucket>.json`: plugin summaries for browsing and search.
- `v1/plugins/<bucket>/<plugin-id>.json`: plugin detail and download metadata.

Bucket values use the first two hex characters of `sha256(pluginId)`.