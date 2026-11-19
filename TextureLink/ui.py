import bpy

from .update_checker import get_check_status, get_update_info


_UPDATE_REPO = ("Neocvsu-commits", "Blender-TextureLink")


def _refresh_update_result():
    if get_check_status(*_UPDATE_REPO)["status"] == "checking":
        return 0.4
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {'NODE_EDITOR', 'PREFERENCES'}:
                area.tag_redraw()
    return None


def watch_update_check():
    if not bpy.app.background and not bpy.app.timers.is_registered(_refresh_update_result):
        bpy.app.timers.register(_refresh_update_result, first_interval=0.4)


def stop_update_redraw():
    if bpy.app.timers.is_registered(_refresh_update_result):
        bpy.app.timers.unregister(_refresh_update_result)


def _draw_update_controls(layout, *, show_error_detail=False):
    from . import bl_info

    status = get_check_status(*_UPDATE_REPO)
    state = status["status"]
    row = layout.row(align=True)
    row.alignment = 'LEFT'
    row.label(text="v" + ".".join(map(str, bl_info["version"])), icon='INFO')
    row.operator("node.pbr_check_update", text="检查更新", icon='FILE_REFRESH')
    if state == "checking":
        row.label(text="检查中…", icon='SORTTIME')
        watch_update_check()
    elif state == "no_update":
        row.label(text="已是最新版", icon='CHECKMARK')
    elif state == "no_release":
        row.label(text="暂无发布版本", icon='INFO')
    elif state == "error":
        layout.label(text="更新检查失败，请重试", icon='ERROR')
        if show_error_detail and status["error"]:
            layout.label(text=status["error"])
    elif state == "has_update":
        info = get_update_info(*_UPDATE_REPO)
        if info:
            update_row = layout.row(align=True)
            update_row.alert = True
            update_row.label(text=f"可更新至 v{info['latest_version']}", icon='IMPORT')
            update_row.operator("node.pbr_install_update", text="一键更新", icon='IMPORT')
            update_row.operator("wm.url_open", text="更新说明", icon='URL').url = info["html_url"]


def _draw_scoped_action(layout, props, operator, text, icon, scope):
    row = layout.split(factor=0.68, align=True)
    row.operator(operator, text=text, icon=icon)
    row.prop(props, scope, text="")


def _redraw_node_editors(self, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


class TextureLinkPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    show_node_previews: bpy.props.BoolProperty(
        name="节点贴图预览",
        description="在着色器编辑器的图像纹理节点上显示缩略图",
        default=True,
        update=_redraw_node_editors,
    )

    def draw(self, context):
        layout = self.layout
        _draw_update_controls(layout, show_error_detail=True)
        layout.separator()
        layout.prop(self, "show_node_previews")

        layout.separator()
        row = layout.row(align=True)
        row.operator("wm.url_open", text="问题与建议", icon='HELP').url = (
            "https://github.com/Neocvsu-commits/Blender-TextureLink/issues/new"
        )
        row.operator("wm.url_open", text="匿名反馈", icon='COMMUNITY').url = (
            "https://docs.qq.com/form/page/DTnV6S25STkxJR0Zy"
        )
        row.operator("wm.url_open", text="作者主页", icon='URL').url = (
            "https://github.com/Neocvsu-commits"
        )


class NODE_PT_PBRMainPanel(bpy.types.Panel):
    bl_label = "贴图链接"
    bl_idname = "NODE_PT_pbr_main"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'

    def draw(self, context):
        layout = self.layout
        _draw_update_controls(layout)
        props = context.scene.pbr_v2_props
        _draw_scoped_action(layout, props, "node.pbr_connect_arrange", "自动连接贴图", 'NODETREE', "scope_connect")
        _draw_scoped_action(layout, props, "node.pbr_arrange_texture_links", "整理贴图连线", 'ALIGN_JUSTIFY', "scope_arrange")

        addon = context.preferences.addons.get(__package__)
        if addon:
            layout.prop(addon.preferences, "show_node_previews")

class _TextureLinkSubpanel:
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"
    bl_options = {'DEFAULT_CLOSED'}


class NODE_PT_PBRCorePanel(_TextureLinkSubpanel, bpy.types.Panel):
    bl_label = "节点整理"
    bl_idname = "NODE_PT_pbr_core"

    def draw(self, context):
        props = context.scene.pbr_v2_props
        col = self.layout.column(align=True)
        _draw_scoped_action(col, props, "node.pbr_clean_nodes", "清除无用节点", 'TRASH', "scope_clean")
        _draw_scoped_action(col, props, "node.pbr_reset_nodes", "重置材质节点", 'FILE_REFRESH', "scope_reset")


class NODE_PT_PBRRenamePanel(_TextureLinkSubpanel, bpy.types.Panel):
    bl_label = "重命名"
    bl_idname = "NODE_PT_pbr_rename"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props
        layout.prop(props, "scope_rename", text="范围")
        col = layout.column(align=True)
        col.operator("node.pbr_unify_names", text="按物体命名", icon='OUTLINER_OB_MESH').source = 'OBJECT'
        col.operator("node.pbr_unify_names", text="按材质球命名", icon='MATERIAL').source = 'MATERIAL'
        col.operator("node.pbr_unify_names", text="按贴图命名", icon='IMAGE_DATA').source = 'TEXTURE'


class NODE_PT_PBRPreviewPanel(_TextureLinkSubpanel, bpy.types.Panel):
    bl_label = "图像属性"
    bl_idname = "NODE_PT_pbr_preview"

    @classmethod
    def poll(cls, context):
        node = getattr(context, "active_node", None)
        return node is not None and node.bl_idname == 'ShaderNodeTexImage'

    def draw(self, context):
        layout = self.layout
        node = context.active_node
        layout.template_ID_preview(node, "image", open="image.open")
        image = node.image
        if image:
            width, height = image.size
            channels = {3: "RGB", 4: "RGBA"}.get(image.channels, f"{image.channels} 通道")
            storage = "浮点" if image.is_float else "整数"
            layout.label(text=f"{width} × {height} · {channels} · {image.depth} 位/像素（{storage}）")
            layout.prop(image.colorspace_settings, "name", text="色彩空间")
            layout.prop(image, "alpha_mode", text="Alpha")


class NODE_PT_PBRCompressPanel(_TextureLinkSubpanel, bpy.types.Panel):
    bl_label = "图像压缩"
    bl_idname = "NODE_PT_pbr_compress"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props
        layout.prop(props, "do_resize", text="缩放尺寸")
        if props.do_resize:
            col = layout.column(align=True)
            col.prop(props, "compression_ratio", slider=True)
            col.prop(props, "min_resolution")
        layout.prop(props, "target_format")
        _draw_scoped_action(layout, props, "node.pbr_compress_textures", "应用处理", 'PLAY', "scope_compress")


class NODE_PT_PBRBatchPanel(_TextureLinkSubpanel, bpy.types.Panel):
    bl_label = "贴图导入导出"
    bl_idname = "NODE_PT_pbr_batch"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props
        layout.operator("node.pbr_batch_folder_connect", text="匹配文件夹贴图", icon='FILE_FOLDER')
        col = layout.column(align=True)
        _draw_scoped_action(col, props, "node.pbr_export_textures", "打包导出贴图", 'PACKAGE', "scope_export")
        _draw_scoped_action(col, props, "node.pbr_export_current_textures", "导出当前贴图", 'EXPORT', "scope_rename")
        if props.last_export_path:
            col.operator("node.pbr_open_export_folder", text="打开导出目录", icon='FILE_FOLDER')
        layout.separator()
        _draw_scoped_action(layout, props, "node.pbr_rename_textures_sync_disk", "重命名并写回磁盘", 'FILE_TICK', "scope_rename")
