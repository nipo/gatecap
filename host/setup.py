from setuptools import setup, find_namespace_packages

setup(
    name="gatecap_acrobe",
    version="0.1",
    description="acrobe plugin for the gatecap capture core",
    author="Nicolas Pouillon",
    license="BSD",
    # Two plugin namespaces: the instrumentation stack as an acrobe plugin,
    # and the rack generator as a gbs plugin so a gbs project can list a
    # description as a source. The gbs namespace is installed
    # unconditionally: without gbs there is simply nothing importing it.
    packages=(find_namespace_packages(include=["acrobe_plugin.*"])
              + find_namespace_packages(include=["gbs.*"])),
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
