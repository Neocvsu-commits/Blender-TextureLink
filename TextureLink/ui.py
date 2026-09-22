import bpy


def _draw_update_banner(layout):
    """面板顶部：版本状态 + 刷新按钮。"""
    try:
        from .update_checker import get_update_info, get_check_status
    except ImportError:
        return

    row = layout.row(align=True)
    status = get_check_status("Neocvsu-commits", "Blender-TextureLink")
    st = status.get("status", "pending")

    if st == "checking":
        row.label(text="正在检查更新...", icon="SORTTIME")
    elif st == "error":
        row.label(text=f"更新检查失败: {status.get('error', '未知错误')}", icon="CANCEL")
    elif st == "no_release":
        row.label(text="暂无可获取的 Release", icon="INFO")
    elif st == "no_update" and status.get("current_version"):
        row.label(text=f"已是最新版本 v{status['current_version']}", icon="CHECKMARK")
    elif st == "pending":
        row.label(text="等待更新检查...", icon="TIME")

    row.operator("node.pbr_check_update", text="", icon="FILE_REFRESH")

    info = get_update_info("Neocvsu-commits", "Blender-TextureLink")
    if not info:
        return
    box = layout.box()
    box.alert = True
    col = box.column(align=True)
    col.label(text=f" 当前版本: v{info['current_version']}", icon="INFO")
    col.label(text=f" 最新版本: v{info['latest_version']}", icon="URL")
    row = col.row(align=True)
    row.operator("wm.url_open", text="查看 Release", icon="URL").url = info["html_url"]
    row.operator("node.pbr_install_update", text="一键更新", icon="IMPORT")


class NODE_PT_PBRMainPanel(bpy.types.Panel):
    bl_label = "贴图链接"
    bl_idname = "NODE_PT_pbr_main"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'

    def draw(self, context):
        _draw_update_banner(self.layout)


class NODE_PT_PBRCorePanel(bpy.types.Panel):
    bl_label = "贴图连接"
    bl_idname = "NODE_PT_pbr_core"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props

        def draw_action_row(layout, operator, icon, prop_name, override_text=""):
            row = layout.row(align=True)
            sub_left = row.row(align=True)
            sub_left.scale_x = 2.0
            if override_text:
                sub_left.operator(operator, icon=icon, text=override_text)
            else:
                sub_left.operator(operator, icon=icon)

            sub_right = row.row(align=True)
            sub_right.scale_x = 1.0
            sub_right.prop(props, prop_name, text="")

        draw_action_row(layout, "node.pbr_connect_arrange", 'NODETREE', "scope_connect", "自动连接贴图")
        draw_action_row(layout, "node.pbr_clean_nodes", 'TRASH', "scope_clean", "清除无用节点")
        draw_action_row(layout, "node.pbr_reset_nodes", 'FILE_REFRESH', "scope_reset", "重置材质节点")


class NODE_PT_PBRRenamePanel(bpy.types.Panel):
    bl_label = "命名规则"
    bl_idname = "NODE_PT_pbr_rename"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props

        tip = layout.box()
        tip.label(text="贴图命名可能是自定义，建议以贴图名同步材质。", icon='INFO')

        def draw_action_row(layout, operator, icon, prop_name, override_text=""):
            row = layout.row(align=True)
            sub_left = row.row(align=True)
            sub_left.scale_x = 2.0
            if override_text:
                sub_left.operator(operator, icon=icon, text=override_text)
            else:
                sub_left.operator(operator, icon=icon)

            sub_right = row.row(align=True)
            sub_right.scale_x = 1.0
            sub_right.prop(props, prop_name, text="")

        # 统一的命名规则按钮组（默认折叠）
        draw_action_row(layout, "node.pbr_rename_mat_from_obj", 'OUTLINER_OB_MESH', "scope_rename", "根据物体重命名材质")
        draw_action_row(layout, "node.pbr_rename_textures", 'SORT_DESC', "scope_rename", "基于材质球命名贴图")
        draw_action_row(layout, "node.pbr_rename_material", 'SORTALPHA', "scope_rename", "贴图命名同步至材质")

class NODE_PT_PBRPreviewPanel(bpy.types.Panel):
    bl_label = "图像预览"
    bl_idname = "NODE_PT_pbr_preview"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"

    def draw(self, context):
        layout = self.layout
        active_node = context.active_node
        if active_node and active_node.type == 'TEX_IMAGE':
            img = active_node.image
            if img:
                layout.template_ID_preview(active_node, "image", open="image.open")

                # 图片信息展示扩展
                box = layout.box()
                col = box.column(align=True)

                # 1. 尺寸、色深和格式 (例如: 2048 x 2048, RGB 字节型, sRGB)
                w, h = img.size
                depth = img.depth

                channels = "RGBA" if depth in (32, 64, 128) else "RGB"
                if img.file_format == 'JPEG':
                    depth_str = "字节型"
                else:
                    depth_str = f"字节型" if depth <= 32 else "浮点型"

                colorspace_val = img.colorspace_settings.name
                info_str = f"{w} × {h}, {channels} {depth_str}, {colorspace_val}"

                col.label(text=info_str, icon='INFO')

                col.separator()

                # 2. 色彩空间下拉框
                col.prop(img.colorspace_settings, "name", text="色彩空间")

                # 3. Alpha模式选择
                col.prop(img, "alpha_mode", text="Alpha")

            else:
                layout.label(text="无图像内容", icon='ERROR')
        else:
            layout.label(text="在节点树中选中贴图", icon='INFO')

class NODE_PT_PBRCompressPanel(bpy.types.Panel):
    bl_label = "图像压缩"
    bl_idname = "NODE_PT_pbr_compress"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props

        # 暂时隐藏"同步压缩到磁盘"入口，默认只改 Blender 内数据
        box = layout.box()
        box.label(text="仅修改当前 Blender 内数据，不影响磁盘原图", icon='CHECKMARK')

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "do_resize", text="启用尺寸压缩")
        if props.do_resize:
            col.prop(props, "compression_ratio", slider=True)
            col.prop(props, "min_resolution")

        layout.separator()

        def draw_action_row(layout, operator, icon, prop_name, override_text=""):
            row = layout.row(align=True)
            sub_left = row.row(align=True)
            sub_left.scale_x = 2.0
            if operator:
                sub_left.operator(operator, icon=icon, text=override_text)
            else:
                sub_left.label(text=override_text, icon=icon)

            sub_right = row.row(align=True)
            sub_right.scale_x = 1.0
            sub_right.prop(props, prop_name, text="")

        draw_action_row(layout, None, 'IMAGE_DATA', "target_format", "格式转换:")
        draw_action_row(layout, "node.pbr_compress_textures", 'PLAY', "scope_compress", "执行处理")
        draw_action_row(layout, "node.pbr_export_textures", 'FILE_FOLDER', "scope_export", "打包导出贴图")

        # 仅在已有历史导出路径时显示"一键打开目录"
        if props.last_export_path:
            row = layout.row()
            row.operator("node.pbr_open_export_folder", icon='FILE_FOLDER', text="打开刚才导出的文件夹")

class NODE_PT_PBRBatchPanel(bpy.types.Panel):
    bl_label = "贴图导入导出"
    bl_idname = "NODE_PT_pbr_batch"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'PBR Tool'
    bl_parent_id = "NODE_PT_pbr_main"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pbr_v2_props

        def draw_action_row(layout, operator, icon, prop_name, override_text=""):
            row = layout.row(align=True)
            sub_left = row.row(align=True)
            sub_left.scale_x = 2.0
            sub_left.operator(operator, icon=icon, text=override_text)

            sub_right = row.row(align=True)
            sub_right.scale_x = 1.0
            sub_right.prop(props, prop_name, text="")

        draw_action_row(layout, "node.pbr_batch_folder_connect", 'FILE_FOLDER', "scope_connect", "基于名称匹配文件夹贴图")
        draw_action_row(layout, "node.pbr_export_current_textures", 'EXPORT', "scope_rename", "导出当前修改贴图")
        draw_action_row(layout, "node.pbr_rename_textures_sync_disk", 'FILE_TICK', "scope_rename", "同步当前修改贴图至磁盘")

        # ---- 反馈入口 ----
        layout.separator()
        feedback_box = layout.box()
        feedback_box.label(text="反馈 & 支持", icon="HELP")
        fb_row = feedback_box.row(align=True)
        fb_row.operator(
            "wm.url_open",
            text="Bug / 功能建议",
            icon="GHOST_ENABLED",
        ).url = "https://github.com/Neocvsu-commits/Blender-TextureLink/issues/new"
        fb_row.operator(
            "wm.url_open",
            text="匿名反馈",
            icon="COMMUNITY",
        ).url = "https://docs.qq.com/form/page/DTnV6S25STkxJR0Zy"
        fb_row2 = feedback_box.row()
        fb_row2.operator(
            "wm.url_open",
            text="⭐ 作者主页（了解更多工具）",
            icon="URL",
        ).url = "https://github.com/Neocvsu-commits"
