# Description: A main window embedding a table of signals' description on the right
#               and a Qt iplotlib canvas on the left.
# Author: Piotr Mazur
# Changelog:
#  Sept 2021: Refactored ui design classes [Jaswant Sai Panchumarti]

import weakref
from collections import defaultdict
from dataclasses import fields
from datetime import datetime
from pathlib import Path
import json
import os
import pkgutil
import socket
import typing
import pandas as pd

from PySide6.QtCore import QCoreApplication, QMargins, QModelIndex, QTimer, Qt, QItemSelectionModel
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence, QPixmap, QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, \
    QSplitter, QVBoxLayout, QWidget

from iplotDataAccess.dataAccess import DataAccess
from iplotlib.core.axis import LinearAxis
from iplotlib.core.canvas import Canvas
from iplotlib.core.plot import Plot, PlotXY, PlotContour, PlotXYWithSlider, PlotContourWithSlider, PlotImage
from iplotlib.core.signal import SignalXY
from iplotlib.data_access import CanvasStreamer
from iplotlib.interface.iplotSignalAdapter import ParserHelper
from iplotlib.qt.gui.iplotQtMainWindow import IplotQtMainWindow

from mint.gui.mtAbout import MTAbout
from mint.gui.mtDataRangeSelector import MTDataRangeSelector
from mint.gui.mtMemoryMonitor import MTMemoryMonitor
from mint.gui.mtStatusBar import MTStatusBar
from mint.gui.mtStreamConfigurator import MTStreamConfigurator
from mint.gui.mtSignalConfigurator import MTSignalConfigurator
from mint.gui.mtExportConfigurator import MTExportConfigurator
from mint.models.utils import mtBlueprintParser
from mint.tools.map_tricks import delete_keys_from_dict
from mint.tools.sanity_checks import check_data_range
from mint.gui.shift_handlers import ShiftHandlerMixin

from iplotLogging import setupLogger as setupLog

logger = setupLog.get_logger(__name__)


class MTMainWindow(ShiftHandlerMixin, IplotQtMainWindow):

    def __init__(self,
                 canvas: Canvas,
                 da: DataAccess,
                 model: dict,
                 app_version: str,
                 data_dir: os.PathLike = '.',
                 data_sources=None,
                 blueprint: dict = mtBlueprintParser.DEFAULT_BLUEPRINT,
                 impl: str = "matplotlib",
                 parent: typing.Optional[QWidget] = None,
                 flags: Qt.WindowFlags = Qt.WindowFlags()):

        if data_sources is None:
            data_sources = []
        self.canvas = canvas
        self.da = da
        self.plot_classes = {"PlotXY": PlotXY,
                             "PlotContour": PlotContour,
                             "PlotXYWithSlider": PlotXYWithSlider,
                             "PlotContourWithSlider": PlotContourWithSlider,
                             "PlotImage": PlotImage}
        self.appVersion = app_version
        self.dragItem = None
        try:
            blueprint['DataSource']['default'] = data_sources[0]
        except IndexError:
            pass
        except KeyError:
            logger.error('Blueprint does not have a DataSource key!')
            QCoreApplication.exit(-1)

        check_data_range(model)
        self.model = model
        self.sigCfgWidget = MTSignalConfigurator(blueprint=blueprint, scsv_dir=os.path.join(data_dir, 'scsv'),
                                                 data_sources=data_sources)
        self.dataRangeSelector = MTDataRangeSelector(self.model.get("range"), )

        self._data_dir = os.path.join(data_dir, 'workspaces')
        self._data_export_dir = os.path.join(data_dir, 'data_signals')
        self._progressBar = QProgressBar()
        self._statusBar = MTStatusBar()

        super().__init__(parent=parent, flags=flags)

        # Console button and Icon
        self.console_button = QPushButton()
        console_pxmap = QPixmap()
        console_pxmap.loadFromData(pkgutil.get_data('mint.gui', 'icons/terminal.png'))
        self.console_button.setIcon(QIcon(console_pxmap))

        self.refreshTimer = QTimer(self)
        self.refreshTimer.setTimerType(Qt.TimerType.CoarseTimer)
        self.refreshTimer.setSingleShot(False)
        self.refreshTimer.timeout.connect(lambda: self.on_timeout())
        self._memoryMonitor = MTMemoryMonitor(parent=self, pid=QCoreApplication.instance().applicationPid())
        self.sigCfgWidget.setParent(self)
        self.dataRangeSelector.setParent(self)
        self._statusBar.setParent(self)
        self._progressBar.setParent(self)
        self._progressBar.setMinimum(0)
        self._progressBar.setMaximum(100)
        self._progressBar.hide()
        self._workspaceLabel = QLabel("No workspace loaded")
        self._statusBar.addPermanentWidget(self._progressBar)
        self._statusBar.addPermanentWidget(QLabel('|'))
        self._statusBar.addPermanentWidget(self.console_button)
        self._statusBar.addPermanentWidget(QLabel('|'))
        self._statusBar.addPermanentWidget(self._memoryMonitor)
        self._statusBar.addPermanentWidget(QLabel('|'))
        self._statusBar.addPermanentWidget(self._workspaceLabel)
        self._statusBar.addPermanentWidget(QLabel('|'))

        self.graphicsArea = QWidget(self)
        self.graphicsArea.setLayout(QVBoxLayout())
        self.graphicsArea.layout().addWidget(self.toolBar)
        self.graphicsArea.layout().addWidget(self.canvasStack)
        self.streamerCfgWidget = MTStreamConfigurator(self)
        self.exportCfgWidget = MTExportConfigurator(self)
        self.aboutMINT = MTAbout(self)
        self.setAcceptDrops(True)

        if impl.lower() == "matplotlib":
            from iplotlib.impl.matplotlib.qt.qtMatplotlibCanvas import QtMatplotlibCanvas
            self.qtcanvas = QtMatplotlibCanvas(tight_layout=True, canvas=self.canvas, parent=self.canvasStack)
        elif impl.lower() == "vtk":
            from iplotlib.impl.vtk.qt import QtVTKCanvas
            self.qtcanvas = QtVTKCanvas(canvas=self.canvas, parent=self.canvasStack)
        elif impl.lower() == "pyqt":
            from iplotlib.impl.pyqtgraph.qt import QtPyQtGraphCanvas
            self.qtcanvas = QtPyQtGraphCanvas(canvas=self.canvas, parent=self.canvasStack)
        self.canvasStack.addWidget(self.qtcanvas)
        self.qtcanvas.dropSignal.connect(self.on_drop_plot)
        self._connect_shift_signals()
        self._init_shift_storage()
        self.sigCfgWidget.model.rowsAboutToBeRemoved.connect(
            self._on_rows_about_to_be_removed)

        file_menu = self.menuBar().addMenu("&File")
        help_menu = self.menuBar().addMenu("&Help")

        exit_action = QAction("Exit", self.menuBar())
        exit_action.setShortcuts(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(QApplication.closeAllWindows)

        about_qt_action = QAction("About Qt", self.menuBar())
        about_qt_action.setStatusTip("About Qt")
        about_qt_action.triggered.connect(QApplication.aboutQt)

        about_action = QAction("About MINT", self.menuBar())
        about_action.setStatusTip("About MINT")
        about_action.triggered.connect(self.aboutMINT.exec_)

        clear_cache_action = QAction("Clear cache", self.menuBar())
        clear_cache_action.setStatusTip("Clear cache")
        clear_cache_action.triggered.connect(self.da.clear_cache)

        help_menu.addAction(clear_cache_action)
        help_menu.addAction(about_action)
        help_menu.addAction(about_qt_action)

        # QAction console widget
        show_console_action = QAction(QIcon(console_pxmap), "&Show Console", self)
        show_console_action.triggered.connect(self.sigCfgWidget.console.show_console)
        self.console_button.clicked.connect(self.sigCfgWidget.console.show_console)

        file_menu.addAction(self.sigCfgWidget.tool_bar().openAction)
        file_menu.addAction(self.sigCfgWidget.tool_bar().saveAction)
        file_menu.addAction(self.toolBar.importAction)
        file_menu.addAction(self.toolBar.exportAction)
        file_menu.addAction(self.toolBar.saveImageAction)
        file_menu.addAction(show_console_action)
        file_menu.addAction(exit_action)

        self.drawBtn = QPushButton("Draw")
        pxmap = QPixmap()
        pxmap.loadFromData(pkgutil.get_data('mint.gui', 'icons/plot.png'))
        self.drawBtn.setIcon(QIcon(pxmap))
        self.streamBtn = QPushButton("Stream")
        self.streamBtn.setIcon(QIcon(pxmap))
        self.exportBtn = QPushButton("Export")
        self.exportBtn.setIcon(QIcon(pxmap))
        self.daWidgetButtons = QWidget(self)
        self.daWidgetButtons.setLayout(QHBoxLayout())
        self.daWidgetButtons.layout().setContentsMargins(QMargins())
        self.daWidgetButtons.layout().addWidget(self.streamBtn)
        self.daWidgetButtons.layout().addWidget(self.drawBtn)
        self.daWidgetButtons.layout().addWidget(self.exportBtn)

        self.dataAccessWidget = QWidget(self)
        self.dataAccessWidget.setLayout(QVBoxLayout())
        self.dataAccessWidget.layout().setContentsMargins(QMargins())
        self.dataAccessWidget.layout().addWidget(self.dataRangeSelector)
        self.dataAccessWidget.layout().addWidget(self.sigCfgWidget)
        self.dataAccessWidget.layout().addWidget(self.daWidgetButtons)

        self._centralWidget = QSplitter(self)
        self._centralWidget.setOrientation(Qt.Orientation.Horizontal)
        self._centralWidget.addWidget(self.dataAccessWidget)
        self._centralWidget.addWidget(self.graphicsArea)
        self.setCentralWidget(self._centralWidget)
        self.setStatusBar(self._statusBar)

        # Setup connections
        self.drawBtn.clicked.connect(self.draw_clicked)
        self.streamBtn.clicked.connect(self.stream_clicked)
        self.exportBtn.clicked.connect(self.export_clicked)
        self.streamerCfgWidget.streamStarted.connect(self.on_stream_started)
        self.streamerCfgWidget.streamStopped.connect(self.on_stream_stopped)
        self.exportCfgWidget.exportStarted.connect(self.on_export_started)
        self.exportCfgWidget.ui.browseExport.connect(self.export_file)
        self.dataRangeSelector.cancelRefresh.connect(self.stop_auto_refresh)
        self.resize(1920, 1080)

    def wire_connections(self):
        super().wire_connections()
        self.sigCfgWidget.statusChanged.connect(self._statusBar.showMessage)
        self.sigCfgWidget.buildAborted.connect(self.on_table_abort)
        self.sigCfgWidget.showProgress.connect(self._progressBar.show)
        self.sigCfgWidget.hideProgress.connect(self._progressBar.hide)
        self.sigCfgWidget.busy.connect(self.indicate_busy)
        self.sigCfgWidget.ready.connect(self.indicate_ready)
        self.sigCfgWidget.progressChanged.connect(self._progressBar.setValue)
        self.toolBar.exportAction.triggered.connect(self.on_export)
        self.toolBar.exportDataAction.triggered.connect(self.on_export_data)
        self.toolBar.importAction.triggered.connect(self.on_import)

    @staticmethod
    def on_table_abort(message):
        logger.error(message)
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Table Build Failed")
        box.setText(message)
        box.exec_()

    def detach(self):
        if self.toolBar.detachAction.text() == 'Detach':
            # we detach now.
            self._floatingWindow.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolBar)
            self.graphicsArea.setLayout(QVBoxLayout())
            self.graphicsArea.layout().addWidget(self.canvasStack)
            self._floatingWindow.setCentralWidget(self.graphicsArea)
            self._floatingWindow.setWindowTitle(self.windowTitle())
            self._floatingWindow.show()
            self.toolBar.detachAction.setText('Reattach')
            self.sigCfgWidget.resize_views_to_contents()
        elif self.toolBar.detachAction.text() == 'Reattach':
            # we attach now.
            self.toolBar.detachAction.setText('Detach')
            self.graphicsArea.setLayout(QVBoxLayout())
            self.graphicsArea.layout().addWidget(self.toolBar)
            self.graphicsArea.layout().addWidget(self.canvasStack)
            self._centralWidget.addWidget(self.graphicsArea)
            self.setCentralWidget(self._centralWidget)
            self._floatingWindow.hide()

    def update_canvas_preferences(self):
        self.indicate_busy('Applying preferences...')
        super().update_canvas_preferences()
        self.indicate_ready()

    def reset_prefs(self):
        self.indicate_busy('Resetting preferences...')
        super().reset_prefs()
        self.indicate_ready()

    def re_draw(self):
        self.indicate_busy('Redrawing...')
        super().re_draw()
        self.indicate_ready()

    def save_canvas_image(self):
        w = self.canvasStack.currentWidget()
        if not w:
            return
        file_filter = "PNG Image (*.png);;SVG Image (*.svg);;JPEG Image (*.jpg *.jpeg)"
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Save Canvas as Image", dir=self._data_dir, filter=file_filter)
        if filename:
            if not any(filename.lower().endswith(ext) for ext in ('.png', '.svg', '.jpg', '.jpeg')):
                if 'SVG' in selected_filter:
                    filename += '.svg'
                elif 'JPEG' in selected_filter:
                    filename += '.jpg'
                else:
                    filename += '.png'
            self._data_dir = os.path.dirname(filename)
            w.save_canvas_image(filename)

    def on_export(self):
        file = QFileDialog.getSaveFileName(
            self, "Save workspaces as ..", dir=self._data_dir, filter='*.json')
        if file and file[0]:
            if not file[0].endswith('.json'):
                file_name = file[0] + '.json'
            else:
                file_name = file[0]
            self.export_json(file_name)
            self._data_dir = os.path.dirname(file_name)

    def on_export_data(self):
        directory = self._data_export_dir + f"/DataExport_{datetime.now().strftime('%Y%m%d')}.csv"
        file = QFileDialog.getSaveFileName(self, "Save Data as ..", dir=directory, filter='*.csv')
        if file and file[0]:
            if not file[0].endswith('.csv'):
                file_name = file[0] + '.csv'
            else:
                file_name = file[0]
            self._data_export_dir = os.path.dirname(file_name)
            self.export_data_plots(file_name)

    def on_import(self):
        file = QFileDialog.getOpenFileName(self, "Open a workspace ..", dir=self._data_dir)
        if file and file[0]:
            self._data_dir = os.path.dirname(file[0])
            self.import_json(file[0])

    def indicate_busy(self, msg='Hang on ..'):
        self._progressBar.setMinimum(0)
        self._progressBar.setMaximum(0)
        self._progressBar.show()
        self.statusBar().showMessage(msg)
        QCoreApplication.processEvents()

    def indicate_ready(self):
        self._progressBar.hide()
        self._progressBar.setMinimum(0)
        self._progressBar.setMaximum(100)
        self.statusBar().showMessage('Ready.')
        QCoreApplication.processEvents()

    def export_dict(self) -> dict:
        self.indicate_busy('Exporting workspace...')
        workspace = {}
        workspace.update({
            '_metadata': {
                'createdAt': datetime.now().isoformat(),
                'createdBy': os.getlogin(),
                'createdOnHost': socket.gethostname(),
                'appVersion': self.appVersion
            }
        })
        workspace.update({'data_range': self.dataRangeSelector.export_dict()})
        workspace.update({'signal_cfg': self.sigCfgWidget.export_dict()})
        workspace.update({'main_canvas': self.canvasStack.currentWidget().export_dict()})
        self.indicate_ready()
        return workspace

    def validate_imported_canvas(self):
        for col in self.canvas.plots:
            for idx_plot, plot in enumerate(col):
                if plot:
                    remove_key = []
                    for key, values in plot.signals.items():
                        plot.signals[key] = [signal for signal in values if
                                             signal is not None and signal.parent is not None]
                        if len(plot.signals[key]) == 0:
                            remove_key.append(key)

                    for key in remove_key:
                        plot.signals.pop(key)

                    if not plot.signals.keys() or plot.parent is None:
                        col[idx_plot] = None

    def import_dict(self, input_dict: dict):
        # Close preferences window to prevent stale state
        if self.prefWindow.isVisible():
            self.prefWindow.close()

        # Clear shared parser environment and internal state to prevent memory leaks and ensure a clean rebuild
        ParserHelper.env.clear()
        self.canvasStack.currentWidget()._parser.clear()

        # Clear shift tracking since workspace import replaces the entire signal table.
        self._init_shift_storage()

        self.indicate_busy('Importing workspace...')
        data_range = input_dict.get('data_range')
        self.dataRangeSelector.import_dict(data_range)

        delete_keys_from_dict(input_dict, ['dec_samples'])

        # Remove previous slider references
        for col in self.canvas.plots:
            for plot in col:
                if isinstance(plot, PlotXYWithSlider) or isinstance(plot, PlotContourWithSlider):
                    plot.clean_slider()

        main_canvas = input_dict.get('main_canvas')
        self.canvas = Canvas.from_dict(main_canvas)

        ts, te = self.dataRangeSelector.get_time_range()
        pulse_number = self.dataRangeSelector.get_pulse_number()
        da_params = dict(ts_start=ts, ts_end=te, pulse_nb=pulse_number)

        signal_cfg = input_dict.get('signal_cfg') or input_dict
        if signal_cfg:
            self.sigCfgWidget.import_dict(signal_cfg)

        path = list(self.sigCfgWidget.build(**da_params))
        path_len = len(path)

        self.indicate_ready()
        self.sigCfgWidget.set_status_message("Update signals ..")
        self.sigCfgWidget.begin_build()
        self.sigCfgWidget.set_progress(0)

        # Clear markers table before importing new signals
        self.qtcanvas._marker_window.clear_info()

        # Clear alias list to check for fetches row
        self.sigCfgWidget.model._alias_list.clear()
        self.sigCfgWidget.model._alias_checked = False

        # Travel the path and update each signal parameters from workspace and trigger a data access request.
        valid_signal = False
        for i, waypt in enumerate(path):
            self.sigCfgWidget.set_status_message(f"Updating {waypt} ..")
            self.sigCfgWidget.set_progress(int(i * 100 / path_len))

            if (not waypt.stack_num) or (not waypt.col_num and not waypt.row_num):
                signal = waypt.func(*waypt.args, **waypt.kwargs)
                self.sigCfgWidget.model.update_signal_data(waypt.idx, signal, True)
                continue

            # Check if signal name is valid (Correct signal name and signal has data)
            signal_valid = waypt.func(*waypt.args, **waypt.kwargs)
            self.sigCfgWidget.model.update_signal_data(waypt.idx, signal_valid, True)
            if signal_valid.status_info.result == 'Fail':
                continue

            plot = self.canvas.plots[waypt.col_num - 1][waypt.row_num - 1]  # type: Plot
            if not plot:
                continue
            plot.parent = weakref.ref(self.canvas)
            old_signal = plot.signals[waypt.stack_num][waypt.signal_stack_id]

            params = dict()
            for f in fields(old_signal):
                if f.name == 'children':  # Don't copy children.
                    continue
                else:
                    params.update({f.name: getattr(old_signal, f.name)})

            # Propagate uid from row to signal for workspace without it
            if 'uid' not in params or params['uid'] is None:
                params['uid'] = waypt.kwargs['uid']

            new_signal = waypt.func(*waypt.args, signal_class=waypt.kwargs.get('signal_class'), **params)
            new_signal.parent = weakref.ref(plot)

            self.sigCfgWidget.model.update_signal_data(waypt.idx, new_signal, True)
            # Check if new signal is valid
            if new_signal.status_info.result == 'Fail':
                plot.signals[waypt.stack_num][waypt.signal_stack_id] = None
                continue

            # Indicate valid signal
            valid_signal = True

            # Replace signal
            plot.signals[waypt.stack_num][waypt.signal_stack_id] = new_signal

            # Add markers in the markers table when importing, only if the signal is SignalXY and has markers
            if isinstance(new_signal, SignalXY) and new_signal.markers_list:
                self.qtcanvas._marker_window.import_table(new_signal)

        if not valid_signal:
            # This means that there are not valid signals to create
            self.canvas.plots = [[]]
        else:
            self.validate_imported_canvas()

        self.sigCfgWidget.set_progress(99)

        self.sigCfgWidget.model.dataChanged.emit(self.sigCfgWidget.model.index(0, 0),
                                                 self.sigCfgWidget.model.index(
                                                     self.sigCfgWidget.model.rowCount(QModelIndex()) - 1,
                                                     self.sigCfgWidget.model.columnCount(QModelIndex()) - 1))

        self.sigCfgWidget.set_progress(100)

        self.indicate_busy('Drawing...')
        self.canvasStack.currentWidget().set_canvas(self.canvas)
        self.canvasStack.refreshLinks()
        # Compute statistics when importing workspace
        if path:
            self.canvasStack.currentWidget().stats(self.canvas)
        self.drop_history()  # clean zoom history
        self.indicate_ready()
        self.sigCfgWidget.resize_views_to_contents()

    def import_json(self, file_path: str):
        self.statusBar().showMessage(f"Importing {file_path} ..")
        try:
            logger.info(f"Loading workspace: {file_path}")
            with open(file_path, mode='r') as f:
                payload = f.read()
                payload = payload.replace("data_access.dataAccessSignal.DataAccessSignal",
                                          "interface.iplotSignalAdapter.IplotSignalAdapter")
                replacements = {'varname': 'name',
                                'datasource': 'data_source',
                                'pulsenb': 'pulse_nb',
                                'time_model': 'data_range'
                                }
                for old, new in replacements.items():
                    payload = payload.replace(old, new)
                self.import_dict(json.loads(payload, object_hook=lambda d: {int(k) if k.lstrip('-').isdigit() else k: v
                                                                            for k, v in d.items()}))
                logger.info(f"Finished loading workspace {file_path}")
                # Update the workspace label in the status bar after successful import
                self._workspaceLabel.setText(os.path.basename(file_path))
                self._workspaceLabel.setToolTip(file_path)
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"Error {str(e)}: cannot import workspace from file: {file_path}")
            logger.exception(e)
            box.exec_()
            self.indicate_ready()
            return

    def export_data_plots(self, file_path: str):
        self.statusBar().showMessage(f"Exporting {file_path} ..")
        try:
            with open(file_path, mode='w') as f:
                f.write(self.canvas.get_signals_as_csv())
            logger.info(f"Finished exporting data {file_path}")
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"Error {str(e)}: cannot export data plots to file: {file_path}")
            logger.exception(e)
            box.exec_()
            self.indicate_ready()
            return

    def export_json(self, file_path: str):
        self.statusBar().showMessage(f"Exporting {file_path} ..")
        try:
            with open(file_path, mode='w') as f:
                f.write(json.dumps(self.export_dict()))
            logger.info(f"Finished exporting workspace {file_path}")
        except Exception as e:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"Error {str(e)}: cannot export workspace to file: {file_path}")
            logger.exception(e)
            box.exec_()
            self.indicate_ready()
            return

    def start_auto_refresh(self):
        if self.canvas.auto_refresh:
            logger.info(F"Scheduling canvas refresh in {self.canvas.auto_refresh} seconds")
            self.refreshTimer.start(self.canvas.auto_refresh * 1000)
            self.dataRangeSelector.refreshActivate.emit()

    def stop_auto_refresh(self):
        self.dataRangeSelector.refreshDeactivate.emit()
        if self.refreshTimer is not None:
            self.refreshTimer.stop()

    def draw_clicked(self, no_build: bool = False):
        """This function creates and draws the canvas getting data from variables table and time/pulse widget"""

        if self.streamerCfgWidget.is_activated():
            return

        # Close preferences window to prevent stale state (same as import_dict)
        if self.prefWindow.isVisible():
            self.prefWindow.close()

        if not no_build:
            # Dumps are done before canvas processing
            dump_dir = os.path.expanduser("~/.local/1Dtool/dumps/")
            Path(dump_dir).mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"signals_table_{os.getpid()}_{timestamp}.scsv"
            self.sigCfgWidget.export_scsv(os.path.join(dump_dir, file_name))

            self.build()

        self.indicate_busy("Drawing...")
        self.stop_auto_refresh()

        self.canvasStack.currentWidget().unfocus_plot()
        self.canvasStack.currentWidget().set_canvas(self.canvas)
        self.canvasStack.refreshLinks()
        self.canvasStack.currentWidget().check_markers(self.canvas)
        # Compute statistics when drawing
        self.canvasStack.currentWidget().stats(self.canvas)

        self.prefWindow.update()
        if self.prefWindow.isVisible():
            self.prefWindow.treeView.selectionModel().select(self.prefWindow.treeView.model().index(0, 0),
                                                             QItemSelectionModel.Select)

        self.drop_history()  # clean zoom history
        self.start_auto_refresh()
        self.indicate_ready()

    def stream_clicked(self):
        """This function shows the streaming dialog and then creates a canvas that is used when streaming"""
        if self.streamerCfgWidget.is_activated():
            self.streamBtn.setText("Stopping")
            self.streamerCfgWidget.stop()
        else:
            self.streamerCfgWidget.show()

    def export_clicked(self):
        """This function shows the export dialog and then creates a file with the correspondence format that contains
        info of the whole canvas"""
        self.exportCfgWidget.show()

    def stream_callback(self, signal):
        self.canvasStack.currentWidget()._parser.process_ipl_signal(signal)

    def on_stream_started(self):
        self.streamerCfgWidget.hide()
        self.streamBtn.setText("Stop")
        self.build(stream=True)

        self.indicate_busy('Connecting to stream and drawing...')
        self.canvasStack.currentWidget().unfocus_plot()
        self.canvasStack.currentWidget().set_canvas(self.canvas)
        self.canvasStack.refreshLinks()

        self.streamerCfgWidget.streamer = CanvasStreamer(self.da)
        self.streamerCfgWidget.streamer.start(self.canvas, self.stream_callback)
        self.indicate_ready()

    def on_stream_stopped(self):
        self.streamBtn.setText("Stream")

    def export_file(self):
        file, export_filter = QFileDialog.getSaveFileName(self,
                                                          "Export file as ..",
                                                          dir=self._data_export_dir,
                                                          filter="HDF5 (*.hdf5);;Parquet (*.parquet)")
        if not file:
            return

        ext = export_filter.split("*.")[-1].rstrip(")")
        if not file.endswith(f'.{ext}'):
            file += f'.{ext}'

        self._data_export_dir = os.path.dirname(file)
        self.exportCfgWidget.ui.pathLineEdit.setText(file)

    def on_export_started(self, data: dict):
        self.exportCfgWidget.hide()
        logger.warning(f"Export will be performed using the global time settings. Custom time and processing columns "
                       f"will not be applied")

        ts, te = self.dataRangeSelector.get_time_range()
        pulse_number = self.dataRangeSelector.get_pulse_number()
        export_format = data['format'].split(".")[-1]
        chunks = data['chunks']
        output_path = data['output_path']

        if not output_path or export_format not in ["parquet", "hdf5"]:
            self.show_popup_msg("Invalid export configuration",
                                "Please provide a valid output path and select a supported export format (parquet or hdf5)",
                                QMessageBox.Icon.Critical)
            self.exportCfgWidget.ui.pathLineEdit.clear()
            self.indicate_ready()
            return

        table, errors = self.sigCfgWidget.model.export_information()
        self.indicate_busy('Exporting canvas information...')

        if table.empty:
            error_details = "\n".join(f"- {e}" for e in errors)
            logger.warning("Export failed: no data available to export")
            self.show_popup_msg("Export failed: no data available to export",
                                f"No data could be exported due to:\n{error_details}",
                                QMessageBox.Icon.Critical)
            self.exportCfgWidget.ui.pathLineEdit.clear()
            self.indicate_ready()
            return

        for ds_name, group in table.groupby('DS'):
            filename = f"{ds_name}.csv"
            export_group = group[['Variable', 'Comment']]
            export_group.to_csv(filename, index=False, sep=',', header=False)
            conn = self.da.ds_list[ds_name]

            if conn.source_type != "CODAC_UDA":
                msg = f"The data source: '{ds_name}' is invalid. Only CODAC UDA data sources can be exported"
                errors.append(msg)
                logger.warning(msg)
                continue

            from iplotDataAccess.dataHandling.exportData.exportData import generateData

            # Pulse mode
            if pulse_number is not None:
                for pulse in pulse_number:
                    result = conn.get_pulse_info(pulse)
                    ts_str = f"{result.timeFrom}"
                    te_str = f"{result.timeTo}"
                    ts_iso = pd.to_datetime(result.timeFrom).isoformat()
                    ts_iso = ts_iso.replace(':', '-')

                    original_path = Path(output_path)
                    valid_name = f"{ds_name}_{ts_iso}_{original_path.stem}{original_path.suffix}"
                    new_path = original_path.with_name(valid_name)

                    # Use of export data script
                    valid = generateData(logfile=None, conn=conn, csvfile=filename, formatType=export_format,
                                         startTime=ts_str, endTime=te_str, outputFolder=new_path, chunkS=chunks)
                    if valid:
                        logger.info(f"Export successful for data source: {ds_name} (format: {export_format} | Path: {new_path}")
                    else:
                        logger.info(f"Export failed for data source: {ds_name}")
            else:
                ts_str = f"{ts}"
                te_str = f"{te}"

                original_path = Path(output_path)
                valid_name = f"{ds_name}_{original_path.stem}{original_path.suffix}"
                new_path = original_path.with_name(valid_name)

                # Use of export data script
                valid = generateData(logfile=None, conn=conn, csvfile=filename, formatType=export_format,
                                     startTime=ts_str, endTime=te_str, outputFolder=new_path, chunkS=chunks)
                if valid:
                    logger.info(f"Export successful for data source: {ds_name} (format: {export_format} | Path: {new_path}")
                else:
                    logger.info(f"Export failed for data source: {ds_name}")

        if errors:
            error_details = "\n".join(f"- {e}" for e in errors)
            logger.warning("Export completed with issues:\n%s", error_details)
            self.show_popup_msg("Export completed with issues",
                                f"The following conditions prevented a full export:\n{error_details}",
                                QMessageBox.Icon.Information)
        else:
            self.show_popup_msg("Export completed",
                                "The data export process finished successfully", QMessageBox.Icon.Information)
        self.exportCfgWidget.ui.pathLineEdit.clear()
        self.indicate_ready()

    def closeEvent(self, event: QCloseEvent) -> None:
        QApplication.closeAllWindows()
        super().closeEvent(event)

    def build(self, stream=False):
        # Clear shared parser environment and internal state to prevent memory leaks and ensure a clean rebuild
        ParserHelper.env.clear()
        self.canvasStack.currentWidget()._parser.clear()

        # Clear shift tracking so stale state from previous canvas does not
        # interfere with future shift operations (UIDs are regenerated on each build).
        self._init_shift_storage()

        self.canvas.streaming = stream
        stream_window = self.streamerCfgWidget.time_window() * 1000000000

        x_axis_date = (self.dataRangeSelector.is_x_axis_date() and not stream) or stream
        x_axis_follow = stream
        x_axis_window = stream_window if stream else None
        refresh_interval = 0 if stream else self.dataRangeSelector.get_auto_refresh()
        pulse_number = None if stream else self.dataRangeSelector.get_pulse_number()

        if stream and stream_window > 0:
            now = self.dataRangeSelector.get_time_now()
            ts, te = now - stream_window, now
        else:
            ts, te = self.dataRangeSelector.get_time_range()

        # Check dates
        mode = self.dataRangeSelector.accessModes[1 if pulse_number else 0]
        valid = True
        if te != '' and ts != '' and te <= ts:
            valid = False
            mode.set_valid_date(False)
            start = pd.to_datetime(ts) if pulse_number is None else ts
            end = pd.to_datetime(te) if pulse_number is None else te
            logger.warning(f"Chronology error. EndTime: {end} must be later than StartTime: {start}")
            # self.on_table_abort()
        else:
            mode.set_valid_date(True)

        self.canvas.auto_refresh = refresh_interval

        da_params = dict(ts_start=ts, ts_end=te, pulse_nb=pulse_number)
        plan = dict()

        # Get signals in order to preserve markers
        previous_signals = {sig.uid: sig for sig in self.canvasStack.currentWidget().get_signals(self.canvas)}

        # Clear alias list to check for fetches row
        self.sigCfgWidget.model._alias_list.clear()
        self.sigCfgWidget.model._alias_checked = False

        if valid:
            for waypt in self.sigCfgWidget.build(**da_params):
                if not waypt.func and not waypt.args:
                    continue

                if not waypt.stack_num or (not waypt.col_num and not waypt.row_num):
                    signal = waypt.func(*waypt.args, **waypt.kwargs)
                    if not stream:
                        self.sigCfgWidget.model.update_signal_data(waypt.idx, signal, True)
                    continue

                signal = waypt.func(*waypt.args, **waypt.kwargs)
                if not signal.label:
                    continue

                signal.data_access_enabled = False if self.canvas.streaming else True
                signal.hi_precision_data = True if self.canvas.streaming else False
                if not stream:
                    self.sigCfgWidget.model.update_signal_data(waypt.idx, signal, True)
                else:
                    # In the case of streaming, only simple variables are kept
                    conditions = (
                        ts != signal.ts_start,
                        te != signal.ts_end,
                        signal.envelope,
                        signal.x_expr != '${self}.time',
                        signal.y_expr != '${self}.data',
                        signal.z_expr != '${self}.data_store[2]',
                        len(signal.children) > 1  # Only support one level processing
                    )
                    if any(conditions):
                        signal.stream_valid = False

                if signal.status_info.result == 'Fail':
                    continue

                # Preserve markers
                prev_signal = previous_signals.get(signal.uid)
                if isinstance(signal, SignalXY) and prev_signal and prev_signal.markers_list:
                    signal.markers_list = prev_signal.markers_list

                if waypt.col_num not in plan:
                    plan[waypt.col_num] = {}

                if waypt.row_num not in plan[waypt.col_num]:
                    plan[waypt.col_num][waypt.row_num] = {"row_span": waypt.row_span, "col_span": waypt.col_span,
                                                          "signals": defaultdict(list),
                                                          "ts_start": waypt.ts_start,
                                                          "ts_end": waypt.ts_end,
                                                          "x_axis_date": x_axis_date,
                                                          "x_axis_follow": x_axis_follow,
                                                          "x_axis_window": x_axis_window,
                                                          "streaming": self.canvas.streaming
                                                          }

                else:
                    existing = plan[waypt.col_num][waypt.row_num]
                    existing["row_span"] = waypt.row_span if waypt.row_span > existing["row_span"] else existing[
                        "row_span"]
                    existing["col_span"] = waypt.col_span if waypt.col_span > existing["col_span"] else existing[
                        "col_span"]
                    if waypt.ts_start is not None:
                        if existing["ts_start"] is None or waypt.ts_start < existing["ts_start"]:
                            existing["ts_start"] = waypt.ts_start
                    if waypt.ts_end is not None:
                        if existing["ts_end"] is None or waypt.ts_end > existing["ts_end"]:
                            existing["ts_end"] = waypt.ts_end

                plan[waypt.col_num][waypt.row_num]["signals"][waypt.stack_num].append(signal)
                # Set end time to avoid None values for EndTime in case of pulses
                if plan[waypt.col_num][waypt.row_num]["ts_end"] is None:
                    plan[waypt.col_num][waypt.row_num]["ts_end"] = signal.data_xrange[1]

        self.indicate_busy('Retrieving data...')

        # For Plots with Slider, slider callback connections are not preserved after deepcopy. Therefore, we must clear
        # the slider references from the old canvas before rebuilding it. This prevents issues related to invalid
        # callback references during redrawing.
        for col in self.canvas.plots:
            for plot in col:
                if isinstance(plot, PlotXYWithSlider) or isinstance(plot, PlotContourWithSlider):
                    plot.clean_slider()

        # Keep copy of previous canvas to be able to restore preferences
        old_canvas = self.canvas.to_dict()

        self.build_canvas(self.canvas, plan, x_axis_date, x_axis_follow, x_axis_window)

        self.indicate_busy('Applying preferences...')
        # Merge with previous preferences
        self.canvas.merge(old_canvas)

        logger.info("Built canvas")
        logger.debug(f"{self.canvas}")
        self.indicate_ready()

    def build_canvas(self, canvas: Canvas, plan: dict, x_axis_date=False, x_axis_follow=False, x_axis_window=None):
        if not plan.keys():
            self.canvas.plots = [[]]
            return
        max_col = 0
        max_row = 0
        for col, row_plots in plan.items():
            for row, plot in row_plots.items():
                max_col = max(max_col, col + plot["col_span"] - 1)
                max_row = max(max_row, row + plot["row_span"] - 1)

        canvas.cols = max_col
        canvas.rows = max_row
        canvas.plots = [[] for _ in range(canvas.cols)]

        for col, rows in plan.items():
            for row in range(1, max(rows.keys()) + 1):
                if row not in rows.keys():
                    self.canvas.add_plot(None, col=col - 1)
                    continue

                plot_types = list(
                    set(signal.plot_type for signals in rows[row]["signals"].values() for signal in signals))
                if len(plot_types) > 1 or any(value not in self.plot_classes.keys() for value in plot_types):
                    self.canvas.add_plot(None, col=col - 1)
                    continue

                x_axis_transformed = False
                for signals in rows[row]["signals"].values():
                    for signal in signals:
                        if signal.x_expr != '${self}.time':
                            x_axis_transformed = True
                            break

                if not canvas.streaming:
                    signal_x_is_date = False
                    has_date = False
                    has_non_date = False
                    for stack, signals in rows[row]["signals"].items():
                        for signal in signals:
                            try:
                                x_data = signal.get_data()[0]
                                if len(x_data) > 0:
                                    sig_is_date = bool(min(x_data) > (1 << 53) and max(x_data) < (1 << 62))
                                    signal_x_is_date |= sig_is_date
                                    if sig_is_date:
                                        has_date = True
                                    else:
                                        has_non_date = True
                                elif not x_axis_transformed and rows[row]["ts_start"] is not None:
                                    signal_x_is_date |= bool(
                                        rows[row]["ts_start"] > (1 << 53) and rows[row]["ts_end"] < (1 << 62))
                            except (IndexError, ValueError) as _:
                                signal_x_is_date = False
                    mixed_time_bases = has_date and has_non_date
                else:
                    signal_x_is_date = True
                    mixed_time_bases = False

                y_axes = [LinearAxis() for _ in range(len(rows[row]["signals"].items()))]

                x_axis = LinearAxis(is_date=x_axis_date and signal_x_is_date, follow=x_axis_follow,
                                    window=x_axis_window)

                # In case of processed signals, the limits are not set until the drawn_fn occurs
                # In the other hand, for no processed signals and for pulses the limits are set as follows:
                if not x_axis_transformed:
                    x_axis.original_begin = rows[row]["ts_start"]
                    x_axis.original_end = rows[row]["ts_end"]
                    x_axis.begin = rows[row]["ts_start"]
                    x_axis.end = rows[row]["ts_end"]

                if mixed_time_bases:
                    logger.warning(f"Plot at row {row}, col {col} contains signals with mixed time bases "
                                   f"(absolute and relative timestamps). Signals will not be drawn.")
                    plot = None
                else:
                    plot = self.plot_classes[plot_types[0]](axes=[x_axis, y_axes], row_span=rows[row]["row_span"],
                                                            col_span=rows[row]["col_span"], row=row, col=col)
                    for stack, signals in rows[row]["signals"].items():
                        for signal in signals:
                            if signal.stream_valid:
                                plot.add_signal(signal, stack=stack)

                # In case of streaming, when the plot does not contain any signals that can be streamed, the plot
                # is not added to the Canvas and None is added instead.
                if canvas.streaming and not plot.signals:
                    plot = None

                self.canvas.add_plot(plot, col=col - 1)

    def on_timeout(self):
        self.build()
        self.indicate_busy("Drawing...")

        self.canvasStack.currentWidget().unfocus_plot()
        self.canvasStack.currentWidget().set_canvas(self.canvas)
        self.canvasStack.refreshLinks()
        self.prefWindow.formsStack.currentWidget().widgetMapper.revert()
        self.prefWindow.update()
        self.canvasStack.currentWidget().stats(self.canvas)

        self.indicate_ready()

    def on_drop_plot(self, drop_info):
        dragged_item = drop_info.dragged_item
        row = drop_info.row
        col = drop_info.col
        new_data = pd.DataFrame([['codacuda', f"{dragged_item.key}", f'{col}.{row}']],
                                columns=['DS', 'Variable', 'Stack'])
        self.sigCfgWidget.append_dataframe(new_data)
        self.draw_clicked()

    def show_popup_msg(self, title: str, message: str, icon: QMessageBox.Icon):
        box = QMessageBox()
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.exec_()
