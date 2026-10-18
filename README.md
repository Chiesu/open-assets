# Open Assets 0.7.1

Open Assets is a Blender 4.5+ / 5.x extension for searching, downloading, placing, and managing **free, no-attribution assets** from multiple libraries without syncing entire catalogs first.

## Sources

The browser currently combines five sources:

- **Poly Haven** — models, PBR materials, HDRIs
- **ambientCG** — PBR materials, HDRIs, models
- **Blendkit** — only results that pass the add-on's free/no-attribution safety gate
- **CGBookcase** — free PBR materials, including scanned/procedural surfaces
- **Kenney** — free 3D asset packs

There is no license control in the UI. The provider adapters enforce the product rule before an item is shown; paid or unsupported results never enter the browser.

## Search and research workflow

The default browser stays compact:

1. Search field
2. Source dropdown: All / PH / ACG / BK / CGB / KNY
3. Type dropdown: All / Models / Materials / HDRIs
4. Results

Open **Filters** for:

- category
- relevance / popularity / newest / name sorting
- cloud vs local availability
- scanned vs procedural creation method where the provider identifies it
- minimum available resolution
- **Deep Search**, which asks enabled providers for a broader result set

Deep Search is intentionally opt-in because it is slower and creates more thumbnail/network traffic. Kenney also uses its own tag search to improve research queries such as `tree`, even when a pack name itself does not contain that word.

## Result interaction

- Normal results stay fixed half-width in a two-column browser.
- Selecting one result expands only that result to a full-width row.
- Its former row partner is pushed forward and may move to the following page.
- Result cards use Blender image previews at full card size.
- Models/materials expose a compact placement control next to the preview.
- The placement popup keeps quality and final import settings close to the target point.

## Quality

Browse-time quality presets are shared across compatible providers:

- **Preview**: prefers 1K / light model data
- **Working**: prefers 2K / balanced model data
- **Final**: prefers 4K / higher model data

Poly Haven assets retain live post-import resolution and LOD switching. ambientCG, Blendkit, and CGBookcase are downloaded/built at the chosen browse-time quality and can be rebuilt to another quality later.

Kenney is pack-based rather than resolution-based. Selecting a Kenney result downloads the 3D pack, imports the supported model files in a background Blender process, and marks the individual models as collections in the local **Open Assets** Asset Browser.

## Local library and lifecycle

Every downloaded asset is converted into the local **Open Assets** Blender Asset Library. Existing Poly Haven Lite/Open Assets downloads are kept compatible because the extension ID and legacy asset keys are preserved.

Managed assets support:

- remove from the current Blender file
- optional deletion of heavy local payload files
- lightweight restore metadata kept locally
- session list grouped by HDRIs, models, and materials
- focus / restore / remove actions
- cache cleanup
- batch material resolution changes for compatible providers

## CGBookcase details

CGBookcase materials are built from downloaded texture archives. Its DirectX normal maps are converted for Blender by inverting the green channel in the generated material node graph. The downloader first uses CGBookcase's compatibility metadata endpoint and falls back to the site's public download naming/Referer flow if needed.

## Installation

Install `polyhaven_lite-0.7.1.zip` through Blender **Preferences > Extensions > Install from Disk**. Installing over Open Assets 0.5.x or Poly Haven Lite upgrades the existing extension because the extension ID remains `polyhaven_lite` for compatibility.

The local library location is configurable in add-on preferences. Existing installations keep their current library folder.

## Implementation notes

The add-on uses Blender's Python API and Python standard library. Network downloads are streamed to `.part` files, support retry/resume behavior where the server permits it, and are renamed atomically after completion.

Provider results are normalized into one internal asset format; browsing, session management, placement, cache state, and the native Blender Asset Browser share one interaction model even though provider download formats differ.


## Stable catalog index

Open Assets now keeps a tiny `.open_assets_catalog_index.json` file in the local library. It stores only last-known-good provider totals and timestamps, not asset payloads. This separates the **global library count** from the current search result cache, which is intentionally capped and filter-dependent.

- The header count no longer changes when you search/filter/paginate.
- A provider timeout no longer resets its count to zero; the last known good value is retained and marked stale.
- An unfiltered all-source search reuses provider totals it already received, so no duplicate catalog request is needed.
- Filtered searches refresh the lightweight index only when it is missing or older than 12 hours.
- `Refresh Catalog Index` updates all five source totals and still performs the exact Poly Haven proxy update check.
- No models, textures, HDRIs, thumbnails, or whole asset libraries are downloaded to count availability.

## 0.7.0

- Added persistent lightweight catalog totals independent of search results.
- Preserves last-known-good provider counts across network/API failures.
- Fixed header totals jumping higher/lower/zero after filtered searches.
- Renamed maintenance actions to distinguish global catalog counts from Poly Haven proxy sync.
- Retains CGBookcase, Kenney, Deep Search, selected-card pagination, and 0.5.1 preview fixes.

## 0.6.0

- Added CGBookcase material search/import.
- Added Kenney 3D pack search/download and local Asset Browser conversion.
- Added five-source compact selector UI.
- Kenney tag-search enrichment improves research queries.
- CGBookcase download resolver has a compatibility endpoint plus a public-page/CDN fallback.
- Existing deep-search, provider failure isolation, selected-card pagination, and 0.5.1 preview fixes are retained.


## 0.7.0 fixes

- Fixed drop-popup imports failing with `browser_get_asset.poll() context is incorrect` by deferring the import until Blender has returned to a normal window context.
- Fixed ambientCG/CGBookcase PBR materials silently importing as empty Principled shaders when their download bundle also contained a `.blend`. Map-based providers now build from the extracted PBR maps and fail explicitly if no maps are found.
- Existing affected external material caches are invalidated once via `oa_builder_schema=2` and rebuilt.
- **0.7.1 public-release change:** the experimental CGTrader Free adapter and token preference were removed because a zero-price royalty-free license does not meet the CC0-only product rule.

## Public GitHub distribution

Source: https://github.com/Chiesu/open-assets

Download the compiled Blender Extension ZIP from the latest [GitHub Release](https://github.com/Chiesu/open-assets/releases/latest), not the source-code ZIP shown by GitHub's green Code menu. Blender: **Edit > Preferences > Get Extensions > Install from Disk**.

The GitHub distribution is an independent experimental project. Changes to external provider APIs, or installed add-on versions, may require follow-up updates. See [PUBLIC-RELEASE.md](PUBLIC-RELEASE.md) for upgrade/licensing notes.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). External downloaded assets have their own provider licenses and are not included in this repository.
