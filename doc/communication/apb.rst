``APB``
=======

No adapter at all: the backplane's APB completer becomes the rack's port pair,
``apb_i``/``apb_o``. Use it when your design already has a register bus — a
CPU, a bridge of your own — or in a testbench driving the map directly.

Description
-----------

.. code-block:: yaml

   communication:
     mode: apb            # no clock key: clock_i / reset_n_i are added

It is the one mode with no host path of its own: reaching it from a host means
giving that bus a link (:ref:`why <host-transport-apb>`).

Instantiation
-------------

The rack drops its protocol front end adapter and becomes an APB
completer, whose geometry is a generic with a default:

.. code-block:: vhdl

   -- In the generated rack entity.
   generic (
     apb_config_c : nsl_amba.apb.config_t := board_rack_apb_config
     );

The default — a package function of whatever generics the instruments take —
spans the allocated map. Take it as it stands, or state the configuration your
own interconnect dictates: a wider address space is accepted, a narrower one
fails elaboration with *the APB configuration must span the allocated map*, and
a different word width fails likewise.
