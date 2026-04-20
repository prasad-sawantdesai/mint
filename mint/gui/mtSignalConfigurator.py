# Description: A GUI to configure signal parameters for data access and so on.
# Author: Piotr Mazur
# Changelog:
#  Sept 2021: Refactored ui design classes [Jaswant Sai Panchumarti]
#  Sept 2021: Add iplotProcessing context [Jaswant Sai Panchumarti]

from collections import defaultdict
import json
import os
from enum import Enum
from pathlib import Path

import pandas as pd
import numpy as np
import sys
import typing
from typing import List

from PySide6.QtCore import QCoreApplication, QMargins, QModelIndex, Qt, Signal
from PySide6.QtGui import QContextMenuEvent, QShortcut, QKeySequence, QPalette, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QStyle, \
    QTabWidget, QTableView, QVBoxLayout, QWidget, QDialog, QTextEdit, QDialogButtonBox

from iplotProcessing.common import InvalidExpression
from iplotlib.interface.iplotSignalAdapter import Result, StatusInfo
from iplotProcessing.tools.parsers import Parser

from iplotWidgets.variableBrowser.variableBrowser import VariableBrowser
from iplotWidgets.pulseBrowser.pulseBrowser import PulseBrowser
from iplotWidgets.moduleImporter.moduleImporter import ModuleImporter
from iplotWidgets.consoleWidget.consoleWidget import ConsoleWidget
from mint.gui.mtSignalToolBar import MTSignalsToolBar
from mint.gui.mtFindReplace import FindReplaceDialog
from mint.gui.views import MTDataSourcesDelegate, MTSignalItemView
from mint.gui.views.mtDataSourcesDelegate import MTCalibratedDelegate, MTPlotTypeDelegate
from mint.models import MTSignalsModel
from mint.models.mtSignalsModel import Waypoint
from mint.models.utils import mtBlueprintParser as mtBp

from iplotLogging import setupLogger

logger = setupLogger.get_logger(__name__)

# These variables act as a single point reference to define the title of the tabs.
ALL_VIEW_NAME = "All"
DA_VIEW_NAME = "Data-Access"
PLAYOUT_VIEW_NAME = "Plot-Layout"
PROC_VIEW_NAME = "Data-Processing"

# This is a pre-defined state for the signal-views.
NEAT_VIEW = {
    ALL_VIEW_NAME: {
        "DS": True,
        "Variable": True,
        "Stack": True,
        "Row span": True,
        "Col span": True,
        "Envelope": True,
        "Alias": True,
        "PulseId": True,
        "StartTime": True,
        "EndTime": True,
        "x": True,
        "y": True,
        "z": True,
        "Extremities": True,
        "Plot type": True,
        "Status": True,
        "Output Datatype": True
    },
    DA_VIEW_NAME: {
        "DS": True,
        "Variable": True,
        "Stack": False,
        "Row span": False,
        "Col span": False,
        "Envelope": True,
        "Alias": True,
        "PulseId": True,
        "StartTime": True,
        "EndTime": True,
        "x": False,
        "y": False,
        "z": False,
        "Extremities": False,
        "Plot type": False,
        "Status": True,
        "Output Datatype": True
    },
    PLAYOUT_VIEW_NAME: {
        "DS": True,
        "Variable": True,
        "Stack": True,
        "Row span": True,
        "Col span": True,
        "Envelope": False,
        "Alias": True,
        "PulseId": False,
        "StartTime": False,
        "EndTime": False,
        "x": False,
        "y": False,
        "z": False,
        "Extremities": False,
        "Plot type": True,
        "Status": True,
        "Output Datatype": False
    },
    PROC_VIEW_NAME: {
        "DS": True,
        "Variable": True,
        "Stack": False,
        "Row span": False,
        "Col span": False,
        "Envelope": False,
        "Alias": True,
        "PulseId": True,
        "StartTime": True,
        "EndTime": True,
        "x": True,
        "y": True,
        "z": True,
        "Extremities": False,
        "Plot type": False,
        "Status": True,
        "Output Datatype": True
    }
}


class RowAliasType(Enum):
    Simple = 'SIMPLE'
    Mixed = 'MIXED'


def _row_predicate(row: pd.Series, aliases: list, blueprint: dict) -> typing.Tuple[RowAliasType, str, Parser, set]:
    name = row[mtBp.get_column_name(blueprint, 'Variable')]
    alias = row[mtBp.get_column_name(blueprint, 'Alias')]
    depends_on = set()

    is_simple = True
    p = Parser()
    for dim in ['x', 'y', 'z']:
        expression = row[mtBp.get_column_name(blueprint, dim)]
        if expression == '':
            continue
        try:
            p.set_expression(expression)
            # Check if the expression only uses the row's alias or 'self'
            expr = all([var == alias or var == 'self' for var in list(p.var_map.keys())])
            is_simple &= expr
            if not expr:
                result_dependencies = p.get_var_expression(expression)
                for res in result_dependencies:
                    depends_on.add(res)

        except InvalidExpression:
            pass

    # Check for variable column
    try:
        p.set_expression(name)
    except InvalidExpression:
        p.set_expression("")

    # True: name does not consist of any pre-defined aliases
    if p.is_valid:
        is_simple &= all([var not in aliases for var in list(p.var_map.keys())])
        if not is_simple:
            result_dependencies = p.get_var_expression(name)
            for res in result_dependencies:
                depends_on.add(res)

    if is_simple:
        return RowAliasType.Simple, name, p, depends_on
    else:
        return RowAliasType.Mixed, name, p, depends_on


class MTSignalConfigurator(QWidget):
    progressChanged = Signal(float)
    statusChanged = Signal(str)
    buildAborted = Signal(str)
    showProgress = Signal()
    hideProgress = Signal()
    busy = Signal()
    ready = Signal()

    # add_dataframe = Signal(pd.DataFrame)

    def __init__(self, blueprint: dict = mtBp.DEFAULT_BLUEPRINT, scsv_dir: str = '.', data_sources=None, parent=None):
        super().__init__(parent)

        if data_sources is None:
            data_sources = []

        self._model = MTSignalsModel(blueprint=blueprint)

        self._scsv_dir = scsv_dir
        self.data_sources = data_sources
        self._toolbar = MTSignalsToolBar(parent=self)
        self._toolbar.openAction.triggered.connect(self.on_import)
        self._toolbar.appendAction.triggered.connect(self.on_append)
        self._toolbar.saveAction.triggered.connect(self.on_export)
        self._toolbar.searchVarsBtn.clicked.connect(self.on_tree_view)
        self._toolbar.loadModules.clicked.connect(self.on_load)

        self._signal_item_widgets = [MTSignalItemView(title=ALL_VIEW_NAME, parent=self),
                                     MTSignalItemView(title=DA_VIEW_NAME, parent=self),
                                     MTSignalItemView(title=PLAYOUT_VIEW_NAME, parent=self)]  # ,
        #  MTSignalItemView(PROC_VIEW_NAME, view_type=QTreeView, parent=self)]

        self._ds_delegate = MTDataSourcesDelegate(data_sources, self)
        self._cal_delegate = MTCalibratedDelegate(self)
        self._pt_delegate = MTPlotTypeDelegate(
            ["PlotXY", "PlotContour", "PlotXYWithSlider", "PlotContourWithSlider", "PlotImage"], self)
        self._tabs = QTabWidget(parent=self)
        self._tabs.setMovable(True)

        self.parseBtn = QPushButton("Parse", self)
        self.parseBtn.clicked.connect(self.on_parse_button_pressed)

        for widget in self._signal_item_widgets:
            widget.set_model(self._model)
            widget.import_dict(NEAT_VIEW.get(widget.windowTitle()))
            self._tabs.addTab(widget, widget.windowTitle())
            widget.view().setItemDelegateForColumn(0, self._ds_delegate)
            widget.view().setItemDelegateForColumn(6, self._cal_delegate)
            widget.view().setItemDelegateForColumn(15, self._pt_delegate)
            widget.view().setSortingEnabled(True)

        self._tabs.currentChanged.connect(self.on_current_view_changed)
        # Set menu for configure columns button.
        self._toolbar.configureColsBtn.setMenu(self._signal_item_widgets[0].header_menu())

        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(QMargins())
        self.layout().addWidget(self._toolbar)
        self.layout().addWidget(self._tabs)
        self.layout().addWidget(self.parseBtn)

        self.model.dataChanged.connect(self.resize_view_to_columns)

        self.selectVarDialog = VariableBrowser()

        self.selectVarDialog.cmd_finish.connect(self.append_dataframe)

        self.selectModuleDialog = ModuleImporter()

        self.selectPulseDialog = PulseBrowser()
        self.selectPulseDialog.cmd_finish.connect(self.append_pulse)

        # MINT Console
        self.console = ConsoleWidget()
        self.console.setup_logging()

        self.model.insertRows(0, 1, QModelIndex())
        self._find_replace_dialog = None

        self._processed = set()  # Set used to store the different rows processed

        self.invalid_stacks = []  # Used to verify the stacks

        shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        shortcut.activated.connect(self.copy_contents_to_clipboard)
        shortcut2 = QShortcut(QKeySequence("Ctrl+V"), self)
        shortcut2.activated.connect(self.paste_contents_from_clipboard)

    def on_current_view_changed(self, index: int):
        current_view = self.item_widgets[index]
        self._toolbar.configureColsBtn.setMenu(current_view.header_menu())

    def on_parse_button_pressed(self):
        logger.debug('Build order:')
        for waypt in self.build():
            logger.debug(f'Row: {waypt.idx}')
        self.resize_views_to_contents()

    def tool_bar(self) -> MTSignalsToolBar:
        return self._toolbar

    @property
    def model(self) -> MTSignalsModel:
        return self._model

    @property
    def item_widgets(self) -> typing.List[MTSignalItemView]:
        return self._signal_item_widgets

    def resize_view_to_columns(self, top_left: QModelIndex, bottom_right: QModelIndex):
        columns = range(top_left.column(), bottom_right.column() + 1)
        for widget in self.item_widgets:
            view = widget.view()
            if isinstance(view, QTableView):
                for col in columns:
                    view.resizeColumnToContents(col)

    def resize_views_to_contents(self):
        for widget in self.item_widgets:
            view = widget.view()
            if isinstance(view, QTableView):
                view.resizeColumnsToContents()

    def on_export(self):
        file = QFileDialog.getSaveFileName(self, "Save SCSV", filter='*.scsv', dir=self._scsv_dir)
        if file and file[0]:
            if not file[0].endswith('.scsv'):
                file_name = file[0] + '.scsv'
            else:
                file_name = file[0]
            self._scsv_dir = os.path.dirname(file_name)
            self.export_scsv(file_name)

    def on_import(self):
        file = QFileDialog.getOpenFileName(self, "Open SCSV", dir=self._scsv_dir, filter='*.scsv')
        if file and file[0]:
            self._scsv_dir = os.path.dirname(file[0])
            self.import_scsv(file[0])

    def on_append(self):
        file = QFileDialog.getOpenFileName(self, "Append SCSV", dir=self._scsv_dir, filter='*.scsv')
        if file and file[0]:
            self.append_scsv(file[0])

    def on_tree_view(self):
        self.selectVarDialog.show()
        self.selectVarDialog.activateWindow()

    def on_load(self):
        self.selectModuleDialog.show()
        self.selectModuleDialog.activateWindow()

    def append_dataframe(self, df):
        if not df.empty:
            self._model.append_dataframe(df)

    def append_pulse(self, pulses):
        current_tab_id = self._tabs.currentIndex()
        selected_ids = self._signal_item_widgets[current_tab_id].view().selectionModel().selectedIndexes()
        if not len(selected_ids):
            return

        for idx in selected_ids:
            new_idx = self._model.index(idx.row(), 7)
            cur_pulses = self._model.data(new_idx, Qt.ItemDataRole.DisplayRole)
            pulse_set = set(cur_pulses.replace(" ", "").split(",")) if cur_pulses else set()
            # Check that the pulse is not already added
            for pulse in pulses:
                pulse_set.add(pulse)

            final_text = ", ".join(pulse_set)

            # Add the pulse in the corresponding cells
            self.set_bulk_contents(final_text, [new_idx])

    def insert_empty_rows(self, above: bool):
        current_tab_id = self._tabs.currentIndex()
        selection = self._signal_item_widgets[current_tab_id].view().selectionModel() \
            .selectedIndexes()  # type: List[QModelIndex()]

        total_rows = set(index.row() for index in selection)
        if selection:
            if above:
                row_position = selection[0].row()
            else:
                row_position = selection[-1].row() + 1
            self._model.insertRows(row_position, len(total_rows), QModelIndex())
        else:
            self._model.insertRow(self._model.rowCount(QModelIndex()))

    def remove_row(self):
        selected_rows = set()
        current_tab_id = self._tabs.currentIndex()
        for idx in self._signal_item_widgets[current_tab_id].view().selectionModel().selectedIndexes():
            selected_rows.add(idx.row())

        for row in sorted(selected_rows, reverse=True):
            self._model.removeRow(row)

    def set_bulk_contents(self, text: str, indices: typing.List[QModelIndex]):
        self.busy.emit()
        left = 1 << 32
        right = 0
        top = 1 << 32
        bottom = 0
        with self._model.activate_fast_mode():
            for idx in indices:
                left = min(left, idx.column())
                right = max(right, idx.column())
                top = min(top, idx.row())
                bottom = max(bottom, idx.row())
                self._model.setData(idx, text, Qt.ItemDataRole.EditRole)
        self.ready.emit()

    def delete_contents(self):
        current_tab_id = self._tabs.currentIndex()
        selected_ids = self._signal_item_widgets[current_tab_id].view().selectionModel().selectedIndexes()
        self.set_bulk_contents('', selected_ids)

    def paste_contents_from_clipboard(self):
        current_tab_id = self._tabs.currentIndex()
        selected_ids = self._signal_item_widgets[current_tab_id].view().selectionModel().selectedIndexes()
        if not len(selected_ids):
            return

        text = QGuiApplication.clipboard().text()  # type: str
        text = text.strip()  # sometimes, user might have copied unnecessary line breaks at the start / end.
        if not text:
            data = [['']]
        else:
            data = [line.split(';') for line in text.splitlines()]
        copy_rows = len(data)
        copy_cols = len(data[0])
        self.busy.emit()
        if len(data) == 1 and len(data[0]) == 1:
            self.set_bulk_contents(text, selected_ids)
            return

        rows = set()
        columns = set()
        for idx in selected_ids:
            rows.add(idx.row())
            columns.add(idx.column())
        if len(columns) == 1:
            columns = set(range(min(columns), min(columns) + copy_cols))
        if len(rows) == 1:
            rows = set(range(min(rows), min(rows) + copy_rows))

        total_columns = max(columns) - min(columns) + 1
        total_rows = max(rows) - min(rows) + 1
        if total_columns != len(columns) or total_rows != len(rows):
            show_popup_msg("Can't paste data", "Only rectangular selection is available")
            self.ready.emit()
            return
        if total_rows % copy_rows != 0 or total_columns % copy_cols != 0:
            self.ready.emit()
            show_popup_msg("Can't paste data", f"Copied data {copy_rows}x{copy_cols} and "
                                               f"selection cells {total_rows}x{total_columns} are not proportional")
            return
        left = 1 << 32
        right = 0
        top = 1 << 32
        bottom = 0

        for col in range(min(columns), max(columns) + 1, copy_cols):
            for row in range(min(rows), max(rows) + 1, copy_rows):
                for i, line in enumerate(data):
                    for j, value in enumerate(line):
                        idx = self._model.createIndex(row + i, col + j)
                        left = min(left, idx.column())
                        right = max(right, idx.column())
                        top = min(top, idx.row())
                        bottom = max(bottom, idx.row())
                        with self._model.activate_fast_mode():
                            self._model.setData(idx, value, Qt.ItemDataRole.EditRole)

        self._model.dataChanged.emit(self._model.index(top, left), self._model.index(bottom, right))
        self.ready.emit()

    def copy_contents_to_clipboard(self):
        current_tab_id = self._tabs.currentIndex()
        selected_ids = self._signal_item_widgets[current_tab_id].view().selectionModel().selectedIndexes()
        contents = defaultdict(lambda: defaultdict(str))
        rows = set()
        columns = set()
        for idx in selected_ids:
            value = self._model.data(idx, Qt.ItemDataRole.DisplayRole)
            column = idx.column()
            row = idx.row()
            contents[row][column] = value
            columns.add(column)
            rows.add(row)

        if columns:
            cmin, cmax = min(columns), max(columns)
            selectable_in_span = 0
            for c in range(cmin, cmax + 1):
                # If the column is not selectable (e.g. 'Status'), it does not count towards "rectangularity"
                if self._model.flags(self._model.createIndex(0, c)) & Qt.ItemFlag.ItemIsSelectable:
                    selectable_in_span += 1
            total_columns = selectable_in_span
        else:
            total_columns = 0

        total_rows = max(rows) - min(rows) + 1 if rows else 0
        if total_columns != len(columns) or total_rows != len(rows):
            show_popup_msg("Can't copy data", "Only rectangular selection is available")
            self.ready.emit()
            return
        result = []
        for row in range(min(rows), max(rows) + 1):
            row_text = []
            for col in range(min(columns), max(columns) + 1):
                row_text.append(contents.get(row).get(col, ""))
            result.append(';'.join(row_text))

        text = '\n'.join(result)

        QGuiApplication.clipboard().setText(text)

    def find_replace(self):
        current_tab_id = self._tabs.currentIndex()
        table_view = self._signal_item_widgets[current_tab_id].view()

        if not self._find_replace_dialog:
            self._find_replace_dialog = FindReplaceDialog(self, model=table_view)
            palette = table_view.palette()
            palette.setColor(QPalette.ColorRole.Highlight, palette.color(QPalette.ColorRole.Highlight))
            table_view.setPalette(palette)
        else:
            self._find_replace_dialog.set_model(model=table_view)
        self._find_replace_dialog.show()

        self._find_replace_dialog.show()

    def open_editor_mode(self):
        current_tab_id = self._tabs.currentIndex()
        table_view = self._signal_item_widgets[current_tab_id].view()
        selected_ids = table_view.selectionModel().selectedIndexes()
        if not selected_ids:
            return
        index = selected_ids[0]
        model = index.model()
        current_text = model.data(index, Qt.EditRole) or ""

        dlg = QDialog(self)
        dlg.setWindowTitle("Editor mode")
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowMaximizeButtonHint)
        dlg.setModal(False)

        layout = QVBoxLayout(dlg)
        text_edit = QTextEdit(dlg)
        text_edit.setPlainText(current_text)
        layout.addWidget(text_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal, dlg
        )
        layout.addWidget(buttons)

        def on_accept():
            new_text = text_edit.toPlainText()
            model.setData(index, new_text, Qt.EditRole)
            dlg.close()

        buttons.accepted.connect(on_accept)
        buttons.rejected.connect(dlg.close)

        dlg.show()

    def on_search_pulse(self):
        self.selectPulseDialog.flag = "table"
        self.selectPulseDialog.show()
        self.selectPulseDialog.activateWindow()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_contents()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        context_menu = QMenu(self)
        context_menu.addAction("Set pulse", self.on_search_pulse)
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_DialogOkButton")), "Insert above", lambda: self.insert_empty_rows(True))
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_DialogOkButton")), "Insert below", lambda: self.insert_empty_rows(False))
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_TrashIcon")), "Remove", self.remove_row)
        context_menu.addAction("Copy", self.copy_contents_to_clipboard)
        context_menu.addAction("Paste", self.paste_contents_from_clipboard)
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_DialogResetButton")), "Clear cells", self.delete_contents)
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_FileDialogContentsView")), "Find and Replace", self.find_replace)
        context_menu.addAction(self.style().standardIcon(
            getattr(QStyle, "SP_FileDialogDetailedView")), "Editor mode", self.open_editor_mode)

        context_menu.popup(event.globalPos())

    def export_scsv(self, file_path=None):
        try:
            self.busy.emit()
            df = self._model.get_dataframe().drop(labels=['Status', 'Output Datatype', 'uid'], axis=1)
            logger.info(f"Saved signal set: {file_path}")
            return df.to_csv(file_path, index=False, sep=";")
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(
                f"Error when dumping variables to file: {file_path} {e}")
            logger.exception(e)
            box.exec_()
        finally:
            self.ready.emit()

    def import_scsv(self, file_path):
        try:
            self.busy.emit()
            df = pd.read_csv(file_path, dtype=str, sep=';', keep_default_na=False)
            if not df.empty:
                self._model.set_dataframe(df)
            self.resize_views_to_contents()
            logger.info(f"Opened signal set: {file_path}")
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"Error parsing file. {e}")
            logger.exception(e)
            box.exec_()
        finally:
            self.ready.emit()

    def import_last_dump(self) -> None:
        path = os.environ.get('IPLOT_DUMP_PATH') or f"{Path.home()}/.local/1Dtool"
        path += "/dumps"
        files = os.listdir(path)

        # Filter only files, not directories
        files = [file for file in files if os.path.isfile(os.path.join(path, file))]
        if not files:
            return
        # Get the most recently modified file
        most_recent_file = max(files, key=lambda file: os.path.getmtime(os.path.join(path, file)))

        # Get the full path of the most recent file
        full_path_most_recent_file = os.path.join(path, most_recent_file)

        self.import_scsv(full_path_most_recent_file)

    def append_scsv(self, file_path):
        try:
            self.busy.emit()
            df = pd.read_csv(file_path, dtype=str, sep=';', keep_default_na=False)
            if not df.empty:
                self._model.append_dataframe(df)
            self.resize_views_to_contents()
            logger.info(f"Appended signal set: {file_path}")
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"Error parsing file. {e}")
            logger.exception(e)
            box.exec_()
        finally:
            self.ready.emit()

    def export_dict(self) -> dict:
        output = dict()
        # 1. view options.
        view_options = dict()
        for view in self._signal_item_widgets:
            view_options.update({view.windowTitle(): view.export_dict()})
        output.update({'view_options': view_options})
        # 2. Model
        output.update({'model': self._model.export_dict()})
        return output

    def import_dict(self, input_dict: dict):
        # 1. view options.
        view_options = input_dict.get('view_options') or NEAT_VIEW
        for view in self._signal_item_widgets:
            key = view.windowTitle()
            options = view_options.get(key) or NEAT_VIEW.get(key)
            for k, v in options.copy().items():
                if k not in mtBp.get_column_names(self._model.blueprint):
                    options.pop(k)
                    col_name = mtBp.get_column_name(self._model.blueprint, k)
                    if col_name:
                        options.update({col_name: v})
            view.import_dict(options)
        self._tabs.currentWidget().show()
        QCoreApplication.processEvents()

        # 2. Model
        self._model.import_dict(input_dict.get('model') or input_dict)
        self.resize_views_to_contents()

    def export_json(self):
        return json.dumps(self.export_dict())

    def import_json(self, input_file):
        self.import_dict(json.loads(input_file))

    def _abort_build(self, message):
        self.set_status_message("Failed")
        self.buildAborted.emit(message)
        self.hideProgress.emit()

    def build(self, **kwargs) -> typing.Iterator[Waypoint]:
        self.begin_build()
        self.set_progress(0)
        self.set_status_message("Parsing table ..")
        QCoreApplication.instance().processEvents()

        # Load defaults from keyword args
        for key in mtBp.get_keys_with_override(self._model.blueprint):
            v = self._model.blueprint.get(key)
            code_name = v.get('code_name')
            v.update({'default': kwargs.get(code_name) if not isinstance(kwargs.get(code_name), np.int64) else int(
                kwargs.get(code_name))})
        # Initialize pre-requisites
        df = self._model.get_dataframe()
        aliases = df.loc[:, mtBp.get_column_name(self._model.blueprint, 'Alias')].tolist()
        duplicates = set([a for a in aliases if aliases.count(a) > 1 and a != ''])

        # Stack and PlotType
        self.check_stack_table(df)

        error_msgs = []
        graph = defaultdict(list)
        column_names = list(mtBp.get_column_names(self._model.blueprint))
        status_col_idx = column_names.index("Status")
        with self._model.activate_fast_mode():
            for idx, row in df.iterrows():
                logger.debug(f"Row: {idx}")
                model_idx = self.model.createIndex(idx, status_col_idx)
                row_type, name, p, depends_on = _row_predicate(row, aliases, self._model.blueprint)

                for var_name in depends_on:
                    if var_name in duplicates:
                        sinfo = StatusInfo()
                        sinfo.result = Result.INVALID
                        conflict_row_ids = []
                        for alias_idx, alias in enumerate(aliases):
                            if var_name == alias:
                                conflict_row_ids.append(alias_idx + 1)
                        sinfo.msg = f"Conflicted row: {idx + 1}, '{var_name}' is defined in row (s): {conflict_row_ids}"
                        error_msgs.append(sinfo.msg)
                        self.model.setData(model_idx, str(sinfo), Qt.ItemDataRole.DisplayRole)
                        if idx in graph:
                            graph.pop(idx)
                else:
                    if row_type == RowAliasType.Simple:
                        graph[idx].clear()
                        continue

                    logger.debug(f"Is a mixed alias")
                    for k in depends_on:
                        try:
                            alias_idx = aliases.index(k)
                            if alias_idx == idx:
                                sinfo = StatusInfo()
                                sinfo.result = Result.INVALID
                                sinfo.msg = f"Conflicted row: {idx + 1} , '{aliases[idx]}' short circuit in '{name}'"
                                error_msgs.append(sinfo.msg)
                                self.model.setData(model_idx, str(sinfo), Qt.ItemDataRole.DisplayRole)
                                break
                            elif idx not in graph[alias_idx]:
                                graph[idx].append(aliases.index(k))
                            else:
                                sinfo = StatusInfo()
                                sinfo.result = Result.INVALID
                                sinfo.msg = f"Conflicted row: {idx + 1} , circular dependency with alias '{k}'"
                                error_msgs.append(sinfo.msg)
                                self.model.setData(model_idx, str(sinfo), Qt.ItemDataRole.DisplayRole)
                                break
                        except ValueError:
                            continue

        if error_msgs:
            error_msg = '\n----\n'.join(error_msgs)
            self._abort_build(error_msg)
            return

        # Traverse the graph's edges.
        with self._model.init_create_signals():
            num_keys = len(graph.keys())
            for i, k in enumerate(graph.keys()):
                self.set_status_message(f"Creating signals | row: {k}")
                self.set_progress(int(i * 100 / num_keys))
                yield from self._traverse(graph, k)

        self._model.layoutChanged.emit()
        self._model.aliases = []
        self._processed.clear()
        self.set_progress(100)
        self.ready.emit()
        self.end_build()

    def _traverse(self, graph: typing.DefaultDict[int, typing.List[int]], row_idx: int) -> \
            typing.Iterator[Waypoint]:

        for idx in graph[row_idx]:
            if idx in self._processed:
                continue
            else:
                yield from self._traverse(graph, idx)
        else:
            if row_idx not in self._processed:
                self._processed.add(row_idx)
                yield from self._model.create_signals(row_idx, self.invalid_stacks)

    def check_stack_table(self, df):
        # Filter dataframe and extract valid values for 'Stack' and 'Plot Type' columns
        df_filtered = df[df['Stack'] != ""]
        stack_valid = df_filtered['Stack'].tolist()
        plot_types_valid = df_filtered['Plot type'].replace("", "PlotXY").tolist()  # Check if "" in column Plot Type

        # Create new dataframe to validate stacks against their plot types
        data = pd.DataFrame({
            "Stack": stack_valid,
            "PlotType": plot_types_valid
        })

        def is_invalid(group):
            # Identify invalid stacks
            types = group['PlotType']

            # Rule #1: Stacked plots must contain only PlotXY types
            if group["Stack"].values[0].count(".") > 1 and not all(types == 'PlotXY'):
                return True

            # Rule #2: A PlotContour or PlotContourWithSlider stack cannot contain more than one signal
            if (all(types == 'PlotContour') or all(types == 'PlotContourWithSlider')) and len(types) > 1:
                return True

            # Rule #3: Mixing different plot types in the same stack is not allowed
            if types.nunique() != 1:
                return True

        # Filter invalid stacks
        self.invalid_stacks = (
            data.groupby('Stack')
            .filter(is_invalid)['Stack']
            .unique()
        )

    def begin_build(self):
        self.showProgress.emit()
        QCoreApplication.instance().processEvents()

    def end_build(self):
        self.hideProgress.emit()
        QCoreApplication.instance().processEvents()

    def set_status_message(self, msg):
        self.statusChanged.emit(msg)

    def set_progress(self, value: int):
        self.progressChanged.emit(value)


def main():
    import argparse
    from PySide6.QtWidgets import QApplication

    parser = argparse.ArgumentParser('Quick demonstration of Signal Table')
    parser.add_argument('-b', '--blueprint', help="Path to blueprint.json file", type=str, required=False)
    parser.add_argument('-i', '--input', help="Path to input *.scsv file", type=str, required=False)
    args = parser.parse_args()

    app = QApplication([])

    if args.blueprint:
        blueprint = json.load(args.blueprint)
    else:
        blueprint = mtBp.DEFAULT_BLUEPRINT

    main_win = QMainWindow()
    progress_bar = QProgressBar(main_win)
    main_win.statusBar().addPermanentWidget(progress_bar)
    progress_bar.hide()

    sig_table = MTSignalConfigurator(blueprint=blueprint, parent=main_win)
    sig_table.statusChanged.connect(main_win.statusBar().showMessage)
    sig_table.progressChanged.connect(progress_bar.setValue)
    sig_table.showProgress.connect(progress_bar.show)
    sig_table.hideProgress.connect(progress_bar.hide)
    sig_table.buildAborted.connect(show_msg)

    main_win.setCentralWidget(sig_table)

    if isinstance(args.input, str) and args.input.endswith('.scsv'):
        sig_table.import_scsv(args.input)
    main_win.show()
    main_win.resize(1280, 720)
    sys.exit(app.exec_())


def show_msg(message):
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("Table parse failed")
    box.setText(message)
    box.exec_()


def show_popup_msg(title: str, message: str):
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    box.exec_()
