# -*- coding: utf-8 -*-
"""選択頂点のウェイト先ボーンを 3D ビューポート上で視覚化する制御モジュール。"""

import os
from collections import defaultdict

from maya import cmds
import maya.api.OpenMaya as om2
import maya.api.OpenMayaRender as omr
import maya.api.OpenMayaUI as omui


NODE_TYPE_NAME = "siweBoneVisualizer"
TRANSFORM_NAME = "siweBoneVisualizerNode"
SHAPE_NAME = "siweBoneVisualizerShape"
PLUGIN_FILENAME = "siwe_bone_visualizer.py"

# 計算負荷を抑えるための対象頂点数の上限
MAX_TARGET_VERTICES = 2000
# 頂点重心の計算に使うサンプリング上限（複数選択時の描画を軽量に保つ）
MAX_SAMPLE_VERTICES = 64
# 描画対象とみなす最小ウェイトしきい値
WEIGHT_EPSILON = 0.0001


# 描画プラグイン（siwe_bone_visualizer.py）と共有する表示状態
STATE = {
    "version": 0,
    "enabled": False,
    "mesh_vertices": [],
    "influences": [],
    "show_lines": True,
    "show_labels": True,
    "dashed_tiny": True,
    "tiny_threshold": 0.05,
    "aggregate_mode": "avg",
    "max_weight": 100.0,
    "decimals": 1,
    "vertex_count": 0,
    "remap_target": None,
}

_SCENE_CALLBACK_IDS = []
_RESTORE_AFTER_SAVE = False


def _bump_version():
    """描画キャッシュ更新のトリガーとなる状態バージョンを進める。"""
    STATE["version"] = int(STATE.get("version", 0)) + 1


def get_bundled_plugin_path():
    """スクリプト位置から相対パスで描画プラグインのフルパスを解決する。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    contents_dir = os.path.abspath(os.path.join(current_dir, os.pardir, os.pardir, os.pardir))
    return os.path.join(contents_dir, "plug-ins", PLUGIN_FILENAME)


def ensure_plugin_loaded():
    """描画プラグインが未ロードであれば相対パス優先でロードする。"""
    try:
        if cmds.pluginInfo(PLUGIN_FILENAME, query=True, loaded=True):
            return True
    except Exception:
        pass

    plugin_path = get_bundled_plugin_path()
    try:
        if os.path.exists(plugin_path):
            cmds.loadPlugin(plugin_path, quiet=True)
        else:
            cmds.loadPlugin(PLUGIN_FILENAME, quiet=True)
        return bool(cmds.pluginInfo(PLUGIN_FILENAME, query=True, loaded=True))
    except Exception as exc:
        print("SIWeightEditor Improve bone visualizer plugin load error:", exc)
        return False


def get_visualizer_transform():
    """シーン内に存在するビジュアライザーのトランスフォーム名を返す。"""
    if cmds.objExists(TRANSFORM_NAME):
        return TRANSFORM_NAME
    return None


def get_visualizer_shape():
    """シーン内に存在するビジュアライザーのシェイプ名を返す。"""
    if cmds.objExists(SHAPE_NAME):
        return SHAPE_NAME
    transform = get_visualizer_transform()
    if transform:
        shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
        for shape in shapes:
            try:
                if cmds.nodeType(shape) == NODE_TYPE_NAME:
                    return shape
            except Exception:
                pass
    return None


def ensure_node():
    """一時描画用ノードが無ければ作成し、アウトライナー非表示・選択不可に設定する。"""
    if not ensure_plugin_loaded():
        return None

    shape = get_visualizer_shape()
    if shape:
        return shape

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        transform = get_visualizer_transform()
        if not transform:
            transform = cmds.createNode("transform", name=TRANSFORM_NAME, skipSelect=True)
        shape = cmds.createNode(NODE_TYPE_NAME, name=SHAPE_NAME, parent=transform, skipSelect=True)

        # アウトライナーから隠し、ビューポート上での誤選択や影の描画を防ぐ
        _safe_set_attr(transform + ".hiddenInOutliner", True)
        _safe_set_attr(shape + ".hiddenInOutliner", True)
        _safe_set_attr(shape + ".overrideEnabled", True)
        _safe_set_attr(shape + ".overrideDisplayType", 2)
        _safe_set_attr(shape + ".castsShadows", False)
        _safe_set_attr(shape + ".receiveShadows", False)
        return shape
    except Exception as exc:
        print("SIWeightEditor Improve bone visualizer node create error:", exc)
        return None
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def remove_node():
    """一時描画用ノードをアンドゥ履歴に残さず安全に削除する。"""
    targets = []
    transform = get_visualizer_transform()
    if transform:
        targets.append(transform)
    else:
        shape = get_visualizer_shape()
        if shape:
            targets.append(shape)

    if not targets:
        return

    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        for node in targets:
            if cmds.objExists(node):
                cmds.delete(node)
    except Exception:
        pass
    finally:
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def _safe_set_attr(attr_path, value):
    """アトリビュートが存在し設定可能な場合のみ値を反映する。"""
    try:
        if cmds.objExists(attr_path):
            cmds.setAttr(attr_path, value)
    except Exception:
        pass


def mark_dirty():
    """Viewport 2.0 に対して再描画を通知する。"""
    shape = get_visualizer_shape()
    if shape:
        try:
            sel = om2.MSelectionList()
            sel.add(shape)
            mobj = sel.getDependNode(0)
            omr.MRenderer.setGeometryDrawDirty(mobj)
        except Exception:
            pass
    try:
        view = omui.M3dView.active3dView()
        if view:
            view.refresh(False, True)
    except Exception:
        try:
            cmds.refresh(currentView=True)
        except Exception:
            pass


def set_options(show_lines=None, show_labels=None, dashed_tiny=None, aggregate_mode=None,
                tiny_threshold=None, max_weight=None, decimals=None):
    """表示オプションを更新する。"""
    changed = False
    if show_lines is not None and STATE["show_lines"] != bool(show_lines):
        STATE["show_lines"] = bool(show_lines)
        changed = True
    if show_labels is not None and STATE["show_labels"] != bool(show_labels):
        STATE["show_labels"] = bool(show_labels)
        changed = True
    if dashed_tiny is not None and STATE["dashed_tiny"] != bool(dashed_tiny):
        STATE["dashed_tiny"] = bool(dashed_tiny)
        changed = True
    if aggregate_mode in ("avg", "max") and STATE["aggregate_mode"] != aggregate_mode:
        STATE["aggregate_mode"] = aggregate_mode
        changed = True
    if tiny_threshold is not None and STATE["tiny_threshold"] != float(tiny_threshold):
        STATE["tiny_threshold"] = float(tiny_threshold)
        changed = True
    if max_weight is not None and STATE["max_weight"] != float(max_weight):
        STATE["max_weight"] = float(max_weight)
        changed = True
    if decimals is not None and STATE["decimals"] != int(decimals):
        STATE["decimals"] = int(decimals)
        changed = True
    if changed:
        _bump_version()


def set_remap_target(joint_path, label=None):
    """Remap 変更先ボーンの 3D X-Ray ハイライト表示を設定または解除する。"""
    if joint_path and cmds.objExists(joint_path):
        short_name = label or joint_path.split("|")[-1].split(":")[-1]
        STATE["remap_target"] = {
            "joint": joint_path,
            "label": "[Target] " + short_name,
        }
        register_scene_callbacks()
        _bump_version()
        ensure_node()
        mark_dirty()
    else:
        if STATE.get("remap_target") is None:
            return
        STATE["remap_target"] = None
        _bump_version()
        if not STATE.get("enabled"):
            remove_node()
        mark_dirty()


def clear_state(delete_node=False):
    """描画データのみ、または一時ノードごとクリアする。"""
    STATE["mesh_vertices"] = []
    STATE["influences"] = []
    STATE["vertex_count"] = 0
    if delete_node:
        STATE["enabled"] = False
        if not STATE.get("remap_target"):
            remove_node()
    _bump_version()
    mark_dirty()


def _on_before_save(*args):
    """シーン保存前に一時ノードを削除してシーンファイルへの混入を防ぐ。"""
    global _RESTORE_AFTER_SAVE
    has_active = bool(STATE.get("enabled") or STATE.get("remap_target"))
    _RESTORE_AFTER_SAVE = bool(has_active and get_visualizer_shape())
    if _RESTORE_AFTER_SAVE:
        remove_node()


def _on_after_save(*args):
    """シーン保存後に必要に応じて一時ノードを復元する。"""
    global _RESTORE_AFTER_SAVE
    has_active = bool(STATE.get("enabled") or STATE.get("remap_target"))
    if _RESTORE_AFTER_SAVE and has_active:
        _RESTORE_AFTER_SAVE = False
        ensure_node()
        _bump_version()
        mark_dirty()
    else:
        _RESTORE_AFTER_SAVE = False


def _on_scene_reset(*args):
    """新規シーンやシーン読み込み前に状態と一時ノードを初期化する。"""
    global _RESTORE_AFTER_SAVE
    _RESTORE_AFTER_SAVE = False
    STATE["remap_target"] = None
    clear_state(delete_node=False)
    remove_node()


def register_scene_callbacks():
    """シーン保存・リセット時の保護コールバックを登録する。"""
    if _SCENE_CALLBACK_IDS:
        return
    callback_specs = (
        (om2.MSceneMessage.kBeforeSave, _on_before_save),
        (om2.MSceneMessage.kAfterSave, _on_after_save),
        (om2.MSceneMessage.kBeforeNew, _on_scene_reset),
        (om2.MSceneMessage.kBeforeOpen, _on_scene_reset),
    )
    for event_type, handler in callback_specs:
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
    """ウィンドウ終了時やアンロード時にノード・コールバック・状態を片付ける。"""
    global _RESTORE_AFTER_SAVE
    _RESTORE_AFTER_SAVE = False
    STATE["remap_target"] = None
    unregister_scene_callbacks()
    clear_state(delete_node=True)


def _resolve_long_name_cached(node_name, cache):
    """フルパス名をキャッシュ併用で取得し、不要な cmds.ls 呼び出しを避ける。"""
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


def _sample_mesh_vertices(mesh_vtx_map, max_samples):
    """複数頂点選択時に重心計算用の頂点IDを均等サンプリングして軽量化する。"""
    total_count = sum(len(vids) for vids in mesh_vtx_map.values())
    if total_count <= max_samples or max_samples <= 0:
        return list(mesh_vtx_map.items())

    step = max(1, total_count // max_samples)
    sampled = []
    for mesh_path, vids in mesh_vtx_map.items():
        picked = vids[::step]
        if not picked and vids:
            picked = [vids[0]]
        if picked:
            sampled.append((mesh_path, picked))
    return sampled


def update_from_window(window, max_weight=100.0, decimals=1):
    """SIWeightEditor Improve の現在状態から選択頂点と影響ボーンを高速集計し 3D 描画を更新する。"""
    enabled = bool(hasattr(window, "bone_vis_but") and window.bone_vis_but.isChecked())
    STATE["enabled"] = enabled
    STATE["max_weight"] = float(max_weight)
    STATE["decimals"] = int(decimals)

    if not enabled:
        clear_state(delete_node=True)
        return

    register_scene_callbacks()

    if not hasattr(window, "vtx_row_dict") or not hasattr(window, "weight_model"):
        clear_state(delete_node=False)
        return

    model_data = getattr(window.weight_model, "_data", None)
    vtx_row_dict = window.vtx_row_dict
    if not model_data or not vtx_row_dict:
        clear_state(delete_node=False)
        return

    long_name_cache = {}
    selected_items = getattr(window, "selected_items", None) or []
    selected_joints = set()

    if selected_items:
        target_rows = sorted(set(item.row() for item in selected_items if item.row() in vtx_row_dict))
        all_influences = getattr(window, "all_influences", []) or []
        inf_len = len(all_influences)
        seen_cols = set()
        for item in selected_items:
            col = item.column()
            if col not in seen_cols and 0 <= col < inf_len:
                seen_cols.add(col)
                selected_joints.add(_resolve_long_name_cached(all_influences[col], long_name_cache))
    else:
        target_rows = sorted(vtx_row_dict.keys())

    if not target_rows:
        clear_state(delete_node=False)
        return

    # 頂点数が極端に多い場合は負荷を避けるため描画をスキップする
    if len(target_rows) > MAX_TARGET_VERTICES:
        clear_state(delete_node=False)
        if hasattr(window, "set_message"):
            window.set_message(
                msg="- Bone Vis skipped : too many vertices ({0}) -".format(len(target_rows)),
                error=True,
            )
        return

    mesh_vtx_map = defaultdict(list)
    mesh_sum_arrays = {}
    mesh_max_arrays = {}
    mesh_inf_lists = {}
    valid_row_count = 0
    model_len = len(model_data)

    # メッシュごとの配列で集計することで複数頂点時の辞書操作コストを抑える
    for row in target_rows:
        if row >= model_len:
            continue
        row_data = vtx_row_dict.get(row)
        if not row_data:
            continue

        vtx_id = row_data[0]
        row_influences = row_data[2]
        mesh_node = row_data[6]
        weight_list = model_data[row]

        mesh_vtx_map[mesh_node].append(int(vtx_id))
        valid_row_count += 1

        sum_arr = mesh_sum_arrays.get(mesh_node)
        if sum_arr is None:
            inf_count = len(row_influences)
            sum_arr = [0.0] * inf_count
            max_arr = [0.0] * inf_count
            mesh_sum_arrays[mesh_node] = sum_arr
            mesh_max_arrays[mesh_node] = max_arr
            mesh_inf_lists[mesh_node] = row_influences
        else:
            max_arr = mesh_max_arrays[mesh_node]

        inf_count = len(sum_arr)
        for inf_id, weight_val in enumerate(weight_list):
            if weight_val > WEIGHT_EPSILON and inf_id < inf_count:
                sum_arr[inf_id] += weight_val
                if weight_val > max_arr[inf_id]:
                    max_arr[inf_id] = weight_val

    if valid_row_count == 0:
        clear_state(delete_node=False)
        return

    weight_sum_map = defaultdict(float)
    weight_max_map = defaultdict(float)
    for mesh_node, sum_arr in mesh_sum_arrays.items():
        max_arr = mesh_max_arrays[mesh_node]
        inf_list = mesh_inf_lists[mesh_node]
        for inf_id, total_w in enumerate(sum_arr):
            if total_w > WEIGHT_EPSILON:
                joint_path = _resolve_long_name_cached(inf_list[inf_id], long_name_cache)
                weight_sum_map[joint_path] += total_w
                if max_arr[inf_id] > weight_max_map[joint_path]:
                    weight_max_map[joint_path] = max_arr[inf_id]

    if not weight_sum_map:
        clear_state(delete_node=False)
        return

    aggregate_mode = STATE.get("aggregate_mode", "avg")
    inv_row_count = 1.0 / float(valid_row_count)
    influences_entries = []
    for joint_path, total_w in weight_sum_map.items():
        agg_weight = weight_max_map[joint_path] if aggregate_mode == "max" else total_w * inv_row_count
        if agg_weight <= WEIGHT_EPSILON:
            continue

        short_name = joint_path.split("|")[-1].split(":")[-1]
        influences_entries.append({
            "joint": joint_path,
            "label": short_name,
            "weight": agg_weight,
            "selected": joint_path in selected_joints,
        })

    # ウェイトが大きい順に並べる
    influences_entries.sort(key=lambda entry: entry["weight"], reverse=True)

    STATE["mesh_vertices"] = _sample_mesh_vertices(mesh_vtx_map, MAX_SAMPLE_VERTICES)
    STATE["influences"] = influences_entries
    STATE["vertex_count"] = valid_row_count
    _bump_version()

    shape = ensure_node()
    if not shape and hasattr(window, "set_message"):
        window.set_message(
            msg="- Bone Vis Error : failed to load siwe_bone_visualizer.py -",
            error=True,
        )
        return
    mark_dirty()
