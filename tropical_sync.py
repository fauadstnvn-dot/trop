#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TROPICAL TIDBITS AUTO SYNC (chuyển từ trop.php sang Python để chạy bằng GitHub Actions)

Quy trình cho từng model:
  1. Lấy danh sách TẤT CẢ pkg (thông số) từ #pkgnavbar và lưu vào tropical_models.
  2. Với từng pkg, lấy APP.imgURLs, so sánh với dữ liệu đã lưu:
       - Nếu ảnh đầu tiên trùng VÀ số lượng ảnh bằng nhau -> bỏ qua (chỉ cập nhật updateat).
       - Nếu không -> xóa ảnh của runtime cũ, tải lại toàn bộ ảnh mới (force overwrite),
         thu nhỏ còn 80%, đóng dấu dongdau.png ở góc phải dưới, lưu JPG chất lượng 85.
  3. Tùy chọn upload ảnh lên hosting qua FTP/FTPS (và xóa ảnh cũ trên hosting).

Lưu trạng thái:
  - Nếu có biến DB_HOST -> dùng MySQL (cùng bảng tropical_models / tropical_images như bản PHP).
  - Nếu không -> dùng file state.json (workflow sẽ commit file này lại vào repo).

Toàn bộ cấu hình đọc từ biến môi trường (xem README.md).
"""

from __future__ import annotations

import io
import json
import os
import queue as queue_mod
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -------------------------------------------------------------------------
# CẤU HÌNH
# -------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
BASE_URL = "https://www.tropicaltidbits.com/analysis/models/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value and value.strip() else default


MODELS = [m.strip().lower() for m in env("MODELS", "gfs,ecmwf,jma,ec-aifs,icon,gem").split(",") if m.strip()]
REGION = env("REGION", "wpac")
PKG_FILTER = {p.strip().lower() for p in env("PKG_FILTER").split(",") if p.strip()}
THREADS = max(1, int(env("THREADS", "8")))
SCALE = float(env("SCALE", "0.8"))
JPG_QUALITY = int(env("JPG_QUALITY", "85"))
SAVE_DIR = BASE_DIR / env("SAVE_DIR", "imgtrop")
WATERMARK_FILE = BASE_DIR / env("WATERMARK_FILE", "dongdau.png")
STATE_FILE = BASE_DIR / env("STATE_FILE", "state.json")
KEEP_LOCAL = env("KEEP_LOCAL", "true").lower() == "true"

DB_HOST = env("DB_HOST")
DB_PORT = int(env("DB_PORT", "3306"))
DB_USER = env("DB_USER")
DB_PASS = env("DB_PASS")
DB_NAME = env("DB_NAME")

FTP_HOST = env("FTP_HOST")
FTP_PORT = int(env("FTP_PORT", "21"))
FTP_USER = env("FTP_USER")
FTP_PASS = env("FTP_PASS")
FTP_DIR = env("FTP_DIR", "/public_html/imgtrop")
FTP_TLS_ON = env("FTP_TLS", "false").lower() == "true"
FTP_MAX_CONN = max(1, int(env("FTP_MAX_CONN", "5")))
FTP_RETRIES = max(1, int(env("FTP_RETRIES", "4")))
PKG_DELAY = float(env("PKG_DELAY", "0.5"))
MAX_PENDING_UPLOADS = max(1, int(env("MAX_PENDING_UPLOADS", "300")))

# -------------------------------------------------------------------------
# LOG
# -------------------------------------------------------------------------
_log_lock = threading.Lock()
COLORS = {"info": "", "success": "\033[32m", "error": "\033[31m", "system": "\033[36m"}


def log(msg: str, kind: str = "info") -> None:
    with _log_lock:
        color = COLORS.get(kind, "")
        reset = "\033[0m" if color else ""
        print(f"{color}{msg}{reset}", flush=True)


def now_vn() -> str:
    return datetime.now(TZ_VN).strftime("%Y-%m-%d %H:%M:%S")


# -------------------------------------------------------------------------
# HTTP
# -------------------------------------------------------------------------
def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=THREADS * 2, pool_maxsize=THREADS * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


SESSION = build_session()


def fetch_text(url: str, referer: str = "") -> str:
    headers = {"Referer": referer} if referer else {}
    try:
        resp = SESSION.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.text
        log(f"  => HTTP {resp.status_code} khi truy cập {url}", "error")
    except requests.RequestException as exc:
        log(f"  => Lỗi kết nối {url}: {exc}", "error")
    return ""


# -------------------------------------------------------------------------
# LƯU TRỮ TRẠNG THÁI (MySQL hoặc state.json)
# -------------------------------------------------------------------------
class MySQLStore:
    def __init__(self) -> None:
        import pymysql

        self.conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=20,
        )
        self._ensure_tables()

    def _cursor(self):
        self.conn.ping(reconnect=True)
        return self.conn.cursor()

    def _ensure_tables(self) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tropical_models (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    model_name VARCHAR(50) NOT NULL,
                    category VARCHAR(255) DEFAULT NULL,
                    pkg_display_name VARCHAR(255) DEFAULT NULL,
                    pkg_url_name VARCHAR(100) NOT NULL,
                    updateat DATETIME DEFAULT NULL,
                    UNIQUE KEY uniq_model_pkg (model_name, pkg_url_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tropical_images (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    model_id INT NOT NULL,
                    image_name VARCHAR(255) NOT NULL,
                    runtime VARCHAR(50) DEFAULT NULL,
                    UNIQUE KEY uniq_image (image_name),
                    KEY idx_model (model_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

    def upsert_pkg(self, model: str, category: str, name: str, pkg: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO tropical_models (model_name, category, pkg_display_name, pkg_url_name) "
                "VALUES (%s, %s, %s, %s)",
                (model, category, name, pkg),
            )

    def get_model_id(self, model: str, pkg: str):
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM tropical_models WHERE model_name = %s AND pkg_url_name = %s",
                (model, pkg),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_images(self, model_id) -> list[str]:
        with self._cursor() as cur:
            cur.execute("SELECT image_name FROM tropical_images WHERE model_id = %s ORDER BY id ASC", (model_id,))
            return [r[0] for r in cur.fetchall()]

    def delete_image(self, image_name: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM tropical_images WHERE image_name = %s", (image_name,))

    def add_image(self, model_id, image_name: str, runtime: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO tropical_images (model_id, image_name, runtime) VALUES (%s, %s, %s)",
                (model_id, image_name, runtime),
            )

    def touch(self, model_id) -> str:
        ts = now_vn()
        with self._cursor() as cur:
            cur.execute("UPDATE tropical_models SET updateat = %s WHERE id = %s", (ts, model_id))
        return ts

    def close(self) -> None:
        self.conn.close()


class JsonStore:
    """Thay thế MySQL bằng file state.json khi không cấu hình DB."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        if STATE_FILE.exists():
            self.data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        else:
            self.data = {"models": {}}

    def upsert_pkg(self, model: str, category: str, name: str, pkg: str) -> None:
        key = f"{model}|{pkg}"
        with self.lock:
            self.data["models"].setdefault(
                key,
                {"model_name": model, "category": category, "pkg_display_name": name,
                 "pkg_url_name": pkg, "updateat": None, "images": []},
            )

    def get_model_id(self, model: str, pkg: str):
        key = f"{model}|{pkg}"
        return key if key in self.data["models"] else None

    def get_images(self, model_id) -> list[str]:
        return [img["image_name"] for img in self.data["models"][model_id]["images"]]

    def delete_image(self, image_name: str) -> None:
        with self.lock:
            for entry in self.data["models"].values():
                entry["images"] = [i for i in entry["images"] if i["image_name"] != image_name]

    def add_image(self, model_id, image_name: str, runtime: str) -> None:
        with self.lock:
            images = self.data["models"][model_id]["images"]
            if not any(i["image_name"] == image_name for i in images):
                images.append({"image_name": image_name, "runtime": runtime})

    def touch(self, model_id) -> str:
        ts = now_vn()
        with self.lock:
            self.data["models"][model_id]["updateat"] = ts
        self.save()
        return ts

    def save(self) -> None:
        with self.lock:
            STATE_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        self.save()


# -------------------------------------------------------------------------
# FTP (tùy chọn) - dùng chung 1 "hồ" kết nối cố định (FTP_MAX_CONN) cho mọi luồng,
# để không bao giờ vượt giới hạn số kết nối/IP của hosting (lỗi 421).
# -------------------------------------------------------------------------
class FtpUploader:
    def __init__(self) -> None:
        self.enabled = bool(FTP_HOST and FTP_USER)
        self.pool: queue_mod.Queue = queue_mod.Queue()
        self.slots = threading.Semaphore(FTP_MAX_CONN)
        self.all_conns: list = []
        self.lock = threading.Lock()
        if self.enabled:
            ftp = self._connect()
            self._ensure_dir(ftp, FTP_DIR)
            self.pool.put(ftp)
            log(f"Đã kết nối FTP {FTP_HOST} -> thư mục {FTP_DIR} (tối đa {FTP_MAX_CONN} kết nối)", "success")

    def _connect(self):
        for attempt in range(FTP_RETRIES):
            try:
                ftp = FTP_TLS() if FTP_TLS_ON else FTP()
                ftp.connect(FTP_HOST, FTP_PORT, timeout=60)
                ftp.login(FTP_USER, FTP_PASS)
                if FTP_TLS_ON:
                    ftp.prot_p()
                ftp.set_pasv(True)
                with self.lock:
                    self.all_conns.append(ftp)
                return ftp
            except Exception as exc:
                if "421" in str(exc) and attempt < FTP_RETRIES - 1:
                    wait = 5 * (attempt + 1)
                    log(f"    FTP báo quá nhiều kết nối, chờ {wait}s rồi thử lại...", "system")
                    time.sleep(wait)
                    continue
                raise

    def _acquire(self):
        self.slots.acquire()
        try:
            ftp = self.pool.get_nowait()
        except queue_mod.Empty:
            return self._connect_or_release()
        try:
            ftp.voidcmd("NOOP")
            return ftp
        except Exception:
            self._drop(ftp)
            return self._connect_or_release()

    def _connect_or_release(self):
        try:
            return self._connect()
        except Exception:
            self.slots.release()
            raise

    def _release(self, ftp) -> None:
        self.pool.put(ftp)
        self.slots.release()

    def _drop(self, ftp) -> None:
        try:
            ftp.close()
        except Exception:
            pass
        with self.lock:
            if ftp in self.all_conns:
                self.all_conns.remove(ftp)

    @staticmethod
    def _ensure_dir(ftp, path: str) -> None:
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            try:
                ftp.mkd(current)
            except error_perm:
                pass

    def upload(self, name: str, data: bytes) -> None:
        for attempt in range(FTP_RETRIES):
            ftp = self._acquire()
            try:
                ftp.storbinary(f"STOR {FTP_DIR.rstrip('/')}/{name}", io.BytesIO(data), blocksize=262144)
                self._release(ftp)
                return
            except Exception:
                self._drop(ftp)
                self.slots.release()
                if attempt == FTP_RETRIES - 1:
                    raise
                time.sleep(3 * (attempt + 1))

    def delete(self, name: str) -> None:
        try:
            ftp = self._acquire()
        except Exception:
            return
        try:
            ftp.delete(f"{FTP_DIR.rstrip('/')}/{name}")
            self._release(ftp)
        except error_perm:
            self._release(ftp)
        except Exception:
            self._drop(ftp)
            self.slots.release()

    def close(self) -> None:
        with self.lock:
            conns = list(self.all_conns)
            self.all_conns.clear()
        for ftp in conns:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


# -------------------------------------------------------------------------
# XỬ LÝ ẢNH: thu nhỏ 80%, nền trắng, đóng dấu, lưu JPG
# -------------------------------------------------------------------------
WATERMARK: Image.Image | None = None
if WATERMARK_FILE.exists():
    try:
        WATERMARK = Image.open(WATERMARK_FILE).convert("RGBA")
    except Exception as exc:
        print(f"Không đọc được {WATERMARK_FILE.name}: {exc}")


def process_image(img_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(img_bytes)) as src:
        src = src.convert("RGBA")
        new_w, new_h = max(1, int(src.width * SCALE)), max(1, int(src.height * SCALE))
        resized = src.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
    canvas.paste(resized, (0, 0), resized)

    if WATERMARK is not None:
        padding = 10
        dest = (new_w - WATERMARK.width - padding, new_h - WATERMARK.height - padding)
        canvas.paste(WATERMARK, dest, WATERMARK)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=JPG_QUALITY, optimize=True)
    return out.getvalue()


# -------------------------------------------------------------------------
# BƯỚC 1: LẤY DANH SÁCH PKG
# -------------------------------------------------------------------------
def get_pkgs(model: str, store) -> list[dict]:
    url = f"{BASE_URL}?model={model}&region={REGION}"
    html = fetch_text(url)
    if not html:
        log(f"Không thể kết nối lấy dữ liệu cho {model}.", "error")
        return []

    soup = BeautifulSoup(html, "html.parser")
    navbar = soup.find(id="pkgnavbar")
    if navbar is None:
        log(f"Không có dữ liệu pkg cho model '{model}'.", "error")
        return []

    pkgs: list[dict] = []
    seen: set[str] = set()
    for cat_li in navbar.find_all("li", recursive=False):
        cat_a = cat_li.find("a", class_="menuTarget", recursive=False)
        category = cat_a.get_text(strip=True) if cat_a else "Khác"

        for ul in cat_li.find_all("ul", class_="pkgmenu", recursive=False):
            for li in ul.find_all("li", recursive=False):
                a = li.find("a", recursive=False)
                if a is None:
                    continue
                match = re.search(r"[?&]pkg=([^&]+)", a.get("href", ""))
                if not match:
                    continue
                pkg = match.group(1).strip().lower()
                if pkg in seen:
                    continue
                seen.add(pkg)
                name = a.get_text(" ", strip=True)
                store.upsert_pkg(model, category, name, pkg)
                pkgs.append({"pkg": pkg, "name": name, "category": category})
    return pkgs


# -------------------------------------------------------------------------
# BƯỚC 2: TẢI ẢNH CHO 1 PKG
# -------------------------------------------------------------------------
def download_one(item: dict) -> tuple[bytes | None, str]:
    try:
        resp = SESSION.get(item["url"], headers={"Referer": item["referer"]}, timeout=60)
        if resp.status_code != 200 or not resp.content:
            return None, f"LỖI HTTP {resp.status_code}: {item['name']}"
        jpg = process_image(resp.content)
    except Exception as exc:
        return None, f"LỖI xử lý {item['name']}: {exc}"

    if KEEP_LOCAL:
        (SAVE_DIR / item["name"]).write_bytes(jpg)
    return jpg, f"Tải & xử lý JPG OK -> {item['name']} ({len(jpg) / 1024:.2f} KB)"


class Uploader:
    """Đẩy ảnh lên FTP ở nền, để luồng tải có thể chuyển ngay sang pkg tiếp theo."""

    def __init__(self, ftp: FtpUploader) -> None:
        self.ftp = ftp
        self.pool = ThreadPoolExecutor(max_workers=FTP_MAX_CONN) if ftp.enabled else None
        # Giới hạn số ảnh đang chờ upload trong RAM
        self.inflight = threading.Semaphore(MAX_PENDING_UPLOADS)

    def submit(self, name: str, jpg: bytes):
        if self.pool is None:
            return None
        self.inflight.acquire()
        fut = self.pool.submit(self._upload, name, jpg)
        fut.add_done_callback(lambda _f: self.inflight.release())
        return fut

    def _upload(self, name: str, jpg: bytes) -> tuple[bool, str]:
        try:
            self.ftp.upload(name, jpg)
            return True, ""
        except Exception as exc:
            return False, f"LỖI FTP {name}: {exc}"

    def shutdown(self) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True)


class PkgJob:
    def __init__(self, model: str, pkg: str, model_id: int, queue: list[dict]) -> None:
        self.model = model
        self.pkg = pkg
        self.model_id = model_id
        self.queue = queue
        self.results: dict[str, object] = {}

    def ready(self) -> bool:
        return all(r is None or r is True or r.done() for r in self.results.values())


def finalize_job(job: PkgJob, store, stats: dict) -> None:
    ok_count = 0
    for item in job.queue:
        res = job.results.get(item["name"])
        if res is None:
            continue
        if res is not True:
            ok, msg = res.result()
            if not ok:
                stats["fail"] += 1
                log(f"    {msg}", "error")
                continue
        store.add_image(job.model_id, item["name"], item["runtime"])
        stats["success"] += 1
        ok_count += 1
    ts = store.touch(job.model_id)
    log(f"  => [{job.model}/{job.pkg}] Hoàn tất {ok_count}/{len(job.queue)} ảnh. Cập nhật: {ts} (GMT+7)", "info")


def flush_jobs(pending: list[PkgJob], store, stats: dict, wait: bool = False) -> None:
    for job in list(pending):
        if wait or job.ready():
            finalize_job(job, store, stats)
            pending.remove(job)


def sync_pkg(model: str, pkg: str, store, ftp: FtpUploader, uploader: Uploader, stats: dict) -> PkgJob | None:
    model_id = store.get_model_id(model, pkg)
    if model_id is None:
        log(f"  => LỖI: Không tìm thấy [{model} - {pkg}] trong dữ liệu.", "error")
        return None

    target_url = f"{BASE_URL}?model={model}&region={REGION}&pkg={pkg}"
    log(f"Đang truy cập Model [{model}] - Pkg [{pkg}]...", "system")
    html = fetch_text(target_url, target_url)

    img_paths: list[str] = []
    block = re.search(r"APP\.imgURLs\s*=\s*\[(.*?)\];", html, re.S | re.I)
    if block:
        img_paths = re.findall(r"'([^']+)'", block.group(1))
    if not img_paths:
        log(f"  => LỖI: Không tìm thấy ảnh cho pkg [{pkg}]. Bỏ qua.", "error")
        return None

    queue: list[dict] = []
    for rel in img_paths:
        rel = rel.lstrip("/")
        parts = rel.split("/")
        runtime = parts[1] if len(parts) > 1 else "unknown"
        file_name = parts[-1].replace(".png", f"_{runtime}.jpg")
        queue.append({"url": BASE_URL + rel, "name": file_name, "runtime": runtime, "referer": target_url})
    new_names = [q["name"] for q in queue]

    db_images = store.get_images(model_id)
    if db_images and db_images[0] == new_names[0] and len(db_images) == len(new_names):
        ts = store.touch(model_id)
        log(f"  => ĐÃ CẬP NHẬT: Pkg [{pkg}] đã mới nhất ({len(new_names)} ảnh). Bỏ qua. [{ts} GMT+7]", "success")
        stats["skipped"] += 1
        return None

    prefix = f"{model}_{pkg}_"
    local_files = {p.name for p in SAVE_DIR.glob(f"{prefix}*")} if SAVE_DIR.exists() else set()
    new_set = set(new_names)
    for old in (set(db_images) | local_files) - new_set:
        (SAVE_DIR / old).unlink(missing_ok=True)
        if ftp.enabled:
            ftp.delete(old)
        store.delete_image(old)
        stats["deleted"] += 1
        log(f"    [Xóa] Đã dọn dẹp ảnh cũ: {old}", "system")

    total = len(queue)
    log(f"  => Tải & ghi đè toàn bộ: {total} ảnh với {THREADS} luồng...", "info")
    job = PkgJob(model, pkg, model_id, queue)
    done = 0
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = {pool.submit(download_one, item): item["name"] for item in queue}
        for fut in as_completed(futures):
            name = futures[fut]
            jpg, msg = fut.result()
            done += 1
            if jpg is None:
                stats["fail"] += 1
                log(f"    [{done}/{total}] {msg}", "error")
                continue
            log(f"    [{done}/{total}] {msg}", "success")
            upload_future = uploader.submit(name, jpg)
            job.results[name] = upload_future if upload_future is not None else True
    return job


# -------------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------------
def main() -> int:
    started = time.time()
    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    log("=================================================", "system")
    log(f"BẮT ĐẦU: {now_vn()} (GMT+7) | Models: {', '.join(MODELS)} | Region: {REGION}", "system")

    if DB_HOST:
        store = MySQLStore()
        log(f"Lưu trạng thái: MySQL ({DB_HOST}/{DB_NAME})", "success")
    else:
        store = JsonStore()
        log(f"Lưu trạng thái: {STATE_FILE.name} (chưa cấu hình DB_HOST)", "system")

    ftp = FtpUploader()
    if not ftp.enabled:
        log("Chưa cấu hình FTP -> ảnh chỉ lưu trong thư mục imgtrop/ của runner.", "system")
    if WATERMARK is not None:
        log(f"Đã nhận diện logo {WATERMARK_FILE.name}: đóng dấu góc phải dưới.", "success")
    else:
        log(f"Không tìm thấy {WATERMARK_FILE.name}: ảnh sẽ không được đóng dấu.", "system")

    stats = {"success": 0, "fail": 0, "deleted": 0, "skipped": 0, "pkgs": 0}
    uploader = Uploader(ftp)
    pending: list[PkgJob] = []
    try:
        for model in MODELS:
            log("", "info")
            log(f"########## MODEL: {model.upper()} ##########", "system")
            pkgs = get_pkgs(model, store)
            if PKG_FILTER:
                pkgs = [p for p in pkgs if p["pkg"] in PKG_FILTER]
            log(f"Tìm thấy {len(pkgs)} pkg cho {model}.", "info")

            for p in pkgs:
                stats["pkgs"] += 1
                try:
                    job = sync_pkg(model, p["pkg"], store, ftp, uploader, stats)
                except Exception as exc:
                    job = None
                    stats["fail"] += 1
                    log(f"  => LỖI không mong muốn ở [{model} - {p['pkg']}]: {exc}", "error")
                if job is not None:
                    pending.append(job)
                    if PKG_DELAY > 0:
                        time.sleep(PKG_DELAY)
                flush_jobs(pending, store, stats)

        if pending:
            log(f"Đang chờ đẩy nốt ảnh lên FTP cho {len(pending)} pkg...", "system")
        uploader.shutdown()
        flush_jobs(pending, store, stats, wait=True)
    finally:
        uploader.shutdown()
        store.close()
        ftp.close()

    minutes = (time.time() - started) / 60
    summary = (
        f"Pkg đã xử lý: {stats['pkgs']} | Bỏ qua (đã mới nhất): {stats['skipped']} | "
        f"Ảnh tải mới: {stats['success']} | Ảnh cũ đã xóa: {stats['deleted']} | "
        f"Lỗi: {stats['fail']} | Thời gian: {minutes:.1f} phút"
    )
    log("=================================================", "system")
    log("HOÀN TẤT ĐỒNG BỘ!", "success")
    log(summary, "success" if stats["fail"] == 0 else "error")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Tropical Tidbits Sync - {now_vn()} (GMT+7)\n\n")
            fh.write("| Mục | Giá trị |\n|---|---|\n")
            for k, label in [("pkgs", "Pkg đã xử lý"), ("skipped", "Bỏ qua (mới nhất)"),
                             ("success", "Ảnh tải mới"), ("deleted", "Ảnh cũ đã xóa"), ("fail", "Lỗi")]:
                fh.write(f"| {label} | {stats[k]} |\n")
            fh.write(f"| Thời gian | {minutes:.1f} phút |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
