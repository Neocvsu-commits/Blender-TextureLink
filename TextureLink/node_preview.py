"""Draw image thumbnails in the shader editor without editing the node tree."""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader


_handler = None
_shader = None
_quad = None


def _make_quad():
    interface = gpu.types.GPUStageInterfaceInfo("texturelink_preview_uv")
    interface.smooth('VEC2', "uv")
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC2', "corner")
    info.vertex_out(interface)
    info.push_constant('MAT4', "viewProjection")
    info.push_constant('VEC4', "rect")
    info.push_constant('INT', "displayColor")
    info.push_constant('INT', "alphaMode")
    info.sampler(0, 'FLOAT_2D', "imageTexture")
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source("""
        void main() {
            uv = corner;
            gl_Position = viewProjection * vec4(rect.xy + corner * rect.zw, 0.0, 1.0);
        }
    """)
    info.fragment_source("""
        void main() {
            vec4 pixel = texture(imageTexture, uv);
            float alpha = alphaMode == 0 ? 1.0 : clamp(pixel.a, 0.0, 1.0);
            vec3 rgb = pixel.rgb;
            if (alphaMode == 2 && alpha > 0.0) rgb /= alpha;
            if (displayColor != 0) {
                rgb = max(rgb, vec3(0.0));
                rgb = mix(12.92 * rgb, 1.055 * pow(rgb, vec3(1.0 / 2.4)) - 0.055,
                          step(vec3(0.0031308), rgb));
            }
            vec2 cell = floor(gl_FragCoord.xy / 8.0);
            float background = mix(0.25, 0.37, mod(cell.x + cell.y, 2.0));
            fragColor = vec4(mix(vec3(background), rgb, alpha), 1.0);
        }
    """)
    shader = gpu.shader.create_from_info(info)
    quad = batch_for_shader(shader, 'TRI_STRIP', {"corner": ((0, 0), (1, 0), (0, 1), (1, 1))})
    return shader, quad


def _node_origin(node):
    if hasattr(node, "location_absolute"):
        return node.location_absolute
    location = node.location.copy()
    parent = node.parent
    while parent:
        location += parent.location
        parent = parent.parent
    return location


def _overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _draw_image(image, rect):
    global _shader, _quad
    texture = gpu.texture.from_image(image)
    if _shader is None:
        _shader, _quad = _make_quad()
    x, y, right, top = rect
    _shader.bind()
    _shader.uniform_float("viewProjection", gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
    _shader.uniform_float("rect", (x, y, right - x, top - y))
    _shader.uniform_int("displayColor", int(not image.colorspace_settings.is_data))
    _shader.uniform_int("alphaMode", {'STRAIGHT': 1, 'PREMUL': 2}.get(image.alpha_mode, 0))
    _shader.uniform_sampler("imageTexture", texture)
    _quad.draw(_shader)


def _draw_previews():
    context = bpy.context
    space = context.space_data
    if space.tree_type != 'ShaderNodeTree' or not space.edit_tree or not space.overlay.show_overlays:
        return
    addon = context.preferences.addons.get(__package__)
    if not addon or not addon.preferences.show_node_previews:
        return

    region = context.region
    view = region.view2d
    viewport = (*view.region_to_view(0, 0), *view.region_to_view(region.width, region.height))
    scale = context.preferences.system.ui_scale
    size = 96 * scale
    gap = 8 * scale
    if size * region.width < 12 * (viewport[2] - viewport[0]):
        return

    occupied = []
    candidates = []
    for node in space.edit_tree.nodes:
        if node.bl_idname == 'NodeFrame':
            continue
        x, y = _node_origin(node) * scale
        width, height = node.dimensions
        bounds = (x, y - height, x + width, y)
        if _overlaps(bounds, viewport):
            occupied.append(bounds)
        if node.bl_idname == 'ShaderNodeTexImage' and not node.hide and node.image:
            # Cull both possible positions before asking Blender for image pixels.
            envelope = (x - size - gap, y - size, x + width + size, y + size + gap)
            if _overlaps(envelope, viewport):
                candidates.append((node.image, x, y, width))

    for image, x, y, node_width in candidates:
        # A node-specific frame or UDIM tile cannot be selected with from_image().
        if image.source not in {'FILE', 'GENERATED'}:
            continue
        try:
            w, h = image.size
            if not w or not h:
                continue
            factor = size / max(w, h)
            w, h = w * factor, h * factor
            center = x + node_width * 0.5
            above = (center - w / 2, y + gap, center + w / 2, y + gap + h)
            left = (x - gap - w, y - h, x - gap, y)
            for rect in (above, left):
                if _overlaps(rect, viewport) and not any(_overlaps(rect, other) for other in occupied):
                    _draw_image(image, rect)
                    occupied.append(rect)
                    break
        except (ReferenceError, RuntimeError, ValueError):
            # Missing or temporarily unavailable images must not interrupt the editor.
            continue


def register():
    global _handler
    if _handler is None and not bpy.app.background:
        _handler = bpy.types.SpaceNodeEditor.draw_handler_add(_draw_previews, (), 'WINDOW', 'POST_VIEW')


def unregister():
    global _handler, _shader, _quad
    if _handler is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    _quad = _shader = None
