# -*- coding: utf-8 -*-
"""Joint Hilite（ボーン色強調）・Used Joints（使用ボーン表示）・Remap 変更先ハイライトを制御するモジュール。"""

from collections import defaultdict

from maya import cmds
import maya.api.OpenMaya as om2


WEIGHT_EPSILON = 0.0001
MAX_TARGET_VERTICES = 5000
REMAP_HIGHLIGHT_RGB = (1.00, 0.15, 0.95)

_SAVED_COLOR_ATTRS = {}
_SAVED_DRAW_STYLE = {}
_SKELETON_CACHE_KEY = None
_SKELETON_CACHE_JOINTS = set()
_REMAP_HIGHLIGHT_JOINT = None

_SCENE_CALLBACK_IDS = []
_ACTIVE_WINDOW = None
_SAVED_BEFORE_SAVE_FLAGS = (False, False, None)


def _resolve_long_name_cached(node_name, cache):
    """フルパス名をキャッシュ併用で解決し、不要な cmds.ls 呼び出しを避ける。"""
    cached = cache.get(node_name)
    if cached is not None:
        return cached
    if node_name.startswith("|"):
        cache[node_name] = node_name
        return node_name
    try:
        found = cmds.ls(node_name, long=True) or []
        resolved = found[0] if found else node_name
    except Exception:
        resolved = node_name
    cache[node_name] = resolved
    return resolved


def _resolve_existing_joint_long(joint_path):
    """指定されたジョイントが存在すればフルパスを返し、存在しなければ None を返す。"""
    if not joint_path:
        return None
    try:
        if not cmds.objExists(joint_path):
            return None
        found = cmds.ls(joint_path, type="joint", long=True) or []
        return found[0] if found else None
    except Exception:
        return None


def _weight_to_rgb(weight):
    """ウェイト値（0.0～1.0）に応じたヒートマップ RGB タプルを返す。"""
    w = max(0.0, min(1.0, float(weight)))
    stops = (
        (0.00, (0.20, 0.72, 1.00)),
        (0.25, (0.25, 0.90, 0.55)),
        (0.50, (0.95, 0.90, 0.20)),
        (0.75, (1.00, 0.50, 0.12)),
        (1.00, (1.00, 0.18, 0.15)),
    )
    for idx in range(len(stops) - 1):
        t0, c0 = stops[idx]
        t1, c1 = stops[idx + 1]
        if w <= t1:
            span = t1 - t0
            ratio = (w - t0) / span if span > 0.0 else 0.0
            return (
                c0[0] + (c1[0] - c0[0]) * ratio,
                c0[1] + (c1[1] - c0[1]) * ratio,
                c0[2] + (c1[2] - c0[2]) * ratio,
            )
    return (1.00, 0.18, 0.15)


def _safe_set_attr(attr_path, value):
    """アトリビュートが存在し設定可能な場合のみ値を反映する。"""
    try:
        if cmds.objExists(attr_path) and cmds.getAttr(attr_path, settable=True):
            cmds.setAttr(attr_path, value)
            return True
    except Exception:
        pass
    return False


def _safe_get_attr(attr_path, default=None):
    """アトリビュート値を安全に取得する。"""
    try:
        if cmds.objExists(attr_path):
            return cmds.getAttr(attr_path)
    except Exception:
        pass
    return default


def _collect_skeleton_joints_cached(bound_joints):
    """バインド済みボーンが属するスケルトン全体のジョイント集合をキャッシュ付きで返す。"""
    global _SKELETON_CACHE_KEY, _SKELETON_CACHE_JOINTS
    key = tuple(sorted(bound_joints))
    if key == _SKELETON_CACHE_KEY and _SKELETON_CACHE_JOINTS:
        return _SKELETON_CACHE_JOINTS

    roots = set()
    for joint in bound_joints:
        if not cmds.objExists(joint):
            continue
        current = joint
        while True:
            try:
                parents = cmds.listRelatives(current, parent=True, type="joint", fullPath=True) or []
            except Exception:
                parents = []
            if not parents:
                roots.add(current)
                break
            current = parents[0]

    skeleton_joints = set(bound_joints)
    for root in roots:
        skeleton_joints.add(root)
        try:
            descendants = cmds.listRelatives(root, allDescendents=True, type="joint", fullPath=True) or []
            skeleton_joints.update(descendants)
        except Exception:
            pass

    _SKELETON_CACHE_KEY = key
    _SKELETON_CACHE_JOINTS = skeleton_joints
    return skeleton_joints


def collect_joint_display_context(window):
    """SIWeightEditor Improve の現在状態から全バインドボーン・選択列・頂点ウェイトを高速集計する。"""
    long_cache = {}
    all_bound_joints = set()

    if window is None:
        return {
            "all_bound_joints": all_bound_joints,
            "selected_col_joints": set(),
            "weighted_map": {},
            "too_many": False,
            "row_count": 0,
        }

    store = getattr(window, "store_skin_weight", None)
    if store is not None:
        for inf in getattr(store, "all_influences", None) or []:
            all_bound_joints.add(_resolve_long_name_cached(inf, long_cache))

    for inf_list in (getattr(window, "influences_dict", None) or {}).values():
        for inf in inf_list:
            all_bound_joints.add(_resolve_long_name_cached(inf, long_cache))

    for inf in getattr(window, "all_influences", None) or []:
        all_bound_joints.add(_resolve_long_name_cached(inf, long_cache))

    selected_col_joints = set()
    selected_items = getattr(window, "selected_items", None) or []
    all_infs = getattr(window, "all_influences", None) or []
    inf_len = len(all_infs)

    vtx_row_dict = getattr(window, "vtx_row_dict", None) or {}
    weight_model = getattr(window, "weight_model", None)
    model_data = getattr(weight_model, "_data", None) if weight_model else None

    if selected_items:
        target_rows = sorted(set(item.row() for item in selected_items if item.row() in vtx_row_dict))
        seen_cols = set()
        for item in selected_items:
            col = item.column()
            if col not in seen_cols and 0 <= col < inf_len:
                seen_cols.add(col)
                selected_col_joints.add(_resolve_long_name_cached(all_infs[col], long_cache))
    else:
        target_rows = sorted(vtx_row_dict.keys())

    if len(target_rows) > MAX_TARGET_VERTICES:
        return {
            "all_bound_joints": all_bound_joints,
            "selected_col_joints": selected_col_joints,
            "weighted_map": {},
            "too_many": True,
            "row_count": len(target_rows),
        }

    weighted_map = {}
    if model_data and target_rows:
        mesh_sum_arrays = {}
        mesh_inf_lists = {}
        valid_count = 0
        model_len = len(model_data)

        for row in target_rows:
            if row >= model_len:
                continue
            row_data = vtx_row_dict.get(row)
            if not row_data:
                continue
            row_influences = row_data[2]
            mesh_node = row_data[6]
            weight_list = model_data[row]
            valid_count += 1

            sum_arr = mesh_sum_arrays.get(mesh_node)
            if sum_arr is None:
                sum_arr = [0.0] * len(row_influences)
                mesh_sum_arrays[mesh_node] = sum_arr
                mesh_inf_lists[mesh_node] = row_influences

            inf_count = len(sum_arr)
            for inf_id, w in enumerate(weight_list):
                if w > WEIGHT_EPSILON and inf_id < inf_count:
                    sum_arr[inf_id] += w

        if valid_count > 0:
            inv_count = 1.0 / float(valid_count)
            merged_sum = defaultdict(float)
            for mesh_node, sum_arr in mesh_sum_arrays.items():
                inf_list = mesh_inf_lists[mesh_node]
                for inf_id, total_w in enumerate(sum_arr):
                    if total_w > WEIGHT_EPSILON:
                        jp = _resolve_long_name_cached(inf_list[inf_id], long_cache)
                        merged_sum[jp] += total_w
            for jp, total_w in merged_sum.items():
                weighted_map[jp] = total_w * inv_count

    return {
        "all_bound_joints": all_bound_joints,
        "selected_col_joints": selected_col_joints,
        "weighted_map": weighted_map,
        "too_many": False,
        "row_count": len(target_rows),
    }


def _save_joint_color_once(joint):
    """初回変更時のみジョイントの元のカラー設定を退避する（既存退避は上書きしない）。"""
    if joint in _SAVED_COLOR_ATTRS or not cmds.objExists(joint):
        return
    _SAVED_COLOR_ATTRS[joint] = {
        "useObjectColor": _safe_get_attr(joint + ".useObjectColor"),
        "wireColorR": _safe_get_attr(joint + ".wireColorR"),
        "wireColorG": _safe_get_attr(joint + ".wireColorG"),
        "wireColorB": _safe_get_attr(joint + ".wireColorB"),
        "overrideEnabled": _safe_get_attr(joint + ".overrideEnabled"),
        "overrideRGBColors": _safe_get_attr(joint + ".overrideRGBColors"),
        "overrideColor": _safe_get_attr(joint + ".overrideColor"),
        "overrideColorR": _safe_get_attr(joint + ".overrideColorR"),
        "overrideColorG": _safe_get_attr(joint + ".overrideColorG"),
        "overrideColorB": _safe_get_attr(joint + ".overrideColorB"),
    }


def _apply_joint_rgb(joint, rgb, use_maya_wire=True):
    """ジョイントへ指定の RGB カラーを安全に適用する。"""
    if not cmds.objExists(joint):
        return
    _save_joint_color_once(joint)
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])

    if use_maya_wire:
        # オーバーライドをオフにできる場合は wireColorRGB を使用し、ロック時は overrideColorRGB にフォールバックする
        override_off = _safe_set_attr(joint + ".overrideEnabled", 0)
        wire_ok = _safe_set_attr(joint + ".useObjectColor", 2)
        if wire_ok:
            _safe_set_attr(joint + ".wireColorR", r)
            _safe_set_attr(joint + ".wireColorG", g)
            _safe_set_attr(joint + ".wireColorB", b)
        if not override_off:
            if _safe_set_attr(joint + ".overrideRGBColors", 1):
                _safe_set_attr(joint + ".overrideColorR", r)
                _safe_set_attr(joint + ".overrideColorG", g)
                _safe_set_attr(joint + ".overrideColorB", b)
    else:
        _safe_set_attr(joint + ".overrideEnabled", 1)
        _safe_set_attr(joint + ".overrideRGBColors", 1)
        _safe_set_attr(joint + ".overrideColorR", r)
        _safe_set_attr(joint + ".overrideColorG", g)
        _safe_set_attr(joint + ".overrideColorB", b)


def _restore_single_joint_color(joint, saved):
    """退避済みデータから単一ジョイントの元のカラー設定を復元する。"""
    if not cmds.objExists(joint):
        return
    for attr_name in (
        "useObjectColor",
        "wireColorR",
        "wireColorG",
        "wireColorB",
        "overrideRGBColors",
        "overrideColor",
        "overrideColorR",
        "overrideColorG",
        "overrideColorB",
        "overrideEnabled",
    ):
        val = saved.get(attr_name)
        if val is not None:
            _safe_set_attr(joint + "." + attr_name, val)


def restore_joint_colors(keep_remap=False, use_maya_wire=True):
    """退避したジョイントのカラー設定を元の状態へ復元する（必要に応じて Remap 変更先のみ維持）。"""
    remap_joint = _resolve_existing_joint_long(_REMAP_HIGHLIGHT_JOINT) if keep_remap else None
    if not _SAVED_COLOR_ATTRS and not remap_joint:
        return

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        for joint in list(_SAVED_COLOR_ATTRS.keys()):
            if remap_joint and joint == remap_joint:
                continue
            saved = _SAVED_COLOR_ATTRS.pop(joint)
            _restore_single_joint_color(joint, saved)
        if remap_joint:
            _apply_joint_rgb(remap_joint, REMAP_HIGHLIGHT_RGB, use_maya_wire=use_maya_wire)
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def update_joint_hilite(window, maya_ver=2025):
    """Joint Hilite（選択列強調／頂点ウェイト・ヒートマップ）および Remap 変更先ハイライトを更新する。"""
    global _ACTIVE_WINDOW
    if window is not None:
        _ACTIVE_WINDOW = window
    win = _ACTIVE_WINDOW
    use_wire = maya_ver >= 2016
    remap_joint = _resolve_existing_joint_long(_REMAP_HIGHLIGHT_JOINT)
    enabled = bool(win is not None and hasattr(win, "joint_hl_but") and win.joint_hl_but.isChecked())

    if not enabled:
        if remap_joint:
            register_scene_callbacks()
        restore_joint_colors(keep_remap=bool(remap_joint), use_maya_wire=use_wire)
        return

    register_scene_callbacks()
    ctx = collect_joint_display_context(win)
    all_bound = set(ctx["all_bound_joints"])
    if remap_joint:
        all_bound.add(remap_joint)

    if not all_bound:
        restore_joint_colors(keep_remap=False, use_maya_wire=use_wire)
        return

    mode = getattr(win, "joint_hilite_mode", "heatmap")
    selected_cols = ctx["selected_col_joints"]
    weighted_map = ctx["weighted_map"]

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        # 今回の対象外になった退避済みジョイントは元の色へ戻す
        for old_joint in list(_SAVED_COLOR_ATTRS.keys()):
            if old_joint not in all_bound:
                _restore_single_joint_color(old_joint, _SAVED_COLOR_ATTRS.pop(old_joint))

        if mode == "selected":
            # Remap 変更先を最優先でマゼンタ、選択列を黄色で強調し、それ以外は元の色を維持する
            for joint in all_bound:
                if remap_joint and joint == remap_joint:
                    _apply_joint_rgb(joint, REMAP_HIGHLIGHT_RGB, use_maya_wire=use_wire)
                elif joint in selected_cols:
                    _apply_joint_rgb(joint, (1.0, 0.95, 0.25), use_maya_wire=use_wire)
                elif joint in _SAVED_COLOR_ATTRS:
                    _restore_single_joint_color(joint, _SAVED_COLOR_ATTRS.pop(joint))
        else:
            # 頂点ウェイト・ヒートマップ：Remap 変更先を最優先マゼンタ、選択列は黄、ウェイトありはヒートマップ
            if ctx["too_many"] and not selected_cols:
                restore_joint_colors(keep_remap=bool(remap_joint), use_maya_wire=use_wire)
                return
            for joint in all_bound:
                if remap_joint and joint == remap_joint:
                    _apply_joint_rgb(joint, REMAP_HIGHLIGHT_RGB, use_maya_wire=use_wire)
                elif joint in selected_cols:
                    _apply_joint_rgb(joint, (1.0, 1.0, 0.20), use_maya_wire=use_wire)
                elif joint in weighted_map:
                    _apply_joint_rgb(joint, _weight_to_rgb(weighted_map[joint]), use_maya_wire=use_wire)
                else:
                    _apply_joint_rgb(joint, (0.28, 0.28, 0.30), use_maya_wire=use_wire)
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def restore_used_joints(keep_remap=False):
    """退避したジョイントの drawStyle 設定を元の状態へ復元する（必要時は Remap 変更先の可視性を維持）。"""
    remap_joint = _resolve_existing_joint_long(_REMAP_HIGHLIGHT_JOINT) if keep_remap else None
    if not _SAVED_DRAW_STYLE and not remap_joint:
        return

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        for joint in list(_SAVED_DRAW_STYLE.keys()):
            if remap_joint and joint == remap_joint:
                continue
            orig_style = _SAVED_DRAW_STYLE.pop(joint)
            if cmds.objExists(joint):
                _safe_set_attr(joint + ".drawStyle", orig_style)

        if remap_joint:
            attr = remap_joint + ".drawStyle"
            if remap_joint not in _SAVED_DRAW_STYLE:
                orig_val = _safe_get_attr(attr)
                if orig_val == 2:
                    _SAVED_DRAW_STYLE[remap_joint] = orig_val
                    _safe_set_attr(attr, 0)
            else:
                orig_val = _SAVED_DRAW_STYLE.get(remap_joint, 0)
                _safe_set_attr(attr, 0 if orig_val == 2 else orig_val)
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def update_used_joints(window):
    """Used Joints（.drawStyle による使用ボーンのピンポイント表示）を更新する。"""
    global _ACTIVE_WINDOW
    if window is not None:
        _ACTIVE_WINDOW = window
    win = _ACTIVE_WINDOW
    remap_joint = _resolve_existing_joint_long(_REMAP_HIGHLIGHT_JOINT)
    enabled = bool(win is not None and hasattr(win, "used_joint_vis_but") and win.used_joint_vis_but.isChecked())

    if not enabled:
        if remap_joint:
            register_scene_callbacks()
        restore_used_joints(keep_remap=bool(remap_joint))
        return

    register_scene_callbacks()
    ctx = collect_joint_display_context(win)
    if ctx["too_many"]:
        restore_used_joints(keep_remap=bool(remap_joint))
        if hasattr(win, "set_message"):
            win.set_message(
                msg="- Used Joints skipped : too many vertices ({0}) -".format(ctx["row_count"]),
                error=True,
            )
        return

    all_bound = set(ctx["all_bound_joints"])
    used_joints = set(ctx["weighted_map"].keys()) | set(ctx["selected_col_joints"])
    if remap_joint:
        used_joints.add(remap_joint)
        all_bound.add(remap_joint)

    if not all_bound or not used_joints:
        restore_used_joints(keep_remap=bool(remap_joint))
        return

    mode = getattr(win, "used_joints_mode", "exact")
    keep_joints = set(used_joints)
    if mode == "with_children":
        # 骨の三角錐形状を保つため使用ボーンおよび Remap 変更先ボーンの直下の子 1 階層を追加表示する
        for joint in list(used_joints):
            if not cmds.objExists(joint):
                continue
            try:
                children = cmds.listRelatives(joint, children=True, type="joint", fullPath=True) or []
                keep_joints.update(children)
            except Exception:
                pass
    elif remap_joint:
        # exact モードでも Remap 変更先ボーンの向きが視認しやすいよう本体は確実に表示する
        keep_joints.add(remap_joint)

    target_joints = set(_collect_skeleton_joints_cached(all_bound))
    target_joints.update(keep_joints)

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        # 今回のスケルトン対象外になった退避済みジョイントは元へ戻す
        for old_joint in list(_SAVED_DRAW_STYLE.keys()):
            if old_joint not in target_joints:
                orig_style = _SAVED_DRAW_STYLE.pop(old_joint)
                if cmds.objExists(old_joint):
                    _safe_set_attr(old_joint + ".drawStyle", orig_style)

        shown_count = 0
        hidden_count = 0
        for joint in target_joints:
            if not cmds.objExists(joint):
                continue
            attr = joint + ".drawStyle"
            if joint not in _SAVED_DRAW_STYLE:
                orig_val = _safe_get_attr(attr)
                if orig_val is not None:
                    _SAVED_DRAW_STYLE[joint] = orig_val
            if joint in keep_joints:
                orig_val = _SAVED_DRAW_STYLE.get(joint, 0)
                show_val = 0 if orig_val == 2 else orig_val
                _safe_set_attr(attr, show_val)
                shown_count += 1
            else:
                _safe_set_attr(attr, 2)
                hidden_count += 1
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)

    if hasattr(win, "set_message"):
        win.set_message(
            msg="- Used Joints : {0} shown / {1} hidden -".format(shown_count, hidden_count),
            error=False,
        )


def set_remap_target_highlight(joint_path, window=None, maya_ver=2025):
    """Remap 変更先ボーンのビューポートハイライトを設定し、Joint Hilite / Used Joints と連動更新する。"""
    global _REMAP_HIGHLIGHT_JOINT, _ACTIVE_WINDOW
    if window is not None:
        _ACTIVE_WINDOW = window
    _REMAP_HIGHLIGHT_JOINT = _resolve_existing_joint_long(joint_path)
    if _REMAP_HIGHLIGHT_JOINT:
        register_scene_callbacks()
    update_joint_hilite(_ACTIVE_WINDOW, maya_ver=maya_ver)
    update_used_joints(_ACTIVE_WINDOW)


def clear_remap_target_highlight(window=None, maya_ver=2025):
    """Remap 変更先ボーンのハイライトを解除し、元の状態または既存表示モードへ戻す。"""
    set_remap_target_highlight(None, window=window, maya_ver=maya_ver)


def _on_before_save(*args):
    """シーン保存前にジョイントの色・表示スタイルを一時的に元の状態へ戻す。"""
    global _SAVED_BEFORE_SAVE_FLAGS
    _SAVED_BEFORE_SAVE_FLAGS = (bool(_SAVED_COLOR_ATTRS), bool(_SAVED_DRAW_STYLE), _REMAP_HIGHLIGHT_JOINT)
    if _SAVED_COLOR_ATTRS:
        restore_joint_colors(keep_remap=False)
    if _SAVED_DRAW_STYLE:
        restore_used_joints(keep_remap=False)


def _on_after_save(*args):
    """シーン保存後に必要に応じて Joint Hilite / Used Joints / Remap ハイライトを再適用する。"""
    global _SAVED_BEFORE_SAVE_FLAGS, _REMAP_HIGHLIGHT_JOINT
    restore_hl, restore_used, saved_remap = _SAVED_BEFORE_SAVE_FLAGS
    _SAVED_BEFORE_SAVE_FLAGS = (False, False, None)
    _REMAP_HIGHLIGHT_JOINT = _resolve_existing_joint_long(saved_remap)
    win = _ACTIVE_WINDOW
    if win is not None and getattr(win, "closed_flag", False):
        return
    if restore_hl or _REMAP_HIGHLIGHT_JOINT:
        try:
            update_joint_hilite(win)
        except Exception:
            pass
    if restore_used or _REMAP_HIGHLIGHT_JOINT:
        try:
            update_used_joints(win)
        except Exception:
            pass


def _on_scene_reset(*args):
    """新規シーンやシーン読み込み前に退避辞書とキャッシュを安全にクリアする。"""
    global _SKELETON_CACHE_KEY, _SKELETON_CACHE_JOINTS, _SAVED_BEFORE_SAVE_FLAGS, _REMAP_HIGHLIGHT_JOINT
    _SAVED_COLOR_ATTRS.clear()
    _SAVED_DRAW_STYLE.clear()
    _SKELETON_CACHE_KEY = None
    _SKELETON_CACHE_JOINTS = set()
    _REMAP_HIGHLIGHT_JOINT = None
    _SAVED_BEFORE_SAVE_FLAGS = (False, False, None)


def register_scene_callbacks():
    """シーン保存・リセット時の保護コールバックを登録する。"""
    if _SCENE_CALLBACK_IDS:
        return
    specs = (
        (om2.MSceneMessage.kBeforeSave, _on_before_save),
        (om2.MSceneMessage.kAfterSave, _on_after_save),
        (om2.MSceneMessage.kBeforeNew, _on_scene_reset),
        (om2.MSceneMessage.kBeforeOpen, _on_scene_reset),
    )
    for event_type, handler in specs:
        try:
            cb_id = om2.MSceneMessage.addCallback(event_type, handler)
            _SCENE_CALLBACK_IDS.append(cb_id)
        except Exception:
            pass


def unregister_scene_callbacks():
    """登録済みのシーン保護コールバックを解除する。"""
    global _SCENE_CALLBACK_IDS
    for cb_id in list(_SCENE_CALLBACK_IDS):
        try:
            om2.MMessage.removeCallback(cb_id)
        except Exception:
            pass
    _SCENE_CALLBACK_IDS = []


def cleanup():
    """ウィンドウ終了時やアンロード時に元のジョイント状態へ復元してコールバックを解除する。"""
    global _ACTIVE_WINDOW, _SKELETON_CACHE_KEY, _SKELETON_CACHE_JOINTS, _SAVED_BEFORE_SAVE_FLAGS, _REMAP_HIGHLIGHT_JOINT
    _REMAP_HIGHLIGHT_JOINT = None
    restore_joint_colors(keep_remap=False)
    restore_used_joints(keep_remap=False)
    unregister_scene_callbacks()
    _ACTIVE_WINDOW = None
    _SKELETON_CACHE_KEY = None
    _SKELETON_CACHE_JOINTS = set()
    _SAVED_BEFORE_SAVE_FLAGS = (False, False, None)
