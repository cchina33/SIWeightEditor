# -*- coding: utf-8 -*-
"""スキニング済みメッシュ＆ボーンの修正（記録→解除→修正→再バインド＆補間復元）、ジョイント整理・Joint Orient、メッシュ＆シーンクリーンアップ専用ウィンドウ。"""

import datetime as dt
import json
import math
import os
import re

from maya import cmds
from maya import mel
import maya.api.OpenMaya as om2
import maya.api.OpenMayaAnim as oma2
import maya.api.OpenMayaRender as omr

try:
    from importlib import reload as _reload
except ImportError:
    from imp import reload as _reload

from .. import common
from .. import lang
from .. import qt
from . import reference_joint_helper
from . import rig_cleanup_helper
from . import unweighted_bind_window
from . import weight_mirror_copy_helper
_reload(rig_cleanup_helper)
_reload(reference_joint_helper)
_reload(unweighted_bind_window)
_reload(weight_mirror_copy_helper)
from ..qt import *


WEIGHT_EPSILON = 1e-6
DATA_DIR_NAME = "siweighteditor_improve_model_edit"
LAST_JOINT_CACHE_FILENAME = "_last_joint_cache.json"

# 各タブの表示・非表示設定（True: 表示 / False: 非表示）
TAB_VISIBILITY = {
    "model_bone": True,      # モデル / ボーン修正
    "bind_mirror": True,     # バインド / ミラー＆コピー
    "joint_orient": True,    # ジョイント整理 / Orient
    "joint_inspect": False,  # ジョイント診断 / 調整
    "ref_joint": False,      # リファレンス軸上書き
    "cleanup": True,         # メッシュ / シーンクリーンアップ
}


def _get_storage_dir():
    """モデリング修正用 JSON バックアップの保存ディレクトリを返す。"""
    base_dir = os.getenv("MAYA_APP_DIR") or os.path.expanduser("~")
    path = os.path.join(base_dir, "Scripting_Files", DATA_DIR_NAME)
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _get_last_joint_cache_path():
    """修正開始時の記憶ジョイントを一時保存するキャッシュ JSON パスを返す。"""
    return os.path.join(_get_storage_dir(), LAST_JOINT_CACHE_FILENAME)


def _save_last_seed_joints(joint_paths):
    """使用したジョイント名一覧を一時キャッシュ JSON へ保存する。"""
    unique_joints = []
    seen = set()
    for jnt in (joint_paths or []):
        if jnt and jnt not in seen:
            seen.add(jnt)
            unique_joints.append(jnt)
    if not unique_joints:
        return []
    payload = {
        "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "joints": unique_joints,
    }
    try:
        with open(_get_last_joint_cache_path(), "w") as fp:
            json.dump(payload, fp)
    except Exception:
        pass
    return unique_joints


def _load_last_seed_joints():
    """一時キャッシュ JSON から記憶済みのジョイント名一覧を読み込む。"""
    cache_path = _get_last_joint_cache_path()
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, "r") as fp:
            data = json.load(fp)
        joints = data.get("joints") or []
        return [j for j in joints if isinstance(j, str) and j]
    except Exception:
        return []


def _clear_last_seed_joints():
    """記憶済みジョイントの一時キャッシュを削除する。"""
    cache_path = _get_last_joint_cache_path()
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except Exception:
        pass


def _resolve_cached_joints(cached_joints):
    """記憶済みジョイント一覧を現在シーンに存在するジョイントフルパス一覧へ解決する。"""
    resolved = []
    seen = set()
    for jnt in (cached_joints or []):
        if cmds.objExists(jnt):
            found = cmds.ls(jnt, type="joint", long=True) or []
            for item in found:
                if item not in seen:
                    seen.add(item)
                    resolved.append(item)
            continue
        short_name = jnt.split("|")[-1]
        found = cmds.ls(short_name, type="joint", long=True) or []
        if found and found[0] not in seen:
            seen.add(found[0])
            resolved.append(found[0])
    return resolved


def _is_hips_named_joint(joint_path):
    """ジョイント名（ネームスペース除外）が '[キャラ名]_Hips' 形式かどうかを判定する。"""
    if not joint_path:
        return False
    short_name = str(joint_path).split("|")[-1]
    base_name = short_name.split(":")[-1]
    return bool(re.match(r"^.+_Hips$", base_name, re.IGNORECASE))


def _find_hips_joint_in_skeleton(seed_joints):
    """指定ジョイント群と同一スケルトン内に存在する '[キャラ名]_Hips' ジョイントを探索して返す。"""
    roots = []
    seen_roots = set()
    for jnt in (seed_joints or []):
        if not jnt or not cmds.objExists(jnt):
            continue
        cur = jnt
        while True:
            parents = cmds.listRelatives(cur, parent=True, type="joint", fullPath=True) or []
            if not parents:
                break
            cur = parents[0]
        if cur not in seen_roots:
            seen_roots.add(cur)
            roots.append(cur)

    for root in roots:
        if _is_hips_named_joint(root):
            return root
        desc = cmds.listRelatives(root, allDescendents=True, type="joint", fullPath=True) or []
        for child in desc:
            if _is_hips_named_joint(child):
                return child
    return None


def _make_safe_filename(mesh_name):
    """メッシュ名からファイルシステム上安全な JSON ファイル名を生成する。"""
    short_name = mesh_name.split("|")[-1]
    safe = re.sub(r'[\\/:*?"<>|]+', "_", short_name).strip("_")
    return (safe or "mesh") + ".json"


def _get_visible_mesh_dag(node_path):
    """メッシュトランスフォームまたはシェイプから中間オブジェクト以外の可視メッシュ DAG を返す。"""
    try:
        sel = om2.MSelectionList()
        sel.add(node_path)
        dag_path = sel.getDagPath(0)
    except Exception:
        return None

    if dag_path.node().hasFn(om2.MFn.kMesh):
        return dag_path

    shape_count = dag_path.numberOfShapesDirectlyBelow()
    fallback_dag = None
    for idx in range(shape_count):
        shape_dag = om2.MDagPath(dag_path)
        try:
            shape_dag.extendToShape(idx)
        except Exception:
            continue
        if not shape_dag.node().hasFn(om2.MFn.kMesh):
            continue
        if fallback_dag is None:
            fallback_dag = shape_dag
        try:
            if not om2.MFnDagNode(shape_dag).isIntermediateObject:
                return shape_dag
        except Exception:
            return shape_dag
    return fallback_dag


def _get_skin_cluster(mesh_node):
    """メッシュに接続されているスキンクラスタ名を返す。"""
    try:
        history = cmds.listHistory(mesh_node, pruneDagObjects=True) or []
        skins = cmds.ls(history, type="skinCluster") or []
        if skins:
            return skins[0]
        # pruneDagObjects で拾えない構成にもフォールバックする
        history = cmds.listHistory(mesh_node) or []
        skins = cmds.ls(history, type="skinCluster") or []
        return skins[0] if skins else None
    except Exception:
        return None


def _safe_freeze_transform(node):
    """ロック・接続済みチャンネルを避けて安全にフリーズトランスフォームを実行する。"""
    def _can_freeze(prefix):
        for axis in ("X", "Y", "Z"):
            attr = "{0}.{1}{2}".format(node, prefix, axis)
            try:
                if not cmds.getAttr(attr, settable=True):
                    return False
                if cmds.listConnections(attr, source=True, destination=False):
                    return False
            except Exception:
                return False
        return True

    can_t = _can_freeze("translate")
    can_r = _can_freeze("rotate")
    can_s = _can_freeze("scale")
    if can_t or can_r or can_s:
        try:
            cmds.makeIdentity(
                node,
                apply=True,
                translate=can_t,
                rotate=can_r,
                scale=can_s,
                normal=False,
                preserveNormals=True,
            )
        except Exception as exc:
            print("ModelEdit freeze skipped/warning:", node, exc)


def _cleanup_unused_intermediate_shapes(mesh_node):
    """不要になった中間メッシュシェイプを削除する。"""
    shapes = cmds.listRelatives(mesh_node, shapes=True, fullPath=True, type="mesh") or []
    for shape in shapes:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                conns = cmds.listConnections(shape, source=True, destination=True) or []
                if not conns:
                    cmds.delete(shape)
        except Exception:
            pass


def _compute_barycentric_weights(pt, a, b, c):
    """点 pt の三角形 (a, b, c) 上における重心座標 (wa, wb, wc) を安全に算出する。"""
    v0x, v0y, v0z = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    v1x, v1y, v1z = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    v2x, v2y, v2z = pt[0] - a[0], pt[1] - a[1], pt[2] - a[2]

    d00 = v0x * v0x + v0y * v0y + v0z * v0z
    d01 = v0x * v1x + v0y * v1y + v0z * v1z
    d11 = v1x * v1x + v1y * v1y + v1z * v1z
    d20 = v2x * v0x + v2y * v0y + v2z * v0z
    d21 = v2x * v1x + v2y * v1y + v2z * v1z

    denom = d00 * d11 - d01 * d01
    if abs(denom) > 1e-12:
        vb = (d11 * d20 - d01 * d21) / denom
        vc = (d00 * d21 - d01 * d20) / denom
        va = 1.0 - vb - vc
        # 数値誤差によるわずかな負値をクランプして正規化する
        va = max(0.0, va)
        vb = max(0.0, vb)
        vc = max(0.0, vc)
        total = va + vb + vc
        if total > 1e-12:
            inv = 1.0 / total
            return va * inv, vb * inv, vc * inv

    # 面積がほぼ 0 の縮退三角形では 3 頂点への逆距離加重へフォールバックする
    dists = []
    for vert in (a, b, c):
        dx = pt[0] - vert[0]
        dy = pt[1] - vert[1]
        dz = pt[2] - vert[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 1e-9:
            return (1.0, 0.0, 0.0) if vert is a else ((0.0, 1.0, 0.0) if vert is b else (0.0, 0.0, 1.0))
        dists.append(1.0 / d)
    inv = 1.0 / sum(dists)
    return dists[0] * inv, dists[1] * inv, dists[2] * inv


def _normalize_and_limit_weights(weight_dict, maintain_max, max_infs):
    """ウェイト辞書を最大インフルエンス数制限および合計 1.0 正規化して返す。"""
    items = [(idx, w) for idx, w in weight_dict.items() if w > WEIGHT_EPSILON]
    if not items:
        return {}
    if maintain_max and max_infs > 0 and len(items) > max_infs:
        items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[:max_infs]
    total = sum(w for _, w in items)
    if total <= WEIGHT_EPSILON:
        return {}
    inv = 1.0 / total
    return {idx: w * inv for idx, w in items}


def _format_frame_value(frame_val):
    """フレーム数値を整数なら小数なし、端数があれば小数表記で返す。"""
    fval = float(frame_val)
    if abs(fval - round(fval)) < 1e-6:
        return str(int(round(fval)))
    return "{0:.2f}".format(fval)


def export_mesh_skin_json(mesh_long, saved_frame=None, edit_frame=None):
    """対象メッシュの全頂点ウェイト・形状・スキン設定・ボーン情報・フレーム情報を JSON ファイルへ保存する。"""
    skin_cluster = _get_skin_cluster(mesh_long)
    if not skin_cluster:
        raise RuntimeError("No skinCluster found on: {0}".format(mesh_long))

    shape_dag = _get_visible_mesh_dag(mesh_long)
    if shape_dag is None:
        raise RuntimeError("No visible mesh shape found on: {0}".format(mesh_long))

    mesh_fn = om2.MFnMesh(shape_dag)
    num_verts = mesh_fn.numVertices
    if num_verts <= 0:
        raise RuntimeError("Mesh has no vertices: {0}".format(mesh_long))

    points = mesh_fn.getPoints(om2.MSpace.kWorld)
    points_list = [[float(p.x), float(p.y), float(p.z)] for p in points]
    poly_counts, poly_connects = mesh_fn.getVertices()

    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    inf_dags = skin_fn.influenceObjects()
    inf_count = len(inf_dags)
    if inf_count == 0:
        raise RuntimeError("SkinCluster has no influences: {0}".format(skin_cluster))

    influences = [inf_dags[i].fullPathName() for i in range(inf_count)]
    joint_snapshot = rig_cleanup_helper.snapshot_joints_transform(influences)

    comp_fn = om2.MFnSingleIndexedComponent()
    vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
    comp_fn.setCompleteData(num_verts)
    flat_weights, _ = skin_fn.getWeights(shape_dag, vtx_comp)

    # スパース形式で保存してファイルサイズと読み書き時間を抑える
    sparse_weights = []
    for vid in range(num_verts):
        base = vid * inf_count
        entries = []
        for inf_idx in range(inf_count):
            w = float(flat_weights[base + inf_idx])
            if w > WEIGHT_EPSILON:
                entries.append([inf_idx, w])
        sparse_weights.append(entries)

    skin_params = {
        "skinningMethod": int(cmds.getAttr(skin_cluster + ".skinningMethod")),
        "dropoffRate": float(cmds.getAttr(skin_cluster + ".dropoffRate")),
        "maintainMaxInfluences": bool(cmds.getAttr(skin_cluster + ".maintainMaxInfluences")),
        "maxInfluences": int(cmds.getAttr(skin_cluster + ".maxInfluences")),
        "bindMethod": int(cmds.getAttr(skin_cluster + ".bindMethod")),
        "normalizeWeights": int(cmds.getAttr(skin_cluster + ".normalizeWeights")),
    }

    payload = {
        "version": 2,
        "mesh_short": mesh_long.split("|")[-1],
        "mesh_long": mesh_long,
        "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "editing",
        "saved_frame": float(saved_frame) if saved_frame is not None else None,
        "edit_frame": float(edit_frame) if edit_frame is not None else None,
        "vertex_count": num_verts,
        "influences": influences,
        "joint_snapshot": joint_snapshot,
        "skin_params": skin_params,
        "points": points_list,
        "poly_counts": [int(c) for c in poly_counts],
        "poly_connects": [int(i) for i in poly_connects],
        "weights": sparse_weights,
    }

    save_path = os.path.join(_get_storage_dir(), _make_safe_filename(mesh_long))
    with open(save_path, "w") as fp:
        json.dump(payload, fp)
    return save_path, payload


def _resolve_existing_influences(saved_influences):
    """JSON 内のボーン名リストを現在シーンの存在するフルパス一覧へ解決する。"""
    resolved = []
    missing = []
    for saved_inf in saved_influences:
        if cmds.objExists(saved_inf):
            found = cmds.ls(saved_inf, long=True) or [saved_inf]
            resolved.append(found[0])
            continue
        short_name = saved_inf.split("|")[-1]
        found = cmds.ls(short_name, type="joint", long=True) or []
        if found:
            resolved.append(found[0])
        else:
            missing.append(saved_inf)
    return resolved, missing


def _detach_and_freeze_for_modeling(mesh_long):
    """バインド解除・ヒストリ削除・安全フリーズを実行して修正可能状態にする。"""
    dummy = common.TemporaryReparent().main(mode="create")
    try:
        common.TemporaryReparent().main(mesh_long, dummyParent=dummy, mode="cut")

        skin_cluster = _get_skin_cluster(mesh_long)
        if skin_cluster and cmds.objExists(skin_cluster):
            try:
                cmds.skinCluster(skin_cluster, e=True, unbind=True)
            except Exception:
                pass

        try:
            cmds.delete(mesh_long, constructionHistory=True)
        except Exception:
            pass

        _cleanup_unused_intermediate_shapes(mesh_long)
        _safe_freeze_transform(mesh_long)
    finally:
        try:
            common.TemporaryReparent().main(mesh_long, dummyParent=dummy, mode="parent")
        except Exception:
            pass
        try:
            common.TemporaryReparent().main(dummyParent=dummy, mode="delete")
        except Exception:
            pass


def _compute_restored_weights_by_surface(new_points, payload, old_to_new_inf_map, maintain_max, max_infs):
    """元メッシュ形状の三角形サーフェスから重心座標補間で全頂点ウェイトを算出する。"""
    orig_points = payload["points"]
    orig_weights = payload["weights"]
    poly_counts = payload["poly_counts"]
    poly_connects = payload["poly_connects"]

    # JSON 内の元ポリゴン形状を一時メッシュとして構築し、MMeshIntersector で高速補間する
    prev_undo = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)
    temp_xform = None
    try:
        temp_xform = cmds.createNode("transform", name="siweModelEditTempRef", skipSelect=True)
        sel = om2.MSelectionList()
        sel.add(temp_xform)
        parent_obj = sel.getDependNode(0)

        om_pts = om2.MPointArray([om2.MPoint(p[0], p[1], p[2]) for p in orig_points])
        temp_mesh_fn = om2.MFnMesh()
        temp_mesh_obj = temp_mesh_fn.create(om_pts, poly_counts, poly_connects, parent=parent_obj)

        intersector = om2.MMeshIntersector()
        intersector.create(temp_mesh_obj)

        restored_per_vtx = []
        changed_vids = []

        for vid, pt in enumerate(new_points):
            om_pt = om2.MPoint(pt.x, pt.y, pt.z)
            pom = intersector.getClosestPoint(om_pt)
            tri_vids = temp_mesh_fn.getPolygonTriangleVertices(pom.face, pom.triangle)
            v0, v1, v2 = int(tri_vids[0]), int(tri_vids[1]), int(tri_vids[2])

            closest = pom.point
            closest_xyz = (float(closest.x), float(closest.y), float(closest.z))
            wa, wb, wc = _compute_barycentric_weights(
                closest_xyz, orig_points[v0], orig_points[v1], orig_points[v2]
            )

            # 元頂点とほぼ一致していない新規・移動頂点を記録する
            dx = pt.x - closest_xyz[0]
            dy = pt.y - closest_xyz[1]
            dz = pt.z - closest_xyz[2]
            dist_sq = dx * dx + dy * dy + dz * dz
            is_exact_orig = (dist_sq < 1e-8) and (max(wa, wb, wc) > 0.9999)
            if not is_exact_orig:
                changed_vids.append(vid)

            merged = {}
            for src_vid, coeff in ((v0, wa), (v1, wb), (v2, wc)):
                if coeff <= WEIGHT_EPSILON or src_vid >= len(orig_weights):
                    continue
                for old_inf_idx, orig_w in orig_weights[src_vid]:
                    new_inf_idx = old_to_new_inf_map.get(old_inf_idx)
                    if new_inf_idx is not None:
                        merged[new_inf_idx] = merged.get(new_inf_idx, 0.0) + orig_w * coeff

            restored_per_vtx.append(_normalize_and_limit_weights(merged, maintain_max, max_infs))

        return restored_per_vtx, changed_vids
    finally:
        if temp_xform and cmds.objExists(temp_xform):
            try:
                cmds.delete(temp_xform)
            except Exception:
                pass
        cmds.undoInfo(stateWithoutFlush=prev_undo)


def finish_modeling_and_restore(
    mesh_long,
    payload,
    restore_mode="auto",
    smooth_iters=0,
    clean_orphaned_bind_pose=True,
):
    """修正後メッシュをフリーズ・ヒストリ削除・再バインドし、JSON からウェイトを復元する。"""
    if not cmds.objExists(mesh_long):
        raise RuntimeError("Target mesh does not exist: {0}".format(mesh_long))

    # 既存スキンクラスタが残っていれば先に解除してから綺麗にフリーズ・ヒストリ削除する
    _detach_and_freeze_for_modeling(mesh_long)

    shape_dag = _get_visible_mesh_dag(mesh_long)
    if shape_dag is None:
        raise RuntimeError("No visible mesh shape found: {0}".format(mesh_long))

    mesh_fn = om2.MFnMesh(shape_dag)
    new_vtx_count = mesh_fn.numVertices
    if new_vtx_count <= 0:
        raise RuntimeError("Mesh has zero vertices: {0}".format(mesh_long))

    saved_infs = payload.get("influences") or []
    resolved_infs, missing_infs = _resolve_existing_influences(saved_infs)
    if not resolved_infs:
        raise RuntimeError("None of the recorded influences exist in the scene.")

    # ボーン位置変更後に古い孤立 bindPose が残っていると不整合になるため整理する
    cleaned_poses = 0
    if clean_orphaned_bind_pose:
        cleaned_poses = len(rig_cleanup_helper.cleanup_orphaned_bind_poses(resolved_infs))

    moved_joints = rig_cleanup_helper.count_moved_joints(payload.get("joint_snapshot"))

    skin_params = payload.get("skin_params") or {}
    maintain_max = bool(skin_params.get("maintainMaxInfluences", False))
    max_infs = int(skin_params.get("maxInfluences", 4))
    dropoff_rate = float(skin_params.get("dropoffRate", 4.0))
    skinning_method = int(skin_params.get("skinningMethod", 0))
    normalize_weights = int(skin_params.get("normalizeWeights", 1))

    # 子ノードを一時退避して安全に再バインドする
    dummy = common.TemporaryReparent().main(mode="create")
    try:
        common.TemporaryReparent().main(mesh_long, dummyParent=dummy, mode="cut")
        skin_clusters = cmds.skinCluster(
            resolved_infs,
            mesh_long,
            toSelectedBones=True,
            obeyMaxInfluences=maintain_max,
            maximumInfluences=max_infs,
            dropoffRate=dropoff_rate,
            skinMethod=skinning_method,
            normalizeWeights=normalize_weights,
        )
        skin_cluster = skin_clusters[0]
    finally:
        try:
            common.TemporaryReparent().main(mesh_long, dummyParent=dummy, mode="parent")
        except Exception:
            pass
        try:
            common.TemporaryReparent().main(dummyParent=dummy, mode="delete")
        except Exception:
            pass

    # 再バインド後の実インフルエンス順序と JSON 側のインフルエンス番号を対応付ける
    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    new_inf_dags = skin_fn.influenceObjects()
    new_inf_count = len(new_inf_dags)
    new_inf_by_long = {}
    new_inf_by_short = {}
    for idx in range(new_inf_count):
        full_p = new_inf_dags[idx].fullPathName()
        new_inf_by_long[full_p] = idx
        new_inf_by_short[full_p.split("|")[-1]] = idx

    old_to_new_inf_map = {}
    for old_idx, saved_inf in enumerate(saved_infs):
        if saved_inf in new_inf_by_long:
            old_to_new_inf_map[old_idx] = new_inf_by_long[saved_inf]
        else:
            short_n = saved_inf.split("|")[-1]
            if short_n in new_inf_by_short:
                old_to_new_inf_map[old_idx] = new_inf_by_short[short_n]

    orig_vtx_count = int(payload.get("vertex_count", 0))
    use_index_mode = (
        (restore_mode == "index" and new_vtx_count == orig_vtx_count)
        or (restore_mode == "auto" and new_vtx_count == orig_vtx_count)
    )

    changed_vids = []
    if use_index_mode:
        used_method = "index"
        orig_weights = payload["weights"]
        restored_per_vtx = []
        for vid in range(new_vtx_count):
            merged = {}
            for old_inf_idx, w in orig_weights[vid]:
                new_idx = old_to_new_inf_map.get(old_inf_idx)
                if new_idx is not None:
                    merged[new_idx] = merged.get(new_idx, 0.0) + w
            restored_per_vtx.append(_normalize_and_limit_weights(merged, maintain_max, max_infs))
    else:
        used_method = "surface"
        shape_dag = _get_visible_mesh_dag(mesh_long)
        new_mesh_fn = om2.MFnMesh(shape_dag)
        new_points = new_mesh_fn.getPoints(om2.MSpace.kWorld)
        restored_per_vtx, changed_vids = _compute_restored_weights_by_surface(
            new_points, payload, old_to_new_inf_map, maintain_max, max_infs
        )

    # OpenMaya API 2.0 で全頂点のウェイトを一括設定する
    shape_dag = _get_visible_mesh_dag(mesh_long)
    inf_indices = om2.MIntArray(list(range(new_inf_count)))
    comp_fn = om2.MFnSingleIndexedComponent()
    vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
    comp_fn.setCompleteData(new_vtx_count)

    flat_weights = om2.MDoubleArray(new_vtx_count * new_inf_count, 0.0)
    for vid, w_dict in enumerate(restored_per_vtx):
        base = vid * new_inf_count
        if not w_dict:
            flat_weights[base] = 1.0
        else:
            for inf_idx, w_val in w_dict.items():
                flat_weights[base + inf_idx] = w_val

    rig_cleanup_helper._set_weights_without_norm_warning(
        skin_fn, shape_dag, vtx_comp, inf_indices, flat_weights
    )

    # 新規・移動頂点への任意スムース適用（トポロジ変更時のみ）
    if smooth_iters > 0 and changed_vids:
        try:
            prev_sel = cmds.ls(sl=True, l=True) or []
            vtx_names = ["{0}.vtx[{1}]".format(mesh_long, vid) for vid in changed_vids]
            cmds.select(vtx_names, replace=True)
            cmds.skinCluster(skin_cluster, e=True, smoothWeights=0.0, smoothWeightsMaxIterations=int(smooth_iters))
            cmds.skinCluster(skin_cluster, e=True, forceNormalizeWeights=True)
            if maintain_max:
                cmds.setAttr(skin_cluster + ".maintainMaxInfluences", True)
            if prev_sel:
                cmds.select(prev_sel, replace=True)
            else:
                cmds.select(mesh_long, replace=True)
        except Exception as exc:
            print("ModelEdit smooth warning:", exc)

    # Viewport 2.0 の描画・ピッキングキャッシュを同期する
    try:
        omr.MRenderer.setGeometryDrawDirty(shape_dag.node())
        cmds.dgdirty(shape_dag.fullPathName())
    except Exception:
        pass

    return {
        "skin_cluster": skin_cluster,
        "orig_vtx_count": orig_vtx_count,
        "new_vtx_count": new_vtx_count,
        "method": used_method,
        "changed_count": len(changed_vids),
        "missing_infs": missing_infs,
        "moved_joints": moved_joints,
        "cleaned_poses": cleaned_poses,
    }


class ModelEditLogWindow(qt.SubWindow):
    """モデリング・ボーン修正およびクリーンアップの詳細履歴を表示する専用ログウィンドウ。"""

    WINDOW_TITLE = "Model Edit Log - SIWeightEditor Improve"
    WINDOW_OBJECT_NAME = "SIWeightEditorImproveModelEditLogWindow"

    def __init__(self, owner_window):
        super(ModelEditLogWindow, self).__init__(parent=qt.get_maya_window())
        self.owner = owner_window
        self.setWindowTitle(
            lang.Lang(
                en="Model Edit Log - SIWeightEditor Improve",
                ja=u"モデル修正ログ - SIWeightEditor Improve",
            ).output()
        )
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(620, 380)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.log_text = QPlainTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_text.setStyleSheet(
            "QPlainTextEdit { background-color: #24262b; color: #dcdcdc; border: 1px solid #3c4048; }"
        )
        layout.addWidget(self.log_text, 1)

        btn_row = QHBoxLayout()
        self.clear_but = QPushButton(lang.Lang(en="Clear Log", ja=u"ログをクリア").output())
        self.clear_but.clicked.connect(self._on_clear_clicked)
        btn_row.addWidget(self.clear_but)

        btn_row.addStretch(1)

        self.close_but = QPushButton(lang.Lang(en="Close", ja=u"閉じる").output())
        self.close_but.clicked.connect(self.close)
        btn_row.addWidget(self.close_but)
        layout.addLayout(btn_row)

    def set_entries(self, entries):
        self.log_text.setPlainText("\n\n".join(entries))
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def append_entry(self, entry_text):
        if self.log_text.toPlainText():
            self.log_text.appendPlainText("")
        self.log_text.appendPlainText(entry_text)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_clear_clicked(self):
        self.log_text.clear()
        if self.owner is not None:
            self.owner.clear_log_entries()

    def closeEvent(self, event):
        if self.owner is not None and getattr(self.owner, "log_window", None) is self:
            self.owner.log_window = None
        super(ModelEditLogWindow, self).closeEvent(event)


class ModelEditWindow(qt.SubWindow):
    """スキニング済みメッシュ＆ボーン修正・ジョイント整理・クリーンアップ専用ウィンドウ。"""

    WINDOW_TITLE = "Model & Bone Edit / Cleanup - SIWeightEditor Improve"
    WINDOW_OBJECT_NAME = "SIWeightEditorImproveModelEditWindow"

    def __init__(self, editor_window):
        super(ModelEditWindow, self).__init__(parent=qt.get_maya_window())
        self.editor = editor_window
        self.setWindowTitle(
            lang.Lang(
                en="Model & Bone Edit / Cleanup - SIWeightEditor Improve",
                ja=u"モデル＆ボーン修正 / クリーンアップ - SIWeightEditor Improve",
            ).output()
        )
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(650, 645)

        self._records = []
        self._joint_issues = []
        self._log_entries = []
        self._saved_frame = None
        self._cached_joints = _load_last_seed_joints()
        self._ref_cur_records = []
        self._ref_cur_meshes = []
        self._ref_source_records = []
        self._ref_mapping_data = None
        self._quick_axis_ref = None
        self._weight_copy_src_mesh = None
        self._radius_syncing = False
        self._radius_undo_open = False
        self._radius_script_job_id = None
        self.log_window = None
        self.unweighted_bind_window = None
        self._init_ui()
        self._update_cached_joint_ui()
        self.reload_record_list()
        self._install_radius_selection_job()
        self._sync_radius_from_selection()

    def _init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        self.tabs = QTabWidget(self)
        self._tab_order = []
        self._tab_entries = {}
        self._tab_visibility = dict(TAB_VISIBILITY)

        tab_defs = [
            (
                "model_bone",
                self._build_model_bone_tab(),
                lang.Lang(en="Model / Bone Edit", ja=u"モデル / ボーン修正").output(),
            ),
            (
                "bind_mirror",
                self._build_bind_mirror_tab(),
                lang.Lang(en="Bind / Mirror & Copy", ja=u"バインド / ミラー＆コピー").output(),
            ),
            (
                "joint_orient",
                self._build_joint_orient_tab(),
                lang.Lang(en="Joint Cleanup / Orient", ja=u"ジョイント整理 / Orient").output(),
            ),
            (
                "joint_inspect",
                self._build_joint_inspect_tab(),
                lang.Lang(en="Joint Inspect / Adjust", ja=u"ジョイント診断 / 調整").output(),
            ),
            (
                "ref_joint",
                self._build_reference_joint_tab(),
                lang.Lang(en="Ref Joint Overwrite", ja=u"リファレンス軸上書き").output(),
            ),
            (
                "cleanup",
                self._build_cleanup_tab(),
                lang.Lang(en="Mesh / Scene Cleanup", ja=u"メッシュ / シーンクリーンアップ").output(),
            ),
        ]
        for tab_key, widget, label in tab_defs:
            self._tab_order.append(tab_key)
            self._tab_entries[tab_key] = {"widget": widget, "label": label}
            self.tabs.addTab(widget, label)

        self.apply_tab_visibility(self._tab_visibility)
        main_layout.addWidget(self.tabs, 1)

        # 全タブ共通の 1 行ステータス欄と詳細ログウィンドウボタン
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(False)
        self.status_label.setFixedHeight(26)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        qt.change_button_color(self.status_label, textColor=[150, 230, 160], bgColor=[42, 44, 48])
        self.status_label.setMargin(4)
        status_row.addWidget(self.status_label, 1)

        self.open_log_but = QPushButton(lang.Lang(en="Log (0)", ja=u"ログ表示 (0)").output())
        self.open_log_but.setFixedHeight(26)
        self.open_log_but.setMinimumWidth(96)
        self.open_log_but.clicked.connect(self.open_log_window)
        status_row.addWidget(self.open_log_but, 0)
        main_layout.addLayout(status_row)

    # ------------------------------------------------------------------
    # タブ構築：モデル / ボーン修正（ウェイト保持）
    # ------------------------------------------------------------------
    def _build_model_bone_tab(self):
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        guide_label = QLabel(
            lang.Lang(
                en="Safely edit skinned meshes (verts/edges) AND bone positions while preserving weights via JSON.",
                ja=u"メッシュ（頂点移動・エッジ追加/削除）だけでなくボーン位置の変更にも対応します。\nJSON へ記録・解除してから自由にモデルやジョイントを修正し、完了後に新ボーン位置で再バインド＆ウェイト復元を行います。",
            ).output()
        )
        guide_label.setWordWrap(True)
        qt.change_button_color(guide_label, textColor=[220, 220, 220], bgColor=[50, 52, 58])
        guide_label.setMargin(6)
        layout.addWidget(guide_label)

        # ステップ：修正開始
        step1_box = QGroupBox(
            lang.Lang(
                en="Step 1 : Start Editing (Record JSON -> Freeze & Unbind)",
                ja=u"ステップ 1：修正開始（JSON 記録 → フリーズ・ヒストリ削除・バインド解除）",
            ).output()
        )
        step1_layout = QVBoxLayout(step1_box)
        step1_layout.setSpacing(4)

        frame_row = QHBoxLayout()
        frame_row.setContentsMargins(0, 0, 0, 0)
        frame_row.setSpacing(6)

        self.restore_frame_chk = QCheckBox(
            lang.Lang(
                en="Jump to edit frame on start & restore original frame after finish:",
                ja=u"修正開始時に指定フレームへ移動し、完了後に元のフレームへ戻す:",
            ).output()
        )
        self.restore_frame_chk.setChecked(True)
        frame_row.addWidget(self.restore_frame_chk)

        self.edit_frame_spin = QSpinBox(self)
        self.edit_frame_spin.setRange(-100000, 100000)
        self.edit_frame_spin.setValue(0)
        self.edit_frame_spin.setMaximumWidth(72)
        self.edit_frame_spin.setToolTip(
            lang.Lang(
                en="Frame used while editing mesh/bones (default: 0)",
                ja=u"モデリング・ボーン修正時に移動する基準フレーム（デフォルト: 0）",
            ).output()
        )
        self.restore_frame_chk.toggled.connect(self.edit_frame_spin.setEnabled)
        frame_row.addWidget(self.edit_frame_spin)
        frame_row.addWidget(QLabel(lang.Lang(en="f", ja=u"フレーム").output()))
        frame_row.addStretch(1)
        step1_layout.addLayout(frame_row)

        self.bind_pose_chk = QCheckBox(
            lang.Lang(
                en="Go to bind pose before recording weights (Recommended)",
                ja=u"記録前にバインドポーズへ戻す（推奨）",
            ).output()
        )
        self.bind_pose_chk.setChecked(True)
        step1_layout.addWidget(self.bind_pose_chk)

        self.include_shared_meshes_chk = QCheckBox(
            lang.Lang(
                en="Include all meshes sharing the same skeleton (Best when moving bones)",
                ja=u"同じスケルトンを共有する全スキニングメッシュも一括記録・解除（ボーン位置変更時は推奨）",
            ).output()
        )
        self.include_shared_meshes_chk.setChecked(True)
        step1_layout.addWidget(self.include_shared_meshes_chk)

        # 記憶済みジョイント表示行（2 回目以降は未選択でも修正開始可能）
        cached_row = QHBoxLayout()
        cached_row.setContentsMargins(0, 0, 0, 0)
        cached_row.setSpacing(6)

        self.cached_joint_label = QLabel("")
        self.cached_joint_label.setWordWrap(False)
        self.cached_joint_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.cached_joint_label.setFixedHeight(22)
        qt.change_button_color(self.cached_joint_label, textColor=[180, 215, 245], bgColor=[44, 48, 56])
        self.cached_joint_label.setMargin(3)
        cached_row.addWidget(self.cached_joint_label, 1)

        self.clear_cached_joint_but = QPushButton(lang.Lang(en="Clear", ja=u"クリア").output())
        self.clear_cached_joint_but.setFixedHeight(22)
        self.clear_cached_joint_but.setMaximumWidth(64)
        self.clear_cached_joint_but.setToolTip(
            lang.Lang(
                en="Clear temporarily saved joint memory",
                ja=u"一時保存しているジョイント記憶をクリアします",
            ).output()
        )
        self.clear_cached_joint_but.clicked.connect(self.clear_cached_joint_memory)
        cached_row.addWidget(self.clear_cached_joint_but, 0)
        step1_layout.addLayout(cached_row)

        self.start_edit_but = QPushButton(
            lang.Lang(
                en="Record Weights & Start Editing (Selected Mesh / Joint, or Saved Joint)",
                ja=u"選択メッシュ / ジョイント（または記憶ジョイント）で修正開始",
            ).output()
        )
        self.start_edit_but.setMinimumHeight(30)
        self.start_edit_but.clicked.connect(self.start_modeling_selected)
        step1_layout.addWidget(self.start_edit_but)
        layout.addWidget(step1_box)

        # 記録済みバックアップ一覧
        list_box = QGroupBox(
            lang.Lang(en="Recorded Weight Backups (JSON) - Multi-select supported",
                      ja=u"記録済みバックアップ一覧（JSON / 複数選択で一括復元に対応）").output()
        )
        list_layout = QVBoxLayout(list_box)
        list_layout.setSpacing(4)

        self.record_table = QTableWidget(0, 5, self)
        self.record_table.setHorizontalHeaderLabels([
            lang.Lang(en="Mesh", ja=u"メッシュ").output(),
            lang.Lang(en="Status", ja=u"状態").output(),
            lang.Lang(en="Verts", ja=u"元頂点数").output(),
            lang.Lang(en="Bones", ja=u"ボーン数").output(),
            lang.Lang(en="Saved At", ja=u"記録日時").output(),
        ])
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.record_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.record_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.record_table.setAlternatingRowColors(True)
        self.record_table.verticalHeader().setVisible(False)
        self.record_table.verticalHeader().setDefaultSectionSize(22)

        header = self.record_table.horizontalHeader()
        resize_fn = getattr(header, "setSectionResizeMode", None) or getattr(header, "setResizeMode")
        resize_fn(0, QHeaderView.Stretch)
        resize_fn(1, QHeaderView.ResizeToContents)
        resize_fn(2, QHeaderView.ResizeToContents)
        resize_fn(3, QHeaderView.ResizeToContents)
        resize_fn(4, QHeaderView.ResizeToContents)
        list_layout.addWidget(self.record_table, 1)

        list_btn_row = QHBoxLayout()
        self.reload_list_but = QPushButton(lang.Lang(en="Refresh", ja=u"一覧更新").output())
        self.reload_list_but.clicked.connect(self.reload_record_list)
        list_btn_row.addWidget(self.reload_list_but)

        self.select_editing_but = QPushButton(lang.Lang(en="Select Editing", ja=u"修正中を全選択").output())
        self.select_editing_but.clicked.connect(self.select_all_editing_records)
        list_btn_row.addWidget(self.select_editing_but)

        self.select_mesh_but = QPushButton(lang.Lang(en="Select Mesh", ja=u"対象メッシュ選択").output())
        self.select_mesh_but.clicked.connect(self.select_recorded_mesh)
        list_btn_row.addWidget(self.select_mesh_but)

        self.open_folder_but = QPushButton(lang.Lang(en="Open JSON Folder", ja=u"JSONフォルダを開く").output())
        self.open_folder_but.clicked.connect(self.open_json_folder)
        list_btn_row.addWidget(self.open_folder_but)

        list_btn_row.addStretch(1)

        self.delete_record_but = QPushButton(lang.Lang(en="Delete JSON", ja=u"選択 JSON 削除").output())
        self.delete_record_but.clicked.connect(self.delete_selected_record)
        list_btn_row.addWidget(self.delete_record_but)
        list_layout.addLayout(list_btn_row)

        layout.addWidget(list_box, 1)

        # ステップ：修正完了
        step2_box = QGroupBox(
            lang.Lang(
                en="Step 2 : Finish Editing (Clean bindPose -> Freeze -> Rebind -> Restore)",
                ja=u"ステップ 2：修正完了（古い bindPose 整理 → フリーズ → 再バインド → ウェイト復元）",
            ).output()
        )
        step2_layout = QVBoxLayout(step2_box)
        step2_layout.setSpacing(6)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(6)
        opt_row.addWidget(QLabel(lang.Lang(en="Restore:", ja=u"復元方式:").output()))
        self.restore_mode_combo = QComboBox(self)
        self.restore_mode_combo.addItem(
            lang.Lang(en="Auto (Index if same count / Surface if changed)",
                      ja=u"自動（頂点数一致はインデックス / 変更時はサーフェス補間）").output(),
            "auto",
        )
        self.restore_mode_combo.addItem(
            lang.Lang(en="Surface Barycentric (Best for add/delete edges)",
                      ja=u"サーフェス重心補間（エッジ追加・削除・移動向け）").output(),
            "surface",
        )
        self.restore_mode_combo.addItem(
            lang.Lang(en="Vertex Index Only (Same topology)",
                      ja=u"インデックス優先（頂点数が同じ場合）").output(),
            "index",
        )
        opt_row.addWidget(self.restore_mode_combo, 1)

        opt_row.addWidget(QLabel(lang.Lang(en="Smooth New:", ja=u"追加頂点スムース:").output()))
        self.smooth_spin = QSpinBox(self)
        self.smooth_spin.setRange(0, 5)
        self.smooth_spin.setValue(0)
        opt_row.addWidget(self.smooth_spin)
        step2_layout.addLayout(opt_row)

        self.clean_bind_pose_chk = QCheckBox(
            lang.Lang(
                en="Clean orphaned bindPose before rebinding (Recommended after moving bones)",
                ja=u"再バインド前に孤立した古い bindPose を整理する（ボーン位置変更時は推奨）",
            ).output()
        )
        self.clean_bind_pose_chk.setChecked(True)
        step2_layout.addWidget(self.clean_bind_pose_chk)

        finish_btn_row = QHBoxLayout()
        finish_btn_row.setSpacing(6)

        self.finish_edit_but = QPushButton(
            lang.Lang(
                en="Finish & Restore All Recorded Meshes",
                ja=u"修正完了（全メッシュを再バインド＆復元）",
            ).output()
        )
        self.finish_edit_but.setMinimumHeight(30)
        self.finish_edit_but.clicked.connect(lambda: self.finish_modeling_selected(use_viewport_mesh=False))
        finish_btn_row.addWidget(self.finish_edit_but, 2)

        self.apply_to_sel_but = QPushButton(
            lang.Lang(en="Restore to Viewport Mesh", ja=u"選択メッシュへ適用").output()
        )
        self.apply_to_sel_but.setMinimumHeight(30)
        self.apply_to_sel_but.clicked.connect(lambda: self.finish_modeling_selected(use_viewport_mesh=True))
        finish_btn_row.addWidget(self.apply_to_sel_but, 1)
        step2_layout.addLayout(finish_btn_row)

        layout.addWidget(step2_box)
        return tab

    # ------------------------------------------------------------------
    # タブ構築：バインド / 高精度ウェイトミラー＆コピー
    # ------------------------------------------------------------------
    def _build_bind_mirror_tab(self):
        outer = QWidget(self)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        scroll.setWidget(tab)
        outer_layout.addWidget(scroll)

        # ボーンの安全バインド（既存ウェイト維持）
        bind_box = QGroupBox(
            lang.Lang(
                en="Safe Bind Skin for Selected Joints (Preserves Existing Weights)",
                ja=u"ボーンの安全バインド（既存ウェイトを維持して選択ジョイント・子をバインド）",
            ).output()
        )
        bind_layout = QVBoxLayout(bind_box)
        bind_layout.setContentsMargins(8, 6, 8, 6)
        bind_layout.setSpacing(5)

        bind_mode_row = QHBoxLayout()
        bind_mode_row.setSpacing(6)
        bind_mode_row.addWidget(
            QLabel(lang.Lang(en="Weight Mode:", ja=u"ウェイト割当:").output())
        )
        self.safe_bind_mode_combo = QComboBox(self)
        self.safe_bind_mode_combo.addItem(
            lang.Lang(
                en="Mirror from Opposite Bone (e.g. Left -> Right, keeps existing)",
                ja=u"反対側ボーンからミラー転写（例: Left → Right・既存ウェイト保護）",
            ).output(),
            "mirror_opposite",
        )
        self.safe_bind_mode_combo.addItem(
            lang.Lang(
                en="Auto-Bind Nearby Vertices (blends new bone, keeps existing)",
                ja=u"近傍の自動バインドで初期ウェイト割当（既存ウェイト保護）",
            ).output(),
            "auto_bind_new",
        )
        self.safe_bind_mode_combo.addItem(
            lang.Lang(
                en="Add Influence Only (weight = 0.0, untouched existing)",
                ja=u"インフルエンス追加のみ（ウェイト 0・既存ウェイト維持）",
            ).output(),
            "add_zero_only",
        )
        self.safe_bind_mode_combo.setToolTip(
            lang.Lang(
                en="Select target joint(s) only (auto-detects shared skinned mesh) or select joints + mesh together.\nExisting weights on other bones/sides remain untouched.",
                ja=u"ジョイントだけ選択すれば同一スケルトンのスキニング済みメッシュを自動検出します（メッシュ同時選択も可）。\n他のボーンや反対側の既存ウェイトを壊さずに安全にバインドします。",
            ).output()
        )
        bind_mode_row.addWidget(self.safe_bind_mode_combo, 1)
        bind_layout.addLayout(bind_mode_row)

        bind_opt_row = QHBoxLayout()
        bind_opt_row.setSpacing(10)
        self.safe_bind_children_chk = QCheckBox(
            lang.Lang(en="Include child joints", ja=u"選択ジョイントの子階層も含める").output()
        )
        self.safe_bind_children_chk.setChecked(True)
        bind_opt_row.addWidget(self.safe_bind_children_chk)

        self.safe_bind_unlock_chk = QCheckBox(
            lang.Lang(
                en="Unlock added influences (ready to adjust)",
                ja=u"追加後にインフルエンスロックを解除",
            ).output()
        )
        self.safe_bind_unlock_chk.setChecked(True)
        bind_opt_row.addWidget(self.safe_bind_unlock_chk)
        bind_opt_row.addStretch(1)
        bind_layout.addLayout(bind_opt_row)

        bind_btn_row = QHBoxLayout()
        bind_btn_row.setSpacing(6)
        self.exec_safe_bind_but = QPushButton(
            lang.Lang(
                en="Safe Bind Selected Joint(s) to Skin (Keep Existing Weights)",
                ja=u"選択ジョイントを安全にバインド（既存ウェイト維持）",
            ).output()
        )
        self.exec_safe_bind_but.setMinimumHeight(28)
        self.exec_safe_bind_but.setToolTip(
            lang.Lang(
                en="Run at bind pose (frame 0) for cleanest deformation. Supports 1-click Undo (Ctrl+Z).",
                ja=u"バインドポーズ（基準フレーム）での実行を推奨します。Ctrl+Z 1 回で元に戻せます。",
            ).output()
        )
        self.exec_safe_bind_but.clicked.connect(self.run_safe_bind_selected_joints)
        bind_btn_row.addWidget(self.exec_safe_bind_but, 2)

        self.open_unweighted_bind_but = QPushButton(
            lang.Lang(
                en="Bind Unweighted Meshes...",
                ja=u"ウェイト無しメッシュ一覧バインド...",
            ).output()
        )
        self.open_unweighted_bind_but.setMinimumHeight(28)
        self.open_unweighted_bind_but.setToolTip(
            lang.Lang(
                en="Open a list window showing meshes with no weights and destination bones so you can pick and bind them at a glance.",
                ja=u"ウェイトが 1 つも入っていないメッシュと割り当て先ボーンを一覧ウィンドウで選択・確認してバインドできます。",
            ).output()
        )
        qt.change_button_color(
            self.open_unweighted_bind_but,
            textColor=[235, 230, 255],
            bgColor=[66, 56, 86],
            hiColor=[114, 92, 160],
            mode="button",
            hover=True,
            destroy=True,
            dsColor=[152, 134, 198],
        )
        self.open_unweighted_bind_but.clicked.connect(self.open_unweighted_bind_window)
        bind_btn_row.addWidget(self.open_unweighted_bind_but, 1)
        bind_layout.addLayout(bind_btn_row)
        layout.addWidget(bind_box)

        # 高精度スキンウェイト・ミラー（ボーン / メッシュ / 頂点すべて対応）
        mirror_box = QGroupBox(
            lang.Lang(
                en="Precision Skin Weight Mirror (Works with Bones / Meshes / Vertices)",
                ja=u"高精度スキンウェイト・ミラー（ボーン / メッシュ / 頂点すべて対応）",
            ).output()
        )
        mirror_layout = QVBoxLayout(mirror_box)
        mirror_layout.setContentsMargins(8, 6, 8, 6)
        mirror_layout.setSpacing(5)

        mirror_guide = QLabel(
            lang.Lang(
                en="Works with Bones (auto-detects mesh & weighted side), 1 Mesh (whole mirror), 2+ Meshes (1st -> 2nd mirror), or Vertices only.",
                ja=u"ボーン選択（共有メッシュ＆ウェイト側を自動判定）・1 メッシュ全体・2 メッシュ間（1番目→2番目）・選択頂点のみのどれでも動作します。",
            ).output()
        )
        mirror_guide.setWordWrap(True)
        qt.change_button_color(mirror_guide, textColor=[215, 222, 232], bgColor=[48, 50, 56])
        mirror_guide.setMargin(5)
        mirror_layout.addWidget(mirror_guide)

        mir_dir_row = QHBoxLayout()
        mir_dir_row.setSpacing(6)
        mir_dir_row.addWidget(QLabel(lang.Lang(en="Direction:", ja=u"ミラー方向:").output()))
        self.mirror_dir_combo = QComboBox(self)
        self.mirror_dir_combo.addItem(
            lang.Lang(
                en="Auto (From weighted bone / +X -> -X for mesh)",
                ja=u"自動判定（ボーン選択時はウェイト側から自動 / メッシュは +X → -X）",
            ).output(),
            "auto",
        )
        self.mirror_dir_combo.addItem(
            lang.Lang(en="+X -> -X (Character Left -> Right)", ja=u"+X → -X（キャラ左 → 右）").output(),
            "pos_to_neg",
        )
        self.mirror_dir_combo.addItem(
            lang.Lang(en="-X -> +X (Character Right -> Left)", ja=u"-X → +X（キャラ右 → 左）").output(),
            "neg_to_pos",
        )
        mir_dir_row.addWidget(self.mirror_dir_combo, 2)

        mir_dir_row.addWidget(QLabel(lang.Lang(en="Plane:", ja=u"対称面:").output()))
        self.mirror_plane_combo = QComboBox(self)
        self.mirror_plane_combo.addItem("YZ (X)", "YZ")
        self.mirror_plane_combo.addItem("XZ (Y)", "XZ")
        self.mirror_plane_combo.addItem("XY (Z)", "XY")
        self.mirror_plane_combo.setMaximumWidth(86)
        mir_dir_row.addWidget(self.mirror_plane_combo, 0)
        mirror_layout.addLayout(mir_dir_row)

        mir_sample_row = QHBoxLayout()
        mir_sample_row.setSpacing(6)
        mir_sample_row.addWidget(QLabel(lang.Lang(en="Sampling:", ja=u"補間方式:").output()))
        self.mirror_sample_combo = QComboBox(self)
        self.mirror_sample_combo.addItem(
            lang.Lang(
                en="Auto (Exact vertex snap + Surface Barycentric for offsets)",
                ja=u"自動（一致頂点はそのまま / ズレはサーフェス重心補間）",
            ).output(),
            "auto",
        )
        self.mirror_sample_combo.addItem(
            lang.Lang(
                en="Surface Barycentric (Smooth across different topology)",
                ja=u"サーフェス重心補間（トポロジ違い・非対称向け）",
            ).output(),
            "surface",
        )
        self.mirror_sample_combo.addItem(
            lang.Lang(en="Nearest Vertex", ja=u"最寄り頂点").output(),
            "nearest",
        )
        mir_sample_row.addWidget(self.mirror_sample_combo, 1)
        mirror_layout.addLayout(mir_sample_row)

        mir_opt_grid = QGridLayout()
        mir_opt_grid.setSpacing(4)
        self.mirror_children_chk = QCheckBox(
            lang.Lang(en="Include child bones (on bone selection)", ja=u"子ボーンも含める（ボーン選択時）").output()
        )
        self.mirror_children_chk.setChecked(True)
        mir_opt_grid.addWidget(self.mirror_children_chk, 0, 0)

        self.mirror_auto_add_chk = QCheckBox(
            lang.Lang(en="Auto-add missing opposite bones", ja=u"未登録の反対側ボーンを自動追加").output()
        )
        self.mirror_auto_add_chk.setChecked(True)
        mir_opt_grid.addWidget(self.mirror_auto_add_chk, 0, 1)

        self.mirror_center_eq_chk = QCheckBox(
            lang.Lang(en="Equalize center seam weights", ja=u"中央シームの左右ウェイトを均等化").output()
        )
        self.mirror_center_eq_chk.setChecked(True)
        mir_opt_grid.addWidget(self.mirror_center_eq_chk, 1, 0)

        self.mirror_pos_fallback_chk = QCheckBox(
            lang.Lang(en="Position fallback matching", ja=u"位置フォールバック照合（命名外対応）").output()
        )
        self.mirror_pos_fallback_chk.setChecked(True)
        mir_opt_grid.addWidget(self.mirror_pos_fallback_chk, 1, 1)
        mirror_layout.addLayout(mir_opt_grid)

        mir_btn_row = QHBoxLayout()
        mir_btn_row.setSpacing(6)
        self.exec_precision_mirror_but = QPushButton(
            lang.Lang(en="Execute Precision Weight Mirror", ja=u"高精度ウェイトミラーを実行").output()
        )
        self.exec_precision_mirror_but.setMinimumHeight(30)
        self.exec_precision_mirror_but.clicked.connect(self.run_precision_weight_mirror)
        mir_btn_row.addWidget(self.exec_precision_mirror_but, 2)

        self.diag_mirror_but = QPushButton(
            lang.Lang(en="Check Pairs (Preview)", ja=u"対応ボーン診断").output()
        )
        self.diag_mirror_but.setMinimumHeight(30)
        self.diag_mirror_but.clicked.connect(self.run_diagnose_weight_mirror)
        mir_btn_row.addWidget(self.diag_mirror_but, 1)
        mirror_layout.addLayout(mir_btn_row)
        layout.addWidget(mirror_box)

        # 安全・高精度ウェイトコピー（別メッシュ / 頂点部分コピー / 左右反転コピー対応）
        copy_box = QGroupBox(
            lang.Lang(
                en="Safe & Accurate Weight Copy (Mesh -> Mesh / Vertices / Mirrored Part)",
                ja=u"安全・高精度ウェイトコピー（別メッシュ / 頂点部分 / 左右パーツ反転対応）",
            ).output()
        )
        copy_layout = QVBoxLayout(copy_box)
        copy_layout.setContentsMargins(8, 6, 8, 6)
        copy_layout.setSpacing(5)

        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        self.copy_src_label = QLabel(
            lang.Lang(
                en="Source Mesh: (Not set - or select Source -> Target meshes directly)",
                ja=u"コピー元: 未登録（または コピー元 → コピー先 の順に 2 メッシュ直接選択でも可）",
            ).output()
        )
        self.copy_src_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.copy_src_label.setFixedHeight(24)
        qt.change_button_color(self.copy_src_label, textColor=[200, 225, 245], bgColor=[42, 46, 54])
        self.copy_src_label.setMargin(4)
        src_row.addWidget(self.copy_src_label, 1)

        self.set_copy_src_but = QPushButton(
            lang.Lang(en="Set Source", ja=u"コピー元登録").output()
        )
        self.set_copy_src_but.setFixedHeight(24)
        self.set_copy_src_but.clicked.connect(self.register_weight_copy_source)
        src_row.addWidget(self.set_copy_src_but, 0)

        self.clear_copy_src_but = QPushButton(
            lang.Lang(en="Clear", ja=u"クリア").output()
        )
        self.clear_copy_src_but.setFixedHeight(24)
        self.clear_copy_src_but.setMaximumWidth(56)
        self.clear_copy_src_but.clicked.connect(self.clear_weight_copy_source)
        src_row.addWidget(self.clear_copy_src_but, 0)
        copy_layout.addLayout(src_row)

        copy_mode_row = QHBoxLayout()
        copy_mode_row.setSpacing(6)
        copy_mode_row.addWidget(QLabel(lang.Lang(en="Transfer:", ja=u"転送方式:").output()))
        self.copy_mode_combo = QComboBox(self)
        self.copy_mode_combo.addItem(
            lang.Lang(
                en="Auto (Index if matching / Surface Barycentric otherwise)",
                ja=u"自動（頂点一致はインデックス / 異なる場合はサーフェス重心補間）",
            ).output(),
            "auto",
        )
        self.copy_mode_combo.addItem(
            lang.Lang(
                en="Surface Barycentric (Best for clothing / different topology)",
                ja=u"サーフェス重心補間（衣装・別トポロジ向け）",
            ).output(),
            "surface",
        )
        self.copy_mode_combo.addItem(
            lang.Lang(en="Nearest Vertex", ja=u"最寄り頂点").output(),
            "nearest",
        )
        self.copy_mode_combo.addItem(
            lang.Lang(en="Vertex Index Only (Same topology)", ja=u"頂点インデックス（同一トポロジ専用）").output(),
            "index",
        )
        copy_mode_row.addWidget(self.copy_mode_combo, 1)
        copy_layout.addLayout(copy_mode_row)

        copy_opt_row = QHBoxLayout()
        copy_opt_row.setSpacing(10)
        self.copy_mirror_chk = QCheckBox(
            lang.Lang(en="Mirror L <-> R while copying", ja=u"左右反転（ミラー）してコピー").output()
        )
        self.copy_mirror_chk.setChecked(False)
        copy_opt_row.addWidget(self.copy_mirror_chk)

        self.copy_auto_bind_chk = QCheckBox(
            lang.Lang(en="Auto-bind / add missing bones", ja=u"未バインド自動バインド＆不足ボーン追加").output()
        )
        self.copy_auto_bind_chk.setChecked(True)
        copy_opt_row.addWidget(self.copy_auto_bind_chk)
        copy_opt_row.addStretch(1)
        copy_layout.addLayout(copy_opt_row)

        self.exec_safe_copy_but = QPushButton(
            lang.Lang(
                en="Copy Weights Safely to Selected Mesh(es) / Vertices",
                ja=u"選択メッシュ / 頂点へ安全にウェイトをコピー",
            ).output()
        )
        self.exec_safe_copy_but.setMinimumHeight(30)
        self.exec_safe_copy_but.clicked.connect(self.run_safe_weight_copy)
        copy_layout.addWidget(self.exec_safe_copy_but)
        layout.addWidget(copy_box)

        layout.addStretch(1)
        return outer

    # ------------------------------------------------------------------
    # タブ構築：ジョイント整理 ＆ Joint Orient
    # ------------------------------------------------------------------
    def _build_joint_orient_tab(self):
        outer = QWidget(self)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        scroll.setWidget(tab)
        outer_layout.addWidget(scroll)

        # 対象範囲設定
        scope_row = QHBoxLayout()
        self.jnt_include_children_chk = QCheckBox(
            lang.Lang(
                en="Include child joints in hierarchy (for Inspect / Clean / Local Axes)",
                ja=u"選択ジョイントの子階層も含めて対象にする（診断・値整理・ローカル軸表示）",
            ).output()
        )
        self.jnt_include_children_chk.setChecked(True)
        scope_row.addWidget(self.jnt_include_children_chk)
        layout.addLayout(scope_row)

        # ビューポート選択ジョイントの表示サイズ（Radius）リアルタイム調整
        radius_box = QGroupBox(
            lang.Lang(
                en="Selected Joint Radius (Real-Time)",
                ja=u"選択ジョイントの大きさ調整（Radius リアルタイム反映）",
            ).output()
        )
        radius_layout = QVBoxLayout(radius_box)
        radius_layout.setContentsMargins(8, 6, 8, 6)
        radius_layout.setSpacing(4)

        radius_row = QHBoxLayout()
        radius_row.setSpacing(6)
        radius_row.addWidget(QLabel("Radius:"))

        self.jnt_radius_slider = QSlider(Qt.Horizontal, self)
        self.jnt_radius_slider.setRange(1, 1000)
        self.jnt_radius_slider.setValue(100)
        self.jnt_radius_slider.setToolTip(
            lang.Lang(
                en="Drag to adjust selected joint Radius in real time (0.01 - 10.00)",
                ja=u"ドラッグして選択中ジョイントの Radius をリアルタイム調整（0.01 ～ 10.00）",
            ).output()
        )
        self.jnt_radius_slider.sliderPressed.connect(self._on_radius_slider_pressed)
        self.jnt_radius_slider.valueChanged.connect(self._on_radius_slider_changed)
        self.jnt_radius_slider.sliderReleased.connect(self._on_radius_slider_released)
        radius_row.addWidget(self.jnt_radius_slider, 1)

        self.jnt_radius_spin = QDoubleSpinBox(self)
        self.jnt_radius_spin.setRange(0.01, 1000.0)
        self.jnt_radius_spin.setDecimals(2)
        self.jnt_radius_spin.setSingleStep(0.1)
        self.jnt_radius_spin.setValue(1.0)
        self.jnt_radius_spin.setMinimumWidth(78)
        self.jnt_radius_spin.setToolTip(
            lang.Lang(
                en="Enter exact Radius value for selected joint(s) in real time",
                ja=u"選択中ジョイントの Radius 値を数値入力でリアルタイム反映",
            ).output()
        )
        self.jnt_radius_spin.valueChanged.connect(self._on_radius_spin_changed)
        radius_row.addWidget(self.jnt_radius_spin, 0)

        self.jnt_radius_children_chk = QCheckBox(
            lang.Lang(en="Children", ja=u"子も含める").output()
        )
        self.jnt_radius_children_chk.setChecked(False)
        self.jnt_radius_children_chk.setToolTip(
            lang.Lang(
                en="Also adjust Radius of child joints under the viewport selection",
                ja=u"チェックすると選択ジョイント配下の子ジョイントにも Radius を一括適用します",
            ).output()
        )
        radius_row.addWidget(self.jnt_radius_children_chk, 0)

        self.jnt_radius_sync_but = QPushButton(
            lang.Lang(en="Sync", ja=u"取得").output()
        )
        self.jnt_radius_sync_but.setMaximumWidth(56)
        self.jnt_radius_sync_but.setToolTip(
            lang.Lang(
                en="Read current Radius from selected joint(s)",
                ja=u"現在選択中ジョイントの Radius 値を読み込みます",
            ).output()
        )
        self.jnt_radius_sync_but.clicked.connect(lambda: self._sync_radius_from_selection(show_status=True))
        radius_row.addWidget(self.jnt_radius_sync_but, 0)
        radius_layout.addLayout(radius_row)

        self.jnt_radius_info_label = QLabel(
            lang.Lang(
                en="Viewport Selection: No joint selected",
                ja=u"ビューポート選択: ジョイント未選択",
            ).output()
        )
        self.jnt_radius_info_label.setStyleSheet("color: #aeb6c4; font-size: 11px;")
        radius_layout.addWidget(self.jnt_radius_info_label)
        layout.addWidget(radius_box)

        # 選択ボーン回転フリーズ（子ボーン含めキー退避＆書き戻し・0F=0 / 0F以降そのまま復元）
        freeze_rot_box = QGroupBox(
            lang.Lang(
                en="Freeze Selected Bone Rotation (Preserve Keyframes of Selected & Child Bones)",
                ja=u"選択ボーンの回転 XYZ フリーズ（子ボーン含めキー保存＆書き戻し）",
            ).output()
        )
        freeze_rot_layout = QVBoxLayout(freeze_rot_box)
        freeze_rot_layout.setContentsMargins(8, 6, 8, 6)
        freeze_rot_layout.setSpacing(5)

        freeze_rot_guide = QLabel(
            lang.Lang(
                en=(
                    "Bakes current Rotate XYZ of selected bone(s) into jointOrient and resets Rotate to (0, 0, 0).\n"
                    "Saves and restores keyframes of both selected and child bones (Base Frame -> 0, frames after Base Frame kept verbatim)."
                ),
                ja=(
                    u"選択ボーンの現在の回転 XYZ を jointOrient へ焼き込んで 0 へ戻します。\n"
                    u"選択ボーンと子ボーンのキーフレームを退避・書き戻し、基準フレーム（0F）は 0、0F 以降の値はそのまま復元します。"
                ),
            ).output()
        )
        freeze_rot_guide.setWordWrap(True)
        qt.change_button_color(freeze_rot_guide, textColor=[215, 222, 232], bgColor=[48, 50, 56])
        freeze_rot_guide.setMargin(5)
        freeze_rot_layout.addWidget(freeze_rot_guide)

        freeze_rot_opt_row = QHBoxLayout()
        freeze_rot_opt_row.setSpacing(8)
        freeze_rot_opt_row.addWidget(
            QLabel(lang.Lang(en="Base Frame (A-Pose):", ja=u"基準フレーム（Aポーズ）:").output())
        )
        self.freeze_rot_base_frame_spin = QSpinBox(self)
        self.freeze_rot_base_frame_spin.setRange(-100000, 100000)
        self.freeze_rot_base_frame_spin.setValue(0)
        self.freeze_rot_base_frame_spin.setMaximumWidth(72)
        self.freeze_rot_base_frame_spin.setToolTip(
            lang.Lang(
                en="Frame to freeze Rotate XYZ to (0, 0, 0). Keyframes on other frames are restored verbatim.",
                ja=u"回転 XYZ を (0, 0, 0) にフリーズする基準フレーム（デフォルト: 0）。これ以外のフレームのキー値はそのまま復元されます。",
            ).output()
        )
        freeze_rot_opt_row.addWidget(self.freeze_rot_base_frame_spin)

        self.freeze_rot_children_chk = QCheckBox(
            lang.Lang(
                en="Also freeze rotation of child bones (child keyframes are always preserved)",
                ja=u"子ボーンも回転フリーズ対象に含める（OFF でも子キーは常に保存＆復元）",
            ).output()
        )
        self.freeze_rot_children_chk.setChecked(False)
        freeze_rot_opt_row.addWidget(self.freeze_rot_children_chk)
        freeze_rot_opt_row.addStretch(1)
        freeze_rot_layout.addLayout(freeze_rot_opt_row)

        self.exec_freeze_rot_keep_keys_but = QPushButton(
            lang.Lang(
                en="Freeze Selected Bone Rotate -> 0 (Save & Restore Keys)",
                ja=u"選択ボーンの回転をフリーズして 0 に戻す（子含めキー保存＆書き戻し）",
            ).output()
        )
        self.exec_freeze_rot_keep_keys_but.setMinimumHeight(28)
        self.exec_freeze_rot_keep_keys_but.clicked.connect(self.run_freeze_selected_bone_rot_keep_keys)
        freeze_rot_layout.addWidget(self.exec_freeze_rot_keep_keys_but)
        layout.addWidget(freeze_rot_box)

        # Orient Joint Options（画像準拠）
        orient_box = QGroupBox(
            lang.Lang(en="Orient Joint Options", ja=u"Orient Joint 設定（ジョイントの方向付け）").output()
        )
        orient_layout = QVBoxLayout(orient_box)
        orient_layout.setSpacing(5)

        self.oj_world_chk = QCheckBox(
            lang.Lang(en="Orient Joint to World", ja=u"Orient Joint to World（ワールド軸に揃える）").output()
        )
        self.oj_world_chk.setChecked(False)
        self.oj_world_chk.toggled.connect(self._update_orient_controls_enabled)
        orient_layout.addWidget(self.oj_world_chk)

        axis_grid = QGridLayout()
        axis_grid.setSpacing(4)

        # Primary Axis
        axis_grid.addWidget(QLabel(lang.Lang(en="Primary Axis:", ja=u"主軸 (Primary Axis):").output()), 0, 0)
        self.primary_group = QButtonGroup(self)
        self.primary_radios = {}
        for col, ax in enumerate(("X", "Y", "Z"), start=1):
            rb = QRadioButton(ax)
            if ax == "X":
                rb.setChecked(True)
            rb.toggled.connect(self._on_primary_axis_changed)
            self.primary_group.addButton(rb)
            self.primary_radios[ax] = rb
            axis_grid.addWidget(rb, 0, col)

        # Secondary Axis
        axis_grid.addWidget(QLabel(lang.Lang(en="Secondary Axis:", ja=u"副軸 (Secondary Axis):").output()), 1, 0)
        self.secondary_group = QButtonGroup(self)
        self.secondary_radios = {}
        for col, ax in enumerate(("X", "Y", "Z", "None"), start=1):
            label_ax = lang.Lang(en="None", ja=u"なし").output() if ax == "None" else ax
            rb = QRadioButton(label_ax)
            if ax == "Y":
                rb.setChecked(True)
            rb.toggled.connect(self._on_secondary_axis_changed)
            self.secondary_group.addButton(rb)
            self.secondary_radios[ax] = rb
            axis_grid.addWidget(rb, 1, col)

        # Secondary Axis World Orientation
        axis_grid.addWidget(
            QLabel(
                lang.Lang(
                    en="Secondary Axis World Orientation:",
                    ja=u"副軸のワールド方向 (Secondary World):",
                ).output()
            ),
            2,
            0,
        )
        self.world_axis_group = QButtonGroup(self)
        self.world_axis_radios = {}
        for col, ax in enumerate(("X", "Y", "Z"), start=1):
            rb = QRadioButton(ax)
            if ax == "Z":
                rb.setChecked(True)
            self.world_axis_group.addButton(rb)
            self.world_axis_radios[ax] = rb
            axis_grid.addWidget(rb, 2, col)

        self.world_sign_combo = QComboBox(self)
        self.world_sign_combo.addItem("+")
        self.world_sign_combo.addItem("-")
        self.world_sign_combo.setCurrentIndex(1)
        self.world_sign_combo.setMaximumWidth(54)
        axis_grid.addWidget(self.world_sign_combo, 2, 4)

        self._orient_axis_widgets = (
            list(self.primary_radios.values())
            + list(self.secondary_radios.values())
            + list(self.world_axis_radios.values())
            + [self.world_sign_combo]
        )
        orient_layout.addLayout(axis_grid)

        self.oj_children_chk = QCheckBox(
            lang.Lang(
                en="Orient children of selected joints",
                ja=u"子ジョイントも方向付け (Orient children of selected joints)",
            ).output()
        )
        self.oj_children_chk.setChecked(True)
        orient_layout.addWidget(self.oj_children_chk)

        self.oj_scale_chk = QCheckBox(
            lang.Lang(
                en="Reorient the local scale axes",
                ja=u"ローカルスケール軸も再方向付け (Reorient local scale axes)",
            ).output()
        )
        self.oj_scale_chk.setChecked(True)
        orient_layout.addWidget(self.oj_scale_chk)

        self.oj_zero_end_chk = QCheckBox(
            lang.Lang(
                en="Zero jointOrient on end (leaf) joints to match parent",
                ja=u"末端（エンド）ジョイントの jointOrient を 0 に揃える（親の向きに一致）",
            ).output()
        )
        self.oj_zero_end_chk.setChecked(True)
        orient_layout.addWidget(self.oj_zero_end_chk)

        oj_btn_row = QHBoxLayout()
        self.exec_oj_but = QPushButton(
            lang.Lang(en="Apply Orient Joint", ja=u"Orient Joint を実行").output()
        )
        self.exec_oj_but.setMinimumHeight(28)
        self.exec_oj_but.clicked.connect(self.run_orient_joints)
        oj_btn_row.addWidget(self.exec_oj_but, 2)

        self.toggle_lra_but = QPushButton(
            lang.Lang(en="Toggle Local Axes Visibility", ja=u"ローカル軸表示を切替").output()
        )
        self.toggle_lra_but.setMinimumHeight(28)
        self.toggle_lra_but.clicked.connect(self.run_toggle_local_axes)
        oj_btn_row.addWidget(self.toggle_lra_but, 1)
        orient_layout.addLayout(oj_btn_row)
        layout.addWidget(orient_box)

        # ローカル軸の微調整（子位置を維持したまま ±90° / 180° 回転）
        tweak_box = QGroupBox(
            lang.Lang(
                en="Manual Local Axis Tweak (Keeps child world positions)",
                ja=u"軸の向き微調整（子ジョイントの位置を保ったまま Joint Orient を回転）",
            ).output()
        )
        tweak_grid = QGridLayout(tweak_box)
        tweak_grid.setSpacing(4)
        for row_idx, ax in enumerate(("X", "Y", "Z")):
            lbl = QLabel(lang.Lang(en="{0} Axis:", ja=u"{0} 軸:").output().format(ax))
            tweak_grid.addWidget(lbl, row_idx, 0)
            for col_idx, deg in enumerate((-90, 90, 180), start=1):
                sign_txt = "+{0}".format(deg) if deg > 0 else str(deg)
                btn = QPushButton(u"{0}°".format(sign_txt))
                btn.clicked.connect(lambda _=False, a=ax, d=deg: self.run_tweak_orient(a, d))
                tweak_grid.addWidget(btn, row_idx, col_idx)
        layout.addWidget(tweak_box)

        layout.addStretch(1)
        return outer

    # ------------------------------------------------------------------
    # タブ構築：ジョイント診断 ＆ 回転等調整（強制フリーズ一括）
    # ------------------------------------------------------------------
    def _build_joint_inspect_tab(self):
        outer = QWidget(self)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        scroll.setWidget(tab)
        outer_layout.addWidget(scroll)

        # 対象範囲設定（ジョイント整理タブ側のチェックボックスと相互同期）
        scope_row = QHBoxLayout()
        self.jnt_inspect_include_children_chk = QCheckBox(
            lang.Lang(
                en="Include child joints in hierarchy (for Inspect / Clean / Force Freeze)",
                ja=u"選択ジョイントの子階層も含めて対象にする（診断・値整理・強制フリーズ）",
            ).output()
        )
        self.jnt_inspect_include_children_chk.setChecked(self.jnt_include_children_chk.isChecked())
        self.jnt_inspect_include_children_chk.toggled.connect(self._sync_include_children_from_inspect)
        self.jnt_include_children_chk.toggled.connect(self._sync_include_children_from_orient)
        scope_row.addWidget(self.jnt_inspect_include_children_chk)
        layout.addLayout(scope_row)

        # ジョイント回転・スケール診断と整理
        norm_box = QGroupBox(
            lang.Lang(
                en="Joint Value Inspection & Cleanup (Rotate = 0 / Scale = 1)",
                ja=u"ジョイント回転・スケールの診断と整理（Rotate = 0 / Scale = 1）",
            ).output()
        )
        norm_layout = QVBoxLayout(norm_box)
        norm_layout.setSpacing(4)

        inspect_btn_row = QHBoxLayout()
        self.inspect_jnt_but = QPushButton(lang.Lang(en="Inspect Selected Joints", ja=u"ジョイント状態を診断").output())
        self.inspect_jnt_but.clicked.connect(self.inspect_selected_joints)
        inspect_btn_row.addWidget(self.inspect_jnt_but)

        self.select_issue_jnts_but = QPushButton(
            lang.Lang(en="Select Issue Joints", ja=u"問題ジョイントを選択").output()
        )
        self.select_issue_jnts_but.clicked.connect(self.select_joint_issues)
        inspect_btn_row.addWidget(self.select_issue_jnts_but)
        norm_layout.addLayout(inspect_btn_row)

        self.joint_issue_table = QTableWidget(0, 4, self)
        self.joint_issue_table.setHorizontalHeaderLabels([
            lang.Lang(en="Joint", ja=u"ジョイント").output(),
            lang.Lang(en="Issues", ja=u"検出内容").output(),
            lang.Lang(en="Rotate", ja=u"Rotate").output(),
            lang.Lang(en="Scale", ja=u"Scale").output(),
        ])
        self.joint_issue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.joint_issue_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.joint_issue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.joint_issue_table.verticalHeader().setVisible(False)
        self.joint_issue_table.verticalHeader().setDefaultSectionSize(20)
        self.joint_issue_table.setMinimumHeight(140)
        self.joint_issue_table.setMaximumHeight(220)
        self.joint_issue_table.itemClicked.connect(self._on_joint_issue_clicked)

        j_header = self.joint_issue_table.horizontalHeader()
        j_resize = getattr(j_header, "setSectionResizeMode", None) or getattr(j_header, "setResizeMode")
        j_resize(0, QHeaderView.Stretch)
        j_resize(1, QHeaderView.ResizeToContents)
        j_resize(2, QHeaderView.ResizeToContents)
        j_resize(3, QHeaderView.ResizeToContents)
        norm_layout.addWidget(self.joint_issue_table)

        clean_btn_row = QHBoxLayout()
        self.fix_rot_but = QPushButton(
            lang.Lang(en="Rotate -> 0 (Bake Orient)", ja=u"Rotate → 0（Orient へ焼込）").output()
        )
        self.fix_rot_but.clicked.connect(self.run_fix_joint_rotations)
        clean_btn_row.addWidget(self.fix_rot_but)

        self.fix_scl_but = QPushButton(
            lang.Lang(en="Scale -> 1 (Keep Children)", ja=u"Scale → 1（子位置維持）").output()
        )
        self.fix_scl_but.clicked.connect(self.run_fix_joint_scales)
        clean_btn_row.addWidget(self.fix_scl_but)

        self.fix_all_jnt_but = QPushButton(
            lang.Lang(en="Clean Both (Rot=0 & Scl=1)", ja=u"一括整理（Rot=0 / Scl=1）").output()
        )
        self.fix_all_jnt_but.clicked.connect(self.run_fix_joint_all)
        clean_btn_row.addWidget(self.fix_all_jnt_but)
        norm_layout.addLayout(clean_btn_row)
        layout.addWidget(norm_box)

        # 回転等調整 → ポーズ（キーフレーム）書き出し → 接続解除フリーズ → キーフレーム書き戻し一括実行
        freeze_box = QGroupBox(
            lang.Lang(
                en="Adjust Rotation -> Export Pose -> Force Freeze -> Write Back Keyframes",
                ja=u"回転等調整 → ポーズ書き出し → 強制フリーズ → キーフレーム書き戻し（一括実行）",
            ).output()
        )
        freeze_layout = QVBoxLayout(freeze_box)
        freeze_layout.setContentsMargins(8, 6, 8, 6)
        freeze_layout.setSpacing(5)

        fz_adj_row = QHBoxLayout()
        fz_adj_row.setSpacing(6)
        fz_adj_row.addWidget(
            QLabel(lang.Lang(en="Rotate Adjust (deg):", ja=u"回転調整 (度):").output())
        )
        self.fz_rot_adj_spins = {}
        for ax in ("X", "Y", "Z"):
            fz_adj_row.addWidget(QLabel(ax + ":"))
            sp = QDoubleSpinBox(self)
            sp.setRange(-360.0, 360.0)
            sp.setDecimals(2)
            sp.setSingleStep(5.0)
            sp.setValue(0.0)
            sp.setMaximumWidth(74)
            sp.setToolTip(
                lang.Lang(
                    en="Optional relative rotation offset applied before freeze (leave 0 to use current viewport pose).",
                    ja=u"フリーズ前に適用する任意の回転オフセット角度（0 のままなら現在のビューポート調整姿勢をそのまま使用）。",
                ).output()
            )
            self.fz_rot_adj_spins[ax] = sp
            fz_adj_row.addWidget(sp)

        self.fz_rot_adj_reset_but = QPushButton("0")
        self.fz_rot_adj_reset_but.setMaximumWidth(28)
        self.fz_rot_adj_reset_but.setToolTip(
            lang.Lang(en="Reset rotation adjust values to 0", ja=u"回転調整値を 0 にリセット").output()
        )
        self.fz_rot_adj_reset_but.clicked.connect(self._reset_force_freeze_rotate_adjust)
        fz_adj_row.addWidget(self.fz_rot_adj_reset_but)
        fz_adj_row.addStretch(1)
        freeze_layout.addLayout(fz_adj_row)

        fz_ch_row = QHBoxLayout()
        fz_ch_row.setSpacing(10)
        self.fz_rot_chk = QCheckBox("Rotate")
        self.fz_rot_chk.setChecked(True)
        fz_ch_row.addWidget(self.fz_rot_chk)

        self.fz_scl_chk = QCheckBox("Scale")
        self.fz_scl_chk.setChecked(True)
        fz_ch_row.addWidget(self.fz_scl_chk)

        self.fz_trans_chk = QCheckBox("Translate")
        self.fz_trans_chk.setChecked(False)
        self.fz_trans_chk.setToolTip(
            lang.Lang(
                en="Warning: Freezing Translate on joints resets pivot to origin (keep OFF normally).",
                ja=u"注意：ジョイントで Translate をフリーズするとピボットが原点移動するため通常は OFF 推奨です。",
            ).output()
        )
        fz_ch_row.addWidget(self.fz_trans_chk)

        self.fz_clean_anim_chk = QCheckBox(
            lang.Lang(en="Clean orphan animCurves", ja=u"孤立キー削除").output()
        )
        self.fz_clean_anim_chk.setChecked(True)
        fz_ch_row.addWidget(self.fz_clean_anim_chk)

        self.fz_use_frame_chk = QCheckBox(
            lang.Lang(en="Freeze frame:", ja=u"基準フレーム:").output()
        )
        self.fz_use_frame_chk.setChecked(False)
        fz_ch_row.addWidget(self.fz_use_frame_chk)

        self.fz_frame_spin = QSpinBox(self)
        self.fz_frame_spin.setRange(-100000, 100000)
        self.fz_frame_spin.setValue(0)
        self.fz_frame_spin.setMaximumWidth(62)
        self.fz_frame_spin.setEnabled(False)
        self.fz_use_frame_chk.toggled.connect(self.fz_frame_spin.setEnabled)
        fz_ch_row.addWidget(self.fz_frame_spin)
        fz_ch_row.addStretch(1)
        freeze_layout.addLayout(fz_ch_row)

        fz_btn_row = QHBoxLayout()
        fz_btn_row.setSpacing(6)
        self.exec_force_freeze_but = QPushButton(
            lang.Lang(
                en="Adjust -> Export Pose -> Freeze -> Write Back Keyframes",
                ja=u"回転調整 → ポーズ書き出し → フリーズ → キー書き戻し（一括実行）",
            ).output()
        )
        self.exec_force_freeze_but.setMinimumHeight(30)
        self.exec_force_freeze_but.clicked.connect(self.run_force_freeze_joints)
        fz_btn_row.addWidget(self.exec_force_freeze_but, 1)

        self.open_pose_json_dir_but = QPushButton(
            lang.Lang(en="JSON Folder", ja=u"JSON フォルダ").output()
        )
        self.open_pose_json_dir_but.setMinimumHeight(30)
        self.open_pose_json_dir_but.clicked.connect(self.open_pose_backup_folder)
        fz_btn_row.addWidget(self.open_pose_json_dir_but, 0)
        freeze_layout.addLayout(fz_btn_row)
        layout.addWidget(freeze_box)

        layout.addStretch(1)
        return outer

    def _sync_include_children_from_inspect(self, checked):
        """ジョイント診断タブ側の子階層チェック状態をジョイント整理タブ側と同期する。"""
        if hasattr(self, "jnt_include_children_chk") and self.jnt_include_children_chk.isChecked() != checked:
            self.jnt_include_children_chk.blockSignals(True)
            self.jnt_include_children_chk.setChecked(checked)
            self.jnt_include_children_chk.blockSignals(False)

    def _sync_include_children_from_orient(self, checked):
        """ジョイント整理タブ側の子階層チェック状態をジョイント診断タブ側と同期する。"""
        if hasattr(self, "jnt_inspect_include_children_chk") and self.jnt_inspect_include_children_chk.isChecked() != checked:
            self.jnt_inspect_include_children_chk.blockSignals(True)
            self.jnt_inspect_include_children_chk.setChecked(checked)
            self.jnt_inspect_include_children_chk.blockSignals(False)

    def set_tab_visible(self, tab_key, visible):
        """指定したキーのタブの表示・非表示を Python から切り替える。"""
        if tab_key not in self._tab_entries:
            raise KeyError("Unknown tab key: {0} (valid: {1})".format(tab_key, ", ".join(self._tab_order)))
        is_vis = bool(visible)
        self._tab_visibility[tab_key] = is_vis
        entry = self._tab_entries[tab_key]
        widget = entry["widget"]

        if hasattr(self.tabs, "setTabVisible"):
            idx = self.tabs.indexOf(widget)
            if idx >= 0:
                self.tabs.setTabVisible(idx, is_vis)
            return

        # PySide2 互換フォールバック（insertTab / removeTab で順序を保って切り替える）
        idx = self.tabs.indexOf(widget)
        if is_vis and idx < 0:
            insert_pos = 0
            for key in self._tab_order:
                if key == tab_key:
                    break
                if self.tabs.indexOf(self._tab_entries[key]["widget"]) >= 0:
                    insert_pos += 1
            self.tabs.insertTab(insert_pos, widget, entry["label"])
        elif (not is_vis) and idx >= 0:
            self.tabs.removeTab(idx)

    def apply_tab_visibility(self, visibility_map=None):
        """辞書（TAB_VISIBILITY 形式）に基づいて全タブの表示・非表示を一括適用する。"""
        target_map = visibility_map if visibility_map is not None else TAB_VISIBILITY
        for key in self._tab_order:
            if key in target_map:
                self.set_tab_visible(key, target_map[key])
        # 現在選択中のタブが非表示になった場合は最初の表示中タブへ移動する
        if hasattr(self.tabs, "isTabVisible"):
            cur_idx = self.tabs.currentIndex()
            if cur_idx < 0 or not self.tabs.isTabVisible(cur_idx):
                for i in range(self.tabs.count()):
                    if self.tabs.isTabVisible(i):
                        self.tabs.setCurrentIndex(i)
                        break

    # ------------------------------------------------------------------
    # タブ構築：リファレンスジョイントによる Local Rotation Axis・構造上書き
    # ------------------------------------------------------------------
    def _build_reference_joint_tab(self):
        outer = QWidget(self)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        scroll.setWidget(tab)
        outer_layout.addWidget(scroll)

        # クイック軸方向コピー＆変更（JSON 保存なし・指ボーン等の素早い軸揃え向け）
        quick_box = QGroupBox(
            lang.Lang(
                en="Quick Axis Copy & Apply (No JSON - Fast Finger / Joint Orientation Match)",
                ja=u"クイック軸方向コピー＆変更（JSON 保存なし・指ボーン等の素早い方向修正）",
            ).output()
        )
        quick_layout = QVBoxLayout(quick_box)
        quick_layout.setContentsMargins(8, 6, 8, 6)
        quick_layout.setSpacing(5)

        self.quick_axis_label = QLabel(
            lang.Lang(
                en="Stored Axis Direction: (Not stored yet - select a reference bone and click Store)",
                ja=u"一時保存中の方向: 未保存（手本ボーンを選択して「方向を一時保存」をクリック）",
            ).output()
        )
        self.quick_axis_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.quick_axis_label.setFixedHeight(24)
        qt.change_button_color(self.quick_axis_label, textColor=[190, 235, 200], bgColor=[40, 48, 44])
        self.quick_axis_label.setMargin(4)
        quick_layout.addWidget(self.quick_axis_label)

        quick_btn_row = QHBoxLayout()
        quick_btn_row.setSpacing(6)
        self.quick_axis_copy_but = QPushButton(
            lang.Lang(en="Store Ref Axis (Temp)", ja=u"リファレンス方向を一時保存").output()
        )
        self.quick_axis_copy_but.setMinimumHeight(28)
        self.quick_axis_copy_but.clicked.connect(self.store_quick_axis_reference)
        quick_btn_row.addWidget(self.quick_axis_copy_but, 2)

        self.quick_axis_apply_but = QPushButton(
            lang.Lang(en="Apply Axis to Selected", ja=u"選択ボーンの軸方向を変更").output()
        )
        self.quick_axis_apply_but.setMinimumHeight(28)
        self.quick_axis_apply_but.clicked.connect(self.apply_quick_axis_to_selected)
        quick_btn_row.addWidget(self.quick_axis_apply_but, 2)

        self.quick_axis_lra_but = QPushButton(
            lang.Lang(en="LRA", ja=u"軸表示").output()
        )
        self.quick_axis_lra_but.setMinimumHeight(28)
        self.quick_axis_lra_but.setMaximumWidth(64)
        self.quick_axis_lra_but.setToolTip(
            lang.Lang(
                en="Toggle Local Rotation Axes visibility on selected bone(s)",
                ja=u"選択ボーンのローカル回転軸表示（LRA）を切り替えます",
            ).output()
        )
        self.quick_axis_lra_but.clicked.connect(self.toggle_quick_axis_lra)
        quick_btn_row.addWidget(self.quick_axis_lra_but, 0)
        quick_layout.addLayout(quick_btn_row)

        quick_opt_row = QHBoxLayout()
        quick_opt_row.setSpacing(10)
        self.quick_axis_children_chk = QCheckBox(
            lang.Lang(en="Include children", ja=u"子階層も含める").output()
        )
        self.quick_axis_children_chk.setChecked(False)
        quick_opt_row.addWidget(self.quick_axis_children_chk)

        self.quick_axis_rot_order_chk = QCheckBox(
            lang.Lang(en="Match Rotate Order", ja=u"Rotate Order も合わせる").output()
        )
        self.quick_axis_rot_order_chk.setChecked(True)
        quick_opt_row.addWidget(self.quick_axis_rot_order_chk)

        self.quick_axis_skin_chk = QCheckBox(
            lang.Lang(en="Keep skinned mesh pose (bindPreMatrix)", ja=u"バインド中メッシュ変形防止").output()
        )
        self.quick_axis_skin_chk.setChecked(True)
        self.quick_axis_skin_chk.setToolTip(
            lang.Lang(
                en="Updates skinCluster bindPreMatrix automatically so bound meshes stay undeformed without unbinding.",
                ja=u"ON にするとバインド解除せず skinCluster の bindPreMatrix を即時更新し、メッシュを変形させずに軸だけ差し替えます。",
            ).output()
        )
        quick_opt_row.addWidget(self.quick_axis_skin_chk)
        quick_opt_row.addStretch(1)
        quick_layout.addLayout(quick_opt_row)

        self.quick_freeze_rot_but = QPushButton(
            lang.Lang(
                en="Freeze Rotation of Selected Bone(s) (Keeps Child Hierarchy Unrotated)",
                ja=u"選択ボーンの回転をフリーズ（子階層は回転させない）",
            ).output()
        )
        self.quick_freeze_rot_but.setMinimumHeight(26)
        self.quick_freeze_rot_but.clicked.connect(self.freeze_quick_selected_bone_rotations)
        quick_layout.addWidget(self.quick_freeze_rot_but)
        layout.addWidget(quick_box)

        guide = QLabel(
            lang.Lang(
                en="Save/Load full Reference skeleton as '[CharName]_Hips' JSON for use across scenes. If Current skeleton is registered, overwrites all joints & reapplies weights; if not registered, creates the Reference skeleton in the scene.",
                ja=u"リファレンスボーン全体を「[キャラ名等]_Hips」形式で JSON 保存・読込し、他シーンでも再利用できます。\n現在ジョイントが登録済みなら全ジョイントを上書き＆ウェイト再適用し、未登録の場合はリファレンスボーンを新規作成します。",
            ).output()
        )
        guide.setWordWrap(True)
        qt.change_button_color(guide, textColor=[220, 224, 232], bgColor=[50, 52, 58])
        guide.setMargin(6)
        layout.addWidget(guide)

        # スケルトン登録エリア（常に全階層を対象に変更または新規作成）
        reg_box = QGroupBox(
            lang.Lang(
                en="Step 1 : Register Reference & Current Skeleton (Full Hierarchy)",
                ja=u"ステップ 1：リファレンス ＆ 現在ジョイント登録（全階層対象・他シーン対応）",
            ).output()
        )
        reg_layout = QVBoxLayout(reg_box)
        reg_layout.setSpacing(6)

        cur_row = QHBoxLayout()
        self.ref_cur_label = QLabel(
            lang.Lang(en="Current Skeleton: (Not set -> Create mode)", ja=u"現在のジョイント: 未登録（未登録時はリファレンスを新規作成）").output()
        )
        self.ref_cur_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.ref_cur_label.setFixedHeight(24)
        qt.change_button_color(self.ref_cur_label, textColor=[200, 220, 240], bgColor=[42, 46, 54])
        self.ref_cur_label.setMargin(4)
        cur_row.addWidget(self.ref_cur_label, 1)

        self.set_ref_cur_but = QPushButton(
            lang.Lang(en="Set Current Skeleton", ja=u"現在ジョイント登録").output()
        )
        self.set_ref_cur_but.setFixedHeight(24)
        self.set_ref_cur_but.setMinimumWidth(140)
        self.set_ref_cur_but.clicked.connect(self.register_current_skeleton_for_ref)
        cur_row.addWidget(self.set_ref_cur_but, 0)

        self.clear_ref_cur_but = QPushButton(
            lang.Lang(en="Clear", ja=u"クリア").output()
        )
        self.clear_ref_cur_but.setFixedHeight(24)
        self.clear_ref_cur_but.setMaximumWidth(56)
        self.clear_ref_cur_but.clicked.connect(self.clear_current_skeleton_for_ref)
        cur_row.addWidget(self.clear_ref_cur_but, 0)
        reg_layout.addLayout(cur_row)

        src_row = QHBoxLayout()
        self.ref_src_label = QLabel(
            lang.Lang(en="Reference Skeleton: (Not set)", ja=u"リファレンスジョイント: 未登録").output()
        )
        self.ref_src_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.ref_src_label.setFixedHeight(24)
        qt.change_button_color(self.ref_src_label, textColor=[240, 220, 175], bgColor=[50, 46, 40])
        self.ref_src_label.setMargin(4)
        src_row.addWidget(self.ref_src_label, 1)

        self.set_ref_src_but = QPushButton(
            lang.Lang(en="Set Reference Skeleton", ja=u"リファレンス登録（全階層）").output()
        )
        self.set_ref_src_but.setFixedHeight(24)
        self.set_ref_src_but.setMinimumWidth(202)
        self.set_ref_src_but.clicked.connect(self.register_reference_skeleton)
        src_row.addWidget(self.set_ref_src_but, 0)
        reg_layout.addLayout(src_row)

        json_row = QHBoxLayout()
        self.save_ref_json_but = QPushButton(
            lang.Lang(en="Save Reference JSON", ja=u"リファレンス JSON 保存").output()
        )
        self.save_ref_json_but.clicked.connect(self.save_reference_session_only)
        json_row.addWidget(self.save_ref_json_but, 1)

        self.load_ref_json_but = QPushButton(
            lang.Lang(en="Load Reference JSON...", ja=u"リファレンス JSON 読込...").output()
        )
        self.load_ref_json_but.clicked.connect(self.load_reference_session_from_json)
        json_row.addWidget(self.load_ref_json_but, 1)

        self.open_ref_json_dir_but = QPushButton(
            lang.Lang(en="JSON Folder", ja=u"JSON フォルダ").output()
        )
        self.open_ref_json_dir_but.clicked.connect(self.open_reference_session_folder)
        json_row.addWidget(self.open_ref_json_dir_but, 0)
        reg_layout.addLayout(json_row)
        layout.addWidget(reg_box)

        # 上書きオプション
        opt_box = QGroupBox(
            lang.Lang(en="Step 2 : Overwrite & Weight Reapply Options", ja=u"ステップ 2：上書き＆ウェイト再適用オプション").output()
        )
        opt_layout = QVBoxLayout(opt_box)
        opt_layout.setSpacing(4)

        self.ref_opt_orient_chk = QCheckBox(
            lang.Lang(
                en="Overwrite Local Rotation Axis (jointOrient, keeps child positions)",
                ja=u"Local Rotation Axis（jointOrient）をリファレンスで上書き（子位置維持・HumanIK 軸修正）",
            ).output()
        )
        self.ref_opt_orient_chk.setChecked(True)
        opt_layout.addWidget(self.ref_opt_orient_chk)

        self.ref_opt_rot_order_chk = QCheckBox(
            lang.Lang(
                en="Match Rotate Order to Reference",
                ja=u"回転順序（Rotate Order）もリファレンスに合わせる",
            ).output()
        )
        self.ref_opt_rot_order_chk.setChecked(True)
        opt_layout.addWidget(self.ref_opt_rot_order_chk)

        self.ref_opt_pos_chk = QCheckBox(
            lang.Lang(
                en="Also overwrite joint world positions from Reference",
                ja=u"ジョイントのワールド位置もリファレンスに合わせる",
            ).output()
        )
        self.ref_opt_pos_chk.setChecked(False)
        opt_layout.addWidget(self.ref_opt_pos_chk)

        self.ref_opt_rename_chk = QCheckBox(
            lang.Lang(
                en="Apply joint renames configured in Mapping UI",
                ja=u"調整 UI で設定したジョイント名リネームを適用する",
            ).output()
        )
        self.ref_opt_rename_chk.setChecked(True)
        opt_layout.addWidget(self.ref_opt_rename_chk)

        self.ref_opt_bind_added_chk = QCheckBox(
            lang.Lang(
                en="Include newly added joints when rebinding skinCluster",
                ja=u"構造追加した新規ジョイントも再バインド時のインフルエンスに含める",
            ).output()
        )
        self.ref_opt_bind_added_chk.setChecked(True)
        opt_layout.addWidget(self.ref_opt_bind_added_chk)

        self.ref_opt_auto_dialog_chk = QCheckBox(
            lang.Lang(
                en="Automatically open Rename / Structure UI when names or hierarchy differ",
                ja=u"ジョイント名や構造に差分がある場合は自動でリネーム＆構造調整 UI を開く",
            ).output()
        )
        self.ref_opt_auto_dialog_chk.setChecked(True)
        opt_layout.addWidget(self.ref_opt_auto_dialog_chk)
        layout.addWidget(opt_box)

        # 照合サマリー表示と実行ボタン
        self.ref_summary_label = QLabel(
            lang.Lang(
                en="Register both skeletons to inspect naming / hierarchy differences.",
                ja=u"現在ジョイントとリファレンスジョイントを登録すると差分診断が表示されます。",
            ).output()
        )
        self.ref_summary_label.setWordWrap(True)
        qt.change_button_color(self.ref_summary_label, textColor=[210, 218, 228], bgColor=[44, 46, 52])
        self.ref_summary_label.setMargin(6)
        layout.addWidget(self.ref_summary_label)

        exec_row = QHBoxLayout()
        exec_row.setSpacing(6)
        self.open_ref_map_but = QPushButton(
            lang.Lang(en="Open Rename / Structure UI...", ja=u"リネーム・構造調整 UI を開く...").output()
        )
        self.open_ref_map_but.setMinimumHeight(32)
        self.open_ref_map_but.clicked.connect(self.open_reference_mapping_ui)
        exec_row.addWidget(self.open_ref_map_but, 1)

        self.exec_ref_overwrite_but = QPushButton(
            lang.Lang(
                en="Save JSON & Overwrite / Create Reference Bones",
                ja=u"JSON 保存 ＆ リファレンスで上書き／新規作成",
            ).output()
        )
        self.exec_ref_overwrite_but.setMinimumHeight(32)
        self.exec_ref_overwrite_but.clicked.connect(self.run_reference_joint_overwrite)
        exec_row.addWidget(self.exec_ref_overwrite_but, 2)
        layout.addLayout(exec_row)

        layout.addStretch(1)
        return outer

    # ------------------------------------------------------------------
    # タブ構築：メッシュ ＆ シーンクリーンアップ
    # ------------------------------------------------------------------
    def _build_cleanup_tab(self):
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # メッシュクリーンアップ（画像準拠）
        mesh_box = QGroupBox(
            lang.Lang(
                en="Mesh Cleanup (Fix by Tesselation & Geometry)",
                ja=u"メッシュクリーンアップ（Fix by Tesselation ＆ 不正ジオメトリ）",
            ).output()
        )
        mesh_layout = QVBoxLayout(mesh_box)
        mesh_layout.setSpacing(6)

        mc_cols = QHBoxLayout()
        mc_cols.setSpacing(12)

        # 左列：Fix by Tesselation
        tess_col = QVBoxLayout()
        tess_col.addWidget(
            QLabel(
                lang.Lang(
                    en="<b>Fix by Tesselation</b>",
                    ja=u"<b>テッセレーション修正 (Fix by Tesselation)</b>",
                ).output()
            )
        )
        self.mc_quads_chk = QCheckBox(
            lang.Lang(en="4-sided faces", ja=u"4角形フェース (4-sided faces)").output()
        )
        self.mc_quads_chk.setChecked(False)
        tess_col.addWidget(self.mc_quads_chk)

        self.mc_nsided_chk = QCheckBox(
            lang.Lang(
                en="Faces with more than 4 sides",
                ja=u"5角形以上 (Faces with more than 4 sides)",
            ).output()
        )
        self.mc_nsided_chk.setChecked(True)
        tess_col.addWidget(self.mc_nsided_chk)

        self.mc_concave_chk = QCheckBox(
            lang.Lang(en="Concave faces", ja=u"凹面フェース (Concave faces)").output()
        )
        self.mc_concave_chk.setChecked(True)
        tess_col.addWidget(self.mc_concave_chk)

        self.mc_holed_chk = QCheckBox(
            lang.Lang(en="Faces with holes", ja=u"穴あきフェース (Faces with holes)").output()
        )
        self.mc_holed_chk.setChecked(True)
        tess_col.addWidget(self.mc_holed_chk)

        self.mc_nonplanar_chk = QCheckBox(
            lang.Lang(en="Non-planar faces", ja=u"非平面フェース (Non-planar faces)").output()
        )
        self.mc_nonplanar_chk.setChecked(False)
        tess_col.addWidget(self.mc_nonplanar_chk)
        tess_col.addStretch(1)
        mc_cols.addLayout(tess_col, 1)

        # 右列：Remove Geometry（不正ジオメトリ）
        geo_col = QVBoxLayout()
        geo_col.addWidget(
            QLabel(
                lang.Lang(
                    en="<b>Remove Geometry</b>",
                    ja=u"<b>不正ジオメトリ除去 (Remove Geometry)</b>",
                ).output()
            )
        )
        self.mc_lamina_chk = QCheckBox(
            lang.Lang(en="Lamina faces", ja=u"重なりフェース (Lamina faces)").output()
        )
        self.mc_lamina_chk.setChecked(True)
        geo_col.addWidget(self.mc_lamina_chk)

        self.mc_nonmanifold_chk = QCheckBox(
            lang.Lang(en="Nonmanifold geometry", ja=u"非多様体 (Nonmanifold geometry)").output()
        )
        self.mc_nonmanifold_chk.setChecked(True)
        geo_col.addWidget(self.mc_nonmanifold_chk)

        self.mc_zero_geom_chk = QCheckBox(
            lang.Lang(
                en="Faces with zero geometry area",
                ja=u"面積 0 フェース (Zero geometry area)",
            ).output()
        )
        self.mc_zero_geom_chk.setChecked(False)
        geo_col.addWidget(self.mc_zero_geom_chk)

        self.mc_zero_edge_chk = QCheckBox(
            lang.Lang(
                en="Edges with zero length",
                ja=u"長さ 0 エッジ (Zero length edges)",
            ).output()
        )
        self.mc_zero_edge_chk.setChecked(False)
        geo_col.addWidget(self.mc_zero_edge_chk)
        geo_col.addStretch(1)
        mc_cols.addLayout(geo_col, 1)

        mesh_layout.addLayout(mc_cols)

        mc_btn_row = QHBoxLayout()
        self.mc_detect_but = QPushButton(
            lang.Lang(en="Detect & Select Only", ja=u"問題箇所を検出・選択（非破壊）").output()
        )
        self.mc_detect_but.setMinimumHeight(30)
        self.mc_detect_but.clicked.connect(lambda: self.run_mesh_cleanup_ui(select_only=True))
        mc_btn_row.addWidget(self.mc_detect_but, 1)

        self.mc_fix_but = QPushButton(
            lang.Lang(en="Execute Mesh Cleanup (Fix)", ja=u"Mesh Cleanup を実行（修正）").output()
        )
        self.mc_fix_but.setMinimumHeight(30)
        self.mc_fix_but.clicked.connect(lambda: self.run_mesh_cleanup_ui(select_only=False))
        mc_btn_row.addWidget(self.mc_fix_but, 1)
        mesh_layout.addLayout(mc_btn_row)
        layout.addWidget(mesh_box)

        # シーンクリーンアップ
        scene_box = QGroupBox(
            lang.Lang(en="Scene Cleanup", ja=u"シーンクリーンアップ（不要ノード・履歴の診断と整理）").output()
        )
        scene_layout = QVBoxLayout(scene_box)
        scene_layout.setSpacing(4)

        self.sc_interm_chk = QCheckBox(
            lang.Lang(en="Delete unused intermediate shapes (ShapeOrig)",
                      ja=u"未使用の中間シェイプ（ShapeOrig）を削除").output()
        )
        self.sc_interm_chk.setChecked(True)
        scene_layout.addWidget(self.sc_interm_chk)

        self.sc_bindpose_chk = QCheckBox(
            lang.Lang(en="Delete orphaned bindPose (dagPose) nodes",
                      ja=u"孤立した bindPose（dagPose）を削除").output()
        )
        self.sc_bindpose_chk.setChecked(True)
        scene_layout.addWidget(self.sc_bindpose_chk)

        self.sc_unknown_chk = QCheckBox(
            lang.Lang(en="Delete unknown / unknownDag nodes",
                      ja=u"未知ノード（unknown / unknownDag）を削除").output()
        )
        self.sc_unknown_chk.setChecked(True)
        scene_layout.addWidget(self.sc_unknown_chk)

        self.sc_empty_grp_chk = QCheckBox(
            lang.Lang(en="Delete empty unconnected transform groups",
                      ja=u"空の未使用トランスフォームグループを削除").output()
        )
        self.sc_empty_grp_chk.setChecked(False)
        scene_layout.addWidget(self.sc_empty_grp_chk)

        self.sc_unused_mat_chk = QCheckBox(
            lang.Lang(en="Delete unused materials / shading nodes (MLdeleteUnused)",
                      ja=u"未使用のマテリアル・シェーディングノードを削除（MLdeleteUnused）").output()
        )
        self.sc_unused_mat_chk.setChecked(False)
        scene_layout.addWidget(self.sc_unused_mat_chk)

        self.scene_scan_label = QLabel("")
        self.scene_scan_label.setWordWrap(True)
        qt.change_button_color(self.scene_scan_label, textColor=[210, 215, 225], bgColor=[46, 48, 54])
        self.scene_scan_label.setMargin(5)
        scene_layout.addWidget(self.scene_scan_label)

        sc_btn_row = QHBoxLayout()
        self.sc_scan_but = QPushButton(
            lang.Lang(en="Scan Scene (Check Counts)", ja=u"シーン内を診断（件数確認）").output()
        )
        self.sc_scan_but.setMinimumHeight(30)
        self.sc_scan_but.clicked.connect(self.run_scene_scan_ui)
        sc_btn_row.addWidget(self.sc_scan_but, 1)

        self.sc_exec_but = QPushButton(
            lang.Lang(en="Execute Scene Cleanup", ja=u"Scene Cleanup を実行").output()
        )
        self.sc_exec_but.setMinimumHeight(30)
        self.sc_exec_but.clicked.connect(self.run_scene_cleanup_ui)
        sc_btn_row.addWidget(self.sc_exec_but, 1)
        scene_layout.addLayout(sc_btn_row)
        layout.addWidget(scene_box)

        layout.addStretch(1)
        return tab

    # ------------------------------------------------------------------
    # 共通イベント・ヘルパー
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self._radius_undo_open:
            try:
                cmds.undoInfo(closeChunk=True)
            except Exception:
                pass
            self._radius_undo_open = False
        self._remove_radius_selection_job()
        if self.log_window is not None:
            try:
                self.log_window.close()
            except Exception:
                pass
            self.log_window = None
        if self.unweighted_bind_window is not None:
            try:
                self.unweighted_bind_window.close()
            except Exception:
                pass
            self.unweighted_bind_window = None
        if self.editor is not None and getattr(self.editor, "model_edit_window", None) is self:
            self.editor.model_edit_window = None
        super(ModelEditWindow, self).closeEvent(event)

    def _update_log_button_label(self):
        cnt = len(self._log_entries)
        self.open_log_but.setText(
            lang.Lang(en="Log ({0})", ja=u"ログ表示 ({0})").output().format(cnt)
        )

    def open_log_window(self):
        """詳細ログを表示する別ウィンドウを開く（既に開いていれば前面へ表示する）。"""
        if self.log_window is not None:
            try:
                self.log_window.show()
                self.log_window.raise_()
                self.log_window.activateWindow()
                return
            except Exception:
                self.log_window = None

        self.log_window = ModelEditLogWindow(self)
        self.log_window.set_entries(self._log_entries)
        self.log_window.show()

    def clear_log_entries(self):
        """保持しているログ履歴をクリアする。"""
        self._log_entries = []
        self._update_log_button_label()

    def _set_status(self, message, error=False, detail=None):
        """下部 1 行バーへ簡潔な状態を表示し、詳細履歴をログウィンドウへ記録する。"""
        single_line = message.replace("\r", " ").replace("\n", " ")
        self.status_label.setText(single_line)
        self.status_label.setToolTip(detail if detail else message)
        if error:
            qt.change_button_color(self.status_label, textColor=[255, 145, 95], bgColor=[54, 40, 38])
        else:
            qt.change_button_color(self.status_label, textColor=[155, 235, 165], bgColor=[40, 48, 42])

        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        level = "ERR" if error else "OK"
        entry = "[{0}] [{1}] {2}".format(timestamp, level, message)
        if detail:
            entry += "\n" + detail
        self._log_entries.append(entry)
        self._update_log_button_label()
        if self.log_window is not None:
            try:
                self.log_window.append_entry(entry)
            except Exception:
                self.log_window = None

    def open_json_folder(self):
        """JSON バックアップ保存先フォルダをエクスプローラーで開く。"""
        folder = os.path.normpath(_get_storage_dir())
        try:
            if hasattr(os, "startfile"):
                os.startfile(folder)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            self._set_status(
                lang.Lang(en="Opened JSON folder.", ja=u"JSON 保存フォルダを開きました。").output(),
                error=False,
                detail=folder,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Folder open error: {0}", ja=u"フォルダを開けませんでした: {0}").output().format(exc),
                error=True,
            )

    def _update_cached_joint_ui(self):
        """一時保存中の記憶ジョイント表示ラベルとクリアボタンの状態を更新する。"""
        if not hasattr(self, "cached_joint_label"):
            return
        cached = self._cached_joints or []
        if not cached:
            self.cached_joint_label.setText(
                lang.Lang(
                    en="Saved Joint: None (Recorded automatically on Step 1)",
                    ja=u"記憶ジョイント: なし（ステップ 1 実行時に自動記憶）",
                ).output()
            )
            self.cached_joint_label.setToolTip("")
            self.clear_cached_joint_but.setEnabled(False)
            return

        short_names = [j.split("|")[-1] for j in cached]
        head = short_names[0]
        if len(short_names) > 1:
            disp = "{0} (+{1})".format(head, len(short_names) - 1)
        else:
            disp = head

        is_hips_ok = all(_is_hips_named_joint(j) for j in cached)
        if is_hips_ok:
            self.cached_joint_label.setText(
                lang.Lang(
                    en="Saved Joint: {0} (auto-used when unselected)",
                    ja=u"記憶ジョイント: {0}（未選択時に自動使用）",
                ).output().format(disp)
            )
            qt.change_button_color(self.cached_joint_label, textColor=[180, 215, 245], bgColor=[44, 48, 56])
        else:
            self.cached_joint_label.setText(
                lang.Lang(
                    en="Saved Joint: {0} (Not [Char]_Hips - will ask confirmation)",
                    ja=u"記憶ジョイント: {0}（[キャラ名]_Hips 以外・開始時に確認）",
                ).output().format(disp)
            )
            qt.change_button_color(self.cached_joint_label, textColor=[255, 200, 120], bgColor=[62, 48, 38])
        self.cached_joint_label.setToolTip("\n".join(cached[:20]))
        self.clear_cached_joint_but.setEnabled(True)

    def clear_cached_joint_memory(self):
        """一時保存していた記憶ジョイント情報をクリアする。"""
        self._cached_joints = []
        _clear_last_seed_joints()
        self._update_cached_joint_ui()
        self._set_status(
            lang.Lang(
                en="Cleared saved joint memory.",
                ja=u"一時保存していたジョイント記憶をクリアしました。",
            ).output(),
            error=False,
        )

    def _get_selected_records(self):
        rows = sorted(set(idx.row() for idx in self.record_table.selectedIndexes()))
        return [self._records[r] for r in rows if 0 <= r < len(self._records)]

    def _get_selected_polygon_transforms(self):
        """ビューポート選択（オブジェクトまたはコンポーネント）から対象メッシュのトランスフォーム一覧を返す。"""
        selection = cmds.ls(sl=True, long=True) or []
        meshes = []
        for item in selection:
            node = item.split(".")[0]
            if not cmds.objExists(node):
                continue
            node_type = cmds.nodeType(node)
            if node_type == "mesh":
                parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
                if parents and parents[0] not in meshes:
                    meshes.append(parents[0])
            elif node_type == "transform":
                shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, type="mesh", fullPath=True) or []
                if shapes and node not in meshes:
                    meshes.append(node)
        return meshes

    def _confirm_skinned_joints_edit(self, joints):
        """修正対象のジョイントがスキニング接続中であればユーザーへ確認ダイアログを出す。"""
        active_skins = rig_cleanup_helper.find_skin_clusters_for_joints(joints)
        if not active_skins:
            return True
        ans = QMessageBox.warning(
            self,
            "SIWeightEditor Improve",
            lang.Lang(
                en="Target joints are currently bound to {0} skinCluster(s).\nModifying joint transforms/orient while bound can deform the mesh.\nIt is recommended to unbind via Tab 1 (Step 1) first.\n\nContinue anyway?",
                ja=u"対象ジョイントは現在 {0} 個のスキンクラスタへバインドされています。\nバインドしたまま回転・スケール・Joint Orient を変更するとメッシュが崩れる可能性があります。\n先に「モデル / ボーン修正」タブのステップ 1 で記録・解除することを推奨します。\n\nこのまま続行しますか？",
            ).output().format(len(active_skins)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ans == QMessageBox.Yes

    # ------------------------------------------------------------------
    # タブ 1 ハンドラ：モデル / ボーン修正
    # ------------------------------------------------------------------
    def reload_record_list(self, select_paths=None):
        """保存フォルダ内の JSON バックアップ一覧を読み込んでテーブルへ表示する。"""
        storage_dir = _get_storage_dir()
        records = []
        for fname in sorted(os.listdir(storage_dir)):
            if not fname.lower().endswith(".json") or fname == LAST_JOINT_CACHE_FILENAME:
                continue
            full_path = os.path.join(storage_dir, fname)
            try:
                with open(full_path, "r") as fp:
                    data = json.load(fp)
                if "weights" not in data or "influences" not in data:
                    continue
                records.append({
                    "path": full_path,
                    "mesh_short": data.get("mesh_short", fname),
                    "mesh_long": data.get("mesh_long", ""),
                    "status": data.get("status", "editing"),
                    "vertex_count": int(data.get("vertex_count", 0)),
                    "bone_count": len(data.get("influences") or []),
                    "saved_at": data.get("saved_at", ""),
                })
            except Exception:
                continue

        # 修正中（editing）を上位、同一ステータス内は新しい順に並べる
        records.sort(key=lambda r: r["saved_at"], reverse=True)
        records.sort(key=lambda r: 0 if r["status"] == "editing" else 1)
        self._records = records

        norm_targets = set()
        if select_paths:
            if isinstance(select_paths, (list, tuple, set)):
                norm_targets = set(os.path.normcase(p) for p in select_paths if p)
            else:
                norm_targets = {os.path.normcase(select_paths)}

        self.record_table.setRowCount(len(records))
        rows_to_select = []
        for idx, rec in enumerate(records):
            mesh_item = QTableWidgetItem(rec["mesh_short"])
            mesh_item.setToolTip(rec["mesh_long"])

            is_editing = rec["status"] == "editing"
            status_text = (
                lang.Lang(en="Editing", ja=u"修正中").output()
                if is_editing
                else lang.Lang(en="Restored", ja=u"復元済").output()
            )
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QColor(255, 200, 90) if is_editing else QColor(150, 225, 160))

            vtx_item = QTableWidgetItem(str(rec["vertex_count"]))
            vtx_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            bone_item = QTableWidgetItem(str(rec["bone_count"]))
            bone_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            time_item = QTableWidgetItem(rec["saved_at"])

            self.record_table.setItem(idx, 0, mesh_item)
            self.record_table.setItem(idx, 1, status_item)
            self.record_table.setItem(idx, 2, vtx_item)
            self.record_table.setItem(idx, 3, bone_item)
            self.record_table.setItem(idx, 4, time_item)

            if norm_targets and os.path.normcase(rec["path"]) in norm_targets:
                rows_to_select.append(idx)

        self.record_table.clearSelection()
        if rows_to_select:
            sel_model = self.record_table.selectionModel()
            for r in rows_to_select:
                idx_left = self.record_table.model().index(r, 0)
                idx_right = self.record_table.model().index(r, 4)
                sel_model.select(QItemSelection(idx_left, idx_right), QItemSelectionModel.Select)
        elif records:
            self.record_table.selectRow(0)

    def select_all_editing_records(self):
        """一覧の中から「修正中」ステータスのバックアップをすべて選択する。"""
        self.record_table.clearSelection()
        sel_model = self.record_table.selectionModel()
        count = 0
        for idx, rec in enumerate(self._records):
            if rec["status"] == "editing":
                idx_left = self.record_table.model().index(idx, 0)
                idx_right = self.record_table.model().index(idx, 4)
                sel_model.select(QItemSelection(idx_left, idx_right), QItemSelectionModel.Select)
                count += 1
        self._set_status(
            lang.Lang(en="Selected {0} editing backup(s).", ja=u"修正中のバックアップを {0} 件選択しました。").output().format(count),
            error=(count == 0),
        )

    @staticmethod
    def _disable_auto_keyframe_temporarily():
        """処理中の意図しないキー上書きを防ぐためオートキーを一時停止し、元の状態を返す。"""
        try:
            prev_state = bool(cmds.autoKeyframe(query=True, state=True))
            if prev_state:
                cmds.autoKeyframe(state=False)
            return prev_state
        except Exception:
            return None

    @staticmethod
    def _restore_auto_keyframe_state(prev_state):
        """一時停止していたオートキーを元の状態へ戻す。"""
        if prev_state is True:
            try:
                cmds.autoKeyframe(state=True)
            except Exception:
                pass

    @staticmethod
    def _collect_skeleton_joints_for_meshes(target_meshes):
        """対象メッシュ群のスキンクラスタに紐づくスケルトン全ジョイントを収集する。"""
        seed_joints = []
        seen = set()
        for mesh_long in (target_meshes or []):
            sc = _get_skin_cluster(mesh_long)
            if not sc:
                continue
            infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], type="joint", long=True) or []
            for jnt in infs:
                if jnt not in seen and cmds.objExists(jnt):
                    seen.add(jnt)
                    seed_joints.append(jnt)
        if not seed_joints:
            return []

        roots = []
        seen_roots = set()
        for jnt in seed_joints:
            cur = jnt
            while True:
                parents = cmds.listRelatives(cur, parent=True, type="joint", fullPath=True) or []
                if not parents:
                    break
                cur = parents[0]
            if cur not in seen_roots:
                seen_roots.add(cur)
                roots.append(cur)

        all_joints = list(roots)
        seen_all = set(roots)
        for root in roots:
            desc = cmds.listRelatives(root, allDescendents=True, type="joint", fullPath=True) or []
            for child in desc:
                if child not in seen_all and cmds.objExists(child):
                    seen_all.add(child)
                    all_joints.append(child)
        return all_joints

    @classmethod
    def _inspect_skeleton_rotation_state(cls, target_meshes, edit_frame):
        """gotoBindPose 実行前にスケルトンの 0F 回転キー有無と現在の回転値を検査する。"""
        joints = cls._collect_skeleton_joints_for_meshes(target_meshes)
        pre_rot_map = {}
        keyed_at_edit_frame = 0
        non_zero_joints = 0

        for jnt in joints:
            try:
                rot = cmds.getAttr(jnt + ".rotate")[0]
                rot_tuple = (float(rot[0]), float(rot[1]), float(rot[2]))
            except Exception:
                rot_tuple = (0.0, 0.0, 0.0)
            pre_rot_map[jnt] = rot_tuple
            if any(abs(v) > 1e-4 for v in rot_tuple):
                non_zero_joints += 1

            if edit_frame is not None:
                found_key_at_frame = False
                for axis in ("X", "Y", "Z"):
                    attr = "{0}.rotate{1}".format(jnt, axis)
                    t_list = []
                    try:
                        t_list = cmds.keyframe(attr, q=True, timeChange=True) or []
                    except Exception:
                        t_list = []
                    if any(abs(float(t) - float(edit_frame)) <= 1e-4 for t in t_list):
                        found_key_at_frame = True
                        break
                if found_key_at_frame:
                    keyed_at_edit_frame += 1

        return {
            "joints": joints,
            "pre_rot_map": pre_rot_map,
            "keyed_at_edit_frame": keyed_at_edit_frame,
            "all_zero_rot": bool(joints) and (non_zero_joints == 0),
        }

    @staticmethod
    def _sanitize_rotations_after_bind_pose(pre_rot_map):
        """gotoBindPose 後に混入した微小ノイズを 0.0 へ丸め、大きく変化したボーン一覧を返す。"""
        drifted = []
        for jnt, pre_rot in (pre_rot_map or {}).items():
            if not cmds.objExists(jnt):
                continue
            try:
                cur_rot = cmds.getAttr(jnt + ".rotate")[0]
            except Exception:
                continue
            cleaned = []
            changed_small = False
            for val in cur_rot:
                fv = float(val)
                if 0.0 < abs(fv) < 1e-4:
                    cleaned.append(0.0)
                    changed_small = True
                else:
                    cleaned.append(fv)
            if changed_small:
                try:
                    if rig_cleanup_helper._is_attr_editable(jnt, "rotate"):
                        cmds.setAttr(jnt + ".rotate", cleaned[0], cleaned[1], cleaned[2], type="double3")
                except Exception:
                    pass
            # 実行前は (0, 0, 0) だったのに有意な回転が混入したボーンを検知する
            if all(abs(v) <= 1e-4 for v in pre_rot) and any(abs(v) > 1e-4 for v in cleaned):
                drifted.append((jnt.split("|")[-1], cleaned))
        return drifted

    def _confirm_non_hips_saved_joint(self, joints, is_using_cached=False):
        """記憶ジョイントが '[キャラ名]_Hips' でない場合にユーザーへ確認ダイアログを出し、採用ジョイント一覧（または None=中止）を返す。"""
        if not joints or all(_is_hips_named_joint(j) for j in joints):
            return list(joints)

        disp_names = ", ".join(j.split("|")[-1] for j in joints[:5])
        if len(joints) > 5:
            disp_names += " ..."
        hips_candidate = _find_hips_joint_in_skeleton(joints)
        hips_short = hips_candidate.split("|")[-1] if hips_candidate else None

        if is_using_cached:
            base_msg = lang.Lang(
                en="Saved Joint [{0}] does not match '[CharacterName]_Hips'.\nAre you sure you want to start with this joint?",
                ja=u"記憶ジョイント [{0}] は「[キャラ名等]_Hips」になっていません。\nこのまま修正を開始してよろしいですか？",
            ).output().format(disp_names)
        else:
            base_msg = lang.Lang(
                en="Joint to remember [{0}] does not match '[CharacterName]_Hips'.\nDo you want to continue and save this joint?",
                ja=u"記憶ジョイントに登録しようとしている [{0}] は「[キャラ名]_Hips」になっていません。\nこのまま続行してよろしいですか？",
            ).output().format(disp_names)

        if hips_short:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("SIWeightEditor Improve")
            box.setText(
                base_msg
                + lang.Lang(
                    en="\n\nFound Hips joint in the same skeleton: {0}",
                    ja=u"\n\n同じスケルトン内に「{0}」が見つかりました。",
                ).output().format(hips_short)
            )
            use_hips_btn = box.addButton(
                lang.Lang(
                    en="Use {0} & Continue",
                    ja=u"{0} を記憶して続行",
                ).output().format(hips_short),
                QMessageBox.AcceptRole,
            )
            keep_btn = box.addButton(
                lang.Lang(
                    en="Continue As Is",
                    ja=u"このまま続行",
                ).output(),
                QMessageBox.YesRole,
            )
            cancel_btn = box.addButton(
                lang.Lang(en="Cancel", ja=u"キャンセル").output(),
                QMessageBox.RejectRole,
            )
            box.setDefaultButton(use_hips_btn)
            box.exec_() if hasattr(box, "exec_") else box.exec()
            clicked = box.clickedButton()
            if clicked is use_hips_btn:
                return [hips_candidate]
            if clicked is keep_btn:
                return list(joints)
            return None

        ans = QMessageBox.warning(
            self,
            "SIWeightEditor Improve",
            base_msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return list(joints) if ans == QMessageBox.Yes else None

    def start_modeling_selected(self):
        """選択メッシュ・ジョイント（または記憶済みジョイント）に紐づくスキニングメッシュを JSON 記録し、解除・フリーズする。"""
        meshes = self._get_selected_polygon_transforms()
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        seed_nodes = list(meshes) + list(sel_joints)
        used_cached_joints = False

        # 未選択時（またはメッシュ・ジョイント以外を選択中）は一時保存されたジョイントを自動使用する
        if not seed_nodes and self._cached_joints:
            resolved_cached = _resolve_cached_joints(self._cached_joints)
            if resolved_cached:
                confirmed_cached = self._confirm_non_hips_saved_joint(resolved_cached, is_using_cached=True)
                if confirmed_cached is None:
                    self._set_status(
                        lang.Lang(
                            en="Cancelled because saved joint is not '[CharacterName]_Hips'.",
                            ja=u"記憶ジョイントが [キャラ名]_Hips ではないため開始を中止しました。",
                        ).output(),
                        error=True,
                    )
                    return
                if confirmed_cached != resolved_cached:
                    self._cached_joints = _save_last_seed_joints(confirmed_cached)
                    self._update_cached_joint_ui()
                seed_nodes = confirmed_cached
                sel_joints = confirmed_cached
                used_cached_joints = True

        if not seed_nodes:
            self._set_status(
                lang.Lang(
                    en="Please select a skinned polygon mesh or a joint.",
                    ja=u"スキニング済みのポリゴンメッシュまたはジョイントを選択してください。",
                ).output(),
                error=True,
            )
            return

        target_meshes = []
        seen = set()
        for m in meshes:
            if _get_skin_cluster(m) and m not in seen:
                seen.add(m)
                target_meshes.append(m)

        if self.include_shared_meshes_chk.isChecked() or (sel_joints and not target_meshes):
            shared = rig_cleanup_helper.find_meshes_sharing_skeleton(seed_nodes)
            for m in shared:
                if _get_skin_cluster(m) and m not in seen:
                    seen.add(m)
                    target_meshes.append(m)

        if not target_meshes:
            self._set_status(
                lang.Lang(
                    en="No skinned meshes found for the current selection or saved joint.",
                    ja=u"選択対象または記憶ジョイントに紐づくスキニング済みメッシュが見つかりませんでした。",
                ).output(),
                error=True,
            )
            return

        # 2 回目以降に未選択でも開始できるよう、今回使用したジョイントを一時保存する
        joints_to_remember = []
        if sel_joints and not used_cached_joints:
            joints_to_remember = list(sel_joints)
        elif not used_cached_joints:
            for mesh_long in target_meshes:
                sc = _get_skin_cluster(mesh_long)
                if sc:
                    infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], type="joint", long=True) or []
                    if infs:
                        joints_to_remember = [infs[0]]
                        break
        if joints_to_remember:
            confirmed_remember = self._confirm_non_hips_saved_joint(joints_to_remember, is_using_cached=False)
            if confirmed_remember is None:
                self._set_status(
                    lang.Lang(
                        en="Cancelled because joint to save is not '[CharacterName]_Hips'.",
                        ja=u"記憶ジョイントが [キャラ名]_Hips ではないため開始を中止しました。",
                    ).output(),
                    error=True,
                )
                return
            self._cached_joints = _save_last_seed_joints(confirmed_remember)
            self._update_cached_joint_ui()

        saved_frame = None
        edit_frame = None
        bind_pose_skipped_reason = None
        drifted_bones = []
        saved_paths = []
        started_names = []

        prev_autokey = self._disable_auto_keyframe_temporarily()
        try:
            if self.restore_frame_chk.isChecked():
                try:
                    saved_frame = float(cmds.currentTime(query=True))
                    edit_frame = float(self.edit_frame_spin.value())
                    self._saved_frame = saved_frame
                    if abs(saved_frame - edit_frame) > 1e-6:
                        cmds.currentTime(edit_frame, edit=True)
                except Exception as exc:
                    print("ModelEdit frame switch warning:", exc)

            cmds.undoInfo(openChunk=True, chunkName="SIWE_StartModelEdit")
            try:
                if self.bind_pose_chk.isChecked():
                    pose_info = self._inspect_skeleton_rotation_state(target_meshes, edit_frame)
                    # 基準フレームに回転キーがある、または既に全ボーン Rotate=(0,0,0) の場合は古い dagPose による回転汚染を防ぐためスキップする
                    if edit_frame is not None and (pose_info["keyed_at_edit_frame"] > 0 or pose_info["all_zero_rot"]):
                        if pose_info["keyed_at_edit_frame"] > 0:
                            bind_pose_skipped_reason = lang.Lang(
                                en="Skipped gotoBindPose (frame {0} has rotation keyframes on {1} joint(s); protected from dagPose drift)",
                                ja=u"gotoBindPose をスキップ（{0} フレームに回転キー {1} 本を検出：古い dagPose による回転混入を防止）",
                            ).output().format(_format_frame_value(edit_frame), pose_info["keyed_at_edit_frame"])
                        else:
                            bind_pose_skipped_reason = lang.Lang(
                                en="Skipped gotoBindPose (all joints already at Rotate=(0,0,0) on frame {0})",
                                ja=u"gotoBindPose をスキップ（{0} フレームの全ボーン回転が既に 0,0,0 の A ポーズのため保護）",
                            ).output().format(_format_frame_value(edit_frame))
                    else:
                        try:
                            cmds.select(target_meshes, replace=True)
                            mel.eval("gotoBindPose;")
                            drifted_bones = self._sanitize_rotations_after_bind_pose(pose_info["pre_rot_map"])
                        except Exception:
                            pass
                cmds.selectMode(object=True)
                for mesh_long in target_meshes:
                    save_path, payload = export_mesh_skin_json(
                        mesh_long,
                        saved_frame=saved_frame,
                        edit_frame=edit_frame,
                    )
                    _detach_and_freeze_for_modeling(mesh_long)
                    saved_paths.append(save_path)
                    started_names.append("{0}({1}vtx)".format(payload["mesh_short"], payload["vertex_count"]))
                cmds.select(target_meshes, replace=True)
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Error: {0}", ja=u"エラー: {0}").output().format(exc),
                    error=True,
                )
                raise
            finally:
                cmds.undoInfo(closeChunk=True)
        finally:
            self._restore_auto_keyframe_state(prev_autokey)

        self.reload_record_list(select_paths=saved_paths)
        short_msg = lang.Lang(
            en="Ready ({0} mesh(es)): Weights recorded & unbound.",
            ja=u"修正開始（{0} メッシュ）：ウェイト記録・解除完了。",
        ).output().format(len(started_names))
        if used_cached_joints:
            short_msg += lang.Lang(
                en=" | From saved joint",
                ja=u" ｜ 記憶ジョイント使用",
            ).output()
        if saved_frame is not None and edit_frame is not None:
            short_msg += lang.Lang(
                en=" | Frame: {0} -> {1}",
                ja=u" ｜ フレーム: {0} → {1}",
            ).output().format(_format_frame_value(saved_frame), _format_frame_value(edit_frame))
        detail_lines = []
        if used_cached_joints and self._cached_joints:
            detail_lines.append(
                lang.Lang(
                    en="Auto-started from saved joint: {0}",
                    ja=u"記憶ジョイントから自動開始: {0}",
                ).output().format(", ".join(j.split("|")[-1] for j in self._cached_joints[:5]))
            )
        if saved_frame is not None and edit_frame is not None:
            detail_lines.append(
                lang.Lang(
                    en="Frame switched: {0} -> {1} (will restore to {0} on finish)",
                    ja=u"フレーム移動: {0} → {1}（完了後に {0} へ復帰）",
                ).output().format(
                    _format_frame_value(saved_frame), _format_frame_value(edit_frame)
                )
            )
        if bind_pose_skipped_reason:
            detail_lines.append(bind_pose_skipped_reason)
        if drifted_bones:
            detail_lines.append(
                lang.Lang(
                    en="Warning: gotoBindPose changed rotation on {0} joint(s): {1}",
                    ja=u"注意: gotoBindPose により回転が変化したボーン ({0} 本): {1}",
                ).output().format(
                    len(drifted_bones),
                    ", ".join("{0}({1:.2f},{2:.2f},{3:.2f})".format(n, r[0], r[1], r[2]) for n, r in drifted_bones[:8]),
                )
            )
        detail_lines.append(lang.Lang(en="Target meshes:", ja=u"対象メッシュ一覧:").output())
        for item_name in started_names:
            detail_lines.append("  " + item_name)
        detail_msg = "\n".join(detail_lines)
        self._set_status(short_msg, error=False, detail=detail_msg)
        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
            except Exception:
                pass

    def select_recorded_mesh(self):
        """一覧で選択中の記録に対応するシーン内メッシュを選択する。"""
        recs = self._get_selected_records()
        if not recs:
            return
        found = []
        missing = []
        for rec in recs:
            target = rec["mesh_long"] if cmds.objExists(rec["mesh_long"]) else rec["mesh_short"]
            if cmds.objExists(target):
                found.append(target)
            else:
                missing.append(rec["mesh_short"])
        if found:
            cmds.select(found, replace=True)
            self._set_status(
                lang.Lang(en="Selected {0} mesh(es).", ja=u"{0} 件のメッシュを選択しました。").output().format(len(found)),
                error=False,
            )
        else:
            self._set_status(
                lang.Lang(en="Mesh not found in scene: {0}", ja=u"シーン内にメッシュが見つかりません: {0}").output().format(", ".join(missing)),
                error=True,
            )

    def delete_selected_record(self):
        """一覧で選択中の JSON バックアップファイルを削除する。"""
        recs = self._get_selected_records()
        if not recs:
            return
        deleted = 0
        try:
            for rec in recs:
                if os.path.exists(rec["path"]):
                    os.remove(rec["path"])
                    deleted += 1
            self.reload_record_list()
            self._set_status(
                lang.Lang(en="Deleted {0} backup(s).", ja=u"バックアップを {0} 件削除しました。").output().format(deleted),
                error=False,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Delete error: {0}", ja=u"削除エラー: {0}").output().format(exc),
                error=True,
            )

    def finish_modeling_selected(self, use_viewport_mesh=False):
        """修正後のメッシュをフリーズ・再バインドし、JSON からウェイトを復元・補間適用する。"""
        pairs = []
        missing_meshes = []

        if use_viewport_mesh:
            recs = self._get_selected_records()
            if not recs:
                self._set_status(
                    lang.Lang(
                        en="Select a recorded backup entry from the list.",
                        ja=u"適用する記録データを一覧から選択してください。",
                    ).output(),
                    error=True,
                )
                return
            meshes = self._get_selected_polygon_transforms()
            if not meshes:
                self._set_status(
                    lang.Lang(en="Select a target mesh in the viewport.", ja=u"適用先のメッシュをビューポートで選択してください。").output(),
                    error=True,
                )
                return
            pairs.append((recs[0], meshes[0]))
        else:
            # 修正完了ボタンでは一覧に記録されている全メッシュ（シーン内に存在するもの）を一括復元する
            self.reload_record_list()
            if not self._records:
                self._set_status(
                    lang.Lang(
                        en="No recorded JSON backups found.",
                        ja=u"復元可能な JSON バックアップが一覧にありません。",
                    ).output(),
                    error=True,
                )
                return
            for rec in self._records:
                if cmds.objExists(rec["mesh_long"]):
                    pairs.append((rec, rec["mesh_long"]))
                elif cmds.objExists(rec["mesh_short"]):
                    found = cmds.ls(rec["mesh_short"], long=True) or [rec["mesh_short"]]
                    pairs.append((rec, found[0]))
                else:
                    missing_meshes.append(rec["mesh_short"])
            if not pairs:
                self._set_status(
                    lang.Lang(
                        en="No recorded meshes exist in the current scene ({0} skipped).",
                        ja=u"現在のシーンに該当するメッシュが見つかりませんでした（未検出: {0} 件）。",
                    ).output().format(len(missing_meshes)),
                    error=True,
                    detail=lang.Lang(en="Missing meshes:\n  ", ja=u"未検出メッシュ:\n  ").output() + "\n  ".join(missing_meshes),
                )
                return

        restore_mode = self.restore_mode_combo.currentData() or "auto"
        smooth_iters = int(self.smooth_spin.value())
        clean_bind_pose = self.clean_bind_pose_chk.isChecked()
        use_frame_restore = self.restore_frame_chk.isChecked()
        edit_frame = float(self.edit_frame_spin.value())

        restored_paths = []
        restored_meshes = []
        summaries = []
        total_missing_bones = 0
        max_moved_joints = 0
        saved_frame_to_restore = None
        restored_frame_val = None

        prev_autokey = self._disable_auto_keyframe_temporarily()
        try:
            # 再バインドは必ず基準（0）フレーム上で実行し、仮ポーズのままバインドされる事故を防ぐ
            if use_frame_restore:
                try:
                    cur_frame = float(cmds.currentTime(query=True))
                    if abs(cur_frame - edit_frame) > 1e-6:
                        cmds.currentTime(edit_frame, edit=True)
                except Exception as exc:
                    print("ModelEdit pre-restore frame switch warning:", exc)

            cmds.undoInfo(openChunk=True, chunkName="SIWE_FinishModelEdit")
            try:
                cmds.selectMode(object=True)
                for rec, target_mesh in pairs:
                    with open(rec["path"], "r") as fp:
                        payload = json.load(fp)
                    if saved_frame_to_restore is None and payload.get("saved_frame") is not None:
                        saved_frame_to_restore = float(payload["saved_frame"])
                    result = finish_modeling_and_restore(
                        target_mesh,
                        payload,
                        restore_mode=restore_mode,
                        smooth_iters=smooth_iters,
                        clean_orphaned_bind_pose=clean_bind_pose,
                    )
                    payload["status"] = "restored"
                    with open(rec["path"], "w") as fp:
                        json.dump(payload, fp)

                    restored_paths.append(rec["path"])
                    restored_meshes.append(target_mesh)
                    total_missing_bones += len(result["missing_infs"])
                    max_moved_joints = max(max_moved_joints, int(result.get("moved_joints", 0)))

                    m_tag = u"Idx" if result["method"] == "index" else u"Surf"
                    summaries.append(
                        "{0} ({1}->{2} / {3})".format(
                            target_mesh.split("|")[-1],
                            result["orig_vtx_count"],
                            result["new_vtx_count"],
                            m_tag,
                        )
                    )
                if restored_meshes:
                    cmds.select(restored_meshes, replace=True)
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Restore error: {0}", ja=u"復元エラー: {0}").output().format(exc),
                    error=True,
                )
                raise
            finally:
                cmds.undoInfo(closeChunk=True)

            # 全メッシュのウェイト復元完了後、事前に記録した元フレームへ復帰する
            if use_frame_restore:
                if saved_frame_to_restore is None:
                    saved_frame_to_restore = self._saved_frame
                if saved_frame_to_restore is not None:
                    try:
                        cmds.currentTime(saved_frame_to_restore, edit=True)
                        restored_frame_val = saved_frame_to_restore
                    except Exception as exc:
                        print("ModelEdit restore frame warning:", exc)
        finally:
            self._restore_auto_keyframe_state(prev_autokey)

        self.reload_record_list(select_paths=restored_paths)

        short_msg = lang.Lang(
            en="Restored {0} mesh(es)",
            ja=u"復元完了（{0} メッシュ）",
        ).output().format(len(summaries))
        if restored_frame_val is not None:
            short_msg += lang.Lang(
                en=" | Frame: {0}",
                ja=u" ｜ フレーム復帰: {0}",
            ).output().format(_format_frame_value(restored_frame_val))
        if max_moved_joints > 0:
            short_msg += lang.Lang(
                en=" | Moved bones: {0}",
                ja=u" ｜ 位置変更ボーン: {0} 本",
            ).output().format(max_moved_joints)
        if missing_meshes:
            short_msg += lang.Lang(
                en=" | Skipped: {0}",
                ja=u" ｜ シーン外スキップ: {0} 件",
            ).output().format(len(missing_meshes))
        if total_missing_bones > 0:
            short_msg += lang.Lang(
                en=" | Missing bones: {0}",
                ja=u" ｜ 未検出ボーン: {0}",
            ).output().format(total_missing_bones)

        detail_lines = []
        if restored_frame_val is not None:
            detail_lines.append(
                lang.Lang(
                    en="Frame restored: {0} -> {1}",
                    ja=u"フレーム復帰: {0} → {1}",
                ).output().format(
                    _format_frame_value(edit_frame), _format_frame_value(restored_frame_val)
                )
            )
        detail_lines.append(
            lang.Lang(
                en="Restored meshes ({0}):",
                ja=u"復元メッシュ一覧 ({0} 件):",
            ).output().format(len(summaries))
        )
        for item in summaries:
            detail_lines.append("  " + item)
        if missing_meshes:
            detail_lines.append(
                lang.Lang(
                    en="Skipped (not found in scene) ({0}):",
                    ja=u"シーン内に存在しないためスキップ ({0} 件):",
                ).output().format(len(missing_meshes))
            )
            for name in missing_meshes:
                detail_lines.append("  " + name)
        detail_msg = "\n".join(detail_lines)

        self._set_status(short_msg, error=False, detail=detail_msg)
        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
                self.editor.set_message(msg="- " + short_msg + " -", error=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # タブ 2 ハンドラ：ジョイント整理 ＆ Joint Orient
    # ------------------------------------------------------------------
    def _install_radius_selection_job(self):
        """ビューポート選択変更時に選択ジョイントの Radius 値を自動同期する scriptJob を登録する。"""
        self._remove_radius_selection_job()
        try:
            self._radius_script_job_id = cmds.scriptJob(
                event=["SelectionChanged", self._on_viewport_selection_changed_for_radius],
                protected=True,
            )
        except Exception:
            self._radius_script_job_id = None

    def _remove_radius_selection_job(self):
        """登録済みの選択監視 scriptJob を安全に解除する。"""
        job_id = getattr(self, "_radius_script_job_id", None)
        if job_id is not None:
            try:
                if cmds.scriptJob(exists=job_id):
                    cmds.scriptJob(kill=job_id, force=True)
            except Exception:
                pass
            self._radius_script_job_id = None

    def _on_viewport_selection_changed_for_radius(self):
        """Maya の SelectionChanged 通知を受けて Radius UI を現在選択へ同期する。"""
        if self._radius_undo_open:
            return
        try:
            self._sync_radius_from_selection(show_status=False)
        except Exception:
            pass

    def _get_radius_target_joints(self):
        """Radius 調整の対象となるビューポート選択ジョイント一覧を返す。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            return []
        if hasattr(self, "jnt_radius_children_chk") and self.jnt_radius_children_chk.isChecked():
            return rig_cleanup_helper.collect_target_joints(include_children=True)
        return sel_joints

    def _sync_radius_from_selection(self, show_status=False):
        """現在ビューポートで選択中のジョイントから Radius 値を読み取り UI へ反映する。"""
        if not hasattr(self, "jnt_radius_spin"):
            return
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self.jnt_radius_info_label.setText(
                lang.Lang(
                    en="Viewport Selection: No joint selected",
                    ja=u"ビューポート選択: ジョイント未選択",
                ).output()
            )
            if show_status:
                self._set_status(
                    lang.Lang(
                        en="Select one or more joints in the viewport first.",
                        ja=u"Radius を取得するジョイントをビューポートで選択してください。",
                    ).output(),
                    error=True,
                )
            return

        radii = []
        for jnt in sel_joints:
            if cmds.attributeQuery("radius", node=jnt, exists=True):
                try:
                    radii.append(float(cmds.getAttr(jnt + ".radius")))
                except Exception:
                    pass
        if not radii:
            return

        val = max(0.01, min(1000.0, radii[0]))
        is_mixed = any(abs(r - radii[0]) > 1e-4 for r in radii[1:])

        self._radius_syncing = True
        try:
            self.jnt_radius_spin.setValue(val)
            slider_val = int(round(max(0.01, min(10.0, val)) * 100.0))
            self.jnt_radius_slider.setValue(slider_val)
        finally:
            self._radius_syncing = False

        head = sel_joints[0].split("|")[-1]
        if len(sel_joints) == 1:
            info_txt = lang.Lang(
                en="Viewport Selection: {0} (Radius: {1:.2f})",
                ja=u"ビューポート選択: {0}（Radius: {1:.2f}）",
            ).output().format(head, val)
        else:
            mixed_tag = lang.Lang(en="mixed", ja=u"混在").output() if is_mixed else "{0:.2f}".format(val)
            info_txt = lang.Lang(
                en="Viewport Selection: {0} joints ({1} ... | Radius: {2})",
                ja=u"ビューポート選択: {0} 本（{1} ほか ｜ Radius: {2}）",
            ).output().format(len(sel_joints), head, mixed_tag)
        self.jnt_radius_info_label.setText(info_txt)

        if show_status:
            self._set_status(info_txt, error=False)

    def _apply_radius_to_selection(self, radius_value):
        """指定した Radius 値をビューポート選択ジョイントへリアルタイム反映する。"""
        joints = self._get_radius_target_joints()
        if not joints:
            self.jnt_radius_info_label.setText(
                lang.Lang(
                    en="Viewport Selection: No joint selected",
                    ja=u"ビューポート選択: ジョイント未選択",
                ).output()
            )
            return

        val = max(0.01, float(radius_value))
        updated = 0
        for jnt in joints:
            if not cmds.objExists(jnt):
                continue
            attr = jnt + ".radius"
            try:
                if cmds.getAttr(attr, settable=True):
                    cmds.setAttr(attr, val)
                    updated += 1
            except Exception:
                pass

        head = joints[0].split("|")[-1]
        if len(joints) == 1:
            self.jnt_radius_info_label.setText(
                lang.Lang(
                    en="Adjusted: {0} -> Radius {1:.2f}",
                    ja=u"適用中: {0} → Radius {1:.2f}",
                ).output().format(head, val)
            )
        else:
            self.jnt_radius_info_label.setText(
                lang.Lang(
                    en="Adjusted: {0} joint(s) -> Radius {1:.2f}",
                    ja=u"適用中: {0} 本のジョイント → Radius {1:.2f}",
                ).output().format(updated, val)
            )

    def _on_radius_slider_pressed(self):
        """スライダードラッグ開始時に 1 つの Undo チャンクを開く。"""
        if not self._radius_undo_open:
            try:
                cmds.undoInfo(openChunk=True, chunkName="SIWE_JointRadius")
                self._radius_undo_open = True
            except Exception:
                self._radius_undo_open = False

    def _on_radius_slider_changed(self, int_val):
        """スライダー移動に応じて数値入力とビューポート選択ジョイントの Radius をリアルタイム更新する。"""
        if self._radius_syncing:
            return
        val = max(0.01, float(int_val) / 100.0)
        self._radius_syncing = True
        try:
            self.jnt_radius_spin.setValue(val)
        finally:
            self._radius_syncing = False
        if self._radius_undo_open:
            self._apply_radius_to_selection(val)
        else:
            with qt.undo_chunk("SIWE_JointRadius"):
                self._apply_radius_to_selection(val)

    def _on_radius_slider_released(self):
        """スライダードラッグ終了時に Undo チャンクを閉じる。"""
        if self._radius_undo_open:
            try:
                cmds.undoInfo(closeChunk=True)
            except Exception:
                pass
            self._radius_undo_open = False

    def _on_radius_spin_changed(self, val):
        """数値入力変更に応じてスライダーと選択ジョイントの Radius をリアルタイム更新する。"""
        if self._radius_syncing:
            return
        self._radius_syncing = True
        try:
            slider_val = int(round(max(0.01, min(10.0, float(val))) * 100.0))
            self.jnt_radius_slider.setValue(slider_val)
        finally:
            self._radius_syncing = False

        if self._radius_undo_open:
            self._apply_radius_to_selection(val)
        else:
            cmds.undoInfo(openChunk=True, chunkName="SIWE_JointRadius")
            try:
                self._apply_radius_to_selection(val)
            finally:
                cmds.undoInfo(closeChunk=True)

    def _update_orient_controls_enabled(self, world_checked):
        for widget in self._orient_axis_widgets:
            widget.setEnabled(not world_checked)
        self.oj_zero_end_chk.setEnabled(not world_checked)

    def _get_selected_axis(self, radio_dict, default_val):
        for key, rb in radio_dict.items():
            if rb.isChecked():
                return key
        return default_val

    def _on_primary_axis_changed(self):
        p_ax = self._get_selected_axis(self.primary_radios, "X")
        s_ax = self._get_selected_axis(self.secondary_radios, "Y")
        if p_ax == s_ax:
            # 主軸と副軸が重複しないよう自動で別軸へ切り替える
            for candidate in ("Y", "Z", "X"):
                if candidate != p_ax:
                    self.secondary_radios[candidate].setChecked(True)
                    break

    def _on_secondary_axis_changed(self):
        s_ax = self._get_selected_axis(self.secondary_radios, "Y")
        world_enabled = (not self.oj_world_chk.isChecked()) and (s_ax != "None")
        for rb in self.world_axis_radios.values():
            rb.setEnabled(world_enabled)
        self.world_sign_combo.setEnabled(world_enabled)

        p_ax = self._get_selected_axis(self.primary_radios, "X")
        if s_ax == p_ax and s_ax != "None":
            for candidate in ("X", "Y", "Z"):
                if candidate != s_ax:
                    self.primary_radios[candidate].setChecked(True)
                    break

    def _populate_joint_issue_table(self, issues):
        self._joint_issues = issues
        self.joint_issue_table.setRowCount(len(issues))
        for idx, info in enumerate(issues):
            j_item = QTableWidgetItem(info["short_name"])
            j_item.setToolTip(info["joint"])

            s_item = QTableWidgetItem(info["summary"])
            if info["bad_rot"] or info["bad_scl"] or info["bad_ra"] or info.get("has_conn") or info.get("has_lock"):
                s_item.setForeground(QColor(255, 190, 95))
            else:
                s_item.setForeground(QColor(175, 210, 255))

            r = info["rotate"]
            s = info["scale"]
            r_item = QTableWidgetItem("{0:.2f}, {1:.2f}, {2:.2f}".format(r[0], r[1], r[2]))
            scl_item = QTableWidgetItem("{0:.3f}, {1:.3f}, {2:.3f}".format(s[0], s[1], s[2]))

            self.joint_issue_table.setItem(idx, 0, j_item)
            self.joint_issue_table.setItem(idx, 1, s_item)
            self.joint_issue_table.setItem(idx, 2, r_item)
            self.joint_issue_table.setItem(idx, 3, scl_item)

    def _on_joint_issue_clicked(self, item):
        row = item.row()
        if 0 <= row < len(self._joint_issues):
            jnt = self._joint_issues[row]["joint"]
            if cmds.objExists(jnt):
                cmds.select(jnt, replace=True)

    def inspect_selected_joints(self):
        """選択ジョイント（必要に応じて子階層含む）の Rotate / Scale 等を診断して一覧へ表示する。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"診断対象のジョイントを選択してください。").output(),
                error=True,
            )
            return

        issues = rig_cleanup_helper.inspect_joints(joints)
        self._populate_joint_issue_table(issues)

        rot_cnt = sum(1 for i in issues if i["bad_rot"] or i["bad_ra"])
        scl_cnt = sum(1 for i in issues if i["bad_scl"])
        if not issues:
            self._set_status(
                lang.Lang(
                    en="Checked {0} joint(s): All clean (Rotate=0, Scale=1).",
                    ja=u"{0} 本のジョイントを診断: 異常なし（Rotate=0 / Scale=1）",
                ).output().format(len(joints)),
                error=False,
            )
        else:
            self._set_status(
                lang.Lang(
                    en="Checked {0} joint(s): {1} item(s) detected (Rotate/Axis: {2}, Scale: {3}).",
                    ja=u"{0} 本を診断: {1} 件検出（Rotate/Axis 非ゼロ: {2} / Scale 非1: {3}）",
                ).output().format(len(joints), len(issues), rot_cnt, scl_cnt),
                error=True,
            )

    def select_joint_issues(self):
        """診断テーブルで検出された問題ジョイント（または選択行）をシーン内で選択する。"""
        if not self._joint_issues:
            self._set_status(
                lang.Lang(en="No issue joints in list.", ja=u"一覧に検出されたジョイントがありません。").output(),
                error=True,
            )
            return
        rows = sorted(set(idx.row() for idx in self.joint_issue_table.selectedIndexes()))
        if rows:
            targets = [self._joint_issues[r]["joint"] for r in rows if 0 <= r < len(self._joint_issues)]
        else:
            targets = [info["joint"] for info in self._joint_issues]
        targets = [j for j in targets if cmds.objExists(j)]
        if targets:
            cmds.select(targets, replace=True)
            self._set_status(
                lang.Lang(en="Selected {0} joint(s).", ja=u"{0} 本のジョイントを選択しました。").output().format(len(targets)),
                error=False,
            )

    def run_fix_joint_rotations(self):
        """選択ジョイントの Rotate / RotateAxis を jointOrient へ焼き込んで 0 に整理する。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"対象のジョイントを選択してください。").output(),
                error=True,
            )
            return
        if not self._confirm_skinned_joints_edit(joints):
            return

        cmds.undoInfo(openChunk=True)
        try:
            fixed, skipped = rig_cleanup_helper.freeze_joint_rotations_to_orient(joints)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._populate_joint_issue_table(rig_cleanup_helper.inspect_joints(joints))
        self._set_status(
            lang.Lang(
                en="Baked Rotate -> jointOrient on {0} joint(s) (skipped/locked: {1}).",
                ja=u"Rotate → 0（Orient 焼込）完了: {0} 本整理（ロック等でスキップ: {1} 本）",
            ).output().format(len(fixed), len(skipped)),
            error=False,
        )

    def run_fix_joint_scales(self):
        """子位置を維持したまま選択ジョイントの Scale を (1, 1, 1) に整理する。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"対象のジョイントを選択してください。").output(),
                error=True,
            )
            return
        if not self._confirm_skinned_joints_edit(joints):
            return

        cmds.undoInfo(openChunk=True)
        try:
            fixed, skipped = rig_cleanup_helper.normalize_joint_scales(joints)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._populate_joint_issue_table(rig_cleanup_helper.inspect_joints(joints))
        self._set_status(
            lang.Lang(
                en="Normalized Scale -> (1, 1, 1) on {0} joint(s) (skipped/locked: {1}).",
                ja=u"Scale → 1（子位置維持）完了: {0} 本整理（ロック等でスキップ: {1} 本）",
            ).output().format(len(fixed), len(skipped)),
            error=False,
        )

    def run_fix_joint_all(self):
        """Scale -> 1 と Rotate -> 0（Orient 焼込）を一括で整理する。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"対象のジョイントを選択してください。").output(),
                error=True,
            )
            return
        if not self._confirm_skinned_joints_edit(joints):
            return

        cmds.undoInfo(openChunk=True)
        try:
            scl_fixed, scl_skipped = rig_cleanup_helper.normalize_joint_scales(joints)
            rot_fixed, rot_skipped = rig_cleanup_helper.freeze_joint_rotations_to_orient(joints)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._populate_joint_issue_table(rig_cleanup_helper.inspect_joints(joints))
        self._set_status(
            lang.Lang(
                en="Cleaned joints: Scale fixed={0}, Rotate baked={1} (skipped: {2}).",
                ja=u"ジョイント一括整理完了: Scale 修正 {0} 本 / Rotate 焼込 {1} 本（スキップ: {2}）",
            ).output().format(len(scl_fixed), len(rot_fixed), len(scl_skipped) + len(rot_skipped)),
            error=False,
        )

    def run_orient_joints(self):
        """Orient Joint Options 準拠の設定で選択ジョイントへ方向付けを実行する。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(en="Select one or more joints to orient.", ja=u"Orient Joint を適用するジョイントを選択してください。").output(),
                error=True,
            )
            return

        orient_children = self.oj_children_chk.isChecked()
        check_joints = rig_cleanup_helper.collect_target_joints(include_children=orient_children)
        if not self._confirm_skinned_joints_edit(check_joints):
            return

        world_mode = self.oj_world_chk.isChecked()
        primary = self._get_selected_axis(self.primary_radios, "X")
        secondary = self._get_selected_axis(self.secondary_radios, "Y")
        world_ax = self._get_selected_axis(self.world_axis_radios, "Z")
        world_sign = self.world_sign_combo.currentText() or "-"
        reorient_scale = self.oj_scale_chk.isChecked()
        zero_end = self.oj_zero_end_chk.isChecked()

        cmds.undoInfo(openChunk=True)
        try:
            res = rig_cleanup_helper.execute_orient_joints(
                sel_joints,
                orient_to_world=world_mode,
                primary_axis=primary,
                secondary_axis=secondary,
                secondary_world_axis=world_ax,
                secondary_world_sign=world_sign,
                orient_children=orient_children,
                reorient_scale=reorient_scale,
                zero_end_joints=zero_end,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Orient Joint error: {0}", ja=u"Orient Joint エラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        self._populate_joint_issue_table(rig_cleanup_helper.inspect_joints(check_joints))
        if world_mode:
            mode_desc = "World"
        else:
            mode_desc = "Pri:{0} / Sec:{1} / World:{2}{3}".format(primary, secondary, world_ax, world_sign)

        self._set_status(
            lang.Lang(
                en="Orient Joint applied ({0}): {1} joint(s), end zeroed: {2}.",
                ja=u"Orient Joint 実行完了（{0}）: 対象 {1} 本 / 末端 0 揃え {2} 本",
            ).output().format(mode_desc, res["processed"], res["end_zeroed"]),
            error=False,
        )

    def run_toggle_local_axes(self):
        """選択ジョイントのローカル回転軸表示（displayLocalAxis）をトグルする。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"ローカル軸を切り替えるジョイントを選択してください。").output(),
                error=True,
            )
            return

        with qt.undo_chunk("SIWE_ToggleLocalAxes"):
            state = rig_cleanup_helper.toggle_local_axes_visibility(joints)
        label = "ON" if state else "OFF"
        self._set_status(
            lang.Lang(
                en="Local Rotation Axes: {0} ({1} joint(s)).",
                ja=u"ローカル回転軸表示を {0} に切り替えました（{1} 本）",
            ).output().format(label, len(joints)),
            error=False,
        )

    def run_freeze_selected_bone_rot_keep_keys(self):
        """選択ボーンの回転 XYZ を現在値でフリーズして 0 へ戻し、選択ボーンと子ボーンのキーフレームを退避・書き戻す。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Select one or more joints to freeze rotation.",
                    ja=u"回転をフリーズするジョイントを選択してください。",
                ).output(),
                error=True,
            )
            return

        base_frame = float(self.freeze_rot_base_frame_spin.value())
        include_children = self.freeze_rot_children_chk.isChecked()

        with qt.undo_chunk("SIWE_FreezeBoneRotKeepKeys"):
            try:
                res = rig_cleanup_helper.freeze_selected_bone_rotation_preserving_keyframes(
                    selected_joints=sel_joints,
                    base_storage_dir=_get_storage_dir(),
                    base_frame=base_frame,
                    include_children_in_freeze=include_children,
                )
            except Exception as exc:
                self._set_status(
                    lang.Lang(
                        en="Bone Rotation Freeze error: {0}",
                        ja=u"ボーン回転フリーズエラー: {0}",
                    ).output().format(exc),
                    error=True,
                )
                raise

        short_msg = lang.Lang(
            en="Frozen Rotate -> 0 on {0} joint(s) (Base F={1} | Keys restored: {2} keys on {3} joint(s)).",
            ja=u"回転フリーズ完了: {0} 本を 0 へ固定（基準F={1} ｜ キー復元: {3} 本 / {2} キー）",
        ).output().format(
            len(res["frozen_joints"]),
            _format_frame_value(res["base_frame"]),
            res["restored_keys"],
            res["restored_joints"],
        )

        detail_lines = [
            lang.Lang(
                en="Base Frame: {0} (frozen joints set to 0 on base frame, frames after base frame restored verbatim)",
                ja=u"基準フレーム: {0}（フリーズ対象の基準フレーム回転を 0 に固定、それ以降のキー値はそのまま復元）",
            ).output().format(_format_frame_value(res["base_frame"])),
            lang.Lang(
                en="Frozen joints ({0}): {1}",
                ja=u"回転フリーズ対象 ({0} 本): {1}",
            ).output().format(
                len(res["frozen_joints"]),
                ", ".join(j.split("|")[-1] for j in res["frozen_joints"]),
            ),
            lang.Lang(
                en="Child joints keys preserved ({0}): {1}",
                ja=u"キー退避・保護した子ボーン ({0} 本): {1}",
            ).output().format(
                len(res["child_joints"]),
                ", ".join(j.split("|")[-1] for j in res["child_joints"][:20])
                + (u" ..." if len(res["child_joints"]) > 20 else u""),
            ),
            lang.Lang(
                en="Restored keyframes: {0} keys across {1} joint(s) (base-frame zeroed channels: {2})",
                ja=u"書き戻しキー数: {1} 本 / 合計 {0} キー（基準フレーム 0 固定チャネル: {2}）",
            ).output().format(
                res["restored_keys"],
                res["restored_joints"],
                res["zeroed_base_keys"],
            ),
        ]
        if res.get("skipped_joints"):
            detail_lines.append(
                lang.Lang(
                    en="Skipped joints ({0}): {1}",
                    ja=u"スキップしたジョイント ({0} 本): {1}",
                ).output().format(
                    len(res["skipped_joints"]),
                    ", ".join(j.split("|")[-1] for j in res["skipped_joints"]),
                )
            )
        if res.get("backup_path"):
            detail_lines.append(
                lang.Lang(
                    en="Keyframe backup JSON: {0}",
                    ja=u"キーフレーム退避 JSON: {0}",
                ).output().format(res["backup_path"])
            )

        self._set_status(short_msg, error=False, detail="\n".join(detail_lines))

    def run_tweak_orient(self, axis, degrees):
        """選択ジョイント自身のローカル軸を子位置維持で回転微調整する。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(en="Select joint(s) to tweak local axis.", ja=u"軸を微調整したいジョイントを選択してください。").output(),
                error=True,
            )
            return
        if not self._confirm_skinned_joints_edit(sel_joints):
            return

        cmds.undoInfo(openChunk=True)
        try:
            updated = rig_cleanup_helper.tweak_joint_orient_axis(sel_joints, axis, degrees)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._set_status(
            lang.Lang(
                en="Tweaked local axis {0} by {1} deg on {2} joint(s).",
                ja=u"ローカル軸 {0} を {1}° 回転補正しました（対象: {2} 本 / 子位置維持）",
            ).output().format(axis, degrees, len(updated)),
            error=False,
        )

    def open_unweighted_bind_window(self):
        """ウェイトが入っていないメッシュ一覧と割り当て先ボーン一覧の専用ウィンドウを開く。"""
        if self.unweighted_bind_window is not None:
            try:
                self.unweighted_bind_window.refresh_lists(apply_viewport_selection=True)
                self.unweighted_bind_window.show()
                self.unweighted_bind_window.raise_()
                self.unweighted_bind_window.activateWindow()
                return
            except Exception:
                self.unweighted_bind_window = None

        self.unweighted_bind_window = unweighted_bind_window.UnweightedMeshBindWindow(
            owner_window=self,
            editor_window=self.editor,
        )
        self.unweighted_bind_window.show()

    def run_safe_bind_selected_joints(self):
        """選択ジョイント（必要に応じて子階層含む）を既存ウェイトを維持したまま安全にバインドする。"""
        include_children = self.safe_bind_children_chk.isChecked()
        target_joints = rig_cleanup_helper.collect_target_joints(include_children=include_children)
        if not target_joints:
            self._set_status(
                lang.Lang(
                    en="Select one or more joints to bind (mesh selection is optional).",
                    ja=u"バインドするジョイントをビューポートまたはアウトライナーで選択してください（メッシュは未選択でも自動検出されます）。",
                ).output(),
                error=True,
            )
            return

        selected_meshes = self._get_selected_polygon_transforms()
        mode = self.safe_bind_mode_combo.currentData() or "mirror_opposite"
        unlock_added = self.safe_bind_unlock_chk.isChecked()

        with qt.undo_chunk("SIWE_SafeBindJoints"):
            try:
                res = rig_cleanup_helper.safe_bind_joints_to_meshes(
                    target_joints=target_joints,
                    target_meshes=selected_meshes or None,
                    mode=mode,
                    unlock_added=unlock_added,
                )
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Safe Bind error: {0}", ja=u"安全バインドエラー: {0}").output().format(exc),
                    error=True,
                )
                return

        added_cnt = len(res["added_influences"])
        vtx_cnt = int(res["weighted_vertices"])
        mesh_cnt = len(res["meshes"])
        pair_cnt = len(res["matched_pairs"])
        new_mesh_cnt = len(res["newly_bound_meshes"])

        if new_mesh_cnt > 0 and added_cnt == 0 and vtx_cnt == 0:
            short_msg = lang.Lang(
                en="Bound {0} joint(s) to {1} unbound mesh(es).",
                ja=u"新規バインド完了 ｜ ジョイント:{0} 本 / メッシュ:{1} 件",
            ).output().format(len(res["target_joints"]), new_mesh_cnt)
        elif mode == "mirror_opposite":
            short_msg = lang.Lang(
                en="Safe Bind (Mirror) done | Meshes:{0} AddedInf:{1} Pairs:{2} MirroredVtx:{3}",
                ja=u"安全バインド（ミラー）完了 ｜ メッシュ:{0} / 追加:{1} 本 / 左右対:{2} / 転写頂点:{3}",
            ).output().format(mesh_cnt, added_cnt, pair_cnt, vtx_cnt)
        elif mode == "auto_bind_new":
            short_msg = lang.Lang(
                en="Safe Bind (Auto) done | Meshes:{0} AddedInf:{1} WeightedVtx:{2}",
                ja=u"安全バインド（近傍自動）完了 ｜ メッシュ:{0} / 追加:{1} 本 / 割当頂点:{2}",
            ).output().format(mesh_cnt, added_cnt, vtx_cnt)
        else:
            short_msg = lang.Lang(
                en="Safe Add Influence done | Meshes:{0} AddedInf:{1} (weight=0)",
                ja=u"安全インフルエンス追加完了 ｜ メッシュ:{0} / 追加:{1} 本（既存ウェイト維持）",
            ).output().format(mesh_cnt, added_cnt)

        detail_lines = [
            lang.Lang(en="Target Joints ({0}):", ja=u"対象ジョイント ({0} 本):").output().format(len(res["target_joints"])),
            "  " + ", ".join(j.split("|")[-1] for j in res["target_joints"][:30]),
            lang.Lang(en="Target Meshes ({0}):", ja=u"対象メッシュ ({0} 件):").output().format(mesh_cnt),
            "  " + ", ".join(m.split("|")[-1] for m in res["meshes"]),
        ]
        if res["matched_pairs"]:
            detail_lines.append(
                lang.Lang(en="Matched Opposite Pairs:", ja=u"検出した左右対応ペア:").output()
            )
            for src_j, dst_j in res["matched_pairs"][:30]:
                detail_lines.append("  {0} -> {1}".format(src_j.split("|")[-1], dst_j.split("|")[-1]))
        elif mode == "mirror_opposite" and new_mesh_cnt == 0:
            detail_lines.append(
                lang.Lang(
                    en="Note: No weighted opposite bone found. Added influence with weight 0.0 (existing weights untouched). Try 'Auto-Bind Nearby Vertices' if the bone is not symmetric.",
                    ja=u"補足：対応する反対側ボーン（ウェイトあり）が見つからなかったため、インフルエンス追加（wt=0）のみ行いました。左右非対称ボーンの場合は「近傍の自動バインド」モードをお試しください。",
                ).output()
            )

        self._set_status(short_msg, error=False, detail="\n".join(detail_lines))

        # SIWeightEditor 本体の表示も最新状態へ同期する
        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
                self.editor.set_message(msg="- " + short_msg + " -", error=False)
            except Exception:
                pass

    def _run_mirror_common(self, dry_run=False):
        """高精度スキンウェイト・ミラーの実行または診断プレビューを共通処理する。"""
        direction = self.mirror_dir_combo.currentData() or "auto"
        plane = self.mirror_plane_combo.currentData() or "YZ"
        sample_mode = self.mirror_sample_combo.currentData() or "auto"
        include_children = self.mirror_children_chk.isChecked()
        auto_add = self.mirror_auto_add_chk.isChecked()
        equalize_center = self.mirror_center_eq_chk.isChecked()
        pos_fallback = self.mirror_pos_fallback_chk.isChecked()

        try:
            if dry_run:
                res = weight_mirror_copy_helper.execute_precision_weight_mirror(
                    direction=direction,
                    plane=plane,
                    sample_mode=sample_mode,
                    include_children=include_children,
                    auto_add_opposite=auto_add,
                    equalize_center=equalize_center,
                    use_position_fallback=pos_fallback,
                    dry_run=True,
                )
            else:
                with qt.undo_chunk("SIWE_PrecisionWeightMirror"):
                    res = weight_mirror_copy_helper.execute_precision_weight_mirror(
                        direction=direction,
                        plane=plane,
                        sample_mode=sample_mode,
                        include_children=include_children,
                        auto_add_opposite=auto_add,
                        equalize_center=equalize_center,
                        use_position_fallback=pos_fallback,
                        dry_run=False,
                    )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Weight Mirror error: {0}", ja=u"ウェイトミラーエラー: {0}").output().format(exc),
                error=True,
            )
            return

        mode_labels = {
            "bone_mirror": lang.Lang(en="Bone", ja=u"ボーン指定").output(),
            "single_mesh_mirror": lang.Lang(en="Mesh", ja=u"メッシュ全体").output(),
            "multi_mesh_mirror": lang.Lang(en="Multi-Mesh", ja=u"別メッシュ間").output(),
            "component_mirror": lang.Lang(en="Vertices", ja=u"選択頂点").output(),
        }
        mode_txt = mode_labels.get(res["mode"], res["mode"])
        name_cnt = len(res["name_pairs"])
        pos_cnt = len(res["pos_pairs"])
        added_cnt = len(res["added_influences"])
        unm_cnt = len(res["unmatched_sided"])

        if dry_run:
            short_msg = lang.Lang(
                en="Mirror Check [{0}] | Meshes:{1} NamePairs:{2} PosPairs:{3} AutoAdd:{4} Unmatched:{5}",
                ja=u"ミラー診断 [{0}] ｜ メッシュ:{1} / 名前対:{2} / 位置対:{3} / 自動追加予定:{4} / 未対:{5}",
            ).output().format(mode_txt, len(res["meshes"]), name_cnt, pos_cnt, added_cnt, unm_cnt)
        else:
            short_msg = lang.Lang(
                en="Mirror Done [{0}] | Meshes:{1} MirroredVtx:{2} AddedInf:{3}",
                ja=u"高精度ミラー完了 [{0}] ｜ メッシュ:{1} / 転写頂点:{2} / 追加ボーン:{3}",
            ).output().format(mode_txt, len(res["meshes"]), res["mirrored_vertices"], added_cnt)

        detail_lines = [
            lang.Lang(en="Mode: {0} / Plane: {1}", ja=u"モード: {0} / 対称面: {1}").output().format(mode_txt, plane),
            lang.Lang(en="Meshes ({0}):", ja=u"対象メッシュ ({0} 件):").output().format(len(res["meshes"])),
            "  " + ", ".join(m.split("|")[-1] for m in res["meshes"]),
        ]
        if res.get("source_bones") and res.get("target_bones"):
            detail_lines.append(
                lang.Lang(en="Bone Mirror Direction:", ja=u"ボーン転写方向:").output()
            )
            for sj, tj in zip(res["source_bones"][:20], res["target_bones"][:20]):
                detail_lines.append("  {0} -> {1}".format(sj.split("|")[-1], tj.split("|")[-1]))
        if res["pos_pairs"]:
            detail_lines.append(
                lang.Lang(en="Matched by Position ({0}):", ja=u"3D 座標フォールバック照合 ({0} 組):").output().format(pos_cnt)
            )
            for j1, j2 in res["pos_pairs"][:20]:
                detail_lines.append("  {0} <-> {1}".format(j1.split("|")[-1], j2.split("|")[-1]))
        if res["added_influences"]:
            detail_lines.append(
                lang.Lang(en="Added Opposite Bones ({0}):", ja=u"自動追加ボーン ({0} 本):").output().format(added_cnt)
            )
            detail_lines.append("  " + ", ".join(j.split("|")[-1] for j in res["added_influences"][:30]))
        if res["unmatched_sided"]:
            detail_lines.append(
                lang.Lang(en="Unmatched Sided Bones ({0}):", ja=u"反対側が見つからなかった非中央ボーン ({0} 本):").output().format(unm_cnt)
            )
            detail_lines.append("  " + ", ".join(j.split("|")[-1] for j in res["unmatched_sided"][:30]))

        self._set_status(short_msg, error=False, detail="\n".join(detail_lines))
        if not dry_run and self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
                self.editor.set_message(msg="- " + short_msg + " -", error=False)
            except Exception:
                pass

    def run_precision_weight_mirror(self):
        """高精度スキンウェイト・ミラーを実行する。"""
        self._run_mirror_common(dry_run=False)

    def run_diagnose_weight_mirror(self):
        """ミラー実行前に対応ボーンペアや自動追加予定ボーンを非破壊で診断する。"""
        self._run_mirror_common(dry_run=True)

    def register_weight_copy_source(self):
        """選択中のスキニング済みメッシュをウェイトコピー元として登録する。"""
        meshes = self._get_selected_polygon_transforms()
        if not meshes:
            self._set_status(
                lang.Lang(
                    en="Select a skinned polygon mesh to register as Copy Source.",
                    ja=u"コピー元にするスキニング済みポリゴンメッシュを 1 つ選択してください。",
                ).output(),
                error=True,
            )
            return

        src_mesh = meshes[0]
        if not _get_skin_cluster(src_mesh):
            self._set_status(
                lang.Lang(
                    en="Selected mesh has no skinCluster: {0}",
                    ja=u"選択メッシュにスキンクラスタがありません: {0}",
                ).output().format(src_mesh.split("|")[-1]),
                error=True,
            )
            return

        self._weight_copy_src_mesh = src_mesh
        short_n = src_mesh.split("|")[-1]
        self.copy_src_label.setText(
            lang.Lang(en="Source Mesh: {0}", ja=u"コピー元: {0}").output().format(short_n)
        )
        self.copy_src_label.setToolTip(src_mesh)
        self._set_status(
            lang.Lang(
                en="Registered weight copy source: {0}",
                ja=u"ウェイトコピー元を登録しました: {0}（コピー先のメッシュまたは頂点を選んで実行してください）",
            ).output().format(short_n),
            error=False,
        )

    def clear_weight_copy_source(self):
        """登録中のウェイトコピー元メッシュをクリアする。"""
        self._weight_copy_src_mesh = None
        self.copy_src_label.setText(
            lang.Lang(
                en="Source Mesh: (Not set - or select Source -> Target meshes directly)",
                ja=u"コピー元: 未登録（または コピー元 → コピー先 の順に 2 メッシュ直接選択でも可）",
            ).output()
        )
        self.copy_src_label.setToolTip("")
        self._set_status(
            lang.Lang(en="Cleared weight copy source.", ja=u"ウェイトコピー元の登録をクリアしました。").output(),
            error=False,
        )

    def run_safe_weight_copy(self):
        """コピー元メッシュから選択メッシュ（または選択頂点群）へ安全かつ高精度にウェイトをコピーする。"""
        sel_meshes = self._get_selected_polygon_transforms()
        comp_vtx_map = weight_mirror_copy_helper._collect_selection_components_by_mesh()

        src_mesh = self._weight_copy_src_mesh
        if src_mesh and not cmds.objExists(src_mesh):
            src_mesh = None
            self.clear_weight_copy_source()

        dst_meshes = []
        dst_vtx_map = None

        if src_mesh:
            if comp_vtx_map:
                dst_vtx_map = comp_vtx_map
            else:
                dst_meshes = [m for m in sel_meshes if m != src_mesh]
        else:
            if len(sel_meshes) >= 2:
                src_mesh = sel_meshes[0]
                dst_meshes = sel_meshes[1:]
            else:
                self._set_status(
                    lang.Lang(
                        en="Register a Source Mesh first, or select Source -> Target meshes (2+) together.",
                        ja=u"先にコピー元を登録するか、「コピー元 → コピー先」の順に 2 つ以上のメッシュを選択してください。",
                    ).output(),
                    error=True,
                )
                return

        if not dst_meshes and not dst_vtx_map:
            self._set_status(
                lang.Lang(
                    en="Select target mesh(es) or target vertices to copy weights to.",
                    ja=u"コピー先のメッシュまたは頂点を選択してください。",
                ).output(),
                error=True,
            )
            return

        copy_mode = self.copy_mode_combo.currentData() or "auto"
        mirror = self.copy_mirror_chk.isChecked()
        plane = self.mirror_plane_combo.currentData() or "YZ"
        auto_bind = self.copy_auto_bind_chk.isChecked()
        pos_fallback = self.mirror_pos_fallback_chk.isChecked()

        with qt.undo_chunk("SIWE_SafeWeightCopy"):
            try:
                res = weight_mirror_copy_helper.execute_safe_weight_copy(
                    src_mesh=src_mesh,
                    dst_meshes=dst_meshes,
                    dst_vtx_map=dst_vtx_map,
                    copy_mode=copy_mode,
                    mirror=mirror,
                    plane=plane,
                    auto_bind=auto_bind,
                    use_position_fallback=pos_fallback,
                )
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Weight Copy error: {0}", ja=u"ウェイトコピーエラー: {0}").output().format(exc),
                    error=True,
                )
                return

        tag = lang.Lang(en="Mirrored Copy", ja=u"ミラーコピー").output() if mirror else lang.Lang(en="Copy", ja=u"コピー").output()
        short_msg = lang.Lang(
            en="Weight {0} Done | Src:{1} -> Dst:{2} mesh(es) | Verts:{3} | Bound:{4} AddedInf:{5}",
            ja=u"ウェイト{0}完了 ｜ {1} → {2} メッシュ / 転写頂点:{3} / 新規バインド:{4} / 追加ボーン:{5}",
        ).output().format(
            tag,
            src_mesh.split("|")[-1],
            len(res["dst_meshes"]),
            res["copied_vertices"],
            len(res["newly_bound_meshes"]),
            len(res["added_influences"]),
        )
        detail_lines = [
            lang.Lang(en="Source: {0}", ja=u"コピー元: {0}").output().format(src_mesh.split("|")[-1]),
            lang.Lang(en="Targets ({0}):", ja=u"コピー先 ({0} 件):").output().format(len(res["dst_meshes"])),
            "  " + ", ".join(m.split("|")[-1] for m in res["dst_meshes"]),
        ]
        if res["newly_bound_meshes"]:
            detail_lines.append(
                lang.Lang(en="Auto-bound meshes:", ja=u"自動バインドしたメッシュ:").output()
                + " "
                + ", ".join(m.split("|")[-1] for m in res["newly_bound_meshes"])
            )
        if res["added_influences"]:
            detail_lines.append(
                lang.Lang(en="Added influences ({0}):", ja=u"自動追加したボーン ({0} 本):").output().format(len(res["added_influences"]))
            )
            detail_lines.append("  " + ", ".join(j.split("|")[-1] for j in res["added_influences"][:30]))

        self._set_status(short_msg, error=False, detail="\n".join(detail_lines))
        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
                self.editor.set_message(msg="- " + short_msg + " -", error=False)
            except Exception:
                pass

    def _reset_force_freeze_rotate_adjust(self):
        """強制フリーズ前の回転調整スピンボックスをすべて 0 に戻す。"""
        for sp in self.fz_rot_adj_spins.values():
            sp.setValue(0.0)

    def run_force_freeze_joints(self):
        """回転等調整 → ポーズ（キーフレーム）JSON 書き出し → 接続・ロック解除フリーズ → キーフレーム書き戻しを一括実行する。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.jnt_include_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(
                    en="Select one or more joints to adjust & freeze.",
                    ja=u"回転調整・フリーズするジョイントを選択してください。",
                ).output(),
                error=True,
            )
            return

        do_rot = self.fz_rot_chk.isChecked()
        do_scl = self.fz_scl_chk.isChecked()
        do_trs = self.fz_trans_chk.isChecked()
        if not (do_rot or do_scl or do_trs):
            self._set_status(
                lang.Lang(
                    en="Check at least one channel (Rotate / Scale / Translate).",
                    ja=u"Rotate / Scale / Translate のいずれか 1 つ以上をチェックしてください。",
                ).output(),
                error=True,
            )
            return

        if not self._confirm_skinned_joints_edit(joints):
            return

        rot_adj = (
            float(self.fz_rot_adj_spins["X"].value()),
            float(self.fz_rot_adj_spins["Y"].value()),
            float(self.fz_rot_adj_spins["Z"].value()),
        )
        freeze_frame = float(self.fz_frame_spin.value()) if self.fz_use_frame_chk.isChecked() else None
        clean_anim = self.fz_clean_anim_chk.isChecked()

        cmds.undoInfo(openChunk=True, chunkName="SIWE_AllInOneFreezeJoints")
        try:
            res = rig_cleanup_helper.freeze_joints_with_pose_backup(
                joints,
                _get_storage_dir(),
                freeze_rotate=do_rot,
                freeze_scale=do_scl,
                freeze_translate=do_trs,
                backup_and_restore_pose=True,
                freeze_frame=freeze_frame,
                delete_unused_anim_curves=clean_anim,
                rotate_adjust_deg=rot_adj,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Force Freeze error: {0}", ja=u"一括フリーズエラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        self._populate_joint_issue_table(rig_cleanup_helper.inspect_joints(joints))

        fz = res["freeze"]
        rt = res.get("restore")
        short_msg = lang.Lang(
            en="All-in-One Freeze done | Joints:{0} BrokenConn:{1} Unlocked:{2}",
            ja=u"一括フリーズ完了 ｜ 対象:{0} 本 / 切断:{1} 件 / ロック解除:{2} 件",
        ).output().format(fz["processed"], len(fz["broken"]), fz["unlocked"])
        if rt is not None:
            short_msg += lang.Lang(
                en=" | Keyframes written back: {0} frame(s)",
                ja=u" ｜ キー書き戻し: {0} フレーム",
            ).output().format(rt["restored_frames"])

        detail_lines = []
        if res.get("rotate_adjust_applied"):
            detail_lines.append(
                lang.Lang(
                    en="Rotation Adjust Applied: ({0:.2f}, {1:.2f}, {2:.2f}) deg",
                    ja=u"適用した回転調整: ({0:.2f}, {1:.2f}, {2:.2f})°",
                ).output().format(rot_adj[0], rot_adj[1], rot_adj[2])
            )
        if res.get("pose_json"):
            detail_lines.append(
                lang.Lang(en="Pose JSON: {0}", ja=u"書き出しポーズ JSON: {0}").output().format(res["pose_json"])
            )
        detail_lines.append(
            lang.Lang(
                en="Frozen -> Rotate:{0} / Scale:{1} / Translate:{2} / Removed orphan curves:{3}",
                ja=u"フリーズ結果 → Rotate:{0} / Scale:{1} / Translate:{2} / 孤立カーブ削除:{3}",
            ).output().format(fz["rot_fixed"], fz["scale_fixed"], fz["trans_fixed"], fz["removed_nodes"])
        )
        if fz["broken"]:
            detail_lines.append(lang.Lang(en="Disconnected:", ja=u"切断した入力接続:").output())
            for conn_str in fz["broken"][:40]:
                detail_lines.append("  " + conn_str)
            if len(fz["broken"]) > 40:
                detail_lines.append("  ... (+{0})".format(len(fz["broken"]) - 40))
        if rt is not None and rt.get("missing_joints"):
            detail_lines.append(
                lang.Lang(en="Missing joints on restore:", ja=u"書き戻し時に未検出のジョイント:").output()
                + " "
                + ", ".join(rt["missing_joints"])
            )

        self._set_status(short_msg, error=False, detail="\n".join(detail_lines))

    def open_pose_backup_folder(self):
        """ジョイントポーズ退避用 JSON フォルダを開く。"""
        folder = os.path.normpath(rig_cleanup_helper.get_pose_backup_dir(_get_storage_dir()))
        try:
            if hasattr(os, "startfile"):
                os.startfile(folder)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Folder open error: {0}", ja=u"フォルダを開けませんでした: {0}").output().format(exc),
                error=True,
            )

    # ------------------------------------------------------------------
    # リファレンスジョイント上書きタブ ハンドラ
    # ------------------------------------------------------------------
    def store_quick_axis_reference(self):
        """選択したリファレンスボーン（単体または複数指チェーン）の軸方向を JSON 保存なしでメモリへ一時保存する。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Select a reference joint (or finger chain) in the viewport first.",
                    ja=u"手本にするリファレンスボーンをビューポートで選択してください。",
                ).output(),
                error=True,
            )
            return

        include_children = self.quick_axis_children_chk.isChecked()
        try:
            self._quick_axis_ref = reference_joint_helper.capture_quick_axis_reference(
                sel_joints,
                include_children=include_children,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Axis store error: {0}", ja=u"方向の一時保存エラー: {0}").output().format(exc),
                error=True,
            )
            return

        entries = self._quick_axis_ref.get("entries") or [self._quick_axis_ref]
        name_disp = self._quick_axis_ref["short_name"]
        if len(entries) > 1:
            name_disp = "{0} (+{1})".format(name_disp, len(entries) - 1)
        disp = lang.Lang(
            en="Stored Axis: {0} (RotateOrder: {1})",
            ja=u"一時保存中の方向: {0}（RotateOrder: {1}）",
        ).output().format(name_disp, self._quick_axis_ref["rotateOrderLabel"])
        self.quick_axis_label.setText(disp)
        self.quick_axis_label.setToolTip("\n".join(e["full_path"] for e in entries[:30]))
        self._set_status(
            lang.Lang(
                en="Stored axis direction from {0} ({1} bone(s), no JSON saved).",
                ja=u"{0} のボーン軸方向を一時保存しました（{1} 本・JSON 保存なし）。変更したいボーンを選択してください。",
            ).output().format(self._quick_axis_ref["short_name"], len(entries)),
            error=False,
        )

    def apply_quick_axis_to_selected(self):
        """一時保存したリファレンス方向へ、子階層を回転させずに選択ボーンのローカル回転軸だけを変更する。"""
        if not self._quick_axis_ref:
            self._set_status(
                lang.Lang(
                    en="Store a reference bone axis direction first.",
                    ja=u"先に手本ボーンを選択して「リファレンス方向を一時保存」を押してください。",
                ).output(),
                error=True,
            )
            return

        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Select target joint(s) to change axis direction.",
                    ja=u"軸方向を変更したいボーンをビューポートで選択してください。",
                ).output(),
                error=True,
            )
            return

        include_children = self.quick_axis_children_chk.isChecked()
        match_rot_order = self.quick_axis_rot_order_chk.isChecked()
        update_skin = self.quick_axis_skin_chk.isChecked()

        cmds.undoInfo(openChunk=True, chunkName="SIWE_QuickApplyAxis")
        try:
            res = reference_joint_helper.apply_quick_axis_to_joints(
                sel_joints,
                self._quick_axis_ref,
                include_children=include_children,
                match_rotate_order=match_rot_order,
                update_skin_bind_pre=update_skin,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Quick axis apply error: {0}", ja=u"軸方向の変更エラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        upd_cnt = len(res["updated"])
        skip_cnt = len(res["skipped"])
        skin_cnt = len(res["updated_skins"])
        end_cnt = int(res.get("end_aligned", 0))
        msg = lang.Lang(
            en="Quick Axis Applied ({0}) | Updated:{1} EndAligned:{2} Skipped:{3}",
            ja=u"軸方向を変更しました（基準: {0}） ｜ 変更:{1} 本 / 末端揃え:{2} / スキップ:{3}",
        ).output().format(self._quick_axis_ref["short_name"], upd_cnt, end_cnt, skip_cnt)
        if skin_cnt > 0:
            msg += lang.Lang(
                en=" | Skin bindPreMatrix:{0}",
                ja=u" ｜ スキン補正:{0} 件",
            ).output().format(skin_cnt)
        detail = ""
        if res["skipped"]:
            detail = lang.Lang(
                en="Skipped (locked or connected):\n  ",
                ja=u"ロックまたは入力接続のためスキップ:\n  ",
            ).output() + "\n  ".join(j.split("|")[-1] for j in res["skipped"])
        self._set_status(msg, error=(upd_cnt == 0 and skip_cnt > 0), detail=detail or None)

    def freeze_quick_selected_bone_rotations(self):
        """選択ボーンの回転（Rotate / RotateAxis）を子階層を回転させずに jointOrient へフリーズする。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Select bone(s) to freeze rotation first.",
                    ja=u"回転をフリーズするボーンをビューポートで選択してください。",
                ).output(),
                error=True,
            )
            return

        include_children = self.quick_axis_children_chk.isChecked()
        update_skin = self.quick_axis_skin_chk.isChecked()

        cmds.undoInfo(openChunk=True, chunkName="SIWE_QuickFreezeRotation")
        try:
            res = reference_joint_helper.freeze_quick_selected_rotations(
                sel_joints,
                include_children=include_children,
                update_skin_bind_pre=update_skin,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Freeze rotation error: {0}", ja=u"回転フリーズエラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        fz_cnt = len(res["frozen"])
        skip_cnt = len(res["skipped"])
        skin_cnt = len(res["updated_skins"])
        msg = lang.Lang(
            en="Frozen selected bone rotation | Frozen:{0} Skipped:{1}",
            ja=u"選択ボーンの回転をフリーズしました ｜ フリーズ:{0} 本 / スキップ:{1} 本",
        ).output().format(fz_cnt, skip_cnt)
        if skin_cnt > 0:
            msg += lang.Lang(
                en=" | Skin bindPreMatrix:{0}",
                ja=u" ｜ スキン補正:{0} 件",
            ).output().format(skin_cnt)
        detail = ""
        if res["skipped"]:
            detail = lang.Lang(
                en="Skipped (locked or connected):\n  ",
                ja=u"ロックまたは入力接続のためスキップ:\n  ",
            ).output() + "\n  ".join(j.split("|")[-1] for j in res["skipped"])
        self._set_status(msg, error=(fz_cnt == 0 and skip_cnt > 0), detail=detail or None)

    def toggle_quick_axis_lra(self):
        """クイック軸変更エリアから選択ボーンのローカル回転軸表示（LRA）を切り替える。"""
        joints = rig_cleanup_helper.collect_target_joints(
            include_children=self.quick_axis_children_chk.isChecked()
        )
        if not joints:
            self._set_status(
                lang.Lang(en="Select one or more joints first.", ja=u"軸表示を切り替えるボーンを選択してください。").output(),
                error=True,
            )
            return
        with qt.undo_chunk("SIWE_ToggleLocalAxes"):
            state = rig_cleanup_helper.toggle_local_axes_visibility(joints)
        label = "ON" if state else "OFF"
        self._set_status(
            lang.Lang(
                en="Local Rotation Axes: {0} ({1} joint(s)).",
                ja=u"ローカル回転軸表示を {0} に切り替えました（{1} 本）",
            ).output().format(label, len(joints)),
            error=False,
        )

    def _refresh_reference_mapping_summary(self):
        """現在ジョイントとリファレンスジョイントの照合サマリーを更新する。"""
        if not self._ref_source_records:
            self.ref_summary_label.setText(
                lang.Lang(
                    en="Register or load a Reference skeleton to overwrite or create bones.",
                    ja=u"リファレンスジョイントを登録（または JSON 読込）すると上書き・新規作成できます。",
                ).output()
            )
            return
        if not self._ref_cur_records:
            pfx_info = reference_joint_helper.detect_skeleton_prefix_info(self._ref_source_records)
            self.ref_summary_label.setText(
                lang.Lang(
                    en="Create Mode (No Current Skeleton) | Will create {0} bone(s) from Reference ({1})",
                    ja=u"新規作成モード（現在ジョイント未登録） ｜ リファレンスから {0} 本を新規作成（ルート: {1}）",
                ).output().format(len(self._ref_source_records), pfx_info["root_short"])
            )
            return
        if self._ref_mapping_data is None:
            self._ref_mapping_data = reference_joint_helper.build_auto_mapping(
                self._ref_cur_records, self._ref_source_records
            )
        cur_rows = self._ref_mapping_data.get("current_rows") or []
        extra_rows = self._ref_mapping_data.get("extra_ref_rows") or []
        overwrite_cnt = sum(1 for r in cur_rows if r["action"] == "overwrite")
        del_cnt = sum(1 for r in cur_rows if r["action"] == "delete_merge")
        unmatched_cnt = sum(1 for r in cur_rows if not r.get("ref_full"))
        add_cnt = sum(1 for r in extra_rows if r["action"] == "add")

        self.ref_summary_label.setText(
            lang.Lang(
                en="Overwrite Mode | Match:{0} / Unmatched Current:{1} (Del:{2}) / Extra Ref:{3} (Add:{4}) | Meshes:{5}",
                ja=u"上書きモード ｜ 対応:{0} 本 / 未対応(現在側):{1} 本（削除統合:{2}） / 追加候補:{3} 本（追加:{4}） ｜ 対象メッシュ:{5} 件",
            ).output().format(
                overwrite_cnt, unmatched_cnt, del_cnt, len(extra_rows), add_cnt, len(self._ref_cur_meshes)
            )
        )

    def clear_current_skeleton_for_ref(self):
        """登録済みの現在ジョイントをクリアし、リファレンスボーン新規作成モードへ切り替える。"""
        self._ref_cur_records = []
        self._ref_cur_meshes = []
        self._ref_mapping_data = None
        self.ref_cur_label.setText(
            lang.Lang(
                en="Current Skeleton: (Not set -> Create mode)",
                ja=u"現在のジョイント: 未登録（未登録時はリファレンスを新規作成）",
            ).output()
        )
        self.ref_cur_label.setToolTip("")
        self._refresh_reference_mapping_summary()
        self._set_status(
            lang.Lang(
                en="Cleared Current Skeleton registration (Create mode enabled).",
                ja=u"現在ジョイントの登録をクリアしました（実行時はリファレンスボーンを新規作成します）。",
            ).output(),
            error=False,
        )

    def register_current_skeleton_for_ref(self):
        """選択ジョイント・メッシュ（または記憶ジョイント）から現在のスケルトン全体と対象メッシュを登録する。"""
        meshes = self._get_selected_polygon_transforms()
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []

        seed_nodes = list(meshes) + list(sel_joints)
        if not seed_nodes and self._cached_joints:
            seed_nodes = _resolve_cached_joints(self._cached_joints)
            sel_joints = list(seed_nodes)

        if not seed_nodes:
            self._set_status(
                lang.Lang(
                    en="Select a current joint or bound mesh first.",
                    ja=u"現在のジョイントまたはスキニング済みメッシュを選択してください。",
                ).output(),
                error=True,
            )
            return

        target_meshes = rig_cleanup_helper.find_meshes_sharing_skeleton(seed_nodes)
        for m in meshes:
            if _get_skin_cluster(m) and m not in target_meshes:
                target_meshes.append(m)

        seed_joints = list(sel_joints)
        for m in target_meshes:
            sc = _get_skin_cluster(m)
            if sc:
                infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], type="joint", long=True) or []
                seed_joints.extend(infs)

        cur_joints, roots = reference_joint_helper.collect_skeleton_hierarchy(seed_joints)
        if not cur_joints:
            self._set_status(
                lang.Lang(
                    en="No current joints found from selection.",
                    ja=u"選択対象から現在のスケルトン階層を取得できませんでした。",
                ).output(),
                error=True,
            )
            return

        self._ref_cur_records = reference_joint_helper.snapshot_skeleton_info(cur_joints)
        self._ref_cur_meshes = target_meshes
        self._ref_mapping_data = None

        root_disp = roots[0].split("|")[-1] if roots else cur_joints[0].split("|")[-1]
        self.ref_cur_label.setText(
            lang.Lang(
                en="Current: {0} ({1} joints / {2} mesh(es))",
                ja=u"現在: {0}（全 {1} 本 / {2} メッシュ）",
            ).output().format(root_disp, len(self._ref_cur_records), len(target_meshes))
        )
        self.ref_cur_label.setToolTip("\n".join(cur_joints[:40]))
        self._refresh_reference_mapping_summary()
        self._set_status(
            lang.Lang(
                en="Registered current full skeleton: {0} ({1} joints, {2} mesh(es)).",
                ja=u"現在のスケルトン全体を登録しました: {0}（{1} 本 / {2} メッシュ）",
            ).output().format(root_disp, len(self._ref_cur_records), len(target_meshes)),
            error=False,
        )

    def register_reference_skeleton(self):
        """選択ジョイントを含むリファレンススケルトン全体（ルートから全階層）を登録する。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Select a reference joint in Maya first.",
                    ja=u"リファレンスにするジョイントを Maya 上で選択してください。",
                ).output(),
                error=True,
            )
            return

        ref_joints, roots = reference_joint_helper.collect_skeleton_hierarchy(sel_joints)
        if not ref_joints:
            self._set_status(
                lang.Lang(en="No reference joints found.", ja=u"リファレンスジョイントを取得できませんでした。").output(),
                error=True,
            )
            return

        self._ref_source_records = reference_joint_helper.snapshot_skeleton_info(ref_joints)
        self._ref_mapping_data = None
        pfx_info = reference_joint_helper.detect_skeleton_prefix_info(self._ref_source_records)
        root_disp = pfx_info["root_short"] or (roots[0].split("|")[-1] if roots else ref_joints[0].split("|")[-1])
        self.ref_src_label.setText(
            lang.Lang(
                en="Reference: {0} ({1} joints)",
                ja=u"リファレンス: {0}（全 {1} 本）",
            ).output().format(root_disp, len(self._ref_source_records))
        )
        self.ref_src_label.setToolTip("\n".join(ref_joints[:40]))
        self._refresh_reference_mapping_summary()
        self._set_status(
            lang.Lang(
                en="Registered full reference skeleton: {0} ({1} joints).",
                ja=u"リファレンススケルトン全体を登録しました: {0}（{1} 本）",
            ).output().format(root_disp, len(self._ref_source_records)),
            error=False,
        )

    def load_reference_session_from_json(self):
        """他シーンで保存したリファレンスジョイント JSON を読み込み、リファレンスとして登録する。"""
        start_dir = reference_joint_helper.get_session_storage_dir(_get_storage_dir())
        file_res = QFileDialog.getOpenFileName(
            self,
            lang.Lang(en="Load Reference Skeleton JSON", ja=u"リファレンスジョイント JSON を選択").output(),
            start_dir,
            "JSON Files (*.json)",
        )
        json_path = file_res[0] if isinstance(file_res, (tuple, list)) else file_res
        if not json_path or not os.path.exists(json_path):
            return

        try:
            payload = reference_joint_helper.load_reference_session_json(json_path)
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Failed to load Reference JSON: {0}", ja=u"リファレンス JSON 読込エラー: {0}").output().format(exc),
                error=True,
            )
            return

        self._ref_source_records = payload.get("reference_joints") or []
        self._ref_mapping_data = None
        pfx_info = reference_joint_helper.detect_skeleton_prefix_info(self._ref_source_records)
        root_disp = pfx_info["root_short"] or (self._ref_source_records[0]["short_name"] if self._ref_source_records else "-")
        self.ref_src_label.setText(
            lang.Lang(
                en="Reference [JSON]: {0} ({1} joints)",
                ja=u"リファレンス[JSON]: {0}（全 {1} 本）",
            ).output().format(root_disp, len(self._ref_source_records))
        )
        self.ref_src_label.setToolTip(json_path)
        self._refresh_reference_mapping_summary()
        self._set_status(
            lang.Lang(
                en="Loaded Reference skeleton from JSON: {0} ({1} joints).",
                ja=u"JSON からリファレンススケルトンを読み込みました: {0}（{1} 本）",
            ).output().format(root_disp, len(self._ref_source_records)),
            error=False,
            detail=json_path,
        )

    def _prompt_character_prefix_for_reference(self, is_create_mode=False):
        """リファレンスボーンのプレフィックス規則（○○_Hips / _Hips / Hips）に応じてキャラ名を確認・入力させる。"""
        pfx_info = reference_joint_helper.detect_skeleton_prefix_info(self._ref_source_records)
        detected_pfx = pfx_info["prefix"]
        pattern_type = pfx_info["pattern_type"]
        root_base = pfx_info["root_base"] or "Hips"

        # 現在スケルトン側に既存プレフィックスがあれば入力欄の初期候補にする
        default_suggest = ""
        if self._ref_cur_records:
            cur_pfx_info = reference_joint_helper.detect_skeleton_prefix_info(self._ref_cur_records)
            default_suggest = cur_pfx_info["prefix"]

        action_ja = u"新規作成" if is_create_mode else u"変更（上書き）"
        action_en = "create" if is_create_mode else "overwrite/rename"

        if pattern_type == "named_prefix" and detected_pfx:
            ans = QMessageBox.question(
                self,
                "SIWeightEditor Improve",
                lang.Lang(
                    en="Reference skeleton uses character prefix '{0}' (e.g. {0}_{1}).\nDo you want to {2} bones with '{0}'?\n\nClick 'No' to enter a new character name.",
                    ja=u"リファレンスボーンにキャラ名「{0}」（例: {0}_{1}）が設定されています。\n「{0}」のボーンへ{2}しますか？\n\n「いいえ」を選択すると新しいキャラ名を入力できます。",
                ).output().format(detected_pfx, root_base, action_ja if lang.Lang(en="en", ja="ja").output() == "ja" else action_en),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if ans == QMessageBox.Cancel:
                return False, ""
            if ans == QMessageBox.Yes:
                return True, detected_pfx

            # 「いいえ」の場合は新しいキャラ名を入力させる
            new_name, ok = QInputDialog.getText(
                self,
                lang.Lang(en="Enter New Character Name", ja=u"新しいキャラ名を入力").output(),
                lang.Lang(
                    en="Enter new character prefix (e.g. charA -> charA_{0} / leave blank for no prefix):",
                    ja=u"新しいキャラ名を入力してください（例: charA → charA_{0} ／ 空欄でプレフィックスなし）:",
                ).output().format(root_base),
                QLineEdit.Normal,
                default_suggest,
            )
            if not ok:
                return False, ""
            return True, new_name.strip().rstrip("_")

        # "_Hips" または "Hips" の場合はキャラ名を決めるダイアログを表示する
        new_name, ok = QInputDialog.getText(
            self,
            lang.Lang(en="Set Character Name Prefix", ja=u"キャラ名（プレフィックス）を設定").output(),
            lang.Lang(
                en="Reference bone '{0}' has no character name prefix.\nEnter character name (e.g. charA -> charA_{1} / leave blank for '{1}'):",
                ja=u"リファレンスボーン「{0}」にキャラ名が付いていません。\nキャラ名を入力してください（例: charA → charA_{1} ／ 空欄で「{1}」のまま）:",
            ).output().format(pfx_info["root_short"] or root_base, root_base),
            QLineEdit.Normal,
            default_suggest,
        )
        if not ok:
            return False, ""
        return True, new_name.strip().rstrip("_")

    def _apply_target_prefix_to_mapping(self, target_prefix):
        """決定したキャラ名プレフィックスを現在のマッピング辞書（上書き先名・追加名）へ反映する。"""
        if not self._ref_mapping_data:
            return
        prefixed_refs = reference_joint_helper.apply_prefix_to_records(self._ref_source_records, target_prefix=target_prefix)
        ref_new_name_map = {r["full_path"]: r["prefixed_name"] for r in prefixed_refs}

        for row in (self._ref_mapping_data.get("current_rows") or []):
            rfp = row.get("ref_full") or ""
            if rfp and rfp in ref_new_name_map and row.get("action") != "delete_merge":
                desired = ref_new_name_map[rfp]
                if desired != row.get("cur_short"):
                    self._ref_mapping_data["has_diff"] = True
                row["new_name"] = desired

        for row in (self._ref_mapping_data.get("extra_ref_rows") or []):
            rfp = row.get("ref_full") or ""
            if rfp and rfp in ref_new_name_map:
                row["new_name"] = ref_new_name_map[rfp]

    def _ensure_reference_pair_ready(self, require_current=True):
        """実行前にリファレンス（および必要時現在ジョイント）が揃っているか確認する。"""
        if not self._ref_source_records:
            sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
            if sel_joints:
                self.register_reference_skeleton()
        if not self._ref_source_records:
            self._set_status(
                lang.Lang(
                    en="Please register or load a Reference skeleton first.",
                    ja=u"先にリファレンスジョイントを登録（または JSON 読込）してください。",
                ).output(),
                error=True,
            )
            return False

        if not require_current:
            return True

        if not self._ref_cur_records:
            self._set_status(
                lang.Lang(
                    en="Please register Current Skeleton first to open mapping UI.",
                    ja=u"対応調整 UI を開くには現在のジョイントを登録してください。",
                ).output(),
                error=True,
            )
            return False

        cur_set = {r["full_path"] for r in self._ref_cur_records}
        ref_set = {r["full_path"] for r in self._ref_source_records}
        if cur_set & ref_set:
            self._set_status(
                lang.Lang(
                    en="Current skeleton and Reference skeleton overlap. Please select separate skeletons.",
                    ja=u"現在のジョイントとリファレンスジョイントが重複しています。別のスケルトンを登録してください。",
                ).output(),
                error=True,
            )
            return False

        if self._ref_mapping_data is None:
            self._ref_mapping_data = reference_joint_helper.build_auto_mapping(
                self._ref_cur_records, self._ref_source_records
            )
        return True

    def save_reference_session_only(self):
        """リファレンスジョイント（および登録済みなら現在ジョイント）の情報を JSON へ保存する（他シーン再利用向け）。"""
        if not self._ensure_reference_pair_ready(require_current=False):
            return
        try:
            save_path, _ = reference_joint_helper.save_reference_session_json(
                _get_storage_dir(),
                self._ref_cur_records,
                self._ref_source_records,
                self._ref_cur_meshes,
            )
            self._set_status(
                lang.Lang(
                    en="Saved Reference skeleton JSON (reusable in other scenes).",
                    ja=u"リファレンスジョイント情報を JSON 保存しました（他シーンでも読込可能）。",
                ).output(),
                error=False,
                detail=save_path,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="JSON save error: {0}", ja=u"JSON 保存エラー: {0}").output().format(exc),
                error=True,
            )

    def open_reference_session_folder(self):
        """リファレンスジョイント用セッション JSON フォルダを開く。"""
        folder = os.path.normpath(reference_joint_helper.get_session_storage_dir(_get_storage_dir()))
        try:
            if hasattr(os, "startfile"):
                os.startfile(folder)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Folder open error: {0}", ja=u"フォルダを開けませんでした: {0}").output().format(exc),
                error=True,
            )

    def open_reference_mapping_ui(self):
        """リネーム＆構造調整ダイアログを開いてユーザーによる調整を受け付ける。"""
        if not self._ensure_reference_pair_ready(require_current=True):
            return False
        dialog = reference_joint_helper.ReferenceJointMappingDialog(
            self._ref_cur_records,
            self._ref_source_records,
            self._ref_mapping_data,
            parent=self,
        )
        exec_fn = getattr(dialog, "exec_", None) or getattr(dialog, "exec")
        if exec_fn() == QDialog.Accepted:
            self._ref_mapping_data = dialog.get_result_mapping()
            self._refresh_reference_mapping_summary()
            return True
        return False

    def run_reference_joint_overwrite(self):
        """キャラ名確認を経て、現在ジョイントがあれば全階層上書き＆ウェイト再適用、未登録ならリファレンスボーンを新規作成する。"""
        if not self._ensure_reference_pair_ready(require_current=False):
            return

        is_create_mode = (not self._ref_cur_records)
        ok_pfx, target_prefix = self._prompt_character_prefix_for_reference(is_create_mode=is_create_mode)
        if not ok_pfx:
            self._set_status(
                lang.Lang(en="Operation cancelled.", ja=u"処理をキャンセルしました。").output(),
                error=False,
            )
            return

        # 現在ジョイントが未登録の場合はリファレンスボーン一式をシーンへ新規作成する
        if is_create_mode:
            cmds.undoInfo(openChunk=True, chunkName="SIWE_CreateReferenceBones")
            try:
                save_path, _ = reference_joint_helper.save_reference_session_json(
                    _get_storage_dir(),
                    [],
                    self._ref_source_records,
                    [],
                )
                created_res = reference_joint_helper.create_skeleton_from_reference_records(
                    self._ref_source_records,
                    target_prefix=target_prefix,
                )
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Reference bone creation error: {0}", ja=u"リファレンスボーン作成エラー: {0}").output().format(exc),
                    error=True,
                )
                raise
            finally:
                cmds.undoInfo(closeChunk=True)

            root_name = created_res["roots"][0].split("|")[-1] if created_res["roots"] else "-"
            short_msg = lang.Lang(
                en="Created Reference skeleton: {0} ({1} bone(s))",
                ja=u"リファレンスボーンを新規作成しました: {0}（{1} 本）",
            ).output().format(root_name, len(created_res["created"]))
            detail_msg = lang.Lang(
                en="Saved Reference JSON: {0}\nRoot: {1}",
                ja=u"保存 JSON: {0}\n作成ルート: {1}",
            ).output().format(save_path, root_name)
            self._set_status(short_msg, error=False, detail=detail_msg)
            return

        # 現在ジョイントが登録されている場合は全階層の上書き・リネーム・構造調整＆ウェイト再適用を実行する
        if not self._ensure_reference_pair_ready(require_current=True):
            return

        self._apply_target_prefix_to_mapping(target_prefix)
        self._refresh_reference_mapping_summary()

        # 名前や構造に差分がある場合は自動でリネーム・構造調整 UI を開いて確認してもらう
        if self.ref_opt_auto_dialog_chk.isChecked() and self._ref_mapping_data.get("has_diff", False):
            accepted = self.open_reference_mapping_ui()
            if not accepted:
                self._set_status(
                    lang.Lang(en="Reference joint overwrite cancelled.", ja=u"リファレンス上書きをキャンセルしました。").output(),
                    error=False,
                )
                return

        cmds.undoInfo(openChunk=True, chunkName="SIWE_ReferenceJointOverwrite")
        try:
            res = reference_joint_helper.execute_reference_joint_overwrite(
                self._ref_cur_records,
                self._ref_source_records,
                self._ref_mapping_data,
                self._ref_cur_meshes,
                _get_storage_dir(),
                overwrite_orient=self.ref_opt_orient_chk.isChecked(),
                overwrite_position=self.ref_opt_pos_chk.isChecked(),
                overwrite_rotate_order=self.ref_opt_rot_order_chk.isChecked(),
                apply_rename=self.ref_opt_rename_chk.isChecked(),
                include_added_in_skin=self.ref_opt_bind_added_chk.isChecked(),
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Reference overwrite error: {0}", ja=u"リファレンス上書きエラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        self.reload_record_list()
        short_msg = lang.Lang(
            en="Overwrite done | Axis:{0} Added:{1} Deleted:{2} Renamed:{3} Meshes:{4}",
            ja=u"リファレンス上書き完了 ｜ 軸上書き:{0} 追加:{1} 削除統合:{2} リネーム:{3} 再バインド:{4} メッシュ",
        ).output().format(
            res["overwritten"],
            res["added"],
            res["deleted"],
            res["renamed"],
            len(res["rebound_meshes"]),
        )
        detail_msg = lang.Lang(
            en="Session JSON: {0}\nRebound Meshes:\n  {1}",
            ja=u"保存 JSON: {0}\nウェイト再適用メッシュ:\n  {1}",
        ).output().format(
            res["session_json"],
            "\n  ".join(m.split("|")[-1] for m in res["rebound_meshes"]) if res["rebound_meshes"] else "-",
        )
        self._set_status(short_msg, error=False, detail=detail_msg)
        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
                self.editor.set_message(msg="- " + short_msg + " -", error=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # タブ 3 ハンドラ：メッシュ ＆ シーンクリーンアップ
    # ------------------------------------------------------------------
    def run_mesh_cleanup_ui(self, select_only=True):
        """選択ポリゴンメッシュに対して Mesh Cleanup（検出選択または自動修正）を実行する。"""
        meshes = self._get_selected_polygon_transforms()
        if not meshes:
            self._set_status(
                lang.Lang(en="Select one or more polygon meshes first.", ja=u"対象のポリゴンメッシュを選択してください。").output(),
                error=True,
            )
            return

        any_checked = any([
            self.mc_quads_chk.isChecked(),
            self.mc_nsided_chk.isChecked(),
            self.mc_concave_chk.isChecked(),
            self.mc_holed_chk.isChecked(),
            self.mc_nonplanar_chk.isChecked(),
            self.mc_lamina_chk.isChecked(),
            self.mc_nonmanifold_chk.isChecked(),
            self.mc_zero_geom_chk.isChecked(),
            self.mc_zero_edge_chk.isChecked(),
        ])
        if not any_checked:
            self._set_status(
                lang.Lang(en="Check at least one Mesh Cleanup option.", ja=u"メッシュクリーンアップの項目を 1 つ以上チェックしてください。").output(),
                error=True,
            )
            return

        if not select_only:
            skinned = [m for m in meshes if _get_skin_cluster(m)]
            if skinned:
                ans = QMessageBox.warning(
                    self,
                    "SIWeightEditor Improve",
                    lang.Lang(
                        en="{0} selected mesh(es) have a skinCluster.\nRunning Mesh Cleanup while bound can change topology and deform weights.\nIt is recommended to unbind via Tab 1 (Step 1) first.\n\nContinue anyway?",
                        ja=u"選択メッシュのうち {0} 件はスキニング済みです。\nバインドしたまま Mesh Cleanup を実行するとトポロジ変化でウェイトが乱れる場合があります。\n先に「モデル / ボーン修正」タブのステップ 1 で記録・解除することを推奨します。\n\nこのまま修正を実行しますか？",
                    ).output().format(len(skinned)),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if ans != QMessageBox.Yes:
                    return

        cmds.undoInfo(openChunk=True)
        try:
            res = rig_cleanup_helper.run_mesh_cleanup(
                meshes,
                select_only=select_only,
                quads=self.mc_quads_chk.isChecked(),
                nsided=self.mc_nsided_chk.isChecked(),
                concave=self.mc_concave_chk.isChecked(),
                holed=self.mc_holed_chk.isChecked(),
                nonplanar=self.mc_nonplanar_chk.isChecked(),
                zero_geom=self.mc_zero_geom_chk.isChecked(),
                zero_edge=self.mc_zero_edge_chk.isChecked(),
                nonmanifold=self.mc_nonmanifold_chk.isChecked(),
                lamina=self.mc_lamina_chk.isChecked(),
                keep_history=False,
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Mesh Cleanup error: {0}", ja=u"Mesh Cleanup エラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        if select_only:
            cnt = res["matched_count"]
            if cnt == 0:
                self._set_status(
                    lang.Lang(
                        en="Mesh check passed: No matching geometry issues found.",
                        ja=u"メッシュ検出完了: 該当する不正フェース・ジオメトリはありませんでした。",
                    ).output(),
                    error=False,
                )
            else:
                self._set_status(
                    lang.Lang(
                        en="Detected & selected {0} problematic component(s) in viewport.",
                        ja=u"問題箇所を {0} 件検出し、ビューポートで選択しました。",
                    ).output().format(cnt),
                    error=True,
                )
        else:
            self._set_status(
                lang.Lang(
                    en="Mesh Cleanup executed on {0} mesh(es).",
                    ja=u"{0} 件のメッシュに対して Mesh Cleanup（修正）を実行しました。",
                ).output().format(len(meshes)),
                error=False,
            )

    def run_scene_scan_ui(self):
        """シーン内の不要ノード件数を診断して表示する。"""
        scan = rig_cleanup_helper.scan_scene_cleanup_targets()
        summary = lang.Lang(
            en="Scan Result | Unused Intermediates: {0} / Orphaned bindPose: {1} / Unknown Nodes: {2} / Empty Groups: {3}",
            ja=u"診断結果 ｜ 未使用中間シェイプ: {0} 件 / 孤立 bindPose: {1} 件 / 未知ノード: {2} 件 / 空グループ: {3} 件",
        ).output().format(
            len(scan["unused_intermediates"]),
            len(scan["orphaned_bind_poses"]),
            len(scan["unknown_nodes"]),
            len(scan["empty_groups"]),
        )
        self.scene_scan_label.setText(summary)
        total = (
            len(scan["unused_intermediates"])
            + len(scan["orphaned_bind_poses"])
            + len(scan["unknown_nodes"])
            + len(scan["empty_groups"])
        )
        self._set_status(summary, error=(total > 0))

    def run_scene_cleanup_ui(self):
        """チェックされているシーンクリーンアップ項目を実行する。"""
        cmds.undoInfo(openChunk=True)
        try:
            report = rig_cleanup_helper.run_scene_cleanup(
                clean_intermediates=self.sc_interm_chk.isChecked(),
                clean_bind_poses=self.sc_bindpose_chk.isChecked(),
                clean_unknown=self.sc_unknown_chk.isChecked(),
                clean_empty_groups=self.sc_empty_grp_chk.isChecked(),
                clean_unused_shading=self.sc_unused_mat_chk.isChecked(),
            )
        except Exception as exc:
            self._set_status(
                lang.Lang(en="Scene Cleanup error: {0}", ja=u"Scene Cleanup エラー: {0}").output().format(exc),
                error=True,
            )
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        summary = lang.Lang(
            en="Scene Cleanup Done | Intermediates: {0} / bindPose: {1} / Unknown: {2} / Empty Groups: {3} / Unused Shaders: {4}",
            ja=u"シーンクリーンアップ完了 ｜ 中間シェイプ: {0} / 孤立 bindPose: {1} / 未知ノード: {2} / 空グループ: {3} / 未使用シェーダー: {4}",
        ).output().format(
            report["unused_intermediates"],
            report["orphaned_bind_poses"],
            report["unknown_nodes"],
            report["empty_groups"],
            report["unused_shading"],
        )
        self.scene_scan_label.setText(summary)
        self._set_status(summary, error=False)
