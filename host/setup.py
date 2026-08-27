from setuptools import setup, find_namespace_packages

setup(
    name="gatecap_acrobe",
    version="0.1",
    description="acrobe plugin for the gatecap capture core",
    author="Nicolas Pouillon",
    license="BSD",
    packages=find_namespace_packages(include=["acrobe_plugin.*"]),
    # A block driver ships its UI (panel.js, ...) as a package resource next to
    # its Python; the GUI shell is a package resource too. Include them so a
    # non-editable install can still serve each driver's UI.
    package_data={"acrobe_plugin.gatecap.instrument.bus_explorer": ["*.js"],
                  "acrobe_plugin.gatecap.instrument.clock_measurer": ["*.js"],
                  "acrobe_plugin.gatecap.instrument.control_status": ["*.js"],
                  "acrobe_plugin.gatecap.instrument.la": ["*.js"],
                  "acrobe_plugin.gatecap.instrument.la.blocks.control": ["*.js"],
                  "acrobe_plugin.gatecap.instrument.la.blocks.trigger": ["*.js"],
                  "acrobe_plugin.gatecap.gui": ["assets/*.html", "assets/*.png"]},
    install_requires=["aiohttp", "cbor2", "pyvcd"],
)
