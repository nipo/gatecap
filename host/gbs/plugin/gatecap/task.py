"""Emission of a rack's VHDL."""

from gbs.build.task import Task


class GatecapGenerateTask(Task):
    """Renders one rack assembly onto the resources declared for it.

    The assembly also renders a gbs partition manifest for a stand-alone
    generated directory; here the build already knows the file list and
    the dependencies, so the manifest is not among the declared outputs
    and is not written.
    """

    def __init__(self, dispatcher, source, rack, outputs):
        self.rack = rack
        name = rack.description.name.dotted()
        super().__init__(dispatcher,
                         name=f"gatecap_generate_{name}",
                         inputs=[source],
                         outputs=outputs,
                         description=f"generate rack {name}")

    async def work(self):
        rendered = self.rack.files()
        declared = {output.path.name: output for output in self.outputs}
        names = set(self.rack.file_names())
        assert names == set(declared), (
            f"rack {self.rack.description.name.dotted()} emits {sorted(names)}, "
            f"the build declared {sorted(declared)}")

        for name in self.rack.file_names():
            path = declared[name].path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered[name])
            self.debug(f"wrote {path}")
