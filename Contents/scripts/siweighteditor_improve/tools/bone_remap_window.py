# -*- coding: utf-8 -*-
"""選択頂点のウェイトを別ボーンへリアルタイムに移動（付け替え）する専用ウィンドウ。"""

from collections import defaultdict

from maya import cmds
import maya.api.OpenMaya as om2
import maya.api.OpenMayaAnim as oma2

from ..viewport import bone_visualizer
from ..viewport import joint_display_helper
from .. import lang
from .. import qt
from ..qt import *


WEIGHT_EPSILON = 0.0001
# メッシュ全体表示時の統計集計で負荷を抑えるための最大走査頂点数
MAX_STAT_ROWS = 10000
# 頂点重心や別頂点からのボーン取得で使うサンプリング上限
MAX_PICK_SAMPLE_VERTICES = 128
# 近傍ボーンとして上位表示する最大本数
MAX_NEARBY_BONES = 8


def _short_joint_name(joint_path):
    """フルパスから表示用の短いボーン名を返す。"""
    return joint_path.split("|")[-1].split(":")[-1]


def _resolve_long_name_cached(node_name, cache):
    """フルパス名をキャッシュ併用で解決する。"""
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


def _has_viewport_component_selection():
    """現在のビューポート選択に頂点・エッジ・フェースが含まれるかを返す。"""
    try:
        selection = cmds.ls(sl=True, l=True) or []
    except Exception:
        return False
    for item in selection:
        if any(token in item for token in (".vtx[", ".e[", ".f[")):
            return True
    return False


def _get_visible_mesh_dag(mesh_path):
    """メッシュトランスフォームまたはシェイプから中間オブジェクト以外の可視メッシュ DAG を返す。"""
    try:
        sel = om2.MSelectionList()
        sel.add(mesh_path)
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


def _compute_centroid_from_vtx_keys(vtx_keys):
    """(メッシュパス, 頂点ID) リストからワールド重心座標を軽量計算する。"""
    if not vtx_keys:
        return None

    mesh_vids = defaultdict(list)
    step = max(1, len(vtx_keys) // 32) if len(vtx_keys) > 32 else 1
    for mesh_path, vid in vtx_keys[::step]:
        mesh_vids[mesh_path].append(int(vid))

    tx = 0.0
    ty = 0.0
    tz = 0.0
    count = 0
    for mesh_path, vids in mesh_vids.items():
        shape_dag = _get_visible_mesh_dag(mesh_path)
        if shape_dag is None:
            continue
        try:
            mesh_fn = om2.MFnMesh(shape_dag)
            num_verts = mesh_fn.numVertices
        except Exception:
            continue
        for vid in vids:
            if 0 <= vid < num_verts:
                try:
                    pt = mesh_fn.getPoint(vid, om2.MSpace.kWorld)
                except Exception:
                    continue
                tx += pt.x
                ty += pt.y
                tz += pt.z
                count += 1

    if count == 0:
        return None
    inv = 1.0 / float(count)
    return om2.MPoint(tx * inv, ty * inv, tz * inv)


def _find_nearby_joints(origin_pt, candidate_joints, exclude_joints, max_count=MAX_NEARBY_BONES):
    """ワールド重心から距離が近いボーン上位を昇順で返す。"""
    if origin_pt is None or not candidate_joints:
        return []

    zero_pt = om2.MPoint(0.0, 0.0, 0.0)
    scored = []
    for joint_path in candidate_joints:
        if joint_path in exclude_joints:
            continue
        try:
            sel = om2.MSelectionList()
            sel.add(joint_path)
            dag_path = sel.getDagPath(0)
            j_pt = zero_pt * dag_path.inclusiveMatrix()
        except Exception:
            continue
        dx = j_pt.x - origin_pt.x
        dy = j_pt.y - origin_pt.y
        dz = j_pt.z - origin_pt.z
        dist_sq = dx * dx + dy * dy + dz * dz
        scored.append((dist_sq, joint_path))

    scored.sort(key=lambda item: item[0])
    return [joint_path for _, joint_path in scored[:max_count]]


def _collect_weighted_bones_from_viewport_vertices():
    """現在のビューポート選択頂点から OpenMaya 経由でウェイト先ボーンと平均値を取得する。"""
    slist = om2.MGlobal.getActiveSelectionList()
    it = om2.MItSelectionList(slist)

    mesh_vtx_map = defaultdict(set)
    while not it.isDone():
        try:
            dag_path, component = it.getComponent()
        except Exception:
            it.next()
            continue

        if component.isNull():
            it.next()
            continue

        shape_dag = om2.MDagPath(dag_path)
        if not shape_dag.node().hasFn(om2.MFn.kMesh):
            shape_dag = _get_visible_mesh_dag(dag_path.fullPathName())
            if shape_dag is None:
                it.next()
                continue

        try:
            if component.hasFn(om2.MFn.kMeshVertComponent):
                comp_fn = om2.MFnSingleIndexedComponent(component)
                vids = comp_fn.getElements()
            elif component.hasFn(om2.MFn.kMeshEdgeComponent):
                vids = []
                edge_it = om2.MItMeshEdge(shape_dag, component)
                while not edge_it.isDone():
                    vids.append(int(edge_it.vertexId(0)))
                    vids.append(int(edge_it.vertexId(1)))
                    edge_it.next()
            elif component.hasFn(om2.MFn.kMeshPolygonComponent):
                vids = []
                poly_it = om2.MItMeshPolygon(shape_dag, component)
                while not poly_it.isDone():
                    for vid in poly_it.getVertices():
                        vids.append(int(vid))
                    poly_it.next()
            else:
                vids = []
        except Exception:
            vids = []

        if vids:
            mesh_vtx_map[shape_dag.fullPathName()].update(int(v) for v in vids)
        it.next()

    if not mesh_vtx_map:
        return [], 0

    weight_sum = defaultdict(float)
    total_vtx_count = 0

    for shape_path, vid_set in mesh_vtx_map.items():
        vids = sorted(vid_set)
        if len(vids) > MAX_PICK_SAMPLE_VERTICES:
            step = max(1, len(vids) // MAX_PICK_SAMPLE_VERTICES)
            vids = vids[::step]

        try:
            skin_clusters = cmds.ls(cmds.listHistory(shape_path), type="skinCluster") or []
            if not skin_clusters:
                continue
            skin_sel = om2.MGlobal.getSelectionListByName(skin_clusters[0])
            skin_fn = oma2.MFnSkinCluster(skin_sel.getDependNode(0))
            inf_dags = skin_fn.influenceObjects()
            inf_count = len(inf_dags)
            if inf_count == 0:
                continue

            mesh_sel = om2.MSelectionList()
            mesh_sel.add(shape_path)
            shape_dag = mesh_sel.getDagPath(0)

            comp_fn = om2.MFnSingleIndexedComponent()
            vtx_comp = comp_fn.create(om2.MFn.kMeshVertComponent)
            comp_fn.addElements(vids)

            weights, _ = skin_fn.getWeights(shape_dag, vtx_comp)
            inf_paths = [inf_dags[i].fullPathName() for i in range(inf_count)]

            num_sampled = len(vids)
            for v_idx in range(num_sampled):
                base = v_idx * inf_count
                for i in range(inf_count):
                    w = weights[base + i]
                    if w > WEIGHT_EPSILON:
                        weight_sum[inf_paths[i]] += w
            total_vtx_count += num_sampled
        except Exception:
            continue

    if total_vtx_count == 0 or not weight_sum:
        return [], total_vtx_count

    inv = 1.0 / float(total_vtx_count)
    results = [(joint_path, total_w * inv) for joint_path, total_w in weight_sum.items()]
    results.sort(key=lambda item: (-item[1], _short_joint_name(item[0]).upper()))
    return results, total_vtx_count


def _compress_vtx_keys_to_selection_strings(vtx_keys):
    """(メッシュパス, 頂点ID) 一覧を高速選択用の圧縮頂点指定文字列へ変換する。"""
    mesh_map = defaultdict(list)
    for mesh_path, vid in vtx_keys:
        mesh_map[mesh_path].append(int(vid))

    sel_strings = []
    for mesh_path, vids in mesh_map.items():
        sorted_vids = sorted(set(vids))
        if not sorted_vids:
            continue
        start = sorted_vids[0]
        prev = start
        for vid in sorted_vids[1:]:
            if vid == prev + 1:
                prev = vid
            else:
                if start == prev:
                    sel_strings.append("{0}.vtx[{1}]".format(mesh_path, start))
                else:
                    sel_strings.append("{0}.vtx[{1}:{2}]".format(mesh_path, start, prev))
                start = vid
                prev = vid
        if start == prev:
            sel_strings.append("{0}.vtx[{1}]".format(mesh_path, start))
        else:
            sel_strings.append("{0}.vtx[{1}:{2}]".format(mesh_path, start, prev))
    return sel_strings


class BoneRemapWindow(qt.SubWindow):
    """選択頂点のボーンウェイトを別ボーンへ付け替えるリアルタイム連動ウィンドウ。"""

    WINDOW_TITLE = "Bone Weight Remap - SIWeightEditor Improve"
    WINDOW_OBJECT_NAME = "SIWeightEditorImproveBoneRemapWindow"

    def __init__(self, editor_window):
        super(BoneRemapWindow, self).__init__(parent=qt.get_maya_window())
        self.editor = editor_window
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(700, 585)

        self._source_entries = []
        self._target_joints = []
        self._target_rows = []
        self._target_vtx_keys = []
        self._locked_vtx_keys = []
        self._is_locked = False
        self._is_vertex_mode = False
        self._target_meshes = []
        self._vtx_weight_map = {}
        self._nearby_joints = []
        self._last_active_pane = None
        self._add_weight_normalized = 0.05
        self._max_weight = 100.0
        self._decimals = 1

        self._init_ui()
        self.refresh_from_editor()

    def _init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ツールの概要・使い方の案内
        self.guide_label = QLabel(
            lang.Lang(
                en=(
                    "Move weights from source bone(s) on the left to a destination bone on the right, "
                    "or add a new bone's weight onto selected vertices.\n"
                    "Use 'Lock Target' to keep the current vertices fixed while picking another vertex/bone in the viewport."
                ),
                ja=(
                    u"左の「変更元ボーン」のウェイトを右の「変更先ボーン」へ一括移動したり、"
                    u"選択頂点へ別ボーンのウェイトを追加できます。\n"
                    u"「対象を固定」を ON にすると、作業対象の頂点を保持したままビューポートで別の頂点やボーンを選んで移動先を指定できます。"
                ),
            ).output()
        )
        self.guide_label.setWordWrap(True)
        qt.change_button_color(self.guide_label, textColor=[218, 224, 232], bgColor=[46, 50, 58])
        self.guide_label.setMargin(6)
        main_layout.addWidget(self.guide_label)

        # 上部ステータスバー（現在の対象モードと対象固定ボタン）
        top_layout = QHBoxLayout()
        top_layout.setSpacing(6)

        self.mode_label = QLabel("")
        self.mode_label.setWordWrap(True)
        qt.change_button_color(self.mode_label, textColor=[220, 220, 220], bgColor=[52, 52, 56])
        self.mode_label.setMargin(5)
        top_layout.addWidget(self.mode_label, 1)

        self.lock_target_but = QPushButton(
            lang.Lang(en="Lock Target", ja=u"対象を固定").output()
        )
        self.lock_target_but.setCheckable(True)
        self.lock_target_but.setMinimumHeight(28)
        self.lock_target_but.setMinimumWidth(96)
        self.lock_target_but.setToolTip(
            lang.Lang(
                en="Lock current target vertices so you can click another vertex in viewport to pick the destination bone",
                ja=u"移動対象の頂点を固定します。\n固定したままビューポートで別の頂点を選び、右側の「選択頂点から取得」で変更先ボーンを拾えます。",
            ).output()
        )
        self.lock_target_but.toggled.connect(self._on_lock_target_toggled)
        top_layout.addWidget(self.lock_target_but)
        main_layout.addLayout(top_layout)

        # 中央スプリッター（左：変更元ボーン一覧 / 右：変更先ボーン一覧）
        splitter = QSplitter(Qt.Horizontal, self)
        main_layout.addWidget(splitter, 1)

        # 左ペイン：変更元ボーン
        left_panel = QWidget(splitter)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(4)

        left_header = QLabel(
            lang.Lang(
                en="Source Bones (Weighted on selection / All if none)",
                ja=u"変更元ボーン（選択頂点のウェイト先 / 未選択時は全ボーン）",
            ).output()
        )
        left_layout.addWidget(left_header)

        self.src_filter_edit = QLineEdit()
        self.src_filter_edit.setPlaceholderText(
            lang.Lang(en="Filter source bones...", ja=u"変更元ボーンを検索...").output()
        )
        self.src_filter_edit.textChanged.connect(self._populate_source_table)
        left_layout.addWidget(self.src_filter_edit)

        self.src_table = QTableWidget(0, 3, self)
        self.src_table.setHorizontalHeaderLabels([
            lang.Lang(en="Bone", ja=u"ボーン名").output(),
            lang.Lang(en="Weight", ja=u"ウェイト").output(),
            lang.Lang(en="Verts", ja=u"頂点数").output(),
        ])
        self.src_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.src_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.src_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.src_table.setAlternatingRowColors(True)
        self.src_table.verticalHeader().setVisible(False)
        self.src_table.verticalHeader().setDefaultSectionSize(22)

        header = self.src_table.horizontalHeader()
        resize_fn = getattr(header, "setSectionResizeMode", None) or getattr(header, "setResizeMode")
        resize_fn(0, QHeaderView.Stretch)
        resize_fn(1, QHeaderView.ResizeToContents)
        resize_fn(2, QHeaderView.ResizeToContents)

        self.src_table.itemSelectionChanged.connect(self._on_source_selection_changed)
        left_layout.addWidget(self.src_table, 1)

        # 右ペイン：変更先ボーン
        right_panel = QWidget(splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(4)

        right_header_layout = QHBoxLayout()
        right_header_layout.setContentsMargins(0, 0, 0, 0)
        right_header_layout.setSpacing(6)
        right_header = QLabel(
            lang.Lang(en="Target Bone (Move weight to)", ja=u"変更先ボーン（ウェイト移動先）").output()
        )
        right_header_layout.addWidget(right_header)
        right_header_layout.addStretch(1)

        self.highlight_target_chk = QCheckBox(
            lang.Lang(en="Highlight Target", ja=u"変更先をハイライト").output()
        )
        self.highlight_target_chk.setChecked(True)
        self.highlight_target_chk.setToolTip(
            lang.Lang(
                en="Highlight the selected target bone in the 3D viewport (works even when Joint Hilite / Used Joints are enabled)",
                ja=u"選択中の変更先ボーンをビューポートでマゼンタ色＋X-Rayマーカーで強調表示します\n（Joint Hilite や Used Joints が有効でも動作します）",
            ).output()
        )
        self.highlight_target_chk.toggled.connect(self._on_highlight_target_toggled)
        right_header_layout.addWidget(self.highlight_target_chk)
        right_layout.addLayout(right_header_layout)

        self.dst_filter_edit = QLineEdit()
        self.dst_filter_edit.setPlaceholderText(
            lang.Lang(en="Search target bone...", ja=u"変更先ボーンを検索...").output()
        )
        self.dst_filter_edit.textChanged.connect(self._populate_target_list)
        right_layout.addWidget(self.dst_filter_edit)

        # 頂点またはボーンから変更先を取得するボタン行
        pick_layout = QHBoxLayout()
        pick_layout.setSpacing(4)

        self.pick_from_vtx_but = QPushButton(
            lang.Lang(en="Pick from Verts", ja=u"選択頂点から取得").output()
        )
        self.pick_from_vtx_but.setToolTip(
            lang.Lang(
                en="Pick target bone from currently selected vertex weights (no need to select joints)",
                ja=u"ビューポートで選択中の頂点に入っているボーンから変更先を取得します（ボーン選択不要）",
            ).output()
        )
        self.pick_from_vtx_but.clicked.connect(self._pick_target_from_selected_vertices)
        pick_layout.addWidget(self.pick_from_vtx_but, 1)

        self.pick_viewport_joint_but = QPushButton(
            lang.Lang(en="Pick Joint", ja=u"選択ボーン取得").output()
        )
        self.pick_viewport_joint_but.setToolTip(
            lang.Lang(
                en="Set the joint currently selected in viewport/outliner as target",
                ja=u"ビューポートまたはアウトライナーで選択中のジョイントを変更先に指定",
            ).output()
        )
        self.pick_viewport_joint_but.clicked.connect(self._pick_selected_joint_as_target)
        pick_layout.addWidget(self.pick_viewport_joint_but)
        right_layout.addLayout(pick_layout)

        # 変更先リストの表示オプション行
        dst_opt_layout = QHBoxLayout()
        dst_opt_layout.setSpacing(8)

        self.prioritize_near_chk = QCheckBox(
            lang.Lang(en="Prioritize Vert & Nearby", ja=u"頂点・近傍ボーンを上位表示").output()
        )
        self.prioritize_near_chk.setChecked(True)
        self.prioritize_near_chk.toggled.connect(lambda: self._populate_target_list())
        dst_opt_layout.addWidget(self.prioritize_near_chk)

        self.show_scene_joints_chk = QCheckBox(
            lang.Lang(en="All scene joints", ja=u"シーン内全ジョイント").output()
        )
        self.show_scene_joints_chk.toggled.connect(self._refresh_target_candidates)
        dst_opt_layout.addWidget(self.show_scene_joints_chk)
        dst_opt_layout.addStretch(1)
        right_layout.addLayout(dst_opt_layout)

        self.dst_list = QListWidget(self)
        self.dst_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.dst_list.setAlternatingRowColors(True)
        self.dst_list.itemSelectionChanged.connect(self._on_target_selection_changed)
        right_layout.addWidget(self.dst_list, 1)

        splitter.setSizes([350, 350])

        # 下部プレビューおよび移動・追加実行バー
        bottom_frame = QFrame(self)
        bottom_frame.setFrameShape(QFrame.StyledPanel)
        bottom_vbox = QVBoxLayout(bottom_frame)
        bottom_vbox.setContentsMargins(8, 6, 8, 6)
        bottom_vbox.setSpacing(6)

        move_row = QHBoxLayout()
        move_row.setSpacing(8)
        self.preview_label = QLabel("")
        self.preview_label.setMinimumWidth(260)
        move_row.addWidget(self.preview_label, 1)

        self.move_but = QPushButton(
            lang.Lang(en="Move Weight", ja=u"ウェイトを移動").output()
        )
        self.move_but.setMinimumHeight(28)
        self.move_but.setMinimumWidth(140)
        self.move_but.clicked.connect(self.execute_remap)
        move_row.addWidget(self.move_but)
        bottom_vbox.addLayout(move_row)

        bottom_vbox.addWidget(qt.make_h_line())

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.add_preview_label = QLabel("")
        self.add_preview_label.setMinimumWidth(220)
        add_row.addWidget(self.add_preview_label, 1)

        add_val_label = QLabel(lang.Lang(en="Weight:", ja=u"追加値:").output())
        add_row.addWidget(add_val_label)

        self.add_weight_spin = QDoubleSpinBox(self)
        self.add_weight_spin.setMinimumWidth(72)
        self.add_weight_spin.setMaximumWidth(84)
        self.add_weight_spin.setToolTip(
            lang.Lang(
                en="Initial weight value added to vertices that do not yet have this bone",
                ja=u"まだそのボーンのウェイトが入っていない頂点に追加する初期ウェイト値",
            ).output()
        )
        self.add_weight_spin.valueChanged.connect(self._on_add_weight_spin_changed)
        add_row.addWidget(self.add_weight_spin)

        self.add_but = QPushButton(
            lang.Lang(en="Add Bone", ja=u"ウェイトを追加").output()
        )
        self.add_but.setMinimumHeight(28)
        self.add_but.setMinimumWidth(140)
        self.add_but.setToolTip(
            lang.Lang(
                en="Add selected bone (from either Source or Target pane) to target vertices; vertices already weighted to that bone are skipped",
                ja=u"左（変更元）または右（変更先）で選択したボーンを選択頂点へ追加します\n既にそのボーンのウェイトが入っている頂点はスルーされます",
            ).output()
        )
        self.add_but.clicked.connect(self.execute_add_weight)
        add_row.addWidget(self.add_but)
        bottom_vbox.addLayout(add_row)

        main_layout.addWidget(bottom_frame)

        # 実行結果メッセージ表示
        self.status_label = QLabel("")
        main_layout.addWidget(self.status_label)
        self._sync_add_weight_spinbox()

    def closeEvent(self, event):
        self._clear_target_highlight()
        if self.editor is not None and getattr(self.editor, "bone_remap_window", None) is self:
            self.editor.bone_remap_window = None
        super(BoneRemapWindow, self).closeEvent(event)

    def _clear_target_highlight(self):
        """ビューポート上の変更先ボーンハイライトを解除して元の状態へ戻す。"""
        try:
            joint_display_helper.clear_remap_target_highlight(window=self.editor)
        except Exception:
            pass
        try:
            bone_visualizer.set_remap_target(None)
        except Exception:
            pass

    def _sync_target_highlight(self):
        """チェック状態と選択中変更先ボーンに応じてビューポートハイライトを同期する。"""
        if not hasattr(self, "highlight_target_chk") or not self.highlight_target_chk.isChecked():
            self._clear_target_highlight()
            return

        dst_joint = self._get_selected_target_joint()
        if not dst_joint or not cmds.objExists(dst_joint):
            self._clear_target_highlight()
            return

        try:
            joint_display_helper.set_remap_target_highlight(dst_joint, window=self.editor)
        except Exception:
            pass
        try:
            bone_visualizer.set_remap_target(dst_joint, label=_short_joint_name(dst_joint))
        except Exception:
            pass

    def _on_highlight_target_toggled(self, checked):
        """変更先ハイライトのチェック切替時に即座へビューポート表示へ反映する。"""
        self._sync_target_highlight()

    def _resolve_skin_cluster_for_mesh(self, mesh_node):
        """メッシュに対応するスキンクラスタ名を確実に取得する。"""
        editor = self.editor
        if editor is not None:
            sc = (getattr(editor, "all_skin_clusters", None) or {}).get(mesh_node)
            if sc and cmds.objExists(sc):
                return sc
        try:
            skins = cmds.ls(cmds.listHistory(mesh_node, pruneDagObjects=True) or [], type="skinCluster") or []
            if skins:
                return skins[0]
            skins = cmds.ls(cmds.listHistory(mesh_node) or [], type="skinCluster") or []
            if skins:
                return skins[0]
        except Exception:
            pass
        return mesh_node

    def _on_lock_target_toggled(self, checked):
        """移動対象の頂点群を固定・解除する。"""
        if checked:
            if not self._target_vtx_keys:
                self.lock_target_but.blockSignals(True)
                self.lock_target_but.setChecked(False)
                self.lock_target_but.blockSignals(False)
                self._set_status(
                    lang.Lang(
                        en="Select vertices or a mesh before locking target.",
                        ja=u"固定する対象の頂点またはメッシュを選択してから押してください。",
                    ).output(),
                    error=True,
                )
                return
            self._is_locked = True
            self._locked_vtx_keys = list(self._target_vtx_keys)
            self.lock_target_but.setText(lang.Lang(en="Locked", ja=u"対象固定中").output())
            qt.change_button_color(self.lock_target_but, textColor=[255, 255, 255], bgColor=[170, 75, 45], mode="button")
            self._set_status(
                lang.Lang(
                    en="Target locked ({0} verts). Select another vertex and click 'Pick from Verts'.",
                    ja=u"移動対象を固定しました（{0} 頂点）。別頂点を選んで「選択頂点から取得」を押せます。",
                ).output().format(len(self._locked_vtx_keys)),
                error=False,
            )
        else:
            self._is_locked = False
            self._locked_vtx_keys = []
            self.lock_target_but.setText(lang.Lang(en="Lock Target", ja=u"対象を固定").output())
            self.lock_target_but.setStyleSheet("")
            self.refresh_from_editor()

    def _get_selected_source_joints(self):
        """左テーブルで選択中の変更元ボーン（フルパス一覧）を返す。"""
        selected_rows = sorted(set(idx.row() for idx in self.src_table.selectedIndexes()))
        joints = []
        for row in selected_rows:
            item = self.src_table.item(row, 0)
            if item is not None:
                joint_path = item.data(Qt.UserRole)
                if joint_path:
                    joints.append(joint_path)
        return joints

    def _get_selected_target_joint(self):
        """右リストで選択中の変更先ボーン（フルパス）を返す。"""
        items = self.dst_list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _collect_scene_joints(self):
        """シーン内の全ジョイントをフルパスで返す。"""
        try:
            joints = cmds.ls(type="joint", long=True) or []
        except Exception:
            joints = []
        return sorted(set(joints), key=lambda path: _short_joint_name(path).upper())

    def _collect_mesh_influences(self):
        """現在対象としているメッシュの全バインドボーンを返す。"""
        editor = self.editor
        if editor is None:
            return []

        long_cache = {}
        influences = set()
        influences_dict = getattr(editor, "influences_dict", None) or {}
        for mesh_node in self._target_meshes:
            for inf in influences_dict.get(mesh_node, []):
                influences.add(_resolve_long_name_cached(inf, long_cache))

        if not influences and hasattr(editor, "store_skin_weight"):
            for inf in getattr(editor.store_skin_weight, "all_influences", None) or []:
                influences.add(_resolve_long_name_cached(inf, long_cache))

        return sorted(influences, key=lambda path: _short_joint_name(path).upper())

    def refresh_from_editor(self, max_weight=None, decimals=None, force=False):
        """SIWeightEditor Improve 本体の現在状態に合わせて表示をリアルタイム更新する。"""
        scale_changed = False
        if max_weight is not None and self._max_weight != float(max_weight):
            self._max_weight = float(max_weight)
            scale_changed = True
        if decimals is not None and self._decimals != int(decimals):
            self._decimals = int(decimals)
            scale_changed = True
        if scale_changed:
            self._sync_add_weight_spinbox()

        # 対象固定中は左ペインの選択頂点と変更元一覧を維持し、桁数・単位表示だけ再描画する
        if self._is_locked and not force:
            self._populate_source_table()
            self._update_preview_and_button()
            return

        prev_sources = set(self._get_selected_source_joints())
        prev_target = self._get_selected_target_joint()

        editor = self.editor
        self._source_entries = []
        self._target_rows = []
        self._target_vtx_keys = []
        self._target_meshes = []
        self._vtx_weight_map = {}
        self._nearby_joints = []
        self._is_vertex_mode = False

        vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {} if editor else {}
        weight_model = getattr(editor, "weight_model", None) if editor else None
        model_data = getattr(weight_model, "_data", None) if weight_model else None
        selected_items = getattr(editor, "selected_items", None) or [] if editor else []
        hl_nodes = list(getattr(editor, "hl_nodes", None) or []) if editor else []

        # テーブル選択またはビューポートのコンポーネント選択があれば選択頂点モードとみなす
        has_table_selection = bool(selected_items)
        has_component_selection = _has_viewport_component_selection()

        if vtx_row_dict and model_data and (has_table_selection or has_component_selection):
            self._is_vertex_mode = True
            if has_table_selection:
                target_rows = sorted(set(item.row() for item in selected_items if item.row() in vtx_row_dict))
                mode_source = lang.Lang(en="Table Selection", ja=u"テーブル選択行").output()
            else:
                target_rows = sorted(vtx_row_dict.keys())
                mode_source = lang.Lang(en="Selected Vertices", ja=u"選択頂点").output()

            self._target_rows = target_rows
            self._target_vtx_keys = [(vtx_row_dict[r][6], vtx_row_dict[r][0]) for r in target_rows if r in vtx_row_dict]
            self._build_entries_from_rows(target_rows, vtx_row_dict, model_data, weighted_only=True)
            mesh_names = ", ".join(_short_joint_name(m) for m in self._target_meshes) or "-"
            self.mode_label.setText(
                lang.Lang(
                    en="Mode: {0} ({1} verts / {2}) - Showing weighted bones only",
                    ja=u"対象: {0}（{1} 頂点 / メッシュ: {2}） - ウェイト設定済みボーンのみ表示",
                ).output().format(mode_source, len(target_rows), mesh_names)
            )
        elif vtx_row_dict and model_data:
            # メッシュは選択されているが頂点が未選択の場合は全ボーンを表示する
            self._is_vertex_mode = False
            all_rows = sorted(vtx_row_dict.keys())
            self._target_rows = all_rows
            self._target_vtx_keys = [(vtx_row_dict[r][6], vtx_row_dict[r][0]) for r in all_rows if r in vtx_row_dict]
            self._target_meshes = hl_nodes
            self._build_entries_from_rows(all_rows, vtx_row_dict, model_data, weighted_only=False)
            mesh_names = ", ".join(_short_joint_name(m) for m in self._target_meshes) or "-"
            self.mode_label.setText(
                lang.Lang(
                    en="Mode: Mesh ({0} / {1} verts) - No vertex selected: showing all bones",
                    ja=u"対象: メッシュ全体（{0} / {1} 頂点） - 頂点未選択のため全ボーンを表示",
                ).output().format(mesh_names, len(all_rows))
            )
        else:
            # 何も選択されていない場合はシーン内の全ボーンを表示する
            self._is_vertex_mode = False
            scene_joints = self._collect_scene_joints()
            self._source_entries = [
                {"joint": j, "label": _short_joint_name(j), "weight": 0.0, "count": 0}
                for j in scene_joints
            ]
            self.mode_label.setText(
                lang.Lang(
                    en="Mode: No Selection - Showing all scene joints ({0})",
                    ja=u"対象: 未選択 - シーン内の全ボーンを表示（{0} 本 / 移動時はメッシュまたは頂点を選択してください）",
                ).output().format(len(scene_joints))
            )

        self._vtx_weight_map = {
            entry["joint"]: entry["weight"]
            for entry in self._source_entries
            if entry["weight"] > WEIGHT_EPSILON
        }

        self._populate_source_table(preserve_selection=prev_sources)
        self._refresh_target_candidates(preserve_selection=prev_target)
        self._update_preview_and_button()

    def _build_entries_from_rows(self, target_rows, vtx_row_dict, model_data, weighted_only):
        """対象行群からボーンごとの平均ウェイトと影響頂点数を高速集計する。"""
        long_cache = {}
        mesh_set = set()
        mesh_sum_arrays = {}
        mesh_cnt_arrays = {}
        mesh_inf_lists = {}
        model_len = len(model_data)

        # 全ボーン表示時の統計計算で極端な頂点数がある場合は均等サンプリングで軽量化する
        row_count = len(target_rows)
        step = 1
        if not weighted_only and row_count > MAX_STAT_ROWS:
            step = max(1, row_count // MAX_STAT_ROWS)

        sampled_rows = target_rows[::step] if step > 1 else target_rows
        valid_count = 0

        for row in sampled_rows:
            if row >= model_len:
                continue
            row_data = vtx_row_dict.get(row)
            if not row_data:
                continue

            row_influences = row_data[2]
            mesh_node = row_data[6]
            weight_list = model_data[row]
            mesh_set.add(mesh_node)
            valid_count += 1

            sum_arr = mesh_sum_arrays.get(mesh_node)
            if sum_arr is None:
                inf_count = len(row_influences)
                sum_arr = [0.0] * inf_count
                cnt_arr = [0] * inf_count
                mesh_sum_arrays[mesh_node] = sum_arr
                mesh_cnt_arrays[mesh_node] = cnt_arr
                mesh_inf_lists[mesh_node] = row_influences
            else:
                cnt_arr = mesh_cnt_arrays[mesh_node]

            inf_count = len(sum_arr)
            for inf_id, w in enumerate(weight_list):
                if w > WEIGHT_EPSILON and inf_id < inf_count:
                    sum_arr[inf_id] += w
                    cnt_arr[inf_id] += 1

        self._target_meshes = sorted(mesh_set)

        weight_sum_map = defaultdict(float)
        vtx_cnt_map = defaultdict(int)
        all_joints = set()

        for mesh_node, sum_arr in mesh_sum_arrays.items():
            cnt_arr = mesh_cnt_arrays[mesh_node]
            inf_list = mesh_inf_lists[mesh_node]
            for inf_id, inf_name in enumerate(inf_list):
                joint_path = _resolve_long_name_cached(inf_name, long_cache)
                all_joints.add(joint_path)
                if sum_arr[inf_id] > WEIGHT_EPSILON:
                    weight_sum_map[joint_path] += sum_arr[inf_id]
                    vtx_cnt_map[joint_path] += cnt_arr[inf_id] * step

        inv_count = 1.0 / float(valid_count) if valid_count > 0 else 0.0
        entries = []
        target_joints = weight_sum_map.keys() if weighted_only else all_joints
        for joint_path in target_joints:
            total_w = weight_sum_map.get(joint_path, 0.0)
            avg_w = total_w * inv_count
            if weighted_only and avg_w <= WEIGHT_EPSILON:
                continue
            entries.append({
                "joint": joint_path,
                "label": _short_joint_name(joint_path),
                "weight": avg_w,
                "count": min(row_count, vtx_cnt_map.get(joint_path, 0)),
            })

        # ウェイトが大きい順、同値なら名前順で並べる
        entries.sort(key=lambda item: (-item["weight"], item["label"].upper()))
        self._source_entries = entries

    def _populate_source_table(self, preserve_selection=None):
        """フィルタ文字列を適用して変更元ボーンテーブルを描画する。"""
        if not isinstance(preserve_selection, set):
            preserve_selection = set(self._get_selected_source_joints())

        tokens = [t.upper() for t in self.src_filter_edit.text().split() if t]
        filtered = []
        for entry in self._source_entries:
            name_upper = entry["label"].upper()
            if tokens and not all(tok in name_upper for tok in tokens):
                continue
            filtered.append(entry)

        suffix = "%" if self._max_weight >= 50.0 else ""
        fmt = "{0:." + str(max(0, self._decimals)) + "f}"

        self.src_table.blockSignals(True)
        self.src_table.setRowCount(len(filtered))
        for row_idx, entry in enumerate(filtered):
            name_item = QTableWidgetItem(entry["label"])
            name_item.setData(Qt.UserRole, entry["joint"])
            name_item.setToolTip(entry["joint"])

            val_text = fmt.format(entry["weight"] * self._max_weight) + suffix
            weight_item = QTableWidgetItem(val_text)
            weight_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            cnt_item = QTableWidgetItem(str(entry["count"]))
            cnt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            # ウェイトが 0 のボーンは文字色を控えめにして見やすく区別する
            if entry["weight"] <= WEIGHT_EPSILON:
                dim_color = QColor(135, 135, 135)
                name_item.setForeground(dim_color)
                weight_item.setForeground(dim_color)
                cnt_item.setForeground(dim_color)

            self.src_table.setItem(row_idx, 0, name_item)
            self.src_table.setItem(row_idx, 1, weight_item)
            self.src_table.setItem(row_idx, 2, cnt_item)

            if entry["joint"] in preserve_selection:
                self.src_table.selectRow(row_idx)
        self.src_table.blockSignals(False)
        self._update_preview_and_button()

    def _refresh_target_candidates(self, preserve_selection=None):
        """変更先ボーンの候補一覧と近傍ボーン情報を更新する。"""
        if not isinstance(preserve_selection, str):
            preserve_selection = self._get_selected_target_joint()

        mesh_joints = self._collect_mesh_influences()
        if self.show_scene_joints_chk.isChecked() or not mesh_joints:
            scene_joints = self._collect_scene_joints()
            merged = sorted(set(mesh_joints) | set(scene_joints), key=lambda p: _short_joint_name(p).upper())
            self._target_joints = merged
        else:
            self._target_joints = mesh_joints

        # 頂点選択中は 3D 距離が近いボーン上位を算出して選びやすくする
        self._nearby_joints = []
        if self._is_vertex_mode and self._target_vtx_keys and self._target_joints:
            origin_pt = _compute_centroid_from_vtx_keys(self._target_vtx_keys)
            exclude = set(self._vtx_weight_map.keys())
            self._nearby_joints = _find_nearby_joints(origin_pt, self._target_joints, exclude)

        self._populate_target_list(preserve_selection=preserve_selection)

    def _build_ordered_target_items(self):
        """表示優先設定に応じて (ジョイントパス, タグ種別) の順序リストを返す。"""
        if not self.prioritize_near_chk.isChecked() or not self._is_vertex_mode:
            return [(j, "normal") for j in self._target_joints]

        target_set = set(self._target_joints)
        ordered = []
        added = set()

        # 選択頂点に現在ウェイトが入っているボーンを大きい順で最上位へ配置
        vtx_joints = sorted(
            [j for j in self._vtx_weight_map.keys() if j in target_set],
            key=lambda j: (-self._vtx_weight_map[j], _short_joint_name(j).upper()),
        )
        for j in vtx_joints:
            ordered.append((j, "vtx"))
            added.add(j)

        # 選択頂点から 3D 空間距離が近いボーンを次に配置
        for j in self._nearby_joints:
            if j in target_set and j not in added:
                ordered.append((j, "near"))
                added.add(j)

        # 残りのボーンを名前順で配置
        for j in self._target_joints:
            if j not in added:
                ordered.append((j, "normal"))
                added.add(j)

        return ordered

    def _populate_target_list(self, preserve_selection=None):
        """フィルタ文字列を適用して変更先ボーンリストを描画する。"""
        if not isinstance(preserve_selection, str):
            preserve_selection = self._get_selected_target_joint()

        tokens = [t.upper() for t in self.dst_filter_edit.text().split() if t]
        bound_set = set(self._collect_mesh_influences())
        ordered_items = self._build_ordered_target_items()

        suffix = "%" if self._max_weight >= 50.0 else ""
        fmt = "{0:." + str(max(0, self._decimals)) + "f}"

        self.dst_list.blockSignals(True)
        self.dst_list.clear()
        for joint_path, tag in ordered_items:
            short_name = _short_joint_name(joint_path)
            name_upper = short_name.upper()
            if tokens and not all(tok in name_upper for tok in tokens):
                continue

            prefix = ""
            extra = ""
            if tag == "vtx":
                w_val = self._vtx_weight_map.get(joint_path, 0.0)
                prefix = "[Vtx] "
                extra = "  ({0}{1})".format(fmt.format(w_val * self._max_weight), suffix)
            elif tag == "near":
                prefix = "[Near] "

            bind_mark = "" if (not bound_set or joint_path in bound_set) else "  [+Bind]"
            display_name = "{0}{1}{2}{3}".format(prefix, short_name, extra, bind_mark)

            item = QListWidgetItem(display_name)
            item.setData(Qt.UserRole, joint_path)
            item.setToolTip(joint_path)
            if tag == "vtx":
                item.setForeground(QColor(145, 225, 255))
            elif tag == "near":
                item.setForeground(QColor(180, 235, 175))
            self.dst_list.addItem(item)
            if preserve_selection and joint_path == preserve_selection:
                item.setSelected(True)
                self.dst_list.scrollToItem(item)
        self.dst_list.blockSignals(False)
        self._update_preview_and_button()
        self._sync_target_highlight()

    def _select_target_joint(self, target_joint, source_desc=None):
        """指定ジョイントを変更先リスト上で確実に選択状態にする。"""
        if target_joint not in self._target_joints:
            self.show_scene_joints_chk.blockSignals(True)
            self.show_scene_joints_chk.setChecked(True)
            self.show_scene_joints_chk.blockSignals(False)
            self._refresh_target_candidates(preserve_selection=target_joint)

        self.dst_filter_edit.clear()
        for idx in range(self.dst_list.count()):
            item = self.dst_list.item(idx)
            if item.data(Qt.UserRole) == target_joint:
                self.dst_list.setCurrentItem(item)
                self.dst_list.scrollToItem(item)
                short_name = _short_joint_name(target_joint)
                if source_desc:
                    msg = lang.Lang(
                        en="Target set from {0}: {1}",
                        ja=u"{0}から変更先に設定しました: {1}",
                    ).output().format(source_desc, short_name)
                else:
                    msg = lang.Lang(
                        en="Target set: {0}",
                        ja=u"変更先に設定しました: {0}",
                    ).output().format(short_name)
                self._set_status(msg, error=False)
                return

    def _pick_target_from_selected_vertices(self):
        """ビューポートで現在選択中の頂点に入っているボーンから変更先を取得する。"""
        bones, vtx_count = _collect_weighted_bones_from_viewport_vertices()
        if not bones:
            self._set_status(
                lang.Lang(
                    en="No weighted bones found on currently selected vertices.",
                    ja=u"現在選択中の頂点にウェイト設定済みボーンが見つかりませんでした。",
                ).output(),
                error=True,
            )
            return

        source_desc = lang.Lang(en="vertex", ja=u"選択頂点").output()
        if len(bones) == 1:
            self._select_target_joint(bones[0][0], source_desc=source_desc)
            return

        # 複数ボーンが入っている頂点の場合はウェイト順メニューから 1 クリックで選択できるようにする
        menu = QMenu(self)
        suffix = "%" if self._max_weight >= 50.0 else ""
        fmt = "{0:." + str(max(0, self._decimals)) + "f}"
        selected_sources = set(self._get_selected_source_joints())

        title_act = menu.addAction(
            lang.Lang(
                en="Pick bone from selected verts ({0})",
                ja=u"選択頂点のボーンから変更先を選択（{0} 頂点）",
            ).output().format(vtx_count)
        )
        title_act.setEnabled(False)
        menu.addSeparator()

        for joint_path, avg_w in bones:
            short_name = _short_joint_name(joint_path)
            val_str = fmt.format(avg_w * self._max_weight) + suffix
            mark = "  [Source]" if joint_path in selected_sources else ""
            action = menu.addAction("{0}  ({1}){2}".format(short_name, val_str, mark))
            action.triggered.connect(
                lambda checked=False, jp=joint_path: self._select_target_joint(jp, source_desc=source_desc)
            )

        exec_func = getattr(menu, "exec_", None) or getattr(menu, "exec")
        exec_func(QCursor.pos())

    def _pick_selected_joint_as_target(self):
        """ビューポートまたはアウトライナーで選択中のジョイントを変更先として選択する。"""
        sel_joints = cmds.ls(sl=True, type="joint", long=True) or []
        if not sel_joints:
            self._set_status(
                lang.Lang(
                    en="Please select a joint in the viewport or outliner.",
                    ja=u"ビューポートまたはアウトライナーでジョイントを 1 つ選択してください。",
                ).output(),
                error=True,
            )
            return
        self._select_target_joint(sel_joints[0], source_desc=lang.Lang(en="joint", ja=u"選択ボーン").output())

    def _on_source_selection_changed(self):
        """変更元ペインの選択変更時にアクティブペインを記録してプレビューを更新する。"""
        if self._get_selected_source_joints():
            self._last_active_pane = "source"
        self._update_preview_and_button()

    def _on_target_selection_changed(self):
        """変更先ペインの選択変更時にアクティブペイン・プレビュー・ビューポートハイライトを更新する。"""
        if self._get_selected_target_joint():
            self._last_active_pane = "target"
        self._update_preview_and_button()
        self._sync_target_highlight()

    def _sync_add_weight_spinbox(self):
        """表示単位（0-1 / 0-100）および小数点桁数に合わせて追加値スピンボックスを同期する。"""
        if not hasattr(self, "add_weight_spin"):
            return
        self.add_weight_spin.blockSignals(True)
        try:
            decimals = max(2, self._decimals) if self._max_weight < 50.0 else max(1, self._decimals)
            self.add_weight_spin.setDecimals(decimals)
            if self._max_weight >= 50.0:
                self.add_weight_spin.setRange(0.01, 100.0)
                self.add_weight_spin.setSingleStep(5.0)
                self.add_weight_spin.setSuffix("%")
            else:
                self.add_weight_spin.setRange(0.0001, 1.0)
                self.add_weight_spin.setSingleStep(0.05)
                self.add_weight_spin.setSuffix("")
            self.add_weight_spin.setValue(self._add_weight_normalized * self._max_weight)
        finally:
            self.add_weight_spin.blockSignals(False)

    def _on_add_weight_spin_changed(self, value):
        """追加ウェイト値のスピンボックス変更を内部正規化値へ反映する。"""
        if self._max_weight > 0.0:
            self._add_weight_normalized = max(0.0001, min(1.0, float(value) / self._max_weight))
        self._update_add_preview_and_button()

    def _get_add_target_joints(self):
        """変更元・変更先のどちらからでも直近選択したボーンを追加対象として返す。"""
        src_joints = self._get_selected_source_joints()
        dst_joint = self._get_selected_target_joint()
        if self._last_active_pane == "source" and src_joints:
            return src_joints, "source"
        if self._last_active_pane == "target" and dst_joint:
            return [dst_joint], "target"
        if dst_joint:
            return [dst_joint], "target"
        if src_joints:
            return src_joints, "source"
        return [], None

    def _update_add_preview_and_button(self):
        """ウェイト追加プレビューと追加ボタンの有効状態を更新する。"""
        if not hasattr(self, "add_preview_label"):
            return
        add_joints, pane = self._get_add_target_joints()
        active_keys = self._locked_vtx_keys if self._is_locked else self._target_vtx_keys
        has_targets = bool(active_keys)

        if not add_joints:
            self.add_preview_label.setText(
                lang.Lang(
                    en="Select bone(s) from Source or Target to add",
                    ja=u"左（変更元）または右（変更先）から追加するボーンを選択",
                ).output()
            )
            self.add_but.setEnabled(False)
            return

        names = ", ".join(_short_joint_name(j) for j in add_joints[:3])
        if len(add_joints) > 3:
            names += " (+{0})".format(len(add_joints) - 3)
        pane_tag = (
            lang.Lang(en="Source", ja=u"変更元").output()
            if pane == "source"
            else lang.Lang(en="Target", ja=u"変更先").output()
        )
        lock_prefix = lang.Lang(en="[Locked] ", ja=u"[固定] ").output() if self._is_locked else ""
        scope_text = (
            lang.Lang(en="{0}{1} verts / skip weighted", ja=u"{0}{1} 頂点・重複スルー").output().format(
                lock_prefix, len(active_keys)
            )
            if has_targets
            else lang.Lang(en="no target mesh", ja=u"対象メッシュなし").output()
        )
        self.add_preview_label.setText(
            lang.Lang(
                en="Add [{0}]: {1} ({2})",
                ja=u"追加[{0}]: {1}（{2}）",
            ).output().format(pane_tag, names, scope_text)
        )
        self.add_but.setEnabled(has_targets)

    def _update_preview_and_button(self):
        """選択中の変更元・変更先ボーンからプレビュー表示と実行ボタン状態を更新する。"""
        src_joints = self._get_selected_source_joints()
        dst_joint = self._get_selected_target_joint()
        active_keys = self._locked_vtx_keys if self._is_locked else self._target_vtx_keys
        has_targets = bool(active_keys)

        if not src_joints or not dst_joint:
            self.preview_label.setText(
                lang.Lang(
                    en="Select source bone(s) and target bone",
                    ja=u"左から変更元ボーン、右から変更先ボーンを選択してください",
                ).output()
            )
            self.move_but.setEnabled(False)
            self._update_add_preview_and_button()
            return

        valid_sources = [j for j in src_joints if j != dst_joint]
        if not valid_sources:
            self.preview_label.setText(
                lang.Lang(
                    en="Source and target bone are the same",
                    ja=u"変更元と変更先に同じボーンが選ばれています",
                ).output()
            )
            self.move_but.setEnabled(False)
            self._update_add_preview_and_button()
            return

        src_names = ", ".join(_short_joint_name(j) for j in valid_sources[:3])
        if len(valid_sources) > 3:
            src_names += " (+{0})".format(len(valid_sources) - 3)
        dst_name = _short_joint_name(dst_joint)

        lock_prefix = lang.Lang(en="[Locked] ", ja=u"[固定] ").output() if self._is_locked else ""
        scope_text = (
            lang.Lang(en="{0}{1} verts", ja=u"{0}{1} 頂点").output().format(lock_prefix, len(active_keys))
            if has_targets
            else lang.Lang(en="no target mesh", ja=u"対象メッシュなし").output()
        )
        self.preview_label.setText("{0}  →  {1}  ({2})".format(src_names, dst_name, scope_text))
        self.move_but.setEnabled(has_targets)
        self._update_add_preview_and_button()

    def _set_status(self, message, error=False):
        """ウィンドウ下部のステータス表示を更新する。"""
        self.status_label.setText(message)
        if error:
            qt.change_button_color(self.status_label, textColor=[250, 128, 64])
        else:
            qt.change_button_color(self.status_label, textColor=[150, 230, 160])

    def execute_remap(self):
        """変更元ボーンのウェイト値を変更先ボーンへ移動し、Undo 対応で焼き込む。"""
        editor = self.editor
        if editor is None or getattr(editor, "closed_flag", False):
            return

        src_joints = self._get_selected_source_joints()
        dst_joint = self._get_selected_target_joint()
        if not src_joints or not dst_joint:
            return

        valid_sources = [j for j in src_joints if j != dst_joint]
        if not valid_sources:
            self._set_status(
                lang.Lang(
                    en="Source and target bone are the same.",
                    ja=u"変更元と変更先が同じボーンです。",
                ).output(),
                error=True,
            )
            return

        # 対象固定中は固定した頂点群を、通常時は現在の対象頂点群を使用する
        active_vtx_keys = list(self._locked_vtx_keys if self._is_locked else self._target_vtx_keys)
        if not active_vtx_keys:
            self._set_status(
                lang.Lang(
                    en="No target vertices found.",
                    ja=u"移動対象の頂点が見つかりません。",
                ).output(),
                error=True,
            )
            return

        target_vtx_keys = set(active_vtx_keys)
        target_meshes = set(mesh_node for mesh_node, _ in active_vtx_keys)

        cmds.undoInfo(openChunk=True)
        try:
            # 対象固定中などで本体テーブルに対象頂点が揃っていない場合は、対象頂点を再選択して読み込む
            vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}
            current_keys = set((row_data[6], row_data[0]) for row_data in vtx_row_dict.values())
            if not target_vtx_keys.issubset(current_keys):
                sel_strings = _compress_vtx_keys_to_selection_strings(active_vtx_keys)
                if sel_strings:
                    cmds.selectMode(component=True)
                    cmds.select(sel_strings, replace=True)
                    editor.get_set_skin_weight(cycle=True)
                    vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}

            # 変更先ボーンが対象メッシュに未バインドなら自動でインフルエンス追加する
            added_influence = False
            long_cache = {}
            influences_dict = getattr(editor, "influences_dict", None) or {}
            for mesh_node in target_meshes:
                mesh_infs = set(
                    _resolve_long_name_cached(inf, long_cache)
                    for inf in influences_dict.get(mesh_node, [])
                )
                if dst_joint not in mesh_infs:
                    if not cmds.objExists(dst_joint):
                        self._set_status(
                            lang.Lang(
                                en="Target joint does not exist: {0}",
                                ja=u"変更先ボーンが見つかりません: {0}",
                            ).output().format(_short_joint_name(dst_joint)),
                            error=True,
                        )
                        return
                    skin_cluster = self._resolve_skin_cluster_for_mesh(mesh_node)
                    cmds.skinCluster(skin_cluster, e=True, ai=dst_joint, lw=True, ug=False, wt=0, ps=0)
                    added_influence = True

            if added_influence:
                editor.get_set_skin_weight(cycle=True)
                vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}

            target_rows = [
                r for r, row_data in vtx_row_dict.items()
                if (row_data[6], row_data[0]) in target_vtx_keys
            ]
            data_rows = getattr(editor, "_data", None)
            if not target_rows or not data_rows:
                self._set_status(
                    lang.Lang(
                        en="No target vertices found in editor.",
                        ja=u"移動対象の頂点データを取得できませんでした。",
                    ).output(),
                    error=True,
                )
                return

            # メッシュごとのボーン名→ローカルインデックス辞書をキャッシュして高速移動する
            mesh_inf_index_map = {}
            update_rows = set()
            valid_source_set = set(valid_sources)

            for row in target_rows:
                row_data = vtx_row_dict.get(row)
                if not row_data or row >= len(data_rows):
                    continue
                mesh_node = row_data[6]
                inf_index_map = mesh_inf_index_map.get(mesh_node)
                if inf_index_map is None:
                    row_influences = row_data[2]
                    inf_index_map = {
                        _resolve_long_name_cached(inf, long_cache): idx
                        for idx, inf in enumerate(row_influences)
                    }
                    mesh_inf_index_map[mesh_node] = inf_index_map

                dst_id = inf_index_map.get(dst_joint)
                if dst_id is None:
                    continue

                src_ids = [inf_index_map[j] for j in valid_source_set if j in inf_index_map]
                if not src_ids:
                    continue

                weight_list = data_rows[row]
                moved_sum = 0.0
                for src_id in src_ids:
                    w = weight_list[src_id]
                    if w > 0.0:
                        moved_sum += w
                        weight_list[src_id] = 0.0

                if moved_sum > 0.0:
                    weight_list[dst_id] += moved_sum
                    update_rows.add(row)

            if not update_rows:
                self._set_status(
                    lang.Lang(
                        en="No non-zero weights to move for the selected source bone(s).",
                        ja=u"選択された変更元ボーンに移動可能なウェイト値がありませんでした。",
                    ).output(),
                    error=True,
                )
                return

            editor.update_rows = update_rows
            editor.row_column_dict = {r: [] for r in update_rows}
            editor.precalculate_bake_weights(skip_normalize=True)

            # Filter ON 時は変更先ボーンの列を表示・不要になった 0 列を整理するため再フィルタする
            if hasattr(editor, "filter_but") and editor.filter_but.isChecked():
                editor.get_set_skin_weight(filter=True)

            src_label = ", ".join(_short_joint_name(j) for j in valid_sources)
            dst_label = _short_joint_name(dst_joint)
            msg = lang.Lang(
                en="Moved: {0} -> {1} ({2} verts)",
                ja=u"移動完了: {0} → {1}（{2} 頂点）",
            ).output().format(src_label, dst_label, len(update_rows))
            self._set_status(msg, error=False)
            if hasattr(editor, "set_message"):
                editor.set_message(msg="- " + msg + " -", error=False)
        except Exception as exc:
            self._set_status("Error: {0}".format(exc), error=True)
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        # 実行後は最新のウェイト状態へ強制同期する（固定中も移動結果を左ペインへ反映）
        self.refresh_from_editor(force=True)

    def execute_add_weight(self):
        """選択ボーン（変更元・変更先どちらでも可）を対象頂点へ追加する（既設定頂点はスルー）。"""
        editor = self.editor
        if editor is None or getattr(editor, "closed_flag", False):
            return

        add_joints, _ = self._get_add_target_joints()
        if not add_joints:
            self._set_status(
                lang.Lang(
                    en="Select bone(s) from Source or Target list first.",
                    ja=u"左（変更元）または右（変更先）から追加するボーンを選択してください。",
                ).output(),
                error=True,
            )
            return

        active_vtx_keys = list(self._locked_vtx_keys if self._is_locked else self._target_vtx_keys)
        if not active_vtx_keys:
            self._set_status(
                lang.Lang(
                    en="No target vertices found.",
                    ja=u"追加対象の頂点が見つかりません。",
                ).output(),
                error=True,
            )
            return

        add_val = max(0.0001, min(1.0, float(self._add_weight_normalized)))
        target_vtx_keys = set(active_vtx_keys)
        target_meshes = set(mesh_node for mesh_node, _ in active_vtx_keys)
        normalize_on = bool(hasattr(editor, "norm_but") and editor.norm_but.isChecked())

        cmds.undoInfo(openChunk=True)
        try:
            # 対象固定中などで本体テーブルに対象頂点が揃っていない場合は、対象頂点を再選択して読み込む
            vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}
            current_keys = set((row_data[6], row_data[0]) for row_data in vtx_row_dict.values())
            if not target_vtx_keys.issubset(current_keys):
                sel_strings = _compress_vtx_keys_to_selection_strings(active_vtx_keys)
                if sel_strings:
                    cmds.selectMode(component=True)
                    cmds.select(sel_strings, replace=True)
                    editor.get_set_skin_weight(cycle=True)
                    vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}

            # 追加するボーンが対象メッシュに未バインドなら自動でインフルエンス追加する
            added_influence = False
            long_cache = {}
            influences_dict = getattr(editor, "influences_dict", None) or {}
            for mesh_node in target_meshes:
                mesh_infs = set(
                    _resolve_long_name_cached(inf, long_cache)
                    for inf in influences_dict.get(mesh_node, [])
                )
                missing = []
                for joint in add_joints:
                    if joint not in mesh_infs:
                        if not cmds.objExists(joint):
                            self._set_status(
                                lang.Lang(
                                    en="Joint does not exist: {0}",
                                    ja=u"ボーンが見つかりません: {0}",
                                ).output().format(_short_joint_name(joint)),
                                error=True,
                            )
                            return
                        missing.append(joint)
                if missing:
                    skin_cluster = self._resolve_skin_cluster_for_mesh(mesh_node)
                    cmds.skinCluster(skin_cluster, e=True, ai=missing, lw=True, ug=False, wt=0, ps=0)
                    added_influence = True

            if added_influence:
                editor.get_set_skin_weight(cycle=True)
                vtx_row_dict = getattr(editor, "vtx_row_dict", None) or {}

            target_rows = [
                r for r, row_data in vtx_row_dict.items()
                if (row_data[6], row_data[0]) in target_vtx_keys
            ]
            data_rows = getattr(editor, "_data", None)
            if not target_rows or not data_rows:
                self._set_status(
                    lang.Lang(
                        en="No target vertices found in editor.",
                        ja=u"追加対象の頂点データを取得できませんでした。",
                    ).output(),
                    error=True,
                )
                return

            mesh_inf_index_map = {}
            update_rows = set()
            skipped_rows = 0

            for row in target_rows:
                row_data = vtx_row_dict.get(row)
                if not row_data or row >= len(data_rows):
                    continue
                mesh_node = row_data[6]
                inf_index_map = mesh_inf_index_map.get(mesh_node)
                if inf_index_map is None:
                    row_influences = row_data[2]
                    inf_index_map = {
                        _resolve_long_name_cached(inf, long_cache): idx
                        for idx, inf in enumerate(row_influences)
                    }
                    mesh_inf_index_map[mesh_node] = inf_index_map

                weight_list = data_rows[row]
                # 既にそのボーンのウェイトが入っている頂点（被り）はスルーし、未設定分だけ抽出する
                new_add_ids = []
                for joint in add_joints:
                    j_id = inf_index_map.get(joint)
                    if j_id is not None and j_id < len(weight_list) and weight_list[j_id] <= WEIGHT_EPSILON:
                        new_add_ids.append(j_id)

                if not new_add_ids:
                    skipped_rows += 1
                    continue

                if normalize_on:
                    total_add = add_val * len(new_add_ids)
                    if total_add >= 1.0:
                        each_val = 1.0 / float(len(new_add_ids))
                        for idx in range(len(weight_list)):
                            weight_list[idx] = 0.0
                        for j_id in new_add_ids:
                            weight_list[j_id] = each_val
                    else:
                        existing_sum = sum(weight_list)
                        if existing_sum > 0.0:
                            scale_ratio = (1.0 - total_add) / existing_sum
                            for idx in range(len(weight_list)):
                                weight_list[idx] *= scale_ratio
                            for j_id in new_add_ids:
                                weight_list[j_id] = add_val
                        else:
                            each_val = 1.0 / float(len(new_add_ids))
                            for j_id in new_add_ids:
                                weight_list[j_id] = each_val
                else:
                    for j_id in new_add_ids:
                        weight_list[j_id] = add_val

                update_rows.add(row)

            if not update_rows:
                self._set_status(
                    lang.Lang(
                        en="Skipped all vertices ({0}): bone(s) already weighted.",
                        ja=u"全 {0} 頂点に既に設定済みのためスルーしました。",
                    ).output().format(skipped_rows),
                    error=False,
                )
                return

            editor.update_rows = update_rows
            editor.row_column_dict = {r: [] for r in update_rows}
            editor.precalculate_bake_weights(skip_normalize=True)

            # Filter ON 時は追加したボーン列を即座に表示へ反映するため再フィルタする
            if hasattr(editor, "filter_but") and editor.filter_but.isChecked():
                editor.get_set_skin_weight(filter=True)

            bone_label = ", ".join(_short_joint_name(j) for j in add_joints)
            msg = lang.Lang(
                en="Added: {0} ({1} verts updated, {2} skipped)",
                ja=u"追加完了: {0}（{1} 頂点に追加 / {2} 頂点スルー）",
            ).output().format(bone_label, len(update_rows), skipped_rows)
            self._set_status(msg, error=False)
            if hasattr(editor, "set_message"):
                editor.set_message(msg="- " + msg + " -", error=False)
        except Exception as exc:
            self._set_status("Error: {0}".format(exc), error=True)
            raise
        finally:
            cmds.undoInfo(closeChunk=True)

        self.refresh_from_editor(force=True)
