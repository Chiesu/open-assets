"""Provider adapters for the unified Open Assets browser.

Only assets that satisfy the add-on's free/no-attribution provider contract are
returned. Poly Haven, ambientCG, CGBookcase, and Kenney are provider-wide sources.
Blendkit is queried with hard free/no-attribution filters and every returned row
is checked again locally before it reaches the browser.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import uuid
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import network
from .constants import (
    AMBIENTCG_API,
    BLENDKIT_API,
    CGBOOKCASE_API,
    KENNEY_BASE,
    PROVIDERS,
    RESOLUTION_ORDER,
)

_SOURCE_TYPES = {
    "ALL": (0, 1, 2),
    "HDRIS": (0,),
    "TEXTURES": (1,),
    "MODELS": (2,),
}

_AMBIENT_TYPES = {0: "HDRI", 1: "Material", 2: "3DModel"}
_BLENDKIT_TYPES = {0: "hdr", 1: "material", 2: "model"}


def provider_name(info):
    provider = str((info or {}).get("provider") or "polyhaven")
    return PROVIDERS.get(provider, {}).get("label", provider)


def provider_badge(info):
    provider = str((info or {}).get("provider") or "polyhaven")
    return PROVIDERS.get(provider, {}).get("badge", provider[:3].upper())


def source_url(info, slug=""):
    info = info or {}
    if info.get("source_url"):
        return str(info["source_url"])
    provider = str(info.get("provider") or "polyhaven")
    provider_id = str(info.get("provider_id") or slug)
    if provider == "ambientcg":
        return f"https://ambientcg.com/view?id={urllib.parse.quote(provider_id)}"
    if provider == "blendkit":
        query = urllib.parse.quote(f"asset_base_id:{provider_id}")
        return f"https://www.blenderkit.com/asset-gallery?query={query}"
    if provider == "cgbookcase":
        return f"https://www.cgbookcase.com/textures/{urllib.parse.quote(provider_id)}"
    if provider == "kenney":
        return f"{KENNEY_BASE}/assets/{urllib.parse.quote(provider_id)}"
    return f"https://polyhaven.com/a/{urllib.parse.quote(provider_id)}"


def local_key(provider, provider_id):
    provider_id = str(provider_id or "").strip()
    if provider == "polyhaven":
        return provider_id
    prefixes = {
        "ambientcg": "acg__",
        "blendkit": "bk__",
        "cgbookcase": "cgb__",
        "kenney": "kny__",
    }
    prefix = prefixes.get(provider, f"{provider[:4]}__")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", provider_id).strip("_")
    return prefix + safe


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, dict):
        return [str(k) for k, v in value.items() if v]
    text = str(value).strip()
    return [text] if text else []


def _dedupe(values):
    seen = set()
    output = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _resolution_from_text(text):
    value = str(text or "")
    match = re.search(r"(?<!\d)(0[_.]5|1|2|4|8|16)\s*K", value, re.IGNORECASE)
    if not match:
        return ""
    raw = match.group(1).replace("_", ".")
    if raw == "0.5":
        return "1k"
    return f"{int(float(raw))}k"


def _creation_method(*values):
    blob = " ".join(str(v) for v in values if v).casefold()
    if any(token in blob for token in ("photogram", "scan", "photoscan", "photometric")):
        return "SCANNED"
    if any(token in blob for token in ("procedural", "substance", "generated")):
        return "PROCEDURAL"
    return "OTHER"


def _preview_url(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        preferred = (
            "512-PNG",
            "512-JPG",
            "512-WEBP",
            "256-PNG",
            "256-JPG",
            "256-WEBP",
            "1024-PNG",
            "1024-JPG",
        )
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
            if isinstance(candidate, dict):
                url = candidate.get("url") or candidate.get("downloadLink")
                if url:
                    return url
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
            if isinstance(candidate, dict):
                url = candidate.get("url") or candidate.get("downloadLink")
                if url:
                    return url
    return ""


def normalize_polyhaven(slug, raw):
    info = dict(raw or {})
    info.update(
        provider="polyhaven",
        provider_name="Poly Haven",
        provider_id=slug,
        source_url=f"https://polyhaven.com/a/{slug}",
    )
    info["name"] = str(info.get("name") or slug.replace("_", " ").title())
    info["categories"] = _dedupe(_as_list(info.get("categories")))
    info["tags"] = _dedupe(_as_list(info.get("tags")))
    info["method"] = _creation_method(info.get("method"), info.get("categories"), info.get("tags"))
    return info


def _ambient_downloads(raw, asset_type):
    folders = raw.get("downloadFolders") or {}
    default = folders.get("default") or next(iter(folders.values()), {})
    categories = default.get("downloadFiletypeCategories") or {}
    candidates = []
    if asset_type == 0:
        for key in ("exr", "hdr", "zip"):
            candidates.extend((categories.get(key) or {}).get("downloads") or [])
    else:
        for key in ("zip", "blend", "gltf", "fbx"):
            candidates.extend((categories.get(key) or {}).get("downloads") or [])

    variants = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        resolution = _resolution_from_text(item.get("attribute") or item.get("fileName"))
        if not resolution:
            continue
        url = item.get("downloadLink") or item.get("url")
        if not url:
            continue
        file_name = item.get("fileName") or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "asset.zip"
        entry = {
            "url": url,
            "file_name": file_name,
            "size": int(item.get("downloadSize") or item.get("size") or 0),
            "zip_content": list(item.get("zipContent") or []),
            "kind": "zip" if str(file_name).lower().endswith(".zip") or "zipContent" in item else "file",
        }
        current = variants.get(resolution)
        # Prefer direct EXR/HDR for HDRIs, ZIP for material/model bundles.
        if current is None:
            variants[resolution] = entry
        elif asset_type == 0 and entry["kind"] == "file":
            variants[resolution] = entry
        elif asset_type != 0 and entry["kind"] == "zip":
            variants[resolution] = entry
    return variants


def normalize_ambient(raw, forced_type=None):
    raw = dict(raw or {})
    type_name = str(raw.get("type") or forced_type or "").casefold()
    if "hdri" in type_name:
        asset_type = 0
    elif "model" in type_name:
        asset_type = 2
    else:
        asset_type = 1
    asset_id = str(raw.get("assetId") or raw.get("id") or "")
    if not asset_id:
        return None
    slug = local_key("ambientcg", asset_id)
    category = raw.get("category") or raw.get("displayCategory") or ""
    tags = raw.get("tags") or raw.get("tagData") or []
    if isinstance(tags, dict):
        tags = list(tags.keys())
    categories = _dedupe([category, raw.get("displayCategory")])
    downloads = _ambient_downloads(raw, asset_type)
    if not downloads:
        return None
    dimensions = []
    method_raw = raw.get("method") or raw.get("dataType") or raw.get("creationMethod")
    preview = _preview_url(raw.get("previewImage") or raw.get("previewImages") or raw.get("displayData"))
    if not preview:
        preview = f"https://ambientcg.com/get?file={urllib.parse.quote(asset_id)}_1K-PNG.png"
    return slug, {
        "provider": "ambientcg",
        "provider_name": "ambientCG",
        "provider_id": asset_id,
        "source_url": f"https://ambientcg.com/view?id={urllib.parse.quote(asset_id)}",
        "type": asset_type,
        "name": str(raw.get("displayName") or raw.get("name") or asset_id),
        "categories": categories,
        "tags": _dedupe(_as_list(tags)),
        "method": _creation_method(method_raw, tags, category),
        "thumbnail_url": preview,
        "date_published": raw.get("datePublished") or raw.get("date") or "",
        "download_count": int(raw.get("downloadCount") or raw.get("downloads") or 0),
        "dimensions": dimensions,
        "download_variants": downloads,
        "available_resolutions": [r for r in RESOLUTION_ORDER if r in downloads],
    }


def _blendkit_thumb_candidates(raw):
    """Return Blendkit preview URLs in UI-quality order.

    The official Blendkit add-on prefers the middle preview for regular assets
    and the large non-squared preview for HDRIs. Keep multiple candidates so a
    stale/missing CDN variant can fall back cleanly.
    """
    asset_type = str(raw.get("assetType") or "").lower()
    keys = (
        (
            "thumbnailLargeUrlNonsquaredWebp",
            "thumbnailLargeUrlNonsquared",
            "thumbnailMiddleUrlWebp",
            "thumbnailMiddleUrl",
            "thumbnailSmallUrlWebp",
            "thumbnailSmallUrl",
        )
        if asset_type == "hdr"
        else (
            "thumbnailMiddleUrlWebp",
            "thumbnailMiddleUrl",
            "thumbnailSmallUrlWebp",
            "thumbnailSmallUrl",
            "thumbnailLargeUrlNonsquaredWebp",
            "thumbnailLargeUrlNonsquared",
        )
    )
    output = []
    seen = set()
    for key in keys:
        value = str(raw.get(key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _blendkit_thumb(raw):
    candidates = _blendkit_thumb_candidates(raw)
    return candidates[0] if candidates else ""


def _blendkit_variants(raw):
    variants = {}
    files = raw.get("files") or []
    for item in files:
        if not isinstance(item, dict):
            continue
        file_type = str(item.get("fileType") or "")
        if not (file_type == "blend" or file_type.startswith("resolution_")):
            continue
        resolution = _resolution_from_text(file_type)
        if not resolution:
            resolution = "2k" if file_type == "blend" else "1k"
        url = item.get("downloadUrl") or item.get("download_url")
        if not url:
            continue
        entry = {
            "resolver_url": url,
            "file_name": str(item.get("filePath") or item.get("fileName") or f"asset_{resolution}.blend").rsplit("/", 1)[-1],
            "size": int(item.get("fileSize") or item.get("size") or 0),
            "kind": "resolver",
            "file_type": file_type,
        }
        # Prefer explicit resolution files over generic blend for the same slot.
        if resolution not in variants or file_type.startswith("resolution_"):
            variants[resolution] = entry
    return variants


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "free"}


def normalize_blendkit(raw):
    raw = dict(raw or {})
    # Defense in depth: the server query is already hard-filtered, but a result
    # is never surfaced unless its returned metadata also explicitly passes.
    params = raw.get("dictParameters") or {}
    license_value = str(
        raw.get("license")
        or raw.get("licenseType")
        or params.get("license")
        or params.get("licenseType")
        or ""
    ).casefold().replace("-", "_")
    if license_value not in {"cc_zero", "cc0", "creative_commons_zero"}:
        return None
    if not _truthy(raw.get("isFree", raw.get("is_free", False))):
        return None
    if raw.get("canDownload") is False or raw.get("can_download") is False:
        return None

    asset_type_name = str(raw.get("assetType") or "").casefold()
    asset_type = {"hdr": 0, "material": 1, "model": 2}.get(asset_type_name)
    if asset_type is None:
        return None
    base_id = str(raw.get("assetBaseId") or raw.get("asset_base_id") or "")
    if not base_id:
        return None
    variants = _blendkit_variants(raw)
    if not variants:
        return None
    slug = local_key("blendkit", base_id)
    categories = _dedupe(_as_list(raw.get("category")) + _as_list(raw.get("categories")))
    tags = _dedupe(_as_list(raw.get("tags")))
    method = _creation_method(params.get("productionLevel"), params.get("style"), tags, categories)
    return slug, {
        "provider": "blendkit",
        "provider_name": "Blendkit",
        "provider_id": base_id,
        "provider_version_id": str(raw.get("id") or ""),
        "source_url": source_url({"provider": "blendkit", "provider_id": base_id}),
        "type": asset_type,
        "name": str(raw.get("displayName") or raw.get("name") or base_id),
        "categories": categories,
        "tags": tags,
        "method": method,
        "thumbnail_url": _blendkit_thumb(raw),
        "thumbnail_candidates": _blendkit_thumb_candidates(raw),
        "date_published": raw.get("created") or raw.get("lastBlendUpload") or "",
        "download_count": int(raw.get("downloadCount") or raw.get("downloads") or 0),
        "download_variants": variants,
        "available_resolutions": [r for r in RESOLUTION_ORDER if r in variants],
        "blendkit_files": raw.get("files") or [],
    }



# -----------------------------------------------------------------------------
# CGBookcase (provider-wide CC0 material library)

_CGBOOKCASE_NEW_SCHEME_ID = 539


def _cgbookcase_slug(title):
    return re.sub(r"\s+", "-", str(title or "").strip()).lower()


def _cgbookcase_thumbnail(raw):
    title = str(raw.get("title") or "")
    try:
        asset_id = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        asset_id = 0
    if asset_id >= _CGBOOKCASE_NEW_SCHEME_ID:
        compact = title.replace(" ", "")
        return f"https://cgbookcase.b-cdn.net/textures/renders/2024_b/{compact}_render_default.jpg?width=480"

    value = title.replace(" ", "_").lower()
    value = value[:1].upper() + value[1:]
    fixes = (
        ("Small", "small"),
        ("Large", "large"),
        ("Medium", "medium"),
        ("3d", "3D"),
        ("lack_ti", "lack_Ti"),
        ("shed_metal_ti", "shed_Metal_Ti"),
        ("ark_ocean_ti", "ark_Ocean_Ti"),
        ("hite_ti", "hite_Ti"),
        ("Snow_white_Tiles", "Snow_white_tiles"),
        ("small_", "Small_"),
        ("large_", "Large_"),
        ("Natural_stone", "Natural_Stone"),
    )
    for old, new in fixes:
        value = value.replace(old, new)
    return f"https://cdn.cgbookcase.cloud/file/cgbookcase/textures/renders/bg_white/480w/{value}_default.jpg"


def _cgbookcase_resolutions(raw):
    try:
        maximum = int(float(raw.get("resolution") or 0))
    except (TypeError, ValueError):
        maximum = 0
    return [r for r in RESOLUTION_ORDER if int(r[:-1]) <= maximum]


def normalize_cgbookcase(raw):
    raw = dict(raw or {})
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    native_id = _cgbookcase_slug(title)
    slug = local_key("cgbookcase", native_id)
    categories = _dedupe(_as_list(raw.get("categories")))
    tags = _dedupe(_as_list(raw.get("tags")) + _as_list(raw.get("colors")))
    one_type = str(raw.get("oneType") or "")
    method = "SCANNED" if "scanned" in one_type.casefold() else ("PROCEDURAL" if "procedural" in one_type.casefold() else "OTHER")
    resolutions = _cgbookcase_resolutions(raw)
    return slug, {
        "provider": "cgbookcase",
        "provider_name": "CGBookcase",
        "provider_id": native_id,
        "source_url": f"https://www.cgbookcase.com/textures/{urllib.parse.quote(native_id)}",
        "type": 1,
        "name": title,
        "categories": categories,
        "tags": tags,
        "method": method,
        "thumbnail_url": _cgbookcase_thumbnail(raw),
        "date_published": raw.get("releasedate") or "",
        "download_count": 0,
        "available_resolutions": resolutions,
        "download_variants": {},
        "cgbookcase_max_resolution": raw.get("resolution") or 0,
    }


def search_cgbookcase(params, verify_ssl=True):
    if 1 not in _SOURCE_TYPES.get(params.get("asset_type", "ALL"), _SOURCE_TYPES["ALL"]):
        return {}, {"provider": "cgbookcase", "total": 0}
    data = network.http_json(CGBOOKCASE_API, verify_ssl=verify_ssl)
    if isinstance(data, dict):
        rows = data.get("textures") or data.get("results") or data.get("data") or []
    else:
        rows = data
    if not isinstance(rows, list):
        raise RuntimeError("CGBookcase returned an unexpected catalog response")
    normalized = []
    for raw in rows:
        item = normalize_cgbookcase(raw)
        if item is None:
            continue
        slug, info = item
        if _matches_common(info, params):
            normalized.append((slug, info))
    mode = str(params.get("sort") or "RELEVANCE")
    if mode == "NAME":
        normalized.sort(key=lambda item: str(item[1].get("name") or "").casefold())
    elif mode == "NEW":
        normalized.sort(key=lambda item: str(item[1].get("date_published") or ""), reverse=True)
    else:
        # API order is currently newest-first; keep it for relevance/popular fallback.
        pass
    cap = 500 if params.get("deep_search") else 160
    return dict(normalized[:cap]), {"provider": "cgbookcase", "total": len(normalized)}


def _choose_variant(variants, resolution, provider_label):
    chosen = variants.get(resolution)
    if chosen is not None:
        return dict(chosen)
    ordered = [r for r in RESOLUTION_ORDER if r in variants]
    if not ordered:
        raise RuntimeError(f"{provider_label} did not expose downloadable texture files")
    if resolution in RESOLUTION_ORDER:
        target = RESOLUTION_ORDER.index(resolution)
        lower = [r for r in ordered if RESOLUTION_ORDER.index(r) <= target]
        if lower:
            return dict(variants[lower[-1]])
        higher = [r for r in ordered if RESOLUTION_ORDER.index(r) > target]
        if higher:
            return dict(variants[higher[0]])
    return dict(variants[ordered[0]])


def _cgbookcase_page_variants(info, native_id, verify_ssl=True):
    """Recover direct CGBookcase ZIPs from the public texture page.

    CGBookcase's public download page links to a small `thanks` route and then
    starts the CDN download in the browser. We only need the exact archive
    naming stem from that link; the public CDN accepts the request when the
    texture page is supplied as Referer. This fallback keeps the provider
    working if the long-standing LilySurfaceScraper JSON endpoint is absent.
    """
    page_url = str(info.get("source_url") or f"https://www.cgbookcase.com/textures/{native_id}")
    page_html = network.http_text(page_url, verify_ssl=verify_ssl)
    hrefs = re.findall(r'href=["\']([^"\']*thanks\?[^"\']+)["\']', page_html, re.I)
    if not hrefs:
        raise RuntimeError("CGBookcase texture page did not expose a download link")

    variants = {}
    for href in hrefs:
        link = urllib.parse.urlparse(html.unescape(urllib.parse.urljoin(page_url, href)))
        query = urllib.parse.parse_qs(link.query)
        filename = (query.get("t") or [""])[0]
        if not filename:
            continue
        match = re.match(r"^(.*_)(\d+)K\.zip$", filename, re.I)
        if not match:
            continue
        prefix = match.group(1)
        # The catalog is authoritative for maximum resolution. Generate the
        # common quality steps rather than depending on the page's current UI
        # selection, which only exposes one `thanks` URL in its HTML.
        resolutions = available_resolutions(info) or [_resolution_from_text(match.group(2))]
        for res in resolutions:
            if not res or not res.endswith("k"):
                continue
            k = res[:-1]
            archive_name = f"{prefix}{k}K.zip"
            direct = f"https://cgbookcase-volume.b-cdn.net/t/{urllib.parse.quote(archive_name)}"
            variants[res] = {
                "url": direct,
                "file_name": archive_name,
                "kind": "zip",
                "headers": {"Referer": page_url},
            }
        if variants:
            break
    return variants


def resolve_cgbookcase_variant(info, resolution, verify_ssl=True):
    native_id = str(info.get("provider_id") or "")
    if not native_id:
        raise RuntimeError("CGBookcase asset id is missing")
    page_url = str(info.get("source_url") or f"https://www.cgbookcase.com/textures/{native_id}")
    endpoint = f"{page_url}/LilySurfaceScraper.json"
    variants = {}
    try:
        data = network.http_json(endpoint, verify_ssl=verify_ssl, headers={"Referer": page_url})
        files = data.get("files") or {}
        for key, url in files.items():
            res = _resolution_from_text(key)
            if not res or not url:
                continue
            variants[res] = {
                "url": str(url),
                "file_name": urllib.parse.urlparse(str(url)).path.rsplit("/", 1)[-1] or f"{native_id}_{res}.zip",
                "kind": "zip",
                "headers": {"Referer": page_url},
            }
    except Exception:
        # Do not make this optional compatibility endpoint a single point of
        # failure. The current site still exposes deterministic public ZIPs.
        variants = {}
    if not variants:
        variants = _cgbookcase_page_variants(info, native_id, verify_ssl=verify_ssl)
    return _choose_variant(variants, resolution, "CGBookcase")


# -----------------------------------------------------------------------------
# Kenney (provider-wide CC0 3D packs)


def _clean_html_text(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _kenney_listing_entries(page_html):
    # Cards are stable `.asset` blocks. Keep this parser intentionally small so
    # a site markup change fails only this provider instead of the whole browser.
    starts = [m.start() for m in re.finditer(r'<(?:div|article)[^>]+class=["\'][^"\']*\basset\b[^"\']*["\'][^>]*>', page_html, re.I)]
    entries = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        block = page_html[start:end]
        title_match = re.search(r'<h2[^>]*>.*?<a[^>]+href=["\'](?:https?://kenney\.nl)?/assets/([a-z0-9][a-z0-9-]*)/?["\'][^>]*>(.*?)</a>', block, re.I | re.S)
        if not title_match:
            continue
        native_id = title_match.group(1)
        title = _clean_html_text(title_match.group(2))
        category_match = re.search(r'<a[^>]+href=["\'][^"\']*/assets/category:([^"\']+)["\'][^>]*>(.*?)</a>', block, re.I | re.S)
        series_match = re.search(r'<a[^>]+href=["\'][^"\']*/assets/series:([^"\']+)["\'][^>]*>(.*?)</a>', block, re.I | re.S)
        thumb = ""
        cover_match = re.search(
            r'class=["\'][^"\']*\bcover\b[^"\']*["\'][^>]*background-image\s*:\s*url\(\s*(?:&quot;|["\']?)(https?://[^)"\']+)',
            block,
            re.I | re.S,
        )
        if cover_match:
            thumb = html.unescape(cover_match.group(1))
        entries.append({
            "provider_id": native_id,
            "title": title or native_id.replace("-", " ").title(),
            "category": _clean_html_text(category_match.group(2)) if category_match else "3D",
            "series": _clean_html_text(series_match.group(2)) if series_match else "",
            "thumbnail_url": thumb,
        })
    last_page = 1
    for value in re.findall(r'/assets/(?:category:3D/)?page:(\d+)', page_html, re.I):
        try:
            last_page = max(last_page, int(value))
        except ValueError:
            pass
    return entries, last_page


def normalize_kenney(raw):
    native_id = str(raw.get("provider_id") or "").strip()
    if not native_id:
        return None
    title = str(raw.get("title") or native_id.replace("-", " ").title())
    category = str(raw.get("category") or "3D")
    series = str(raw.get("series") or "")
    categories = _dedupe([category, series])
    slug = local_key("kenney", native_id)
    return slug, {
        "provider": "kenney",
        "provider_name": "Kenney",
        "provider_id": native_id,
        "source_url": f"{KENNEY_BASE}/assets/{urllib.parse.quote(native_id)}",
        "type": 2,
        "name": title,
        "categories": categories,
        "tags": _dedupe(["3D", "pack", series] + _as_list(raw.get("search_terms"))),
        "method": "OTHER",
        "thumbnail_url": str(raw.get("thumbnail_url") or ""),
        "date_published": "",
        "download_count": 0,
        "available_resolutions": [],
        "download_variants": {},
        "pack": True,
        "pack_kind": "kenney_3d",
    }


def search_kenney(params, verify_ssl=True):
    if 2 not in _SOURCE_TYPES.get(params.get("asset_type", "ALL"), _SOURCE_TYPES["ALL"]):
        return {}, {"provider": "kenney", "total": 0}
    first_url = f"{KENNEY_BASE}/assets/category:3D"
    first_html = network.http_text(first_url, verify_ssl=verify_ssl)
    first_entries, last_page = _kenney_listing_entries(first_html)
    last_page = min(max(1, last_page), 8)
    all_entries = list(first_entries)
    if last_page > 1:
        urls = [f"{KENNEY_BASE}/assets/category:3D/page:{page}" for page in range(2, last_page + 1)]
        with ThreadPoolExecutor(max_workers=min(4, len(urls))) as executor:
            futures = [executor.submit(network.http_text, url, verify_ssl) for url in urls]
            for future in futures:
                page_entries, _ = _kenney_listing_entries(future.result())
                all_entries.extend(page_entries)

    # Kenney's own search includes detail-page tags that are not present in the
    # category listing. Fold those hits into our local metadata so queries such
    # as "tree" can find a generic "Nature Kit" without resolving every
    # detail page up front.
    query = str(params.get("query") or "").strip()
    search_hit_ids = set()
    if query:
        try:
            search_html = network.http_text(f"{KENNEY_BASE}/assets", verify_ssl=verify_ssl, params={"search": query})
            search_entries, search_last = _kenney_listing_entries(search_html)
            if params.get("deep_search") and search_last > 1:
                for page in range(2, min(search_last, 4) + 1):
                    page_html = network.http_text(
                        f"{KENNEY_BASE}/assets/page:{page}",
                        verify_ssl=verify_ssl,
                        params={"search": query},
                    )
                    extra, _ = _kenney_listing_entries(page_html)
                    search_entries.extend(extra)
            for row in search_entries:
                if str(row.get("category") or "").casefold() == "3d":
                    search_hit_ids.add(str(row.get("provider_id") or ""))
                    row = dict(row)
                    row["search_terms"] = [query]
                    all_entries.insert(0, row)
        except Exception:
            # Search metadata is an enhancement only; the category catalog is
            # still usable if Kenney changes its query endpoint.
            pass

    seen = set()
    normalized = []
    for raw in all_entries:
        native_id = raw.get("provider_id")
        if native_id in seen:
            continue
        seen.add(native_id)
        if native_id in search_hit_ids and query:
            raw = dict(raw)
            raw["search_terms"] = [query]
        item = normalize_kenney(raw)
        if item is None:
            continue
        slug, info = item
        if _matches_common(info, params):
            normalized.append((slug, info))
    mode = str(params.get("sort") or "RELEVANCE")
    if mode == "NAME":
        normalized.sort(key=lambda item: str(item[1].get("name") or "").casefold())
    # Default site order is newest/most recently updated and is useful for both
    # Relevance (without a query) and Newest.
    cap = 300 if params.get("deep_search") else 120
    return dict(normalized[:cap]), {"provider": "kenney", "total": len(normalized)}


def resolve_kenney_variant(info, verify_ssl=True):
    page_url = str(info.get("source_url") or source_url(info))
    page_html = network.http_text(page_url, verify_ssl=verify_ssl)
    patterns = (
        r'<a[^>]+id=["\']donate-text["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+\.zip(?:\?[^"\']*)?)["\'][^>]*>',
    )
    download_url = ""
    for pattern in patterns:
        match = re.search(pattern, page_html, re.I | re.S)
        if match:
            download_url = urllib.parse.urljoin(KENNEY_BASE, html.unescape(match.group(1)))
            break
    if not download_url:
        raise RuntimeError("Kenney did not expose a direct pack download")
    return {
        "url": download_url,
        "file_name": urllib.parse.urlparse(download_url).path.rsplit("/", 1)[-1] or f"{info.get('provider_id', 'kenney')}.zip",
        "kind": "zip",
        "headers": {"Referer": page_url},
    }


def _matches_common(info, params):
    asset_type = params.get("asset_type", "ALL")
    allowed = _SOURCE_TYPES.get(asset_type, _SOURCE_TYPES["ALL"])
    if int(info.get("type", -1)) not in allowed:
        return False
    query = str(params.get("query") or "").strip().casefold()
    if query:
        blob = " ".join(
            [str(info.get("name") or ""), str(info.get("provider_id") or "")]
            + [str(x) for x in info.get("categories", []) or []]
            + [str(x) for x in info.get("tags", []) or []]
        ).casefold()
        if not all(token in blob for token in query.split()):
            return False
    method = str(params.get("creation_method") or "ANY")
    if method != "ANY" and info.get("method") != method:
        return False
    minimum = str(params.get("min_resolution") or "ANY").lower()
    if minimum != "any" and minimum in RESOLUTION_ORDER:
        available = available_resolutions(info)
        threshold = RESOLUTION_ORDER.index(minimum)
        # Some provider index endpoints (notably Poly Haven's lightweight list)
        # do not expose per-resolution files until an asset is opened/downloaded.
        # Unknown capability is kept rather than incorrectly hidden.
        if available and not any(r in RESOLUTION_ORDER and RESOLUTION_ORDER.index(r) >= threshold for r in available):
            return False
    return True


def search_polyhaven(params, verify_ssl=True):
    raw_assets = network.get_assets("all", verify_ssl=verify_ssl)
    normalized = []
    for slug, raw in raw_assets.items():
        info = normalize_polyhaven(slug, raw)
        if _matches_common(info, params):
            normalized.append((slug, info))
    cap = 500 if params.get("deep_search") else 180
    return dict(normalized[:cap]), {"provider": "polyhaven", "total": len(normalized)}


def search_ambientcg(params, verify_ssl=True):
    deep = bool(params.get("deep_search"))
    limit = 100 if deep else 36
    max_pages = 3 if deep else 1
    query = str(params.get("query") or "").strip()
    asset_types = _SOURCE_TYPES.get(params.get("asset_type", "ALL"), _SOURCE_TYPES["ALL"])
    assets = {}
    total = 0
    for asset_type in asset_types:
        api_type = _AMBIENT_TYPES[asset_type]
        type_total = None
        for page in range(max_pages):
            request_params = {
                "type": api_type,
                "limit": limit,
                "offset": page * limit,
                "include": "tagData,displayData,dimensionsData,downloadData,previewData",
                "sort": {"NEW": "Latest", "NAME": "Alphabet", "POPULAR": "Popular", "RELEVANCE": "Popular"}.get(params.get("sort"), "Popular"),
            }
            if query:
                request_params["q"] = query
            method = str(params.get("creation_method") or "ANY")
            if method == "SCANNED":
                request_params["method"] = "PBRPhotogrammetry,3DPhotogrammetry"
            elif method == "PROCEDURAL":
                request_params["method"] = "PBRProcedural"
            data = network.http_json(AMBIENTCG_API, verify_ssl=verify_ssl, params=request_params)
            found = data.get("foundAssets") or data.get("assets") or []
            if type_total is None:
                type_total = int(data.get("numberOfResults") or len(found))
                total += type_total
            for raw in found:
                normalized = normalize_ambient(raw, forced_type=api_type)
                if normalized is None:
                    continue
                slug, info = normalized
                if _matches_common(info, params):
                    assets[slug] = info
            if len(found) < limit:
                break
    return assets, {"provider": "ambientcg", "total": total}

def _blendkit_search_url(asset_type, query, page_size):
    terms = [f"asset_type:{_BLENDKIT_TYPES[asset_type]}", "license:cc_zero", "is_free:true"]
    search_query = str(query or "").strip()
    prefix = urllib.parse.quote_plus(search_query) if search_query else ""
    # Literal '+' separators match the official add-on's API query builder.
    q = prefix + "".join(f"+{term}" for term in terms)
    return f"{BLENDKIT_API}?query={q}&page_size={int(page_size)}&dict_parameters=1"


def search_blendkit(params, verify_ssl=True):
    deep = bool(params.get("deep_search"))
    page_size = 80 if deep else 32
    max_pages = 3 if deep else 1
    query = str(params.get("query") or "").strip()
    asset_types = _SOURCE_TYPES.get(params.get("asset_type", "ALL"), _SOURCE_TYPES["ALL"])
    assets = {}
    total = 0
    for asset_type in asset_types:
        url = _blendkit_search_url(asset_type, query, page_size)
        type_total = None
        for _page in range(max_pages):
            data = network.http_json(url, verify_ssl=verify_ssl)
            results = data.get("results") or []
            if type_total is None:
                type_total = int(data.get("count") or len(results))
                total += type_total
            for raw in results:
                normalized = normalize_blendkit(raw)
                if normalized is None:
                    continue
                slug, info = normalized
                if _matches_common(info, params):
                    assets[slug] = info
            next_url = data.get("next")
            if not deep or not next_url or len(results) < page_size:
                break
            url = str(next_url)
    return assets, {"provider": "blendkit", "total": total}

def search_all(params, verify_ssl=True):
    selected = str(params.get("source") or "ALL")
    jobs = []
    if selected in {"ALL", "POLYHAVEN"}:
        jobs.append(("polyhaven", search_polyhaven))
    if selected in {"ALL", "AMBIENTCG"}:
        jobs.append(("ambientcg", search_ambientcg))
    if selected in {"ALL", "BLENDKIT"}:
        jobs.append(("blendkit", search_blendkit))
    if selected in {"ALL", "CGBOOKCASE"}:
        jobs.append(("cgbookcase", search_cgbookcase))
    if selected in {"ALL", "KENNEY"}:
        jobs.append(("kenney", search_kenney))

    merged = {}
    meta = {"providers": {}, "errors": {}}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        futures = {executor.submit(fn, dict(params), verify_ssl): name for name, fn in jobs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                assets, provider_meta = future.result()
                merged.update(assets)
                meta["providers"][name] = provider_meta
            except Exception as exc:
                meta["errors"][name] = str(exc)
    if not merged and meta["errors"]:
        detail = "; ".join(f"{name}: {error}" for name, error in meta["errors"].items())
        raise RuntimeError(f"No provider returned results. {detail}")
    meta["loaded"] = len(merged)
    return merged, meta


def available_resolutions(info):
    info = info or {}
    provider = str(info.get("provider") or "polyhaven")
    if provider == "polyhaven":
        files = info.get("files") or {}
        branch = files.get("hdri", {}) if int(info.get("type", 0)) == 0 else files.get("blend", {})
        values = list(branch.keys())
    elif provider in {"cgbookcase", "kenney"} and info.get("available_resolutions") is not None:
        values = list(info.get("available_resolutions") or [])
    else:
        values = list((info.get("download_variants") or {}).keys())
    return [r for r in RESOLUTION_ORDER if r in values] + [r for r in values if r not in RESOLUTION_ORDER]


def pick_resolution(info, preferred):
    available = available_resolutions(info)
    if not available:
        return ""
    if preferred in available:
        return preferred
    if preferred in RESOLUTION_ORDER:
        index = RESOLUTION_ORDER.index(preferred)
        lower = [r for r in available if r in RESOLUTION_ORDER and RESOLUTION_ORDER.index(r) <= index]
        if lower:
            return lower[-1]
        higher = [r for r in available if r in RESOLUTION_ORDER and RESOLUTION_ORDER.index(r) > index]
        if higher:
            return higher[0]
    return available[0]


def resolve_blendkit_variant(info, resolution, verify_ssl=True):
    variant = (info.get("download_variants") or {}).get(resolution)
    if not variant:
        raise RuntimeError(f"{resolution.upper()} is not available")
    resolver = variant.get("resolver_url")
    if not resolver:
        raise RuntimeError("Blendkit download metadata is incomplete")
    scene_uuid = str(uuid.uuid4())
    payload = network.http_json(resolver, verify_ssl=verify_ssl, params={"scene_uuid": scene_uuid})
    url = payload.get("filePath") or payload.get("download_url") or payload.get("url")
    if not url:
        raise RuntimeError("Blendkit did not return a downloadable file URL")
    result = dict(variant)
    result["url"] = url
    result["file_name"] = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or result.get("file_name") or "asset.blend"
    return result
