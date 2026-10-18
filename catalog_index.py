"""Persistent lightweight catalog statistics for Open Assets.

The browser result cache is intentionally query-scoped and capped, so it must never
be used as the total library size. This module stores a small last-known-good index
containing only provider totals/timestamps. No asset payloads or thumbnails are
required to refresh it.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import network, providers
from .constants import AMBIENTCG_API, BLENDKIT_API, CGBOOKCASE_API, KENNEY_BASE

INDEX_FILE = ".open_assets_catalog_index.json"
SCHEMA_VERSION = 1
STALE_AFTER = 12 * 60 * 60
_PROVIDER_IDS = ("polyhaven", "ambientcg", "blendkit", "cgbookcase", "kenney")


def index_path(root):
    return Path(root) / INDEX_FILE


def _empty():
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": 0.0,
        "providers": {},
    }


def load(root):
    path = index_path(root)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    providers_data = data.get("providers")
    if not isinstance(providers_data, dict):
        providers_data = {}
    return {
        "schema_version": int(data.get("schema_version") or SCHEMA_VERSION),
        "updated_at": float(data.get("updated_at") or 0.0),
        "providers": providers_data,
    }


def save(root, data):
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    payload["schema_version"] = SCHEMA_VERSION
    payload["updated_at"] = float(payload.get("updated_at") or time.time())
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def total(root):
    data = load(root)
    value = 0
    for entry in data.get("providers", {}).values():
        try:
            value += max(0, int(entry.get("total") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return value


def provider_totals(root):
    output = {}
    for provider_id, entry in load(root).get("providers", {}).items():
        try:
            output[provider_id] = max(0, int(entry.get("total") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return output


def age(root):
    updated_at = float(load(root).get("updated_at") or 0.0)
    return max(0.0, time.time() - updated_at) if updated_at else float("inf")


def is_stale(root, max_age=STALE_AFTER):
    data = load(root)
    provider_data = data.get("providers", {})
    now = time.time()
    for provider_id in _PROVIDER_IDS:
        entry = provider_data.get(provider_id) or {}
        checked = float(entry.get("checked_at") or 0.0)
        if not checked or now - checked > max_age or entry.get("last_error"):
            return True
    return False


def stale_provider_count(root):
    now = time.time()
    count = 0
    for provider_id in _PROVIDER_IDS:
        entry = load(root).get("providers", {}).get(provider_id) or {}
        checked = float(entry.get("checked_at") or 0.0)
        if not checked or now - checked > STALE_AFTER or entry.get("last_error"):
            count += 1
    return count


def _ambientcg_count(verify_ssl):
    total_count = 0
    for type_name in ("HDRI", "Material", "3DModel"):
        data = network.http_json(
            AMBIENTCG_API,
            verify_ssl=verify_ssl,
            params={"type": type_name, "limit": 1, "offset": 0},
        )
        total_count += int(data.get("numberOfResults") or len(data.get("foundAssets") or []))
    return total_count


def _blendkit_count(verify_ssl):
    # Keep the exact same server-side product rule as the browser adapter.
    total_count = 0
    for asset_type in (0, 1, 2):
        url = providers._blendkit_search_url(asset_type, "", 1)
        data = network.http_json(url, verify_ssl=verify_ssl)
        total_count += int(data.get("count") or len(data.get("results") or []))
    return total_count


def _cgbookcase_count(verify_ssl):
    data = network.http_json(CGBOOKCASE_API, verify_ssl=verify_ssl)
    rows = (data.get("textures") or data.get("results") or data.get("data") or []) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError("CGBookcase returned an unexpected catalog response")
    return sum(1 for raw in rows if providers.normalize_cgbookcase(raw) is not None)


def _kenney_count(verify_ssl):
    first_url = f"{KENNEY_BASE}/assets/category:3D"
    first_html = network.http_text(first_url, verify_ssl=verify_ssl)
    first_entries, last_page = providers._kenney_listing_entries(first_html)
    last_page = min(max(1, int(last_page)), 8)
    ids = {str(row.get("provider_id") or "") for row in first_entries if row.get("provider_id")}
    if last_page > 1:
        urls = [f"{KENNEY_BASE}/assets/category:3D/page:{page}" for page in range(2, last_page + 1)]
        with ThreadPoolExecutor(max_workers=min(4, len(urls))) as executor:
            futures = [executor.submit(network.http_text, url, verify_ssl) for url in urls]
            for future in futures:
                page_entries, _ = providers._kenney_listing_entries(future.result())
                ids.update(str(row.get("provider_id") or "") for row in page_entries if row.get("provider_id"))
    return len(ids)


def _polyhaven_count(verify_ssl, prefetched=None):
    if prefetched is not None:
        return int(prefetched)
    return len(network.get_assets("all", verify_ssl=verify_ssl))


def refresh(root, verify_ssl=True, prefetched=None):
    """Refresh global provider totals, preserving last-known-good values on failure.

    `prefetched` may contain provider totals already obtained by another request,
    e.g. {"polyhaven": 2384}, avoiding duplicate network work.
    """
    previous = load(root)
    output = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "providers": {key: dict(value) for key, value in previous.get("providers", {}).items()},
    }
    prefetched = dict(prefetched or {})
    now = time.time()
    errors = {}

    jobs = {}
    if "polyhaven" not in prefetched:
        jobs["polyhaven"] = lambda: _polyhaven_count(verify_ssl)
    if "ambientcg" not in prefetched:
        jobs["ambientcg"] = lambda: _ambientcg_count(verify_ssl)
    if "blendkit" not in prefetched:
        jobs["blendkit"] = lambda: _blendkit_count(verify_ssl)
    if "cgbookcase" not in prefetched:
        jobs["cgbookcase"] = lambda: _cgbookcase_count(verify_ssl)
    if "kenney" not in prefetched:
        jobs["kenney"] = lambda: _kenney_count(verify_ssl)

    for provider_id, value in prefetched.items():
        if provider_id not in _PROVIDER_IDS:
            continue
        output["providers"][provider_id] = {
            "total": max(0, int(value)),
            "checked_at": now,
            "last_error": "",
        }

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as executor:
        future_map = {executor.submit(fn): provider_id for provider_id, fn in jobs.items()}
        for future in as_completed(future_map):
            provider_id = future_map[future]
            try:
                value = max(0, int(future.result()))
                output["providers"][provider_id] = {
                    "total": value,
                    "checked_at": now,
                    "last_error": "",
                }
            except Exception as exc:
                errors[provider_id] = str(exc)
                old = dict(output["providers"].get(provider_id) or {})
                # Never replace a known-good count with zero because a provider
                # was temporarily unavailable.
                old.setdefault("total", 0)
                old["last_error"] = str(exc)
                old["error_at"] = now
                output["providers"][provider_id] = old

    save(root, output)
    return output, errors


def update_from_search_meta(root, params, meta):
    """Use an unfiltered all-source search as a free index refresh.

    Provider adapters already report their global totals for this exact query,
    so no second request is needed. Filtered/query searches are deliberately
    ignored because their totals are not library totals.
    """
    params = dict(params or {})
    if str(params.get("source") or "ALL") != "ALL":
        return False
    if str(params.get("asset_type") or "ALL") != "ALL":
        return False
    if str(params.get("query") or "").strip():
        return False
    if str(params.get("creation_method") or "ANY") != "ANY":
        return False
    if str(params.get("min_resolution") or "ANY").lower() != "any":
        return False

    provider_meta = dict((meta or {}).get("providers") or {})
    if not provider_meta:
        return False

    previous = load(root)
    output = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": time.time(),
        "providers": {key: dict(value) for key, value in previous.get("providers", {}).items()},
    }
    now = time.time()
    changed = False
    for provider_id in _PROVIDER_IDS:
        row = provider_meta.get(provider_id)
        if not isinstance(row, dict) or "total" not in row:
            continue
        try:
            value = max(0, int(row.get("total") or 0))
        except (TypeError, ValueError):
            continue
        output["providers"][provider_id] = {
            "total": value,
            "checked_at": now,
            "last_error": "",
        }
        changed = True
    if changed:
        save(root, output)
    return changed
