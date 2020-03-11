import copy
import enum
import logging
import os.path
import pathlib
import functools

from qtpy import QtWidgets
from qtpy.QtCore import Q_ENUMS, Property, Slot, Qt

import pcdsutils
from pydm import Display
from pydm.utilities.display_loading import load_py_file
from typhos import utils

from . import utils
from . import widgets

logger = logging.getLogger(__name__)


class DisplayTypes(enum.IntEnum):
    """Types of Available Templates"""
    embedded_screen = 0
    detailed_screen = 1
    engineering_screen = 2


_DisplayTypes = utils.pyqt_class_from_enum(DisplayTypes)

DEFAULT_TEMPLATES = {
    type_.name: [(utils.ui_dir / f'{type_.name}.ui').resolve()]
    for type_ in DisplayTypes
}

class TyphosDeviceDisplay(utils.TyphosBase, widgets.TyphosDesignerMixin,
                          _DisplayTypes):
    """
    Main Panel display for a signal Ophyd Device

    This widget lays out all of the architecture for a single Ophyd display.
    The structure matches an ophyd Device, but for this specific instantation,
    one is not required to be given. There are four main panels available;
    :attr:`.read_panel`, :attr:`.config_panel`, :attr:`.method_panel`. These
    each provide a quick way to organize signals and methods by their
    importance to an operator. Because each panel can be hidden interactively,
    the screen works as both an expert and novice entry point for users. By
    default, widgets are hidden until contents are added. For instance, if you
    do not add any methods to the main panel it will not be visible.

    This contains the widgets for all of the root devices signals, any methods
    you would like to display, and an optional image. As with ``typhos``
    convention, the base initialization sets up the widgets and the
    ``.from_device`` class method will automatically populate them.

    Parameters
    ----------
    name: str, optional
        Name to displayed at the top of the panel

    image: str, optional
        Path to image file to displayed at the header

    parent: QWidget, optional
    """
    # Template types and defaults
    Q_ENUMS(_DisplayTypes)
    TemplateEnum = DisplayTypes  # For convenience

    def __init__(self, parent=None, **kwargs):
        # Intialize background variable
        self._forced_template = ''
        self._macros = dict()
        self._main_widget = None
        self._display_type = DisplayTypes.detailed_screen

        self.templates = {type_.name: [] for type_ in DisplayTypes}

        # Set this to None first so we don't render
        super().__init__(parent=parent)

        self.setLayout(QtWidgets.QHBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.load_best_template()

        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        self.contextMenuEvent = self.open_context_menu

    def generate_context_menu(self):
        """
        Generates the custom context menu, and populates it with any external
        tools that have been loaded.  PyDMWidget subclasses should override
        this method (after calling superclass implementation) to add the menu.

        Returns
        -------
        QMenu
        """
        base_menu = QtWidgets.QMenu(parent=self)

        for view, filenames in self.templates.items():
            if view.endswith('_screen'):
                view = view.split('_screen')[0]
            menu = base_menu.addMenu(view.capitalize())

            for filename in filenames:
                def switch_template(*, filename=filename):
                    self.force_template = filename

                action = menu.addAction(os.path.split(filename)[-1])
                action.triggered.connect(switch_template)

        return base_menu

    def open_context_menu(self, ev):
        """
        Handler for when the Default Context Menu is requested.

        Parameters
        ----------
        ev : QEvent
        """
        menu = self.generate_context_menu()
        menu.exec_(self.mapToGlobal(ev.pos()))

    @property
    def current_template(self):
        """Current template being rendered"""
        return self._current_template

    @Property(_DisplayTypes)
    def display_type(self):
        return self._display_type

    @display_type.setter
    def display_type(self, value):
        # Store our new value
        if self._display_type != value:
            self._display_type = value
            self.load_best_template(macros=self._macros)

    @Property(str, designable=False)
    def device_class(self):
        """Full class with module name of loaded device"""
        device = self.device
        cls = self.device.__class__
        return f'{cls.__module__}.{cls.__name__}' if device else ''

    @Property(str, designable=False)
    def device_name(self):
        "Name of loaded device"
        device = self.device
        return device.name if device else ''

    @property
    def device(self):
        '''The device associated with this Device Display'''
        try:
            device, = self.devices
            return device
        except ValueError:
            ...

    def get_best_template(self, display_type, macros):
        if hasattr(display_type, 'name'):
            display_type = display_type.name

        templates = self.templates[display_type]
        if templates:
            return templates[0]

        logger.warning("No templates available for display type: %s",
                       self._display_type)

    def load_best_template(self, macros=None):
        """
        Load a new template

        Parameters
        ----------
        template: str
            Absolute path to template location

        macros: dict, optional
            Macro substitutions to be made in the file
        """
        if self.layout() is None:
            # If we are not fully initialized yet do not try and add anything
            # to the layout. This will happen if the QApplication has a
            # stylesheet that forces a template prior to the creation of this
            # display
            return

        # Clear anything that exists in the current layout
        if self._main_widget:
            logger.debug("Clearing existing layout ...")
            utils.clear_layout(self.layout())

        self._macros = macros or self._macros

        template = (self._forced_template or
                    self.get_best_template(self._display_type, self._macros))

        if not template:
            self._main_widget = QtWidgets.QWidget()
            self._current_template = None
            return

        try:
            self._load_template(template)
        except Exception:
            logger.exception("Unable to load file %r", self.current_template)
            self._main_widget = QtWidgets.QWidget()
            self._current_template = None
        else:
            self._current_template = template
        finally:
            self.layout().addWidget(self._main_widget)
            utils.reload_widget_stylesheet(self)

    def _get_templates_from_macros(self, macros=None):
        macros = macros or self._macros
        ret = {}
        for display_type in DisplayTypes:
            ret[display_type.name] = None
            try:
                value = self._macros[display_type]
            except KeyError:
                ...
            else:
                value = pathlib.Path(value).resolve()
                if self._templates_from_macros[display_type] != value:
                    if value.exists() and value.is_file():
                        ret[display_type.name] = value

        return ret

    def _load_template(self, filename):
        logger.debug("Loading %s", filename)
        ext = os.path.splitext(filename)[1]
        # Support Python files
        if ext == '.py':
            logger.debug('Load Python template: %r', filename)
            self._main_widget = load_py_file(filename, macros=self._macros)
        else:
            # Otherwise assume you have given use a UI file
            logger.debug('Load UI template: %r', filename)
            self._main_widget = Display(ui_filename=filename,
                                        macros=self._macros)
        # Add device to all children widgets
        if not self.devices:
            return

        main = self._main_widget
        designer = main.findChildren(widgets.TyphosDesignerMixin) or []
        bases = main.findChildren(utils.TyphosBase) or []

        device, = self.devices
        for widget in set(bases + designer):
            widget.add_device(device)

            if hasattr(widget, 'set_device_display'):
                widget.set_device_display(self)

    @Property(str)
    def force_template(self):
        """Force a specific template"""
        return self._forced_template

    @force_template.setter
    def force_template(self, value):
        if value != self._forced_template:
            self._forced_template = value
            self.load_best_template(macros=self._macros)

    def add_device(self, device, macros=None):
        """
        Add a Device and signals to the TyphosDeviceDisplay

        Parameters
        ----------
        device: ophyd.Device

        macros: dict, optional
            Set of macros to reload the template with. There are two fallback
            options attempted if no information is passed in. First, if the
            device has an ``md`` attribute after being loaded from a ``happi``
            database, that information will be passed in as macros. Finally, if
            no ``name`` field is passed in, we ensure the ``device.name`` and
            ``device.prefix`` are entered as well.
        """
        # We only allow one device at a time
        if self.devices:
            logger.debug("Removing devices %r", self.devices)
            self.devices.clear()
        # Add the device to the cache
        super().add_device(device)
        # Try and collect macros from device
        if not macros:
            if hasattr(device, 'md'):
                macros = device.md.post()
            else:
                macros = dict()
        # Ensure we at least pass in the device name and prefix
        if 'name' not in macros:
            macros['name'] = device.name
        if 'prefix' not in macros and hasattr(device, 'prefix'):
            macros['prefix'] = device.prefix

        self.search_for_templates()
        self.load_best_template(macros=macros)

    def search_for_templates(self):
        '''
        Search the filesystem for device-specific templates
        '''
        device = self.device
        if not device:
            return

        cls = device.__class__

        # TODO other paths
        directories = [utils.ui_dir]

        logger.debug('Searching for templates for %s', cls.__name__)

        macro_templates = self._get_templates_from_macros(self._macros)

        for display_type in DisplayTypes:
            # TODO display_type names make me sad
            view = display_type.name
            if view.endswith('_screen'):
                view = view.split('_screen')[0]

            template_list = self.templates[display_type.name]
            template_list.clear()

            # 1. Highest priority: macros
            macro_template = macro_templates[display_type.name]
            if macro_template and macro_template not in template_list:
                template_list.append(macro_template)
                logger.debug('Adding macro template %s: %s (total=%d)',
                             display_type, macro_template, len(template_list))

            # 2. Templates based on class hierarchy names
            filenames = utils.find_templates_for_class(cls, view, directories)
            for filename in filenames:
                if filename not in template_list:
                    template_list.append(filename)
                    logger.debug('Found new template %s: %s (total=%d)',
                                 display_type, filename, len(template_list))

            # 3. Default templates
            template_list.extend(
                [templ for templ in DEFAULT_TEMPLATES[display_type.name]
                 if templ not in template_list]
            )


    @classmethod
    def from_device(cls, device, template=None, macros=None):
        """
        Create a new TyphosDeviceDisplay from a Device

        Loads the signals in to the appropriate positions and sets the title to
        a cleaned version of the device name

        Parameters
        ----------
        device: ophyd.Device

        template :str, optional
            Set the ``display_template``
        macros: dict, optional
            Macro substitutions to be placed in template
        """
        display = cls()
        # Reset the template if provided
        if template:
            display.force_template = template
        # Add the device
        display.add_device(device, macros=macros)
        return display

    @classmethod
    def from_class(cls, klass, *, template=None, macros=None, **kwargs):
        """
        Create a new TyphosDeviceDisplay from a Device class

        Loads the signals in to the appropriate positions and sets the title to
        a cleaned version of the device name

        Parameters
        ----------
        klass : str or class
        template :str, optional
            Set the ``display_template``
        macros: dict, optional
            Macro substitutions to be placed in template
        kwargs : dict
            Extra arguments are used at device instantiation

        Returns
        -------
        TyphosDeviceDisplay
        """
        try:
            obj = pcdsutils.utils.get_instance_by_name(klass, **kwargs)
        except Exception:
            logger.exception('Failed to generate TyphosDeviceDisplay from '
                             'device %s', obj)
            return None

        return cls.from_device(obj, template=template, macros=macros)

    @Slot(object)
    def _tx(self, value):
        """Receive information from happi channel"""
        self.add_device(value['obj'], macros=value['md'])
