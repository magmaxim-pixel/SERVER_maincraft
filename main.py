# minecraft_server_launcher.py
import os
import sys
import json
import time
import socket
import shutil
import platform
import subprocess
import threading
import webbrowser
import zipfile
import tarfile
import stat
from pathlib import Path
from datetime import datetime
from collections import deque

def install_dependencies():
    """Автоматическая установка системных зависимостей Python"""
    required_packages = ['flask', 'requests']
    for package in required_packages:
        try:
            __import__(package.replace('-', '_'))
        except ImportError:
            print(f"[System] Установка пакета {package}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])
            except Exception as e:
                print(f"✗ Ошибка установки {package}: {e}")
                sys.exit(1)

install_dependencies()

from flask import Flask, render_template_string, jsonify, request
import requests

class HeadlessMinecraftServer:
    def __init__(self):
        # ГЛАВНОЕ ИЗМЕНЕНИЕ: Жесткий путь до папки на диске D
        self.server_dir = Path(r"D:\SErVER")
        self.runtime_dir = self.server_dir / "runtime"
        self.plugins_dir = self.server_dir / "plugins"
        self.logs_dir = self.server_dir / "logs"
        self.saved_worlds_dir = self.server_dir / "saved_worlds"
        self.backups_dir = self.server_dir / "backups"
        self.config_file = self.server_dir / "launcher_config.json"
        
        for folder in [self.server_dir, self.runtime_dir, self.plugins_dir, self.logs_dir, self.saved_worlds_dir, self.backups_dir]:
            folder.mkdir(exist_ok=True, parents=True)
            
        self.process = None
        self.is_running = False
        self.start_time = None
        self.log_buffer = deque(maxlen=300)
        self.log(f" [System] REST API инициализирован. Путь: {self.server_dir}")
        
        self.config = self.load_config()
        self.current_world = self.config.get("active_world", "Основной_мир")
        
        self.java_path = None
        self.local_ip = self.get_local_ip()
        self.public_ip = self.get_public_ip()
        
        self.web_app = Flask(__name__)
        self.setup_routes()

    def log(self, text):
        time_str = datetime.now().strftime('%H:%M:%S')
        msg = f"[{time_str}] {text}"
        print(msg)
        self.log_buffer.append(msg + "\n")

    def load_config(self):
        default_config = {
            "active_world": "Основной_мир",
            "version": "1.20.4",
            "port": 25565,
            "max_players": 25,
            "ram_max": "2G",
            "ram_min": "1G",
            "motd": "§b§lREST API §f| §aАвтономный сервер TLauncher"
        }
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return {**default_config, **json.load(f)}
            except Exception:
                pass
        return default_config

    def save_config(self, new_data=None):
        if new_data:
            self.config.update(new_data)
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            self.log(f"⚠ Ошибка сохранения конфига: {e}")
            return False

    def get_local_ip(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def get_public_ip(self):
        try:
            return requests.get('https://api.ipify.org', timeout=3).text
        except Exception:
            return "Не определен"

    # ==========================================
    # 1. УПРАВЛЕНИЕ МИРАМИ И БЭКАПАМИ
    # ==========================================
    def get_available_worlds(self):
        worlds = set([self.current_world])
        if self.saved_worlds_dir.exists():
            for item in self.saved_worlds_dir.iterdir():
                if item.is_dir(): worlds.add(item.name)
        return sorted(list(worlds))

    def save_current_world_files(self):
        self.log(f"💾 Упаковка мира '{self.current_world}' в хранилище...")
        target_dir = self.saved_worlds_dir / self.current_world
        target_dir.mkdir(exist_ok=True, parents=True)
        for dim in ["world", "world_nether", "world_the_end"]:
            src = self.server_dir / dim
            dst = target_dir / dim
            if src.exists():
                if dst.exists(): shutil.rmtree(dst)
                shutil.move(str(src), str(dst))

    def load_world_files(self, world_name):
        self.log(f"🗺️ Распаковка мира '{world_name}'...")
        source_dir = self.saved_worlds_dir / world_name
        for dim in ["world", "world_nether", "world_the_end"]:
            dst = self.server_dir / dim
            if dst.exists(): shutil.rmtree(dst)
        if source_dir.exists():
            for dim in ["world", "world_nether", "world_the_end"]:
                src = source_dir / dim
                dst = self.server_dir / dim
                if src.exists(): shutil.move(str(src), str(dst))
        else:
            self.log(f"✨ Создается абсолютно чистая карта для мира '{world_name}'.")

    def switch_world(self, new_world_name):
        new_world_name = "".join(c for c in new_world_name if c.isalnum() or c in (" ", "_", "-")).strip()
        if not new_world_name or new_world_name == self.current_world:
            return False, "Некорректное имя или мир уже активен!"

        was_running = self.is_running
        if was_running:
            self.log("⚠ Остановка сервера для безопасной смены карты...")
            self.stop_server()
            time.sleep(2)

        self.save_current_world_files()
        self.current_world = new_world_name
        self.save_config({"active_world": new_world_name})
        self.load_world_files(new_world_name)

        if was_running:
            threading.Thread(target=self.auto_start_all, daemon=True).start()

        return True, f"Мир успешно переключен на '{new_world_name}'!"

    def delete_world(self, world_name):
        if world_name == self.current_world:
            return False, "Нельзя удалить активный мир!"
        target_dir = self.saved_worlds_dir / world_name
        if target_dir.exists():
            shutil.rmtree(target_dir)
            return True, f"Мир '{world_name}' навсегда удален."
        return False, "Мир не найден."

    def backup_current_world(self):
        if self.is_running:
            self.send_command("save-all")
            time.sleep(1)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        zip_name = self.backups_dir / f"{self.current_world}_{timestamp}.zip"
        self.log(f"📦 Генерация ZIP-архива для '{self.current_world}'...")
        try:
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for dim in ["world", "world_nether", "world_the_end"]:
                    dim_dir = self.server_dir / dim
                    if dim_dir.exists():
                        for root, _, files in os.walk(dim_dir):
                            for file in files:
                                file_path = Path(root) / file
                                zipf.write(file_path, file_path.relative_to(self.server_dir))
            self.log(f"✅ Бэкап сохранен: {zip_name.name}")
            return True, zip_name.name
        except Exception as e:
            self.log(f"❌ Ошибка бэкапа: {e}")
            return False, str(e)

    # ==========================================
    # 2. РАБОТА С КОНФИГАМИ (SERVER.PROPERTIES)
    # ==========================================
    def get_server_properties(self):
        props_file = self.server_dir / "server.properties"
        props = {}
        if props_file.exists():
            with open(props_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        props[k.strip()] = v.strip()
        return props

    def save_server_properties(self, props_dict):
        props_file = self.server_dir / "server.properties"
        try:
            with open(props_file, 'w', encoding='utf-8') as f:
                f.write("#Minecraft server properties\n")
                for k, v in props_dict.items():
                    f.write(f"{k}={v}\n")
            return True
        except Exception as e:
            self.log(f"❌ Ошибка записи server.properties: {e}")
            return False

    # ==========================================
    # 3. АВТО-УСТАНОВКА КОМПОНЕНТОВ
    # ==========================================
    def ensure_java(self):
        for root, _, files in os.walk(self.runtime_dir):
            for file in files:
                if file in ['java.exe', 'java']:
                    self.java_path = str(Path(root) / file)
                    return True
        try:
            if subprocess.run(['java', '-version'], capture_output=True).returncode == 0:
                self.java_path = 'java'
                return True
        except Exception: pass

        self.log("⚠ Java не найдена! Загрузка Portable JRE 21...")
        os_name = "windows" if os.name == 'nt' else "linux"
        arch = "x64" if platform.machine().lower() in ['x86_64', 'amd64'] else "aarch64"
        ext = "zip" if os_name == "windows" else "tar.gz"
        api_url = f"https://api.adoptium.net/v3/binary/latest/21/ga/{os_name}/{arch}/jre/hotspot/normal/eclipse"
        archive_path = self.runtime_dir / f"java.{ext}"
        try:
            with requests.get(api_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(archive_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        if chunk: f.write(chunk)
            if ext == "zip":
                with zipfile.ZipFile(archive_path, 'r') as z: z.extractall(self.runtime_dir)
            else:
                with tarfile.open(archive_path, 'r:gz') as t: t.extractall(self.runtime_dir)
            archive_path.unlink()
            for root, _, files in os.walk(self.runtime_dir):
                for file in files:
                    if file in ['java.exe', 'java']:
                        bin_path = str(Path(root) / file)
                        if os_name == "linux": os.chmod(bin_path, os.stat(bin_path).st_mode | stat.S_IEXEC)
                        self.java_path = bin_path
                        return True
        except Exception as e: self.log(f"❌ Ошибка загрузки Java: {e}")
        return False

    def ensure_server_jar(self):
        jar_path = self.server_dir / "server.jar"
        if jar_path.exists() and os.path.getsize(jar_path) > 10 * 1024 * 1024: return True
        version = self.config["version"]
        self.log(f"📥 Скачивание PaperMC {version} в {jar_path}...")
        try:
            api_url = f"https://api.papermc.io/v2/projects/paper/versions/{version}"
            res = requests.get(api_url, timeout=10).json()
            latest_build = res["builds"][-1]
            dl_url = f"{api_url}/builds/{latest_build}/downloads/paper-{version}-{latest_build}.jar"
            with requests.get(dl_url, stream=True, timeout=30) as r:
                with open(jar_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        if chunk: f.write(chunk)
            return True
        except Exception as e: self.log(f"❌ Ошибка ядра: {e}"); return False

    def ensure_essential_plugins(self):
        for slug in ["viaversion", "skinrestorer"]:
            if list(self.plugins_dir.glob(f"*{slug}*.jar")) or list(self.plugins_dir.glob(f"*{slug.title()}*.jar")): continue
            try:
                res = requests.get(f"https://api.modrinth.com/v2/project/{slug}/version", timeout=10).json()
                if res and res[0].get("files"):
                    with requests.get(res[0]["files"][0]["url"], stream=True, timeout=20) as r:
                        with open(self.plugins_dir / res[0]["files"][0]["filename"], 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk: f.write(chunk)
            except Exception: pass

    # ==========================================
    # 4. УПРАВЛЕНИЕ ПРОЦЕССОМ СЕРВЕРА
    # ==========================================
    def auto_start_all(self):
        if self.is_running: return
        self.log(f"🚀 ЗАПУСК СЕРВЕРА [Мир: {self.current_world}]...")
        if not self.ensure_java() or not self.ensure_server_jar(): return
        self.ensure_essential_plugins()
        
        if not (self.server_dir / "eula.txt").exists():
            with open(self.server_dir / "eula.txt", 'w') as f: f.write("eula=true\n")
        if not (self.server_dir / "server.properties").exists():
            self.save_server_properties({
                "motd": self.config["motd"], "server-port": self.config["port"],
                "max-players": self.config["max_players"], "online-mode": "false",
                "gamemode": "survival", "difficulty": "normal", "pvp": "true"
            })
        
        # Передаем полный путь до D:\SErVER\server.jar для надежности
        jar_full_path = str(self.server_dir / "server.jar")
        cmd = [
            self.java_path, f'-Xmx{self.config["ram_max"]}', f'-Xms{self.config["ram_min"]}',
            '-XX:+UseG1GC', '-XX:+ParallelRefProcEnabled', '-XX:MaxGCPauseMillis=200',
            '-jar', jar_full_path, 'nogui'
        ]
        try:
            self.process = subprocess.Popen(
                cmd, cwd=self.server_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, text=True, bufsize=1, encoding='utf-8', errors='replace'
            )
            self.is_running = True
            self.start_time = datetime.now()
            threading.Thread(target=self.read_output, daemon=True).start()
        except Exception as e:
            self.log(f"❌ Ошибка старта: {e}")
            self.is_running = False

    def read_output(self):
        with open(self.logs_dir / "console.log", 'a', encoding='utf-8') as f:
            while self.is_running and self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None: break
                if line:
                    clean = line.rstrip()
                    print(f"[Server] {clean}")
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {clean}\n")
                    f.flush()
                    self.log_buffer.append(clean + "\n")
        self.is_running = False
        self.start_time = None
        self.log("🛑 Сервер остановлен.")

    def send_command(self, cmd):
        if self.is_running and self.process and self.process.stdin:
            self.process.stdin.write(cmd + "\n")
            self.process.stdin.flush()
            self.log(f"👉 Команда: {cmd}")
            return True
        return False

    def stop_server(self):
        if not self.is_running: return False
        self.log("🛑 Сохранение и остановка...")
        self.send_command("stop")
        for _ in range(20):
            if not self.is_running or self.process.poll() is not None: break
            time.sleep(1)
        if self.process and self.process.poll() is None: self.process.terminate()
        self.is_running = False
        self.save_current_world_files()
        self.load_world_files(self.current_world)
        return True

    def kill_server(self):
        """Аварийное завершение процесса (Force Kill)"""
        if self.process and self.process.poll() is None:
            self.log("⚡ АВАРИЙНОЕ УБИЙСТВО ПРОЦЕССА!")
            self.process.kill()
            self.is_running = False
            return True
        return False

    # ==========================================
    # 5. REST API ЭНДПОИНТЫ И РОУТЫ
    # ==========================================
    def setup_routes(self):
        @self.web_app.route('/')
        def index():
            html = '''
            <!DOCTYPE html>
            <html lang="ru">
            <head>
                <meta charset="UTF-8">
                <title>Minecraft REST API Server</title>
                <style>
                    :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --accent: #3b82f6; --green: #22c55e; --red: #ef4444; }
                    body { font-family: 'Segoe UI', Tahoma, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
                    .container { max-width: 1100px; margin: 0 auto; }
                    .header { display: flex; justify-content: space-between; align-items: center; background: var(--card); padding: 20px; border-radius: 12px; border: 1px solid var(--border); margin-bottom: 20px; }
                    .status-pill { padding: 8px 16px; border-radius: 50px; font-weight: bold; background: {{ '#166534' if status else '#991b1b' }}; color: white; display: flex; align-items: center; gap: 8px; }
                    .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
                    .tab-btn { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.2s; }
                    .tab-btn.active { background: var(--accent); border-color: var(--accent); }
                    .tab-content { display: none; }
                    .tab-content.active { display: block; }
                    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
                    .card { background: var(--card); padding: 20px; border-radius: 12px; border: 1px solid var(--border); margin-bottom: 20px; }
                    .btn { width: 100%; padding: 12px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; margin-bottom: 10px; transition: 0.2s; font-size: 0.95em; }
                    .btn-green { background: var(--green); } .btn-green:hover { background: #15803d; }
                    .btn-red { background: var(--red); } .btn-red:hover { background: #b91c1c; }
                    .btn-blue { background: var(--accent); } .btn-blue:hover { background: #2563eb; }
                    .btn-kill { background: #7f1d1d; border: 1px dashed var(--red); }
                    input, select { width: calc(100% - 24px); padding: 10px; border-radius: 6px; border: 1px solid var(--border); background: #0b0f19; color: white; margin-bottom: 10px; }
                    pre.console { background: #090d16; color: #38bdf8; padding: 15px; border-radius: 8px; height: 350px; overflow-y: auto; border: 1px solid var(--border); font-family: 'Consolas', monospace; white-space: pre-wrap; margin: 0; }
                    .prop-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
                    .prop-item { display: flex; flex-direction: column; }
                    .prop-item label { font-size: 0.8em; color: #94a3b8; margin-bottom: 4px; }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <div>
                            <h1 style="margin:0; font-size:1.5em;">🎮 Minecraft TLauncher Server</h1>
                            <small style="color:#94a3b8;">REST API &bull; Папка: <strong style="color:#38bdf8;">D:\\SErVER</strong> &bull; Мир: <strong style="color:#facc15;">{{ current_world }}</strong></small>
                        </div>
                        <div class="status-pill">
                            <span style="height:10px; width:10px; border-radius:50%; background:{{ '#22c55e' if status else '#ef4444' }};"></span>
                            {{ 'ОНЛАЙН' if status else 'ОСТАНОВЛЕН' }}
                        </div>
                    </div>

                    <div class="tabs">
                        <button class="tab-btn active" onclick="showTab('tab-dash', this)">🚀 Дашборд и Консоль</button>
                        <button class="tab-btn" onclick="showTab('tab-worlds', this)">🌍 Миры и Бэкапы</button>
                        <button class="tab-btn" onclick="showTab('tab-config', this)">⚙ Настройки (Properties)</button>
                    </div>

                    <!-- Вклака 1: Дашборд -->
                    <div id="tab-dash" class="tab-content active">
                        <div class="grid">
                            <div class="card">
                                <h3>⚡ Управление питанием</h3>
                                <button class="btn btn-green" onclick="apiPost('/api/power/start')" {{ 'disabled' if status else '' }}>▶ Запустить сервер</button>
                                <button class="btn btn-red" onclick="apiPost('/api/power/stop')" {{ '' if status else 'disabled' }}>⏹ Остановить сервер</button>
                                <button class="btn btn-kill" onclick="if(confirm('Убить процесс? Используйте только при зависании!')) apiPost('/api/power/kill')" {{ '' if status else 'disabled' }}>💀 Аварийный Kill (Если завис)</button>
                                
                                <h3>📡 Адреса для подключения</h3>
                                <p style="margin:5px 0; font-size:0.9em;">Локально (Wi-Fi): <code style="color:#38bdf8; background:#0b0f19; padding:4px 8px; border-radius:4px;">{{ local_ip }}:{{ port }}</code></p>
                                <p style="margin:5px 0; font-size:0.9em;">Интернет: <code style="color:#38bdf8; background:#0b0f19; padding:4px 8px; border-radius:4px;">{{ public_ip }}:{{ port }}</code></p>
                            </div>
                            
                            <div class="card">
                                <h3>👥 Игроки и Быстрые команды</h3>
                                <div style="display:flex; gap:10px;">
                                    <input type="text" id="playerInput" placeholder="Ник игрока..." style="margin:0;">
                                    <button class="btn btn-blue" style="width:40%; margin:0;" onclick="playerAction('op')">👑 Выдать OP</button>
                                    <button class="btn btn-red" style="width:40%; margin:0;" onclick="playerAction('kick')">👢 Кикнуть</button>
                                </div>
                                <div style="display:flex; gap:10px; margin-top:15px;">
                                    <input type="text" id="cmdInput" placeholder="Команда без '/' (напр: time set day)..." style="margin:0;" onkeypress="if(event.key==='Enter') sendCmd()">
                                    <button class="btn btn-blue" style="width:30%; margin:0;" onclick="sendCmd()">Отправить</button>
                                </div>
                            </div>
                        </div>
                        <div class="card" style="padding:10px;">
                            <pre class="console" id="consoleLogs">Загрузка консоли...</pre>
                        </div>
                    </div>

                    <!-- Владка 2: Миры -->
                    <div id="tab-worlds" class="tab-content">
                        <div class="grid">
                            <div class="card">
                                <h3>🗺️ Менеджер миров</h3>
                                <label>Переключить активный мир:</label>
                                <div style="display:flex; gap:10px;">
                                    <select id="worldSelect" style="margin:0;">
                                        {% for w in worlds %}
                                        <option value="{{ w }}" {{ 'selected' if w == current_world else '' }}>{{ w }}</option>
                                        {% endfor %}
                                    </select>
                                    <button class="btn btn-blue" style="width:40%; margin:0;" onclick="switchWorld()">Загрузить</button>
                                </div>
                                <hr style="border-color:var(--border); margin:20px 0;">
                                <label>Создать новую карту:</label>
                                <div style="display:flex; gap:10px;">
                                    <input type="text" id="newWorldName" placeholder="Имя (напр: Skyblock)..." style="margin:0;">
                                    <button class="btn btn-green" style="width:40%; margin:0;" onclick="createWorld()">➕ Создать</button>
                                </div>
                            </div>
                            
                            <div class="card">
                                <h3>💾 Резервные копии (Бэкапы)</h3>
                                <button class="btn btn-blue" onclick="createBackup()">📦 Сделать ZIP-бэкап текущего мира</button>
                                <p style="font-size:0.85em; color:#94a3b8;">Архивы сохраняются в папку <code>D:\\SErVER\\backups\\</code>. Резервное копирование происходит в фоновом режиме без остановки сервера.</p>
                                <ul id="backupList" style="color:#38bdf8; font-size:0.9em; padding-left:20px;">Загрузка списка...</ul>
                            </div>
                        </div>
                    </div>

                    <!-- Вкладка 3: Конфиг -->
                    <div id="tab-config" class="tab-content">
                        <div class="card">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
                                <h3 style="margin:0;">⚙ Редактор server.properties</h3>
                                <button class="btn btn-green" style="width:200px; margin:0;" onclick="saveProperties()">💾 Сохранить конфиг</button>
                            </div>
                            <form id="propsForm" class="prop-grid">
                                <!-- Сюда JS загрузит инпуты -->
                            </form>
                        </div>
                    </div>
                </div>

                <script>
                    function showTab(tabId, btn) {
                        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                        document.getElementById(tabId).classList.add('active');
                        btn.classList.add('active');
                        if(tabId === 'tab-config') loadProperties();
                        if(tabId === 'tab-worlds') loadBackups();
                    }
                    function apiPost(url, data=null) {
                        fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: data ? JSON.stringify(data) : null })
                        .then(r => r.json()).then(res => {
                            if(res.message) alert(res.message);
                            setTimeout(() => location.reload(), 800);
                        });
                    }
                    function sendCmd() {
                        const inp = document.getElementById('cmdInput');
                        if(!inp.value.trim()) return;
                        apiPost('/api/console/command', {command: inp.value.trim()});
                        inp.value = '';
                    }
                    function playerAction(act) {
                        const p = document.getElementById('playerInput').value.trim();
                        if(!p) return alert('Введите ник игрока!');
                        apiPost('/api/players/action', {player: p, action: act});
                    }
                    function switchWorld() {
                        const w = document.getElementById('worldSelect').value;
                        if(confirm(`Сменить мир на "${w}"? Сервер перезапустится.`)) apiPost('/api/worlds/switch', {world: w});
                    }
                    function createWorld() {
                        const w = document.getElementById('newWorldName').value.trim();
                        if(!w) return alert('Введите имя!');
                        if(confirm(`Создать чистый мир "${w}"?`)) apiPost('/api/worlds/switch', {world: w});
                    }
                    function createBackup() {
                        alert('Генерация ZIP-архива запущена в фоне!');
                        apiPost('/api/backups/create');
                    }
                    function loadBackups() {
                        fetch('/api/backups').then(r=>r.json()).then(res => {
                            const ul = document.getElementById('backupList');
                            ul.innerHTML = res.backups.map(b => `<li>${b}</li>`).join('') || '<li>Бэкапов пока нет</li>';
                        });
                    }
                    function loadProperties() {
                        fetch('/api/config/properties').then(r=>r.json()).then(res => {
                            const form = document.getElementById('propsForm');
                            form.innerHTML = '';
                            for(const [k, v] of Object.entries(res.properties)) {
                                form.innerHTML += `<div class="prop-item"><label>${k}</label><input type="text" name="${k}" value="${v}"></div>`;
                            }
                        });
                    }
                    function saveProperties() {
                        const form = document.getElementById('propsForm');
                        const data = {};
                        new FormData(form).forEach((v, k) => data[k] = v);
                        fetch('/api/config/properties', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) })
                        .then(r=>r.json()).then(res => alert(res.message || 'Сохранено! Перезапустите сервер.'));
                    }
                    function updateLogs() {
                        fetch('/api/console/logs').then(r=>r.json()).then(res => {
                            const el = document.getElementById('consoleLogs');
                            const scroll = el.scrollHeight - el.clientHeight <= el.scrollTop + 30;
                            el.textContent = res.logs;
                            if(scroll) el.scrollTop = el.scrollHeight;
                        });
                    }
                    setInterval(updateLogs, 2000); updateLogs();
                </script>
            </body>
            </html>
            '''
            return render_template_string(
                html, status=self.is_running, local_ip=self.local_ip, public_ip=self.public_ip,
                port=self.config['port'], current_world=self.current_world, worlds=self.get_available_worlds()
            )

        # 1. СТАТУС
        @self.web_app.route('/api/status', methods=['GET'])
        def api_status():
            uptime = str(datetime.now().replace(microsecond=0) - self.start_time.replace(microsecond=0)) if self.start_time else "Offline"
            return jsonify({
                "running": self.is_running, "uptime": uptime, "version": self.config["version"],
                "active_world": self.current_world, "port": self.config["port"],
                "ip_local": self.local_ip, "ip_public": self.public_ip, "ram": f'{self.config["ram_min"]} - {self.config["ram_max"]}'
            })

        # 2. ПИТАНИЕ
        @self.web_app.route('/api/power/start', methods=['POST'])
        def api_start():
            if self.is_running: return jsonify({"success": False, "message": "Сервер уже работает!"})
            threading.Thread(target=self.auto_start_all, daemon=True).start()
            return jsonify({"success": True, "message": "Запуск процесса сервера..."})

        @self.web_app.route('/api/power/stop', methods=['POST'])
        def api_stop():
            if not self.is_running: return jsonify({"success": False, "message": "Сервер выключен!"})
            threading.Thread(target=self.stop_server, daemon=True).start()
            return jsonify({"success": True, "message": "Отправлена команда сохранения и остановки..."})

        @self.web_app.route('/api/power/kill', methods=['POST'])
        def api_kill():
            success = self.kill_server()
            return jsonify({"success": success, "message": "Процесс убит!" if success else "Процесс не найден."})

        # 3. КОНСОЛЬ
        @self.web_app.route('/api/console/logs', methods=['GET'])
        def api_logs():
            return jsonify({"logs": "".join(self.log_buffer)})

        @self.web_app.route('/api/console/command', methods=['POST'])
        def api_cmd():
            d = request.get_json()
            if d and d.get('command'):
                self.send_command(d['command'])
                return jsonify({"success": True})
            return jsonify({"success": False, "message": "Команда пуста."}), 400

        # 4. ИГРОКИ
        @self.web_app.route('/api/players/action', methods=['POST'])
        def api_player():
            d = request.get_json()
            player = d.get('player') if d else None
            action = d.get('action') if d else None
            if not player or action not in ['op', 'deop', 'kick', 'ban']:
                return jsonify({"success": False, "message": "Некорректный игрок или действие."}), 400
            self.send_command(f"{action} {player}")
            return jsonify({"success": True, "message": f"Команда {action} {player} выполнена."})

        # 5. МИРЫ И БЭКАПЫ
        @self.web_app.route('/api/worlds/switch', methods=['POST'])
        def api_switch_world():
            d = request.get_json()
            if d and d.get('world'):
                success, msg = self.switch_world(d['world'])
                return jsonify({"success": success, "message": msg})
            return jsonify({"success": False, "message": "Имя мира не указано."}), 400

        @self.web_app.route('/api/backups', methods=['GET'])
        def api_list_backups():
            backups = [f.name for f in self.backups_dir.glob("*.zip")] if self.backups_dir.exists() else []
            return jsonify({"backups": sorted(backups, reverse=True)})

        @self.web_app.route('/api/backups/create', methods=['POST'])
        def api_create_backup():
            threading.Thread(target=self.backup_current_world, daemon=True).start()
            return jsonify({"success": True, "message": "Резервное копирование запущено."})

        # 6. КОНФИГУРАЦИЯ
        @self.web_app.route('/api/config/properties', methods=['GET', 'POST'])
        def api_props():
            if request.method == 'GET':
                return jsonify({"properties": self.get_server_properties()})
            else:
                new_props = request.get_json()
                if new_props and isinstance(new_props, dict):
                    self.save_server_properties(new_props)
                    return jsonify({"success": True, "message": "server.properties успешно обновлен!"})
                return jsonify({"success": False, "message": "Неверный формат данных."}), 400

    def run(self, port=5000):
        threading.Thread(target=lambda: self.web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False), daemon=True).start()
        time.sleep(1)
        print(f"\n[System] REST API Сервер запущен на порту {port}")
        print(f"[System] Панель управления: http://localhost:{port}")
        try: webbrowser.open(f"http://localhost:{port}")
        except: pass

if __name__ == "__main__":
    print("==========================================================")
    print(" 🎮 MINECRAFT REST API ENGINE (100% HEADLESS / NO-CLI) 🎮 ")
    print("==========================================================")
    
    app = HeadlessMinecraftServer()
    app.run(5000)
    app.auto_start_all()
    
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("\n👋 Получен сигнал завершения. Выключаю сервер...")
        app.stop_server()
        sys.exit(0)