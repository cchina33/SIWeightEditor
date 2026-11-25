# -*- coding: utf-8 -*-
"""Anim Picker 風の図柄（人型ボディ＆ハンド図柄 / 実スケルトン 2D 投影マップ）でボーンを選択・検索追加・3D ハイライトする専用ウィンドウ。"""

import math
import re

from maya import cmds

from ..maya_version import MAYA_VER
from .. import lang
from .. import qt
from ..qt import *
from ..editor import state as editor_state
from ..viewport import joint_display_helper


# ヒューマノイド図柄スロット定義（正規化座標 x, y, w, h, 部位キー, サイド, 表示ラベル）
# 正面ビュー基準で画面左がキャラクター右側（R）、画面右がキャラクター左側（L）
HUMANOID_SLOT_DEFS = (
    # 中央体幹
    ("head", "C", 0.50, 0.10, 0.11, 0.085, "Head"),
    ("neck", "C", 0.50, 0.18, 0.07, 0.042, "Neck"),
    ("chest", "C", 0.50, 0.27, 0.15, 0.085, "Chest"),
    ("spine", "C", 0.50, 0.37, 0.13, 0.075, "Spine"),
    ("hips", "C", 0.50, 0.47, 0.16, 0.075, "Hips"),
    # キャラクター右側（画面左）
    ("clavicle_R", "R", 0.37, 0.23, 0.085, 0.045, "Clav R"),
    ("upper_arm_R", "R", 0.27, 0.28, 0.085, 0.060, "Arm R"),
    ("forearm_R", "R", 0.18, 0.36, 0.085, 0.060, "Fore R"),
    ("hand_R", "R", 0.11, 0.45, 0.080, 0.058, "Hand R"),
    ("thigh_R", "R", 0.42, 0.59, 0.090, 0.095, "Thigh R"),
    ("shin_R", "R", 0.41, 0.72, 0.085, 0.095, "Shin R"),
    ("foot_R", "R", 0.40, 0.83, 0.085, 0.052, "Foot R"),
    ("toe_R", "R", 0.36, 0.895, 0.075, 0.042, "Toe R"),
    # キャラクター左側（画面右）
    ("clavicle_L", "L", 0.63, 0.23, 0.085, 0.045, "Clav L"),
    ("upper_arm_L", "L", 0.73, 0.28, 0.085, 0.060, "Arm L"),
    ("forearm_L", "L", 0.82, 0.36, 0.085, 0.060, "Fore L"),
    ("hand_L", "L", 0.89, 0.45, 0.080, 0.058, "Hand L"),
    ("thigh_L", "L", 0.58, 0.59, 0.090, 0.095, "Thigh L"),
    ("shin_L", "L", 0.59, 0.72, 0.085, 0.095, "Shin L"),
    ("foot_L", "L", 0.60, 0.83, 0.085, 0.052, "Foot L"),
    ("toe_L", "L", 0.64, 0.895, 0.075, 0.042, "Toe L"),
    # 右手 5 本指（画面左下のミニハンドエリア）
    ("thumb_R", "R", 0.23, 0.54, 0.058, 0.036, "Thm R"),
    ("index_R", "R", 0.21, 0.60, 0.058, 0.036, "Idx R"),
    ("middle_R", "R", 0.145, 0.615, 0.058, 0.036, "Mid R"),
    ("ring_R", "R", 0.085, 0.60, 0.058, 0.036, "Rng R"),
    ("pinky_R", "R", 0.045, 0.555, 0.054, 0.036, "Pnk R"),
    # 左手 5 本指（画面右下のミニハンドエリア）
    ("thumb_L", "L", 0.77, 0.54, 0.058, 0.036, "Thm L"),
    ("index_L", "L", 0.79, 0.60, 0.058, 0.036, "Idx L"),
    ("middle_L", "L", 0.855, 0.615, 0.058, 0.036, "Mid L"),
    ("ring_L", "L", 0.915, 0.60, 0.058, 0.036, "Rng L"),
    ("pinky_L", "L", 0.955, 0.555, 0.054, 0.036, "Pnk L"),
    # その他・補助ボーン（顔・髪・衣装・尻尾など）
    ("other_R", "R", 0.16, 0.86, 0.15, 0.060, "Extra R"),
    ("other_C", "C", 0.50, 0.96, 0.22, 0.052, "Extra Center"),
    ("other_L", "L", 0.84, 0.86, 0.15, 0.060, "Extra L"),
)

# ヒューマノイド図柄の骨格接続線（スロットキー同士のペア）
HUMANOID_CONNECT_PAIRS = (
    ("head", "neck"),
    ("neck", "chest"),
    ("chest", "spine"),
    ("spine", "hips"),
    ("chest", "clavicle_R"),
    ("clavicle_R", "upper_arm_R"),
    ("upper_arm_R", "forearm_R"),
    ("forearm_R", "hand_R"),
    ("chest", "clavicle_L"),
    ("clavicle_L", "upper_arm_L"),
    ("upper_arm_L", "forearm_L"),
    ("forearm_L", "hand_L"),
    ("hips", "thigh_R"),
    ("thigh_R", "shin_R"),
    ("shin_R", "foot_R"),
    ("foot_R", "toe_R"),
    ("hips", "thigh_L"),
    ("thigh_L", "shin_L"),
    ("shin_L", "foot_L"),
    ("foot_L", "toe_L"),
    ("hand_R", "thumb_R"),
    ("hand_R", "index_R"),
    ("hand_R", "middle_R"),
    ("hand_R", "ring_R"),
    ("hand_R", "pinky_R"),
    ("hand_L", "thumb_L"),
    ("hand_L", "index_L"),
    ("hand_L", "middle_L"),
    ("hand_L", "ring_L"),
    ("hand_L", "pinky_L"),
)


def detect_bone_side(short_name):
    """ボーン名から左右・中央（L / R / C）を判定する（Left / Right キャメルケース・数字付き対応）。"""
    base = short_name.split("|")[-1].split(":")[-1]
    upper = base.upper()

    # Left / Right の単語（アンダースコア区切り・キャメルケース・末尾数字付きすべて対応）
    has_left = bool(re.search(r"(?:^|[^A-Z])LEFT(?:[^A-Z]|$)|(?<=[a-z0-9_])Left(?=[A-Z0-9_.]|$)|^Left(?=[A-Z0-9_.]|$)", base))
    has_right = bool(re.search(r"(?:^|[^A-Z])RIGHT(?:[^A-Z]|$)|(?<=[a-z0-9_])Right(?=[A-Z0-9_.]|$)|^Right(?=[A-Z0-9_.]|$)", base))
    if has_left and not has_right:
        return "L"
    if has_right and not has_left:
        return "R"

    # L / R 略記（先頭・中間区切り・末尾・末尾数字付き _L01 / .L など）
    if re.search(r"(?:^|[_\.\-])L(?:[_\.\-]|[0-9]+$|$)", upper):
        return "L"
    if re.search(r"(?:^|[_\.\-])R(?:[_\.\-]|[0-9]+$|$)", upper):
        return "R"
    # 大文字 L/R の後ろがすぐ小文字ではない区切り（例: ArmL01 は誤爆を避け末尾のみ判定）
    if re.search(r"(?<=[a-z])L(?:[0-9]*)$", base):
        return "L"
    if re.search(r"(?<=[a-z])R(?:[0-9]*)$", base):
        return "R"
    return "C"


def classify_humanoid_slot(short_name):
    """ボーン名からヒューマノイド図柄の部位スロットキーを自動判定する。"""
    base = short_name.split("|")[-1].split(":")[-1]
    low = base.lower()
    side = detect_bone_side(base)

    # 指系（ハンドより先に判定して誤分類を防ぐ）
    if side in ("L", "R"):
        if "thumb" in low or "oyayubi" in low:
            return "thumb_" + side
        if "index" in low or "hitosashi" in low:
            return "index_" + side
        if "middle" in low or "naka" in low:
            return "middle_" + side
        if "ring" in low or "kusuri" in low:
            return "ring_" + side
        if "pinky" in low or "little" in low or "koyubi" in low:
            return "pinky_" + side

        # 腕・肩系
        if any(k in low for k in ("clavicle", "collar", "shoulder", "kata")):
            # UpperArm に shoulder が含まれない場合を優先（shoulder は通常鎖骨〜肩）
            if "upperarm" not in low and "uparm" not in low:
                return "clavicle_" + side
        if any(k in low for k in ("forearm", "lowerarm", "lowarm", "elbow", "hiji", "ulna", "radius")):
            return "forearm_" + side
        if any(k in low for k in ("hand", "wrist", "palm", "tekubi")):
            return "hand_" + side
        if any(k in low for k in ("upperarm", "uparm", "arm", "bicep", "humerus", "ude")):
            return "upper_arm_" + side

        # 脚・足系
        if any(k in low for k in ("toe", "ball", "tsumasaki")):
            return "toe_" + side
        if any(k in low for k in ("foot", "ankle", "heel", "ashikubi")):
            return "foot_" + side
        if any(k in low for k in ("shin", "calf", "knee", "lowerleg", "lowleg", "leg", "hiza", "sune")):
            if "upleg" not in low and "upperleg" not in low and "thigh" not in low:
                return "shin_" + side
        if any(k in low for k in ("thigh", "upleg", "upperleg", "hip", "femur", "momo")):
            return "thigh_" + side

        return "other_" + side

    # 中央体幹系
    if any(k in low for k in ("head", "skull", "atama")):
        return "head"
    if any(k in low for k in ("neck", "kubi")):
        return "neck"
    if any(k in low for k in ("chest", "upperchest", "rib", "thorax", "mune")):
        return "chest"
    if any(k in low for k in ("spine", "waist", "torso", "abdomen", "sebone", "back")):
        return "spine"
    if any(k in low for k in ("hips", "hip", "pelvis", "root", "cog", "koshi")):
        return "hips"
    return "other_C"


def _weight_to_qcolor(weight):
    """ウェイト値（0.0〜1.0）に応じたヒートマップ QColor を返す。"""
    r, g, b = joint_display_helper._weight_to_rgb(weight)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


class HumanoidPickerCanvas(QWidget):
    """Anim Picker 風の人型ボディ＆ハンド図柄キャンバス。"""

    selectionChanged = Signal(list)
    slotDoubleClicked = Signal(list)

    def __init__(self, parent=None):
        super(HumanoidPickerCanvas, self).__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(340, 420)
        self.slot_bones = {}          # slot_key -> list[full_path]
        self.weight_stats = {}        # full_path -> {"max": float, "count": int}
        self.selected_bones = set()   # set[full_path]
        self.side_filter = "all"      # all / L / R / C
        self._slot_rects = {}         # slot_key -> QRectF
        self._hover_slot = None
        self._drag_origin = None
        self._drag_rect = None

    def set_data(self, influences, weight_stats, selected_bones, side_filter="all"):
        self.weight_stats = weight_stats or {}
        self.selected_bones = set(selected_bones or [])
        self.side_filter = side_filter
        mapping = {}
        for inf in influences or []:
            short_name = inf.split("|")[-1]
            slot_key = classify_humanoid_slot(short_name)
            mapping.setdefault(slot_key, []).append(inf)
        self.slot_bones = mapping
        self.update()

    def _compute_slot_rects(self):
        w = max(1, self.width())
        h = max(1, self.height())
        pad_x = 12.0
        pad_y = 10.0
        avail_w = max(100.0, w - pad_x * 2.0)
        avail_h = max(100.0, h - pad_y * 2.0)
        rects = {}
        for key, side, nx, ny, nw, nh, _ in HUMANOID_SLOT_DEFS:
            rw = nw * avail_w
            rh = nh * avail_h
            cx = pad_x + nx * avail_w
            cy = pad_y + ny * avail_h
            rects[key] = QRectF(cx - rw * 0.5, cy - rh * 0.5, rw, rh)
        self._slot_rects = rects
        return rects

    def _slot_is_visible_by_side(self, side):
        return self.side_filter == "all" or self.side_filter == side

    def _slot_weight_max(self, bones):
        best = 0.0
        for b in bones:
            stat = self.weight_stats.get(b)
            if stat and stat.get("max", 0.0) > best:
                best = float(stat["max"])
        return best

    def _base_color_for_side(self, side):
        if side == "L":
            return QColor(56, 98, 152)
        if side == "R":
            return QColor(152, 68, 72)
        return QColor(145, 122, 54)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 背景グラデーション
        bg_grad = QLinearGradient(0, 0, 0, self.height())
        bg_grad.setColorAt(0.0, QColor(34, 37, 43))
        bg_grad.setColorAt(1.0, QColor(24, 26, 31))
        painter.fillRect(self.rect(), bg_grad)

        rects = self._compute_slot_rects()

        # 左右ガイド表示（ビュー正面基準：左=R / 右=L）
        painter.setPen(QColor(135, 145, 160, 160))
        guide_font = QFont("sans-serif", 8)
        guide_font.setBold(True)
        painter.setFont(guide_font)
        painter.drawText(10, 18, "R (Right)")
        painter.drawText(self.width() - 62, 18, "L (Left)")

        # 骨格接続ライン描画
        line_pen = QPen(QColor(95, 105, 120, 150), 2.0)
        painter.setPen(line_pen)
        for src_k, dst_k in HUMANOID_CONNECT_PAIRS:
            r1 = rects.get(src_k)
            r2 = rects.get(dst_k)
            if r1 is not None and r2 is not None:
                painter.drawLine(r1.center(), r2.center())

        # 各スロット図柄ボタン描画
        label_font = QFont("sans-serif", 8)
        badge_font = QFont("sans-serif", 7)
        badge_font.setBold(True)
        max_wt = editor_state.MAXIMUM_WEIGHT

        for key, side, _, _, _, _, label in HUMANOID_SLOT_DEFS:
            rect = rects[key]
            bones = self.slot_bones.get(key) or []
            visible_side = self._slot_is_visible_by_side(side)
            has_bones = bool(bones) and visible_side
            is_hover = (self._hover_slot == key) and has_bones

            selected_cnt = sum(1 for b in bones if b in self.selected_bones) if has_bones else 0
            is_selected = selected_cnt > 0
            is_partial = 0 < selected_cnt < len(bones)
            w_max = self._slot_weight_max(bones) if has_bones else 0.0

            if not has_bones:
                fill_col = QColor(42, 45, 52, 90)
                border_pen = QPen(QColor(72, 76, 86, 110), 1.0, Qt.DashLine)
                text_col = QColor(95, 100, 110)
            else:
                base_col = self._base_color_for_side(side)
                if w_max > 0.0001:
                    hm = _weight_to_qcolor(w_max)
                    fill_col = QColor(
                        int(base_col.red() * 0.35 + hm.red() * 0.65),
                        int(base_col.green() * 0.35 + hm.green() * 0.65),
                        int(base_col.blue() * 0.35 + hm.blue() * 0.65),
                        235,
                    )
                else:
                    fill_col = QColor(base_col.red(), base_col.green(), base_col.blue(), 190)

                if is_hover:
                    fill_col = fill_col.lighter(118)

                if is_selected:
                    border_pen = QPen(QColor(50, 245, 255), 2.6 if not is_partial else 2.0)
                elif w_max > 0.0001:
                    border_pen = QPen(QColor(255, 235, 120), 1.8)
                else:
                    border_pen = QPen(QColor(170, 180, 195, 180), 1.1)
                text_col = QColor(245, 248, 252)

            radius = 7.0 if key != "head" else min(rect.width(), rect.height()) * 0.45
            painter.setBrush(QBrush(fill_col))
            painter.setPen(border_pen)
            painter.drawRoundedRect(rect, radius, radius)

            # ラベル表示
            painter.setFont(label_font)
            painter.setPen(text_col)
            disp_label = label
            if has_bones and len(bones) > 1:
                disp_label = "{0}({1})".format(label, len(bones))
            painter.drawText(rect, Qt.AlignCenter, disp_label)

            # ウェイト値ミニバッジ（ウェイトがある部位のみ右上表示）
            if has_bones and w_max > 0.0001:
                wt_str = "{0:.0f}".format(w_max * max_wt) if max_wt >= 10.0 else "{0:.2f}".format(w_max)
                badge_rect = QRectF(rect.right() - 26, rect.top() - 7, 28, 13)
                painter.setBrush(QBrush(QColor(20, 22, 26, 220)))
                painter.setPen(QPen(QColor(255, 230, 110), 1.0))
                painter.drawRoundedRect(badge_rect, 4.0, 4.0)
                painter.setFont(badge_font)
                painter.setPen(QColor(255, 240, 150))
                painter.drawText(badge_rect, Qt.AlignCenter, wt_str)

        # ドラッグ矩形選択（ラバーバンド）
        if self._drag_rect is not None and not self._drag_rect.isNull():
            painter.setBrush(QBrush(QColor(60, 220, 255, 45)))
            painter.setPen(QPen(QColor(80, 235, 255, 220), 1.2, Qt.DashLine))
            painter.drawRect(self._drag_rect)

    def _hit_slot(self, pos):
        pt = QPointF(pos.x(), pos.y())
        for key, side, _, _, _, _, _ in HUMANOID_SLOT_DEFS:
            if not self._slot_is_visible_by_side(side):
                continue
            bones = self.slot_bones.get(key) or []
            if not bones:
                continue
            rect = self._slot_rects.get(key)
            if rect is not None and rect.contains(pt):
                return key
        return None

    def _event_pos(self, event):
        if hasattr(event, "position"):
            p = event.position()
            return QPoint(int(p.x()), int(p.y()))
        return event.pos()

    def mouseMoveEvent(self, event):
        pos = self._event_pos(event)
        if self._drag_origin is not None:
            self._drag_rect = QRectF(
                QPointF(self._drag_origin.x(), self._drag_origin.y()),
                QPointF(pos.x(), pos.y()),
            ).normalized()
            self.update()
            return

        hit = self._hit_slot(pos)
        if hit != self._hover_slot:
            self._hover_slot = hit
            if hit:
                bones = self.slot_bones.get(hit) or []
                max_wt = editor_state.MAXIMUM_WEIGHT
                fmt = editor_state.FLOAT_FORMAT
                lines = []
                for b in bones[:12]:
                    sn = b.split("|")[-1]
                    st = self.weight_stats.get(b)
                    if st and st.get("count", 0) > 0:
                        lines.append("{0}  [Max: {1} / {2}vtx]".format(sn, fmt.format(st["max"] * max_wt), st["count"]))
                    else:
                        lines.append(sn)
                if len(bones) > 12:
                    lines.append("... (+{0})".format(len(bones) - 12))
                self.setToolTip("\n".join(lines))
            else:
                self.setToolTip("")
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = self._event_pos(event)
        self._drag_origin = pos
        self._drag_rect = None

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._drag_origin is None:
            return
        pos = self._event_pos(event)
        mods = QApplication.keyboardModifiers()
        add_mod = bool(mods & (Qt.ControlModifier | Qt.ShiftModifier))
        dragged = (abs(pos.x() - self._drag_origin.x()) > 5 or abs(pos.y() - self._drag_origin.y()) > 5)

        new_sel = set(self.selected_bones) if add_mod else set()

        if dragged and self._drag_rect is not None:
            for key, side, _, _, _, _, _ in HUMANOID_SLOT_DEFS:
                if not self._slot_is_visible_by_side(side):
                    continue
                bones = self.slot_bones.get(key) or []
                if not bones:
                    continue
                rect = self._slot_rects.get(key)
                if rect is not None and rect.intersects(self._drag_rect):
                    new_sel.update(bones)
        else:
            hit = self._hit_slot(pos)
            if hit:
                bones = self.slot_bones.get(hit) or []
                if add_mod and all(b in self.selected_bones for b in bones):
                    for b in bones:
                        new_sel.discard(b)
                else:
                    new_sel.update(bones)

        self._drag_origin = None
        self._drag_rect = None
        self.selected_bones = new_sel
        self.update()
        self.selectionChanged.emit(sorted(new_sel))

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = self._event_pos(event)
        hit = self._hit_slot(pos)
        if hit:
            bones = self.slot_bones.get(hit) or []
            if bones:
                self.selected_bones = set(bones)
                self.update()
                self.slotDoubleClicked.emit(list(bones))


class Skeleton2DCanvas(QWidget):
    """Maya シーン内の実ジョイント座標を 2D 投影して図柄選択できるスケルトンマップ。"""

    selectionChanged = Signal(list)
    boneDoubleClicked = Signal(list)

    def __init__(self, parent=None):
        super(Skeleton2DCanvas, self).__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(340, 420)
        self.projection = "front"     # front (XY) / side (ZY) / top (XZ)
        self.side_filter = "all"      # all / L / R / C
        self.bones = []               # list[full_path]
        self.bone_pos_3d = {}         # full_path -> (x, y, z)
        self.bone_edges = []          # list[(parent_path, child_path)]
        self.weight_stats = {}
        self.selected_bones = set()
        self._screen_pts = {}         # full_path -> QPointF
        self._hover_bone = None
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._pan_origin = None
        self._drag_origin = None
        self._drag_rect = None

    def set_projection(self, proj):
        self.projection = proj
        self.reset_view()

    def reset_view(self):
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def set_data(self, influences, weight_stats, selected_bones, side_filter="all", refresh_positions=True):
        self.weight_stats = weight_stats or {}
        self.selected_bones = set(selected_bones or [])
        self.side_filter = side_filter
        self.bones = list(influences or [])
        if refresh_positions or not self.bone_pos_3d:
            self._sample_scene_positions()
        self.update()

    def _sample_scene_positions(self):
        pos_map = {}
        bone_set = set(self.bones)
        edges = []
        for b in self.bones:
            if not cmds.objExists(b):
                continue
            try:
                pos = cmds.xform(b, q=True, ws=True, t=True)
                pos_map[b] = (float(pos[0]), float(pos[1]), float(pos[2]))
            except Exception:
                continue
            # 親方向をたどり、バインド済みジョイント同士の接続線を作る
            cur = b
            for _ in range(16):
                try:
                    parents = cmds.listRelatives(cur, parent=True, type="joint", fullPath=True) or []
                except Exception:
                    parents = []
                if not parents:
                    break
                parent = parents[0]
                if parent in bone_set:
                    edges.append((parent, b))
                    break
                cur = parent
        self.bone_pos_3d = pos_map
        self.bone_edges = edges

    def _project_pt(self, xyz):
        x, y, z = xyz
        if self.projection == "side":
            return (z, -y)
        if self.projection == "top":
            return (x, z)
        return (x, -y)

    def _bone_is_visible(self, bone_path):
        if self.side_filter == "all":
            return True
        return detect_bone_side(bone_path) == self.side_filter

    def _compute_screen_pts(self):
        pts_2d = {}
        for b, xyz in self.bone_pos_3d.items():
            pts_2d[b] = self._project_pt(xyz)
        if not pts_2d:
            self._screen_pts = {}
            return self._screen_pts

        xs = [p[0] for p in pts_2d.values()]
        ys = [p[1] for p in pts_2d.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(1e-4, max_x - min_x)
        span_y = max(1e-4, max_y - min_y)
        cx_data = (min_x + max_x) * 0.5
        cy_data = (min_y + max_y) * 0.5

        pad = 28.0
        avail_w = max(80.0, self.width() - pad * 2.0)
        avail_h = max(80.0, self.height() - pad * 2.0)
        base_scale = min(avail_w / span_x, avail_h / span_y) * self._zoom
        cx_scr = self.width() * 0.5 + self._pan.x()
        cy_scr = self.height() * 0.5 + self._pan.y()

        screen = {}
        for b, (px, py) in pts_2d.items():
            sx = cx_scr + (px - cx_data) * base_scale
            sy = cy_scr + (py - cy_data) * base_scale
            screen[b] = QPointF(sx, sy)
        self._screen_pts = screen
        return screen

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        bg_grad = QLinearGradient(0, 0, 0, self.height())
        bg_grad.setColorAt(0.0, QColor(30, 33, 40))
        bg_grad.setColorAt(1.0, QColor(21, 23, 28))
        painter.fillRect(self.rect(), bg_grad)

        pts = self._compute_screen_pts()
        if not pts:
            painter.setPen(QColor(150, 155, 165))
            painter.drawText(
                self.rect(),
                Qt.AlignCenter,
                lang.Lang(en="No joint positions available", ja=u"表示できるジョイント座標がありません").output(),
            )
            return

        # 骨格ライン描画
        for p_bone, c_bone in self.bone_edges:
            p1 = pts.get(p_bone)
            p2 = pts.get(c_bone)
            if p1 is None or p2 is None:
                continue
            both_vis = self._bone_is_visible(p_bone) and self._bone_is_visible(c_bone)
            col = QColor(115, 125, 145, 175) if both_vis else QColor(75, 80, 90, 70)
            painter.setPen(QPen(col, 1.8 if both_vis else 1.0))
            painter.drawLine(p1, p2)

        # ジョイントノード描画（ウェイトあり・選択中を手前に描く）
        ordered = sorted(
            pts.keys(),
            key=lambda b: (
                1 if b in self.selected_bones else 0,
                1 if (self.weight_stats.get(b) or {}).get("max", 0.0) > 0.0001 else 0,
            ),
        )
        for b in ordered:
            pt = pts[b]
            vis = self._bone_is_visible(b)
            side = detect_bone_side(b)
            is_sel = b in self.selected_bones
            is_hover = (b == self._hover_bone)
            w_max = float((self.weight_stats.get(b) or {}).get("max", 0.0))

            if not vis:
                painter.setBrush(QBrush(QColor(65, 70, 78, 70)))
                painter.setPen(QPen(QColor(80, 85, 95, 80), 1.0))
                painter.drawEllipse(pt, 3.5, 3.5)
                continue

            if w_max > 0.0001:
                fill = _weight_to_qcolor(w_max)
            elif side == "L":
                fill = QColor(72, 132, 205)
            elif side == "R":
                fill = QColor(205, 86, 90)
            else:
                fill = QColor(200, 172, 76)

            radius = 7.0 if is_sel else (6.5 if w_max > 0.0001 else 5.2)
            if is_hover:
                radius += 1.5
                fill = fill.lighter(120)

            if is_sel:
                pen = QPen(QColor(50, 245, 255), 2.4)
            elif w_max > 0.0001:
                pen = QPen(QColor(255, 238, 135), 1.6)
            else:
                pen = QPen(QColor(225, 230, 240, 190), 1.1)

            painter.setBrush(QBrush(fill))
            painter.setPen(pen)
            painter.drawEllipse(pt, radius, radius)

        # ホバー中ボーン名を画面下部バーに表示
        if self._hover_bone:
            short_n = self._hover_bone.split("|")[-1]
            st = self.weight_stats.get(self._hover_bone)
            info = short_n
            if st and st.get("count", 0) > 0:
                max_wt = editor_state.MAXIMUM_WEIGHT
                fmt = editor_state.FLOAT_FORMAT
                info = "{0}  [Max: {1} / {2}vtx]".format(short_n, fmt.format(st["max"] * max_wt), st["count"])
            bar_rect = QRectF(8, self.height() - 24, self.width() - 16, 18)
            painter.setBrush(QBrush(QColor(18, 20, 24, 215)))
            painter.setPen(QPen(QColor(95, 110, 130), 1.0))
            painter.drawRoundedRect(bar_rect, 4.0, 4.0)
            painter.setPen(QColor(235, 242, 250))
            painter.drawText(bar_rect.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, info)

        # 矩形選択ラバーバンド
        if self._drag_rect is not None and not self._drag_rect.isNull():
            painter.setBrush(QBrush(QColor(60, 220, 255, 45)))
            painter.setPen(QPen(QColor(80, 235, 255, 220), 1.2, Qt.DashLine))
            painter.drawRect(self._drag_rect)

    def _hit_bone(self, pos):
        best_bone = None
        best_dist = 11.0
        for b, pt in self._screen_pts.items():
            if not self._bone_is_visible(b):
                continue
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d < best_dist:
                best_dist = d
                best_bone = b
        return best_bone

    def _event_pos(self, event):
        if hasattr(event, "position"):
            p = event.position()
            return QPoint(int(p.x()), int(p.y()))
        return event.pos()

    def wheelEvent(self, event):
        raw_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else event.delta()
        if raw_delta == 0:
            return
        factor = 1.15 if raw_delta > 0 else (1.0 / 1.15)
        self._zoom = max(0.35, min(6.0, self._zoom * factor))
        self.update()

    def mouseMoveEvent(self, event):
        pos = self._event_pos(event)
        if self._pan_origin is not None:
            delta = QPointF(pos.x() - self._pan_origin.x(), pos.y() - self._pan_origin.y())
            self._pan = QPointF(self._pan.x() + delta.x(), self._pan.y() + delta.y())
            self._pan_origin = pos
            self.update()
            return

        if self._drag_origin is not None:
            self._drag_rect = QRectF(
                QPointF(self._drag_origin.x(), self._drag_origin.y()),
                QPointF(pos.x(), pos.y()),
            ).normalized()
            self.update()
            return

        hit = self._hit_bone(pos)
        if hit != self._hover_bone:
            self._hover_bone = hit
            self.setToolTip(hit if hit else "")
            self.update()

    def mousePressEvent(self, event):
        pos = self._event_pos(event)
        mods = QApplication.keyboardModifiers()
        if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton and bool(mods & Qt.AltModifier)):
            self._pan_origin = pos
            return
        if event.button() == Qt.LeftButton:
            self._drag_origin = pos
            self._drag_rect = None

    def mouseReleaseEvent(self, event):
        if self._pan_origin is not None:
            self._pan_origin = None
            return
        if event.button() != Qt.LeftButton or self._drag_origin is None:
            return
        pos = self._event_pos(event)
        mods = QApplication.keyboardModifiers()
        add_mod = bool(mods & (Qt.ControlModifier | Qt.ShiftModifier))
        dragged = (abs(pos.x() - self._drag_origin.x()) > 5 or abs(pos.y() - self._drag_origin.y()) > 5)

        new_sel = set(self.selected_bones) if add_mod else set()
        if dragged and self._drag_rect is not None:
            for b, pt in self._screen_pts.items():
                if self._bone_is_visible(b) and self._drag_rect.contains(pt):
                    new_sel.add(b)
        else:
            hit = self._hit_bone(pos)
            if hit:
                if add_mod and hit in self.selected_bones:
                    new_sel.discard(hit)
                else:
                    new_sel.add(hit)

        self._drag_origin = None
        self._drag_rect = None
        self.selected_bones = new_sel
        self.update()
        self.selectionChanged.emit(sorted(new_sel))

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = self._event_pos(event)
        hit = self._hit_bone(pos)
        if hit:
            self.selected_bones = {hit}
            self.update()
            self.boneDoubleClicked.emit([hit])


class BonePickerWindow(qt.SubWindow):
    """Anim Picker 風の図柄でボーン選択・検索追加・3D ビューポート強調を行う非モーダルウィンドウ。"""

    WINDOW_OBJECT_NAME = "SIWeightEditorImproveBonePickerWindow"

    def __init__(self, editor_window, parent=None):
        super(BonePickerWindow, self).__init__(parent=parent or qt.get_maya_window())
        self.editor = editor_window
        self.setObjectName(self.WINDOW_OBJECT_NAME)
        self.setWindowTitle(
            lang.Lang(
                en="Bone Picker - SIWeightEditor Improve",
                ja=u"ボーンピッカー - SIWeightEditor Improve",
            ).output()
        )
        self.setWindowFlag(Qt.Window, True)
        self.resize(450, 680)

        self._influences = []
        self._weight_stats = {}
        self._selected_bones = set()
        self._side_filter = "all"
        self._syncing_detail = False

        self._init_ui()

    def _init_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 上部：左右フィルタ行（L/Left, R/Right 対応）
        side_row = QHBoxLayout()
        side_row.setSpacing(4)
        self.side_all_but = QPushButton(lang.Lang(en="All Sides", ja=u"左右すべて").output())
        self.side_l_but = QPushButton(lang.Lang(en="L / Left", ja=u"左 (L/Left)").output())
        self.side_r_but = QPushButton(lang.Lang(en="R / Right", ja=u"右 (R/Right)").output())
        self.side_c_but = QPushButton(lang.Lang(en="Center", ja=u"中央").output())
        for but in (self.side_all_but, self.side_l_but, self.side_r_but, self.side_c_but):
            but.setCheckable(True)
            side_row.addWidget(but)
        self.side_all_but.setChecked(True)
        layout.addLayout(side_row)

        # 中央：2 モード切替タブ（Humanoid Picker / Skeleton 2D Map）
        self.tabs = QTabWidget(self)

        self.humanoid_canvas = HumanoidPickerCanvas(self)
        self.tabs.addTab(
            self.humanoid_canvas,
            lang.Lang(en="Humanoid Picker", ja=u"人型ピッカー").output(),
        )

        # Skeleton 2D タブ（投影切替バー付き）
        skel_tab = QWidget(self)
        skel_layout = QVBoxLayout(skel_tab)
        skel_layout.setContentsMargins(4, 4, 4, 4)
        skel_layout.setSpacing(4)

        proj_row = QHBoxLayout()
        proj_row.setSpacing(4)
        self.proj_front_but = QPushButton(lang.Lang(en="Front (XY)", ja=u"正面 (XY)").output())
        self.proj_side_but = QPushButton(lang.Lang(en="Side (ZY)", ja=u"側面 (ZY)").output())
        self.proj_top_but = QPushButton(lang.Lang(en="Top (XZ)", ja=u"上面 (XZ)").output())
        self.proj_fit_but = QPushButton(lang.Lang(en="Reset View", ja=u"表示リセット").output())
        for but in (self.proj_front_but, self.proj_side_but, self.proj_top_but):
            but.setCheckable(True)
            proj_row.addWidget(but)
        self.proj_front_but.setChecked(True)
        proj_row.addStretch()
        proj_row.addWidget(self.proj_fit_but)
        skel_layout.addLayout(proj_row)

        self.skeleton_canvas = Skeleton2DCanvas(self)
        skel_layout.addWidget(self.skeleton_canvas, 1)
        self.tabs.addTab(
            skel_tab,
            lang.Lang(en="Skeleton 2D Map", ja=u"実スケルトン 2D").output(),
        )
        layout.addWidget(self.tabs, 3)

        # 選択中ボーン一覧（複数ボーンが属する部位の個別確認・調整用）
        detail_label = QLabel(
            lang.Lang(
                en="Selected Bones in Picker (Click / Drag on canvas, Ctrl/Shift to multi-select):",
                ja=u"ピッカーで選択中のボーン（図柄クリック / ドラッグ囲み / Ctrl・Shift 複数選択）:",
            ).output()
        )
        layout.addWidget(detail_label)

        self.detail_list = QListWidget(self)
        self.detail_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.detail_list.setMaximumHeight(110)
        layout.addWidget(self.detail_list)

        # 連動オプション行
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        self.auto_apply_chk = QCheckBox(lang.Lang(en="Auto Search", ja=u"検索へ自動反映").output())
        self.auto_apply_chk.setChecked(False)
        self.auto_apply_chk.setToolTip(
            lang.Lang(
                en="Automatically update the main search bar whenever picker selection changes",
                ja=u"図柄でボーンを選ぶと同時にメイン画面の検索欄へ自動反映します",
            ).output()
        )
        self.exact_chk = QCheckBox(lang.Lang(en="Exact (=)", ja=u"厳密一致 (=)").output())
        self.exact_chk.setChecked(True)
        self.highlight_3d_chk = QCheckBox(lang.Lang(en="3D Highlight", ja=u"3D ハイライト").output())
        self.highlight_3d_chk.setChecked(True)
        self.highlight_3d_chk.setToolTip(
            lang.Lang(
                en="Highlight picker-selected bones in cyan inside the Maya 3D viewport",
                ja=u"ピッカーで選択中のボーンを Maya の 3D ビューポート上でシアン色に強調表示します",
            ).output()
        )
        opt_row.addWidget(self.auto_apply_chk)
        opt_row.addWidget(self.exact_chk)
        opt_row.addWidget(self.highlight_3d_chk)
        opt_row.addStretch()
        layout.addLayout(opt_row)

        # 操作ボタン行（置換 / 追加 / 3D選択 / クリア / 更新）
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.replace_but = QPushButton(lang.Lang(en="Replace Search", ja=u"検索を置換").output())
        self.add_but = QPushButton(lang.Lang(en="Add to Search", ja=u"検索へ追加").output())
        self.select_scene_but = QPushButton(lang.Lang(en="Select in Maya", ja=u"Maya で選択").output())
        self.clear_but = QPushButton(lang.Lang(en="Clear", ja=u"解除").output())
        self.refresh_but = QPushButton(lang.Lang(en="Refresh", ja=u"更新").output())
        btn_row.addWidget(self.replace_but)
        btn_row.addWidget(self.add_but)
        btn_row.addWidget(self.select_scene_but)
        btn_row.addWidget(self.clear_but)
        btn_row.addWidget(self.refresh_but)
        layout.addLayout(btn_row)

        # シグナル接続
        self.side_all_but.clicked.connect(lambda: self._set_side_filter("all"))
        self.side_l_but.clicked.connect(lambda: self._set_side_filter("L"))
        self.side_r_but.clicked.connect(lambda: self._set_side_filter("R"))
        self.side_c_but.clicked.connect(lambda: self._set_side_filter("C"))

        self.proj_front_but.clicked.connect(lambda: self._set_projection("front"))
        self.proj_side_but.clicked.connect(lambda: self._set_projection("side"))
        self.proj_top_but.clicked.connect(lambda: self._set_projection("top"))
        self.proj_fit_but.clicked.connect(self.skeleton_canvas.reset_view)

        self.humanoid_canvas.selectionChanged.connect(self._on_canvas_selection_changed)
        self.humanoid_canvas.slotDoubleClicked.connect(self._on_canvas_double_clicked)
        self.skeleton_canvas.selectionChanged.connect(self._on_canvas_selection_changed)
        self.skeleton_canvas.boneDoubleClicked.connect(self._on_canvas_double_clicked)
        self.detail_list.itemChanged.connect(self._on_detail_item_changed)

        self.highlight_3d_chk.toggled.connect(self._apply_3d_highlight)
        self.replace_but.clicked.connect(self.apply_replace_to_search)
        self.add_but.clicked.connect(self.apply_add_to_search)
        self.select_scene_but.clicked.connect(self.select_bones_in_maya)
        self.clear_but.clicked.connect(self.clear_selection)
        self.refresh_but.clicked.connect(lambda: self.refresh_from_editor(refresh_positions=True))

    def _set_side_filter(self, side):
        self._side_filter = side
        self.side_all_but.setChecked(side == "all")
        self.side_l_but.setChecked(side == "L")
        self.side_r_but.setChecked(side == "R")
        self.side_c_but.setChecked(side == "C")
        self._sync_canvases(refresh_positions=False)

    def _set_projection(self, proj):
        self.proj_front_but.setChecked(proj == "front")
        self.proj_side_but.setChecked(proj == "side")
        self.proj_top_but.setChecked(proj == "top")
        self.skeleton_canvas.set_projection(proj)

    def _sync_canvases(self, refresh_positions=False):
        self.humanoid_canvas.set_data(
            self._influences,
            self._weight_stats,
            self._selected_bones,
            side_filter=self._side_filter,
        )
        self.skeleton_canvas.set_data(
            self._influences,
            self._weight_stats,
            self._selected_bones,
            side_filter=self._side_filter,
            refresh_positions=refresh_positions,
        )

    def _rebuild_detail_list(self):
        self._syncing_detail = True
        try:
            self.detail_list.clear()
            max_wt = editor_state.MAXIMUM_WEIGHT
            fmt = editor_state.FLOAT_FORMAT
            for b in sorted(self._selected_bones, key=lambda s: s.split("|")[-1].upper()):
                sn = b.split("|")[-1]
                st = self._weight_stats.get(b)
                if st and st.get("count", 0) > 0:
                    label = "{0}   [Max: {1} / {2}vtx]".format(sn, fmt.format(st["max"] * max_wt), st["count"])
                else:
                    label = sn
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, b)
                item.setToolTip(b)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                self.detail_list.addItem(item)
        finally:
            self._syncing_detail = False

    def _on_detail_item_changed(self, item):
        if self._syncing_detail:
            return
        bone_path = item.data(Qt.UserRole)
        if not bone_path:
            return
        if item.checkState() == Qt.Checked:
            self._selected_bones.add(bone_path)
        else:
            self._selected_bones.discard(bone_path)
        self._sync_canvases(refresh_positions=False)
        self._apply_3d_highlight()
        if self.auto_apply_chk.isChecked():
            self.apply_replace_to_search()

    def _on_canvas_selection_changed(self, bone_paths):
        self._selected_bones = set(bone_paths or [])
        self._sync_canvases(refresh_positions=False)
        self._rebuild_detail_list()
        self._apply_3d_highlight()
        if self.auto_apply_chk.isChecked():
            self.apply_replace_to_search()

    def _on_canvas_double_clicked(self, bone_paths):
        self._selected_bones = set(bone_paths or [])
        self._sync_canvases(refresh_positions=False)
        self._rebuild_detail_list()
        self._apply_3d_highlight()
        self.apply_replace_to_search()

    def _format_tokens(self):
        short_names = sorted(set(b.split("|")[-1] for b in self._selected_bones), key=lambda s: s.upper())
        if self.exact_chk.isChecked():
            return ["=" + name for name in short_names]
        return short_names

    def apply_replace_to_search(self):
        if self.editor is not None and hasattr(self.editor, "set_joint_names_to_search"):
            self.editor.set_joint_names_to_search(self._format_tokens())

    def apply_add_to_search(self):
        tokens = self._format_tokens()
        if tokens and self.editor is not None and hasattr(self.editor, "append_joint_names_to_search"):
            self.editor.append_joint_names_to_search(tokens)

    def select_bones_in_maya(self):
        existing = [b for b in sorted(self._selected_bones) if cmds.objExists(b)]
        if existing:
            cmds.select(existing, replace=True)

    def clear_selection(self):
        self._selected_bones.clear()
        self._sync_canvases(refresh_positions=False)
        self._rebuild_detail_list()
        self._apply_3d_highlight()
        if self.auto_apply_chk.isChecked():
            self.apply_replace_to_search()

    def _apply_3d_highlight(self, *args):
        if self.highlight_3d_chk.isChecked() and self._selected_bones:
            joint_display_helper.set_picker_highlight_joints(
                self._selected_bones,
                window=self.editor,
                maya_ver=MAYA_VER,
            )
        else:
            joint_display_helper.clear_picker_highlight_joints(
                window=self.editor,
                maya_ver=MAYA_VER,
            )

    def refresh_from_editor(self, refresh_positions=False):
        """メインウィンドウの最新インフルエンス・ウェイト統計からピッカー表示を更新する。"""
        if self.editor is None or getattr(self.editor, "closed_flag", False):
            return
        influences = self.editor._collect_bound_influences() if hasattr(self.editor, "_collect_bound_influences") else []
        weight_stats = self.editor.get_influence_weight_stats() if hasattr(self.editor, "get_influence_weight_stats") else {}
        self._influences = list(influences or [])
        self._weight_stats = weight_stats or {}
        valid_set = set(self._influences)
        self._selected_bones = self._selected_bones & valid_set
        self._sync_canvases(refresh_positions=refresh_positions)
        self._rebuild_detail_list()

    def closeEvent(self, event):
        joint_display_helper.clear_picker_highlight_joints(
            window=self.editor,
            maya_ver=MAYA_VER,
        )
        if self.editor is not None and getattr(self.editor, "bone_picker_window", None) is self:
            self.editor.bone_picker_window = None
        super(BonePickerWindow, self).closeEvent(event)
