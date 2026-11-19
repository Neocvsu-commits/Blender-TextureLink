import re
from uuid import uuid4

import bpy

from .utils import _image_extension, _indexed_slot_suffix, _strip_common_prefixes, material_image_names


def _core(name):
    return re.sub(r'[^\w]+', '_', _strip_common_prefixes(name)).strip('_')


def _slot_suffix(materials, index):
    return '_' + _indexed_slot_suffix(index) if len(materials) > 1 else ''


def _source_core(obj, materials, images, source, editor_node):
    if source == 'OBJECT':
        return _core(obj.name)
    if not materials:
        return ''
    mat = obj.active_material or materials[0]
    if source == 'MATERIAL':
        core = _core(mat.name)
    else:
        # Prefer an explicitly selected texture in the active material, including
        # its open group. With no texture selected, choose the base-color branch.
        for mat in dict.fromkeys([mat, *materials]):
            names = images[mat]
            if not names:
                continue
            active = mat.node_tree.nodes.active
            for node in (editor_node, active):
                if node and node.type == 'TEX_IMAGE' and node.select and node.image in names:
                    if names[node.image][0]:
                        return _asset_core(names[node.image][0], obj, materials, mat)
            choices = [(stem, suffix) for stem, suffix in names.values() if stem]
            if choices:
                core = next((stem for stem, suffix in choices if suffix == '_D'), choices[0][0])
                break
        else:
            return ''
    return _asset_core(core, obj, materials, mat)


def _asset_core(name, obj, materials, mat):
    core = _core(name)
    suffix = _slot_suffix(materials, materials.index(mat))
    if suffix and core.endswith(suffix):
        base = core[:-len(suffix)]
        if base == _core(obj.name) or all(
            m.name == 'M_' + base + _slot_suffix(materials, i) for i, m in enumerate(materials)
        ):
            return base
    return core


def _fit(stem, budget):
    # Blender 4.x ID names have a 63-byte limit. Reserve the channel and extension.
    return stem.encode('utf-8')[:budget].decode('utf-8', errors='ignore')


def _editable(block):
    return block is not None and not block.is_property_readonly('name') and getattr(block, 'is_editable', not block.library)


def _make_plan(objects, source, editor_node):
    groups = []
    images = {}
    targets = {'objects': set(), 'meshes': set(), 'materials': set(), 'images': set()}
    skipped = 0
    for obj in objects:
        mats = list(dict.fromkeys(slot.material for slot in obj.material_slots if slot.material))
        if not _editable(obj) or (obj.type != 'MESH' and not mats):
            skipped += 1
            continue
        for mat in mats:
            if mat not in images:
                images[mat] = material_image_names(mat)
        core = _source_core(obj, mats, images, source, editor_node if obj == objects[0] else None)
        if not core:
            skipped += 1
            continue
        mesh = obj.data if obj.type == 'MESH' and _editable(obj.data) else None
        groups.append((obj, mesh, mats, core))
        targets['objects'].add(obj)
        if mesh:
            targets['meshes'].add(mesh)
        for mat in mats:
            if _editable(mat):
                targets['materials'].add(mat)
                targets['images'].update(img for img in images[mat] if _editable(img))

    used = {kind: set() for kind in targets}
    plan = {}

    def available(kind, name):
        owner = getattr(bpy.data, kind).get(name)
        return name not in used[kind] and (owner is None or owner in targets[kind])

    def assign(block, kind, name):
        plan[block] = name
        used[kind].add(name)

    for obj, mesh, mats, core in groups:
        prefix = 'SM_' if obj.type == 'MESH' else ''
        slots = [(mat, _slot_suffix(mats, i)) for i, mat in enumerate(mats)
                 if mat in targets['materials'] and mat not in plan]
        # Pick one common base across object, mesh and material namespaces.
        # Existing names outside the operation retain ownership of their names.
        budget = 63 - max([len(prefix)] + [2 + len(slot) for _, slot in slots])
        for mat, slot in slots:
            for img, (_, channel) in images[mat].items():
                # Keep the same stem even on long names that need a channel,
                # extension and collision number in Blender's image namespace.
                tail = slot + channel + f'_{len(bpy.data.images) + 1}' + _image_extension(img)
                budget = min(budget, 61 - len(tail.encode('utf-8')))
        index = 0
        while True:
            number = f'_{index}' if index else ''
            stem = _fit(core, budget - len(number)) + number
            obj_name = prefix + stem
            if (available('objects', obj_name)
                    and (not mesh or mesh in plan or available('meshes', obj_name))
                    and all(available('materials', 'M_' + stem + slot) for _, slot in slots)):
                break
            index += 1
        assign(obj, 'objects', obj_name)
        if mesh and mesh not in plan:
            assign(mesh, 'meshes', obj_name)
        for mat, slot in slots:
            mat_stem = stem + slot
            assign(mat, 'materials', 'M_' + mat_stem)
            for img, (_, channel) in images[mat].items():
                if img not in targets['images'] or img in plan:
                    continue
                ext = _image_extension(img)
                copy = 1
                while True:
                    tail = channel + (f'_{copy}' if copy > 1 else '') + ext
                    name = 'T_' + _fit(mat_stem, 61 - len(tail.encode('utf-8'))) + tail
                    if available('images', name):
                        break
                    copy += 1
                assign(img, 'images', name)
    return plan, skipped


def _apply_names(plan):
    changes = [(block, block.name, name) for block, name in plan.items() if block.name != name]
    # Vacate destinations first so swaps never displace an unrelated datablock.
    token = uuid4().hex
    staged = []
    try:
        for i, (block, original, desired) in enumerate(changes):
            temporary = f'_texturelink_{token}_{i}'
            block.name = temporary
            staged.append((block, original, desired, temporary))
        for block, _, desired, _ in staged:
            block.name = desired
            if block.name != desired:
                raise RuntimeError(f"无法设置名称 {desired}")
    except (RuntimeError, TypeError, ValueError, AttributeError):
        for block, _, _, temporary in staged:
            block.name = temporary
        for block, original, _, _ in staged:
            block.name = original
        raise
    return len(changes)


class NODE_OT_PBRUnifyNames(bpy.types.Operator):
    bl_idname = "node.pbr_unify_names"
    bl_label = "统一资产命名"
    bl_description = "按选定的名称统一物体、网格、材质和贴图数据块，可用 Ctrl+Z 撤销"
    bl_options = {'REGISTER', 'UNDO'}

    source: bpy.props.EnumProperty(name="名称来源", items=[
        ('OBJECT', "物体", "取物体名称"),
        ('MATERIAL', "材质球", "取活动材质槽中材质球的名称"),
        ('TEXTURE', "贴图", "优先取活动材质选中的贴图，其次取底色贴图"),
    ])

    def execute(self, context):
        active = context.active_object
        if context.scene.pbr_v2_props.scope_rename == 'CURRENT':
            objects = [active] if active else []
        else:
            objects = list(context.selected_objects)
            if active in objects:
                objects.remove(active)
                objects.insert(0, active)
        if not objects:
            self.report({'WARNING'}, "请先选择物体")
            return {'CANCELLED'}
        plan, skipped = _make_plan(objects, self.source, getattr(context, 'active_node', None))
        if not plan:
            self.report({'WARNING'}, "当前范围内没有可用的命名来源")
            return {'CANCELLED'}
        try:
            count = _apply_names(plan)
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        msg = f"已更新 {count} 个名称" if count else "名称已统一"
        if skipped:
            msg += f"，跳过 {skipped} 个物体"
        self.report({'INFO'}, msg)
        return {'FINISHED'}
