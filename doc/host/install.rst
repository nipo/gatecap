Installing the host tools
=========================

Requirements
------------

* Python 3.13
* `acrobe <https://github.com/nipo/acrobe>`_, installed and able to see your
  probes and links
* the gatecap plugin, from this repository's ``host/`` directory

The plugin pulls in ``cbor2`` (to read the core's self-description) and
``pyvcd`` (to write waveforms). The graphical interface additionally needs
``pywebview``.

The same distribution also installs a `gbs <https://github.com/nipo/gbs>`_
plugin, which is what lets a build generate a rack straight from its
description (:doc:`../usage/build`). It is installed unconditionally: without
gbs, nothing imports it.

Installing
----------

From a checkout::

   python3.13 -m pip install -e host/ --config-settings editable_mode=compat

.. important::

   The ``editable_mode=compat`` setting is not cosmetic. acrobe finds its
   plugins by walking the ``acrobe_plugin`` namespace package; a default
   editable install hides the sources behind an import hook that walk cannot
   see, and the plugin silently never loads — connecting then fails with
   ``NoMatch for 'gatecap'``. A non-editable install (``pip install host/``)
   is fine too.

Checking the installation
-------------------------

With a target reachable — a board, or a simulation bench (see
:doc:`../developer/simulation`) — ask it to describe itself::

   acrobe gatecap -r udp/127.0.0.1:4242/gatecap info

Output listing a control block, its probes and its trigger means the plugin
loaded, the transport resolved and the core answered.

If the command itself is unknown, the plugin is not visible to acrobe: check
the install mode above. If the command exists but the path does not resolve,
the transport is the problem, not gatecap.

Graphical interface
-------------------

The GUI needs ``pywebview`` for its window (``acrobe gatecap serve``, which
leaves the browser to you, does not), and it embeds the `Surfer
<https://surfer-project.org/>`_ waveform viewer as a WebAssembly build. Those
viewer assets are looked up on disk first; when they are missing, the first
GUI start downloads Surfer's official prebuilt web build and caches it next
to the package. Point ``GATECAP_SURFER_ASSETS`` at a directory holding that
build to use your own copy and skip the download entirely — useful on a
machine with no outbound network access.
