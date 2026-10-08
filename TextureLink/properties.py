import bpy
from bpy.props import StringProperty, EnumProperty, BoolProperty, FloatProperty, IntProperty


def ue_model_property():
    """UE 模式开关：决定材质命名使用 MI_ 还是 M_ 前缀。"""
    return BoolProperty(
        name="UE模式",
        default=True,
        description="开启（高亮）：UE 模型材质使用 MI_ 前缀；关闭：非 UE 模型材质使用 M_ 前缀",
    )


def get_addon_preferences(context=None):
    """取回本插件偏好设置；插件未启用或上下文不可用时返回 None。"""
    ctx = context or bpy.context
    try:
        addon = ctx.preferences.addons.get(__package__)
    except Exception:
        return None
    return addon.preferences if addon else None


def is_ue_mode(context=None):
    """当前是否为 UE 模式；读取失败时按默认开启处理。"""
    prefs = get_addon_preferences(context)
    return bool(getattr(prefs, "is_ue_model", True))


def get_material_name_prefix(context=None):
    """材质命名前缀：UE 模式 MI_，非 UE 模式 M_。"""
    return "MI_" if is_ue_mode(context) else "M_"


class PBRV2ToolProperties(bpy.types.PropertyGroup):
    scope_connect: EnumProperty(
        name="连接排版范围",
        items=[('CURRENT', "当前", "仅对当前"), ('ALL', "全部", "对所有")],
        default='CURRENT'
    )
    scope_clean: EnumProperty(
        name="清理节点范围",
        items=[('CURRENT', "当前", "仅对当前"), ('ALL', "全部", "对所有")],
        default='ALL'
    )
    scope_rename: EnumProperty(
        name="重命名范围",
        items=[('CURRENT', "当前", "仅对当前"), ('ALL', "全部", "对所有")],
        default='ALL'
    )
    scope_reset: EnumProperty(
        name="重置节点范围",
        items=[('CURRENT', "当前", "仅对当前"), ('ALL', "全部", "对所有")],
        default='ALL'
    )
    scope_compress: EnumProperty(
        name="压缩范围",
        items=[('CURRENT', "当前", "仅对当前"), ('ALL', "全部", "对所有")],
        default='CURRENT'
    )
    scope_export: EnumProperty(
        name="导出范围",
        items=[('SELECTED', "所选", "导出所选模型下的所有贴图"), ('SCENE', "全场景", "导出整个场景所有材质的贴图")],
        default='SELECTED'
    )
    
    # 图像压缩相关
    show_compress_settings: BoolProperty(
        name="图像压缩",
        default=False
    )
    do_resize: BoolProperty(
        name="启用尺寸压缩",
        default=True,
    )
    compression_ratio: FloatProperty(
        name="压缩比例",
        default=0.5,
        min=0.01,
        max=1.0
    )
    min_resolution: IntProperty(
        name="最小分辨率",
        description="缩放后宽、高均不会低于该值；贴图已接近该尺寸时无法按压缩比例继续缩小",
        default=256,
        min=1,
    )
    target_format: EnumProperty(
        name="目标格式",
        description="选择要转换的图像格式",
        items=[
            ('CURRENT', "当前", "保持当前格式不变"),
            ('JPEG', "JPEG", "生成 JPEG 格式压缩图像，不支持 Alpha"),
            ('PNG', "PNG", "生成无损 PNG"),
            ('TARGA', "TGA", "TARGA 格式"),
            ('TIFF', "TIFF", "TIFF 图像"),
            ('BMP', "BMP", "Bitmap BMP"),
            ('OPEN_EXR', "EXR", "OpenEXR 浮点数格式")
        ],
        default='CURRENT'
    )
    compress_disk_files: BoolProperty(
        name="同步压缩磁盘贴图文件",
        description="压缩后直接覆盖磁盘源贴图（或按目标格式更新路径），关闭时仅更新当前 Blender 内存/打包数据",
        default=False
    )

    # 记录上一次导出贴图目录（用于一键打开输出目录）
    last_export_path: StringProperty(
        name="上次导出路径",
        default=""
    )

    # 是否在重命名贴图数据块时，同时重命名磁盘上的贴图文件并更新路径
    rename_disk_files: BoolProperty(
        name="同步重命名磁盘贴图文件",
        description="根据材质重命名贴图时，同时重命名磁盘文件并更新 Image 的路径，便于后续模型导出保持贴图链接",
        default=False
    )
