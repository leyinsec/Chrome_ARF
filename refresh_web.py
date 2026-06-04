#!/usr/bin/env python3
"""
Chrome 定时刷新工具 - Web 管理界面
"""

from flask import Flask, render_template, jsonify, request
import threading
import time
import json
import requests as http_requests
import websocket
import subprocess
import os
import platform
import shutil
import socket

app = Flask(__name__)

# ============ 全局状态 ============
class RefreshState:
    def __init__(self):
        self.running = False
        self.interval = 60
        self.count = 0
        self.current_tab = None
        self.status = "未连接"
        self.chrome_path = None
        self.cdp_port = 9222
        self.cdp_base = "http://127.0.0.1:9222"
        self.debug_profile = os.path.join(
            os.environ.get("LOCALAPPDATA", "") or os.path.expanduser("~"),
            "ChromeRefreshDebug"
        )
        self.logs = []
        self.thread = None
        self._lock = threading.Lock()

    def add_log(self, msg, level="info"):
        with self._lock:
            timestamp = time.strftime("%H:%M:%S")
            self.logs.append({"time": timestamp, "msg": msg, "level": level})
            # 保留最近100条
            if len(self.logs) > 100:
                self.logs = self.logs[-100:]

    def to_dict(self):
        with self._lock:
            return {
                "running": self.running,
                "interval": self.interval,
                "count": self.count,
                "current_tab": self.current_tab,
                "status": self.status,
                "chrome_path": self.chrome_path,
                "cdp_port": self.cdp_port,
                "logs": self.logs[-20:]  # 只返回最近20条
            }

state = RefreshState()


# ============ Chrome 控制 ============
def find_chrome():
    system = platform.system()
    if system == "Windows":
        for env in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
            p = os.path.join(os.environ.get(env, ""), "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(p):
                return p
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
            return winreg.QueryValue(key, "")
        except:
            pass
    elif system == "Darwin":
        p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(p):
            return p
    else:
        for cmd in ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]:
            p = shutil.which(cmd)
            if p:
                return p
    return None


def chrome_ready():
    try:
        return http_requests.get(f"{state.cdp_base}/json/version", timeout=2).status_code == 200
    except:
        return False


def is_port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) != 0


def launch_chrome():
    if chrome_ready():
        state.status = "已连接"
        state.add_log("Chrome 调试实例已在运行")
        return True

    chrome = find_chrome()
    if not chrome:
        state.status = "未找到Chrome"
        state.add_log("未找到 Chrome 浏览器", "error")
        return False

    state.chrome_path = chrome

    # 寻找可用端口
    if not is_port_free(state.cdp_port):
        for port in range(9223, 9233):
            if is_port_free(port):
                state.cdp_port = port
                state.cdp_base = f"http://127.0.0.1:{port}"
                break

    # 复制书签
    if not os.path.exists(state.debug_profile):
        src = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "Default")
        if os.path.exists(src):
            dst = os.path.join(state.debug_profile, "Default")
            os.makedirs(dst, exist_ok=True)
            for f in ["Bookmarks", "Bookmarks.bak"]:
                fp = os.path.join(src, f)
                if os.path.exists(fp):
                    shutil.copy2(fp, os.path.join(dst, f))

    state.add_log("正在启动 Chrome...")
    subprocess.Popen([chrome, f"--remote-debugging-port={state.cdp_port}",
                      f"--user-data-dir={state.debug_profile}", "--remote-allow-origins=*",
                      "--no-first-run", "--no-default-browser-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        time.sleep(1)
        if chrome_ready():
            state.status = "已连接"
            state.add_log(f"Chrome 已就绪 (端口: {state.cdp_port})")
            return True

    state.status = "启动超时"
    state.add_log("Chrome 启动超时", "error")
    return False


def get_active_tab():
    try:
        tabs = [t for t in http_requests.get(f"{state.cdp_base}/json", timeout=3).json()
                if t.get("type") == "page"]
        if not tabs:
            return None

        for tab in tabs:
            ws_url = tab.get("webSocketDebuggerUrl", "")
            if not ws_url:
                continue
            try:
                ws = websocket.create_connection(ws_url, timeout=2)
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                    "params": {"expression": "document.hasFocus()"}}))
                result = json.loads(ws.recv())
                ws.close()
                if result.get("result", {}).get("result", {}).get("value"):
                    return tab
            except:
                continue
        return max(tabs, key=lambda t: t.get("lastSeenTime", 0))
    except:
        return None


def reload_tab(ws_url):
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
        ws.send(json.dumps({"id": 1, "method": "Page.reload"}))
        ws.recv()
        ws.close()
        return True
    except:
        return False


def refresh_loop():
    """刷新主循环"""
    prev_title = ""
    state.add_log(f"开始刷新 (间隔: {state.interval}秒)")

    while state.running:
        tab = get_active_tab()
        if tab:
            ws_url = tab.get("webSocketDebuggerUrl", "")
            title = tab.get("title", "未知")[:50]
            url = tab.get("url", "")

            if ws_url and reload_tab(ws_url):
                if title != prev_title:
                    state.count = 0
                    prev_title = title
                state.count += 1
                state.current_tab = {"title": title, "url": url, "count": state.count}
                state.add_log(f"刷新: {title} (第{state.count}次)")
            else:
                state.add_log("刷新失败", "warning")
        else:
            state.add_log("未找到活跃标签页", "warning")

        # 等待间隔，但要检查是否停止
        for _ in range(state.interval * 10):
            if not state.running:
                break
            time.sleep(0.1)

    state.add_log("已停止刷新")
    state.status = "已停止"


# ============ 路由 ============
@app.route("/")
def index():
    return render_template("refresh.html")


@app.route("/api/status")
def api_status():
    # 更新连接状态
    if chrome_ready():
        state.status = "已连接"
    return jsonify(state.to_dict())


@app.route("/api/launch", methods=["POST"])
def api_launch():
    success = launch_chrome()
    return jsonify({"success": success, "status": state.status})


@app.route("/api/start", methods=["POST"])
def api_start():
    if state.running:
        return jsonify({"success": False, "msg": "已在运行"})

    data = request.json or {}
    state.interval = max(5, min(3600, data.get("interval", 60)))

    if not chrome_ready():
        success = launch_chrome()
        if not success:
            return jsonify({"success": False, "msg": "Chrome 启动失败"})

    state.running = True
    state.thread = threading.Thread(target=refresh_loop, daemon=True)
    state.thread.start()

    return jsonify({"success": True, "msg": "已开始刷新"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state.running = False
    return jsonify({"success": True, "msg": "已停止刷新"})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    state.count = 0
    state.current_tab = None
    state.logs = []
    return jsonify({"success": True})


@app.route("/api/open-browser", methods=["POST"])
def api_open_browser():
    """打开普通Chrome浏览器窗口"""
    data = request.json or {}
    url = data.get("url", "")

    chrome = find_chrome()
    if not chrome:
        return jsonify({"success": False, "msg": "未找到 Chrome"})

    try:
        args = [chrome]
        if url:
            args.append(url)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state.add_log(f"已打开浏览器" + (f": {url}" if url else ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})


if __name__ == "__main__":
    print("=" * 50)
    print("  Chrome 定时刷新工具 - Web 管理界面")
    print("  访问: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
