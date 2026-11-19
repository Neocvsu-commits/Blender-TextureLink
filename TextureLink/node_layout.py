"""Arrange texture branches using their existing links and socket order."""

from collections import defaultdict, deque

import bpy

from .utils import find_channel_match, get_search_name, get_target_materials


_CHANNELS = ('BASE_COLOR', 'AO', 'ORM', 'RMA', 'METALLIC', 'ROUGHNESS', 'ALPHA', 'NORMAL')


def _branch_nodes(images, neighbors):
    visited = set(images)
    pending = list(images)
    while pending:
        for node in neighbors[pending.pop()]:
            if node in visited or node.type == 'GROUP_INPUT' or not node.outputs:
                continue
            if any(socket.type == 'SHADER' for socket in node.outputs):
                continue
            visited.add(node)
            pending.append(node)
    return visited


def _fallback_order(node):
    channel = None
    if node.type == 'TEX_IMAGE' and node.image:
        match = find_channel_match(get_search_name(node.image.name))
        channel = match.channel if match else None
    return (1, '', _CHANNELS.index(channel) if channel in _CHANNELS else len(_CHANNELS))


def _node_size(node, scale):
    w, h = node.dimensions
    if w and h:
        return w / scale, h / scale
    if node.type == 'REROUTE':
        return 16, 16
    if node.hide:
        return node.width, 36
    # Blender only measures nodes after their editor has drawn them.
    if node.type == 'TEX_IMAGE':
        return node.width, 320
    sockets = sum(socket.enabled and not socket.hide for socket in (*node.inputs, *node.outputs))
    return node.width, max(160, node.height, 100 + sockets * 24)


def _place_columns(nodes, levels, order, inputs, outputs, sizes, stationary):
    columns = defaultdict(list)
    for node in nodes:
        columns[levels[node]].append(node)
    anchors = {target for node in nodes for target in outputs[node] if target in stationary}
    if not anchors:
        anchors = {node for node in stationary if node.type == 'BSDF_PRINCIPLED'}
    if anchors:
        right = min(node.location.x for node in anchors) - 125
        top = max(node.location.y for node in anchors)
    else:
        right = max(node.location.x + sizes[node][0] for node in nodes)
        top = max(node.location.y for node in nodes)

    blockers = []
    for node in stationary:
        x, y = node.location
        w, h = sizes[node]
        blockers.append((x, y - h, x + w, y))
    blockers.sort(key=lambda box: -box[3])
    column_x = {}
    for level in sorted(columns):
        column_x[level] = right - max(sizes[node][0] for node in columns[level])
        right = column_x[level] - 125

    positions = {}
    for level in sorted(columns, reverse=True):
        x = column_x[level]
        next_y = top
        for node in sorted(columns[level], key=lambda n: (order[n], n.name)):
            w, h = sizes[node]
            parents_y = [positions[source][1] for source in inputs[node] if source in positions]
            y = min(next_y, sum(parents_y) / len(parents_y)) if parents_y else next_y
            padding = 120 if node.type == 'TEX_IMAGE' and not node.hide else 44
            for left, bottom, right, upper in blockers:
                if x < right + 20 and x + w > left - 20 and y + padding > bottom and y - h < upper:
                    y = bottom - padding
            positions[node] = (x, y)
            # Leave room above the next node for the thumbnail.
            next_y = y - h - 120
    return positions


def arrange_texture_links(tree, scale):
    images = [node for node in tree.nodes if node.type == 'TEX_IMAGE']
    if not images:
        return 0
    inputs, outputs = defaultdict(set), defaultdict(set)
    socket_order = {}
    socket_indices = {socket: i for node in tree.nodes for i, socket in enumerate(node.inputs)}
    for link in tree.links:
        if not link.is_valid:
            continue
        source, target = link.from_node, link.to_node
        inputs[target].add(source)
        outputs[source].add(target)
        pair = source, target
        index = socket_indices[link.to_socket]
        socket_order[pair] = min(index, socket_order.get(pair, index))

    movable = _branch_nodes(images, outputs) | _branch_nodes(images, inputs)
    # Visit consumers first. A shared input receives one column far enough
    # left for every consumer. Cyclic branches without an order stay in place.
    pending = {node: len(outputs[node] & movable) for node in movable}
    ready = deque(node for node in movable if pending[node] == 0)
    sequence = []
    levels, order = {}, {}
    while ready:
        node = ready.popleft()
        sequence.append(node)
        consumers = outputs[node]
        levels[node] = 1 + max((levels[n] for n in consumers if n in movable), default=-1)
        ranks = [
            order[n] + (socket_order[node, n],) if n in movable else (0, n.name, socket_order[node, n])
            for n in consumers
        ]
        # The node's file name only decides order when the branch has no destination.
        order[node] = min(ranks + [_fallback_order(node)])
        for source in inputs[node] & movable:
            pending[source] -= 1
            if pending[source] == 0:
                ready.append(source)
    if not sequence:
        return 0

    by_frame = defaultdict(set)
    for node in sequence:
        by_frame[node.parent].add(node)
    siblings = defaultdict(set)
    for node in tree.nodes:
        siblings[node.parent].add(node)
    sizes = {node: _node_size(node, scale) for node in tree.nodes}
    positions = {}
    for frame, nodes in by_frame.items():
        image_level = max((levels[n] for n in nodes if n.type == 'TEX_IMAGE'), default=0)
        # Align ordinary texture maps in one column and keep any texture that
        # drives another texture's coordinates upstream.
        for node in sequence:
            if node not in nodes:
                continue
            if node.type == 'TEX_IMAGE':
                levels[node] = max(levels[node], image_level)
            for source in inputs[node] & nodes:
                levels[source] = max(levels[source], levels[node] + 1)
        positions.update(_place_columns(nodes, levels, order, inputs, outputs, sizes, siblings[frame] - nodes))
    for node, location in positions.items():
        node.location = location
    return len(positions)


class NODE_OT_PBRArrangeTextureLinks(bpy.types.Operator):
    bl_idname = "node.pbr_arrange_texture_links"
    bl_label = "整理贴图连线"
    bl_description = "按现有连线排列贴图及中间节点，保留所有连接和材质设置"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scope = context.scene.pbr_v2_props.scope_arrange
        space = context.space_data
        if scope == 'CURRENT' and space and space.type == 'NODE_EDITOR':
            trees = [space.edit_tree] if space.tree_type == 'ShaderNodeTree' else []
        else:
            material_scope = 'ALL' if scope == 'SELECTED' else 'CURRENT'
            trees = [mat.node_tree for mat in get_target_materials(context, material_scope)
                     if mat.use_nodes and getattr(mat, 'is_editable', not mat.library)]
        count = 0
        for tree in dict.fromkeys(trees):
            if tree and getattr(tree, 'is_editable', not tree.library):
                count += arrange_texture_links(tree, context.preferences.system.ui_scale or 1)
        if not count:
            self.report({'INFO'}, "当前范围内没有可整理的贴图节点")
            return {'CANCELLED'}
        self.report({'INFO'}, f"已排列 {count} 个节点")
        return {'FINISHED'}
