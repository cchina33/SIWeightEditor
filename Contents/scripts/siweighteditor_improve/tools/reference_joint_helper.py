# -*- coding: utf-8 -*-
"""リファレンスジョイントによる Local Rotation Axis・構造上書き、リネーム調整 UI、ウェイト再適用モジュール。"""

import datetime as dt
import json
import math
import os
import re

from maya import cmds
import maya.api.OpenMaya as om2
import maya.api.OpenMayaAnim as oma2
import maya.api.OpenMayaRender as omr

from .. import common
from .. import lang
from .. import qt
from ..qt import *
from . import bone_picker_window
from . import rig_cleanup_helper


REF_SESSION_SUBDIR = "_ref_joint_sessions"
WEIGHT_EPSILON = 1e-6

# 接頭辞として除去して照合する一般的なリグプレフィックス群
COMMON_PREFIXES = (
    "mixamorig",
    "hik",
    "bip001",
    "bip01",
    "valvebiped",
    "valve_biped",
    "character1",
    "def",
    "jnt",
    "bn",
    "sk",
    "rig",
    "ctrl",
    "drv",
)

# HumanIK / 一般的リグ間の標準部位エイリアス辞書（正規化トークン -> 共通部位キー）
HUMANOID_ALIAS_MAP = {
    "hips": "hips",
    "pelvis": "hips",
    "cog": "hips",
    "root": "root",
    "spine": "spine",
    "spine1": "spine1",
    "spine2": "spine2",
    "spine3": "spine3",
    "chest": "spine2",
    "upperchest": "spine3",
    "neck": "neck",
    "neck1": "neck1",
    "head": "head",
    "shoulder": "clavicle",
    "clavicle": "clavicle",
    "collar": "clavicle",
    "arm": "upperarm",
    "upperarm": "upperarm",
    "uparm": "upperarm",
    "forearm": "forearm",
    "lowerarm": "forearm",
    "lowarm": "forearm",
    "elbow": "forearm",
    "hand": "hand",
    "wrist": "hand",
    "upleg": "thigh",
    "upperleg": "thigh",
    "thigh": "thigh",
    "leg": "shin",
    "lowerleg": "shin",
    "lowleg": "shin",
    "shin": "shin",
    "calf": "shin",
    "knee": "shin",
    "foot": "foot",
    "ankle": "foot",
    "toebase": "toe",
    "toe": "toe",
    "ball": "toe",
    "handthumb1": "thumb1",
    "handthumb2": "thumb2",
    "handthumb3": "thumb3",
    "thumb1": "thumb1",
    "thumb2": "thumb2",
    "thumb3": "thumb3",
    "handindex1": "index1",
    "handindex2": "index2",
    "handindex3": "index3",
    "index1": "index1",
    "index2": "index2",
    "index3": "index3",
    "handmiddle1": "middle1",
    "handmiddle2": "middle2",
    "handmiddle3": "middle3",
    "middle1": "middle1",
    "middle2": "middle2",
    "middle3": "middle3",
    "handring1": "ring1",
    "handring2": "ring2",
    "handring3": "ring3",
    "ring1": "ring1",
    "ring2": "ring2",
    "ring3": "ring3",
    "handpinky1": "pinky1",
    "handpinky2": "pinky2",
    "handpinky3": "pinky3",
    "pinky1": "pinky1",
    "pinky2": "pinky2",
    "pinky3": "pinky3",
}


def get_session_storage_dir(base_storage_dir):
    """リファレンスジョイント上書きセッション用 JSON 保存ディレクトリを返す。"""
    path = os.path.join(base_storage_dir, REF_SESSION_SUBDIR)
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def collect_skeleton_hierarchy(seed_joints):
    """指定ジョイントからルートを辿り、階層全体（親→子順）のジョイントフルパス一覧を返す。"""
    roots = []
    seen_roots = set()
    for jnt in (seed_joints or []):
        if not cmds.objExists(jnt) or cmds.nodeType(jnt) != "joint":
            continue
        found = cmds.ls(jnt, type="joint", long=True) or [jnt]
        cur = found[0]
        while True:
            parents = cmds.listRelatives(cur, parent=True, type="joint", fullPath=True) or []
            if not parents:
                break
            cur = parents[0]
        if cur not in seen_roots:
            seen_roots.add(cur)
            roots.append(cur)

    ordered = []
    seen = set()
    for root in roots:
        if root not in seen:
            seen.add(root)
            ordered.append(root)
        desc = cmds.listRelatives(root, allDescendents=True, type="joint", fullPath=True) or []
        for child in desc:
            if child not in seen:
                seen.add(child)
                ordered.append(child)

    ordered.sort(key=lambda p: (p.count("|"), p))
    return ordered, roots


def collect_joint_subtree(selected_joints):
    """選択ジョイント自身とその配下すべて（親→子順）を収集して返す。"""
    ordered = []
    seen = set()
    roots = []
    for jnt in (selected_joints or []):
        if not cmds.objExists(jnt) or cmds.nodeType(jnt) != "joint":
            continue
        found = cmds.ls(jnt, type="joint", long=True) or [jnt]
        full = found[0]
        if full not in seen:
            seen.add(full)
            ordered.append(full)
            roots.append(full)
        desc = cmds.listRelatives(full, allDescendents=True, type="joint", fullPath=True) or []
        for child in desc:
            if child not in seen:
                seen.add(child)
                ordered.append(child)
    ordered.sort(key=lambda p: (p.count("|"), p))
    return ordered, roots


def collect_selected_joints_only(selected_joints):
    """選択されているジョイントのみを重複除去し、親→子順で返す（一部ジョイント専用）。"""
    ordered = []
    seen = set()
    for jnt in (selected_joints or []):
        if not cmds.objExists(jnt) or cmds.nodeType(jnt) != "joint":
            continue
        found = cmds.ls(jnt, type="joint", long=True) or [jnt]
        full = found[0]
        if full not in seen:
            seen.add(full)
            ordered.append(full)
    ordered.sort(key=lambda p: (p.count("|"), p))
    # 選択集合内で上位に位置するジョイント群を代表ルートとして返す
    roots = []
    for p in ordered:
        has_selected_ancestor = any(
            other != p and p.startswith(other + "|") for other in ordered
        )
        if not has_selected_ancestor:
            roots.append(p)
    return ordered, roots


def snapshot_skeleton_info(joint_paths):
    """ジョイント階層の位置・純粋回転行列・Joint Orient・階層・ラベル情報を JSON 用リストで返す。"""
    records = []
    for jnt in (joint_paths or []):
        if not cmds.objExists(jnt):
            continue
        try:
            parents = cmds.listRelatives(jnt, parent=True, type="joint", fullPath=True) or []
            parent_full = parents[0] if parents else ""
            ws_pos = cmds.xform(jnt, q=True, ws=True, t=True)
            rot_mat = rig_cleanup_helper._get_orthonormal_rotation_matrix(jnt)
            mat_vals = [float(rot_mat[i]) for i in range(16)]
            rot = cmds.getAttr(jnt + ".rotate")[0]
            jo = cmds.getAttr(jnt + ".jointOrient")[0] if cmds.attributeQuery("jointOrient", node=jnt, exists=True) else (0.0, 0.0, 0.0)
            ra = cmds.getAttr(jnt + ".rotateAxis")[0] if cmds.attributeQuery("rotateAxis", node=jnt, exists=True) else (0.0, 0.0, 0.0)
            scl = cmds.getAttr(jnt + ".scale")[0]
            rot_order = int(cmds.getAttr(jnt + ".rotateOrder"))
            radius = float(cmds.getAttr(jnt + ".radius")) if cmds.attributeQuery("radius", node=jnt, exists=True) else 1.0
            side_attr = int(cmds.getAttr(jnt + ".side")) if cmds.attributeQuery("side", node=jnt, exists=True) else 0
            type_attr = int(cmds.getAttr(jnt + ".type")) if cmds.attributeQuery("type", node=jnt, exists=True) else 0
            other_type = str(cmds.getAttr(jnt + ".otherType") or "") if cmds.attributeQuery("otherType", node=jnt, exists=True) else ""

            records.append({
                "full_path": jnt,
                "short_name": jnt.split("|")[-1],
                "parent_full": parent_full,
                "parent_short": parent_full.split("|")[-1] if parent_full else "",
                "depth": jnt.count("|"),
                "ws_pos": [float(ws_pos[0]), float(ws_pos[1]), float(ws_pos[2])],
                "ws_rot_mat": mat_vals,
                "jointOrient": [float(jo[0]), float(jo[1]), float(jo[2])],
                "rotate": [float(rot[0]), float(rot[1]), float(rot[2])],
                "rotateAxis": [float(ra[0]), float(ra[1]), float(ra[2])],
                "scale": [float(scl[0]), float(scl[1]), float(scl[2])],
                "rotateOrder": rot_order,
                "radius": radius,
                "label_side": side_attr,
                "label_type": type_attr,
                "label_otherType": other_type,
            })
        except Exception:
            continue
    return records


def parse_joint_prefix(short_name):
    """ジョイント名からキャラ名プレフィックスとベース名、およびパターン種別を返す。"""
    raw = (short_name or "").split("|")[-1].split(":")[-1]
    if not raw:
        return "", "", "no_prefix"
    if raw.startswith("_"):
        return "", raw.lstrip("_"), "leading_underscore"
    if "_" in raw:
        pfx, rest = raw.split("_", 1)
        if pfx and rest:
            return pfx, rest, "named_prefix"
    return "", raw, "no_prefix"


def detect_skeleton_prefix_info(records):
    """スケルトンレコード群からルート（または Hips）基準のキャラ名プレフィックス情報を判定する。"""
    if not records:
        return {"prefix": "", "pattern_type": "no_prefix", "root_short": "", "root_base": ""}

    # Hips 相当のジョイントを優先し、無ければ先頭（ルート）ジョイントを基準にする
    anchor = records[0]
    for rec in records:
        raw = (rec.get("short_name") or "").split(":")[-1]
        low = raw.lower()
        if low == "hips" or low.endswith("_hips"):
            anchor = rec
            break

    root_short = (anchor.get("short_name") or "").split("|")[-1].split(":")[-1]
    pfx, base, ptype = parse_joint_prefix(root_short)
    return {
        "prefix": pfx,
        "pattern_type": ptype,
        "root_short": root_short,
        "root_base": base or root_short.lstrip("_"),
    }


def strip_known_prefix(short_name, detected_prefix=""):
    """検出されたキャラ名プレフィックス（または先頭アンダースコア）を取り除いたベース名を返す。"""
    raw = (short_name or "").split("|")[-1].split(":")[-1]
    if detected_prefix:
        head = detected_prefix + "_"
        if raw.startswith(head) and len(raw) > len(head):
            return raw[len(head):]
    if raw.startswith("_"):
        return raw.lstrip("_")
    return raw


def build_prefixed_name(base_name, target_prefix=""):
    """ベース名に指定キャラ名プレフィックスを付与したボーン名を返す（空ならプレフィックスなし）。"""
    clean_base = (base_name or "").split("|")[-1].split(":")[-1].lstrip("_")
    clean_pfx = (target_prefix or "").strip().rstrip("_")
    if not clean_base:
        return clean_pfx or "joint"
    if clean_pfx:
        return "{0}_{1}".format(clean_pfx, clean_base)
    return clean_base


def apply_prefix_to_records(ref_records, target_prefix=""):
    """リファレンスレコード群の各ボーンへ指定キャラ名プレフィックスを反映した新リストを返す。"""
    info = detect_skeleton_prefix_info(ref_records)
    src_prefix = info["prefix"]
    updated = []
    for rec in (ref_records or []):
        item = dict(rec)
        base = item.get("base_name") or strip_known_prefix(item.get("short_name", ""), src_prefix)
        item["base_name"] = base
        item["prefixed_name"] = build_prefixed_name(base, target_prefix)
        updated.append(item)
    return updated


def save_reference_session_json(base_storage_dir, current_records, ref_records, target_meshes, mesh_backup_paths=None):
    """現在のジョイント情報・リファレンスジョイント情報（[キャラ名]_Hips 構造付き）・対象メッシュ情報を JSON へ保存する。"""
    session_dir = get_session_storage_dir(base_storage_dir)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    prefix_info = detect_skeleton_prefix_info(ref_records)
    src_pfx = prefix_info["prefix"]
    enriched_refs = []
    for rec in (ref_records or []):
        item = dict(rec)
        base = item.get("base_name") or strip_known_prefix(item.get("short_name", ""), src_pfx)
        item["base_name"] = base
        item["template_name"] = "[char]_{0}".format(base) if base else item.get("short_name", "")
        enriched_refs.append(item)

    if ref_records:
        name_seed = ref_records[0]["short_name"]
    elif current_records:
        name_seed = current_records[0]["short_name"]
    else:
        name_seed = "skeleton"
    safe_root = re.sub(r'[\\/:*?"<>|]+', "_", name_seed).strip("_") or "skeleton"
    filename = "ref_joint_{0}_{1}.json".format(safe_root, stamp)
    save_path = os.path.join(session_dir, filename)

    payload = {
        "version": 2,
        "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reference_prefix": prefix_info["prefix"],
        "reference_prefix_type": prefix_info["pattern_type"],
        "reference_root_short": prefix_info["root_short"],
        "reference_root_base": prefix_info["root_base"],
        "current_joint_count": len(current_records or []),
        "reference_joint_count": len(enriched_refs),
        "target_meshes": list(target_meshes or []),
        "mesh_backup_paths": list(mesh_backup_paths or []),
        "current_joints": list(current_records or []),
        "reference_joints": enriched_refs,
    }
    with open(save_path, "w") as fp:
        json.dump(payload, fp, indent=2)
    return save_path, payload


def load_reference_session_json(json_path):
    """保存済みのリファレンスジョイント JSON を読み込み、他シーンでも再利用可能な辞書を返す。"""
    with open(json_path, "r") as fp:
        payload = json.load(fp)
    ref_joints = payload.get("reference_joints") or []
    if not ref_joints:
        raise RuntimeError("No reference_joints found in JSON file.")
    return payload


def create_skeleton_from_reference_records(ref_records, target_prefix=""):
    """現在ジョイントが無いシーンへ、リファレンス情報と指定キャラ名プレフィックスからボーン一式を新規作成する。"""
    if not ref_records:
        raise RuntimeError("No reference joint records to create.")

    prefixed_recs = apply_prefix_to_records(ref_records, target_prefix=target_prefix)
    # 親→子順（階層深さ順）で安全に生成する
    prefixed_recs.sort(key=lambda r: (int(r.get("depth", r.get("full_path", "").count("|"))), r.get("full_path", "")))

    ref_full_to_created = {}
    created_paths = []
    created_roots = []

    for rec in prefixed_recs:
        rfp = rec.get("full_path", "")
        r_parent = rec.get("parent_full", "")
        desired_name = rec.get("prefixed_name") or rec.get("short_name") or "joint"

        cmds.select(clear=True)
        new_jnt = cmds.joint(name=desired_name)
        new_long = (cmds.ls(new_jnt, long=True) or [new_jnt])[0]

        parent_live = ref_full_to_created.get(r_parent)
        if parent_live and cmds.objExists(parent_live):
            parented = cmds.parent(new_long, parent_live)
            if parented:
                new_long = (cmds.ls(parented[0], long=True) or [new_long])[0]
        else:
            created_roots.append(new_long)

        ws_pos = rec.get("ws_pos")
        if ws_pos and len(ws_pos) == 3:
            cmds.xform(new_long, ws=True, t=ws_pos)

        try:
            cmds.setAttr(new_long + ".rotateOrder", int(rec.get("rotateOrder", 0)))
        except Exception:
            pass
        try:
            cmds.setAttr(new_long + ".radius", float(rec.get("radius", 1.0)))
        except Exception:
            pass
        try:
            if "label_side" in rec:
                cmds.setAttr(new_long + ".side", int(rec["label_side"]))
            if "label_type" in rec:
                cmds.setAttr(new_long + ".type", int(rec["label_type"]))
            if "label_otherType" in rec and rec["label_otherType"]:
                cmds.setAttr(new_long + ".otherType", str(rec["label_otherType"]), type="string")
        except Exception:
            pass

        ws_rot_vals = rec.get("ws_rot_mat")
        if ws_rot_vals and len(ws_rot_vals) == 16:
            rig_cleanup_helper._set_joint_world_rot_to_orient(new_long, om2.MMatrix(ws_rot_vals))

        ref_full_to_created[rfp] = new_long
        created_paths.append(new_long)

    if created_roots:
        try:
            cmds.select(created_roots, replace=True)
        except Exception:
            pass

    return {
        "created": created_paths,
        "roots": created_roots,
        "prefix": (target_prefix or "").strip().rstrip("_"),
    }


def normalize_joint_token(short_name):
    """比較用にネームスペース・共通プレフィックス・左右表記を取り除いた正規化キーとサイドを返す。"""
    base = short_name.split("|")[-1].split(":")[-1]
    side = bone_picker_window.detect_bone_side(base)

    # 左右ワードを除去して部位名コアを抽出する
    cleaned = re.sub(
        r"(?:^|[_\.\-])(?:Left|Right|LEFT|RIGHT|left|right|L|R|l|r)(?=[_\.\-]|[A-Z0-9]|$)",
        "_",
        base,
    )
    cleaned = re.sub(r"(?<=[a-z0-9])(?:Left|Right)(?=[A-Z0-9_.]|$)", "", cleaned)
    cleaned = re.sub(r"(?<=[a-z])(?:L|R)(?=[0-9]*$)", "", cleaned)

    parts = [p for p in re.split(r"[_\.\-\s]+", cleaned) if p]
    while parts and parts[0].lower() in COMMON_PREFIXES:
        parts.pop(0)
    while parts and parts[-1].lower() in ("jnt", "joint", "bn", "bone", "end"):
        parts.pop()

    core = "".join(parts).lower()
    alias_core = HUMANOID_ALIAS_MAP.get(core, core)
    return alias_core, side


def _calc_skeleton_bbox_diag(records):
    """ジョイント群の 3D バウンディングボックス対角長を返す。"""
    if not records:
        return 1.0
    xs = [r["ws_pos"][0] for r in records]
    ys = [r["ws_pos"][1] for r in records]
    zs = [r["ws_pos"][2] for r in records]
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    dz = max(zs) - min(zs)
    diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    return max(diag, 1.0)


def _dist_3d(p1, p2):
    return math.sqrt(
        (p1[0] - p2[0]) ** 2
        + (p1[1] - p2[1]) ** 2
        + (p1[2] - p2[2]) ** 2
    )


def build_auto_mapping(current_records, ref_records):
    """現在ジョイントとリファレンスジョイントを名前・エイリアス・近傍座標の順で自動照合する。"""
    ref_by_short_lower = {}
    ref_by_alias = {}
    for rec in ref_records:
        sn_low = rec["short_name"].split(":")[-1].lower()
        ref_by_short_lower.setdefault(sn_low, []).append(rec)
        alias_key = normalize_joint_token(rec["short_name"])
        if alias_key[0]:
            ref_by_alias.setdefault(alias_key, []).append(rec)

    matched_ref_paths = set()
    cur_to_ref = {}
    cur_reason = {}

    # 段階照合：ショートネーム一致
    for cur in current_records:
        sn_low = cur["short_name"].split(":")[-1].lower()
        cands = ref_by_short_lower.get(sn_low) or []
        for cand in cands:
            if cand["full_path"] not in matched_ref_paths:
                cur_to_ref[cur["full_path"]] = cand["full_path"]
                cur_reason[cur["full_path"]] = "exact"
                matched_ref_paths.add(cand["full_path"])
                break

    # 段階照合：HumanIK / 標準部位エイリアス＋左右一致
    for cur in current_records:
        if cur["full_path"] in cur_to_ref:
            continue
        alias_key = normalize_joint_token(cur["short_name"])
        if not alias_key[0]:
            continue
        cands = [c for c in (ref_by_alias.get(alias_key) or []) if c["full_path"] not in matched_ref_paths]
        if cands:
            cands.sort(key=lambda c: _dist_3d(cur["ws_pos"], c["ws_pos"]))
            best = cands[0]
            cur_to_ref[cur["full_path"]] = best["full_path"]
            cur_reason[cur["full_path"]] = "alias"
            matched_ref_paths.add(best["full_path"])

    # 段階照合：3D ワールド座標の近傍一致（同一サイド優先）
    diag = max(_calc_skeleton_bbox_diag(current_records), _calc_skeleton_bbox_diag(ref_records))
    max_dist = diag * 0.12
    unmatched_cur = [c for c in current_records if c["full_path"] not in cur_to_ref]
    unmatched_ref = [r for r in ref_records if r["full_path"] not in matched_ref_paths]

    pair_scores = []
    for cur in unmatched_cur:
        c_side = bone_picker_window.detect_bone_side(cur["short_name"])
        for ref in unmatched_ref:
            r_side = bone_picker_window.detect_bone_side(ref["short_name"])
            if c_side != r_side and c_side != "C" and r_side != "C":
                continue
            d = _dist_3d(cur["ws_pos"], ref["ws_pos"])
            if d <= max_dist:
                pair_scores.append((d, cur["full_path"], ref["full_path"]))
    pair_scores.sort(key=lambda item: item[0])

    for _, cur_fp, ref_fp in pair_scores:
        if cur_fp in cur_to_ref or ref_fp in matched_ref_paths:
            continue
        cur_to_ref[cur_fp] = ref_fp
        cur_reason[cur_fp] = "nearest"
        matched_ref_paths.add(ref_fp)

    # 逆引き辞書（リファレンス -> 現在ジョイント）を構築
    ref_to_cur = {rfp: cfp for cfp, rfp in cur_to_ref.items()}
    ref_lookup = {r["full_path"]: r for r in ref_records}

    current_rows = []
    has_diff = False
    for cur in current_records:
        cfp = cur["full_path"]
        rfp = cur_to_ref.get(cfp)
        reason = cur_reason.get(cfp, "none")
        ref_short = ref_lookup[rfp]["short_name"] if rfp in ref_lookup else ""
        if not rfp or cur["short_name"] != ref_short:
            has_diff = True
        current_rows.append({
            "cur_full": cfp,
            "cur_short": cur["short_name"],
            "cur_parent": cur["parent_full"],
            "ref_full": rfp or "",
            "ref_short": ref_short,
            "match_reason": reason,
            "action": "overwrite" if rfp else "keep",
            "new_name": cur["short_name"],
            "merge_target": cur["parent_full"],
        })

    extra_ref_rows = []
    for ref in ref_records:
        rfp = ref["full_path"]
        if rfp in matched_ref_paths:
            continue
        has_diff = True
        # リファレンス側の親に対応する現在ジョイントを挿入先候補にする
        attach_parent = ""
        p_fp = ref["parent_full"]
        while p_fp:
            if p_fp in ref_to_cur:
                attach_parent = ref_to_cur[p_fp]
                break
            p_rec = ref_lookup.get(p_fp)
            p_fp = p_rec["parent_full"] if p_rec else ""
        if not attach_parent and current_records:
            attach_parent = current_records[0]["full_path"]

        extra_ref_rows.append({
            "ref_full": rfp,
            "ref_short": ref["short_name"],
            "ref_parent": ref["parent_full"],
            "action": "skip",
            "new_name": ref["short_name"].split(":")[-1],
            "attach_parent": attach_parent,
        })

    return {
        "current_rows": current_rows,
        "extra_ref_rows": extra_ref_rows,
        "has_diff": has_diff,
    }


def resolve_weight_merge_map(current_rows):
    """削除対象ジョイントのウェイトを最も近い残存親（または指定先）へ解決する辞書を返す。"""
    row_by_path = {r["cur_full"]: r for r in current_rows}
    deleted_set = {r["cur_full"] for r in current_rows if r["action"] == "delete_merge"}
    surviving_paths = [r["cur_full"] for r in current_rows if r["cur_full"] not in deleted_set]
    merge_map = {}

    for row in current_rows:
        cfp = row["cur_full"]
        if cfp not in deleted_set:
            continue
        target = row.get("merge_target") or row.get("cur_parent") or ""
        visited = {cfp}
        while target and target in deleted_set and target not in visited:
            visited.add(target)
            t_row = row_by_path.get(target)
            if not t_row:
                target = ""
                break
            target = t_row.get("merge_target") or t_row.get("cur_parent") or ""
        # 循環指定などで削除対象のまま残った場合は親階層から残存ジョイントを探索する
        if target in deleted_set or not target:
            p = row.get("cur_parent") or ""
            p_visited = {cfp}
            while p and p in deleted_set and p not in p_visited:
                p_visited.add(p)
                p_row = row_by_path.get(p)
                p = p_row.get("cur_parent") if p_row else ""
            if p and p not in deleted_set:
                target = p
            else:
                target = surviving_paths[0] if surviving_paths else ""
        merge_map[cfp] = target
    return merge_map


class ReferenceJointMappingDialog(QDialog):
    """ジョイント対応・リネーム・構造追加／削除およびウェイト移譲先を調整するダイアログ。"""

    def __init__(self, current_records, ref_records, mapping_data, parent=None):
        super(ReferenceJointMappingDialog, self).__init__(parent)
        self.setWindowTitle(
            lang.Lang(
                en="Reference Joint Mapping / Rename & Structure Editor",
                ja=u"リファレンスジョイント対応・リネーム＆構造調整",
            ).output()
        )
        self.resize(980, 660)

        self.current_records = current_records
        self.ref_records = ref_records
        self.mapping_data = mapping_data
        self._cur_widgets = []
        self._extra_widgets = []

        self._init_ui()
        self._populate_tables()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        info_label = QLabel(
            lang.Lang(
                en="Adjust joint pairing, target names, and structure changes (Add / Delete & Merge Weight) before overwriting Local Rotation Axis.",
                ja=u"リファレンスジョイントとの対応関係・リネーム・構造の追加／削除（削除ボーンのウェイト親統合）を調整してから上書きを実行できます。",
            ).output()
        )
        info_label.setWordWrap(True)
        qt.change_button_color(info_label, textColor=[220, 225, 232], bgColor=[48, 52, 60])
        info_label.setMargin(6)
        layout.addWidget(info_label)

        # 一括リネーム＆クイック設定バー
        tool_row = QHBoxLayout()
        tool_row.setSpacing(6)

        self.use_ref_names_but = QPushButton(
            lang.Lang(en="Use Reference Names", ja=u"リファレンス名に揃える").output()
        )
        self.use_ref_names_but.clicked.connect(self._apply_reference_names)
        tool_row.addWidget(self.use_ref_names_but)

        self.keep_cur_names_but = QPushButton(
            lang.Lang(en="Keep Current Names", ja=u"現在の名前を維持").output()
        )
        self.keep_cur_names_but.clicked.connect(self._restore_current_names)
        tool_row.addWidget(self.keep_cur_names_but)

        self.del_unmatched_but = QPushButton(
            lang.Lang(en="Mark Unmatched -> Delete & Merge", ja=u"未対応を削除＆ウェイト統合").output()
        )
        self.del_unmatched_but.clicked.connect(self._mark_unmatched_delete)
        tool_row.addWidget(self.del_unmatched_but)

        tool_row.addStretch(1)

        tool_row.addWidget(QLabel(lang.Lang(en="Replace:", ja=u"一括置換:").output()))
        self.find_edit = QLineEdit(self)
        self.find_edit.setPlaceholderText(lang.Lang(en="Find", ja=u"検索").output())
        self.find_edit.setMaximumWidth(110)
        tool_row.addWidget(self.find_edit)

        self.repl_edit = QLineEdit(self)
        self.repl_edit.setPlaceholderText(lang.Lang(en="Replace", ja=u"置換後").output())
        self.repl_edit.setMaximumWidth(110)
        tool_row.addWidget(self.repl_edit)

        self.bulk_repl_but = QPushButton(lang.Lang(en="Apply", ja=u"置換実行").output())
        self.bulk_repl_but.clicked.connect(self._bulk_replace_names)
        tool_row.addWidget(self.bulk_repl_but)
        layout.addLayout(tool_row)

        # 上下スプリッタ（上：現在ジョイント対応＆削除 / 下：リファレンスのみの追加候補）
        splitter = QSplitter(Qt.Vertical, self)

        # 上テーブル：現在ジョイント
        cur_box = QGroupBox(
            lang.Lang(
                en="Current Joints (Overwrite Axis / Keep / Delete & Merge Weight)",
                ja=u"現在のジョイント一覧（軸上書き / 維持 / 削除してウェイト統合）",
            ).output()
        )
        cur_layout = QVBoxLayout(cur_box)
        cur_layout.setContentsMargins(6, 6, 6, 6)

        self.cur_table = QTableWidget(0, 6, self)
        self.cur_table.setHorizontalHeaderLabels([
            lang.Lang(en="Current Joint", ja=u"現在のジョイント").output(),
            lang.Lang(en="Match", ja=u"照合").output(),
            lang.Lang(en="Reference Joint", ja=u"リファレンス対応先").output(),
            lang.Lang(en="Action", ja=u"アクション").output(),
            lang.Lang(en="New Name (Rename)", ja=u"適用後の名前").output(),
            lang.Lang(en="Weight Merge Target (if Deleted)", ja=u"削除時ウェイト移譲先").output(),
        ])
        self.cur_table.verticalHeader().setVisible(False)
        self.cur_table.verticalHeader().setDefaultSectionSize(24)
        self.cur_table.setAlternatingRowColors(True)
        c_header = self.cur_table.horizontalHeader()
        c_resize = getattr(c_header, "setSectionResizeMode", None) or getattr(c_header, "setResizeMode")
        c_resize(0, QHeaderView.Stretch)
        c_resize(1, QHeaderView.ResizeToContents)
        c_resize(2, QHeaderView.Stretch)
        c_resize(3, QHeaderView.ResizeToContents)
        c_resize(4, QHeaderView.Stretch)
        c_resize(5, QHeaderView.Stretch)
        cur_layout.addWidget(self.cur_table)
        splitter.addWidget(cur_box)

        # 下テーブル：リファレンス側のみのジョイント（追加候補）
        extra_box = QGroupBox(
            lang.Lang(
                en="Reference-Only Joints (Add Missing Structure Joints)",
                ja=u"リファレンス側のみに存在するジョイント（構造追加の調整）",
            ).output()
        )
        extra_layout = QVBoxLayout(extra_box)
        extra_layout.setContentsMargins(6, 6, 6, 6)

        extra_bar = QHBoxLayout()
        self.add_all_but = QPushButton(lang.Lang(en="Set All -> Add Joint", ja=u"すべて追加に設定").output())
        self.add_all_but.clicked.connect(lambda: self._set_all_extra_action("add"))
        extra_bar.addWidget(self.add_all_but)

        self.skip_all_but = QPushButton(lang.Lang(en="Set All -> Skip", ja=u"すべてスキップに設定").output())
        self.skip_all_but.clicked.connect(lambda: self._set_all_extra_action("skip"))
        extra_bar.addWidget(self.skip_all_but)
        extra_bar.addStretch(1)
        extra_layout.addLayout(extra_bar)

        self.extra_table = QTableWidget(0, 4, self)
        self.extra_table.setHorizontalHeaderLabels([
            lang.Lang(en="Reference Joint", ja=u"リファレンスジョイント").output(),
            lang.Lang(en="Action", ja=u"アクション").output(),
            lang.Lang(en="New Joint Name", ja=u"追加時の名前").output(),
            lang.Lang(en="Insert Under Parent", ja=u"挿入先の親ジョイント").output(),
        ])
        self.extra_table.verticalHeader().setVisible(False)
        self.extra_table.verticalHeader().setDefaultSectionSize(24)
        self.extra_table.setAlternatingRowColors(True)
        e_header = self.extra_table.horizontalHeader()
        e_resize = getattr(e_header, "setSectionResizeMode", None) or getattr(e_header, "setResizeMode")
        e_resize(0, QHeaderView.Stretch)
        e_resize(1, QHeaderView.ResizeToContents)
        e_resize(2, QHeaderView.Stretch)
        e_resize(3, QHeaderView.Stretch)
        extra_layout.addWidget(self.extra_table)
        splitter.addWidget(extra_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # 下部 OK / Cancel ボタン
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.ok_but = QPushButton(
            lang.Lang(en="Apply & Continue", ja=u"この設定で決定").output()
        )
        self.ok_but.setMinimumWidth(140)
        self.ok_but.setMinimumHeight(28)
        self.ok_but.clicked.connect(self.accept)
        btn_row.addWidget(self.ok_but)

        self.cancel_but = QPushButton(lang.Lang(en="Cancel", ja=u"キャンセル").output())
        self.cancel_but.setMinimumWidth(90)
        self.cancel_but.setMinimumHeight(28)
        self.cancel_but.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_but)
        layout.addLayout(btn_row)

    def _populate_tables(self):
        cur_rows = self.mapping_data.get("current_rows") or []
        extra_rows = self.mapping_data.get("extra_ref_rows") or []

        ref_options = [("", lang.Lang(en="(None)", ja=u"（なし）").output())] + [
            (r["full_path"], r["short_name"]) for r in self.ref_records
        ]
        cur_parent_options = [("", lang.Lang(en="(World / Parent)", ja=u"（ワールド / 自動親）").output())]
        seen_parent_opt = {""}
        for c in self.current_records:
            if c["full_path"] not in seen_parent_opt:
                seen_parent_opt.add(c["full_path"])
                cur_parent_options.append((c["full_path"], c["short_name"]))
        # 一部ジョイントのみ登録した場合でも、直上の親ジョイントを移譲先・挿入先に選べるよう候補へ追加する
        parent_suffix = lang.Lang(en=" [Parent]", ja=u" [親]").output()
        for c in self.current_records:
            p_fp = c.get("parent_full") or ""
            if p_fp and p_fp not in seen_parent_opt:
                seen_parent_opt.add(p_fp)
                p_short = c.get("parent_short") or p_fp.split("|")[-1]
                cur_parent_options.append((p_fp, p_short + parent_suffix))
        reason_labels = {
            "exact": lang.Lang(en="Exact", ja=u"一致").output(),
            "alias": lang.Lang(en="Alias", ja=u"部位一致").output(),
            "nearest": lang.Lang(en="Nearest", ja=u"近傍").output(),
            "none": lang.Lang(en="Unmatched", ja=u"未対応").output(),
        }

        self._cur_widgets = []
        self.cur_table.setRowCount(len(cur_rows))
        for idx, row in enumerate(cur_rows):
            cur_item = QTableWidgetItem(row["cur_short"])
            cur_item.setToolTip(row["cur_full"])
            cur_item.setFlags(cur_item.flags() & ~Qt.ItemIsEditable)
            self.cur_table.setItem(idx, 0, cur_item)

            reason = row.get("match_reason", "none")
            r_item = QTableWidgetItem(reason_labels.get(reason, reason))
            r_item.setFlags(r_item.flags() & ~Qt.ItemIsEditable)
            if reason == "exact":
                r_item.setForeground(QColor(145, 230, 155))
            elif reason in ("alias", "nearest"):
                r_item.setForeground(QColor(255, 215, 110))
            else:
                r_item.setForeground(QColor(255, 130, 120))
            self.cur_table.setItem(idx, 1, r_item)

            ref_combo = QComboBox(self)
            sel_ref_idx = 0
            for opt_i, (rfp, rlabel) in enumerate(ref_options):
                ref_combo.addItem(rlabel, rfp)
                if rfp == row.get("ref_full", ""):
                    sel_ref_idx = opt_i
            ref_combo.setCurrentIndex(sel_ref_idx)
            self.cur_table.setCellWidget(idx, 2, ref_combo)

            act_combo = QComboBox(self)
            act_combo.addItem(lang.Lang(en="Overwrite Axis", ja=u"上書き").output(), "overwrite")
            act_combo.addItem(lang.Lang(en="Keep As-Is", ja=u"維持").output(), "keep")
            act_combo.addItem(lang.Lang(en="Delete & Merge Wt", ja=u"削除＆ウェイト統合").output(), "delete_merge")
            cur_act = row.get("action", "keep")
            for a_i in range(act_combo.count()):
                if act_combo.itemData(a_i) == cur_act:
                    act_combo.setCurrentIndex(a_i)
                    break
            self.cur_table.setCellWidget(idx, 3, act_combo)

            name_edit = QLineEdit(row.get("new_name", row["cur_short"]), self)
            self.cur_table.setCellWidget(idx, 4, name_edit)

            merge_combo = QComboBox(self)
            sel_m_idx = 0
            for m_i, (cfp, clabel) in enumerate(cur_parent_options):
                if cfp == row["cur_full"]:
                    continue
                merge_combo.addItem(clabel, cfp)
                if cfp == row.get("merge_target", ""):
                    sel_m_idx = merge_combo.count() - 1
            merge_combo.setCurrentIndex(sel_m_idx)
            merge_combo.setEnabled(cur_act == "delete_merge")
            self.cur_table.setCellWidget(idx, 5, merge_combo)

            # リファレンス変更やアクション変更時の連動
            def _make_ref_handler(a_cmb, r_cmb):
                def _on_ref_changed(_):
                    if r_cmb.currentData():
                        if a_cmb.currentData() == "keep":
                            a_cmb.setCurrentIndex(0)
                    elif a_cmb.currentData() == "overwrite":
                        a_cmb.setCurrentIndex(1)
                return _on_ref_changed

            def _make_act_handler(a_cmb, m_cmb, n_ed):
                def _on_act_changed(_):
                    is_del = (a_cmb.currentData() == "delete_merge")
                    m_cmb.setEnabled(is_del)
                    n_ed.setEnabled(not is_del)
                return _on_act_changed

            ref_combo.currentIndexChanged.connect(_make_ref_handler(act_combo, ref_combo))
            act_combo.currentIndexChanged.connect(_make_act_handler(act_combo, merge_combo, name_edit))

            self._cur_widgets.append((row, ref_combo, act_combo, name_edit, merge_combo))

        self._extra_widgets = []
        self.extra_table.setRowCount(len(extra_rows))
        for idx, row in enumerate(extra_rows):
            ref_item = QTableWidgetItem(row["ref_short"])
            ref_item.setToolTip(row["ref_full"])
            ref_item.setFlags(ref_item.flags() & ~Qt.ItemIsEditable)
            self.extra_table.setItem(idx, 0, ref_item)

            act_combo = QComboBox(self)
            act_combo.addItem(lang.Lang(en="Add Joint", ja=u"ジョイント追加").output(), "add")
            act_combo.addItem(lang.Lang(en="Skip", ja=u"スキップ").output(), "skip")
            act_combo.setCurrentIndex(0 if row.get("action") == "add" else 1)
            self.extra_table.setCellWidget(idx, 1, act_combo)

            name_edit = QLineEdit(row.get("new_name", row["ref_short"]), self)
            self.extra_table.setCellWidget(idx, 2, name_edit)

            parent_combo = QComboBox(self)
            sel_p_idx = 0
            for cfp, clabel in cur_parent_options:
                parent_combo.addItem(clabel, cfp)
                if cfp == row.get("attach_parent", ""):
                    sel_p_idx = parent_combo.count() - 1
            parent_combo.setCurrentIndex(sel_p_idx)
            self.extra_table.setCellWidget(idx, 3, parent_combo)

            self._extra_widgets.append((row, act_combo, name_edit, parent_combo))

    def _apply_reference_names(self):
        for _, ref_combo, act_combo, name_edit, _ in self._cur_widgets:
            rfp = ref_combo.currentData()
            if rfp and act_combo.currentData() != "delete_merge":
                short_name = rfp.split("|")[-1].split(":")[-1]
                name_edit.setText(short_name)

    def _restore_current_names(self):
        for row, _, _, name_edit, _ in self._cur_widgets:
            name_edit.setText(row["cur_short"])

    def _mark_unmatched_delete(self):
        for _, ref_combo, act_combo, _, _ in self._cur_widgets:
            if not ref_combo.currentData():
                for a_i in range(act_combo.count()):
                    if act_combo.itemData(a_i) == "delete_merge":
                        act_combo.setCurrentIndex(a_i)
                        break

    def _bulk_replace_names(self):
        find_txt = self.find_edit.text()
        repl_txt = self.repl_edit.text()
        if not find_txt:
            return
        for _, _, _, name_edit, _ in self._cur_widgets:
            name_edit.setText(name_edit.text().replace(find_txt, repl_txt))
        for _, _, name_edit, _ in self._extra_widgets:
            name_edit.setText(name_edit.text().replace(find_txt, repl_txt))

    def _set_all_extra_action(self, action_key):
        target_idx = 0 if action_key == "add" else 1
        for _, act_combo, _, _ in self._extra_widgets:
            act_combo.setCurrentIndex(target_idx)

    def get_result_mapping(self):
        """UI で調整された最新のマッピング辞書を返す。"""
        out_cur = []
        for row, ref_combo, act_combo, name_edit, merge_combo in self._cur_widgets:
            updated = dict(row)
            rfp = ref_combo.currentData() or ""
            updated["ref_full"] = rfp
            updated["ref_short"] = rfp.split("|")[-1] if rfp else ""
            updated["action"] = act_combo.currentData() or "keep"
            cleaned_name = name_edit.text().strip() or row["cur_short"]
            updated["new_name"] = cleaned_name
            updated["merge_target"] = merge_combo.currentData() or ""
            out_cur.append(updated)

        out_extra = []
        for row, act_combo, name_edit, parent_combo in self._extra_widgets:
            updated = dict(row)
            updated["action"] = act_combo.currentData() or "skip"
            updated["new_name"] = name_edit.text().strip() or row["ref_short"]
            updated["attach_parent"] = parent_combo.currentData() or ""
            out_extra.append(updated)

        return {
            "current_rows": out_cur,
            "extra_ref_rows": out_extra,
            "has_diff": self.mapping_data.get("has_diff", False),
        }


def _rebind_and_apply_remapped_weights(
    mesh_long,
    payload,
    old_to_final_joint_map,
    extra_bind_joints=None,
    smooth_new_bones=False,
    clean_orphaned_bind_pose=True,
):
    """構造変更・リネーム・削除移譲を反映してスキンクラスタを再バインドし、ウェイトを綺麗に復元する。"""
    from . import model_edit_window

    if not cmds.objExists(mesh_long):
        raise RuntimeError("Target mesh does not exist: {0}".format(mesh_long))

    model_edit_window._detach_and_freeze_for_modeling(mesh_long)

    shape_dag = model_edit_window._get_visible_mesh_dag(mesh_long)
    if shape_dag is None:
        raise RuntimeError("No visible mesh shape found: {0}".format(mesh_long))

    mesh_fn = om2.MFnMesh(shape_dag)
    vtx_count = mesh_fn.numVertices
    if vtx_count <= 0:
        raise RuntimeError("Mesh has zero vertices: {0}".format(mesh_long))

    saved_infs = payload.get("influences") or []
    bind_joints = []
    seen_bind = set()
    old_idx_to_bind_joint = {}

    for old_idx, saved_inf in enumerate(saved_infs):
        mapped_target = old_to_final_joint_map.get(saved_inf)
        if not mapped_target:
            # 名前だけ残っている場合のフォールバック解決
            if cmds.objExists(saved_inf):
                found = cmds.ls(saved_inf, type="joint", long=True) or []
                mapped_target = found[0] if found else None
            else:
                found = cmds.ls(saved_inf.split("|")[-1], type="joint", long=True) or []
                mapped_target = found[0] if found else None
        if mapped_target and cmds.objExists(mapped_target):
            full_target = (cmds.ls(mapped_target, type="joint", long=True) or [mapped_target])[0]
            old_idx_to_bind_joint[old_idx] = full_target
            if full_target not in seen_bind:
                seen_bind.add(full_target)
                bind_joints.append(full_target)

    for extra_j in (extra_bind_joints or []):
        if extra_j and cmds.objExists(extra_j):
            full_extra = (cmds.ls(extra_j, type="joint", long=True) or [extra_j])[0]
            if full_extra not in seen_bind:
                seen_bind.add(full_extra)
                bind_joints.append(full_extra)

    if not bind_joints:
        raise RuntimeError("No valid target joints remain for rebinding: {0}".format(mesh_long))

    if clean_orphaned_bind_pose:
        rig_cleanup_helper.cleanup_orphaned_bind_poses(bind_joints)

    skin_params = payload.get("skin_params") or {}
    maintain_max = bool(skin_params.get("maintainMaxInfluences", False))
    max_infs = int(skin_params.get("maxInfluences", 4))
    dropoff_rate = float(skin_params.get("dropoffRate", 4.0))
    skinning_method = int(skin_params.get("skinningMethod", 0))
    normalize_weights = int(skin_params.get("normalizeWeights", 1))

    dummy = common.TemporaryReparent().main(mode="create")
    try:
        common.TemporaryReparent().main(mesh_long, dummyParent=dummy, mode="cut")
        skin_clusters = cmds.skinCluster(
            bind_joints,
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

    # 再バインド後の実インフルエンス番号へ対応付ける
    skin_sel = om2.MGlobal.getSelectionListByName(skin_cluster)
    skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
    new_inf_dags = skin_fn.influenceObjects()
    new_inf_count = len(new_inf_dags)
    new_inf_index_map = {new_inf_dags[i].fullPathName(): i for i in range(new_inf_count)}

    old_to_new_inf_idx = {}
    for old_idx, target_full in old_idx_to_bind_joint.items():
        if target_full in new_inf_index_map:
            old_to_new_inf_idx[old_idx] = new_inf_index_map[target_full]

    orig_vtx_count = int(payload.get("vertex_count", 0))
    if orig_vtx_count == vtx_count:
        orig_weights = payload["weights"]
        restored_per_vtx = []
        for vid in range(vtx_count):
            merged = {}
            for old_inf_idx, w in orig_weights[vid]:
                new_idx = old_to_new_inf_idx.get(old_inf_idx)
                if new_idx is not None:
                    merged[new_idx] = merged.get(new_idx, 0.0) + w
            restored_per_vtx.append(
                model_edit_window._normalize_and_limit_weights(merged, maintain_max, max_infs)
            )
    else:
        new_mesh_fn = om2.MFnMesh(shape_dag)
        new_points = new_mesh_fn.getPoints(om2.MSpace.kWorld)
        restored_per_vtx, _ = model_edit_window._compute_restored_weights_by_surface(
            new_points, payload, old_to_new_inf_idx, maintain_max, max_infs
        )

    shape_dag = model_edit_window._get_visible_mesh_dag(mesh_long)
    inf_indices = om2.MIntArray(list(range(new_inf_count)))
    comp_fn = om2.MFnSingleIndexedComponent()
    vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
    comp_fn.setCompleteData(vtx_count)

    flat_weights = om2.MDoubleArray(vtx_count * new_inf_count, 0.0)
    for vid, w_dict in enumerate(restored_per_vtx):
        base = vid * new_inf_count
        if not w_dict:
            flat_weights[base] = 1.0
        else:
            for inf_idx, w_val in w_dict.items():
                flat_weights[base + inf_idx] = w_val

    skin_fn.setWeights(shape_dag, vtx_comp, inf_indices, flat_weights, False)

    try:
        omr.MRenderer.setGeometryDrawDirty(shape_dag.node())
        cmds.dgdirty(shape_dag.fullPathName())
    except Exception:
        pass

    return {
        "skin_cluster": skin_cluster,
        "mesh": mesh_long,
        "influence_count": new_inf_count,
    }


def execute_reference_joint_overwrite(
    current_records,
    ref_records,
    mapping_data,
    target_meshes,
    base_storage_dir,
    overwrite_orient=True,
    overwrite_position=False,
    overwrite_rotate_order=True,
    apply_rename=True,
    include_added_in_skin=True,
):
    """JSON 退避 → バインド解除 → 構造追加/削除 → Local Rotation Axis 上書き → リネーム → ウェイト再適用を実行する。"""
    from . import model_edit_window

    current_rows = mapping_data.get("current_rows") or []
    extra_ref_rows = mapping_data.get("extra_ref_rows") or []
    ref_rec_by_path = {r["full_path"]: r for r in ref_records}

    # リファレンス側の最新ワールド姿勢を事前にスナップショットしておく
    ref_live_pose = {}
    for rec in ref_records:
        rfp = rec["full_path"]
        if cmds.objExists(rfp):
            ref_live_pose[rfp] = {
                "ws_pos": list(cmds.xform(rfp, q=True, ws=True, t=True)),
                "ws_rot_mat": rig_cleanup_helper._get_orthonormal_rotation_matrix(rfp),
                "rotateOrder": int(cmds.getAttr(rfp + ".rotateOrder")),
                "radius": float(cmds.getAttr(rfp + ".radius")) if cmds.attributeQuery("radius", node=rfp, exists=True) else 1.0,
            }
        else:
            ref_live_pose[rfp] = {
                "ws_pos": list(rec["ws_pos"]),
                "ws_rot_mat": om2.MMatrix(rec["ws_rot_mat"]),
                "rotateOrder": int(rec.get("rotateOrder", 0)),
                "radius": float(rec.get("radius", 1.0)),
            }

    # 対象メッシュのウェイトを JSON へバックアップしてからバインド解除する
    mesh_backups = []
    backup_paths = []
    for mesh_long in (target_meshes or []):
        if not cmds.objExists(mesh_long):
            continue
        if model_edit_window._get_skin_cluster(mesh_long):
            save_path, payload = model_edit_window.export_mesh_skin_json(mesh_long)
            mesh_backups.append((mesh_long, save_path, payload))
            backup_paths.append(save_path)

    # セッション全体の JSON（現在ジョイント＋リファレンスジョイント＋バックアップ参照）を保存
    session_json_path, _ = save_reference_session_json(
        base_storage_dir,
        current_records,
        ref_records,
        target_meshes,
        mesh_backup_paths=backup_paths,
    )

    for mesh_long, _, _ in mesh_backups:
        model_edit_window._detach_and_freeze_for_modeling(mesh_long)

    # 操作中にフルパスが変化しても追跡できるよう、登録ジョイント・直上親・全バインドジョイントを Maya UUID で管理する
    orig_path_to_uuid = {}

    def _register_uuid(node_path):
        if not node_path or node_path in orig_path_to_uuid:
            return
        if cmds.objExists(node_path):
            uuids = cmds.ls(node_path, uuid=True) or []
            if uuids:
                orig_path_to_uuid[node_path] = uuids[0]

    for rec in current_records:
        _register_uuid(rec["full_path"])
        _register_uuid(rec.get("parent_full") or "")
    for row in current_rows:
        _register_uuid(row.get("merge_target") or "")
    for row in extra_ref_rows:
        _register_uuid(row.get("attach_parent") or "")
    for _, _, payload in mesh_backups:
        for inf_path in (payload.get("influences") or []):
            _register_uuid(inf_path)

    def _uuid_to_path(uid):
        if not uid:
            return None
        found = cmds.ls(uid, long=True) or []
        return found[0] if found else None

    # 削除対象ジョイントのウェイト移譲先を事前解決しておく
    merge_map_orig = resolve_weight_merge_map(current_rows)

    # 削除対象ジョイントを子→親の順で処理し、残存子ジョイントを親へ付け替えてから削除する
    deleted_count = 0
    delete_rows = [r for r in current_rows if r["action"] == "delete_merge"]
    delete_rows.sort(key=lambda r: r["cur_full"].count("|"), reverse=True)
    for row in delete_rows:
        uid = orig_path_to_uuid.get(row["cur_full"])
        cur_path = _uuid_to_path(uid)
        if not cur_path or not cmds.objExists(cur_path):
            continue
        # 付け替え先の親ジョイントを決定
        parent_orig = merge_map_orig.get(row["cur_full"]) or row.get("cur_parent") or ""
        parent_live = _uuid_to_path(orig_path_to_uuid.get(parent_orig))

        direct_children = cmds.listRelatives(cur_path, children=True, type="transform", fullPath=True) or []
        for child in direct_children:
            try:
                if parent_live and cmds.objExists(parent_live):
                    cmds.parent(child, parent_live)
                else:
                    cmds.parent(child, world=True)
            except Exception:
                pass
        try:
            cmds.delete(cur_path)
            deleted_count += 1
        except Exception:
            pass

    # リファレンス側のみの追加ジョイント（action == "add"）を親→子順で生成する
    added_uuids = []
    added_ref_to_uuid = {}
    add_rows = [r for r in extra_ref_rows if r["action"] == "add"]
    add_rows.sort(key=lambda r: r["ref_full"].count("|"))
    for row in add_rows:
        rfp = row["ref_full"]
        pose = ref_live_pose.get(rfp)
        if not pose:
            continue
        # 親が同じく追加済みジョイントならその下、そうでなければ attach_parent の下へ配置
        parent_live = None
        ref_parent = row.get("ref_parent") or ""
        if ref_parent in added_ref_to_uuid:
            parent_live = _uuid_to_path(added_ref_to_uuid[ref_parent])
        if not parent_live:
            attach_orig = row.get("attach_parent") or ""
            parent_live = _uuid_to_path(orig_path_to_uuid.get(attach_orig))

        cmds.select(clear=True)
        new_jnt = cmds.joint(name=row["new_name"])
        new_jnt_long = (cmds.ls(new_jnt, long=True) or [new_jnt])[0]
        if parent_live and cmds.objExists(parent_live):
            new_jnt_long = (cmds.ls(cmds.parent(new_jnt_long, parent_live)[0], long=True) or [new_jnt_long])[0]
        cmds.xform(new_jnt_long, ws=True, t=pose["ws_pos"])
        try:
            cmds.setAttr(new_jnt_long + ".rotateOrder", pose["rotateOrder"])
            cmds.setAttr(new_jnt_long + ".radius", pose["radius"])
        except Exception:
            pass
        rig_cleanup_helper._set_joint_world_rot_to_orient(new_jnt_long, pose["ws_rot_mat"])
        uids = cmds.ls(new_jnt_long, uuid=True) or []
        if uids:
            added_uuids.append(uids[0])
            added_ref_to_uuid[rfp] = uids[0]

    # 対応ジョイントの Local Rotation Axis（jointOrient）・位置・回転順序を親→子順で上書きする
    overwritten_count = 0
    overwrite_rows = [r for r in current_rows if r["action"] == "overwrite" and r.get("ref_full")]
    overwrite_rows.sort(key=lambda r: r["cur_full"].count("|"))
    overwrite_uuids = {orig_path_to_uuid.get(r["cur_full"]) for r in overwrite_rows}

    for row in overwrite_rows:
        uid = orig_path_to_uuid.get(row["cur_full"])
        jnt_path = _uuid_to_path(uid)
        pose = ref_live_pose.get(row["ref_full"])
        if not jnt_path or not cmds.objExists(jnt_path) or not pose:
            continue

        # 直接の子のワールド位置・回転を退避して、親の軸や位置変更で子が崩れないよう保護する
        direct_children = cmds.listRelatives(jnt_path, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            c_uids = cmds.ls(child, uuid=True) or []
            c_uid = c_uids[0] if c_uids else None
            ws_p = cmds.xform(child, q=True, ws=True, t=True)
            ws_r = rig_cleanup_helper._get_orthonormal_rotation_matrix(child)
            is_j = (cmds.nodeType(child) == "joint")
            saved_children.append((c_uid, ws_p, ws_r, is_j))

        # スケールを (1, 1, 1) に整理
        if rig_cleanup_helper._is_attr_editable(jnt_path, "scale"):
            cmds.setAttr(jnt_path + ".scale", 1.0, 1.0, 1.0, type="double3")

        if overwrite_position and rig_cleanup_helper._is_attr_editable(jnt_path, "translate"):
            cmds.xform(jnt_path, ws=True, t=pose["ws_pos"])

        if overwrite_rotate_order:
            try:
                if cmds.getAttr(jnt_path + ".rotateOrder", settable=True):
                    cmds.setAttr(jnt_path + ".rotateOrder", pose["rotateOrder"])
            except Exception:
                pass

        if overwrite_orient:
            rig_cleanup_helper._set_joint_world_rot_to_orient(jnt_path, pose["ws_rot_mat"])

        # 子のワールド位置・姿勢を復元する（後続で上書き対象の子は後段ループで最終調整される）
        for c_uid, ws_p, ws_r, is_j in saved_children:
            child_live = _uuid_to_path(c_uid)
            if not child_live or not cmds.objExists(child_live):
                continue
            try:
                if rig_cleanup_helper._is_attr_editable(child_live, "translate"):
                    cmds.xform(child_live, ws=True, t=ws_p)
                if is_j and c_uid not in overwrite_uuids:
                    rig_cleanup_helper._set_joint_world_rot_to_orient(child_live, ws_r)
            except Exception:
                pass

        overwritten_count += 1

    # リネーム適用（名前衝突を避けるため 2 パス方式で安全にリネームする）
    renamed_count = 0
    if apply_rename:
        rename_targets = []
        for row in current_rows:
            if row["action"] == "delete_merge":
                continue
            new_name = (row.get("new_name") or "").strip()
            if new_name and new_name != row["cur_short"]:
                uid = orig_path_to_uuid.get(row["cur_full"])
                if uid:
                    rename_targets.append((uid, new_name))

        temp_pairs = []
        for idx, (uid, desired) in enumerate(rename_targets):
            live_p = _uuid_to_path(uid)
            if live_p and cmds.objExists(live_p):
                try:
                    cmds.rename(live_p, "__siweRefRenameTemp_{0}_#".format(idx))
                    temp_pairs.append((uid, desired))
                except Exception:
                    pass
        for uid, desired in temp_pairs:
            live_p = _uuid_to_path(uid)
            if live_p and cmds.objExists(live_p):
                try:
                    cmds.rename(live_p, desired)
                    renamed_count += 1
                except Exception:
                    pass

    # 旧ジョイントフルパス -> 最終ジョイントフルパス（削除時は移譲先）のマップを構築する
    # 一部ジョイントのみ上書きした場合でも、親リネーム等でパスが変わった非選択バインドジョイントを UUID から正確に追跡する
    old_to_final_map = {}
    for orig_path, uid in orig_path_to_uuid.items():
        live_p = _uuid_to_path(uid)
        if live_p:
            old_to_final_map[orig_path] = live_p

    for row in current_rows:
        cfp = row["cur_full"]
        if row["action"] == "delete_merge":
            merge_orig = merge_map_orig.get(cfp) or ""
            target_live = _uuid_to_path(orig_path_to_uuid.get(merge_orig))
            if target_live:
                old_to_final_map[cfp] = target_live
            elif cfp in old_to_final_map:
                del old_to_final_map[cfp]
        else:
            live_p = _uuid_to_path(orig_path_to_uuid.get(cfp))
            if live_p:
                old_to_final_map[cfp] = live_p

    added_final_paths = [p for p in (_uuid_to_path(u) for u in added_uuids) if p]
    extra_bind = added_final_paths if include_added_in_skin else []

    # 全対象メッシュを再バインドしてウェイトを綺麗に適用し直す
    rebound_meshes = []
    for mesh_long, save_path, payload in mesh_backups:
        _rebind_and_apply_remapped_weights(
            mesh_long,
            payload,
            old_to_final_map,
            extra_bind_joints=extra_bind,
            clean_orphaned_bind_pose=True,
        )
        try:
            payload["status"] = "restored"
            with open(save_path, "w") as fp:
                json.dump(payload, fp)
        except Exception:
            pass
        rebound_meshes.append(mesh_long)

    if rebound_meshes:
        cmds.select(rebound_meshes, replace=True)

    return {
        "session_json": session_json_path,
        "overwritten": overwritten_count,
        "added": len(added_final_paths),
        "deleted": deleted_count,
        "renamed": renamed_count,
        "rebound_meshes": rebound_meshes,
    }


# ------------------------------------------------------------------
# クイック軸方向コピー＆適用（JSON 保存なし・指ボーン等の素早い方向修正向け）
# ------------------------------------------------------------------
ROTATE_ORDER_LABELS = ("XYZ", "YZX", "ZXY", "XZY", "YXZ", "ZYX")
_FINGER_INDEX_RE = re.compile(r"(thumb|index|middle|ring|pinky|little).*?(\d+)", re.IGNORECASE)


def _extract_finger_key(short_name):
    """指ボーン名（例: LeftHandPinky3 / Pinky4_L）から (指種別, 左右, 関節番号) を抽出する。"""
    base = (short_name or "").split("|")[-1].split(":")[-1]
    side = bone_picker_window.detect_bone_side(base)
    m = _FINGER_INDEX_RE.search(base)
    if not m:
        return None
    f_name = m.group(1).lower()
    if f_name == "little":
        f_name = "pinky"
    f_idx = int(m.group(2))
    return (f_name, side, f_idx)


def capture_quick_axis_reference(joint_paths, include_children=False):
    """選択したリファレンスボーン（単体または複数チェーン）の方向を JSON 保存なしでメモリへ一時保存する。"""
    if isinstance(joint_paths, str):
        seed = [joint_paths]
    else:
        seed = list(joint_paths or [])

    if include_children:
        ordered, _ = collect_joint_subtree(seed)
    else:
        ordered, _ = collect_selected_joints_only(seed)

    if not ordered:
        raise RuntimeError("Valid reference joint is required.")

    entries = []
    for full_path in ordered:
        rot_mat = rig_cleanup_helper._get_orthonormal_rotation_matrix(full_path)
        rot_order = int(cmds.getAttr(full_path + ".rotateOrder"))
        order_label = ROTATE_ORDER_LABELS[rot_order] if 0 <= rot_order < len(ROTATE_ORDER_LABELS) else str(rot_order)
        short_name = full_path.split("|")[-1]
        entries.append({
            "full_path": full_path,
            "short_name": short_name,
            "ws_rot_mat": [float(rot_mat[i]) for i in range(16)],
            "rotateOrder": rot_order,
            "rotateOrderLabel": order_label,
            "finger_key": _extract_finger_key(short_name),
            "alias_key": normalize_joint_token(short_name),
        })

    first = entries[0]
    return {
        "full_path": first["full_path"],
        "short_name": first["short_name"],
        "ws_rot_mat": first["ws_rot_mat"],
        "rotateOrder": first["rotateOrder"],
        "rotateOrderLabel": first["rotateOrderLabel"],
        "entries": entries,
    }


def _resolve_quick_ref_entry_for_joint(jnt_path, idx_in_ordered, axis_ref_data):
    """対象ジョイントに最も適したリファレンス方向エントリを選ぶ（4番目欠損の指ボーンにも対応）。"""
    entries = axis_ref_data.get("entries") or [axis_ref_data]
    if len(entries) == 1:
        return entries[0]

    short_name = jnt_path.split("|")[-1]
    short_low = short_name.split(":")[-1].lower()
    # ショートネーム一致
    for e in entries:
        if e["short_name"].split(":")[-1].lower() == short_low:
            return e

    # 指種別・左右・関節番号一致（例: Pinky1..3 があり Pinky4 が無い場合も 1..3 が厳密一致する）
    f_key = _extract_finger_key(short_name)
    if f_key:
        for e in entries:
            if e.get("finger_key") == f_key:
                return e
        # 同側・同関節番号へのフォールバック（別指を手本にしている場合など）
        for e in entries:
            ek = e.get("finger_key")
            if ek and ek[1] == f_key[1] and ek[2] == f_key[2]:
                return e
        # 関節番号のみ一致（左右をまたぐ場合）
        for e in entries:
            ek = e.get("finger_key")
            if ek and ek[2] == f_key[2]:
                return e

    # 標準部位エイリアス＋左右一致
    a_key = normalize_joint_token(short_name)
    if a_key[0]:
        for e in entries:
            if e.get("alias_key") == a_key:
                return e

    # チェーン内の順番へフォールバック（対象側が 3 本でリファレンスが 4 本なら先頭 3 本を対応）
    if 0 <= idx_in_ordered < len(entries):
        return entries[idx_in_ordered]
    return entries[-1]


def _preserve_child_pose_strict(child, ws_pos, ws_rot_mat, is_joint, orig_rot, orig_rot_order):
    """親ボーンの軸変更後も子階層が一切回転・移動しないよう、子の jointOrient のみ逆算補正して姿勢を維持する。"""
    if not cmds.objExists(child):
        return False

    try:
        if rig_cleanup_helper._is_attr_editable(child, "translate"):
            cmds.xform(child, ws=True, t=ws_pos)
    except Exception:
        pass

    if is_joint:
        try:
            # 子の既存ローカル rotate を維持したまま jointOrient を逆算し、子と孫以下が回転しないようにする
            # Maya 行ベクトル規約: W_child = R_rot * R_jo_new * W_parent_new
            # よって R_jo_new = R_rot^-1 * W_child * W_parent_new^-1
            parent_rot = rig_cleanup_helper._get_parent_rotation_matrix(child)
            r_rot = om2.MEulerRotation(
                math.radians(float(orig_rot[0])),
                math.radians(float(orig_rot[1])),
                math.radians(float(orig_rot[2])),
                int(orig_rot_order),
            ).asMatrix()
            jo_mat = r_rot.inverse() * ws_rot_mat * parent_rot.inverse()
            euler = om2.MTransformationMatrix(jo_mat).rotation(asQuaternion=False)
            dx = math.degrees(euler.x)
            dy = math.degrees(euler.y)
            dz = math.degrees(euler.z)
            dx = 0.0 if abs(dx) < 1e-6 else dx
            dy = 0.0 if abs(dy) < 1e-6 else dy
            dz = 0.0 if abs(dz) < 1e-6 else dz

            if rig_cleanup_helper._is_attr_editable(child, "jointOrient"):
                cmds.setAttr(child + ".jointOrient", dx, dy, dz, type="double3")
                return True
        except Exception:
            pass
    else:
        try:
            m_vals = [float(ws_rot_mat[i]) for i in range(16)]
            m_vals[12], m_vals[13], m_vals[14] = float(ws_pos[0]), float(ws_pos[1]), float(ws_pos[2])
            cmds.xform(child, ws=True, matrix=m_vals)
            return True
        except Exception:
            pass
    return False


def _update_skin_bind_pre_for_modified_joints(old_world_matrices):
    """軸変更によって変化したジョイントの skinCluster.bindPreMatrix を補正し、メッシュ変形を防いで軸だけ差し替える。"""
    updated_skins = set()
    dirty_geos = set()

    for jnt, old_wm in (old_world_matrices or {}).items():
        if not cmds.objExists(jnt):
            continue
        try:
            new_wm_inv = om2.MMatrix(cmds.getAttr(jnt + ".worldInverseMatrix[0]"))
            # 変更前後の変換差分: new_bindPreMatrix = old_bindPreMatrix * old_worldMatrix * new_worldInverseMatrix
            delta_mat = old_wm * new_wm_inv
        except Exception:
            continue

        # ジョイントの worldMatrix から skinCluster.matrix[idx] への接続を探索
        conns = cmds.listConnections(
            jnt + ".worldMatrix[0]",
            source=False,
            destination=True,
            connections=False,
            plugs=True,
            type="skinCluster",
        ) or []
        for dst_plug in conns:
            m = re.match(r"^([^.]+)\.matrix\[(\d+)\]$", dst_plug)
            if not m:
                continue
            sc_node = m.group(1)
            idx_str = m.group(2)
            bpm_attr = "{0}.bindPreMatrix[{1}]".format(sc_node, idx_str)
            try:
                old_bpm = om2.MMatrix(cmds.getAttr(bpm_attr))
                new_bpm = old_bpm * delta_mat
                cmds.setAttr(bpm_attr, [float(new_bpm[i]) for i in range(16)], type="matrix")
                updated_skins.add(sc_node)
                for geo in (cmds.skinCluster(sc_node, q=True, geometry=True) or []):
                    dirty_geos.add(geo)
            except Exception:
                pass

    for geo in dirty_geos:
        try:
            cmds.dgdirty(geo)
        except Exception:
            pass
    return sorted(updated_skins)


def apply_quick_axis_to_joints(
    target_joints,
    axis_ref_data,
    include_children=False,
    match_rotate_order=True,
    update_skin_bind_pre=True,
):
    """一時保存したリファレンス方向へ、JSON 保存なしで対象ボーンのローカル回転軸だけを素早く変更する（子階層非回転・4番目指欠損最適化）。"""
    if not axis_ref_data or not axis_ref_data.get("ws_rot_mat"):
        raise RuntimeError("Reference axis direction is not stored yet.")

    if include_children:
        ordered, _ = collect_joint_subtree(target_joints)
    else:
        ordered, _ = collect_selected_joints_only(target_joints)

    if not ordered:
        return {"updated": [], "skipped": [], "updated_skins": [], "end_aligned": 0}

    ordered_set = set(ordered)

    # スキン補正用に、対象ジョイントおよび直接の子の変更前 worldMatrix を記録しておく
    old_world_matrices = {}
    if update_skin_bind_pre:
        for jnt in ordered:
            if cmds.objExists(jnt):
                try:
                    old_world_matrices[jnt] = om2.MMatrix(cmds.getAttr(jnt + ".worldMatrix[0]"))
                except Exception:
                    pass
                for child in (cmds.listRelatives(jnt, children=True, type="joint", fullPath=True) or []):
                    if child not in old_world_matrices and cmds.objExists(child):
                        try:
                            old_world_matrices[child] = om2.MMatrix(cmds.getAttr(child + ".worldMatrix[0]"))
                        except Exception:
                            pass

    updated = []
    skipped = []
    touched_for_skin = {}
    end_aligned = 0

    for idx_in_ord, jnt in enumerate(ordered):
        if not cmds.objExists(jnt):
            continue
        if not (rig_cleanup_helper._is_attr_editable(jnt, "jointOrient") and rig_cleanup_helper._is_attr_editable(jnt, "rotate")):
            skipped.append(jnt)
            continue

        ref_entry = _resolve_quick_ref_entry_for_joint(jnt, idx_in_ord, axis_ref_data)
        target_rot_mat = om2.MMatrix(ref_entry["ws_rot_mat"])
        target_rot_order = int(ref_entry.get("rotateOrder", 0))

        # 直接の子のワールド位置・回転・既存ローカル rotate を退避し、子階層が一切回転しないように保護する
        direct_children = cmds.listRelatives(jnt, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            if not cmds.objExists(child):
                continue
            ws_p = cmds.xform(child, q=True, ws=True, t=True)
            ws_r = rig_cleanup_helper._get_orthonormal_rotation_matrix(child)
            is_j = (cmds.nodeType(child) == "joint")
            orig_rot = cmds.getAttr(child + ".rotate")[0] if is_j else (0.0, 0.0, 0.0)
            orig_ro = int(cmds.getAttr(child + ".rotateOrder")) if is_j else 0
            saved_children.append((child, ws_p, ws_r, is_j, orig_rot, orig_ro))

        if match_rotate_order:
            try:
                if cmds.getAttr(jnt + ".rotateOrder", settable=True):
                    cmds.setAttr(jnt + ".rotateOrder", target_rot_order)
            except Exception:
                pass

        # 4番目ボーンが無い末端指ジョイント（子ジョイントなし）で、親も今回の変更対象だった場合は親の軸方向と一致させる
        child_joints = cmds.listRelatives(jnt, children=True, type="joint", fullPath=True) or []
        parent_joints = cmds.listRelatives(jnt, parent=True, type="joint", fullPath=True) or []
        is_leaf_with_updated_parent = (not child_joints) and bool(parent_joints) and (parent_joints[0] in ordered_set)
        if is_leaf_with_updated_parent and len(axis_ref_data.get("entries") or [axis_ref_data]) == 1:
            # 1本リファレンスで末端を親と揃える場合、jointOrient = (0, 0, 0) で厳密一致させる
            cmds.setAttr(jnt + ".rotate", 0.0, 0.0, 0.0, type="double3")
            if rig_cleanup_helper._is_attr_editable(jnt, "rotateAxis"):
                cmds.setAttr(jnt + ".rotateAxis", 0.0, 0.0, 0.0, type="double3")
            cmds.setAttr(jnt + ".jointOrient", 0.0, 0.0, 0.0, type="double3")
            ok = True
            end_aligned += 1
        else:
            ok = rig_cleanup_helper._set_joint_world_rot_to_orient(jnt, target_rot_mat)
            if ok and not child_joints:
                end_aligned += 1

        if not ok:
            skipped.append(jnt)
            continue

        updated.append(jnt)
        if jnt in old_world_matrices:
            touched_for_skin[jnt] = old_world_matrices[jnt]

        # すべての子階層が回転・移動しないよう元のワールド位置・姿勢を厳密復元する
        for child, ws_p, ws_r, is_j, orig_rot, orig_ro in saved_children:
            if not cmds.objExists(child):
                continue
            if child in ordered_set:
                # 後続で軸変更する子ジョイントも位置ズレを防ぐためワールド位置だけは即座に戻しておく
                try:
                    if rig_cleanup_helper._is_attr_editable(child, "translate"):
                        cmds.xform(child, ws=True, t=ws_p)
                except Exception:
                    pass
            else:
                if _preserve_child_pose_strict(child, ws_p, ws_r, is_j, orig_rot, orig_ro):
                    if child in old_world_matrices:
                        touched_for_skin[child] = old_world_matrices[child]

    updated_skins = []
    if update_skin_bind_pre and touched_for_skin:
        updated_skins = _update_skin_bind_pre_for_modified_joints(touched_for_skin)

    return {
        "updated": updated,
        "skipped": skipped,
        "updated_skins": updated_skins,
        "end_aligned": end_aligned,
    }


def freeze_quick_selected_rotations(
    target_joints,
    include_children=False,
    update_skin_bind_pre=True,
):
    """選択ボーンの回転（Rotate / RotateAxis）を子階層が回転しないよう jointOrient へフリーズする。"""
    if include_children:
        ordered, _ = collect_joint_subtree(target_joints)
    else:
        ordered, _ = collect_selected_joints_only(target_joints)

    if not ordered:
        return {"frozen": [], "skipped": [], "updated_skins": []}

    ordered_set = set(ordered)
    old_world_matrices = {}
    if update_skin_bind_pre:
        for jnt in ordered:
            if cmds.objExists(jnt):
                try:
                    old_world_matrices[jnt] = om2.MMatrix(cmds.getAttr(jnt + ".worldMatrix[0]"))
                except Exception:
                    pass
                for child in (cmds.listRelatives(jnt, children=True, type="joint", fullPath=True) or []):
                    if child not in old_world_matrices and cmds.objExists(child):
                        try:
                            old_world_matrices[child] = om2.MMatrix(cmds.getAttr(child + ".worldMatrix[0]"))
                        except Exception:
                            pass

    frozen = []
    skipped = []
    touched_for_skin = {}

    for jnt in ordered:
        if not cmds.objExists(jnt):
            continue
        if not (rig_cleanup_helper._is_attr_editable(jnt, "jointOrient") and rig_cleanup_helper._is_attr_editable(jnt, "rotate")):
            skipped.append(jnt)
            continue

        direct_children = cmds.listRelatives(jnt, children=True, type="transform", fullPath=True) or []
        saved_children = []
        for child in direct_children:
            if not cmds.objExists(child):
                continue
            ws_p = cmds.xform(child, q=True, ws=True, t=True)
            ws_r = rig_cleanup_helper._get_orthonormal_rotation_matrix(child)
            is_j = (cmds.nodeType(child) == "joint")
            orig_rot = cmds.getAttr(child + ".rotate")[0] if is_j else (0.0, 0.0, 0.0)
            orig_ro = int(cmds.getAttr(child + ".rotateOrder")) if is_j else 0
            saved_children.append((child, ws_p, ws_r, is_j, orig_rot, orig_ro))

        world_rot_mat = rig_cleanup_helper._get_orthonormal_rotation_matrix(jnt)
        ok = rig_cleanup_helper._set_joint_world_rot_to_orient(jnt, world_rot_mat)
        if not ok:
            skipped.append(jnt)
            continue

        frozen.append(jnt)
        if jnt in old_world_matrices:
            touched_for_skin[jnt] = old_world_matrices[jnt]

        for child, ws_p, ws_r, is_j, orig_rot, orig_ro in saved_children:
            if not cmds.objExists(child):
                continue
            if child in ordered_set:
                try:
                    if rig_cleanup_helper._is_attr_editable(child, "translate"):
                        cmds.xform(child, ws=True, t=ws_p)
                except Exception:
                    pass
            else:
                if _preserve_child_pose_strict(child, ws_p, ws_r, is_j, orig_rot, orig_ro):
                    if child in old_world_matrices:
                        touched_for_skin[child] = old_world_matrices[child]

    updated_skins = []
    if update_skin_bind_pre and touched_for_skin:
        updated_skins = _update_skin_bind_pre_for_modified_joints(touched_for_skin)

    return {
        "frozen": frozen,
        "skipped": skipped,
        "updated_skins": updated_skins,
    }
