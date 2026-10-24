import bpy
import os
import re
import tempfile
from collections import namedtuple

# --- 全局配置 ---
# 通道字段表：值是文件名里可能出现的「字段」写法。
# 识别时与位置、大小写、分隔符无关：RedRoot_BaseColor_2048、BaseColor_RedRoot、
# T_Rock_bc、RedRoot_Normal_OpenGL_2048 都能认出来。
SUFFIX_MAP = {
    'BASE_COLOR': ['_basecolor', '_base_color', '_albedo', '_diffuse', '_diff', '_color', '_col', '_bc', '_d'],
    'ORM': ['_orm', '_arm', '_ao_r_m'], # R=AO, G=Roughness, B=Metallic
    'RMA': ['_rma'],                    # R=Roughness, G=Metallic
    'NORMAL': ['_normal', '_normalmap', '_nor', '_nrm', '_n'],
    'ROUGHNESS': ['_roughness', '_rough', '_rgh', '_r'],
    'METALLIC': ['_metallic', '_metal', '_met', '_m'],
    'AO': ['_ao', '_ambientocclusion', '_occlusion'],
    'ALPHA': ['_alpha', '_opacity', '_a']
}

# 同一位置命中多个通道时的取舍顺序（越靠前越优先）
CHANNEL_TIE_BREAK = ('ORM', 'RMA', 'NORMAL', 'BASE_COLOR', 'AO', 'ALPHA', 'ROUGHNESS', 'METALLIC')

# 单字母别名：只在「最后一个有效字段」上生效，避免 RedRoot 的 r 被当成 Roughness
_SINGLE_LETTER_ALIASES = frozenset({'_d', '_n', '_r', '_m', '_a'})

# 「自动连接」过程中需要提醒用户的事项（如 DirectX 法线），由操作符取走并显示
CONNECT_NOTES = []

# UE 材质命名规范：材质统一 M_ 前缀，贴图统一 T_ 前缀（旧 MI_ 规范已废弃）
MATERIAL_PREFIX = "M_"

def get_search_name(image_name):
    """返回用于识别的贴图名：保留大小写，便于按大小写边界拆词。"""
    name = image_name
    if len(name) > 4 and name[-4] == '.' and name[-3:].isdigit():
        name = name[:-4]
    return os.path.splitext(name)[0]


def get_clean_name_for_search(image_name):
    return get_search_name(image_name).lower()

def clean_name_for_rename(image_name):
    name = image_name
    if len(name) > 4 and name[-4] == '.' and name[-3:].isdigit():
        name = name[:-4]
    base, ext = os.path.splitext(name)
    if ext.lower() in ['.png', '.jpg', '.jpeg', '.tga', '.exr', '.tif', '.tiff', '.bmp', '.psd']:
        name = base
    return name

# --- 通道字段识别 ---
# 名字先按分隔符与大小写边界拆词（RedRoot → red root，Normal_OpenGL → normal open gl），
# 再把词拼起来找别名，因此字段落在文件名哪一段都能命中。

_WORD_RE = re.compile(r'[A-Za-z0-9]+')
_CAMEL_WORD_RE = re.compile(r'[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')

# 结尾噪音词：分辨率、取向、色彩空间、通用尾词，判断「最后一个有效字段」时跳过
_NOISE_WORDS = frozenset({
    'map', 'tex', 'texture', 'mask',
    'srgb', 'linear', 'noncolor', 'raw',
    'res', 'low', 'high', 'lowres', 'highres', 'hd', 'uhd',
    'final', 'copy', 'new', 'test', 'preview', 'export',
})
_NOISE_PATTERN = re.compile(r'^(?:\d+|[1-9]k|k|v\d+)$')

# 法线朝向标记：OpenGL 是 Blender 默认；DirectX 的 G 通道方向相反
# （open / gl 分开列出，是因为 OpenGL 会被大小写边界拆成两个词）
_DEVICE_WORDS = frozenset({'opengl', 'open', 'gl', 'gles', 'ogl', 'dx', 'd3d', 'direct3d', 'directx', 'nox'})
_DIRECTX_WORDS = frozenset({'dx', 'd3d', 'direct3d', 'directx'})
_OPENGL_WORDS = frozenset({'opengl'})

# 别名允许停在词中间（如 normalmap 里认 normal）的最短长度；短别名必须吃满整词
_PARTIAL_MATCH_MIN_LEN = 6

ChannelMatch = namedtuple('ChannelMatch', 'channel start end alias variant')


def _split_words(name):
    """把文件名拆成 [(小写词, 原字符串里的起始下标)]。"""
    words = []
    for chunk in _WORD_RE.finditer(name):
        text = chunk.group(0)
        for piece in _CAMEL_WORD_RE.finditer(text):
            words.append((piece.group(0).lower(), chunk.start() + piece.start()))
    return words


def _is_noise_word(word):
    return word in _NOISE_WORDS or word in _DEVICE_WORDS or _NOISE_PATTERN.match(word) is not None


def _last_meaningful_word_index(words):
    """从尾部跳过分辨率、取向、通用尾词，返回最后一个有效词的下标。"""
    idx = len(words) - 1
    while idx >= 0:
        word = words[idx][0]
        if _is_noise_word(word):
            idx -= 1
            continue
        # DirectX / OpenGL 这类词可能被大小写拆成两段（open + gl）
        if idx > 0 and (words[idx - 1][0] + word) in _DEVICE_WORDS:
            idx -= 2
            continue
        break
    return idx


def _detect_normal_variant(words):
    """判断法线贴图的朝向标记，返回 'directx' / 'opengl' / ''。"""
    word_list = [w for w, _ in words]
    pairs = {word_list[i] + word_list[i + 1] for i in range(len(word_list) - 1)}
    hits = set(word_list) | pairs
    if hits & _DIRECTX_WORDS:
        return 'directx'
    if hits & _OPENGL_WORDS:
        return 'opengl'
    return ''


def _strip_trailing_noise(text):
    """去掉名称结尾的分辨率、版本、取向等字段，返回主体。"""
    text = text.strip(" _-.")
    while text:
        words = _split_words(text)
        if not words:
            break
        last_word, last_start = words[-1]
        if not _is_noise_word(last_word):
            break
        text = text[:last_start].strip(" _-.")
    return text


def _match_rank(match):
    try:
        tie = CHANNEL_TIE_BREAK.index(match.channel)
    except ValueError:
        tie = len(CHANNEL_TIE_BREAK)
    return (match.end, len(_NON_ALNUM_RE.sub('', match.alias.lower())), -tie)


def find_channel_match(name):
    """
    在贴图文件名里找通道字段，返回 ChannelMatch(channel, start, end, alias, variant)，找不到返回 None。

    规则：
    - 字段可以在任意位置：`RedRoot_BaseColor_2048`、`BaseColor_RedRoot`、`T_Rock_bc` 都认；
    - 只从词首算命中，chaos 里的 ao、method 里的 met 不会被误判；
    - 越靠后的字段越可信（材质名带 Metal 也压不过后面的 _BaseColor）；
    - 同一位置长别名优先（_ao_r_m 优于单独的 _r、_m）；
    - 单字母别名（_d/_n/_r/_m/_a）只认最后一个有效字段，RedRoot 的 r 不会被当成 Roughness。
    """
    if not name:
        return None

    words = _split_words(name)
    if not words:
        return None

    tokens = [w for w, _ in words]
    squashed = ''.join(tokens)

    # 拼接串里每个字符对应的原字符串下标，命中后可直接切出字段位置
    char_origin = []
    for token, origin in words:
        for offset in range(len(token)):
            char_origin.append(origin + offset)

    word_starts = set()
    word_ends = set()
    cursor = 0
    for token in tokens:
        word_starts.add(cursor)
        cursor += len(token)
        word_ends.add(cursor)

    tail_idx = _last_meaningful_word_index(words)
    tail_end = None if tail_idx < 0 else sum(len(t) for t in tokens[:tail_idx + 1])

    best = None
    for channel, aliases in SUFFIX_MAP.items():
        for alias in aliases:
            needle = _NON_ALNUM_RE.sub('', alias.lower())
            if not needle:
                continue
            search_from = 0
            while True:
                pos = squashed.find(needle, search_from)
                if pos < 0:
                    break
                search_from = pos + 1
                if pos not in word_starts:
                    continue
                match_end = pos + len(needle)
                if match_end not in word_ends and len(needle) < _PARTIAL_MATCH_MIN_LEN:
                    continue
                if alias in _SINGLE_LETTER_ALIASES and match_end != tail_end:
                    continue
                candidate = ChannelMatch(
                    channel,
                    char_origin[pos],
                    char_origin[match_end - 1] + 1,
                    alias,
                    '',
                )
                if best is None or _match_rank(candidate) > _match_rank(best):
                    best = candidate

    if best is not None and best.channel == 'NORMAL':
        best = best._replace(variant=_detect_normal_variant(words))
    return best


def check_suffix(clean_name_lower, suffixes):
    """兼容旧接口：命中的别名属于给定列表时返回该别名。"""
    match = find_channel_match(clean_name_lower)
    if match and match.alias in suffixes:
        return match.alias
    return None


def match_orm_filename_suffix(name_lower):
    """ORM/ARM 复合图（含 glTF 常见的 _orm_0）命中时返回别名。"""
    match = find_channel_match(name_lower)
    if match and match.channel == 'ORM':
        return match.alias
    return None


def is_pbr_texture_name_lower(name_lower):
    """是否属于本工具识别的 PBR 贴图命名。"""
    return find_channel_match(name_lower) is not None


def note_connect_warning(message):
    """记录一条自动连接的提醒，等操作符取走显示。"""
    if message not in CONNECT_NOTES and len(CONNECT_NOTES) < 20:
        CONNECT_NOTES.append(message)


def consume_connect_notes():
    """取走并清空自动连接提醒。"""
    notes = list(CONNECT_NOTES)
    CONNECT_NOTES.clear()
    return notes


def _strip_common_prefixes(name: str) -> str:
    """循环去掉常见的 SM_/M_/T_ 前缀（含旧规范 MI_），返回核心名。支持 SM_T_RockWall_D 这种多层前缀。"""
    KNOWN_PREFIXES = ("sm_", "mi_", "m_", "t_")
    n = name.strip()
    while True:
        lower = n.lower()
        stripped = None
        for pf in KNOWN_PREFIXES:
            if lower.startswith(pf):
                stripped = n[len(pf):]
                break
        if stripped is None and len(n) >= 2 and n[0].lower() in ("m", "t") and n[1] == "_":
            stripped = n[2:]
        if stripped is None:
            break
        n = stripped
    return n


_MIN_CORE_LEN = 3  # 核心名最短长度，防止 "a" 之类的碰瓷大量文件
_MIN_LENGTH_RATIO = 0.5  # 子串匹配时，较短串长度需 ≥ 较长串 × 此比例


def _file_stem_core_candidates(file_stem):
    """
    贴图文件名的核心名候选：原始主干，以及去掉通道字段与分辨率等尾巴后的主干。
    后者让 `M_RedRoot` 也能匹配 `RedRoot_BaseColor_2048.png`、`BaseColor_RedRoot_2048.png`。
    """
    candidates = [_strip_common_prefixes(file_stem)]
    match = find_channel_match(file_stem)
    if match:
        for part in (_strip_trailing_noise(file_stem[:match.start]),
                     _strip_trailing_noise(file_stem[match.end:])):
            if part:
                candidates.append(_strip_common_prefixes(part))
    return [c for c in candidates if c]


def _score_core_match(mat_lower, file_lower):
    """核心名比对打分：精确 100，子串 40-79，过短或无关 0。"""
    # 过短核心名 → 拒绝（如 "a" 会碰瓷所有含 a 的文件）
    if len(mat_lower) < _MIN_CORE_LEN or len(file_lower) < _MIN_CORE_LEN:
        return 0

    if mat_lower == file_lower:
        return 100

    # 模糊子串匹配：较短的 core 被较长的 core 包含
    shorter = mat_lower if len(mat_lower) < len(file_lower) else file_lower
    longer = file_lower if len(mat_lower) < len(file_lower) else mat_lower

    if shorter in longer:
        ratio = len(shorter) / len(longer)
        if ratio >= _MIN_LENGTH_RATIO:
            return int(ratio * 80)  # 映射到 40-79 分，和精确匹配拉开差距

    return 0


def fuzzy_match_core_name(mat_name: str, file_stem: str) -> int:
    """
    材质名与贴图文件名主干做模糊匹配，忽略 T_/M_/SM_ 前缀差异（旧规范 MI_ 同样忽略）。
    策略：精确优先 → 模糊子串回退；文件名里的通道字段与分辨率不参与比对。
    返回匹配质量分 (0 = 不匹配，100 = 精确匹配)，分数越高越可靠。
    """
    if not mat_name or not file_stem:
        return 0

    mat_core = _strip_common_prefixes(mat_name)
    # 空核心名 → 拒绝（如 "M_" 去掉前缀后为空）
    if not mat_core:
        return 0

    mat_lower = mat_core.lower()
    best = 0
    for file_core in _file_stem_core_candidates(file_stem):
        best = max(best, _score_core_match(mat_lower, file_core.lower()))
        if best == 100:
            break
    return best


def _force_claim_name(datablocks, current, target_name):
    """
    强制夺取命名权：循环驱逐同名占位者到 _N 增序号，再设当前块为目标名。
    返回 True 当且仅当 current.name 最终等于 target_name。
    """
    if not current or not target_name:
        return False

    if current.name == target_name:
        return True

    # 循环驱逐同名占位者到增序备用名（_1, _2, ...），避免 _old 只能挡一次
    counter = 1
    while counter <= 100:
        existing = datablocks.get(target_name)
        if not existing or existing == current:
            break
        backup = f"{target_name}_{counter}"
        while backup in datablocks and counter <= 100:
            counter += 1
            backup = f"{target_name}_{counter}"
        if counter > 100:
            break
        try:
            existing.name = backup
        except Exception:
            pass
        counter += 1

    try:
        current.name = target_name
    except Exception:
        return False
    return current.name == target_name


def is_multiply_mix_node(node):
    """判断节点是否为正片叠底混合节点（兼容旧版 MixRGB 与新版 Mix）。"""
    if node is None or node.type not in ('MIX_RGB', 'MIX'):
        return False
    if getattr(node, 'blend_type', '') != 'MULTIPLY':
        return False
    if node.type == 'MIX':
        return getattr(node, 'data_type', 'RGBA') in ('RGBA', 'VECTOR')
    return True


def create_multiply_mix_node(nodes):
    """
    创建正片叠底颜色混合节点。
    Blender 4.x 使用 ShaderNodeMix（颜色混合），GLB 导出可正确保留 AO 叠乘；
    旧版 ShaderNodeMixRGB 在 glTF 导出时可能不生效。
    """
    try:
        mix_node = nodes.new(type='ShaderNodeMix')
        mix_node.data_type = 'RGBA'
        mix_node.blend_type = 'MULTIPLY'
        fac = mix_node.inputs.get('Factor') or mix_node.inputs.get('Fac')
        if fac:
            fac.default_value = 1.0
        return mix_node
    except Exception:
        mix_node = nodes.new(type='ShaderNodeMixRGB')
        mix_node.blend_type = 'MULTIPLY'
        mix_node.inputs['Fac'].default_value = 1.0
        return mix_node


def get_mix_input_a(mix_node):
    inp = mix_node.inputs.get('A') or mix_node.inputs.get('Color1')
    if not inp and len(mix_node.inputs) > 1:
        inp = mix_node.inputs[1]
    return inp


def get_mix_input_b(mix_node):
    inp = mix_node.inputs.get('B') or mix_node.inputs.get('Color2')
    if not inp and len(mix_node.inputs) > 2:
        inp = mix_node.inputs[2]
    return inp


def get_mix_output(mix_node):
    return mix_node.outputs.get('Result') or mix_node.outputs.get('Color')


def upgrade_legacy_multiply_mix(nodes, links, legacy_node, base_color_input):
    """将旧版 MixRGB 正片叠底节点升级为 ShaderNodeMix，以兼容 GLB 导出。"""
    if legacy_node.type != 'MIX_RGB':
        return legacy_node

    in1 = get_mix_input_a(legacy_node)
    in2 = get_mix_input_b(legacy_node)
    fac = legacy_node.inputs.get('Fac') or legacy_node.inputs.get('Factor')
    fac_val = fac.default_value if fac else 1.0

    in1_socket = in1.links[0].from_socket if in1 and in1.is_linked else None
    in2_socket = in2.links[0].from_socket if in2 and in2.is_linked else None

    new_node = create_multiply_mix_node(nodes)
    new_fac = new_node.inputs.get('Factor') or new_node.inputs.get('Fac')
    if new_fac:
        new_fac.default_value = fac_val

    for link in list(base_color_input.links):
        links.remove(link)

    new_out = get_mix_output(new_node)
    if new_out:
        links.new(new_out, base_color_input)

    new_in1 = get_mix_input_a(new_node)
    new_in2 = get_mix_input_b(new_node)
    if in1_socket and new_in1:
        links.new(in1_socket, new_in1)
    if in2_socket and new_in2:
        links.new(in2_socket, new_in2)

    nodes.remove(legacy_node)
    return new_node


def ensure_multiply_mix_for_base_color(nodes, links, base_color_input):
    """获取、升级或创建用于 Base Color 的正片叠底混合节点。"""
    mix_node = None
    if base_color_input.is_linked:
        conn_node = base_color_input.links[0].from_node
        if is_multiply_mix_node(conn_node):
            mix_node = conn_node

    if mix_node and mix_node.type == 'MIX_RGB':
        mix_node = upgrade_legacy_multiply_mix(nodes, links, mix_node, base_color_input)

    if not mix_node:
        mix_node = create_multiply_mix_node(nodes)
        if base_color_input.is_linked:
            existing_link = base_color_input.links[0]
            in1 = get_mix_input_a(mix_node)
            if in1:
                links.new(existing_link.from_socket, in1)
        out = get_mix_output(mix_node)
        if out:
            links.new(out, base_color_input)

    return mix_node


def connect_pbr_nodes(nodes, links, principled_node, image_nodes):
    """将图像节点连接到 Principled BSDF 对应通道，并自动修正已有节点和色彩空间"""

    for img_node in image_nodes:
        if not img_node.image:
            continue
        
        channel = find_channel_match(get_search_name(img_node.image.name))
        if channel is None:
            continue

        if channel.channel == 'BASE_COLOR':
            try: img_node.image.colorspace_settings.name = 'sRGB'
            except: pass
            
            target_socket = principled_node.inputs.get('Base Color')
            mix_node = None
            if target_socket and target_socket.is_linked:
                conn_node = target_socket.links[0].from_node
                if is_multiply_mix_node(conn_node):
                    mix_node = conn_node
                    if mix_node.type == 'MIX_RGB':
                        mix_node = upgrade_legacy_multiply_mix(nodes, links, mix_node, target_socket)
            
            if mix_node:
                in1 = get_mix_input_a(mix_node)
                if in1:
                    links.new(img_node.outputs['Color'], in1)
            elif target_socket:
                links.new(img_node.outputs['Color'], target_socket)

            # 团队规范：_D（基础色）不再自动连接 Alpha，避免影响 GLB 导出。
            # 如需透明效果，由美术手动判定并连接。

        elif channel.channel == 'ORM':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            sep_node = None
            rough_socket = principled_node.inputs.get('Roughness')
            if rough_socket and rough_socket.is_linked:
                for link in rough_socket.links:
                    if link.from_node.type == 'SEPARATE_COLOR':
                        sep_node = link.from_node
                        break
            
            if not sep_node:
                sep_node = nodes.new(type='ShaderNodeSeparateColor')
            
            links.new(img_node.outputs['Color'], sep_node.inputs['Color'])
            if 'Roughness' in principled_node.inputs:
                links.new(sep_node.outputs[1], principled_node.inputs['Roughness'])
            if 'Metallic' in principled_node.inputs:
                links.new(sep_node.outputs[2], principled_node.inputs['Metallic'])
            
            base_color_input = principled_node.inputs.get('Base Color')
            if base_color_input:
                mix_node = ensure_multiply_mix_for_base_color(nodes, links, base_color_input)
                in2 = get_mix_input_b(mix_node)
                if in2:
                    links.new(sep_node.outputs[0], in2)

        elif channel.channel == 'RMA':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            sep_node = None
            rough_socket = principled_node.inputs.get('Roughness')
            if rough_socket and rough_socket.is_linked:
                for link in rough_socket.links:
                    if link.from_node.type == 'SEPARATE_COLOR':
                        sep_node = link.from_node
                        break
            if not sep_node:
                sep_node = nodes.new(type='ShaderNodeSeparateColor')
            
            links.new(img_node.outputs['Color'], sep_node.inputs['Color'])
            if 'Roughness' in principled_node.inputs:
                links.new(sep_node.outputs[0], principled_node.inputs['Roughness'])
            if 'Metallic' in principled_node.inputs:
                links.new(sep_node.outputs[1], principled_node.inputs['Metallic'])

        elif channel.channel == 'NORMAL':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass

            # DirectX 朝向的法线在 Blender 里 G 通道方向相反，不静默改线，先提醒
            if channel.variant == 'directx':
                note_connect_warning(f"{img_node.image.name}: DirectX 法线，G 通道需翻转")
            
            nrm_node = None
            normal_socket = principled_node.inputs.get('Normal')
            if normal_socket and normal_socket.is_linked:
                for link in normal_socket.links:
                    if link.from_node.type == 'NORMAL_MAP':
                        nrm_node = link.from_node
                        break
            
            if not nrm_node:
                nrm_node = nodes.new(type='ShaderNodeNormalMap')
            
            links.new(img_node.outputs['Color'], nrm_node.inputs['Color'])
            if normal_socket:
                links.new(nrm_node.outputs['Normal'], normal_socket)

        elif channel.channel == 'ROUGHNESS':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            target_socket = principled_node.inputs.get('Roughness')
            if target_socket:
                links.new(img_node.outputs['Color'], target_socket)

        elif channel.channel == 'METALLIC':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            target_socket = principled_node.inputs.get('Metallic')
            if target_socket:
                links.new(img_node.outputs['Color'], target_socket)

        elif channel.channel == 'AO':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            base_color_input = principled_node.inputs.get('Base Color')
            if base_color_input:
                mix_node = ensure_multiply_mix_for_base_color(nodes, links, base_color_input)
                in2 = get_mix_input_b(mix_node)
                if in2:
                    links.new(img_node.outputs['Color'], in2)

        elif channel.channel == 'ALPHA':
            try: img_node.image.colorspace_settings.name = 'Non-Color'
            except: pass
            target_socket = principled_node.inputs.get('Alpha')
            if target_socket:
                links.new(img_node.outputs['Color'], target_socket)


def get_target_materials(context, scope):
    materials = []
    if scope == 'CURRENT':
        obj = context.active_object
        if obj and obj.active_material:
            materials.append(obj.active_material)
    elif scope == 'ALL':
        selected_objs = context.selected_objects
        processed = set()
        for obj in selected_objs:
            if not hasattr(obj, "material_slots"): continue
            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat not in processed:
                    materials.append(mat)
                    processed.add(mat)
    return materials


def get_compress_target_materials(context, scope):
    """
    压缩专用材质范围（与「自动连接」的 CURRENT 区分）：
    - CURRENT：活动物体上所有材质槽（多材质会全部处理）
    - ALL：所选物体全部材质槽（去重）
    """
    materials = []
    if scope == 'CURRENT':
        obj = context.active_object
        if obj and getattr(obj, "material_slots", None):
            seen = set()
            for slot in obj.material_slots:
                m = slot.material
                if m and m not in seen:
                    seen.add(m)
                    materials.append(m)
        if not materials and obj and getattr(obj, "active_material", None):
            materials.append(obj.active_material)
    elif scope == 'ALL':
        seen = set()
        for obj in context.selected_objects or []:
            if not obj or not hasattr(obj, "material_slots"):
                continue
            for slot in obj.material_slots:
                m = slot.material
                if m and m not in seen:
                    seen.add(m)
                    materials.append(m)
    return materials


def collect_images_from_materials(materials):
    """收集材质节点树内所有 Image Texture 节点引用的 bpy.data.images。"""
    images = set()
    for mat in materials or []:
        if not mat or not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and getattr(node, "image", None):
                images.add(node.image)
    return images


def ensure_image_pixels_loaded(image):
    """
    Image.scale / 写盘依赖像素缓冲已载入内存。
    外部 FILE 且 has_data 为假时，从磁盘 reload。
    """
    if not image:
        return False
    if image.size[0] == 0 or image.size[1] == 0:
        return False
    if getattr(image, "has_data", False):
        return True
    try:
        if image.source == 'FILE' and image.filepath:
            abs_path = bpy.path.abspath(image.filepath)
            if os.path.isfile(abs_path):
                image.reload()
                return bool(getattr(image, "has_data", False))
    except Exception:
        pass
    return bool(getattr(image, "has_data", False))


def save_image_to_path_atomic(image, final_abs_path: str) -> bool:
    """
    将当前内存中的 Image 写入磁盘：先写入同目录临时文件，再 os.replace 覆盖目标。
    避免 Windows 下对正在读写的源文件原地 save() 失败或写盘后预览仍显示旧尺寸。
    """
    if not image or not final_abs_path:
        return False
    directory = os.path.dirname(final_abs_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass

    old_filepath = image.filepath
    old_raw = getattr(image, "filepath_raw", None)
    target_ext = os.path.splitext(final_abs_path)[1] or ".png"
    fd, tmp_path = tempfile.mkstemp(prefix=".pbr_save_", suffix=target_ext, dir=directory)
    os.close(fd)
    target_size = (int(image.size[0]), int(image.size[1]))
    try:
        image.filepath = tmp_path
        if old_raw is not None:
            image.filepath_raw = tmp_path
        image.save()
        os.replace(tmp_path, final_abs_path)
        tmp_path = None
        image.filepath = final_abs_path
        if old_raw is not None:
            image.filepath_raw = image.filepath
        try:
            image.reload()
        except Exception:
            pass
        # 强校验：磁盘尺寸应与当前内存尺寸一致
        disk_size = read_disk_image_size(final_abs_path)
        if disk_size == target_size:
            return True

        # 回退：直接覆盖目标路径再写一次（避免某些环境下临时替换后尺寸未更新）
        try:
            image.filepath = final_abs_path
            if old_raw is not None:
                image.filepath_raw = final_abs_path
            image.save()
            try:
                image.reload()
            except Exception:
                pass
            disk_size = read_disk_image_size(final_abs_path)
            return disk_size == target_size
        except Exception as e:
            print(f"[PBR Tools] 回退直接写盘失败 {getattr(image, 'name', '?')}: {e}")
            return False
    except Exception as e:
        print(f"[PBR Tools] 原子保存失败 {getattr(image, 'name', '?')}: {e}")
        try:
            image.filepath = old_filepath
            if old_raw is not None:
                image.filepath_raw = old_raw
        except Exception:
            pass
        return False
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


_EXT_TO_FILE_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".tga": "TARGA",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".bmp": "BMP",
    ".exr": "OPEN_EXR",
}


def write_image_memory_to_path(image, final_abs_path: str, scene=None) -> bool:
    """
    将当前内存像素写出到磁盘。
    glb/blend 打包贴图必须用 save_render：img.save() 会写出 packed_file 里的原始字节。
    """
    if not image or not final_abs_path:
        return False
    directory = os.path.dirname(final_abs_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass

    if not ensure_image_pixels_loaded(image):
        packed = getattr(image, "packed_file", None)
        if packed and packed.data:
            try:
                with open(final_abs_path, "wb") as f:
                    f.write(packed.data)
                return os.path.getsize(final_abs_path) > 0
            except Exception as e:
                print(f"[PBR Tools] 写出 packed 字节失败 {getattr(image, 'name', '?')}: {e}")
        return False

    ext = os.path.splitext(final_abs_path)[1].lower()
    target_fmt = _EXT_TO_FILE_FORMAT.get(ext)
    old_fmt = getattr(image, "file_format", None)

    try:
        if target_fmt:
            image.file_format = target_fmt
        image.save_render(final_abs_path, scene=scene)
        if os.path.isfile(final_abs_path) and os.path.getsize(final_abs_path) > 0:
            return True
    except Exception as e:
        print(f"[PBR Tools] save_render 写出失败 {getattr(image, 'name', '?')}: {e}")
    finally:
        if old_fmt is not None:
            try:
                image.file_format = old_fmt
            except Exception:
                pass

    packed = getattr(image, "packed_file", None)
    if packed and packed.data:
        try:
            with open(final_abs_path, "wb") as f:
                f.write(packed.data)
            return os.path.getsize(final_abs_path) > 0
        except Exception as e:
            print(f"[PBR Tools] save_render 失败后 packed 兜底写出失败 {getattr(image, 'name', '?')}: {e}")
    return False


def save_image_to_path_verified(image, final_abs_path: str, scene=None) -> bool:
    """
    写盘（稳定优先）：
    1) 直接 save 到目标路径（最贴近当前 Image 数据写出）；
    2) 失败时回退 save_render；
    3) 再失败才尝试原子替换。

    成功时保留 filepath 指向 final_abs_path，避免写盘后路径回滚导致 Blender 从旧文件重载变粉。
    """
    if not image or not final_abs_path:
        return False
    directory = os.path.dirname(final_abs_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass

    old_filepath = image.filepath
    old_raw = getattr(image, "filepath_raw", None)

    def _restore_path():
        try:
            image.filepath = old_filepath
            if old_raw is not None:
                image.filepath_raw = old_raw
        except Exception:
            pass

    def _commit_path():
        image.filepath = final_abs_path
        if old_raw is not None:
            image.filepath_raw = final_abs_path

    # 打包贴图 / 内存已改：save_render 优先，img.save() 会写出旧 packed 字节
    if getattr(image, "packed_file", None) or getattr(image, "has_data", False):
        if write_image_memory_to_path(image, final_abs_path, scene=scene):
            _commit_path()
            return True

    # 路径 1：直接 save 到目标路径
    try:
        _commit_path()
        image.save()
        try:
            image.reload()
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[PBR Tools] 直接 save 失败 {getattr(image, 'name', '?')}: {e}")
        _restore_path()

    # 路径 2：save_render 回退
    try:
        image.save_render(final_abs_path, scene=scene)
        _commit_path()
        try:
            image.reload()
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[PBR Tools] save_render 回退失败 {getattr(image, 'name', '?')}: {e}")
        _restore_path()

    # 路径 3：原子替换兜底
    if save_image_to_path_atomic(image, final_abs_path):
        return True
    _restore_path()
    return False


def read_disk_image_size(abs_path: str):
    """
    读取磁盘图片尺寸，不影响现有 Image datablock。
    返回 (w, h) 或 None。
    """
    if not abs_path or not os.path.isfile(abs_path):
        return None
    tmp_img = None
    try:
        tmp_img = bpy.data.images.load(abs_path, check_existing=False)
        w, h = int(tmp_img.size[0]), int(tmp_img.size[1])
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except Exception:
        return None
    finally:
        if tmp_img is not None:
            try:
                bpy.data.images.remove(tmp_img)
            except Exception:
                pass


def find_images_recursive(socket, found_images, visited_nodes):
    if not socket.is_linked:
        return
    for link in socket.links:
        node = link.from_node
        if node in visited_nodes:
            continue
        visited_nodes.add(node)
        if node.type == 'TEX_IMAGE' and node.image:
            found_images.add(node.image)
        else:
            for input_socket in node.inputs:
                if input_socket.is_linked:
                    find_images_recursive(input_socket, found_images, visited_nodes)

def process_reset_material(mat):
    if not mat:
        return False
    
    # 确保启用节点系统（Blender 会自动创建 node_tree）
    if not mat.use_nodes:
        mat.use_nodes = True
    
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    output_node = None
    principled_node = None
    nodes_to_remove = []

    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL' and output_node is None:
            output_node = n
        elif n.type == 'BSDF_PRINCIPLED' and principled_node is None:
            principled_node = n
        else:
            nodes_to_remove.append(n)
    
    for n in nodes_to_remove:
        nodes.remove(n)
    
    if not output_node:
        output_node = nodes.new('ShaderNodeOutputMaterial')
    if not principled_node:
        principled_node = nodes.new('ShaderNodeBsdfPrincipled')
    
    principled_node.location = (0, 0)
    output_node.location = (300, 0)
    
    is_connected = False
    if output_node.inputs['Surface'].is_linked:
        for link in output_node.inputs['Surface'].links:
            if link.from_node == principled_node:
                is_connected = True
                break
    
    if not is_connected:
        links.new(principled_node.outputs[0], output_node.inputs['Surface'])
    return True

def process_connect_and_arrange(mat, context=None):
    if not mat or not mat.use_nodes: return False
    
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    
    principled_node = None
    image_nodes = []
    
    for n in nodes:
        if n.type == 'BSDF_PRINCIPLED':
            principled_node = n
        elif n.type == 'TEX_IMAGE':
            image_nodes.append(n)
            
    if not principled_node:
        return False
        
    if not image_nodes:
        return True
        
    # 自动修正模式下不再盲目断开所有连接，而是由 connect_pbr_nodes 决定
    # 但如果是完全乱连的情况，用户执行“自动连接”仍期望重新建立标准连接
    # 为了满足“一键修正”，我们保留对 image_node 的重新连接，但 connect_pbr_nodes 内部变得聪明了
    for img_node in image_nodes:
        if not img_node.image: continue
        # 仅当它是我们需要管理的 PBR 贴图时，才由我们接管其连接
        is_pbr = is_pbr_texture_name_lower(get_search_name(img_node.image.name))
        if is_pbr:
            for out in img_node.outputs:
                if out.is_linked:
                    for link in list(out.links):
                        # 如果连接的目标不是 Principled BSDF 或相关的中间节点，通常说明连错了，断开它
                        links.remove(link)

    
    connect_pbr_nodes(nodes, links, principled_node, image_nodes)
    
    # 调整贴图节点的垂直分布
    # 增加间距以避免重叠，同时处理不同贴图类型的默认排列顺序
    base_y = principled_node.location.y + 300
    y_spacing = 300  # 每张贴图之间的垂直间距 (高度)
    
    loc_y_map = {
        'BASE_COLOR': base_y,
        'METALLIC': base_y - y_spacing * 1,
        'ROUGHNESS': base_y - y_spacing * 2,
        'ORM': base_y - y_spacing * 2,
        'RMA': base_y - y_spacing * 2,
        'NORMAL': base_y - y_spacing * 3,
        'AO': base_y - y_spacing * 4,
        'ALPHA': base_y - y_spacing * 5
    }
    
    intermediate_x = principled_node.location.x - 260
    base_x = principled_node.location.x - 560
    
    # 排列中间节点（如果存在）
    nrm_socket = principled_node.inputs.get('Normal')
    if nrm_socket and nrm_socket.is_linked:
        nrm_node = nrm_socket.links[0].from_node
        if nrm_node.type == 'NORMAL_MAP':
            nrm_node.location = (intermediate_x, loc_y_map['NORMAL'])
            
    rough_socket = principled_node.inputs.get('Roughness')
    if rough_socket and rough_socket.is_linked:
        sep_node = rough_socket.links[0].from_node
        if sep_node.type == 'SEPARATE_COLOR':
            sep_node.location = (intermediate_x, loc_y_map['ORM'])
            
    bc_socket = principled_node.inputs.get('Base Color')
    if bc_socket and bc_socket.is_linked:
        mix_node = bc_socket.links[0].from_node
        if is_multiply_mix_node(mix_node):
            mix_node.location = (intermediate_x, loc_y_map['BASE_COLOR'])
            
    # 排列贴图节点
    for img_node in image_nodes:
        if not img_node.image: continue

        # 判断当前贴图属于什么类型，按类型决定摆放高度
        img_node.location.x = base_x
        channel = find_channel_match(get_search_name(img_node.image.name))
        if channel is not None:
            img_node.location.y = loc_y_map.get(channel.channel, principled_node.location.y)
        else:
            img_node.location.y = principled_node.location.y - y_spacing * 5
            
    # --- 调用 Node Arrange 插件 (如果可用) ---
    if context:
        try:
            if hasattr(bpy.ops.node, "button"):
                area = next((a for a in context.screen.areas if a.type == 'NODE_EDITOR'), None)
                if area:
                    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                    if region:
                        # 对于不同 Blender 版本，temp_override 可能是需要 dict 或者 **kwargs
                        # 我们先强制将当前活动对象的材质切换为正在处理的 mat
                        obj = context.active_object
                        old_idx = -1
                        if obj and hasattr(obj, 'material_slots'):
                            old_idx = obj.active_material_index
                            for i, slot in enumerate(obj.material_slots):
                                if slot.material == mat:
                                    obj.active_material_index = i
                                    break
                        
                        # Node Arrange 会重排 active material, 所以上面的切换很重要
                        if bpy.app.version >= (3, 2, 0):
                            with context.temp_override(area=area, region=region):
                                bpy.ops.node.button()
                        else:
                            override = context.copy()
                            override['area'] = area
                            override['region'] = region
                            bpy.ops.node.button(override)
                            
                        if obj and old_idx >= 0:
                            obj.active_material_index = old_idx
        except Exception as e:
            print(f"[PBR Tools] 自动调用 Node Arrange 失败: {e}")
            
    return True

def process_clean_unused_nodes(mat):
    if not mat or not mat.use_nodes: return False
    
    nodes = mat.node_tree.nodes
    outputs = [n for n in nodes if n.type == 'OUTPUT_MATERIAL']
    if not outputs:
        return False
        
    visited = set()
    
    def traverse_backwards(node):
        if node in visited:
            return
        visited.add(node)
        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    if link.from_node:
                        traverse_backwards(link.from_node)
                        
    for out_node in outputs:
        traverse_backwards(out_node)
        
    to_remove = [n for n in nodes if n not in visited]
    
    for n in to_remove:
        nodes.remove(n)
        
    return len(to_remove) > 0

def process_rename_material(mat):
    """
    根据优先级最高的已连接贴图重命名材质球，材质名统一 M_ 前缀。
    """
    if not mat or not mat.use_nodes: return False
    nodes = mat.node_tree.nodes
    prefix = MATERIAL_PREFIX
    prefix_stem = prefix[:-1]  # M_ -> M（兼容 T_Name 这类被误当 T_ 前缀的名字）
    
    image_nodes = [n for n in nodes if n.type == 'TEX_IMAGE']
    if not image_nodes: return False

    new_material_name = None
    best_priority = -1 

    for img_node in image_nodes:
        if not img_node.image:
            continue
        
        raw_clean_name = clean_name_for_rename(img_node.image.name)
        channel = find_channel_match(raw_clean_name)
        if channel is None:
            continue

        # 基础色最能代表整套贴图的名字，优先用它；其余通道作兜底
        current_priority = 2 if channel.channel == 'BASE_COLOR' else 1
        if current_priority <= best_priority:
            continue

        # 字段之前的部分就是材质名来源；字段写在最前面时，退回取它后面的部分
        core_name = _strip_trailing_noise(raw_clean_name[:channel.start])
        if not core_name:
            core_name = _strip_trailing_noise(raw_clean_name[channel.end:])
        if not core_name:
            continue

        if core_name.upper().startswith("T_"):
            final_name = prefix + core_name[2:]
        elif core_name.upper().startswith("T"):
            final_name = prefix_stem + core_name[1:]
        else:
            final_name = prefix + core_name

        new_material_name = final_name
        best_priority = current_priority

    if new_material_name and mat.name != new_material_name:
        _force_claim_name(bpy.data.materials, mat, new_material_name)
        return True
        
    return False


def process_rename_materials_from_object(obj):
    """
    根据物体名重命名其材质槽中的材质，材质名统一 M_ 前缀：
    - 物体名前缀：SM_ / sm_ -> 去掉 SM_，加材质前缀
    - 无 SM_ 前缀：直接加材质前缀
    - 已有 M_ / 旧 MI_ 前缀的旧名先剥掉，保证重复执行不叠加前缀
    - 单材质：直接用目标名称
    - 多材质：按槽位顺序追加大写字母 A/B/C...：M_xxx_A/B/C...
    返回值：成功重命名的材质数量
    """
    if not obj or not hasattr(obj, "material_slots"):
        return 0

    # 收集非空材质槽并保持顺序
    non_empty_slots = []
    for slot in obj.material_slots:
        if getattr(slot, "material", None) is not None:
            non_empty_slots.append(slot)

    if not non_empty_slots:
        return 0

    obj_name = obj.name
    if obj_name.lower().startswith("sm_"):
        core_name = obj_name[3:]  # 去掉 SM_
    else:
        core_name = obj_name
    # 旧名可能带 M_ / 旧 MI_ 前缀，先剥掉，保证重复执行不叠加前缀
    lowered_core = core_name.lower()
    for legacy_prefix in ("mi_", "m_"):
        if lowered_core.startswith(legacy_prefix):
            core_name = core_name[len(legacy_prefix):]
            break
    target_base = MATERIAL_PREFIX + core_name

    renamed = 0
    if len(non_empty_slots) == 1:
        target_name = target_base
        mat = non_empty_slots[0].material
        if mat and mat.name != target_name:
            if _force_claim_name(bpy.data.materials, mat, target_name):
                renamed = 1
        return renamed

    def _indexed_slot_suffix(index: int) -> str:
        """
        后缀规则：
        - 第 1 轮：A ~ Z
        - 第 2 轮：A1 ~ Z1
        - 第 3 轮：A2 ~ Z2
        """
        letter = chr(65 + (index % 26))  # 65='A'
        round_idx = index // 26
        if round_idx == 0:
            return letter
        return f"{letter}{round_idx}"

    # 多材质：按槽位顺序追加后缀，支持超出 Z 的连续编号
    for idx, slot in enumerate(non_empty_slots):
        suffix = _indexed_slot_suffix(idx)
        target_name = f"{target_base}_{suffix}"
        mat = slot.material
        if mat and mat.name != target_name:
            if _force_claim_name(bpy.data.materials, mat, target_name):
                renamed += 1

    return renamed


def _build_texture_base_name_from_material_name(material_name):
    """将材质名转换为贴图基础名：M_xxx -> T_xxx（旧 MI_xxx 同样兼容）；否则前置 T_。"""
    if not material_name:
        return "T_Untitled"
    lowered = material_name.lower()
    if lowered.startswith("mi_"):
        return "T_" + material_name[3:]
    if lowered.startswith("m_"):
        return "T_" + material_name[2:]
    return "T_" + material_name


def _image_extension(image):
    """获取真实的图像扩展名。file_format 反映内存中实际格式，优先于 filepath（转换后路径可能未改）。"""
    format_ext_map = {
        'JPEG': '.jpg', 'PNG': '.png', 'TARGA': '.tga', 'TARGA_RAW': '.tga',
        'TIFF': '.tif', 'BMP': '.bmp', 'OPEN_EXR': '.exr'
    }
    if image.file_format in format_ext_map:
        return format_ext_map[image.file_format]

    name_ext = os.path.splitext(image.name)[1].lower()
    valid_exts = ['.png', '.jpg', '.jpeg', '.tga', '.exr', '.tif', '.tiff', '.bmp', '.psd']
    if name_ext in valid_exts:
        return '.jpg' if name_ext == '.jpeg' else name_ext

    if image.filepath:
        ext = os.path.splitext(bpy.path.abspath(image.filepath))[1].lower()
        if ext in valid_exts:
            return '.jpg' if ext == '.jpeg' else ext

    return ".png"


def _make_link_key(link):
    try:
        return (
            link.from_node.name,
            link.from_socket.identifier,
            link.to_node.name,
            link.to_socket.identifier,
        )
    except Exception:
        return (id(link),)


def _is_socket_reaching_principled_input(output_socket, input_name, visited_links):
    """
    递归判断某个输出 socket 是否最终连到 Principled 的指定输入。
    """
    for link in output_socket.links:
        link_key = _make_link_key(link)
        if link_key in visited_links:
            continue
        visited_links.add(link_key)

        to_node = link.to_node
        to_socket = link.to_socket

        if to_node.type == 'BSDF_PRINCIPLED' and to_socket.name == input_name:
            return True

        if to_node.type == 'NORMAL_MAP' and to_socket.name == 'Color':
            normal_out = to_node.outputs.get('Normal')
            if normal_out and _is_socket_reaching_principled_input(normal_out, 'Normal', visited_links):
                return True

        for out_socket in to_node.outputs:
            if out_socket.is_linked and _is_socket_reaching_principled_input(out_socket, input_name, visited_links):
                return True
    return False


def _is_socket_reaching_gltf_occlusion(output_socket, visited_links):
    """
    递归判断输出是否最终连到 glTF Material Output（或同类）的 Occlusion 槽，
    与 GLB 导入常见的 ORM：Separate Color 的 Red → Occlusion 一致。
    """
    for link in output_socket.links:
        link_key = _make_link_key(link)
        if link_key in visited_links:
            continue
        visited_links.add(link_key)

        to_node = link.to_node
        to_socket = link.to_socket

        if to_socket and to_socket.name == 'Occlusion':
            return True

        for out_sock in to_node.outputs:
            if out_sock.is_linked and _is_socket_reaching_gltf_occlusion(out_sock, visited_links):
                return True
    return False


def _trace_usage_suffixes_from_socket(output_socket, visited_links):
    """
    从图像节点某个输出出发，追踪最终用途并返回命名后缀集合。
    """
    found = set()
    for link in output_socket.links:
        link_key = _make_link_key(link)
        if link_key in visited_links:
            continue
        visited_links.add(link_key)

        to_node = link.to_node
        to_socket = link.to_socket

        # 直接连 Principled 通道
        if to_node.type == 'BSDF_PRINCIPLED':
            if to_socket.name == 'Base Color':
                found.add('_D')
            elif to_socket.name == 'Roughness':
                found.add('_R')
            elif to_socket.name == 'Metallic':
                found.add('_M')
            elif to_socket.name == 'Alpha':
                found.add('_Alpha')
            continue

        if to_socket and to_socket.name == 'Occlusion':
            found.add('_AO')
            continue

        # 通过 Normal Map 连 Principled.Normal
        if to_node.type == 'NORMAL_MAP' and to_socket.name == 'Color':
            normal_out = to_node.outputs.get('Normal')
            if normal_out:
                if _is_socket_reaching_principled_input(normal_out, 'Normal', set(visited_links)):
                    found.add('_N')
            continue

        # Separate Color：绿→Roughness 且 蓝→Metallic 时视为完整 ORM，命名始终 _ORM（红仍可接 Occlusion）。
        # 仅当「只有红通道」接到外置 Occlusion（如 glTF Material Output）且未形成上述 ORM 时，才按 _AO 命名。
        if to_node.type == 'SEPARATE_COLOR' and to_socket.name == 'Color':
            has_rough = False
            has_metal = False
            for out_socket in to_node.outputs:
                if _is_socket_reaching_principled_input(out_socket, 'Roughness', set(visited_links)):
                    has_rough = True
                if _is_socket_reaching_principled_input(out_socket, 'Metallic', set(visited_links)):
                    has_metal = True
            red_out = to_node.outputs.get('Red')
            is_full_orm = has_rough and has_metal
            if is_full_orm:
                found.add('_ORM')
            elif red_out and _is_socket_reaching_gltf_occlusion(red_out, set(visited_links)):
                found.add('_AO')
            if not is_full_orm:
                if has_rough:
                    found.add('_R')
                if has_metal:
                    found.add('_M')
            continue

        # 其他中间节点：继续向后递归追踪
        for next_output in to_node.outputs:
            if next_output.is_linked:
                found.update(_trace_usage_suffixes_from_socket(next_output, visited_links))

    return found


def _image_needs_pixel_flush_to_disk(image, abs_path: str) -> bool:
    """
    判断当前内存中的 Image（含 scale 后分辨率）是否需覆盖写入磁盘文件。
    与压缩面板的 _pbr_unsynced_resize 标记及磁盘/内存尺寸比较一致。
    """
    if not image:
        return False
    if not abs_path or not os.path.isfile(abs_path):
        return bool(image.get("_pbr_unsynced_resize"))
    if not ensure_image_pixels_loaded(image):
        return bool(image.get("_pbr_unsynced_resize"))
    if image.get("_pbr_unsynced_resize"):
        return True
    ds = read_disk_image_size(abs_path)
    if not ds:
        return True
    w, h = int(image.size[0]), int(image.size[1])
    return ds[0] != w or ds[1] != h


def process_rename_textures_from_material(mat, props=None, scene=None):
    """
    根据材质球名称逆向重命名连接贴图。
    返回 (数据块重命名张数, 磁盘文件重命名次数, 将内存像素写回磁盘的张数)；
    未开启磁盘同步时后两项恒为 0。
    """
    if not mat or not mat.use_nodes or not mat.node_tree:
        return 0, 0, 0

    base_name = _build_texture_base_name_from_material_name(mat.name)
    image_nodes = [n for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image]
    if not image_nodes:
        return 0, 0, 0

    # 优先级：ORM > AO（仅红→外置 Occlusion 且无完整 ORM 时）> Normal > BaseColor > Roughness > Metallic
    suffix_priority = ['_ORM', '_AO', '_N', '_D', '_Alpha', '_R', '_M']
    image_to_suffixes = {}

    for img_node in image_nodes:
        img = img_node.image
        usage_suffixes = set()

        color_out = img_node.outputs.get('Color')
        if color_out and color_out.is_linked:
            usage_suffixes.update(_trace_usage_suffixes_from_socket(color_out, set()))

        alpha_out = img_node.outputs.get('Alpha')
        if alpha_out and alpha_out.is_linked:
            usage_suffixes.update(_trace_usage_suffixes_from_socket(alpha_out, set()))

        if not usage_suffixes:
            continue

        if img not in image_to_suffixes:
            image_to_suffixes[img] = set()
        image_to_suffixes[img].update(usage_suffixes)

    renamed_count = 0
    disk_renamed_count = 0
    disk_pixels_flushed = 0
    for img, suffixes in image_to_suffixes.items():
        chosen_suffix = None
        for s in suffix_priority:
            if s in suffixes:
                chosen_suffix = s
                break
        if not chosen_suffix:
            continue

        ext = _image_extension(img)
        new_base_filename = f"{base_name}{chosen_suffix}"
        new_name = f"{new_base_filename}{ext}"
        if img.name != new_name:
            if _force_claim_name(bpy.data.images, img, new_name):
                renamed_count += 1

        # 可选：同步磁盘文件（重命名路径 + 将内存中当前像素/尺寸写回文件，继承压缩/缩放结果）
        if props is not None and getattr(props, "rename_disk_files", False):
            try:
                old_path = bpy.path.abspath(img.filepath) if img.filepath else ""
            except Exception:
                old_path = ""

            if not (old_path and os.path.isfile(old_path)):
                continue

            dir_path = os.path.dirname(old_path)
            target_filename = f"{new_base_filename}{ext}"
            target_path = os.path.join(dir_path, target_filename)

            final_path = target_path
            if os.path.exists(final_path) and os.path.normpath(final_path) != os.path.normpath(old_path):
                base_no_ext, _ = os.path.splitext(new_base_filename)
                idx = 1
                while True:
                    candidate_filename = f"{base_no_ext}_{idx}{ext}"
                    candidate_path = os.path.join(dir_path, candidate_filename)
                    if not os.path.exists(candidate_path):
                        final_path = candidate_path
                        break
                    idx += 1

            did_rename = os.path.normpath(final_path) != os.path.normpath(old_path)
            if did_rename:
                try:
                    os.rename(old_path, final_path)
                    img.filepath = final_path
                    raws = getattr(img, "filepath_raw", None)
                    if raws is not None:
                        img.filepath_raw = final_path
                    disk_renamed_count += 1
                except Exception as e:
                    print(f"[PBR Tools] 重命名磁盘贴图失败 {img.name}: {e}")
                    continue
            else:
                final_path = old_path

            # 将内存中当前贴图写回 final_path：覆盖「仅 os.rename 未改像素」与「压缩后未同步到磁盘」两种情况
            if _image_needs_pixel_flush_to_disk(img, final_path):
                if save_image_to_path_verified(img, final_path, scene):
                    disk_pixels_flushed += 1
                    try:
                        if "_pbr_unsynced_resize" in img:
                            del img["_pbr_unsynced_resize"]
                    except Exception:
                        pass

    return renamed_count, disk_renamed_count, disk_pixels_flushed
