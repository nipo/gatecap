"""Namespace package holding the gbs plugins shipped by third parties.

Extending the path lets `gbs.plugin.gatecap` live in this distribution
while `gbs.plugin.*` of other distributions stay importable.
"""

__path__ = __import__('pkgutil').extend_path(__path__, __name__)
