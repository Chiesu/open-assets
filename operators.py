import json
import os
import threading
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy_extras import view3d_utils

from . import browser, catalog_index, inventory, lifecycle, lod, material_tools, network, providers, quality, resolution, scene_import, state, sync
from .constants import QUALITY_PRESETS
from .library import (
    addon_preferences,
    ensure_asset_library,
    library_root,
    load_info,
    load_runtime_state,
    save_runtime_state,
)


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _require_online(operator):
    if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
        operator.report({"ERROR"}, "Blender Online Access is disabled in Preferences")
        return False
    return True


def _ensure_runtime(context):
    root = library_root(context)
    if state.RUNTIME.loaded_root != str(root):
        state.RUNTIME.ensure_root(root, load_runtime_state(root))
    return root


def _save_runtime(root):
    data = load_runtime_state(root)
    data.update(state.RUNTIME.snapshot())
    save_runtime_state(root, data)


def _sync_config(context, asset_type="all", revalidate=False, only_new=False, resolution_override=None):
    ensure_asset_library(context)
    prefs = addon_preferences(context)
    root = library_root(context)
    return {
        "root": str(root),
        "resolution": resolution_override or prefs.default_resolution,
        "workers": prefs.concurrent_assets,
        "verify_ssl": prefs.verify_ssl,
        "asset_type": asset_type,
        "revalidate": revalidate,
        "only_new": only_new,
        "blender_binary": bpy.app.binary_path,
        "builder_script": str(Path(__file__).with_name("builder.py")),
        "external_builder_script": str(Path(__file__).with_name("external_builder.py")),
    }


class PHL_OT_setup_library(bpy.types.Operator):
    bl_idname = "phl.setup_library"
    bl_label = "Set Up Asset Library"
    bl_description = "Create the local folder and register it with Blender's Asset Browser"

    def execute(self, context):
        try:
            lib = ensure_asset_library(context)
            _ensure_runtime(context)
            self.report({"INFO"}, f"Asset library ready: {lib.path}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_open_library_folder(bpy.types.Operator):
    bl_idname = "phl.open_library_folder"
    bl_label = "Open Library Folder"

    def execute(self, context):
        try:
            root = library_root(context)
            root.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.path_open(filepath=str(root))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_check_updates(bpy.types.Operator):
    bl_idname = "phl.check_updates"
    bl_label = "Refresh Catalog Index"
    bl_description = "Refresh lightweight library totals for all sources and check Poly Haven proxy updates"

    _timer = None
    _thread = None

    @classmethod
    def poll(cls, context):
        return not state.CHECK.snapshot()["running"] and not state.SYNC.snapshot()["running"]

    def execute(self, context):
        if not _require_online(self):
            return {"CANCELLED"}
        try:
            root = _ensure_runtime(context)
            prefs = addon_preferences(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.CHECK.begin()

        def worker():
            try:
                # Poly Haven's lightweight manifest is also needed for exact proxy
                # update counts, so reuse its size in the global catalog index.
                assets = network.get_assets("all", verify_ssl=prefs.verify_ssl)
                new_count, updated_count = inventory.compare_manifest(root, assets)
                index_data, index_errors = catalog_index.refresh(
                    root,
                    verify_ssl=prefs.verify_ssl,
                    prefetched={"polyhaven": len(assets)},
                )
                state.RUNTIME.set_update_counts(new_count, updated_count)
                _save_runtime(root)
                state.CHECK.finish(
                    result={
                        "new": new_count,
                        "updated": updated_count,
                        "catalog_total": catalog_index.total(root),
                        "index_errors": index_errors,
                    }
                )
            except Exception as exc:
                state.CHECK.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        snap = state.CHECK.snapshot()
        if snap["running"]:
            return {"PASS_THROUGH"}
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        result = snap.get("result") or {}
        errors = result.get("index_errors") or {}
        suffix = f" · {len(errors)} source kept from cache" if errors else ""
        self.report(
            {"INFO"},
            f"{result.get('catalog_total', 0):,} available · "
            f"PH {result.get('new', 0)} missing / {result.get('updated', 0)} updated{suffix}",
        )
        return {"FINISHED"}


class PHL_OT_sync(bpy.types.Operator):
    bl_idname = "phl.sync"
    bl_label = "Sync Poly Haven Assets"
    bl_description = "Download missing proxy assets into the native Blender Asset Browser"

    asset_type: EnumProperty(
        name="Type",
        items=(
            ("all", "All", "HDRIs, textures and models"),
            ("hdris", "HDRIs", "HDRIs only"),
            ("textures", "Textures", "Textures only"),
            ("models", "Models", "3D models only"),
        ),
        default="all",
    )
    revalidate: BoolProperty(name="Revalidate Existing", default=False)
    only_new: BoolProperty(name="Only New Assets", default=False)

    _timer = None
    _thread = None

    @classmethod
    def poll(cls, context):
        return not state.SYNC.snapshot()["running"] and not state.CHECK.snapshot()["running"]

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if not _require_online(self):
            return {"CANCELLED"}
        try:
            config = _sync_config(context, self.asset_type, self.revalidate, self.only_new)
            root = _ensure_runtime(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.SYNC.begin()
        self._thread = threading.Thread(
            target=sync.sync_assets,
            args=(config, state.SYNC, state.RUNTIME),
            daemon=True,
        )
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        self._root = root
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        snap = state.SYNC.snapshot()
        _tag_redraw()
        if snap["running"]:
            return {"PASS_THROUGH"}

        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._thread is not None:
            self._thread.join(timeout=0.1)

        try:
            bpy.ops.asset.library_refresh()
        except Exception:
            pass

        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        if snap["cancelled"]:
            self.report({"WARNING"}, f"Sync cancelled after {snap['completed']} assets")
            return {"CANCELLED"}

        if self.asset_type == "all" and not self.revalidate:
            runtime = state.RUNTIME.snapshot()
            if self.only_new:
                state.RUNTIME.set_update_counts(0, runtime["updated_assets"])
            else:
                state.RUNTIME.set_update_counts(0, 0)
            _save_runtime(self._root)
        self.report({"INFO"}, f"Synced {snap['downloaded']} assets")
        return {"FINISHED"}


class PHL_OT_cancel_sync(bpy.types.Operator):
    bl_idname = "phl.cancel_sync"
    bl_label = "Cancel Sync"

    @classmethod
    def poll(cls, context):
        return state.SYNC.snapshot()["running"]

    def execute(self, context):
        state.SYNC.cancel()
        self.report({"INFO"}, "Cancelling after the current file operation")
        return {"FINISHED"}


class PHL_OT_cancel_download(bpy.types.Operator):
    bl_idname = "phl.cancel_download"
    bl_label = "Cancel Asset Download"
    bl_description = "Cancel the current per-asset resolution or quality download"

    @classmethod
    def poll(cls, context):
        return state.DOWNLOAD.snapshot()["running"]

    def execute(self, context):
        state.DOWNLOAD.cancel()
        self.report({"INFO"}, "Cancelling asset download")
        return {"FINISHED"}


class _AsyncResolutionMixin:
    _timer = None
    _thread = None
    _plan = None

    def _start_resolution(self, context, target_resolution):
        try:
            self._plan = resolution.build_plan(context, target_resolution)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        needs_network = any(not destination.exists() for _url, destination, _md5 in self._plan["downloads"])
        if needs_network and not _require_online(self):
            return {"CANCELLED"}

        state.DOWNLOAD.begin()

        def worker():
            try:
                resolution.download_plan(self._plan, state.DOWNLOAD)
                state.DOWNLOAD.finish()
            except InterruptedError:
                state.DOWNLOAD.finish(cancelled=True)
            except Exception as exc:
                state.DOWNLOAD.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish_resolution(self, context):
        snap = state.DOWNLOAD.snapshot()
        if snap["running"]:
            return None
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._thread:
            self._thread.join(timeout=0.1)
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        if snap["cancelled"]:
            self.report({"WARNING"}, "Resolution download cancelled")
            return {"CANCELLED"}
        try:
            count = resolution.apply_plan(self._plan)
            state.SESSION.update(
                self._plan["slug"],
                self._plan["asset_type"],
                resolution=self._plan["resolution"],
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return count


class PHL_OT_switch_resolution(_AsyncResolutionMixin, bpy.types.Operator):
    bl_idname = "phl.switch_resolution"
    bl_label = "Switch Asset Resolution"
    bl_description = "Download the requested resolution if needed and repoint the active asset"

    resolution: StringProperty()

    @classmethod
    def poll(cls, context):
        return not state.DOWNLOAD.snapshot()["running"]

    def execute(self, context):
        return self._start_resolution(context, self.resolution)

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        result = self._finish_resolution(context)
        if result is None:
            return {"PASS_THROUGH"}
        if isinstance(result, set):
            return result
        self.report({"INFO"}, f"Switched to {self.resolution.upper()} ({result} images updated)")
        return {"FINISHED"}


class PHL_OT_switch_lod(bpy.types.Operator):
    bl_idname = "phl.switch_lod"
    bl_label = "Switch Model LOD"
    bl_description = "Swap the collection instance to another level of detail from the same source asset"
    lod_name: StringProperty()

    def execute(self, context):
        try:
            detected = resolution.detect_active_asset(context)
            name = lod.switch_lod(context, self.lod_name)
            if detected:
                state.SESSION.update(detected[0], detected[1], lod=name)
            self.report({"INFO"}, f"Switched to {name}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_quality_preset(_AsyncResolutionMixin, bpy.types.Operator):
    bl_idname = "phl.quality_preset"
    bl_label = "Set Asset Quality"
    bl_description = "Apply a production quality preset combining texture resolution and model LOD"

    preset: EnumProperty(
        items=(
            ("PREVIEW", "Preview", "Small proxy for layout and fast viewport work"),
            ("WORKING", "Working", "Balanced quality for look development"),
            ("FINAL", "Final", "Higher detail for close-up/final rendering"),
        ),
        default="WORKING",
    )

    @classmethod
    def poll(cls, context):
        return not state.DOWNLOAD.snapshot()["running"]

    def execute(self, context):
        try:
            self._target_resolution, self._target_lod = quality.targets_for_preset(context, self.preset)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return self._start_resolution(context, self._target_resolution)

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        result = self._finish_resolution(context)
        if result is None:
            return {"PASS_THROUGH"}
        if isinstance(result, set):
            return result
        detected = resolution.detect_active_asset(context)
        if self._target_lod:
            try:
                lod.switch_lod(context, self._target_lod)
            except Exception as exc:
                self.report({"WARNING"}, f"Resolution applied, LOD switch failed: {exc}")
        if detected:
            state.SESSION.update(
                detected[0],
                detected[1],
                quality=self.preset,
                resolution=self._target_resolution,
                lod=self._target_lod or "",
            )
        self.report({"INFO"}, f"Applied {self.preset.title()} quality")
        return {"FINISHED"}


class PHL_OT_apply_real_scale(bpy.types.Operator):
    bl_idname = "phl.apply_real_scale"
    bl_label = "Apply Real Scale"
    bl_description = "Add dedicated PHL mapping nodes that match this texture's physical dimensions to the active object's UVs"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            count, x, y = material_tools.apply_real_scale(context)
            self.report({"INFO"}, f"Updated {count} texture nodes (scale {x:.3f}, {y:.3f})")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_setup_displacement(bpy.types.Operator):
    bl_idname = "phl.setup_displacement"
    bl_label = "Set Up Displacement"
    bl_description = "Create a guarded bump, true displacement, or adaptive displacement setup using the asset's height map"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=(
            ("BUMP", "Bump", "Shader-only bump; fastest and safest"),
            ("TRUE", "True", "Material displacement with a Simple subdivision modifier"),
            ("ADAPTIVE", "Adaptive", "Cycles adaptive subdivision when supported"),
        ),
        default="BUMP",
    )
    strength: FloatProperty(name="Strength", default=0.1, min=0.0, soft_max=1.0)
    midlevel: FloatProperty(name="Midlevel", default=0.5, min=0.0, max=1.0)
    subdivisions: IntProperty(name="Subdivisions", default=2, min=0, max=6)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def execute(self, context):
        try:
            result = material_tools.setup_displacement(
                context,
                mode=self.mode,
                strength=self.strength,
                midlevel=self.midlevel,
                subdivisions=self.subdivisions,
            )
            detected = resolution.detect_active_asset(context)
            if detected:
                record = lifecycle.capture_state(context, detected[0], detected[1])
                state.SESSION.merge(record)
            self.report({"INFO"}, f"{result.title()} displacement configured")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}



class PHL_OT_browser_refresh(bpy.types.Operator):
    bl_idname = "phl.browser_refresh"
    bl_label = "Search Open Assets"
    bl_description = "Search the configured Open Assets providers using the current browser query"

    _timer = None
    _thread = None

    @classmethod
    def poll(cls, context):
        return not state.BROWSER.snapshot()["running"] and not state.SYNC.snapshot()["running"]

    def execute(self, context):
        if not _require_online(self):
            return {"CANCELLED"}
        try:
            root = _ensure_runtime(context)
            ensure_asset_library(context)
            prefs = addon_preferences(context)
            props = context.window_manager.phl_browser
            query = browser.search_params(props)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.BROWSER.begin()
        state.BROWSER.set_total(1)
        state.BROWSER.set_current("Searching libraries")

        def worker():
            try:
                assets, meta = providers.search_all(query, verify_ssl=prefs.verify_ssl)
                browser.save_manifest(root, assets, meta=meta)
                browser.REMOTE.set_assets(assets, source="remote", meta=meta)

                # A default all-source search already contains global provider
                # totals, so reuse them. For filtered searches, only rebuild the
                # tiny stats index when it is missing/stale. Search results never
                # overwrite the global library total.
                index_errors = {}
                if not catalog_index.update_from_search_meta(root, query, meta) and catalog_index.is_stale(root):
                    _index, index_errors = catalog_index.refresh(root, verify_ssl=prefs.verify_ssl)

                state.BROWSER.advance("Results ready", downloaded=True)
                state.BROWSER.finish(
                    result={
                        "count": len(assets),
                        "meta": meta,
                        "catalog_total": catalog_index.total(root),
                        "index_errors": index_errors,
                    }
                )
            except Exception as exc:
                state.BROWSER.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        snap = state.BROWSER.snapshot()
        if snap["running"]:
            return {"PASS_THROUGH"}
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._thread:
            self._thread.join(timeout=0.1)
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        props = context.window_manager.phl_browser
        props.page = 0
        props.category = "ALL"
        props.selected_slug = ""
        browser.invalidate_local_count()
        result = snap.get("result") or {}
        meta = result.get("meta") or {}
        errors = meta.get("errors") or {}
        index_errors = result.get("index_errors") or {}
        suffix = f" · {len(errors)} search source unavailable" if errors else ""
        if index_errors:
            suffix += f" · {len(index_errors)} catalog count kept from cache"
        self.report(
            {"INFO"},
            f"Loaded {result.get('count', 0):,} matches · {result.get('catalog_total', 0):,} available{suffix}",
        )
        return {"FINISHED"}


class PHL_OT_browser_select(bpy.types.Operator):
    bl_idname = "phl.browser_select"
    bl_label = "Select Catalog Asset"
    bl_description = "Expand this asset into a full-width detail card"

    slug: StringProperty()

    def execute(self, context):
        props = context.window_manager.phl_browser
        if props.selected_slug == self.slug:
            props.selected_slug = ""
            return {"FINISHED"}
        props.selected_slug = self.slug
        columns = 2 if getattr(context.region, "width", 0) >= 430 else 1
        props.page = browser.page_for_slug(props, self.slug, root=library_root(context), columns=columns)
        return {"FINISHED"}


class PHL_OT_browser_page(bpy.types.Operator):
    bl_idname = "phl.browser_page"
    bl_label = "Browse Catalog Page"

    direction: IntProperty(default=1, min=-1, max=1)

    def execute(self, context):
        props = context.window_manager.phl_browser
        columns = 2 if getattr(context.region, "width", 0) >= 430 else 1
        _items, _total, page, pages = browser.page_assets(props, root=library_root(context), columns=columns)
        props.page = min(max(0, page + self.direction), pages - 1)
        return {"FINISHED"}


class PHL_OT_browser_cancel_import(bpy.types.Operator):
    bl_idname = "phl.browser_cancel_import"
    bl_label = "Cancel Asset Download"

    @classmethod
    def poll(cls, context):
        return state.IMPORT.snapshot()["running"]

    def execute(self, context):
        state.IMPORT.cancel()
        self.report({"INFO"}, "Cancelling asset download")
        return {"FINISHED"}


class PHL_OT_browser_get_asset(bpy.types.Operator):
    bl_idname = "phl.browser_get_asset"
    bl_label = "Download Asset"
    bl_description = "Download this asset from its source and optionally add it to the current scene"

    slug: StringProperty()
    quality: EnumProperty(
        items=(
            ("PREVIEW", "Preview", "1K target and low model LOD"),
            ("WORKING", "Working", "2K target and medium model LOD"),
            ("FINAL", "Final", "4K target and high model LOD"),
        ),
        default="WORKING",
    )
    import_after: BoolProperty(default=True)
    resolution_override: StringProperty(default="", options={"HIDDEN"})
    use_drop_location: BoolProperty(default=False, options={"HIDDEN"})
    drop_location: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0), options={"HIDDEN"})
    target_object_name: StringProperty(default="", options={"HIDDEN"})
    restore_json: StringProperty(default="", options={"HIDDEN"})

    _timer = None
    _thread = None
    _root = None
    _info = None

    @classmethod
    def poll(cls, context):
        return not state.IMPORT.snapshot()["running"] and not state.SYNC.snapshot()["running"]

    def _import_local(self, context, info):
        record = scene_import.import_local_asset(
            context,
            self._root,
            self.slug,
            info,
            self.quality,
            location=self.drop_location if self.use_drop_location else None,
            target_object_name=self.target_object_name,
        )
        state.SESSION.add_import(record)

        restored = None
        if self.restore_json:
            try:
                restored = json.loads(self.restore_json)
            except Exception:
                restored = None
        if restored:
            restored = dict(restored)
            restored.pop("count", None)
            restored["status"] = "LIVE"
            desired_resolution = str(restored.get("resolution") or "")
            if desired_resolution:
                try:
                    plan = resolution.build_plan(context, desired_resolution)
                    if all(destination.exists() for _url, destination, _md5 in plan["downloads"]):
                        resolution.apply_plan(plan)
                except Exception:
                    pass
            if int(info.get("type", 0)) == 2 and restored.get("lod"):
                try:
                    lod.switch_lod(context, str(restored.get("lod")))
                except Exception:
                    pass
            state.SESSION.merge(restored)
            lifecycle.apply_material_restore(context, restored)
            try:
                state.SESSION.merge(lifecycle.capture_state(context, self.slug, int(info.get("type", 0))))
            except Exception:
                pass

        try:
            props = context.window_manager.phl_browser
            if props.selected_slug == self.slug:
                props.selected_slug = ""
        except Exception:
            pass
        self.report({"INFO"}, record.get("message", f"Imported {self.slug}"))
        return {"FINISHED"}

    def execute(self, context):
        try:
            ensure_asset_library(context)
            self._root = _ensure_runtime(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self._info = browser.REMOTE.get(self.slug) or load_info(self._root, self.slug)
        if not self._info:
            self.report({"ERROR"}, "Asset metadata is not loaded. Refresh the catalog first.")
            return {"CANCELLED"}

        provider = str(self._info.get("provider") or "polyhaven")
        target_resolution = "pack" if provider == "kenney" else (
            self.resolution_override or quality.resolution_for_info(self._info, self.quality) or QUALITY_PRESETS[self.quality]["resolution"]
        )
        local_info = load_info(self._root, self.slug) if browser.is_local(self._root, self.slug) else None
        needs_material_rebuild = bool(
            local_info
            and int(self._info.get("type", 0)) == 1
            and provider in {"ambientcg", "cgbookcase"}
            and int(local_info.get("oa_builder_schema") or 0) < 2
        )
        local_matches = bool(
            local_info
            and not needs_material_rebuild
            and (
                provider in {"polyhaven", "kenney"}
                or str(local_info.get("phl_base_resolution") or "") == str(target_resolution)
            )
        )
        if local_matches:
            if self.import_after:
                try:
                    return self._import_local(context, local_info or self._info)
                except Exception as exc:
                    self.report({"ERROR"}, str(exc))
                    return {"CANCELLED"}
            self.report({"INFO"}, "Asset is already downloaded")
            return {"FINISHED"}

        if not _require_online(self):
            return {"CANCELLED"}

        try:
            self._config = _sync_config(
                context,
                "all",
                revalidate=False,
                only_new=False,
                resolution_override=target_resolution,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.IMPORT.begin()
        state.IMPORT.set_total(1)
        state.IMPORT.set_current(self.slug)

        def worker():
            try:
                _slug, status, built_info = sync.sync_one(self.slug, self._info, self._config, state.IMPORT)
                if status == "downloaded":
                    state.IMPORT.advance(self.slug, downloaded=True)
                    state.RUNTIME.add_recent(
                        self.slug,
                        name=(built_info or {}).get("name", self.slug),
                        resolution=(built_info or {}).get("phl_base_resolution", ""),
                    )
                    _save_runtime(self._root)
                    state.IMPORT.finish(result=built_info)
                elif status == "cancelled":
                    state.IMPORT.finish(cancelled=True)
                else:
                    state.IMPORT.finish(result=built_info)
            except InterruptedError:
                state.IMPORT.finish(cancelled=True)
            except Exception as exc:
                state.IMPORT.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        snap = state.IMPORT.snapshot()
        if snap["running"]:
            return {"PASS_THROUGH"}
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._thread:
            self._thread.join(timeout=0.1)
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        if snap["cancelled"]:
            self.report({"WARNING"}, "Asset download cancelled")
            return {"CANCELLED"}

        browser.invalidate_local_count()
        try:
            bpy.ops.asset.library_refresh()
        except Exception:
            pass

        info = snap.get("result") or load_info(self._root, self.slug) or self._info
        if self.import_after:
            try:
                return self._import_local(context, info)
            except Exception as exc:
                self.report({"ERROR"}, f"Downloaded, but import failed: {exc}")
                return {"CANCELLED"}
        self.report({"INFO"}, f"Downloaded {self.slug}")
        return {"FINISHED"}



def _view3d_target(context, mouse_x, mouse_y):
    """Return (area, region, location, hit_object) under window-space coordinates."""
    window = context.window
    if window is None:
        return None
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        if not (area.x <= mouse_x < area.x + area.width and area.y <= mouse_y < area.y + area.height):
            continue
        region = next(
            (
                r
                for r in area.regions
                if r.type == "WINDOW"
                and r.x <= mouse_x < r.x + r.width
                and r.y <= mouse_y < r.y + r.height
            ),
            None,
        )
        if region is None:
            continue
        space = area.spaces.active
        rv3d = getattr(space, "region_3d", None)
        if rv3d is None:
            continue
        coord = (mouse_x - region.x, mouse_y - region.y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _normal, _index, hit_object, _matrix = context.scene.ray_cast(
            depsgraph, origin, direction, distance=100000.0
        )
        if not hit:
            location = view3d_utils.region_2d_to_location_3d(
                region, rv3d, coord, context.scene.cursor.location
            )
            hit_object = None
        return area, region, tuple(location), hit_object
    return None


class PHL_OT_browser_drag_asset(bpy.types.Operator):
    bl_idname = "phl.browser_drag_asset"
    bl_label = "Drag Asset to View"
    bl_description = "Place this asset in the 3D View, then choose import settings at the drop point"
    bl_options = {"BLOCKING"}

    slug: StringProperty()

    def _finish(self, context):
        try:
            context.window.cursor_set("DEFAULT")
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def invoke(self, context, event):
        if not self.slug:
            return {"CANCELLED"}
        props = context.window_manager.phl_browser
        props.selected_slug = self.slug
        self._origin_x = getattr(event, "mouse_x", 0)
        self._origin_y = getattr(event, "mouse_y", 0)
        self._moved = False
        try:
            context.window.cursor_set("CROSSHAIR")
            context.workspace.status_text_set(
                "Open Assets: move into the 3D View and release/click to place · Esc cancels"
            )
        except Exception:
            pass
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _drop(self, context, event):
        target = _view3d_target(context, event.mouse_x, event.mouse_y)
        if target is None:
            return False
        area, region, location, hit_object = target
        self._finish(context)
        target_name = hit_object.name if hit_object is not None and hit_object.type == "MESH" else ""
        try:
            with context.temp_override(window=context.window, area=area, region=region, space_data=area.spaces.active):
                bpy.ops.phl.drop_popup(
                    "INVOKE_DEFAULT",
                    slug=self.slug,
                    drop_location=location,
                    target_object_name=target_name,
                )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return None
        return True

    def modal(self, context, event):
        if event.type in {"ESC", "RIGHTMOUSE"}:
            self._finish(context)
            return {"CANCELLED"}

        if event.type == "MOUSEMOVE":
            dx = abs(getattr(event, "mouse_x", 0) - getattr(self, "_origin_x", 0))
            dy = abs(getattr(event, "mouse_y", 0) - getattr(self, "_origin_y", 0))
            if dx + dy > 6:
                self._moved = True
            return {"RUNNING_MODAL"}

        # If Blender invokes the UI operator on mouse-down, releasing over the
        # 3D View behaves as a genuine press-drag-release gesture.  Some UI
        # configurations invoke buttons on release instead; in that case the
        # following PRESS branch provides the same placement flow as a picked-up
        # asset, without requiring a separate Drag button.
        if event.type == "LEFTMOUSE" and event.value in {"RELEASE", "PRESS"}:
            dropped = self._drop(context, event)
            if dropped is True:
                return {"FINISHED"}
            if dropped is None:
                self._finish(context)
                return {"CANCELLED"}
            return {"RUNNING_MODAL"}

        return {"RUNNING_MODAL"}


class PHL_OT_drop_popup(bpy.types.Operator):
    bl_idname = "phl.drop_popup"
    bl_label = "Open Asset"
    bl_description = "Choose quality and confirm the dropped asset"

    slug: StringProperty(options={"HIDDEN"})
    quality: EnumProperty(
        name="Import Quality",
        items=(
            ("PREVIEW", "Preview", "1K target and low model LOD"),
            ("WORKING", "Working", "2K target and medium model LOD"),
            ("FINAL", "Final", "4K target and high model LOD"),
        ),
        default="WORKING",
    )
    drop_location: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0), options={"HIDDEN"})
    target_object_name: StringProperty(default="", options={"HIDDEN"})

    def invoke(self, context, event):
        try:
            self.quality = context.window_manager.phl_browser.quality
        except Exception:
            pass
        info = browser.REMOTE.get(self.slug)
        name = str((info or {}).get("name") or self.slug.replace("_", " ").title())
        asset_type = int((info or {}).get("type", 0))
        confirm = {0: "Set World", 1: "Assign Material", 2: "Place Model"}.get(asset_type, "Import")
        return context.window_manager.invoke_props_dialog(
            self,
            width=390,
            title=name,
            confirm_text=confirm,
        )

    def draw(self, context):
        layout = self.layout
        root = _ensure_runtime(context)
        info = browser.REMOTE.get(self.slug) or load_info(root, self.slug) or {}
        prefs = addon_preferences(context)
        icon = browser.icon_id(root, self.slug, info, verify_ssl=prefs.verify_ssl if prefs else True)
        asset_type = int(info.get("type", 0))

        split = layout.split(factor=0.34)
        left = split.column()
        if icon:
            left.template_icon(icon_value=icon, scale=6.0)
        else:
            left.label(text="Preview", icon="IMAGE_DATA")
        right = split.column(align=True)
        right.label(text=f"{ {0: 'HDRI', 1: 'MATERIAL', 2: 'MODEL'}.get(asset_type, 'ASSET') } · {providers.provider_badge(info)}")
        categories = [str(x) for x in info.get("categories", []) or []]
        if categories:
            right.label(text=" / ".join(categories[:3]))
        right.separator()
        right.prop(self, "quality", text="Quality")
        target_res = quality.resolution_for_info(info, self.quality) or QUALITY_PRESETS[self.quality]["resolution"]
        size = inventory.resolution_size(info, target_res)
        right.label(text=f"{target_res.upper()} · {inventory.human_size(size)}")
        right.label(text="Local" if browser.is_local(root, self.slug) else "Cloud download", icon="CHECKMARK" if browser.is_local(root, self.slug) else "IMPORT")

        layout.separator()
        if asset_type == 2:
            x, y, z = self.drop_location
            layout.label(text=f"Place at  X {x:.2f}  Y {y:.2f}  Z {z:.2f}", icon="PIVOT_CURSOR")
        elif asset_type == 1:
            if self.target_object_name:
                layout.label(text=f"Assign to {self.target_object_name}", icon="MATERIAL")
            else:
                layout.label(text="No mesh under drop point; active mesh will be used", icon="INFO")
        else:
            layout.label(text="Replace the current scene World", icon="WORLD")

        source = layout.operator("wm.url_open", text="View Source", icon="URL")
        source.url = providers.source_url(info, self.slug)

    def execute(self, context):
        info = browser.REMOTE.get(self.slug) or load_info(_ensure_runtime(context), self.slug)
        if not info:
            self.report({"ERROR"}, "Asset metadata is unavailable")
            return {"CANCELLED"}
        asset_type = int(info.get("type", 0))
        payload = {
            "slug": self.slug,
            "quality": self.quality,
            "import_after": True,
            "use_drop_location": (asset_type == 2),
            "drop_location": tuple(self.drop_location),
            "target_object_name": self.target_object_name,
        }
        attempts = {"count": 0}

        def deferred_import():
            # Dialog execution happens inside a temporary UI context. Calling a
            # second operator synchronously from there can make Blender reject
            # its poll even though the same action works from the sidebar. Wait
            # until the dialog/drag modal has fully closed, then execute from the
            # normal window context.
            attempts["count"] += 1
            if state.IMPORT.snapshot()["running"] or state.SYNC.snapshot()["running"]:
                return 0.10 if attempts["count"] < 80 else None
            try:
                bpy.ops.phl.browser_get_asset("EXEC_DEFAULT", **payload)
            except Exception as exc:
                print(f"Open Assets deferred import failed: {exc}")
            return None

        bpy.app.timers.register(deferred_import, first_interval=0.05)
        return {"FINISHED"}


class PHL_OT_focus_session_asset(bpy.types.Operator):
    bl_idname = "phl.focus_session_asset"
    bl_label = "Focus Session Asset"
    bl_description = "Select the object using this asset or restore its world context"

    slug: StringProperty()
    asset_type: IntProperty()

    def execute(self, context):
        name = lifecycle.focus_asset(context, self.slug, self.asset_type)
        if not name:
            self.report({"WARNING"}, "This asset is no longer present in the current file")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Focused {name}")
        return {"FINISHED"}


class PHL_OT_remove_session_asset(bpy.types.Operator):
    bl_idname = "phl.remove_session_asset"
    bl_label = "Remove Managed Asset"
    bl_description = "Remove the asset from the Blender file, optionally deleting its heavy local files"

    slug: StringProperty(options={"HIDDEN"})
    asset_type: IntProperty(options={"HIDDEN"})
    delete_local: BoolProperty(
        name="Delete local files",
        description="Delete downloaded .blend/textures/HDRI but retain metadata and restore settings as a cloud placeholder",
        default=False,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(
            self,
            width=380,
            title="Remove Asset",
            confirm_text="Remove",
        )

    def draw(self, context):
        layout = self.layout
        record = state.SESSION.get(self.slug, self.asset_type) or {}
        layout.label(text=record.get("name") or self.slug.replace("_", " ").title())
        layout.label(text="Remove this asset from the current Blender file.", icon="TRASH")
        layout.prop(self, "delete_local")
        if self.delete_local:
            layout.label(text="Metadata + last managed settings are kept for Restore.", icon="IMPORT")

    def execute(self, context):
        try:
            record = lifecycle.remove_asset_from_file(context, self.slug, self.asset_type)
            if self.delete_local:
                count, size = inventory.delete_asset_payload(library_root(context), self.slug)
                state.SESSION.mark_removed(self.slug, self.asset_type, cloud=True)
                browser.invalidate_local_count()
                self.report({"INFO"}, f"Removed asset and deleted {count} files / {inventory.human_size(size)}")
            else:
                state.SESSION.mark_removed(self.slug, self.asset_type, cloud=False)
                self.report({"INFO"}, "Removed from Blender file; local library kept")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_restore_session_asset(bpy.types.Operator):
    bl_idname = "phl.restore_session_asset"
    bl_label = "Restore Session Asset"
    bl_description = "Restore a removed/cloud asset using its last managed quality and placement settings"

    slug: StringProperty()
    asset_type: IntProperty()

    def execute(self, context):
        root = _ensure_runtime(context)
        record = inventory.load_restore_state(root, self.slug) or state.SESSION.get(self.slug, self.asset_type) or {}
        quality_name = str(record.get("quality") or "WORKING")
        if quality_name not in {"PREVIEW", "WORKING", "FINAL"}:
            quality_name = "WORKING"
        location = record.get("location") or (0.0, 0.0, 0.0)
        try:
            result = bpy.ops.phl.browser_get_asset(
                "EXEC_DEFAULT",
                slug=self.slug,
                quality=quality_name,
                resolution_override=str(record.get("resolution") or ""),
                import_after=True,
                use_drop_location=(int(self.asset_type) == 2 and bool(record.get("location"))),
                drop_location=location,
                target_object_name=str(record.get("target_object") or ""),
                restore_json=json.dumps(record),
            )
            return {"FINISHED"} if "CANCELLED" not in result else {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_cancel_batch(bpy.types.Operator):
    bl_idname = "phl.cancel_batch"
    bl_label = "Cancel Batch Resolution"

    @classmethod
    def poll(cls, context):
        return state.BATCH.snapshot()["running"]

    def execute(self, context):
        state.BATCH.cancel()
        return {"FINISHED"}


class PHL_OT_batch_texture_resolution(bpy.types.Operator):
    bl_idname = "phl.batch_texture_resolution"
    bl_label = "Batch Texture Resolution"
    bl_description = "Switch all compatible managed texture materials in this Blender file to one resolution"

    resolution_name: StringProperty(default="2k")

    _timer = None
    _thread = None
    _plans = None
    _external_jobs = None

    @classmethod
    def poll(cls, context):
        return not state.BATCH.snapshot()["running"] and not state.DOWNLOAD.snapshot()["running"]

    def execute(self, context):
        materials = resolution.imported_texture_materials()
        if not materials:
            self.report({"WARNING"}, "No compatible imported texture materials found")
            return {"CANCELLED"}

        root = library_root(context)
        plans = []
        external_by_slug = {}
        skipped = 0
        already = 0

        for material in materials:
            slug = str(material.get("phl_slug") or "")
            info = load_info(root, slug) if slug else None
            if not info:
                skipped += 1
                continue
            target = quality.nearest_resolution(
                inventory.available_resolutions(info),
                self.resolution_name,
                final=self.resolution_name in {"8k", "16k"},
            )
            if not target:
                skipped += 1
                continue
            current = str(material.get("phl_resolution", info.get("phl_base_resolution", "")))
            if current == target:
                already += 1
                state.SESSION.update(slug, 1, resolution=target)
                continue

            provider = str(info.get("provider") or "polyhaven")
            if provider == "polyhaven":
                try:
                    plans.append(resolution.material_plan(context, material, target))
                except Exception:
                    skipped += 1
                continue

            job = external_by_slug.get(slug)
            if job is None:
                job = {
                    "slug": slug,
                    "info": dict(info),
                    "target": target,
                    "materials": [],
                    "config": _sync_config(context, resolution_override=target),
                }
                external_by_slug[slug] = job
            job["materials"].append(material)

        external_jobs = list(external_by_slug.values())
        downloads = {}
        for plan in plans:
            for url, destination, md5 in plan["downloads"]:
                if not destination.exists():
                    downloads[str(destination)] = (url, destination, md5, plan["verify_ssl"])

        if not plans and not external_jobs:
            if already:
                self.report({"INFO"}, f"All compatible materials are already at {self.resolution_name.upper()}")
                return {"FINISHED"}
            self.report({"ERROR"}, "No compatible materials could be switched")
            return {"CANCELLED"}

        if (downloads or external_jobs) and not _require_online(self):
            return {"CANCELLED"}

        self._plans = plans
        self._external_jobs = external_jobs
        state.BATCH.begin()
        state.BATCH.set_total(len(downloads) + len(external_jobs))

        # If every Poly Haven target is already cached and there are no external
        # rebuilds, the operation is entirely local and can finish immediately.
        if not downloads and not external_jobs:
            for plan in plans:
                resolution.apply_plan(plan)
                state.SESSION.update(plan["slug"], 1, resolution=plan["resolution"])
            state.BATCH.finish(result={"materials": len(plans), "skipped": skipped, "already": already})
            self.report({"INFO"}, f"Updated {len(plans)} materials")
            return {"FINISHED"}

        def worker():
            try:
                for url, destination, md5, verify_ssl in downloads.values():
                    if state.BATCH.cancel_event.is_set():
                        raise InterruptedError()
                    network.download_file(
                        url,
                        destination,
                        md5,
                        verify_ssl=verify_ssl,
                        cancel_event=state.BATCH.cancel_event,
                    )
                    state.BATCH.advance(destination.name, downloaded=True)

                for job in external_jobs:
                    if state.BATCH.cancel_event.is_set():
                        raise InterruptedError()
                    slug, status, _updated = sync.sync_one(
                        job["slug"],
                        job["info"],
                        job["config"],
                        state.BATCH,
                    )
                    if status == "cancelled":
                        raise InterruptedError()
                    state.BATCH.advance(slug, downloaded=(status == "downloaded"))

                changed = len(plans) + sum(len(job["materials"]) for job in external_jobs)
                state.BATCH.finish(result={"materials": changed, "skipped": skipped, "already": already})
            except InterruptedError:
                state.BATCH.finish(cancelled=True)
            except Exception as exc:
                state.BATCH.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        snap = state.BATCH.snapshot()
        if snap["running"]:
            return {"PASS_THROUGH"}
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if self._thread:
            self._thread.join(timeout=0.1)
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        if snap["cancelled"]:
            self.report({"WARNING"}, "Batch resolution cancelled")
            return {"CANCELLED"}

        for plan in self._plans or []:
            resolution.apply_plan(plan)
            state.SESSION.update(plan["slug"], 1, resolution=plan["resolution"])

        root = library_root(context)
        for job in self._external_jobs or []:
            new_material = scene_import.replace_material_resolution(
                root,
                job["slug"],
                job["target"],
                job["materials"],
            )
            try:
                new_material["phl_resolution"] = job["target"]
            except Exception:
                pass
            state.SESSION.update(job["slug"], 1, resolution=job["target"], datablock_name=new_material.name)

        result = snap.get("result") or {}
        count = int(result.get("materials", 0))
        already = int(result.get("already", 0))
        message = f"Updated {count} materials"
        if already:
            message += f" · {already} already matched"
        self.report({"INFO"}, message)
        return {"FINISHED"}


class PHL_OT_clean_asset_cache(bpy.types.Operator):
    bl_idname = "phl.clean_asset_cache"
    bl_label = "Clean Unused Resolutions"
    bl_description = "Remove downloaded resolutions for the selected asset except its current/base resolution"

    def execute(self, context):
        detected = resolution.detect_active_asset(context)
        if not detected:
            self.report({"ERROR"}, "Select a managed asset first")
            return {"CANCELLED"}
        slug, _asset_type, datablock, _trees = detected
        try:
            root = library_root(context)
            info = load_info(root, slug)
            keep = datablock.get("phl_resolution", info.get("phl_base_resolution", "1k") if info else "1k")
            count, size = inventory.clean_asset_high_res(root, slug, keep)
            self.report({"INFO"}, f"Removed {count} files / {inventory.human_size(size)}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_clean_library_cache(bpy.types.Operator):
    bl_idname = "phl.clean_library_cache"
    bl_label = "Clean High-Res Library Cache"
    bl_description = "Remove non-proxy resolution files across the library while preserving each asset's synced base resolution"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            count, size = inventory.clean_library_high_res(library_root(context))
            self.report({"INFO"}, f"Removed {count} files / {inventory.human_size(size)}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_go_to_asset(bpy.types.Operator):
    bl_idname = "phl.go_to_asset"
    bl_label = "Find Asset"
    bl_description = "Filter the current Asset Browser to this asset"
    slug: StringProperty()

    def execute(self, context):
        try:
            context.space_data.params.filter_search = self.slug
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Open this from the Asset Browser: {exc}")
            return {"CANCELLED"}


class PHL_OT_make_editable(bpy.types.Operator):
    bl_idname = "phl.make_editable"
    bl_label = "Make Model Editable"
    bl_description = "Make a collection-instance model real so its objects can be edited"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and getattr(obj, "instance_collection", None))

    def execute(self, context):
        try:
            bpy.ops.object.duplicates_make_real(use_base_parent=True, use_hierarchy=True)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class PHL_OT_repair_asset(bpy.types.Operator):
    bl_idname = "phl.repair_asset"
    bl_label = "Repair Selected Asset"
    bl_description = "Re-download and rebuild the selected asset without revalidating the whole library"

    _timer = None
    _thread = None

    @classmethod
    def poll(cls, context):
        return not state.SYNC.snapshot()["running"]

    def execute(self, context):
        if not _require_online(self):
            return {"CANCELLED"}
        detected = resolution.detect_active_asset(context)
        if not detected:
            self.report({"ERROR"}, "Select a managed asset first")
            return {"CANCELLED"}
        self._slug = detected[0]
        try:
            self._config = _sync_config(context, "all", revalidate=True)
            _ensure_runtime(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        state.SYNC.begin()
        state.SYNC.set_total(1)

        def worker():
            try:
                local_info = load_info(Path(self._config["root"]), self._slug)
                if not local_info:
                    raise RuntimeError(f"Local metadata is missing for {self._slug}")
                provider = str(local_info.get("provider") or "polyhaven")
                if provider == "polyhaven":
                    assets = network.get_assets("all", verify_ssl=self._config["verify_ssl"])
                    raw = assets.get(local_info.get("provider_id") or self._slug)
                    if raw is None:
                        raise RuntimeError(f"{self._slug} is no longer available from its source")
                    info = providers.normalize_polyhaven(local_info.get("provider_id") or self._slug, raw)
                else:
                    info = local_info
                _slug, status, built_info = sync.sync_one(self._slug, info, self._config, state.SYNC)
                if status == "downloaded":
                    state.SYNC.advance(self._slug, downloaded=True)
                    state.RUNTIME.add_recent(
                        self._slug,
                        name=(built_info or {}).get("name", self._slug),
                        resolution=(built_info or {}).get("phl_base_resolution", ""),
                    )
                    _save_runtime(Path(self._config["root"]))
                state.SYNC.finish()
            except InterruptedError:
                state.SYNC.finish(cancelled=True)
            except Exception as exc:
                state.SYNC.fail(str(exc))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _tag_redraw()
        snap = state.SYNC.snapshot()
        if snap["running"]:
            return {"PASS_THROUGH"}
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        try:
            bpy.ops.asset.library_refresh()
        except Exception:
            pass
        if snap["error"]:
            self.report({"ERROR"}, snap["error"])
            return {"CANCELLED"}
        self.report({"INFO"}, f"Repaired {self._slug}")
        return {"FINISHED"}


class PHL_OT_diagnostics(bpy.types.Operator):
    bl_idname = "phl.diagnostics"
    bl_label = "Run Diagnostics"
    bl_description = "Check the local extension, asset-library path, permissions, and Blender background-builder prerequisites"

    def execute(self, context):
        messages = []
        try:
            root = library_root(context)
            root.mkdir(parents=True, exist_ok=True)
            messages.append(f"Library: {root}")
            test_path = root / ".phl_write_test"
            test_path.write_text("ok", encoding="utf-8")
            test_path.unlink(missing_ok=True)
        except Exception as exc:
            self.report({"ERROR"}, f"Library path failed: {exc}")
            return {"CANCELLED"}

        if not bpy.app.binary_path or not Path(bpy.app.binary_path).exists():
            self.report({"ERROR"}, "Blender binary path is unavailable; background asset building cannot run")
            return {"CANCELLED"}
        builders = [Path(__file__).with_name("builder.py"), Path(__file__).with_name("external_builder.py")]
        missing = [builder.name for builder in builders if not builder.exists()]
        if missing:
            self.report({"ERROR"}, f"Missing background builder: {', '.join(missing)}")
            return {"CANCELLED"}
        if hasattr(bpy.app, "online_access") and not bpy.app.online_access:
            messages.append("Online Access: disabled")
        else:
            messages.append("Online Access: enabled")
        messages.append(f"Blender: {bpy.app.version_string}")
        messages.append("Builders: ready")
        self.report({"INFO"}, " | ".join(messages))
        return {"FINISHED"}


CLASSES = (
    PHL_OT_setup_library,
    PHL_OT_open_library_folder,
    PHL_OT_check_updates,
    PHL_OT_sync,
    PHL_OT_cancel_sync,
    PHL_OT_cancel_download,
    PHL_OT_switch_resolution,
    PHL_OT_switch_lod,
    PHL_OT_quality_preset,
    PHL_OT_apply_real_scale,
    PHL_OT_setup_displacement,
    PHL_OT_browser_refresh,
    PHL_OT_browser_select,
    PHL_OT_browser_page,
    PHL_OT_browser_cancel_import,
    PHL_OT_browser_get_asset,
    PHL_OT_browser_drag_asset,
    PHL_OT_drop_popup,
    PHL_OT_focus_session_asset,
    PHL_OT_remove_session_asset,
    PHL_OT_restore_session_asset,
    PHL_OT_cancel_batch,
    PHL_OT_batch_texture_resolution,
    PHL_OT_clean_asset_cache,
    PHL_OT_clean_library_cache,
    PHL_OT_go_to_asset,
    PHL_OT_make_editable,
    PHL_OT_repair_asset,
    PHL_OT_diagnostics,
)
