# Description: Delegates editing for data source column
# Author: Jaswant Sai Panchumarti

import typing

from PySide6.QtCore import QAbstractItemModel, QCoreApplication, QLocale, QModelIndex, QObject, QSize, Qt
from PySide6.QtWidgets import QComboBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget


class MTDataSourcesDelegate(QStyledItemDelegate):
    def __init__(self, data_sources: list, parent: typing.Optional[QObject] = None):
        super().__init__(parent=parent)
        self._data_sources = data_sources

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:
        combobox = QComboBox(parent)
        combobox.addItems(self._data_sources)
        if combobox.count():
            combobox.setCurrentIndex(0)
        return combobox

    def setEditorData(self, editor: QComboBox, index: QModelIndex):
        value = index.data(Qt.ItemDataRole.EditRole)
        loc = editor.findText(value)
        if loc >= 0:
            editor.setCurrentIndex(loc)
        else:
            editor.setCurrentIndex(0)

    def setModelData(self, editor: QComboBox, model: QAbstractItemModel, index: QModelIndex):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QComboBox, option: QStyleOptionViewItem, index: QModelIndex):
        editor.setGeometry(option.rect)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        max_width = 0
        for value in self._data_sources:
            item_width = option.fontMetrics.horizontalAdvance(self.displayText(value, QLocale()))
            max_width = max(item_width, max_width)

        return QCoreApplication.instance().style().sizeFromContents(
            QStyle.ContentsType.CT_ComboBox,
            option,
            QSize(max_width * 1.5, option.fontMetrics.height())
        )


class MTCalibratedDelegate(QStyledItemDelegate):
    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:
        combobox = QComboBox(parent)
        combobox.addItems(["false", "true"])
        return combobox

    def setEditorData(self, editor: QComboBox, index: QModelIndex):
        value = str(index.data(Qt.ItemDataRole.EditRole)).lower()
        loc = editor.findText(value)
        editor.setCurrentIndex(loc if loc >= 0 else 0)

    def setModelData(self, editor: QComboBox, model: QAbstractItemModel, index: QModelIndex):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QComboBox, option: QStyleOptionViewItem, index: QModelIndex):
        editor.setGeometry(option.rect)


class MTPlotTypeDelegate(QStyledItemDelegate):
    def __init__(self, plot_types: list, parent: typing.Optional[QObject] = None):
        super().__init__(parent=parent)
        self._plot_types = plot_types

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:
        combobox = QComboBox(parent)
        combobox.addItems(self._plot_types)
        combobox.addItems('')
        if combobox.count():
            combobox.setCurrentIndex(0)
        return combobox

    def setEditorData(self, editor: QWidget, index: QModelIndex):
        value = index.data(Qt.ItemDataRole.EditRole)
        loc = editor.findText(value)
        if loc >= 0:
            editor.setCurrentIndex(loc)
        else:
            editor.setCurrentIndex(0)

    def setModelData(self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex):
        editor.setGeometry(option.rect)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        max_width = 0
        for value in self._plot_types:
            item_width = option.fontMetrics.horizontalAdvance(self.displayText(value, QLocale()))
            max_width = max(item_width, max_width)

        return QCoreApplication.instance().style().sizeFromContents(
            QStyle.ContentsType.CT_ComboBox,
            option,
            QSize(max_width * 1.5, option.fontMetrics.height())
        )
