"""Download/build pipeline for non-Poly-Haven Open Assets providers."""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import zipfile
from pathlib import Path

from . import network, providers
from .library import catalog_id_for_info, save_info, write_catalog_file


def _safe_extract(zip_path, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if destination not in member_path.parents and member_path != destination:
                raise RuntimeError(f"Unsafe path inside archive: {member.filename}")
            archive.extract(member, destination)
            if not member.is_dir():
                extracted.append(member_path)
    return extracted


def _thumbnail_destination(asset_dir, url):
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    return asset_dir / f"thumbnail{suffix}"


def _find_source_file(files, asset_type):
    files = [Path(p) for p in files if Path(p).is_file()]
    if asset_type == 0:
        for ext in (".exr", ".hdr"):
            match = next((p for p in files if p.suffix.lower() == ext), None)
            if match:
                return match
        return files[0] if files else None
    if asset_type == 2:
        for ext in (".blend", ".glb", ".gltf", ".fbx", ".obj"):
            match = next((p for p in files if p.suffix.lower() == ext), None)
            if match:
                return match
    return next((p for p in files if p.suffix.lower() == ".blend"), None)


def _run_builder(config, info_path, out_file, thumbnail, source_file, catalog_id, catalog_path):
    source_file = Path(source_file) if source_file else None
    cmd = [config["blender_binary"], "--background", "--factory-startup"]
    if source_file and source_file.suffix.lower() == ".blend":
        cmd.append(str(source_file))
    cmd += [
        "--python",
        str(config["external_builder_script"]),
        "--",
        str(info_path),
        str(out_file),
        str(thumbnail) if thumbnail else "NONE",
        str(source_file) if source_file else "NONE",
        catalog_id,
        catalog_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-40:])
        raise RuntimeError(f"Blender asset build failed for {info_path.parent.name}:\n{tail}")


def _download_variant(info, resolution, payload_dir, verify_ssl, cancel_event):
    provider = str(info.get("provider") or "")
    if provider == "blendkit":
        variant = providers.resolve_blendkit_variant(info, resolution, verify_ssl=verify_ssl)
    elif provider == "cgbookcase":
        variant = providers.resolve_cgbookcase_variant(info, resolution, verify_ssl=verify_ssl)
    elif provider == "kenney":
        variant = providers.resolve_kenney_variant(info, verify_ssl=verify_ssl)
    else:
        variant = dict((info.get("download_variants") or {}).get(resolution) or {})
    if not variant:
        raise RuntimeError(f"No {resolution.upper()} download is available")
    url = variant.get("url")
    if not url:
        raise RuntimeError("Provider download metadata is incomplete")

    filename = variant.get("file_name") or urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or "asset.bin"
    destination = payload_dir / filename
    network.download_file(
        url,
        destination,
        verify_ssl=verify_ssl,
        cancel_event=cancel_event,
        headers=variant.get("headers") or None,
    )
    local_files = [destination]
    for companion in variant.get("companions") or []:
        companion_url = companion.get("url")
        if not companion_url:
            continue
        companion_name = companion.get("file_name") or urllib.parse.urlparse(companion_url).path.rsplit("/", 1)[-1] or "companion.bin"
        companion_path = payload_dir / companion_name
        network.download_file(
            companion_url,
            companion_path,
            verify_ssl=verify_ssl,
            cancel_event=cancel_event,
            headers=variant.get("headers") or None,
        )
        local_files.append(companion_path)
    extracted = []
    if variant.get("kind") == "zip" or destination.suffix.lower() == ".zip":
        extracted = _safe_extract(destination, payload_dir / "extracted")
        local_files.extend(extracted)
    return variant, local_files, extracted


def sync_one_external(slug, base_info, config, task_state):
    if task_state.cancel_event.is_set():
        return slug, "cancelled", None

    info = dict(base_info or {})
    provider = str(info.get("provider") or "")
    if provider not in {"ambientcg", "blendkit", "cgbookcase", "kenney"}:
        raise RuntimeError(f"Unsupported external provider: {provider}")

    root = Path(config["root"])
    asset_dir = root / slug
    asset_dir.mkdir(parents=True, exist_ok=True)
    task_state.set_current(slug)

    selected_resolution = "pack" if provider == "kenney" else providers.pick_resolution(info, config["resolution"])
    if not selected_resolution:
        raise RuntimeError(f"No downloadable resolution found for {info.get('name', slug)}")
    info["phl_base_resolution"] = selected_resolution

    thumbnail = None
    thumb_url = str(info.get("thumbnail_url") or "")
    if thumb_url:
        thumbnail = _thumbnail_destination(asset_dir, thumb_url)
        network.download_file(
            thumb_url,
            thumbnail,
            verify_ssl=config["verify_ssl"],
            cancel_event=task_state.cancel_event,
            retries=2,
        )
        info["local_thumbnail"] = thumbnail.name

    payload_dir = asset_dir / "payload" / selected_resolution
    payload_dir.mkdir(parents=True, exist_ok=True)
    _variant, local_files, extracted = _download_variant(
        info,
        selected_resolution,
        payload_dir,
        config["verify_ssl"],
        task_state.cancel_event,
    )
    if task_state.cancel_event.is_set():
        return slug, "cancelled", None

    asset_type = int(info.get("type", 0))
    source_candidates = extracted or local_files
    source_file = _find_source_file(source_candidates, asset_type)
    # Map-based material providers must be built from the extracted PBR maps.
    # ambientCG packages often include a convenience .blend; choosing that file
    # here previously caused the map builder to receive a file path and silently
    # create an empty Principled material.
    if asset_type == 1 and provider in {"ambientcg", "cgbookcase"}:
        extracted_root = payload_dir / "extracted"
        source_file = extracted_root if extracted_root.exists() else payload_dir
    if asset_type == 2 and provider == "kenney":
        source_file = payload_dir / "extracted"

    info.setdefault("local_resolution_files", {})[selected_resolution] = [
        str(Path(path).relative_to(asset_dir)) for path in local_files if Path(path).exists()
    ]

    write_catalog_file(root, [info])
    catalog_id, catalog_path = catalog_id_for_info(info)
    info["phl_catalog_path"] = catalog_path
    info_path = asset_dir / "info.json"
    save_info(root, slug, info)

    # Build transactionally. A failed conversion must never mark an old/broken
    # .blend as current, and must never stamp the new builder schema until the
    # replacement file actually exists.
    out_file = asset_dir / f"{slug}.blend"
    building_file = asset_dir / f".{slug}.building.blend"
    try:
        building_file.unlink(missing_ok=True)
    except OSError:
        pass
    _run_builder(config, info_path, building_file, thumbnail, source_file, catalog_id, catalog_path)
    if not building_file.exists():
        raise RuntimeError(f"Asset builder did not produce {building_file.name}")
    building_file.replace(out_file)
    info["oa_builder_schema"] = 2
    save_info(root, slug, info)
    return slug, "downloaded", info
