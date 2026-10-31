"""Standalone data-plotter UI and charge-integration utilities.

This legacy utility module hosts plotting widgets and helper calculations used
outside the main run orchestration flow.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from seva.app.views import view_utils

# Legacy .data pipeline imports (no longer required for CSV workflow).
# Wrapped in try/except to avoid hard dependency when running CSV-only.
try:
    from DCPgetdata.VariablesTo_df import variablesTo_df  # noqa: F401
    from DCPgetdata.GetSectionInfo import getSectionInfo  # noqa: F401
except Exception:
    variablesTo_df = None
    getSectionInfo = None

import os
import sys
from sympy import symbols  # noqa: F401
import pythonnet  # noqa: F401
import clr  # noqa: F401
from matplotlib.widgets import SpanSelector
from scipy import integrate  # noqa: F401
import numpy as np

# Make sure to include scipy.signals as a hidden import in auto-py-to-exe
global redoxEvents

import numpy as np
from scipy.integrate import simpson as simps

def computeChargePassed(df, min_time, max_time):
    """Integrate current over a selected time window and report charge metrics.
    
    Args:
        df (Any): Input provided by callers or UI events.
        min_time (Any): Input provided by callers or UI events.
        max_time (Any): Input provided by callers or UI events.
    
    Returns:
        Any: Value returned to callers.
    
    Raises:
        RuntimeError: Raised when UI orchestration encounters unrecoverable errors.
    """
    integrals = {}

    # Filter the DataFrame for the specified time interval
    df = df[(df['Time'] >= min_time) & (df['Time'] <= max_time)]

    pos_df = df.copy()  # Create a copy to preserve the original dataframe
    pos_df.loc[df['Voltage'] <= 0, 'Voltage'] = 0
    pos_df.loc[df['Current'] <= 0, 'Current'] = 0

    neg_df = df.copy()  # Create a copy to preserve the original dataframe
    neg_df.loc[df['Voltage'] >= 0, 'Voltage'] = 0
    neg_df.loc[df['Current'] >= 0, 'Current'] = 0

    PosCurrent_IntTraps = np.trapz(pos_df['Current'], pos_df['Time'])
    PosCurrent_IntSimps = simps(pos_df['Current'], pos_df['Time'])

    integrals['PosCurrent_IntTraps'] = PosCurrent_IntTraps
    integrals['PosCurrent_IntSimps'] = PosCurrent_IntSimps

    print("Positive Current Integration using trapezoidal rule:", PosCurrent_IntTraps)
    print("Positive Current Integration using Simpson's rule:", PosCurrent_IntSimps)

    NegCurrent_IntTraps = np.trapz(neg_df['Current'], neg_df['Time'])
    NegCurrent_IntSimps = simps(neg_df['Current'], neg_df['Time'])

    integrals['NegCurrent_IntTraps'] = NegCurrent_IntTraps
    integrals['NegCurrent_IntSimps'] = NegCurrent_IntSimps

    print("Negative Current Integration using trapezoidal rule:", NegCurrent_IntTraps)
    print("Negative Current Integration using Simpson's rule:", NegCurrent_IntSimps)

    pos_coulombs = PosCurrent_IntTraps
    neg_coulombs = NegCurrent_IntTraps

    Faradays_Constant = 96485

    pos_mol_electrons = pos_coulombs / Faradays_Constant
    neg_mol_electrons = neg_coulombs / Faradays_Constant
    print("Oxidative electrons: " + str(pos_mol_electrons * 1000) + " (mmol electrons)")
    print("Reductive electrons: " + str(neg_mol_electrons * 1000) + " (mmol electrons)")
    print("Total electrons passed: " + str((pos_mol_electrons - neg_mol_electrons) * 1000) + " (mmol electrons)")

    return integrals



def DataProcessingGUI(master=None):
    """Create and run the standalone data-processing GUI window.

    Args:
        master: Optional Tk parent for opening as child window.
    """

    global df
    df = None  # current (filtered) view

    global df_full
    df_full = None  # full (unfiltered) dataframe

    # Global selection (0 = all)
    global selected_section_id
    selected_section_id = 0

    # Maps the section combobox's display text -> Cycle id; rebuilt by
    # update_section_dropdown() each time a new CSV is loaded.
    section_dict: dict = {}

    # CSV header mapper: map common export headers to internal canonical names
    CSV_COLUMN_MAPPER = {
        # Core CV columns
        'time (s)': 'Time',
        'time[s]': 'Time',
        'time': 'Time',

        'potential (v)': 'Voltage',
        'potential': 'Voltage',
        'voltage (v)': 'Voltage',
        'voltage': 'Voltage',

        'current (a)': 'Current',
        'current[a]': 'Current',
        'current': 'Current',

        'applied potential (v)': 'Applied (V)',
        'applied (v)': 'Applied (V)',
        'applied potential': 'Applied (V)',

        # Cycle info (for Selections)
        'cycle': 'Cycle',

        # EIS / impedance (optional)
        'frequency (hz)': 'Z (omega)',
        'freq (hz)': 'Z (omega)',
        'phase (deg)': 'Z (theta)',
        'phase (°)': 'Z (theta)',
        'z (theta)': 'Z (theta)',
        'z magnitude': 'Z Magnitude',
        'zmod': 'Z Magnitude',
        'z real': 'Z Real',
        're(z)': 'Z Real',
        'z imaginary': 'Z Imaginary',
        'im(z)': 'Z Imaginary',
    }

    # Required for the main CV plotting/integration features
    ESSENTIAL_COLUMNS = ['Time', 'Voltage', 'Current']

    def resource_path(relative_path):
        """Resolve resource path for both source and PyInstaller runs.

        Args:
            relative_path: Relative path to resolve.
        """
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, relative_path)

    def apply_column_mapper(df_in: pd.DataFrame) -> pd.DataFrame:
        """Normalize and map user CSV headers to canonical names.

        Args:
            df_in: Raw dataframe loaded from CSV.
        """
        df_local = df_in.copy()

        # Strip header whitespace
        df_local.rename(columns={c: c.strip() for c in df_local.columns}, inplace=True)

        # Case-insensitive mapping
        lower_map = {k.lower(): v for k, v in CSV_COLUMN_MAPPER.items()}
        rename_dict = {}
        for c in df_local.columns:
            key = c.strip().lower()
            if key in lower_map:
                target = lower_map[key]
                if target not in df_local.columns:
                    rename_dict[c] = target
        if rename_dict:
            df_local.rename(columns=rename_dict, inplace=True)

        # Coerce numeric for known numeric columns if present
        numeric_candidates = [
            'Time', 'Voltage', 'Current', 'Cycle',
            'Z (omega)', 'Z (theta)', 'Z Magnitude', 'Z Real', 'Z Imaginary',
            'Applied (V)',
            'E_real'
        ]
        for col in numeric_candidates:
            if col in df_local.columns:
                df_local[col] = pd.to_numeric(df_local[col], errors='coerce')

        # Normalize Cycle to integer if present
        if 'Cycle' in df_local.columns:
            try:
                df_local['Cycle'] = df_local['Cycle'].round().astype('Int64')
            except Exception:
                pass

        return df_local

    def load_csv_with_mapper(file_path: str) -> pd.DataFrame:
        """Load CSV, normalize column names, and validate essentials.

        Args:
            file_path: CSV file path selected by the user.
        """
        try:
            raw = pd.read_csv(file_path)
        except Exception as e:
            messagebox.showerror("CSV Error", f"Failed to read CSV:\n{e}")
            raise

        mapped = apply_column_mapper(raw)

        missing = [c for c in ESSENTIAL_COLUMNS if c not in mapped.columns]
        if missing:
            messagebox.showerror(
                "Missing columns",
                f"The CSV is missing required columns: {missing}\n\n"
                "Either export these columns or extend the CSV_COLUMN_MAPPER."
            )
            raise ValueError(f"Missing columns: {missing}")

        return mapped

    def update_variable_dropdowns_from_df(df_source: pd.DataFrame):
        """Rebuild X/Y/Y2 menus to columns present in the dataframe.

        Args:
            df_source: Dataframe currently loaded in the GUI.
        """
        preferred = [
            'Time', 'Voltage', 'Current', 'Applied (V)',
            'E_real',
            'Z (omega)', 'Z (theta)', 'Z Magnitude', 'Z Real', 'Z Imaginary'
        ]

        options = [c for c in preferred if c in df_source.columns]

        # Add other numeric columns at the end
        for c in df_source.columns:
            if c in options:
                continue
            try:
                if pd.api.types.is_numeric_dtype(df_source[c]):
                    options.append(c)
            except Exception:
                pass

        if not options:
            options = list(df_source.columns)

        x_menu.configure(values=options)
        y_menu.configure(values=options)
        secondary_y_menu.configure(values=options)

        # Apply sensible defaults if available
        if x_axis_var.get() not in options:
            x_axis_var.set('Time' if 'Time' in options else options[0])
        if y_axis_var.get() not in options:
            y_axis_var.set('Current' if 'Current' in options else options[0])
        if secondary_y_axis_var.get() not in options:
            secondary_y_axis_var.set('Voltage' if 'Voltage' in options else options[0])

    def update_section_dropdown(df_source: pd.DataFrame):
        """Update section dropdown from ``Cycle`` values.

        Args:
            df_source: Dataframe currently loaded in the GUI.
        """
        nonlocal section_dict
        global selected_section_id

        section_dict = {}
        if df_source is not None and 'Cycle' in df_source.columns and df_source['Cycle'].dropna().size > 0:
            cycles = sorted(df_source['Cycle'].dropna().unique().tolist())
            section_dict["All cycles"] = 0
            for cy in cycles:
                section_dict[f"Cycle {int(cy)}"] = int(cy)
        else:
            section_dict["All data"] = 0

        section_options = list(section_dict.keys())
        section_menu.configure(values=section_options)
        section_var.set(section_options[0])

        selected_section_id = 0

    def on_section_selected(event=None):
        """Apply the newly chosen section id and refresh the plot.

        Args:
            event: Tk ``<<ComboboxSelected>>`` event (unused).
        """
        global selected_section_id
        selected_section_id = section_dict.get(section_var.get(), 0)
        update_plot()

    def select_directory():
        """
        CSV file picker (kept original function name to minimize UI changes).
        Loads the CSV into df_full and refreshes selections and axis dropdowns.
        """
        global redoxEvents, df_full, df, selected_section_id

        file_path = filedialog.askopenfilename(
            title="Select .csv file",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*"))
        )

        if not file_path:
            return

        try:
            df_loaded = load_csv_with_mapper(file_path)
        except Exception:
            return

        df_full = df_loaded
        df = df_full.copy()

        # Show truncated filename (fits old small entry)
        filename = os.path.basename(file_path)
        truncated = f"...{filename[-20:]}" if len(filename) > 23 else filename

        path_label.config(state=tk.NORMAL)
        path_label.delete(0, tk.END)
        path_label.insert(0, truncated)
        path_label.config(state='readonly')
        path_label.full_path = file_path

        # Update dropdowns (cycle selections + axes)
        update_section_dropdown(df_full)
        update_variable_dropdowns_from_df(df_full)

        selected_section_id = 0
        redoxEvents = None
        update_plot()

    def update_plot():
        """Filter by Cycle (if any) and re-render the plot using the in-memory dataframe."""
        global selected_section_id, df, df_full, redoxEvents
        redoxEvents = None  # Clear redox events

        try:
            if df_full is None or df_full.empty:
                return

            x_col = x_axis_var.get()
            y_col = y_axis_var.get()
            secondary_y_col = secondary_y_axis_var.get()
            log_scale = log_x_var.get()
            y2_enabled = y2_enabled_var.get()

            df_plot = df_full.copy()

            # Apply cycle filter if present and selected
            if selected_section_id and 'Cycle' in df_plot.columns:
                df_plot = df_plot[df_plot['Cycle'] == selected_section_id].copy()

            # Nyquist convention: invert imaginary impedance for display
            if 'Z Imaginary' in df_plot.columns:
                try:
                    df_plot['Z Imaginary'] = -df_plot['Z Imaginary']
                except Exception:
                    pass

            df = df_plot  # Store current view for zoom/integration

            display_plot(df, x_col, y_col, secondary_y_col, log_scale, y2_enabled)
        except Exception as e:
            print(f"Update plot error: {e}")

    def display_plot(df_in, col1, col2, secondary_col, log_x, y2_enabled):
        """Create and embed matplotlib figure into the Tk UI.

        Args:
            df_in: Dataframe to plot.
            col1: X-axis column name.
            col2: Primary Y-axis column name.
            secondary_col: Optional secondary Y-axis column name.
            log_x: Whether X axis should be logarithmic.
            y2_enabled: Whether secondary axis should be rendered.
        """
        global fig, ax, canvas, toolbar, span_selector, redoxEvents
        fig, ax = plt.subplots()

        if df_in is not None and not df_in.empty:
            # Only require secondary column if Y2 is enabled and the column exists
            subset_cols = []
            if col1 in df_in.columns:
                subset_cols.append(col1)
            if col2 in df_in.columns:
                subset_cols.append(col2)
            if y2_enabled and secondary_col in df_in.columns:
                subset_cols.append(secondary_col)

            if len(subset_cols) >= 2:
                df_cleaned = df_in.dropna(subset=subset_cols).copy()

                # Ensure numeric for plotted columns
                for c in subset_cols:
                    df_cleaned.loc[:, c] = pd.to_numeric(df_cleaned[c], errors='coerce')

                # Plotting convention: Current in mA, sign inverted (legacy behavior)
                try:
                    if 'Current' in df_cleaned.columns:
                        df_cleaned.loc[:, 'Current'] = df_cleaned['Current'] * -1000
                except Exception:
                    pass

                if log_x:
                    ax.set_xscale('log')

                # #F3822F matches the main window's Start button accent
                # color (seva.app.views.view_utils.ACCENT) for a consistent
                # brand color across the app; this file stays dependency-free
                # from the rest of the view layer, so the hex is just
                # hardcoded to match rather than imported.
                ax.plot(df_cleaned[col1], df_cleaned[col2], marker='o', linestyle='-', markersize=2,
                        color='#F3822F', label=col2)

                ax.axhline(0, color="#DEDEDE", linewidth=1.2, linestyle='--')
                ax.axvline(0, color="#DEDEDE", linewidth=1.2, linestyle='--')
                ax.set_xlabel(get_label_with_units(col1))
                ax.set_ylabel(get_label_with_units(col2))
                ax.set_title(f'{col1} vs {col2}')

                if col1 in ('Voltage', 'E_real'):
                    ax.invert_xaxis()

                if y2_enabled and secondary_col in df_cleaned.columns:
                    ax2 = ax.twinx()
                    ax2.plot(df_cleaned[col1], df_cleaned[secondary_col], marker='x', linestyle='-', markersize=2,
                             color='#FFC7AF', label=secondary_col)
                    ax2.set_ylabel(get_label_with_units(secondary_col))

                # Add Redox events to the plot if they exist
                if redoxEvents is not None:
                    red_full = redoxEvents[redoxEvents['Type'].str.contains('Red. Full')]
                    red_half = redoxEvents[redoxEvents['Type'].str.contains('Red. Half')]
                    ox_full = redoxEvents[redoxEvents['Type'].str.contains('Ox. Full')]
                    ox_half = redoxEvents[redoxEvents['Type'].str.contains('Ox. Half')]

                    for _, row in red_full.iterrows():
                        ax.plot(row['Voltage'], row['Current'], linestyle='none', marker='o', markersize=4,
                                markerfacecolor="#FA6B2E", markeredgecolor='#FA6B2E')
                        ax.annotate(f"{row['Entry Number']}", (row['Voltage'], row['Current']), fontsize=8)

                    for _, row in red_half.iterrows():
                        ax.plot(row['Voltage'], row['Current'], linestyle='none', marker='o', markersize=4,
                                markerfacecolor="#FFC7AF", markeredgecolor='#FFC7AF')
                        ax.annotate(f"{row['Entry Number']}", (row['Voltage'], row['Current']), fontsize=8)

                    for _, row in ox_full.iterrows():
                        ax.plot(row['Voltage'], row['Current'], linestyle='none', marker='o', markersize=4,
                                markerfacecolor="#1b67ff", markeredgecolor='#1b67ff')
                        ax.annotate(f"{row['Entry Number']}", (row['Voltage'], row['Current']), fontsize=8)

                    for _, row in ox_half.iterrows():
                        ax.plot(row['Voltage'], row['Current'], linestyle='none', marker='o', markersize=4,
                                markerfacecolor="#6c92df", markeredgecolor='#6c92df')
                        ax.annotate(f"{row['Entry Number']}", (row['Voltage'], row['Current']), fontsize=8)

                if span_selector_enabled.get():
                    span_selector = SpanSelector(
                        ax, lambda xmin, xmax: computeChargePassed(df_in, xmin, xmax),
                        'horizontal', useblit=True, props=dict(alpha=0.5, facecolor='#DEDEDE')
                    )
                else:
                    span_selector = None
            else:
                ax.set_title('Select Data and Columns to Plot')
        else:
            ax.set_title('Select Data and Columns to Plot')

        ax.grid(False)
        plt.close(fig)

        # Rebuild plot frame content
        for widget in plot_frame.winfo_children():
            widget.destroy()

        sub_plot_frame = ttk.Frame(plot_frame, style="CardPlain.TFrame")
        sub_plot_frame.pack(expand=True, fill=tk.BOTH)

        button_frame = ttk.Frame(sub_plot_frame, style="CardPlain.TFrame")
        button_frame.pack(fill=tk.X)

        # IR Correct button (reintroduced)
        ir_correct_button = ttk.Button(
            button_frame, text="IR Correct",
            command=lambda: IRCorrect(df_in, path_label.full_path, col1, col2)
        )
        ir_correct_button.pack(pady=(20, 0), padx=20, side=tk.RIGHT)

        save_csv_button = ttk.Button(
            button_frame, text="Save .csv",
            command=lambda: data_to_CSV(path_label.full_path)
        )
        save_csv_button.pack(pady=(20, 0), padx=20, side=tk.LEFT)

        graph_frame = ttk.Frame(sub_plot_frame, style="CardPlain.TFrame")
        graph_frame.pack(side=tk.BOTTOM, expand=True, fill=tk.BOTH)

        canvas = FigureCanvasTkAgg(fig, master=graph_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(expand=True, fill=tk.BOTH)

        toolbar = NavigationToolbar2Tk(canvas, graph_frame)
        toolbar.update()
        canvas.get_tk_widget().pack(expand=True, fill=tk.BOTH)

    def IRCorrect(df_in, FilePath, col1, col2):
        """Apply IR correction and update ``E_real`` values.

        Args:
            df_in: Currently displayed dataframe.
            FilePath: Source file path (unused, retained for compatibility).
            col1: Active x-axis column.
            col2: Active y-axis column.

        Notes:
            - Only applies when X is Voltage/E_real and Y is Current.
            - Prompts for series resistance Rs (ohm).
            - Adds/updates column ``E_real`` in ``df_full``.
        """
        global df_full

        if df_full is None or df_full.empty:
            messagebox.showwarning("IR Correct", "No data loaded.")
            return

        if not (col2 == 'Current' and col1 in ('Voltage', 'E_real')):
            messagebox.showinfo("IR Correct", "IR correction requires X=Voltage (or E_real) and Y=Current.")
            return

        rs = simpledialog.askfloat("IR Correct", "Series resistance Rs (Ω):", minvalue=0.0)
        if rs is None:
            return

        if 'Voltage' not in df_full.columns or 'Current' not in df_full.columns:
            messagebox.showerror("IR Correct", "Dataframe must contain 'Voltage' and 'Current' columns.")
            return

        # Compute corrected potential (Current must be in A in the dataframe)
        try:
            df_full['E_real'] = pd.to_numeric(df_full['Voltage'], errors='coerce') - (
                pd.to_numeric(df_full['Current'], errors='coerce') * float(rs)
            )
        except Exception as e:
            messagebox.showerror("IR Correct", f"Failed to compute E_real:\n{e}")
            return

        # Refresh axis dropdowns so E_real becomes selectable, and plot it immediately
        update_variable_dropdowns_from_df(df_full)
        x_axis_var.set('E_real')
        update_plot()

    def get_label_with_units(variable):
        """Return human-readable axis label for a dataframe column name.

        Args:
            variable: Canonical dataframe column name.
        """
        units = {
            'Time': 'Time (s)',
            'Voltage': 'Voltage (V)',
            'E_real': 'E_real (V)',
            'Current': 'Current (mA)',
            'Applied (V)': 'Applied (V)',
            'Z (omega)': 'Frequency (Hz)',
            'Z (theta)': 'Phase Angle (°)',
            'Z Magnitude': 'Impedance Magnitude (Ω)',
            'Z Real': 'Real Impedance (Ω)',
            'Z Imaginary': '-Imaginary Impedance (Ω)'
        }
        return units.get(variable, variable)

    def zoom_plot(event=None):
        """Apply manual x-range zoom and recompute charge over visible window.

        Args:
            event: Tk event from Enter key binding.
        """
        global df
        try:
            xmin_val = float(xmin_entry.get())
            xmax_val = float(xmax_entry.get())
            if ax:
                ax.set_xlim([xmin_val, xmax_val])
                canvas.draw()

                # Use the previously loaded DataFrame to compute charge passed
                if df is not None:
                    _ = computeChargePassed(df, xmin_val, xmax_val)

        except ValueError:
            messagebox.showerror("Input Error", "Please enter valid numeric values for xmin and xmax.")
        except Exception as e:
            messagebox.showerror("Error", f"An error occurred: {e}")

        # Keep original behavior (double attempt) for maximum compatibility
        try:
            xmin_val = float(xmin_entry.get())
            xmax_val = float(xmax_entry.get())
            if ax:
                ax.set_xlim([xmin_val, xmax_val])
                canvas.draw()
        except ValueError:
            messagebox.showerror("Input Error", "Please enter valid numeric values for xmin and xmax.")
        except Exception as e:
            messagebox.showerror("Error", f"An error occurred: {e}")

    def get_peaks(df_in, FilePath, col1, col2):
        """Legacy placeholder for peak extraction workflow hooks.

        Args:
            df_in: Currently displayed dataframe.
            FilePath: Source data file path.
            col1: Active x-axis column.
            col2: Active y-axis column.
        """
        global redoxEvents

        if col1 == 'Voltage' and col2 == 'Current':
            pass
        else:
            return

        if df_in is not None:
            try:
                app = None
                root.wait_window(app)  # Wait for the SmoothingParametersGUI window to close

                if app.submitted:
                    lmbd = app.lam
                    min_prominence = app.min_prominence
                    sensitivity = app.sensitivity

                    redoxEvents = None
                    print(redoxEvents)

                    # Update the plot with redox events
                    display_plot(df_in, x_axis_var.get(), y_axis_var.get(), secondary_y_axis_var.get(), log_x_var.get(),
                                 y2_enabled_var.get())

            except Exception as e:
                print(f'Could not find peaks due to: {e}')

    def data_to_CSV(FilePath):
        """Save current dataframe view to ``dataframe.csv`` next to source file.

        Args:
            FilePath: Source CSV path used as output directory anchor.
        """
        global df_full, selected_section_id

        if df_full is None or df_full.empty:
            messagebox.showwarning("Save CSV", "No data loaded.")
            return

        selected_directory = os.path.dirname(FilePath) if FilePath else os.getcwd()

        df_export = df_full.copy()
        if selected_section_id and 'Cycle' in df_export.columns:
            df_export = df_export[df_export['Cycle'] == selected_section_id].copy()

        csv_file_path = os.path.join(selected_directory, 'dataframe.csv')
        try:
            df_export.to_csv(csv_file_path, index=False)
            print('.csv of current file saved to: ', csv_file_path)
        except Exception as e:
            messagebox.showerror("Save CSV", f"Failed to save CSV:\n{e}")

        return

    # -------------------------------------------------------------------------
    # Tk window creation (kept from original)
    if master:
        root = tk.Toplevel(master)
    else:
        root = tk.Tk()

    root.title("SEVA Data Processing")
    root.geometry("900x750")
    root.configure(bg=view_utils.PAGE_BG)
    view_utils.apply_theme(root)

    root.grid_rowconfigure(1, weight=1)
    root.grid_columnconfigure(0, weight=1)
    root.grid_columnconfigure(1, weight=1)
    root.grid_columnconfigure(2, weight=1)

    global span_selector_enabled
    span_selector_enabled = tk.BooleanVar()

    user_input_frame = ttk.LabelFrame(root, text="Fetch Data", style='Card.TLabelframe')
    user_input_frame.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=10, pady=10)

    input_frame = ttk.Frame(user_input_frame, style="CardPlain.TFrame")
    input_frame.grid(row=0, column=0, sticky="w")

    file_frame = ttk.Frame(input_frame, style="CardPlain.TFrame")
    file_frame.grid(row=0, column=0, pady=5, sticky="w")

    select_button = ttk.Button(file_frame, text="Select .csv file", command=select_directory, style="Accent.TButton")
    select_button.grid(row=0, column=0, padx=5)

    path_label = ttk.Entry(file_frame, state='readonly', width=13)
    path_label.grid(row=0, column=1, padx=5)
    path_label.full_path = ""

    section_frame = ttk.Frame(input_frame, style="CardPlain.TFrame")
    section_frame.grid(row=1, column=0, pady=5, sticky="w")
    section_var = tk.StringVar(root)
    section_menu = ttk.Combobox(section_frame, textvariable=section_var, values=["All data"], state="readonly", width=11)
    section_menu.grid(row=0, column=1, padx=5, pady=2)
    section_menu.bind("<<ComboboxSelected>>", on_section_selected)

    axes_frame = ttk.LabelFrame(root, text="Axes", style='Card.TLabelframe')
    axes_frame.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=10, pady=10)

    axes_input_frame = ttk.Frame(axes_frame, style="CardPlain.TFrame")
    axes_input_frame.grid(row=0, column=0, sticky="w")

    # Initial placeholder options (will be rebuilt after CSV load)
    variable_options = ['Time', 'Voltage', 'Current']

    x_axis_frame = ttk.Frame(axes_input_frame, style="CardPlain.TFrame")
    x_axis_frame.grid(row=0, column=0, pady=5, sticky="w")
    ttk.Label(x_axis_frame, text="X-axis:", style="CardMuted.TLabel").grid(row=0, column=0, padx=(5, 8))
    x_axis_var = tk.StringVar(root)
    x_axis_var.set(variable_options[0])
    x_menu = ttk.Combobox(x_axis_frame, textvariable=x_axis_var, values=variable_options, state="readonly", width=7)
    x_menu.grid(row=0, column=1, padx=5)
    x_menu.bind("<<ComboboxSelected>>", lambda _: update_plot())

    y_axis_frame = ttk.Frame(axes_input_frame, style="CardPlain.TFrame")
    y_axis_frame.grid(row=0, column=1, pady=5, sticky="e")
    ttk.Label(y_axis_frame, text="Y-axis:", style="CardMuted.TLabel").grid(row=0, column=0, padx=(15, 8))
    y_axis_var = tk.StringVar(root)
    y_axis_var.set(variable_options[0])
    y_menu = ttk.Combobox(y_axis_frame, textvariable=y_axis_var, values=variable_options, state="readonly", width=7)
    y_menu.grid(row=0, column=1, padx=5)
    y_menu.bind("<<ComboboxSelected>>", lambda _: update_plot())

    secondary_y_axis_frame = ttk.Frame(axes_input_frame, style="CardPlain.TFrame")
    secondary_y_axis_frame.grid(row=1, column=1, pady=5, sticky="e")
    ttk.Label(secondary_y_axis_frame, text="Y2-axis:", style="CardMuted.TLabel").grid(row=0, column=0, padx=(15, 8))
    secondary_y_axis_var = tk.StringVar(root)
    secondary_y_axis_var.set(variable_options[0])
    secondary_y_menu = ttk.Combobox(
        secondary_y_axis_frame, textvariable=secondary_y_axis_var, values=variable_options,
        state="readonly", width=7,
    )
    secondary_y_menu.grid(row=0, column=1, padx=5)
    secondary_y_menu.bind("<<ComboboxSelected>>", lambda _: update_plot())

    checks_frame = ttk.Frame(axes_input_frame, style="CardPlain.TFrame")
    checks_frame.grid(row=1, column=0, pady=5, sticky="w")

    log_x_var = tk.BooleanVar()
    log_check = ttk.Checkbutton(
        checks_frame, text="Log X", variable=log_x_var, command=lambda: update_plot(),
        style="Card.TCheckbutton",
    )
    log_check.grid(row=0, column=0, padx=(5, 12))

    y2_enabled_var = tk.BooleanVar()
    y2_check = ttk.Checkbutton(
        checks_frame, text="Show Y2", variable=y2_enabled_var, command=lambda: update_plot(),
        style="Card.TCheckbutton",
    )
    y2_check.grid(row=0, column=1, padx=(0, 5))

    # Integration Frame
    new_frame = ttk.LabelFrame(root, text="Integration", style='Card.TLabelframe')
    new_frame.grid(row=0, column=2, columnspan=1, sticky="nsew", padx=10, pady=10)

    options_frame = ttk.Frame(new_frame, style="CardPlain.TFrame")
    options_frame.grid(row=0, column=0, sticky="ew")

    span_check = ttk.Checkbutton(
        options_frame, text="Integrate Current", variable=span_selector_enabled,
        command=update_plot, style="Card.TCheckbutton",
    )
    span_check.grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky="w")

    # xmin / xmax entries share a single row so the frame stays two lines tall.
    xrange_frame = ttk.Frame(options_frame, style="CardPlain.TFrame")
    xrange_frame.grid(row=1, column=0, columnspan=4, sticky="w")

    ttk.Label(xrange_frame, text="xmin:", style="CardMuted.TLabel").grid(row=0, column=0, padx=(5, 5), pady=5, sticky="w")
    xmin_entry = ttk.Entry(xrange_frame, width=7)
    xmin_entry.grid(row=0, column=1, padx=(0, 12), pady=5, sticky="w")
    xmin_entry.bind("<Return>", zoom_plot)

    ttk.Label(xrange_frame, text="xmax:", style="CardMuted.TLabel").grid(row=0, column=2, padx=(0, 5), pady=5, sticky="w")
    xmax_entry = ttk.Entry(xrange_frame, width=7)
    xmax_entry.grid(row=0, column=3, padx=(0, 5), pady=5, sticky="w")
    xmax_entry.bind("<Return>", zoom_plot)

    plot_frame = ttk.LabelFrame(root, text="Plot", style='Card.TLabelframe')
    plot_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10, pady=0)

    plot_frame.grid_rowconfigure(0, weight=1)
    plot_frame.grid_columnconfigure(0, weight=1)

    # Initialize the display plot with placeholders
    display_plot(None, '', '', '', False, False)

    icon_path = resource_path("Logo.ico")
    if os.path.exists(icon_path):
        try:
            root.iconbitmap(icon_path)
        except Exception:
            pass
    else:
        print(f"Icon file not found at {icon_path}")

    if not master:
        root.mainloop()


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()  # Hide root window, only show the DataProcessing window
    DataProcessingGUI(root)
    root.mainloop()
