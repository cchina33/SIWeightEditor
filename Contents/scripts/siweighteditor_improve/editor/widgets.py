# -*- coding: utf-8 -*-
"""SIWeightEditor Improve で使用するカスタム Qt ウィジェットおよびテーブルモデル群。"""

import re

from maya import cmds

from ..maya_version import MAYA_VER
from .. import lang
from .. import qt
from ..qt import *
from . import state


class EditorDoubleSpinbox(QDoubleSpinBox):
    """ホイール増減・フォーカス時全選択を備えたカスタム浮動小数点スピンボックス。"""

    wheeled = Signal()
    focused = Signal()
    keypressed = Signal()
    mousepressed = Signal()
    wheel_flag = False

    def __init__(self, parent=None):
        super(EditorDoubleSpinbox, self).__init__(parent)
        self.installEventFilter(self)

    def wheelEvent(self, e):
        pass

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.sel_all_input()
            self.focused.emit()
        if event.type() == QEvent.Wheel:
            raw_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else event.delta()
            if raw_delta != 0:
                delta = raw_delta / abs(raw_delta)
                shift_mod = self.check_shift_modifiers()
                ctrl_mod = self.check_ctrl_modifiers()
                max_wt = state.MAXIMUM_WEIGHT
                if shift_mod:
                    self.setValue(self.value() + delta * 0.001 * max_wt)
                elif ctrl_mod:
                    self.setValue(self.value() + delta * 0.1 * max_wt)
                else:
                    self.setValue(self.value() + delta * 0.01 * max_wt)
                self.wheel_flag = True
                cmds.evalDeferred(self.emit_wheel_event)
        if event.type() == QEvent.KeyPress:
            self.keypressed.emit()
        if event.type() == QEvent.MouseButtonPress:
            self.mousepressed.emit()
        return False

    def emit_wheel_event(self):
        self.wheeled.emit()
        self.wheel_flag = False

    def sel_all_input(self):
        cmds.evalDeferred(self.select_box_all)

    def select_box_all(self):
        try:
            self.selectAll()
        except Exception:
            pass

    def check_shift_modifiers(self):
        mods = QApplication.keyboardModifiers()
        return bool(mods & Qt.ShiftModifier)

    def check_ctrl_modifiers(self):
        mods = QApplication.keyboardModifiers()
        return bool(mods & Qt.ControlModifier)


class WeightSlider(QSlider):
    """ドラッグ以外のバークリックやキー・ホイール操作による単発変更を検出するスライダー。"""

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super(WeightSlider, self).__init__(orientation, parent)
        self._pending_one_shot = False
        self.actionTriggered.connect(self._on_action_triggered)

    def _on_action_triggered(self, action):
        if not self.isSliderDown():
            self._pending_one_shot = True

    def consume_one_shot(self):
        """単発操作フラグを取得してクリアする。"""
        flag = self._pending_one_shot
        self._pending_one_shot = False
        return flag


class EditorLineEdit(QLineEdit):
    """Ctrl / Shift 押下時にフォーカスが飛ばないカスタムラインエディット。"""

    modifired = Signal()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Control or key == Qt.Key.Key_Shift:
            self.modifired.emit()
            return
        super(EditorLineEdit, self).keyPressEvent(event)


class JointSearchDialog(QDialog):
    """現在のインフルエンス一覧から検索対象ジョイントを絞り込み・選択する非モーダルダイアログ。"""

    def __init__(self, influences, selected_names=None, nearby_names=None, weight_stats=None, parent=None):
        if parent is None:
            parent = qt.get_maya_window()
        super(JointSearchDialog, self).__init__(parent)
        self.setWindowTitle(lang.Lang(en="Joints Search", ja=u"ジョイント検索").output())
        self.setWindowFlag(Qt.Window, True)
        self.resize(390, 500)
        self._syncing_list = False
        self.filter_scope = "all"  # all / weighted / selected
        self.side_scope = "all"    # all / L / R / C
        self.selected_names = set(self._strip_query_prefix(name) for name in (selected_names or []) if name)
        self.nearby_names = set(nearby_names or [])
        self.weight_stats = weight_stats or {}
        self.influences = sorted(influences, key=lambda inf: inf.split("|")[-1].upper())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ダイアログ内リアルタイム検索バー
        filter_text_layout = QHBoxLayout()
        filter_text_layout.setSpacing(4)
        filter_text_layout.addWidget(QLabel(lang.Lang(en="Filter:", ja=u"絞り込み:").output()))
        self.filter_line = QLineEdit(self)
        self.filter_line.setPlaceholderText(
            lang.Lang(
                en="Filter by name (space-separated, -exclude)",
                ja=u"名前で絞り込み（スペース区切り・-除外対応）",
            ).output()
        )
        self.filter_clear_but = QPushButton("C")
        self.filter_clear_but.setToolTip(lang.Lang(en="Clear filter text", ja=u"絞り込み文字をクリア").output())
        self.filter_clear_but.setMaximumWidth(24)
        filter_text_layout.addWidget(self.filter_line)
        filter_text_layout.addWidget(self.filter_clear_but)
        layout.addLayout(filter_text_layout)

        # 範囲フィルタ（All / Weighted / Selected）
        scope_layout = QHBoxLayout()
        scope_layout.setSpacing(4)
        self.all_but = QPushButton(lang.Lang(en="All", ja=u"すべて").output())
        self.weighted_but = QPushButton(lang.Lang(en="Weighted", ja=u"ウェイトあり").output())
        self.nearby_but = QPushButton(lang.Lang(en="Selected", ja=u"選択関連").output())
        for but in (self.all_but, self.weighted_but, self.nearby_but):
            but.setCheckable(True)
            scope_layout.addWidget(but)
        self.all_but.setChecked(True)
        layout.addLayout(scope_layout)

        # 左右フィルタ（All Sides / L・Left / R・Right / Center）
        side_layout = QHBoxLayout()
        side_layout.setSpacing(4)
        self.side_all_but = QPushButton(lang.Lang(en="All Sides", ja=u"左右すべて").output())
        self.side_l_but = QPushButton(lang.Lang(en="L / Left", ja=u"左 (L/Left)").output())
        self.side_r_but = QPushButton(lang.Lang(en="R / Right", ja=u"右 (R/Right)").output())
        self.side_c_but = QPushButton(lang.Lang(en="Center", ja=u"中央").output())
        for but in (self.side_all_but, self.side_l_but, self.side_r_but, self.side_c_but):
            but.setCheckable(True)
            side_layout.addWidget(but)
        self.side_all_but.setChecked(True)
        layout.addLayout(side_layout)

        # ボーン一覧リスト
        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.list_widget)

        # オプション・補助操作行
        opt_layout = QHBoxLayout()
        opt_layout.setSpacing(6)
        self.exact_chk = QCheckBox(lang.Lang(en="Exact Match (=)", ja=u"厳密一致 (=)").output())
        self.exact_chk.setChecked(True)
        self.exact_chk.setToolTip(
            lang.Lang(
                en="Send as =BoneName for exact match so similar derived bone names are not included",
                ja=u"ON にすると =ボーン名 で厳密一致指定し、似た名前の派生ボーンを巻き込みません",
            ).output()
        )
        self.picker_but = QPushButton(lang.Lang(en="Bone Picker...", ja=u"ボーンピッカー...").output())
        self.picker_but.setToolTip(
            lang.Lang(
                en="Open Anim Picker style visual bone picker window (select, add to search, 3D highlight)",
                ja=u"Anim Picker 風の図柄ウィンドウを開いてボーン選択・検索追加・3D ハイライトを行います",
            ).output()
        )
        self.from_scene_but = QPushButton(lang.Lang(en="From Scene", ja=u"シーン選択").output())
        self.from_scene_but.setToolTip(
            lang.Lang(
                en="Select joints currently selected in the 3D viewport or Outliner",
                ja=u"3D ビューやアウトライナで選択中のジョイントを一覧で選択します",
            ).output()
        )
        self.refresh_but = QPushButton(lang.Lang(en="Refresh", ja=u"再読込").output())
        self.refresh_but.setToolTip(
            lang.Lang(
                en="Reload joint list and weight statistics from the current table",
                ja=u"現在のテーブル状態からボーン一覧とウェイト統計を再読み込みします",
            ).output()
        )
        opt_layout.addWidget(self.exact_chk)
        opt_layout.addStretch()
        opt_layout.addWidget(self.picker_but)
        opt_layout.addWidget(self.from_scene_but)
        opt_layout.addWidget(self.refresh_but)
        layout.addLayout(opt_layout)

        # 検索反映ボタン行（Replace / Add / Clear / Close）
        button_layout = QHBoxLayout()
        button_layout.setSpacing(4)
        self.replace_but = QPushButton(lang.Lang(en="Replace", ja=u"置換").output())
        self.replace_but.setToolTip(
            lang.Lang(
                en="Replace the search bar with selected joints (clears search bar if none selected)",
                ja=u"選択中のボーンで検索欄を上書きします（未選択なら検索欄をクリア）",
            ).output()
        )
        self.add_but = QPushButton(lang.Lang(en="Add", ja=u"追加").output())
        self.add_but.setToolTip(
            lang.Lang(
                en="Append selected joints to the current search bar",
                ja=u"選択中のボーンを現在の検索欄へ追加します",
            ).output()
        )
        self.clear_sel_but = QPushButton(lang.Lang(en="Clear Sel", ja=u"選択解除").output())
        self.close_but = QPushButton(lang.Lang(en="Close", ja=u"閉じる").output())
        button_layout.addWidget(self.replace_but)
        button_layout.addWidget(self.add_but)
        button_layout.addWidget(self.clear_sel_but)
        button_layout.addStretch()
        button_layout.addWidget(self.close_but)
        layout.addLayout(button_layout)

        # シグナル接続
        self.filter_line.textChanged.connect(self.populate_list)
        self.filter_clear_but.clicked.connect(lambda: self.filter_line.setText(""))
        self.all_but.clicked.connect(lambda: self.set_filter_scope("all"))
        self.weighted_but.clicked.connect(lambda: self.set_filter_scope("weighted"))
        self.nearby_but.clicked.connect(lambda: self.set_filter_scope("selected"))
        self.side_all_but.clicked.connect(lambda: self.set_side_scope("all"))
        self.side_l_but.clicked.connect(lambda: self.set_side_scope("L"))
        self.side_r_but.clicked.connect(lambda: self.set_side_scope("R"))
        self.side_c_but.clicked.connect(lambda: self.set_side_scope("C"))
        self.list_widget.itemSelectionChanged.connect(self._on_list_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.picker_but.clicked.connect(self.open_bone_picker)
        self.from_scene_but.clicked.connect(self.select_from_scene)
        self.refresh_but.clicked.connect(self.refresh_from_parent)
        self.replace_but.clicked.connect(self.replace_selection)
        self.add_but.clicked.connect(self.append_selection)
        self.clear_sel_but.clicked.connect(self.clear_list_selection)
        self.close_but.clicked.connect(self.close)

        self._update_scope_buttons()
        self.populate_list()

    def showEvent(self, event):
        super(JointSearchDialog, self).showEvent(event)
        qt.attach_maya_window_owner(self)

    @staticmethod
    def _strip_query_prefix(token):
        """検索クエリ接頭辞（= / + / - / ! / クォート）を除いたボーン名を返す。"""
        text = token.strip()
        while text and text[0] in ("=", "+", "-", "!"):
            text = text[1:]
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1]
        return text

    @staticmethod
    def _detect_bone_side(short_name):
        """ボーン名から左右・中央（L / R / C）を判定する（Left / Right キャメルケース・数字付き対応）。"""
        base = short_name.split("|")[-1].split(":")[-1]
        upper = base.upper()
        has_left = bool(re.search(r"(?:^|[^A-Z])LEFT(?:[^A-Z]|$)|(?<=[a-z0-9_])Left(?=[A-Z0-9_.]|$)|^Left(?=[A-Z0-9_.]|$)", base))
        has_right = bool(re.search(r"(?:^|[^A-Z])RIGHT(?:[^A-Z]|$)|(?<=[a-z0-9_])Right(?=[A-Z0-9_.]|$)|^Right(?=[A-Z0-9_.]|$)", base))
        if has_left and not has_right:
            return "L"
        if has_right and not has_left:
            return "R"
        if re.search(r"(?:^|[_\.\-])L(?:[_\.\-]|[0-9]+$|$)", upper):
            return "L"
        if re.search(r"(?:^|[_\.\-])R(?:[_\.\-]|[0-9]+$|$)", upper):
            return "R"
        if re.search(r"(?<=[a-z])L(?:[0-9]*)$", base):
            return "L"
        if re.search(r"(?<=[a-z])R(?:[0-9]*)$", base):
            return "R"
        return "C"

    @staticmethod
    def _match_filter_text(short_name, filter_text):
        """ダイアログ内の簡易フィルタ文字列（スペース区切り・-除外）でボーン名を判定する。"""
        tokens = [t for t in filter_text.strip().split(" ") if t]
        if not tokens:
            return True
        upper_name = short_name.upper()
        positives = []
        negatives = []
        for token in tokens:
            if len(token) > 1 and token[0] in ("-", "!"):
                negatives.append(token[1:].upper())
            else:
                positives.append(token.upper())
        for neg in negatives:
            if neg in upper_name:
                return False
        if positives and not all(pos in upper_name for pos in positives):
            return False
        return True

    def _update_scope_buttons(self):
        self.all_but.setChecked(self.filter_scope == "all")
        self.weighted_but.setChecked(self.filter_scope == "weighted")
        self.nearby_but.setChecked(self.filter_scope == "selected")
        self.nearby_but.setEnabled(bool(self.nearby_names))
        if self.filter_scope == "selected" and not self.nearby_names:
            self.filter_scope = "all"
            self.all_but.setChecked(True)
            self.nearby_but.setChecked(False)

        self.side_all_but.setChecked(self.side_scope == "all")
        self.side_l_but.setChecked(self.side_scope == "L")
        self.side_r_but.setChecked(self.side_scope == "R")
        self.side_c_but.setChecked(self.side_scope == "C")

    def set_filter_scope(self, scope):
        self.filter_scope = scope
        self._update_scope_buttons()
        self.populate_list()

    def set_side_scope(self, side):
        self.side_scope = side
        self._update_scope_buttons()
        self.populate_list()

    def update_data(self, influences, selected_names=None, nearby_names=None, weight_stats=None, preserve_selection=True):
        """メインウィンドウ側の最新状態に合わせて一覧とウェイト統計を更新する。"""
        if not preserve_selection and selected_names is not None:
            self.selected_names = set(self._strip_query_prefix(name) for name in selected_names if name)
        self.nearby_names = set(nearby_names or [])
        self.weight_stats = weight_stats or {}
        self.influences = sorted(influences, key=lambda inf: inf.split("|")[-1].upper())
        self._update_scope_buttons()
        self.populate_list()

    def _on_list_selection_changed(self):
        if self._syncing_list:
            return
        # 現在表示されているアイテムの選択状態だけを selected_names へ反映する
        for idx in range(self.list_widget.count()):
            item = self.list_widget.item(idx)
            short_name = item.data(Qt.UserRole)
            if item.isSelected():
                self.selected_names.add(short_name)
            else:
                self.selected_names.discard(short_name)

    def _on_item_double_clicked(self, item):
        short_name = item.data(Qt.UserRole)
        if not short_name:
            return
        parent = self.parent()
        if parent and hasattr(parent, "set_joint_names_to_search"):
            tokens = self._format_tokens([short_name])
            parent.set_joint_names_to_search(tokens)

    def populate_list(self):
        filter_text = self.filter_line.text() if hasattr(self, "filter_line") else ""
        max_wt = state.MAXIMUM_WEIGHT
        fmt = state.FLOAT_FORMAT
        self._syncing_list = True
        try:
            self.list_widget.clear()
            for influence in self.influences:
                short_name = influence.split("|")[-1]
                stat = self.weight_stats.get(influence) or self.weight_stats.get(short_name)
                has_weight = bool(stat and stat.get("count", 0) > 0)

                if self.filter_scope == "weighted" and not has_weight:
                    continue
                if self.filter_scope == "selected" and short_name not in self.nearby_names:
                    continue
                if self.side_scope != "all" and self._detect_bone_side(short_name) != self.side_scope:
                    continue
                if not self._match_filter_text(short_name, filter_text):
                    continue

                if has_weight:
                    max_disp = fmt.format(stat["max"] * max_wt)
                    label = "{0}   [Max: {1} / {2}vtx]".format(short_name, max_disp, stat["count"])
                else:
                    label = short_name

                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, short_name)
                item.setData(Qt.UserRole + 1, influence)
                item.setToolTip(influence)
                if has_weight:
                    item.setForeground(QColor(235, 240, 210))
                else:
                    item.setForeground(QColor(150, 150, 150))
                self.list_widget.addItem(item)
                if short_name in self.selected_names:
                    item.setSelected(True)
        finally:
            self._syncing_list = False

    def _format_tokens(self, names):
        """Exact Match 設定に応じて検索欄用トークンへ整形する。"""
        if self.exact_chk.isChecked():
            return ["=" + name for name in names]
        return list(names)

    def get_selected_names(self):
        """表示中リストでの選択順を優先しつつ選択済みボーン名一覧を返す。"""
        visible_selected = [item.data(Qt.UserRole) for item in self.list_widget.selectedItems()]
        result = list(visible_selected)
        for name in sorted(self.selected_names, key=lambda s: s.upper()):
            if name not in result:
                result.append(name)
        return result

    def replace_selection(self):
        parent = self.parent()
        if parent and hasattr(parent, "set_joint_names_to_search"):
            names = self.get_selected_names()
            parent.set_joint_names_to_search(self._format_tokens(names))

    def append_selection(self):
        parent = self.parent()
        if parent and hasattr(parent, "append_joint_names_to_search"):
            names = self.get_selected_names()
            parent.append_joint_names_to_search(self._format_tokens(names))

    def clear_list_selection(self):
        self.selected_names.clear()
        self._syncing_list = True
        try:
            self.list_widget.clearSelection()
        finally:
            self._syncing_list = False

    def select_from_scene(self):
        """Maya シーン上で選択中のジョイントをリストで選択状態にする。"""
        sel_joints = cmds.ls(sl=True, type="joint", l=True) or []
        if not sel_joints:
            return
        short_set = set(j.split("|")[-1] for j in sel_joints)
        available = set(inf.split("|")[-1] for inf in self.influences)
        matched = short_set & available
        if not matched:
            return
        self.selected_names = set(matched)
        self.populate_list()

    def open_bone_picker(self):
        parent = self.parent()
        if parent and hasattr(parent, "open_bone_picker_window"):
            parent.open_bone_picker_window()

    def refresh_from_parent(self):
        parent = self.parent()
        if parent and hasattr(parent, "update_joint_search_dialog"):
            parent.update_joint_search_dialog(force=True)


class EditorSpinbox(QSpinBox):
    """フォーカス時に数値を全選択するカスタム整数スピンボックス。"""

    def __init__(self, parent=None):
        super(EditorSpinbox, self).__init__(parent)
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.sel_all_input()
        return False

    def sel_all_input(self):
        cmds.evalDeferred(self.select_box_all)

    def select_box_all(self):
        try:
            self.selectAll()
        except Exception:
            pass


class PopInputBox(qt.SubWindow):
    """セル右クリック・直接キー入力時にカーソル位置へ表示する小型入力ウィンドウ。"""

    closed = Signal()

    def __init__(self, parent=None, value=0.0, mode=0, direct=False, pos=None):
        super(PopInputBox, self).__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        if direct:
            self.input = qt.LineEdit(self)
            self.setCentralWidget(self.input)
            self.input.setText(value)
        else:
            self.input = QDoubleSpinBox(self)
            self.input.setButtonSymbols(QAbstractSpinBox.NoButtons)
            self.setCentralWidget(self.input)

            max_wt = state.MAXIMUM_WEIGHT
            if mode == 0:
                self.input.setDecimals(state.FLOAT_DECIMALS)
                self.input.setRange(0, max_wt * 10)
                self.input.setValue(value)
            elif mode == 1:
                self.input.setDecimals(state.FLOAT_DECIMALS)
                self.input.setRange(-1 * max_wt * 10, max_wt * 10)
                self.input.setValue(value)
            elif mode == 2:
                self.input.setDecimals(1)
                self.input.setRange(-999, 999)
            self.input.selectAll()

        pos = QCursor.pos()
        self.resize(50, 24)
        self.move(pos.x() - 20, pos.y() - 12)

        self.input.editingFinished.connect(self.close)
        self.show()

        self.activateWindow()
        self.raise_()
        self.input.setFocus()

    def closeEvent(self, e):
        self.closed.emit()


class RightClickTableView(QTableView):
    """右クリックおよび直接キー入力シグナルを発行するテーブルビュー。"""

    rightClicked = Signal()
    keyPressed = Signal(str)
    ignore_key_input = False

    def mouseReleaseEvent(self, e):
        self.mouse_pos = QCursor.pos()
        if e.button() == Qt.RightButton:
            self.rightClicked.emit()
        else:
            super(RightClickTableView, self).mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        self.keyPressed.emit(e.text())
        if self.ignore_key_input:
            return
        super(RightClickTableView, self).keyPressEvent(e)


class MyHeaderView(QHeaderView):
    """ボーン名を 90 度縦書き表示するカスタム水平ヘッダー。"""

    doubleClicked = Signal(int)
    rightClicked = Signal()
    active_section_list = []

    def __init__(self, parent=None):
        super(MyHeaderView, self).__init__(Qt.Horizontal, parent)
        self._font = QFont("helvetica", 9)
        self._actived_font = QFont("helvetica", 9)
        self._actived_font.setWeight(QFont.DemiBold)
        self._metrics = QFontMetrics(self._font)
        self._descent = self._metrics.descent()
        self._margin = 5
        try:
            self.setClickable(True)
        except Exception:
            self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.sectionDoubleClicked.connect(self.double_clicked_section)

    def mouseReleaseEvent(self, e):
        if state.WINDOW is not None and hasattr(state.WINDOW, "view_widget"):
            state.WINDOW.view_widget.mouse_pos = QCursor.pos()
        if e.button() == Qt.RightButton:
            self.rightClicked.emit()
        else:
            super(MyHeaderView, self).mouseReleaseEvent(e)

    def double_clicked_section(self, e):
        self.doubleClicked.emit(e)

    def clicked_section(self, e):
        print(" clicked_section", e)

    def section_enter(self, e):
        print("section_enter", e)

    def paintSection(self, painter, rect, index):
        painter.rotate(-90)
        data = self._get_data(index)
        data = data.split(":")[-1]
        is_active = self.check_section_is_active(index)

        font = self._actived_font if is_active else self._font
        painter.setFont(font)

        color = self.model().headerData(index, Qt.Horizontal, Qt.BackgroundRole)
        dark_color = QColor(*map(lambda c: c - 42, color.getRgb()[:3]))
        draw_rect = self.rotate_rect(index, rect)
        painter.fillRect(draw_rect, color)

        painter.setPen(QPen(dark_color))
        painter.drawRect(draw_rect)

        painter.setPen(QColor(*[230] * 3))
        tx = int(-rect.height() + self._margin)
        ty = int(rect.left() + (rect.width() + self._descent) / 2.0)
        painter.drawText(tx, ty, data)

    def check_section_is_active(self, index):
        sel_model = self.selectionModel()
        if MAYA_VER >= 2022:
            return sel_model.columnIntersectsSelection(index)
        selected_item = sel_model.currentIndex()
        return sel_model.columnIntersectsSelection(index, selected_item)

    def rotate_rect(self, index, rect):
        offset = 1 if index == 0 else 0
        rect = rect.getRect()
        rect = [rect[1] - rect[3] + 1, rect[0] - 1 + offset, rect[3] - 1, rect[2] - offset]
        return QRect(*rect)

    def sizeHint(self):
        return QSize(0, int(self._get_text_width() + 2 * self._margin))

    def _get_text_width(self):
        if self.model() is None:
            return 0
        if MAYA_VER >= 2025:
            width_list = [
                self._metrics.horizontalAdvance(self._get_data(i))
                for i in range(0, self.model().columnCount())
            ] or [0]
        else:
            width_list = [
                self._metrics.width(self._get_data(i))
                for i in range(0, self.model().columnCount())
            ] or [0]
        return max(width_list) * 1.1

    def _get_data(self, index):
        return self.model().headerData(index, self.orientation(), 0)


def set_table_header_width(view, model, index=None, space=55, max=55, add=0):
    """QTableView の各カラム幅を表示単位と桁数に合わせて設定する。"""
    max += add

    def __resize_main(col_index):
        width = space + add
        if state.MAXIMUM_WEIGHT == 1.0:
            width -= 7
        if width > max:
            width = max
        view.setColumnWidth(col_index, int(width))

    if index is None:
        count = model.columnCount()
        for i in range(count):
            __resize_main(i)
    else:
        __resize_main(index)


class TableModel(QAbstractTableModel):
    """頂点×インフルエンスのウェイト一覧を描画・編集するテーブルモデル。"""

    norm = False

    def __init__(self, data, parent=None, mesh_rows=[], influences=[],
                 v_header_list=[], header_color_list=[]):
        super(TableModel, self).__init__(parent)
        self.under_weight_rows = set()
        self.over_weight_rows = set()
        self.over_influence_limit_dict = {}
        self.over_influence_limit_rows = set()
        self.none_cells = []
        self.v_header_list = v_header_list
        self.mesh_rows = mesh_rows
        self.weight_lock_cells = set()
        self.header_list = influences
        self.header_color_list = header_color_list
        self.copy_cell_list = []
        self.copy_vertex_rows = []
        self._data = data

    def headerData(self, id, orientation, role):
        """見出しラベルおよび背景色・配置を返す。"""
        if orientation == Qt.Horizontal:
            if id >= len(self.header_list):
                return None
            if role == Qt.DisplayRole:
                return self.header_list[id].split("|")[-1]
            elif role == Qt.BackgroundRole:
                color_id = self.header_color_list[id]
                return QColor(*self.joint_color_list[color_id])

        if orientation == Qt.Vertical:
            if role == Qt.DisplayRole:
                return self.v_header_list[id]
            elif role == Qt.BackgroundRole:
                max_inf = state.MAXIMUM_INFLUENCE_COUNT
                if id in self.mesh_rows:
                    end_of_mesh_rows = self.mesh_rows[self.mesh_rows.index(id) + 1]
                    mesh_rows = set(range(id + 1, end_of_mesh_rows))
                    if mesh_rows & self.under_weight_rows:
                        return QColor(*self.under_weight_color)
                    if mesh_rows & self.over_weight_rows:
                        return QColor(*self.over_weight_color)
                    for mrow in mesh_rows:
                        if self.over_influence_limit_dict[mrow] > max_inf > 0:
                            return QColor(*self.over_influence_color)

                if id in self.under_weight_rows:
                    return QColor(*self.under_weight_color)
                if id in self.over_weight_rows:
                    return QColor(*self.over_weight_color)
                try:
                    if self.over_influence_limit_dict[id] > max_inf > 0:
                        return QColor(*self.over_influence_color)
                except Exception:
                    pass
                return QColor(*self.v_header_bg)
            elif role == Qt.TextAlignmentRole:
                return Qt.AlignRight
        return None

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return len(self._data[0]) - 1 if self.rowCount() else 0

    joint_color_list = [
        [161, 105, 48],
        [159, 161, 48],
        [104, 161, 48],
        [48, 161, 93],
        [48, 161, 161],
        [48, 103, 161],
        [111, 48, 161],
        [161, 48, 105],
    ]
    v_header_text = [220] * 3
    v_header_bg = [100] * 3
    under_weight_color = [230, 54, 54]
    over_weight_color = [210, 116, 32]
    over_influence_color = [210, 190, 40]
    lock_weight_bg_color = [100] * 3
    lock_weight_text_color = [160] * 3
    cell_copy_color = [90, 120, 90]
    vertex_copy_color = [90, 90, 120]
    cell_vertex_copy_color = [70, 120, 120]

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        column = index.column()
        win = state.WINDOW
        if role == Qt.DisplayRole:
            try:
                node = win.vtx_row_dict[row][6]
                inf_id_list = win.node_influence_id_list_dict[node]
                local_column = inf_id_list[column]
                weight = self._data[row][local_column] * state.MAXIMUM_WEIGHT
                return state.FLOAT_FORMAT.format(weight)
            except Exception:
                return None
        elif role == Qt.BackgroundRole:
            if row in self.mesh_rows:
                color_id = self.header_color_list[column]
                return QColor(*self.joint_color_list[color_id])
            if row in self.copy_vertex_rows and index in self.copy_cell_list:
                return QColor(*self.cell_vertex_copy_color)
            if row in self.copy_vertex_rows:
                return QColor(*self.vertex_copy_color)
            if index in self.copy_cell_list:
                return QColor(*self.cell_copy_color)
            if (row, column) in self.weight_lock_cells:
                return QColor(*self.lock_weight_bg_color)
        elif role == Qt.ForegroundRole:
            zero_flag = False
            color = [210] * 3
            try:
                node = win.vtx_row_dict[row][6]
                inf_id_list = win.node_influence_id_list_dict[node]
                local_column = inf_id_list[column]
                weight = self._data[row][local_column] * state.MAXIMUM_WEIGHT
                if weight == 0.0:
                    zero_flag = True
            except Exception:
                pass

            if (row, column) in self.weight_lock_cells:
                color = self.lock_weight_text_color
            else:
                if row in self.under_weight_rows:
                    color = self.under_weight_color
                elif row in self.over_weight_rows:
                    color = self.over_weight_color
                else:
                    try:
                        if self.over_influence_limit_dict[row] > state.MAXIMUM_INFLUENCE_COUNT > 0:
                            color = self.over_influence_color
                    except Exception:
                        pass
                if zero_flag:
                    darken = state.ZERO_CELL_DARKEN
                    color = [min([c - darken, 255]) if c - darken > 43 else 43 for c in color]

            return QColor(*color)
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignRight

    def get_data(self, index=None, row=0, column=0):
        try:
            if index:
                row = index.row()
                column = index.column()
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            value = self._data[row][local_column]
        except Exception:
            value = None
        return value

    def setData(self, index, value, role=Qt.EditRole):
        if not isinstance(index, tuple):
            if not index.isValid() or not 0 <= index.row() < len(self._data):
                print("can not set data : retrun")
                return
            row = index.row()
            column = index.column()
        else:
            row = index[0]
            column = index[1]
        if role == Qt.EditRole and value != "":
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            self._data[row][local_column] = value
            model_index = self.index(row, column)
            self.dataChanged.emit(model_index, model_index)
            return True
        return False

    def flags(self, index):
        row = index.row()
        column = index.column()
        try:
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            value = self._data[row][local_column]
        except Exception:
            value = None
        if row in self.mesh_rows or value is None:
            return Qt.ItemIsEnabled
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable


def model_iter(model, parent_index=QModelIndex(), col_iter=True):
    """テーブルモデルの全アイテムインデックスを順に返すジェネレータ。"""
    index = model.index(0, 0, parent_index)

    while True:
        if col_iter:
            for col in range(0, model.columnCount(parent_index)):
                yield index.sibling(index.row(), col)
        else:
            yield index

        if model.rowCount(index) > 0:
            for item in model_iter(model, index, col_iter):
                yield item

        index = index.sibling(index.row() + 1, index.column())
        if not index.isValid():
            break
