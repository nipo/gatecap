SPHINXBUILD ?= python3.13 -m sphinx
SOURCEDIR = .
BUILDDIR = _build

.PHONY: help html clean Makefile

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)"

clean:
	rm -rf "$(BUILDDIR)"

# Phony targets never match the catch-all pattern rule below, so html
# needs its own rule.
html:
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)"

%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)"
