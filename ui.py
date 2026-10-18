import time

import bpy

from . import browser, catalog_index, inventory, lifecycle, lod, providers, quality, resolution, state
from .constants import LIBRARY_NAME, QUALITY_PRESETS
from .library import addon_preferences, find_asset_library, library_root, load_info, load_runtime_state


def _ensure_runtime(context):
    try:
        root = library_root(context)
    except Exception:
        return None
    if state.RUNTIME.loaded_root != str(root):
        state.RUNTIME.ensure_root(root, load_runtime_state(root))
    browser.ensure_cached(root)
    return root


def _asset_browser_is_ours(context):
    try:
        space = context.space_data
        if space is None or getattr(space, "type", "") != "FILE_BROWSER" or getattr(space, "browse_mode", "") != "ASSETS":
            return False
        params = space.params
        ref = getattr(params, "asset_library_reference", getattr(params, "asset_library_ref", ""))
        if ref and LIBRARY_NAME.casefold() in str(ref).casefold():
            return True
        lib = find_asset_library(context)
        return bool(lib and ref and str(ref).casefold() == str(lib.name).casefold())
    except Exception:
        return False


def _draw_task(layout, task, cancel_operator=None, label=None):
    snap = task.snapshot()
    if not snap["running"]:
        return False
    box = layout.box()
    if label:
        box.label(text=label)
    total = max(1, snap["total"])
    factor = min(1.0, snap["completed"] / total)
    if hasattr(box, "progress"):
        box.progress(factor=factor, type="BAR", text=f"{snap['completed']} / {snap['total']}")
    else:
        box.label(text=f"{snap['completed']} / {snap['total']}")
    if snap["current"]:
        box.label(text=snap["current"], icon="IMPORT")
    if cancel_operator:
        box.operator(cancel_operator, icon="CANCEL")
    return True


def _asset_name(info, slug):
    return str((info or {}).get("name") or slug.replace("_", " ").title())


def _short(text, max_chars=28):
    text = str(text)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _relative_time(timestamp):
    if not timestamp:
        return "never"
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _type_label(asset_type):
    return {0: "HDRI", 1: "TEXTURE", 2: "MODEL"}.get(int(asset_type), "ASSET")


def _current_asset_summary(detected, info):
    _slug, asset_type, datablock, _trees = detected
    resolution_name = str(datablock.get("phl_resolution", info.get("phl_base_resolution", "1k"))).upper()
    pieces = [_type_label(asset_type), resolution_name]
    if asset_type == 2:
        current_lod = lod.current_lod(bpy.context)
        if current_lod:
            suffix = current_lod.rsplit("_LOD", 1)[-1] if "_LOD" in current_lod else current_lod
            pieces.append(f"LOD{suffix}")
    return " · ".join(pieces)


def _draw_quality(layout, context, detected, info):
    _slug, asset_type, datablock, _trees = detected
    box = layout.box()
    box.label(text="QUALITY")

    current = str(datablock.get("phl_resolution", info.get("phl_base_resolution", "1k")))
    provider = str(info.get("provider") or "polyhaven")
    if provider != "polyhaven":
        box.label(text=f"Imported at {current.upper()} · {providers.provider_badge(info)}")
        box.label(text="Choose another quality from Browse to add a different local version.", icon="INFO")
        return

    row = box.row(align=True)
    for preset, label in (("PREVIEW", "Preview"), ("WORKING", "Working"), ("FINAL", "Final")):
        target = QUALITY_PRESETS[preset]["resolution"]
        op = row.operator("phl.quality_preset", text=label, depress=(target == current))
        op.preset = preset

    box.separator()
    try:
        rows = resolution.resolution_rows(context)
    except Exception:
        rows = []
    for item in rows:
        row = box.row(align=True)
        icon = "CHECKMARK" if item["local"] else "IMPORT"
        op = row.operator(
            "phl.switch_resolution",
            text=item["resolution"].upper(),
            icon=icon,
            depress=item["resolution"] == current,
        )
        op.resolution = item["resolution"]
        right = row.row()
        right.alignment = "RIGHT"
        right.label(text=inventory.human_size(item["size"]))

    if asset_type == 2:
        lods = lod.available_lods(context)
        if lods:
            box.separator()
            box.label(text="GEOMETRY")
            current_lod = lod.current_lod(context)
            row = box.row(align=True)
            for idx, lod_name in enumerate(lods):
                suffix = lod_name.rsplit("_LOD", 1)[-1] if "_LOD" in lod_name else str(idx)
                op = row.operator("phl.switch_lod", text=f"LOD{suffix}", depress=(lod_name == current_lod))
                op.lod_name = lod_name


def _draw_material(layout, context, detected, info):
    slug, asset_type, _datablock, _trees = detected
    if asset_type != 1:
        return
    obj = context.active_object
    mat = getattr(obj, "active_material", None) if obj else None
    if not mat or mat.get("phl_slug") != slug:
        return

    box = layout.box()
    box.label(text="MATERIAL")
    dims = info.get("dimensions", []) if info else []
    if len(dims) >= 2:
        try:
            box.label(text=f"Physical size  {float(dims[0]) / 1000:.2f} × {float(dims[1]) / 1000:.2f} m")
        except Exception:
            pass
    row = box.row(align=True)
    if len(dims) >= 2:
        row.operator("phl.apply_real_scale", text="Real Scale", icon="CON_SIZELIMIT")
    row.operator("phl.setup_displacement", text="Displacement", icon="MOD_DISPLACE")


def _draw_storage(layout, context, slug, datablock):
    root = library_root(context)
    box = layout.box()
    row = box.row(align=True)
    row.label(text=f"STORAGE · {inventory.human_size(inventory.asset_folder_size(root, slug))}")
    row.operator("phl.clean_asset_cache", text="Clean", icon="TRASH")


def _draw_active_asset_panel(layout, context):
    detected = resolution.detect_active_asset(context)
    if detected is None:
        return False

    slug, asset_type, datablock, _trees = detected
    try:
        info = load_info(library_root(context), slug)
    except Exception:
        info = None

    props = context.window_manager.phl_browser
    panel = layout.box()
    head = panel.row(align=True)
    head.prop(
        props,
        "show_active_details",
        text="",
        icon="TRIA_DOWN" if props.show_active_details else "TRIA_RIGHT",
        emboss=False,
    )
    head.label(text=_short(_asset_name(info, slug), 30), icon="RESTRICT_SELECT_OFF")
    right = head.row(align=True)
    right.alignment = "RIGHT"
    right.label(text=f"ACTIVE · {providers.provider_badge(info or {})}")

    if not info:
        panel.label(text="Local metadata is missing", icon="ERROR")
        panel.operator("phl.repair_asset", icon="FILE_REFRESH")
        return True

    panel.label(text=_current_asset_summary(detected, info))
    if not props.show_active_details:
        return True

    if state.DOWNLOAD.snapshot()["running"]:
        _draw_task(panel, state.DOWNLOAD, "phl.cancel_download")
    else:
        _draw_quality(panel, context, detected, info)

    _draw_material(panel, context, detected, info)
    _draw_storage(panel, context, slug, datablock)

    tools = panel.row(align=True)
    if asset_type == 2 and context.active_object and getattr(context.active_object, "instance_collection", None):
        tools.operator("phl.make_editable", text="Editable", icon="GREASEPENCIL")
    tools.operator("phl.repair_asset", text="Repair", icon="FILE_REFRESH")
    remove = tools.operator("phl.remove_session_asset", text="Remove", icon="TRASH")
    remove.slug = slug
    remove.asset_type = asset_type
    source = tools.operator("wm.url_open", text="", icon="URL")
    source.url = providers.source_url(info, slug)
    return True


def _action_label(asset_type, local):
    if local:
        return {0: "Set as World", 1: "Assign Material", 2: "Add to Scene"}.get(asset_type, "Import")
    return {0: "Download + Set World", 1: "Download + Assign", 2: "Download + Add"}.get(asset_type, "Download + Import")


def _draw_preview_surface(layout, slug, asset_type, icon, scale=5.0):
    """Draw an actual Blender preview image.

    Important: a normal ``UILayout.operator(..., icon_value=...)`` only draws a
    standard icon-sized glyph. Scaling the operator vertically makes a narrow
    strip, not a large asset preview. Keep the image itself as a
    ``template_icon`` and expose placement as a separate compact action.
    """
    if icon:
        layout.template_icon(icon_value=icon, scale=scale)
    else:
        loading = layout.row()
        loading.alignment = "CENTER"
        loading.label(text="Loading preview…", icon="IMAGE_DATA")
        layout.separator(factor=1.2)


def _provider_direct_download(info):
    return bool((info or {}).get("download_variants")) and bool((info or {}).get("remote_downloadable", True))


def _draw_small_card(card, context, root, props, slug, info, verify_ssl):
    asset_type = int(info.get("type", -1))
    icon = browser.icon_id(root, slug, info, verify_ssl=verify_ssl)
    _draw_preview_surface(card, slug, asset_type, icon, scale=5.0)

    name = _asset_name(info, slug)
    op = card.operator("phl.browser_select", text=_short(name, 24))
    op.slug = slug

    local = browser.is_local(root, slug)
    footer = card.row(align=True)
    footer.label(text=f"{_type_label(asset_type)} · {providers.provider_badge(info)}")
    actions = footer.row(align=True)
    actions.alignment = "RIGHT"
    actions.label(text="LOCAL" if local else "CLOUD", icon="CHECKMARK" if local else "IMPORT")
    is_pack = bool(info.get("pack"))
    direct = _provider_direct_download(info)
    if asset_type in {1, 2} and not is_pack and direct:
        place = actions.operator("phl.browser_drag_asset", text="", icon="PIVOT_CURSOR")
        place.slug = slug
    if direct:
        quick = actions.operator("phl.browser_get_asset", text="", icon="IMPORT")
        quick.slug = slug
        quick.quality = props.quality
        quick.import_after = not is_pack
    else:
        source = actions.operator("wm.url_open", text="", icon="URL")
        source.url = providers.source_url(info, slug)


def _draw_selected_full_card(layout, context, root, props, slug, info, verify_ssl):
    card = layout.box()
    local = browser.is_local(root, slug)
    asset_type = int(info.get("type", -1))
    icon = browser.icon_id(root, slug, info, verify_ssl=verify_ssl)

    is_pack = bool(info.get("pack"))
    direct = _provider_direct_download(info)
    split = card.split(factor=0.40)
    visual = split.column()
    _draw_preview_surface(visual, slug, asset_type, icon, scale=7.0)
    if asset_type in {1, 2} and not is_pack and direct:
        place = visual.operator("phl.browser_drag_asset", text="Place in View", icon="PIVOT_CURSOR")
        place.slug = slug

    details = split.column(align=True)
    head = details.row(align=True)
    head.label(text=_short(_asset_name(info, slug), 36), icon="ASSET_MANAGER")
    close = head.operator("phl.browser_select", text="", icon="PANEL_CLOSE", emboss=False)
    close.slug = slug
    details.label(text=f"{_type_label(asset_type)} · {providers.provider_badge(info)}")
    categories = [str(x) for x in info.get("categories", []) or []]
    if categories:
        details.label(text=" / ".join(categories[:3]))
    details.separator()
    if is_pack:
        details.label(text="3D pack · installs individual models to the local Asset Browser", icon="ASSET_MANAGER")
        if local and info.get("pack_asset_count"):
            details.label(text=f"{int(info.get('pack_asset_count'))} local models")
    else:
        details.prop(props, "quality", text="Quality")
        target_res = quality.resolution_for_info(info, props.quality) or QUALITY_PRESETS[props.quality]["resolution"]
        size = inventory.resolution_size(info, target_res)
        size_text = f" · {inventory.human_size(size)}" if size else ""
        status = "Local" if local else "Downloads on import"
        details.label(text=f"{target_res.upper()}{size_text} · {status}", icon="CHECKMARK" if local else "IMPORT")

    card.separator()
    action = card.row(align=True)
    if direct:
        primary_label = ("Pack downloaded" if local else "Download Pack") if is_pack else _action_label(asset_type, local)
        primary = action.operator("phl.browser_get_asset", text=primary_label, icon="CHECKMARK" if (is_pack and local) else "IMPORT")
        primary.slug = slug
        primary.quality = props.quality
        primary.import_after = not is_pack
        if is_pack and local:
            action.enabled = False
    else:
        source = action.operator("wm.url_open", text="Open Free Download", icon="URL")
        source.url = providers.source_url(info, slug)

    secondary = card.row(align=True)
    if direct and not local:
        download = secondary.operator("phl.browser_get_asset", text="Download Only", icon="FILE_BLEND")
        download.slug = slug
        download.quality = props.quality
        download.import_after = False
    secondary.alignment = "RIGHT"
    source = secondary.operator("wm.url_open", text="Source", icon="URL")
    source.url = providers.source_url(info, slug)


def draw_remote_browser(layout, context, root):
    props = context.window_manager.phl_browser
    prefs = addon_preferences(context)
    verify_ssl = prefs.verify_ssl if prefs else True
    remote = browser.REMOTE.snapshot()

    browse = layout.box()
    title = browse.row(align=True)
    title.label(text="SEARCH", icon="VIEWZOOM")
    right = title.row(align=True)
    right.alignment = "RIGHT"
    right.prop(
        props,
        "show_filters",
        text="Filters",
        icon="DISCLOSURE_TRI_DOWN" if props.show_filters else "DISCLOSURE_TRI_RIGHT",
        emboss=False,
    )

    if state.BROWSER.snapshot()["running"]:
        _draw_task(browse, state.BROWSER, label="SEARCHING LIBRARIES")
        return

    search = browse.row(align=True)
    search.prop(props, "search", text="", icon="VIEWZOOM")
    search.operator("phl.browser_refresh", text="Search", icon="VIEWZOOM")

    filters = browse.row(align=True)
    filters.prop(props, "source", text="Source")
    filters.prop(props, "asset_type", text="Type")

    if props.show_filters:
        advanced = browse.box()
        advanced.label(text="FILTERS")
        row = advanced.row(align=True)
        row.prop(props, "category", text="")
        row.prop(props, "sort", text="")
        row = advanced.row(align=True)
        row.prop(props, "availability", text="")
        row.prop(props, "creation_method", text="")
        row = advanced.row(align=True)
        row.prop(props, "min_resolution", text="")
        row.prop(props, "deep_search", text="Deep Search", toggle=True, icon="VIEWZOOM")
        if props.deep_search:
            row = advanced.row()
            row.alert = True
            row.label(text="Broader provider query · slower", icon="INFO")

    if not remote["assets"]:
        browse.separator()
        browse.label(text="Search the configured libraries from one place.")
        browse.label(text="Only matching previews are cached locally.", icon="INFO")
        browse.operator("phl.browser_refresh", text="Search Libraries", icon="WORLD")
        return

    columns = 2 if getattr(context.region, "width", 0) >= 430 else 1
    items, total, page, pages = browser.page_assets(props, root=root, columns=columns)
    count = browse.row(align=True)
    count.label(text=f"{total:,} matches")
    meta = remote.get("meta") or {}
    provider_meta = meta.get("providers") or {}
    if provider_meta:
        badges = []
        for provider_id, short in (("polyhaven", "PH"), ("ambientcg", "ACG"), ("blendkit", "BK"), ("cgbookcase", "CGB"), ("kenney", "KNY")):
            if provider_id in provider_meta:
                badges.append(short)
        if badges:
            count.label(text=" · ".join(badges))
    count_right = count.row()
    count_right.alignment = "RIGHT"
    count_right.label(text=f"{browser.local_count(root):,} local")

    errors = meta.get("errors") or {}
    if errors:
        warning = browse.row()
        warning.alert = True
        names = {"polyhaven": "PH", "ambientcg": "ACG", "blendkit": "BK", "cgbookcase": "CGB", "kenney": "KNY"}
        warning.label(text="Unavailable: " + ", ".join(names.get(k, k) for k in errors), icon="ERROR")

    if not items:
        browse.label(text="No assets match these filters", icon="INFO")
    else:
        pending = []

        def flush_small_cards():
            nonlocal pending
            if not pending:
                return
            if columns <= 1:
                for small_slug, small_info in pending:
                    _draw_small_card(browse.box(), context, root, props, small_slug, small_info, verify_ssl)
            else:
                row = browse.row(align=True)
                split = row.split(factor=0.5, align=True)
                left = split.column(align=True)
                right_col = split.column(align=True)
                small_slug, small_info = pending[0]
                _draw_small_card(left.box(), context, root, props, small_slug, small_info, verify_ssl)
                if len(pending) > 1:
                    small_slug, small_info = pending[1]
                    _draw_small_card(right_col.box(), context, root, props, small_slug, small_info, verify_ssl)
                else:
                    spacer = right_col.box()
                    spacer.enabled = False
                    spacer.label(text="")
            pending = []

        for slug, info in items:
            if slug == props.selected_slug:
                flush_small_cards()
                _draw_selected_full_card(browse, context, root, props, slug, info, verify_ssl)
                continue
            pending.append((slug, info))
            if len(pending) >= columns:
                flush_small_cards()
        flush_small_cards()

    nav = browse.row(align=True)
    left = nav.row(align=True)
    left.enabled = page > 0
    op = left.operator("phl.browser_page", text="", icon="TRIA_LEFT")
    op.direction = -1
    middle = nav.row()
    middle.alignment = "CENTER"
    middle.label(text=f"{page + 1} / {pages}")
    right = nav.row(align=True)
    right.enabled = page + 1 < pages
    op = right.operator("phl.browser_page", text="", icon="TRIA_RIGHT")
    op.direction = 1


def _session_setting(item):
    parts = []
    res = str(item.get("resolution") or "")
    if res:
        parts.append(res.upper())
    if int(item.get("asset_type", -1)) == 2 and item.get("lod"):
        lod_name = str(item.get("lod"))
        suffix = lod_name.rsplit("_LOD", 1)[-1] if "_LOD" in lod_name else lod_name
        parts.append(f"LOD{suffix}")
    return " · ".join(parts) or "Default"


def _draw_session_group(layout, context, root, asset_type, label, items):
    group_items = [item for item in items if int(item.get("asset_type", -1)) == asset_type]
    if not group_items:
        return
    box = layout.box()
    box.label(text=f"{label} · {len(group_items)}")
    for item in group_items:
        slug = item.get("slug", "")
        live = lifecycle.usage_count(slug, asset_type)
        local = browser.is_local(root, slug)
        row = box.row(align=True)
        icon = "CHECKMARK" if live else ("FILE_BLEND" if local else "CLOUD")
        if live:
            focus = row.operator("phl.focus_session_asset", text=f"{_short(item.get('name') or slug, 20)} · {providers.provider_badge(item)}", icon=icon, emboss=False)
            focus.slug = slug
            focus.asset_type = asset_type
        else:
            row.label(text=f"{_short(item.get('name') or slug, 20)} · {providers.provider_badge(item)}", icon=icon)
        info = row.row(align=True)
        info.alignment = "RIGHT"
        info.label(text=_session_setting(item))
        if asset_type == 2 and live > 1:
            info.label(text=f"×{live}")
        if live:
            remove = info.operator("phl.remove_session_asset", text="", icon="TRASH")
            remove.slug = slug
            remove.asset_type = asset_type
        else:
            restore = info.operator("phl.restore_session_asset", text="Restore", icon="IMPORT")
            restore.slug = slug
            restore.asset_type = asset_type


def draw_session_assets(layout, context, root):
    props = context.window_manager.phl_browser
    prefs = addon_preferences(context)
    items = state.SESSION.items()

    header = layout.box()
    header.label(text=f"SESSION ASSETS · {len(items)}", icon="FILE_BLEND")
    header.label(text="Imported during this Blender session. Removed assets stay restorable.")

    batch = layout.box()
    batch.label(text="BATCH MATERIAL RESOLUTION")
    row = batch.row(align=True)
    row.prop(props, "batch_resolution", text="")
    op = row.operator("phl.batch_texture_resolution", text="Apply to All Materials", icon="MATERIAL")
    op.resolution_name = props.batch_resolution
    if state.BATCH.snapshot()["running"]:
        _draw_task(batch, state.BATCH, "phl.cancel_batch")
    if prefs is not None:
        batch.prop(prefs, "auto_cleanup_deleted_materials", text="Auto-delete local files when a session material datablock is deleted")

    if not items:
        empty = layout.box()
        empty.label(text="No managed assets imported yet", icon="INFO")
        empty.label(text="Import or drag an asset from Browse to start this list.")
        return

    _draw_session_group(layout, context, root, 0, "HDRIs", items)
    _draw_session_group(layout, context, root, 2, "MODELS", items)
    _draw_session_group(layout, context, root, 1, "MATERIALS", items)


class PHL_MT_sync_menu(bpy.types.Menu):
    bl_label = "Library Maintenance"

    def draw(self, context):
        layout = self.layout
        runtime = state.RUNTIME.snapshot()
        op = layout.operator("phl.sync", text=f"PH: Sync Missing Proxies ({runtime['new_assets']})", icon="IMPORT")
        op.asset_type = "all"
        op.only_new = True
        layout.separator()
        for kind, label in (("hdris", "PH HDRIs"), ("textures", "PH Textures"), ("models", "PH Models")):
            op = layout.operator("phl.sync", text=label)
            op.asset_type = kind
        op = layout.operator("phl.sync", text="PH Sync All", icon="FILE_REFRESH")
        op.asset_type = "all"
        layout.separator()
        layout.operator("phl.check_updates", text="Refresh Catalog Index", icon="FILE_REFRESH")
        op = layout.operator("phl.sync", text="Revalidate PH Existing", icon="TOOL_SETTINGS")
        op.asset_type = "all"
        op.revalidate = True
        layout.separator()
        layout.operator("phl.clean_library_cache", text="Clean High-Res Cache", icon="TRASH")


class PHL_PT_sidebar(bpy.types.Panel):
    bl_label = "Open Assets"
    bl_idname = "PHL_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Open Assets"

    def draw(self, context):
        layout = self.layout
        root = _ensure_runtime(context)
        try:
            lib = find_asset_library(context)
        except Exception:
            lib = None

        if lib is None:
            layout.operator("phl.setup_library", icon="ASSET_MANAGER")
            layout.operator("phl.diagnostics", icon="INFO")
            return

        runtime = state.RUNTIME.snapshot()
        catalog_total = catalog_index.total(root)
        head = layout.box()
        row = head.row(align=True)
        row.label(text=f"OPEN ASSETS · {catalog_total:,} available" if catalog_total else "OPEN ASSETS")
        tools = row.row(align=True)
        tools.alignment = "RIGHT"
        tools.operator("phl.browser_refresh", text="", icon="FILE_REFRESH")
        tools.menu("PHL_MT_sync_menu", text="", icon="DOWNARROW_HLT")
        tools.operator("phl.open_library_folder", text="", icon="FILE_FOLDER")

        if catalog_total:
            status = f"{browser.local_count(root):,} local"
            stale_sources = catalog_index.stale_provider_count(root)
            if stale_sources:
                status += f" · {stale_sources} source{'s' if stale_sources != 1 else ''} stale"
            if runtime["updated_assets"]:
                status += f" · {runtime['updated_assets']} PH updates"
            head.label(text=status)
        else:
            head.label(text="Catalog index not built yet · refresh to count sources")

        if _draw_task(layout, state.SYNC, "phl.cancel_sync", label="BULK SYNC"):
            return

        props = context.window_manager.phl_browser
        tabs = layout.row(align=True)
        tabs.prop(props, "view_mode", expand=True)
        count = tabs.row()
        count.alignment = "RIGHT"
        if state.SESSION.count():
            count.label(text=str(state.SESSION.count()))

        if props.view_mode == "SESSION":
            _draw_active_asset_panel(layout, context)
            draw_session_assets(layout, context, root)
        else:
            draw_remote_browser(layout, context, root)


class PHL_PT_asset_browser(bpy.types.Panel):
    bl_label = "Open Assets"
    bl_idname = "PHL_PT_asset_browser"
    bl_space_type = "FILE_BROWSER"
    bl_region_type = "TOOLS"

    @classmethod
    def poll(cls, context):
        return _asset_browser_is_ours(context)

    def draw(self, context):
        root = _ensure_runtime(context)
        layout = self.layout
        runtime = state.RUNTIME.snapshot()

        box = layout.box()
        box.label(text="LOCAL LIBRARY")
        box.label(text=f"{browser.local_count(root):,} downloaded assets")
        row = box.row(align=True)
        row.menu("PHL_MT_sync_menu", text="PH Maintenance", icon="FILE_REFRESH")
        row.operator("phl.browser_refresh", text="Search")

        if _draw_task(layout, state.SYNC, "phl.cancel_sync"):
            return

        recent = runtime["recent"]
        if recent:
            box = layout.box()
            box.label(text="RECENTLY DOWNLOADED")
            for item in recent[:8]:
                row = box.row(align=True)
                op = row.operator("phl.go_to_asset", text=item.get("name") or item.get("slug"), emboss=False)
                op.slug = item.get("slug", "")
                res = item.get("resolution", "")
                if res:
                    row.label(text=res.upper())


def asset_browser_header(self, context):
    if not _asset_browser_is_ours(context):
        return
    _ensure_runtime(context)
    layout = self.layout
    snap = state.SYNC.snapshot()
    runtime = state.RUNTIME.snapshot()
    row = layout.row(align=True)
    if snap["running"]:
        row.label(text=f"OA {snap['completed']}/{snap['total']}", icon="IMPORT")
        row.operator("phl.cancel_sync", text="", icon="CANCEL")
    else:
        updates = runtime["updated_assets"]
        label = f"OA · {updates} PH UPDATES" if updates else "OA"
        row.menu("PHL_MT_sync_menu", text=label, icon="ASSET_MANAGER")


def asset_browser_menu(self, context):
    if not _asset_browser_is_ours(context):
        return
    self.layout.menu("PHL_MT_sync_menu", text="Open Assets")


CLASSES = (
    PHL_MT_sync_menu,
    PHL_PT_sidebar,
    PHL_PT_asset_browser,
)
