import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import network
from . import external_sync
from .constants import RESOLUTION_ORDER
from .inventory import asset_changed
from .library import (
    catalog_id_for_info,
    load_info,
    load_runtime_state,
    save_info,
    save_runtime_state,
    write_catalog_file,
)


def _pick_resolution(files, asset_type, preferred):
    branch = files.get("hdri", {}) if asset_type == 0 else files.get("blend", {})
    available = [r for r in RESOLUTION_ORDER if r in branch]
    if not available:
        available = list(branch.keys())
    if not available:
        raise RuntimeError("No supported downloadable resolution found")
    if preferred in available:
        return preferred
    if preferred in RESOLUTION_ORDER:
        pidx = RESOLUTION_ORDER.index(preferred)
        lower = [r for r in available if r in RESOLUTION_ORDER and RESOLUTION_ORDER.index(r) <= pidx]
        if lower:
            return lower[-1]
    return available[0]


def _needs_sync(root, slug, info, revalidate=False, only_new=False):
    old = load_info(root, slug)
    if only_new:
        return old is None
    if revalidate:
        return old is not None
    status = asset_changed(old, info)
    if status in {"new", "updated"}:
        return True
    blend_path = Path(root) / slug / f"{slug}.blend"
    return not blend_path.exists()


def _thumbnail_url(slug, info):
    return info.get("thumbnail_url") or (
        f"https://cdn.polyhaven.com/asset_img/thumbs/{slug}.png?width=256&height=256&format=webp"
    )


def _authors(info):
    raw = info.get("authors", {})
    if isinstance(raw, dict):
        return list(raw.keys())
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _run_builder(blender_binary, builder_script, source_blend, slug, info, thumbnail, resolution, hdri_file, out_file):
    asset_type = int(info.get("type", 0))
    catalog_id, catalog_path = catalog_id_for_info(info)
    cmd = [blender_binary, "--background", "--factory-startup"]
    if source_blend is not None:
        cmd.append(str(source_blend))
    cmd += [
        "--python",
        str(builder_script),
        "--",
        slug,
        str(asset_type),
        str(thumbnail),
        json.dumps(_authors(info)),
        json.dumps(info.get("categories", [])),
        json.dumps(info.get("tags", [])),
        json.dumps(info.get("dimensions", [])),
        resolution,
        str(hdri_file) if hdri_file else "NONE",
        str(out_file),
        catalog_id,
        catalog_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-30:])
        raise RuntimeError(f"Blender asset build failed for {slug}:\n{tail}")


def _sync_one_polyhaven(slug, base_info, config, task_state):
    if task_state.cancel_event.is_set():
        return (slug, "cancelled", None)

    root = Path(config["root"])
    asset_dir = root / slug
    asset_dir.mkdir(parents=True, exist_ok=True)
    task_state.set_current(slug)

    files = network.get_files(slug, verify_ssl=config["verify_ssl"])
    info = dict(base_info)
    info["files"] = files
    asset_type = int(info.get("type", 0))
    selected_resolution = _pick_resolution(files, asset_type, config["resolution"])
    info["phl_base_resolution"] = selected_resolution

    thumbnail = asset_dir / "thumbnail.webp"
    network.download_file(
        _thumbnail_url(slug, info),
        thumbnail,
        verify_ssl=config["verify_ssl"],
        cancel_event=task_state.cancel_event,
    )

    out_file = asset_dir / f"{slug}.blend"
    source_blend = None
    hdri_file = None

    if asset_type == 0:
        file_info = files["hdri"][selected_resolution].get("hdr") or files["hdri"][selected_resolution].get("exr")
        if not file_info:
            raise RuntimeError(f"No HDR/EXR source found for {slug} at {selected_resolution}")
        hdri_file = asset_dir / Path(file_info["url"]).name
        network.download_file(
            file_info["url"], hdri_file, file_info.get("md5"), config["verify_ssl"], task_state.cancel_event
        )
    else:
        blend_info = files["blend"][selected_resolution]["blend"]
        source_blend = out_file
        network.download_file(
            blend_info["url"], source_blend, blend_info.get("md5"), config["verify_ssl"], task_state.cancel_event
        )
        for sub_path, file_info in blend_info.get("include", {}).items():
            if task_state.cancel_event.is_set():
                return (slug, "cancelled", None)
            network.download_file(
                file_info["url"],
                asset_dir / sub_path,
                file_info.get("md5"),
                config["verify_ssl"],
                task_state.cancel_event,
            )

    if task_state.cancel_event.is_set():
        return (slug, "cancelled", None)

    _run_builder(
        config["blender_binary"],
        config["builder_script"],
        source_blend,
        slug,
        info,
        thumbnail,
        selected_resolution,
        hdri_file,
        out_file,
    )
    save_info(root, slug, info)
    return (slug, "downloaded", info)


def sync_one(slug, base_info, config, task_state):
    provider = str((base_info or {}).get("provider") or "polyhaven")
    if provider == "polyhaven":
        return _sync_one_polyhaven(slug, base_info, config, task_state)
    return external_sync.sync_one_external(slug, base_info, config, task_state)


def sync_assets(config, task_state, runtime_state=None):
    try:
        root = Path(config["root"])
        root.mkdir(parents=True, exist_ok=True)

        assets = network.get_assets(config["asset_type"], verify_ssl=config["verify_ssl"])
        write_catalog_file(root, assets.values())
        candidates = [
            (slug, info)
            for slug, info in assets.items()
            if _needs_sync(
                root,
                slug,
                info,
                config.get("revalidate", False),
                config.get("only_new", False),
            )
        ]
        task_state.set_total(len(candidates))

        if not candidates:
            task_state.finish(cancelled=False)
            return

        with ThreadPoolExecutor(max_workers=config["workers"]) as executor:
            futures = {
                executor.submit(sync_one, slug, info, config, task_state): slug for slug, info in candidates
            }
            for future in as_completed(futures):
                slug = futures[future]
                if task_state.cancel_event.is_set():
                    break
                try:
                    _slug, status, info = future.result()
                    if status == "downloaded":
                        task_state.advance(slug, downloaded=True)
                        if runtime_state is not None:
                            runtime_state.add_recent(
                                slug,
                                name=(info or {}).get("name", slug),
                                resolution=(info or {}).get("phl_base_resolution", ""),
                            )
                    elif status == "cancelled":
                        task_state.advance(slug)
                    else:
                        task_state.advance(slug)
                except InterruptedError:
                    task_state.advance(slug)
                    task_state.cancel()
                    break
                except Exception as exc:
                    task_state.fail(str(exc))
                    task_state.cancel()
                    return

        if runtime_state is not None:
            persisted = load_runtime_state(root)
            persisted.update(runtime_state.snapshot())
            save_runtime_state(root, persisted)
        task_state.finish(cancelled=task_state.cancel_event.is_set())
    except Exception as exc:
        task_state.fail(str(exc))
