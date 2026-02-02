import os
import re
import shutil
import subprocess
import threading
import queue
import requests
import argparse
import browser_cookie3
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
DEFAULT_COOKIE = "SESSDATA=33df90d2%2C1785494051%2C1b9e1*21CjBRoHd87qx54owtbhOPXvnViMnGiFP012pcPlQrIEM5OgKvgfub6VtaDEVcK2EP5AsSVm1LZWZhcW43QkhSYWhWQi1sSFF3cVRzWmk1U1hobXQzcVhjU1ZfckIxaEh6RG9CeDJXQWpQYTd4ZmVIbmpoVUVQWFRMRW5NSjM0czcyVnJOMHJabWF3IIEC; bili_jct=628da9944a030d4c6e2351dbfdc88c38; DedeUserID=439048405"
import sys
import os

def get_ffmpeg_path():
    """
    优先使用打包进 app 的 ffmpeg，其次使用系统 ffmpeg
    """
    # PyInstaller 打包后临时目录
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # PyInstaller 解包路径
        ff = os.path.join(base, "ffmpeg")
        if os.path.exists(ff):
            return ff

    # 开发环境：使用项目 bin/ffmpeg
    local_ff = os.path.join(os.path.dirname(__file__), "bin", "ffmpeg")
    if os.path.exists(local_ff):
        return local_ff

    # 最后兜底：系统 PATH
    return "ffmpeg"

# ================== 工具函数 ==================
def safe_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", name).strip()


def find_bvid(url: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        raise ValueError("无法从链接中解析 BV 号，请检查链接是否正确。")
    return m.group(1)


def run_cmd(cmd: list[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"命令执行失败：{' '.join(cmd)}\n\nstderr:\n{p.stderr}")
    return p.stdout


# ================== B站 API ==================
def get_video_info(bvid: str, headers=None):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取视频信息失败：{data}")
    return data["data"]


def get_playurl(bvid: str, cid: int, headers=None):
    url = "https://api.bilibili.com/x/player/playurl"
    params = {
        "bvid": bvid,
        "cid": cid,
        "fnval": 4048,  # DASH
        "fourk": 1,     # 尝试 4K
    }
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取播放地址失败：{data}")
    return data["data"]


# ================== 选择最高清晰度 ==================
def pick_best_stream(dash: dict):
    videos = dash.get("video", [])
    audios = dash.get("audio", [])

    if not videos or not audios:
        raise RuntimeError("未获取到 DASH 音视频流，可能需要登录Cookie或该视频限制访问。")

    # 视频选带宽最高（通常就是最高清晰度）
    videos = sorted(videos, key=lambda x: x.get("bandwidth", 0), reverse=True)
    video_url = videos[0]["baseUrl"]

    # 音频选带宽最高
    audios = sorted(audios, key=lambda x: x.get("bandwidth", 0), reverse=True)
    audio_url = audios[0]["baseUrl"]

    return video_url, audio_url


# ================== 下载（带进度回调） ==================
def download_file(url: str, save_path: str, headers=None, progress_cb=None, stage=""):
    headers = headers or {}
    with requests.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        downloaded = 0
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if progress_cb and total > 0:
                    percent = downloaded / total * 100
                    progress_cb(percent, stage)


# ================== ffmpeg 合并 ==================
def merge_with_ffmpeg(video_path: str, audio_path: str, out_path: str):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = [get_ffmpeg_path(), "-y", "-i", video_path, "-i", audio_path, "-c", "copy", out_path]

    # cmd = ["ffmpeg", "-y", "-i", video_path, "-i", audio_path, "-c", "copy", out_path]
    run_cmd(cmd)


# ================== Cookie ==================
def get_browser_cookie() -> str:
    """
    自动获取 B站登录 Cookie（支持 Chrome, Edge, Firefox, Safari）
    """
    try:
        cj = browser_cookie3.bilibili()
        cookie_str = "; ".join([f"{c.name}={c.value}" for c in cj])
        return cookie_str
    except Exception:
        return None


def normalize_cookie(cookie: str | None) -> str | None:
    """
    清洗 cookie：去掉换行/回车/多余空格
    """
    if not cookie:
        return None
    cookie = cookie.replace("\n", "").replace("\r", "")
    cookie = re.sub(r"\s+", " ", cookie).strip()
    return cookie or None


# ================== 核心下载函数 ==================
def download_bilibili(url: str, out_dir: str = "./downloads", cookie: str = None, ui_cb=None):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
    }

    # 1) 优先用传入 cookie
    cookie = normalize_cookie(cookie)

    # 2) 没传就用写死的 DEFAULT_COOKIE
    if not cookie:
        cookie = normalize_cookie(DEFAULT_COOKIE)

    # 3) 仍然没有才尝试读浏览器
    if not cookie:
        cookie = normalize_cookie(get_browser_cookie())

    if cookie:
        headers["Cookie"] = cookie
    else:
        raise RuntimeError("没有可用 Cookie（写死Cookie为空且自动读取失败）")

    bvid = find_bvid(url)
    info = get_video_info(bvid, headers=headers)
    title = safe_filename(info.get("title", bvid))
    cid = info["cid"]

    if ui_cb:
        ui_cb(0, f"解析完成：{title}")

    playdata = get_playurl(bvid, cid, headers=headers)
    dash = playdata.get("dash")
    if not dash:
        raise RuntimeError("该视频未返回 dash 数据，可能为特殊类型视频/权限限制。")

    video_url, audio_url = pick_best_stream(dash)

    tmp_dir = os.path.join(out_dir, ".tmp", bvid)
    os.makedirs(tmp_dir, exist_ok=True)

    video_file = os.path.join(tmp_dir, "video.m4s")
    audio_file = os.path.join(tmp_dir, "audio.m4s")

    if ui_cb:
        ui_cb(0, "开始下载视频流...")
    download_file(video_url, video_file, headers=headers, progress_cb=ui_cb, stage="下载视频")

    if ui_cb:
        ui_cb(0, "开始下载音频流...")
    download_file(audio_url, audio_file, headers=headers, progress_cb=ui_cb, stage="下载音频")

    out_path = os.path.join(out_dir, f"{title}.mp4")

    if ui_cb:
        ui_cb(0, "ffmpeg 合并中...")
    merge_with_ffmpeg(video_file, audio_file, out_path)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if ui_cb:
        ui_cb(100, f"完成！已保存：{out_path}")

    return out_path

# def download_bilibili(url: str, out_dir: str = "./downloads", cookie: str = None, ui_cb=None):
#     headers = {
#         "User-Agent": "Mozilla/5.0",
#         "Referer": "https://www.bilibili.com/",
#     }
#
#     cookie = normalize_cookie(cookie)
#     if not cookie:
#         cookie = get_browser_cookie()
#         cookie = normalize_cookie(cookie)
#
#     if cookie:
#         headers["Cookie"] = cookie
#
#     bvid = find_bvid(url)
#     info = get_video_info(bvid, headers=headers)
#     title = safe_filename(info.get("title", bvid))
#     cid = info["cid"]
#
#     if ui_cb:
#         ui_cb(0, f"解析完成：{title}")
#
#     playdata = get_playurl(bvid, cid, headers=headers)
#     dash = playdata.get("dash")
#     if not dash:
#         raise RuntimeError("该视频未返回 dash 数据，可能为特殊类型视频/权限限制。")
#
#     video_url, audio_url = pick_best_stream(dash)
#
#     tmp_dir = os.path.join(out_dir, ".tmp", bvid)
#     os.makedirs(tmp_dir, exist_ok=True)
#
#     video_file = os.path.join(tmp_dir, "video.m4s")
#     audio_file = os.path.join(tmp_dir, "audio.m4s")
#
#     # 下载视频
#     if ui_cb:
#         ui_cb(0, "开始下载视频流...")
#     download_file(video_url, video_file, headers=headers,
#                   progress_cb=ui_cb, stage="下载视频")
#
#     # 下载音频
#     if ui_cb:
#         ui_cb(0, "开始下载音频流...")
#     download_file(audio_url, audio_file, headers=headers,
#                   progress_cb=ui_cb, stage="下载音频")
#
#     out_path = os.path.join(out_dir, f"{title}.mp4")
#
#     if ui_cb:
#         ui_cb(0, "ffmpeg 合并中...")
#     merge_with_ffmpeg(video_file, audio_file, out_path)
#
#     shutil.rmtree(tmp_dir, ignore_errors=True)
#
#     if ui_cb:
#         ui_cb(100, f"完成！已保存：{out_path}")
#
#     return out_path


# ================== UI 界面 ==================
class BiliDownloaderUI:
    def __init__(self, root):
        self.root = root
        root.title("B站视频下载器（最高画质）")
        root.geometry("720x520")

        self.msg_queue = queue.Queue()

        # URL
        ttk.Label(root, text="视频链接：").pack(anchor="w", padx=12, pady=(12, 0))
        self.url_entry = ttk.Entry(root)
        self.url_entry.pack(fill="x", padx=12, pady=6)
        self.url_entry.insert(0, "https://www.bilibili.com/video/BVxxxxxx")

        # 输出目录
        frame_out = ttk.Frame(root)
        frame_out.pack(fill="x", padx=12, pady=6)
        ttk.Label(frame_out, text="输出目录：").pack(side="left")
        self.out_entry = ttk.Entry(frame_out)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.out_entry.insert(0, os.path.abspath("./downloads"))
        ttk.Button(frame_out, text="选择", command=self.choose_out_dir).pack(side="left")

        # Cookie
        ttk.Label(root, text="Cookie（可选，自动读取失败时再填；支持粘贴整段，会自动清洗换行）：").pack(anchor="w", padx=12, pady=(12, 0))
        self.cookie_text = tk.Text(root, height=6)
        self.cookie_text.pack(fill="both", padx=12, pady=6)

        # 进度条
        self.progress = ttk.Progressbar(root, length=400, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(10, 0))

        self.stage_label = ttk.Label(root, text="状态：等待开始")
        self.stage_label.pack(anchor="w", padx=12, pady=6)

        # 日志
        ttk.Label(root, text="日志：").pack(anchor="w", padx=12, pady=(8, 0))
        self.log_text = tk.Text(root, height=10)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=6)

        # 按钮
        frame_btn = ttk.Frame(root)
        frame_btn.pack(fill="x", padx=12, pady=10)
        self.start_btn = ttk.Button(frame_btn, text="开始下载", command=self.start_download)
        self.start_btn.pack(side="left")
        ttk.Button(frame_btn, text="清空日志", command=self.clear_log).pack(side="left", padx=8)

        # 定时刷新 UI
        self.root.after(100, self.process_queue)

    def choose_out_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.out_entry.delete(0, tk.END)
            self.out_entry.insert(0, path)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def log(self, msg: str):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def ui_callback(self, percent, stage):
        # 子线程 -> queue -> 主线程更新 UI
        self.msg_queue.put(("progress", percent, stage))

    def process_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                if item[0] == "progress":
                    _, percent, stage = item
                    self.progress["value"] = percent
                    self.stage_label.config(text=f"状态：{stage}（{percent:.1f}%）")
                    if isinstance(stage, str):
                        self.log(f"{stage} - {percent:.1f}%")
                elif item[0] == "log":
                    self.log(item[1])
                elif item[0] == "done":
                    self.start_btn.config(state=tk.NORMAL)
                    messagebox.showinfo("完成", item[1])
                elif item[0] == "error":
                    self.start_btn.config(state=tk.NORMAL)
                    messagebox.showerror("失败", item[1])
        except queue.Empty:
            pass

        self.root.after(100, self.process_queue)

    def start_download(self):
        url = self.url_entry.get().strip()
        out_dir = self.out_entry.get().strip()

        cookie = self.cookie_text.get("1.0", tk.END)
        cookie = normalize_cookie(cookie)

        if not url or "bilibili.com" not in url:
            messagebox.showwarning("提示", "请输入正确的 B站视频链接")
            return

        self.start_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0
        self.stage_label.config(text="状态：准备开始...")
        self.log("开始下载：" + url)

        def worker():
            try:
                out_path = download_bilibili(url, out_dir, cookie, ui_cb=self.ui_callback)
                self.msg_queue.put(("done", f"下载完成！\n{out_path}"))
            except Exception as e:
                self.msg_queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()


# ================== CLI / UI 入口 ==================
def main_cli():
    parser = argparse.ArgumentParser(description="B站视频下载（最高画质 + ffmpeg合并 + Cookie支持）")
    parser.add_argument("url", nargs="?", help="B站视频网页链接，如 https://www.bilibili.com/video/BVxxxxxx")
    parser.add_argument("-o", "--out", default="./downloads", help="输出目录")
    parser.add_argument("--cookie", type=str, default=None, help="B站登录 Cookie（可选）")
    parser.add_argument("--ui", action="store_true", help="启动图形界面")
    args = parser.parse_args()

    if args.ui or not args.url:
        root = tk.Tk()
        app = BiliDownloaderUI(root)
        root.mainloop()
    else:
        out = download_bilibili(args.url, args.out, args.cookie, ui_cb=None)
        print("✅ 完成：", out)


if __name__ == "__main__":
    main_cli()
