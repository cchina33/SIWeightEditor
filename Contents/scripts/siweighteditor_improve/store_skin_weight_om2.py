# -*- coding: utf-8 -*-
"""OpenMaya API 2.0 を用いて選択オブジェクト・頂点・エッジ・フェースのスキンウェイトを取得するモジュール。"""

import maya.OpenMaya as om
import maya.OpenMayaAnim as oma
import maya.api.OpenMaya as om2
import maya.api.OpenMayaAnim as oma2
from maya import cmds
from . import common
from . import prof


def _get_visible_mesh_shape_dag(dag_path):
    """トランスフォームまたはシェイプ DAG から中間オブジェクト以外の可視メッシュシェイプ DAG を返す。"""
    if dag_path is None:
        return None
    if dag_path.node().hasFn(om2.MFn.kMesh):
        return om2.MDagPath(dag_path)

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


class StoreSkinWeight():
    def run_store(self):
        self.dag_skin_id_dict = {}
        self.om_all_mesh_vertex()
        self.om_get_skin_weight()

    # ノード名・ノードタイプキャッシュ
    node_type_dict = {}
    shape_transform_dict = {}

    def _get_transform_path(self, dag_path):
        """メッシュシェイプまたはトランスフォームのフルパス名から親トランスフォーム名を返す。"""
        mesh_path_name = dag_path.fullPathName()
        node_type = self.node_type_dict.get(mesh_path_name)
        if node_type is None or not cmds.objExists(mesh_path_name):
            try:
                node_type = cmds.nodeType(mesh_path_name)
                self.node_type_dict[mesh_path_name] = node_type
            except Exception:
                return mesh_path_name

        if node_type == "mesh":
            transform_name = self.shape_transform_dict.get(mesh_path_name)
            if transform_name is None or not cmds.objExists(transform_name):
                parents = cmds.listRelatives(mesh_path_name, p=True, f=True) or []
                transform_name = parents[0] if parents else mesh_path_name
                self.shape_transform_dict[mesh_path_name] = transform_name
            return transform_name
        return mesh_path_name

    # 指定ノード中の選択バーテックスを返す（オブジェクト・グループ・エッジ・フェース選択に対応）
    def om_selected_mesh_vertex(self, node, show_bad=False):
        sList = om2.MGlobal.getActiveSelectionList()
        sel_iter = om2.MItSelectionList(sList)
        loop = 0
        vtx_set = set()
        group_matched = False

        while not sel_iter.isDone():
            loop += 1
            if loop >= 10000:
                print('too many loop :')
                return []

            try:
                meshDag, component = sel_iter.getComponent()
            except Exception:
                sel_iter.next()
                continue

            mesh_path_name = self._get_transform_path(meshDag)

            if node == mesh_path_name:
                skinFn, vtxArray, skinName = self.adust_to_vertex_list(meshDag, component, force=True)
                if vtxArray:
                    vtx_set.update(int(v) for v in vtxArray)
            elif component.isNull() and node.startswith(mesh_path_name + "|"):
                # 親グループをオブジェクトモードで選択した場合は配下メッシュ全頂点を対象にする
                group_matched = True

            sel_iter.next()

        if vtx_set:
            return sorted(vtx_set)

        if group_matched and hasattr(self, "node_vtx_dict") and node in self.node_vtx_dict:
            return [int(v) for v in self.node_vtx_dict[node]]

        return []

    # すべてのバーテックス ID リストとメッシュ情報の辞書を作って返す
    def om_all_mesh_vertex(self):
        sList = om2.MGlobal.getActiveSelectionList()
        sel_iter = om2.MItSelectionList(sList)
        loop = 0
        om_add_nodes = []
        while not sel_iter.isDone():
            loop += 1
            if loop >= 10000:
                print('too many loop :')
                return []
            try:
                meshDag = sel_iter.getDagPath()
            except Exception:
                sel_iter.next()
                continue
            mesh_path_name = meshDag.fullPathName()
            om_add_nodes.append(mesh_path_name)
            sel_iter.next()

        resolved_nodes = []
        for node in om_add_nodes:
            if not cmds.objExists(node):
                continue
            try:
                if cmds.nodeType(node) == "mesh":
                    parents = cmds.listRelatives(node, p=True, f=True) or []
                    if parents:
                        resolved_nodes.append(parents[0])
                else:
                    resolved_nodes.append(node)
            except Exception:
                pass
        om_add_nodes = common.search_polygon_mesh(resolved_nodes, fullPath=True, serchChildeNode=True) or []

        if cmds.selectMode(q=True, co=True):
            # コンポーネント選択の時でもハイライト対象を末端まで取得する
            self.hl_nodes = cmds.ls(hl=True, l=True) or []
            self.hl_nodes = common.search_polygon_mesh(self.hl_nodes, fullPath=True, serchChildeNode=True) or []
            add_node = common.search_polygon_mesh(cmds.ls(sl=True, l=True, tr=True) or [], fullPath=True, serchChildeNode=True) or []
            if add_node:
                self.hl_nodes += add_node
            if om_add_nodes:
                self.hl_nodes += om_add_nodes
        else:
            sel_tr = (cmds.ls(sl=True, l=True, tr=True) or []) + (cmds.ls(hl=True, l=True, tr=True) or [])
            self.hl_nodes = common.search_polygon_mesh(sel_tr, fullPath=True, serchChildeNode=True) or []
            if om_add_nodes:
                self.hl_nodes += om_add_nodes
        self.hl_nodes = sorted(set(self.hl_nodes))

        for node in self.hl_nodes[:]:
            sList = om2.MSelectionList()
            try:
                sList.add(node)
                meshDag, component = sList.getComponent(0)
            except Exception:
                continue

            shape_dag = _get_visible_mesh_shape_dag(meshDag)
            if shape_dag is None:
                continue

            skinFn, vtxArray, skinName = self.adust_to_vertex_list(shape_dag, component)
            if skinFn is None or vtxArray is None:
                continue
            self.dag_skin_id_dict[node] = [skinFn, vtxArray, skinName, shape_dag]

    def adust_to_vertex_list(self, meshDag, component, force=False):
        """選択コンポーネント（頂点・エッジ・フェース・オブジェクト全体）を安全に頂点 ID 配列へ変換する。"""
        shape_dag = _get_visible_mesh_shape_dag(meshDag)
        if shape_dag is None:
            return None, None, None

        skinFn, skinName = self.om_get_skin_cluster(shape_dag)
        if not force:
            if not skinFn or not skinName:
                return None, None, None
            if not shape_dag.node().hasFn(om2.MFn.kMesh) or skinName == "":
                return None, None, None

        cmpType = None
        if component is not None and not component.isNull():
            if component.hasFn(om2.MFn.kMeshVertComponent):
                cmpType = "vtx"
            elif component.hasFn(om2.MFn.kMeshEdgeComponent):
                cmpType = "edge"
            elif component.hasFn(om2.MFn.kMeshPolygonComponent):
                cmpType = "face"

        try:
            meshFn = om2.MFnMesh(shape_dag)
        except Exception:
            return None, None, None

        if cmpType == "vtx":
            compFn = om2.MFnSingleIndexedComponent(component)
            vtxArray = compFn.getElements()
        elif cmpType == "edge":
            # OpenMaya API 2.0 では MItMeshEdge でエッジ両端の頂点 ID を安全に取得する
            vtx_set = set()
            edge_it = om2.MItMeshEdge(shape_dag, component)
            while not edge_it.isDone():
                vtx_set.add(int(edge_it.vertexId(0)))
                vtx_set.add(int(edge_it.vertexId(1)))
                edge_it.next()
            vtxArray = om2.MIntArray(sorted(vtx_set))
        elif cmpType == "face":
            # OpenMaya API 2.0 の MItMeshPolygon でフェース構成頂点 ID を安全に取得する
            vtx_set = set()
            poly_it = om2.MItMeshPolygon(shape_dag, component)
            while not poly_it.isDone():
                for vid in poly_it.getVertices():
                    vtx_set.add(int(vid))
                poly_it.next()
            vtxArray = om2.MIntArray(sorted(vtx_set))
        else:
            vtxArray = om2.MIntArray(list(range(meshFn.numVertices)))

        return skinFn, vtxArray, skinName

    # ディペンデンシーグラフをたどってスキンクラスタを探す
    def om_get_skin_cluster(self, dagPath=None):
        if not dagPath:
            return None, None
        full_name = dagPath.fullPathName()
        skinCluster = cmds.ls(cmds.listHistory(full_name, pruneDagObjects=True) or [], type="skinCluster") or []
        if not skinCluster:
            skinCluster = cmds.ls(cmds.listHistory(full_name) or [], type="skinCluster") or []
        if not skinCluster:
            return None, None
        clusterName = skinCluster[0]
        sellist = om2.MGlobal.getSelectionListByName(clusterName)
        skinNode = sellist.getDependNode(0)
        skinFn = oma2.MFnSkinCluster(skinNode)
        return skinFn, clusterName

    def om_get_skin_weight(self):
        self.node_weight_dict = {}
        self.node_vtx_dict = {}
        self.inf_id_list = list()
        self.influences_dict = {}
        self.all_influences = list()
        self.all_skin_clusters = {}
        self.mesh_node_list = list()
        self.show_dict = {}
        self.node_skinFn_dict = {}

        for transform_path, skin_vtx in self.dag_skin_id_dict.items():
            skinFn = skin_vtx[0]
            vtxArry = skin_vtx[1]
            skinName = skin_vtx[2]
            meshPath = skin_vtx[3]

            mesh_path_name = transform_path
            try:
                if cmds.nodeType(mesh_path_name) == "mesh":
                    parents = cmds.listRelatives(mesh_path_name, p=True, f=True) or []
                    if parents:
                        mesh_path_name = parents[0]
            except Exception:
                pass

            self.node_skinFn_dict[mesh_path_name] = skinFn

            if not vtxArry:
                continue

            singleIdComp = om2.MFnSingleIndexedComponent()
            vertexComp = singleIdComp.create(om2.MFn.kMeshVertComponent)
            singleIdComp.addElements(vtxArry)

            infDags = skinFn.influenceObjects()
            infCount = len(infDags)
            if infCount == 0:
                continue
            infIndices = om2.MIntArray(infCount, 0)
            for x in range(infCount):
                infIndices[x] = int(skinFn.indexForInfluenceObject(infDags[x]))

            try:
                weights = skinFn.getWeights(meshPath, vertexComp)
            except Exception as e:
                print('get skin weight error :', '{}'.format(e))
                continue

            weights = self.conv_weight_shape(infCount, weights[0])
            influence_list = [infDags[x].fullPathName() for x in range(infCount)]

            self.node_vtx_dict[mesh_path_name] = vtxArry
            self.all_skin_clusters[mesh_path_name] = skinName
            self.mesh_node_list.append(mesh_path_name)
            self.inf_id_list.append(infIndices)
            self.node_weight_dict[mesh_path_name] = weights
            self.influences_dict[mesh_path_name] = influence_list
            self.all_influences += influence_list
            self.show_dict[mesh_path_name] = vtxArry

        self.all_influences = sorted(list(set(self.all_influences)))

    # ウェイトをバーテックス単位の 2 次元配列にリシェイプする
    def conv_weight_shape(self, shape, weights):
        if shape <= 0:
            return []
        return [[weights[i + j * shape] for i in range(shape)] for j in range(len(weights) // shape)]

    # 名前からシェイプを逆引きして返す
    def om_get_shape(self, name):
        sllist = om.MSelectionList()
        om.MGlobal.getSelectionListByName(name, sllist)
        dpg = om.MDagPath()
        sllist.getDagPath(0, dpg)
        return dpg
