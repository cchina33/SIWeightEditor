# -*- coding: utf-8 -*-
"""選択頂点のウェイト先ボーンを Viewport 2.0 上に X-Ray 描画するプラグイン。"""

import sys
import traceback

import maya.api.OpenMaya as om2
import maya.api.OpenMayaRender as omr
import maya.api.OpenMayaUI as omui


kNodeName = "siweBoneVisualizer"
kNodeId = om2.MTypeId(0x0007E510)
kDrawDbClassification = "drawdb/geometry/siweBoneVisualizer"
kDrawRegistrantId = "siweBoneVisualizerPlugin"

_REPORTED_ERRORS = set()
_DEFAULT_BBOX = om2.MBoundingBox(om2.MPoint(-1.0, -1.0, -1.0), om2.MPoint(1.0, 1.0, 1.0))
_SHARED_BBOX = om2.MBoundingBox(om2.MPoint(-1.0, -1.0, -1.0), om2.MPoint(1.0, 1.0, 1.0))


def maya_useNewAPI():
    """OpenMaya API 2.0 を使用することを Maya に宣言する。"""
    pass


def _report_error_once(key, exc):
    """同一エラーの大量出力を避けつつ初回のみスクリプトエディタへ通知する。"""
    sig = "{0}:{1}".format(key, exc)
    if sig in _REPORTED_ERRORS:
        return
    _REPORTED_ERRORS.add(sig)
    sys.stderr.write("SIWeightEditor BoneVisualizer error ({0}): {1}\n".format(key, exc))
    traceback.print_exc()


def _get_visualizer_state():
    """制御モジュール側の共有状態辞書を安全に取得する。"""
    try:
        from siweighteditor_improve.viewport import bone_visualizer
        return bone_visualizer.STATE, bone_visualizer.MAX_SAMPLE_VERTICES
    except Exception as exc:
        _report_error_once("import_state", exc)
        return None, 64


def _weight_to_heatmap_color(weight):
    """ウェイト値（0.0～1.0）に応じたヒートマップ色（青→緑→黄→赤）を返す。"""
    w = max(0.0, min(1.0, float(weight)))
    stops = (
        (0.00, (0.20, 0.72, 1.00)),
        (0.25, (0.25, 0.90, 0.55)),
        (0.50, (0.95, 0.90, 0.20)),
        (0.75, (1.00, 0.50, 0.12)),
        (1.00, (1.00, 0.18, 0.15)),
    )
    for idx in range(len(stops) - 1):
        t0, c0 = stops[idx]
        t1, c1 = stops[idx + 1]
        if w <= t1:
            span = t1 - t0
            ratio = (w - t0) / span if span > 0.0 else 0.0
            r = c0[0] + (c1[0] - c0[0]) * ratio
            g = c0[1] + (c1[1] - c0[1]) * ratio
            b = c0[2] + (c1[2] - c0[2]) * ratio
            return om2.MColor((r, g, b, 1.0))
    return om2.MColor((1.0, 0.18, 0.15, 1.0))


def _resolve_dag_path(node_path):
    """ノードパスから MDagPath を解決する。"""
    try:
        sel = om2.MSelectionList()
        sel.add(node_path)
        return sel.getDagPath(0)
    except Exception:
        return None


def _get_visible_mesh_dag(mesh_path):
    """スキニング済みメッシュから中間オブジェクト以外の表示用メッシュシェイプを取得する。"""
    dag_path = _resolve_dag_path(mesh_path)
    if dag_path is None:
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
            dag_fn = om2.MFnDagNode(shape_dag)
            if not dag_fn.isIntermediateObject:
                return shape_dag
        except Exception:
            return shape_dag
    return fallback_dag


def _compute_origin_from_cached_meshes(cached_meshes, collect_individual):
    """キャッシュ済みメッシュ DAG から現在フレームの頂点重心を高速計算する。"""
    total_x = 0.0
    total_y = 0.0
    total_z = 0.0
    sampled_count = 0
    individual_points = []

    for shape_dag, vtx_ids in cached_meshes:
        if not vtx_ids or not shape_dag.isValid():
            continue
        try:
            mesh_fn = om2.MFnMesh(shape_dag)
            num_verts = mesh_fn.numVertices
        except Exception:
            continue

        for vid in vtx_ids:
            if vid < 0 or vid >= num_verts:
                continue
            try:
                pt = mesh_fn.getPoint(vid, om2.MSpace.kWorld)
            except Exception:
                continue
            total_x += pt.x
            total_y += pt.y
            total_z += pt.z
            sampled_count += 1
            if collect_individual:
                individual_points.append(pt)

    if sampled_count == 0:
        return None, []

    inv_count = 1.0 / float(sampled_count)
    origin = om2.MPoint(total_x * inv_count, total_y * inv_count, total_z * inv_count)
    return origin, individual_points


def _build_bbox_from_points(origin, vertex_points, entries, remap_target=None):
    """計算済み座標から余白付きバウンディングボックスを作成する。"""
    bbox = om2.MBoundingBox()
    if origin is not None:
        bbox.expand(origin)
    for pt in vertex_points:
        bbox.expand(pt)
    for entry in entries:
        bbox.expand(entry["position"])
    if remap_target is not None:
        bbox.expand(remap_target["position"])
        for child_pt in remap_target.get("child_positions") or []:
            bbox.expand(child_pt)

    margin = 10.0
    min_pt = bbox.min
    max_pt = bbox.max
    return om2.MBoundingBox(
        om2.MPoint(min_pt.x - margin, min_pt.y - margin, min_pt.z - margin),
        om2.MPoint(max_pt.x + margin, max_pt.y + margin, max_pt.z + margin),
    )


def _get_direct_child_joint_dags(joint_dag):
    """指定ジョイント直下の子ジョイント DAG 一覧を OpenMaya 経由で高速取得する。"""
    children = []
    if joint_dag is None or not joint_dag.isValid():
        return children
    try:
        count = joint_dag.childCount()
        for idx in range(count):
            child_obj = joint_dag.child(idx)
            if child_obj.hasFn(om2.MFn.kJoint):
                child_dag = om2.MDagPath(joint_dag)
                child_dag.push(child_obj)
                children.append(child_dag)
    except Exception:
        pass
    return children


class BoneVisualizerData(om2.MUserData):
    """Viewport 2.0 描画フレーム間で渡すユーザーデータ。"""

    def __init__(self):
        # Maya 2022 以降の API 2.0 では引数なし、旧版では deleteAfterUse 指定へフォールバックする
        try:
            super(BoneVisualizerData, self).__init__()
        except TypeError:
            super(BoneVisualizerData, self).__init__(False)
        self.clear()

    def clear(self):
        self.enabled = False
        self.origin = None
        self.vertex_points = []
        self.entries = []
        self.remap_target = None
        self.show_lines = True
        self.show_labels = True
        self.dashed_tiny = True
        self.tiny_threshold = 0.05


class BoneVisualizerLocatorNode(omui.MPxLocatorNode):
    """描画トリガーとなる一時カスタムロケータノード。"""

    def __init__(self):
        super(BoneVisualizerLocatorNode, self).__init__()

    def isBounded(self):
        return True

    def boundingBox(self):
        return _SHARED_BBOX

    def excludeAsLocator(self):
        # ビューポートのロケータ非表示設定の影響を受けず常に見えるようにする
        return True

    @staticmethod
    def creator():
        return BoneVisualizerLocatorNode()

    @staticmethod
    def initialize():
        pass


class BoneVisualizerDrawOverride(omr.MPxDrawOverride):
    """Viewport 2.0 上に X-Ray ライン・マーカー・ラベルを描画するオーバーライド。"""

    def __init__(self, obj):
        # 毎フレーム最新座標へ追従させるため isAlwaysDirty を有効にする
        super(BoneVisualizerDrawOverride, self).__init__(obj, None, True)
        self._cached_version = -1
        self._cached_enabled = False
        self._cached_meshes = []
        self._cached_joints = []
        self._cached_remap_dag = None
        self._cached_remap_child_dags = []
        self._cached_remap_label = ""
        self._cached_collect_individual = False
        self._cached_options = (True, True, True, 0.05)
        self._cached_bbox = _DEFAULT_BBOX

    def supportedDrawAPIs(self):
        return omr.MRenderer.kAllDevices

    def isBounded(self, objPath, cameraPath):
        return True

    def _refresh_dag_cache_if_needed(self):
        """共有状態のバージョンが変わったときだけ DAG パスと表示文字列を再構築する。"""
        state, max_samples = _get_visualizer_state()
        if not state:
            self._cached_enabled = False
            self._cached_meshes = []
            self._cached_joints = []
            self._cached_remap_dag = None
            self._cached_remap_child_dags = []
            self._cached_remap_label = ""
            self._cached_bbox = _DEFAULT_BBOX
            return False

        version = int(state.get("version", 0))
        if version == self._cached_version:
            return False

        self._cached_version = version
        self._cached_meshes = []
        self._cached_joints = []
        self._cached_remap_dag = None
        self._cached_remap_child_dags = []
        self._cached_remap_label = ""
        self._cached_enabled = bool(state.get("enabled", False))
        self._cached_options = (
            bool(state.get("show_lines", True)),
            bool(state.get("show_labels", True)),
            bool(state.get("dashed_tiny", True)),
            float(state.get("tiny_threshold", 0.05)),
        )

        # Remap 変更先ボーンの X-Ray ハイライト対象は Bone Vis の ON/OFF に関係なく解決する
        remap_info = state.get("remap_target")
        if isinstance(remap_info, dict) and remap_info.get("joint"):
            remap_dag = _resolve_dag_path(remap_info["joint"])
            if remap_dag is not None:
                self._cached_remap_dag = remap_dag
                self._cached_remap_child_dags = _get_direct_child_joint_dags(remap_dag)
                self._cached_remap_label = str(remap_info.get("label") or "")

        if not self._cached_enabled:
            if self._cached_remap_dag is None:
                self._cached_bbox = _DEFAULT_BBOX
            return True

        mesh_vertices = state.get("mesh_vertices") or []
        influences = state.get("influences") or []
        if not mesh_vertices or not influences:
            self._cached_enabled = False
            if self._cached_remap_dag is None:
                self._cached_bbox = _DEFAULT_BBOX
            return True

        total_vtx_count = int(state.get("vertex_count", 0))
        self._cached_collect_individual = 1 < total_vtx_count <= 16

        # メッシュ DAG をキャッシュし、必要であればここでも上限サンプリングする
        raw_total = sum(len(vids) for _, vids in mesh_vertices)
        step = max(1, raw_total // max_samples) if raw_total > max_samples and max_samples > 0 else 1
        for mesh_path, vtx_ids in mesh_vertices:
            if not vtx_ids:
                continue
            shape_dag = _get_visible_mesh_dag(mesh_path)
            if shape_dag is not None:
                picked = vtx_ids[::step] if step > 1 else list(vtx_ids)
                if picked:
                    self._cached_meshes.append((shape_dag, picked))

        max_weight = float(state.get("max_weight", 100.0))
        decimals = int(state.get("decimals", 1))
        aggregate_mode = state.get("aggregate_mode", "avg")
        suffix = "%" if max_weight >= 50.0 else ""
        mode_tag = ""
        if total_vtx_count > 1:
            mode_tag = " [max]" if aggregate_mode == "max" else " [avg]"

        value_format = "{0:." + str(max(0, decimals)) + "f}"
        joint_items = []
        for inf in influences:
            joint_dag = _resolve_dag_path(inf["joint"])
            if joint_dag is None:
                continue
            weight = float(inf["weight"])
            display_val = value_format.format(weight * max_weight)
            label_text = "{0}: {1}{2}{3}".format(inf["label"], display_val, suffix, mode_tag)
            selected = bool(inf.get("selected", False))
            joint_items.append((joint_dag, weight, label_text, selected))

        # ウェイトが小さい順に並べて描画し、重要度の高いボーンを最前面にする
        joint_items.sort(key=lambda item: (item[3], item[1]))
        self._cached_joints = joint_items
        return True

    def _evaluate_positions(self):
        """キャッシュ済み DAG から現在フレームの頂点重心・ボーン座標・Remap 変更先座標を高速取得する。"""
        global _SHARED_BBOX
        zero_pt = om2.MPoint(0.0, 0.0, 0.0)

        origin = None
        vertex_points = []
        entries = []
        if self._cached_enabled and self._cached_meshes and self._cached_joints:
            origin, vertex_points = _compute_origin_from_cached_meshes(
                self._cached_meshes, self._cached_collect_individual
            )
            if origin is not None:
                for joint_dag, weight, label_text, selected in self._cached_joints:
                    if not joint_dag.isValid():
                        continue
                    try:
                        joint_pos = zero_pt * joint_dag.inclusiveMatrix()
                    except Exception:
                        continue
                    entries.append({
                        "position": joint_pos,
                        "weight": weight,
                        "label": label_text,
                        "selected": selected,
                    })

        remap_target = None
        if self._cached_remap_dag is not None and self._cached_remap_dag.isValid():
            try:
                remap_pos = zero_pt * self._cached_remap_dag.inclusiveMatrix()
                child_positions = []
                for child_dag in self._cached_remap_child_dags:
                    if child_dag.isValid():
                        try:
                            child_positions.append(zero_pt * child_dag.inclusiveMatrix())
                        except Exception:
                            pass
                remap_target = {
                    "position": remap_pos,
                    "child_positions": child_positions,
                    "label": self._cached_remap_label,
                }
            except Exception:
                remap_target = None

        if (origin is None or not entries) and remap_target is None:
            self._cached_bbox = _DEFAULT_BBOX
            _SHARED_BBOX = _DEFAULT_BBOX
            return None, [], [], None

        bbox = _build_bbox_from_points(origin, vertex_points, entries, remap_target=remap_target)
        self._cached_bbox = bbox
        _SHARED_BBOX = bbox
        return origin, vertex_points, entries, remap_target

    def boundingBox(self, objPath, cameraPath):
        try:
            # データ更新直後の初回だけ最新範囲を算出し、以降はキャッシュを即返却する
            if self._refresh_dag_cache_if_needed():
                self._evaluate_positions()
            return self._cached_bbox
        except Exception as exc:
            _report_error_once("override_bbox", exc)
            return _DEFAULT_BBOX

    def hasUIDrawables(self):
        return True

    def wantUserSelection(self):
        # 頂点クリックやマーキー選択の対象外であることを明示する
        return False

    def prepareForDraw(self, objPath, cameraPath, frameContext, oldData):
        try:
            data = oldData if isinstance(oldData, BoneVisualizerData) else BoneVisualizerData()
            data.clear()

            self._refresh_dag_cache_if_needed()
            if not self._cached_enabled and self._cached_remap_dag is None:
                return data

            origin, vertex_points, entries, remap_target = self._evaluate_positions()
            has_bone_vis = bool(self._cached_enabled and origin is not None and entries)
            if not has_bone_vis and remap_target is None:
                return data

            show_lines, show_labels, dashed_tiny, tiny_threshold = self._cached_options
            data.enabled = has_bone_vis
            data.origin = origin
            data.vertex_points = vertex_points
            data.entries = entries
            data.remap_target = remap_target
            data.show_lines = show_lines
            data.show_labels = show_labels
            data.dashed_tiny = dashed_tiny
            data.tiny_threshold = tiny_threshold
            return data
        except Exception as exc:
            _report_error_once("prepareForDraw", exc)
            return oldData if isinstance(oldData, BoneVisualizerData) else None

    def addUIDrawables(self, objPath, drawManager, frameContext, data):
        if not isinstance(data, BoneVisualizerData):
            return
        has_bone_vis = bool(data.enabled and data.entries and data.origin is not None)
        has_remap = data.remap_target is not None
        if not has_bone_vis and not has_remap:
            return

        # 選択用レンダーパスでは一切描画せず、ビューポートの頂点クリック判定への干渉を防ぐ
        try:
            pass_ctx = frameContext.getPassContext()
            semantics = pass_ctx.passSemantics() if pass_ctx else ()
            if any("selection" in str(sem).lower() for sem in semantics):
                return
        except Exception:
            pass

        try:
            drawManager.beginDrawable(omr.MUIDrawManager.kNonSelectable)
        except Exception:
            drawManager.beginDrawable()
        drawManager.beginDrawInXray()
        try:
            if has_bone_vis:
                origin = data.origin
                # 頂点起点から各ボーンへの接続ラインを描画
                if data.show_lines:
                    for entry in data.entries:
                        weight = entry["weight"]
                        joint_pos = entry["position"]
                        is_selected = entry["selected"]
                        color = _weight_to_heatmap_color(weight)
                        is_tiny = data.dashed_tiny and weight < data.tiny_threshold

                        if is_tiny:
                            drawManager.setLineStyle(omr.MUIDrawManager.kShortDotted)
                        else:
                            drawManager.setLineStyle(omr.MUIDrawManager.kSolid)

                        line_width = 1.5 + weight * 4.5
                        if is_selected:
                            # 選択列のボーンは白い外枠線を先に引いて強調する
                            drawManager.setColor(om2.MColor((1.0, 1.0, 1.0, 0.95)))
                            drawManager.setLineWidth(line_width + 2.5)
                            drawManager.line(origin, joint_pos)

                        drawManager.setColor(color)
                        drawManager.setLineWidth(line_width)
                        drawManager.line(origin, joint_pos)

                    drawManager.setLineStyle(omr.MUIDrawManager.kSolid)

                # 少数選択時は各選択頂点にも補助ドットを描画
                if len(data.vertex_points) > 1:
                    drawManager.setColor(om2.MColor((1.0, 1.0, 0.75, 0.85)))
                    drawManager.setPointSize(5.0)
                    for pt in data.vertex_points:
                        drawManager.point(pt)

                # 選択頂点側（重心）の起点マーカーを描画
                drawManager.setColor(om2.MColor((0.05, 0.05, 0.05, 0.95)))
                drawManager.setPointSize(11.0)
                drawManager.point(origin)
                drawManager.setColor(om2.MColor((1.0, 1.0, 1.0, 1.0)))
                drawManager.setPointSize(7.0)
                drawManager.point(origin)

                # ボーン位置のマーカーとテキストラベルを描画
                for entry in data.entries:
                    weight = entry["weight"]
                    joint_pos = entry["position"]
                    is_selected = entry["selected"]
                    color = _weight_to_heatmap_color(weight)

                    point_size = 8.0 + weight * 8.0
                    outline_color = om2.MColor((1.0, 1.0, 1.0, 1.0)) if is_selected else om2.MColor((0.05, 0.05, 0.05, 0.95))
                    drawManager.setColor(outline_color)
                    drawManager.setPointSize(point_size + 4.0)
                    drawManager.point(joint_pos)

                    drawManager.setColor(color)
                    drawManager.setPointSize(point_size)
                    drawManager.point(joint_pos)

                    if data.show_labels:
                        drawManager.setFontSize(12)
                        text_color = om2.MColor((1.0, 1.0, 0.55, 1.0)) if is_selected else om2.MColor((0.98, 0.98, 0.98, 1.0))
                        bg_color = om2.MColor((0.18, 0.14, 0.04, 0.85)) if is_selected else om2.MColor((0.06, 0.06, 0.08, 0.78))
                        drawManager.setColor(text_color)
                        try:
                            drawManager.text(
                                joint_pos,
                                entry["label"],
                                alignment=omr.MUIDrawManager.kCenter,
                                backgroundColor=bg_color,
                            )
                        except Exception:
                            drawManager.text(joint_pos, entry["label"], omr.MUIDrawManager.kCenter)

            # Remap 変更先ボーンの X-Ray マゼンタ強調描画（メッシュ越しでも視認可能にする）
            if has_remap:
                remap_pos = data.remap_target["position"]
                child_positions = data.remap_target.get("child_positions") or []
                remap_label = data.remap_target.get("label") or ""
                magenta = om2.MColor((1.0, 0.18, 0.95, 1.0))
                white = om2.MColor((1.0, 1.0, 1.0, 0.95))

                # 頂点起点があれば変更先ボーンへの破線ガイドを描画
                if data.origin is not None:
                    drawManager.setLineStyle(omr.MUIDrawManager.kDashed)
                    drawManager.setColor(magenta)
                    drawManager.setLineWidth(2.2)
                    drawManager.line(data.origin, remap_pos)
                    drawManager.setLineStyle(omr.MUIDrawManager.kSolid)

                # 子ボーン方向への強調ボーンラインを描画
                for child_pos in child_positions:
                    drawManager.setColor(white)
                    drawManager.setLineWidth(5.5)
                    drawManager.line(remap_pos, child_pos)
                    drawManager.setColor(magenta)
                    drawManager.setLineWidth(3.2)
                    drawManager.line(remap_pos, child_pos)

                # 変更先ボーン起点の二重ターゲットマーカーを描画
                drawManager.setColor(white)
                drawManager.setPointSize(19.0)
                drawManager.point(remap_pos)
                drawManager.setColor(magenta)
                drawManager.setPointSize(14.0)
                drawManager.point(remap_pos)
                drawManager.setColor(white)
                drawManager.setPointSize(5.0)
                drawManager.point(remap_pos)

                if remap_label:
                    drawManager.setFontSize(13)
                    drawManager.setColor(om2.MColor((1.0, 0.78, 1.0, 1.0)))
                    bg_color = om2.MColor((0.24, 0.04, 0.22, 0.88))
                    try:
                        drawManager.text(
                            remap_pos,
                            remap_label,
                            alignment=omr.MUIDrawManager.kCenter,
                            backgroundColor=bg_color,
                        )
                    except Exception:
                        drawManager.text(remap_pos, remap_label, omr.MUIDrawManager.kCenter)
        except Exception as exc:
            _report_error_once("addUIDrawables", exc)
        finally:
            drawManager.endDrawInXray()
            drawManager.endDrawable()

    @staticmethod
    def creator(obj):
        return BoneVisualizerDrawOverride(obj)


def initializePlugin(mobject):
    """プラグインのノードと Viewport 2.0 描画オーバーライドを登録する。"""
    mplugin = om2.MFnPlugin(mobject, "SIWeightEditorImprove", "1.0", "Any")
    try:
        mplugin.registerNode(
            kNodeName,
            kNodeId,
            BoneVisualizerLocatorNode.creator,
            BoneVisualizerLocatorNode.initialize,
            om2.MPxNode.kLocatorNode,
            kDrawDbClassification,
        )
    except Exception:
        sys.stderr.write("Failed to register node: " + kNodeName + "\n")
        raise

    try:
        omr.MDrawRegistry.registerDrawOverrideCreator(
            kDrawDbClassification,
            kDrawRegistrantId,
            BoneVisualizerDrawOverride.creator,
        )
    except Exception:
        sys.stderr.write("Failed to register draw override: " + kDrawRegistrantId + "\n")
        raise


def uninitializePlugin(mobject):
    """プラグインの描画オーバーライドとノードの登録を解除する。"""
    mplugin = om2.MFnPlugin(mobject)
    try:
        omr.MDrawRegistry.deregisterDrawOverrideCreator(
            kDrawDbClassification,
            kDrawRegistrantId,
        )
    except Exception:
        sys.stderr.write("Failed to deregister draw override: " + kDrawRegistrantId + "\n")

    try:
        mplugin.deregisterNode(kNodeId)
    except Exception:
        sys.stderr.write("Failed to deregister node: " + kNodeName + "\n")
