project = "gatecap"
copyright = "Nicolas Pouillon"
author = "Nicolas Pouillon"

extensions = []

exclude_patterns = [
    "_build",
]

# furo is the intended theme; alabaster (bundled with sphinx) keeps the tree
# buildable without it.
try:
    import furo  # noqa: F401
    html_theme = "furo"
except ImportError:
    html_theme = "alabaster"

html_title = "gatecap"
html_static_path = []
html_logo = "images/gatecap-icon.png"
html_favicon = "images/gatecap-icon.png"

# Unmarked literal blocks are command lines, tool output or name specs; VHDL
# blocks say so explicitly.
highlight_language = "text"
