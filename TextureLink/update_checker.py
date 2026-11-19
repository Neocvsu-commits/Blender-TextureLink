"""
Blender 插件自动更新检查（共用模块）

用法：
    # __init__.py
    from update_checker import check_for_updates, get_update_info

    def register():
        ...
        check_for_updates(
            owner="Neocvsu-commits",
            repo="Blender-TextureLink",
            current_version=bl_info["version"],
            plugin_dir=os.path.dirname(__file__),
        )

    # ui.py
    info = get_update_info("Neocvsu-commits", "Blender-TextureLink")
    if info:
        layout.label(text=f"有新版本 v{info['latest_version']}")
"""

import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import urllib.error
import zipfile

_cache = {}
_cache_lock = threading.Lock()


def _parse_version_tag(tag):
    """从 tag（如 'v2.3.9' 或 '2.3.9'）提取版本 tuple。"""
    tag = tag.strip().lstrip("v").lstrip("V")
    parts = tag.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except (ValueError, IndexError):
        return None


def _version_newer(latest, current):
    """latest > current → True。填充到等长比较。"""
    max_len = max(len(latest), len(current))
    a = list(latest) + [0] * (max_len - len(latest))
    b = list(current) + [0] * (max_len - len(current))
    for av, bv in zip(a, b):
        if av > bv:
            return True
        if av < bv:
            return False
    return False


def _check_thread(owner, repo, current_version, plugin_dir):
    """后台线程：请求 GitHub API，比对版本，写入缓存。"""
    repo_key = f"{owner}/{repo}"
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"

    result = {"plugin_dir": plugin_dir}

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Blender-Addon-Update-Checker"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        tag = data.get("tag_name", "")
        latest_ver = _parse_version_tag(tag)
        if latest_ver is None:
            result["status"] = "error"
            result["error"] = f"无法解析版本标签: {tag}"
            return

        current_ver = tuple(current_version)
        has_update = _version_newer(latest_ver, current_ver)

        cur_str = ".".join(str(v) for v in current_ver)
        latest_str = ".".join(str(v) for v in latest_ver)

        result["status"] = "has_update" if has_update else "no_update"
        result["latest_version"] = latest_str
        result["current_version"] = cur_str
        result["tag_name"] = tag
        result["html_url"] = data.get("html_url", "")
        result["zip_url"] = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip"

    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 仓库尚未发布任何 Release
            result["status"] = "no_release"
            result["current_version"] = ".".join(str(v) for v in current_version)
        else:
            result["status"] = "error"
            result["error"] = f"GitHub API HTTP {e.code}"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    finally:
        # Publish the status and version/URL together for the sidebar draw callback.
        with _cache_lock:
            _cache[repo_key] = result


# ---- 公开 API ----


def check_for_updates(owner, repo, current_version, plugin_dir):
    """启动后台检查（首次调用发起请求，后续调用走缓存，不重复查）。"""
    repo_key = f"{owner}/{repo}"
    with _cache_lock:
        if repo_key in _cache:
            return
    _start_check_thread(owner, repo, current_version, plugin_dir)


def force_check_for_updates(owner, repo, current_version, plugin_dir):
    """强制重新检查（忽略缓存，用户手动触发）。"""
    _start_check_thread(owner, repo, current_version, plugin_dir)


def _start_check_thread(owner, repo, current_version, plugin_dir):
    repo_key = f"{owner}/{repo}"
    with _cache_lock:
        if _cache.get(repo_key, {}).get("status") == "checking":
            return
        _cache[repo_key] = {"status": "checking", "plugin_dir": plugin_dir}
    t = threading.Thread(
        target=_check_thread,
        args=(owner, repo, current_version, plugin_dir),
        daemon=True,
    )
    t.start()


def get_update_info(owner, repo):
    """UI 线程调用：返回 update 信息 dict，无更新返回 None。"""
    repo_key = f"{owner}/{repo}"
    with _cache_lock:
        info = _cache.get(repo_key)
    if info and info.get("status") == "has_update":
        return {
            "latest_version": info["latest_version"],
            "current_version": info["current_version"],
            "html_url": info["html_url"],
            "zip_url": info["zip_url"],
        }
    return None


def get_check_status(owner, repo):
    """UI 线程调用：返回检查状态，用于显示「检查中/已是最新/出错/有更新」。"""
    repo_key = f"{owner}/{repo}"
    with _cache_lock:
        info = _cache.get(repo_key)
    if not info:
        return {"status": "pending", "current_version": None}
    return {
        "status": info.get("status", "error"),
        "current_version": info.get("current_version"),
        "latest_version": info.get("latest_version"),
        "error": info.get("error", ""),
    }


def install_update(owner, repo, plugin_dir=None):
    """下载最新版 zip 并覆盖 plugin_dir 下文件。返回 (success, message)。"""
    repo_key = f"{owner}/{repo}"
    with _cache_lock:
        info = _cache.get(repo_key)
    if not info or info.get("status") != "has_update":
        return False, "没有可用的更新信息，请先检查更新"

    zip_url = info.get("zip_url", "")
    effective_dir = plugin_dir or info.get("plugin_dir", "")
    if not zip_url or not effective_dir:
        return False, "缺少下载地址或插件路径"

    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="blender_addon_update_")
        zip_path = os.path.join(tmp_dir, "update.zip")

        # 重试逻辑：国内访问 GitHub 归档偶尔 504/超时，最多试 3 次，间隔递增
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    zip_url,
                    headers={"User-Agent": "Blender-Addon-Update-Checker"},
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    with open(zip_path, "wb") as f:
                        shutil.copyfileobj(resp, f)
                break
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
            except Exception as e:
                last_error = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))  # 2s, 4s
        else:
            return False, f"下载失败（已重试 {max_retries} 次）: {last_error}"

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # GitHub archive 解压后外层是 {repo}-{tag} 目录
        items = os.listdir(extract_dir)
        root_dir = os.path.join(extract_dir, items[0]) if len(items) == 1 else extract_dir

        # 在 repo 根目录下找插件包（含 __init__.py 的目录）
        addon_src = None
        for item in os.listdir(root_dir):
            item_path = os.path.join(root_dir, item)
            if os.path.isdir(item_path) and os.path.isfile(os.path.join(item_path, "__init__.py")):
                addon_src = item_path
                break

        if not addon_src:
            return False, "下载的压缩包中未找到插件包，请手动更新"

        # 覆盖插件目录
        for item in os.listdir(addon_src):
            src = os.path.join(addon_src, item)
            dst = os.path.join(effective_dir, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        return True, f"已更新到 v{info['latest_version']}，请重启 Blender 以生效"

    except Exception as e:
        return False, f"更新失败: {e}"
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass
