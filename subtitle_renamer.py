from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from renamer_core import (
    ItemStatus,
    RenamePlan,
    ScanResult,
    build_rename_plan,
    execute_rename_plan,
    scan_directory,
    selected_numeric_field,
)
from subtitle_extractor import ExtractorWindow


STATUS_TEXT = {
    ItemStatus.READY: "可重命名",
    ItemStatus.ALREADY_CORRECT: "无需更改",
    ItemStatus.UNMATCHED: "无匹配视频",
    ItemStatus.INVALID: "无法提取集数",
    ItemStatus.CONFLICT: "冲突",
}


class SubtitleRenamerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("字幕批量重命名")
        self.root.geometry("1100x720")
        self.root.minsize(820, 600)

        self.folder_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="请选择包含视频和字幕的文件夹。")
        self.video_sample_var = tk.StringVar()
        self.subtitle_sample_var = tk.StringVar()
        self.video_field_var = tk.StringVar(value="尚未确认集数字段")
        self.subtitle_field_var = tk.StringVar(value="尚未确认集数字段")
        self.detail_var = tk.StringVar(value="扫描后选择样例，并分别框选完整的集数数字。")

        self.scan_result = ScanResult((), ())
        self.video_field_index: int | None = None
        self.subtitle_field_index: int | None = None
        self.plan: RenamePlan | None = None
        self.extractor_window: ExtractorWindow | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        folder_row = ttk.Frame(outer)
        folder_row.grid(row=0, column=0, sticky="ew")
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="处理文件夹").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(folder_row, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(folder_row, text="选择...", command=self.choose_folder).grid(row=0, column=2, padx=8)
        ttk.Button(folder_row, text="扫描", command=self.scan).grid(row=0, column=3)

        ttk.Label(outer, textvariable=self.summary_var).grid(
            row=1, column=0, sticky="w", pady=(8, 10)
        )

        samples = ttk.Frame(outer)
        samples.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        samples.columnconfigure(0, weight=1)
        samples.columnconfigure(1, weight=1)
        self.video_sample_combo, self.video_name_entry = self._build_sample_panel(
            samples,
            0,
            "媒体文件样例",
            self.video_sample_var,
            self.video_field_var,
            self.confirm_video_field,
        )
        self.subtitle_sample_combo, self.subtitle_name_entry = self._build_sample_panel(
            samples,
            1,
            "字幕文件样例",
            self.subtitle_sample_var,
            self.subtitle_field_var,
            self.confirm_subtitle_field,
        )
        self.video_sample_combo.bind("<<ComboboxSelected>>", self._video_sample_changed)
        self.subtitle_sample_combo.bind("<<ComboboxSelected>>", self._subtitle_sample_changed)

        preview_frame = ttk.Frame(outer)
        preview_frame.grid(row=3, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(preview_frame)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(actions, text="生成预览", command=self.preview).pack(side=tk.LEFT)
        self.rename_button = ttk.Button(
            actions, text="执行重命名", command=self.execute, state=tk.DISABLED
        )
        self.rename_button.pack(side=tk.LEFT, padx=8)
        ttk.Label(actions, textvariable=self.detail_var).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="弹幕/字幕提取...", command=self.open_extractor).pack(side=tk.RIGHT)

        columns = ("status", "episode", "source", "target", "detail")
        self.tree = ttk.Treeview(preview_frame, columns=columns, show="headings")
        headings = {
            "status": "状态",
            "episode": "集数",
            "source": "原字幕文件名",
            "target": "新文件名",
            "detail": "说明",
        }
        widths = {"status": 95, "episode": 60, "source": 320, "target": 320, "detail": 220}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, stretch=column not in {"status", "episode"})
        self.tree.tag_configure(ItemStatus.READY.value, foreground="#176b31")
        self.tree.tag_configure(ItemStatus.CONFLICT.value, foreground="#b3261e")
        self.tree.tag_configure(ItemStatus.INVALID.value, foreground="#9a5b00")
        self.tree.tag_configure(ItemStatus.UNMATCHED.value, foreground="#6b5c00")

        y_scroll = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll.grid(row=2, column=0, sticky="ew")

        ttk.Label(
            outer,
            text="程序只处理当前文件夹中的字幕文件；预览有冲突时不会执行。",
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

    def _build_sample_panel(
        self,
        parent: ttk.Frame,
        column: int,
        title: str,
        sample_var: tk.StringVar,
        field_var: tk.StringVar,
        command: object,
    ) -> tuple[ttk.Combobox, ttk.Entry]:
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 5) if column == 0 else (5, 0))
        frame.columnconfigure(0, weight=1)

        combo = ttk.Combobox(frame, textvariable=sample_var, state="readonly")
        combo.grid(row=0, column=0, columnspan=2, sticky="ew")
        entry = ttk.Entry(frame, textvariable=sample_var, state="readonly", exportselection=False)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(frame, text="确认所选集数", command=command).grid(row=2, column=0, sticky="w")
        ttk.Label(frame, textvariable=field_var).grid(row=2, column=1, sticky="e", padx=(8, 0))
        return combo, entry

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含视频和字幕的文件夹")
        if selected:
            self.folder_var.set(selected)
            self.scan()

    def open_extractor(self) -> None:
        if self.extractor_window is not None and self.extractor_window.winfo_exists():
            self.extractor_window.deiconify()
            self.extractor_window.lift()
            self.extractor_window.focus_set()
            return
        self.extractor_window = ExtractorWindow(self.root)
        self.extractor_window.lift()

    def scan(self) -> None:
        raw_folder = self.folder_var.get().strip()
        if not raw_folder:
            messagebox.showwarning("未选择文件夹", "请先选择处理文件夹。")
            return
        try:
            self.scan_result = scan_directory(Path(raw_folder))
        except (OSError, ValueError) as exc:
            messagebox.showerror("扫描失败", str(exc))
            return

        video_names = [path.name for path in self.scan_result.videos]
        subtitle_names = [path.name for path in self.scan_result.subtitles]
        self.video_sample_combo.configure(values=video_names)
        self.subtitle_sample_combo.configure(values=subtitle_names)
        self.video_sample_var.set(video_names[0] if video_names else "")
        self.subtitle_sample_var.set(subtitle_names[0] if subtitle_names else "")
        self._reset_field_selections()
        self._clear_preview()
        self.summary_var.set(
            f"检测到 {len(video_names)} 个媒体文件、{len(subtitle_names)} 个字幕文件（仅当前目录）。"
        )
        if not video_names or not subtitle_names:
            self.detail_var.set("需要同时存在受支持的媒体文件和字幕文件。")
        else:
            self.detail_var.set("在两个样例文件名中分别框选完整集数数字，然后点击确认。")

    def _reset_field_selections(self) -> None:
        self.video_field_index = None
        self.subtitle_field_index = None
        self.video_field_var.set("尚未确认集数字段")
        self.subtitle_field_var.set("尚未确认集数字段")

    def _video_sample_changed(self, _event: tk.Event[tk.Misc]) -> None:
        self.video_field_index = None
        self.video_field_var.set("尚未确认集数字段")
        self._clear_preview()

    def _subtitle_sample_changed(self, _event: tk.Event[tk.Misc]) -> None:
        self.subtitle_field_index = None
        self.subtitle_field_var.set("尚未确认集数字段")
        self._clear_preview()

    def _confirm_field(
        self, entry: ttk.Entry, filename: str, kind: str
    ) -> tuple[int, str] | None:
        if not filename:
            messagebox.showwarning("没有样例", f"没有可用的{kind}文件样例。")
            return None
        try:
            if not entry.selection_present():
                raise ValueError("请先在完整文件名中拖动框选集数数字。")
            start = int(entry.index(tk.SEL_FIRST))
            end = int(entry.index(tk.SEL_LAST))
            return selected_numeric_field(filename, start, end)
        except (tk.TclError, ValueError) as exc:
            messagebox.showwarning("选区无效", str(exc))
            return None

    def confirm_video_field(self) -> None:
        result = self._confirm_field(
            self.video_name_entry, self.video_sample_var.get(), "媒体"
        )
        if result:
            self.video_field_index, text = result
            self.video_field_var.set(f"第 {self.video_field_index + 1} 个数字字段：{text}")
            self._clear_preview()

    def confirm_subtitle_field(self) -> None:
        result = self._confirm_field(
            self.subtitle_name_entry, self.subtitle_sample_var.get(), "字幕"
        )
        if result:
            self.subtitle_field_index, text = result
            self.subtitle_field_var.set(f"第 {self.subtitle_field_index + 1} 个数字字段：{text}")
            self._clear_preview()

    def _clear_preview(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.plan = None
        self.rename_button.configure(state=tk.DISABLED)

    def preview(self) -> None:
        if self.video_field_index is None or self.subtitle_field_index is None:
            messagebox.showwarning("尚未选择集数", "请分别框选并确认媒体与字幕的集数字段。")
            return
        self.plan = build_rename_plan(
            self.scan_result.videos,
            self.scan_result.subtitles,
            self.video_field_index,
            self.subtitle_field_index,
        )
        self._render_plan(self.plan)

    def _render_plan(self, plan: RenamePlan) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for item in plan.items:
            self.tree.insert(
                "",
                tk.END,
                values=(
                    STATUS_TEXT[item.status],
                    "" if item.episode is None else item.episode,
                    item.source.name,
                    "" if item.target is None else item.target.name,
                    item.detail,
                ),
                tags=(item.status.value,),
            )

        ready_count = len(plan.ready_items)
        warning_count = sum(
            item.status in {ItemStatus.INVALID, ItemStatus.UNMATCHED} for item in plan.items
        ) + len(plan.warnings)
        if plan.errors:
            self.detail_var.set(f"发现 {len(plan.errors)} 个冲突，不能执行。")
            messagebox.showerror("预检发现冲突", "\n".join(plan.errors[:8]))
        else:
            self.detail_var.set(f"可重命名 {ready_count} 个，跳过或提示 {warning_count} 个。")
        state = tk.NORMAL if plan.can_execute and ready_count else tk.DISABLED
        self.rename_button.configure(state=state)

    def execute(self) -> None:
        if self.plan is None or not self.plan.ready_items:
            return
        count = len(self.plan.ready_items)
        if not messagebox.askyesno("确认重命名", f"将重命名 {count} 个字幕文件，是否继续？"):
            return

        result = execute_rename_plan(self.plan)
        if result.success:
            messagebox.showinfo("完成", f"已成功重命名 {len(result.renamed)} 个字幕文件。")
            self.scan()
            return

        message = f"重命名失败：{result.error}"
        if result.renamed:
            message += f"\n失败前已处理 {len(result.renamed)} 个，成功回滚 {len(result.rolled_back)} 个。"
        if result.rollback_errors:
            message += "\n以下文件未能回滚：\n" + "\n".join(result.rollback_errors)
        messagebox.showerror("执行失败", message)
        self.scan()


def main() -> None:
    root = tk.Tk()
    SubtitleRenamerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
