import logging

from ophyd import Device

from typhos.tools import TyphosLogDisplay


def test_log_display(qtbot):
    dev = Device(name='test')
    log_tool = TyphosLogDisplay.from_device(dev)
    qtbot.addWidget(log_tool)
    dev.log.error(dev.name)
    assert log_tool.logdisplay.handler in dev.log.handlers
    dev2 = Device(name='blah')
    log_tool.add_device(dev2)
    assert log_tool.logdisplay.handler in dev2.log.handlers
