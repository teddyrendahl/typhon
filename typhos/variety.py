"""
Typhos handling of "variety" metadata and related utilities.
"""

import inspect
import logging

from pydm.widgets.display_format import DisplayFormat

logger = logging.getLogger(__name__)
_variety_to_widget_class = {}


def _warn_unhandled(instance, metadata_key, value):
    if value is None:
        return

    logger.warning(
        '%s: Not yet implemented variety handling: key=%s value=%s',
        instance.__class__.__name__, metadata_key, value
    )


def _warn_unhandled_kwargs(instance, kwargs):
    for key, value in kwargs.items():
        _warn_unhandled(instance, key, value)


def key_handler(key):
    """
    A method wrapper to mark a specific variety metadata key with the method.

    Parameters
    ----------
    key : str
        The variety key (e.g., 'delta')
    """

    def wrapper(method):
        assert callable(method)
        if not hasattr(method, '_variety_handler'):
            method._variety_handler_keys = set()
        method._variety_handler_keys.add(key)
        return method

    return wrapper


def _get_variety_handlers(members):
    handlers = {}
    for attr, method in members:
        for key in getattr(method, '_variety_handler_keys', []):
            if key not in handlers:
                handlers[key] = [method]
            handlers[key].append(method)

    return handlers


def uses_key_handlers(cls):
    """
    Class wrapper to finish variety handler configuration.

    Parameters
    ----------
    cls : class
        The class to wrap.
    """
    cls._variety_handlers = _get_variety_handlers(inspect.getmembers(cls))
    return cls


def use_for_variety(variety, *, read=True, write=True):
    """
    A class wrapper to associate a specific variety with the class.

    Defaults to registering for both read and write widgets.

    Parameters
    ----------
    variety : str
        The variety (e.g., 'command')

    read : bool, optional
        Use for readback widgets

    write : bool, optional
        Use for setpoint widgets
    """

    known_varieties = {
        'array-histogram',
        'array-image',
        'array-nd',
        'array-tabular',
        'array-timeseries',
        'bitmask',
        'command',
        'command-enum',
        'command-proc',
        'command-setpoint-tracks-readback',
        'enum',
        'scalar',
        'scalar-range',
        'scalar-tweakable',
        'text',
        'text-enum',
        'text-multiline',
    }

    if variety not in known_varieties:
        # NOTE: not kept in sync with pcdsdevices; so this wrapper may need
        # updating.
        raise ValueError(f'Not a known variety: {variety}')

    def wrapper(cls):
        if variety not in _variety_to_widget_class:
            _variety_to_widget_class[variety] = {}

        if read:
            _variety_to_widget_class[variety]['read'] = cls
            if cls.__doc__ is not None:
                cls.__doc__ += f'\n    * Used for variety {variety} (readback)'

        if write:
            _variety_to_widget_class[variety]['write'] = cls
            if cls.__doc__ is not None:
                cls.__doc__ += f'\n    * Used for variety {variety} (setpoint)'

        if not read and not write:
            raise ValueError('`write` or `read` must be set.')

        return cls

    return wrapper


def use_for_variety_read(variety):
    """`for_variety` shorthand for setting the readback widget class."""
    return use_for_variety(variety, read=True, write=False)


def use_for_variety_write(variety):
    """`for_variety` shorthand for setting the setpoint widget class."""
    return use_for_variety(variety, read=False, write=True)


def _get_widget_class_from_variety(desc, variety_md, read_only):
    variety = variety_md['variety']  # a required key
    read_key = 'read' if read_only else 'write'
    try:
        widget_cls = _variety_to_widget_class[variety].get(read_key)
    except KeyError:
        logger.error('Unsupported variety: %s (%s / %s)',
                     variety_md['variety'], desc, variety_md)
    else:
        if widget_cls is None:
            # TODO: remove
            logger.error('TODO no widget?: %s (%s / %s)',
                         variety_md['variety'], desc, variety_md)
        return widget_cls


def get_referenced_signal(widget, name_or_component):
    """
    Get the signal referenced from metadata.

    Parameters
    ----------
    widget : QWidget
        The widget which holds the metadata.

    name_or_component : str or ophyd.Component
        The signal name or ophyd Component.
    """
    ophyd_signal = getattr(widget, 'ophyd_signal', None)
    if ophyd_signal is None:
        logger.error('Incorrectly configured widget (ophyd_signal unset?)')
        return

    device = ophyd_signal.parent
    if device is None:
        logger.debug('Cannot be used on isolated (non-Device) signal')
        return

    if hasattr(name_or_component, 'attr'):
        name_or_component = name_or_component.attr

    return getattr(device, name_or_component)


def create_variety_property():
    """
    Create a property for widgets that helps in setting variety metadata.

    On setting variety metadata::

        1. self._variety_metadata is updated
        2. self._update_variety_metadata(**md) is called
        3. All registered variety key handlers are called.
    """

    def fget(self):
        return dict(self._variety_metadata)

    def fset(self, metadata):
        self._variety_metadata = dict(metadata or {})

        # Catch-all handler for variety metadata.
        try:
            if hasattr(self, '_update_variety_metadata'):
                self._update_variety_metadata(**self._variety_metadata)
        except Exception:
            logger.exception('Failed to set variety metadata for class %s: %s',
                             type(self).__name__, self._variety_metadata)

        # Optionally, there may be 'handlers' for individual top-level keys.
        handlers = getattr(self, '_variety_handlers', {})
        for key, handler_list in handlers.items():
            for unbound in handler_list:
                handler = getattr(self, unbound.__name__)

                info = self._variety_metadata.get(key)
                if info is None:
                    continue

                try:
                    if isinstance(info, dict):
                        handler(**info)
                    else:
                        handler(info)
                except Exception:
                    logger.exception(
                        'Failed to set variety metadata for class %s.%s %r: '
                        '%s', type(self).__name__, handler.__name__, key, info
                    )

    return property(fget, fset,
                    doc='Additional component variety metadata.')


def get_enum_strings(enum_strings, enum_dict):
    """Get enum strings from either `enum_strings` or `enum_dict`."""
    if enum_dict:
        max_value = max(enum_dict)
        return [enum_dict.get(idx, '')
                for idx in range(max_value + 1)]

    return enum_strings


def get_display_format(value):
    """Get the display format enum value from the variety metadata value."""
    if isinstance(value, DisplayFormat):
        return value
    if isinstance(value, str):
        return getattr(DisplayFormat, value.capitalize(),
                       DisplayFormat.Default)
