# -*- coding: utf-8 -*-
"""ウェイトが入っていないメッシュを一覧表示し、指定ボーンへ直感的にバインド・割り当てする専用ウィンドウ。"""

from maya import cmds

from ..maya_version import MAYA_VER
from .. import lang
from .. import qt
from . import rig_cleanup_helper

if MAYA_VER >= 2025:
    from PySide6.QtWidgets import *
    from PySide6.QtGui import *
    from PySide6.QtCore import *
elif 2017 <= MAYA_VER < 2025:
    from PySide2.QtWidgets import *
    from PySide2.QtGui import *
    from PySide2.QtCore import *
else:
    from PySide.QtGui import *
    from PySide.QtCore import *


class UnweightedMeshBindWindow(qt.SubWindow):
    """ウェイトが入っていないメッシュ一覧と割り当て先ボーン一覧を並べて確認・バインド実行できるウィンドウ。"""

    WINDOW_TITLE = "Unweighted Mesh Bind - SIWeightEditor Improve"
    WINDOW_OBJECT_NAME = "SIWeightEditorImproveUnweightedBindWindow"

    def __init__(self, owner_window=None, editor_window=None):
        super(UnweightedMeshBindWindow, self).__init__(parent=qt.get_maya_window())
        self.owner = owner_window
        self.editor = editor_window
        self.setWindowTitle(
            lang.Lang(
                en="Bind Unweighted Meshes to Bone - SIWeightEditor Improve",
                ja=u"ウェイト無しメッシュの一覧バインド - SIWeightEditor Improve",
            ).output()
        )
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(780, 560)

        self._mesh_entries = []
        self._joint_entries = []
        self._checked_meshes = set()
        self._updating_tables = False

        self._init_ui()
        self.refresh_lists(apply_viewport_selection=True)

    def _init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # 上部ガイド説明
        self.guide_label = QLabel(
            lang.Lang(
                en=(
                    "Lists visible polygon meshes in the scene that currently have no skin weights (unbound or all-zero weights; hidden objects are excluded).\n"
                    "Check target mesh(es) on the left, select destination bone(s) on the right, confirm the summary below, and execute Bind."
                ),
                ja=(
                    u"シーン内で表示中かつウェイトが 1 つも入っていないメッシュ（未バインド、または全頂点ウェイト 0・非表示オブジェクトは除外）を一覧表示します。\n"
                    u"左で対象メッシュにチェックを入れ、右で割り当て先のボーンを選択し、下部サマリーを確認してバインドを実行してください。"
                ),
            ).output()
        )
        self.guide_label.setWordWrap(True)
        qt.change_button_color(self.guide_label, textColor=[218, 224, 232], bgColor=[46, 50, 58])
        self.guide_label.setMargin(6)
        main_layout.addWidget(self.guide_label)

        # 上部アクションバー
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.rescan_but = QPushButton(
            lang.Lang(en="Rescan Scene", ja=u"シーンを再スキャン").output()
        )
        self.rescan_but.clicked.connect(lambda: self.refresh_lists(apply_viewport_selection=False))
        top_row.addWidget(self.rescan_but)

        self.sync_vp_but = QPushButton(
            lang.Lang(en="Sync from Viewport Selection", ja=u"ビューポート選択を反映").output()
        )
        self.sync_vp_but.clicked.connect(self.sync_from_viewport_selection)
        top_row.addWidget(self.sync_vp_but)

        self.include_zero_chk = QCheckBox(
            lang.Lang(
                en="Include skinned meshes with all-zero weights",
                ja=u"スキンクラスタ有りで全頂点ウェイト 0 のメッシュも含める",
            ).output()
        )
        self.include_zero_chk.setChecked(True)
        self.include_zero_chk.toggled.connect(lambda: self.refresh_lists(apply_viewport_selection=False))
        top_row.addWidget(self.include_zero_chk)
        top_row.addStretch(1)
        main_layout.addLayout(top_row)

        # 中央スプリッター（左：ウェイト無しメッシュ一覧 / 右：割り当て先ボーン一覧）
        splitter = QSplitter(Qt.Horizontal, self)
        main_layout.addWidget(splitter, 1)

        # 左ペイン：ウェイト無しメッシュ
        left_panel = QWidget(splitter)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(4)

        left_header_row = QHBoxLayout()
        self.mesh_header_label = QLabel(
            lang.Lang(en="Unweighted Meshes (0)", ja=u"ウェイト無しメッシュ (0 件)").output()
        )
        left_header_row.addWidget(self.mesh_header_label)
        left_header_row.addStretch(1)

        self.check_all_mesh_but = QPushButton(lang.Lang(en="Check All", ja=u"全選択").output())
        self.check_all_mesh_but.setMaximumHeight(22)
        self.check_all_mesh_but.clicked.connect(lambda: self._set_all_visible_meshes_checked(True))
        left_header_row.addWidget(self.check_all_mesh_but)

        self.uncheck_all_mesh_but = QPushButton(lang.Lang(en="Uncheck All", ja=u"全解除").output())
        self.uncheck_all_mesh_but.setMaximumHeight(22)
        self.uncheck_all_mesh_but.clicked.connect(lambda: self._set_all_visible_meshes_checked(False))
        left_header_row.addWidget(self.uncheck_all_mesh_but)
        left_layout.addLayout(left_header_row)

        self.mesh_filter_edit = QLineEdit(self)
        self.mesh_filter_edit.setPlaceholderText(
            lang.Lang(en="Filter meshes...", ja=u"メッシュ名で絞り込み...").output()
        )
        self.mesh_filter_edit.textChanged.connect(self._populate_mesh_table)
        left_layout.addWidget(self.mesh_filter_edit)

        self.mesh_table = QTableWidget(0, 3, self)
        self.mesh_table.setHorizontalHeaderLabels([
            lang.Lang(en="Mesh", ja=u"メッシュ名").output(),
            lang.Lang(en="Status", ja=u"状態").output(),
            lang.Lang(en="Verts", ja=u"頂点数").output(),
        ])
        self.mesh_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mesh_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.mesh_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mesh_table.setAlternatingRowColors(True)
        self.mesh_table.verticalHeader().setVisible(False)
        self.mesh_table.verticalHeader().setDefaultSectionSize(22)
        m_header = self.mesh_table.horizontalHeader()
        m_resize = getattr(m_header, "setSectionResizeMode", None) or getattr(m_header, "setResizeMode")
        m_resize(0, QHeaderView.Stretch)
        m_resize(1, QHeaderView.ResizeToContents)
        m_resize(2, QHeaderView.ResizeToContents)
        self.mesh_table.itemChanged.connect(self._on_mesh_item_changed)
        self.mesh_table.itemDoubleClicked.connect(self._on_mesh_double_clicked)
        left_layout.addWidget(self.mesh_table, 1)

        mesh_hint = QLabel(
            lang.Lang(
                en="Tip: Double-click a mesh row to select it in the viewport.",
                ja=u"ヒント: 行をダブルクリックするとビューポート上で対象メッシュを選択確認できます。",
            ).output()
        )
        mesh_hint.setStyleSheet("color: #aeb6c4; font-size: 11px;")
        left_layout.addWidget(mesh_hint)

        # 右ペイン：割り当て先ボーン一覧
        right_panel = QWidget(splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(4)

        right_header_row = QHBoxLayout()
        self.joint_header_label = QLabel(
            lang.Lang(en="Destination Bone(s) (0)", ja=u"割り当て先ボーン (0 本)").output()
        )
        right_header_row.addWidget(self.joint_header_label)
        right_header_row.addStretch(1)

        self.pick_vp_joint_but = QPushButton(
            lang.Lang(en="Get Selected Bone", ja=u"ビューポート選択を取得").output()
        )
        self.pick_vp_joint_but.setMaximumHeight(22)
        self.pick_vp_joint_but.clicked.connect(self.select_viewport_joints_in_table)
        right_header_row.addWidget(self.pick_vp_joint_but)
        right_layout.addLayout(right_header_row)

        self.joint_filter_edit = QLineEdit(self)
        self.joint_filter_edit.setPlaceholderText(
            lang.Lang(en="Filter bones...", ja=u"ボーン名で絞り込み...").output()
        )
        self.joint_filter_edit.textChanged.connect(self._populate_joint_table)
        right_layout.addWidget(self.joint_filter_edit)

        self.joint_table = QTableWidget(0, 2, self)
        self.joint_table.setHorizontalHeaderLabels([
            lang.Lang(en="Bone (Hierarchy)", ja=u"ボーン名（階層）").output(),
            lang.Lang(en="Parent", ja=u"親ボーン").output(),
        ])
        self.joint_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.joint_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.joint_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.joint_table.setAlternatingRowColors(True)
        self.joint_table.verticalHeader().setVisible(False)
        self.joint_table.verticalHeader().setDefaultSectionSize(22)
        j_header = self.joint_table.horizontalHeader()
        j_resize = getattr(j_header, "setSectionResizeMode", None) or getattr(j_header, "setResizeMode")
        j_resize(0, QHeaderView.Stretch)
        j_resize(1, QHeaderView.ResizeToContents)
        self.joint_table.itemSelectionChanged.connect(self._update_summary)
        self.joint_table.itemDoubleClicked.connect(self._on_joint_double_clicked)
        right_layout.addWidget(self.joint_table, 1)

        self.include_children_chk = QCheckBox(
            lang.Lang(
                en="Also include child bones under selected bone(s)",
                ja=u"選択ボーン配下の子ボーンもまとめて割り当て対象に含める",
            ).output()
        )
        self.include_children_chk.setChecked(False)
        self.include_children_chk.toggled.connect(self._update_summary)
        right_layout.addWidget(self.include_children_chk)

        splitter.setSizes([380, 380])

        # 下部：割り当て方式・確認サマリー・実行ボタン
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        opt_row.addWidget(QLabel(lang.Lang(en="Assign Mode:", ja=u"割り当て方式:").output()))
        self.assign_mode_combo = QComboBox(self)
        self.assign_mode_combo.addItem(
            lang.Lang(
                en="Assign 100% Weight to Selected Bone(s) (Best for accessories / rigid parts)",
                ja=u"選択ボーンへ 100% ウェイト割り当て（アクセサリー・パーツ向け・単一ボーン推奨）",
            ).output(),
            "rigid_single",
        )
        self.assign_mode_combo.addItem(
            lang.Lang(
                en="Auto-Bind by Proximity (Best for multi-bone deformable parts)",
                ja=u"近傍の自動バインド（複数ボーン・変形パーツ向け）",
            ).output(),
            "auto_bind",
        )
        self.assign_mode_combo.addItem(
            lang.Lang(
                en="Bind / Add Influence Only (No manual overwrite)",
                ja=u"バインド / インフルエンス登録のみ",
            ).output(),
            "zero_only",
        )
        self.assign_mode_combo.currentIndexChanged.connect(self._update_summary)
        opt_row.addWidget(self.assign_mode_combo, 1)

        self.unlock_inf_chk = QCheckBox(
            lang.Lang(en="Unlock influences", ja=u"ロック解除").output()
        )
        self.unlock_inf_chk.setChecked(True)
        opt_row.addWidget(self.unlock_inf_chk)
        main_layout.addLayout(opt_row)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setMargin(6)
        main_layout.addWidget(self.summary_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.exec_bind_but = QPushButton(
            lang.Lang(
                en="Bind Selected Mesh(es) to Selected Bone(s)",
                ja=u"選択メッシュを指定ボーンへバインド実行",
            ).output()
        )
        self.exec_bind_but.setMinimumHeight(32)
        self.exec_bind_but.clicked.connect(self.execute_bind)
        btn_row.addWidget(self.exec_bind_but, 2)

        self.close_but = QPushButton(lang.Lang(en="Close", ja=u"閉じる").output())
        self.close_but.setMinimumHeight(32)
        self.close_but.clicked.connect(self.close)
        btn_row.addWidget(self.close_but, 0)
        main_layout.addLayout(btn_row)

        self.status_label = QLabel("")
        self.status_label.setMargin(4)
        self.status_label.setStyleSheet("color: #aeb6c4;")
        main_layout.addWidget(self.status_label)

    def refresh_lists(self, apply_viewport_selection=False):
        """シーンを再走査してウェイト無しメッシュ一覧とボーン一覧を更新する。"""
        include_zero = self.include_zero_chk.isChecked()
        self._mesh_entries = rig_cleanup_helper.find_unweighted_meshes(
            include_zero_weight_skinned=include_zero,
            exclude_hidden=True,
        )
        self._joint_entries = rig_cleanup_helper.list_scene_joints_hierarchy()

        valid_meshes = set(e["mesh_long"] for e in self._mesh_entries)
        self._checked_meshes = set(m for m in self._checked_meshes if m in valid_meshes)

        self._populate_mesh_table()
        self._populate_joint_table()

        if apply_viewport_selection:
            self.sync_from_viewport_selection()
        else:
            self._update_summary()

    def _populate_mesh_table(self):
        keyword = (self.mesh_filter_edit.text() or "").strip().lower()
        filtered = [
            e for e in self._mesh_entries
            if not keyword or keyword in e["mesh_short"].lower() or keyword in e["mesh_long"].lower()
        ]
        self.mesh_header_label.setText(
            lang.Lang(
                en="Unweighted Meshes ({0})",
                ja=u"ウェイト無しメッシュ ({0} 件)",
            ).output().format(len(self._mesh_entries))
        )

        self._updating_tables = True
        try:
            self.mesh_table.setRowCount(len(filtered))
            for row, entry in enumerate(filtered):
                name_item = QTableWidgetItem(entry["mesh_short"])
                name_item.setFlags(name_item.flags() | Qt.ItemIsUserCheckable)
                is_chk = entry["mesh_long"] in self._checked_meshes
                name_item.setCheckState(Qt.Checked if is_chk else Qt.Unchecked)
                name_item.setData(Qt.UserRole, entry["mesh_long"])
                name_item.setToolTip(entry["mesh_long"])

                is_unbound = entry["status"] == "unbound"
                status_text = (
                    lang.Lang(en="Unbound", ja=u"未バインド").output()
                    if is_unbound
                    else lang.Lang(en="All Zero Wt", ja=u"全頂点ウェイト 0").output()
                )
                status_item = QTableWidgetItem(status_text)
                status_item.setForeground(QColor(255, 195, 110) if is_unbound else QColor(255, 150, 130))

                vtx_item = QTableWidgetItem(str(entry["vertex_count"]))
                vtx_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

                self.mesh_table.setItem(row, 0, name_item)
                self.mesh_table.setItem(row, 1, status_item)
                self.mesh_table.setItem(row, 2, vtx_item)
        finally:
            self._updating_tables = False
        self._update_summary()

    def _populate_joint_table(self):
        prev_selected = set(self._get_selected_joints())
        keyword = (self.joint_filter_edit.text() or "").strip().lower()
        filtered = [
            e for e in self._joint_entries
            if not keyword or keyword in e["joint_short"].lower() or keyword in e["joint_long"].lower()
        ]
        self.joint_header_label.setText(
            lang.Lang(
                en="Destination Bone(s) ({0})",
                ja=u"割り当て先ボーン ({0} 本)",
            ).output().format(len(self._joint_entries))
        )

        self._updating_tables = True
        try:
            self.joint_table.setRowCount(len(filtered))
            rows_to_select = []
            for row, entry in enumerate(filtered):
                indent = "  " * min(int(entry["depth"]), 12)
                disp_name = indent + entry["joint_short"]
                j_item = QTableWidgetItem(disp_name)
                j_item.setData(Qt.UserRole, entry["joint_long"])
                j_item.setToolTip(entry["joint_long"])

                p_item = QTableWidgetItem(entry["parent_short"])
                p_item.setForeground(QColor(165, 175, 190))

                self.joint_table.setItem(row, 0, j_item)
                self.joint_table.setItem(row, 1, p_item)

                if entry["joint_long"] in prev_selected:
                    rows_to_select.append(row)

            if rows_to_select:
                sel_model = self.joint_table.selectionModel()
                for r in rows_to_select:
                    idx_l = self.joint_table.model().index(r, 0)
                    idx_r = self.joint_table.model().index(r, 1)
                    sel_model.select(QItemSelection(idx_l, idx_r), QItemSelectionModel.Select)
        finally:
            self._updating_tables = False
        self._update_summary()

    def _on_mesh_item_changed(self, item):
        if self._updating_tables or item.column() != 0:
            return
        mesh_long = item.data(Qt.UserRole)
        if not mesh_long:
            return
        if item.checkState() == Qt.Checked:
            self._checked_meshes.add(mesh_long)
        else:
            self._checked_meshes.discard(mesh_long)
        self._update_summary()

    def _set_all_visible_meshes_checked(self, checked):
        self._updating_tables = True
        try:
            state = Qt.Checked if checked else Qt.Unchecked
            for row in range(self.mesh_table.rowCount()):
                item = self.mesh_table.item(row, 0)
                if item is None:
                    continue
                item.setCheckState(state)
                mesh_long = item.data(Qt.UserRole)
                if mesh_long:
                    if checked:
                        self._checked_meshes.add(mesh_long)
                    else:
                        self._checked_meshes.discard(mesh_long)
        finally:
            self._updating_tables = False
        self._update_summary()

    def _on_mesh_double_clicked(self, item):
        row = item.row()
        name_item = self.mesh_table.item(row, 0)
        if name_item is None:
            return
        mesh_long = name_item.data(Qt.UserRole)
        if mesh_long and cmds.objExists(mesh_long):
            cmds.select(mesh_long, replace=True)

    def _on_joint_double_clicked(self, item):
        row = item.row()
        j_item = self.joint_table.item(row, 0)
        if j_item is None:
            return
        jnt_long = j_item.data(Qt.UserRole)
        if jnt_long and cmds.objExists(jnt_long):
            cmds.select(jnt_long, replace=True)

    def _get_selected_joints(self):
        rows = sorted(set(idx.row() for idx in self.joint_table.selectedIndexes()))
        joints = []
        for r in rows:
            item = self.joint_table.item(r, 0)
            if item is not None:
                j_long = item.data(Qt.UserRole)
                if j_long:
                    joints.append(j_long)
        return joints

    def _get_checked_meshes(self):
        valid_order = [e["mesh_long"] for e in self._mesh_entries if e["mesh_long"] in self._checked_meshes]
        return valid_order

    def sync_from_viewport_selection(self):
        """ビューポートで現在選択中のメッシュとジョイントを左右テーブルへ反映する。"""
        sel = cmds.ls(sl=True, long=True) or []
        vp_meshes = set()
        vp_joints = set()
        for item in sel:
            node = item.split(".")[0]
            if not cmds.objExists(node):
                continue
            ntype = cmds.nodeType(node)
            if ntype == "joint":
                vp_joints.add(node)
            elif ntype == "mesh":
                parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
                if parents:
                    vp_meshes.add(parents[0])
            elif ntype == "transform":
                shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, type="mesh", fullPath=True) or []
                if shapes:
                    vp_meshes.add(node)

        valid_meshes = set(e["mesh_long"] for e in self._mesh_entries)
        matched_meshes = vp_meshes.intersection(valid_meshes)
        if matched_meshes:
            self._checked_meshes = set(matched_meshes)
            self._populate_mesh_table()

        if vp_joints:
            self._select_joints_in_table(vp_joints)

        self._update_summary()

    def select_viewport_joints_in_table(self):
        """ビューポート選択中のジョイントを右ペイン一覧で選択する。"""
        sel_joints = set(cmds.ls(sl=True, type="joint", long=True) or [])
        if not sel_joints:
            self._set_status(
                lang.Lang(en="No joint selected in viewport.", ja=u"ビューポートでジョイントが選択されていません。").output(),
                error=True,
            )
            return
        self._select_joints_in_table(sel_joints)
        self._update_summary()

    def _select_joints_in_table(self, joint_long_set):
        self.joint_table.clearSelection()
        sel_model = self.joint_table.selectionModel()
        first_row = None
        for row in range(self.joint_table.rowCount()):
            item = self.joint_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) in joint_long_set:
                idx_l = self.joint_table.model().index(row, 0)
                idx_r = self.joint_table.model().index(row, 1)
                sel_model.select(QItemSelection(idx_l, idx_r), QItemSelectionModel.Select)
                if first_row is None:
                    first_row = row
        if first_row is not None:
            self.joint_table.scrollToItem(self.joint_table.item(first_row, 0))

    def _update_summary(self):
        meshes = self._get_checked_meshes()
        joints = self._get_selected_joints()
        include_children = self.include_children_chk.isChecked()

        if not meshes and not joints:
            msg = lang.Lang(
                en="Select unweighted mesh(es) on the left and destination bone(s) on the right.",
                ja=u"左で対象メッシュにチェックを入れ、右で割り当て先のボーンを選択してください。",
            ).output()
            qt.change_button_color(self.summary_label, textColor=[235, 200, 125], bgColor=[54, 48, 38])
            self.summary_label.setText(msg)
            self.exec_bind_but.setEnabled(False)
            return

        if not meshes:
            msg = lang.Lang(
                en="No unweighted mesh checked on the left.",
                ja=u"左の一覧でバインド対象のメッシュにチェックを入れてください。",
            ).output()
            qt.change_button_color(self.summary_label, textColor=[245, 180, 110], bgColor=[54, 44, 38])
            self.summary_label.setText(msg)
            self.exec_bind_but.setEnabled(False)
            return

        if not joints:
            msg = lang.Lang(
                en="No destination bone selected on the right.",
                ja=u"右の一覧で割り当て先のボーンを選択してください。",
            ).output()
            qt.change_button_color(self.summary_label, textColor=[245, 180, 110], bgColor=[54, 44, 38])
            self.summary_label.setText(msg)
            self.exec_bind_but.setEnabled(False)
            return

        m_names = ", ".join(m.split("|")[-1] for m in meshes[:4])
        if len(meshes) > 4:
            m_names += " ..."
        j_names = ", ".join(j.split("|")[-1] for j in joints[:4])
        if len(joints) > 4:
            j_names += " ..."
        child_note = (
            lang.Lang(en=" (+children)", ja=u"（＋子階層）").output()
            if include_children
            else ""
        )
        msg = lang.Lang(
            en="Ready: {0} mesh(es) [{1}]  -->  {2} bone(s) [{3}]{4}",
            ja=u"実行内容: メッシュ {0} 件 [{1}]  →  割り当て先ボーン {2} 本 [{3}]{4}",
        ).output().format(len(meshes), m_names, len(joints), j_names, child_note)
        qt.change_button_color(self.summary_label, textColor=[165, 235, 185], bgColor=[38, 52, 44])
        self.summary_label.setText(msg)
        self.exec_bind_but.setEnabled(True)

    def _set_status(self, msg, error=False):
        self.status_label.setText(msg)
        if error:
            self.status_label.setStyleSheet("color: #ff9466;")
        else:
            self.status_label.setStyleSheet("color: #9be8a8;")

    def execute_bind(self):
        """選択メッシュを指定ボーンへバインド・ウェイト割り当てする。"""
        meshes = self._get_checked_meshes()
        joints = self._get_selected_joints()
        if not meshes or not joints:
            self._update_summary()
            return

        assign_mode = self.assign_mode_combo.currentData() or "rigid_single"
        include_children = self.include_children_chk.isChecked()
        unlock_infs = self.unlock_inf_chk.isChecked()

        with qt.undo_chunk("SIWE_BindUnweightedMeshes"):
            try:
                res = rig_cleanup_helper.bind_unweighted_meshes_to_joints(
                    target_meshes=meshes,
                    target_joints=joints,
                    assign_mode=assign_mode,
                    include_children=include_children,
                    unlock_influences=unlock_infs,
                )
            except Exception as exc:
                self._set_status(
                    lang.Lang(en="Bind error: {0}", ja=u"バインドエラー: {0}").output().format(exc),
                    error=True,
                )
                raise

        done_count = len(res["newly_bound_meshes"]) + len(res["updated_meshes"])
        short_msg = lang.Lang(
            en="Bound {0} mesh(es) ({1} verts) to {2} bone(s).",
            ja=u"バインド完了: {0} メッシュ（計 {1} 頂点）→ {2} 本のボーンへ割り当てました。",
        ).output().format(done_count, res["total_vertices"], len(res["target_joints"]))
        self._set_status(short_msg, error=False)

        if self.owner is not None:
            try:
                detail = lang.Lang(
                    en="Bound meshes: {0}\nTarget bones: {1}",
                    ja=u"バインド済みメッシュ: {0}\n割り当て先ボーン: {1}",
                ).output().format(
                    ", ".join(m.split("|")[-1] for m in (res["newly_bound_meshes"] + res["updated_meshes"])),
                    ", ".join(j.split("|")[-1] for j in res["target_joints"][:20]),
                )
                self.owner._set_status(short_msg, error=False, detail=detail)
            except Exception:
                pass

        if self.editor is not None and not getattr(self.editor, "closed_flag", False):
            try:
                self.editor.get_set_skin_weight(cycle=True)
            except Exception:
                pass

        self._checked_meshes.clear()
        self.refresh_lists(apply_viewport_selection=False)

    def closeEvent(self, event):
        if self.owner is not None and getattr(self.owner, "unweighted_bind_window", None) is self:
            self.owner.unweighted_bind_window = None
        super(UnweightedMeshBindWindow, self).closeEvent(event)
