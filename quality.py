from .constants import QUALITY_PRESETS, RESOLUTION_ORDER
from .inventory import available_resolutions
from .library import library_root, load_info
from .resolution import detect_active_asset
from . import lod


def nearest_resolution(available, preferred, final=False):
    if not available:
        return ""
    if preferred in available:
        return preferred
    ranked = [r for r in RESOLUTION_ORDER if r in available]
    if not ranked:
        return available[0]
    if preferred in RESOLUTION_ORDER:
        idx = RESOLUTION_ORDER.index(preferred)
        lower = [r for r in ranked if RESOLUTION_ORDER.index(r) <= idx]
        if lower:
            return lower[-1]
    return ranked[-1] if final else ranked[0]


def resolution_for_info(info, preset):
    config = QUALITY_PRESETS[preset]
    return nearest_resolution(
        available_resolutions(info),
        config["resolution"],
        final=(preset == "FINAL"),
    )


def targets_for_preset(context, preset):
    detected = detect_active_asset(context)
    if not detected:
        raise RuntimeError("Select a managed asset first")
    slug, asset_type, _datablock, _trees = detected
    info = load_info(library_root(context), slug)
    if not info:
        raise RuntimeError(f"Missing metadata for {slug}")
    target_res = resolution_for_info(info, preset)
    config = QUALITY_PRESETS[preset]
    target_lod = lod.preset_lod(context, config["lod"]) if asset_type == 2 else ""
    return target_res, target_lod
