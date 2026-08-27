"""gbs partition manifest for a generated core.

The generated directory is a gbs partition of the user's own library, so it
carries no library name: two keys, the VHDL sources in analysis order and the
partition keys it depends on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GbsManifest:
    """``sources`` (a single VHDL file group) and ``deps``."""

    sources: tuple
    deps: tuple

    FILE_TYPE = "vhdl"

    @classmethod
    def of(cls, sources, deps):
        """Manifest for the given file names; dependencies are deduplicated
        and sorted, so a description change moves a dependency only when the
        set really changes."""
        return cls(sources=tuple(sources), deps=tuple(sorted(set(deps))))

    def render(self):
        assert self.sources, "a partition with no source is not buildable"
        lines = ["sources:",
                 f"  - file_type: {self.FILE_TYPE}",
                 "    files:"]
        lines += [f"      - {name}" for name in self.sources]
        if self.deps:
            lines.append("deps:")
            lines += [f"  - {dep}" for dep in self.deps]
        else:
            lines.append("deps: []")
        return "\n".join(lines) + "\n"

    def write(self, path):
        with open(path, "w") as f:
            f.write(self.render())
