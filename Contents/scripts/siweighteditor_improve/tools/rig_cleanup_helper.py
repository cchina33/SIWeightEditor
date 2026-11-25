# -*- coding: utf-8 -*-
"""ジョイント値整理・Joint Orient・メッシュクリーンアップ・シーンクリーンアップ用ヘルパー。"""

import datetime as dt
import json
import math
import os
import re

from maya import cmds
from maya import mel
import maya.api.OpenMaya as om2
try:
    import maya.api.OpenMayaAnim as oma2
except Exception:
    oma2 = None


EPSILON_ROT = 1e-4
EPSILON_SCALE = 1e-4
EPSILON_TRANS = 1e-4
POSE_BACKUP_SUBDIR = "_joint_pose_backups"


def _dag_depth(dag_path):
    """DAG フルパスの階層深さを返す。"""
    return dag_path.count("|")


def _get_orthonormal_rotation_matrix(node):
    """ノードのワールド変換行列からスケール成分を除いた純粋回転行列（4x4）を返す。"""
    mat_list = cmds.xform(node, q=True, ws=True, m=True)
    x_vec = om2.MVector(mat_list[0], mat_list[1], mat_list[2])
    y_vec = om2.MVector(mat_list[4], mat_list[5], mat_list[6])
    z_vec = om2.MVector(mat_list[8], mat_list[9], mat_list[10])

    if x_vec.length() > 1e-9:
        x_vec.normalize()
    else:
        x_vec = om2.MVector(1.0, 0.0, 0.0)

    # グラム・シュミット直交化で非均一スケールやせん断の影響を取り除く
    y_vec = y_vec - x_vec * (y_vec * x_vec)
    if y_vec.length() > 1e-9:
        y_vec.normalize()
    else:
        fallback = om2.MVector(0.0, 1.0, 0.0) if abs(x_vec.y) < 0.9 else om2.MVector(0.0, 0.0, 1.0)
        y_vec = (fallback - x_vec * (fallback * x_vec)).normal()

    z_ortho = (x_vec ^ y_vec)
    if z_ortho.length() > 1e-9:
        z_ortho.normalize()
    else:
        z_ortho = om2.MVector(0.0, 0.0, 1.0)

    # 右手系を保つため元の Z 方向との内積で反転チェックは行わず、直交基底として構築する
    return om2.MMatrix([
        x_vec.x, x_vec.y, x_vec.z, 0.0,
        y_vec.x, y_vec.y, y_vec.z, 0.0,
        z_ortho.x, z_ortho.y, z_ortho.z, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ])


def _get_parent_rotation_matrix(node):
    """ノードの親トランスフォームの純粋回転行列を返す（ルートの場合は単位行列）。"""
    parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
    if not parents:
        return om2.MMatrix()
    return _get_orthonormal_rotation_matrix(parents[0])


def _is_attr_editable(node, prefix):
    """指定 XYZ アトリビュート群が編集可能かつ外部入力接続を持たないか確認する。"""
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


def _set_joint_world_rot_to_orient(jnt, target_world_rot_mat):
    """ジョイントのワールド回転行列を jointOrient（XYZ）へ焼き込み、rotate と rotateAxis を 0 にする。"""
    if not (_is_attr_editable(jnt, "jointOrient") and _is_attr_editable(jnt, "rotate")):
        return False

    parent_rot_mat = _get_parent_rotation_matrix(jnt)
    local_rot_mat = target_world_rot_mat * parent_rot_mat.inverse()
    euler = om2.MTransformationMatrix(local_rot_mat).rotation(asQuaternion=False)
    deg_x = math.degrees(euler.x)
    deg_y = math.degrees(euler.y)
    deg_z = math.degrees(euler.z)

    # 微小ノイズを 0 に丸めて数値を綺麗に保つ
    deg_x = 0.0 if abs(deg_x) < 1e-6 else deg_x
    deg_y = 0.0 if abs(deg_y) < 1e-6 else deg_y
    deg_z = 0.0 if abs(deg_z) < 1e-6 else deg_z

    cmds.setAttr(jnt + ".rotate", 0.0, 0.0, 0.0, type="double3")
    if _is_attr_editable(jnt, "rotateAxis"):
        cmds.setAttr(jnt + ".rotateAxis", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(jnt + ".jointOrient", deg_x, deg_y, deg_z, type="double3")
    return True


def collect_target_joints(include_children=True):
    """現在選択中のジョイント（必要に応じて子階層含む）を親から子の順で返す。"""
    sel = cmds.ls(sl=True, type="joint", long=True) or []
    if not sel:
        return []

    found = []
    seen = set()
    for jnt in sel:
        if jnt not in seen:
            seen.add(jnt)
            found.append(jnt)
        if include_children:
            desc = cmds.listRelatives(jnt, allDescendents=True, type="joint", fullPath=True) or []
            for child in desc:
                if child not in seen:
                    seen.add(child)
                    found.append(child)

    found.sort(key=_dag_depth)
    return found


def find_skin_clusters_for_joints(joints):
    """指定ジョイント群に接続されているスキンクラスタ名の一覧を返す。"""
    skins = []
    seen = set()
    for jnt in joints:
        if not cmds.objExists(jnt):
            continue
        conns = cmds.listConnections(jnt, type="skinCluster", source=False, destination=True) or []
        for sc in conns:
            if sc not in seen:
                seen.add(sc)
                skins.append(sc)
    return skins


def find_meshes_sharing_skeleton(seed_nodes):
    """選択メッシュまたはジョイントから同一スケルトンを共有する全スキニングメッシュを収集する。"""
    seed_joints = []
    seen_j = set()

    for node in seed_nodes:
        if not cmds.objExists(node):
            continue
        ntype = cmds.nodeType(node)
        if ntype == "joint":
            if node not in seen_j:
                seen_j.add(node)
                seed_joints.append(node)
        else:
            history = cmds.listHistory(node, pruneDagObjects=True) or []
            skins = cmds.ls(history, type="skinCluster") or []
            if not skins:
                history = cmds.listHistory(node) or []
                skins = cmds.ls(history, type="skinCluster") or []
            for sc in skins:
                infs = cmds.skinCluster(sc, q=True, inf=True) or []
                for inf in cmds.ls(infs, long=True) or []:
                    if cmds.nodeType(inf) == "joint" and inf not in seen_j:
                        seen_j.add(inf)
                        seed_joints.append(inf)

    if not seed_joints:
        return []

    # 各シードジョイントのルートジョイントを辿り、スケルトン配下すべてのジョイントを集める
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
            if child not in seen_all:
                seen_all.add(child)
                all_joints.append(child)

    # スケルトン全体に接続されているスキンクラスタから変形対象メッシュを取得する
    skins = find_skin_clusters_for_joints(all_joints)
    meshes = []
    seen_m = set()
    for sc in skins:
        geos = cmds.skinCluster(sc, q=True, geometry=True) or []
        for geo in cmds.ls(geos, long=True) or []:
            if cmds.nodeType(geo) == "mesh":
                parents = cmds.listRelatives(geo, parent=True, fullPath=True) or []
                if parents and parents[0] not in seen_m:
                    seen_m.add(parents[0])
                    meshes.append(parents[0])
            elif cmds.nodeType(geo) == "transform":
                shapes = cmds.listRelatives(geo, shapes=True, noIntermediate=True, type="mesh", fullPath=True) or []
                if shapes and geo not in seen_m:
                    seen_m.add(geo)
                    meshes.append(geo)
    return meshes


def snapshot_joints_transform(joint_paths):
    """ジョイント一覧のワールド座標・回転・Joint Orient・スケール情報を辞書化して返す。"""
    snapshot = {}
    for jnt in joint_paths:
        if not cmds.objExists(jnt):
            continue
        try:
            ws_pos = cmds.xform(jnt, q=True, ws=True, t=True)
            rot = cmds.getAttr(jnt + ".rotate")[0]
            jo = cmds.getAttr(jnt + ".jointOrient")[0] if cmds.attributeQuery("jointOrient", node=jnt, exists=True) else (0.0, 0.0, 0.0)
            scl = cmds.getAttr(jnt + ".scale")[0]
            snapshot[jnt] = {
                "ws_pos": [float(ws_pos[0]), float(ws_pos[1]), float(ws_pos[2])],
                "rotate": [float(rot[0]), float(rot[1]), float(rot[2])],
                "jointOrient": [float(jo[0]), float(jo[1]), float(jo[2])],
                "scale": [float(scl[0]), float(scl[1]), float(scl[2])],
            }
        except Exception:
            continue
    return snapshot


def count_moved_joints(saved_snapshot):
    """JSON 記録時のジョイント座標と現在座標を比較し、移動・変更された本数を返す。"""
    if not saved_snapshot:
        return 0
    moved = 0
    for jnt_path, data in saved_snapshot.items():
        target = jnt_path if cmds.objExists(jnt_path) else jnt_path.split("|")[-1]
        if not cmds.objExists(target):
            continue
        try:
            old_p = data.get("ws_pos")
            if not old_p:
                continue
            cur_p = cmds.xform(target, q=True, ws=True, t=True)
            dx = cur_p[0] - old_p[0]
            dy = cur_p[1] - old_p[1]
            dz = cur_p[2] - old_p[2]
            if (dx * dx + dy * dy + dz * dz) > 1e-6:
                moved += 1
        except Exception:
            continue
    return moved


def cleanup_orphaned_bind_poses(joint_paths=None):
    """スキンクラスタに接続されていない孤立した bindPose（dagPose）を削除する。"""
    if joint_paths:
        candidates = []
        seen = set()
        for jnt in joint_paths:
            if not cmds.objExists(jnt):
                continue
            dps = cmds.listConnections(jnt, type="dagPose") or []
            for dp in dps:
                if dp not in seen:
                    seen.add(dp)
                    candidates.append(dp)
    else:
        candidates = cmds.ls(type="dagPose") or []

    removed = []
    for dp in candidates:
        if not cmds.objExists(dp):
            continue
        try:
            is_bind = cmds.getAttr(dp + ".bindPose")
        except Exception:
            is_bind = False
        if not is_bind:
            continue
        # 稼働中のスキンクラスタが参照している bindPose は保護する
        connected_skins = cmds.listConnections(dp + ".message", type="skinCluster", source=False, destination=True) or []
        if connected_skins:
            continue
        try:
            cmds.delete(dp)
            removed.append(dp)
        except Exception:
            pass
    return removed


def _has_attr_incoming_connection(node, prefix):
    """指定トランスフォーム属性（複合または X/Y/Z）に外部入力接続があるか判定する。"""
    attrs = [
        "{0}.{1}".format(node, prefix),
        "{0}.{1}X".format(node, prefix),
        "{0}.{1}Y".format(node, prefix),
        "{0}.{1}Z".format(node, prefix),
    ]
    for attr in attrs:
        try:
            if cmds.listConnections(attr, source=True, destination=False):
                return True
        except Exception:
            pass
    return False


def _has_attr_locked(node, prefix):
    """指定トランスフォーム属性（複合または X/Y/Z）がロックされているか判定する。"""
    attrs = [
        "{0}.{1}".format(node, prefix),
        "{0}.{1}X".format(node, prefix),
        "{0}.{1}Y".format(node, prefix),
        "{0}.{1}Z".format(node, prefix),
    ]
    for attr in attrs:
        try:
            if cmds.getAttr(attr, lock=True):
                return True
        except Exception:
            pass
    return False


def inspect_joints(joints):
    """ジョイント群の Rotate / Scale / RotateAxis / Translate / 接続・ロック状態を診断して返す。"""
    issues = []
    for jnt in joints:
        if not cmds.objExists(jnt):
            continue
        rot = cmds.getAttr(jnt + ".rotate")[0]
        scl = cmds.getAttr(jnt + ".scale")[0]
        ra = cmds.getAttr(jnt + ".rotateAxis")[0]
        trans = cmds.getAttr(jnt + ".translate")[0]
        parents = cmds.listRelatives(jnt, parent=True, type="joint", fullPath=True) or []

        bad_rot = any(abs(v) > EPSILON_ROT for v in rot)
        bad_scl = any(abs(v - 1.0) > EPSILON_SCALE for v in scl)
        bad_ra = any(abs(v) > EPSILON_ROT for v in ra)
        # 親がジョイントの場合、主軸（X）以外の平行移動ズレも参考情報として検出する
        bad_ty_tz = bool(parents) and (abs(trans[1]) > EPSILON_TRANS or abs(trans[2]) > EPSILON_TRANS)
        has_conn = any(
            _has_attr_incoming_connection(jnt, pfx)
            for pfx in ("translate", "rotate", "scale", "jointOrient", "rotateAxis")
        )
        has_lock = any(
            _has_attr_locked(jnt, pfx)
            for pfx in ("translate", "rotate", "scale", "jointOrient", "rotateAxis")
        )

        if bad_rot or bad_scl or bad_ra or bad_ty_tz or has_conn or has_lock:
            tags = []
            if bad_rot:
                tags.append("Rotate!=0")
            if bad_scl:
                tags.append("Scale!=1")
            if bad_ra:
                tags.append("RotAxis!=0")
            if bad_ty_tz:
                tags.append("Ty/Tz!=0")
            if has_conn:
                tags.append("Connected")
            if has_lock:
                tags.append("Locked")
            issues.append({
                "joint": jnt,
                "short_name": jnt.split("|")[-1],
                "bad_rot": bad_rot,
                "bad_scl": bad_scl,
                "bad_ra": bad_ra,
                "bad_ty_tz": bad_ty_tz,
                "has_conn": has_conn,
                "has_lock": has_lock,
                "rotate": rot,
                "scale": scl,
                "rotateAxis": ra,
                "translate": trans,
                "summary": ", ".join(tags),
            })
    return issues


def normalize_joint_scales(joints):
    """子ノードのワールド位置・回転を維持したまま、対象ジョイントの Scale を (1, 1, 1) へ整理する。"""
    ordered = sorted(set(joints), key=_dag_depth)
    fixed = []
    skipped = []

    for jnt in ordered:
        if not cmds.objExists(jnt):
            continue
        scl = cmds.getAttr(jnt + ".scale")[0]
        if all(abs(v - 1.0) <= EPSILON_SCALE for v in scl):
            continue
        if not _is_attr_editable(jnt, "scale"):
            skipped.append(jnt)
            continue

        direct_children = cmds.listRelatives(jnt, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            ws_pos = cmds.xform(child, q=True, ws=True, t=True)
            ws_rot_mat = _get_orthonormal_rotation_matrix(child)
            is_jnt = cmds.nodeType(child) == "joint"
            had_zero_rot = False
            if is_jnt:
                c_rot = cmds.getAttr(child + ".rotate")[0]
                had_zero_rot = all(abs(v) <= EPSILON_ROT for v in c_rot)
            saved_children.append((child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot))

        cmds.setAttr(jnt + ".scale", 1.0, 1.0, 1.0, type="double3")

        for child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot in saved_children:
            if not cmds.objExists(child):
                continue
            try:
                if _is_attr_editable(child, "translate"):
                    cmds.xform(child, ws=True, t=ws_pos)
                if is_jnt and had_zero_rot:
                    _set_joint_world_rot_to_orient(child, ws_rot_mat)
            except Exception:
                pass
        fixed.append(jnt)

    return fixed, skipped


def freeze_joint_rotations_to_orient(joints):
    """対象ジョイントの Rotate と RotateAxis を jointOrient へ焼き込み、Rotate = (0, 0, 0) に整理する。"""
    ordered = sorted(set(joints), key=_dag_depth)
    fixed = []
    skipped = []

    for jnt in ordered:
        if not cmds.objExists(jnt):
            continue
        rot = cmds.getAttr(jnt + ".rotate")[0]
        ra = cmds.getAttr(jnt + ".rotateAxis")[0]
        if all(abs(v) <= EPSILON_ROT for v in rot) and all(abs(v) <= EPSILON_ROT for v in ra):
            continue

        world_rot_mat = _get_orthonormal_rotation_matrix(jnt)
        direct_children = cmds.listRelatives(jnt, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            ws_pos = cmds.xform(child, q=True, ws=True, t=True)
            ws_rot_mat = _get_orthonormal_rotation_matrix(child)
            is_jnt = cmds.nodeType(child) == "joint"
            had_zero_rot = False
            if is_jnt:
                c_rot = cmds.getAttr(child + ".rotate")[0]
                had_zero_rot = all(abs(v) <= EPSILON_ROT for v in c_rot)
            saved_children.append((child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot))

        ok = _set_joint_world_rot_to_orient(jnt, world_rot_mat)
        if not ok:
            skipped.append(jnt)
            continue

        for child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot in saved_children:
            if not cmds.objExists(child):
                continue
            try:
                if _is_attr_editable(child, "translate"):
                    cmds.xform(child, ws=True, t=ws_pos)
                if is_jnt and had_zero_rot:
                    _set_joint_world_rot_to_orient(child, ws_rot_mat)
            except Exception:
                pass
        fixed.append(jnt)

    return fixed, skipped


# ------------------------------------------------------------------
# ポーズ退避（JSON）・接続解除フリーズ・新軸へのポーズ書き戻し
# ------------------------------------------------------------------
def get_pose_backup_dir(base_storage_dir):
    """ジョイントポーズ退避用 JSON フォルダパスを返す。"""
    path = os.path.join(base_storage_dir, POSE_BACKUP_SUBDIR)
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _mul_mat4(a, b):
    """行優先の 4x4 行列（16要素リスト）同士の積 a * b を返す。"""
    out = [0.0] * 16
    for r in range(4):
        r4 = r * 4
        a0, a1, a2, a3 = a[r4], a[r4 + 1], a[r4 + 2], a[r4 + 3]
        for c in range(4):
            out[r4 + c] = a0 * b[c] + a1 * b[4 + c] + a2 * b[8 + c] + a3 * b[12 + c]
    return out


def _transpose_rot3_mat4(m):
    """純粋回転 4x4 行列の逆行列（左上 3x3 の転置）を返す。"""
    return [
        m[0], m[4], m[8], 0.0,
        m[1], m[5], m[9], 0.0,
        m[2], m[6], m[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _euler_xyz_deg_to_mat4(deg_xyz):
    """XYZ 順オイラー角（度）を Maya 行ベクトル規約の 4x4 回転行列（16要素）へ変換する。"""
    rx = math.radians(float(deg_xyz[0]))
    ry = math.radians(float(deg_xyz[1]))
    rz = math.radians(float(deg_xyz[2]))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    mx = [
        1.0, 0.0, 0.0, 0.0,
        0.0, cx, sx, 0.0,
        0.0, -sx, cx, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    my = [
        cy, 0.0, -sy, 0.0,
        0.0, 1.0, 0.0, 0.0,
        sy, 0.0, cy, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    mz = [
        cz, sz, 0.0, 0.0,
        -sz, cz, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    return _mul_mat4(_mul_mat4(mx, my), mz)


def compute_local_rotate_euler_from_matrices(
    target_world_rot_mat,
    parent_world_rot_mat,
    joint_orient_deg,
    rotate_order=0,
):
    """目標ワールド回転・親ワールド回転・新 jointOrient からローカル回転オイラー角（度）を逆算する。"""
    jo_mat = _euler_xyz_deg_to_mat4(joint_orient_deg)
    # Maya 行ベクトル規約: M_world = R_local * R_jo * R_parent
    # よって R_local = M_world * R_parent^-1 * R_jo^-1
    jo_parent = _mul_mat4(jo_mat, parent_world_rot_mat)
    local_rot_mat = _mul_mat4(target_world_rot_mat, _transpose_rot3_mat4(jo_parent))

    m = om2.MMatrix(local_rot_mat)
    euler = om2.MTransformationMatrix(m).rotation(asQuaternion=False)
    try:
        euler.reorderIt(int(rotate_order))
    except Exception:
        pass
    dx = math.degrees(euler.x)
    dy = math.degrees(euler.y)
    dz = math.degrees(euler.z)
    dx = 0.0 if abs(dx) < 1e-6 else dx
    dy = 0.0 if abs(dy) < 1e-6 else dy
    dz = 0.0 if abs(dz) < 1e-6 else dz
    return [dx, dy, dz]


def _ensure_attr_unlocked_for_key(node, prefix):
    """キーフレーム書き戻し前に複合属性および X/Y/Z 各軸のロックを解除する（animCurve 接続は維持）。"""
    for suffix in ("", "X", "Y", "Z"):
        attr = "{0}.{1}{2}".format(node, prefix, suffix)
        if not cmds.objExists(attr):
            continue
        try:
            if cmds.getAttr(attr, lock=True):
                cmds.setAttr(attr, lock=False)
        except Exception:
            pass
    return True


def _collect_joint_key_times(joints):
    """対象ジョイント群に打たれているキーフレーム時刻（昇順ユニーク）とキー付き属性を集約する。"""
    all_times = set()
    keyed_channels_by_joint = {}
    channel_times_by_joint = {}
    for jnt in joints:
        if not cmds.objExists(jnt):
            continue
        ch_set = set()
        ch_times_map = {}
        for ch in ("translate", "rotate", "scale"):
            this_ch_times = set()
            for axis in ("X", "Y", "Z"):
                attr = "{0}.{1}{2}".format(jnt, ch, axis)
                t_list = []
                try:
                    t_list = cmds.keyframe(attr, q=True, timeChange=True) or []
                except Exception:
                    t_list = []
                if not t_list:
                    # unitConversion やアニメーションレイヤ・ブレンドノード経由の animCurve も検出する
                    try:
                        hist = cmds.listHistory(attr, pruneDagObjects=True) or []
                        curves = cmds.ls(hist, type="animCurve") or []
                        if curves:
                            t_list = cmds.keyframe(curves, q=True, timeChange=True) or []
                    except Exception:
                        t_list = []
                if t_list:
                    for t in t_list:
                        rt = round(float(t), 6)
                        all_times.add(rt)
                        this_ch_times.add(rt)
            if this_ch_times:
                ch_set.add(ch)
                ch_times_map[ch] = sorted(this_ch_times)
        keyed_channels_by_joint[jnt] = sorted(ch_set)
        channel_times_by_joint[jnt] = ch_times_map
    return sorted(all_times), keyed_channels_by_joint, channel_times_by_joint


def export_joints_pose_json(joints, base_storage_dir, extra_frames=None, rotate_adjust_deg=None):
    """対象ジョイント群の現在姿勢と全キーフレームのワールド姿勢・ローカル値を JSON へ保存する。"""
    ordered = sorted(set(j for j in (joints or []) if cmds.objExists(j)), key=_dag_depth)
    if not ordered:
        raise RuntimeError("No valid joints to export pose.")

    orig_time = float(cmds.currentTime(query=True))
    orig_time_r = round(orig_time, 6)
    key_times, keyed_channels_map, channel_times_map = _collect_joint_key_times(ordered)
    has_keys = bool(key_times)

    has_rot_adj = bool(rotate_adjust_deg) and any(abs(float(v)) > 1e-6 for v in rotate_adjust_deg)
    adj_mat = _euler_xyz_deg_to_mat4(rotate_adjust_deg) if has_rot_adj else None

    sample_times = list(key_times)
    if not sample_times:
        sample_times = [orig_time_r]
    elif orig_time_r not in sample_times:
        sample_times.append(orig_time_r)
    for ef in (extra_frames or []):
        ef_r = round(float(ef), 6)
        if ef_r not in sample_times:
            sample_times.append(ef_r)
    sample_times.sort()

    # 元の入力接続情報も参考メタデータとして記録しておく
    connections_meta = {}
    for jnt in ordered:
        c_list = []
        for pfx in ("translate", "rotate", "scale", "jointOrient", "rotateAxis"):
            for suffix in ("", "X", "Y", "Z"):
                dst = "{0}.{1}{2}".format(jnt, pfx, suffix)
                try:
                    srcs = cmds.listConnections(dst, source=True, destination=False, plugs=True) or []
                    for src in srcs:
                        c_list.append({"src": src, "dst": dst})
                except Exception:
                    pass
        if c_list:
            connections_meta[jnt] = c_list

    joint_meta = []
    for jnt in ordered:
        parents = cmds.listRelatives(jnt, parent=True, fullPath=True) or []
        joint_meta.append({
            "full_path": jnt,
            "short_name": jnt.split("|")[-1],
            "parent_full": parents[0] if parents else "",
            "rotateOrder": int(cmds.getAttr(jnt + ".rotateOrder")),
            "keyed_channels": keyed_channels_map.get(jnt, []),
            "channel_key_times": channel_times_map.get(jnt, {}),
        })

    def _capture_current_frame(apply_adj=False):
        j_poses = {}
        for jnt in ordered:
            if not cmds.objExists(jnt):
                continue
            ws_pos = cmds.xform(jnt, q=True, ws=True, t=True)
            ws_rot_mat = _get_orthonormal_rotation_matrix(jnt)
            ws_rot_vals = [float(ws_rot_mat[i]) for i in range(16)]
            if apply_adj and adj_mat is not None:
                ws_rot_vals = _mul_mat4(adj_mat, ws_rot_vals)
            tr = cmds.getAttr(jnt + ".translate")[0]
            rt = cmds.getAttr(jnt + ".rotate")[0]
            sc = cmds.getAttr(jnt + ".scale")[0]
            j_poses[jnt] = {
                "ws_pos": [float(ws_pos[0]), float(ws_pos[1]), float(ws_pos[2])],
                "ws_rot_mat": ws_rot_vals,
                "translate": [float(tr[0]), float(tr[1]), float(tr[2])],
                "rotate": [float(rt[0]), float(rt[1]), float(rt[2])],
                "scale": [float(sc[0]), float(sc[1]), float(sc[2])],
            }
        return j_poses

    # ビューポートで手動調整した直後の未キー姿勢がフレーム移動で消えないよう、現在フレームを最優先で取得する
    captured_by_time = {}
    if orig_time_r in sample_times:
        captured_by_time[orig_time_r] = _capture_current_frame(apply_adj=has_rot_adj)

    try:
        for t in sample_times:
            if t in captured_by_time:
                continue
            if abs(float(cmds.currentTime(query=True)) - t) > 1e-6:
                cmds.currentTime(t, edit=True)
            captured_by_time[t] = _capture_current_frame(apply_adj=False)
    finally:
        if abs(float(cmds.currentTime(query=True)) - orig_time) > 1e-6:
            cmds.currentTime(orig_time, edit=True)

    frames_data = []
    for t in sample_times:
        frames_data.append({
            "time": float(t),
            "is_key_frame": (t in key_times) or (has_rot_adj and t == orig_time_r),
            "joints": captured_by_time.get(t, {}),
        })

    backup_dir = get_pose_backup_dir(base_storage_dir)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    head_name = re.sub(r'[\\/:*?"<>|]+', "_", ordered[0].split("|")[-1]).strip("_") or "joints"
    save_path = os.path.join(backup_dir, "joint_pose_{0}_{1}.json".format(head_name, stamp))

    payload = {
        "version": 1,
        "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "orig_time": orig_time,
        "has_keys": has_keys,
        "key_times": key_times,
        "joint_count": len(ordered),
        "joints": joint_meta,
        "incoming_connections": connections_meta,
        "frames": frames_data,
    }
    with open(save_path, "w") as fp:
        json.dump(payload, fp, indent=2)
    return save_path, payload


def break_and_unlock_joint_attrs(joints, channels=("rotate", "scale", "jointOrient", "rotateAxis"), delete_unused_anim_curves=True):
    """対象ジョイントの指定チャンネルのロック解除と外部入力接続の切断を安全に行う。"""
    unlocked_count = 0
    broken_list = []
    candidate_cleanup_nodes = set()

    for jnt in (joints or []):
        if not cmds.objExists(jnt):
            continue
        for ch in channels:
            attr_names = [
                "{0}.{1}".format(jnt, ch),
                "{0}.{1}X".format(jnt, ch),
                "{0}.{1}Y".format(jnt, ch),
                "{0}.{1}Z".format(jnt, ch),
            ]
            for dst_attr in attr_names:
                if not cmds.objExists(dst_attr):
                    continue
                # ロック解除
                try:
                    if cmds.getAttr(dst_attr, lock=True):
                        cmds.setAttr(dst_attr, lock=False)
                        unlocked_count += 1
                except Exception:
                    pass
                # 外部入力接続を切断（複合属性・個別軸の接続ペアを正確に取得して切断する）
                try:
                    conns = cmds.listConnections(
                        dst_attr,
                        source=True,
                        destination=False,
                        connections=True,
                        plugs=True,
                    ) or []
                    for idx in range(0, len(conns), 2):
                        actual_dst = conns[idx]
                        actual_src = conns[idx + 1]
                        try:
                            src_node = actual_src.split(".")[0]
                            if src_node and cmds.objExists(src_node):
                                candidate_cleanup_nodes.add(src_node)
                                # unitConversion 経由の animCurve も孤立掃除候補に追加しておく
                                try:
                                    if cmds.nodeType(src_node) == "unitConversion":
                                        up_nodes = cmds.listConnections(src_node, source=True, destination=False) or []
                                        for up_n in up_nodes:
                                            if cmds.objExists(up_n):
                                                candidate_cleanup_nodes.add(up_n)
                                except Exception:
                                    pass
                            if cmds.isConnected(actual_src, actual_dst):
                                cmds.disconnectAttr(actual_src, actual_dst)
                                broken_list.append("{0} -> {1}".format(actual_src, actual_dst))
                        except Exception:
                            pass
                except Exception:
                    pass

    removed_nodes = 0
    if delete_unused_anim_curves and candidate_cleanup_nodes:
        # unitConversion を先に消してから孤立した animCurve を消せるよう 2 パスで確認する
        for _ in range(2):
            for node in list(candidate_cleanup_nodes):
                if not cmds.objExists(node):
                    continue
                try:
                    ntype = cmds.nodeType(node)
                    is_anim_or_conv = ntype.startswith("animCurve") or ntype == "unitConversion"
                    if not is_anim_or_conv:
                        continue
                    out_conns = cmds.listConnections(node, source=False, destination=True) or []
                    if not out_conns:
                        cmds.delete(node)
                        removed_nodes += 1
                except Exception:
                    pass

    return {
        "unlocked": unlocked_count,
        "broken": broken_list,
        "removed_nodes": removed_nodes,
    }


def force_freeze_joints(
    joints,
    freeze_rotate=True,
    freeze_scale=True,
    freeze_translate=False,
    delete_unused_anim_curves=True,
    also_break_translate_for_pose_restore=False,
    pre_freeze_overrides=None,
    rotate_adjust_deg=None,
):
    """入力接続・ロックを解除した上でジョイントのフリーズ（Scale=1 / Rotate=0 焼込 / 任意 Translate）を実行する。"""
    ordered = sorted(set(j for j in (joints or []) if cmds.objExists(j)), key=_dag_depth)
    if not ordered:
        return {
            "processed": 0,
            "scale_fixed": 0,
            "rot_fixed": 0,
            "trans_fixed": 0,
            "unlocked": 0,
            "broken": [],
            "removed_nodes": 0,
            "skipped": [],
        }

    channels = []
    if freeze_rotate:
        channels.extend(["rotate", "jointOrient", "rotateAxis"])
    if freeze_scale:
        channels.append("scale")
    if freeze_translate or also_break_translate_for_pose_restore:
        channels.append("translate")

    break_report = break_and_unlock_joint_attrs(
        ordered,
        channels=channels,
        delete_unused_anim_curves=delete_unused_anim_curves,
    )

    # 接続切断やタイムライン移動による DG 再評価で失われやすい未キー入力値（ピンク色）をフリーズ直前に復元する
    if pre_freeze_overrides:
        for jnt in ordered:
            ov = pre_freeze_overrides.get(jnt)
            if not ov or not cmds.objExists(jnt):
                continue
            try:
                if freeze_scale and ov.get("scale") is not None:
                    sc = ov["scale"]
                    cmds.setAttr(jnt + ".scale", float(sc[0]), float(sc[1]), float(sc[2]), type="double3")
                if freeze_rotate and ov.get("rotateAxis") is not None:
                    ra = ov["rotateAxis"]
                    cmds.setAttr(jnt + ".rotateAxis", float(ra[0]), float(ra[1]), float(ra[2]), type="double3")
                if freeze_rotate and ov.get("rotate") is not None:
                    rt = ov["rotate"]
                    cmds.setAttr(jnt + ".rotate", float(rt[0]), float(rt[1]), float(rt[2]), type="double3")
            except Exception:
                pass

    has_rot_adj = bool(rotate_adjust_deg) and any(abs(float(v)) > 1e-6 for v in rotate_adjust_deg)
    if has_rot_adj:
        rx, ry, rz = float(rotate_adjust_deg[0]), float(rotate_adjust_deg[1]), float(rotate_adjust_deg[2])
        for jnt in ordered:
            if not cmds.objExists(jnt):
                continue
            try:
                cmds.rotate(rx, ry, rz, jnt, relative=True, objectSpace=True)
            except Exception:
                pass

    scale_fixed = []
    rot_fixed = []
    trans_fixed = []
    skipped = []

    if freeze_scale:
        s_fix, s_skip = normalize_joint_scales(ordered)
        scale_fixed = s_fix
        skipped.extend(s_skip)

    if freeze_rotate:
        r_fix, r_skip = freeze_joint_rotations_to_orient(ordered)
        rot_fixed = r_fix
        skipped.extend(r_skip)

    if freeze_translate:
        for jnt in ordered:
            if not cmds.objExists(jnt):
                continue
            try:
                cmds.makeIdentity(jnt, apply=True, translate=True, rotate=False, scale=False, normal=False)
                trans_fixed.append(jnt)
            except Exception:
                skipped.append(jnt)

    return {
        "processed": len(ordered),
        "scale_fixed": len(scale_fixed),
        "rot_fixed": len(rot_fixed),
        "trans_fixed": len(trans_fixed),
        "unlocked": break_report["unlocked"],
        "broken": break_report["broken"],
        "removed_nodes": break_report["removed_nodes"],
        "skipped": sorted(set(skipped)),
    }


def _time_in_list(target_t, time_list):
    """指定時刻 target_t が時刻リストに含まれているか（許容誤差 1e-4）を判定する。"""
    for kt in (time_list or []):
        if abs(float(kt) - float(target_t)) <= 1e-4:
            return True
    return False


def import_joints_pose_json(
    payload,
    restore_rotate=True,
    restore_translate=True,
    restore_scale=True,
    scale_was_frozen=False,
    apply_euler_filter=True,
    restore_orig_time=True,
):
    """JSON 退避されたワールド姿勢をフリーズ後の新 jointOrient に合わせて逆算し、XYZ=0 を含め全軸へキーフレームを書き戻す。"""
    joint_metas = payload.get("joints") or []
    frames = payload.get("frames") or []
    has_keys = bool(payload.get("has_keys", False))
    orig_time = payload.get("orig_time")

    if not joint_metas or not frames:
        return {"restored_joints": 0, "restored_frames": 0, "missing_joints": [], "restored_keys": 0}

    # シーン上のジョイントを解決し、親→子順（階層深さ順）に並べる
    resolved_list = []
    missing_joints = []
    for meta in joint_metas:
        fp = meta.get("full_path") or ""
        sn = meta.get("short_name") or fp.split("|")[-1]
        live = None
        if fp and cmds.objExists(fp):
            live = fp
        elif sn and cmds.objExists(sn):
            found = cmds.ls(sn, type="joint", long=True) or []
            live = found[0] if found else None
        if live:
            resolved_list.append((fp, live, meta))
        else:
            missing_joints.append(sn or fp)

    resolved_list.sort(key=lambda item: _dag_depth(item[1]))
    if not resolved_list:
        return {"restored_joints": 0, "restored_frames": 0, "missing_joints": missing_joints, "restored_keys": 0}

    cur_time_backup = float(cmds.currentTime(query=True))
    restored_frame_count = 0
    restored_key_count = 0
    keyed_joints = set()

    # キーがある場合はキーフレーム時刻を書き戻す
    target_frames = [f for f in frames if f.get("is_key_frame")] if has_keys else frames
    if not target_frames:
        target_frames = frames

    try:
        for frame_entry in target_frames:
            t = float(frame_entry["time"])
            if has_keys or len(target_frames) > 1:
                if abs(float(cmds.currentTime(query=True)) - t) > 1e-6:
                    cmds.currentTime(t, edit=True)

            j_poses = frame_entry.get("joints") or {}
            for orig_fp, live_jnt, meta in resolved_list:
                pose = j_poses.get(orig_fp)
                if not pose or not cmds.objExists(live_jnt):
                    continue
                keyed_ch = set(meta.get("keyed_channels") or [])
                ch_times_map = meta.get("channel_key_times") or {}

                # Translate の書き戻し（元々キーがあった場合、またはキー無しで位置補正が必要な場合）
                if restore_translate and ((not has_keys) or ("translate" in keyed_ch)):
                    ws_pos = pose.get("ws_pos")
                    if ws_pos:
                        try:
                            _ensure_attr_unlocked_for_key(live_jnt, "translate")
                            cmds.xform(live_jnt, ws=True, t=ws_pos)
                            tr_times = ch_times_map.get("translate")
                            should_key_tr = has_keys and ("translate" in keyed_ch) and (
                                (not tr_times) or _time_in_list(t, tr_times)
                            )
                            if should_key_tr:
                                cur_tr = cmds.getAttr(live_jnt + ".translate")[0]
                                for ax_attr, val in zip(("translateX", "translateY", "translateZ"), cur_tr):
                                    cmds.setKeyframe(
                                        live_jnt,
                                        attribute=ax_attr,
                                        time=float(t),
                                        value=float(val),
                                    )
                                    restored_key_count += 1
                                keyed_joints.add(live_jnt)
                        except Exception as exc:
                            print("SIWE Pose Restore translate warning:", exc)

                # Scale の書き戻し（元々スケールにキーがあった場合、XYZ=1 でも全軸へ書き戻す）
                if restore_scale and ("scale" in keyed_ch):
                    sc_val = [1.0, 1.0, 1.0] if scale_was_frozen else (pose.get("scale") or [1.0, 1.0, 1.0])
                    try:
                        _ensure_attr_unlocked_for_key(live_jnt, "scale")
                        cmds.setAttr(
                            live_jnt + ".scale",
                            float(sc_val[0]),
                            float(sc_val[1]),
                            float(sc_val[2]),
                            type="double3",
                        )
                        sc_times = ch_times_map.get("scale")
                        should_key_sc = has_keys and ((not sc_times) or _time_in_list(t, sc_times))
                        if should_key_sc:
                            for ax_attr, val in zip(("scaleX", "scaleY", "scaleZ"), sc_val):
                                cmds.setKeyframe(
                                    live_jnt,
                                    attribute=ax_attr,
                                    time=float(t),
                                    value=float(val),
                                )
                                restored_key_count += 1
                            keyed_joints.add(live_jnt)
                    except Exception as exc:
                        print("SIWE Pose Restore scale warning:", exc)

                # Rotate の書き戻し（親の最新姿勢と自身の新しい jointOrient から厳密逆算し、XYZ=0 でも全軸へ書き戻す）
                if restore_rotate and ((not has_keys) or ("rotate" in keyed_ch) or (not keyed_ch)):
                    ws_rot_vals = pose.get("ws_rot_mat")
                    if ws_rot_vals and len(ws_rot_vals) == 16:
                        try:
                            _ensure_attr_unlocked_for_key(live_jnt, "rotate")
                            parent_rot = _get_parent_rotation_matrix(live_jnt)
                            parent_vals = [float(parent_rot[i]) for i in range(16)]
                            jo = cmds.getAttr(live_jnt + ".jointOrient")[0]
                            rot_order = int(cmds.getAttr(live_jnt + ".rotateOrder"))
                            new_deg = compute_local_rotate_euler_from_matrices(
                                ws_rot_vals,
                                parent_vals,
                                jo,
                                rotate_order=rot_order,
                            )
                            cmds.setAttr(
                                live_jnt + ".rotate",
                                float(new_deg[0]),
                                float(new_deg[1]),
                                float(new_deg[2]),
                                type="double3",
                            )
                            rot_times = ch_times_map.get("rotate")
                            should_key_rot = has_keys and (
                                ("rotate" in keyed_ch and ((not rot_times) or _time_in_list(t, rot_times)))
                                or (not keyed_ch)
                            )
                            if should_key_rot:
                                for ax_attr, val in zip(("rotateX", "rotateY", "rotateZ"), new_deg):
                                    cmds.setKeyframe(
                                        live_jnt,
                                        attribute=ax_attr,
                                        time=float(t),
                                        value=float(val),
                                    )
                                    restored_key_count += 1
                                keyed_joints.add(live_jnt)
                        except Exception as exc:
                            print("SIWE Pose Restore rotate warning:", exc)

            restored_frame_count += 1

        # キーフレームが複数ある場合はオイラー角フリップ防止フィルタを適用
        if apply_euler_filter and has_keys and restored_frame_count > 1 and keyed_joints:
            for live_jnt in keyed_joints:
                try:
                    curves = cmds.listConnections(live_jnt, type="animCurveTA", source=True, destination=False) or []
                    if curves:
                        cmds.filterCurve(curves)
                except Exception:
                    pass
    finally:
        target_t = float(orig_time) if (restore_orig_time and orig_time is not None) else cur_time_backup
        if abs(float(cmds.currentTime(query=True)) - target_t) > 1e-6:
            try:
                cmds.currentTime(target_t, edit=True)
            except Exception:
                pass

    return {
        "restored_joints": len(resolved_list),
        "restored_frames": restored_frame_count,
        "restored_keys": restored_key_count,
        "missing_joints": missing_joints,
        "has_keys": has_keys,
    }


def freeze_joints_with_pose_backup(
    joints,
    base_storage_dir,
    freeze_rotate=True,
    freeze_scale=True,
    freeze_translate=False,
    backup_and_restore_pose=True,
    freeze_frame=None,
    delete_unused_anim_curves=True,
    rotate_adjust_deg=None,
):
    """回転等調整 → ポーズ（キーフレーム）JSON 書き出し → 接続・ロック解除＆フリーズ → キーフレーム書き戻しを一括実行する。"""
    ordered = sorted(set(j for j in (joints or []) if cmds.objExists(j)), key=_dag_depth)
    if not ordered:
        raise RuntimeError("No valid joints selected.")

    orig_time = float(cmds.currentTime(query=True))
    orig_time_r = round(orig_time, 6)
    has_rot_adj = bool(rotate_adjust_deg) and any(abs(float(v)) > 1e-6 for v in rotate_adjust_deg)

    def _snapshot_live_trs(jnt_list):
        snap = {}
        for jnt in jnt_list:
            if not cmds.objExists(jnt):
                continue
            try:
                snap[jnt] = {
                    "rotate": list(cmds.getAttr(jnt + ".rotate")[0]),
                    "rotateAxis": list(cmds.getAttr(jnt + ".rotateAxis")[0]),
                    "scale": list(cmds.getAttr(jnt + ".scale")[0]),
                }
            except Exception:
                pass
        return snap

    # タイムライン移動や接続切断の前に、チャネルボックス上の未キー編集値（ピンク色）を含むライブ値を退避する
    live_orig_overrides = _snapshot_live_trs(ordered)

    # 処理中のタイムライン移動や setAttr で Maya の AutoKey が誤作動しないよう一時的に無効化する
    orig_autokey = False
    try:
        orig_autokey = bool(cmds.autoKeyframe(q=True, state=True))
        if orig_autokey:
            cmds.autoKeyframe(state=False)
    except Exception:
        orig_autokey = False

    try:
        pose_json_path = ""
        pose_payload = None
        if backup_and_restore_pose:
            pose_json_path, pose_payload = export_joints_pose_json(
                ordered,
                base_storage_dir,
                rotate_adjust_deg=rotate_adjust_deg if has_rot_adj else None,
            )

        freeze_time_r = orig_time_r
        if freeze_frame is not None:
            target_f = float(freeze_frame)
            freeze_time_r = round(target_f, 6)
            if abs(float(cmds.currentTime(query=True)) - target_f) > 1e-6:
                cmds.currentTime(target_f, edit=True)

        if abs(freeze_time_r - orig_time_r) <= 1e-6:
            freeze_overrides = live_orig_overrides
        else:
            freeze_overrides = _snapshot_live_trs(ordered)

        freeze_res = force_freeze_joints(
            ordered,
            freeze_rotate=freeze_rotate,
            freeze_scale=freeze_scale,
            freeze_translate=freeze_translate,
            delete_unused_anim_curves=delete_unused_anim_curves,
            also_break_translate_for_pose_restore=bool(backup_and_restore_pose),
            pre_freeze_overrides=freeze_overrides,
            rotate_adjust_deg=rotate_adjust_deg if has_rot_adj else None,
        )

        # フリーズ直後の基準フレーム姿勢を JSON データ側とも同期し、基準フレームが確実に Rotate=(0,0,0) で書き戻るようにする
        if backup_and_restore_pose and pose_payload is not None:
            for f_entry in (pose_payload.get("frames") or []):
                if abs(float(f_entry.get("time", -999999.0)) - freeze_time_r) <= 1e-4:
                    j_poses = f_entry.get("joints") or {}
                    for jnt in ordered:
                        if not cmds.objExists(jnt):
                            continue
                        try:
                            ws_pos = cmds.xform(jnt, q=True, ws=True, t=True)
                            ws_rot_mat = _get_orthonormal_rotation_matrix(jnt)
                            entry_p = j_poses.get(jnt) or {}
                            entry_p["ws_pos"] = [float(ws_pos[0]), float(ws_pos[1]), float(ws_pos[2])]
                            entry_p["ws_rot_mat"] = [float(ws_rot_mat[i]) for i in range(16)]
                            j_poses[jnt] = entry_p
                        except Exception:
                            pass
                    f_entry["joints"] = j_poses
            if pose_json_path:
                try:
                    with open(pose_json_path, "w") as fp:
                        json.dump(pose_payload, fp, indent=2)
                except Exception:
                    pass

        restore_res = None
        if backup_and_restore_pose and pose_payload is not None:
            restore_res = import_joints_pose_json(
                pose_payload,
                restore_rotate=freeze_rotate,
                restore_translate=True,
                restore_scale=True,
                scale_was_frozen=bool(freeze_scale),
                apply_euler_filter=True,
                restore_orig_time=True,
            )
        elif freeze_frame is not None and abs(float(cmds.currentTime(query=True)) - orig_time) > 1e-6:
            try:
                cmds.currentTime(orig_time, edit=True)
            except Exception:
                pass
    finally:
        if orig_autokey:
            try:
                cmds.autoKeyframe(state=True)
            except Exception:
                pass

    return {
        "pose_json": pose_json_path,
        "freeze": freeze_res,
        "restore": restore_res,
        "rotate_adjust_applied": has_rot_adj,
    }


def execute_orient_joints(
    root_joints,
    orient_to_world=False,
    primary_axis="x",
    secondary_axis="y",
    secondary_world_axis="z",
    secondary_world_sign="-",
    orient_children=True,
    reorient_scale=True,
    zero_end_joints=True,
):
    """Orient Joint Options 準拠の設定でジョイントの方向付けを実行する。"""
    if not root_joints:
        return {"processed": 0, "end_zeroed": 0}

    targets = []
    seen = set()
    for jnt in root_joints:
        if cmds.objExists(jnt) and jnt not in seen:
            seen.add(jnt)
            targets.append(jnt)
    targets.sort(key=_dag_depth)

    # 処理対象となる全ジョイント一覧を先に把握しておく
    all_affected = list(targets)
    if orient_children:
        seen_all = set(all_affected)
        for jnt in targets:
            desc = cmds.listRelatives(jnt, allDescendents=True, type="joint", fullPath=True) or []
            for child in desc:
                if child not in seen_all:
                    seen_all.add(child)
                    all_affected.append(child)

    # 事前に Rotate を 0 へ焼き込んでおくと Orient Joint の挙動が安定する
    freeze_joint_rotations_to_orient(all_affected)

    if orient_to_world:
        for jnt in targets:
            cmds.joint(
                jnt,
                edit=True,
                orientJoint="none",
                children=bool(orient_children),
                zeroScaleOrient=bool(reorient_scale),
            )
    else:
        p_ax = primary_axis.lower()
        s_ax = secondary_axis.lower()
        if s_ax == "none" or s_ax == p_ax:
            default_map = {"x": "xyz", "y": "yzx", "z": "zxy"}
            oj_str = default_map.get(p_ax, "xyz")
            for jnt in targets:
                cmds.joint(
                    jnt,
                    edit=True,
                    orientJoint=oj_str,
                    children=bool(orient_children),
                    zeroScaleOrient=bool(reorient_scale),
                )
        else:
            third_ax = [ax for ax in ("x", "y", "z") if ax not in (p_ax, s_ax)][0]
            oj_str = p_ax + s_ax + third_ax
            sao_str = secondary_world_axis.lower() + ("up" if secondary_world_sign == "+" else "down")
            for jnt in targets:
                cmds.joint(
                    jnt,
                    edit=True,
                    orientJoint=oj_str,
                    secondaryAxisOrient=sao_str,
                    children=bool(orient_children),
                    zeroScaleOrient=bool(reorient_scale),
                )

    # 末端ジョイント（子ジョイントを持たない先端）の jointOrient を 0 に揃えて親と向きを一致させる
    end_zeroed = 0
    if zero_end_joints and not orient_to_world:
        for jnt in all_affected:
            if not cmds.objExists(jnt):
                continue
            child_joints = cmds.listRelatives(jnt, children=True, type="joint") or []
            if child_joints:
                continue
            if _is_attr_editable(jnt, "jointOrient"):
                cmds.setAttr(jnt + ".jointOrient", 0.0, 0.0, 0.0, type="double3")
                if _is_attr_editable(jnt, "rotate"):
                    cmds.setAttr(jnt + ".rotate", 0.0, 0.0, 0.0, type="double3")
                if _is_attr_editable(jnt, "rotateAxis"):
                    cmds.setAttr(jnt + ".rotateAxis", 0.0, 0.0, 0.0, type="double3")
                end_zeroed += 1

    return {"processed": len(all_affected), "end_zeroed": end_zeroed}


def tweak_joint_orient_axis(joints, axis, degrees):
    """子ジョイントのワールド位置・回転を維持したまま、選択ジョイントのローカル軸を指定角度だけ回す。"""
    ordered = sorted(set(joints), key=_dag_depth)
    axis_l = axis.lower()
    rx = math.radians(degrees) if axis_l == "x" else 0.0
    ry = math.radians(degrees) if axis_l == "y" else 0.0
    rz = math.radians(degrees) if axis_l == "z" else 0.0
    delta_mat = om2.MEulerRotation(rx, ry, rz, om2.MEulerRotation.kXYZ).asMatrix()

    updated = []
    for jnt in ordered:
        if not cmds.objExists(jnt):
            continue
        if not _is_attr_editable(jnt, "jointOrient"):
            continue

        direct_children = cmds.listRelatives(jnt, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            ws_pos = cmds.xform(child, q=True, ws=True, t=True)
            ws_rot_mat = _get_orthonormal_rotation_matrix(child)
            is_jnt = cmds.nodeType(child) == "joint"
            had_zero_rot = False
            if is_jnt:
                c_rot = cmds.getAttr(child + ".rotate")[0]
                had_zero_rot = all(abs(v) <= EPSILON_ROT for v in c_rot)
            saved_children.append((child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot))

        old_world_rot = _get_orthonormal_rotation_matrix(jnt)
        new_world_rot = delta_mat * old_world_rot
        if _set_joint_world_rot_to_orient(jnt, new_world_rot):
            for child, ws_pos, ws_rot_mat, is_jnt, had_zero_rot in saved_children:
                if not cmds.objExists(child):
                    continue
                try:
                    if _is_attr_editable(child, "translate"):
                        cmds.xform(child, ws=True, t=ws_pos)
                    if is_jnt and had_zero_rot:
                        _set_joint_world_rot_to_orient(child, ws_rot_mat)
                except Exception:
                    pass
            updated.append(jnt)

    return updated


def toggle_local_axes_visibility(joints):
    """対象ジョイント群のローカル回転軸（displayLocalAxis）表示をトグルして新しい状態を返す。"""
    valid = [j for j in joints if cmds.objExists(j)]
    if not valid:
        return None
    any_visible = False
    for jnt in valid:
        try:
            if cmds.getAttr(jnt + ".displayLocalAxis"):
                any_visible = True
                break
        except Exception:
            pass

    new_state = not any_visible
    for jnt in valid:
        try:
            cmds.setAttr(jnt + ".displayLocalAxis", new_state)
        except Exception:
            pass
    return new_state


def run_mesh_cleanup(
    meshes,
    select_only=True,
    quads=False,
    nsided=True,
    concave=True,
    holed=True,
    nonplanar=False,
    zero_geom=False,
    zero_edge=False,
    nonmanifold=True,
    lamina=True,
    keep_history=False,
):
    """Maya 標準の polyCleanupArgList 4 を用いてメッシュの検出またはテッセレーション修正を行う。"""
    if not meshes:
        return {"matched_count": 0, "matched_items": []}

    args = [
        "0",                               # 選択メッシュのみ対象
        "1" if select_only else "0",       # 1=検出選択のみ / 0=自動修正
        "1" if keep_history else "0",      # コンストラクションヒストリ保持
        "1" if quads else "0",             # 4-sided faces
        "1" if nsided else "0",            # Faces with more than 4 sides
        "1" if concave else "0",           # Concave faces
        "1" if holed else "0",             # Faces with holes
        "1" if nonplanar else "0",         # Non-planar faces
        "1" if zero_geom else "0",         # Faces with zero geometry area
        "1e-05",                           # zeroGeomTol
        "1" if zero_edge else "0",         # Edges with zero length
        "1e-05",                           # zeroEdgeTol
        "0",                               # Faces with zero map area
        "1e-05",                           # zeroMapTol
        "0",                               # sharedUVs
        "1" if nonmanifold else "0",       # Nonmanifold geometry（法線・ジオメトリ修正）
        "1" if lamina else "0",            # Lamina faces
    ]

    mel_arg_str = ", ".join('"{0}"'.format(a) for a in args)
    mel_cmd = "polyCleanupArgList 4 {{ {0} }};".format(mel_arg_str)

    cmds.select(meshes, replace=True)
    mel.eval(mel_cmd)

    if select_only:
        matched = cmds.ls(sl=True, flatten=True) or []
        # 対象コンポーネントが 0 件のときはオブジェクト自体が残る場合があるため除外して数える
        comp_matched = [m for m in matched if "." in m]
        return {"matched_count": len(comp_matched), "matched_items": comp_matched}

    cmds.select(meshes, replace=True)
    return {"matched_count": 0, "matched_items": []}


def scan_scene_cleanup_targets():
    """シーン内のクリーンアップ候補（未使用中間シェイプ・孤立 bindPose・未知ノード・空グループ）を診断する。"""
    unused_intermediates = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if cmds.getAttr(shape + ".intermediateObject"):
                conns = cmds.listConnections(shape, source=True, destination=True) or []
                if not conns:
                    unused_intermediates.append(shape)
        except Exception:
            pass

    orphaned_bind_poses = []
    for dp in cmds.ls(type="dagPose") or []:
        try:
            if not cmds.getAttr(dp + ".bindPose"):
                continue
            skins = cmds.listConnections(dp + ".message", type="skinCluster", source=False, destination=True) or []
            if not skins:
                orphaned_bind_poses.append(dp)
        except Exception:
            pass

    unknown_nodes = []
    for utype in ("unknown", "unknownDag"):
        found = cmds.ls(type=utype) or []
        for node in found:
            if not cmds.referenceQuery(node, isNodeReferenced=True):
                unknown_nodes.append(node)

    default_cameras = {"|persp", "|top", "|front", "|side"}
    empty_groups = []
    for xf in cmds.ls(type="transform", long=True) or []:
        if xf in default_cameras:
            continue
        try:
            if cmds.referenceQuery(xf, isNodeReferenced=True):
                continue
            children = cmds.listRelatives(xf, children=True) or []
            if children:
                continue
            conns = cmds.listConnections(xf, source=True, destination=True) or []
            if conns:
                continue
            empty_groups.append(xf)
        except Exception:
            pass

    return {
        "unused_intermediates": unused_intermediates,
        "orphaned_bind_poses": orphaned_bind_poses,
        "unknown_nodes": unknown_nodes,
        "empty_groups": empty_groups,
    }


def run_scene_cleanup(
    clean_intermediates=True,
    clean_bind_poses=True,
    clean_unknown=True,
    clean_empty_groups=False,
    clean_unused_shading=False,
):
    """選択された項目についてシーンクリーンアップを実行し、削除件数レポートを返す。"""
    scan = scan_scene_cleanup_targets()
    report = {
        "unused_intermediates": 0,
        "orphaned_bind_poses": 0,
        "unknown_nodes": 0,
        "empty_groups": 0,
        "unused_shading": 0,
    }

    if clean_intermediates:
        for node in scan["unused_intermediates"]:
            if cmds.objExists(node):
                try:
                    cmds.delete(node)
                    report["unused_intermediates"] += 1
                except Exception:
                    pass

    if clean_bind_poses:
        removed = cleanup_orphaned_bind_poses()
        report["orphaned_bind_poses"] = len(removed)

    if clean_unknown:
        for node in scan["unknown_nodes"]:
            if cmds.objExists(node):
                try:
                    cmds.lockNode(node, lock=False)
                except Exception:
                    pass
                try:
                    cmds.delete(node)
                    report["unknown_nodes"] += 1
                except Exception:
                    pass

    if clean_empty_groups:
        # 深い階層から順に削除する
        groups = sorted(scan["empty_groups"], key=_dag_depth, reverse=True)
        for grp in groups:
            if cmds.objExists(grp):
                try:
                    children = cmds.listRelatives(grp, children=True) or []
                    if not children:
                        cmds.delete(grp)
                        report["empty_groups"] += 1
                except Exception:
                    pass

    if clean_unused_shading:
        try:
            res = mel.eval("MLdeleteUnused;")
            report["unused_shading"] = int(res) if res is not None else 0
        except Exception:
            pass

    return report


# ----------------------------------------------------------------------
# ボーンの安全バインド（既存ウェイト維持・反対側ミラー転写・近傍自動バインド）
# ----------------------------------------------------------------------
def build_opposite_joint_name_candidates(short_name):
    """ジョイント短縮名から左右反転した反対側ボーン名候補一覧を生成する。"""
    if not short_name:
        return []
    base = short_name.split("|")[-1]
    ns = ""
    local = base
    if ":" in base:
        ns, local = base.rsplit(":", 1)
        ns = ns + ":"

    candidates = []

    def _add(name):
        if name and name != local:
            full_c = ns + name
            if full_c not in candidates:
                candidates.append(full_c)

    # Left / Right 表記の相互置換（CamelCase・小文字・大文字すべて対応）
    word_pairs = (
        ("Left", "Right"),
        ("Right", "Left"),
        ("left", "right"),
        ("right", "left"),
        ("LEFT", "RIGHT"),
        ("RIGHT", "LEFT"),
    )
    for src_tok, dst_tok in word_pairs:
        if src_tok in local:
            _add(local.replace(src_tok, dst_tok))

    # L / R 略記（先頭・中間区切り・末尾・末尾数字付き）の相互置換
    regex_pairs = (
        (r"^L([_\.\-])", r"R\1"),
        (r"^R([_\.\-])", r"L\1"),
        (r"^l([_\.\-])", r"r\1"),
        (r"^r([_\.\-])", r"l\1"),
        (r"([_\.\-])L([_\.\-])", r"\1R\2"),
        (r"([_\.\-])R([_\.\-])", r"\1L\2"),
        (r"([_\.\-])l([_\.\-])", r"\1r\2"),
        (r"([_\.\-])r([_\.\-])", r"\1l\2"),
        (r"([_\.\-])L([0-9]*)$", r"\1R\2"),
        (r"([_\.\-])R([0-9]*)$", r"\1L\2"),
        (r"([_\.\-])l([0-9]*)$", r"\1r\2"),
        (r"([_\.\-])r([0-9]*)$", r"\1l\2"),
    )
    for pattern, repl in regex_pairs:
        replaced = re.sub(pattern, repl, local)
        if replaced != local:
            _add(replaced)

    return candidates


def find_opposite_joint_in_list(joint_path, candidate_joints):
    """候補ジョイント一覧から指定ジョイントの左右反対側ボーンを解決して返す。"""
    if not joint_path or not candidate_joints:
        return None
    short_name = joint_path.split("|")[-1]
    opp_candidates = build_opposite_joint_name_candidates(short_name)
    if not opp_candidates:
        return None

    short_to_paths = {}
    bare_to_paths = {}
    for cand in candidate_joints:
        if cand == joint_path:
            continue
        sn = cand.split("|")[-1]
        short_to_paths.setdefault(sn, []).append(cand)
        bare = sn.split(":")[-1]
        bare_to_paths.setdefault(bare, []).append(cand)

    # 同一ネームスペースを含む短縮名一致を最優先で検索する
    for opp in opp_candidates:
        matches = short_to_paths.get(opp)
        if matches:
            return matches[0]

    # ネームスペースを除いたボーン名一致でフォールバック検索する
    for opp in opp_candidates:
        bare_opp = opp.split(":")[-1]
        matches = bare_to_paths.get(bare_opp)
        if matches:
            return matches[0]

    return None


def _get_visible_mesh_shape_dag_for_bind(mesh_node):
    """メッシュノードから可視シェイプ（中間オブジェクト除外）の MDagPath を取得する。"""
    try:
        sel = om2.MSelectionList()
        sel.add(mesh_node)
        dag = sel.getDagPath(0)
        if dag.node().hasFn(om2.MFn.kMesh):
            return dag
        shape_count = dag.numberOfShapesDirectlyBelow()
        fallback = None
        for idx in range(shape_count):
            cand = om2.MDagPath(dag)
            try:
                cand.extendToShape(idx)
            except Exception:
                continue
            if not cand.node().hasFn(om2.MFn.kMesh):
                continue
            if fallback is None:
                fallback = cand
            if not om2.MFnDagNode(cand).isIntermediateObject:
                return cand
        return fallback
    except Exception:
        return None


def _get_skin_cluster_for_mesh_bind(mesh_node):
    """指定メッシュに接続されているスキンクラスタ名を返す（未バインド時は None）。"""
    if not mesh_node or not cmds.objExists(mesh_node):
        return None
    for prune in (True, False):
        try:
            hist = cmds.listHistory(mesh_node, pruneDagObjects=prune) or []
            skins = cmds.ls(hist, type="skinCluster") or []
            if skins:
                return skins[0]
        except Exception:
            continue
    return None


def _set_weights_without_norm_warning(skin_fn, shape_dag, vtx_comp, inf_indices, weights):
    """正規化警告を抑止しつつ OpenMaya 経由でスキンクラスタへウェイトを書き込む。"""
    nw_plug = None
    prev_nw = None
    nw_switched = False
    try:
        plug = skin_fn.findPlug("normalizeWeights", False)
        is_null = plug.isNull() if callable(plug.isNull) else bool(plug.isNull)
        if not is_null:
            is_locked = plug.isLocked() if callable(plug.isLocked) else bool(plug.isLocked)
            is_conn = plug.isConnected() if callable(plug.isConnected) else bool(plug.isConnected)
            if not is_locked and not is_conn:
                prev_nw = plug.asShort()
                nw_plug = plug
                if prev_nw != 0:
                    plug.setShort(0)
                nw_switched = True
    except Exception:
        nw_plug = None
        prev_nw = None
        nw_switched = False

    prev_suppress = None
    if not nw_switched:
        try:
            prev_suppress = bool(cmds.scriptEditorInfo(query=True, suppressWarnings=True))
            if not prev_suppress:
                cmds.scriptEditorInfo(suppressWarnings=True)
        except Exception:
            prev_suppress = None

    try:
        skin_fn.setWeights(shape_dag, vtx_comp, inf_indices, weights, False)
    finally:
        if nw_plug is not None and prev_nw is not None and prev_nw != 0:
            try:
                nw_plug.setShort(prev_nw)
            except Exception:
                pass
        if prev_suppress is False:
            try:
                cmds.scriptEditorInfo(suppressWarnings=False)
            except Exception:
                pass


def _apply_vertex_weights_undoable(mesh_node, skin_fn, vtx_indices, new_weights_per_vtx, org_weights_per_vtx, inf_count):
    """Undo 可能な bakeSkinWeightImprove コマンド経由で指定頂点群のウェイトを書き込む。"""
    if not vtx_indices:
        return

    from ..editor import state as editor_state

    bake_node_id_dict = {mesh_node: om2.MIntArray(vtx_indices)}
    flat_new = []
    flat_org = []
    for nw, ow in zip(new_weights_per_vtx, org_weights_per_vtx):
        flat_new.extend(nw)
        flat_org.extend(ow)
    bake_node_weight_dict = {mesh_node: om2.MDoubleArray(flat_new)}
    org_node_weight_dict = {mesh_node: om2.MDoubleArray(flat_org)}
    bake_node_inf_dict = {mesh_node: om2.MIntArray(list(range(inf_count)))}
    node_skinFn_dict = {mesh_node: skin_fn}
    undo_node_weight_dict = {mesh_node: [list(ow) for ow in org_weights_per_vtx]}
    redo_node_weight_dict = {mesh_node: [list(nw) for nw in new_weights_per_vtx]}

    try:
        if not hasattr(cmds, "bakeSkinWeightImprove"):
            editor_state.load_bundled_plugin(editor_state.BAKE_PLUGIN_NAME, autoload=False)
        editor_state.set_current_data(
            bake_node_id_dict,
            bake_node_weight_dict,
            bake_node_inf_dict,
            node_skinFn_dict,
            undo_node_weight_dict,
            redo_node_weight_dict,
            org_node_weight_dict,
        )
        cmds.bakeSkinWeightImprove(rb=True, iu=False)
        return
    except Exception:
        pass

    # プラグインコマンドが使えない場合のみ OpenMaya 直接書き込みへフォールバックする
    shape_dag = _get_visible_mesh_shape_dag_for_bind(mesh_node)
    if shape_dag is None:
        return
    single_comp = om2.MFnSingleIndexedComponent()
    vtx_comp = single_comp.create(om2.MFn.kMeshVertComponent)
    single_comp.addElements(vtx_indices)
    _set_weights_without_norm_warning(
        skin_fn,
        shape_dag,
        vtx_comp,
        om2.MIntArray(list(range(inf_count))),
        om2.MDoubleArray(flat_new),
    )


def compute_mirrored_weights_for_targets(
    vtx_positions,
    all_weights,
    target_to_src_inf_map,
    full_mirror_inf_map,
    mirror_center_x=0.0,
    target_side_sign=1.0,
    nearest_vertex_fn=None,
    weight_eps=1e-4,
):
    """反対側ボーンにウェイトがある領域だけをターゲット側へミラー転写するウェイト差分を算出する。"""
    if not target_to_src_inf_map or not vtx_positions or not all_weights:
        return [], [], []

    inf_count = len(all_weights[0])
    src_inf_set = set(target_to_src_inf_map.values())
    target_inf_set = set(target_to_src_inf_map.keys())
    src_to_target_map = {s_idx: t_idx for t_idx, s_idx in target_to_src_inf_map.items()}

    if nearest_vertex_fn is None:
        def nearest_vertex_fn(mx, my, mz):
            best_idx = 0
            best_dist = float("inf")
            for idx, (vx, vy, vz) in enumerate(vtx_positions):
                dx = vx - mx
                dy = vy - my
                dz = vz - mz
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_dist:
                    best_dist = d2
                    best_idx = idx
            return best_idx

    changed_vids = []
    new_list = []
    org_list = []

    for v_dst, (px, py, pz) in enumerate(vtx_positions):
        signed_dist = (px - mirror_center_x) * target_side_sign
        # ソース側（例：左脚側）の頂点は一切変更せずそのまま保護する
        if signed_dist < -1e-4:
            continue

        mx = 2.0 * mirror_center_x - px
        v_src = nearest_vertex_fn(mx, py, pz)
        if v_src is None or v_src < 0 or v_src >= len(all_weights):
            continue

        src_w = all_weights[v_src]
        # 反対側の対象ボーン群にウェイトが無い領域（腕・胴・頭など）は触らない
        if not any(src_w[s_idx] > weight_eps for s_idx in src_inf_set):
            continue

        org_w = all_weights[v_dst]

        if abs(signed_dist) <= 1e-4 and v_dst == v_src:
            # 対称面ちょうど上の頂点はソース側ウェイトを左右半分ずつ分配してバランスを保つ
            mir_w = list(org_w)
            for s_idx in src_inf_set:
                w_s = mir_w[s_idx]
                if w_s > weight_eps:
                    t_idx = src_to_target_map[s_idx]
                    half_w = w_s * 0.5
                    mir_w[s_idx] = half_w
                    mir_w[t_idx] += half_w
        else:
            mir_w = [0.0] * inf_count
            for idx, w_val in enumerate(src_w):
                if w_val > 0.0:
                    mapped_idx = full_mirror_inf_map.get(idx, idx)
                    mir_w[mapped_idx] += w_val

        total_w = sum(mir_w)
        if total_w <= 1e-12:
            continue
        if abs(total_w - 1.0) > 1e-9:
            inv_total = 1.0 / total_w
            mir_w = [w * inv_total for w in mir_w]

        # 対象ボーン側に変化があり、元ウェイトと異なる頂点だけ書き込み対象にする
        if any(mir_w[t_idx] > weight_eps for t_idx in target_inf_set) and any(
            abs(a - b) > 1e-9 for a, b in zip(mir_w, org_w)
        ):
            changed_vids.append(v_dst)
            new_list.append(mir_w)
            org_list.append(list(org_w))

    return changed_vids, new_list, org_list


def _mirror_weights_from_opposite_bones(mesh_node, skin_cluster, target_joints):
    """既存スキンクラスタ上で反対側ボーンから選択ボーン群へミラー転写を実行する。"""
    if oma2 is None:
        return 0, []

    shape_dag = _get_visible_mesh_shape_dag_for_bind(mesh_node)
    if shape_dag is None:
        return 0, []

    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    inf_dags = skin_fn.influenceObjects()
    inf_paths = [inf_dags[i].fullPathName() for i in range(len(inf_dags))]
    inf_count = len(inf_paths)
    if inf_count == 0:
        return 0, []

    inf_index_map = {p: idx for idx, p in enumerate(inf_paths)}

    # 対象ジョイントそれぞれについてスキンクラスタ内の反対側ボーンを照合する
    target_to_src_map = {}
    matched_pairs = []
    target_xs = []
    src_xs = []
    for t_jnt in target_joints:
        t_idx = inf_index_map.get(t_jnt)
        if t_idx is None:
            continue
        opp_jnt = find_opposite_joint_in_list(t_jnt, inf_paths)
        if opp_jnt and opp_jnt in inf_index_map:
            s_idx = inf_index_map[opp_jnt]
            if s_idx != t_idx:
                target_to_src_map[t_idx] = s_idx
                matched_pairs.append((opp_jnt, t_jnt))
                try:
                    t_pos = cmds.xform(t_jnt, q=True, ws=True, t=True)
                    s_pos = cmds.xform(opp_jnt, q=True, ws=True, t=True)
                    target_xs.append(float(t_pos[0]))
                    src_xs.append(float(s_pos[0]))
                except Exception:
                    pass

    if not target_to_src_map:
        return 0, []

    # 全インフルエンスの左右対応マップを構築（中央ボーンは自分自身へマップ）
    full_mirror_map = {}
    for idx, j_path in enumerate(inf_paths):
        opp = find_opposite_joint_in_list(j_path, inf_paths)
        if opp and opp in inf_index_map:
            full_mirror_map[idx] = inf_index_map[opp]
        else:
            full_mirror_map[idx] = idx

    # 対称面の X 座標とターゲット側の符号をジョイント座標から自動算出する
    if target_xs and src_xs:
        avg_tx = sum(target_xs) / float(len(target_xs))
        avg_sx = sum(src_xs) / float(len(src_xs))
        center_x = (avg_tx + avg_sx) * 0.5
        if abs(center_x) < 1e-3:
            center_x = 0.0
        side_sign = 1.0 if (avg_tx - avg_sx) >= 0.0 else -1.0
    else:
        center_x = 0.0
        side_sign = 1.0

    mesh_fn = om2.MFnMesh(shape_dag)
    num_verts = mesh_fn.numVertices
    if num_verts <= 0:
        return 0, matched_pairs

    points = mesh_fn.getPoints(om2.MSpace.kWorld)
    vtx_positions = [(float(pt.x), float(pt.y), float(pt.z)) for pt in points]

    single_comp = om2.MFnSingleIndexedComponent()
    all_vtx_comp = single_comp.create(om2.MFn.kMeshVertComponent)
    single_comp.addElements(list(range(num_verts)))
    flat_weights, _ = skin_fn.getWeights(shape_dag, all_vtx_comp)
    all_weights = [
        [float(flat_weights[v * inf_count + c]) for c in range(inf_count)]
        for v in range(num_verts)
    ]

    poly_verts_cache = {}

    def _nearest_vtx_via_mesh(mx, my, mz):
        try:
            _, poly_id = mesh_fn.getClosestPoint(om2.MPoint(mx, my, mz), om2.MSpace.kWorld)
            poly_vids = poly_verts_cache.get(poly_id)
            if poly_vids is None:
                poly_vids = list(mesh_fn.getPolygonVertices(poly_id))
                poly_verts_cache[poly_id] = poly_vids
            best_v = poly_vids[0]
            best_d2 = float("inf")
            for vid in poly_vids:
                vx, vy, vz = vtx_positions[vid]
                dx = vx - mx
                dy = vy - my
                dz = vz - mz
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_d2:
                    best_d2 = d2
                    best_v = vid
            return best_v
        except Exception:
            return None

    changed_vids, new_list, org_list = compute_mirrored_weights_for_targets(
        vtx_positions=vtx_positions,
        all_weights=all_weights,
        target_to_src_inf_map=target_to_src_map,
        full_mirror_inf_map=full_mirror_map,
        mirror_center_x=center_x,
        target_side_sign=side_sign,
        nearest_vertex_fn=_nearest_vtx_via_mesh,
    )

    if changed_vids:
        _apply_vertex_weights_undoable(mesh_node, skin_fn, changed_vids, new_list, org_list, inf_count)

    return len(changed_vids), matched_pairs


def _auto_bind_new_weights_preserving_existing(mesh_node, skin_cluster, target_joints):
    """一時複製メッシュの距離ベース自動バインドから対象ジョイント分のみ抽出し、既存ウェイトを保護しつつ合成する。"""
    if oma2 is None:
        return 0

    shape_dag = _get_visible_mesh_shape_dag_for_bind(mesh_node)
    if shape_dag is None:
        return 0

    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    inf_dags = skin_fn.influenceObjects()
    inf_paths = [inf_dags[i].fullPathName() for i in range(len(inf_dags))]
    inf_count = len(inf_paths)
    if inf_count == 0:
        return 0

    inf_index_map = {p: idx for idx, p in enumerate(inf_paths)}
    target_indices = {inf_index_map[j] for j in target_joints if j in inf_index_map}
    if not target_indices:
        return 0

    mesh_fn = om2.MFnMesh(shape_dag)
    num_verts = mesh_fn.numVertices
    if num_verts <= 0:
        return 0

    single_comp = om2.MFnSingleIndexedComponent()
    all_vtx_comp = single_comp.create(om2.MFn.kMeshVertComponent)
    single_comp.addElements(list(range(num_verts)))
    org_flat, _ = skin_fn.getWeights(shape_dag, all_vtx_comp)
    org_weights = [
        [float(org_flat[v * inf_count + c]) for c in range(inf_count)]
        for v in range(num_verts)
    ]

    max_inf = 4
    obey_max = False
    try:
        max_inf = int(cmds.getAttr(skin_cluster + ".maxInfluences"))
        obey_max = bool(cmds.getAttr(skin_cluster + ".maintainMaxInfluences"))
    except Exception:
        pass

    dup_node = None
    dup_skin = None
    try:
        dup_res = cmds.duplicate(mesh_node, returnRootsOnly=True) or []
        if not dup_res:
            return 0
        dup_node = cmds.ls(dup_res[0], long=True)[0]
        # 複製に付随した子トランスフォームがあれば削除してクリーンにする
        dup_children = cmds.listRelatives(dup_node, children=True, type="transform", fullPath=True) or []
        if dup_children:
            cmds.delete(dup_children)

        dup_skin = cmds.skinCluster(
            inf_paths,
            dup_node,
            toSelectedBones=True,
            bindMethod=0,
            normalizeWeights=1,
            maximumInfluences=max(1, max_inf),
            obeyMaxInfluences=obey_max,
        )[0]

        dup_dag = _get_visible_mesh_shape_dag_for_bind(dup_node)
        dup_skin_sel = om2.MGlobal.getSelectionListByName(dup_skin)
        dup_skin_fn = oma2.MFnSkinCluster(dup_skin_sel.getDependNode(0))
        dup_inf_dags = dup_skin_fn.influenceObjects()
        dup_inf_paths = [dup_inf_dags[i].fullPathName() for i in range(len(dup_inf_dags))]
        dup_inf_cnt = len(dup_inf_paths)

        dup_to_org = {}
        for d_idx, dp in enumerate(dup_inf_paths):
            if dp in inf_index_map:
                dup_to_org[d_idx] = inf_index_map[dp]

        dup_comp_fn = om2.MFnSingleIndexedComponent()
        dup_vtx_comp = dup_comp_fn.create(om2.MFn.kMeshVertComponent)
        dup_comp_fn.addElements(list(range(num_verts)))
        dup_flat, _ = dup_skin_fn.getWeights(dup_dag, dup_vtx_comp)
    finally:
        if dup_node and cmds.objExists(dup_node):
            try:
                cmds.delete(dup_node)
            except Exception:
                pass

    changed_vids = []
    new_list = []
    org_list = []

    for v in range(num_verts):
        base = v * dup_inf_cnt
        added_map = {}
        added_sum = 0.0
        for d_idx, o_idx in dup_to_org.items():
            if o_idx in target_indices:
                w = float(dup_flat[base + d_idx])
                if w > 1e-4:
                    added_map[o_idx] = w
                    added_sum += w

        if added_sum <= 1e-4:
            continue

        org_w = org_weights[v]
        new_w = list(org_w)
        other_sum = sum(org_w[i] for i in range(inf_count) if i not in target_indices)

        if added_sum >= 1.0 or other_sum <= 1e-9:
            inv_add = 1.0 / added_sum
            for i in range(inf_count):
                new_w[i] = added_map.get(i, 0.0) * inv_add
        else:
            remain = 1.0 - added_sum
            scale_other = remain / other_sum
            for i in range(inf_count):
                if i in target_indices:
                    new_w[i] = added_map.get(i, 0.0)
                else:
                    new_w[i] = org_w[i] * scale_other

        if any(abs(a - b) > 1e-9 for a, b in zip(new_w, org_w)):
            changed_vids.append(v)
            new_list.append(new_w)
            org_list.append(list(org_w))

    if changed_vids:
        _apply_vertex_weights_undoable(mesh_node, skin_fn, changed_vids, new_list, org_list, inf_count)

    return len(changed_vids)


def safe_bind_joints_to_meshes(
    target_joints,
    target_meshes=None,
    mode="mirror_opposite",
    unlock_added=True,
):
    """選択ジョイント（子階層含む）を既存ウェイトを保護しながら対象メッシュへ安全にバインドする。"""
    joints = []
    seen_j = set()
    for jnt in (target_joints or []):
        if cmds.objExists(jnt) and cmds.nodeType(jnt) == "joint":
            long_j = (cmds.ls(jnt, long=True) or [jnt])[0]
            if long_j not in seen_j:
                seen_j.add(long_j)
                joints.append(long_j)
    if not joints:
        raise RuntimeError("バインド対象のジョイントが見つかりません。")

    meshes = []
    seen_m = set()
    for mesh in (target_meshes or []):
        if cmds.objExists(mesh):
            long_m = (cmds.ls(mesh, long=True) or [mesh])[0]
            if long_m not in seen_m:
                seen_m.add(long_m)
                meshes.append(long_m)

    # メッシュ未選択時は同一スケルトンを共有するスキニング済みメッシュを自動検出する
    if not meshes:
        meshes = find_meshes_sharing_skeleton(joints)
    if not meshes:
        raise RuntimeError("対象メッシュが見つかりません。ジョイントと一緒にメッシュを選択して実行してください。")

    orig_sel = cmds.ls(sl=True, long=True) or []
    result = {
        "meshes": list(meshes),
        "target_joints": list(joints),
        "newly_bound_meshes": [],
        "added_influences": [],
        "already_bound_count": 0,
        "weighted_vertices": 0,
        "matched_pairs": [],
        "mode": mode,
    }

    try:
        for mesh_node in meshes:
            skin_cluster = _get_skin_cluster_for_mesh_bind(mesh_node)
            if not skin_cluster:
                # 未バインドメッシュは通常バインドを実行する
                cmds.skinCluster(joints, mesh_node, toSelectedBones=True, normalizeWeights=1)
                result["newly_bound_meshes"].append(mesh_node)
                continue

            existing_infs = cmds.ls(cmds.skinCluster(skin_cluster, q=True, inf=True) or [], long=True) or []
            existing_set = set(existing_infs)
            missing_joints = [j for j in joints if j not in existing_set]
            result["already_bound_count"] += len(joints) - len(missing_joints)

            if missing_joints:
                # 既存ウェイトを崩さないよう lockWeights=True, weight=0.0 で安全にインフルエンス追加する
                cmds.skinCluster(
                    skin_cluster,
                    edit=True,
                    addInfluence=missing_joints,
                    lockWeights=True,
                    useGeometry=False,
                    weight=0.0,
                    polySmoothness=0.0,
                )
                result["added_influences"].extend(missing_joints)

            if unlock_added:
                for jnt in joints:
                    attr = jnt + ".liw"
                    if cmds.objExists(attr):
                        try:
                            if cmds.getAttr(attr, settable=True):
                                cmds.setAttr(attr, False)
                        except Exception:
                            pass

            if mode == "mirror_opposite":
                vtx_cnt, pairs = _mirror_weights_from_opposite_bones(mesh_node, skin_cluster, joints)
                result["weighted_vertices"] += vtx_cnt
                for pair in pairs:
                    if pair not in result["matched_pairs"]:
                        result["matched_pairs"].append(pair)
            elif mode == "auto_bind_new":
                vtx_cnt = _auto_bind_new_weights_preserving_existing(mesh_node, skin_cluster, joints)
                result["weighted_vertices"] += vtx_cnt
            # add_zero_only の場合は wt=0 追加のみ実行して終了する
    finally:
        if orig_sel:
            existing_orig = [n for n in orig_sel if cmds.objExists(n)]
            if existing_orig:
                try:
                    cmds.select(existing_orig, replace=True)
                except Exception:
                    pass

    return result


# ------------------------------------------------------------------
# 選択ボーン回転フリーズ（子ボーン含めキー退避＆書き戻し・基準F以降そのまま復元）
# ------------------------------------------------------------------
def _resolve_attr_anim_curve(attr):
    """属性に接続された animCurve ノード（unitConversion 経由も含む）を返す。"""
    try:
        conns = cmds.listConnections(attr, source=True, destination=False) or []
    except Exception:
        conns = []
    for src in conns:
        if not cmds.objExists(src):
            continue
        try:
            ntype = cmds.nodeType(src)
        except Exception:
            continue
        if ntype.startswith("animCurve"):
            return src
        if ntype == "unitConversion":
            try:
                up = cmds.listConnections(src, source=True, destination=False) or []
            except Exception:
                up = []
            for up_node in up:
                if cmds.objExists(up_node):
                    try:
                        if cmds.nodeType(up_node).startswith("animCurve"):
                            return up_node
                    except Exception:
                        pass
    return None


def _snapshot_single_attr_keyframes(attr):
    """単一チャネル属性のキーフレーム時刻・値・タンジェント・インフィニティ設定をそのまま退避する。"""
    if not cmds.objExists(attr):
        return None
    curve = _resolve_attr_anim_curve(attr)
    times = []
    values = []
    query_target = attr
    try:
        times = cmds.keyframe(attr, q=True, timeChange=True) or []
        values = cmds.keyframe(attr, q=True, valueChange=True) or []
    except Exception:
        times = []
        values = []

    if (not times or len(times) != len(values)) and curve:
        try:
            times = cmds.keyframe(curve, q=True, timeChange=True) or []
            values = cmds.keyframe(curve, q=True, valueChange=True) or []
            query_target = curve
        except Exception:
            times = []
            values = []

    if not times or len(times) != len(values):
        return None

    weighted_tangents = False
    try:
        wt_res = cmds.keyTangent(query_target, q=True, weightedTangents=True)
        if wt_res:
            weighted_tangents = bool(wt_res[0])
    except Exception:
        weighted_tangents = False

    pre_inf = None
    post_inf = None
    if curve and cmds.objExists(curve):
        try:
            pre_inf = int(cmds.getAttr(curve + ".preInfinity"))
            post_inf = int(cmds.getAttr(curve + ".postInfinity"))
        except Exception:
            pre_inf = None
            post_inf = None

    keys = []
    for t_val, v_val in zip(times, values):
        t_f = float(t_val)
        v_f = float(v_val)
        t_range = (t_f, t_f)
        k_entry = {
            "time": t_f,
            "value": v_f,
            "inTangentType": None,
            "outTangentType": None,
            "inAngle": None,
            "outAngle": None,
            "inWeight": None,
            "outWeight": None,
            "lock": None,
            "weightLock": None,
        }
        try:
            itt = cmds.keyTangent(query_target, q=True, time=t_range, inTangentType=True)
            ott = cmds.keyTangent(query_target, q=True, time=t_range, outTangentType=True)
            ia = cmds.keyTangent(query_target, q=True, time=t_range, inAngle=True)
            oa = cmds.keyTangent(query_target, q=True, time=t_range, outAngle=True)
            iw = cmds.keyTangent(query_target, q=True, time=t_range, inWeight=True)
            ow = cmds.keyTangent(query_target, q=True, time=t_range, outWeight=True)
            lk = cmds.keyTangent(query_target, q=True, time=t_range, lock=True)
            wlk = cmds.keyTangent(query_target, q=True, time=t_range, weightLock=True)
            if itt:
                k_entry["inTangentType"] = str(itt[0])
            if ott:
                k_entry["outTangentType"] = str(ott[0])
            if ia:
                k_entry["inAngle"] = float(ia[0])
            if oa:
                k_entry["outAngle"] = float(oa[0])
            if iw:
                k_entry["inWeight"] = float(iw[0])
            if ow:
                k_entry["outWeight"] = float(ow[0])
            if lk is not None and len(lk) > 0:
                k_entry["lock"] = bool(lk[0])
            if wlk is not None and len(wlk) > 0:
                k_entry["weightLock"] = bool(wlk[0])
        except Exception:
            pass
        keys.append(k_entry)

    return {
        "weightedTangents": weighted_tangents,
        "preInfinity": pre_inf,
        "postInfinity": post_inf,
        "keys": keys,
    }


def snapshot_joints_keyframes_exact(joints, channels=("translate", "rotate", "scale")):
    """ジョイント群の全キーフレーム（値・時刻・タンジェント）をそのまま辞書へ退避する。"""
    ordered = sorted(set(j for j in (joints or []) if cmds.objExists(j)), key=_dag_depth)
    snapshot = {}
    for jnt in ordered:
        j_data = {}
        for ch in channels:
            for axis in ("X", "Y", "Z"):
                ch_name = "{0}{1}".format(ch, axis)
                attr = "{0}.{1}".format(jnt, ch_name)
                curve_data = _snapshot_single_attr_keyframes(attr)
                if curve_data:
                    j_data[ch_name] = curve_data
        if j_data:
            snapshot[jnt] = j_data
    return snapshot


def _apply_attr_keyframe_snapshot(attr, curve_data, zero_at_base_frame=False, base_frame=0.0):
    """退避済みキーフレームを属性へそのまま書き戻す（必要時のみ基準フレーム値を 0.0 に固定）。"""
    keys = list(curve_data.get("keys") or [])
    if not keys:
        return 0

    has_base_key = False
    written = 0
    for k in keys:
        t_f = float(k["time"])
        is_base = abs(t_f - float(base_frame)) <= 1e-4
        if is_base:
            has_base_key = True
        val = 0.0 if (zero_at_base_frame and is_base) else float(k["value"])
        cmds.setKeyframe(attr, time=(t_f,), value=val)
        written += 1

    # フリーズ対象ボーンで基準フレームより後にキーがある場合、基準フレームにも 0.0 キーを確実に打つ
    if zero_at_base_frame and not has_base_key:
        cmds.setKeyframe(attr, time=(float(base_frame),), value=0.0)
        written += 1

    # カーブのウェイト付きタンジェント・インフィニティ設定を復元する
    if curve_data.get("weightedTangents"):
        try:
            cmds.keyTangent(attr, edit=True, weightedTangents=True)
        except Exception:
            pass

    for k in keys:
        t_f = float(k["time"])
        t_range = (t_f, t_f)
        itt = k.get("inTangentType")
        ott = k.get("outTangentType")
        try:
            if itt and ott:
                cmds.keyTangent(attr, edit=True, time=t_range, inTangentType=itt, outTangentType=ott)
            if k.get("lock") is not None:
                cmds.keyTangent(attr, edit=True, time=t_range, lock=bool(k["lock"]))
            if k.get("weightLock") is not None:
                cmds.keyTangent(attr, edit=True, time=t_range, weightLock=bool(k["weightLock"]))
            if itt == "fixed" and k.get("inAngle") is not None:
                cmds.keyTangent(attr, edit=True, time=t_range, inAngle=float(k["inAngle"]))
            if ott == "fixed" and k.get("outAngle") is not None:
                cmds.keyTangent(attr, edit=True, time=t_range, outAngle=float(k["outAngle"]))
            if curve_data.get("weightedTangents"):
                if k.get("inWeight") is not None:
                    cmds.keyTangent(attr, edit=True, time=t_range, inWeight=float(k["inWeight"]))
                if k.get("outWeight") is not None:
                    cmds.keyTangent(attr, edit=True, time=t_range, outWeight=float(k["outWeight"]))
        except Exception:
            pass

    new_curve = _resolve_attr_anim_curve(attr)
    if new_curve and cmds.objExists(new_curve):
        if curve_data.get("preInfinity") is not None:
            try:
                cmds.setAttr(new_curve + ".preInfinity", int(curve_data["preInfinity"]))
            except Exception:
                pass
        if curve_data.get("postInfinity") is not None:
            try:
                cmds.setAttr(new_curve + ".postInfinity", int(curve_data["postInfinity"]))
            except Exception:
                pass

    return written


def restore_joints_keyframes_exact(snapshot, frozen_joints, base_frame=0.0):
    """退避したキーフレーム群を書き戻す（フリーズ対象の回転は基準F=0、基準F以外は元の値をそのまま復元）。"""
    frozen_set = set(frozen_joints or [])
    rot_channels = ("rotateX", "rotateY", "rotateZ")
    restored_joints = 0
    restored_keys = 0
    zeroed_base_keys = 0

    for jnt, j_data in (snapshot or {}).items():
        if not cmds.objExists(jnt):
            continue
        is_frozen = jnt in frozen_set
        had_any_rot_key = any(ch in j_data for ch in rot_channels)
        joint_touched = False

        # 退避済みチャネルを復元する（非フリーズ対象の子ボーンも含め、元の時刻・値をそのまま書き戻す）
        for ch_name, curve_data in j_data.items():
            attr = "{0}.{1}".format(jnt, ch_name)
            if not cmds.objExists(attr):
                continue
            prefix = ch_name[:-1]
            _ensure_attr_unlocked_for_key(jnt, prefix)
            zero_base = is_frozen and (ch_name in rot_channels)
            try:
                count = _apply_attr_keyframe_snapshot(
                    attr,
                    curve_data,
                    zero_at_base_frame=zero_base,
                    base_frame=base_frame,
                )
                if count > 0:
                    restored_keys += count
                    joint_touched = True
                    if zero_base:
                        zeroed_base_keys += 1
            except Exception:
                pass

        # フリーズ対象ボーンで一部の回転軸にのみキーがあった場合も、残りの回転軸へ基準F=0 を揃えておく
        if is_frozen and had_any_rot_key:
            for ch_name in rot_channels:
                if ch_name in j_data:
                    continue
                attr = "{0}.{1}".format(jnt, ch_name)
                if not cmds.objExists(attr):
                    continue
                _ensure_attr_unlocked_for_key(jnt, "rotate")
                try:
                    cmds.setKeyframe(attr, time=(float(base_frame),), value=0.0)
                    restored_keys += 1
                    zeroed_base_keys += 1
                    joint_touched = True
                except Exception:
                    pass

        if joint_touched:
            restored_joints += 1

    return {
        "restored_joints": restored_joints,
        "restored_keys": restored_keys,
        "zeroed_base_keys": zeroed_base_keys,
    }


def freeze_selected_bone_rotation_preserving_keyframes(
    selected_joints,
    base_storage_dir=None,
    base_frame=0.0,
    include_children_in_freeze=False,
):
    """選択ボーンの回転XYZを現在値で jointOrient へフリーズして 0 へ戻し、選択ボーンと子ボーンのキーフレームを復元する。"""
    sel_ordered = sorted(
        set(j for j in (selected_joints or []) if cmds.objExists(j) and cmds.nodeType(j) == "joint"),
        key=_dag_depth,
    )
    if not sel_ordered:
        raise RuntimeError("フリーズ対象のジョイントが選択されていません。")

    # 選択ボーン配下の全子ボーンを収集する
    child_joints = []
    seen_all = set(sel_ordered)
    for jnt in sel_ordered:
        desc = cmds.listRelatives(jnt, allDescendents=True, type="joint", fullPath=True) or []
        for child in desc:
            if child not in seen_all and cmds.objExists(child):
                seen_all.add(child)
                child_joints.append(child)
    child_joints.sort(key=_dag_depth)

    all_key_joints = sorted(seen_all, key=_dag_depth)
    freeze_targets = list(all_key_joints) if include_children_in_freeze else list(sel_ordered)

    orig_frame = float(cmds.currentTime(q=True))
    orig_sel = cmds.ls(sl=True, long=True) or []
    prev_autokey = None
    try:
        prev_autokey = bool(cmds.autoKeyframe(q=True, state=True))
        if prev_autokey:
            cmds.autoKeyframe(state=False)
    except Exception:
        prev_autokey = None

    backup_path = None
    frozen_joints = []
    skipped_joints = []
    restore_report = {"restored_joints": 0, "restored_keys": 0, "zeroed_base_keys": 0}

    try:
        # 基準フレーム上にいる場合は、未キーの入力値（チャンネルボックス表示値）も含めてそのまま保持する
        if abs(orig_frame - float(base_frame)) > 1e-6:
            cmds.currentTime(float(base_frame), edit=True)

        # 選択ボーンと全子ボーンのキーフレームを退避する
        key_snapshot = snapshot_joints_keyframes_exact(all_key_joints, channels=("translate", "rotate", "scale"))

        # フリーズ対象ボーンの基準フレームにおける現在回転・回転軸を退避する
        live_rot_map = {}
        for jnt in freeze_targets:
            try:
                rot = cmds.getAttr(jnt + ".rotate")[0]
                ra = cmds.getAttr(jnt + ".rotateAxis")[0]
                live_rot_map[jnt] = {
                    "rotate": [float(rot[0]), float(rot[1]), float(rot[2])],
                    "rotateAxis": [float(ra[0]), float(ra[1]), float(ra[2])],
                }
            except Exception:
                pass

        # 安全のため JSON バックアップを保存する
        if base_storage_dir:
            try:
                backup_dir = get_pose_backup_dir(base_storage_dir)
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(backup_dir, "freeze_rot_keys_{0}.json".format(stamp))
                with open(backup_path, "w") as fp:
                    json.dump(
                        {
                            "version": 1,
                            "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "base_frame": float(base_frame),
                            "selected_joints": list(sel_ordered),
                            "child_joints": list(child_joints),
                            "freeze_targets": list(freeze_targets),
                            "keyframes": key_snapshot,
                        },
                        fp,
                        indent=2,
                    )
            except Exception:
                backup_path = None

        # フリーズ対象の回転系入力接続を安全に解除してから現在回転を焼き込む
        break_report = break_and_unlock_joint_attrs(
            freeze_targets,
            channels=("rotate", "jointOrient", "rotateAxis"),
            delete_unused_anim_curves=True,
        )

        # 接続切断前のライブ回転値を反映し、親→子順で現在姿勢をそのまま jointOrient へ焼き込む
        for jnt in freeze_targets:
            ov = live_rot_map.get(jnt)
            if ov and cmds.objExists(jnt):
                try:
                    cmds.setAttr(jnt + ".rotateAxis", ov["rotateAxis"][0], ov["rotateAxis"][1], ov["rotateAxis"][2], type="double3")
                    cmds.setAttr(jnt + ".rotate", ov["rotate"][0], ov["rotate"][1], ov["rotate"][2], type="double3")
                except Exception:
                    pass

        for jnt in freeze_targets:
            if not cmds.objExists(jnt):
                continue
            world_rot_mat = _get_orthonormal_rotation_matrix(jnt)
            ok = _set_joint_world_rot_to_orient(jnt, world_rot_mat)
            if ok:
                frozen_joints.append(jnt)
            else:
                skipped_joints.append(jnt)

        # 選択ボーンと子ボーンのキーフレームを書き戻す（フリーズ対象回転の基準Fは 0.0、基準F以外は元の値をそのまま復元）
        restore_report = restore_joints_keyframes_exact(
            key_snapshot,
            frozen_joints=frozen_joints,
            base_frame=base_frame,
        )

        # 基準フレームを再評価し、フリーズ対象の基準F回転が確実に (0, 0, 0) になっている状態へ揃える
        cmds.currentTime(float(base_frame), edit=True)
        for jnt in frozen_joints:
            if not cmds.objExists(jnt):
                continue
            if jnt not in key_snapshot or not any(ch in key_snapshot[jnt] for ch in ("rotateX", "rotateY", "rotateZ")):
                try:
                    cmds.setAttr(jnt + ".rotate", 0.0, 0.0, 0.0, type="double3")
                except Exception:
                    pass

        if abs(orig_frame - float(base_frame)) > 1e-6:
            cmds.currentTime(orig_frame, edit=True)
    finally:
        if prev_autokey is True:
            try:
                cmds.autoKeyframe(state=True)
            except Exception:
                pass
        if orig_sel:
            existing_sel = [n for n in orig_sel if cmds.objExists(n)]
            if existing_sel:
                try:
                    cmds.select(existing_sel, replace=True)
                except Exception:
                    pass

    return {
        "selected_joints": sel_ordered,
        "child_joints": child_joints,
        "freeze_targets": freeze_targets,
        "frozen_joints": frozen_joints,
        "skipped_joints": skipped_joints,
        "base_frame": float(base_frame),
        "backup_path": backup_path,
        "broken_connections": len(break_report.get("broken", [])),
        "restored_joints": restore_report["restored_joints"],
        "restored_keys": restore_report["restored_keys"],
        "zeroed_base_keys": restore_report["zeroed_base_keys"],
    }


# ------------------------------------------------------------------
# ウェイト無し（未バインド / 全頂点 0）メッシュ検出 ＆ 指定ボーン一括バインド
# ------------------------------------------------------------------
def _get_visible_mesh_dag_for_unweighted(mesh_transform):
    """メッシュトランスフォームから中間オブジェクト以外の可視メッシュ DAG を返す。"""
    try:
        sel = om2.MSelectionList()
        sel.add(mesh_transform)
        dag = sel.getDagPath(0)
    except Exception:
        return None
    if dag.node().hasFn(om2.MFn.kMesh):
        return dag
    for idx in range(dag.numberOfShapesDirectlyBelow()):
        s_dag = om2.MDagPath(dag)
        try:
            s_dag.extendToShape(idx)
        except Exception:
            continue
        if not s_dag.node().hasFn(om2.MFn.kMesh):
            continue
        try:
            if not om2.MFnDagNode(s_dag).isIntermediateObject:
                return s_dag
        except Exception:
            return s_dag
    return None


def _is_dag_visible(mesh_long, shape_dag=None):
    """対象メッシュがビューポート上で表示状態か判定する（自身・シェイプ・親階層・描画オーバーライドを含む）。"""
    if shape_dag is not None and hasattr(shape_dag, "isVisible"):
        try:
            if not shape_dag.isVisible():
                return False
        except Exception:
            pass

    # DAG パス上の先祖トランスフォームおよび自身・シェイプの可視性を確認する
    check_nodes = []
    parts = [p for p in str(mesh_long).split("|") if p]
    cur_path = ""
    for part in parts:
        cur_path += "|" + part
        check_nodes.append(cur_path)

    if shape_dag is not None:
        try:
            sh_path = shape_dag.fullPathName()
            if sh_path and sh_path not in check_nodes:
                check_nodes.append(sh_path)
        except Exception:
            pass

    for node in check_nodes:
        if not cmds.objExists(node):
            continue
        vis_attr = node + ".visibility"
        if cmds.objExists(vis_attr):
            try:
                if not cmds.getAttr(vis_attr):
                    return False
            except Exception:
                pass
        ov_en_attr = node + ".overrideEnabled"
        ov_vis_attr = node + ".overrideVisibility"
        if cmds.objExists(ov_en_attr) and cmds.objExists(ov_vis_attr):
            try:
                if cmds.getAttr(ov_en_attr) and not cmds.getAttr(ov_vis_attr):
                    return False
            except Exception:
                pass
    return True


def find_unweighted_meshes(include_zero_weight_skinned=True, exclude_hidden=True):
    """シーン内でウェイトが一つも入っていないポリゴンメッシュ一覧を返す。"""
    shapes = cmds.ls(type="mesh", noIntermediate=True, long=True) or []
    seen_transforms = set()
    results = []

    for shape in shapes:
        if not cmds.objExists(shape):
            continue
        parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
        if not parents:
            continue
        mesh_long = parents[0]
        if mesh_long in seen_transforms or not cmds.objExists(mesh_long):
            continue
        seen_transforms.add(mesh_long)

        shape_dag = _get_visible_mesh_dag_for_unweighted(mesh_long)
        if shape_dag is None:
            continue
        if exclude_hidden and not _is_dag_visible(mesh_long, shape_dag=shape_dag):
            continue
        try:
            vtx_count = int(om2.MFnMesh(shape_dag).numVertices)
        except Exception:
            vtx_count = 0
        if vtx_count <= 0:
            continue

        sc = _get_skin_cluster_for_mesh_bind(mesh_long)
        if not sc:
            results.append({
                "mesh_long": mesh_long,
                "mesh_short": mesh_long.split("|")[-1],
                "status": "unbound",
                "vertex_count": vtx_count,
                "skin_cluster": None,
            })
            continue

        if not include_zero_weight_skinned or oma2 is None:
            continue

        # スキンクラスタが存在する場合でも、全頂点ウェイト最大値がほぼ 0 ならウェイト無しとして検出する
        try:
            skin_sel = om2.MGlobal.getSelectionListByName(sc)
            skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
            infs = skin_fn.influenceObjects()
            if len(infs) == 0:
                is_all_zero = True
            else:
                comp_fn = om2.MFnSingleIndexedComponent()
                vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
                comp_fn.setCompleteData(vtx_count)
                flat_w, _ = skin_fn.getWeights(shape_dag, vtx_comp)
                is_all_zero = (len(flat_w) == 0) or (max(flat_w) <= 1e-6)
        except Exception:
            is_all_zero = False

        if is_all_zero:
            results.append({
                "mesh_long": mesh_long,
                "mesh_short": mesh_long.split("|")[-1],
                "status": "zero_weight",
                "vertex_count": vtx_count,
                "skin_cluster": sc,
            })

    results.sort(key=lambda item: (0 if item["status"] == "unbound" else 1, item["mesh_short"].lower()))
    return results


def list_scene_joints_hierarchy():
    """シーン内の全ジョイントを階層順（親→子・深さ付き）で返す。"""
    all_joints = cmds.ls(type="joint", long=True) or []
    if not all_joints:
        return []
    joint_set = set(all_joints)
    children_map = {}
    roots = []

    for jnt in all_joints:
        parents = cmds.listRelatives(jnt, parent=True, type="joint", fullPath=True) or []
        parent_jnt = parents[0] if (parents and parents[0] in joint_set) else None
        if parent_jnt is None:
            roots.append(jnt)
        else:
            children_map.setdefault(parent_jnt, []).append(jnt)

    roots.sort(key=lambda p: p.split("|")[-1].lower())
    for k in children_map:
        children_map[k].sort(key=lambda p: p.split("|")[-1].lower())

    ordered = []

    def _walk(node, depth, parent_short):
        short_name = node.split("|")[-1]
        ordered.append({
            "joint_long": node,
            "joint_short": short_name,
            "parent_short": parent_short or "",
            "depth": depth,
        })
        for child in children_map.get(node, []):
            _walk(child, depth + 1, short_name)

    for root in roots:
        _walk(root, 0, "")

    return ordered


def bind_unweighted_meshes_to_joints(
    target_meshes,
    target_joints,
    assign_mode="rigid_single",
    include_children=False,
    unlock_influences=True,
):
    """ウェイトが入っていないメッシュ群を指定ボーン群へ安全にバインド・ウェイト割り当てする。"""
    meshes = []
    seen_m = set()
    for m in (target_meshes or []):
        if m and cmds.objExists(m) and m not in seen_m:
            seen_m.add(m)
            meshes.append(m)
    if not meshes:
        raise RuntimeError("バインド対象のメッシュが指定されていません。")

    joints = []
    seen_j = set()
    for j in (target_joints or []):
        if not j or not cmds.objExists(j) or cmds.nodeType(j) != "joint":
            continue
        if j not in seen_j:
            seen_j.add(j)
            joints.append(j)
        if include_children:
            desc = cmds.listRelatives(j, allDescendents=True, type="joint", fullPath=True) or []
            for child in desc:
                if child not in seen_j and cmds.objExists(child):
                    seen_j.add(child)
                    joints.append(child)
    joints.sort(key=_dag_depth)
    if not joints:
        raise RuntimeError("割り当て先のジョイントが指定されていません。")

    orig_sel = cmds.ls(sl=True, long=True) or []
    joint_pos_map = {}
    for jnt in joints:
        try:
            pos = cmds.xform(jnt, q=True, ws=True, t=True)
            joint_pos_map[jnt] = (float(pos[0]), float(pos[1]), float(pos[2]))
        except Exception:
            joint_pos_map[jnt] = (0.0, 0.0, 0.0)

    newly_bound = []
    updated_meshes = []
    total_vertices = 0

    try:
        for mesh_long in meshes:
            shape_dag = _get_visible_mesh_dag_for_unweighted(mesh_long)
            if shape_dag is None:
                continue
            mesh_fn = om2.MFnMesh(shape_dag)
            vtx_count = int(mesh_fn.numVertices)
            if vtx_count <= 0:
                continue

            sc = _get_skin_cluster_for_mesh_bind(mesh_long)
            was_unbound = not bool(sc)

            if was_unbound:
                sc_list = cmds.skinCluster(
                    joints,
                    mesh_long,
                    toSelectedBones=True,
                    bindMethod=0,
                    normalizeWeights=1,
                )
                sc = sc_list[0]
                newly_bound.append(mesh_long)
            else:
                existing_infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], long=True) or []
                existing_set = set(existing_infs)
                missing = [j for j in joints if j not in existing_set]
                if missing:
                    cmds.skinCluster(
                        sc,
                        edit=True,
                        addInfluence=missing,
                        lockWeights=True,
                        useGeometry=False,
                        weight=0.0,
                        polySmoothness=0.0,
                    )
                updated_meshes.append(mesh_long)

            if unlock_influences:
                for jnt in joints:
                    attr = jnt + ".liw"
                    if cmds.objExists(attr):
                        try:
                            if cmds.getAttr(attr, settable=True):
                                cmds.setAttr(attr, False)
                        except Exception:
                            pass

            # 割り当て方式に応じて OpenMaya API 2.0 でウェイトを一括設定する
            if assign_mode in ("rigid_single", "auto_bind") and oma2 is not None:
                # 新規バインド＋auto_bind の場合は Maya の近接バインド結果をそのまま活かす
                need_manual_weights = (assign_mode == "rigid_single") or (not was_unbound)
                if need_manual_weights:
                    skin_sel = om2.MGlobal.getSelectionListByName(sc)
                    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
                    inf_dags = skin_fn.influenceObjects()
                    inf_count = len(inf_dags)
                    inf_index_map = {}
                    for idx in range(inf_count):
                        full_p = inf_dags[idx].fullPathName()
                        inf_index_map[full_p] = idx
                        inf_index_map[full_p.split("|")[-1]] = idx

                    target_inf_entries = []
                    for jnt in joints:
                        idx = inf_index_map.get(jnt)
                        if idx is None:
                            idx = inf_index_map.get(jnt.split("|")[-1])
                        if idx is not None:
                            target_inf_entries.append((idx, joint_pos_map[jnt]))

                    if target_inf_entries:
                        shape_dag = _get_visible_mesh_dag_for_unweighted(mesh_long)
                        mesh_pts = om2.MFnMesh(shape_dag).getPoints(om2.MSpace.kWorld)
                        inf_indices = om2.MIntArray(list(range(inf_count)))
                        comp_fn = om2.MFnSingleIndexedComponent()
                        vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
                        comp_fn.setCompleteData(vtx_count)

                        flat_weights = om2.MDoubleArray(vtx_count * inf_count, 0.0)
                        if assign_mode == "rigid_single" and len(target_inf_entries) == 1:
                            only_idx = target_inf_entries[0][0]
                            for vid in range(vtx_count):
                                flat_weights[vid * inf_count + only_idx] = 1.0
                        elif assign_mode == "rigid_single":
                            for vid in range(vtx_count):
                                pt = mesh_pts[vid]
                                best_idx = target_inf_entries[0][0]
                                best_d2 = float("inf")
                                for idx, jp in target_inf_entries:
                                    dx = pt.x - jp[0]
                                    dy = pt.y - jp[1]
                                    dz = pt.z - jp[2]
                                    d2 = dx * dx + dy * dy + dz * dz
                                    if d2 < best_d2:
                                        best_d2 = d2
                                        best_idx = idx
                                flat_weights[vid * inf_count + best_idx] = 1.0
                        else:
                            # 既存ウェイト 0 スキンへの auto_bind は最寄り最大 4 本への距離逆二乗加重で滑らかに割り当てる
                            for vid in range(vtx_count):
                                pt = mesh_pts[vid]
                                scored = []
                                for idx, jp in target_inf_entries:
                                    dx = pt.x - jp[0]
                                    dy = pt.y - jp[1]
                                    dz = pt.z - jp[2]
                                    d2 = dx * dx + dy * dy + dz * dz
                                    scored.append((d2, idx))
                                scored.sort(key=lambda item: item[0])
                                top_k = scored[: min(4, len(scored))]
                                base = vid * inf_count
                                if top_k[0][0] < 1e-10:
                                    flat_weights[base + top_k[0][1]] = 1.0
                                else:
                                    inv_list = [(idx, 1.0 / (d2 + 1e-6)) for d2, idx in top_k]
                                    total_inv = sum(w for _, w in inv_list)
                                    for idx, w in inv_list:
                                        flat_weights[base + idx] = w / total_inv

                        _set_weights_without_norm_warning(
                            skin_fn, shape_dag, vtx_comp, inf_indices, flat_weights
                        )

            try:
                cmds.dgdirty(mesh_long)
            except Exception:
                pass
            total_vertices += vtx_count
    finally:
        if orig_sel:
            existing_sel = [n for n in orig_sel if cmds.objExists(n)]
            if existing_sel:
                try:
                    cmds.select(existing_sel, replace=True)
                except Exception:
                    pass

    return {
        "newly_bound_meshes": newly_bound,
        "updated_meshes": updated_meshes,
        "target_joints": joints,
        "total_vertices": total_vertices,
        "assign_mode": assign_mode,
    }
