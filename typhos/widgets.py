"""
Typhos widgets and related utilities.
"""

import collections
import datetime
import inspect
import logging

import numpy as np
import pydm
import pydm.widgets
import pydm.widgets.base
import pydm.widgets.byte
import pydm.widgets.enum_button
import qtawesome as qta
from ophyd.signal import EpicsSignalBase
from pydm.widgets.display_format import DisplayFormat
from pyqtgraph.parametertree import parameterTypes as ptypes
from qtpy import QtGui, QtWidgets
from qtpy.QtCore import Property, QObject, QSize, Qt, Signal, Slot
from qtpy.QtWidgets import (QAction, QDialog, QDockWidget, QPushButton,
                            QToolBar, QVBoxLayout, QWidget)

from . import plugins, utils, variety
from .textedit import TyphosTextEdit  # noqa: F401
from .tweakable import TyphosTweakable  # noqa: F401
from .variety import use_for_variety_read, use_for_variety_write

logger = logging.getLogger(__name__)

EXPONENTIAL_UNITS = ['mtorr', 'torr', 'kpa', 'pa']


class SignalWidgetInfo(
        collections.namedtuple(
            'SignalWidgetInfo',
            'read_cls read_kwargs write_cls write_kwargs'
        )):
    """
    Provides information on how to create signal widgets: class and kwargs.

    Parameters
    ----------
    read_cls : type
        The readback widget class.

    read_kwargs : dict
        The readback widget initialization keyword arguments.

    write_cls : type
        The setpoint widget class.

    write_kwargs : dict
        The setpoint widget initialization keyword arguments.
    """

    @classmethod
    def from_signal(cls, obj, desc=None):
        """
        Create a `SignalWidgetInfo` given an object and its description.

        Parameters
        ----------
        obj : :class:`ophyd.OphydObj`
            The object

        desc : dict, optional
            The object description, if available.
        """
        if desc is None:
            desc = obj.describe()

        read_cls, read_kwargs = widget_type_from_description(
            obj, desc, read_only=True)

        is_read_only = utils.is_signal_ro(obj) or (
            read_cls is not None and issubclass(read_cls, SignalDialogButton))

        if is_read_only:
            write_cls = None
            write_kwargs = {}
        else:
            write_cls, write_kwargs = widget_type_from_description(obj, desc)

        return cls(read_cls, read_kwargs, write_cls, write_kwargs)


class TogglePanel(QWidget):
    """
    Generic Panel Widget

    Displays a widget below QPushButton that hides and shows the contents. It
    is up to subclasses to re-point the attribute :attr:`.contents` to the
    widget whose visibility you would like to toggle.

    By default, it is assumed that the Panel is initialized with the
    :attr:`.contents` widget as visible, however the contents will be hidden
    and the button synced to the proper position if :meth:`.show_contents` is
    called after instance creation

    Parameters
    ----------
    title : str
        Title of Panel. This will be the text on the QPushButton

    parent : QWidget

    Attributes
    ----------
    contents : QWidget
        Widget whose visibility is controlled via the QPushButton
    """
    def __init__(self, title, parent=None):
        super().__init__(parent=parent)
        # Create Widget Infrastructure
        self.title = title
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(2, 2, 2, 2)
        self.layout().setSpacing(5)
        # Create button control
        # Assuming widget is visible, set the button as checked
        self.contents = None
        self.hide_button = QPushButton(self.title)
        self.hide_button.setCheckable(True)
        self.hide_button.setChecked(True)
        self.layout().addWidget(self.hide_button)
        self.hide_button.clicked.connect(self.show_contents)

    @Slot(bool)
    def show_contents(self, show):
        """
        Show the contents of the Widget

        Hides the :attr:`.contents` QWidget and sets the :attr:`.hide_button`
        to the proper status to indicate whether the widget is hidden or not

        Parameters
        ----------
        show : bool
        """
        # Configure our button in case this slot was called elsewhere
        self.hide_button.setChecked(show)
        # Show or hide the widget if the contents exist
        if self.contents:
            if show:
                self.show()
                self.contents.show()
            else:
                self.contents.hide()


@use_for_variety_write('enum')
@use_for_variety_write('text-enum')
class TyphosComboBox(pydm.widgets.PyDMEnumComboBox):
    """
    Notes
    -----
    """
    def wheelEvent(self, event: QtGui.QWheelEvent):
        event.ignore()


@use_for_variety_write('scalar')
@use_for_variety_write('text')
class TyphosLineEdit(pydm.widgets.PyDMLineEdit):
    """
    Reimplementation of PyDMLineEdit to set some custom defaults

    Notes
    -----
    """
    def __init__(self, *args, display_format=None, **kwargs):
        self._channel = None
        self._setpoint_history_count = 5
        self._setpoint_history = collections.deque(
            [],  self._setpoint_history_count)

        super().__init__(*args, **kwargs)
        self.showUnits = True
        if display_format is not None:
            self.displayFormat = variety.get_display_format(display_format)

    @property
    def setpoint_history(self):
        """
        History of setpoints, as a dictionary of {setpoint: timestamp}
        """
        return dict(self._setpoint_history)

    @Property(int, designable=True)
    def setpointHistoryCount(self):
        """
        Number of items to show in the context menu "setpoint history"
        """
        return self._setpoint_history_count

    @setpointHistoryCount.setter
    def setpointHistoryCount(self, value):
        self._setpoint_history_count = max((0, int(value)))
        self._setpoint_history = collections.deque(
            self._setpoint_history, self._setpoint_history_count)

    def _remove_history_item_by_value(self, remove_value):
        """
        Remove an item from the history buffer by value
        """
        new_history = [(value, ts) for value, ts in self._setpoint_history
                       if value != remove_value]
        self._setpoint_history = collections.deque(
            new_history, self._setpoint_history_count)

    def _add_history_item(self, value, *, timestamp=None):
        """
        Add an item to the history buffer
        """
        if value in dict(self._setpoint_history):
            # Push this value to the end of the list as most-recently used
            self._remove_history_item_by_value(value)

        self._setpoint_history.append(
            (value, timestamp or datetime.datetime.now())
        )

    def send_value(self):
        """
        Update channel value while recording setpoint history
        """
        value = self.text().strip()
        retval = super().send_value()
        self._add_history_item(value)
        return retval

    def _create_history_menu(self):
        if not self._setpoint_history:
            return None

        history_menu = QtWidgets.QMenu("&History")
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        history_menu.setFont(font)

        max_len = max(len(value)
                      for value, timestamp in self._setpoint_history)

        # Pad values such that timestamp lines up:
        # (Value)     @ (Timestamp)
        action_format = '{value:<%d} @ {timestamp}' % (max_len + 1)

        for value, timestamp in reversed(self._setpoint_history):
            timestamp = timestamp.strftime('%m/%d %H:%M')
            action = history_menu.addAction(
                action_format.format(value=value, timestamp=timestamp))

            def history_selected(*, value=value):
                self.setText(str(value))

            action.triggered.connect(history_selected)

        return history_menu

    def widget_ctx_menu(self):
        menu = super().widget_ctx_menu()
        if self._setpoint_history_count > 0:
            self._history_menu = self._create_history_menu()
            if self._history_menu is not None:
                menu.addSeparator()
                menu.addMenu(self._history_menu)

        return menu

    def unit_changed(self, new_unit):
        """
        Callback invoked when the Channel has new unit value.
        This callback also triggers an update_format_string call so the
        new unit value is considered if ```showUnits``` is set.

        Parameters
        ----------
        new_unit : str
            The new unit
        """
        if self._unit == new_unit:
            return

        super().unit_changed(new_unit)
        default = (self.displayFormat == DisplayFormat.Default)
        if new_unit.lower() in EXPONENTIAL_UNITS and default:
            self.displayFormat = DisplayFormat.Exponential


@use_for_variety_read('array-nd')
@use_for_variety_read('command-enum')
@use_for_variety_read('command-setpoint-tracks-readback')
@use_for_variety_read('enum')
@use_for_variety_read('scalar')
@use_for_variety_read('scalar-range')
@use_for_variety_read('scalar-tweakable')
@use_for_variety_read('text')
@use_for_variety_read('text-enum')
@use_for_variety_read('text-multiline')
@use_for_variety_write('array-nd')
class TyphosLabel(pydm.widgets.PyDMLabel):
    """
    Reimplementation of PyDMLabel to set some custom defaults

    Notes
    -----
    """
    def __init__(self, *args, display_format=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Maximum)
        self.showUnits = True
        if display_format is not None:
            self.displayFormat = variety.get_display_format(display_format)

    def unit_changed(self, new_unit):
        """
        Callback invoked when the Channel has new unit value.
        This callback also triggers an update_format_string call so the
        new unit value is considered if ```showUnits``` is set.

        Parameters
        ----------
        new_unit : str
            The new unit
        """
        if self._unit == new_unit:
            return

        super().unit_changed(new_unit)
        default = (self.displayFormat == DisplayFormat.Default)
        if new_unit.lower() in EXPONENTIAL_UNITS and default:
            self.displayFormat = DisplayFormat.Exponential


class TyphosSidebarItem(ptypes.ParameterItem):
    """
    Class to display a Device or Tool in the sidebar

    Notes
    -----
    """
    def __init__(self, param, depth):
        super().__init__(param, depth)
        # Configure a QToolbar
        self.toolbar = QToolBar()
        self.toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.toolbar.setIconSize(QSize(15, 15))
        # Setup the action to open the widget
        self.open_action = QAction(
            qta.icon('fa.square', color='green'), 'Open', self.toolbar)
        self.open_action.triggered.connect(self.open_requested)
        # Setup the action to embed the widget
        self.embed_action = QAction(
            qta.icon('fa.th-large', color='yellow'), 'Embed', self.toolbar)
        self.embed_action.triggered.connect(self.embed_requested)
        # Setup the action to hide the widget
        self.hide_action = QAction(
            qta.icon('fa.times-circle', color='red'), 'Close', self.toolbar)
        self.hide_action.triggered.connect(self.hide_requested)
        self.hide_action.setEnabled(False)
        # Add actions to toolbars
        self.toolbar.addAction(self.open_action)
        self.toolbar.addAction(self.hide_action)
        if self.param.embeddable:
            self.toolbar.insertAction(self.hide_action, self.embed_action)

    def open_requested(self, triggered):
        """Request to open display for sidebar item"""
        self.param.sigOpen.emit(self)
        self._mark_shown()

    def embed_requested(self, triggered):
        """Request to open embedded display for sidebar item"""
        self.param.sigEmbed.emit(self)
        self._mark_shown()

    def hide_requested(self, triggered):
        """Request to hide display for sidebar item"""
        self.param.sigHide.emit(self)
        self._mark_hidden()

    def _mark_shown(self):
        self.open_action.setEnabled(False)
        self.embed_action.setEnabled(False)
        self.hide_action.setEnabled(True)

    def _mark_hidden(self):
        self.open_action.setEnabled(True)
        self.embed_action.setEnabled(True)
        self.hide_action.setEnabled(False)

    def treeWidgetChanged(self):
        """Update the widget when add to a QTreeWidget"""
        super().treeWidgetChanged()
        tree = self.treeWidget()
        if tree is None:
            return
        tree.setItemWidget(self, 1, self.toolbar)


class SubDisplay(QDockWidget):
    """QDockWidget modified to emit a signal when closed"""
    closing = Signal()

    def closeEvent(self, evt):
        self.closing.emit()
        super().closeEvent(evt)


class HappiChannel(pydm.widgets.channel.PyDMChannel, QObject):
    """
    PyDMChannel to transport Device Information

    Parameters
    ----------
    tx_slot: callable
        Slot on widget to accept a dictionary of both the device and metadata
        information
    """

    def __init__(self, *, tx_slot, **kwargs):
        super().__init__(**kwargs)
        QObject.__init__(self)
        self._tx_slot = tx_slot
        self._last_md = None

    @Slot(dict)
    def tx_slot(self, value):
        """Transmission Slot"""
        # Do not fire twice for the same device
        if not self._last_md or self._last_md != value['md']:
            self._last_md = value['md']
            self._tx_slot(value)
        else:
            logger.debug("HappiChannel %r received same device. "
                         "Ignoring for now ...", self)


class TyphosDesignerMixin(pydm.widgets.base.PyDMWidget):
    """
    A mixin class used to display Typhos widgets in the Qt designer.
    """

    # Unused properties that we don't want visible in designer
    alarmSensitiveBorder = Property(bool, designable=False)
    alarmSensitiveContent = Property(bool, designable=False)
    precisionFromPV = Property(bool, designable=False)
    precision = Property(int, designable=False)
    showUnits = Property(bool, designable=False)

    @Property(str)
    def channel(self):
        """The channel address to use for this widget"""
        if self._channel:
            return str(self._channel)
        return None

    @channel.setter
    def channel(self, value):
        if self._channel != value:
            # Remove old connection
            if self._channels:
                self._channels.clear()
                for channel in self._channels:
                    if hasattr(channel, 'disconnect'):
                        channel.disconnect()
            # Load new channel
            self._channel = str(value)
            channel = HappiChannel(address=self._channel,
                                   tx_slot=self._tx)
            self._channels = [channel]
            # Connect the channel to the HappiPlugin
            if hasattr(channel, 'connect'):
                channel.connect()

    @Slot(object)
    def _tx(self, value):
        """Receive information from happi channel"""
        self.add_device(value['obj'])


class SignalDialogButton(QPushButton):
    """QPushButton to launch a QDialog with a PyDMWidget"""
    text = NotImplemented
    icon = NotImplemented
    parent_widget_class = QtWidgets.QWidget

    def __init__(self, init_channel, text=None, icon=None, parent=None):
        self.text = text or self.text
        self.icon = icon or self.icon
        super().__init__(qta.icon(self.icon), self.text, parent=parent)
        self.clicked.connect(self.show_dialog)
        self.dialog = None
        self.channel = init_channel
        self.setIconSize(QSize(15, 15))

    def widget(self, channel):
        """Return a widget created with channel"""
        raise NotImplementedError

    def show_dialog(self):
        """Show the channel in a QDialog"""
        # Dialog Creation
        if not self.dialog:
            logger.debug("Creating QDialog for %r", self.channel)
            # Set up the QDialog
            parent = utils.find_parent_with_class(
                self, self.parent_widget_class)
            self.dialog = QDialog(parent)
            self.dialog.setWindowTitle(self.channel)
            self.dialog.setLayout(QVBoxLayout())
            self.dialog.layout().setContentsMargins(2, 2, 2, 2)
            # Add the widget
            widget = self.widget()
            self.dialog.layout().addWidget(widget)
        # Handle a lost dialog
        else:
            logger.debug("Redisplaying QDialog for %r", self.channel)
            self.dialog.close()
        # Show the dialog
        logger.debug("Showing QDialog for %r", self.channel)
        self.dialog.show()


@use_for_variety_read('array-image')
class ImageDialogButton(SignalDialogButton):
    """
    QPushButton to show a 2-d array.

    Notes
    -----
    """
    text = 'Show Image'
    icon = 'fa.camera'
    parent_widget_class = QtWidgets.QMainWindow

    def widget(self):
        """Create PyDMImageView"""
        return pydm.widgets.PyDMImageView(
            parent=self, image_channel=self.channel)


@use_for_variety_read('array-timeseries')
@use_for_variety_read('array-histogram')  # TODO: histogram settings?
class WaveformDialogButton(SignalDialogButton):
    """
    QPushButton to show a 1-d array.

    Notes
    -----
    """
    text = 'Show Waveform'
    icon = 'fa5s.chart-line'
    parent_widget_class = QtWidgets.QMainWindow

    def widget(self):
        """Create PyDMWaveformPlot"""
        return pydm.widgets.PyDMWaveformPlot(
            init_y_channels=[self.channel], parent=self)


# @variety.uses_key_handlers
@use_for_variety_write('command')
@use_for_variety_write('command-proc')
@use_for_variety_write('command-setpoint-tracks-readback')  # TODO
class TyphosCommandButton(pydm.widgets.PyDMPushButton):
    """
    A pushbutton widget which executes a command by sending a specific value.

    See Also
    --------
    :class:`TyphosCommandEnumButton`

    Notes
    -----
    """

    default_label = 'Command'

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self.variety_metadata = variety_metadata
        self._forced_enum_strings = None

    variety_metadata = variety.create_variety_property()

    def enum_strings_changed(self, new_enum_strings):
        return super().enum_strings_changed(
            self._forced_enum_strings or new_enum_strings)

    def _update_variety_metadata(self, *, value, enum_strings=None,
                                 enum_dict=None, tags=None, **kwargs):
        self.pressValue = value
        enum_strings = variety.get_enum_strings(enum_strings, enum_dict)
        if enum_strings is not None:
            self._forced_enum_strings = tuple(enum_strings)
            self.enum_strings_changed(None)  # force an update

        tags = set(tags or {})

        if 'protected' in tags:
            self.passwordProtected = True
            self.password = 'typhos'  # ... yeah (TODO)

        if 'confirm' in tags:
            self.showConfirmDialog = True

        variety._warn_unhandled_kwargs(self, kwargs)

        if not self.text():
            self.setText(self.default_label)


@variety.uses_key_handlers
@use_for_variety_write('command-enum')
class TyphosCommandEnumButton(pydm.widgets.enum_button.PyDMEnumButton):
    """
    A group of buttons which represent several command options.

    These options can come from directly from the control layer or can be
    overridden with variety metadata.

    See Also
    --------
    :class:`TyphosCommandButton`

    Notes
    -----
    """

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self.variety_metadata = variety_metadata
        self._forced_enum_strings = None

    variety_metadata = variety.create_variety_property()

    def enum_strings_changed(self, new_enum_strings):
        return super().enum_strings_changed(
            self._forced_enum_strings or new_enum_strings)

    def _update_variety_metadata(self, *, value, enum_strings=None,
                                 enum_dict=None, tags=None, **kwargs):
        enum_strings = variety.get_enum_strings(enum_strings, enum_dict)
        if enum_strings is not None:
            self._forced_enum_strings = tuple(enum_strings)
            self.enum_strings_changed(None)  # force an update

        variety._warn_unhandled_kwargs(self, kwargs)


@use_for_variety_read('bitmask')
@variety.uses_key_handlers
class TyphosByteIndicator(pydm.widgets.PyDMByteIndicator):
    """
    Displays an integer value as individual, read-only bit indicators.

    Notes
    -----
    """

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self.variety_metadata = variety_metadata

    variety_metadata = variety.create_variety_property()

    def _update_variety_metadata(self, *, bits, orientation, first_bit, style,
                                 meaning=None, tags=None, **kwargs):
        self.numBits = bits
        self.orientation = {
            'horizontal': Qt.Horizontal,
            'vertical': Qt.Vertical,
        }[orientation]
        self.bigEndian = (first_bit == 'most-significant')
        # TODO: labels do not display properly
        # if meaning:
        #     self.labels = meaning[:bits]
        #     self.showLabels = True
        variety._warn_unhandled_kwargs(self, kwargs)

    @variety.key_handler('style')
    def _variety_key_handler_style(self, *, shape, on_color, off_color,
                                   **kwargs):
        """Variety hook for the sub-dictionary "style"."""
        on_color = QtGui.QColor(on_color)
        if on_color is not None:
            self.onColor = on_color

        off_color = QtGui.QColor(off_color)
        if off_color is not None:
            self.offColor = off_color

        self.circles = (shape == 'circle')

        variety._warn_unhandled_kwargs(self, kwargs)


@use_for_variety_read('command')
@use_for_variety_read('command-proc')
class TyphosCommandIndicator(pydm.widgets.PyDMByteIndicator):
    """Displays command status as a read-only bit indicator."""

    def __init__(self, *args, ophyd_signal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self.numBits = 1
        self.showLabels = False
        self.circles = True


class ClickableBitIndicator(pydm.widgets.byte.PyDMBitIndicator):
    """A bit indicator that emits `clicked` when clicked."""
    clicked = Signal()

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self.clicked.emit()


@use_for_variety_write('bitmask')
class TyphosByteSetpoint(TyphosByteIndicator,
                         pydm.widgets.base.PyDMWritableWidget):
    """
    Displays an integer value as individual, toggleable bit indicators.

    Notes
    -----
    """

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        # NOTE: need to have these in the signature explicitly
        super().__init__(*args, variety_metadata=variety_metadata,
                         ophyd_signal=ophyd_signal, **kwargs)

        self._requests_pending = {}

    def _get_setpoint_from_requests(self):
        setpoint = self.value
        for bit, request in self._requests_pending.items():
            mask = 1 << bit
            if request:
                setpoint |= mask
            else:
                setpoint &= ~mask
        return setpoint

    def _bit_clicked(self, bit):
        if bit in self._requests_pending:
            old_value = self._requests_pending[bit]
        else:
            old_value = bool(self.value & (1 << bit))

        self._requests_pending[bit] = not old_value
        self.send_value_signal[int].emit(self._get_setpoint_from_requests())

    def value_changed(self, value):
        """Receive and update the TyphosTextEdit for a new channel value."""
        for bit, request in list(self._requests_pending.items()):
            mask = 1 << bit
            is_set = bool(value & mask)
            if is_set == request:
                self._requests_pending.pop(bit, None)

        super().value_changed(value)

    @Property(int, designable=True)
    def numBits(self):
        """
        Number of bits to interpret.

        Re-implemented from PyDM to support changing of bit indicators.
        """
        return self._num_bits

    @numBits.setter
    def numBits(self, num_bits):
        if num_bits < 1:
            return

        self._num_bits = num_bits
        for indicator in self._indicators:
            indicator.deleteLater()

        self._indicators = [
            ClickableBitIndicator(parent=self, circle=self.circles)
            for bit in range(self._num_bits)
        ]

        for bit, indicator in enumerate(self._indicators):
            def indicator_clicked(*, bit=bit):
                self._bit_clicked(bit)

            indicator.clicked.connect(indicator_clicked)

        new_labels = [f"Bit {bit}"
                      for bit in range(len(self.labels), self._num_bits)]
        self.labels = self.labels + new_labels


@variety.uses_key_handlers
@use_for_variety_write('scalar-range')
class TyphosScalarRange(pydm.widgets.PyDMSlider):
    """
    A slider widget which displays a scalar value with an explicit range.

    Notes
    -----
    """

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self._delta_value = None
        self._delta_signal = None
        self._delta_signal_sub = None
        self.variety_metadata = variety_metadata

    variety_metadata = variety.create_variety_property()

    def __dtor__(self):
        """PyQt5 destructor hook"""
        # Ensure our delta signal subscription is cleared:
        self.delta_signal = None

    @variety.key_handler('range')
    def _variety_key_handler_range(self, value, source, **kwargs):
        """Variety hook for the sub-dictionary "range"."""
        if source == 'value':
            if value is not None:
                low, high = value
                self.userMinimum = low
                self.userMaximum = high
                self.userDefinedLimits = True
        # elif source == 'use_limits':
        else:
            variety._warn_unhandled(self, 'range.source', source)

        variety._warn_unhandled_kwargs(self, kwargs)

    @variety.key_handler('delta')
    def _variety_key_handler_delta(self, source, value=None, signal=None,
                                   **kwargs):
        """Variety hook for the sub-dictionary "delta"."""
        if source == 'value':
            if value is not None:
                self.delta_value = value
        elif source == 'signal':
            if signal is not None:
                self.delta_signal = variety.get_referenced_signal(self, signal)
        else:
            variety._warn_unhandled(self, 'delta.source', source)

        # range_ = kwargs.pop('range')  # unhandled
        variety._warn_unhandled_kwargs(self, kwargs)

    @variety.key_handler('display_format')
    def _variety_key_handler_display_format(self, value):
        """Variety hook for the sub-dictionary "delta"."""
        self.displayFormat = variety.get_display_format(value)

    @property
    def delta_signal(self):
        """Delta signal, used as the source for "delta_value"."""
        return self._delta_signal

    @delta_signal.setter
    def delta_signal(self, signal):
        if self._delta_signal is not None:
            self._delta_signal.unsubscribe(self._delta_signal_sub)

        if signal is None:
            return

        def update_delta(value, **kwargs):
            self.delta_value = value

        self._delta_signal_sub = signal.subscribe(update_delta)
        self._delta_signal = signal

    @Property(float, designable=True)
    def delta_value(self):
        """
        Delta value, an alternative to "num_points" provided by PyDMSlider.

        num_points is calculated using the current min/max and the delta value,
        if set.
        """
        return self._delta_value

    @delta_value.setter
    def delta_value(self, value):
        if value is None:
            self._delta_value = None
            return
        if value <= 0.0:
            return

        self._delta_value = value
        if self.minimum is not None and self.maximum is not None:
            self._mute_internal_slider_changes = True
            try:
                self.num_steps = (self.maximum - self.minimum) / value
            except Exception:
                logger.exception('Failed to set number of steps with '
                                 'min=%s, max=%s, delta=%s', self.minimum,
                                 self.maximum, value)
            finally:
                self._mute_internal_slider_changes = False

    def connection_changed(self, connected):
        ret = super().connection_changed(connected)
        if connected:
            self.delta_value = self._delta_value
        return ret


@variety.uses_key_handlers
@use_for_variety_write('array-tabular')
class TyphosArrayTable(pydm.widgets.PyDMWaveformTable):
    """
    A table widget which reshapes and displays a given waveform value.

    Notes
    -----
    """

    def __init__(self, *args, variety_metadata=None, ophyd_signal=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.ophyd_signal = ophyd_signal
        self.variety_metadata = variety_metadata

    variety_metadata = variety.create_variety_property()

    def value_changed(self, value):
        try:
            len(value)
        except TypeError:
            logger.debug('Non-waveform value? %r', value)
            return

        # shape = self.variety_metadata.get('shape')
        # if shape is not None:
        #     expected_length = np.multiply.reduce(shape)
        #     if len(value) == expected_length:
        #         value = np.array(value).reshape(shape)

        return super().value_changed(value)

    def _calculate_size(self, padding=5):
        width = self.verticalHeader().width() + padding
        for col in range(self.columnCount()):
            width += self.columnWidth(col)

        height = self.horizontalHeader().height() + padding
        for row in range(self.rowCount()):
            height += self.rowHeight(row)

        return QSize(width, height)

    def _update_variety_metadata(self, *, shape=None, tags=None, **kwargs):
        if shape:
            # TODO
            columns = max(shape[0], 1)
            rows = max(np.product(shape[1:]), 1)
            self.setRowCount(rows)
            self.setColumnCount(columns)
            self.rowHeaderLabels = [f'{idx}' for idx in range(rows)]
            self.columnHeaderLabels = [f'{idx}' for idx in range(columns)]

            if rows <= 5 and columns <= 5:
                full_size = self._calculate_size()
                self.setFixedSize(full_size)

        variety._warn_unhandled_kwargs(self, kwargs)


def _get_scalar_widget_class(desc, variety_md, read_only):
    """
    From a given description, return the widget to use.

    Parameters
    ----------
    desc : dict
        The object description.

    variety_md : dict
        The variety metadata.  Currently unused.

    read_only : bool
        Set if used for the readback widget.
    """
    # Check for enum_strs, if so create a QCombobox
    if read_only:
        return TyphosLabel

    if 'enum_strs' in desc:
        # Create a QCombobox if the widget has enum_strs
        return TyphosComboBox

    # Otherwise a LineEdit will suffice
    return TyphosLineEdit


def _get_ndimensional_widget_class(dimensions, desc, variety_md, read_only):
    """
    From a given description and dimensionality, return the widget to use.

    Parameters
    ----------
    dimensions : int
        The number of dimensions (e.g., 0D or scalar, 1D array, ND array)

    desc : dict
        The object description.

    variety_md : dict
        The variety metadata.  Currently unused.

    read_only : bool
        Set if used for the readback widget.
    """
    if dimensions == 0:
        return _get_scalar_widget_class(desc, variety_md, read_only)

    return {
        1: WaveformDialogButton,
        2: ImageDialogButton
    }.get(dimensions, TyphosLabel)


def widget_type_from_description(signal, desc, read_only=False):
    """
    Determine which widget class should be used for the given signal

    Parameters
    ----------
    signal : ophyd.Signal
        Signal object to determine widget class

    desc : dict
        Previously recorded description from the signal

    read_only: bool, optional
        Should the chosen widget class be read-only?

    Returns
    -------
    widget_class : class
        The class to use for the widget
    kwargs : dict
        Keyword arguments for the class
    """
    if isinstance(signal, EpicsSignalBase):
        # Still re-route EpicsSignal through the ca:// plugin
        pv = (signal._read_pv
              if read_only else signal._write_pv)
        init_channel = utils.channel_name(pv.pvname)
    else:
        # Register signal with plugin
        plugins.register_signal(signal)
        init_channel = utils.channel_name(signal.name, protocol='sig')

    variety_metadata = utils.get_variety_metadata(signal)
    kwargs = {
        'init_channel': init_channel,
    }

    if variety_metadata:
        widget_cls = variety._get_widget_class_from_variety(
            desc, variety_metadata, read_only)
    else:
        try:
            dimensions = len(desc.get('shape', []))
        except TypeError:
            dimensions = 0

        widget_cls = _get_ndimensional_widget_class(
            dimensions, desc, variety_metadata, read_only)

    if widget_cls is None:
        return None, None

    if desc.get('dtype') == 'string' and widget_cls in (TyphosLabel,
                                                        TyphosLineEdit):
        kwargs['display_format'] = DisplayFormat.String

    class_signature = inspect.signature(widget_cls)
    if 'variety_metadata' in class_signature.parameters:
        kwargs['variety_metadata'] = variety_metadata

    if 'ophyd_signal' in class_signature.parameters:
        kwargs['ophyd_signal'] = signal

    return widget_cls, kwargs


def determine_widget_type(signal, read_only=False):
    """
    Determine which widget class should be used for the given signal.

    Parameters
    ----------
    signal : ophyd.Signal
        Signal object to determine widget class

    read_only: bool, optional
        Should the chosen widget class be read-only?

    Returns
    -------
    widget_class : class
        The class to use for the widget
    kwargs : dict
        Keyword arguments for the class
    """
    try:
        desc = signal.describe()[signal.name]
    except Exception:
        logger.error("Unable to connect to %r during widget creation",
                     signal.name)
        desc = {}

    return widget_type_from_description(signal, desc, read_only=read_only)


def create_signal_widget(signal, read_only=False, tooltip=None):
    """
    Factory for creating a PyDMWidget from a signal

    Parameters
    ----------
    signal : ophyd.Signal
        Signal object to create widget

    read_only: bool, optional
        Whether this widget should be able to write back to the signal you
        provided

    tooltip : str, optional
        Tooltip to use for the widget

    Returns
    -------
    widget : PyDMWidget
        PyDMLabel, PyDMLineEdit, or PyDMEnumComboBox based on whether we should
        be able to write back to the widget and if the signal has ``enum_strs``
    """
    widget_cls, kwargs = determine_widget_type(signal, read_only=read_only)
    if widget_cls is None:
        return

    logger.debug("Creating %s for %s", widget_cls, signal.name)

    widget = widget_cls(**kwargs)
    widget.setObjectName(f'{signal.name}_{widget_cls.__name__}')
    if tooltip is not None:
        widget.setToolTip(tooltip)

    return widget
