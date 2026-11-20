# -*- coding: utf-8 -*-
"""スキニング済みメッシュ＆ボーンの修正（記録→解除→編集→再バインド＆補間復元）、ジョイント整理・Joint Orient、メッシュ＆シーンクリーンアップ専用ウィンドウ。"""

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
from . import rig_cleanup_helper
_reload(rig_cleanup_helper)
from ..qt import *


WEIGHT_EPSILON = 1e-6
DATA_DIR_NAME = "siweighteditor_improve_model_edit"


def _get_storage_dir():
    """モデリング修正用 JSON バックアップの保存ディレクトリを返す。"""
    base_dir = os.getenv("MAYA_APP_DIR") or os.path.expanduser("~")
    path = os.path.join(base_dir, "Scripting_Files", DATA_DIR_NAME)
    if not os.path.exists(path):
        os.makedirs(path)
    return path


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
    """バインド解除・ヒストリ削除・安全フリーズを実行して編集可能状態にする。"""
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

    skin_fn.setWeights(shape_dag, vtx_comp, inf_indices, flat_weights, False)

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
        self.setWindowTitle(self.WINDOW_TITLE)
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
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(650, 620)

        self._records = []
        self._joint_issues = []
        self._log_entries = []
        self._saved_frame = None
        self.log_window = None
        self._init_ui()
        self.reload_record_list()

    def _init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(
            self._build_model_bone_tab(),
            lang.Lang(en="Model / Bone Edit", ja=u"モデル / ボーン修正").output(),
        )
        self.tabs.addTab(
            self._build_joint_orient_tab(),
            lang.Lang(en="Joint Cleanup / Orient", ja=u"ジョイント整理 / Orient").output(),
        )
        self.tabs.addTab(
            self._build_cleanup_tab(),
            lang.Lang(en="Mesh / Scene Cleanup", ja=u"メッシュ / シーンクリーンアップ").output(),
        )
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
                ja=u"メッシュ（頂点移動・エッジ追加/削除）だけでなくボーン位置の変更にも対応します。\nJSON へ記録・解除してから自由にモデルやジョイントを編集し、完了後に新ボーン位置で再バインド＆ウェイト復元を行います。",
            ).output()
        )
        guide_label.setWordWrap(True)
        qt.change_button_color(guide_label, textColor=[220, 220, 220], bgColor=[50, 52, 58])
        guide_label.setMargin(6)
        layout.addWidget(guide_label)

        # ステップ：編集開始
        step1_box = QGroupBox(
            lang.Lang(
                en="Step 1 : Start Editing (Record JSON -> Freeze & Unbind)",
                ja=u"ステップ 1：編集開始（JSON 記録 → フリーズ・ヒストリ削除・バインド解除）",
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
                ja=u"編集開始時に指定フレームへ移動し、完了後に元のフレームへ戻す:",
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
                ja=u"モデリング・ボーン編集時に移動する基準フレーム（デフォルト: 0）",
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

        self.start_edit_but = QPushButton(
            lang.Lang(
                en="Record Weights & Start Editing (Selected Mesh or Joint)",
                ja=u"選択メッシュ / ジョイントのウェイトを記録して編集開始",
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

        # ステップ：編集完了
        step2_box = QGroupBox(
            lang.Lang(
                en="Step 2 : Finish Editing (Clean bindPose -> Freeze -> Rebind -> Restore)",
                ja=u"ステップ 2：編集完了（古い bindPose 整理 → フリーズ → 再バインド → ウェイト復元）",
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
                ja=u"編集完了（全メッシュを再バインド＆復元）",
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
    # タブ構築：ジョイント整理 ＆ Joint Orient
    # ------------------------------------------------------------------
    def _build_joint_orient_tab(self):
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

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
        self.joint_issue_table.setMaximumHeight(125)
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
        axis_grid.addWidget(QLabel("Primary Axis:"), 0, 0)
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
        axis_grid.addWidget(QLabel("Secondary Axis:"), 1, 0)
        self.secondary_group = QButtonGroup(self)
        self.secondary_radios = {}
        for col, ax in enumerate(("X", "Y", "Z", "None"), start=1):
            rb = QRadioButton(ax)
            if ax == "Y":
                rb.setChecked(True)
            rb.toggled.connect(self._on_secondary_axis_changed)
            self.secondary_group.addButton(rb)
            self.secondary_radios[ax] = rb
            axis_grid.addWidget(rb, 1, col)

        # Secondary Axis World Orientation
        axis_grid.addWidget(QLabel("Secondary Axis World Orientation:"), 2, 0)
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

        self.oj_children_chk = QCheckBox("Orient children of selected joints")
        self.oj_children_chk.setChecked(True)
        orient_layout.addWidget(self.oj_children_chk)

        self.oj_scale_chk = QCheckBox("Reorient the local scale axes")
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

        self.toggle_lra_but = QPushButton("Toggle Local Axes Visibility")
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
            lbl = QLabel(u"{0} 軸:".format(ax))
            tweak_grid.addWidget(lbl, row_idx, 0)
            for col_idx, deg in enumerate((-90, 90, 180), start=1):
                sign_txt = "+{0}".format(deg) if deg > 0 else str(deg)
                btn = QPushButton(u"{0}°".format(sign_txt))
                btn.clicked.connect(lambda _=False, a=ax, d=deg: self.run_tweak_orient(a, d))
                tweak_grid.addWidget(btn, row_idx, col_idx)
        layout.addWidget(tweak_box)

        layout.addStretch(1)
        return tab

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

        # 左列：Fix by Tesselation（画像設定に準拠）
        tess_col = QVBoxLayout()
        tess_col.addWidget(QLabel(u"<b>Fix by Tesselation</b>"))
        self.mc_quads_chk = QCheckBox("4-sided faces")
        self.mc_quads_chk.setChecked(False)
        tess_col.addWidget(self.mc_quads_chk)

        self.mc_nsided_chk = QCheckBox(u"Faces with more than 4 sides（5角形以上）")
        self.mc_nsided_chk.setChecked(True)
        tess_col.addWidget(self.mc_nsided_chk)

        self.mc_concave_chk = QCheckBox(u"Concave faces（凹面フェース）")
        self.mc_concave_chk.setChecked(True)
        tess_col.addWidget(self.mc_concave_chk)

        self.mc_holed_chk = QCheckBox(u"Faces with holes（穴あきフェース）")
        self.mc_holed_chk.setChecked(True)
        tess_col.addWidget(self.mc_holed_chk)

        self.mc_nonplanar_chk = QCheckBox(u"Non-planar faces（非平面フェース）")
        self.mc_nonplanar_chk.setChecked(False)
        tess_col.addWidget(self.mc_nonplanar_chk)
        tess_col.addStretch(1)
        mc_cols.addLayout(tess_col, 1)

        # 右列：Remove Geometry（不正ジオメトリ）
        geo_col = QVBoxLayout()
        geo_col.addWidget(QLabel(u"<b>Remove Geometry</b>"))
        self.mc_lamina_chk = QCheckBox(u"Lamina faces（重なりフェース）")
        self.mc_lamina_chk.setChecked(True)
        geo_col.addWidget(self.mc_lamina_chk)

        self.mc_nonmanifold_chk = QCheckBox(u"Nonmanifold geometry（非多様体）")
        self.mc_nonmanifold_chk.setChecked(True)
        geo_col.addWidget(self.mc_nonmanifold_chk)

        self.mc_zero_geom_chk = QCheckBox(u"Faces with zero geometry area（面積 0）")
        self.mc_zero_geom_chk.setChecked(False)
        geo_col.addWidget(self.mc_zero_geom_chk)

        self.mc_zero_edge_chk = QCheckBox(u"Edges with zero length（長さ 0）")
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
        if self.log_window is not None:
            try:
                self.log_window.close()
            except Exception:
                pass
            self.log_window = None
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
            self._set_status("Folder open error: {0}".format(exc), error=True)

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
        """編集対象のジョイントがスキニング接続中であればユーザーへ確認ダイアログを出す。"""
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
            if not fname.lower().endswith(".json"):
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
                lang.Lang(en="Editing", ja=u"編集中").output()
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
        """一覧の中から「編集中」ステータスのバックアップをすべて選択する。"""
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
            lang.Lang(en="Selected {0} editing backup(s).", ja=u"編集中のバックアップを {0} 件選択しました。").output().format(count),
            error=(count == 0),
        )

    def start_modeling_selected(self):
        """選択メッシュまたはジョイントに紐づくスキニングメッシュを JSON 記録し、解除・フリーズする。"""
        meshes = self._get_selected_polygon_transforms()
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        seed_nodes = list(meshes) + list(sel_joints)

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
                    en="No skinned meshes found for the current selection.",
                    ja=u"選択対象に紐づくスキニング済みメッシュが見つかりませんでした。",
                ).output(),
                error=True,
            )
            return

        saved_frame = None
        edit_frame = None
        if self.restore_frame_chk.isChecked():
            try:
                saved_frame = float(cmds.currentTime(query=True))
                edit_frame = float(self.edit_frame_spin.value())
                self._saved_frame = saved_frame
                if abs(saved_frame - edit_frame) > 1e-6:
                    cmds.currentTime(edit_frame, edit=True)
            except Exception as exc:
                print("ModelEdit frame switch warning:", exc)

        if self.bind_pose_chk.isChecked():
            try:
                cmds.select(target_meshes, replace=True)
                mel.eval("gotoBindPose;")
            except Exception:
                pass

        saved_paths = []
        started_names = []
        cmds.undoInfo(openChunk=True)
        try:
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
            self._set_status("Error: {0}".format(exc), error=True)
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        self.reload_record_list(select_paths=saved_paths)
        short_msg = lang.Lang(
            en="Ready ({0} mesh(es)): Weights recorded & unbound.",
            ja=u"編集開始（{0} メッシュ）：ウェイト記録・解除完了。",
        ).output().format(len(started_names))
        if saved_frame is not None and edit_frame is not None:
            short_msg += lang.Lang(
                en=" | Frame: {0} -> {1}",
                ja=u" ｜ フレーム: {0} → {1}",
            ).output().format(_format_frame_value(saved_frame), _format_frame_value(edit_frame))
        detail_lines = []
        if saved_frame is not None and edit_frame is not None:
            detail_lines.append(
                u"フレーム移動: {0} → {1}（完了後に {0} へ復帰）".format(
                    _format_frame_value(saved_frame), _format_frame_value(edit_frame)
                )
            )
        detail_lines.append(u"対象メッシュ一覧:")
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
            self._set_status("Delete error: {0}".format(exc), error=True)

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
            # 編集完了ボタンでは一覧に記録されている全メッシュ（シーン内に存在するもの）を一括復元する
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
                    detail=u"未検出メッシュ:\n  " + "\n  ".join(missing_meshes),
                )
                return

        restore_mode = self.restore_mode_combo.currentData() or "auto"
        smooth_iters = int(self.smooth_spin.value())
        clean_bind_pose = self.clean_bind_pose_chk.isChecked()
        use_frame_restore = self.restore_frame_chk.isChecked()
        edit_frame = float(self.edit_frame_spin.value())

        # 再バインドは必ず基準（0）フレーム上で実行し、仮ポーズのままバインドされる事故を防ぐ
        if use_frame_restore:
            try:
                cur_frame = float(cmds.currentTime(query=True))
                if abs(cur_frame - edit_frame) > 1e-6:
                    cmds.currentTime(edit_frame, edit=True)
            except Exception as exc:
                print("ModelEdit pre-restore frame switch warning:", exc)

        restored_paths = []
        restored_meshes = []
        summaries = []
        total_missing_bones = 0
        max_moved_joints = 0
        saved_frame_to_restore = None

        cmds.undoInfo(openChunk=True)
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
            self._set_status("Restore error: {0}".format(exc), error=True)
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        # 全メッシュのウェイト復元完了後、事前に記録した元フレームへ復帰する
        restored_frame_val = None
        if use_frame_restore:
            if saved_frame_to_restore is None:
                saved_frame_to_restore = self._saved_frame
            if saved_frame_to_restore is not None:
                try:
                    cmds.currentTime(saved_frame_to_restore, edit=True)
                    restored_frame_val = saved_frame_to_restore
                except Exception as exc:
                    print("ModelEdit restore frame warning:", exc)

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
                u"フレーム復帰: {0} → {1}".format(
                    _format_frame_value(edit_frame), _format_frame_value(restored_frame_val)
                )
            )
        detail_lines.append(u"復元メッシュ一覧 ({0} 件):".format(len(summaries)))
        for item in summaries:
            detail_lines.append("  " + item)
        if missing_meshes:
            detail_lines.append(u"シーン内に存在しないためスキップ ({0} 件):".format(len(missing_meshes)))
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
            if info["bad_rot"] or info["bad_scl"] or info["bad_ra"]:
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
            self._set_status("Orient Joint error: {0}".format(exc), error=True)
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

        state = rig_cleanup_helper.toggle_local_axes_visibility(joints)
        label = "ON" if state else "OFF"
        self._set_status(
            lang.Lang(
                en="Local Rotation Axes: {0} ({1} joint(s)).",
                ja=u"ローカル回転軸表示を {0} に切り替えました（{1} 本）",
            ).output().format(label, len(joints)),
            error=False,
        )

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
            self._set_status("Mesh Cleanup error: {0}".format(exc), error=True)
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
        summary = (
            u"診断結果 ｜ 未使用中間シェイプ: {0} 件 / 孤立 bindPose: {1} 件 / "
            u"未知ノード: {2} 件 / 空グループ: {3} 件"
        ).format(
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
            self._set_status("Scene Cleanup error: {0}".format(exc), error=True)
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        summary = (
            u"シーンクリーンアップ完了 ｜ 中間シェイプ: {0} / 孤立 bindPose: {1} / "
            u"未知ノード: {2} / 空グループ: {3} / 未使用シェーダー: {4}"
        ).format(
            report["unused_intermediates"],
            report["orphaned_bind_poses"],
            report["unknown_nodes"],
            report["empty_groups"],
            report["unused_shading"],
        )
        self.scene_scan_label.setText(summary)
        self._set_status(summary, error=False)
