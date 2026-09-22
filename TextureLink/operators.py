import bpy
import os
import shutil
import sys
import subprocess
import tempfile
from bpy.props import StringProperty

from .utils import (
    get_target_materials, process_reset_material, process_connect_and_arrange,
    process_clean_unused_nodes, process_rename_material, find_images_recursive,
    get_clean_name_for_search, check_suffix, SUFFIX_MAP, connect_pbr_nodes,
    process_rename_textures_from_material,
    process_rename_materials_from_object,
    get_compress_target_materials,
    collect_images_from_materials,
    ensure_image_pixels_loaded,
    save_image_to_path_verified,
    read_disk_image_size,
    fuzzy_match_core_name,
    write_image_memory_to_path,
)


def _bake_image_to_packed(img, fmt, target_ext, scene=None):
    """把当前内存像素以 fmt 烘焙进 packed_file。

  流程：save_render(内存→临时文件) → filepath 指向临时文件 → reload → pack → 清空 filepath。
    glb 打包贴图不能用 img.save()（会写出旧 packed 字节）；pack 后不能指向不存在的磁盘路径（会变粉）。
    """
    if not img:
        return False
    if not ensure_image_pixels_loaded(img):
        print(f"[PBR Tools] 烘焙跳过（像素未载入） {getattr(img, 'name', '?')}")
        return False

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=target_ext)
        os.close(fd)

        img.file_format = fmt
        img.save_render(tmp, scene=scene)
        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            print(f"[PBR Tools] 烘焙临时文件无效 {getattr(img, 'name', '?')}")
            return False

        old_fp = img.filepath
        old_raw = getattr(img, "filepath_raw", None)
        img.filepath = tmp
        if old_raw is not None:
            img.filepath_raw = tmp
        img.reload()

        if img.size[0] == 0 or img.size[1] == 0:
            print(f"[PBR Tools] reload 后像素为空 {getattr(img, 'name', '?')}")
            return False

        img.pack()
        img.file_format = fmt

        try:
            os.remove(tmp)
        except OSError:
            pass
        tmp = None

        # 打包后清空外部路径，避免指向不存在文件触发重载丢弃 packed 数据
        try:
            img.filepath = ""
            if old_raw is not None:
                img.filepath_raw = ""
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[PBR Tools] 烘焙打包失败 {getattr(img, 'name', '?')}: {e}")
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _export_textures_for_materials_to_dir(context, mats, directory, update_last_path=True):
    """
    将材质节点树内引用的贴图写出到 directory。
    使用当前 Image 数据块名称与内存中的像素（尺寸），与「打包导出贴图」行为一致。
    返回 (成功张数, 错误信息或 None)
    """
    if not directory:
        return 0, "路径为空"
    if not os.path.exists(directory):
        try:
            os.makedirs(directory)
        except Exception as e:
            return 0, str(e)

    images_to_process = collect_images_from_materials(mats)
    if not images_to_process:
        return 0, "未找到贴图"

    count = 0
    for img in images_to_process:
        if img.source != "FILE" and not img.packed_file:
            continue

        ext = os.path.splitext(img.name)[1]
        if not ext:
            if img.file_format == "JPEG":
                ext = ".jpg"
            elif img.file_format == "PNG":
                ext = ".png"
            elif img.file_format == "TARGA":
                ext = ".tga"
            elif img.file_format == "TIFF":
                ext = ".tif"
            elif img.file_format == "BMP":
                ext = ".bmp"
            elif img.file_format == "OPEN_EXR":
                ext = ".exr"
            else:
                ext = ".png"

        base_name = os.path.splitext(img.name)[0]
        export_path = os.path.join(directory, base_name + ext)

        is_exported = False
        original_filepath = img.filepath
        original_raw = getattr(img, "filepath_raw", None)
        real_filepath = bpy.path.abspath(img.filepath) if img.filepath else ""

        use_memory = bool(
            getattr(img, "packed_file", None)
            or getattr(img, "has_data", False)
            or img.get("_pbr_unsynced_resize")
        )
        if use_memory:
            is_exported = write_image_memory_to_path(img, export_path, scene=context.scene)
        if not is_exported:
            try:
                img.filepath = export_path
                if original_raw is not None:
                    img.filepath_raw = export_path
                img.save()
                is_exported = True
            except Exception as e:
                print(f"[PBR Tools] 导出当前图像数据失败 {img.name}: {e}")
                if real_filepath and os.path.exists(real_filepath):
                    try:
                        shutil.copy2(real_filepath, export_path)
                        is_exported = True
                    except Exception as copy_e:
                        print(f"[PBR Tools] 兜底拷贝失败 {img.name}: {copy_e}")
            finally:
                img.filepath = original_filepath
                if original_raw is not None:
                    img.filepath_raw = original_raw

        if is_exported:
            count += 1

    if update_last_path and count and hasattr(context.scene, "pbr_v2_props"):
        context.scene.pbr_v2_props.last_export_path = directory
    return count, None


class NODE_OT_PBRResetNodes(bpy.types.Operator):
    """重置所有节点，只保留原理化BSDF和输出"""
    bl_idname = "node.pbr_reset_nodes"
    bl_label = "重置材质节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}
            
        scope = context.scene.pbr_v2_props.scope_reset
        mats = get_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质")
            return {'CANCELLED'}
            
        count = 0
        for mat in mats:
            if process_reset_material(mat):
                count += 1
                
        self.report({'INFO'}, f"成功重置了 {count} 个材质")
        return {'FINISHED'}

class NODE_OT_PBRConnectArrange(bpy.types.Operator):
    """自动基于贴图后缀连接PBR通道，并将其整齐排列"""
    bl_idname = "node.pbr_connect_arrange"
    bl_label = "自动连接贴图"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}
            
        scope = context.scene.pbr_v2_props.scope_connect
        mats = get_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质")
            return {'CANCELLED'}
            
        count = 0
        for mat in mats:
            if process_connect_and_arrange(mat, context):
                count += 1
                
        self.report({'INFO'}, f"成功连接并排版了 {count} 个材质")
        return {'FINISHED'}

class NODE_OT_PBRCleanNodes(bpy.types.Operator):
    """删除材质内没有连接到最终输出端口的游离节点"""
    bl_idname = "node.pbr_clean_nodes"
    bl_label = "清除无用节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}
            
        scope = context.scene.pbr_v2_props.scope_clean
        mats = get_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质")
            return {'CANCELLED'}
            
        count = 0
        for mat in mats:
            if process_clean_unused_nodes(mat):
                count += 1
                
        self.report({'INFO'}, f"成功清理了 {count} 个材质中的无用节点")
        return {'FINISHED'}

class NODE_OT_PBRRenameMaterial(bpy.types.Operator):
    """根据优先级最高的贴图名称自动重命名材质球"""
    bl_idname = "node.pbr_rename_material"
    bl_label = "根据贴图重命名"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}
            
        props = context.scene.pbr_v2_props
        scope = props.scope_rename
        mats = get_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质")
            return {'CANCELLED'}
            
        count = 0
        renamed_warning = []
        for mat in mats:
            expected_name = None
            # 先取一下期望值，通过暂存名判断是否被 Blender 自动改了后缀
            old_name = mat.name
            if process_rename_material(mat):
                count += 1
                # 检测实际名称是否与期望不符（说明有重名，被加了.001之类后缀）
                # process_rename_material 内部已打印 print，这里收集给 report
                # 如果名字不以原名开头而是含 . 则是 Blender 自动追加后缀
                if '.' in mat.name.split('_', 1)[-1]:
                    renamed_warning.append(mat.name)

        msg = f"成功重命名了 {count} 个材质"
        if renamed_warning:
            msg += f"（注意: {len(renamed_warning)} 个材质因重名被改为含 '.001' 后缀）"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class NODE_OT_PBRRenameMaterialFromObject(bpy.types.Operator):
    """根据物体名重命名材质"""
    bl_idname = "node.pbr_rename_mat_from_obj"
    bl_label = "根据物体名重命名材质"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context is not None and (context.active_object or context.selected_objects)

    def execute(self, context):
        scope = context.scene.pbr_v2_props.scope_rename

        if scope == 'CURRENT':
            objects = [context.active_object] if context.active_object else []
        else:
            objects = list(context.selected_objects) if context.selected_objects else []

        processed_obj_count = 0
        renamed_mat_count = 0

        for obj in objects:
            if not obj or not hasattr(obj, "material_slots"):
                continue
            # 只统计“实际包含材质”的物体为 processed
            if not any(slot.material is not None for slot in obj.material_slots):
                continue

            processed_obj_count += 1
            renamed_mat_count += process_rename_materials_from_object(obj)

        self.report({'INFO'}, f"完成：处理 {processed_obj_count} 个物体，重命名 {renamed_mat_count} 个材质")
        return {'FINISHED'}


class NODE_OT_PBRRenameTextures(bpy.types.Operator):
    """根据材质球名称逆向重命名其连接贴图（Image Datablock）"""
    bl_idname = "node.pbr_rename_textures"
    bl_label = "根据材质重命名贴图"
    bl_options = {'REGISTER', 'UNDO'}

    def _execute_rename(self, context, sync_disk_files=False):
        props = context.scene.pbr_v2_props
        scope = props.scope_rename
        mats = get_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质")
            return {'CANCELLED'}

        original_sync_flag = bool(getattr(props, "rename_disk_files", False))
        props.rename_disk_files = bool(sync_disk_files)
        try:
            mat_count = 0
            tex_count = 0
            disk_count = 0
            pixel_count = 0
            for mat in mats:
                renamed, disk_renamed, disk_px = process_rename_textures_from_material(
                    mat, props, context.scene
                )
                if renamed > 0 or disk_renamed > 0 or disk_px > 0:
                    mat_count += 1
                tex_count += renamed
                disk_count += disk_renamed
                pixel_count += disk_px
        finally:
            props.rename_disk_files = original_sync_flag

        if sync_disk_files:
            self.report(
                {"INFO"},
                f"完成（含磁盘同步）：{mat_count} 个材质，数据块重命名 {tex_count} 张，磁盘重命名 {disk_count} 个，分辨率写回 {pixel_count} 张",
            )
        else:
            self.report(
                {"INFO"},
                f"完成：{mat_count} 个材质，重命名 {tex_count} 张贴图",
            )
        return {"FINISHED"}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}

        return self._execute_rename(context, sync_disk_files=False)


class NODE_OT_PBRRenameTexturesSyncDisk(bpy.types.Operator):
    """根据材质球名称重命名贴图，并同步重命名磁盘文件及路径"""
    bl_idname = "node.pbr_rename_textures_sync_disk"
    bl_label = "根据材质重命名并同步磁盘"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}

        return NODE_OT_PBRRenameTextures._execute_rename(self, context, sync_disk_files=True)

class NODE_OT_PBRBatchFolderConnect(bpy.types.Operator):
    """选择贴图文件夹，自动匹配选中的所有物体的材质并连接贴图"""
    bl_idname = "node.pbr_batch_folder_connect"
    bl_label = "基于名称匹配文件夹贴图"
    bl_options = {'REGISTER', 'UNDO'}

    # 使用正确的目录选择器而非 ImportHelper（ImportHelper 是文件选择器）
    directory: StringProperty(
        name="贴图文件夹",
        description="选择包含贴图的文件夹",
        subtype='DIR_PATH'
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        dir_path = self.directory
        if not dir_path or not os.path.isdir(dir_path):
            self.report({'ERROR'}, "路径无效")
            return {'CANCELLED'}
        
        selected_objs = context.selected_objects
        if not selected_objs:
            self.report({'ERROR'}, "请先选择至少一个物体")
            return {'CANCELLED'}

        try:
            all_files = os.listdir(dir_path)
        except Exception as e:
            self.report({'ERROR'}, f"无法读取文件夹: {str(e)}")
            return {'CANCELLED'}

        match_count = 0
        processed_materials = set() 
        
        for obj in selected_objs:
            if not hasattr(obj, "material_slots"):
                continue

            for slot in obj.material_slots:
                mat = slot.material
                if not mat: continue
                
                if mat in processed_materials:
                    continue
                processed_materials.add(mat)
                
                if not mat.use_nodes:
                    mat.use_nodes = True
                
                nodes = mat.node_tree.nodes
                
                mat_name = mat.name
                clean_mat_name = mat_name.split('.')[0]  # 去掉 .001 后缀

                # 模糊匹配：忽略 T_/MI_/M_/SM_ 前缀差异，精确优先+子串回退
                matched_files = []
                match_scores = {}  # f_name -> score, 用于排序取最佳匹配
                for f in all_files:
                    f_lower = f.lower()
                    if not f_lower.endswith(('.png', '.jpg', '.jpeg', '.tga', '.exr', '.tif', '.tiff')):
                        continue
                    f_stem = os.path.splitext(f)[0]
                    score = fuzzy_match_core_name(clean_mat_name, f_stem)
                    if score > 0:
                        matched_files.append(f)
                        match_scores[f] = score

                # 按匹配质量降序排列，优先用最佳匹配
                matched_files.sort(key=lambda x: match_scores.get(x, 0), reverse=True)
                
                if not matched_files:
                    continue

                principled = None
                for n in nodes:
                    if n.type == 'BSDF_PRINCIPLED':
                        principled = n
                        break
                
                if not principled:
                    principled = nodes.new('ShaderNodeBsdfPrincipled')
                    principled.location = (0, 0)
                
                for f_name in matched_files:
                    full_path = os.path.join(dir_path, f_name)
                    
                    img = None
                    for existing_img in bpy.data.images:
                        # 使用 abspath 规范化路径再比较，避免相对路径 vs 绝对路径不一致
                        if bpy.path.abspath(existing_img.filepath) == full_path:
                            img = existing_img
                            # 复用已加载 Image datablock 时，强制从磁盘刷新尺寸/像素，
                            # 避免此前压缩后的缓存尺寸（如 1K）残留。
                            try:
                                img.reload()
                            except Exception:
                                pass
                            break
                    if not img:
                        try:
                            img = bpy.data.images.load(full_path)
                        except Exception as e:
                            print(f"[PBR Tools] 加载图片失败 {f_name}: {e}")
                            continue
                    
                    tex_node = nodes.new('ShaderNodeTexImage')
                    tex_node.image = img
                
                # 直接调用 process_connect_and_arrange 统一处理连接和排版
                # （它会扫描材质内所有 TEX_IMAGE 节点，无需手动预连接）
                process_connect_and_arrange(mat, context)
                match_count += 1

        self.report({'INFO'}, f"处理完成，共匹配了 {match_count} 个材质")
        return {'FINISHED'}

class NODE_OT_PBRCompressTextures(bpy.types.Operator):
    """递归追踪并智能压缩贴图。
    - 支持同步压缩到磁盘贴图文件。
    - 也支持仅更新当前 Blender 内存/打包数据。"""
    bl_idname = "node.pbr_compress_textures"
    bl_label = "执行处理"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if not context.active_object:
            self.report({'WARNING'}, "请先选择一个物体")
            return {'CANCELLED'}
            
        props = context.scene.pbr_v2_props
        scope = props.scope_compress
        mats = get_compress_target_materials(context, scope)
        if not mats:
            self.report({'WARNING'}, "未找到可操作的材质（当前范围下无材质槽）")
            return {'CANCELLED'}

        # 收集节点树内全部 Image Texture，避免仅连到原理化 BSDF 输入才生效
        images_to_process = collect_images_from_materials(mats)
        if not images_to_process:
            self.report({'WARNING'}, "目标材质中未找到 Image Texture 节点或未启用节点")
            return {'CANCELLED'}

        processed_count = 0
        converted_count = 0
        resized_count = 0
        disk_sync_failed = []
        skipped_no_pixels = []
        resize_blocked_by_min = []
        resize_failed_runtime = []

        ext_map = {
            'JPEG': '.jpg', 'PNG': '.png', 'TARGA': '.tga',
            'TIFF': '.tif', 'BMP': '.bmp', 'OPEN_EXR': '.exr'
        }

        for img in images_to_process:
            if img.size[0] == 0 or img.size[1] == 0:
                continue
            # 跳过纯程序化贴图（没有文件来源也没有打包数据）
            if img.source not in ('FILE', 'GENERATED') and not img.packed_file:
                continue

            if not ensure_image_pixels_loaded(img):
                skipped_no_pixels.append(img.name)
                continue

            is_modified = False
            baked = False
            original_filepath = img.filepath
            source_abs_path = bpy.path.abspath(original_filepath) if original_filepath else ""
            desired_ext = (
                os.path.splitext(source_abs_path)[1].lower()
                or os.path.splitext(img.filepath)[1].lower()
                or os.path.splitext(img.name)[1].lower()
                or ".png"
            )

            # Step 1: 尺寸压缩（在内存中操作）
            if props.do_resize:
                w, h = img.size
                user_ratio = props.compression_ratio
                # 等比缩放：输出宽高均不低于 min_resolution（与旧逻辑等价，写法更清晰）
                min_r = props.min_resolution
                floor_ratio = max(min_r / w, min_r / h) if w > 0 and h > 0 else 1.0
                effective_ratio = max(user_ratio, floor_ratio)
                effective_ratio = min(effective_ratio, 1.0)

                new_w = max(1, int(w * effective_ratio))
                new_h = max(1, int(h * effective_ratio))

                if new_w < w or new_h < h:
                    try:
                        img.scale(new_w, new_h)
                        img.update()
                        # 某些文件状态下首次 scale 可能未真正生效，做一次兜底重试
                        if img.size[0] == w and img.size[1] == h:
                            try:
                                img.reload()
                            except Exception:
                                pass
                            img.scale(new_w, new_h)
                            img.update()
                        if img.size[0] < w or img.size[1] < h:
                            resized_count += 1
                            is_modified = True
                        else:
                            resize_failed_runtime.append(img.name)
                    except Exception as e:
                        print(f"[PBR Tools] 尺寸压缩失败 {img.name}: {e}")
                        resize_failed_runtime.append(img.name)
                elif user_ratio < 1.0 - 1e-6:
                    resize_blocked_by_min.append(img.name)

            # Step 2: 格式转换 — 通过临时文件烘焙出目标格式的 packed 数据
            if props.target_format != 'CURRENT':
                fmt = props.target_format
                target_ext = ext_map.get(fmt, '.png')
                current_name = img.name
                if os.path.splitext(current_name)[1].lower() != target_ext:
                    if _bake_image_to_packed(img, fmt, target_ext, context.scene):
                        new_name = os.path.splitext(current_name)[0] + target_ext
                        try:
                            img.name = new_name
                        except Exception:
                            pass
                        desired_ext = target_ext
                        is_modified = True
                        baked = True
                        converted_count += 1
            
            needs_disk_sync = bool(img.get("_pbr_unsynced_resize", False))
            # 防止历史标记导致“空转写盘”：若当前尺寸与磁盘一致，则清理标记并跳过补同步
            if needs_disk_sync and source_abs_path:
                disk_size = read_disk_image_size(source_abs_path)
                current_size = (int(img.size[0]), int(img.size[1]))
                if disk_size and disk_size == current_size:
                    needs_disk_sync = False
                    try:
                        del img["_pbr_unsynced_resize"]
                    except Exception:
                        pass
            should_commit = is_modified or (props.compress_disk_files and needs_disk_sync)

            if should_commit:
                if props.compress_disk_files:
                    if source_abs_path:
                        if not os.path.isfile(source_abs_path):
                            disk_sync_failed.append(img.name)
                            continue
                        base_no_ext, _ = os.path.splitext(source_abs_path)
                        target_abs_path = base_no_ext + (desired_ext or ".png")
                        if not save_image_to_path_verified(img, target_abs_path, context.scene):
                            disk_sync_failed.append(img.name)
                        else:
                            if os.path.normcase(target_abs_path) != os.path.normcase(source_abs_path):
                                if os.path.isfile(source_abs_path):
                                    try:
                                        os.remove(source_abs_path)
                                    except OSError as rm_e:
                                        print(f"[PBR Tools] 删除旧格式贴图失败 {source_abs_path}: {rm_e}")
                            if "_pbr_unsynced_resize" in img:
                                try:
                                    del img["_pbr_unsynced_resize"]
                                except Exception:
                                    pass
                    else:
                        disk_sync_failed.append(img.name)
                else:
                    # 不写盘：把当前内存缓冲烘焙成 packed 数据，保证视口与导出都拿到修改后的像素。
                    # 直接 img.pack() 对 source='FILE' 且磁盘原图仍在的贴图会重读原图、丢失缩放，
                    # 故统一走 _bake_image_to_packed（缩放-only 时按当前 file_format 烘焙）。
                    if is_modified and not baked:
                        cur_fmt = img.file_format or 'PNG'
                        cur_ext = ext_map.get(cur_fmt, os.path.splitext(img.name)[1].lower() or '.png')
                        if not _bake_image_to_packed(img, cur_fmt, cur_ext, context.scene):
                            try:
                                img.pack()
                            except Exception as e:
                                print(f"[PBR Tools] 内存更新后打包失败 {img.name}: {e}")
                    if is_modified:
                        img["_pbr_unsynced_resize"] = True

                processed_count += 1

        if props.compress_disk_files:
            msg = f"处理完成: {processed_count} 张贴图已处理（实际缩放 {resized_count} 张，格式转换 {converted_count} 张）并同步到磁盘"
            if disk_sync_failed:
                msg += f"，{len(disk_sync_failed)} 张同步失败（可能是打包贴图或无有效磁盘路径）"
        else:
            msg = f"处理完成: {processed_count} 张贴图已在 Blender 内更新（实际缩放 {resized_count} 张，格式转换 {converted_count} 张）"
        if skipped_no_pixels:
            msg += f"；{len(skipped_no_pixels)} 张无法载入像素已跳过"
        if resize_blocked_by_min:
            msg += f"；{len(resize_blocked_by_min)} 张因最小分辨率限制未缩小（可下调「最小分辨率」或换更大贴图）"
        if resize_failed_runtime:
            msg += f"；{len(resize_failed_runtime)} 张缩放调用后尺寸未变化（建议检查文件读写权限/贴图状态）"
        if processed_count == 0 and images_to_process:
            self.report({'WARNING'}, f"{msg}（若尺寸未变：检查压缩比例/最小分辨率是否阻止缩小，或贴图路径是否缺失）")
            return {'FINISHED'}
        self.report({'INFO'}, msg)
        return {'FINISHED'}



class NODE_OT_PBRExportTextures(bpy.types.Operator):
    """提取场景或选定物体涉及到的关联贴图，打包导出至指定系统文件夹（操作不可撤销）"""
    bl_idname = "node.pbr_export_textures"
    bl_label = "导出打包贴图"
    bl_options = {'REGISTER'}  # 去掉 UNDO：文件系统操作无法通过 Blender Undo 恢复

    directory: StringProperty(
        name="保存路径",
        description="选择要保存贴图的目录",
        subtype='DIR_PATH'
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            return {"CANCELLED"}

        if not context.active_object and context.scene.pbr_v2_props.scope_export == "SELECTED":
            self.report({"WARNING"}, "请先选择一个物体")
            return {"CANCELLED"}

        scope = context.scene.pbr_v2_props.scope_export
        if scope == "SELECTED":
            mats = get_compress_target_materials(context, "ALL")
            if not mats and context.active_object:
                mats = get_compress_target_materials(context, "CURRENT")
        else:
            mats = list(bpy.data.materials)

        if not mats:
            self.report({"WARNING"}, "未找到可供操作的材质")
            return {"CANCELLED"}

        count, err = _export_textures_for_materials_to_dir(
            context, mats, self.directory, update_last_path=True
        )
        if err == "未找到贴图":
            self.report({"WARNING"}, "目标材质中未找到 Image Texture 节点")
            return {"CANCELLED"}
        if count == 0:
            self.report(
                {"WARNING"},
                "没有可导出的贴图（需为外部文件或已打包图像）",
            )
            return {"CANCELLED"}

        self.report({"INFO"}, f"成功向目标目录打出了 {count} 张关联贴图!")
        return {"FINISHED"}


class NODE_OT_PBRExportCurrentTextures(bpy.types.Operator):
    """
    导出当前选中/活动物体上材质所连贴图：文件名与当前 Image 名一致，像素与当前内存一致。
    范围由「贴图导入导出」面板的 当前/全部 下拉（scope_rename）控制。
    """

    bl_idname = "node.pbr_export_current_textures"
    bl_label = "导出当前修改贴图"
    bl_options = {"REGISTER"}

    directory: StringProperty(
        name="保存路径",
        description="选择要保存贴图的目录",
        subtype="DIR_PATH",
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.directory:
            return {"CANCELLED"}

        props = context.scene.pbr_v2_props
        mode = props.scope_rename

        if not context.active_object:
            self.report({"WARNING"}, "请先选择一个物体")
            return {"CANCELLED"}

        if mode == "CURRENT":
            mats = get_compress_target_materials(context, "CURRENT")
        else:
            mats = get_compress_target_materials(context, "ALL")
            if not mats and context.active_object:
                mats = get_compress_target_materials(context, "CURRENT")

        if not mats:
            self.report({"WARNING"}, "未找到可供操作的材质")
            return {"CANCELLED"}

        count, err = _export_textures_for_materials_to_dir(
            context, mats, self.directory, update_last_path=True
        )
        if err and count == 0:
            self.report({"WARNING"}, "未找到可导出的贴图（请确认材质中有图像节点，且非纯程序贴图）")
            return {"CANCELLED"}
        if count == 0:
            self.report(
                {"WARNING"},
                "没有成功导出任何贴图（可能均为程序纹或无打包/文件来源）",
            )
            return {"CANCELLED"}

        self.report({"INFO"}, f"已导出 {count} 张贴图（当前命名与尺寸）")
        return {"FINISHED"}


class NODE_OT_PBROpenExportFolder(bpy.types.Operator):
    """打开上一次导出的贴图文件夹"""
    bl_idname = "node.pbr_open_export_folder"
    bl_label = "打开导出文件夹"
    bl_options = {'REGISTER'}

    def execute(self, context):
        path = (context.scene.pbr_v2_props.last_export_path or "").strip()
        if not path or not os.path.isdir(path):
            self.report({'WARNING'}, "找不到路径或尚未导出")
            return {'CANCELLED'}

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.call(["open", path])
            else:
                subprocess.call(["xdg-open", path])
        except Exception as e:
            self.report({'ERROR'}, f"打开文件夹失败: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, "已打开导出目录")
        return {'FINISHED'}


class NODE_OT_PBRCheckUpdate(bpy.types.Operator):
    """检查 GitHub 上是否有新版本发布"""
    bl_idname = "node.pbr_check_update"
    bl_label = "检查更新"
    bl_description = "立即检查 GitHub 是否有新版本发布"

    def execute(self, context):
        try:
            from .update_checker import force_check_for_updates
            from . import bl_info as addon_bl_info

            force_check_for_updates(
                "Neocvsu-commits",
                "Blender-TextureLink",
                addon_bl_info["version"],
                os.path.dirname(__file__),
            )
            self.report({"INFO"}, "已发起更新检查，请稍后查看面板顶部")
        except ImportError:
            self.report({"ERROR"}, "更新模块不可用")
        except Exception as e:
            self.report({"ERROR"}, f"检查失败: {e}")
        return {"FINISHED"}


class NODE_OT_PBRInstallUpdate(bpy.types.Operator):
    """从 GitHub 下载最新版本并自动覆盖安装，需重启 Blender 生效"""
    bl_idname = "node.pbr_install_update"
    bl_label = "下载并安装更新"
    bl_description = "从 GitHub 下载最新版本并自动覆盖安装，需重启 Blender 生效"

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        try:
            from .update_checker import install_update
        except ImportError:
            self.report({"ERROR"}, "更新模块不可用，请手动更新")
            return {"CANCELLED"}

        plugin_dir = os.path.dirname(__file__)
        success, msg = install_update("Neocvsu-commits", "Blender-TextureLink", plugin_dir=plugin_dir)
        if success:
            self.report({"INFO"}, msg)
        else:
            self.report({"ERROR"}, msg)
        return {"FINISHED"}
