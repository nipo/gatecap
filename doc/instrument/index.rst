The instruments
===============

An ``instruments`` entry is one instance of one instrument type,
selected by its YAML tag. Four types ship with gatecap, and this
chapter is the catalogue: what each is for, every key it takes, and
the ports it puts on the rack's boundary.

.. toctree::
   :maxdepth: 1

   logic_analyzer/index
   control_status
   clock_measurer
   bus_explorer

They mix freely: a rack may hold any number of each, in any
combination, and they share the one link, the one address map and the
one self-description.
