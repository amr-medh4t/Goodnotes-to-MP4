from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dependency_setup import ensure_dependencies


class GoodnotesReplayApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Goodnotes Replay to MP4")
        self.root.minsize(620, 205)
        self.root.columnconfigure(1, weight=1)

        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.status = tk.StringVar(value="Choose a Goodnotes file or an extracted document folder.")
        self.results: queue.Queue[tuple[bool, object]] = queue.Queue()
        self.busy = False

        ttk.Label(root, text="Goodnotes file or folder:").grid(
            row=0, column=0, padx=(16, 8), pady=(18, 8), sticky="w"
        )
        ttk.Entry(root, textvariable=self.source).grid(
            row=0, column=1, padx=8, pady=(18, 8), sticky="ew"
        )
        ttk.Button(root, text="Browse file...", command=self.choose_file).grid(
            row=0, column=2, padx=(8, 16), pady=(18, 8)
        )
        ttk.Button(root, text="Browse folder...", command=self.choose_folder).grid(
            row=1, column=2, padx=(8, 16), pady=8
        )

        ttk.Label(root, text="Output MP4:").grid(
            row=2, column=0, padx=(16, 8), pady=8, sticky="w"
        )
        ttk.Entry(root, textvariable=self.output).grid(
            row=2, column=1, padx=8, pady=8, sticky="ew"
        )
        ttk.Button(root, text="Save as...", command=self.choose_output).grid(
            row=2, column=2, padx=(8, 16), pady=8
        )

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.grid(row=3, column=0, columnspan=3, padx=16, pady=(12, 4), sticky="ew")
        ttk.Label(root, textvariable=self.status, wraplength=590).grid(
            row=4, column=0, columnspan=3, padx=16, pady=4, sticky="w"
        )
        self.convert_button = ttk.Button(
            root, text="Create MP4", command=self.start_conversion
        )
        self.convert_button.grid(row=5, column=2, padx=16, pady=(8, 16), sticky="e")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.check_results)

    def choose_file(self):
        filename = filedialog.askopenfilename(
            title="Select a Goodnotes document",
            filetypes=[
                ("Goodnotes documents", "*.goodnotes"),
                ("ZIP archives", "*.zip"),
                ("All files", "*.*"),
            ],
        )
        if filename:
            self.set_source(Path(filename))

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Select an extracted Goodnotes document")
        if folder:
            self.set_source(Path(folder))

    def set_source(self, source: Path):
        self.source.set(str(source))
        if not self.output.get().strip():
            self.output.set(str(source.with_name(f"{source.stem}-Replay.mp4")))

    def choose_output(self):
        initial = self.output.get().strip()
        initial_path = Path(initial) if initial else Path.cwd() / "Goodnotes-Replay.mp4"
        filename = filedialog.asksaveasfilename(
            title="Save replay video",
            defaultextension=".mp4",
            initialdir=str(initial_path.parent),
            initialfile=initial_path.name,
            filetypes=[("MP4 video", "*.mp4")],
        )
        if filename:
            self.output.set(filename)

    def start_conversion(self):
        source_text = self.source.get().strip()
        output_text = self.output.get().strip()
        if not source_text:
            messagebox.showerror("Missing input", "Choose a Goodnotes file or folder first.")
            return
        if not output_text:
            messagebox.showerror("Missing output", "Choose where to save the MP4.")
            return

        source = Path(source_text).expanduser()
        output = Path(output_text).expanduser()
        if not source.exists():
            messagebox.showerror("Input not found", f"Could not find:\n{source}")
            return
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
            self.output.set(str(output))
        if source.is_file() and source.resolve() == output.resolve():
            messagebox.showerror("Invalid output", "The output cannot overwrite the input file.")
            return
        if output.exists() and not messagebox.askyesno(
            "Replace existing file?",
            f"This file already exists:\n{output}\n\nReplace it?",
        ):
            return

        self.busy = True
        self.convert_button.state(["disabled"])
        self.progress.start(12)
        self.status.set("Rendering handwriting and audio. This can take a few minutes...")
        threading.Thread(
            target=self.convert_in_background,
            args=(source, output),
            daemon=True,
        ).start()

    def convert_in_background(self, source: Path, output: Path):
        try:
            self.results.put(("progress", "Checking required packages and tools..."))
            ensure_dependencies(
                lambda message: self.results.put(("progress", message))
            )
            from make_goodnotes_replay import render_source

            self.results.put(("progress", "Rendering handwriting and audio..."))
            render_source(source, output)
        except Exception as error:
            self.results.put(("error", error))
        else:
            self.results.put(("success", output))

    def check_results(self):
        try:
            status, result = self.results.get_nowait()
        except queue.Empty:
            self.root.after(150, self.check_results)
            return

        if status == "progress":
            self.status.set(str(result))
            self.root.after(150, self.check_results)
            return

        self.busy = False
        self.progress.stop()
        self.convert_button.state(["!disabled"])
        if status == "success":
            output = Path(result)
            self.status.set(f"Finished: {output}")
            messagebox.showinfo("MP4 created", f"Your replay video is ready:\n{output}")
        else:
            self.status.set("Conversion failed.")
            messagebox.showerror("Conversion failed", str(result))
        self.root.after(150, self.check_results)

    def close(self):
        if self.busy:
            messagebox.showinfo(
                "Conversion in progress",
                "Wait for the current conversion to finish before closing the app.",
            )
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    GoodnotesReplayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
