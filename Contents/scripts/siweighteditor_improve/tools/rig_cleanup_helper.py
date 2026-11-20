# -*- coding: utf-8 -*-
"""ジョイント値整理・Joint Orient・メッシュクリーンアップ・シーンクリーンアップ用ヘルパー。"""

import math

from maya import cmds
from maya import mel
import maya.api.OpenMaya as om2


EPSILON_ROT = 1e-4
EPSILON_SCALE = 1e-4
EPSILON_TRANS = 1e-4


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


def inspect_joints(joints):
    """ジョイント群の Rotate / Scale / RotateAxis / Translate 状態を診断して結果一覧を返す。"""
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

        if bad_rot or bad_scl or bad_ra or bad_ty_tz:
            tags = []
            if bad_rot:
                tags.append("Rotate!=0")
            if bad_scl:
                tags.append("Scale!=1")
            if bad_ra:
                tags.append("RotAxis!=0")
            if bad_ty_tz:
                tags.append("Ty/Tz!=0")
            issues.append({
                "joint": jnt,
                "short_name": jnt.split("|")[-1],
                "bad_rot": bad_rot,
                "bad_scl": bad_scl,
                "bad_ra": bad_ra,
                "bad_ty_tz": bad_ty_tz,
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
