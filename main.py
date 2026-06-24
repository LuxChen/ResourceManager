import os
import threading
import hashlib
import subprocess
import sqlite3
import re
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import psutil
from datetime import datetime, timedelta


class LocalDatabaseManager:
    """本地 SQLite 缓存与索引双引擎（含智能分析扩展）"""
    def __init__(self, db_path="optimizer_cache.db"):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # 提升写入性能：启用 WAL 模式和适度同步策略
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=NORMAL')
            except Exception:
                pass
            # 1. 历史哈希表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS file_hashes (
                    filepath TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime REAL,
                    md5_hash TEXT
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_size_mtime ON file_hashes(size, mtime)')
            
            # 2. 全局文件索引表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS global_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filepath TEXT unique,
                    filename TEXT,
                    extension TEXT,
                    size INTEGER,
                    mtime REAL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_filename ON global_index(filename)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_ext ON global_index(extension)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_size ON global_index(size)')
            # 尝试创建 FTS5 虚拟表以加速全文检索，如果不可用则回退
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS global_index_fts USING fts5(filename, extension, filepath, content='global_index', content_rowid='id')")
                conn.execute('''CREATE TRIGGER IF NOT EXISTS global_index_ai AFTER INSERT ON global_index BEGIN
                                   INSERT INTO global_index_fts(rowid, filename, extension, filepath) VALUES (new.id, new.filename, new.extension, new.filepath);
                                 END''')
                conn.execute('''CREATE TRIGGER IF NOT EXISTS global_index_ad AFTER DELETE ON global_index BEGIN
                                   INSERT INTO global_index_fts(global_index_fts, rowid, filename, extension, filepath) VALUES('delete', old.id, old.filename, old.extension, old.filepath);
                                 END''')
                conn.execute('''CREATE TRIGGER IF NOT EXISTS global_index_au AFTER UPDATE ON global_index BEGIN
                                   INSERT INTO global_index_fts(global_index_fts, rowid, filename, extension, filepath) VALUES('delete', old.id, old.filename, old.extension, old.filepath);
                                   INSERT INTO global_index_fts(rowid, filename, extension, filepath) VALUES (new.id, new.filename, new.extension, new.filepath);
                                 END''')
                self.fts_available = True
            except Exception:
                self.fts_available = False

    def clear_global_index(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM global_index")
            try:
                if getattr(self, 'fts_available', False):
                    conn.execute("DELETE FROM global_index_fts")
            except Exception:
                pass

    def get_indexed_files(self, target_dir=None):
        query = "SELECT filepath, size, mtime FROM global_index"
        params = []
        if target_dir:
            target_dir = os.path.normpath(target_dir)
            if not target_dir.endswith(os.sep):
                target_dir += os.sep
            query += " WHERE filepath LIKE ?"
            params.append(f"{target_dir}%")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(query, params)
            return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    def delete_index_paths(self, paths):
        if not paths:
            return 0
        deleted = 0
        with sqlite3.connect(self.db_path) as conn:
            for i in range(0, len(paths), 1000):
                batch = paths[i:i+1000]
                placeholders = ','.join('?' for _ in batch)
                cursor = conn.execute(f"DELETE FROM global_index WHERE filepath IN ({placeholders})", batch)
                deleted += cursor.rowcount
        return deleted

    def batch_insert_index(self, data_list):
        if not data_list: return
        with sqlite3.connect(self.db_path) as conn:
            # 指定列名插入，避免表结构修改或包含自增主键时的列数量不匹配错误
            conn.executemany('INSERT OR REPLACE INTO global_index (filepath, filename, extension, size, mtime) VALUES (?, ?, ?, ?, ?)', data_list)
            # FTS5 触发器会自动保持全局索引的同步，无需额外写入操作

    def _is_safe_fts_query(self, keyword):
        # 仅当关键词中不包含点、连字符等 FTS5 特殊语法字符时，才使用 MATCH
        return bool(re.fullmatch(r'[\w\s]+', keyword))

    def search_global_index(self, keyword, ext_filter="所有格式", limit=800):
        kw = (keyword or '').strip()
        if getattr(self, 'fts_available', False) and kw and self._is_safe_fts_query(kw):
            parts = [p for p in kw.split() if p]
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                query = "SELECT gi.filename, gi.extension, gi.size, gi.mtime, gi.filepath FROM global_index_fts f JOIN global_index gi ON gi.id = f.rowid WHERE "
                params = []
                match = ' OR '.join([f'{p}*' for p in parts])
                query += " f.filename MATCH ?"
                params.append(match)
                if ext_filter and ext_filter != "所有格式":
                    query += " AND gi.extension = ?"
                    params.append(ext_filter.lower())
                query += " ORDER BY gi.size DESC LIMIT ?"
                params.append(limit)
                try:
                    cursor.execute(query, params)
                    return cursor.fetchall()
                except sqlite3.OperationalError:
                    pass
        else:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                query = "SELECT filename, extension, size, mtime, filepath FROM global_index WHERE filename LIKE ?"
                params = [f"%{kw}%"]
                if ext_filter and ext_filter != "所有格式":
                    query += " AND extension = ?"
                    params.append(ext_filter.lower())
                query += " ORDER BY size DESC LIMIT ?"
                params.append(limit)
                cursor.execute(query, params)
                return cursor.fetchall()

    def stream_search_global_index(self, keyword, ext_filter="所有格式", batch=200):
        """按批返回查询结果，适用于大结果集的流式消费，避免一次性将所有行载入内存。"""
        kw = (keyword or '').strip()
        if getattr(self, 'fts_available', False) and kw and self._is_safe_fts_query(kw):
            parts = [p for p in kw.split() if p]
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                query = "SELECT gi.filename, gi.extension, gi.size, gi.mtime, gi.filepath FROM global_index_fts f JOIN global_index gi ON gi.id = f.rowid WHERE "
                params = []
                match = ' OR '.join([f'{p}*' for p in parts])
                query += " f.filename MATCH ?"
                params.append(match)
                if ext_filter and ext_filter != "所有格式":
                    query += " AND gi.extension = ?"
                    params.append(ext_filter.lower())
                query += " ORDER BY gi.size DESC"
                try:
                    cursor.execute(query, params)
                    while True:
                        rows = cursor.fetchmany(batch)
                        if not rows:
                            break
                        yield rows
                except sqlite3.OperationalError:
                    pass
        else:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                query = "SELECT filename, extension, size, mtime, filepath FROM global_index WHERE filename LIKE ?"
                params = [f"%{kw}%"]
                if ext_filter and ext_filter != "所有格式":
                    query += " AND extension = ?"
                    params.append(ext_filter.lower())
                query += " ORDER BY size DESC"
                cursor.execute(query, params)
                while True:
                    rows = cursor.fetchmany(batch)
                    if not rows:
                        break
                    yield rows

    # ==================== ✨ 新增：智能推荐清洗数据源下钻 ====================
    def get_smart_recommendations(self, min_size_mb=50, limit=500):
        """从 SQLite 全局索引库中，通过大小和时间矩阵直接计算清理推荐度"""
        min_bytes = min_size_mb * 1024 * 1024
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 筛选大于指定体积的文件，且过滤掉高危的系统底层格式
            query = '''
                SELECT filename, extension, size, mtime, filepath 
                FROM global_index 
                WHERE size >= ? 
                  AND extension NOT IN ('.dll', '.sys', '.ini', '.drv', '.vmdk', '.ocx')
                ORDER BY size DESC 
                LIMIT ?
            '''
            cursor.execute(query, (min_bytes, limit))
            return cursor.fetchall()

    def get_hash(self, filepath, current_size, current_mtime):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT size, mtime, md5_hash FROM file_hashes WHERE filepath=?", (filepath,))
            row = cursor.fetchone()
            if row and row[0] == current_size and row[1] == current_mtime: return row[2]
        return None

    def set_hash(self, filepath, size, mtime, md5_hash):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO file_hashes VALUES (?, ?, ?, ?)', (filepath, size, mtime, md5_hash))


class SmartPathResolver:
    @staticmethod
    def get_smart_paths():
        paths = {}
        user_profile = os.environ.get('USERPROFILE', '')
        if os.environ.get('TEMP'): paths["🧹 系统临时缓存区 (Temp)"] = os.environ.get('TEMP')
        downloads = os.path.join(user_profile, 'Downloads')
        if os.path.exists(downloads): paths["📦 浏览器下载目录 (Downloads)"] = downloads
        wechat_path = os.path.join(user_profile, 'Documents', 'WeChat Files')
        if os.path.exists(wechat_path): paths["💬 微信数据与缓存 (WeChat Files)"] = wechat_path
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        if local_appdata: paths["⚙️ 应用本地日志与缓存 (LocalAppData)"] = local_appdata
        desktop = os.path.join(user_profile, 'Desktop')
        if os.path.exists(desktop): paths["🖥️ 桌面闲置文件 (Desktop)"] = desktop
        return paths


class FullyAutomatedOptimizerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Python 系统优化 - 智能内核推荐版")
        self.root.geometry("1060x740")
        self.root.minsize(950, 650)
        
        self.junk_extensions = ['.tmp', '.log', '.chk', '.old', '.bak', '.lnk']
        self.smart_paths_dict = SmartPathResolver.get_smart_paths()
        self.cancel_event = threading.Event()
        self.db = LocalDatabaseManager()

        self.create_widgets()
        self.create_context_menu()
# ==================== 核心集成：通用表格交互式排序 ====================
    def sort_treeview(self, tree, col, reverse):
        """
        具备视觉反馈的通用排序器：
        1. 识别数值/文本类型自动排序
        2. 更新表头显示 ▲/▼ 标识
        3. 重置其他列状态并保留排序命令
        """
        # 1. 恢复其他列的原始标题状态（去除图标）并设置排序命令
        for c in tree['columns']:
            header = tree.heading(c, 'text').replace(' ▲', '').replace(' ▼', '')
            tree.heading(c, text=header, command=lambda c=c: self.sort_treeview(tree, c, False))

        # 2. 对当前点击列执行排序
        data = [(tree.set(child, col), child) for child in tree.get_children('')]

        # 智能类型判断
        try:
            data.sort(key=lambda t: float(t[0].split()[0]), reverse=reverse)
        except (ValueError, IndexError):
            data.sort(key=lambda t: t[0].lower() if isinstance(t[0], str) else t[0], reverse=reverse)

        # 3. 移动节点
        for index, (_, child) in enumerate(data):
            tree.move(child, '', index)

        # 4. 更新当前列标题为高亮标识
        heading_text = tree.heading(col, 'text').replace(' ▲', '').replace(' ▼', '')
        tree.heading(col, text=f"{heading_text} {'▲' if reverse else '▼'}", command=lambda: self.sort_treeview(tree, col, not reverse))

    def make_treeview_sortable(self, tree):
        for col in tree['columns']:
            header = tree.heading(col, 'text')
            tree.heading(col, text=header, command=lambda c=col: self.sort_treeview(tree, c, False))
    # ==================== UI 绑定逻辑示例（在所有 Setup 方法中使用） ==================== 
    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.recommend_frame = ttk.Frame(self.notebook) # ✨ 新增：推荐看板排最前
        self.search_frame = ttk.Frame(self.notebook)
        self.unused_frame = ttk.Frame(self.notebook)
        self.clean_frame = ttk.Frame(self.notebook)
        self.proc_frame = ttk.Frame(self.notebook)
        
        self.notebook.add(self.recommend_frame, text=" 💡 智能清理推荐 ")
        self.notebook.add(self.search_frame, text=" ⚡ 全局极速搜索 ")
        self.notebook.add(self.unused_frame, text=" 🔍 长期未使用分析 ")
        self.notebook.add(self.clean_frame, text=" 🧹 基础垃圾与去重 ")
        self.notebook.add(self.proc_frame, text=" ⚙️ 程序进程解耦 ")
        
        self.setup_recommend_tab() # ✨ 初始化推荐 UI
        self.setup_search_tab()
        self.setup_unused_tab()
        self.setup_clean_tab()
        self.setup_proc_tab()

    def create_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="📁 转到文件所在目录", command=self.open_file_location)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗑️ 彻底永久删除该项", command=self.delete_selected_item)

    def show_right_click_menu(self, event, tree_widget):
        self.active_tree = tree_widget
        clicked_item = tree_widget.identify_row(event.y)
        if clicked_item:
            if clicked_item not in tree_widget.selection():
                tree_widget.selection_set(clicked_item)
            self.context_menu.post(event.x_root, event.y_root)

    def open_file_location(self):
        if not hasattr(self, 'active_tree') or not self.active_tree: return
        selected = self.active_tree.selection()
        if not selected: return
        file_path_str = self.active_tree.item(selected[0], 'values')[5 if self.active_tree == self.recommend_tree else 4]
        if os.path.exists(file_path_str):
            try: subprocess.run(['explorer.exe', '/select,', os.path.normpath(file_path_str)], check=False)
            except Exception as e: messagebox.showerror("失败", str(e))

    def delete_selected_item(self):
        if not hasattr(self, 'active_tree') or not self.active_tree: return
        selected_items = self.active_tree.selection()
        if not selected_items: return
        path_idx = 5 if self.active_tree == self.recommend_tree else 4
        if messagebox.askyesno("危险确认", f"确认物理永久抹除选中的 {len(selected_items)} 个文件吗？"):
            for item in selected_items:
                path_str = self.active_tree.item(item, 'values')[path_idx]
                try:
                    os.remove(path_str)
                    self.active_tree.delete(item)
                except: pass

    # ==================== ✨ 新增标签页：智能清理推荐 UI ====================
    def setup_recommend_tab(self):
        top_bar = ttk.LabelFrame(self.recommend_frame, text="智能分析策略过滤器", padding=10)
        top_bar.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(top_bar, text="排查体积 ≥").pack(side=tk.LEFT, padx=5)
        self.rec_size_spin = ttk.Spinbox(top_bar, from_=10, to=10240, width=6)
        self.rec_size_spin.set(100) # 默认过滤出 100MB 以上大文件
        self.rec_size_spin.pack(side=tk.LEFT)
        ttk.Label(top_bar, text="MB").pack(side=tk.LEFT, padx=2)
        
        ttk.Button(top_bar, text="🧠 运行多维价值评估推荐", command=self.execute_smart_analysis).pack(side=tk.LEFT, padx=20)
        ttk.Button(top_bar, text="🗑️ 批量一键粉碎选中推荐项", command=self.delete_selected_item).pack(side=tk.LEFT, padx=5)
        
        self.lbl_rec_status = ttk.Label(self.recommend_frame, text="提示: 本功能基于全局索引库。如果搜索不到文件，请先去【全局极速搜索】标签页构建/更新索引。", foreground="#555")
        self.lbl_rec_status.pack(anchor=tk.W, padx=15, pady=5)

        # 结果表格
        table_frame = ttk.Frame(self.recommend_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        columns = ('score', 'name', 'size', 'idle', 'reason', 'path')
        self.recommend_tree = ttk.Treeview(table_frame, columns=columns, show='headings', selectmode='extended')
        self.recommend_tree.heading('score', text='🔥 清理推荐指数')
        self.recommend_tree.heading('name', text='大文件名称')
        self.recommend_tree.heading('size', text='文件体积')
        self.recommend_tree.heading('idle', text='最后修改时间')
        self.recommend_tree.heading('reason', text='风险/推荐安全原因评估')
        self.recommend_tree.heading('path', text='完整路径')
        
        self.recommend_tree.column('score', width=120, anchor=tk.CENTER)
        self.recommend_tree.column('name', width=180)
        self.recommend_tree.column('size', width=90, anchor=tk.CENTER)
        self.recommend_tree.column('idle', width=120, anchor=tk.CENTER)
        self.recommend_tree.column('reason', width=240)
        self.recommend_tree.column('path', width=260)
        self.recommend_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.make_treeview_sortable(self.recommend_tree)
        self.recommend_tree.bind("<Button-3>", lambda event: self.show_right_click_menu(event, self.recommend_tree))
        scrollbar = ttk.Scrollbar(table_frame, command=self.recommend_tree.yview)
        scrollbar.pack(fill=tk.Y, side=tk.RIGHT)
        self.recommend_tree.config(yscrollcommand=scrollbar.set)
        

    # ==================== ✨ 新增后端：多维价值算法评估内核 ====================
    def execute_smart_analysis(self):
        """核心业务逻辑优化：结合大文件排查与检索，融合评分矩阵"""
        try: min_size = int(self.rec_size_spin.get())
        except: min_size = 100
            
        self.recommend_tree.delete(*self.recommend_tree.get_children())
        raw_files = self.db.get_smart_recommendations(min_size_mb=min_size)
        
        if not raw_files:
            self.lbl_rec_status.config(text="未找到匹配的大文件资产，或者您的本地全局索引库为空，请先去更新索引。")
            return
            
        now = datetime.now()
        recommended_count = 0
        
        for name, ext, size, mtime, filepath in raw_files:
            # 1. 计算 Size Factor (0~100) -> 越接近或超过 2GB 得分越高
            size_mb = size / 1048576
            size_factor = min(100, int((size_mb / 2048) * 100))
            
            # 2. 计算 Age Factor (0~100) -> 越久未改动得分越高，365天封顶
            file_date = datetime.fromtimestamp(mtime)
            days_idle = (now - file_date).days
            age_factor = min(100, int((days_idle / 365) * 100))
            
            # 3. 计算 Path Factor -> 敏感位置加成
            path_upper = filepath.upper()
            path_factor = 30 # 默认普通权重
            location_desc = "常规目录大文件"
            if "TEMP" in path_upper or "CACHE" in path_upper:
                path_factor = 100
                location_desc = "临时缓存堆积"
            elif "DOWNLOADS" in path_upper:
                path_factor = 80
                location_desc = "高频下载沉积"
            elif "WECHAT FILES" in path_upper:
                path_factor = 70
                location_desc = "微信长期缓存占用"

            # 🧩 融合推荐指数公式
            total_score = int((size_factor * 0.4) + (age_factor * 0.4) + (path_factor * 0.2))
            
            # 制定可视化的直观评级与原因
            if total_score >= 75:
                score_str = f"🔴 强烈极力推荐 ({total_score})"
                reason = f"【{location_desc}】体积高达 {size_mb:.0f}MB 且已闲置 {days_idle} 天，极度建议清理。"
            elif total_score >= 45:
                score_str = f"🟡 建议自主甄别 ({total_score})"
                reason = f"体积稍大（{size_mb:.0f}MB），已封存 {days_idle} 天，请确认是否为备份。"
            else:
                score_str = f"🟢 建议予以留存 ({total_score})"
                reason = f"虽占空间但近期（{days_idle}天内）有活动迹象，可能是活跃工程项目。"

            # 格式化输出
            sz_str = f"{size_mb:.2f} MB" if size_mb >= 1.0 else f"{size/1024:.1f} KB"
            mtime_str = file_date.strftime('%Y-%m-%d')
            
            # 过滤干扰项：只推荐有一定清理价值的
            if total_score >= 35:
                recommended_count += 1
                self.recommend_tree.insert('', tk.END, values=(score_str, name, sz_str, mtime_str, reason, filepath))

        self.lbl_rec_status.config(text=f"🧠 智能价值模型审计完毕：已为您精准捕捉并上架了 {recommended_count} 个高价值可清理对象。")

    # ==================== 历史功能模块 UI（完好集成） ====================
    def setup_search_tab(self):
        idx_frame = ttk.LabelFrame(self.search_frame, text="本地高速搜索引擎管理", padding=10)
        idx_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(idx_frame, text="目标目录:").pack(side=tk.LEFT, padx=5)
        self.search_path_entry = ttk.Entry(idx_frame, font=("Segoe UI", 10))
        self.search_path_entry.insert(0, os.environ.get('USERPROFILE', 'C:\\'))
        self.search_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(idx_frame, text="📂 浏览", command=self.browse_search_target).pack(side=tk.LEFT, padx=5)
        self.btn_build_idx = ttk.Button(idx_frame, text="🔄 构建/更新全局索引", command=lambda: self.start_thread(self.build_global_index, [self.btn_build_idx, self.btn_search], cancel_button=self.btn_cancel_search))
        self.btn_build_idx.pack(side=tk.LEFT, padx=5)
        
        filter_frame = ttk.Frame(self.search_frame, padding=5)
        filter_frame.pack(fill=tk.X, padx=10)
        ttk.Label(filter_frame, text="🔍 关键词:").pack(side=tk.LEFT, padx=5)
        self.search_keyword_entry = ttk.Entry(filter_frame, font=("Segoe UI", 10), width=35)
        self.search_keyword_entry.pack(side=tk.LEFT, padx=5)
        self.search_keyword_entry.bind("<Return>", lambda e: self.execute_fast_search())
        self.search_ext_combo = ttk.Combobox(filter_frame, values=["所有格式", ".exe", ".zip", ".mp4", ".pdf", ".log"], state="readonly", width=10)
        self.search_ext_combo.current(0)
        self.search_ext_combo.pack(side=tk.LEFT, padx=5)
        self.btn_search = ttk.Button(filter_frame, text="⚡ 闪电搜索", command=lambda: self.start_thread(self.execute_fast_search, [self.btn_search, self.btn_build_idx], cancel_button=self.btn_cancel_search))
        self.btn_search.pack(side=tk.LEFT, padx=10)
        self.btn_cancel_search = ttk.Button(filter_frame, text="🛑 终止索引", state=tk.DISABLED, command=self.trigger_cancel)
        self.btn_cancel_search.pack(side=tk.LEFT, padx=5)
        
        self.search_progress = ttk.Progressbar(self.search_frame, mode='determinate', maximum=100)
        self.search_progress.pack(fill=tk.X, padx=15, pady=5)
        self.lbl_search_status = ttk.Label(self.search_frame, text="状态: 索引库就绪")
        self.lbl_search_status.pack(anchor=tk.W, padx=15)

        table_frame = ttk.Frame(self.search_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.search_tree = ttk.Treeview(table_frame, columns=('name', 'ext', 'size', 'mtime', 'path'), show='headings', selectmode='extended')
        self.search_tree.heading('name', text='文件名称'); self.search_tree.heading('ext', text='扩展名'); self.search_tree.heading('size', text='大小'); self.search_tree.heading('mtime', text='修改时间'); self.search_tree.heading('path', text='完整路径')
        self.search_tree.column('name', width=200); self.search_tree.column('ext', width=80, anchor=tk.CENTER); self.search_tree.column('size', width=90, anchor=tk.CENTER); self.search_tree.column('mtime', width=130, anchor=tk.CENTER); self.search_tree.column('path', width=400)
        self.search_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.make_treeview_sortable(self.search_tree)
        self.search_tree.bind("<Button-3>", lambda event: self.show_right_click_menu(event, self.search_tree))
        scrollbar = ttk.Scrollbar(table_frame, command=self.search_tree.yview)
        scrollbar.pack(fill=tk.Y, side=tk.RIGHT)
        self.search_tree.config(yscrollcommand=scrollbar.set)

    def setup_unused_tab(self):
        config_frame = ttk.LabelFrame(self.unused_frame, text="智能推荐扫描区域", padding=10)
        config_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(config_frame, text="推荐目标:").pack(side=tk.LEFT, padx=5)
        self.unused_combo = ttk.Combobox(config_frame, values=list(self.smart_paths_dict.keys()), state="readonly", width=45)
        if self.smart_paths_dict: self.unused_combo.current(0)
        self.unused_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Label(config_frame, text="手动目录:").pack(side=tk.LEFT, padx=(10,5))
        self.unused_path_entry = ttk.Entry(config_frame, font=("Segoe UI", 10), width=35)
        self.unused_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(config_frame, text="📂 浏览", command=self.browse_unused_target).pack(side=tk.LEFT, padx=5)
        ttk.Label(config_frame, text="闲置时间 >").pack(side=tk.LEFT, padx=(10, 2))
        self.days_spin = ttk.Spinbox(config_frame, from_=30, to=1095, width=5)
        self.days_spin.set(180)
        self.days_spin.pack(side=tk.LEFT)
        ttk.Label(config_frame, text="天").pack(side=tk.LEFT, padx=2)

        ctrl_frame = ttk.Frame(self.unused_frame, padding=5)
        ctrl_frame.pack(fill=tk.X, padx=10)
        self.btn_scan_unused = ttk.Button(ctrl_frame, text="🚀 一键智能透视闲置资源", command=lambda: self.start_thread(self.scan_unused_files, [self.btn_scan_unused, self.btn_delete_unused], cancel_button=self.btn_cancel_unused))
        self.btn_scan_unused.pack(side=tk.LEFT, padx=5)
        self.btn_delete_unused = ttk.Button(ctrl_frame, text="🗑️ 批量清除选中闲置项", command=self.delete_selected_item)
        self.btn_delete_unused.pack(side=tk.LEFT, padx=5)
        self.btn_cancel_unused = ttk.Button(ctrl_frame, text="🛑 终止当前操作", state=tk.DISABLED, command=self.trigger_cancel)
        self.btn_cancel_unused.pack(side=tk.LEFT, padx=5)
        
        self.unused_progress = ttk.Progressbar(self.unused_frame, mode='determinate', maximum=100)
        self.unused_progress.pack(fill=tk.X, padx=15, pady=5)
        self.lbl_unused_status = ttk.Label(self.unused_frame, text="状态: 准备就绪")
        self.lbl_unused_status.pack(anchor=tk.W, padx=15)

        table_frame = ttk.Frame(self.unused_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.unused_tree = ttk.Treeview(table_frame, columns=('name', 'type', 'size', 'last_used', 'path'), show='headings', selectmode='extended')
        self.unused_tree.heading('name', text='名称'); self.unused_tree.heading('type', text='闲置属性'); self.unused_tree.heading('size', text='大小'); self.unused_tree.heading('last_used', text='最后活动时间'); self.unused_tree.heading('path', text='完整路径')
        self.unused_tree.column('name', width=180); self.unused_tree.column('type', width=160, anchor=tk.CENTER); self.unused_tree.column('size', width=80, anchor=tk.CENTER); self.unused_tree.column('last_used', width=120, anchor=tk.CENTER); self.unused_tree.column('path', width=320)
        self.unused_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.make_treeview_sortable(self.unused_tree)
        self.unused_tree.bind("<Button-3>", lambda event: self.show_right_click_menu(event, self.unused_tree))
        scrollbar = ttk.Scrollbar(table_frame, command=self.unused_tree.yview)
        scrollbar.pack(fill=tk.Y, side=tk.RIGHT)
        self.unused_tree.config(yscrollcommand=scrollbar.set)

    def setup_clean_tab(self):
        path_frame = ttk.LabelFrame(self.clean_frame, text="智能推荐扫描区域", padding=10)
        path_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(path_frame, text="推荐目标:").pack(side=tk.LEFT, padx=5)
        self.clean_combo = ttk.Combobox(path_frame, values=list(self.smart_paths_dict.keys()), state="readonly", width=35)
        if self.smart_paths_dict: self.clean_combo.current(0)
        self.clean_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Label(path_frame, text="手动目录:").pack(side=tk.LEFT, padx=(10, 5))
        self.clean_path_entry = ttk.Entry(path_frame, font=("Segoe UI", 10), width=35)
        self.clean_path_entry.insert(0, os.environ.get('USERPROFILE', 'C:\\'))
        self.clean_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(path_frame, text="📂 浏览", command=self.browse_clean_target).pack(side=tk.LEFT, padx=5)

        filter_frame = ttk.LabelFrame(self.clean_frame, text="清理策略过滤", padding=10)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(filter_frame, text="垃圾类型:").pack(side=tk.LEFT, padx=5)
        self.clean_ext_combo = ttk.Combobox(filter_frame, values=["所有垃圾", ".tmp", ".log", ".bak", ".old", ".chk", ".lnk", ".cache", ".dmp", ".db"], state="readonly", width=12)
        self.clean_ext_combo.current(0)
        self.clean_ext_combo.pack(side=tk.LEFT, padx=5)
        ttk.Label(filter_frame, text="过期天数 >").pack(side=tk.LEFT, padx=(10, 2))
        self.old_days_spin = ttk.Spinbox(filter_frame, from_=7, to=3650, width=6)
        self.old_days_spin.set(180)
        self.old_days_spin.pack(side=tk.LEFT)
        ttk.Label(filter_frame, text="天").pack(side=tk.LEFT, padx=2)

        btn_frame = ttk.Frame(self.clean_frame, padding=5)
        btn_frame.pack(fill=tk.X, padx=10)
        self.btn_clean = ttk.Button(btn_frame, text="🔍 一键清空目标内垃圾", command=lambda: self.start_thread(self.clean_junk, [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_clean.pack(side=tk.LEFT, padx=5)
        self.btn_manual_clean = ttk.Button(btn_frame, text="📂 手动目录清理", command=lambda: self.start_thread(lambda: self.clean_junk(target=self.clean_path_entry.get().strip()), [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_manual_clean.pack(side=tk.LEFT, padx=5)
        self.btn_type_clean = ttk.Button(btn_frame, text="🗂️ 按类型清理", command=lambda: self.start_thread(lambda: self.clean_junk(ext_filter=self.clean_ext_combo.get()), [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_type_clean.pack(side=tk.LEFT, padx=5)
        self.btn_old_clean = ttk.Button(btn_frame, text="🕒 清理旧文件", command=lambda: self.start_thread(self.clean_old_files, [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_old_clean.pack(side=tk.LEFT, padx=5)
        self.btn_empty_clean = ttk.Button(btn_frame, text="🧹 清空空文件夹", command=lambda: self.start_thread(self.clean_empty_folders, [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_empty_clean.pack(side=tk.LEFT, padx=5)
        self.btn_dup = ttk.Button(btn_frame, text="👯 极速扫描重复文件", command=lambda: self.start_thread(self.clean_duplicates, [self.btn_clean, self.btn_dup, self.btn_manual_clean, self.btn_type_clean, self.btn_old_clean, self.btn_empty_clean], cancel_button=self.btn_cancel_clean))
        self.btn_dup.pack(side=tk.LEFT, padx=5)
        self.btn_cancel_clean = ttk.Button(btn_frame, text="🛑 终止当前操作", state=tk.DISABLED, command=self.trigger_cancel)
        self.btn_cancel_clean.pack(side=tk.LEFT, padx=5)
        
        progress_frame = ttk.Frame(self.clean_frame, padding=5)
        progress_frame.pack(fill=tk.X, padx=10, pady=5)
        self.progress = ttk.Progressbar(progress_frame, mode='determinate', maximum=100)
        self.progress.pack(fill=tk.X, side=tk.TOP, pady=(0, 2))
        self.lbl_status = ttk.Label(progress_frame, text="状态: 准备就绪")
        self.lbl_status.pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(self.clean_frame, text="实时日志追踪", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, state=tk.DISABLED, bg="#f8f9fa", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def setup_proc_tab(self):
        top_bar = ttk.Frame(self.proc_frame, padding=5)
        top_bar.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(top_bar, text="🔄 刷新运行中程序", command=self.refresh_processes).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_bar, text="🚫 强制掐断进程锁", command=self.kill_selected_process).pack(side=tk.LEFT, padx=5)
        table_frame = ttk.Frame(self.proc_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.proc_tree = ttk.Treeview(table_frame, columns=('pid', 'name', 'path'), show='headings', selectmode='browse')
        for col, text, width in [('pid','PID',70), ('name','程序名',180), ('path','路径',450)]:
            self.proc_tree.heading(col, text=text)
            self.proc_tree.column(col, width=width)
        self.proc_tree.pack(fill=tk.BOTH, expand=True)
        self.make_treeview_sortable(self.proc_tree)
        self.refresh_processes()

    # ==================== 后台引擎逻辑 ====================
    def build_global_index(self):
        target_dir = self.normalize_path(self.search_path_entry.get().strip())
        if not target_dir or not os.path.exists(target_dir):
            return
        self.safe_ui_update(lambda: self.lbl_search_status.config(text="正在初始化全局索引库..."))

        # 读取已有索引，后续删除已移除的文件并仅更新变动项
        existing_files = self.db.get_indexed_files(target_dir)
        tracked_keys = {os.path.normcase(os.path.normpath(path)): (path, size, mtime) for path, (size, mtime) in existing_files.items()}
        stale_paths = set(tracked_keys.keys())
        batch_pool, batch_size, total_indexed = [], 2000, 0
        updated, added = 0, 0
        self.safe_ui_update(lambda: [self.search_progress.config(mode='indeterminate', value=0), self.search_progress.start(10)])

        def file_iter(root_path):
            stack = [root_path]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    try:
                                        st = entry.stat(follow_symlinks=False)
                                    except Exception:
                                        continue
                                    path = os.path.normpath(entry.path)
                                    yield (path, entry.name, Path(path).suffix.lower(), st.st_size, st.st_mtime)
                            except Exception:
                                continue
                except Exception:
                    continue

        try:
            for file_path, name, suffix, size, mtime in file_iter(target_dir):
                if self.cancel_event.is_set():
                    break
                norm_path = os.path.normcase(file_path)
                existing = tracked_keys.get(norm_path)
                if existing:
                    _, old_size, old_mtime = existing
                    if old_size != size or old_mtime != mtime:
                        batch_pool.append((file_path, name, suffix, size, mtime))
                        updated += 1
                    stale_paths.discard(norm_path)
                else:
                    batch_pool.append((file_path, name, suffix, size, mtime))
                    added += 1

                if len(batch_pool) >= batch_size:
                    try:
                        self.db.batch_insert_index(batch_pool)
                    except Exception:
                        pass
                    total_indexed += len(batch_pool)
                    batch_pool.clear()
                    self.safe_ui_update(lambda c=total_indexed: self.lbl_search_status.config(text=f"建立索引中... 已登记资产: {c}"))
        finally:
            if batch_pool and not self.cancel_event.is_set():
                try:
                    self.db.batch_insert_index(batch_pool)
                except Exception:
                    pass
                total_indexed += len(batch_pool)

        if stale_paths and not self.cancel_event.is_set():
            delete_list = [tracked_keys[key][0] for key in stale_paths]
            try:
                deleted_count = self.db.delete_index_paths(delete_list)
                total_indexed += deleted_count
                self.safe_ui_update(lambda c=deleted_count: self.lbl_search_status.config(text=f"已移除 {c} 个已失效索引条目。"))
            except Exception:
                pass

        if self.cancel_event.is_set():
            self.safe_ui_update(lambda: [self.search_progress.stop(), self.search_progress.config(mode='determinate', value=0), self.lbl_search_status.config(text=f"⚠️ 索引操作已取消，已记录: {total_indexed} 个文件。")])
        else:
            self.safe_ui_update(lambda: [self.search_progress.stop(), self.search_progress.config(mode='determinate', value=100), self.lbl_search_status.config(text=f"🎉 索引更新完毕：新增 {added} 条，更新 {updated} 条，移除 {len(stale_paths)} 条失效索引。")])

    def execute_fast_search(self):
        # 在后台线程执行并以小批量写回主线程，避免界面卡顿
        keyword = self.search_keyword_entry.get().strip()
        ext_filter = self.search_ext_combo.get()
        if not keyword and ext_filter == "所有格式":
            return
        self.search_tree.delete(*self.search_tree.get_children())
        self.safe_ui_update(lambda: [self.search_progress.config(mode='indeterminate', value=0), self.search_progress.start(8), self.lbl_search_status.config(text="正在查询...")])
        inserted = 0
        batch_size = 5000
        try:
            for batch in self.db.stream_search_global_index(keyword, ext_filter, batch=batch_size):
                if self.cancel_event.is_set():
                    break
                rows_to_insert = []
                for name, ext, size, mtime, filepath in batch:
                    sz_str = f"{size/1048576:.2f} MB" if size>=1048576 else f"{size/1024:.1f} KB"
                    mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                    rows_to_insert.append((name, ext.upper(), sz_str, mtime_str, filepath))
                def _insert_rows(rows=rows_to_insert):
                    for r in rows:
                        self.search_tree.insert('', tk.END, values=r)
                self.safe_ui_update(_insert_rows)
                inserted += len(rows_to_insert)
                self.safe_ui_update(lambda c=inserted: self.lbl_search_status.config(text=f"已加载结果: {c}"))
                if self.cancel_event.is_set():
                    break
        finally:
            self.safe_ui_update(lambda: [self.search_progress.stop(), self.search_progress.config(mode='determinate', value=100), self.lbl_search_status.config(text=f"查询结束，已显示 {inserted} 条结果。")])

    def scan_unused_files(self):
        # 优先使用手动输入/选择的路径，否则使用推荐目标
        manual = getattr(self, 'unused_path_entry', None)
        if manual and manual.get().strip():
            target = self.normalize_path(manual.get().strip())
        else:
            target = self.normalize_path(self.get_selected_path(self.unused_combo))
        if not target or not os.path.exists(target):
            self.safe_ui_update(lambda: self.lbl_unused_status.config(text="错误：请选择有效目标路径。"))
            return
        try:
            threshold_days = int(self.days_spin.get())
        except ValueError:
            threshold_days = 180
        self.safe_ui_update(lambda: [self.unused_tree.delete(i) for i in self.unused_tree.get_children()])
        total_files = sum(len(fls) for r, d, fls in os.walk(target))
        if total_files == 0:
            self.safe_ui_update(lambda: self.lbl_unused_status.config(text="该目录没有可扫描的文件。"))
            return
        now, idx, found = datetime.now(), 0, 0
        cutoff = now - timedelta(days=threshold_days)
        for r, d, fls in os.walk(target):
            if self.cancel_event.is_set():
                self.safe_ui_update(lambda: self.lbl_unused_status.config(text=f"扫描已取消，已发现 {found} 个闲置文件。"))
                return
            for f in fls:
                idx += 1
                if idx % 50 == 0:
                    self.update_generic_progress(self.unused_progress, self.lbl_unused_status, idx, total_files, "闲置审查")
                try:
                    fp = Path(r) / f
                    stat = fp.stat()
                    la = max(datetime.fromtimestamp(stat.st_mtime), datetime.fromtimestamp(stat.st_atime))
                    if la < cutoff:
                        found += 1
                        self.safe_ui_update(lambda fp=fp, sz=stat.st_size/1048576, d=(now-la).days, la=la: self.unused_tree.insert('', tk.END, values=(fp.name, f"闲置[{d}天]", f"{sz:.2f} MB", la.strftime('%Y-%m-%d'), str(fp))))
                except Exception:
                    pass
        self.safe_ui_update(lambda: self.lbl_unused_status.config(text=f"扫描完成：共发现 {found} 个闲置文件。"))

    def clean_junk(self, target=None, ext_filter=None):
        if target is None or target == '':
            target = self.get_selected_path(self.clean_combo)
        target = self.normalize_path(target)
        if not target or not os.path.exists(target):
            self.safe_ui_update(lambda: self.lbl_status.config(text="错误：请选择有效目标路径。"))
            return
        if ext_filter is None:
            ext_filter = getattr(self, 'clean_ext_combo', None)
            if ext_filter is not None:
                ext_filter = self.clean_ext_combo.get()
            else:
                ext_filter = "所有垃圾"
        if ext_filter == "所有垃圾":
            suffixes = [ext.lower() for ext in self.junk_extensions]
        else:
            suffixes = [ext_filter.lower()]
        total = sum(len(fls) for r, d, fls in os.walk(target))
        released, idx = 0, 0
        for r, d, fls in os.walk(target):
            if self.cancel_event.is_set():
                self.safe_ui_update(lambda: self.lbl_status.config(text=f"清理已取消，已释放 {released/1048576:.2f} MB。"))
                return
            for f in fls:
                idx += 1
                if idx % 50 == 0:
                    self.update_generic_progress(self.progress, self.lbl_status, idx, total, "基础清理")
                fp = Path(r) / f
                if fp.suffix.lower() in suffixes:
                    try:
                        released += fp.stat().st_size
                        fp.unlink()
                        self.append_log(f"已删除垃圾文件: {fp}")
                    except Exception:
                        self.append_log(f"删除失败: {fp}")
        self.safe_ui_update(lambda: self.lbl_status.config(text=f"清理完成：释放空间 {released/1048576:.2f} MB。"))
        self.safe_ui_update(lambda: messagebox.showinfo("完成", f"释放空间: {released/1048576:.2f} MB"))

    def clean_duplicates(self):
        target = self.normalize_path(self.get_selected_path(self.clean_combo))
        if not target or not os.path.exists(target):
            self.safe_ui_update(lambda: self.lbl_status.config(text="错误：请选择有效目标路径。"))
            return
        size_dict = {}
        for r, d, fls in os.walk(target):
            for f in fls:
                fp = Path(r) / f
                try:
                    size_dict.setdefault(fp.stat().st_size, []).append(fp)
                except Exception:
                    pass
        candidates = {sz: paths for sz, paths in size_dict.items() if len(paths) > 1 and sz > 0}
        candidate_count = sum(len(paths) for paths in candidates.values())
        if candidate_count == 0:
            self.safe_ui_update(lambda: self.lbl_status.config(text="未发现重复文件。"))
            return
        hashes, duplicates, processed = {}, [], 0
        for size, paths in candidates.items():
            for file_path in paths:
                processed += 1
                self.update_generic_progress(self.progress, self.lbl_status, processed, candidate_count, "重复哈希校验")
                try:
                    str_path, mtime = str(file_path), file_path.stat().st_mtime
                    h = self.db.get_hash(str_path, size, mtime)
                    if not h:
                        hasher = hashlib.md5()
                        with open(file_path, 'rb') as f:
                            for chunk in iter(lambda: f.read(8192), b''):
                                hasher.update(chunk)
                        h = hasher.hexdigest()
                        self.db.set_hash(str_path, size, mtime, h)
                    if h in hashes:
                        duplicates.append((file_path, hashes[h]))
                    else:
                        hashes[h] = file_path
                except Exception:
                    pass
        if not duplicates:
            self.safe_ui_update(lambda: self.lbl_status.config(text="未发现重复文件。"))
            return
        duplicate_count = len(duplicates)
        def _del():
            deleted = 0
            for dup, orig in duplicates:
                if messagebox.askyesnocancel("强力去重", f"源文件:\n{orig}\n\n重复文件:\n{dup}\n\n删除该重复文件?") is True:
                    try:
                        dup.unlink()
                        deleted += 1
                    except Exception:
                        pass
            messagebox.showinfo("重复去重", f"处理完成：共删除 {deleted} 个重复文件。")
        self.safe_ui_update(_del)

    def clean_old_files(self, target=None):
        if target is None or target == '':
            target = self.get_selected_path(self.clean_combo)
        target = self.normalize_path(target)
        if not target or not os.path.exists(target):
            self.safe_ui_update(lambda: self.lbl_status.config(text="错误：请选择有效目标路径。"))
            return
        try:
            days = int(self.old_days_spin.get())
        except ValueError:
            days = 180
        cutoff = datetime.now() - timedelta(days=days)
        total = sum(len(fls) for r, d, fls in os.walk(target))
        removed, idx = 0, 0
        for r, d, fls in os.walk(target):
            if self.cancel_event.is_set():
                self.safe_ui_update(lambda: self.lbl_status.config(text=f"操作已取消，已删除 {removed} 个旧文件。"))
                return
            for f in fls:
                idx += 1
                if idx % 50 == 0:
                    self.update_generic_progress(self.progress, self.lbl_status, idx, total, "旧文件清理")
                try:
                    fp = Path(r) / f
                    mtime = datetime.fromtimestamp(fp.stat().st_mtime)
                    if mtime < cutoff:
                        fp.unlink()
                        removed += 1
                        self.append_log(f"已删除旧文件: {fp}")
                except Exception:
                    pass
        self.safe_ui_update(lambda: self.lbl_status.config(text=f"清理完成：已删除 {removed} 个 {days} 天前文件。"))
        self.safe_ui_update(lambda: messagebox.showinfo("完成", f"已删除 {removed} 个 {days} 天前文件。"))

    def clean_empty_folders(self, target=None):
        if target is None or target == '':
            target = self.get_selected_path(self.clean_combo)
        target = self.normalize_path(target)
        if not target or not os.path.exists(target):
            self.safe_ui_update(lambda: self.lbl_status.config(text="错误：请选择有效目标路径。"))
            return
        removed = 0
        for root, dirs, files in os.walk(target, topdown=False):
            if self.cancel_event.is_set():
                self.safe_ui_update(lambda: self.lbl_status.config(text=f"操作已取消，已删除 {removed} 个空文件夹。"))
                return
            try:
                if not os.listdir(root):
                    os.rmdir(root)
                    removed += 1
                    self.append_log(f"已删除空文件夹: {root}")
            except Exception:
                pass
        self.safe_ui_update(lambda: self.lbl_status.config(text=f"清理完成：已删除 {removed} 个空文件夹。"))
        self.safe_ui_update(lambda: messagebox.showinfo("完成", f"已删除 {removed} 个空文件夹。"))

    def refresh_processes(self):
        self.safe_ui_update(lambda: [self.proc_tree.delete(i) for i in self.proc_tree.get_children()])
        def _fetch():
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    if proc.info['exe']:
                        self.safe_ui_update(lambda p=proc: self.proc_tree.insert('', tk.END, values=(p.info['pid'], p.info['name'], p.info['exe'])))
                except: pass
        threading.Thread(target=_fetch, daemon=True).start()

    def kill_selected_process(self):
        sel = self.proc_tree.selection()
        if sel and messagebox.askyesno("警告", "确定中止此进程吗？"):
            try:
                psutil.Process(int(self.proc_tree.item(sel, 'values')[0])).terminate()
                self.refresh_processes()
            except Exception as e: messagebox.showerror("错误", str(e))

    def safe_ui_update(self, func, *args, **kwargs):
        self.root.after(0, lambda: func(*args, **kwargs))

    def append_log(self, message):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.safe_ui_update(_append)

    def update_generic_progress(self, p_bar, lbl, current, total, text):
        percent = int((current / total) * 100) if total > 0 else 100
        self.safe_ui_update(lambda: (p_bar.config(value=percent), lbl.config(text=f"{text}: {percent}% ({current}/{total})")))

    def trigger_cancel(self):
        self.cancel_event.set()

    def normalize_path(self, path):
        if not path:
            return ''
        normalized = os.path.normpath(path)
        # Windows drive-only path like C: should be treated as root C:\\
        if len(normalized) == 2 and normalized[1] == ':':
            normalized = normalized + os.sep
        return normalized

    def browse_clean_target(self):
        selected = filedialog.askdirectory(initialdir=os.environ.get('USERPROFILE', 'C:\\'))
        if selected:
            self.clean_path_entry.delete(0, tk.END)
            self.clean_path_entry.insert(0, selected)

    def browse_search_target(self):
        selected = filedialog.askdirectory(initialdir=os.environ.get('USERPROFILE', 'C:\\'))
        if selected:
            self.search_path_entry.delete(0, tk.END)
            self.search_path_entry.insert(0, selected)

    def browse_unused_target(self):
        selected = filedialog.askdirectory(initialdir=os.environ.get('USERPROFILE', 'C:\\'))
        if selected:
            # if user selected a manual path, populate the unused path entry
            if hasattr(self, 'unused_path_entry'):
                self.unused_path_entry.delete(0, tk.END)
                self.unused_path_entry.insert(0, selected)

    def get_selected_path(self, combo):
        return self.smart_paths_dict.get(combo.get(), '')

    def start_thread(self, target_func, buttons_to_disable, cancel_button=None):
        self.cancel_event.clear()
        for btn in buttons_to_disable:
            btn.config(state=tk.DISABLED)
        if cancel_button is not None:
            cancel_button.config(state=tk.NORMAL)
        def wrapper():
            try:
                target_func()
            finally:
                self.safe_ui_update(lambda: [b.config(state=tk.NORMAL) for b in buttons_to_disable] + ([cancel_button.config(state=tk.DISABLED)] if cancel_button is not None else []))
        threading.Thread(target=wrapper, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = FullyAutomatedOptimizerGUI(root)
    root.mainloop()