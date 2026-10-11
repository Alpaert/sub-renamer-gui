from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from extractor_core import (
    ExtractionItem,
    ExtractionPlan,
    ExtractionResult,
    ExtractionStatus,
    SlotReport,
    VideoTracks,
    build_extraction_plan,
    check_slot_consistency,
    describe_slot,
    describe_track,
    execute_extraction_plan,
    max_slot_count,
    missing_tools,
    probe_folder,
    slot_signature,
    slot_track,
)
from renamer_core import scan_directory


STATUS_TEXT = {
    ExtractionStatus.READY: "可提取",
    ExtractionStatus.ALREADY_EXISTS: "已存在",
    ExtractionStatus.MISSING_TRACK: "无此层级",
    ExtractionStatus.PROBE_FAILED: "读取失败",
    ExtractionStatus.CONFLICT: "冲突",
}

DIVERGENT_COLOR = "#9a5b00"


class ExtractorWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, standalone: bool = False) -> None:
        super().__init__(master)
        self.standalone = standalone
        self.title("弹幕/字幕批量提取")
        self.geometry("1200x840")
        self.minsize(960, 660)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.folder_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="请选择包含视频的文件夹，然后点击扫描。")
        self.detail_var = tk.StringVar()
        self.progress_var = tk.StringVar()
        self.fixed_label_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.use_output_dir_var = tk.IntVar(value=0)
        self.convert_ass_var = tk.IntVar(value=0)
        self.overwrite_var = tk.IntVar(value=0)
        self.all_var = tk.IntVar(value=0)

        self.scanned: tuple[VideoTracks, ...] = ()
        self.slot_vars: list[tk.IntVar] = []
        self.plan: ExtractionPlan | None = None
        self._consistency_notes: tuple[str, ...] = ()

        self._messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False

        self._build_ui()
        self._sync_output_dir_state()
        self.after(120, self._warn_about_missing_tools)

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        folder_row = ttk.Frame(outer)
        folder_row.grid(row=0, column=0, sticky="ew")
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="处理文件夹").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(folder_row, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(folder_row, text="选择...", command=self.choose_folder).grid(row=0, column=2, padx=8)
        self.scan_button = ttk.Button(folder_row, text="扫描", command=self.scan)
        self.scan_button.grid(row=0, column=3)

        ttk.Label(outer, textvariable=self.summary_var).grid(row=1, column=0, sticky="w", pady=(8, 10))

        slot_frame = ttk.LabelFrame(
            outer, text="要提取的轨道层级（同一系列顺序固定，勾选一次即可整批套用）", padding=10
        )
        slot_frame.grid(row=2, column=0, sticky="ew")
        slot_bar = ttk.Frame(slot_frame)
        slot_bar.pack(fill=tk.X)
        ttk.Checkbutton(
            slot_bar, text="全部字幕轨道", variable=self.all_var, command=self._toggle_all
        ).pack(side=tk.LEFT, padx=(0, 16))
        self.slot_holder = ttk.Frame(slot_bar)
        self.slot_holder.pack(side=tk.LEFT)

        consistency_frame = ttk.LabelFrame(
            outer, text="层级核对（琥珀色表示该视频与同批不一致）", padding=10
        )
        consistency_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        consistency_frame.columnconfigure(0, weight=1)
        self.consistency = ttk.Treeview(consistency_frame, columns=("video",), show="headings", height=6)
        self.consistency.tag_configure("divergent", foreground=DIVERGENT_COLOR)
        consistency_y = ttk.Scrollbar(consistency_frame, orient=tk.VERTICAL, command=self.consistency.yview)
        consistency_x = ttk.Scrollbar(consistency_frame, orient=tk.HORIZONTAL, command=self.consistency.xview)
        self.consistency.configure(yscrollcommand=consistency_y.set, xscrollcommand=consistency_x.set)
        self.consistency.grid(row=0, column=0, sticky="nsew")
        consistency_y.grid(row=0, column=1, sticky="ns")
        consistency_x.grid(row=1, column=0, sticky="ew")

        options = ttk.LabelFrame(outer, text="输出选项", padding=10)
        options.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        options.columnconfigure(0, weight=1)

        location = ttk.Frame(options)
        location.grid(row=0, column=0, sticky="ew")
        ttk.Radiobutton(
            location,
            text="与视频同目录",
            variable=self.use_output_dir_var,
            value=0,
            command=self._sync_output_dir_state,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            location,
            text="输出到指定文件夹",
            variable=self.use_output_dir_var,
            value=1,
            command=self._sync_output_dir_state,
        ).pack(side=tk.LEFT, padx=(12, 8))
        self.output_entry = ttk.Entry(location, textvariable=self.output_dir_var)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.output_button = ttk.Button(location, text="选择...", command=self.choose_output_dir)
        self.output_button.pack(side=tk.LEFT, padx=(8, 0))

        naming = ttk.Frame(options)
        naming.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(naming, text="统一命名标签").pack(side=tk.LEFT)
        ttk.Entry(naming, textvariable=self.fixed_label_var, width=18).pack(side=tk.LEFT, padx=8)
        ttk.Label(naming, text="（留空则用各轨道自己的标题）").pack(side=tk.LEFT)
        ttk.Checkbutton(naming, text="统一转 ASS", variable=self.convert_ass_var).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Checkbutton(naming, text="覆盖已存在文件", variable=self.overwrite_var).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        preview_frame = ttk.Frame(outer)
        preview_frame.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(preview_frame)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.preview_button = ttk.Button(actions, text="生成预览", command=self.preview)
        self.preview_button.pack(side=tk.LEFT)
        self.extract_button = ttk.Button(
            actions, text="开始提取", command=self.execute, state=tk.DISABLED
        )
        self.extract_button.pack(side=tk.LEFT, padx=8)
        ttk.Label(actions, textvariable=self.detail_var).pack(side=tk.LEFT, padx=(8, 0))

        columns = ("status", "video", "track", "target", "detail")
        self.tree = ttk.Treeview(preview_frame, columns=columns, show="headings")
        headings = {
            "status": "状态",
            "video": "视频文件",
            "track": "轨道",
            "target": "输出文件名",
            "detail": "说明",
        }
        widths = {"status": 95, "video": 250, "track": 190, "target": 280, "detail": 260}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column in {"target", "detail"},
            )
        self.tree.tag_configure(ExtractionStatus.READY.value, foreground="#176b31")
        self.tree.tag_configure(ExtractionStatus.ALREADY_EXISTS.value, foreground="#6b5c00")
        self.tree.tag_configure(ExtractionStatus.MISSING_TRACK.value, foreground=DIVERGENT_COLOR)
        self.tree.tag_configure(ExtractionStatus.PROBE_FAILED.value, foreground="#b3261e")
        self.tree.tag_configure(ExtractionStatus.CONFLICT.value, foreground="#b3261e")

        tree_y = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.tree.yview)
        tree_x = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        tree_y.grid(row=1, column=1, sticky="ns")
        tree_x.grid(row=2, column=0, sticky="ew")

        progress_row = ttk.Frame(outer)
        progress_row.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(
            progress_row, text="取消", command=self.cancel, state=tk.DISABLED
        )
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(outer, textvariable=self.progress_var).grid(row=7, column=0, sticky="w", pady=(4, 0))

    def _sync_output_dir_state(self) -> None:
        state = tk.NORMAL if self.use_output_dir_var.get() else tk.DISABLED
        self.output_entry.configure(state=state)
        self.output_button.configure(state=state)

    def _warn_about_missing_tools(self) -> None:
        missing = missing_tools()
        if missing:
            messagebox.showwarning(
                "缺少必要程序",
                "未找到 " + "、".join(missing) + "。\n"
                "请安装 ffmpeg 并加入 PATH（或放到 C:\\ffmpeg\\bin\\），否则无法读取或提取字幕轨。",
            )

    # ------------------------------------------------------------ 后台任务

    def _begin_job(self, text: str) -> None:
        self._busy = True
        self._cancel.clear()
        self._messages = queue.Queue()
        self.scan_button.configure(state=tk.DISABLED)
        self.preview_button.configure(state=tk.DISABLED)
        self.extract_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0, maximum=1)
        self.progress_var.set(text)
        self.after(100, self._poll)

    def _finish_job(self) -> None:
        self._busy = False
        self.scan_button.configure(state=tk.NORMAL)
        self.preview_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                current, total, text = payload  # type: ignore[misc]
                self.progress.configure(maximum=max(total, 1), value=current)
                self.progress_var.set(text)
            elif kind == "scanned":
                self._finish_job()
                self._apply_scan(payload)  # type: ignore[arg-type]
                return
            elif kind == "extracted":
                self._finish_job()
                self._report_extraction(payload)  # type: ignore[arg-type]
                return
        if self._busy:
            self.after(100, self._poll)

    def cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state=tk.DISABLED)
        self.progress_var.set("正在取消…")

    # -------------------------------------------------------------- 扫描

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含视频的文件夹")
        if selected:
            self.folder_var.set(selected)
            self.scan()

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(title="选择输出文件夹")
        if selected:
            self.output_dir_var.set(selected)

    def scan(self) -> None:
        if self._busy:
            return
        raw_folder = self.folder_var.get().strip()
        if not raw_folder:
            messagebox.showwarning("未选择文件夹", "请先选择处理文件夹。")
            return
        try:
            scan_result = scan_directory(Path(raw_folder))
        except (OSError, ValueError) as exc:
            messagebox.showerror("扫描失败", str(exc))
            return

        self.scanned = ()
        self._consistency_notes = ()
        self._rebuild_slots(())
        self._render_consistency(())
        self._clear_preview()

        videos = scan_result.videos
        if not videos:
            self.summary_var.set("当前文件夹中没有受支持的视频文件。")
            self.progress_var.set("")
            return

        self._begin_job("正在读取字幕轨道…")
        messages, cancel = self._messages, self._cancel

        def worker() -> None:
            def on_progress(index: int, total: int, video: Path) -> None:
                messages.put(("progress", (index, total, f"正在读取轨道：{video.name}")))

            messages.put(
                ("scanned", probe_folder(videos, on_progress=on_progress, should_cancel=cancel.is_set))
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_scan(self, scanned: tuple[VideoTracks, ...]) -> None:
        self.scanned = scanned
        self._rebuild_slots(scanned)
        self._render_consistency(scanned)
        self._clear_preview()

        failures = sum(1 for entry in scanned if entry.error)
        summary = f"检测到 {len(scanned)} 个视频，最多 {max_slot_count(scanned)} 条字幕轨。"
        if failures:
            summary += f"{failures} 个视频读取失败，见下表。"
        self.summary_var.set(summary)
        self.progress.configure(value=0, maximum=1)
        self.progress_var.set(
            " ".join(self._consistency_notes) if self._consistency_notes else "请勾选要提取的层级。"
        )

    def _toggle_all(self) -> None:
        value = 1 if self.all_var.get() else 0
        for variable in self.slot_vars:
            variable.set(value)

    def _rebuild_slots(self, scanned: tuple[VideoTracks, ...]) -> None:
        for child in self.slot_holder.winfo_children():
            child.destroy()
        self.slot_vars = []
        self.all_var.set(0)
        slot_count = max_slot_count(scanned)
        if slot_count == 0:
            ttk.Label(self.slot_holder, text="未检测到字幕轨道").pack(side=tk.LEFT)
            return
        for slot in range(1, slot_count + 1):
            variable = tk.IntVar(value=0)
            self.slot_vars.append(variable)
            ttk.Checkbutton(self.slot_holder, text=f"第 {slot} 条", variable=variable).pack(
                side=tk.LEFT, padx=(0, 8)
            )

    def selected_slots(self) -> tuple[int, ...]:
        return tuple(
            slot for slot, variable in enumerate(self.slot_vars, start=1) if variable.get()
        )

    def _render_consistency(self, scanned: tuple[VideoTracks, ...]) -> None:
        self.consistency.delete(*self.consistency.get_children())
        slot_count = max_slot_count(scanned)
        columns = ["video"] + [f"slot{slot}" for slot in range(1, slot_count + 1)]
        self.consistency.configure(columns=columns)
        self.consistency.heading("video", text="视频文件")
        self.consistency.column("video", width=320, minwidth=160, stretch=True)
        for slot in range(1, slot_count + 1):
            name = f"slot{slot}"
            self.consistency.heading(name, text=f"第 {slot} 条")
            self.consistency.column(name, width=190, minwidth=90, stretch=True)

        reports = {
            report.slot: report
            for report in check_slot_consistency(scanned, range(1, slot_count + 1))
        }
        for entry in scanned:
            values = [entry.video.name]
            divergent = bool(entry.error)
            for slot in range(1, slot_count + 1):
                track = slot_track(entry, slot)
                report = reports[slot]
                if track is None:
                    values.append("—")
                    if report.dominant:
                        divergent = True
                    continue
                values.append(describe_track(track))
                if report.dominant and slot_signature(track) != report.dominant:
                    divergent = True
            self.consistency.insert(
                "", tk.END, values=values, tags=("divergent",) if divergent else ()
            )

        self._consistency_notes = self._summarize_consistency(reports)

    def _summarize_consistency(self, reports: dict[int, SlotReport]) -> tuple[str, ...]:
        notes: list[str] = []
        for slot, report in sorted(reports.items()):
            if report.consistent or not report.signatures:
                continue
            if len(report.signatures) > 1:
                notes.append(f"第 {slot} 条在不同视频中内容不一致；")
            if report.missing:
                notes.append(f"第 {slot} 条在 {len(report.missing)} 个视频中不存在；")
        return tuple(notes)

    # -------------------------------------------------------------- 预览

    def _clear_preview(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.plan = None
        self.detail_var.set("")
        self.extract_button.configure(state=tk.DISABLED)

    def preview(self) -> None:
        if self._busy:
            return
        if not self.scanned:
            messagebox.showwarning("尚未扫描", "请先选择文件夹并点击扫描。")
            return
        slots = self.selected_slots()
        if not slots:
            messagebox.showwarning("尚未选择层级", "请至少勾选一条要提取的轨道层级。")
            return

        output_dir = None
        if self.use_output_dir_var.get():
            raw_output = self.output_dir_var.get().strip()
            if not raw_output:
                messagebox.showwarning("未选择输出文件夹", "请选择输出文件夹，或改回“与视频同目录”。")
                return
            output_dir = Path(raw_output)
            if not output_dir.is_dir():
                messagebox.showwarning("输出文件夹无效", f"不是有效文件夹：{output_dir}")
                return

        if not self._confirm_divergent_slots(slots):
            return

        self.plan = build_extraction_plan(
            self.scanned,
            slots,
            fixed_label=self.fixed_label_var.get().strip(),
            output_dir=output_dir,
            overwrite=bool(self.overwrite_var.get()),
            convert_to_ass=bool(self.convert_ass_var.get()),
        )
        self._render_plan(self.plan)

    def _confirm_divergent_slots(self, slots: tuple[int, ...]) -> bool:
        risky = [report for report in check_slot_consistency(self.scanned, slots) if not report.consistent]
        if not risky:
            return True
        lines = [f"第 {report.slot} 条：{self._describe_report(report)}" for report in risky]
        return messagebox.askyesno(
            "层级可能不一致",
            "以下层级的轨道在各视频中并不一致，可能提错轨道：\n\n"
            + "\n".join(lines)
            + "\n\n仍然继续生成预览吗？",
        )

    def _describe_report(self, report: SlotReport) -> str:
        parts: list[str] = []
        if len(report.signatures) > 1:
            parts.append(f"{len(report.signatures)} 种不同内容")
        if report.missing:
            names = "、".join(path.name for path in report.missing[:3])
            if len(report.missing) > 3:
                names += " 等"
            parts.append(f"{len(report.missing)} 个视频没有这一层（{names}）")
        return "，".join(parts) or "一致"

    def _render_plan(self, plan: ExtractionPlan) -> None:
        self._clear_preview()
        self.plan = plan
        for item in plan.items:
            self.tree.insert(
                "",
                tk.END,
                values=(
                    STATUS_TEXT[item.status],
                    item.video.name,
                    describe_slot(item.track) if item.track is not None else "",
                    "" if item.destination is None else item.destination.name,
                    item.detail,
                ),
                tags=(item.status.value,),
            )

        ready_count = len(plan.ready_items)
        skipped = sum(
            1
            for item in plan.items
            if item.status
            in {
                ExtractionStatus.ALREADY_EXISTS,
                ExtractionStatus.MISSING_TRACK,
                ExtractionStatus.PROBE_FAILED,
            }
        )
        if plan.errors:
            self.detail_var.set(f"发现 {len(plan.errors)} 个问题，不能执行。")
            messagebox.showerror("预检未通过", "\n".join(plan.errors[:8]))
        elif not ready_count:
            self.detail_var.set("没有可提取的轨道。")
        else:
            self.detail_var.set(f"可提取 {ready_count} 条，跳过 {skipped} 条。")
        if plan.warnings:
            self.progress_var.set(" ".join(plan.warnings[:4]))
        self.extract_button.configure(
            state=tk.NORMAL if plan.can_execute and ready_count else tk.DISABLED
        )

    # -------------------------------------------------------------- 执行

    def execute(self) -> None:
        if self._busy or self.plan is None or not self.plan.ready_items:
            return
        count = len(self.plan.ready_items)
        if not messagebox.askyesno("确认提取", f"将提取 {count} 条字幕轨道，是否继续？"):
            return

        self._begin_job("正在提取…")
        messages, cancel, plan = self._messages, self._cancel, self.plan

        def worker() -> None:
            def on_progress(index: int, total: int, item: ExtractionItem) -> None:
                name = item.destination.name if item.destination is not None else ""
                messages.put(("progress", (index, total, f"已提取：{name}")))

            messages.put(
                (
                    "extracted",
                    execute_extraction_plan(plan, on_progress=on_progress, should_cancel=cancel.is_set),
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def _report_extraction(self, result: ExtractionResult) -> None:
        if result.success:
            self.progress.configure(value=self.progress.cget("maximum"))
            self.progress_var.set(f"已提取 {len(result.extracted)} 条字幕轨道。")
            messagebox.showinfo("完成", f"已成功提取 {len(result.extracted)} 条字幕轨道。")
            # 保留预览表供核对，但计划已执行过，禁止重复提取。
            self.plan = None
            self.extract_button.configure(state=tk.DISABLED)
            return

        self.progress_var.set(result.error)
        message = result.error
        if result.extracted:
            message += f"\n失败前已处理 {len(result.extracted)} 条，已清理 {len(result.rolled_back)} 条。"
        if result.rollback_errors:
            message += "\n以下文件未能清理：\n" + "\n".join(result.rollback_errors)
        if result.cancelled:
            messagebox.showwarning("已取消", message)
        else:
            messagebox.showerror("提取失败", message)
        # 未产出的部分已清理，计划依旧有效，允许直接重试。
        self.extract_button.configure(state=tk.NORMAL)

    # -------------------------------------------------------------- 收尾

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno("正在执行", "任务尚未完成，确定要关闭吗？"):
            return
        self._cancel.set()
        if self.standalone and isinstance(self.master, tk.Tk):
            self.master.destroy()
        else:
            self.destroy()


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    ExtractorWindow(root, standalone=True)
    root.mainloop()


if __name__ == "__main__":
    main()
