"""Relay 028: Station > Stations…, every station the window knows, with what can be done to each.

Open, Show in Explorer, Remove from list, and Close station for the one open now. There is no Delete: a station's
recorded data is only ever deleted by its owner, in Explorer (rule 4). A station whose folder has gone is shown as
"missing (folder not found)" and is never re-created; Remove from list forgets it."""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QMessageBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from marsad_desktop import stations as ST
from marsad_desktop import theme as T
from marsad_desktop.setup_screen import _button, _label

NOTE = "To delete a station's files, close it here, then delete its folder in Explorer."


class StationsDialog(QDialog):
    """`open_folder`, `close_current`: what the owner chose, for the window to act on after the dialog closes."""

    def __init__(self, parent=None, current: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Stations")
        self.setObjectName("StationsDialog")
        self.current = current
        self.open_folder = None
        self.close_current = False
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        v.addWidget(_label("The stations this window knows. Removing one from the list touches none of its files.",
                           muted=True))
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("StationsTable")
        self.table.setHorizontalHeaderLabels(["Station", "Folder", "State"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._buttons)
        self.table.itemDoubleClicked.connect(lambda *_: self._open())
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_open = _button("Open", primary=True)
        self.b_show = _button("Show in Explorer")
        self.b_forget = _button("Remove from list")
        self.b_close_station = _button("Close station")
        self.b_done = _button("Close")
        for b, name, fn in ((self.b_open, "OpenStation", self._open), (self.b_show, "ShowInExplorer", self._show),
                            (self.b_forget, "RemoveFromList", self._forget),
                            (self.b_close_station, "CloseStation", self._close_station),
                            (self.b_done, "Done", self.reject)):
            b.setObjectName(name)
            b.clicked.connect(fn)
        for b in (self.b_open, self.b_show, self.b_forget, self.b_close_station):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.b_done)
        v.addLayout(row)
        note = _label(NOTE, muted=True)
        note.setObjectName("DeleteNote")
        v.addWidget(note)
        self.resize(820, 380)
        self.fill()

    def fill(self):
        self.rows = ST.registry().get("stations") or []
        self.table.setRowCount(len(self.rows))
        for i, e in enumerate(self.rows):
            state = self.state(e["folder"])
            for j, text in enumerate((e.get("name") or "", e["folder"], state)):
                self.table.setItem(i, j, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        if self.rows:
            self.table.selectRow(0)
        self._buttons()

    def state(self, folder: str) -> str:
        if self.current and ST._same(folder, self.current):
            return "open now"
        return ST.MISSING if ST.is_missing(folder) else "ok"

    def selected(self) -> dict | None:
        i = self.table.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _buttons(self):
        e = self.selected()
        st = self.state(e["folder"]) if e else None
        self.b_open.setEnabled(st == "ok")
        self.b_show.setEnabled(e is not None)
        self.b_forget.setEnabled(e is not None and st != "open now")
        self.b_forget.setToolTip("Close this station first." if st == "open now" else
                                 "Forget it in the window's list. No file is touched.")
        self.b_close_station.setEnabled(st == "open now")

    def _open(self):
        e = self.selected()
        if e and self.state(e["folder"]) == "ok":
            self.open_folder = e["folder"]
            self.accept()

    def _show(self):
        e = self.selected()
        if e:
            ST.show_in_explorer(e["folder"])

    def _forget(self):
        e = self.selected()
        if not e or self.state(e["folder"]) == "open now":
            return
        if QMessageBox.question(self, "Remove from list?",
                                f"Forget {e.get('name') or e['folder']} in this window's list?\n\nNo file is touched: "
                                "its folder stays where it is, and Open Station… brings it back.") \
                != QMessageBox.StandardButton.Yes:
            return
        ST.forget(e["folder"])
        self.fill()

    def _close_station(self):
        self.close_current = True
        self.accept()
