"""The instruments gatecap ships, one subpackage each.

An instrument owns its own composition end to end: the generator plugin that
emits it, the drivers of the blocks it is built from, their panes, and any CLI
command it adds. It reaches the framework through the same registries a
third-party instrument uses, so nothing here is special to the framework.
"""
