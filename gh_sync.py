#!/usr/bin/env python3
"""
aistudio-api 的 GitHub 持久化包装器（仅用标准库；加密为可选，需要 cryptography）。

用法（Render 启动命令）：
    python3 gh_sync.py run -- python3 main.py server --port $PORT

做三件事：
  1. 启动前：从 GitHub 仓库把账号/配置拉回本地数据目录
  2. 运行中：每 GH_SYNC_POLL_SEC 秒扫描一次，把变化推送回 GitHub
  3. 关停时：收到 SIGTERM 先转发给服务，等它退出后再把没推完的内容推完

同步范围（相对 AISTUDIO_DATA_DIR）：
  accounts/**   账号 Cookie(auth.json)、meta.json、registry.json、presets.json
  config.yaml   配置（含 API Key、签名节点选择）
  stats.json / rotator_state.json   仅当 GH_SYNC_EXTRA=1

环境变量：
  GH_TOKEN  GH_REPO(owner/repo)  GH_BRANCH(main)  GH_PATH(data)
  COOKIE_ENC_KEY               设置后上传前 AES-256-GCM 加密
  GH_SYNC_POLL_SEC=15          扫描间隔
  GH_SYNC_NEW_DELAY_SEC=10     新增/删除文件多久后推送
  GH_SYNC_META_DELAY_SEC=30    registry/meta/config 等小改动多久后推送
  GH_SYNC_UPDATE_INTERVAL_SEC=1800   auth.json(Cookie 轮换)等高频更新的最小推送间隔
  GH_SYNC_EXTRA=0              是否同步 stats.json / rotator_state.json
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get("GH_API_URL", "https://api.github.com").rstrip("/")
REPO = os.environ.get("GH_REPO", "").strip()
TOKEN = os.environ.get("GH_TOKEN", "").strip()
BRANCH = os.environ.get("GH_BRANCH", "main").strip() or "main"
PREFIX = (os.environ.get("GH_PATH", "data") or "data").strip("/")
DATA_DIR = Path(os.environ.get("AISTUDIO_DATA_DIR") or "data").resolve()
POLL = float(os.environ.get("GH_SYNC_POLL_SEC", "15"))
NEW_DELAY = float(os.environ.get("GH_SYNC_NEW_DELAY_SEC", "10"))
META_DELAY = float(os.environ.get("GH_SYNC_META_DELAY_SEC", "30"))
UPDATE_INTERVAL = float(os.environ.get("GH_SYNC_UPDATE_INTERVAL_SEC", "1800"))
EXTRA = os.environ.get("GH_SYNC_EXTRA", "0") == "1"

ENABLED = bool(REPO and TOKEN)
_lock = threading.Lock()
_stop = threading.Event()

remote_sha: dict[str, str] = {}   # rel -> 远端 blob sha
synced_hash: dict[str, str] = {}  # rel -> 最近一次与远端一致的明文 sha256
dirty_since: dict[str, float] = {}
fast_path: set[str] = set()       # 新增/删除的文件，走快速推送通道
push_allowed = False              # 拉取成功后才允许推送，避免用空数据覆盖远端


def log(msg: str) -> None:
    print(f"[gh-sync] {msg}", flush=True)


# ───────────────────────── 加解密（可选） ─────────────────────────
def _key() -> bytes | None:
    k = os.environ.get("COOKIE_ENC_KEY", "")
    return hashlib.sha256(k.encode()).digest() if k else None


def _aes(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("已设置 COOKIE_ENC_KEY，但未安装 cryptography（请加入 requirements.txt）") from e
    return AESGCM(key)


def encrypt(data: bytes) -> bytes:
    key = _key()
    if not key:
        return data
    iv = os.urandom(12)
    return b"ENC1:" + base64.b64encode(iv + _aes(key).encrypt(iv, data, None))


def decrypt(data: bytes) -> bytes:
    if not data.startswith(b"ENC1:"):
        return data
    key = _key()
    if not key:
        raise RuntimeError("远端文件已加密，但未设置 COOKIE_ENC_KEY")
    raw = base64.b64decode(data[5:])
    return _aes(key).decrypt(raw[:12], raw[12:], None)


# ───────────────────────── GitHub API ─────────────────────────
def api(method: str, path: str, body: dict | None = None, raw: bool = False) -> tuple[int, bytes]:
    url = f"{API}/repos/{REPO}/{path}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "User-Agent": "aistudio-gh-sync",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def remote_path(rel: str) -> str:
    return urllib.parse.quote(f"{PREFIX}/{rel}", safe="/")


def _safe_rel(rel: str) -> bool:
    p = Path(rel)
    return not p.is_absolute() and ".." not in p.parts and bool(rel)


def _wanted(rel: str) -> bool:
    if rel == "config.yaml" or rel.startswith("accounts/"):
        return True
    return EXTRA and rel in ("stats.json", "rotator_state.json")


# ───────────────────────── 拉取 ─────────────────────────
def pull() -> bool:
    """成功（含远端为空）返回 True；出错返回 False。"""
    status, body = api("GET", f"git/trees/{urllib.parse.quote(BRANCH)}?recursive=1")
    if status == 409:  # 空仓库
        log("远端仓库为空，跳过拉取")
        return True
    if status != 200:
        log(f"拉取目录失败 HTTP {status}（检查 GH_TOKEN 权限、GH_REPO、GH_BRANCH）")
        return False
    tree = json.loads(body)
    if tree.get("truncated"):
        log("警告：远端文件过多，目录被截断")
    count = 0
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith(PREFIX + "/"):
            continue
        rel = path[len(PREFIX) + 1:]
        if not _safe_rel(rel) or not _wanted(rel):
            continue
        st, content = api("GET", f"contents/{remote_path(rel)}?ref={urllib.parse.quote(BRANCH)}", raw=True)
        if st != 200:
            log(f"下载 {rel} 失败 HTTP {st}")
            return False
        try:
            plain = decrypt(content)
        except Exception as e:
            log(f"解密 {rel} 失败: {e}")
            return False
        target = DATA_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".gh-sync-part")
        tmp.write_bytes(plain)
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        remote_sha[rel] = item["sha"]
        synced_hash[rel] = hashlib.sha256(plain).hexdigest()
        count += 1
    log(f"已从 GitHub 恢复 {count} 个文件")
    return True


# ───────────────────────── 推送 ─────────────────────────
def local_files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    cands: list[Path] = []
    acc = DATA_DIR / "accounts"
    if acc.is_dir():
        cands += [p for p in acc.rglob("*") if p.is_file()]
    for name in ("config.yaml", "stats.json", "rotator_state.json"):
        p = DATA_DIR / name
        if p.is_file():
            cands.append(p)
    for p in cands:
        if ".tmp." in p.name or p.name.endswith(".gh-sync-part"):
            continue
        rel = p.relative_to(DATA_DIR).as_posix()
        if _wanted(rel):
            out[rel] = p
    return out


def delay_for(rel: str, is_new_or_deleted: bool) -> float:
    if is_new_or_deleted:
        return NEW_DELAY
    if rel.endswith("/auth.json") or rel in ("stats.json", "rotator_state.json"):
        return UPDATE_INTERVAL
    return META_DELAY


def scan() -> None:
    """发现变化，登记首次变脏的时间（节流：之后的重复修改不会推迟推送）。"""
    now = time.time()
    files = local_files()
    with _lock:
        for rel, p in files.items():
            try:
                h = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            if rel not in synced_hash:
                dirty_since.setdefault(rel, now)
                fast_path.add(rel)
            elif h != synced_hash[rel]:
                dirty_since.setdefault(rel, now)
            else:
                dirty_since.pop(rel, None)
                fast_path.discard(rel)
        for rel in list(synced_hash):
            if rel not in files and rel in remote_sha:
                dirty_since.setdefault(rel, now)
                fast_path.add(rel)


def _put(rel: str, p: Path) -> None:
    plain = p.read_bytes()
    h = hashlib.sha256(plain).hexdigest()
    content = base64.b64encode(encrypt(plain)).decode()
    for attempt in range(2):
        body = {"message": f"sync {rel}", "content": content, "branch": BRANCH}
        if rel in remote_sha:
            body["sha"] = remote_sha[rel]
        st, resp = api("PUT", f"contents/{remote_path(rel)}", body)
        if st in (200, 201):
            remote_sha[rel] = json.loads(resp)["content"]["sha"]
            synced_hash[rel] = h
            log(f"已推送 {rel}")
            return
        if st in (409, 422) and attempt == 0:  # sha 过期，取最新 sha 重试
            s2, meta = api("GET", f"contents/{remote_path(rel)}?ref={urllib.parse.quote(BRANCH)}")
            if s2 == 200:
                remote_sha[rel] = json.loads(meta)["sha"]
                continue
        raise RuntimeError(f"PUT {rel} -> HTTP {st}")


def _delete(rel: str) -> None:
    sha = remote_sha.get(rel)
    if not sha:
        synced_hash.pop(rel, None)
        return
    st, _ = api("DELETE", f"contents/{remote_path(rel)}", {"message": f"remove {rel}", "sha": sha, "branch": BRANCH})
    if st in (200, 404):
        remote_sha.pop(rel, None)
        synced_hash.pop(rel, None)
        log(f"已删除远端 {rel}")
        return
    raise RuntimeError(f"DELETE {rel} -> HTTP {st}")


def flush(force: bool = False) -> None:
    if not push_allowed:
        return
    now = time.time()
    files = local_files()
    with _lock:
        todo = list(dirty_since)
    for rel in todo:
        with _lock:
            fast = rel in fast_path
            since = dirty_since.get(rel, now)
        if not force and now - since < delay_for(rel, fast):
            continue
        try:
            if rel in files:
                _put(rel, files[rel])
            else:
                _delete(rel)
            with _lock:
                dirty_since.pop(rel, None)
                fast_path.discard(rel)
        except Exception as e:
            log(f"{e}，60 秒后重试")
            with _lock:
                dirty_since[rel] = now - delay_for(rel, fast) + 60


def watcher() -> None:
    while not _stop.wait(POLL):
        try:
            scan()
            flush()
        except Exception as e:  # 同步失败绝不能影响主服务
            log(f"同步循环异常: {e}")


# ───────────────────────── 入口 ─────────────────────────
def start_sync() -> threading.Thread | None:
    global push_allowed
    if not ENABLED:
        log("未配置 GH_TOKEN/GH_REPO，仅使用本地存储")
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ok = False
    for i in range(3):
        try:
            ok = pull()
        except Exception as e:
            log(f"拉取异常: {e}")
        if ok:
            break
        time.sleep(5)
    if not ok:
        log("拉取未成功：本次运行不会向 GitHub 推送，以免覆盖远端数据。修复配置后请重启服务")
        return None
    push_allowed = True
    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    return t


def run(cmd: list[str]) -> int:
    start_sync()
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "COOKIE_ENC_KEY")}
    child = subprocess.Popen(cmd, env=env)

    def forward(signum, _frame):
        _stop.set()
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    rc = child.wait()
    _stop.set()
    if push_allowed:
        try:
            scan()
            flush(force=True)  # 退出前把所有待推送内容推完
        except Exception as e:
            log(f"退出前同步失败: {e}")
    return rc


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "run":
        cmd = argv[argv.index("--") + 1:] if "--" in argv else argv[2:]
        if not cmd:
            print("用法: gh_sync.py run -- <启动命令>")
            return 2
        return run(cmd)
    if len(argv) >= 2 and argv[1] in ("pull", "push"):
        global push_allowed
        if not ENABLED:
            print("需要设置 GH_TOKEN 和 GH_REPO")
            return 2
        if not pull():
            return 1
        if argv[1] == "push":
            push_allowed = True
            scan()
            flush(force=True)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
