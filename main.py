import os
import sys
import threading
import hashlib
import subprocess
import sqlite3
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import psutil
from datetime import datetime, timedelta

class LocalDatabaseManager:
    """本地 SQLite 缓存与索引双引擎（含智能分析扩展）"""
    def __init__(self, db_path="optimizer_cache.db"):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
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
                    filepath TEXT PRIMARY KEY,
                    filename TEXT,
                    extension TEXT,
                    size INTEGER,
                    mtime REAL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_filename ON global_index(filename)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_ext ON global_index(extension)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_global_size ON global_index(size)')

    def clear_global_index(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM global_index")

    def batch_insert_index(self, data_list):
        if not data_list: return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany('INSERT OR REPLACE INTO global_index VALUES (?, ?, ?, ?, ?)', data_list)

    def search_global_index(self, keyword, ext_filter="所有格式", limit=800):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            query = "SELECT filename, extension, size, mtime, filepath FROM global_index WHERE filename LIKE ?"
            params = [f"%{keyword}%"]
            if ext_filter and ext_filter != "所有格式":
                query += " AND extension = ?"
                params.append(ext_filter.lower())
            query += " ORDER BY size DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, params)
            return cursor.fetchall()

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
        3. 重置其他表头状态
        """
        # 1. 恢复其他列的原始标题状态（去除图标）
        for c in tree['columns']:
            text = tree.heading(c, 'text')
            if ' ▲' in text or ' ▼' in text:
                tree.heading(c, text=text.replace(' ▲', '').replace(' ▼', ''))

        # 2. 对当前点击列执行排序
        data = [(tree.set(child, col), child) for child in tree.get_children('')]
        
        # 智能类型判断
        try:
            # 尝试按数值排序（处理 KB/MB/Byte 等后缀）
            data.sort(key=lambda t: float(t[0].split()[0]), reverse=reverse)
        except (ValueError, IndexError):
            # 文本排序
            data.sort(reverse=reverse)

        # 3. 移动节点
        for index, (val, child) in enumerate(data):
            tree.move(child, '', index)

        # 4. 更新当前列标题为高亮标识
        new_text = f"{tree.heading(col, 'text')} {'▲' if reverse else '▼'}"
        tree.heading(col, text=new_text, command=lambda: self.sort_treeview(tree, col, not reverse))
        self.recommend_tree.heading('score', text='🔥 推荐指数', 
            command=lambda: self.sort_treeview(self.recommend_tree, 'score', False))
        self.recommend_tree.heading('size', text='文件体积', 
            command=lambda: self.sort_treeview(self.recommend_tree, 'size', False))
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
        self.btn_search = ttk.Button(filter_frame, text="⚡ 闪电搜索", command=self.execute_fast_search)
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
        self.unused_tree.bind("<Button-3>", lambda event: self.show_right_click_menu(event, self.unused_tree))
        scrollbar = ttk.Scrollbar(table_frame, command=self.unused_tree.yview)
        scrollbar.pack(fill=tk.Y, side=tk.RIGHT)
        self.unused_tree.config(yscrollcommand=scrollbar.set)

    def setup_clean_tab(self):
        path_frame = ttk.LabelFrame(self.clean_frame, text="智能推荐扫描区域", padding=10)
        path_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(path_frame, text="推荐目标:").pack(side=tk.LEFT, padx=5)
        self.clean_combo = ttk.Combobox(path_frame, values=list(self.smart_paths_dict.keys()), state="readonly", width=45)
        if self.smart_paths_dict: self.clean_combo.current(0)
        self.clean_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        btn_frame = ttk.Frame(self.clean_frame, padding=5)
        btn_frame.pack(fill=tk.X, padx=10)
        self.btn_clean = ttk.Button(btn_frame, text="🔍 一键清空目标内垃圾", command=lambda: self.start_thread(self.clean_junk, [self.btn_clean, self.btn_dup], cancel_button=self.btn_cancel_clean))
        self.btn_clean.pack(side=tk.LEFT, padx=5)
        self.btn_dup = ttk.Button(btn_frame, text="👯 极速扫描重复文件", command=lambda: self.start_thread(self.clean_duplicates, [self.btn_clean, self.btn_dup], cancel_button=self.btn_cancel_clean))
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
        self.refresh_processes()

    # ==================== 后台引擎逻辑 ====================
    def build_global_index(self):
        target_dir = self.search_path_entry.get().strip()
        if not target_dir or not os.path.exists(target_dir): return
        self.safe_ui_update(lambda: self.lbl_search_status.config(text="正在重新初始化全局索引库..."))
        self.db.clear_global_index()
        batch_pool, batch_size, total_indexed = [], 3000, 0
        self.safe_ui_update(lambda: [self.search_progress.config(mode='indeterminate'), self.search_progress.start(10)])
        
        for r, d, fls in os.walk(target_dir):
            if self.cancel_event.is_set(): break
            for f in fls:
                file_path = Path(r) / f
                try:
                    stat_info = file_path.stat()
                    batch_pool.append((str(file_path), f, file_path.suffix.lower(), stat_info.st_size, stat_info.st_mtime))
                    if len(batch_pool) >= batch_size:
                        self.db.batch_insert_index(batch_pool)
                        total_indexed += len(batch_pool)
                        batch_pool.clear()
                        self.safe_ui_update(lambda c=total_indexed: self.lbl_search_status.config(text=f"建立索引中... 已登记资产: {c}"))
                except: continue
        if batch_pool and not self.cancel_event.is_set():
            self.db.batch_insert_index(batch_pool)
            total_indexed += len(batch_pool)
        self.safe_ui_update(lambda: [self.search_progress.stop(), self.search_progress.config(mode='determinate', value=100)])
        self.safe_ui_update(lambda c=total_indexed: self.lbl_search_status.config(text=f"🎉 索引录入完毕，共记录: {c} 个文件。"))

    def execute_fast_search(self):
        keyword = self.search_keyword_entry.get().strip()
        ext_filter = self.search_ext_combo.get()
        if not keyword and ext_filter == "所有格式": return
        self.search_tree.delete(*self.search_tree.get_children())
        results = self.db.search_global_index(keyword, ext_filter)
        for name, ext, size, mtime, filepath in results:
            sz_str = f"{size/1048576:.2f} MB" if size>=1048576 else f"{size/1024:.1f} KB"
            mtime_str = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            self.search_tree.insert('', tk.END, values=(name, ext.upper(), sz_str, mtime_str, filepath))

    def scan_unused_files(self):
        target = self.get_selected_path(self.unused_combo)
        if not target or not os.path.exists(target): return
        try: threshold_days = int(self.days_spin.get())
        except: threshold_days = 180
        self.safe_ui_update(lambda: [self.unused_tree.delete(i) for i in self.unused_tree.get_children()])
        total_files = sum(len(fls) for r, d, fls in os.walk(target))
        if total_files == 0: return
        now, idx, found = datetime.now(), 0, 0
        cutoff = now - timedelta(days=threshold_days)
        for r, d, fls in os.walk(target):
            if self.cancel_event.is_set(): return
            for f in fls:
                idx += 1
                if idx % 50 == 0: self.update_generic_progress(self.unused_progress, self.lbl_unused_status, idx, total_files, "闲置审查")
                try:
                    fp = Path(r) / f
                    stat = fp.stat()
                    la = max(datetime.fromtimestamp(stat.st_mtime), datetime.fromtimestamp(stat.st_atime))
                    if la < cutoff:
                        found += 1
                        self.safe_ui_update(lambda fp=fp, sz=stat.st_size/1048576, d=(now-la).days, la=la: self.unused_tree.insert('', tk.END, values=(fp.name, f"闲置[{d}天]", f"{sz:.2f} MB", la.strftime('%Y-%m-%d'), str(fp))))
                except: pass

    def clean_junk(self):
        target = self.get_selected_path(self.clean_combo)
        if not target or not os.path.exists(target): return
        total = sum(len(fls) for r, d, fls in os.walk(target))
        released, idx = 0, 0
        for r, d, fls in os.walk(target):
            if self.cancel_event.is_set(): return
            for f in fls:
                idx += 1
                if idx % 50 == 0: self.update_generic_progress(self.progress, self.lbl_status, idx, total, "基础清理")
                fp = Path(r) / f
                if fp.suffix.lower() in self.junk_extensions:
                    try:
                        released += fp.stat().st_size
                        fp.unlink()
                    except: pass
        self.safe_ui_update(lambda: messagebox.showinfo("完成", f"释放空间: {released/1048576:.2f} MB"))

    def clean_duplicates(self):
        target = self.get_selected_path(self.clean_combo)
        if not target or not os.path.exists(target): return
        size_dict = {}
        for r, d, fls in os.walk(target):
            for f in fls:
                fp = Path(r) / f
                try: size_dict.setdefault(fp.stat().st_size, []).append(fp)
                except: pass
        candidates = {sz: paths for sz, paths in size_dict.items() if len(paths) > 1 and sz > 0}
        candidate_count = sum(len(paths) for paths in candidates.values())
        if candidate_count == 0: return
        hashes, duplicates, processed = {}, [], 0
        for size, paths in candidates.items():
            for file_path in paths:
                processed += 1
                self.update_generic_progress(self.progress, self.lbl_status, processed, candidate_count, "重复哈希校验")
                try:
                    str_path, mtime = str(file_path), file_path.stat().st_mtime
                    h = self.db.get_hash(str_path, size, mtime)
                    if not h:
                        with open(file_path, 'rb') as f: h = hashlib.md5(f.read(4096)).hexdigest()
                        self.db.set_hash(str_path, size, mtime, h)
                    if h in hashes: duplicates.append((file_path, hashes[h]))
                    else: hashes[h] = file_path
                except: pass
        if duplicates:
            def _del():
                for dup, orig in duplicates:
                    if messagebox.askyesnocancel("强力去重", f"删除克隆体?\n{dup}") is True:
                        try: dup.unlink()
                        except: pass
            self.safe_ui_update(_del)

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

    def safe_ui_update(self, func, *args, **kwargs): self.root.after(0, lambda: func(*args, **kwargs))
    def update_generic_progress(self, p_bar, lbl, current, total, text):
        percent = int((current / total) * 100) if total > 0 else 100
        self.safe_ui_update(lambda: (p_bar.config(value=percent), lbl.config(text=f"{text}: {percent}% ({current}/{total})")))
    def trigger_cancel(self): self.cancel_event.set()
    def get_selected_path(self, combo): return self.smart_paths_dict.get(combo.get(), '')
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