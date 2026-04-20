# Description: A light-weight translation of a dataframe to benefit QTableView
# Author: Piotr Mazur
# Changelog:
#  Sept 2021: Refactored ui design classes [Jaswant Sai Panchumarti]

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import json
import pandas as pd
import typing
import re
import uuid

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from iplotProcessing.common import InvalidExpression
from iplotlib.core import SignalXY, SignalContour
from iplotlib.interface.iplotSignalAdapter import IplotSignalAdapter, Result, ParserHelper
from iplotProcessing.tools import Parser

from mint.models.utils import mtBlueprintParser as mtBP
from mint.tools.table_parser import get_value

from iplotDataAccess.appDataAccess import AppDataAccess

import iplotLogging.setupLogger as setupLog

logger = setupLog.get_logger(__name__)


@dataclass
class Waypoint:
    idx: int = -1
    col_num: int = -1
    row_num: int = -1
    col_span: int = -1
    row_span: int = -1
    stack_num: int = -1
    signal_stack_id: int = -1
    ts_start: int = -1
    ts_end: int = -1
    func: typing.Callable = None
    args: list = None
    kwargs: dict = None

    def __str__(self):
        return f"c:{self.col_num}|r:{self.row_num}|sn:{self.stack_num}|si:{self.signal_stack_id}"


exp_stack = re.compile(r'(\d+)(?:[.](\d+))?(?:[.](\d+))?$')


class MTSignalsModel(QAbstractItemModel):
    SignalRole = Qt.ItemDataRole.UserRole + 10

    ROWUID_COLNAME = 'uid'

    def __init__(self, blueprint: dict = mtBP.DEFAULT_BLUEPRINT, parent=None):

        super().__init__(parent)
        self._white_brush = QBrush(QColor('white'))
        self._red_brush = QBrush(QColor('red'))
        self._orange_brush = QBrush(QColor('orange'))

        self._entity_attribs = None
        column_names = list(mtBP.get_column_names(blueprint))

        self._blueprint = blueprint

        # When true, do not emit `dataChanged` in `setData`. That signal brings `setData` to its knees.
        self._fast_mode = False
        mtBP.parse_raw_blueprint(self._blueprint)

        self._table = pd.DataFrame(columns=column_names)
        self._table_fails = pd.DataFrame(columns=column_names)
        self._signal_stack_ids = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self._alias_list = set()
        self._alias_checked = False

        self.data_sources = AppDataAccess.da.get_connected_data_source_names()
        self.aliases = []

    @property
    def blueprint(self) -> dict:
        return self._blueprint

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        return self.createIndex(row, column)

    def parent(self, child: QModelIndex) -> QModelIndex:
        return QModelIndex()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return self._table.index.size

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return self._table.columns.size

    def data(self, index: QModelIndex, role: int = ...):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            value = self._table.iat[index.row(), index.column()]
            column_name = self._table.columns[index.column()]
            if column_name == "Comment" and isinstance(value, str) and len(value) > 40:
                return value[:40] + "..."
            return value
        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            return self._table.iat[index.row(), index.column()]
        if role == Qt.ItemDataRole.BackgroundRole:
            fail_value = self._table_fails.iat[index.row(), index.column()]
            # Value 0 corresponds to a correct cell.
            # Value 1 corresponds to a main error.
            # Value 2 corresponds to a secondary error resulting from a main error.
            if fail_value == 0:
                return self._white_brush
            elif fail_value == 1:
                return self._red_brush
            else:
                return self._orange_brush
        # tooltip for comment column
        if role == Qt.ItemDataRole.ToolTipRole:
            # get the column name
            column_name = self._table.columns[index.column()]
            if column_name == "Comment":
                return self._table.iat[index.row(), index.column()]

        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            try:
                return self._table.columns[section]
            except IndexError:
                return "N/A"

    @contextmanager
    def activate_fast_mode(self):
        try:
            self._fast_mode = True
            yield None
        finally:
            self._fast_mode = False
            self.layoutChanged.emit()

    def setData(self, index: QModelIndex, value: typing.Any, role: int = ..., is_downsampled: bool = False) -> bool:
        if not index.isValid():
            return False
        row = index.row()
        column = index.column()
        col_name = self._table.columns[column]

        if role != Qt.ItemDataRole.EditRole and role != Qt.ItemDataRole.DisplayRole:
            return False

        if col_name != 'Comment':
            # Filter actual and literal newline/tab on interactive edit
            if not self._fast_mode and isinstance(value, str):
                # replace real CR/LF and tabs
                value = value.replace('\r\n', ' ').replace('\r', ' ')
                value = value.replace('\n', ' ').replace('\t', ' ')
                # replace literal backslash sequences
                value = value.replace('\\n', ' ').replace('\\t', ' ')
                # collapse multiple spaces
                value = re.sub(r' +', ' ', value)
        else:
            # For Comment: preserve newlines, cap at 1000 chars
            if isinstance(value, str):
                value = value[:1000]

        if isinstance(value, str):
            value = value.strip()
            if ',' in value:
                # replaces "" with '' if value has , in it.
                value = value.replace('"', "'")

        if row + 1 >= self._table.index.size:
            self.insertRows(row + 1, 1, QModelIndex())

        # Indicate if the signal is downsampled or not
        if is_downsampled:
            self._table.iloc[row, column] = value + '|Downsampled'
        else:
            self._table.iloc[row, column] = value

        if not self._fast_mode:
            self.dataChanged.emit(self.createIndex(row, column), self.createIndex(row, column))

        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if index.isValid():
            if self._table.columns[index.column()] != 'Status' and self._table.columns[
                index.column()] != 'Output Datatype':
                return Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            else:
                return Qt.ItemFlag.ItemIsEnabled

    def insertRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        self.beginInsertRows(parent, row, row + count)

        for new_row in range(row, row + count):
            # Create empty row
            data = [["" for _ in range(self._table.columns.size)]]
            data_fails = [[0 for _ in range(self._table.columns.size)]]
            empty_row = pd.DataFrame(data=data, columns=self._table.columns)
            col_data_source = mtBP.get_column_name(self.blueprint, 'DataSource')
            col_plot_type = mtBP.get_column_name(self.blueprint, 'PlotType')
            empty_row_fails = pd.DataFrame(data=data_fails, columns=self._table.columns)
            # Set default Datasource
            empty_row.loc[0, col_data_source] = self.blueprint.get('DataSource').get('default')
            # Set default PlotType
            empty_row.loc[0, col_plot_type] = self.blueprint.get('PlotType').get('default')
            # Generate uid
            empty_row.loc[0, self.ROWUID_COLNAME] = str(uuid.uuid4())
            self._table = pd.concat([self._table.iloc[:new_row], empty_row, self._table.iloc[new_row:]]).reset_index(
                drop=True)
            self._table_fails = pd.concat(
                [self._table_fails.iloc[:new_row], empty_row_fails, self._table_fails.iloc[new_row:]]).reset_index(
                drop=True)
        self.layoutChanged.emit()

        self.endInsertRows()

        return super().insertRows(row, count, parent=parent)

    def removeRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()) -> bool:
        self.beginRemoveRows(parent, row, row + count)

        try:
            self._table = self._table.drop(list(range(row, row + count)), axis=0).reset_index(drop=True)
            self._table_fails = self._table_fails.drop(list(range(row, row + count)), axis=0).reset_index(drop=True)
            self.layoutChanged.emit()
            success = True
        except KeyError:
            success = False

        self.endRemoveRows()

        return success

    def get_dataframe(self):
        filtered_rows = self._table[self._table.iloc[:, 1:-5].any(axis=1)]
        if not filtered_rows.empty:
            max_idx = filtered_rows.index[-1]
            return self._table[:max_idx + 1]
        else:
            return pd.DataFrame(columns=self._table.columns)

    def remove_empty_rows(self):
        columns = ['Variable', 'Stack', 'Row span', 'Col span', 'Envelope', 'Alias', 'PulseId', 'StartTime', 'EndTime',
                   'x', 'y', 'z', 'Plot type', 'Status']
        self._table = self._table[(self._table[columns].isnull().sum(1) + (self._table[columns] == "").sum(1)) < 14]

    def accommodate(self, df: pd.DataFrame):
        # Add missing blueprint columns with defaults (backward compatibility)
        columns = list(mtBP.get_column_names(self._blueprint))
        for col_name in columns:
            if col_name not in df.columns:
                # Find the blueprint key for this column name
                for k, v in self._blueprint.items():
                    if mtBP.get_column_name(self._blueprint, k) == col_name and 'default' in v:
                        df[col_name] = v['default']
                        break

        # Accommodate for unknown columns in df.
        for df_column_name in df.columns:
            if df_column_name not in columns:
                if df_column_name == self.ROWUID_COLNAME:
                    # Generate missing UID
                    df.insert(df.columns.size, self.ROWUID_COLNAME, [str(uuid.uuid4()) for _ in range(df.index.size)])
                else:
                    logger.warning(f"{df_column_name} is not a valid column name.")
                    if df_column_name in self._blueprint.keys():
                        df.rename({df_column_name: mtBP.get_column_name(self._blueprint, df_column_name)}, axis=1,
                                  inplace=True)
                    elif df_column_name.lower() in columns:
                        df.rename({df_column_name: df_column_name.lower()}, axis=1, inplace=True)
                    elif df_column_name.upper() in columns:
                        df.rename({df_column_name: df_column_name.upper()}, axis=1, inplace=True)
                    elif df_column_name.capitalize() in columns:
                        df.rename({df_column_name: df_column_name.capitalize()}, axis=1, inplace=True)
                    else:
                        logger.warning(f"{df_column_name} is not possible to convert the column name.")
                        df.drop(df_column_name, axis=1, inplace=True)
        return df

    def set_dataframe(self, df: pd.DataFrame):
        old_size = self.rowCount()
        self.removeRows(0, old_size)
        new_size = df.index.size
        self.insertRows(0, new_size)

        df = self.accommodate(df)

        # Force blueprint to have uid column
        if not self._blueprint.get(self.ROWUID_COLNAME):
            self._blueprint[self.ROWUID_COLNAME] = {
                "code_name": "uid",
                "default": "",
                "label": "uid",
                "type_name": "str",
                "type": str
            }
        columns = list(mtBP.get_column_names(self._blueprint))

        for c, col_name in enumerate(columns):
            if col_name in df.columns:
                self._table.iloc[:df.index.size, c] = df.loc[:, col_name]
            else:
                logger.debug(f"{col_name} is not present in given dataframe.")
                continue

    def append_dataframe(self, df: pd.DataFrame):
        if df.empty:
            return
        df = self.accommodate(df)
        df['uid'] = [str(uuid.uuid4()) for _ in range(len(df.index))]

        # Create False for fails table
        df_fails = pd.DataFrame(data=0, index=range(df.shape[0]), columns=self._table_fails.columns)

        # Check if last row is empty
        if self._table.empty or self._table.iloc[-1:, 1:-5].any(axis=1).item():
            self._table = pd.concat([self._table, df], ignore_index=True).fillna('')
            self.insertRows(len(self._table), 1, QModelIndex())
            self._table_fails = pd.concat([self._table_fails, df_fails], ignore_index=True)
            self.insertRows(len(self._table_fails), 1, QModelIndex())
        else:
            self._table = pd.concat([self._table[:-1], df, self._table[-1:]], ignore_index=True).fillna('')
            self._table_fails = pd.concat([self._table_fails[:-1], df_fails, self._table_fails[-1:]],
                                          ignore_index=True)

        self.layoutChanged.emit()

    def export_dict(self) -> dict:
        # 1. blueprint defines columns..
        output = dict()
        output.update({'blueprint': mtBP.remove_type_info(self.blueprint)})
        # 2. table
        output.update({'table': json.loads(self.get_dataframe().to_json(orient='values'))})
        return output

    def import_dict(self, input_dict: dict):
        # 1. blueprint defines columns..
        try:
            temp_blueprint = mtBP.parse_raw_blueprint(input_dict['blueprint'])
            if not temp_blueprint.get("PulseNumber").get("label"):
                temp_blueprint.get("PulseNumber").update({"label": "PulseId"})
        except KeyError:
            temp_blueprint = self.blueprint
        # 2. table
        column_names = list(mtBP.get_column_names(temp_blueprint))
        self._entity_attribs = list(mtBP.get_code_names(temp_blueprint))
        if 'table' in input_dict:
            raw = input_dict['table']
        elif 'variables_table' in input_dict:
            raw = input_dict['variables_table']
        else:
            raise Exception('No variables table in workspace!')

        df = pd.DataFrame(raw, dtype=str)
        df.columns = column_names[:df.shape[1]]
        df = df.reindex(columns=column_names, fill_value='')

        if not df.empty:
            self.set_dataframe(df)

    def export_json(self):
        return json.dumps(self.export_dict())

    def import_json(self, input_file):
        self.import_dict(json.loads(input_file))

    def check_alias_fetch(self):
        self._alias_checked = True

        table = self._table[self._table['Stack'] != ""]
        p = Parser()
        for row in range(table.shape[0]):
            for col in ['Variable', 'x', 'y', 'z']:
                variable = p.get_var_expression(table.iloc[row][col])
                if variable:
                    for var in variable:
                        if var != 'self':
                            self._alias_list.add(var)

    def update_signal_data(self, row_idx: int, signal: IplotSignalAdapter, fetch_data=False):
        with self.activate_fast_mode():
            model_idx = self.createIndex(row_idx, self._table.columns.get_loc('Status'))
            signal.status_info.reset()
            self.setData(model_idx, str(signal.status_info), Qt.ItemDataRole.DisplayRole)

            if fetch_data:
                if not self._alias_checked:
                    self.check_alias_fetch()

                # Read alias and stack from the table row
                alias_col = mtBP.get_column_name(self._blueprint, 'Alias')
                stack_col = mtBP.get_column_name(self._blueprint, 'Stack')
                alias = self._table.at[row_idx, alias_col]
                stack_val = self._table.at[row_idx, stack_col]

                # Skip rows with no stack unless their alias is used in another row's processing
                if not stack_val and alias not in self._alias_list:
                    logger.debug(f"Row {row_idx} skipped (no stack and alias {alias} is not referenced)")
                    return

                self.setData(model_idx, Result.BUSY, Qt.ItemDataRole.DisplayRole)
                signal.get_data()

                # Check Signal status
                if signal.status_info.result == 'Fail':
                    # If variable is not valid we have two cases:
                    #   1) Incorrect name
                    #   2) No data in that interval
                    index = self._table.index[self._table['uid'] == signal.uid].tolist()
                    self._table_fails.loc[index, 'Variable'] = 1

            self.setData(model_idx, str(signal.status_info), Qt.ItemDataRole.DisplayRole, signal.isDownsampled)

            # Update Output Datatype column after data is fetched
            output_dtype_col = mtBP.get_column_name(self._blueprint, 'OutputDatatype')
            if output_dtype_col in self._table.columns:
                dtype_idx = self.createIndex(row_idx, self._table.columns.get_loc(output_dtype_col))
                output_dtype_str = self._get_signal_output_datatype(signal)
                self.setData(dtype_idx, output_dtype_str, Qt.ItemDataRole.DisplayRole)

    def _get_signal_output_datatype(self, signal: IplotSignalAdapter) -> str:
        """Get a string representation of the signal's output data type."""
        if hasattr(signal, 'y_data') and signal.y_data is not None and len(signal.y_data) > 0:
            return f"{signal.y_data.dtype}"
        return ""

    @contextmanager
    def init_create_signals(self):
        try:
            self._signal_stack_ids.clear()
            self.aliases.clear()
            yield None
        finally:
            self._signal_stack_ids.clear()

    def create_signals(self, row_idx: int, stack) -> typing.Iterator[Waypoint]:
        signal_params = dict()
        # Initialize attributes for Waypoint
        col_num = row_num = col_span = row_span = stack_num = ts_start = ts_end = -1

        for i, parsed_row in enumerate(
                self._parse_series(self._table.loc[row_idx], self._table_fails.loc[row_idx], row_idx + 1, stack)):

            signal_params.update(mtBP.construct_params_from_series(self.blueprint, parsed_row[0]))
            errors = any(parsed_row[1] > 0)
            # Update Status to "Ready" if any cell is invalid.
            status_col = mtBP.get_column_name(self.blueprint, 'Status')
            sc = self._table.columns.get_loc(status_col)
            if errors:
                self._table.iat[row_idx, sc] = "Ready"
                status_idx = self.createIndex(row_idx, sc)
                self.dataChanged.emit(status_idx, status_idx)

            if i == 0:  # grab these from the first row we encounter
                if errors:  # Do not draw Plots containing errors
                    stack_val = ''
                else:
                    stack_val = signal_params.get('stack_val')
                stack_m = exp_stack.match(stack_val)

                if stack_m:
                    stack_groups = stack_m.groups()
                    row_num = int(stack_groups[0])
                    col_num = int(stack_groups[1] or '1')
                    stack_num = int(stack_groups[2] or '1')

                    bad_stack = col_num == 0 or row_num == 0 or stack_num == 0
                else:
                    bad_stack = True

                if bad_stack:
                    # In this case, the signal will only be created if there are no errors
                    stack_num = 1
                    col_num = row_num = 0
                    if errors:
                        logger.warning(f'Errors encountered during signal creation')
                        return
                    else:
                        # Message for status
                        logger.warning(f'Ignored wrong stack value: {stack_val}')

                col_span = signal_params.get('col_span') or 1
                row_span = signal_params.get('row_span') or 1
                ts_start = signal_params.get('ts_start')
                ts_end = signal_params.get('ts_end')

            if signal_params['plot_type'] == 'PlotXY' or signal_params['plot_type'] == 'PlotXYWithSlider' or \
                    signal_params['plot_type'] == 'PlotImage':
                signal_class = SignalXY
            elif signal_params['plot_type'] == 'PlotContour' or signal_params['plot_type'] == 'PlotContourWithSlider':
                signal_class = SignalContour
            else:
                continue

            waypoint = Waypoint(row_idx,
                                col_num,
                                row_num,
                                col_span,
                                row_span,
                                stack_num,
                                self._signal_stack_ids[col_num][row_num][stack_num],
                                ts_start,
                                ts_end,
                                func=mtBP.construct_signal,
                                args=[self.blueprint],
                                kwargs={'signal_class': signal_class, **signal_params}
                                )

            self._signal_stack_ids[col_num][row_num][stack_num] += 1
            yield waypoint

    def _parse_series(self, inp: pd.Series, fls: pd.Series, table_row, stack) -> typing.Iterator[pd.Series]:
        with self.activate_fast_mode():
            out = dict()

            for k, v in self._blueprint.items():
                if k.startswith('$'):
                    continue

                column_name = mtBP.get_column_name(self._blueprint, k)
                default_value = v.get('default')
                if not default_value:
                    if column_name == 'uid':
                        default_value = str(uuid.uuid4())
                    elif default_value is None:
                        default_value = ""
                out.update({column_name: default_value})

                type_func = v.get('type')
                if not callable(type_func):
                    continue

                # Override global values with locals for fields with 'override' attribute
                if v.get('override'):
                    if column_name == 'PulseId':
                        value = get_value(inp, column_name, type_func)
                        override_global = value is not None
                        if override_global:
                            plus_pattern = re.compile(r"\+\((.*)\)")
                            minus_pattern = re.compile(r"-\((.*)\)")

                            # Lists to store the elements corresponding to every pattern
                            elements = [[], [], []]

                            # Iterate over the list and classify the elements
                            for element in value:
                                match_plus = plus_pattern.match(element)
                                match_minus = minus_pattern.match(element)
                                if match_plus:
                                    pulse = match_plus.group(1)
                                    idx = 0
                                elif match_minus:
                                    pulse = match_minus.group(1)
                                    idx = 1
                                else:
                                    pulse = element
                                    idx = 2

                                # Resolve special pulse numbers (0 = last, -N = N-th previous)
                                pulse_parts = pulse.rsplit("/", 1)
                                pulse_num_str = pulse_parts[-1] if len(pulse_parts) > 1 else pulse
                                try:
                                    n = int(pulse_num_str)
                                except ValueError:
                                    n = 1  # non-numeric → not a relative pulse
                                if n <= 0 and inp['DS'] in self.data_sources:
                                    ds = AppDataAccess.da.get_data_source(inp['DS'])
                                    prefix = pulse_parts[0] + "/" if len(pulse_parts) > 1 else ""
                                    # If no prefix, infer category from global pulse
                                    if not prefix and default_value and default_value != '' and default_value != ['']:
                                        global_pulse = default_value[0] if isinstance(default_value, list) else default_value
                                        global_parts = str(global_pulse).rsplit("/", 1)
                                        if len(global_parts) > 1:
                                            prefix = global_parts[0] + "/"
                                    search = prefix + "*" if prefix else "*:*/*"
                                    all_pulses = ds.get_pulses_df(pattern=search)
                                    if not all_pulses.empty:
                                        idx = n - 1  # 0→-1, -N→-(N+1)
                                        if abs(idx) > len(all_pulses):
                                            logger.warning(
                                                f"Requested pulse '{pulse_num_str}' but only {len(all_pulses)} "
                                                f"pulse(s) available in category '{prefix or '*'}' "
                                                f"in the table row [{table_row}]")
                                        else:
                                            pulse = str(all_pulses.iloc[idx]['Pulse'])

                                # Check each pulse
                                if inp['DS'] in self.data_sources:
                                    if not AppDataAccess.da.get_data_source(inp['DS']).get_pulses_df(
                                            pattern=pulse).empty:
                                        elements[idx].append(pulse)
                                        fls[column_name] = 0
                                    else:
                                        fls[column_name] = 1
                                        logger.warning(
                                            f"The pulse '{pulse}' could not be found in the data source '{inp['DS']}' "
                                            f"in the table row [{table_row}]")
                                        break
                                else:
                                    fls[column_name] = 1
                                    logger.warning(
                                        f"The pulse '{pulse}' could not be found in the data source '{inp['DS']}' "
                                        f"in the table row [{table_row}]")
                                    break

                            if len(elements[2]) == 0:
                                # Remove pulses from global
                                value = [i.strip() for i in default_value if i.strip() and i.strip() not in elements[1]]
                                # Add pulses from global
                                value.extend([i for i in elements[0] if i not in value])
                                # If there are no pulses set default list
                                if len(value) == 0:
                                    if default_value == '':
                                        value = default_value
                                    else:
                                        value = ['']
                            else:
                                # Use resolved pulses (handles 0/-1 special values)
                                value = elements[2]

                        else:
                            value = default_value
                            # Check for empty pulse
                            if not (default_value == '' or default_value == ['']):
                                value = [pulse for pulse in value if pulse.strip()]
                            fls[column_name] = 0

                    else:
                        # Dates case
                        is_date = False
                        value = get_value(inp, column_name, type_func)

                        # None value and not pulses
                        if value is None and not out['PulseId']:
                            # Check if None is caused by empty cell or invalid cell
                            if inp[column_name] == '':
                                fls[column_name] = 0
                            else:
                                fls[column_name] = 1
                                logger.warning(f"Invalid date format: expected an absolute timestamp in nanoseconds in "
                                               f"the table row [{table_row}]")
                            value = default_value

                        # None value but there are pulses
                        elif value is None and out['PulseId']:
                            if inp[column_name] == '':
                                fls[column_name] = 0
                            else:
                                fls[column_name] = 1
                                logger.warning(f"Invalid date format: expected a relative timestamp when using pulses "
                                               f"in the table row [{table_row}]")

                            if default_value == '':
                                if column_name == 'StartTime':
                                    value = 0
                                # In case of EndTime keep None
                            else:
                                is_date |= bool(default_value > (1 << 53))
                                if is_date:
                                    if column_name == 'StartTime':
                                        value = 0
                                    else:
                                        value = None
                                    fls[column_name] = 0
                                else:
                                    value = default_value
                                    fls[column_name] = 0

                        # There is a value but not pulses
                        elif value is not None and not out['PulseId']:
                            is_date |= bool(value > (1 << 53))
                            if is_date:
                                # Keep value
                                fls[column_name] = 0
                            else:
                                value = default_value
                                fls[column_name] = 1
                                logger.warning(f"Invalid date format: expected an absolute timestamp when using a time "
                                               f"range without pulses in the table row [{table_row}]")

                        # There is a value and pulses
                        elif value is not None and out['PulseId']:
                            is_date |= bool(value > (1 << 53))
                            if is_date:
                                if column_name == 'StartTime':
                                    value = 0
                                else:
                                    value = None
                                fls[column_name] = 1
                                logger.warning(f"Invalid date format: expected a relative timestamp when using pulses "
                                               f"in the table row [{table_row}]")
                            else:
                                # keep value
                                fls[column_name] = 0

                        # Check chronology of dates
                        if column_name == 'EndTime' and value:
                            # Check if there are no errors in the cells corresponding to the dates
                            if value <= out['StartTime'] or fls['StartTime'] == 1 or fls[column_name] == 1:
                                fls[column_name] = 1
                                fls['StartTime'] = 1
                                logger.warning(f"Chronology error: EndTime must be later than the StartTime in the "
                                               f"table row [{table_row}]")
                            else:
                                fls[column_name] = 0
                                fls['StartTime'] = 0
                else:
                    if k == 'DataSource':  # Do not read default value when parsing an already filled in table
                        value = get_value(inp, column_name, type_func)
                        if value == '':
                            fls[column_name] = 1
                            logger.warning(f"Invalid datasource: the 'Datasource' field cannot be empty in the table "
                                           f"row [{table_row}]")
                        else:
                            if value in self.data_sources:
                                fls[column_name] = 0
                            else:
                                fls[column_name] = 1
                                logger.warning(f"Invalid datasource: the value '{value}' is not found in the list of "
                                               f"available datasources in the table row [{table_row}]")
                    else:
                        value = get_value(inp, column_name, type_func) or default_value

                        # Checks of the different cases
                        p = Parser()

                        # Variable
                        if column_name == 'Variable':
                            fls[column_name] = 0

                        # Stack
                        elif column_name == 'Stack':
                            if value == '':
                                fls[column_name] = 0
                            elif value in stack:
                                fls[column_name] = 1
                                logger.warning(
                                    f"Invalid stack in table row [{table_row}]: "
                                    f"Plot of type PlotContour, PlotContourWithSlider or PlotXYWithSlider cannot be stacked, just PlotXY.\n"
                                    f"Mixing different plot types in the same stack is not allowed.")
                            else:
                                if exp_stack.match(value):
                                    fls[column_name] = 0
                                else:
                                    fls[column_name] = 1
                                    logger.warning(f"Invalid stack: The stack identifier must be a numeric value in the"
                                                   f" table row [{table_row}]")

                        # Row Span - Col Span
                        elif column_name == 'Row span' or column_name == 'Col span':
                            if value <= 0:
                                fls[column_name] = 1
                                value = 1
                                logger.warning(f"Invalid value for '{column_name}': the value must be greater than 0 in"
                                               f" the table row [{table_row}]")
                            elif value == 1:
                                if inp[column_name] == '1' or inp[column_name] == '':
                                    fls[column_name] = 0
                                else:
                                    fls[column_name] = 1
                                    logger.warning(f"Invalid value for '{column_name}': the value must be numeric in "
                                                   f"the table row [{table_row}]")
                            elif value > 10:
                                fls[column_name] = 1
                                value = 1
                                logger.warning(f"Invalid value for '{column_name}': the value exceeds the maximum limit"
                                               f" of 10 in the table row [{table_row}]")
                            else:
                                # Keep value
                                fls[column_name] = 0

                        # Envelope
                        elif column_name == 'Envelope':
                            if value and inp[column_name] == '1':
                                fls[column_name] = 0
                            else:
                                # In case of no envelope, just 0 or '' is considered valid
                                if inp[column_name] == '0' or inp[column_name] == '':
                                    fls[column_name] = 0
                                else:
                                    fls[column_name] = 1
                                    logger.warning(f"Invalid envelope value: expected '0' or an empty string to disable"
                                                   f" the envelope, or '1' to enable it in the table row [{table_row}]")

                        # Alias
                        elif column_name == 'Alias':
                            if value != '':
                                if value not in self.aliases:
                                    self.aliases.append(value)
                                    fls[column_name] = 0
                                else:
                                    # Repeated alias
                                    fls[column_name] = 1
                                    logger.warning(f"Invalid alias: the alias '{value}' is already present in the list "
                                                   f"of aliases in the table row [{table_row}]")
                            else:
                                # Check if there is variable name
                                if inp['Variable'] == "" and any(inp[exp] != "" for exp in ["x", "y", "z"]):
                                    fls[column_name] = 1
                                    logger.warning(
                                        f"An alias must be specified when no variable is provided in order to"
                                        f" perform the query correctly. Check the table row [{table_row}]")
                                else:
                                    fls[column_name] = 0

                        # X - Y - Z
                        elif column_name == 'x' or column_name == 'y' or column_name == 'z':
                            try:
                                p.set_expression(value)
                                if p.is_valid:
                                    fls[column_name] = 0
                                else:
                                    fls[column_name] = 1
                                    logger.warning(f"Invalid '{column_name}' expression: the provided expression cannot"
                                                   f" be evaluated correctly")
                            except InvalidExpression:
                                fls[column_name] = 1
                                logger.warning(f"Invalid '{column_name}' expression: the provided expression cannot be "
                                               f"evaluated correctly")

                        # Plot Type
                        elif column_name == 'Plot type':
                            if value not in ['PlotXY', 'PlotContour', 'PlotXYWithSlider', 'PlotContourWithSlider']:
                                fls[column_name] = 1
                                logger.warning(f"Invalid plot type: '{value}' is not a valid plot type. Expected"
                                               f" 'PlotXY' or 'PlotContour' or 'PlotXYWithSlider' or 'PlotContourWithSlider'")
                            else:
                                fls[column_name] = 0

                out.update({column_name: value})

            # Check dependencies
            dependencies = ParserHelper.get_dependencies([inp['x'], inp['y'], inp['z']])
            s = self._table['Alias']
            for val in dependencies:
                if val != out['Alias'] and val in s.values:  # Only variables that are defined with an alias
                    index = s[s == val].index[0]
                    # Search if there is an error in the corresponding row of the fails table
                    if any(self._table_fails.loc[index].values > 0):
                        for expr in ['x', 'y', 'z']:
                            marker_in_pos = out[expr].find(Parser.marker_in)
                            marker_out_pos = out[expr].find(Parser.marker_out)
                            var = out[expr][marker_in_pos + len(Parser.marker_in):marker_out_pos]
                            if var == val:
                                fls[expr] = 2  # Secondary error

            for k, v in out.items():
                if isinstance(v, list) and len(v) > 0:
                    for member in v:
                        serie = out.copy()
                        serie.update({k: member})
                        # Checks if there is more than one pulseId to change de uid of the signal
                        if "uid" in out and len(v) > 1:
                            # append pulse nb to uid to make it unique
                            serie['uid'] = str(uuid.uuid5(uuid.UUID(serie['uid']), member))
                        yield pd.Series(serie), fls
                    break
            else:
                yield pd.Series(out), fls

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """
        Sort the model by the given column index and order.
        Empty values always pushed to the bottom, rest by column as string.
        """
        # Determine the column name from the DataFrame
        col_name = self._table.columns[column]
        ascending = (order == Qt.SortOrder.AscendingOrder)

        # Notify views that layout is about to change
        self.layoutAboutToBeChanged.emit()

        # Convert everything to string before flag (homogenize)
        col_as_str = self._table[col_name].astype(str)
        self._table['_empty_flag'] = (col_as_str.isna() | (col_as_str == '')).astype(int)

        self._table.sort_values(
            by=['_empty_flag', col_name],
            ascending=[True, ascending],
            inplace=True,
            ignore_index=True
        )

        self._table.drop(columns=['_empty_flag'], inplace=True)
        self.layoutChanged.emit()

    def export_information(self):
        # Discard if the stack is empty or processing columns are used
        table = self._table[self._table['Stack'] != ""]
        errors = []

        # Active processing check
        if not (table[['x', 'y', 'z']] == "").all(axis=1).all():
            errors.append("Processing in x/y/z columns is not supported for export")

        # Modified dates check
        if not (table[['StartTime', 'EndTime']] == "").all(axis=1).all():
            errors.append("Custom StartTime/EndTime is not supported for export")

        # Filter table
        filter_table = table[
            (table[['x', 'y', 'z']] == "").all(axis=1) &
            (table[['StartTime', 'EndTime']] == "").all(axis=1)
            ]

        # Filter column variable for processing due to for the moment is discarded
        p = Parser()
        mask = []
        for val in filter_table['Variable']:
            p.set_expression(val)
            if p.is_valid:
                errors.append("Processing expression in Variable column is not supported for export")
            mask.append(not p.is_valid)

        result_table = filter_table[mask]

        return result_table, errors
