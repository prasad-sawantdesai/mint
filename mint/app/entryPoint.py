# Description: The entry point to MINT application.
#              All modules are imported only if runApp is actually called. This removes spurious import warnings
#              when all you run is mint -h to display the usage.
# Author: Jaswant Sai Panchumarti


from datetime import datetime, timedelta
import json
import sys

from PySide6.QtWidgets import QApplication


def run_app(q_app: QApplication, args=None):
    if args is None:
        return

    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QLabel

    from iplotlib.core import Canvas
    from iplotlib.interface.iplotSignalAdapter import AccessHelper
    from iplotDataAccess.appDataAccess import AppDataAccess
    import iplotLogging.setupLogger as SetupLog

    from mint.models import MTGenericAccessMode
    from mint.models.utils import mtBlueprintParser
    from mint.gui.mtMainWindow import MTMainWindow
    from mint.tools.icon_loader import create_pxmap
    from mint.app.dirs import DEFAULT_DATA_DIR
    from importlib import metadata

    iplotlib_version = metadata.version('iplotlib')

    logger = SetupLog.get_logger(__name__)

    # Remove older logs
    SetupLog.delete_older_logs(logger)
    SetupLog.delete_older_dumps(logger)

    def export_to_file(impl: str, canvas_exported: Canvas, canvas_filename, **kwargs):
        try:
            if impl.lower() == "matplotlib":
                import matplotlib
                matplotlib.rcParams["figure.dpi"] = kwargs.get('dpi')
                from iplotlib.impl.matplotlib.matplotlibCanvas import MatplotlibParser

                mpl_canvas = MatplotlibParser()
                mpl_canvas.export_image(canvas_filename, canvas=canvas_exported, **kwargs)
            elif impl.lower() == "pyqt":
                from iplotlib.impl.pyqtgraph.pyQtGraphCanvas import PyQtGraphParser

                pg_canvas = PyQtGraphParser()
                pg_canvas.export_image(canvas_filename, canvas=canvas_exported, **kwargs)
        except FileNotFoundError:
            logger.error(f"Unable to open file: {canvas_filename}")

    logger.info("Running version {} iplotlib version {}".format(q_app.applicationVersion(), iplotlib_version))
    if not AppDataAccess.initialize():
        logger.error("no data sources found, exiting")
        sys.exit(-1)

    AccessHelper.da = AppDataAccess.get_data_access()
    # da.udahost = os.environ.get('UDA_HOST') or "io-SetupLog-udafe01.iter.org"
    canvas_impl = args.impl

    workspace_file = args.json_file

    #########################################################################
    # 2. Plot canvas
    canvas = Canvas()

    #########################################################################
    # 3. Prepare model for data range
    t_now = datetime.utcnow().isoformat(timespec='seconds')
    t_now_delta_seven_d = datetime.utcnow() - timedelta(minutes=5)

    time_model = {
        "range": {
            "mode": MTGenericAccessMode.TIME_RANGE,
            "value": [t_now_delta_seven_d.isoformat(timespec='seconds'), t_now]}
    }

    if args.blueprint_file:
        try:
            blueprint = json.load(args.blueprint_file)
        except Exception as e:
            logger.warning(f"Exception {e} occurred for blueprint file: {args.blueprint_file}")
            blueprint = mtBlueprintParser.DEFAULT_BLUEPRINT
    else:
        blueprint = mtBlueprintParser.DEFAULT_BLUEPRINT

    logger.debug(f"Detected {len(QGuiApplication.screens())} screen (s)")
    max_width = 0
    for screen in QGuiApplication.screens():
        max_width = max(screen.geometry().width(), max_width)
    logger.debug(f"Detected max screen width: {max_width}")
    AccessHelper.num_samples = max_width
    AccessHelper.num_samples_override = args.use_fallback_samples
    logger.info(f"Fallback dec_samples : {AccessHelper.num_samples}")

    data_sources = AccessHelper.da.get_connected_data_source_names()

    main_win = MTMainWindow(canvas,
                            AccessHelper.da,
                            time_model,
                            app_version=q_app.applicationVersion(),
                            data_dir=DEFAULT_DATA_DIR,
                            data_sources=data_sources,
                            blueprint=blueprint,
                            impl=canvas_impl)

    main_win.setWindowTitle(f"{q_app.applicationName()}: {q_app.applicationPid()}")
    main_win.statusBar().addPermanentWidget(
        QLabel("MINT version {} iplotlib {} |".format(q_app.applicationVersion(), iplotlib_version)))

    # Preload the table from a SCSV file, if provided
    if args.scsv_file:
        main_win.sigCfgWidget.import_scsv(args.scsv_file)

    if args.last_dump:
        main_win.sigCfgWidget.import_last_dump()

    if workspace_file:
        main_win.import_json(workspace_file)

        if args.image_file:
            export_to_file(canvas_impl, main_win.canvas, args.image_file, dpi=args.export_dpi,
                           width=args.export_width, height=args.export_height)
            exit(0)

    main_win.show()
    app_icon = QIcon()
    for i in range(4, 9):
        sz = 1 << i
        app_icon.addPixmap(create_pxmap(f"mint{sz}x{sz}"))
    q_app.setWindowIcon(app_icon)

    return q_app.exec_()
