"""A small VHDL-93 code model and its renderer.

The generator composes declarations and statements from several contributors
into one design file, so emission is composition rather than text
substitution. Every node renders through :class:`Emitter`, which owns
indentation; rendering is deterministic (declaration order is emission order,
nothing is sorted behind the caller's back, no timestamps).

Elaboration-time expressions are first class but untyped: an expression is a
string, built either by hand or through the :class:`Expr` helpers, so a
constant's value may be ``axis_length(ctrl_command_config_c, "vlr")`` without
the model knowing anything about the function.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Identifier:
    """VHDL-93 basic identifier rules."""

    RESERVED = frozenset("""
        abs access after alias all and architecture array assert attribute
        begin block body buffer bus case component configuration constant
        disconnect downto else elsif end entity exit file for function
        generate generic group guarded if impure in inertial inout is label
        library linkage literal loop map mod nand new next nor not null of on
        open or others out package port postponed procedure process pure
        range record register reject rem report return rol ror select severity
        signal shared sla sll sra srl subtype then to transport type unaffected
        units until use variable wait when while with xnor xor
        """.split())

    @classmethod
    def rejection(cls, name):
        """Why ``name`` is not a legal VHDL basic identifier, or None."""
        if not isinstance(name, str) or name == "":
            return "must be a non-empty string"
        if not name[0].isascii() or not name[0].isalpha():
            return "must start with a letter"
        for c in name:
            if not (c.isascii() and (c.isalnum() or c == "_")):
                return f"illegal character {c!r}"
        if name.endswith("_"):
            return "must not end with an underscore"
        if "__" in name:
            return "must not contain two consecutive underscores"
        if name.lower() in cls.RESERVED:
            return "is a VHDL reserved word"
        return None

    @classmethod
    def legal(cls, name):
        return cls.rejection(name) is None


class Expr:
    """Builders for elaboration-time expression strings."""

    # Expressions longer than this are spread over several lines.
    WIDTH = 72

    @staticmethod
    def string(text):
        """A VHDL string literal (doubling embedded quotes)."""
        return '"' + text.replace('"', '""') + '"'

    @staticmethod
    def call(function, *args, **kwargs):
        """``function(a, b, key => value)``, positional arguments first."""
        parts = [str(a) for a in args]
        parts += [f"{k} => {v}" for k, v in kwargs.items()]
        return f"{function}({', '.join(parts)})"

    @classmethod
    def maybe_call(cls, function, *args):
        """A call, wrapped when it is long, or the bare name when there is no
        argument: VHDL has no empty association list."""
        return cls.wrapped_call(function, *args) if args else function

    @staticmethod
    def concat(*parts):
        return " & ".join(str(p) for p in parts if str(p) != "")

    @staticmethod
    def indent(text, prefix="  "):
        """Shift a whole expression, continuation lines included."""
        return "\n".join(prefix + line for line in str(text).split("\n"))

    @classmethod
    def wrapped_call(cls, function, *args, **kwargs):
        """A call on one line while it fits, one argument per line after."""
        parts = [str(a) for a in args]
        parts += [f"{k} => {v}" for k, v in kwargs.items()]
        single = f"{function}({', '.join(parts)})"
        if len(single) <= cls.WIDTH and "\n" not in single:
            return single
        body = ",\n".join(cls.indent(part) for part in parts)
        return f"{function}(\n{body})"

    @classmethod
    def wrapped_join(cls, parts, separator=" & "):
        """Operands joined, filling lines to the expression width."""
        lines, current = [], ""
        for part in parts:
            if not current:
                current = str(part)
                continue
            joined = current + separator + str(part)
            if len(joined.split("\n")[-1]) > cls.WIDTH:
                lines.append(current + separator.rstrip())
                current = str(part)
            else:
                current = joined
        lines.append(current)
        return "\n".join(lines)

    @staticmethod
    def index(name, position):
        return f"{name}({position})"

    @staticmethod
    def slice_down(name, high, low):
        return f"{name}({high} downto {low})"

    @staticmethod
    def qualified(type_name, expression):
        return f"{type_name}'({expression})"

    @classmethod
    def scalar_vector(cls, expression):
        """A one-element std_ulogic_vector holding a scalar."""
        return cls.qualified("std_ulogic_vector", f"0 => {expression}")

    @staticmethod
    def boolean(value):
        return "true" if value else "false"


class Emitter:
    """Line accumulator with an indentation level."""

    # Comments fill up to this column, indentation and marker included.
    WIDTH = 79

    def __init__(self, indent="  "):
        self.indent = indent
        self.level = 0
        self.lines = []

    def line(self, text=""):
        self.lines.append(self.indent * self.level + text if text else "")
        return self

    def comment(self, text):
        """Emit a comment, filling lines. Line breaks in the text are kept,
        so a caller controls paragraphs and lets the width alone."""
        room = self.WIDTH - len(self.indent) * self.level - len("-- ")
        for paragraph in str(text).split("\n"):
            for part in self.__filled(paragraph, room):
                self.line(f"-- {part}" if part else "--")
        return self

    @staticmethod
    def __filled(text, width):
        lines, current = [], ""
        for word in text.split():
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
        return lines + [current]

    def raw(self, text):
        """Emit pre-formatted text, re-indenting each non-empty line."""
        for part in str(text).split("\n"):
            self.line(part)
        return self

    def push(self):
        self.level += 1
        return self

    def pop(self):
        assert self.level > 0, "indentation underflow"
        self.level -= 1
        return self

    def text(self):
        return "\n".join(self.lines) + "\n"

    @staticmethod
    def render(node):
        out = Emitter()
        node.emit(out)
        return out.text()


@dataclass(frozen=True)
class Generic:
    name: str
    type: str
    default: str | None = None
    comment: str | None = None

    def declaration(self):
        if self.default is None:
            return f"{self.name} : {self.type}"
        return f"{self.name} : {self.type} := {self.default}"


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    type: str
    default: str | None = None
    comment: str | None = None

    def declaration(self):
        head = f"{self.name} : {self.direction} {self.type}"
        if self.default is None:
            return head
        return f"{head} := {self.default}"


class InterfaceList:
    """Renders a generic or port clause, one interface element per line."""

    @staticmethod
    def emit_clause(out, keyword, elements):
        if not elements:
            return
        out.line(f"{keyword} (").push()
        last = len(elements) - 1
        for i, element in enumerate(elements):
            if element.comment:
                out.comment(element.comment)
            out.line(element.declaration() + ("" if i == last else ";"))
        out.line(");").pop()


@dataclass(frozen=True)
class Constant:
    name: str
    type: str
    value: str
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        lines = self.value.split("\n")
        if len(lines) == 1:
            out.line(f"constant {self.name} : {self.type} := {self.value};")
            return
        # A multi-line expression keeps its own line breaks, indented one level
        # under the declaration.
        out.line(f"constant {self.name} : {self.type} :=").push()
        for line in lines[:-1]:
            out.line(line)
        out.line(lines[-1] + ";").pop()


@dataclass(frozen=True)
class SignalDecl:
    name: str
    type: str
    init: str | None = None
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        tail = "" if self.init is None else f" := {self.init}"
        out.line(f"signal {self.name} : {self.type}{tail};")


@dataclass(frozen=True)
class Instance:
    label: str
    unit: str
    generic_map: dict = field(default_factory=dict)
    port_map: dict = field(default_factory=dict)
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.line(f"{self.label}: {self.unit}").push()
        self.__map(out, "generic map", self.generic_map,
                   ")" if self.port_map else ");")
        self.__map(out, "port map", self.port_map, ");")
        out.pop()

    @staticmethod
    def __map(out, keyword, associations, terminator):
        if not associations:
            return
        out.line(f"{keyword}(").push()
        items = list(associations.items())
        last = len(items) - 1
        for i, (formal, actual) in enumerate(items):
            out.line(f"{formal} => {actual}" + ("" if i == last else ","))
        out.line(terminator).pop()


@dataclass(frozen=True)
class Assignment:
    target: str
    value: str
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        lines = self.value.split("\n")
        if len(lines) == 1:
            out.line(f"{self.target} <= {self.value};")
            return
        # A multi-line expression keeps its own line breaks, indented one
        # level under the target.
        out.line(f"{self.target} <=").push()
        for line in lines[:-1]:
            out.line(line)
        out.line(lines[-1] + ";").pop()


@dataclass(frozen=True)
class Process:
    """A process whose body is carried as text."""

    label: str
    sensitivity: tuple
    body: str
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        head = f"{self.label}: " if self.label else ""
        out.line(f"{head}process({', '.join(self.sensitivity)})")
        out.line("begin").push()
        out.raw(self.body)
        out.pop().line("end process;")


@dataclass(frozen=True)
class RawStatement:
    """Fixed boilerplate carried verbatim into a declarative or statement
    part."""

    body: str
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.raw(self.body)


@dataclass(frozen=True)
class Comment:
    text: str

    def emit(self, out):
        out.comment(self.text)


class Blank:
    """A separator line between declaration groups."""

    def emit(self, out):
        out.line()


@dataclass(frozen=True)
class LibraryClause:
    names: tuple

    def emit(self, out):
        if self.names:
            out.line(f"library {', '.join(self.names)};")


@dataclass(frozen=True)
class UseClause:
    name: str

    def emit(self, out):
        out.line(f"use {self.name};")


@dataclass(frozen=True)
class ComponentDecl:
    name: str
    generics: tuple = ()
    ports: tuple = ()

    def emit(self, out):
        out.line(f"component {self.name} is").push()
        InterfaceList.emit_clause(out, "generic", self.generics)
        InterfaceList.emit_clause(out, "port", self.ports)
        out.pop().line("end component;")


@dataclass(frozen=True)
class Entity:
    name: str
    generics: tuple = ()
    ports: tuple = ()
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.line(f"entity {self.name} is").push()
        InterfaceList.emit_clause(out, "generic", self.generics)
        InterfaceList.emit_clause(out, "port", self.ports)
        out.pop().line("end entity;")

    def component(self):
        return ComponentDecl(self.name, self.generics, self.ports)


@dataclass(frozen=True)
class Architecture:
    name: str
    entity: str
    declarations: tuple = ()
    statements: tuple = ()

    def emit(self, out):
        out.line(f"architecture {self.name} of {self.entity} is").push()
        for declaration in self.declarations:
            out.line()
            declaration.emit(out)
        out.pop()
        out.line()
        out.line("begin")
        for statement in self.statements:
            out.line()
            out.push()
            statement.emit(out)
            out.pop()
        out.line()
        out.line("end architecture;")


@dataclass(frozen=True)
class FunctionDecl:
    """A function header: what a package declares, and what a body repeats."""

    name: str
    parameters: tuple = ()
    return_type: str = "byte_string"
    comment: str | None = None

    def header(self):
        """The header. A parameter is the generic it stands for, minus its
        default: the caller always states one, and a second default here could
        only ever disagree with the entity's."""
        if not self.parameters:
            return f"function {self.name} return {self.return_type}"
        body = ";\n".join(f"  {p.name} : {p.type}" for p in self.parameters)
        return f"function {self.name}(\n{body}) return {self.return_type}"

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.raw(self.header() + ";")

    def call(self, arguments=()):
        return Expr.call(self.name, *arguments)


@dataclass(frozen=True)
class FunctionBody:
    """A function definition: its header, its declarative part, and the one
    expression it returns."""

    declaration: FunctionDecl
    declarations: tuple = ()
    expression: str = ""

    def emit(self, out):
        out.raw(self.declaration.header() + " is").push()
        for item in self.declarations:
            item.emit(out)
        out.pop().line("begin").push()
        lines = self.expression.split("\n")
        if len(lines) == 1:
            out.line(f"return {self.expression};")
        else:
            out.line("return").push()
            for line in lines[:-1]:
                out.line(line)
            out.line(lines[-1] + ";").pop()
        out.pop().line("end function;")


@dataclass(frozen=True)
class Package:
    name: str
    declarations: tuple = ()
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.line(f"package {self.name} is").push()
        for declaration in self.declarations:
            out.line()
            declaration.emit(out)
        out.pop()
        out.line()
        out.line("end package;")


@dataclass(frozen=True)
class PackageBody:
    name: str
    declarations: tuple = ()
    comment: str | None = None

    def emit(self, out):
        if self.comment:
            out.comment(self.comment)
        out.line(f"package body {self.name} is").push()
        for declaration in self.declarations:
            out.line()
            declaration.emit(out)
        out.pop()
        out.line()
        out.line("end package body;")


@dataclass(frozen=True)
class DesignFile:
    """A whole VHDL source file: header comment, context clauses, units."""

    clauses: tuple = ()
    units: tuple = ()
    header: str | None = None

    def emit(self, out):
        if self.header:
            out.comment(self.header)
            out.line()
        for clause in self.clauses:
            clause.emit(out)
        for unit in self.units:
            out.line()
            unit.emit(out)

    def render(self):
        return Emitter.render(self)

    @staticmethod
    def context(libraries=(), uses=()):
        """Standard clauses: ieee first, then the given libraries and uses."""
        clauses = [LibraryClause(("ieee",)),
                   UseClause("ieee.std_logic_1164.all"),
                   UseClause("ieee.numeric_std.all")]
        extra = tuple(name for name in libraries if name != "ieee")
        if extra:
            clauses += [Blank(), LibraryClause(extra)]
        clauses += [UseClause(name) for name in uses]
        return tuple(clauses)

    @staticmethod
    def libraries_of(deps):
        """Library names behind gbs dependency keys, in first-seen order."""
        seen = []
        for dep in deps:
            library = dep.split(".", 1)[0]
            if library not in seen:
                seen.append(library)
        return tuple(seen)
