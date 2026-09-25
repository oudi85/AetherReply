"""Favorite-sticker annotation window for the dashboard and CLI."""

import json
import threading
import tkinter as tk
from contextlib import closing
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk

from .core import stickers, store
from .ui import fx, theme as T


def open_labeler(cfg: dict, con, *, parent=None, on_close=None,
                 demo: bool = False):
    root = tk.Toplevel(parent) if parent else tk.Tk()
    if parent is None:
        T.init(root)
    root.title("收藏表情人工标注" + ("（演示模式）" if demo else ""))
    width = min(T.S(1030), int(root.winfo_screenwidth() * .9))
    height = min(T.S(710), int(root.winfo_screenheight() * .85))
    root.geometry(f"{width}x{height}")
    root.minsize(min(T.S(850), width), min(T.S(600), height))
    if parent is not None:
        parent.update_idletasks()
        root.geometry(f"+{parent.winfo_rootx() + max(0, (parent.winfo_width()-width)//2)}"
                      f"+{parent.winfo_rooty() + max(0, (parent.winfo_height()-height)//2)}")
    root.configure(bg=T.BG)
    fx.set_caption(root, T.BG)
    style = ttk.Style(root)
    style.configure("Sticker.TFrame", background=T.BG)
    style.configure("Sticker.TLabel", background=T.BG, foreground=T.TEXT,
                    font=T.font(11.5))
    style.configure("Sticker.TButton", font=T.font(11.5), padding=6)
    style.configure("Sticker.Treeview", background=T.SURFACE, fieldbackground=T.SURFACE,
                    foreground=T.TEXT_2, font=T.font(11.5), rowheight=T.S(68), borderwidth=0)
    style.map("Sticker.Treeview", background=[("selected", T.INDIGO_SOFT)],
              foreground=[("selected", T.TEXT)])
    chosen = {"md5": None, "photo": None}
    rows = []
    row_by_md5 = {}
    thumbnails = {}
    blank = Image.new("RGBA", (T.S(58), T.S(58)), T.SURFACE_2)
    pen = ImageDraw.Draw(blank)
    pen.rounded_rectangle((2, 2, blank.width - 3, blank.height - 3),
                          radius=T.S(8), outline=T.BORDER, width=2)
    pen.ellipse((T.S(23), T.S(23), T.S(35), T.S(35)), fill=T.SUBTLE)
    placeholder = ImageTk.PhotoImage(blank, master=root)
    busy = {"download": False, "scan": False, "select_job": None, "closed": False,
            "failed": set()}

    def close():
        if busy["closed"]:
            return
        busy["closed"] = True
        if busy["select_job"] is not None:
            root.after_cancel(busy["select_job"])
        root.destroy()
        if on_close:
            on_close()

    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<Escape>", lambda _: close())

    left = ttk.Frame(root, padding=12, style="Sticker.TFrame")
    left.pack(side="left", fill="y")
    right = ttk.Frame(root, padding=12, style="Sticker.TFrame")
    right.pack(side="left", fill="both", expand=True)
    ttk.Label(left, text="收藏表情（最近在前）", style="Sticker.TLabel").pack(anchor="w")
    filters = ttk.Combobox(left, state="readonly",
                           values=("全部", "未标注", "已标注"), width=30)
    filters.set("全部")
    filters.pack(fill="x", pady=(4, 7))
    listing = ttk.Treeview(left, show="tree", selectmode="browse",
                           style="Sticker.Treeview", height=12)
    listing.column("#0", width=T.S(280), stretch=True)
    listing.pack(side="left", fill="both", expand=True)
    scroll = ttk.Scrollbar(left, orient="vertical", command=listing.yview)
    scroll.pack(side="left", fill="y")
    listing.configure(yscrollcommand=scroll.set)

    preview_box = tk.Frame(right, bg=T.SURFACE_2, height=T.S(205))
    preview_box.pack(fill="x")
    preview_box.pack_propagate(False)
    preview = tk.Label(preview_box, text="选择左侧表情查看预览", anchor="center",
                       bg=T.SURFACE_2, fg=T.MUTED, font=T.font(12))
    preview.pack(fill="both", expand=True)
    info = ttk.Label(right, text="", wraplength=600, justify="left", style="Sticker.TLabel")
    info.pack(fill="x", pady=(8, 12))
    ttk.Label(right, text="分类（可以把同一张表情放入多个分类）",
              style="Sticker.TLabel").pack(anchor="w")
    category = ttk.Combobox(right, state="readonly")
    category.pack(fill="x", pady=4)
    ttk.Label(right, text="具体用途：描述这张图适合什么语境，DS 会根据它选择",
              style="Sticker.TLabel").pack(anchor="w")
    description = ttk.Entry(right)
    description.pack(fill="x", pady=4)
    ttk.Label(right, text="这张表情已确认的分类", style="Sticker.TLabel").pack(
        anchor="w", pady=(12, 0))
    assigned = tk.Listbox(right, height=3, exportselection=False,
                          bg=T.SURFACE, fg=T.TEXT_2, selectbackground=T.INDIGO_SOFT,
                          selectforeground=T.TEXT, relief="flat", bd=0,
                          font=T.font(11.5))
    assigned.pack(fill="x", pady=4)
    status = ttk.Label(right, text="视觉模型猜测的标签仅作参考，不会直接允许自动发送",
                       style="Sticker.TLabel")
    status.pack(fill="x", pady=8)

    def refresh_categories():
        names = [r[0] for r in con.execute("SELECT name FROM sticker_categories ORDER BY name")]
        category["values"] = names
        if category.get() not in names:
            category.set(names[0] if names else "")

    def refresh_rows(keep_md5=None):
        nonlocal rows, row_by_md5
        all_rows = con.execute("SELECT s.md5,s.caption,s.tags_json,s.asset_path,"
                               "s.favorite_order,COUNT(a.category) AS assigned_count "
                               "FROM stickers s LEFT JOIN sticker_assignments a ON a.md5=s.md5 "
                               "WHERE s.favorite_order>=0 GROUP BY s.md5 "
                               "ORDER BY s.favorite_order DESC").fetchall()
        mode = filters.get()
        rows = [row for row in all_rows if mode == "全部" or
                (mode == "已标注") == bool(row["assigned_count"])]
        row_by_md5 = {row["md5"]: row for row in rows}
        listing.delete(*listing.get_children())
        thumbnails.clear()
        for index, row in enumerate(rows, 1):
            mark = "✓" if row["assigned_count"] else "·"
            path = Path(row["asset_path"]) if row["asset_path"] else None
            thumb = thumbnail(path) if path and path.is_file() else placeholder
            thumbnails[row["md5"]] = thumb
            label = row["caption"][:10] or row["md5"][:10]
            listing.insert("", "end", iid=row["md5"], text=f"{index:>3}  {mark}  {label}",
                           image=thumb)
        if rows:
            target = keep_md5 if keep_md5 in row_by_md5 else rows[0]["md5"]
            listing.selection_set(target)
            listing.see(target)
            selected()
        else:
            chosen["md5"] = None
            assigned.delete(0, "end")
            preview.configure(image="", text="没有符合筛选条件的表情")

    def thumbnail(path):
        try:
            with Image.open(path) as source:
                still = source.convert("RGBA")
                still.thumbnail((T.S(56), T.S(56)))
            tile = Image.new("RGBA", (T.S(58), T.S(58)), T.SURFACE)
            tile.alpha_composite(still, ((tile.width - still.width) // 2,
                                         (tile.height - still.height) // 2))
            return ImageTk.PhotoImage(tile, master=root)
        except (OSError, ValueError):
            return placeholder

    def show_image(path):
        try:
            with Image.open(path) as source:
                still = source.convert("RGBA")
            still.thumbnail((T.S(340), T.S(190)))
            photo = ImageTk.PhotoImage(still, master=root)
            chosen["photo"] = photo
            preview.configure(image=photo, text="")
        except (OSError, ValueError):
            chosen["photo"] = None
            preview.configure(image="", text="图片预览失败")

    def selected(_event=None):
        if busy["select_job"] is not None:
            root.after_cancel(busy["select_job"])
            busy["select_job"] = None
        selection = listing.selection()
        if not selection:
            return
        row = row_by_md5[selection[0]]
        md5 = chosen["md5"] = row["md5"]
        description.delete(0, "end")
        labels = stickers.annotations(con, md5)
        assigned.delete(0, "end")
        for label in labels:
            assigned.insert("end", f"{label['category']}：{label['description']}")
        hints = "、".join(json.loads(row["tags_json"])) or "无"
        info.configure(text=f"MD5：{md5}\n微信自带描述：{row['caption'] or '无'}"
                       f"\n视觉模型参考标签：{hints}")
        path = Path(row["asset_path"]) if row["asset_path"] else None
        if path and path.is_file():
            show_image(path)
        else:
            chosen["photo"] = None
            preview.configure(image="", text="图片正在加载…" if not demo else "演示图片未提供")
            if not demo and md5 not in busy["failed"]:
                if busy["select_job"] is not None:
                    root.after_cancel(busy["select_job"])
                busy["select_job"] = root.after(250, download_current)

    def add_category():
        name = simpledialog.askstring("新建分类", "分类名称（如：开心、无语、安慰）", parent=root)
        if name is None:
            return
        guidance = simpledialog.askstring("分类说明", "这个分类适合什么场景？", parent=root)
        if guidance is None:
            return
        try:
            stickers.save_category(con, name, guidance)
            refresh_categories()
            category.set(name.strip())
        except ValueError as exc:
            messagebox.showerror("分类未保存", str(exc), parent=root)

    def edit_category():
        name = category.get()
        if not name:
            return
        current = con.execute("SELECT guidance FROM sticker_categories WHERE name=?",
                              (name,)).fetchone()[0]
        guidance = simpledialog.askstring("修改分类说明", name, initialvalue=current, parent=root)
        if guidance is not None:
            stickers.save_category(con, name, guidance)

    def remove_category():
        name = category.get()
        if name and messagebox.askyesno("删除分类", f"删除「{name}」及其所有表情关联？",
                                         parent=root):
            stickers.delete_category(con, name)
            refresh_categories()
            refresh_rows(chosen["md5"])

    def save_assignment():
        if not chosen["md5"]:
            return False
        try:
            stickers.assign(con, chosen["md5"], category.get(), description.get())
            refresh_rows(chosen["md5"])
            status.configure(text="人工分类已保存；只有校准范围内的表情会进入 DS 候选")
            return True
        except ValueError as exc:
            messagebox.showerror("标注未保存", str(exc), parent=root)
            return False

    def remove_assignment():
        index = assigned.curselection()
        if not chosen["md5"] or not index:
            return
        name = stickers.annotations(con, chosen["md5"])[index[0]]["category"]
        stickers.unassign(con, chosen["md5"], name)
        refresh_rows(chosen["md5"])
        status.configure(text=f"已移除「{name}」分类")

    def edit_assignment(_event=None):
        index = assigned.curselection()
        if chosen["md5"] and index:
            label = stickers.annotations(con, chosen["md5"])[index[0]]
            category.set(label["category"])
            description.delete(0, "end")
            description.insert(0, label["description"])

    def download_current():
        busy["select_job"] = None
        md5 = chosen["md5"]
        if not md5 or demo or busy["download"]:
            return
        busy["download"] = True
        status.configure(text="正在下载并校验当前图片…")
        button_download.configure(state="disabled")
        db_path = Path(con.execute("PRAGMA database_list").fetchone()[2])

        def work():
            try:
                with closing(store.connect(db_path)) as worker_con:
                    path = stickers.download(cfg, worker_con, md5)
                try:
                    root.after(0, lambda: downloaded(md5, path, ""))
                except (tk.TclError, RuntimeError):
                    pass
            except Exception as exc:
                try:
                    root.after(0, lambda reason=str(exc): downloaded(md5, None, reason))
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=work, daemon=True).start()

    def downloaded(md5, path, error):
        if busy["closed"]:
            return
        busy["download"] = False
        button_download.configure(state="normal")
        if error:
            busy["failed"].add(md5)
            status.configure(text=f"下载失败：{error}")
        else:
            refresh_rows(chosen["md5"])
            status.configure(text="图片已下载并核对 MD5，可填写人工分类")
        if error and chosen["md5"] != md5:
            selected()

    def scan_current():
        if demo or busy["scan"]:
            return
        busy["scan"] = True
        button_scan.configure(state="disabled")
        status.configure(text="正在同步本机收藏表情…")
        db_path = Path(con.execute("PRAGMA database_list").fetchone()[2])

        def work():
            try:
                with closing(store.connect(db_path)) as worker_con:
                    result = stickers.scan(cfg, worker_con)
                try:
                    root.after(0, lambda: scanned(result, ""))
                except (tk.TclError, RuntimeError):
                    pass
            except Exception as exc:
                try:
                    root.after(0, lambda reason=str(exc): scanned(None, reason))
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=work, daemon=True).start()

    def scanned(result, error):
        if busy["closed"]:
            return
        busy["scan"] = False
        button_scan.configure(state="normal")
        if error:
            status.configure(text=f"同步失败：{error}")
        else:
            refresh_rows(chosen["md5"])
            status.configure(text=f"已同步 {result['indexed']} 张收藏表情")

    category_buttons = ttk.Frame(right, style="Sticker.TFrame")
    category_buttons.pack(fill="x", pady=(5, 2))
    ttk.Button(category_buttons, text="新建分类", command=add_category,
               style="Sticker.TButton").pack(side="left", padx=3)
    ttk.Button(category_buttons, text="修改说明", command=edit_category,
               style="Sticker.TButton").pack(side="left", padx=3)
    ttk.Button(category_buttons, text="删除分类", command=remove_category,
               style="Sticker.TButton").pack(side="left", padx=3)
    assignment_buttons = ttk.Frame(right, style="Sticker.TFrame")
    assignment_buttons.pack(fill="x", pady=(2, 5))
    ttk.Button(assignment_buttons, text="保存标注", command=save_assignment,
               style="Sticker.TButton").pack(side="left", padx=3)
    ttk.Button(assignment_buttons, text="移除关联", command=remove_assignment,
               style="Sticker.TButton").pack(side="left", padx=3)
    ttk.Button(assignment_buttons, text="保存并下一张", command=lambda: save_and_next(),
               style="Sticker.TButton").pack(side="left", padx=3)
    button_scan = ttk.Button(right, text="同步收藏", command=scan_current,
                             style="Sticker.TButton")
    button_scan.pack(side="left", pady=4, padx=(0, 8))
    button_download = ttk.Button(right, text="重试加载图片", command=lambda: retry_download(),
                                 style="Sticker.TButton")
    button_download.pack(side="left", pady=4)
    if demo:
        button_scan.configure(state="disabled")
        button_download.configure(state="disabled")

    def save_and_next():
        selection = listing.selection()
        index = next((i for i, row in enumerate(rows)
                      if selection and row["md5"] == selection[0]), None)
        saved_md5 = chosen["md5"]
        if not save_assignment() or index is None:
            return
        next_index = index if saved_md5 not in row_by_md5 else index + 1
        if next_index < len(rows):
            target = rows[next_index]["md5"]
            listing.selection_set(target)
            listing.see(target)
            selected()

    def retry_download():
        if chosen["md5"]:
            busy["failed"].discard(chosen["md5"])
        download_current()
    listing.bind("<<TreeviewSelect>>", selected)
    filters.bind("<<ComboboxSelected>>", lambda _: refresh_rows(chosen["md5"]))
    assigned.bind("<<ListboxSelect>>", edit_assignment)
    root.bind("<Control-s>", lambda _: save_assignment())
    root.bind("<Control-Return>", lambda _: save_and_next())
    refresh_categories()
    refresh_rows()
    if parent is None:
        root.mainloop()
    return root
