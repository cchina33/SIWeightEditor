#-*-coding:utf-8 -*-
from maya import mel
from maya import cmds
import os
import json
import datetime as dt
from . import modeling
from . import common
from . import lang
from . import weight

def main(mesh=None, pop_zero_poly=False):
    cmds.selectMode(o=True)
    #pop_zero_poly→ゼロポリゴンメッシュを発見した場合に警告メッセージを出すかどうか
    msg01 = lang.Lang(
        en='There is a zero polygon object : ',
        ja=u'ゼロポリゴンのメッシュが存在します : ')   
    msg02 = lang.Lang(
        en='As it is in selected state, please process accordingly\n(Recommended to delete)',
        ja=u'選択状態になっているので適宜処理してください \n（削除推奨）')   
    if mesh is None:
        selection = cmds.ls(sl=True)
        selection_l = cmds.ls(sl=True, l=True)
    else:
        selection = mesh
        selection_l = cmds.ls(mesh, l=True)
    zero_mesh = modeling.cehck_zero_poly_object(mesh=selection, pop_msg=False)
    #リストタイプじゃなかったらリストに変換する
    if not isinstance(selection, list):
        temp = selection
        selection = []
        selection.append(temp)
    clusterCopy = modeling.ClusterCopy()
    engine = 'maya'
    for node, node_l in zip(selection, selection_l):
        if node in zero_mesh:
            print('Skip Zero Triangle Mesh :', node)
            continue
        #メッシュノードを含むオブジェクトのみ処理する。
        meshnode = cmds.listRelatives(node_l, s=True, pa=True, type='mesh', fullPath=True)
        if meshnode:
            defCls = clusterCopy.copy(node)
            bs_dict = store_blend_shape(node)
            #ブレンドシェイプを保存
            copyWeight(node_l, engine=engine )
            freezeModeling(node_l, engine=engine )
            if defCls:
                clusterCopy.paste(node)
            set_blend_shape(node, bs_dict)
    cmds.select(cl=True)
    for s in selection:
        try:
            cmds.select(s, add=True)
        except Exception as e:
            print('{}'.format(e))
    if zero_mesh and pop_zero_poly:
        msg = msg01.output()+str(len(zero_mesh))
        msg += '\n'+msg02.output()
        for p in zero_mesh:
            msg+='\n[ '+p+' ]'
        cmds.confirmDialog( title="Warning", message=msg )
        cmds.select(zero_mesh, r=True)
        
# ブレンドシェイプ情報を保存
def store_blend_shape(mesh):
    shapes = cmds.listRelatives(mesh, s=True, f=True) or []
    if not shapes:
        return {}
    skin = cmds.ls(cmds.listHistory(shapes) or [], type='skinCluster') or []
    if skin:
        connections = cmds.listConnections(skin, s=True, d=False) or []
    else:
        connections = cmds.listConnections(shapes, s=True, d=False) or []
    blend_shapes = cmds.ls(connections, l=True, type='blendShape') or [] if connections else []
    bs_dict = {}
    if blend_shapes:
        for bs in blend_shapes:
            bs_conns = cmds.listConnections(bs, s=True, d=False) or []
            shape_target = cmds.ls(bs_conns, l=True, type='transform') or [] if bs_conns else []
            if shape_target:
                bs_dict[bs] = shape_target
        return bs_dict
    return {}
    
def set_blend_shape(mesh, bs_dict):
    if not bs_dict:
        return
    for bs, shape_target in bs_dict.items():
        if not shape_target:
            continue
        cmds.blendShape(shape_target + [mesh], name=bs, frontOfChain=True)
    
def repareDagSetMember(node):
    # Dag セットメンバーの接続を修正する、シンメトリウェイトできないとき用。
    shadingEngin = get_shading_engines(node)
    if not shadingEngin:
        return
    shapes = cmds.listRelatives(node, s=True, pa=True, type='mesh') or []
    if not shapes:
        return
    connections = cmds.listConnections(shapes[0], d=True, s=False, p=True, c=True) or []
    conAttr = None
    for idx in range(1, len(connections), 2):
        con = connections[idx]
        if shadingEngin[0] + '.dagSetMembers' in con:
            conAttr = con
            disconAttr = connections[idx - 1]
            cmds.disconnectAttr(disconAttr, conAttr)
    if conAttr:
        cmds.connectAttr(shapes[0] + '.instObjGroups[0]', conAttr, f=True)
    
def copyWeight(node, engine='maya'):
    weight.WeightCopyPaste().main(node, mode='copy', saveName=__name__, engine=engine)
    
def freezeModeling(node, engine='maya'):
    #子供のノード退避用ダミーペアレントを用意
    dummy = common.TemporaryReparent().main(mode='create')
    common.TemporaryReparent().main(node,dummyParent=dummy, mode='cut')
    #ヒストリを全削除
    cmds.bakePartialHistory(node,pc=True)
    #ウェイトを書き戻してくる
    weight.WeightCopyPaste().main(node, mode='paste', saveName=__name__, engine=engine)
    #いらないシェイプを消す
    deleteZeroShape(node)
    #親子付けを戻す
    common.TemporaryReparent().main(node, dummyParent=dummy, mode='parent')
    #ダミーペアレントを削除
    common.TemporaryReparent().main(dummyParent=dummy, mode='delete')
        
# 接続の無い不要なシェイプを削除（唯一の可視シェイプは誤削除しない）
def deleteZeroShape(node):
    meshnode = cmds.listRelatives(node, s=True, pa=True, type='mesh', fullPath=True) or []
    if len(meshnode) <= 1:
        return
    visible_shapes = [
        m for m in meshnode
        if not cmds.getAttr(m + '.intermediateObject')
    ]
    for mesh in meshnode:
        is_intermediate = cmds.getAttr(mesh + '.intermediateObject')
        historyNode = cmds.listHistory(mesh, f=True) or []
        if len(historyNode) <= 1:
            if not is_intermediate and len(visible_shapes) <= 1:
                continue
            cmds.delete(mesh)
            if not is_intermediate and mesh in visible_shapes:
                visible_shapes.remove(mesh)
        
def save_cluster(node):
    #ノードの中からスキンクラスタを取得してくる#inMesh直上がSkinClusterとは限らないので修正
    srcDeformerCluster = cmds.ls(cmds.listHistory(node),type='cluster')
    if not srcDeformerCluster:
        return#スキンクラスタがなかったら関数抜ける
    #スキンクラスタのパラメータ色々を取得しておく
    srcDeformerCluster = srcDeformerCluster[0]
    attributes = cmds.listAttr(srcDeformerCluster)
    weightList = cmds.getAttr(srcDeformerCluster+'.weightList[0]')
    envelope = cmds.getAttr(srcDeformerCluster+'.envelope')
    clusterMssage = cmds.getAttr(srcDeformerCluster+'.message')
    clusterWeight = cmds.getAttr(srcDeformerCluster+'.weightList[0].weights')
    
def freeze():
    cmds.selectMode(o=True)
    selection = cmds.ls(sl=True, type='transform') or []
    if not selection:
        return
    dummy = common.TemporaryReparent().main(mode='create')  # モジュールでダミーの親作成
    clusterCopy = modeling.ClusterCopy()
    for sel in selection:
        relatives = cmds.listRelatives(sel, ad=True, f=True) or []
        allChildren = [sel] + relatives  # 子供を取得して一つのリストにする
        polyMesh = common.search_polygon_mesh(allChildren)
        if polyMesh:
            for mesh in polyMesh:
                common.TemporaryReparent().main(mesh, dummyParent=dummy, mode='cut')
                defCls = clusterCopy.copy(mesh)
                cmds.bakePartialHistory(mesh, pc=True)
                if defCls:
                    clusterCopy.paste(mesh)
                common.TemporaryReparent().main(mesh, dummyParent=dummy, mode='parent')  # コピーの終わったメッシュの子供を元に戻す
    common.TemporaryReparent().main(dummyParent=dummy, mode='delete')  # ダミー親削除
    cmds.select(selection, r=True)
    
def get_shading_engines(root_node=None):
    en_list = []
    if root_node is None:
        shapes = cmds.ls(type="mesh")
    else:
        shapes = cmds.listRelatives(root_node, ad=True, type="mesh") or []
    file_nodes = []
    for i in shapes:
        shading_engines = cmds.listConnections(i, type="shadingEngine") or []
        en_list+=shading_engines
    return list(set(en_list))