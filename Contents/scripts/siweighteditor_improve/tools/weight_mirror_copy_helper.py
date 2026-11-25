# -*- coding: utf-8 -*-
"""ボーン・メッシュ・頂点選択すべてに対応する安全・高精度スキンウェイトミラーおよびコピー用ヘルパー。"""

import math
from collections import defaultdict

from maya import cmds
import maya.api.OpenMaya as om2
try:
    import maya.api.OpenMayaAnim as oma2
except Exception:
    oma2 = None

from . import rig_cleanup_helper


WEIGHT_EPS = 1e-4
DUST_EPS = 1e-6
SNAP_TOL_DEFAULT = 1e-3
SEAM_TOL_DEFAULT = 1e-3

PLANE_AXIS_MAP = {
    "YZ": 0,  # X 軸反転
    "XZ": 1,  # Y 軸反転
    "XY": 2,  # Z 軸反転
}


def _mirror_point(pt, axis_idx, center_val=0.0):
    """指定軸について対称面 center_val で反転した 3D 座標タプルを返す。"""
    coords = [float(pt[0]), float(pt[1]), float(pt[2])]
    coords[axis_idx] = 2.0 * float(center_val) - coords[axis_idx]
    return (coords[0], coords[1], coords[2])


def _dist_sq(p1, p2):
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    dz = p1[2] - p2[2]
    return dx * dx + dy * dy + dz * dz


def _closest_point_barycentric_on_triangle(p, a, b, c):
    """点 p から三角形 (a, b, c) 上の最近傍点を求め、(距離二乗, (u, v, w)) を返す。"""
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    ap = (p[0] - a[0], p[1] - a[1], p[2] - a[2])

    d1 = ab[0] * ap[0] + ab[1] * ap[1] + ab[2] * ap[2]
    d2 = ac[0] * ap[0] + ac[1] * ap[1] + ac[2] * ap[2]
    if d1 <= 0.0 and d2 <= 0.0:
        return _dist_sq(p, a), (1.0, 0.0, 0.0)

    bp = (p[0] - b[0], p[1] - b[1], p[2] - b[2])
    d3 = ab[0] * bp[0] + ab[1] * bp[1] + ab[2] * bp[2]
    d4 = ac[0] * bp[0] + ac[1] * bp[1] + ac[2] * bp[2]
    if d3 >= 0.0 and d4 <= d3:
        return _dist_sq(p, b), (0.0, 1.0, 0.0)

    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        denom = d1 - d3
        v = (d1 / denom) if abs(denom) > 1e-12 else 0.0
        q = (a[0] + v * ab[0], a[1] + v * ab[1], a[2] + v * ab[2])
        return _dist_sq(p, q), (1.0 - v, v, 0.0)

    cp = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
    d5 = ab[0] * cp[0] + ab[1] * cp[1] + ab[2] * cp[2]
    d6 = ac[0] * cp[0] + ac[1] * cp[1] + ac[2] * cp[2]
    if d6 >= 0.0 and d5 <= d6:
        return _dist_sq(p, c), (0.0, 0.0, 1.0)

    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        denom = d2 - d6
        w = (d2 / denom) if abs(denom) > 1e-12 else 0.0
        q = (a[0] + w * ac[0], a[1] + w * ac[1], a[2] + w * ac[2])
        return _dist_sq(p, q), (1.0 - w, 0.0, w)

    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        denom = (d4 - d3) + (d5 - d6)
        w = ((d4 - d3) / denom) if abs(denom) > 1e-12 else 0.0
        q = (
            b[0] + w * (c[0] - b[0]),
            b[1] + w * (c[1] - b[1]),
            b[2] + w * (c[2] - b[2]),
        )
        return _dist_sq(p, q), (0.0, 1.0 - w, w)

    denom = va + vb + vc
    if abs(denom) <= 1e-12:
        return _dist_sq(p, a), (1.0, 0.0, 0.0)
    inv_d = 1.0 / denom
    v = vb * inv_d
    w = vc * inv_d
    u = 1.0 - v - w
    q = (
        u * a[0] + v * b[0] + w * c[0],
        u * a[1] + v * b[1] + w * c[1],
        u * a[2] + v * b[2] + w * c[2],
    )
    return _dist_sq(p, q), (u, v, w)


def sample_polygon_barycentric_weights(query_pt, poly_vids, vtx_positions, sample_mode="auto", snap_tol=SNAP_TOL_DEFAULT):
    """対象ポリゴン上の最近傍点から (頂点ID, 補間係数) リストを算出する。"""
    if not poly_vids:
        return []

    best_v = poly_vids[0]
    best_v_d2 = float("inf")
    for vid in poly_vids:
        d2 = _dist_sq(query_pt, vtx_positions[vid])
        if d2 < best_v_d2:
            best_v_d2 = d2
            best_v = vid

    if sample_mode == "nearest" or len(poly_vids) < 3:
        return [(best_v, 1.0)]

    if sample_mode == "auto" and best_v_d2 <= (snap_tol * snap_tol):
        return [(best_v, 1.0)]

    # ポリゴンを扇形三角形分割して最も近い三角形上の重心座標（Barycentric）を求める
    v0 = poly_vids[0]
    p0 = vtx_positions[v0]
    best_tri_d2 = float("inf")
    best_terms = [(best_v, 1.0)]
    for idx in range(1, len(poly_vids) - 1):
        v1 = poly_vids[idx]
        v2 = poly_vids[idx + 1]
        d2, (u, v, w) = _closest_point_barycentric_on_triangle(
            query_pt, p0, vtx_positions[v1], vtx_positions[v2]
        )
        if d2 < best_tri_d2:
            best_tri_d2 = d2
            terms = []
            if u > 1e-9:
                terms.append((v0, u))
            if v > 1e-9:
                terms.append((v1, v))
            if w > 1e-9:
                terms.append((v2, w))
            if terms:
                best_terms = terms
    return best_terms


def _clean_and_normalize_weight_vector(weights, max_influences=0, dust_eps=DUST_EPS):
    """微小ノイズの除去・最大インフルエンス数制限・合計 1.0 正規化を行う。"""
    cleaned = [w if w > dust_eps else 0.0 for w in weights]
    if max_influences and max_influences > 0:
        non_zero = [(idx, w) for idx, w in enumerate(cleaned) if w > 0.0]
        if len(non_zero) > max_influences:
            non_zero.sort(key=lambda item: item[1], reverse=True)
            keep_ids = {idx for idx, _ in non_zero[:max_influences]}
            cleaned = [w if idx in keep_ids else 0.0 for idx, w in enumerate(cleaned)]

    total = sum(cleaned)
    if total <= 1e-12:
        return list(weights)
    if abs(total - 1.0) > 1e-9:
        inv_t = 1.0 / total
        cleaned = [w * inv_t for w in cleaned]
    return cleaned


def build_hybrid_mirror_joint_map(
    joints,
    joint_positions=None,
    plane="YZ",
    center_val=0.0,
    use_position_fallback=True,
    pos_tol=None,
    center_eps=1e-3,
):
    """命名規則＋3D 座標フォールバックで左右対称ボーンの対応辞書を構築する。"""
    axis_idx = PLANE_AXIS_MAP.get(plane, 0)
    unique_joints = []
    seen = set()
    for jnt in (joints or []):
        if jnt and jnt not in seen:
            seen.add(jnt)
            unique_joints.append(jnt)

    if joint_positions is None:
        joint_positions = {}
        for jnt in unique_joints:
            if cmds.objExists(jnt):
                try:
                    p = cmds.xform(jnt, q=True, ws=True, t=True)
                    joint_positions[jnt] = (float(p[0]), float(p[1]), float(p[2]))
                except Exception:
                    pass

    mirror_map = {}
    name_pairs = []
    pos_pairs = []
    center_joints = []
    unmatched_sided = []

    # 命名規則による左右対照合を最優先で実施する
    for jnt in unique_joints:
        if jnt in mirror_map:
            continue
        opp = rig_cleanup_helper.find_opposite_joint_in_list(jnt, unique_joints)
        if opp and opp != jnt and opp not in mirror_map:
            mirror_map[jnt] = opp
            mirror_map[opp] = jnt
            name_pairs.append((jnt, opp))

    # 未照合ボーンに対して 3D ミラー座標の相互最近傍フォールバックを適用する
    unmatched = [j for j in unique_joints if j not in mirror_map]
    if use_position_fallback and unmatched:
        if pos_tol is None:
            pts = [joint_positions[j] for j in unique_joints if j in joint_positions]
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                zs = [p[2] for p in pts]
                diag = math.sqrt(
                    (max(xs) - min(xs)) ** 2
                    + (max(ys) - min(ys)) ** 2
                    + (max(zs) - min(zs)) ** 2
                )
                pos_tol = max(0.5, diag * 0.04)
            else:
                pos_tol = 1.0
        tol_sq = float(pos_tol) * float(pos_tol)

        sided_candidates = []
        for jnt in unmatched:
            pos = joint_positions.get(jnt)
            if pos is None:
                mirror_map[jnt] = jnt
                center_joints.append(jnt)
                continue
            if abs(pos[axis_idx] - center_val) <= center_eps:
                mirror_map[jnt] = jnt
                center_joints.append(jnt)
            else:
                sided_candidates.append(jnt)

        best_match = {}
        for jnt in sided_candidates:
            pos = joint_positions[jnt]
            mir_p = _mirror_point(pos, axis_idx, center_val)
            best_k = None
            best_d2 = tol_sq
            for cand in sided_candidates:
                if cand == jnt:
                    continue
                c_pos = joint_positions[cand]
                # 対称面の反対側にあるジョイントだけを候補にする
                if (pos[axis_idx] - center_val) * (c_pos[axis_idx] - center_val) > 0.0:
                    continue
                d2 = _dist_sq(mir_p, c_pos)
                if d2 <= best_d2:
                    best_d2 = d2
                    best_k = cand
            if best_k is not None:
                best_match[jnt] = best_k

        for jnt in sided_candidates:
            if jnt in mirror_map:
                continue
            partner = best_match.get(jnt)
            if partner and partner not in mirror_map and best_match.get(partner) == jnt:
                mirror_map[jnt] = partner
                mirror_map[partner] = jnt
                pos_pairs.append((jnt, partner))
            else:
                mirror_map[jnt] = jnt
                unmatched_sided.append(jnt)
    else:
        for jnt in unmatched:
            pos = joint_positions.get(jnt)
            mirror_map[jnt] = jnt
            if pos is not None and abs(pos[axis_idx] - center_val) > center_eps:
                unmatched_sided.append(jnt)
            else:
                center_joints.append(jnt)

    info = {
        "name_pairs": name_pairs,
        "pos_pairs": pos_pairs,
        "center_joints": center_joints,
        "unmatched_sided": unmatched_sided,
    }
    return mirror_map, info


def _collect_full_skeleton_joints_from_seeds(seed_joints):
    """シードジョイント群が属するスケルトン全体のジョイント一覧を収集する。"""
    roots = []
    seen_roots = set()
    for jnt in (seed_joints or []):
        if not cmds.objExists(jnt):
            continue
        cur = (cmds.ls(jnt, long=True) or [jnt])[0]
        while True:
            parents = cmds.listRelatives(cur, parent=True, type="joint", fullPath=True) or []
            if not parents:
                break
            cur = parents[0]
        if cur not in seen_roots:
            seen_roots.add(cur)
            roots.append(cur)

    all_joints = list(roots)
    seen = set(roots)
    for root in roots:
        desc = cmds.listRelatives(root, allDescendents=True, type="joint", fullPath=True) or []
        for child in desc:
            if child not in seen:
                seen.add(child)
                all_joints.append(child)
    return all_joints


def _read_mesh_geometry_and_weights(mesh_node, skin_cluster):
    """メッシュの全頂点ワールド座標・ポリゴン参照関数・全ウェイト配列を OpenMaya API 2.0 で取得する。"""
    if oma2 is None:
        raise RuntimeError("maya.api.OpenMayaAnim を読み込めませんでした。")
    shape_dag = rig_cleanup_helper._get_visible_mesh_shape_dag_for_bind(mesh_node)
    if shape_dag is None:
        raise RuntimeError("可視メッシュシェイプを取得できません: {0}".format(mesh_node))

    mesh_fn = om2.MFnMesh(shape_dag)
    num_verts = mesh_fn.numVertices
    pts = mesh_fn.getPoints(om2.MSpace.kWorld)
    vtx_positions = [(float(pt.x), float(pt.y), float(pt.z)) for pt in pts]

    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    inf_dags = skin_fn.influenceObjects()
    inf_paths = [inf_dags[i].fullPathName() for i in range(len(inf_dags))]
    inf_count = len(inf_paths)

    single_comp = om2.MFnSingleIndexedComponent()
    all_vtx_comp = single_comp.create(om2.MFn.kMeshVertComponent)
    single_comp.addElements(list(range(num_verts)))
    flat_weights, _ = skin_fn.getWeights(shape_dag, all_vtx_comp)
    all_weights = [
        [float(flat_weights[v * inf_count + c]) for c in range(inf_count)]
        for v in range(num_verts)
    ]

    max_inf = 0
    try:
        if cmds.getAttr(skin_cluster + ".maintainMaxInfluences"):
            max_inf = int(cmds.getAttr(skin_cluster + ".maxInfluences"))
    except Exception:
        max_inf = 0

    return {
        "shape_dag": shape_dag,
        "mesh_fn": mesh_fn,
        "num_verts": num_verts,
        "vtx_positions": vtx_positions,
        "skin_fn": skin_fn,
        "inf_paths": inf_paths,
        "inf_count": inf_count,
        "all_weights": all_weights,
        "max_influences": max_inf,
    }


def _make_surface_sampler(mesh_fn, vtx_positions, sample_mode="auto", snap_tol=SNAP_TOL_DEFAULT):
    """OpenMaya MFnMesh.getClosestPoint を使った高速サーフェス補間サンプラーを生成する。"""
    poly_verts_cache = {}

    def _sampler(query_pt):
        try:
            _, poly_id = mesh_fn.getClosestPoint(
                om2.MPoint(query_pt[0], query_pt[1], query_pt[2]),
                om2.MSpace.kWorld,
            )
            poly_vids = poly_verts_cache.get(poly_id)
            if poly_vids is None:
                poly_vids = list(mesh_fn.getPolygonVertices(poly_id))
                poly_verts_cache[poly_id] = poly_vids
            return sample_polygon_barycentric_weights(
                query_pt,
                poly_vids,
                vtx_positions,
                sample_mode=sample_mode,
                snap_tol=snap_tol,
            )
        except Exception:
            return []

    return _sampler


def _blend_weights_from_terms(src_weights, terms, inf_count):
    """(頂点ID, 補間係数) リストから補間済みウェイトベクトルを合成する。"""
    if not terms:
        return None
    if len(terms) == 1 and abs(terms[0][1] - 1.0) < 1e-9:
        return list(src_weights[terms[0][0]])
    out = [0.0] * inf_count
    for vid, coeff in terms:
        sw = src_weights[vid]
        for idx in range(inf_count):
            w = sw[idx]
            if w > 0.0:
                out[idx] += w * coeff
    return out


def compute_precision_mirror_weights(
    vtx_positions,
    all_weights,
    full_mirror_idx_map,
    plane="YZ",
    center_val=0.0,
    dst_side_sign=-1.0,
    target_vtx_indices=None,
    filter_src_inf_indices=None,
    equalize_center=True,
    seam_tol=SEAM_TOL_DEFAULT,
    sample_fn=None,
    max_influences=0,
):
    """単一メッシュ上の高精度ミラーウェイト差分（頂点ID・新ウェイト・元ウェイト）を算出する。"""
    if not vtx_positions or not all_weights:
        return [], [], []

    axis_idx = PLANE_AXIS_MAP.get(plane, 0)
    inf_count = len(all_weights[0])
    num_verts = len(vtx_positions)

    if sample_fn is None:
        def sample_fn(q_pt):
            best_v = 0
            best_d2 = float("inf")
            for idx, vp in enumerate(vtx_positions):
                d2 = _dist_sq(q_pt, vp)
                if d2 < best_d2:
                    best_d2 = d2
                    best_v = idx
            return [(best_v, 1.0)]

    explicit_component_mode = target_vtx_indices is not None
    candidate_vids = list(target_vtx_indices) if explicit_component_mode else list(range(num_verts))
    src_filter_set = set(filter_src_inf_indices) if filter_src_inf_indices else None

    changed_vids = []
    new_list = []
    org_list = []

    for vid in candidate_vids:
        if vid < 0 or vid >= num_verts:
            continue
        pos = vtx_positions[vid]
        signed = (pos[axis_idx] - center_val) * dst_side_sign

        # コンポーネント明示指定がない場合はソース側の頂点を一切変更せず保護する
        if not explicit_component_mode and signed < -seam_tol:
            continue

        org_w = all_weights[vid]
        mir_pt = _mirror_point(pos, axis_idx, center_val)
        terms = sample_fn(mir_pt)
        sampled_src = _blend_weights_from_terms(all_weights, terms, inf_count)
        if sampled_src is None:
            continue

        # ボーン指定ミラー時はソースボーン群が影響している領域のみ転写対象にする
        if src_filter_set is not None:
            if not any(sampled_src[s_idx] > WEIGHT_EPS for s_idx in src_filter_set):
                continue

        if abs(signed) <= seam_tol and equalize_center:
            # 対称面ちょうど上の頂点は現在値とミラー値を左右均等平均する
            mir_w = [0.0] * inf_count
            for idx in range(inf_count):
                mapped = full_mirror_idx_map.get(idx, idx)
                w_org = org_w[idx]
                w_mir = sampled_src[idx]
                if mapped == idx:
                    mir_w[idx] += 0.5 * (w_org + w_mir)
                else:
                    if src_filter_set is None or idx in src_filter_set or mapped in src_filter_set:
                        avg = 0.5 * (w_org + sampled_src[mapped])
                        mir_w[idx] = avg
                        mir_w[mapped] = avg
                    else:
                        mir_w[idx] = w_org
        else:
            mir_w = [0.0] * inf_count
            for idx, w_val in enumerate(sampled_src):
                if w_val > 0.0:
                    mapped = full_mirror_idx_map.get(idx, idx)
                    mir_w[mapped] += w_val

        mir_w = _clean_and_normalize_weight_vector(mir_w, max_influences=max_influences)

        if any(abs(a - b) > 1e-8 for a, b in zip(mir_w, org_w)):
            changed_vids.append(vid)
            new_list.append(mir_w)
            org_list.append(list(org_w))

    return changed_vids, new_list, org_list


def compute_cross_mesh_copy_weights(
    src_positions,
    src_weights,
    src_inf_paths,
    dst_positions,
    dst_org_weights,
    dst_inf_paths,
    copy_mode="auto",
    mirror=False,
    plane="YZ",
    center_val=0.0,
    mirror_joint_map=None,
    dst_vtx_indices=None,
    sample_fn=None,
    max_influences=0,
):
    """別メッシュ間（通常コピーおよび左右反転ミラーコピー）の転写ウェイト差分を算出する。"""
    if not src_positions or not src_weights or not dst_positions or not dst_org_weights:
        return [], [], []

    axis_idx = PLANE_AXIS_MAP.get(plane, 0)
    src_inf_cnt = len(src_inf_paths)
    dst_inf_cnt = len(dst_inf_paths)
    dst_inf_index = {p: i for i, p in enumerate(dst_inf_paths)}

    # ソース側インフルエンス番号 -> ターゲット側インフルエンス番号の対応表を作成
    src_to_dst_inf = {}
    for s_idx, s_path in enumerate(src_inf_paths):
        mapped_path = (mirror_joint_map or {}).get(s_path, s_path) if mirror else s_path
        d_idx = dst_inf_index.get(mapped_path)
        if d_idx is None:
            # 短縮名フォールバック（別階層・ネームスペース違い対応）
            short_m = mapped_path.split("|")[-1].split(":")[-1]
            for dp, di in dst_inf_index.items():
                if dp.split("|")[-1].split(":")[-1] == short_m:
                    d_idx = di
                    break
        if d_idx is not None:
            src_to_dst_inf[s_idx] = d_idx

    if not src_to_dst_inf:
        return [], [], []

    use_index_mode = False
    if copy_mode == "index" and not mirror:
        use_index_mode = (len(src_positions) == len(dst_positions))
    elif copy_mode == "auto" and not mirror and len(src_positions) == len(dst_positions):
        # 頂点数が一致しかつサンプル頂点座標がほぼ一致する場合のみインデックス転送を採用する
        step = max(1, len(src_positions) // 32)
        max_d2 = 0.0
        for i in range(0, len(src_positions), step):
            d2 = _dist_sq(src_positions[i], dst_positions[i])
            if d2 > max_d2:
                max_d2 = d2
        use_index_mode = (max_d2 <= (SNAP_TOL_DEFAULT * SNAP_TOL_DEFAULT))

    if not use_index_mode and sample_fn is None:
        def sample_fn(q_pt):
            best_v = 0
            best_d2 = float("inf")
            for idx, vp in enumerate(src_positions):
                d2 = _dist_sq(q_pt, vp)
                if d2 < best_d2:
                    best_d2 = d2
                    best_v = idx
            return [(best_v, 1.0)]

    target_vids = list(dst_vtx_indices) if dst_vtx_indices is not None else list(range(len(dst_positions)))

    changed_vids = []
    new_list = []
    org_list = []

    for vid in target_vids:
        if vid < 0 or vid >= len(dst_positions):
            continue
        org_w = dst_org_weights[vid]
        if use_index_mode:
            sampled_src = src_weights[vid]
        else:
            q_pt = _mirror_point(dst_positions[vid], axis_idx, center_val) if mirror else dst_positions[vid]
            terms = sample_fn(q_pt)
            sampled_src = _blend_weights_from_terms(src_weights, terms, src_inf_cnt)
            if sampled_src is None:
                continue

        new_w = [0.0] * dst_inf_cnt
        for s_idx, w_val in enumerate(sampled_src):
            if w_val > 0.0:
                d_idx = src_to_dst_inf.get(s_idx)
                if d_idx is not None:
                    new_w[d_idx] += w_val

        if sum(new_w) <= 1e-12:
            continue
        new_w = _clean_and_normalize_weight_vector(new_w, max_influences=max_influences)

        if any(abs(a - b) > 1e-8 for a, b in zip(new_w, org_w)):
            changed_vids.append(vid)
            new_list.append(new_w)
            org_list.append(list(org_w))

    return changed_vids, new_list, org_list


def _collect_selection_components_by_mesh():
    """現在の Maya 選択からメッシュごとの選択頂点ID一覧辞書を返す。"""
    slist = om2.MGlobal.getActiveSelectionList()
    it = om2.MItSelectionList(slist)
    mesh_vtx_map = defaultdict(set)

    while not it.isDone():
        try:
            dag, comp = it.getComponent()
        except Exception:
            it.next()
            continue
        if comp.isNull():
            it.next()
            continue

        shape_dag = om2.MDagPath(dag)
        if not shape_dag.node().hasFn(om2.MFn.kMesh):
            shape_dag = rig_cleanup_helper._get_visible_mesh_shape_dag_for_bind(dag.fullPathName())
            if shape_dag is None:
                it.next()
                continue

        parents = cmds.listRelatives(shape_dag.fullPathName(), parent=True, fullPath=True) or []
        mesh_xform = parents[0] if parents else shape_dag.fullPathName()

        vids = []
        try:
            if comp.hasFn(om2.MFn.kMeshVertComponent):
                vids = list(om2.MFnSingleIndexedComponent(comp).getElements())
            elif comp.hasFn(om2.MFn.kMeshEdgeComponent):
                edge_it = om2.MItMeshEdge(shape_dag, comp)
                while not edge_it.isDone():
                    vids.append(int(edge_it.vertexId(0)))
                    vids.append(int(edge_it.vertexId(1)))
                    edge_it.next()
            elif comp.hasFn(om2.MFn.kMeshPolygonComponent):
                poly_it = om2.MItMeshPolygon(shape_dag, comp)
                while not poly_it.isDone():
                    for v in poly_it.getVertices():
                        vids.append(int(v))
                    poly_it.next()
        except Exception:
            vids = []

        if vids:
            mesh_vtx_map[mesh_xform].update(int(v) for v in vids)
        it.next()

    return {m: sorted(vs) for m, vs in mesh_vtx_map.items() if vs}


def _get_selected_polygon_transforms():
    """現在の選択からポリゴンメッシュのトランスフォーム一覧を選択順で返す。"""
    sel = cmds.ls(sl=True, long=True) or []
    meshes = []
    for item in sel:
        if "." in item:
            continue
        if not cmds.objExists(item):
            continue
        ntype = cmds.nodeType(item)
        if ntype == "mesh":
            parents = cmds.listRelatives(item, parent=True, fullPath=True) or []
            if parents and parents[0] not in meshes:
                meshes.append(parents[0])
        elif ntype == "transform":
            shapes = cmds.listRelatives(item, shapes=True, noIntermediate=True, type="mesh", fullPath=True) or []
            if shapes and item not in meshes:
                meshes.append(item)
    return meshes


def _ensure_influences_on_skin(skin_cluster, required_joints, unlock_added=True):
    """スキンクラスタへ未登録のジョイントを wt=0 で安全に追加し、追加した一覧を返す。"""
    existing = set(cmds.ls(cmds.skinCluster(skin_cluster, q=True, inf=True) or [], long=True) or [])
    missing = [
        j for j in required_joints
        if j not in existing and cmds.objExists(j) and cmds.nodeType(j) == "joint"
    ]
    if missing:
        cmds.skinCluster(
            skin_cluster,
            edit=True,
            addInfluence=missing,
            lockWeights=True,
            useGeometry=False,
            weight=0.0,
            polySmoothness=0.0,
        )
        if unlock_added:
            for j in missing:
                attr = j + ".liw"
                if cmds.objExists(attr):
                    try:
                        if cmds.getAttr(attr, settable=True):
                            cmds.setAttr(attr, False)
                    except Exception:
                        pass
    return missing


def _compute_mirror_center_from_pairs(pairs, axis_idx):
    """左右ペアのワールド座標から対称面の中心値を算出する（原点付近は 0.0 へスナップ）。"""
    mids = []
    for j1, j2 in pairs:
        if cmds.objExists(j1) and cmds.objExists(j2):
            try:
                p1 = cmds.xform(j1, q=True, ws=True, t=True)
                p2 = cmds.xform(j2, q=True, ws=True, t=True)
                mids.append((float(p1[axis_idx]) + float(p2[axis_idx])) * 0.5)
            except Exception:
                pass
    if not mids:
        return 0.0
    avg_c = sum(mids) / float(len(mids))
    return 0.0 if abs(avg_c) < 1e-2 else avg_c


def _decide_bone_mirror_direction(sel_joints, mirror_map, mesh_data_list, direction, axis_idx, center_val):
    """ボーン選択時のミラー元ボーン群・ミラー先ボーン群・ターゲット側符号を自動決定する。"""
    # 選択ボーンと反対側ボーンのペアを抽出する
    sel_set = set(sel_joints)
    paired_sel = []
    paired_opp = []
    for j in sel_joints:
        opp = mirror_map.get(j)
        if opp and opp != j:
            paired_sel.append(j)
            paired_opp.append(opp)
    if not paired_sel:
        return [], [], -1.0

    def _avg_coord(j_list):
        vals = []
        for j in j_list:
            if cmds.objExists(j):
                try:
                    vals.append(float(cmds.xform(j, q=True, ws=True, t=True)[axis_idx]))
                except Exception:
                    pass
        return (sum(vals) / float(len(vals))) if vals else 0.0

    sel_avg = _avg_coord(paired_sel)
    opp_avg = _avg_coord(paired_opp)

    if direction == "pos_to_neg":
        if sel_avg >= opp_avg:
            return paired_sel, paired_opp, -1.0
        return paired_opp, paired_sel, -1.0
    if direction == "neg_to_pos":
        if sel_avg <= opp_avg:
            return paired_sel, paired_opp, 1.0
        return paired_opp, paired_sel, 1.0

    # 自動モード：ウェイトが多く入っている側をソース、少ない側をターゲットにする
    sel_w_sum = 0.0
    opp_w_sum = 0.0
    opp_set = set(paired_opp)
    for mdata in mesh_data_list:
        inf_paths = mdata["inf_paths"]
        sel_ids = [i for i, p in enumerate(inf_paths) if p in sel_set]
        opp_ids = [i for i, p in enumerate(inf_paths) if p in opp_set]
        for w_row in mdata["all_weights"]:
            for i in sel_ids:
                sel_w_sum += w_row[i]
            for i in opp_ids:
                opp_w_sum += w_row[i]

    if opp_w_sum > sel_w_sum * 1.05:
        src_joints, dst_joints = paired_opp, paired_sel
        dst_sign = 1.0 if (sel_avg - center_val) >= 0.0 else -1.0
    else:
        src_joints, dst_joints = paired_sel, paired_opp
        dst_sign = 1.0 if (opp_avg - center_val) >= 0.0 else -1.0
    return src_joints, dst_joints, dst_sign


def execute_precision_weight_mirror(
    direction="auto",
    plane="YZ",
    sample_mode="auto",
    include_children=True,
    auto_add_opposite=True,
    equalize_center=True,
    use_position_fallback=True,
    dry_run=False,
):
    """ボーン・メッシュ・頂点の現在選択に応じて安全かつ高精度なスキンウェイトミラーを実行（または診断）する。"""
    axis_idx = PLANE_AXIS_MAP.get(plane, 0)
    sel_joints = rig_cleanup_helper.collect_target_joints(include_children=include_children)
    sel_meshes = _get_selected_polygon_transforms()
    comp_vtx_map = _collect_selection_components_by_mesh()
    orig_sel = cmds.ls(sl=True, long=True) or []

    # 2 メッシュ以上が選択されている場合は別メッシュ間のミラーコピーとして処理する
    if not sel_joints and not comp_vtx_map and len(sel_meshes) >= 2:
        if dry_run:
            src_sc = rig_cleanup_helper._get_skin_cluster_for_mesh_bind(sel_meshes[0])
            if not src_sc:
                raise RuntimeError("コピー元メッシュにスキンクラスタが見つかりません: {0}".format(sel_meshes[0].split("|")[-1]))
            src_infs = cmds.ls(cmds.skinCluster(src_sc, q=True, inf=True) or [], long=True) or []
            full_skel = _collect_full_skeleton_joints_from_seeds(src_infs)
            _, info = build_hybrid_mirror_joint_map(
                full_skel, plane=plane, use_position_fallback=use_position_fallback
            )
            return {
                "mode": "multi_mesh_mirror",
                "meshes": list(sel_meshes),
                "mirrored_vertices": 0,
                "added_influences": [],
                "name_pairs": info["name_pairs"],
                "pos_pairs": info["pos_pairs"],
                "unmatched_sided": info["unmatched_sided"],
                "dry_run": True,
            }
        copy_res = execute_safe_weight_copy(
            src_mesh=sel_meshes[0],
            dst_meshes=sel_meshes[1:],
            copy_mode=sample_mode,
            mirror=True,
            plane=plane,
            auto_bind=auto_add_opposite,
            use_position_fallback=use_position_fallback,
        )
        return {
            "mode": "multi_mesh_mirror",
            "meshes": [sel_meshes[0]] + list(sel_meshes[1:]),
            "mirrored_vertices": copy_res["copied_vertices"],
            "added_influences": copy_res["added_influences"],
            "name_pairs": copy_res.get("name_pairs", []),
            "pos_pairs": copy_res.get("pos_pairs", []),
            "unmatched_sided": copy_res.get("unmatched_sided", []),
            "dry_run": False,
        }

    # 対象モードと対象メッシュを解決する
    if sel_joints:
        target_mode = "bone_mirror"
        target_meshes = list(sel_meshes) if sel_meshes else rig_cleanup_helper.find_meshes_sharing_skeleton(sel_joints)
    elif comp_vtx_map:
        target_mode = "component_mirror"
        target_meshes = list(comp_vtx_map.keys())
    elif sel_meshes:
        target_mode = "single_mesh_mirror"
        target_meshes = list(sel_meshes)
    else:
        raise RuntimeError("ミラー対象のボーン・メッシュ・または頂点を選択してください。")

    skinned_meshes = []
    for m in target_meshes:
        sc = rig_cleanup_helper._get_skin_cluster_for_mesh_bind(m)
        if sc:
            skinned_meshes.append((m, sc))
    if not skinned_meshes:
        raise RuntimeError("対象にスキニング済みメッシュが見つかりませんでした。")

    result = {
        "mode": target_mode,
        "meshes": [m for m, _ in skinned_meshes],
        "mirrored_vertices": 0,
        "added_influences": [],
        "name_pairs": [],
        "pos_pairs": [],
        "unmatched_sided": [],
        "source_bones": [],
        "target_bones": [],
        "dry_run": dry_run,
    }

    try:
        # まず全対象メッシュのスケルトン共通ミラーマップを構築する
        all_seed_infs = list(sel_joints)
        for _, sc in skinned_meshes:
            infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], long=True) or []
            all_seed_infs.extend(infs)
        full_skel = _collect_full_skeleton_joints_from_seeds(all_seed_infs)
        mirror_map, pair_info = build_hybrid_mirror_joint_map(
            full_skel,
            plane=plane,
            center_val=0.0,
            use_position_fallback=use_position_fallback,
        )
        all_pairs = pair_info["name_pairs"] + pair_info["pos_pairs"]
        center_val = _compute_mirror_center_from_pairs(all_pairs, axis_idx)
        if abs(center_val) > 1e-3:
            mirror_map, pair_info = build_hybrid_mirror_joint_map(
                full_skel,
                plane=plane,
                center_val=center_val,
                use_position_fallback=use_position_fallback,
            )

        result["name_pairs"] = pair_info["name_pairs"]
        result["pos_pairs"] = pair_info["pos_pairs"]
        result["unmatched_sided"] = pair_info["unmatched_sided"]

        # ボーン選択時はソースボーン群とターゲットボーン群・方向を決定する
        src_bones = []
        dst_bones = []
        if target_mode == "bone_mirror":
            pre_data = [_read_mesh_geometry_and_weights(m, sc) for m, sc in skinned_meshes]
            src_bones, dst_bones, dst_side_sign = _decide_bone_mirror_direction(
                sel_joints, mirror_map, pre_data, direction, axis_idx, center_val
            )
            if not src_bones or not dst_bones:
                raise RuntimeError("選択ジョイントに対応する反対側ボーンが見つかりませんでした。")
            result["source_bones"] = src_bones
            result["target_bones"] = dst_bones
        else:
            dst_side_sign = 1.0 if direction == "neg_to_pos" else -1.0

        for mesh_node, sc in skinned_meshes:
            # 必要な反対側ボーンがスキンクラスタに未登録なら安全に wt=0 で追加する
            cur_infs = cmds.ls(cmds.skinCluster(sc, q=True, inf=True) or [], long=True) or []
            if target_mode == "bone_mirror":
                needed_infs = list(dst_bones) + list(src_bones)
            else:
                needed_infs = [mirror_map.get(j, j) for j in cur_infs]

            if dry_run:
                missing_preview = [j for j in needed_infs if j not in set(cur_infs)]
                for mj in missing_preview:
                    if mj not in result["added_influences"]:
                        result["added_influences"].append(mj)
                continue

            if auto_add_opposite:
                added = _ensure_influences_on_skin(sc, needed_infs, unlock_added=True)
                for mj in added:
                    if mj not in result["added_influences"]:
                        result["added_influences"].append(mj)

            mdata = _read_mesh_geometry_and_weights(mesh_node, sc)
            inf_paths = mdata["inf_paths"]
            inf_index_map = {p: idx for idx, p in enumerate(inf_paths)}

            full_mirror_idx_map = {}
            for idx, jp in enumerate(inf_paths):
                opp = mirror_map.get(jp, jp)
                full_mirror_idx_map[idx] = inf_index_map.get(opp, idx)

            filter_src_ids = None
            if target_mode == "bone_mirror":
                filter_src_ids = {inf_index_map[j] for j in src_bones if j in inf_index_map}
                if not filter_src_ids:
                    continue

            target_vids = comp_vtx_map.get(mesh_node) if target_mode == "component_mirror" else None
            sampler = _make_surface_sampler(
                mdata["mesh_fn"],
                mdata["vtx_positions"],
                sample_mode=sample_mode,
            )

            changed_vids, new_list, org_list = compute_precision_mirror_weights(
                vtx_positions=mdata["vtx_positions"],
                all_weights=mdata["all_weights"],
                full_mirror_idx_map=full_mirror_idx_map,
                plane=plane,
                center_val=center_val,
                dst_side_sign=dst_side_sign,
                target_vtx_indices=target_vids,
                filter_src_inf_indices=filter_src_ids,
                equalize_center=equalize_center,
                sample_fn=sampler,
                max_influences=mdata["max_influences"],
            )

            if changed_vids:
                rig_cleanup_helper._apply_vertex_weights_undoable(
                    mesh_node,
                    mdata["skin_fn"],
                    changed_vids,
                    new_list,
                    org_list,
                    mdata["inf_count"],
                )
                result["mirrored_vertices"] += len(changed_vids)
    finally:
        if orig_sel:
            existing = [n for n in orig_sel if cmds.objExists(n)]
            if existing:
                try:
                    cmds.select(existing, replace=True)
                except Exception:
                    pass

    return result


def execute_safe_weight_copy(
    src_mesh,
    dst_meshes=None,
    dst_vtx_map=None,
    copy_mode="auto",
    mirror=False,
    plane="YZ",
    auto_bind=True,
    use_position_fallback=True,
):
    """ソースメッシュからターゲットメッシュ（または選択頂点群）へ安全・高精度にウェイトをコピーする。"""
    if not src_mesh or not cmds.objExists(src_mesh):
        raise RuntimeError("コピー元メッシュが見つかりません。")
    src_mesh = (cmds.ls(src_mesh, long=True) or [src_mesh])[0]
    src_sc = rig_cleanup_helper._get_skin_cluster_for_mesh_bind(src_mesh)
    if not src_sc:
        raise RuntimeError("コピー元メッシュにスキンクラスタがありません: {0}".format(src_mesh.split("|")[-1]))

    target_meshes = []
    seen = set()
    for m in (dst_meshes or []):
        if cmds.objExists(m):
            lm = (cmds.ls(m, long=True) or [m])[0]
            if lm != src_mesh and lm not in seen:
                seen.add(lm)
                target_meshes.append(lm)
    if dst_vtx_map:
        for m in dst_vtx_map.keys():
            if cmds.objExists(m):
                lm = (cmds.ls(m, long=True) or [m])[0]
                if lm not in seen:
                    seen.add(lm)
                    target_meshes.append(lm)

    if not target_meshes:
        raise RuntimeError("コピー先のメッシュまたは頂点を選択してください。")

    orig_sel = cmds.ls(sl=True, long=True) or []
    src_data = _read_mesh_geometry_and_weights(src_mesh, src_sc)
    src_infs = src_data["inf_paths"]

    mirror_joint_map = None
    center_val = 0.0
    pair_info = {"name_pairs": [], "pos_pairs": [], "unmatched_sided": []}
    if mirror:
        axis_idx = PLANE_AXIS_MAP.get(plane, 0)
        full_skel = _collect_full_skeleton_joints_from_seeds(src_infs)
        mirror_joint_map, pair_info = build_hybrid_mirror_joint_map(
            full_skel,
            plane=plane,
            center_val=0.0,
            use_position_fallback=use_position_fallback,
        )
        all_pairs = pair_info["name_pairs"] + pair_info["pos_pairs"]
        center_val = _compute_mirror_center_from_pairs(all_pairs, axis_idx)
        if abs(center_val) > 1e-3:
            mirror_joint_map, pair_info = build_hybrid_mirror_joint_map(
                full_skel,
                plane=plane,
                center_val=center_val,
                use_position_fallback=use_position_fallback,
            )

    required_infs = [
        (mirror_joint_map.get(j, j) if mirror else j) for j in src_infs
    ]
    required_infs = list(dict.fromkeys(j for j in required_infs if cmds.objExists(j)))

    src_sampler = _make_surface_sampler(
        src_data["mesh_fn"],
        src_data["vtx_positions"],
        sample_mode="surface" if copy_mode == "surface" else ("nearest" if copy_mode == "nearest" else "auto"),
    )

    result = {
        "src_mesh": src_mesh,
        "dst_meshes": list(target_meshes),
        "copied_vertices": 0,
        "newly_bound_meshes": [],
        "added_influences": [],
        "mirror": mirror,
        "name_pairs": pair_info["name_pairs"],
        "pos_pairs": pair_info["pos_pairs"],
        "unmatched_sided": pair_info["unmatched_sided"],
    }

    try:
        for dst_mesh in target_meshes:
            dst_sc = rig_cleanup_helper._get_skin_cluster_for_mesh_bind(dst_mesh)
            if not dst_sc:
                if not auto_bind:
                    raise RuntimeError("コピー先メッシュがバインドされていません: {0}".format(dst_mesh.split("|")[-1]))
                max_inf = 4
                obey_max = False
                skin_method = 0
                try:
                    max_inf = int(cmds.getAttr(src_sc + ".maxInfluences"))
                    obey_max = bool(cmds.getAttr(src_sc + ".maintainMaxInfluences"))
                    skin_method = int(cmds.getAttr(src_sc + ".skinningMethod"))
                except Exception:
                    pass
                dst_sc = cmds.skinCluster(
                    required_infs,
                    dst_mesh,
                    toSelectedBones=True,
                    normalizeWeights=1,
                    maximumInfluences=max(1, max_inf),
                    obeyMaxInfluences=obey_max,
                    skinMethod=skin_method,
                )[0]
                result["newly_bound_meshes"].append(dst_mesh)
            elif auto_bind:
                added = _ensure_influences_on_skin(dst_sc, required_infs, unlock_added=True)
                for mj in added:
                    if mj not in result["added_influences"]:
                        result["added_influences"].append(mj)

            dst_data = _read_mesh_geometry_and_weights(dst_mesh, dst_sc)
            vtx_subset = dst_vtx_map.get(dst_mesh) if dst_vtx_map else None

            changed_vids, new_list, org_list = compute_cross_mesh_copy_weights(
                src_positions=src_data["vtx_positions"],
                src_weights=src_data["all_weights"],
                src_inf_paths=src_infs,
                dst_positions=dst_data["vtx_positions"],
                dst_org_weights=dst_data["all_weights"],
                dst_inf_paths=dst_data["inf_paths"],
                copy_mode=copy_mode,
                mirror=mirror,
                plane=plane,
                center_val=center_val,
                mirror_joint_map=mirror_joint_map,
                dst_vtx_indices=vtx_subset,
                sample_fn=src_sampler,
                max_influences=dst_data["max_influences"],
            )

            if changed_vids:
                rig_cleanup_helper._apply_vertex_weights_undoable(
                    dst_mesh,
                    dst_data["skin_fn"],
                    changed_vids,
                    new_list,
                    org_list,
                    dst_data["inf_count"],
                )
                result["copied_vertices"] += len(changed_vids)
    finally:
        if orig_sel:
            existing = [n for n in orig_sel if cmds.objExists(n)]
            if existing:
                try:
                    cmds.select(existing, replace=True)
                except Exception:
                    pass

    return result
